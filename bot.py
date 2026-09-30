#!/usr/bin/env python3
"""Private Telegram gym coach bot.

Runs on a NAS in Docker, talks to Telegram with long polling and uses Claude Code in
headless mode (`claude -p`) for every AI answer and weekly plan. Everything the bot
remembers lives in /data as plain files (plans, logs, ratings, check ins, state).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import html
import json
import logging
import os
import re
import signal
import tempfile
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    TypeHandler,
    filters,
)

log = logging.getLogger("coach")

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAY_LOOKUP = {name.lower(): i for i, name in enumerate(DAY_NAMES)}
DAY_LOOKUP.update({name[:3].lower(): i for i, name in enumerate(DAY_NAMES)})
DAY_LOOKUP.update({"tues": 1, "wed": 2, "thur": 3, "thurs": 3})

MAX_MESSAGE = 4096
MEMORY_TURNS = 6


def now_in(tz: ZoneInfo) -> datetime:
    """Current time in the bot's zone. Tests replace this to travel in time."""
    return datetime.now(tz)


# ---------------------------------------------------------------------------
# Settings (bot.env)
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    pass


def _clean(value: str | None) -> str:
    value = (value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def parse_list(value: str | None) -> list[str]:
    return [item.strip() for item in _clean(value).split(",") if item.strip()]


def parse_days(value: str | None) -> list[int]:
    """'Mon,Tue' or 'Mon-Fri' or 'Saturday' -> sorted weekday numbers (Monday = 0)."""
    days: set[int] = set()
    for part in parse_list(value):
        if "-" in part:
            start, _, end = part.partition("-")
            a, b = DAY_LOOKUP.get(start.strip().lower()), DAY_LOOKUP.get(end.strip().lower())
            if a is None or b is None:
                raise ConfigError(f"Unknown day range '{part}'")
            i = a
            while True:
                days.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
            continue
        idx = DAY_LOOKUP.get(part.lower())
        if idx is None:
            raise ConfigError(f"Unknown day '{part}'")
        days.add(idx)
    return sorted(days)


def parse_time(value: str | None, default: str) -> dtime:
    raw = _clean(value) or default
    m = re.fullmatch(r"(\d{1,2})[:.](\d{2})", raw)
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ConfigError(f"Time '{raw}' should look like 17:30")
    return dtime(int(m.group(1)), int(m.group(2)))


def parse_date(value: str | None) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"Date '{raw}' should look like 2026-10-01") from exc


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


DEFAULT_BLOCKED = (
    "overhead press, shoulder press, military press, push press, arnold press, z press, "
    "clean and press, behind the neck, upright row, dip, wide grip bench, barbell bench, "
    "bench press, fly, flye, flies, pec deck, overhead tricep extension, "
    "overhead triceps extension, overhead carry, overhead squat, snatch, jerk, thruster, "
    "handstand, kipping"
)
DEFAULT_ALLOWED = (
    "reverse * fly, reverse * flye, reverse * flies, rear delt * fly, rear delt * flye, "
    "rear delt * flies, dumbbell * bench press, db * bench press"
)
DEFAULT_ROTATION = "dumbbells, cables, machines, barbell and kettlebells"


@dataclasses.dataclass
class Config:
    telegram_token: str
    allowed_ids: list[int]
    tz: ZoneInfo
    data_dir: Path
    work_dir: Path
    claude_bin: str
    model_ask: str
    model_plan: str
    token_created: date | None
    age: str
    height_cm: str
    weight_kg: str
    goal: str
    experience: str
    session_minutes: str
    equipment: str
    gym_time_mon_thu: str
    gym_time_fri: str
    injury_notes: str
    basketball_days: list[int]
    equipment_rotation: list[str]
    program_start: date | None
    training_days: list[int]
    blocked_movements: list[str]
    allowed_movements: list[str]
    reminder_mon_thu: dtime
    reminder_fri: dtime
    check_time: dtime
    checkin_time: dtime
    plan_time: dtime
    token_check_time: dtime
    garmin_db: str
    garmin_profile: str
    garmin_days: int
    ask_timeout: int = 240
    plan_timeout: int = 600
    secrets: list[str] = dataclasses.field(default_factory=list)

    @property
    def owner_id(self) -> int | None:
        """Reminders go to the first ID in ALLOWED_USER_IDS."""
        return self.allowed_ids[0] if self.allowed_ids else None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        env = dict(os.environ if env is None else env)

        def get(key: str, default: str = "") -> str:
            return _clean(env.get(key)) or default

        token = get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is missing in bot.env")
        try:
            allowed = [int(x) for x in parse_list(env.get("ALLOWED_USER_IDS"))]
        except ValueError as exc:
            raise ConfigError("ALLOWED_USER_IDS must be numbers separated by commas") from exc
        try:
            tz = ZoneInfo(get("TZ", "Asia/Singapore"))
        except Exception as exc:  # noqa: BLE001 - any bad zone name
            raise ConfigError(f"Unknown TZ '{get('TZ')}'") from exc
        rotation = parse_list(env.get("EQUIPMENT_ROTATION")) or parse_list(DEFAULT_ROTATION)
        program_start = parse_date(env.get("PROGRAM_START"))
        if program_start:
            program_start = monday_of(program_start)
        try:
            garmin_days = int(get("GARMIN_DAYS", "7"))
        except ValueError as exc:
            raise ConfigError("GARMIN_DAYS must be a number") from exc
        return cls(
            telegram_token=token,
            allowed_ids=allowed,
            tz=tz,
            data_dir=Path(get("DATA_DIR", "/data")),
            work_dir=Path(get("CLAUDE_WORK_DIR", "/work")),
            claude_bin=get("CLAUDE_BIN", "claude"),
            model_ask=get("CLAUDE_MODEL_ASK", "sonnet"),
            model_plan=get("CLAUDE_MODEL_PLAN", "sonnet"),
            token_created=parse_date(env.get("CLAUDE_TOKEN_CREATED")),
            age=get("AGE", "31"),
            height_cm=get("HEIGHT_CM", "183"),
            weight_kg=get("WEIGHT_KG", "78"),
            goal=get("GOAL", "maintain overall health and fitness, and play better basketball"),
            experience=get("EXPERIENCE", "intermediate"),
            session_minutes=get("SESSION_MINUTES", "60"),
            equipment=get(
                "EQUIPMENT",
                "dumbbells, barbell, cable machine, weight machines, kettlebells, treadmill",
            ),
            gym_time_mon_thu=get("GYM_TIME_MON_THU", "6pm"),
            gym_time_fri=get("GYM_TIME_FRI", "5:30pm"),
            injury_notes=get("INJURY_NOTES"),
            basketball_days=parse_days(env.get("BASKETBALL_DAYS")),
            equipment_rotation=rotation,
            program_start=program_start,
            training_days=parse_days(env.get("TRAINING_DAYS") or "Mon,Tue,Wed,Thu,Fri"),
            # An empty INJURY_BLOCKED_MOVEMENTS= turns the check off; a missing one uses defaults.
            blocked_movements=parse_list(env.get("INJURY_BLOCKED_MOVEMENTS", DEFAULT_BLOCKED)),
            allowed_movements=parse_list(env.get("INJURY_ALLOWED_MOVEMENTS", DEFAULT_ALLOWED)),
            reminder_mon_thu=parse_time(env.get("REMINDER_TIME_MON_THU"), "17:30"),
            reminder_fri=parse_time(env.get("REMINDER_TIME_FRI"), "17:00"),
            check_time=parse_time(env.get("CHECK_TIME"), "21:00"),
            checkin_time=parse_time(env.get("CHECKIN_TIME"), "18:00"),
            plan_time=parse_time(env.get("PLAN_TIME"), "20:00"),
            token_check_time=parse_time(env.get("TOKEN_CHECK_TIME"), "10:00"),
            garmin_db=get("GARMIN_DB", "/garmin/monitor.db"),
            garmin_profile=get("GARMIN_PROFILE", "Me"),
            garmin_days=max(1, garmin_days),
            secrets=[
                v
                for v in (
                    token,
                    get("CLAUDE_CODE_OAUTH_TOKEN"),
                    get("ANTHROPIC_API_KEY"),
                )
                if v
            ],
        )


