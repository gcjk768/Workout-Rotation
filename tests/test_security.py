"""Security and robustness: the allow-list gate, edits, groups and overlapping builds."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from telegram.error import NetworkError

import bot
from conftest import OWNER, STRANGER, edited_update, press, send


async def test_stranger_is_stopped_even_when_the_reply_fails(app, claude):
    """A stranger who blocks the bot must not get their command run."""
    coach = app.bot_data["coach"]
    before = coach.injury_text()
    app.tg.blocked_chats.add(STRANGER)
    for text in ("/injury Physio cleared me for heavy overhead pressing", "/log fake", "/ask spend quota", "/plan"):
        await send(app, text, user_id=STRANGER)
        app.bot_data.pop("stranger_replies", None)  # defeat the rate limit so every reply is tried
    assert coach.injury_text() == before
    assert coach.store.logs() == []
    assert claude.all() == []


async def test_groups_are_ignored_for_everyone(app, claude):
    for user in (OWNER, STRANGER):
        texts = await send(app, "/today", user_id=user, chat_id=-100555, chat_type="supergroup")
        assert texts == []
        texts = await send(app, "/injury none", user_id=user, chat_id=-100555, chat_type="supergroup")
        assert texts == []
    assert "Left shoulder" in app.bot_data["coach"].injury_text()[0]
    assert claude.all() == []


async def test_edited_messages_do_not_run_again(app, claude):
    await app.process_update(edited_update(app, "/log rows 20kg 3x10"))
    await app.process_update(edited_update(app, "what about tomorrow?"))
    assert app.bot_data["coach"].store.logs() == []
    assert claude.all() == []
    assert app.tg.sent() == []


async def test_stranger_buttons_are_refused(app):
    calls = await press(app, "rate:2026-09-30:9", user_id=STRANGER)
    assert [n for n, _ in calls] == ["answerCallbackQuery"]
    assert app.bot_data["coach"].store.ratings() == []


async def test_out_of_range_rating_is_ignored(app):
    await press(app, "rate:2026-09-30:99")
    await press(app, "rate:2026-09-30:-3")
    assert app.bot_data["coach"].store.ratings() == []


async def test_scheduled_build_keeps_a_plan_built_while_it_waited(app, claude, clock):
    coach = app.bot_data["coach"]
    await send(app, "/plan")
    clock.set(2026, 10, 4, 20, 0)
    ctx = SimpleNamespace(bot=app.bot, application=app, job=None)
    await coach.plan_lock.acquire()  # the user's /nextweek is still running
    task = asyncio.create_task(bot.build_next_week(ctx, date(2026, 10, 5)))
    await asyncio.sleep(0.05)
    coach.store.save_plan(date(2026, 10, 5), "📅 Monday: Push\nuser plan", {"built_at": "2026-10-04T19:59:30+08:00", "week": 2}, "x")
    calls = len(claude.plan_calls())
    coach.plan_lock.release()
    await task
    assert len(claude.plan_calls()) == calls  # nothing rebuilt
    assert "user plan" in coach.store.load_plan(date(2026, 10, 5))
    assert app.tg.texts()[-1].startswith("🗓 Next week's plan was already built.")


async def test_leftover_prompt_files_are_removed_at_start(app):
    leftover = Path(tempfile.gettempdir()) / "coach-system-leftover-test.md"
    leftover.write_text("health data")
    await bot.post_init(app)
    assert not leftover.exists()


async def test_telegram_network_errors_are_logged_quietly(app, caplog):
    ctx = SimpleNamespace(bot=app.bot, error=NetworkError("httpx.ConnectError: down"))
    await bot.on_error(None, ctx)
    assert app.tg.sent() == []
    assert any(r.levelname == "WARNING" and "retrying" in r.getMessage() for r in caplog.records)


def test_config_repr_hides_tokens(cfg):
    text = repr(cfg)
    assert cfg.telegram_token not in text
    assert "FAKEFAKE" not in text


async def test_bot_chat_topic_is_home_and_other_topics_are_ignored(cfg, clock, claude, monkeypatch):
    from conftest import FakeTelegram, message_update

    monkeypatch.setattr(cfg, "bot_chat", (-100555, 3038))
    app = bot.build_application(cfg, request=(tg := FakeTelegram()), updates_request=FakeTelegram(), concurrent=False)
    await app.initialize()
    app.tg = tg
    try:
        def in_topic(text, thread, user=OWNER):
            u = message_update(app, text, user_id=user, chat_id=-100555, chat_type="supergroup").to_dict()
            u["message"].update(message_thread_id=thread, is_topic_message=True)
            return bot.Update.de_json(u, app.bot)

        await app.process_update(in_topic("/whoami", 2930))  # another bot's topic
        await app.process_update(in_topic("/whoami", 3038, user=STRANGER))
        assert app.tg.sent() == []
        await app.process_update(in_topic("/whoami", 3038))
        [msg] = app.tg.sent()
        assert int(msg["chat_id"]) == -100555 and int(msg["message_thread_id"]) == 3038
        await bot.owner_send(SimpleNamespace(bot=app.bot, application=app), "reminder")  # scheduled sends go there too
        assert int(app.tg.sent()[-1]["message_thread_id"]) == 3038
    finally:
        await app.shutdown()


def test_week_split_rotates_the_upper_body_days():
    assert [bot.week_split(1)[d] for d in range(3)] == ["Chest", "Back", "Shoulders"]
    assert [bot.week_split(2)[d] for d in range(3)] == ["Back", "Shoulders", "Arms"]
    assert [bot.week_split(3)[d] for d in range(3)] == ["Shoulders", "Arms", "Chest"]
    assert bot.week_split(5) == bot.week_split(1)
    assert bot.week_split(2)[3] == "Legs or run" and bot.week_split(2)[4] == "Run or swim"
