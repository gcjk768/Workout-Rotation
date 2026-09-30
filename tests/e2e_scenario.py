"""End-to-end scenario: drive a running bot process through the stand-in Bot API.

Used by tests/test_e2e_process.py (bot started as a local process) and by hand against the
Docker container:

    python tests/e2e_scenario.py --serve 8089     # then start the container with
                                                  # TELEGRAM_BASE_URL=http://127.0.0.1:8089
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_bot_api import FakeBotAPI  # noqa: E402

TOKEN = "123456789:AAE2eFakeTokenForEndToEndTests_abcdefgh"
OWNER = 111111
STRANGER = 999999


def buttons(params: dict) -> list[str]:
    markup = params.get("reply_markup") or {}
    if isinstance(markup, str):
        markup = json.loads(markup)
    return [b["callback_data"] for row in markup.get("inline_keyboard", []) for b in row]


def run(api: FakeBotAPI, expect_garmin: bool = True, claude_version: str = "2.1.999", real_claude: bool = False) -> list[str]:
    """Runs every check and returns a list of what passed. Raises AssertionError on failure.

    real_claude: the bot uses the real Claude Code, so answers are not canned and take longer."""
    done = []
    slow = 8 if real_claude else 1

    # start up: command menu, webhook removed, long polling for messages and buttons only
    _, cmds = api.wait_for(lambda m, p: m == "setMyCommands", 60)
    names = [c["command"] for c in cmds["commands"]]
    assert names == ["ask", "today", "week", "plan", "nextweek", "log", "done", "shoulder",
                     "injury", "profile", "status", "reset", "whoami"], names
    done.append("command menu registered (13 commands)")
    _, poll = api.wait_for(lambda m, p: m == "getUpdates" and "allowed_updates" in p, 60)
    assert sorted(poll["allowed_updates"]) == ["callback_query", "message"], poll
    done.append("long polling for messages and button presses")

    # strangers only learn their ID
    mark = api.mark()
    api.message("/injury I am cleared for everything", STRANGER)
    api.reply(STRANGER, f"Your Telegram user ID is <code>{STRANGER}</code>", after=mark)
    done.append("stranger gets only their ID")

    mark = api.mark()
    api.message("/injury", OWNER)
    notes = api.reply(OWNER, "njury notes", after=mark)["text"]
    assert "cleared for everything" not in notes, notes
    done.append("the stranger's /injury changed nothing")

    mark = api.mark()
    api.message("/whoami", OWNER)
    api.reply(OWNER, f"<code>{OWNER}</code>", after=mark)
    done.append("/whoami")

    mark = api.mark()
    if real_claude:
        api.message("/ask Find one YouTube video on face pulls with good form and give me its direct link.", OWNER)
        reply = api.reply(OWNER, "youtube.com/watch", timeout=300, after=mark)
        assert reply.get("link_preview_options", {}).get("url", "").startswith("https://www.youtube.com/watch"), reply
        done.append("/ask answered by real Claude with web search, direct video link and preview")
    else:
        api.message("/ask What should I eat before training?", OWNER)
        reply = api.reply(OWNER, "search_query=", timeout=60, after=mark)
        done.append("/ask answered in HTML with a working YouTube search link")
    assert reply.get("parse_mode") == "HTML", reply

    mark = api.mark()
    api.message("/plan", OWNER)
    api.reply(OWNER, "Building this week's plan", after=mark)
    plan = api.reply(OWNER, "📅 Monday", timeout=120 * slow, after=mark)
    assert "Week 1" in plan["text"], plan["text"][:200]
    done.append("/plan built and sent week 1")

    mark = api.mark()
    api.message("/today", OWNER)
    api.reply(OWNER, "📅", after=mark)
    done.append("/today")

    mark = api.mark()
    api.message("/log rows 22kg 3x10, floor press 14kg 3x8 felt easy", OWNER)
    api.reply(OWNER, "Logged for", after=mark)
    done.append("/log")

    mark = api.mark()
    api.message("/done", OWNER)
    done_msg = api.reply(OWNER, "marked done", after=mark)
    rate = buttons(done_msg)
    assert len(rate) == 11 and rate[3].endswith(":3"), rate
    mark = api.mark()
    api.press(rate[3], OWNER, message_id=int(done_msg.get("message_id", 1) or 1))
    api.reply(OWNER, "Left shoulder 3/10 saved", after=mark)
    api.reply(OWNER, "Trend: Latest 3/10", after=mark)
    done.append("/done with 0 to 10 buttons, rating saved")

    mark = api.mark()
    api.message("/shoulder", OWNER)
    api.reply(OWNER, "3/10 (after session)", after=mark)
    done.append("/shoulder log")

    mark = api.mark()
    api.message("/status", OWNER)
    status = api.reply(OWNER, "Next reminders", timeout=60, after=mark)["text"]
    assert f"Version: {claude_version}" in status, status
    assert "Session reminder" in status and "Sunday check in" in status, status
    if expect_garmin:
        assert "✅ Profile Me: latest data" in status, status
    done.append("/status (Claude version, sign in, reminders" + (", Garmin)" if expect_garmin else ")"))

    mark = api.mark()
    api.message("/profile", OWNER)
    profile = api.reply(OWNER, "About you", after=mark)["text"]
    assert "Latest 3/10" in profile, profile
    done.append("/profile with shoulder trend")

    mark = api.mark()
    api.message("/today", OWNER, chat_id=-100777, chat_type="group")
    api.message("/whoami", OWNER)  # handled after the group message, so its reply proves the order
    api.reply(OWNER, f"<code>{OWNER}</code>", after=mark)
    assert not [c for c in api.calls[mark:] if c[0] == "sendMessage" and int(c[1].get("chat_id", 0)) == -100777]
    done.append("group messages ignored")

    assert not api.errors, f"Telegram would have rejected: {api.errors}"
    done.append("every message was valid for Telegram (HTML, length, buttons)")
    return done


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=int, required=True, help="port for the stand-in Bot API")
    parser.add_argument("--no-garmin", action="store_true")
    parser.add_argument("--claude-version", default="2.1.999")
    parser.add_argument("--real-claude", action="store_true", help="the bot uses the real Claude Code")
    args = parser.parse_args()
    api = FakeBotAPI(TOKEN)
    api.start(args.serve, "0.0.0.0")
    print(f"stand-in Bot API on port {args.serve}, waiting for the bot...", flush=True)
    for line in run(api, expect_garmin=not args.no_garmin, claude_version=args.claude_version, real_claude=args.real_claude):
        print("PASS", line, flush=True)
    print("ALL CHECKS PASSED", flush=True)
    api.stop()


if __name__ == "__main__":
    main()
