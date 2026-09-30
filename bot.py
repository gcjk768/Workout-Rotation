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
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
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

    # -- workout logs, shoulder ratings, sessions, check ins ------------------

    def add_log(self, when: datetime, text: str) -> None:
        self._append_jsonl(
            "logs.jsonl",
            {"date": when.date().isoformat(), "time": when.strftime("%H:%M"), "text": text},
        )

    def logs(self, since: date | None = None) -> list[dict]:
        rows = self._read_jsonl("logs.jsonl")
        return [r for r in rows if since is None or str(r.get("date", "")) >= since.isoformat()]

    def add_rating(self, day: date, when: datetime, rating: int, note: str) -> None:
        self._append_jsonl(
            "shoulder.jsonl",
            {"date": day.isoformat(), "time": when.strftime("%H:%M"), "rating": rating, "note": note},
        )

    def ratings(self, since: date | None = None) -> list[dict]:
        rows = [r for r in self._read_jsonl("shoulder.jsonl") if isinstance(r.get("rating"), int)]
        rows.sort(key=lambda r: (str(r.get("date", "")), str(r.get("time", ""))))
        return [r for r in rows if since is None or str(r.get("date", "")) >= since.isoformat()]

    def sessions(self) -> dict:
        return self.read_json("sessions.json", {})

    def set_session(self, day: date, status: str, when: datetime, source: str) -> None:
        data = self.sessions()
        data[day.isoformat()] = {
            "status": status,
            "at": when.isoformat(timespec="minutes"),
            "source": source,
        }
        self.write_json("sessions.json", data)

    def checkin_path(self, sunday: date) -> Path:
        return self.root / "checkins" / f"{sunday.isoformat()}.txt"

    def load_checkin(self, sunday: date) -> str | None:
        path = self.checkin_path(sunday)
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    def save_checkin(self, sunday: date, text: str) -> None:
        existing = self.load_checkin(sunday)
        combined = f"{existing}\n\n{text.strip()}" if existing else text.strip()
        self._write(self.checkin_path(sunday), combined + "\n")

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


def _row_date(row: dict, key: str = "date") -> date | None:
    with contextlib.suppress(ValueError, TypeError):
        return date.fromisoformat(str(row.get(key)))
    return None


def shoulder_trend(ratings: list[dict], today: date) -> str:
    """One or two sentences about where the left shoulder is heading (higher = more pain)."""
    if not ratings:
        return "No shoulder ratings yet. Rate it after a session with /done, or send /shoulder 3."
    latest = ratings[-1]
    latest_day = _row_date(latest)
    parts = [f"Latest {latest['rating']}/10" + (f" on {fmt_day(latest_day)}." if latest_day else ".")]
    recent = [r["rating"] for r in ratings if (d := _row_date(r)) and d > today - timedelta(days=14)]
    before = [
        r["rating"]
        for r in ratings
        if (d := _row_date(r)) and today - timedelta(days=28) < d <= today - timedelta(days=14)
    ]
    if recent:
        avg = sum(recent) / len(recent)
        sentence = f"Last 2 weeks average {avg:.1f} from {len(recent)} rating{'s' if len(recent) > 1 else ''}"
        if before:
            prev = sum(before) / len(before)
            if avg - prev >= 0.5:
                sentence += f", up from {prev:.1f}, so the pain is rising."
            elif prev - avg >= 0.5:
                sentence += f", down from {prev:.1f}, so it is improving."
            else:
                sentence += f", about the same as the 2 weeks before ({prev:.1f})."
        else:
            sentence += "."
        parts.append(sentence)
    last3 = [r["rating"] for r in ratings[-3:]]
    if len(last3) == 3 and last3[0] < last3[1] < last3[2]:
        parts.append("The last three ratings went up each time.")
    return " ".join(parts)


def shoulder_rising(ratings: list[dict], today: date) -> bool:
    return "rising" in shoulder_trend(ratings, today) or "went up" in shoulder_trend(ratings, today)


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
# Garmin data from garmin-monitor (read only)
# ---------------------------------------------------------------------------

GARMIN_COLUMNS = (
    "day, resting_hr, resting_hr_7d_avg, sleep_seconds, sleep_score, hrv_last_night, "
    "hrv_weekly_avg, hrv_status, body_battery_high, body_battery_low, body_battery_latest, "
    "activities_json, last_sync"
)


