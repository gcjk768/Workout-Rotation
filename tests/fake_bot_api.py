"""A small stand-in for Telegram's Bot API, for end-to-end tests of the real bot process.

The bot talks to it exactly like to api.telegram.org (set TELEGRAM_BASE_URL to its address):
long polling with getUpdates, sendMessage, editMessageText, setMyCommands and so on.
Tests queue incoming updates and read back every call the bot made.
"""

from __future__ import annotations

import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

# Tags Telegram accepts in HTML parse mode, and the entities it understands.
TELEGRAM_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre",
                 "tg-spoiler", "span", "blockquote", "tg-emoji"}
BAD_ENTITY_RE = re.compile(r"&(?!(?:lt|gt|amp|quot|#\d+|#x[0-9a-fA-F]+);)")


class _TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text, self.error = [], [], None

    def handle_starttag(self, tag, attrs):
        if tag not in TELEGRAM_TAGS:
            self.error = self.error or f"unsupported start tag \"{tag}\""
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            self.error = self.error or f"can't find end tag corresponding to start tag \"{tag}\""
        else:
            self.stack.pop()

    def handle_data(self, data):
        self.text.append(data)


def check_message(params: dict) -> str | None:
    """What Telegram would say about this message, or None when it would accept it."""
    text = str(params.get("text", ""))
    visible = text
    if params.get("parse_mode") == "HTML":
        if BAD_ENTITY_RE.search(text):
            return "Bad Request: can't parse entities: unsupported entity"
        parser = _TelegramHTML()
        parser.feed(text)
        parser.close()
        if parser.error or parser.stack:
            return f"Bad Request: can't parse entities: {parser.error or 'unclosed tag'}"
        visible = "".join(parser.text)
    if not visible.strip():
        return "Bad Request: message text is empty"
    if len(visible.encode("utf-16-le")) // 2 > 4096:
        return "Bad Request: message is too long"
    markup = params.get("reply_markup") or {}
    for row in markup.get("inline_keyboard", []) if isinstance(markup, dict) else []:
        for button in row:
            if len(str(button.get("callback_data", "")).encode()) > 64:
                return "Bad Request: BUTTON_DATA_INVALID"
    return None

BOT_USER = {
    "id": 4242,
    "is_bot": True,
    "first_name": "Coach",
    "username": "coach_e2e_bot",
    "can_join_groups": False,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}


def _value(raw: str):
    """PTB sends every parameter as a form field; lists and objects are JSON encoded."""
    if raw[:1] in "[{":
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


class FakeBotAPI:
    def __init__(self, token: str):
        self.token = token
        self.calls: list[tuple[str, dict]] = []
        self.errors: list[tuple[str, str]] = []  # messages Telegram would have rejected
        self._updates: list[dict] = []
        self._next_update = 1
        self._next_message = 500
        self._cond = threading.Condition()
        self._server: ThreadingHTTPServer | None = None

    # -- server ---------------------------------------------------------------

    def start(self, port: int = 0, host: str = "127.0.0.1") -> int:
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode() if length else ""
                if "json" in (self.headers.get("Content-Type") or ""):
                    params = json.loads(body or "{}")
                else:
                    params = {k: _value(v[-1]) for k, v in parse_qs(body, keep_blank_values=True).items()}
                status, payload = api.handle(self.path, params)
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the bot closed a long poll while shutting down

            do_GET = do_POST

        self._server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self._server.server_address[1]

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def handle(self, path: str, params: dict) -> tuple[int, dict]:
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return 401, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        method = path[len(prefix):]
        with self._cond:
            self.calls.append((method, params))
            self._cond.notify_all()
        if method == "getMe":
            return 200, {"ok": True, "result": BOT_USER}
        if method == "getUpdates":
            return 200, {"ok": True, "result": self._get_updates(params)}
        if method in ("sendMessage", "editMessageText"):
            problem = check_message(params)
            if problem:
                self.errors.append((problem, str(params.get("text", ""))[:200]))
                return 400, {"ok": False, "error_code": 400, "description": problem}
            with self._cond:
                self._next_message += 1
                message_id = int(params.get("message_id") or self._next_message)
            chat_id = int(params["chat_id"])
            return 200, {
                "ok": True,
                "result": {
                    "message_id": message_id,
                    "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": BOT_USER,
                    "text": params.get("text", ""),
                },
            }
        return 200, {"ok": True, "result": True}

    def _get_updates(self, params: dict) -> list[dict]:
        offset = int(params.get("offset") or 0)
        wait = min(float(params.get("timeout") or 0), 1.0)
        deadline = time.time() + wait
        with self._cond:
            while True:
                self._updates = [u for u in self._updates if u["update_id"] >= offset]
                if self._updates or time.time() >= deadline:
                    return list(self._updates)
                self._cond.wait(deadline - time.time())

    # -- incoming updates -------------------------------------------------------

    def _push(self, update: dict) -> None:
        with self._cond:
            update["update_id"] = self._next_update
            self._next_update += 1
            self._updates.append(update)
            self._cond.notify_all()

    def message(self, text: str, user_id: int, chat_id: int | None = None, chat_type: str = "private") -> None:
        chat_id = user_id if chat_id is None else chat_id
        with self._cond:
            self._next_message += 1
            message_id = self._next_message
        msg = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "the owner"},
            "text": text,
        }
        if text.startswith("/"):
            msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        self._push({"message": msg})

    def press(self, data: str, user_id: int, message_id: int) -> None:
        self._push(
            {
                "callback_query": {
                    "id": f"cb{time.time_ns()}",
                    "from": {"id": user_id, "is_bot": False, "first_name": "the owner"},
                    "chat_instance": "e2e",
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "date": int(time.time()),
                        "chat": {"id": user_id, "type": "private"},
                        "from": BOT_USER,
                        "text": "buttons",
                    },
                }
            }
        )

    # -- reading what the bot did --------------------------------------------------

    def wait_for(self, predicate, timeout: float = 20.0, after: int = 0) -> tuple[str, dict]:
        """Wait for a call (method, params) matching predicate, among calls[after:]."""
        deadline = time.time() + timeout
        with self._cond:
            while True:
                for method, params in self.calls[after:]:
                    if predicate(method, params):
                        return method, params
                left = deadline - time.time()
                if left <= 0:
                    recent = [(m, str(p.get("text", ""))[:80]) for m, p in self.calls[-8:]]
                    raise AssertionError(f"timed out waiting for a call; recent calls: {recent}")
                self._cond.wait(left)

    def reply(self, chat_id: int, contains: str, timeout: float = 20.0, after: int = 0) -> dict:
        _, params = self.wait_for(
            lambda m, p: m in ("sendMessage", "editMessageText")
            and int(p.get("chat_id", 0)) == chat_id
            and contains in str(p.get("text", "")),
            timeout,
            after,
        )
        return params

    def mark(self) -> int:
        with self._cond:
            return len(self.calls)
