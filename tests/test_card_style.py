"""The card style send path: escaping, safe chunking and the plain text fallback."""

from __future__ import annotations

import re

import bot
from conftest import OWNER, send
from fake_bot_api import check_message

HTML = {"parse_mode": "HTML"}


def balanced(message: str) -> bool:
    depth = 0
    for close in bot.TAG_RE.findall(message):
        depth += -1 if close else 1
        if depth < 0:
            return False
    return depth == 0


def test_card_escapes_every_dynamic_value():
    blocks = bot.card("coach", "a <b> & c", "x < y & z > w", hint="<i>hint</i>", background="1 < 2 & 3")
    text = "\n\n".join(blocks)
    assert blocks[0] == "💬 <b>COACH</b> · a &lt;b&gt; &amp; c"
    assert "x &lt; y &amp; z &gt; w" in text and "<i>&lt;i&gt;hint&lt;/i&gt;</i>" in text
    assert "━━━━━━━━━━━━━━━━\n<blockquote expandable>1 &lt; 2 &amp; 3</blockquote>" in text
    assert check_message({"text": text, **HTML}) is None


async def test_claude_answer_is_escaped_inside_the_card(app, claude):
    claude.enqueue({"result": "Use <b>bold</b> & 5 < 6, see <a href='x'>this</a>"})
    texts = await send(app, "/coach anything")
    assert texts[0].startswith("💬 <b>COACH</b>\n\n")
    assert "Use &lt;b&gt;bold&lt;/b&gt; &amp; 5 &lt; 6, see &lt;a href='x'&gt;this&lt;/a&gt;" in texts[0]
    assert check_message({"text": texts[0], **HTML}) is None


def test_chunking_never_splits_a_tag():
    quote = "<blockquote expandable>" + "\n".join(f"💡 cue line {i} &amp; more" for i in range(400)) + "</blockquote>"
    lines = "\n".join(f"🏋️ <b>Move {i}</b> · <code>3 × 10</code> @ <b>{i} kg</b>" for i in range(300))
    items = [f"🏋️ <b>Item {i}</b> · " + "word " * 60 for i in range(40)]
    blocks = [bot.header("week", "big"), *items, lines, quote]
    messages = bot.pack_blocks(blocks)
    assert len(messages) > 3
    for message in messages:
        assert balanced(message), message[:200]
        if message is not messages[-1]:  # the one giant quote is too long by design
            assert check_message({"text": message, **HTML}) is None
    # an oversized block of tagged lines is cut between lines, each piece valid on its own
    pieces = bot.split_block(lines)
    assert len(pieces) > 1 and "\n".join(pieces) == lines
    assert all(check_message({"text": p, **HTML}) is None for p in pieces)
    # a single tag longer than the limit is kept whole rather than cut inside
    assert bot.split_block(quote) == [quote]


async def test_rejected_html_is_resent_as_plain_text(app):
    await send(app, "/log <b>test</b> & more")
    app.tg.reject_html = True
    before = len(app.tg.sent())
    await send(app, "/log")
    attempts = app.tg.sent()[before:]
    sent = [p for p in attempts if "parse_mode" not in p]
    assert [p.get("parse_mode") for p in attempts] == ["HTML", None]  # rejected once, then plain
    text = "\n".join(p["text"] for p in sent)
    assert text.startswith("📝 WORKOUT LOG · your logs from the last 2 weeks")
    assert "<b>test</b> & more" in text  # the user's own text, unescaped, nothing lost
    assert not re.search(r"&(lt|gt|amp);", text)
    assert int(sent[0]["chat_id"]) == OWNER


async def test_a_huge_message_falls_back_without_losing_text(app, claude):
    app.tg.reject_html = True
    claude.enqueue({"result": "\n".join(f"line {i} " + "x" * 80 for i in range(200))})
    texts = await send(app, "/coach long")
    assert len(texts) >= 2 and all(bot._tg_len(t) <= 4096 for t in texts)
    joined = "\n".join(texts)
    assert "line 0 " in joined and "line 199 " in joined