@dataclasses.dataclass
class GarminSummary:
    ok: bool
    context: str  # for Claude
    short: str  # one line for /profile and /status
    recovery_flags: list[str]


class GarminReader:
    """Reads garmin-monitor's SQLite database without ever writing to it.

    garmin-monitor keeps the database in WAL mode. While it runs, a normal read only open
    sees its newest rows. After a clean shutdown the -shm file is gone and a read only
    mount cannot recreate it, so the reader falls back to an immutable open.
    """

    def __init__(self, path: str, profile: str):
        self.path = path
        self.profile = profile

    def _query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not Path(self.path).is_file():
            raise FileNotFoundError(self.path)
        last_error: Exception | None = None
        for options in ("mode=ro", "mode=ro&immutable=1"):
            conn = None
            try:
                conn = sqlite3.connect(f"file:{quote(self.path)}?{options}", uri=True, timeout=5)
                conn.row_factory = sqlite3.Row
                return conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError as exc:
                last_error = exc
                if "no such table" in str(exc) and options.endswith("immutable=1"):
                    break
            finally:
                if conn is not None:
                    conn.close()
        raise last_error or sqlite3.OperationalError("could not read the database")

    def rows(self, since: date, until: date) -> list[dict]:
        rows = self._query(
            f"SELECT {GARMIN_COLUMNS} FROM daily_snapshots WHERE profile = ? AND day >= ? AND day <= ? ORDER BY day",
            (self.profile, since.isoformat(), until.isoformat()),
        )
        return [dict(r) for r in rows]

    def profiles(self) -> list[str]:
        return [r[0] for r in self._query("SELECT DISTINCT profile FROM daily_snapshots ORDER BY profile")]

    def summary(self, today: date, days: int, tz: ZoneInfo) -> GarminSummary:
        try:
            rows = self.rows(today - timedelta(days=days - 1), today)
            if not rows:
                others = self.profiles()
                found = f" (profiles found: {', '.join(others)})" if others else ""
                msg = f"no data for profile '{self.profile}' in the last {days} days{found}"
                return GarminSummary(False, f"Garmin data: not available ({msg}).", msg, [])
        except FileNotFoundError:
            msg = f"no database at {self.path}"
            return GarminSummary(False, "Garmin data: not available right now.", msg, [])
        except sqlite3.Error as exc:
            log.warning("Could not read Garmin data from %s: %s", self.path, exc)
            msg = f"could not read {self.path} ({exc})"
            return GarminSummary(False, "Garmin data: not available right now.", msg, [])
        return summarize_garmin(rows, today, days, tz)


def _hours(seconds: Any) -> str | None:
    return f"{seconds / 3600:.1f} h" if isinstance(seconds, (int, float)) and seconds > 0 else None


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _activities(row: dict) -> list[dict]:
    with contextlib.suppress(ValueError, TypeError):
        items = json.loads(row.get("activities_json") or "[]")
        return [a for a in items if isinstance(a, dict)]
    return []


def recovery_flags(row: dict, history: list[dict]) -> list[str]:
    """Signs of poor recovery on one day: short sleep, low body battery, low HRV, high RHR."""
    flags = []
    sleep = row.get("sleep_seconds")
    if isinstance(sleep, (int, float)) and 0 < sleep < 6 * 3600:
        flags.append(f"short sleep ({_hours(sleep)})")
    battery = row.get("body_battery_high")
    if isinstance(battery, (int, float)) and battery < 40:
        flags.append(f"body battery only reached {int(battery)}")
    hrv = row.get("hrv_last_night")
    earlier = [h["hrv_last_night"] for h in history if isinstance(h.get("hrv_last_night"), (int, float))]
    usual = row.get("hrv_weekly_avg") or (sum(earlier) / len(earlier) if earlier else None)
    if isinstance(hrv, (int, float)) and usual and hrv < 0.9 * usual:
        flags.append(f"HRV {hrv:.0f}, below your usual {usual:.0f}")
    elif str(row.get("hrv_status") or "").upper() in ("LOW", "POOR"):
        flags.append(f"HRV status {str(row['hrv_status']).lower()}")
    rhr, rhr_avg = row.get("resting_hr"), row.get("resting_hr_7d_avg")
    if isinstance(rhr, (int, float)) and isinstance(rhr_avg, (int, float)) and rhr >= rhr_avg + 5:
        flags.append(f"resting HR {rhr:.0f}, {rhr - rhr_avg:.0f} above your 7 day average")
    return flags


