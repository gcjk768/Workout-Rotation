"""Test kit: a fake Telegram API, a fake `claude` on PATH and a controllable clock.

Nothing here needs a real bot token, Claude token or network access.
"""

from __future__ import annotations

import itertools
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from telegram import Update
from telegram.request import BaseRequest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

FAKE_BIN = Path(__file__).resolve().parent / "bin"
BOT_TOKEN = "123456789:AAFakeTokenForTestsOnly_abcdefghijklmnopq"
OWNER = 111111
STRANGER = 999999


class FakeTelegram(BaseRequest):
    """Records every Bot API call and answers like Telegram would."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.reject_html = False
        self.blocked_chats: set[int] = set()  # chats where Telegram answers 403
        self._ids = itertools.count(1000)

    @property
    def read_timeout(self) -> float | None:
        return 5.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((api, params))
        if api == "getMe":
            result = {"id": 42, "is_bot": True, "first_name": "Coach", "username": "coach_test_bot",
                      "can_join_groups": True, "can_read_all_group_messages": False,
                      "supports_inline_queries": False}
        elif api in ("sendMessage", "editMessageText"):
            if int(params.get("chat_id", 0)) in self.blocked_chats:
                return 403, json.dumps({"ok": False, "error_code": 403,
                                        "description": "Forbidden: bot was blocked by the user"}).encode()
            if self.reject_html and params.get("parse_mode") == "HTML":
                return 400, json.dumps({"ok": False, "error_code": 400,
                                        "description": "Bad Request: can't parse entities"}).encode()
            chat_id = int(params.get("chat_id", OWNER))
            result = {
                "message_id": int(params.get("message_id") or next(self._ids)),
                "date": 1759200000,
                "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "group"},
                "text": params.get("text", ""),
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    # -- helpers for assertions -----------------------------------------------

    def sent(self, api: str = "sendMessage") -> list[dict]:
        return [p for name, p in self.calls if name == api]

    def texts(self) -> list[str]:
        return [p["text"] for p in self.sent()]

    def clear(self) -> None:
        self.calls.clear()


class Clock:
    def __init__(self, tz):
        self.tz = tz
        self.value = datetime(2026, 9, 30, 12, 0, tzinfo=tz)  # a Wednesday

    def set(self, *args) -> datetime:
        self.value = datetime(*args, tzinfo=self.tz)
        return self.value

    def __call__(self, tz):
        return self.value.astimezone(tz)


def base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "ALLOWED_USER_IDS": f"{OWNER}, 222222",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE",
        "CLAUDE_TOKEN_CREATED": "2026-01-15",
        "DATA_DIR": str(tmp_path / "data"),
        "CLAUDE_WORK_DIR": str(tmp_path / "work"),
        "GARMIN_DB": str(tmp_path / "garmin" / "monitor.db"),
        "AGE": "31",
        "HEIGHT_CM": "183",
        "WEIGHT_KG": "78",
        "EXPERIENCE": "intermediate",
        "SESSION_MINUTES": "60",
        "INJURY_NOTES": "Left shoulder micro tear. Not cleared for heavy loading.",
        "BASKETBALL_DAYS": "",
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    values = base_env(tmp_path)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("PATH", f"{FAKE_BIN}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "claude_calls.jsonl"))
    monkeypatch.setenv("FAKE_CLAUDE_QUEUE", str(tmp_path / "claude_queue.json"))
    monkeypatch.delenv("FAKE_CLAUDE_MODE", raising=False)
    monkeypatch.delenv("FAKE_CLAUDE_AUTH", raising=False)
    return values


@pytest.fixture
def cfg(env):
    return bot.Config.from_env()


@pytest.fixture
def clock(cfg, monkeypatch):
    c = Clock(cfg.tz)
    monkeypatch.setattr(bot, "now_in", c)
    return c


class ClaudeCalls:
    def __init__(self, tmp_path: Path):
        self.log = tmp_path / "claude_calls.jsonl"
        self.queue = tmp_path / "claude_queue.json"

    def all(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def last(self) -> dict:
        return self.all()[-1]

    def plan_calls(self) -> list[dict]:
        return [c for c in self.all() if "--tools" in c["argv"] and c["argv"][c["argv"].index("--tools") + 1] == ""]

    def enqueue(self, *entries: dict) -> None:
        existing = json.loads(self.queue.read_text()) if self.queue.exists() else []
        self.queue.write_text(json.dumps(existing + list(entries)))


@pytest.fixture
def claude(tmp_path):
    return ClaudeCalls(tmp_path)


@pytest.fixture
def coach(cfg, clock):
    return bot.Coach(cfg)


@pytest_asyncio.fixture
async def app(cfg, clock):
    tg = FakeTelegram()
    application = bot.build_application(cfg, request=tg, updates_request=FakeTelegram(), concurrent=False)
    await application.initialize()
    application.tg = tg
    try:
        yield application
    finally:
        await application.shutdown()


_update_ids = itertools.count(1)
_message_ids = itertools.count(1)


def message_update(app, text: str, user_id: int = OWNER, chat_id: int | None = None,
                   chat_type: str = "private", reply_to: int | None = None) -> Update:
    chat_id = user_id if chat_id is None else chat_id
    message = {
        "message_id": next(_message_ids),
        "date": int(bot.now_in(app.bot_data["coach"].cfg.tz).timestamp()),
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "the owner"},
        "text": text,
    }
    if text.startswith("/"):
        length = len(text.split()[0])
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": length}]
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to,
            "date": message["date"],
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": 42, "is_bot": True, "first_name": "Coach"},
            "text": "earlier bot message",
        }
    return Update.de_json({"update_id": next(_update_ids), "message": message}, app.bot)


def edited_update(app, text: str, user_id: int = OWNER) -> Update:
    data = message_update(app, text, user_id=user_id).to_dict()
    msg = data.pop("message")
    msg["edit_date"] = msg["date"] + 5
    data["edited_message"] = msg
    return Update.de_json(data, app.bot)


def callback_update(app, data: str, user_id: int = OWNER, message_id: int = 5000) -> Update:
    payload = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_update_ids)),
            "from": {"id": user_id, "is_bot": False, "first_name": "the owner"},
            "chat_instance": "ci",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": 1759200000,
                "chat": {"id": user_id, "type": "private"},
                "from": {"id": 42, "is_bot": True, "first_name": "Coach"},
                "text": "buttons",
            },
        },
    }
    return Update.de_json(payload, app.bot)


async def send(app, text: str, **kwargs) -> list[str]:
    """Process one incoming message and return the texts the bot sent back."""
    before = len(app.tg.sent())
    await app.process_update(message_update(app, text, **kwargs))
    return [p["text"] for p in app.tg.sent()[before:]]


async def press(app, data: str, **kwargs) -> list[dict]:
    before = len(app.tg.calls)
    await app.process_update(callback_update(app, data, **kwargs))
    return app.tg.calls[before:]