# ---------------------------------------------------------------------------
# Logging that never prints tokens
# ---------------------------------------------------------------------------

TOKEN_PATTERNS = [
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),  # Telegram bot token
    re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),  # Anthropic keys and OAuth tokens
]


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str, secrets: list[str]):
        super().__init__(fmt)
        self.secrets = [s for s in secrets if len(s) >= 8]

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record), self.secrets)


def redact(text: str, secrets: list[str] | None = None) -> str:
    for secret in secrets or []:
        text = text.replace(secret, "***")
    for pattern in TOKEN_PATTERNS:
        text = pattern.sub("***", text)
    return text


def setup_logging(cfg: Config) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", cfg.secrets)
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # httpx logs every request URL at INFO, and those URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Storage in /data
# ---------------------------------------------------------------------------


class Store:
    """Small JSON and text files under the data folder, written atomically."""

    def __init__(self, data_dir: Path):
        self.root = Path(data_dir)
        for sub in ("plans", "plans/history", "checkins"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)

    def read_json(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("Could not read %s, using an empty value", path)
            return default

    def write_json(self, name: str, data: Any) -> None:
        self._write(self.root / name, json.dumps(data, indent=2, ensure_ascii=False))

    def _read_jsonl(self, name: str) -> list[dict]:
        path = self.root / name
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                log.warning("Skipping a broken line in %s", path)
        return rows

    def _append_jsonl(self, name: str, row: dict) -> None:
        path = self.root / name
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # -- state --------------------------------------------------------------

    def state(self) -> dict:
        return self.read_json("state.json", {})

    def update_state(self, **values: Any) -> dict:
        state = self.state()
        state.update(values)
        self.write_json("state.json", state)
        return state

    # -- plans --------------------------------------------------------------

    def plan_path(self, monday: date) -> Path:
        return self.root / "plans" / f"{monday.isoformat()}.md"

    def load_plan(self, monday: date) -> str | None:
        path = self.plan_path(monday)
        return path.read_text(encoding="utf-8") if path.exists() else None

    def load_plan_meta(self, monday: date) -> dict:
        return self.read_json(f"plans/{monday.isoformat()}.json", {})

    def save_plan(self, monday: date, text: str, meta: dict, stamp: str) -> None:
        path = self.plan_path(monday)
        if path.exists():  # keep every earlier version
            backup = self.root / "plans" / "history" / f"{monday.isoformat()}_{stamp}.md"
            backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        self._write(path, text.rstrip() + "\n")
        self.write_json(f"plans/{monday.isoformat()}.json", meta)

    def plan_mondays(self) -> list[date]:
        out = []
        for path in (self.root / "plans").glob("*.md"):
            with contextlib.suppress(ValueError):
                out.append(date.fromisoformat(path.stem))
        return sorted(out)

    # -- injury notes -------------------------------------------------------

    def injury(self, default: str) -> tuple[str, str | None]:
        data = self.read_json("injury.json", None)
        if data is None:
            return default, None
        return data.get("text", ""), data.get("updated")

    def set_injury(self, text: str, when: datetime) -> None:
        self.write_json("injury.json", {"text": text, "updated": when.isoformat(timespec="minutes")})

    # -- chat memory --------------------------------------------------------

    def memory(self, chat_id: int) -> list[dict]:
        return self.read_json("memory.json", {}).get(str(chat_id), [])

    def add_memory(self, chat_id: int, question: str, answer: str, when: datetime) -> None:
        data = self.read_json("memory.json", {})
        turns = data.get(str(chat_id), [])
        turns.append({"q": question, "a": answer, "at": when.isoformat(timespec="minutes")})
        data[str(chat_id)] = turns[-MEMORY_TURNS:]
        self.write_json("memory.json", data)

    def clear_memory(self, chat_id: int) -> None:
        data = self.read_json("memory.json", {})
        data.pop(str(chat_id), None)
        self.write_json("memory.json", data)


# ---------------------------------------------------------------------------
# Weekly plan text: parsing and checks
# ---------------------------------------------------------------------------

DAY_HEADER_RE = re.compile(
    r"^[\s*_>#]*📅\s*[*_]*\s*(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b(.*)$",
    re.IGNORECASE,
)
NOTES_RE = re.compile(r"^[\s*_>#]*📝")
NUMBERED_RE = re.compile(r"^\s*[*_]*\s*(\d{1,2})\s*[.)]\s*(.+)$")
BULLET_LINE_RE = re.compile(r"^\s*(?:[•\-*+–]\s+|[*_]*\s*\d{1,2}\s*[.)]\s*)(.+)$")
REST_RE = re.compile(r"\b(rest|off)\b", re.IGNORECASE)
TRAINING_WORDS_RE = re.compile(
    r"push|pull|leg|upper|lower|chest|back|arm|shoulder|full body|swim|run|conditioning|"
    r"strength|core|gym|lift|power",
    re.IGNORECASE,
)


@dataclasses.dataclass
class PlanDay:
    index: int
    focus: str
    text: str

    @property
    def name(self) -> str:
        return DAY_NAMES[self.index]

    @property
    def is_rest(self) -> bool:
        return is_rest_focus(self.focus)


@dataclasses.dataclass
class ParsedPlan:
    preface: str
    days: dict[int, PlanDay]
    notes: str

    @property
    def missing_days(self) -> list[str]:
        return [DAY_NAMES[i] for i in range(7) if i not in self.days]


def is_rest_focus(focus: str) -> bool:
    return bool(REST_RE.search(focus)) and not TRAINING_WORDS_RE.search(focus)


def _header_focus(rest: str) -> str:
    rest = rest.strip()
    m = re.match(r"^[^:\-–—,]*?[:\-–—,]\s*(.*)$", rest)
    focus = m.group(1) if m else rest
    return focus.strip().strip("*_ ").strip()


def parse_plan(text: str) -> ParsedPlan:
    lines = (text or "").splitlines()
    headers: list[tuple[int, int, str]] = []
    for i, line in enumerate(lines):
        m = DAY_HEADER_RE.match(line)
        if m:
            headers.append((i, DAY_LOOKUP[m.group(1).lower()], _header_focus(m.group(2))))
    if not headers:
        return ParsedPlan(preface=(text or "").strip(), days={}, notes="")
    last = headers[-1][0]
    notes_start = next((i for i in range(last + 1, len(lines)) if NOTES_RE.match(lines[i])), None)
    days: dict[int, PlanDay] = {}
    for k, (start, idx, focus) in enumerate(headers):
        end = headers[k + 1][0] if k + 1 < len(headers) else (notes_start or len(lines))
        if idx not in days:
            days[idx] = PlanDay(idx, focus, "\n".join(lines[start:end]).strip())
    preface = "\n".join(lines[: headers[0][0]]).strip()
    notes = "\n".join(lines[notes_start:]).strip() if notes_start is not None else ""
    return ParsedPlan(preface=preface, days=days, notes=notes)


def exercise_name(line: str) -> str | None:
    """'1. Single arm row: 3 x 10, rest 60s' -> 'Single arm row'."""
    m = NUMBERED_RE.match(line)
    if not m:
        return None
    name = m.group(2).replace("**", "").replace("__", "").strip()
    name = re.split(r"\s*[:(]|\s[-–—]\s|\s+\d+\s*(?:x|×|sets?\b)", name, maxsplit=1)[0]
    name = name.strip(" .,*_")
    return name or None


def plan_exercises(text: str) -> list[tuple[int, str]]:
    """(weekday, exercise name) for every numbered line inside a day."""
    out = []
    for day in parse_plan(text).days.values():
        for line in day.text.splitlines()[1:]:
            name = exercise_name(line)
            if name:
                out.append((day.index, name))
    return out


NEGATION_RE = re.compile(
    r"\b(instead of|in place of|replac\w*|swap\w*|rather than|avoid\w*|skip\w*|not|no|without)\b.*$",
    re.IGNORECASE,
)


def _term_regex(term: str) -> re.Pattern:
    parts = []
    for word in term.lower().split():
        parts.append(r"(?:[\w'-]+\s+){0,3}?" if word == "*" else re.escape(word) + r"[\s-]*")
    body = "".join(parts)
    body = body[: -len(r"[\s-]*")] if body.endswith(r"[\s-]*") else body
    return re.compile(r"\b" + body + r"(?:e?s)?\b", re.IGNORECASE)


def find_blocked(text: str, blocked: list[str], allowed: list[str]) -> list[dict]:
    """Exercise lines that contain a movement the injury rules leave out."""
    blocked_res = [(term, _term_regex(term)) for term in blocked]
    allowed_res = [_term_regex(term) for term in allowed]
    hits = []
    for day in parse_plan(text).days.values():
        for line in day.text.splitlines()[1:]:
            m = BULLET_LINE_RE.match(line)
            if not m:
                continue
            content = m.group(1)
            if re.match(r"[*_\s]*(video|cue)\s*:", content, re.IGNORECASE) or "[yt:" in content.lower():
                continue
            check = re.sub(r"\([^)]*\)", " ", content)
            check = NEGATION_RE.sub("", check)
            for pattern in allowed_res:
                check = pattern.sub(" ", check)
            for term, pattern in blocked_res:
                if pattern.search(check):
                    hits.append({"day": day.name, "line": line.strip(), "term": term})
                    break
    return hits


def clean_reply(text: str) -> str:
    text = (text or "").strip()
    fence = re.match(r"^```[\w-]*\n(.*)\n```$", text, re.DOTALL)
    return fence.group(1).strip() if fence else text


EFFORT_WAVE = [
    "Building week 1 of 3. Moderate effort, about RPE 7, leave 3 reps in reserve. "
    "Settle weights and technique.",
    "Building week 2 of 3. A little harder, RPE 7 to 8, 2 reps in reserve. "
    "Add a small amount of weight or one rep where last week felt good.",
    "Building week 3 of 3. The hardest week of the wave, RPE 8, 1 to 2 reps in reserve. "
    "Nothing to failure, and nothing that hurts the shoulder.",
    "Deload week. About 60 percent of the usual sets, lighter weights, RPE 6. "
    "Focus on form, mobility and recovery.",
]


def effort_for(week: int) -> str:
    return EFFORT_WAVE[(week - 1) % len(EFFORT_WAVE)]


def is_deload(week: int) -> bool:
    return (week - 1) % len(EFFORT_WAVE) == len(EFFORT_WAVE) - 1


def fmt_day(d: date) -> str:
    return f"{d:%a} {d.day} {d:%b}"


def fmt_long(d: date) -> str:
    return f"{d:%A} {d.day} {d:%B %Y}"


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

YT_TAG_RE = re.compile(r"\[\s*yt\s*:\s*([^\]\n]+?)\s*\]", re.IGNORECASE)
MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
CODE_RE = re.compile(r"`([^`\n]+)`")
BULLET_RE = re.compile(r"^(\s*)[-*+•–]\s+")
HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
RULE_RE = re.compile(r"^\s*([-*_=])(?:\s*\1){2,}\s*$")
VIDEO_LINK_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?(?:[^\s)\]]*&)?v=|shorts/|live/|embed/)"
    r"|youtu\.be/)[\w-]{6,}[^\s)\]]*",
    re.IGNORECASE,
)