def summarize_garmin(rows: list[dict], today: date, days: int, tz: ZoneInfo) -> GarminSummary:
    lines = [f"Garmin data from my watch, last {days} days (oldest first):"]
    runs, other, seen = [], [], set()
    for row in rows:
        d = _row_date(row, "day")
        if not d:
            continue
        bits = []
        if (h := _hours(row.get("sleep_seconds"))):
            bits.append(f"sleep {h}" + (f" (score {row['sleep_score']})" if row.get("sleep_score") else ""))
        if row.get("resting_hr"):
            avg = f" (7 day avg {row['resting_hr_7d_avg']})" if row.get("resting_hr_7d_avg") else ""
            bits.append(f"resting HR {row['resting_hr']}{avg}")
        if row.get("hrv_last_night"):
            extra = [f"weekly avg {row['hrv_weekly_avg']:.0f}" if row.get("hrv_weekly_avg") else "", str(row.get("hrv_status") or "").lower()]
            extra = [e for e in extra if e]
            bits.append(f"HRV {row['hrv_last_night']:.0f}" + (f" ({', '.join(extra)})" if extra else ""))
        if row.get("body_battery_high") is not None:
            bits.append(f"body battery high {row['body_battery_high']}, low {row.get('body_battery_low')}")
        lines.append(f"{fmt_day(d)}: " + (", ".join(bits) if bits else "no data"))
        for act in _activities(row):
            key = act.get("activity_id") or (act.get("start"), act.get("type"))
            if key in seen or act.get("source") == "auto_detected":
                continue
            seen.add(key)
            kind = str(act.get("type") or "activity")
            dur = act.get("duration_s")
            dist = act.get("distance_m")
            hr = f", avg HR {act['avg_hr']}" if act.get("avg_hr") else ""
            if "run" in kind.lower():
                text = f"{fmt_day(d)}: {act.get('name') or 'Run'}"
                if isinstance(dist, (int, float)) and dist > 0:
                    text += f", {dist / 1000:.1f} km"
                if isinstance(dur, (int, float)) and dur > 0:
                    text += f" in {_duration(dur)}"
                    if isinstance(dist, (int, float)) and dist > 0:
                        text += f" ({_duration(dur / (dist / 1000))} /km)"
                runs.append((text + hr, dist if isinstance(dist, (int, float)) else 0))
            else:
                mins = f" {dur / 60:.0f} min" if isinstance(dur, (int, float)) and dur > 0 else ""
                other.append(f"{fmt_day(d)}: {kind.replace('_', ' ')}{mins}{hr}")
    if runs:
        total = sum(dist for _, dist in runs) / 1000
        lines.append(f"Runs in these {days} days: {len(runs)}, {total:.1f} km in total.")
        lines += [f"• {text}" for text, _ in runs]
    else:
        lines.append(f"Runs in these {days} days: none recorded.")
    if other:
        lines.append("Other activities: " + "; ".join(other) + ".")
    latest = rows[-1]
    latest_day = _row_date(latest, "day")
    flags: list[str] = []
    if latest_day == today:
        flags = recovery_flags(latest, rows[:-1])
        lines.append(
            "Recovery today looks low: " + ", ".join(flags) + "."
            if flags
            else "Recovery today looks fine."
        )
    elif latest_day:
        lines.append(f"No Garmin data for today yet. The latest is from {fmt_day(latest_day)}, so it may be out of date.")
    sync = ""
    with contextlib.suppress(ValueError, TypeError):
        synced = datetime.fromisoformat(str(latest.get("last_sync")))
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=ZoneInfo("UTC"))
        sync = f", watch last synced {fmt_day(synced.astimezone(tz).date())} {synced.astimezone(tz):%H:%M}"
    short = f"latest data {fmt_day(latest_day)}{sync}" if latest_day else "no dated rows"
    if latest_day == today:
        short += ". Recovery today " + ("looks low: " + ", ".join(flags) if flags else "looks fine")
    return GarminSummary(True, "\n".join(lines), short, flags)


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
        self.garmin = GarminReader(cfg.garmin_db, cfg.garmin_profile)
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

    def week_sessions_text(self, monday: date) -> str | None:
        sessions = self.store.sessions()
        done = []
        for i in range(7):
            entry = sessions.get((monday + timedelta(days=i)).isoformat())
            if entry:
                done.append(f"{DAY_NAMES[i]} {entry['status']}")
        return ", ".join(done) if done else None

    def trend(self) -> str:
        return shoulder_trend(self.store.ratings(), self.today())

    def extra_context(self, now: datetime) -> list[str]:
        today = now.date()
        since = today - timedelta(days=13)
        parts = []
        week = self.week_sessions_text(monday_of(today))
        parts.append(
            f"Sessions this week so far: {week}." if week else "Sessions this week so far: none marked done or skipped yet."
        )
        logs = self.store.logs(since)
        if logs:
            lines = [f"{fmt_day(d)} {r.get('time', '')}: {r['text']}" for r in logs if (d := _row_date(r))]
            parts.append("My workout logs from the last 14 days:\n" + "\n".join(lines))
        else:
            parts.append("My workout logs from the last 14 days: none.")
        ratings = self.store.ratings(since)
        if ratings:
            lines = [
                f"{fmt_day(d)} {r.get('time', '')}: {r['rating']}/10" + (f" ({r['note']})" if r.get("note") else "")
                for r in ratings
                if (d := _row_date(r))
            ]
            parts.append(
                "My left shoulder ratings from the last 14 days (0 = no pain, 10 = worst pain):\n"
                + "\n".join(lines)
            )
        else:
            parts.append("My left shoulder ratings from the last 14 days: none.")
        parts.append("Shoulder trend: " + self.trend())
        last_sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        checkin = self.store.load_checkin(last_sunday)
        if checkin:
            parts.append(f"My Sunday check in answer from {fmt_day(last_sunday)}:\n{checkin}")
        garmin = self.garmin_context(today)
        if garmin:
            parts.append(garmin)
        return parts

    def garmin_summary(self, today: date) -> GarminSummary:
        return self.garmin.summary(today, self.cfg.garmin_days, self.cfg.tz)

    def garmin_context(self, today: date) -> str | None:
        return self.garmin_summary(today).context

    def garmin_profile_line(self, today: date) -> str | None:
        summary = self.garmin_summary(today)
        return summary.short[:1].upper() + summary.short[1:] + "."

    def recovery_note(self, today: date) -> str | None:
        """A heads up for the session reminder when Garmin shows poor recovery."""
        flags = self.garmin_summary(today).recovery_flags
        if not flags:
            return None
        return (
            "⚠️ Your Garmin data says recovery looks low today: " + ", ".join(flags) + ". "
            "Go lighter today, or ask me for a lighter version of this session."
        )

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
        """The Sunday check in answer given just before this week."""
        return self.store.load_checkin(monday - timedelta(days=1))

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

    async def adjust_after_skip(self, day: date) -> PlanResult | None:
        """Rewrite the rest of the week after a skipped session, without doubling up."""
        monday = monday_of(day)
        wd = day.weekday()
        if wd >= 6:
            return None
        async with self.plan_lock:
            plan = self.store.load_plan(monday)
            if not plan:
                return None
            today_plan = parse_plan(plan).days.get(wd)
            focus = today_plan.focus if today_plan else "today's session"
            kept = "Monday" if wd == 0 else f"Monday to {DAY_NAMES[wd]}"
            request = "\n".join(
                [
                    f"I skipped today's session ({DAY_NAMES[wd]}: {focus}).",
                    f"Adjust the rest of this week, {DAY_NAMES[wd + 1]} to Sunday, following your "
                    "rules: do not double up, keep what matters most, and keep my shoulder safe.",
                    f"Keep {kept} exactly as written, but add (skipped) at the end of the "
                    f"{DAY_NAMES[wd]} line.",
                    "Keep this week's main equipment and effort. Return the full week.",
                    "",
                    PLAN_FORMAT_RULES.format(preface_rule=""),
                    "",
                    "This week's plan:",
                    plan,
                ]
            )
            return await self.generate_plan(monday, request, kind="adjusted", save_split=False)

    def capture_checkin(self, message) -> str | None:
        """Save a reply to the Sunday check in, or the next message before the plan time."""
        st = self.store.state().get("checkin")
        if not st:
            return None
        now = self.now()
        replied = bool(message.reply_to_message) and message.reply_to_message.message_id == st.get("message_id")
        until = datetime.fromisoformat(st["until"])
        asked = datetime.fromisoformat(st["asked_at"])
        waiting = not st.get("answered") and asked <= now < until
        if not (replied or waiting):
            return None
        sunday = date.fromisoformat(st["sunday"])
        self.store.save_checkin(sunday, message.text or "")
        st.update(answered=True, answered_at=now.isoformat(timespec="seconds"))
        self.store.update_state(checkin=st)
        if now < until:
            return f"Thanks, I saved your check in. Next week's plan gets built at {self.cfg.plan_time:%H:%M}."
        return (
            "Thanks, I saved your check in. Next week's plan is already built, so send /nextweek "
            "tonight (or /plan from Monday) if you want it rebuilt with this answer."
        )

    def token_reminder(self) -> str | None:
        """A reminder text when the one year Claude token is within a month of expiring."""
        created = self.cfg.token_created
        if not created:
            return None
        try:
            expires = created.replace(year=created.year + 1)
        except ValueError:  # 29 February
            expires = created + timedelta(days=365)
        today = self.today()
        left = (expires - today).days
        if left > 30:
            return None
        last = self.store.state().get("token_reminder") or {}
        if last.get("created") == created.isoformat():
            gap = 1 if left <= 7 else 7
            if (today - date.fromisoformat(last["sent"])).days < gap:
                return None
        self.store.update_state(token_reminder={"created": created.isoformat(), "sent": today.isoformat()})
        steps = (
            "On your computer run claude setup-token, put the new token in bot.env as "
            "CLAUDE_CODE_OAUTH_TOKEN, set CLAUDE_TOKEN_CREATED to today's date, then run "
            "docker compose up -d in the bot folder."
        )
        if left < 0:
            return f"🔑 Your Claude Code token probably expired on {fmt_day(expires)}. {steps}"
        return f"🔑 Your Claude Code token expires in {left} days, on {fmt_day(expires)}. {steps}"

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
        status = self.store.sessions().get(today.isoformat(), {}).get("status")
        if status == "done":
            return "✅ Already marked done today.\n\n" + day.text
        if status == "skipped":
            return "⏭ Marked as skipped today.\n\n" + day.text
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

    def rest_of_week(self, result: PlanResult, skipped: date) -> str:
        text = self.overview(result, "Here is the rest of your week, adjusted:", start=skipped.weekday() + 1)
        text += "\n\nSend /week for the full plan."
        if result.warnings:
            text += "\n\n⚠️ Please check:\n" + "\n".join(f"• {w}" for w in result.warnings)
        return text

    def overview(self, result: PlanResult, heading: str, start: int = 0) -> str:
        parsed = parse_plan(result.text)
        lines = [heading, ""]
        for idx in range(start, 7):
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
    """A plain message in a private chat works like /ask, unless it answers the check in."""
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    saved = coach_of(context).capture_checkin(update.effective_message)
    if saved:
        await reply(update, context, saved)
        return
    await answer_question(update, context, text)


def rating_keyboard(day: date) -> InlineKeyboardMarkup:
    rows = [range(0, 6), range(6, 11)]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(str(n), callback_data=f"rate:{day.isoformat()}:{n}") for n in row] for row in rows]
    )


RATING_QUESTION = "How does your left shoulder feel right now, from 0 (no pain) to 10 (worst pain)?"


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    text = args_text(update)
    if not text:
        logs = coach.store.logs(coach.today() - timedelta(days=13))
        if not logs:
            await reply(update, context, "No logs in the last 2 weeks. Log a session like this:\n/log rows 22kg 3x10, floor press 14kg 3x8 felt easy")
            return
        lines = [f"• {fmt_day(d)}: {r['text']}" for r in logs if (d := _row_date(r))]
        await reply(update, context, "**Your logs from the last 2 weeks**\n\n" + "\n".join(lines))
        return
    now = coach.now()
    coach.store.add_log(now, text)
    await reply(update, context, f"Logged for {fmt_day(now.date())}. Send /done when you finish the session.")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    now = coach.now()
    extra = args_text(update)
    if extra:
        coach.store.add_log(now, extra)
    coach.store.set_session(now.date(), "done", now, "command")
    await reply(
        update,
        context,
        f"✅ Nice work. Today's session is marked done.\n\n{RATING_QUESTION}",
        reply_markup=rating_keyboard(now.date()),
    )