def yt_search_url(words: str) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(" ".join(words.split()))


def _prepare_line(line: str) -> str:
    if RULE_RE.match(line):
        return ""
    heading = HEADING_RE.match(line)
    if heading:
        line = "**" + heading.group(1).strip().strip("*").strip() + "**"
    return BULLET_RE.sub(lambda m: m.group(1) + "• ", line)


def to_html(text: str) -> str:
    """Claude's plain text (with light markdown) -> Telegram HTML."""
    out = []
    for line in text.replace("\x00", "").split("\n"):
        line = _prepare_line(line)
        keep: list[str] = []

        def stash(snippet: str) -> str:
            keep.append(snippet)
            return f"\x00{len(keep) - 1}\x00"

        line = YT_TAG_RE.sub(
            lambda m: stash(
                f'<a href="{html.escape(yt_search_url(m.group(1)))}">'
                f"▶️ {html.escape(m.group(1).strip(), quote=False)}</a>"
            ),
            line,
        )
        line = MD_LINK_RE.sub(
            lambda m: stash(
                f'<a href="{html.escape(m.group(2))}">{html.escape(m.group(1), quote=False)}</a>'
            ),
            line,
        )
        line = html.escape(line, quote=False)
        line = BOLD_RE.sub(r"<b>\1</b>", line)
        line = CODE_RE.sub(r"<code>\1</code>", line)
        line = re.sub("\x00(\\d+)\x00", lambda m: keep[int(m.group(1))], line)
        out.append(line)
    return "\n".join(out).strip()


def to_plain(text: str) -> str:
    """Fallback when Telegram rejects the HTML: same text, no markup, working links."""
    out = []
    for line in text.replace("\x00", "").split("\n"):
        line = _prepare_line(line)
        line = YT_TAG_RE.sub(lambda m: f"▶️ {m.group(1).strip()}: {yt_search_url(m.group(1))}", line)
        line = MD_LINK_RE.sub(lambda m: f"{m.group(1)}: {m.group(2)}", line)
        line = BOLD_RE.sub(r"\1", line)
        line = CODE_RE.sub(r"\1", line)
        out.append(line)
    return "\n".join(out).strip()