def shoulder_log_text(coach: Coach) -> str:
    ratings = coach.store.ratings()
    if not ratings:
        return "No shoulder ratings yet. You get the buttons after /done, or send /shoulder 3 to add one."
    lines = ["**Left shoulder ratings** (0 = no pain, 10 = worst pain)", ""]
    for r in ratings:
        d = _row_date(r)
        when = f"{d:%a} {d.day} {d:%b %Y}" if d else str(r.get("date"))
        note = f" ({r['note']})" if r.get("note") else ""
        lines.append(f"{when}, {r.get('time', '')}: {r['rating']}/10{note}")
    lines += ["", "Trend: " + coach.trend()]
    return "\n".join(lines)


def rating_feedback(coach: Coach, rating: int) -> str:
    text = "Trend: " + coach.trend()
    if rating >= 6 or shoulder_rising(coach.store.ratings(), coach.today()):
        text += (
            "\n\nThat is on the high side. Keep the left arm light and pain free, and check with "
            "your physio."
        )
    if rating >= 7:
        text += (
            " If the pain is sharp or getting worse, wakes you at night, or comes with numbness, "
            "tingling or sudden weakness, stop training it and see a doctor or physio."
        )
    return text


async def cmd_shoulder(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    arg = args_text(update)
    if arg:
        m = re.fullmatch(r"(\d{1,2})(?:\s*/\s*10)?\s*(.*)", arg, re.DOTALL)
        if not m or int(m.group(1)) > 10:
            await reply(update, context, "Send /shoulder on its own for your log, or /shoulder 3 to save a rating from 0 to 10.")
            return
        now = coach.now()
        rating = int(m.group(1))
        coach.store.add_rating(now.date(), now, rating, m.group(2).strip() or "manual")
        await reply(update, context, f"Left shoulder {rating}/10 saved.\n\n" + rating_feedback(coach, rating))
        return
    await reply(update, context, shoulder_log_text(coach))


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    coach = coach_of(context)
    try:
        _, day_raw, value = query.data.split(":")
        day, rating = date.fromisoformat(day_raw), int(value)
    except ValueError:
        await query.answer()
        return
    coach.store.add_rating(day, coach.now(), rating, "after session")
    await query.answer(f"Saved {rating}/10")
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(f"Left shoulder {rating}/10 saved for {fmt_day(day)}.")
    await send_text(context.bot, query.message.chat.id, rating_feedback(coach, rating))


async def on_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Done or Skipped from the evening check."""
    query = update.callback_query
    coach = coach_of(context)
    try:
        _, day_raw, answer = query.data.split(":")
        day = date.fromisoformat(day_raw)
    except ValueError:
        await query.answer()
        return
    chat_id = query.message.chat.id
    now = coach.now()
    previous = coach.store.sessions().get(day.isoformat(), {}).get("status")
    await query.answer()
    if answer == "done":
        coach.store.set_session(day, "done", now, "button")
        with contextlib.suppress(BadRequest):
            await query.edit_message_text(f"✅ {fmt_day(day)} marked as done. Nice work.")
        await send_text(context.bot, chat_id, RATING_QUESTION, reply_markup=rating_keyboard(day))
        return
    coach.store.set_session(day, "skipped", now, "button")
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(f"⏭ {fmt_day(day)} marked as skipped.")
    if previous == "skipped" or monday_of(day) != monday_of(coach.today()) or day.weekday() >= 6:
        return
    if not coach.store.load_plan(monday_of(day)):
        return
    await send_text(context.bot, chat_id, "No problem. I am adjusting the rest of your week so you do not double up. This takes a minute or two.")
    try:
        async with typing(context.bot, chat_id):
            result = await coach.adjust_after_skip(day)
    except ClaudeError as exc:
        await send_text(context.bot, chat_id, f"I saved the skip but could not adjust the plan. {exc.user_message}")
        return
    if result:
        await send_text(context.bot, chat_id, coach.rest_of_week(result, day))


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
    today = coach.today()
    logs = coach.store.logs(today - timedelta(days=13))
    week = coach.week_sessions_text(monday_of(today))
    lines = [
        "",
        "**Left shoulder**",
        coach.trend(),
        "",
        "**Training**",
        f"• This week: {week or 'nothing marked done or skipped yet'}",
        f"• Workout logs in the last 2 weeks: {len(logs)}",
    ]
    garmin = coach.garmin_profile_line(today)
    if garmin:
        lines += ["", "**Garmin**", garmin]
    return lines


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
        ((when, JOB_LABELS.get(job.name, job.name)) for job in jobs if (when := job_next(job))),
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




def status_extra(coach: Coach) -> list[str]:
    summary = coach.garmin_summary(coach.today())
    icon = "✅" if summary.ok else "⚠️"
    return ["", "**Garmin**", f"• {icon} Profile {coach.cfg.garmin_profile}: {summary.short}"]


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
    application.add_handler(CommandHandler("log", cmd_log))
    application.add_handler(CommandHandler("done", cmd_done))
    application.add_handler(CommandHandler("shoulder", cmd_shoulder))
    application.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    application.add_handler(CallbackQueryHandler(on_check, pattern=r"^chk:", **slow))
    application.add_handler(CommandHandler("injury", cmd_injury))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("status", cmd_status, **slow))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text, **slow)
    )
    application.add_error_handler(on_error)


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

JOB_LABELS = {
    "session_reminder": "Session reminder",
    "session_reminder_fri": "Session reminder",
    "evening_check": "Did you train? check",
    "checkin": "Sunday check in",
    "weekly_plan": "Next week's plan",
    "token_check": "Token expiry check",
    "catch_up": "Start up catch up",
}

CHECKIN_TEXT = """🗓 Weekly check in

How did this week go? Tell me about:
• your energy
• any soreness
• your left shoulder, from 0 (no pain) to 10 (worst pain)

Reply to this message, or just send your next message before {until}. I will use it for next week's plan."""


def job_next(job) -> datetime | None:
    try:
        return job.next_t
    except AttributeError:  # not scheduled yet
        return None


def ptb_days(days: list[int]) -> tuple[int, ...]:
    """python-telegram-bot counts days from Sunday = 0; the rest of the bot from Monday = 0."""
    return tuple(sorted((d + 1) % 7 for d in days))


async def owner_send(context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None) -> list:
    coach = coach_of(context)
    if coach.cfg.owner_id is None:
        log.warning("No ALLOWED_USER_IDS, so reminders have nowhere to go")
        return []
    return await send_text(context.bot, coach.cfg.owner_id, text, reply_markup=reply_markup)


async def job_session_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today's session before the gym."""
    coach = coach_of(context)
    today = coach.today()
    monday = monday_of(today)
    if coach.store.sessions().get(today.isoformat(), {}).get("status") == "done":
        return
    if not coach.store.load_plan(monday):
        if not coach.program_start():
            await owner_send(context, "No training plan yet. Send /plan to build your first week.")
            return
        try:
            await coach.build_week(monday, kind="build", wait=True)
        except ClaudeError as exc:
            await owner_send(context, f"This week has no plan and I could not build one. {exc.user_message} Send /plan to try again.")
            return
    day = parse_plan(coach.store.load_plan(monday) or "").days.get(today.weekday())
    if day is None:
        text = coach.today_text()
    elif day.is_rest:
        text = f"Rest day today.\n\n{day.text}"
    else:
        text = f"🏋️ Today's session\n\n{day.text}"
    note = coach.recovery_note(today)
    if note:
        text += "\n\n" + note
    await owner_send(context, text)


def check_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"chk:{day.isoformat()}:done"),
                InlineKeyboardButton("⏭ Skipped", callback_data=f"chk:{day.isoformat()}:skip"),
            ]
        ]
    )


async def job_evening_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask whether I trained, if /done was not sent on a training day."""
    coach = coach_of(context)
    today = coach.today()
    if coach.store.sessions().get(today.isoformat()):
        return
    plan = coach.store.load_plan(monday_of(today))
    if not plan and not coach.program_start():
        return
    day = parse_plan(plan or "").days.get(today.weekday())
    if day and day.is_rest:
        return
    focus = f" ({day.focus})" if day else ""
    await owner_send(context, f"Did you train today{focus}?", reply_markup=check_keyboard(today))


async def job_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    now = coach.now()
    until = datetime.combine(now.date(), coach.cfg.plan_time, tzinfo=coach.cfg.tz)
    sent = await owner_send(context, CHECKIN_TEXT.format(until=f"{coach.cfg.plan_time:%H:%M}"))
    if sent:
        coach.store.update_state(
            checkin={
                "sunday": now.date().isoformat(),
                "message_id": sent[-1].message_id,
                "asked_at": now.isoformat(timespec="seconds"),
                "until": until.isoformat(timespec="seconds"),
                "answered": False,
            }
        )


async def build_next_week(context: ContextTypes.DEFAULT_TYPE, monday: date) -> None:
    """Build next week's plan with the check in answer and send an overview."""
    coach = coach_of(context)
    sunday = monday - timedelta(days=1)
    st = coach.store.state().get("checkin") or {}
    answered_at = st.get("answered_at") if st.get("sunday") == sunday.isoformat() else None
    existing = coach.store.load_plan(monday)
    meta = coach.store.load_plan_meta(monday)
    if existing and (answered_at is None or meta.get("built_at", "") >= answered_at):
        result = PlanResult(monday, existing, meta, meta.get("warnings", []))
        heading = "🗓 Next week's plan was already built"
    else:
        try:
            result = await coach.build_week(monday, kind="scheduled", wait=True)
        except ClaudeError as exc:
            await owner_send(context, f"I could not build next week's plan. {exc.user_message} Send /nextweek to try again.")
            return
        heading = "🗓 Next week is ready"
    text = coach.overview(result, f"{heading}. {coach.week_label(monday)}")
    text += "\n\nLeft shoulder: " + coach.trend()
    if not coach.store.load_checkin(sunday):
        text += "\n\nI did not get a check in answer this week, so I built it without one."
    if result.warnings:
        text += "\n\n⚠️ Please check:\n" + "\n".join(f"• {w}" for w in result.warnings)
    text += "\n\nSend /week to see the full plan."
    await owner_send(context, text)