def _tg_len(text: str) -> int:
    """Telegram counts UTF-16 code units."""
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on paragraph, then line, then word boundaries so each chunk's HTML fits."""

    def fits(chunk: str) -> bool:
        return max(_tg_len(to_html(chunk)), _tg_len(to_plain(chunk))) <= limit

    def pieces(block: str, level: int) -> list[str]:
        if fits(block) or level > 2:
            if fits(block):
                return [block]
            # a single enormous word: hard cut
            step = max(1, limit // 6)  # room for HTML escaping
            return [block[i : i + step] for i in range(0, len(block), step)]
        sep = ["\n\n", "\n", " "][level]
        parts = block.split(sep)
        chunks: list[str] = []
        current = ""
        for part in parts:
            candidate = part if not current else current + sep + part
            if fits(candidate):
                current = candidate
                continue
            if current:
                chunks.append(current)
            if fits(part):
                current = part
            else:
                chunks.extend(pieces(part, level + 1))
                current = ""
        if current:
            chunks.append(current)
        return chunks

    text = (text or "").strip() or "(empty)"
    return [c for c in (c.strip() for c in pieces(text, 0)) if c]


def find_video_link(text: str) -> str | None:
    m = VIDEO_LINK_RE.search(text or "")
    return m.group(0) if m else None


async def send_text(bot, chat_id: int, text: str, reply_markup=None) -> list:
    """Send as HTML in chunks under 4096 characters; plain text if Telegram objects."""
    chunks = split_text(text)
    sent = []
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        video = find_video_link(chunk)
        preview = LinkPreviewOptions(url=video) if video else LinkPreviewOptions(is_disabled=True)
        try:
            msg = await bot.send_message(
                chat_id,
                to_html(chunk),
                parse_mode=ParseMode.HTML,
                link_preview_options=preview,
                reply_markup=markup,
            )
        except BadRequest as exc:
            log.warning("Telegram rejected the HTML (%s), sending plain text", exc)
            msg = await bot.send_message(
                chat_id, to_plain(chunk), link_preview_options=preview, reply_markup=markup
            )
        sent.append(msg)
    return sent


@contextlib.asynccontextmanager
async def typing(bot, chat_id: int):
    """Show 'typing...' until the block finishes."""

    async def loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await bot.send_chat_action(chat_id, ChatAction.TYPING)
            await asyncio.sleep(4.5)

    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ---------------------------------------------------------------------------
# Claude Code in headless mode
# ---------------------------------------------------------------------------


class ClaudeError(Exception):
    def __init__(self, user_message: str, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message


AUTH_RE = re.compile(
    r"invalid api key|authenticat|unauthori[sz]ed|\b401\b|oauth token|token (?:has )?expired|"
    r"not logged in|/login|login required|invalid bearer|credentials",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(
    r"usage limit|rate limit|\b429\b|overloaded|\b529\b|quota|credit balance|limit reached",
    re.IGNORECASE,
)

MSG_AUTH = (
    "Claude Code could not sign in, so the token probably needs renewing. On your computer run "
    "claude setup-token, put the new token in bot.env as CLAUDE_CODE_OAUTH_TOKEN, update "
    "CLAUDE_TOKEN_CREATED, then restart the bot."
)
MSG_LIMIT = "Claude is busy or your usage limit is reached. Please try again a bit later."


class ClaudeRunner:
    """Runs `claude -p` as an async subprocess from an empty folder."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._slots = asyncio.Semaphore(2)

    def child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if not env.get(key, "").strip():
                env.pop(key, None)
        if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop("ANTHROPIC_API_KEY", None)  # the subscription token wins
        env["DISABLE_AUTOUPDATER"] = "1"
        return env

    def command(self, kind: str, prompt_file: str) -> list[str]:
        model = self.cfg.model_ask if kind == "ask" else self.cfg.model_plan
        cmd = [
            self.cfg.claude_bin,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--model",
            model,
            "--system-prompt-file",
            prompt_file,
        ]
        if kind == "ask":
            cmd += ["--tools", "WebSearch", "--allowedTools", "WebSearch", "--max-turns", "10"]
        else:
            cmd += ["--tools", "", "--max-turns", "3"]
        return cmd

    async def _exec(self, cmd: list[str], stdin: bytes | None, timeout: float):
        self.cfg.work_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self.cfg.work_dir),
            env=self.child_env(),
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 10)
            raise
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def run(self, system_prompt: str, message: str, kind: str) -> str:
        """kind is 'ask' (web search on) or 'plan' (no tools). Returns the reply text."""
        timeout = self.cfg.ask_timeout if kind == "ask" else self.cfg.plan_timeout
        fd, prompt_file = tempfile.mkstemp(prefix="coach-system-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)
        cmd = self.command(kind, prompt_file)
        started = asyncio.get_running_loop().time()
        try:
            async with self._slots:
                code, out, err = await self._exec(cmd, message.encode("utf-8"), timeout)
        except asyncio.TimeoutError as exc:
            minutes = round(timeout / 60)
            raise ClaudeError(
                f"Claude Code took longer than {minutes} minutes, so I stopped it. "
                "Please try again, or ask something shorter."
            ) from exc
        except FileNotFoundError as exc:
            raise ClaudeError(
                "Claude Code is not installed in the container (the claude command was not found)."
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                os.unlink(prompt_file)
        took = asyncio.get_running_loop().time() - started
        return self._parse(code, out, err, kind, took)

    def _parse(self, code: int, out: str, err: str, kind: str, took: float) -> str:
        data = None
        with contextlib.suppress(ValueError):
            data = json.loads(out)
        if isinstance(data, list):  # some versions print every message
            data = next(
                (m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"),
                None,
            )
        if not isinstance(data, dict):
            detail = redact((err or out).strip()[-500:], self.cfg.secrets)
            log.warning("claude (%s) exit %s gave non-JSON output: %s", kind, code, detail)
            if AUTH_RE.search(err + out):
                raise ClaudeError(MSG_AUTH, detail)
            if LIMIT_RE.search(err + out):
                raise ClaudeError(MSG_LIMIT, detail)
            raise ClaudeError(
                "Claude Code sent back something I could not read. Check the bot logs, or test "
                "Claude Code with docker exec as described in the README.",
                detail,
            )
        result = data.get("result") if isinstance(data.get("result"), str) else ""
        subtype = data.get("subtype")
        cost = data.get("total_cost_usd")
        log.info(
            "claude %s finished in %.0fs, subtype=%s, is_error=%s, turns=%s, cost=%s",
            kind,
            took,
            subtype,
            data.get("is_error"),
            data.get("num_turns"),
            cost,
        )
        if data.get("is_error") or subtype != "success":
            detail = redact(f"{subtype}: {result or err}".strip()[:500], self.cfg.secrets)
            log.warning("claude (%s) reported an error: %s", kind, detail)
            if subtype == "error_max_turns":
                raise ClaudeError(
                    "Claude Code ran out of steps before it finished. Please try again, or ask "
                    "in a simpler way.",
                    detail,
                )
            text = f"{result} {err}"
            if AUTH_RE.search(text):
                raise ClaudeError(MSG_AUTH, detail)
            if LIMIT_RE.search(text):
                raise ClaudeError(MSG_LIMIT, detail)
            snippet = (result or subtype or "unknown error").strip()[:200]
            raise ClaudeError(f"Claude Code reported an error: {snippet}", detail)
        if not result.strip():
            raise ClaudeError("Claude Code sent an empty reply. Please try again.")
        return result.strip()

    async def version(self) -> str:
        try:
            code, out, err = await self._exec([self.cfg.claude_bin, "--version"], None, 30)
        except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
            return f"not available ({type(exc).__name__})"
        return (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {code}"

    async def auth_status(self) -> tuple[bool, str]:
        try:
            code, out, err = await self._exec([self.cfg.claude_bin, "auth", "status"], None, 30)
        except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
            return False, f"could not run claude auth status ({type(exc).__name__})"
        data = None
        with contextlib.suppress(ValueError):
            data = json.loads(out)
        if isinstance(data, dict):
            ok = bool(data.get("loggedIn")) and code == 0
            method = data.get("authMethod") or "unknown method"
            return ok, f"signed in with {method}" if ok else "not signed in"
        text = redact((out or err).strip().splitlines()[0] if (out or err).strip() else "", self.cfg.secrets)
        return code == 0, text or f"exit {code}"


# ---------------------------------------------------------------------------
# The coach prompt
# ---------------------------------------------------------------------------

COACH_PROMPT = """You are an experienced strength and conditioning coach and my personal trainer. I talk to you through a Telegram bot, and your reply is sent to me as a message.

About me
Age: {age}
Height: {height}
Weight: {weight}
Goal: {goal}
Training experience: {experience}
Time per session: {session}, including changing and showering
Equipment in the office gym: {equipment}

Schedule
I am in the office Monday to Friday and train at the office gym after work: from {gym_mon_thu} Monday to Thursday and from {gym_fri} on Friday.
Thursday or Friday is reserved for legs and running.
Friday I may go swimming instead, so the plan must still work if Friday becomes a swim day.
{basketball}
If I say I missed a session, adjust the rest of the week instead of doubling up. If I say I only have 20 or 30 minutes, give a shorter version of today's session.
If I am on leave, travelling, or it is a public holiday, give a hotel gym or bodyweight version, or move the session.

Injury
I have a left shoulder injury with a micro tear, and I have no strength in that arm when lifting. Treat it as not cleared for heavy loading unless my latest injury notes say a doctor or physio has cleared it.
1. Never suggest a movement that loads my left shoulder into pain.
2. Leave out overhead pressing, dips, upright rows, behind the neck work, heavy or wide grip bench press and heavy flyes, and give a shoulder friendly swap for each.
3. Use single arm work so my right side trains normally while my left side works light, slow and pain free, or rests.
4. On leg days, skip barbell back squats if holding the bar hurts, and use leg press, hack squat or other machines instead.
5. On upper body days, add a 5 to 10 minute rehab block of gentle rotator cuff and shoulder blade exercises, and remind me to confirm it with my physio.
6. For swimming, favour kick sets and strokes that do not hurt.
7. For basketball, point out what stresses the shoulder (lots of overhead shooting, rebounding contact, falls) and suggest I check with my physio before full games.
8. If my shoulder ratings rise or I report more pain, scale the next sessions back and suggest I check with my physio.
9. If I mention sharp or worsening pain, pain at night, numbness or tingling, or a sudden loss of strength, tell me to stop and see a doctor or physio. You are a coach, not my doctor.

Basketball
Where it fits, include work that carries over to the court: jumping and landing, lateral quickness and footwork, single leg strength, core and anti rotation work, ankle and knee stability, and short conditioning intervals. Build jump volume up slowly.

Recovery and progress
Use my workout logs to set my weights and progress me week to week. If my Garmin data shows poor recovery (short sleep, low body battery, or HRV below my usual), make today lighter. Count my logged runs toward my weekly running.

How my plan works
1. Split the upper body days using either a push/pull structure or an individual body part structure. In the first week, compare both briefly, then pick the one that fits my schedule, basketball and shoulder best, and explain why in two or three sentences.
2. The split stays the same every week, with the same muscle groups on the same days, but every exercise changes each week as the main equipment rotates.
3. Effort follows a 4 week wave: three building weeks, then a lighter deload week.
4. For each training day: a 5 to 10 minute warm up, then each exercise with sets x reps, rest time, one short form cue and a video line, then a short cool down.
5. For the legs and running day, include a run with distance or time and a target pace or effort.
6. For Friday, give a swim session (duration and structure) that is safe for my shoulder, plus the legs option in case I do not swim.
7. In the first week, add a short recovery note: sleep, rest days, and protein per day for my weight.

Video guides
Put a video line under every exercise in this format: Video: [yt: exercise name proper form]
When I ask for a specific video, use web search to find a real one from a reputable coach or fitness organisation and give its direct link. Never guess a URL.

How to write
Plain text for Telegram: no tables and no # headings, short lines, numbered lists or • bullets, and a blank line between sections. Do not use dashes; use commas or full stops instead. Keep answers concise and practical."""


def _with_unit(value: str, unit: str) -> str:
    return f"{value} {unit}" if re.fullmatch(r"\d+(?:\.\d+)?", value) else value


def join_names(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def basketball_line(days: list[int]) -> str:
    if not days:
        return (
            "I play basketball with no fixed day. When I mention an upcoming game, keep the day "
            "before it light on the legs."
        )
    return (
        f"I play basketball on {join_names([DAY_NAMES[d] for d in days])}, so keep the day "
        "before light on the legs."
    )


def coach_prompt(cfg: Config) -> str:
    return COACH_PROMPT.format(
        age=cfg.age,
        height=_with_unit(cfg.height_cm, "cm"),
        weight=_with_unit(cfg.weight_kg, "kg"),
        goal=cfg.goal,
        experience=cfg.experience,
        session=_with_unit(cfg.session_minutes, "minutes"),
        equipment=cfg.equipment,
        gym_mon_thu=cfg.gym_time_mon_thu,
        gym_fri=cfg.gym_time_fri,
        basketball=basketball_line(cfg.basketball_days),
    )


PLAN_FORMAT_RULES = """Format rules. The bot reads your plan automatically, so follow them exactly:
1. Cover all seven days, Monday to Sunday, in order.
2. Start each day with one line in exactly this form: 📅 Monday: Push. Use the day name, a colon and the day's focus. Mark rest days in the focus, for example 📅 Sunday: Rest or light mobility.
3. On training days write the warm up as one line starting with "Warm up:". Then number the main exercises and the rehab exercises, one per line, like "1. Exercise name: 3 x 10, rest 90s". Under each exercise add one "Cue:" line and one "Video: [yt: exercise name proper form]" line. Finish the day with one line starting with "Cool down:".
4. After Sunday, write the general notes once, starting with a line that begins with 📝. Do not use 📝 anywhere else.
5. Write nothing before the first 📅 line{preface_rule}."""


# ---------------------------------------------------------------------------
# The coach: context, questions and plans
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PlanResult:
    monday: date
    text: str
    meta: dict
    warnings: list[str]


class PlanBusy(Exception):
    pass


class Coach:
    def __init__(self, cfg: Config, store: Store | None = None, runner: ClaudeRunner | None = None):
        self.cfg = cfg
        self.store = store or Store(cfg.data_dir)
        self.runner = runner or ClaudeRunner(cfg)
        self.plan_lock = asyncio.Lock()

    # -- time and weeks -----------------------------------------------------

    def now(self) -> datetime:
        return now_in(self.cfg.tz)

    def today(self) -> date:
        return self.now().date()

    def program_start(self) -> date | None:
        if self.cfg.program_start:
            return self.cfg.program_start
        raw = self.store.state().get("program_start")
        return date.fromisoformat(raw) if raw else None

    def week_number(self, monday: date) -> int:
        start = self.program_start() or monday
        return max(1, (monday - start).days // 7 + 1)

    def equipment_for(self, week: int) -> str:
        rotation = self.cfg.equipment_rotation
        return rotation[(week - 1) % len(rotation)]

    def week_label(self, monday: date) -> str:
        meta = self.store.load_plan_meta(monday)
        week = meta.get("week") or self.week_number(monday)
        equipment = meta.get("equipment") or self.equipment_for(week)
        kind = "deload week" if is_deload(week) else f"building week {(week - 1) % 4 + 1} of 3"
        sunday = monday + timedelta(days=6)
        return f"Week {week} · {equipment} · {kind} · {fmt_day(monday)} to {fmt_day(sunday)}"

    # -- context for Claude ---------------------------------------------------

    def injury_text(self) -> tuple[str, str | None]:
        return self.store.injury(self.cfg.injury_notes)

    def extra_context(self, now: datetime) -> list[str]:
        """Sections added by later stages (logs, ratings, Garmin)."""
        return []

    def system_prompt(self) -> str:
        now = self.now()
        today = now.date()
        monday = monday_of(today)
        week = self.week_number(monday)
        parts = [coach_prompt(self.cfg), "Context from the bot (use it, do not repeat it back)"]
        parts.append(
            f"Today is {fmt_long(today)}, {now:%H:%M} Singapore time. This is week {week} of my "
            f"programme (week of {fmt_long(monday)}). Main equipment this week: "
            f"{self.equipment_for(week)}. Effort this week: {effort_for(week)}"
        )
        injury, updated = self.injury_text()
        when = f" (updated {updated[:10]})" if updated else ""
        parts.append(
            f"My latest injury notes{when}:\n{injury}" if injury.strip() else f"My latest injury notes{when}: none."
        )
        plan = self.store.load_plan(monday)
        parts.append(
            f"This week's plan:\n{plan.strip()}" if plan else "This week's plan: none saved yet."
        )
        if today.weekday() >= 5:
            nxt = self.store.load_plan(monday + timedelta(days=7))
            if nxt:
                parts.append(f"Next week's plan (already built):\n{nxt.strip()}")
        parts.extend(self.extra_context(now))
        return "\n\n".join(parts)

    # -- questions ------------------------------------------------------------

    async def ask(self, chat_id: int, question: str) -> str:
        turns = self.store.memory(chat_id)
        if turns:
            history = "\n\n".join(f"Me: {t['q']}\n\nYou: {t['a']}" for t in turns)
            message = (
                "Our recent conversation, oldest first:\n\n"
                f"{history}\n\nMy new message:\n{question}"
            )
        else:
            message = question
        answer = clean_reply(await self.runner.run(self.system_prompt(), message, "ask"))
        self.store.add_memory(chat_id, question, answer[:3000], self.now())
        return answer

    # -- plans ----------------------------------------------------------------

    def split_text(self) -> str | None:
        split = self.store.state().get("split")
        if not split:
            return None
        return "\n".join(f"{DAY_NAMES[int(k)]}: {v}" for k, v in sorted(split.items(), key=lambda kv: int(kv[0])))

    def previous_exercises(self, monday: date) -> list[str]:
        seen: dict[str, str] = {}
        for weeks_back in (1, 2):
            text = self.store.load_plan(monday - timedelta(weeks=weeks_back))
            for _, name in plan_exercises(text or ""):
                seen.setdefault(name.lower(), name)
        return list(seen.values())

    def checkin_for(self, monday: date) -> str | None:
        """Stage 2 fills this in: the Sunday check in before this week."""
        return None

    def plan_problems(self, text: str) -> list[str]:
        problems = []
        parsed = parse_plan(text)
        if parsed.missing_days:
            problems.append(
                "The plan is missing these days, or their line does not start with 📅: "
                + join_names(parsed.missing_days)
                + "."
            )
        for hit in find_blocked(text, self.cfg.blocked_movements, self.cfg.allowed_movements):
            problems.append(
                f"{hit['day']}, \"{hit['line']}\": {hit['term']} is a movement my injury rules "
                "leave out."
            )
        return problems

    def plan_request(self, monday: date, notes: str) -> str:
        week = self.week_number(monday)
        sunday = monday + timedelta(days=6)
        split = self.split_text()
        lines = [
            f"Build my full training plan for the week of {fmt_long(monday)} to {fmt_long(sunday)}.",
            "",
            f"Week number: {week}",
            f"Main equipment this week: {self.equipment_for(week)}. Build most exercises around "
            "it, and use other equipment only where it suits my shoulder better.",
            f"Effort this week: {effort_for(week)}",
        ]
        if split:
            lines += [
                "",
                "Keep my split exactly as it is, with the same focus on the same days, unless my "
                "notes below ask to change it:",
                split,
            ]
        else:
            lines += [
                "",
                "This is my first week, so no split is set yet. Compare a push/pull split with a "
                "body part split briefly, pick the one that fits my schedule, basketball and "
                "shoulder best, and explain why in two or three sentences before the first day.",
            ]
        previous = self.previous_exercises(monday)
        if previous:
            lines += [
                "",
                "Exercises from the previous two weeks. Do not repeat any of them this week, "
                "except exercises in the rehab block:",
                ", ".join(previous),
            ]
        lines += [
            "",
            "Use my workout logs, session history and shoulder ratings in your context to set "
            "weights and progress me.",
        ]
        checkin = self.checkin_for(monday)
        lines.append(
            f"My Sunday check in answer: {checkin}" if checkin else "I did not send a Sunday check in answer this week."
        )
        if notes.strip():
            lines += ["", f"My notes for this plan: {notes.strip()}"]
        preface_rule = ", except the short split comparison" if not split else ""
        lines += ["", PLAN_FORMAT_RULES.format(preface_rule=preface_rule)]
        return "\n".join(lines)

    def fix_request(self, text: str, problems: list[str], had_split: bool) -> str:
        preface_rule = "" if had_split else ", except the short split comparison"
        return "\n".join(
            [
                "Your plan below has problems the bot found:",
                *[f"• {p}" for p in problems],
                "",
                "Rewrite the full plan. Give a shoulder friendly swap for every movement listed, "
                "add any missing days, and keep everything else the same.",
                "",
                PLAN_FORMAT_RULES.format(preface_rule=preface_rule),
                "",
                "The plan:",
                text,
            ]
        )

    async def generate_plan(
        self, monday: date, request: str, *, kind: str, save_split: bool, notes: str = ""
    ) -> PlanResult:
        """Run Claude, check the plan, fix it once if needed, then save it."""
        had_split = bool(self.store.state().get("split"))
        system = self.system_prompt()
        text = clean_reply(await self.runner.run(system, request, "plan"))
        problems = self.plan_problems(text)
        fixed = False
        warnings: list[str] = []
        if problems:
            log.info("Plan for %s has %d problems, asking Claude to fix it once", monday, len(problems))
            try:
                text = clean_reply(
                    await self.runner.run(system, self.fix_request(text, problems, had_split), "plan")
                )
                fixed = True
            except ClaudeError as exc:
                warnings.append(f"I could not get the plan fixed ({exc.user_message}).")
            problems = self.plan_problems(text)
        warnings += problems
        now = self.now()
        week = self.week_number(monday)
        meta = {
            "monday": monday.isoformat(),
            "week": week,
            "equipment": self.equipment_for(week),
            "effort": effort_for(week),
            "built_at": now.isoformat(timespec="seconds"),
            "kind": kind,
            "model": self.cfg.model_plan,
            "notes": notes,
            "fixed_once": fixed,
            "warnings": warnings,
        }
        self.store.save_plan(monday, text, meta, now.strftime("%Y%m%d-%H%M%S"))
        state_update: dict[str, Any] = {
            "last_plan_built": {"at": meta["built_at"], "monday": monday.isoformat(), "kind": kind}
        }
        parsed = parse_plan(text)
        if save_split and len(parsed.days) == 7:
            state_update["split"] = {str(i): d.focus for i, d in sorted(parsed.days.items())}
        self.store.update_state(**state_update)
        return PlanResult(monday, text, meta, warnings)

    async def build_week(self, monday: date, notes: str = "", *, kind: str = "build", wait: bool = False) -> PlanResult:
        if self.plan_lock.locked() and not wait:
            raise PlanBusy()
        async with self.plan_lock:
            if not self.program_start():
                self.store.update_state(program_start=monday.isoformat())
            week = self.week_number(monday)
            state = self.store.state()
            save_split = not state.get("split") or week == 1 or "split" in notes.lower()
            request = self.plan_request(monday, notes)
            return await self.generate_plan(monday, request, kind=kind, save_split=save_split, notes=notes)

    # -- plain views ----------------------------------------------------------

    def today_text(self) -> str:
        today = self.today()
        monday = monday_of(today)
        plan = self.store.load_plan(monday)
        if not plan:
            return "No plan is saved for this week yet. Send /plan to build one."
        day = parse_plan(plan).days.get(today.weekday())
        if not day:
            return (
                f"I could not find {DAY_NAMES[today.weekday()]} in this week's plan, so here is "
                f"the whole week.\n\n{plan}"
            )
        return day.text

    def week_text(self) -> str:
        today = self.today()
        monday = monday_of(today)
        note = ""
        if today.weekday() >= 5:
            nxt = monday + timedelta(days=7)
            if self.store.load_plan(nxt):
                monday = nxt
            else:
                note = (
                    "\n\nNext week's plan is built on Sunday at "
                    f"{self.cfg.plan_time:%H:%M}, or send /nextweek to build it now."
                )
        plan = self.store.load_plan(monday)
        if not plan:
            return "No plan is saved for this week yet. Send /plan to build one." + note
        return f"**{self.week_label(monday)}**\n\n{plan.strip()}{note}"

    def overview(self, result: PlanResult, heading: str) -> str:
        parsed = parse_plan(result.text)
        lines = [heading, ""]
        for idx in range(7):
            day = parsed.days.get(idx)
            if not day:
                continue
            names = [exercise_name(l) for l in day.text.splitlines()[1:]]
            names = [n for n in names if n]
            lines.append(f"📅 {DAY_NAMES[idx][:3]}: {day.focus}")
            if names:
                lines.append(", ".join(names))
            lines.append("")
        return "\n".join(lines).strip()

    def plan_reply(self, result: PlanResult) -> str:
        text = f"**{self.week_label(result.monday)}**\n\n{result.text.strip()}"
        if result.warnings:
            text += "\n\n⚠️ Please check:\n" + "\n".join(f"• {w}" for w in result.warnings)
        return text


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

COMMANDS = [
    ("ask", "Ask your coach anything"),
    ("today", "Today's session"),
    ("week", "This week's plan (next week's on weekends)"),
    ("plan", "Rebuild this week's plan, with optional notes"),
    ("nextweek", "Build next week's plan now"),
    ("log", "Log what you did"),
    ("done", "Mark today's session finished"),
    ("shoulder", "Your shoulder rating log"),
    ("injury", "Show or replace your injury notes"),
    ("profile", "What the coach knows about you"),
    ("status", "Claude Code, sign in and reminders"),
    ("reset", "Clear the chat memory"),
    ("whoami", "Your Telegram user ID"),
]

HELP_TEXT = """Hi, I am your gym coach.

• Just write to me, or use /ask, with any training question.
• /today shows today's session and /week the whole week.
• /plan rebuilds this week and /nextweek builds next week. Add notes after the command.
• /log what you did, then /done when you finish a session.
• /shoulder shows your shoulder ratings, /injury your injury notes.
• /profile, /status, /reset and /whoami are there too."""


def coach_of(context: ContextTypes.DEFAULT_TYPE) -> Coach:
    return context.application.bot_data["coach"]


def args_text(update: Update) -> str:
    """Everything after the command, keeping line breaks."""
    text = update.effective_message.text or ""
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None):
    return await send_text(context.bot, update.effective_chat.id, text, reply_markup=reply_markup)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only ALLOWED_USER_IDS get through. Everyone else learns their own ID."""
    coach = coach_of(context)
    user = update.effective_user
    if user is not None and user.id in coach.cfg.allowed_ids:
        return
    if user is not None:
        if update.callback_query:
            with contextlib.suppress(Exception):
                await update.callback_query.answer("This is a private bot.")
        msg = update.effective_message
        if msg and (msg.chat.type == ChatType.PRIVATE or (msg.text or "").startswith("/")):
            recent = context.application.bot_data.setdefault("stranger_replies", {})
            stamp = coach.now().timestamp()
            if stamp - recent.get(user.id, 0) > 30:
                recent[user.id] = stamp
                log.info("Message from a user who is not allowed: %s", user.id)
                await context.bot.send_message(
                    msg.chat.id,
                    f"Sorry, this is a private bot. Your Telegram user ID is <code>{user.id}</code>.",
                    parse_mode=ParseMode.HTML,
                )
    raise ApplicationHandlerStop


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, HELP_TEXT)


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await reply(update, context, f"Your Telegram user ID is `{user.id}`.")


async def answer_question(update: Update, context: ContextTypes.DEFAULT_TYPE, question: str) -> None:
    coach = coach_of(context)
    chat_id = update.effective_chat.id
    try:
        async with typing(context.bot, chat_id):
            answer = await coach.ask(chat_id, question)
    except ClaudeError as exc:
        await reply(update, context, exc.user_message)
        return
    await reply(update, context, answer)


async def cmd_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = args_text(update)
    if not question:
        await reply(update, context, "Send /ask followed by your question, for example: /ask I only have 30 minutes today")
        return
    await answer_question(update, context, question)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A plain message in a private chat works like /ask."""
    text = (update.effective_message.text or "").strip()
    if text:
        await answer_question(update, context, text)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, coach_of(context).today_text())


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, coach_of(context).week_text())


async def _build_and_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, monday: date, label: str) -> None:
    coach = coach_of(context)
    notes = args_text(update)
    chat_id = update.effective_chat.id
    if coach.plan_lock.locked():
        await reply(update, context, "I am already building a plan. I will send it when it is ready.")
        return
    await reply(update, context, f"Building {label}. This usually takes a minute or two.")
    try:
        async with typing(context.bot, chat_id):
            result = await coach.build_week(monday, notes, kind="rebuild" if coach.store.load_plan(monday) else "build")
    except PlanBusy:
        await reply(update, context, "I am already building a plan. I will send it when it is ready.")
        return
    except ClaudeError as exc:
        await reply(update, context, f"I could not build the plan. {exc.user_message}")
        return
    await reply(update, context, coach.plan_reply(result))


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    monday = monday_of(coach_of(context).today())
    await _build_and_reply(update, context, monday, "this week's plan")


async def cmd_nextweek(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    monday = monday_of(coach_of(context).today()) + timedelta(days=7)
    await _build_and_reply(update, context, monday, "next week's plan")


async def cmd_injury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    new = args_text(update)
    if not new:
        text, updated = coach.injury_text()
        when = f" (updated {updated[:10]})" if updated else " (from bot.env)"
        if text.strip():
            await reply(update, context, f"**Injury notes**{when}\n\n{text}\n\nReplace them with /injury <new notes>, or clear them with /injury none.")
        else:
            await reply(update, context, f"No injury notes are saved{when}. Add some with /injury <notes>.")
        return
    if new.lower() in ("none", "clear", "no", "-"):
        coach.store.set_injury("", coach.now())
        await reply(update, context, "Injury notes cleared.")
        return
    coach.store.set_injury(new, coach.now())
    await reply(update, context, "Injury notes saved. The coach will use them from now on.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach_of(context).store.clear_memory(update.effective_chat.id)
    await reply(update, context, "Chat memory cleared.")


def profile_text(coach: Coach) -> str:
    cfg = coach.cfg
    today = coach.today()
    monday = monday_of(today)
    week = coach.week_number(monday)
    injury, updated = coach.injury_text()
    split = coach.split_text()
    lines = [
        "**About you**",
        f"• Age {cfg.age}, height {_with_unit(cfg.height_cm, 'cm')}, weight {_with_unit(cfg.weight_kg, 'kg')}",
        f"• Goal: {cfg.goal}",
        f"• Experience: {cfg.experience}",
        f"• Session length: {_with_unit(cfg.session_minutes, 'minutes')}",
        f"• Gym equipment: {cfg.equipment}",
        f"• Gym times: {cfg.gym_time_mon_thu} Monday to Thursday, {cfg.gym_time_fri} Friday",
        f"• Basketball: {join_names([DAY_NAMES[d] for d in cfg.basketball_days]) or 'no fixed day'}",
        "",
        "**Programme**",
        f"• This week: {coach.week_label(monday)}",
        f"• Equipment rotation: {', '.join(cfg.equipment_rotation)}",
        f"• Programme started: {coach.program_start() or 'not yet, send /plan'}",
        "• Split: " + (split.replace("\n", ", ") if split else "chosen with your first plan"),
        "",
        "**Injury notes**" + (f" (updated {updated[:10]})" if updated else ""),
        injury.strip() or "none",
    ]
    lines += coach_profile_extra(coach)
    return "\n".join(lines)


def coach_profile_extra(coach: Coach) -> list[str]:
    """Later stages add the shoulder trend and Garmin summary here."""
    return []


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, profile_text(coach_of(context)))


def fmt_when(dt: datetime | None) -> str:
    return f"{dt:%a} {dt.day} {dt:%b} {dt:%H:%M}" if dt else "not scheduled"


async def status_text(coach: Coach, application: Application) -> str:
    version, (signed_in, auth) = await asyncio.gather(coach.runner.version(), coach.runner.auth_status())
    cfg = coach.cfg
    lines = [
        "**Claude Code**",
        f"• Version: {version}",
        f"• Sign in: {'✅ ' if signed_in else '⚠️ '}{auth}",
        f"• Models: questions {cfg.model_ask}, plans {cfg.model_plan}",
    ]
    if cfg.token_created:
        expires = cfg.token_created + timedelta(days=365)
        left = (expires - coach.today()).days
        lines.append(f"• Token created {cfg.token_created}, expires about {expires} ({left} days left)")
    else:
        lines.append("• Token date: set CLAUDE_TOKEN_CREATED in bot.env to get a renewal reminder")
    last = coach.store.state().get("last_plan_built")
    lines += ["", "**Plans**"]
    if last:
        built = datetime.fromisoformat(last["at"])
        lines.append(f"• Last plan built {fmt_when(built)} for the week of {fmt_day(date.fromisoformat(last['monday']))}")
    else:
        lines.append("• No plan built yet. Send /plan")
    lines.append(f"• This week: {coach.week_label(monday_of(coach.today()))}")
    jobs = application.job_queue.jobs() if application.job_queue else ()
    lines += ["", "**Next reminders**"]
    upcoming = sorted(
        ((job.next_t, JOB_LABELS.get(job.name, job.name)) for job in jobs if job.next_t),
        key=lambda item: item[0],
    )
    if upcoming:
        seen = set()
        for when, label in upcoming:
            if label in seen:
                continue
            seen.add(label)
            lines.append(f"• {label}: {fmt_when(when.astimezone(cfg.tz))}")
    else:
        lines.append("• none scheduled")
    lines += status_extra(coach)
    return "\n".join(lines)


JOB_LABELS: dict[str, str] = {}


def status_extra(coach: Coach) -> list[str]:
    """Later stages add Garmin status here."""
    return []


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with typing(context.bot, update.effective_chat.id):
        text = await status_text(coach_of(context), context.application)
    await reply(update, context, text)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, "I do not know that command.\n\n" + HELP_TEXT)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                update.effective_chat.id, "Sorry, something went wrong on my side. Please try again."
            )


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])
    coach: Coach = application.bot_data["coach"]
    work = coach.cfg.work_dir
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        log.warning("%s is not empty. Claude Code should run from an empty folder.", work)
    log.info("Coach bot ready. Allowed users: %s", coach.cfg.allowed_ids)