async def job_weekly_plan(context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    await build_next_week(context, monday_of(coach.today()) + timedelta(days=7))


async def job_token_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    text = coach_of(context).token_reminder()
    if text:
        await owner_send(context, text)


async def job_catch_up(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After a restart, do what was missed: the check in, next week's plan or this week's plan."""
    coach = coach_of(context)
    cfg = coach.cfg
    if not coach.store.plan_mondays():
        return  # the programme starts with the first /plan
    now = coach.now()
    today = now.date()
    monday = monday_of(today)
    if today.weekday() == 6:
        st = coach.store.state().get("checkin") or {}
        if cfg.checkin_time <= now.time() < cfg.plan_time and st.get("sunday") != today.isoformat():
            await job_checkin(context)
            return
        if now.time() >= cfg.plan_time and not coach.store.load_plan(monday + timedelta(days=7)):
            await owner_send(context, "I was offline at plan time, so I am building next week's plan now.")
            await build_next_week(context, monday + timedelta(days=7))
            return
    if today.weekday() <= 4 and not coach.store.load_plan(monday):
        await owner_send(context, "I was offline when this week's plan was due, so I am building it now.")
        try:
            result = await coach.build_week(monday, kind="build", wait=True)
        except ClaudeError as exc:
            await owner_send(context, f"I could not build this week's plan. {exc.user_message} Send /plan to try again.")
            return
        await owner_send(
            context,
            coach.overview(result, f"🗓 This week's plan. {coach.week_label(monday)}")
            + "\n\nSend /today or /week for the details.",
        )


def schedule_jobs(application: Application) -> None:
    coach: Coach = application.bot_data["coach"]
    cfg = coach.cfg
    jq = application.job_queue
    if jq is None:
        log.error("The job queue is missing. Install python-telegram-bot[job-queue].")
        return
    daily = {"job_kwargs": {"misfire_grace_time": 600, "coalesce": True}}

    def at(t: dtime) -> dtime:
        return t.replace(tzinfo=cfg.tz)

    jq.run_daily(job_session_reminder, at(cfg.reminder_mon_thu), days=ptb_days([0, 1, 2, 3]), name="session_reminder", **daily)
    jq.run_daily(job_session_reminder, at(cfg.reminder_fri), days=ptb_days([4]), name="session_reminder_fri", **daily)
    if cfg.training_days:
        jq.run_daily(job_evening_check, at(cfg.check_time), days=ptb_days(cfg.training_days), name="evening_check", **daily)
    jq.run_daily(job_checkin, at(cfg.checkin_time), days=ptb_days([6]), name="checkin", **daily)
    jq.run_daily(job_weekly_plan, at(cfg.plan_time), days=ptb_days([6]), name="weekly_plan", **daily)
    jq.run_daily(job_token_check, at(cfg.token_check_time), name="token_check", **daily)
    jq.run_once(job_catch_up, 20, name="catch_up")


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
    try:
        application = build_application(cfg)
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:  # noqa: BLE001 - log through the redacting formatter, then let Docker restart us
        log.exception("The bot stopped because of an error. Docker will restart it.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