def add_handlers(application: Application, concurrent: bool = True) -> None:
    application.add_handler(TypeHandler(Update, gate), group=-1)
    # Claude calls take minutes, so they run in the background and other commands still work.
    slow = {"block": not concurrent}
    application.add_handler(CommandHandler(["start", "help"], cmd_start))
    application.add_handler(CommandHandler("whoami", cmd_whoami))
    application.add_handler(CommandHandler("ask", cmd_ask, **slow))
    application.add_handler(CommandHandler("today", cmd_today))
    application.add_handler(CommandHandler("week", cmd_week))
    application.add_handler(CommandHandler("plan", cmd_plan, **slow))
    application.add_handler(CommandHandler("nextweek", cmd_nextweek, **slow))
    application.add_handler(CommandHandler("injury", cmd_injury))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("status", cmd_status, **slow))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text, **slow)
    )
    application.add_error_handler(on_error)


def schedule_jobs(application: Application) -> None:
    """Stage 2 adds the reminders."""


def build_application(
    cfg: Config, coach: Coach | None = None, request=None, updates_request=None, concurrent: bool = True
) -> Application:
    builder = ApplicationBuilder().token(cfg.telegram_token).defaults(Defaults(tzinfo=cfg.tz)).post_init(post_init)
    if request is not None:
        builder = builder.request(request).get_updates_request(updates_request or request)
    application = builder.build()
    application.bot_data["coach"] = coach or Coach(cfg)
    add_handlers(application, concurrent)
    schedule_jobs(application)
    return application


def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Settings problem in bot.env: {exc}") from None
    setup_logging(cfg)
    application = build_application(cfg)
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
