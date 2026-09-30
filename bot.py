#!/usr/bin/env python3
"""Private Telegram gym coach bot.

Runs on a NAS in Docker, talks to Telegram with long polling and uses Claude Code in
headless mode (`claude -p`) for every AI answer and weekly plan. Everything the bot
remembers lives in /data as plain files (plans, logs, ratings, check ins, state).
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import dataclasses
import html
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import threading
import time
import traceback
from collections import deque
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote, quote_plus
from zoneinfo import ZoneInfo

import holidays as holiday_calendars

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter
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
HEARTBEAT = Path(tempfile.gettempdir()) / "coach-heartbeat"
HEARTBEAT_MAX_AGE = 180  # seconds; the Docker health check fails after this
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


def parse_switch(value: str | None, default: bool) -> bool:
    raw = _clean(value).lower()
    if not raw:
        return default
    return raw not in ("off", "no", "false", "0")


def parse_optional_time(env: dict, key: str, default: str) -> dtime | None:
    """A missing setting uses the default; an empty one (KEY=) turns that reminder off."""
    if key not in env:
        return parse_time(None, default)
    return parse_time(env[key], default) if _clean(env[key]) else None


def parse_date(value: str | None) -> date | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ConfigError(f"Date '{raw}' should look like 2026-10-01") from exc


def injury_blocked(env: dict) -> list[str]:
    """The built-in list plus INJURY_EXTRA_BLOCKED, or nothing when INJURY_CHECK=off.

    Older bot.env files with INJURY_BLOCKED_MOVEMENTS keep working: a list replaces the
    built-in one and an empty value turns the check off."""
    if _clean(env.get("INJURY_CHECK")).lower() in ("off", "no", "false", "0"):
        return []
    if "INJURY_BLOCKED_MOVEMENTS" in env:
        return parse_list(env["INJURY_BLOCKED_MOVEMENTS"])
    return parse_list(DEFAULT_BLOCKED) + parse_list(env.get("INJURY_EXTRA_BLOCKED"))


MONTH_NAMES = ["january", "february", "march", "april", "may", "june", "july", "august",
               "september", "october", "november", "december"]


def _month(word: str) -> int | None:
    """'Oct', 'october', 'Sept' -> month number; anything else -> None."""
    word = word.lower()
    if word == "sept":
        return 9
    if len(word) < 3:
        return None
    return next((i for i, name in enumerate(MONTH_NAMES, start=1) if name.startswith(word)), None)


def _dated(year: int, month: int, day: int, today: date) -> date:
    """A day and month without a year: this year, or next year if it is well in the past."""
    candidate = date(year, month, day)
    return date(year + 1, month, day) if candidate < today - timedelta(days=7) else candidate


def parse_date_prefix(text: str, today: date) -> tuple[date, str] | None:
    """Read one date at the start of text: 2026-10-08, 8 Oct, Oct 8, 8/10, today, tomorrow, thu."""
    s = text.lstrip()
    try:
        if m := re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\b", s):
            return date(int(m[1]), int(m[2]), int(m[3])), s[m.end():]
        if (m := re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\b", s)) and _month(m[2]):
            return _dated(today.year, _month(m[2]), int(m[1]), today), s[m.end():]
        if (m := re.match(r"([A-Za-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", s)) and _month(m[1]):
            return _dated(today.year, _month(m[1]), int(m[2]), today), s[m.end():]
        if m := re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", s):  # day/month, as in Singapore
            if m[3]:
                year = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
                return date(year, int(m[2]), int(m[1])), s[m.end():]
            return _dated(today.year, int(m[2]), int(m[1]), today), s[m.end():]
    except ValueError:
        return None
    if m := re.match(r"(today|tomorrow)\b", s, re.IGNORECASE):
        return today + timedelta(days=0 if m[1].lower() == "today" else 1), s[m.end():]
    if m := re.match(r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|rsday|urday|sday)?\b", s, re.IGNORECASE):
        target = DAY_LOOKUP[m[1].lower()]
        return today + timedelta(days=(target - today.weekday()) % 7), s[m.end():]
    return None


def parse_away(text: str, today: date) -> tuple[date, date, str]:
    """'8 Oct to 9 Oct Bangkok trip' -> (8 Oct, 9 Oct, 'Bangkok trip'). Raises ValueError."""
    first = parse_date_prefix(text, today)
    if not first:
        raise ValueError("no date")
    start, rest = first
    end = start
    sep = re.match(r"\s*(?:to|until|till|through|-|–|—)\s*", rest, re.IGNORECASE)
    second = parse_date_prefix(rest[sep.end():] if sep else rest, today)
    if second:
        end, rest = second
        if end < start and (start - end).days > 7:  # "28 Dec to 2 Jan"
            end = end.replace(year=end.year + 1)
    elif sep and sep.group(0).strip() not in ("-", "–", "—"):
        raise ValueError("no end date")
    if end < start:
        raise ValueError("end before start")
    if (end - start).days > 60:
        raise ValueError("longer than 60 days")
    return start, end, rest.strip(" ,:;-–—")


@functools.lru_cache(maxsize=16)
def _holiday_calendar(country: str, year: int):
    try:
        return holiday_calendars.country_holidays(country, years=year)
    except (NotImplementedError, KeyError):
        log.warning("No public holiday calendar for HOLIDAYS_COUNTRY=%s", country)
        return {}


def public_holiday(country: str, day: date) -> str | None:
    if not country:
        return None
    return _holiday_calendar(country.upper(), day.year).get(day)


def parse_chat(value: str | None) -> tuple[int, int | None] | None:
    """'-1002069000031/2930' (or ':2930') is a group topic; a bare ID is a whole chat."""
    value = _clean(value)
    if not value:
        return None
    chat, _, topic = value.replace(":", "/").partition("/")
    try:
        return int(chat), int(topic) if topic else None
    except ValueError as exc:
        raise ConfigError("REPAIR_ALERT_CHAT must look like -1002069000031/2930") from exc


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


# A * stands for up to two words, so "overhead * press" also catches "overhead DB press".
DEFAULT_BLOCKED = (
    "overhead * press, shoulder press, military * press, push press, arnold * press, z press, "
    "clean and press, behind the neck, upright * row, dip, wide grip * bench, barbell * bench, "
    "bench press, fly, flye, flies, pec deck, overhead * extension, overhead * carry, "
    "overhead * squat, snatch, jerk, thruster, handstand, kipping, ohp, strict press, behind * neck, "
    "cable * crossover, pike push, high pull, squat to press, french press, incline barbell * press"
)
DEFAULT_ALLOWED = (
    "pec deck rear delt * fly, pec deck reverse * fly, rear delt * fly on the pec deck, "
    "reverse * fly on the pec deck, rear * fly on the pec deck, bent over * fly, "
    "bench press with dumbbell, thoracic extension, fast dip, small dip, short dip, "
    "shallow dip, slight dip, dip to a quarter squat, rowing on the erg, rowing machine, "
    "reverse * fly, reverse * flye, reverse * flies, rear delt * fly, rear delt * flye, "
    "rear delt * flies, rear * fly, rear * flye, rear * flies, reverse pec deck, "
    "rear delt pec deck, dumbbell * bench press, db * bench press, landmine * press, hip dip, "
    "quick dip, dip and jump, dip then jump, snatch grip"
)
DEFAULT_ROTATION = "dumbbells, cables, machines, barbell and kettlebells"


@dataclasses.dataclass
class Config:
    telegram_token: str = dataclasses.field(repr=False)
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
    reminder_mon_thu: dtime | None
    reminder_fri: dtime | None
    check_time: dtime | None
    daily_workout_time: dtime | None
    daily_workout_days: list[int]
    checkin_time: dtime
    plan_time: dtime
    token_check_time: dtime
    garmin_db: str
    garmin_profile: str
    garmin_days: int
    telegram_base_url: str = ""  # empty = api.telegram.org; set for a local Bot API server
    model_check: str = "haiku"
    garmin_auto_done: bool = True
    holidays_country: str = "SG"
    safety_review: bool = True
    structured_plans: bool = True
    self_repair: bool = True
    repair_alert_chat: tuple[int, int | None] | None = None  # (chat, topic) that also gets 🩺 alerts
    claude_retry_delay: float = 5.0
    ask_timeout: int = 240
    plan_timeout: int = 600
    secrets: list[str] = dataclasses.field(default_factory=list, repr=False)

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
            blocked_movements=injury_blocked(env),
            allowed_movements=parse_list(DEFAULT_ALLOWED)
            + parse_list(env.get("INJURY_EXTRA_ALLOWED"))
            + parse_list(env.get("INJURY_ALLOWED_MOVEMENTS")),  # older bot.env files
            reminder_mon_thu=parse_optional_time(env, "REMINDER_TIME_MON_THU", "17:30"),
            reminder_fri=parse_optional_time(env, "REMINDER_TIME_FRI", "17:00"),
            check_time=parse_optional_time(env, "CHECK_TIME", "21:00"),
            daily_workout_time=parse_optional_time(env, "DAILY_WORKOUT_TIME", "07:00"),
            daily_workout_days=parse_days(env.get("DAILY_WORKOUT_DAYS") or "Mon-Sun"),
            checkin_time=parse_time(env.get("CHECKIN_TIME"), "18:00"),
            plan_time=parse_time(env.get("PLAN_TIME"), "20:00"),
            token_check_time=parse_time(env.get("TOKEN_CHECK_TIME"), "10:00"),
            garmin_db=get("GARMIN_DB", "/garmin/monitor.db"),
            garmin_profile=get("GARMIN_PROFILE", "Me"),
            garmin_days=max(1, garmin_days),
            telegram_base_url=get("TELEGRAM_BASE_URL"),
            model_check=get("CLAUDE_MODEL_CHECK", "haiku"),
            garmin_auto_done=parse_switch(env.get("GARMIN_AUTO_DONE"), True),
            holidays_country=get("HOLIDAYS_COUNTRY", "SG") if "HOLIDAYS_COUNTRY" not in env else _clean(env["HOLIDAYS_COUNTRY"]),
            safety_review=parse_switch(env.get("SAFETY_REVIEW"), True),
            structured_plans=parse_switch(env.get("STRUCTURED_PLANS"), True),
            self_repair=parse_switch(env.get("SELF_REPAIR"), True),
            repair_alert_chat=parse_chat(env.get("REPAIR_ALERT_CHAT")),
            claude_retry_delay=float(get("CLAUDE_RETRY_DELAY", "5")),
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


class RecentLog(logging.Handler):
    """The last warnings and errors, for self repair to show Claude."""

    def __init__(self, size: int = 40):
        super().__init__(logging.WARNING)
        self.lines: deque[str] = deque(maxlen=size)

    def emit(self, record: logging.LogRecord) -> None:
        with contextlib.suppress(Exception):
            self.lines.append(self.format(record)[:600])


RECENT_LOG = RecentLog()


def setup_logging(cfg: Config) -> None:
    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", cfg.secrets)
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    RECENT_LOG.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = [handler, RECENT_LOG]
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
        self.repairs: list[dict] = []  # damaged files repaired since the last self check
        for sub in ("plans", "plans/history", "checkins"):
            (self.root / sub).mkdir(parents=True, exist_ok=True)

    # -- helpers ------------------------------------------------------------

    def _write(self, path: Path, text: str) -> None:
        """Write to a temporary file, flush it to disk, then swap it in, so a power cut
        leaves either the old or the new file. JSON files keep the last good version as .bak."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if path.suffix == ".json" and path.exists():
            with contextlib.suppress(OSError, ValueError):
                json.loads(path.read_text(encoding="utf-8"))  # only a good file becomes the backup
                shutil.copyfile(path, path.with_name(path.name + ".bak"))
        os.replace(tmp, path)

    def read_json(self, name: str, default: Any) -> Any:
        path = self.root / name
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return self.repair_file(path, default)
        except OSError:
            log.exception("Could not read %s, using an empty value", path)
            return default

    def repair_file(self, path: Path, default: Any = None) -> Any:
        """A damaged JSON file: move it to data/broken and restore the backup, if it is good."""
        broken = self.root / "broken"
        broken.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        with contextlib.suppress(OSError):
            os.replace(path, broken / f"{path.name}.{stamp}")
        backup = path.with_name(path.name + ".bak")
        try:
            text = backup.read_text(encoding="utf-8")
            data = json.loads(text)
        except (OSError, ValueError):
            log.error("%s was damaged and has no good backup, so it starts empty", path)
            self.repairs.append({"file": self.name_of(path), "restored": False})
            return default
        self._write(path, text)
        log.warning("%s was damaged, restored the backup", path)
        self.repairs.append({"file": self.name_of(path), "restored": True})
        return data

    def name_of(self, path: Path) -> str:
        with contextlib.suppress(ValueError):
            return str(path.relative_to(self.root))
        return path.name

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
        torn = False
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b"\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(("\n" if torn else "") + json.dumps(row, ensure_ascii=False) + "\n")

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

    def load_plan_data(self, monday: date) -> dict | None:
        """The structured version of the plan (for the workout cards), if it has one."""
        return normalize_plan(self.read_json(f"plans/{monday.isoformat()}.plan.json", None))

    def save_plan(self, monday: date, text: str, meta: dict, stamp: str, data: dict | None = None) -> None:
        path = self.plan_path(monday)
        data_path = self.root / "plans" / f"{monday.isoformat()}.plan.json"
        history = self.root / "plans" / "history"
        if path.exists():  # keep every earlier version
            (history / f"{monday.isoformat()}_{stamp}.md").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        if data_path.exists():
            (history / f"{monday.isoformat()}_{stamp}.plan.json").write_text(
                data_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        self._write(path, text.rstrip() + "\n")
        if data:
            self.write_json(f"plans/{monday.isoformat()}.plan.json", data)
        elif data_path.exists():
            data_path.unlink()  # a text plan replaced it
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

    # -- days away ------------------------------------------------------------

    def away(self) -> list[dict]:
        return self.read_json("away.json", [])

    def set_away(self, periods: list[dict]) -> None:
        self.write_json("away.json", sorted(periods, key=lambda p: p["start"]))

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

YT_TAG_RE = re.compile(r"\[\s*yt\s*:\s*([^\]\n]+?)\s*\]", re.IGNORECASE)
DAY_WORDS = (
    "monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    "mon|tues|tue|wed|thurs|thur|thu|fri|sat|sun"
)
DAY_HEADER_RE = re.compile(
    r"^[\s*_>#•\-]*(?:📅|🗓)️?\s*[*_]*\s*(" + DAY_WORDS + r")\b(.*)$",
    re.IGNORECASE,
)
NOTES_RE = re.compile(r"^[\s*_>#•\-]*📝")
# "1. Row", "1) Row", "A1. Row", "1a. Row", "1️⃣ Row", "**1. Row**"
NUMBERED_RE = re.compile(r"^\s*[*_]*\s*([A-Da-d]?\d{1,2}[a-d]?)(?:️?⃣|\s*[.)])\s*(.+)$")
REST_RE = re.compile(r"\b(rest|off)\b", re.IGNORECASE)
STRENGTH_DAY_RE = re.compile(
    r"\b(push|pull|legs?|upper|lower|chest|back|arms?|shoulders?|full body|strength|gym)\b",
    re.IGNORECASE,
)
NOT_GYM_DAY_RE = re.compile(r"\b(rest|off|basketball|game|swim\w*|mobility|recovery)\b", re.IGNORECASE)
TRAINING_WORDS_RE = re.compile(
    r"\b(push|pull|legs?|upper|lower|chest|back|arms?|shoulders?|full body|swim\w*|run\w*|"
    r"conditioning|cardio|strength|core|gym|lift\w*|power|intervals?|plyo\w*|agility|jump\w*|hiit)\b",
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
    if re.match(r"^\W*(rest|off)\b", focus, re.IGNORECASE):
        return True  # "Rest day, off from the gym", "Rest, easy swim optional"
    return bool(REST_RE.search(focus)) and not TRAINING_WORDS_RE.search(focus)


def _header_focus(rest: str) -> str:
    """'(5 Oct): Push' or ', 5 Oct: Push' or ' - Push' -> 'Push'."""
    rest = rest.strip()
    if ":" in rest:
        focus = rest.split(":", 1)[1]
    else:
        m = re.match(r"^[^\-–—,]*?[\-–—,]\s*(.*)$", rest)
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
        block = "\n".join(lines[start:end]).strip()
        if idx in days:  # a second header for the same day, e.g. Friday's legs option
            days[idx].text += "\n\n" + block
        else:
            days[idx] = PlanDay(idx, focus, block)
    preface = "\n".join(lines[: headers[0][0]]).strip()
    notes = "\n".join(lines[notes_start:]).strip() if notes_start is not None else ""
    return ParsedPlan(preface=preface, days=days, notes=notes)


# "6. Rehab: Band external rotation: 2 x 15" names the exercise after the label.
LABEL_RE = re.compile(
    r"^(rehab(?: block)?|prehab|warm ?up|cool ?down|finisher|superset|circuit|core|mobility|"
    r"conditioning|option [a-c1-3])\s*:\s*(?=\S)",
    re.IGNORECASE,
)


def exercise_entry(line: str) -> tuple[str, bool] | None:
    """'1. Single arm row, light: 3 x 10' -> ('Single arm row', False);
    '6. Rehab: Band external rotation: 2 x 15' -> ('Band external rotation', True)."""
    m = NUMBERED_RE.match(line)
    if not m:
        return None
    text = m.group(2).replace("**", "").replace("__", "").strip()
    rehab = False
    label = LABEL_RE.match(text)
    if label:
        rehab = label.group(1).lower() in ("rehab", "rehab block", "prehab")
        text = text[label.end():]
    name = re.split(r"\s*[:(,]|\s[-–—]\s|\s+\d+\s*(?:x|×|sets?\b)", text, maxsplit=1)[0]
    name = name.strip(" .,*_")
    return (name, rehab) if name else None


def exercise_name(line: str) -> str | None:
    entry = exercise_entry(line)
    return entry[0] if entry else None


def plan_exercises(text: str, include_rehab: bool = True) -> list[tuple[int, str]]:
    """(weekday, exercise name) for every numbered line inside a day."""
    out = []
    for day in parse_plan(text).days.values():
        for line in day.text.splitlines()[1:]:
            entry = exercise_entry(line)
            if entry and (include_rehab or not entry[1]):
                out.append((day.index, entry[0]))
    return out


# Words that introduce a movement the line is NOT doing: "Landmine press, swap for overhead
# press" or "Push ups (not dips)". Only the phrase up to the next punctuation is removed.
NEGATION_RE = re.compile(
    r"\b(?:instead of|in place of|rather than|like an?|similar to|(?<!more )(?<!less )than|replac(?:es|ing|ement for)|(?:a )?swap(?:ped)?(?: in)? for|"
    r"alternative to|not|no(?! more than| less than| rest\b)|zero|avoid(?:ing)?|skip(?:ping)?|without|never|"
    r"don'?t|do not)\b[^,.;:()?!\n]*",
    re.IGNORECASE,
)
# A sentence that lists what to leave out: "Shoulder note: no overhead pressing, dips or
# upright rows", "Avoid: dips, upright rows", "Swaps for your shoulder: floor press for ...".
LEFT_OUT_RE = re.compile(
    r"^[\s*_•\->]*(?:"
    r"[^:\n]{0,40}\b(?:avoid\w*|leave out|left out|leaving out|not included|skip\w*|swaps|swap(?=[*_\s]*:)|"
    r"instead|replace\w*|off limits|banned)\b[^:\n]{0,30}[*_]*\s*:"
    r"|(?:[^:\n]{1,30}:\s*)?[*_]*\s*(?:no(?! more than| less than| rest\b)|avoid\w*|skip\w*|leave out|"
    r"left out|leaving out|don'?t|do not|never|without|instead of|rather than|not(?! too\b))\b"
    r"|.*\b(?:stays?|remains?) out\b|.*\b(?:once|until|when)\b.{0,20}\b(?:physio|doctor)\b)",
    re.IGNORECASE,
)
# In the allowed list a * may stand for up to two words, but never for words that start
# a second exercise ("dumbbell pullover and barbell bench press").
ALLOWED_GAP = r"(?:(?!(?:and|or|then|plus|with|barbell|bar|superset)\b)[\w'-]+\s+){0,3}?"
# In the blocked list a * may not jump across joining words ("sit upright and row").
BLOCKED_GAP = r"(?:(?!(?:and|or|then|to|into|with|for|from|plus)\b)[\w'-]+\s+){0,2}?"
SKIP_LINE_RE = re.compile(r"^[\s*_•\-]*(?:form\s+|coaching\s+)?(?:video|cues?)[*_]*\s*[:\-–]", re.IGNORECASE)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!;])\s+|,\s*(?=(?:so|but|then|instead)\b)", re.IGNORECASE)
# "(overhead press alternative)", "(dip replacement)": the move is named, not prescribed.
ALT_NOUN_RE = re.compile(r"[\s-]+(?:alternatives?|replacements?|substitutes?|subs?|swaps?)\b", re.IGNORECASE)


def _term_regex(term: str, gap: str = BLOCKED_GAP) -> re.Pattern:
    parts = []
    for word in term.lower().split():
        if word == "*":
            parts.append(gap)
        else:  # plurals and -ing forms: dip/dips, tricep/triceps, press/pressing
            if word.endswith("y"):  # fly/flys/flyes/flies, carry/carries
                parts.append(re.escape(word[:-1]) + r"(?:y(?:e?s)?|ies)\b[\s-]*")
                continue
            suffix = r"(?:e?s)?" if word.endswith("e") else r"(?:e?s|ing)?"
            parts.append(re.escape(word) + suffix + r"\b[\s-]*")
    body = "".join(parts)
    body = body[: -len(r"[\s-]*")] if body.endswith(r"[\s-]*") else body
    return re.compile(r"\b" + body, re.IGNORECASE)


def _clauses(line: str) -> list[str]:
    """Sentences of a line, with anything in brackets checked as its own clause."""
    line = YT_TAG_RE.sub(" ", line).replace("**", " ").replace("__", " ").replace("\u2019", "'")
    line = re.sub(r"\s[&+]\s", " and ", line)
    out = []
    for sentence in SENTENCE_SPLIT_RE.split(line):
        out.extend(re.findall(r"\(([^)]*)\)", sentence))
        out.append(re.sub(r"\([^)]*\)", " ", sentence))
    return out


def find_blocked(text: str, blocked: list[str], allowed: list[str]) -> list[dict]:
    """Lines inside a day that contain a movement the injury rules leave out.

    Every line of a day is checked (exercises, warm ups, finishers, options), except the
    header and the Cue and Video lines. Each sentence and each bracket is checked on its own:
    sentences that list what to leave out are skipped, negated phrases and allowed movements
    are removed, and what is left is searched for blocked movements.
    """
    blocked_res = [(term, _term_regex(term)) for term in blocked]
    allowed_res = [_term_regex(term, ALLOWED_GAP) for term in sorted(set(allowed), key=len, reverse=True)]
    hits = []
    for day in parse_plan(text).days.values():
        for line in day.text.splitlines():
            if not line.strip() or DAY_HEADER_RE.match(line) or SKIP_LINE_RE.match(line):
                continue
            found = None
            checks = [(clause, True) for clause in _clauses(line)]
            entry = exercise_entry(line)
            if entry:  # "1. Bench dips: no added weight" names the move even if a cue follows
                checks.insert(0, (_clauses(entry[0])[-1], False))
            for clause, may_skip in checks:
                if may_skip and LEFT_OUT_RE.match(clause):
                    continue
                check = NEGATION_RE.sub(" ", clause)
                for pattern in allowed_res:
                    check = pattern.sub(" ", check)
                found = next(
                    (term for term, pattern in blocked_res
                     if any(not ALT_NOUN_RE.match(check, m.end()) for m in pattern.finditer(check))),
                    None,
                )
                if found:
                    break
            if found:
                hits.append({"day": day.name, "line": line.strip(), "term": found})
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


SPARK = "▁▂▃▄▅▆▇█"


def weekly_sparkline(ratings: list[dict], today: date, weeks: int = 8) -> str | None:
    """Weekly average shoulder rating as a tiny bar chart, oldest week first (· = no rating)."""
    this_monday = monday_of(today)
    if not any((d := _row_date(r)) and d >= this_monday for r in ratings):
        this_monday -= timedelta(days=7)  # early in the week: end the chart at last week
    bars, values = [], []
    for k in range(weeks - 1, -1, -1):
        start = this_monday - timedelta(weeks=k)
        week = [r["rating"] for r in ratings if (d := _row_date(r)) and start <= d < start + timedelta(days=7)]
        if week:
            avg = sum(week) / len(week)
            values.append(avg)
            bars.append(SPARK[min(len(SPARK) - 1, round(avg / 10 * (len(SPARK) - 1)))])
        else:
            bars.append("·")
    if not values:
        return None
    return f"{''.join(bars)}  (weekly average, last {weeks} weeks, oldest first)"


WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|kgs|kilos?|lb|lbs)\b", re.IGNORECASE)
SETS_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+)", re.IGNORECASE)


def _norm_exercise(name: str) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    if words and len(words[-1]) > 3 and words[-1].endswith("s") and not words[-1].endswith("ss"):
        words[-1] = words[-1][:-1]  # rows -> row, dips -> dip
    return " ".join(words)


def parse_log_items(text: str) -> list[dict]:
    """'rows 22kg 3x10, floor press 14kg 3x8 felt easy' -> one item per exercise."""
    items = []
    for segment in re.split(r"[,;\n]|\bthen\b", text):
        weight, sets = WEIGHT_RE.search(segment), SETS_RE.search(segment)
        if not (weight or sets):
            continue
        cut = min(m.start() for m in (weight, sets) if m)
        name = re.sub(r"^[\s\d.)-]+", "", segment[:cut]).strip(" -:@")
        if not re.search(r"[A-Za-z]", name):  # "45 lb landmine press 3x8": the name comes after
            first, other = sorted((m for m in (weight, sets) if m), key=lambda m: m.start())[0], None
            other = next((m for m in (weight, sets) if m and m is not first and m.start() > first.end()), None)
            between = segment[first.end(): other.start() if other else len(segment)]
            name = re.split(r"\s\d", between, maxsplit=1)[0].strip(" -:@,")
        if not name or not re.search(r"[A-Za-z]", name):
            continue
        kg = None
        if weight:
            kg = float(weight[1]) * (0.4536 if weight[2].lower().startswith("lb") else 1)
            kg = round(kg, 1)
        items.append({
            "name": name, "key": _norm_exercise(name), "kg": kg,
            "sets": int(sets[1]) if sets else None, "reps": int(sets[2]) if sets else None,
        })
    return items


def _fmt_kg(kg: float | None) -> str:
    return "" if kg is None else (f"{kg:g} kg")


def _fmt_item(item: dict) -> str:
    parts = [p for p in (_fmt_kg(item["kg"]), f"{item['sets']} x {item['reps']}" if item["sets"] else "") if p]
    return " ".join(parts)


def progress_history(logs: list[dict], since: date | None = None) -> dict[str, list[dict]]:
    """Exercise -> dated entries parsed from the workout logs, oldest first."""
    history: dict[str, list[dict]] = {}
    for row in logs:
        d = _row_date(row)
        if not d or (since and d < since):
            continue
        for item in parse_log_items(str(row.get("text", ""))):
            history.setdefault(item["key"], []).append({**item, "date": d})
    return history


def shoulder_rising(ratings: list[dict], today: date) -> bool:
    return "rising" in shoulder_trend(ratings, today) or "went up" in shoulder_trend(ratings, today)


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------

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
        raw: list[str] = []  # the plain text behind each placeholder

        def stash(snippet: str, plain: str = "") -> str:
            keep.append(snippet)
            raw.append(plain)
            return f"\x00{len(keep) - 1}\x00"

        def unstash(text: str) -> str:  # code inside a link or [yt:] tag becomes plain text
            return re.sub("\x00(\\d+)\x00", lambda m: raw[int(m.group(1))], text)

        line = CODE_RE.sub(
            lambda m: stash(f"<code>{html.escape(m.group(1), quote=False)}</code>", m.group(1)), line
        )
        line = YT_TAG_RE.sub(
            lambda m: stash(
                f'<a href="{html.escape(yt_search_url(unstash(m.group(1))))}">'
                f"▶️ {html.escape(unstash(m.group(1)).strip(), quote=False)}</a>",
                "▶️ " + unstash(m.group(1)).strip(),
            ),
            line,
        )
        line = MD_LINK_RE.sub(
            lambda m: stash(
                f'<a href="{html.escape(unstash(m.group(2)))}">{html.escape(unstash(m.group(1)), quote=False)}</a>',
                unstash(m.group(1)),
            ),
            line,
        )
        line = html.escape(line, quote=False)
        line = BOLD_RE.sub(r"<b>\1</b>", line)
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
    def __init__(self, user_message: str, detail: str = "", transient: bool = False):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.transient = transient  # worth one automatic retry
        self.timed_out = False


AUTH_RE = re.compile(
    r"invalid api key|authenticat|unauthori[sz]ed|\b401\b|oauth token|token (?:has )?expired|"
    r"not logged in|/login|login required|invalid bearer|credentials",
    re.IGNORECASE,
)
LIMIT_RE = re.compile(
    r"usage limit|rate limit|\b429\b|overloaded|\b529\b|quota|credit balance|limit reached",
    re.IGNORECASE,
)
# Brief problems on Anthropic's side or the network: one automatic retry usually works.
TRANSIENT_RE = re.compile(
    r"overloaded|\b529\b|\b50[0234]\b|internal server error|service unavailable|bad gateway|"
    r"api_error|ECONNRESET|ETIMEDOUT|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|socket hang up|fetch failed|"
    r"connection error|network is unreachable",
    re.IGNORECASE,
)
PERMANENT_RE = re.compile(r"usage limit|rate limit|\b429\b|quota|credit balance|limit reached", re.IGNORECASE)

MSG_AUTH = (
    "Claude Code could not sign in, so the token probably needs renewing. On your computer run "
    "claude setup-token, put the new token in bot.env as CLAUDE_CODE_OAUTH_TOKEN, update "
    "CLAUDE_TOKEN_CREATED, then restart the bot."
)
NETWORK_RE = re.compile(
    r"ECONNREFUSED|ENOTFOUND|ETIMEDOUT|ECONNRESET|EAI_AGAIN|connection refused|connection error|"
    r"fetch failed|getaddrinfo|network is unreachable",
    re.IGNORECASE,
)
MSG_LIMIT = "Claude is busy or your usage limit is reached. Please try again a bit later."
MSG_NETWORK = (
    "Claude Code could not reach Anthropic's servers. Check the NAS internet connection, "
    "then try again."
)


class ClaudeRunner:
    """Runs `claude -p` as an async subprocess from an empty folder."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._slots = asyncio.Semaphore(2)
        self._status_slot = asyncio.Semaphore(1)  # /status checks: one small process at a time
        self.last_call: dict | None = None  # outcome of the latest real call, for /status

    def child_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
        for key in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
            if not env.get(key, "").strip():
                env.pop(key, None)
        if env.get("CLAUDE_CODE_OAUTH_TOKEN"):
            env.pop("ANTHROPIC_API_KEY", None)  # the subscription token wins
        env["DISABLE_AUTOUPDATER"] = "1"
        return env

    def command(self, kind: str, prompt_file: str, schema: dict | None = None) -> list[str]:
        model = {"ask": self.cfg.model_ask, "review": self.cfg.model_check}.get(kind, self.cfg.model_plan)
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
        if schema:  # the reply comes back as data through a tool call, so it needs 2+ turns
            cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
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

    async def run(self, system_prompt: str, message: str, kind: str, schema: dict | None = None) -> str:
        """kind is 'ask' (web search on), 'plan' or 'review' (no tools). Returns the reply text,
        or with a schema the structured reply as JSON text.

        A brief failure (overloaded, a 5xx error, a network blip) is retried once."""
        for attempt in (1, 2):
            try:
                result = await self._run(system_prompt, message, kind, schema)
            except ClaudeError as exc:
                if exc.transient and attempt == 1:
                    log.warning("claude %s failed briefly, retrying once in %ss", kind, self.cfg.claude_retry_delay)
                    await asyncio.sleep(self.cfg.claude_retry_delay)
                    continue
                self.last_call = {"at": now_in(self.cfg.tz), "kind": kind, "ok": False, "message": exc.user_message}
                raise
            self.last_call = {"at": now_in(self.cfg.tz), "kind": kind, "ok": True, "message": ""}
            return result
        raise AssertionError("unreachable")

    async def _run(self, system_prompt: str, message: str, kind: str, schema: dict | None = None) -> str:
        timeout = self.cfg.plan_timeout if kind == "plan" else self.cfg.ask_timeout
        fd, prompt_file = tempfile.mkstemp(prefix="coach-system-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)
        cmd = self.command(kind, prompt_file, schema)
        started = asyncio.get_running_loop().time()
        try:
            async with self._slots:
                code, out, err = await self._exec(cmd, message.encode("utf-8"), timeout)
        except asyncio.TimeoutError as exc:
            minutes = round(timeout / 60)
            error = ClaudeError(
                f"Claude Code took longer than {minutes} minutes, so I stopped it. "
                "Please try again, or ask something shorter."
            )
            error.timed_out = True
            raise error from exc
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
        try:
            return self._parse_result(code, out, err, kind, took)
        except ClaudeError as exc:
            text = f"{exc} {out[-2000:]} {err[-2000:]}"
            exc.transient = bool(TRANSIENT_RE.search(text)) and not PERMANENT_RE.search(text) and not AUTH_RE.search(text)
            raise

    def _parse_result(self, code: int, out: str, err: str, kind: str, took: float) -> str:
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
            if NETWORK_RE.search(err + out):
                raise ClaudeError(MSG_NETWORK, detail)
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
                raise ClaudeError(  # not worth retrying: the same request would run out again
                    "Claude Code ran out of steps before it finished. Please try again, or ask "
                    "in a simpler way.",
                    detail,
                )
            text = f"{result} {err}"
            if AUTH_RE.search(text):
                raise ClaudeError(MSG_AUTH, detail)
            if LIMIT_RE.search(text):
                raise ClaudeError(MSG_LIMIT, detail)
            if NETWORK_RE.search(text):
                raise ClaudeError(MSG_NETWORK, detail)
            snippet = redact((result or subtype or "unknown error").strip()[:200], self.cfg.secrets)
            raise ClaudeError(f"Claude Code reported an error: {snippet}", detail)
        structured = data.get("structured_output")
        if isinstance(structured, (dict, list)):
            return json.dumps(structured, ensure_ascii=False)
        if not result.strip():
            raise ClaudeError("Claude Code sent an empty reply. Please try again.")
        return result.strip()

    async def version(self) -> str:
        try:
            async with self._status_slot:
                code, out, err = await self._exec([self.cfg.claude_bin, "--version"], None, 30)
        except (FileNotFoundError, asyncio.TimeoutError, OSError) as exc:
            return f"not available ({type(exc).__name__})"
        return (out or err).strip().splitlines()[0] if (out or err).strip() else f"exit {code}"

    async def auth_status(self) -> tuple[bool, str]:
        try:
            async with self._status_slot:
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

# Garmin activity types that count as a training session for the 9pm check.
WORKOUT_TYPES = (
    "strength", "fitness_equipment", "hiit", "cardio", "swim", "run", "cycling", "basketball",
    "rowing", "elliptical", "stair", "boxing", "tennis", "pickleball", "badminton",
)

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

    def week_stats(self, start: date, end: date) -> list[str]:
        """Runs, sleep and resting heart rate between two dates, as short lines."""
        try:
            rows = self.rows(start, end)
        except (FileNotFoundError, sqlite3.Error):
            return []
        if not rows:
            return []
        km, runs, seen = 0.0, 0, set()
        for row in rows:
            for act in _activities(row):
                key = act.get("activity_id") or (act.get("start"), act.get("type"))
                if "run" in str(act.get("type", "")).lower() and act.get("source") != "auto_detected" and key not in seen:
                    seen.add(key)
                    runs += 1
                    km += (act.get("distance_m") or 0) / 1000
        sleep = [r["sleep_seconds"] / 3600 for r in rows if isinstance(r.get("sleep_seconds"), (int, float)) and r["sleep_seconds"] > 0]
        rhr = [r["resting_hr"] for r in rows if isinstance(r.get("resting_hr"), (int, float))]
        out = [f"Runs: {runs}, {km:.1f} km" if runs else "Runs: none recorded"]
        if sleep:
            out.append(f"Sleep: {sum(sleep) / len(sleep):.1f} h a night on average")
        if rhr:
            out.append(f"Resting heart rate: {sum(rhr) / len(rhr):.0f} on average")
        return out

    def workouts(self, day: date, min_minutes: int = 15) -> list[dict]:
        """Workouts recorded on the watch that day (gym, swim, run, ride, basketball...).

        Returns [] when the database cannot be read, so callers never fail on Garmin."""
        try:
            rows = self.rows(day, day)
        except (FileNotFoundError, sqlite3.Error):
            return []
        out = []
        for row in rows:
            for act in _activities(row):
                kind = str(act.get("type") or "").lower()
                minutes = (act.get("duration_s") or 0) / 60 if isinstance(act.get("duration_s"), (int, float)) else 0
                if act.get("source") == "auto_detected" or minutes < min_minutes:
                    continue
                if any(word in kind for word in WORKOUT_TYPES):
                    out.append({"type": kind, "minutes": round(minutes), "name": act.get("name") or kind})
        return out

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
Thursday or Friday is reserved for legs and cardio.
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
5. For the legs and cardio day, include a cardio block (run, bike, rower or similar) with distance or time and a target pace or effort.
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


SAFETY_MARK = "SAFETY REVIEW"


def injury_rules() -> str:
    """The Injury section of the coach prompt, for the safety review."""
    start = COACH_PROMPT.index("Injury\n")
    end = COACH_PROMPT.index("\n\nBasketball")
    return COACH_PROMPT[start:end].strip()


PLAN_FORMAT_RULES = """Format rules. The bot reads your plan automatically, so follow them exactly:
1. Cover all seven days, Monday to Sunday, in order.
2. Start each day with one line in exactly this form: 📅 Monday: Push. Use the day name, a colon and the day's focus. Mark rest days in the focus, for example 📅 Saturday: Basketball or rest, or 📅 Sunday: Rest or light mobility.
3. On training days write the warm up as one line starting with "Warm up:". Then number the main exercises and the rehab exercises, one per line, like "1. Exercise name: 3 x 10, rest 90s". Under each exercise add one "Cue:" line and one "Video: [yt: exercise name proper form]" line. Finish the day with one line starting with "Cool down:".
4. After Sunday, write the general notes once, starting with a line that begins with 📝. Do not use 📝 anywhere else.
5. Write nothing before the first 📅 line{preface_rule}."""


# ---------------------------------------------------------------------------
# Structured plans: Claude returns data, the bot lays out the workout cards
# ---------------------------------------------------------------------------

_EXERCISE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "superset": {"type": "string", "description": "A1, A2 when two exercises are paired, else empty"},
        "sets": {"type": "integer"},
        "reps": {"type": "string", "description": "10, 8-10, 30 s or 400 m"},
        "load": {"type": "string", "description": "12.5 kg, bodyweight, light band or easy pace"},
        "rest_s": {"type": "integer", "description": "rest between sets in seconds"},
        "effort": {"type": "string", "description": "RPE 7, or 3 reps in reserve"},
        "tempo": {"type": "string", "description": "3-1-1, or empty"},
        "muscles": {"type": "string", "description": "the muscles it trains"},
        "cue": {"type": "string", "description": "one short form cue"},
        "left_arm": {"type": "string", "description": "how the injured left arm does it, or empty"},
        "video": {"type": "string", "description": "YouTube search words, like single arm cable row proper form"},
        "swap": {"type": "string", "description": "a shoulder friendly swap"},
    },
    "required": ["name", "sets", "reps", "load", "rest_s", "effort", "muscles", "cue", "video"],
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "split_explanation": {"type": "string"},
        "days": {
            "type": "array",
            "minItems": 7,
            "maxItems": 7,
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "string", "enum": DAY_NAMES},
                    "focus": {"type": "string"},
                    "rest_day": {"type": "boolean"},
                    "minutes": {"type": "integer"},
                    "warm_up": {"type": "array", "items": {"type": "string"}},
                    "sections": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "body_part": {"type": "string"},
                                "exercises": {"type": "array", "items": _EXERCISE_SCHEMA},
                            },
                            "required": ["body_part", "exercises"],
                        },
                    },
                    "cool_down": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                "required": ["day", "focus", "rest_day", "warm_up", "sections", "cool_down"],
            },
        },
        "notes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["days", "notes"],
}
STRUCTURED_RULES = """How to fill in the plan. The bot lays it out for Telegram, so write plain words in every field (no markdown, no emojis):
1. Seven days, Monday to Sunday, in order. A rest day has rest_day true and a focus like "Rest or light mobility" or "Basketball or rest"; it may hold a short Mobility section.
2. Group each training day into sections by body part, in training order: the main compound work first, then secondary and accessory work, then core, conditioning or cardio. Name sections by body part, like Back, Chest, Shoulders, Arms, Legs, Glutes, Core, Court skills, Conditioning, Cardio, Run, Swim or Mobility. On upper body days add a "Shoulder rehab" section.
3. For every exercise give sets, reps (10, 8-10 or 30 s), load (a real starting weight in kg based on my logs, or bodyweight or light band), rest in seconds, effort as RPE that matches this week's effort, tempo, the muscles it trains, one short form cue, how my left arm does it (left_arm, only when the exercise uses the arms), YouTube search words for a form video (like "single arm cable row proper form") and a shoulder friendly swap. Pair two exercises as a superset with A1 and A2 in superset when that saves time.
4. Friday has a "Swim" section and a "Legs (if you do not swim)" section, plus the cardio block with distance or time and pace.
5. Keep each day within my session time and put the total in minutes.
6. warm_up and cool_down are short lists of steps. note is one or two short sentences for the day, or empty.
7. notes are the week's general notes, one short sentence each.
8. The video field holds only the search words, without [yt: ]. Write no dashes; use commas instead.
9. {split_rule}"""

DAY_SCHEMA = PLAN_SCHEMA["properties"]["days"]["items"]
DAY_RULES = """How to fill in the session. The bot lays it out for Telegram, so write plain words in every field (no markdown, no emojis):
1. Group the exercises into sections by body part, in training order, like the plan. Keep a "Shoulder rehab" section on upper body days.
2. For every exercise give sets, reps, load (a real weight in kg, or bodyweight or light band), rest in seconds, effort as RPE, tempo, the muscles it trains, one short form cue, how my left arm does it (left_arm, only when the exercise uses the arms), YouTube search words for a form video and a shoulder friendly swap.
3. Put the new total time in minutes. warm_up and cool_down are short lists of steps. note is one or two short sentences on what changed, or empty.
4. The video field holds only the search words, without [yt: ]. Write no dashes; use commas instead."""

_PLAN_TEXT_FIELDS = ("name", "superset", "reps", "load", "effort", "tempo", "muscles", "cue", "left_arm", "video", "swap")


def _one_line(value: Any) -> str:
    return " ".join(str(value).split()) if value not in (None, "") else ""


def _as_int(value: Any) -> int:
    with contextlib.suppress(TypeError, ValueError):
        return max(0, int(float(value)))
    return 0


def normalize_plan(data: Any) -> dict | None:
    """Check and tidy the plan Claude returned; None when it is not a usable plan."""
    if isinstance(data, str):
        with contextlib.suppress(ValueError):
            data = json.loads(clean_reply(data))
    if not isinstance(data, dict) or not isinstance(data.get("days"), list):
        return None
    days: dict[int, dict] = {}
    for raw in data["days"]:
        if not isinstance(raw, dict):
            continue
        idx = DAY_LOOKUP.get(_one_line(raw.get("day")).lower())
        if idx is None or idx in days:
            continue
        sections = []
        for sec in raw.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            exercises = []
            for ex in sec.get("exercises") or []:
                if isinstance(ex, dict) and _one_line(ex.get("name")):
                    item = {key: _one_line(ex.get(key)).replace("**", "")[:300] for key in _PLAN_TEXT_FIELDS}
                    item["video"] = re.sub(r"^\[?\s*yt:\s*|\]$", "", item["video"], flags=re.IGNORECASE).strip()
                    item.update(sets=_as_int(ex.get("sets")), rest_s=_as_int(ex.get("rest_s")))
                    exercises.append(item)
            if exercises:
                sections.append({"body_part": _one_line(sec.get("body_part")) or "Workout", "exercises": exercises})
        focus = _one_line(raw.get("focus")) or ("Rest" if raw.get("rest_day") else "Training")
        if raw.get("rest_day") is True and not is_rest_focus(focus):
            focus += " (rest day)"  # the reminders read rest days from the focus
        days[idx] = {
            "day": DAY_NAMES[idx],
            "focus": focus,
            "rest_day": bool(raw.get("rest_day")),
            "minutes": _as_int(raw.get("minutes")),
            "warm_up": [_one_line(w) for w in raw.get("warm_up") or [] if _one_line(w)],
            "sections": sections,
            "cool_down": [_one_line(c) for c in raw.get("cool_down") or [] if _one_line(c)],
            "note": _one_line(raw.get("note")),
        }
    if not days:
        return None
    return {
        "split_explanation": _one_line(data.get("split_explanation")),
        "days": [days[i] for i in sorted(days)],
        "notes": [_one_line(n) for n in data.get("notes") or [] if _one_line(n)],
    }


def _dose(ex: dict) -> str:
    parts = [f"{ex['sets']} x {ex['reps']}" if ex["sets"] else ex["reps"]]
    if ex["load"]:
        parts.append(ex["load"])
    if ex["rest_s"]:
        parts.append(f"rest {ex['rest_s']}s")
    if ex["effort"]:
        parts.append(ex["effort"])
    if ex["tempo"]:
        parts.append(f"tempo {ex['tempo']}")
    return ", ".join(p for p in parts if p)


def plan_to_text(plan: dict) -> str:
    """The plan in the plain 📅 format that /today, the checks and the history files use."""
    lines: list[str] = []
    if plan["split_explanation"]:
        lines += [plan["split_explanation"], ""]
    for day in plan["days"]:
        lines.append(f"📅 {day['day']}: {day['focus']}")
        if day["warm_up"]:
            lines.append("Warm up: " + "; ".join(day["warm_up"]))
        number = 0
        for sec in day["sections"]:
            rehab = "rehab" in sec["body_part"].lower()
            lines.append(f"{sec['body_part']}:")
            for ex in sec["exercises"]:
                number += 1
                lines.append(f"{number}. {'Rehab: ' if rehab else ''}{ex['name']}: {_dose(ex)}")
                for label, key in (("Cue", "cue"), ("Left arm", "left_arm"), ("Muscles", "muscles")):
                    if ex[key]:
                        lines.append(f"{label}: {ex[key]}")
                if ex["video"]:
                    lines.append(f"Video: [yt: {ex['video']}]")
                if ex["swap"]:
                    lines.append(f"Swap: {ex['swap']}")
        if day["cool_down"]:
            lines.append("Cool down: " + "; ".join(day["cool_down"]))
        if day["note"]:
            lines.append(f"Note: {day['note']}")
        lines.append("")
    if plan["notes"]:
        lines.append("📝 Notes")
        lines += [f"• {note}" for note in plan["notes"]]
    return "\n".join(lines).strip()


BODY_PART_EMOJI = [
    ("rehab", "🩹"), ("warm", "🔥"), ("cool", "🧊"), ("chest", "🫸"), ("back", "🔙"),
    ("lat", "🔙"), ("shoulder", "🏋️"), ("arm", "💪"), ("bicep", "💪"), ("tricep", "💪"),
    ("glute", "🍑"), ("hip", "🍑"), ("leg", "🦵"), ("quad", "🦵"), ("hamstring", "🦵"),
    ("calf", "🦵"), ("core", "🧱"), ("abs", "🧱"), ("court", "🏀"), ("jump", "🏀"),
    ("plyo", "🏀"), ("basketball", "🏀"), ("swim", "🏊"), ("run", "🏃"), ("conditioning", "❤️‍🔥"),
    ("cardio", "❤️‍🔥"), ("mobility", "🧘"), ("full body", "🏋️"),
]


def body_part_emoji(name: str) -> str:
    lower = name.lower()
    return next((emoji for word, emoji in BODY_PART_EMOJI if word in lower), "🏋️")


def _h(text: str) -> str:
    return html.escape(text, quote=False)


def _dose_html(ex: dict) -> str:
    parts = [f"<code>{_h(f'{ex['sets']} × {ex['reps']}' if ex['sets'] else ex['reps'])}</code>"]
    if ex["load"]:
        parts.append(f"<b>{_h(ex['load'])}</b>")
    if ex["rest_s"]:
        rest = ex["rest_s"]
        parts.append(f"rest {rest // 60} min {rest % 60} s" if rest >= 60 and rest % 60 else
                     (f"rest {rest // 60} min" if rest >= 60 else f"rest {rest} s"))
    if ex["effort"]:
        parts.append(_h(ex["effort"]))
    return " · ".join(parts)


def day_card_blocks(day: dict, subtitle: str = "", when: date | None = None) -> list[str]:
    """One day's workout as Telegram HTML blocks, grouped by body part."""
    name = f"{day['day']} {when.day} {when:%b}" if when else day["day"]
    head = [f"<b>📅 {_h(name)} · {_h(day['focus'])}</b>"]
    if subtitle:
        head.append(f"<i>{_h(subtitle)}</i>")
    sets = sum(ex["sets"] for sec in day["sections"] for ex in sec["exercises"])
    parts = list(dict.fromkeys(sec["body_part"] for sec in day["sections"]))
    stats = []
    if day["minutes"]:
        stats.append(f"⏱ About {day['minutes']} min")
    if parts:
        stats.append("🎯 " + ", ".join(_h(p) for p in parts))
    if sets and not day["rest_day"]:
        stats.append(f"{sets} sets")
    if stats:
        head.append(" · ".join(stats))
    if day["note"]:
        head.append(f"💬 {_h(day['note'])}")
    blocks = ["\n".join(head)]
    if day["warm_up"]:
        blocks.append("<b>🔥 WARM UP</b>\n" + "\n".join(f"• {_h(w)}" for w in day["warm_up"]))
    number = 0
    for sec in day["sections"]:
        title = f"<b>{body_part_emoji(sec['body_part'])} {_h(sec['body_part'].upper())}</b>"
        if "rehab" in sec["body_part"].lower():
            title += " <i>(confirm with your physio)</i>"
        cards = []
        for ex in sec["exercises"]:
            number += 1
            label = ex["superset"] or str(number)
            lines = [f"<b>{_h(label)} · {_h(ex['name'])}</b>", _dose_html(ex)]
            details = []
            if ex["cue"]:
                details.append(f"💡 {_h(ex['cue'])}")
            if ex["left_arm"]:
                details.append(f"🦾 Left arm: {_h(ex['left_arm'])}")
            if VIDEO_LINK_RE.fullmatch(ex["video"]):  # a real video link: shown with a preview
                details.append(f'▶️ <a href="{html.escape(ex["video"])}">Form video</a>')
            elif ex["video"]:
                details.append(f'▶️ <a href="{html.escape(yt_search_url(ex["video"]))}">Form video: {_h(ex["video"])}</a>')
            if ex["muscles"]:
                details.append(f"🎯 {_h(ex['muscles'])}")
            if ex["tempo"]:
                details.append(f"🐢 Tempo {_h(ex['tempo'])}")
            if ex["swap"]:
                details.append(f"🔁 Swap: {_h(ex['swap'])}")
            if details:
                lines.append("<blockquote expandable>" + "\n".join(details) + "</blockquote>")
            cards.append("\n".join(lines))
        if cards:  # the title stays with the first card when the day spans two messages
            blocks += [title + "\n\n" + cards[0], *cards[1:]]
    if day["cool_down"]:
        blocks.append("<b>🧊 COOL DOWN</b>\n" + "\n".join(f"• {_h(c)}" for c in day["cool_down"]))
    if not day["rest_day"] and day["sections"]:
        blocks.append("📝 Log your weights with /log, then send /done when you finish.")
    return blocks


def week_summary_blocks(plan: dict, title: str, marks: dict[str, str] | None = None) -> list[str]:
    """The whole week in short: each day's body parts with sets, reps and loads.
    marks: day name -> ✅ or ⏭ for sessions already done or skipped."""
    head = f"<b>🗓 {_h(title)}</b>"
    if plan["split_explanation"]:
        head += f"\n<blockquote expandable>🧠 {_h(plan['split_explanation'])}</blockquote>"
    blocks = [head]
    for day in plan["days"]:
        top = f"<b>📅 {_h(day['day'])} · {_h(day['focus'])}</b>"
        if marks and marks.get(day["day"]):
            top += f" {marks[day['day']]}"
        if day["minutes"] and day["sections"]:
            top += f" · ⏱ {day['minutes']} min"
        lines = [top]
        for sec in day["sections"]:
            items = []
            for ex in sec["exercises"]:
                dose = f"{ex['sets']}×{ex['reps']}" if ex["sets"] else ex["reps"]
                load = f" @ {ex['load']}" if re.search(r"\d", ex["load"]) else ""
                items.append(f"{_h(ex['name'])} {_h(dose)}{_h(load)}")
            lines.append(f"{body_part_emoji(sec['body_part'])} <b>{_h(sec['body_part'])}</b>: " + " · ".join(items))
        if not day["sections"] and day["note"]:
            lines.append(_h(day["note"]))
        blocks.append("\n".join(lines))
    if plan["notes"]:
        blocks.append("<b>📝 Notes</b>\n<blockquote expandable>" + "\n".join(f"• {_h(n)}" for n in plan["notes"]) + "</blockquote>")
    blocks.append("Send /today for today's full workout, or /day fri for any day.")
    return blocks


def _visible(html_text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


def html_to_plain(html_text: str) -> str:
    """Card HTML as plain text, with links written out."""
    text = re.sub(r'<a href="([^"]*)">(.*?)</a>', lambda m: f"{m.group(2)}: {m.group(1)}", html_text)
    return _visible(text)


ENTITY_RE = re.compile(r"<(?:b|i|u|s|code|pre|a|blockquote)[\s>]")


def pack_blocks(blocks: list[str], limit: int = 3800) -> list[str]:
    """Join HTML blocks into messages under Telegram's length limit (and well under its
    limit on formatted pieces per message)."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if (_tg_len(html_to_plain(candidate)) <= limit and len(candidate) <= 3 * limit
                and len(ENTITY_RE.findall(candidate)) <= 90):
            current = candidate
            continue
        if current:
            messages.append(current)
        current = block
    if current:
        messages.append(current)
    return messages


async def send_blocks(bot, chat_id: int, blocks: list[str], reply_markup=None) -> list:
    """Send ready made HTML blocks; plain text if Telegram rejects the HTML."""
    sent = []
    messages = pack_blocks(blocks)
    for i, message in enumerate(messages):
        markup = reply_markup if i == len(messages) - 1 else None
        links = [html.unescape(href) for href in re.findall(r'href="([^"]+)"', message)]
        video = next((link for link in links if VIDEO_LINK_RE.fullmatch(link)), None)
        preview = LinkPreviewOptions(url=video) if video else LinkPreviewOptions(is_disabled=True)
        try:
            msg = await bot.send_message(chat_id, message, parse_mode=ParseMode.HTML,
                                         link_preview_options=preview, reply_markup=markup)
        except BadRequest as exc:
            log.warning("Telegram rejected the HTML (%s), sending plain text", exc)
            chunks = split_text(html_to_plain(message))
            for k, chunk in enumerate(chunks):
                msg = await bot.send_message(chat_id, chunk, link_preview_options=preview,
                                             reply_markup=markup if k == len(chunks) - 1 else None)
        sent.append(msg)
    return sent


# ---------------------------------------------------------------------------
# The coach: context, questions and plans
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class PlanResult:
    monday: date
    text: str
    meta: dict
    warnings: list[str]
    reused: bool = False  # an existing plan was kept instead of building a new one
    data: dict | None = None  # the structured plan behind the workout cards


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

    def week_in_numbers(self, monday: date) -> str:
        """Sessions, logs, shoulder and Garmin totals for one week (Monday to Sunday)."""
        sunday = monday + timedelta(days=6)
        in_week = lambda d: d is not None and monday <= d <= sunday  # noqa: E731
        sessions = [v for k, v in self.store.sessions().items() if in_week(_row_date({"date": k}))]
        done = sum(1 for v in sessions if v.get("status") == "done")
        skipped = sum(1 for v in sessions if v.get("status") == "skipped")
        plan = self.store.load_plan(monday)
        planned = sum(1 for d in parse_plan(plan or "").days.values() if not d.is_rest) if plan else None
        lines = ["**Last week in numbers**"]
        session_line = f"• Sessions: {done} done, {skipped} skipped"
        if planned:
            session_line += f" ({planned} training days planned)"
        lines.append(session_line)
        logs = [r for r in self.store.logs() if in_week(_row_date(r))]
        lines.append(f"• Workout logs: {len(logs)}")
        ratings = self.store.ratings()
        week = [r["rating"] for r in ratings if in_week(_row_date(r))]
        before = [r["rating"] for r in ratings if (d := _row_date(r)) and monday - timedelta(days=7) <= d < monday]
        if week:
            text = f"• Left shoulder: average {sum(week) / len(week):.1f}"
            if before:
                text += f" ({sum(before) / len(before):.1f} the week before)"
            lines.append(text)
        stats = self.garmin.week_stats(monday, sunday)
        if stats:
            lines += [f"• {line}" for line in stats]
        return "\n".join(lines)

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
        history = progress_history(self.store.logs(), today - timedelta(weeks=8))
        if history:
            recent = sorted(history.items(), key=lambda kv: kv[1][-1]["date"], reverse=True)[:12]
            lines = [
                f"• {entries[-1]['name']}: " + ", ".join(f"{_fmt_item(e)} ({fmt_day(e['date'])})" for e in entries[-4:])
                for _, entries in recent
            ]
            parts.append("Weights and reps from my logs, last 8 weeks (oldest to newest per exercise):\n" + "\n".join(lines))
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
        off = self.days_off(today, today + timedelta(days=13))
        if off:
            parts.append(
                "Days off in the next two weeks (away, on leave or a public holiday). On these days "
                "give a hotel gym or bodyweight version, or move the session:\n"
                + "\n".join(f"{fmt_day(d)}: {label}" for d, label in off)
            )
        garmin = self.garmin_context(today)
        if garmin:
            parts.append(garmin)
        return parts

    def holiday(self, day: date) -> str | None:
        return public_holiday(self.cfg.holidays_country, day)

    def away_on(self, day: date) -> dict | None:
        for period in self.store.away():
            if period["start"] <= day.isoformat() <= period["end"]:
                return period
        return None

    def days_off(self, start: date, end: date) -> list[tuple[date, str]]:
        """Days away or public holidays between two dates, with a short label."""
        out = []
        day = start
        while day <= end:
            away = self.away_on(day)
            holiday = self.holiday(day)
            if away:
                out.append((day, "away" + (f" ({away['note']})" if away.get("note") else "")))
            elif holiday:
                out.append((day, f"public holiday ({holiday})"))
            day += timedelta(days=1)
        return out

    def day_off_line(self, day: date) -> str | None:
        away = self.away_on(day)
        if away:
            return "✈️ You are away today" + (f" ({away['note']})" if away.get("note") else "") + "."
        holiday = self.holiday(day)
        return f"🎉 Public holiday today: {holiday}." if holiday else None

    def garmin_summary(self, today: date) -> GarminSummary:
        return self.garmin.summary(today, self.cfg.garmin_days, self.cfg.tz)

    def garmin_context(self, today: date) -> str | None:
        return self.garmin_summary(today).context

    def garmin_profile_line(self, today: date) -> str | None:
        summary = self.garmin_summary(today)
        return summary.short[:1].upper() + summary.short[1:] + "."

    def recovery_note(self, today: date, rest_day: bool = False) -> str | None:
        """A heads up for the day's message when Garmin shows poor recovery."""
        flags = self.garmin_summary(today).recovery_flags
        if not flags:
            return None
        advice = (
            "Good timing for a rest day. Keep it easy and get an early night."
            if rest_day
            else "Go lighter today, or tap Lighter version below."
        )
        return "⚠️ Your Garmin data says recovery looks low today: " + ", ".join(flags) + ". " + advice

    def tz_label(self) -> str:
        return "Singapore time" if str(self.cfg.tz) == "Asia/Singapore" else f"{self.cfg.tz} time"

    def system_prompt(self, planning: date | None = None) -> str:
        """planning: the Monday of the week a plan is being built for, if not this week."""
        now = self.now()
        today = now.date()
        monday = monday_of(today)
        week = self.week_number(monday)
        parts = [coach_prompt(self.cfg), "Context from the bot (use it, do not repeat it back)"]
        parts.append(
            f"Today is {fmt_long(today)}, {now:%H:%M} {self.tz_label()}. The current week is week "
            f"{week} of my programme (week of {fmt_long(monday)}). Its main equipment is "
            f"{self.equipment_for(week)}. Its effort: {effort_for(week)}"
        )
        if planning and planning != monday:
            target = self.week_number(planning)
            parts.append(
                f"You are now building the plan for a different week: week {target}, the week of "
                f"{fmt_long(planning)}. Its main equipment is {self.equipment_for(target)}. Its "
                f"effort: {effort_for(target)} Follow the plan request for that week."
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
            for _, name in plan_exercises(text or "", include_rehab=False):  # rehab may repeat
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
        for day in parsed.days.values():
            if STRENGTH_DAY_RE.search(day.focus) and not NOT_GYM_DAY_RE.search(day.focus) and not any(
                exercise_name(line) for line in day.text.splitlines()[1:]
            ):
                problems.append(
                    f"{day.name} ({day.focus}) has no numbered exercises. Number each exercise like "
                    "\"1. Exercise name: 3 x 10, rest 90s\"."
                )
        for hit in find_blocked(text, self.cfg.blocked_movements, self.cfg.allowed_movements):
            problems.append(
                f"{hit['day']}, \"{hit['line']}\": {hit['term'].replace(' * ', ' ')} is a movement my injury rules "
                "leave out."
            )
        return problems

    def plan_request(self, monday: date, notes: str, structured: bool = False) -> str:
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
        off = self.days_off(monday, sunday)
        if off:
            lines += [
                "",
                "Days off this week. On these days give a hotel gym or bodyweight version, or move "
                "the session, as your rules say:",
                *[f"{fmt_day(d)}: {label}" for d, label in off],
            ]
        if notes.strip():
            lines += ["", f"My notes for this plan: {notes.strip()}"]
        lines += ["", self.format_rules(bool(split), structured)]
        return "\n".join(lines)

    @staticmethod
    def format_rules(had_split: bool, structured: bool) -> str:
        if structured:
            return STRUCTURED_RULES.format(
                split_rule="Leave split_explanation empty." if had_split else
                "Put the short split comparison, and why you picked this split, in split_explanation."
            )
        return PLAN_FORMAT_RULES.format(preface_rule="" if had_split else ", except the short split comparison")

    @staticmethod
    def plan_json(data: dict) -> str:
        return json.dumps(data, ensure_ascii=False)

    def fix_request(self, text: str, problems: list[str], had_split: bool, data: dict | None = None) -> str:
        return "\n".join(
            [
                "Your plan below has problems the bot found:",
                *[f"• {p}" for p in problems],
                "",
                "Rewrite the full plan. Give a shoulder friendly swap for every movement listed, "
                "add any missing days, and keep everything else the same.",
                "",
                self.format_rules(had_split, structured=data is not None),
                "",
                "The plan:",
                self.plan_json(data) if data else text,
            ]
        )

    async def safety_review(self, text: str) -> list[str]:
        """A second opinion from a small model: plan lines that break the injury rules."""
        injury, _ = self.injury_text()
        system = (
            "You check weekly training plans for safety before a coach bot sends them. You never "
            f"write or rewrite plans.\n\nThe athlete's injury rules:\n{injury_rules()}\n\n"
            f"Latest injury notes: {injury.strip() or 'none'}"
        )
        message = (
            f"{SAFETY_MARK}\n"
            "Check every exercise, warm up and finisher in the plan below against the injury rules. "
            "Flag only movements the rules leave out, or ones that clearly load the left shoulder "
            "heavily. Rehab exercises, swaps, notes that say what to avoid, and single arm work "
            "with the left arm kept light are fine.\n"
            "If nothing breaks the rules, reply with exactly: OK\n"
            "Otherwise reply with one line per problem and nothing else, in this form:\n"
            "PROBLEM: <day>: <exercise line>: <which rule it breaks>\n\n"
            f"The plan:\n{text}"
        )
        reply = await self.runner.run(system, message, "review")
        return [
            "Safety review: " + line.split(":", 1)[1].strip()
            for line in reply.splitlines()
            if line.strip().upper().startswith("PROBLEM:") and line.split(":", 1)[1].strip()
        ]

    def read_plan_reply(self, reply: str, tidy=None) -> tuple[str, dict | None]:
        """Claude's plan as (text, data). Data is None when the reply was plain text."""
        data = normalize_plan(reply) if reply.lstrip().startswith("{") else None
        if data is None:
            return clean_reply(reply), None
        if tidy:
            data = tidy(data)
        return plan_to_text(data), data

    async def first_draft(self, system: str, make_request, tidy) -> tuple[str, dict | None]:
        """The plan as structured data, or in the text format if that fails for a reason
        other than sign in, limits, the network or time."""
        if self.cfg.structured_plans:
            try:
                reply = await self.runner.run(system, make_request(True), "plan", PLAN_SCHEMA)
                return self.read_plan_reply(reply, tidy)
            except ClaudeError as exc:
                if exc.timed_out or exc.user_message in (MSG_AUTH, MSG_LIMIT, MSG_NETWORK):
                    raise
                log.warning("The structured plan failed (%s), asking for the text format", exc)
        return self.read_plan_reply(await self.runner.run(system, make_request(False), "plan"))

    async def generate_plan(
        self, monday: date, make_request, *, kind: str, save_split: bool, notes: str = "", tidy=None
    ) -> PlanResult:
        """Run Claude, check the plan (rules, then a safety review), fix it once if needed, save it.

        make_request(structured) -> the request, for the structured or the text format.
        tidy(data) -> data adjusts a structured plan before the checks, like keeping past days."""
        had_split = bool(self.store.state().get("split"))
        system = self.system_prompt(planning=monday)
        text, data = await self.first_draft(system, make_request, tidy)
        problems = self.plan_problems(text)
        reviewed: list[str] = []
        if self.cfg.safety_review and self.cfg.blocked_movements:
            try:
                reviewed = await self.safety_review(text)
            except ClaudeError as exc:
                log.warning("The safety review failed, the rule check still applies: %s", exc.user_message)
            problems += reviewed
        fixed = False
        warnings: list[str] = []
        if problems:
            log.info("Plan for %s has %d problems, asking Claude to fix it once", monday, len(problems))
            try:
                candidate, candidate_data = self.read_plan_reply(
                    await self.runner.run(
                        system, self.fix_request(text, problems, had_split, data), "plan", PLAN_SCHEMA if data else None
                    ),
                    tidy,
                )
                if len(parse_plan(candidate).missing_days) <= len(parse_plan(text).missing_days) and len(
                    self.plan_problems(candidate)
                ) <= len(problems):
                    text, data, fixed = candidate, candidate_data, True
                else:
                    warnings.append("Claude's fixed version was worse, so I kept the first version.")
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
            "safety_review": reviewed,
            "warnings": warnings,
            "structured": data is not None,
        }
        self.store.save_plan(monday, text, meta, now.strftime("%Y%m%d-%H%M%S"), data)
        state_update: dict[str, Any] = {
            "last_plan_built": {"at": meta["built_at"], "monday": monday.isoformat(), "kind": kind}
        }
        parsed = parse_plan(text)
        if save_split and len(parsed.days) == 7:
            state_update["split"] = {str(i): d.focus for i, d in sorted(parsed.days.items())}
        self.store.update_state(**state_update)
        return PlanResult(monday, text, meta, warnings, data=data)

    async def build_week(
        self, monday: date, notes: str = "", *, kind: str = "build", wait: bool = False, keep_if=None
    ) -> PlanResult:
        """keep_if(meta) -> True keeps an existing plan. It is checked after taking the lock,
        so a scheduled build never overwrites a plan the user built a moment earlier."""
        if self.plan_lock.locked() and not wait:
            raise PlanBusy()
        async with self.plan_lock:
            existing = self.store.load_plan(monday)
            if existing and keep_if is not None:
                meta = self.store.load_plan_meta(monday)
                if keep_if(meta):
                    return PlanResult(monday, existing, meta, meta.get("warnings", []), reused=True,
                                      data=self.store.load_plan_data(monday))
            if not self.program_start():
                self.store.update_state(program_start=monday.isoformat())
            week = self.week_number(monday)
            state = self.store.state()
            save_split = not state.get("split") or week == 1 or "split" in notes.lower()
            return await self.generate_plan(
                monday, lambda structured: self.plan_request(monday, notes, structured),
                kind=kind, save_split=save_split, notes=notes,
            )

    async def adjust_after_skip(self, day: date) -> PlanResult | None:
        """Rewrite the rest of the week after a skipped session, without doubling up.

        Days up to today stay as they are, even when an old Skipped button is tapped late."""
        monday = monday_of(day)
        wd = day.weekday()
        upto = max(day, self.today()).weekday() if monday_of(self.today()) == monday else wd
        if upto >= 6:
            return None
        async with self.plan_lock:
            plan = self.store.load_plan(monday)
            if not plan:
                return None
            today_plan = parse_plan(plan).days.get(wd)
            focus = today_plan.focus if today_plan else "today's session"
            kept = "Monday" if upto == 0 else f"Monday to {DAY_NAMES[upto]}"
            which = "today's session" if upto == wd else f"{DAY_NAMES[wd]}'s session"
            data = self.store.load_plan_data(monday)

            def request(structured: bool) -> str:
                where = f"{DAY_NAMES[wd]} focus" if structured else f"{DAY_NAMES[wd]} line"
                return "\n".join(
                    [
                        f"I skipped {which} ({DAY_NAMES[wd]}: {focus}).",
                        f"Adjust the rest of this week, {DAY_NAMES[upto + 1]} to Sunday, following your "
                        "rules: do not double up, keep what matters most, and keep my shoulder safe.",
                        f"Keep {kept} exactly as written, but add (skipped) at the end of the {where}.",
                        "Keep this week's main equipment and effort. Return the full week.",
                        "",
                        self.format_rules(True, structured),
                        "",
                        "This week's plan:",
                        self.plan_json(data) if structured and data else plan,
                    ]
                )

            def keep_past(new: dict) -> dict:
                """Days up to today stay exactly as they were; the skipped day is marked."""
                old = {DAY_LOOKUP[d["day"].lower()]: d for d in data["days"]}
                days = {DAY_LOOKUP[d["day"].lower()]: d for d in new["days"]}
                for i in range(upto + 1):
                    if i in old:
                        days[i] = dict(old[i])
                if wd in days and "skipped" not in days[wd]["focus"].lower():
                    days[wd] = {**days[wd], "focus": days[wd]["focus"] + " (skipped)"}
                return {**new, "days": [days[i] for i in sorted(days)],
                        "split_explanation": data.get("split_explanation", "")}

            return await self.generate_plan(
                monday, request, kind="adjusted", save_split=False, tidy=keep_past if data else None
            )

    async def alt_session(self, day: date, kind: str) -> tuple[dict, str, list[str]] | None:
        """A lighter (kind "light") or 30 minute ("short") version of the day's session as
        card data: (day data, the question, injury warnings). None when plans use the text
        format or the structured reply fails, so the caller asks for a text answer instead.
        Sign in, limit, network and time out errors are raised."""
        if not self.cfg.structured_plans:
            return None
        monday = monday_of(day)
        name = DAY_NAMES[day.weekday()]
        data = self.store.load_plan_data(monday)
        entry = next((d for d in data["days"] if d["day"] == name), None) if data else None
        parsed = parse_plan(self.store.load_plan(monday) or "").days.get(day.weekday())
        focus = entry["focus"] if entry else (parsed.focus if parsed else "")
        when = "today" if day == self.today() else name
        session = f"{when}'s session" + (f" ({focus})" if focus else "")
        if kind == "short":
            question = f"I only have 30 minutes {when}. Give me a 30 minute version of {session}."
            how = ("Keep the most important work, drop the rest, use supersets and shorter rests. "
                   "The total, warm up and cool down included, must be 30 minutes or less.")
        else:
            question = f"Give me a lighter version of {session}, based on my recovery and my shoulder."
            how = ("Use fewer sets, lighter loads and an effort of about RPE 5 to 6. Keep the "
                   "same body parts where it makes sense.")
        current = self.plan_json(entry) if entry else (parsed.text if parsed else "No session is planned.")
        request = "\n".join([question, how, f"Return only this one day, {name}.", "", DAY_RULES, "",
                             "The session now:", current])

        def read(reply: str) -> dict | None:
            raw = normalize_plan(reply) if reply.lstrip().startswith("{") else None
            if raw is None:  # Claude answers with one day, not a week
                with contextlib.suppress(ValueError):
                    one = json.loads(reply)
                    if isinstance(one, dict):
                        raw = normalize_plan({"days": [{**one, "day": name}]})
            return raw["days"][0] if raw and raw["days"] else None

        def blocked(new: dict) -> list[str]:
            text = plan_to_text({"split_explanation": "", "days": [new], "notes": []})
            return [
                f"\"{hit['line']}\": {hit['term'].replace(' * ', ' ')} is a movement my injury rules leave out."
                for hit in find_blocked(text, self.cfg.blocked_movements, self.cfg.allowed_movements)
            ]

        system = self.system_prompt()
        try:
            new = read(await self.runner.run(system, request, "day", DAY_SCHEMA))
            problems = blocked(new) if new else []
            if problems:  # one fix, like the weekly plans
                fix = "\n".join(["Your session below has problems the bot found:", *[f"• {p}" for p in problems],
                                 "", "Rewrite it with a shoulder friendly swap for each, and keep everything else.",
                                 f"Return only this one day, {name}.", "", DAY_RULES, "", "The session:",
                                 self.plan_json(new)])
                fixed = read(await self.runner.run(system, fix, "day", DAY_SCHEMA))
                if fixed and len(blocked(fixed)) < len(problems):
                    new = fixed
                problems = blocked(new)
        except ClaudeError as exc:
            if exc.timed_out or exc.user_message in (MSG_AUTH, MSG_LIMIT, MSG_NETWORK):
                raise
            log.warning("The structured %s version failed (%s), asking for text", kind, exc)
            return None
        if new is None:
            return None
        new["day"] = name
        if focus:  # the same session, lighter or shorter; the card's subtitle says which
            new["focus"] = focus
        return new, question, problems

    def capture_checkin(self, message) -> str | None:
        """Save a reply to the Sunday check in, or the next message before the plan time."""
        st = self.store.state().get("checkin")
        sender = message.from_user.id if message.from_user else None
        if not st or sender != self.cfg.owner_id:
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

    def token_expires(self) -> date | None:
        created = self.cfg.token_created
        if not created:
            return None
        try:
            return created.replace(year=created.year + 1)
        except ValueError:  # 29 February
            return created + timedelta(days=365)

    def token_reminder(self) -> str | None:
        """A reminder text when the one year Claude token is within a month of expiring."""
        created = self.cfg.token_created
        expires = self.token_expires()
        if not created or not expires:
            return None
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

    # -- views: workout cards for structured plans, text for older plans ------

    def day_status_lines(self, day: date) -> list[str]:
        lines = [line for line in [self.day_off_line(day)] if line]
        when = "today" if day == self.today() else f"on {DAY_NAMES[day.weekday()]}"
        if when != "today":
            lines = [line.replace(" today", f" {when}") for line in lines]
        status = self.store.sessions().get(day.isoformat(), {}).get("status")
        if status == "done":
            lines.append(f"✅ Already marked done {when}.")
        if status == "skipped":
            lines.append(f"⏭ Marked as skipped {when}.")
        return lines

    def day_view(self, day: date, heading: str = "", notes: list[str] | None = None) -> list[str] | None:
        """The day's workout card (HTML blocks), or None when the week has no structured plan."""
        monday = monday_of(day)
        data = self.store.load_plan_data(monday)
        entry = next((d for d in data["days"] if d["day"] == DAY_NAMES[day.weekday()]), None) if data else None
        if entry is None:
            return None
        top = ([f"**{heading}**"] if heading else []) + self.day_status_lines(day) + (notes or [])
        blocks = [to_html("\n".join(top))] if top else []
        return blocks + day_card_blocks(entry, self.week_label(monday), day)

    def day_text(self, day: date) -> str:
        plan = self.store.load_plan(monday_of(day))
        if not plan:
            return "No plan is saved for this week yet. Send /plan to build one."
        found = parse_plan(plan).days.get(day.weekday())
        if not found:
            return (
                f"I could not find {DAY_NAMES[day.weekday()]} in the plan, so here is "
                f"the whole week.\n\n{plan}"
            )
        prefix = self.day_status_lines(day)
        return "\n".join(prefix) + "\n\n" + found.text if prefix else found.text

    def today_text(self) -> str:
        return self.day_text(self.today())

    def today_view(self) -> str | list[str]:
        return self.day_view(self.today()) or self.today_text()

    def upcoming(self, weekday: int) -> date:
        """That weekday this week, or next week's once it has passed and next week is built."""
        today = self.today()
        day = monday_of(today) + timedelta(days=weekday)
        if day < today and self.store.load_plan(monday_of(today) + timedelta(days=7)):
            day += timedelta(days=7)
        return day

    def any_day_view(self, weekday: int) -> str | list[str]:
        day = self.upcoming(weekday)
        return self.day_view(day) or self.day_text(day)

    def week_to_show(self) -> tuple[date, str]:
        """This week's Monday, or next week's on weekends once it is built, plus a note."""
        today = self.today()
        monday = monday_of(today)
        if today.weekday() >= 5:
            nxt = monday + timedelta(days=7)
            if self.store.load_plan(nxt):
                return nxt, ""
            return monday, (
                "Next week's plan is not built yet. It gets built on Sunday at "
                f"{self.cfg.plan_time:%H:%M} after your check in, or send /nextweek to build it now."
            )
        return monday, ""

    def week_marks(self, monday: date) -> dict[str, str]:
        sessions = self.store.sessions()
        marks = {}
        for i in range(7):
            status = sessions.get((monday + timedelta(days=i)).isoformat(), {}).get("status")
            if status in ("done", "skipped"):
                marks[DAY_NAMES[i]] = "✅" if status == "done" else "⏭"
        return marks

    def week_view(self) -> str | list[str]:
        monday, note = self.week_to_show()
        data = self.store.load_plan_data(monday)
        if not data:
            return self.week_text()
        blocks = week_summary_blocks(data, self.week_label(monday), self.week_marks(monday))
        if note:
            blocks.insert(0, to_html(f"{note} Here is this week's plan until then."))
        return blocks

    def plan_view(self, result: PlanResult) -> str | list[str]:
        """The reply after /plan or /nextweek."""
        if not result.data:
            return self.plan_reply(result)
        blocks = week_summary_blocks(result.data, self.week_label(result.monday))
        if result.warnings:
            blocks.insert(-1, to_html("⚠️ Please check:\n" + "\n".join(f"• {w}" for w in result.warnings)))
        return blocks

    def week_text(self) -> str:
        monday, note = self.week_to_show()
        plan = self.store.load_plan(monday)
        if not plan:
            return "No plan is saved for this week yet. Send /plan to build one." + (f" {note}" if note else "")
        if note:
            return f"{note.strip()} Here is this week's plan until then.\n\n**{self.week_label(monday)}**\n\n{plan.strip()}"
        return f"**{self.week_label(monday)}**\n\n{plan.strip()}"

    def rest_of_week(self, result: PlanResult, skipped: date) -> str:
        text = self.overview(result, "Here is the rest of your week, adjusted:", start=skipped.weekday() + 1)
        text += "\n\nSend /week for the full plan."
        if result.warnings:
            text += "\n\n⚠️ Please check:\n" + "\n".join(f"• {w}" for w in result.warnings)
        return text

    def overview(self, result: PlanResult, heading: str, start: int = 0) -> str:
        if result.data:
            lines = [heading, ""]
            for day in result.data["days"]:
                idx = DAY_LOOKUP[day["day"].lower()]
                if idx < start:
                    continue
                lines.append(f"📅 {day['day'][:3]}: {day['focus']}")
                for sec in day["sections"]:
                    names = ", ".join(ex["name"] for ex in sec["exercises"])
                    lines.append(f"{body_part_emoji(sec['body_part'])} {sec['body_part']}: {names}")
                lines.append("")
            return "\n".join(lines).strip()
        parsed = parse_plan(result.text)
        lines = [heading, ""]
        for idx in range(start, 7):
            day = parsed.days.get(idx)
            if not day:
                continue
            entries = [exercise_entry(l) for l in day.text.splitlines()[1:]]
            names = [f"{name} (rehab)" if rehab else name for name, rehab in filter(None, entries)]
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
    ("today", "Today's workout"),
    ("week", "This week's plan (next week's on weekends)"),
    ("day", "Any day's workout, like /day fri"),
    ("plan", "Rebuild this week's plan, with optional notes"),
    ("nextweek", "Build next week's plan now"),
    ("log", "Log what you did"),
    ("done", "Mark today's session finished"),
    ("shoulder", "Your shoulder rating log"),
    ("progress", "Weights from your logs"),
    ("injury", "Show or replace your injury notes"),
    ("away", "Days away, on leave or travelling"),
    ("profile", "What the coach knows about you"),
    ("status", "Claude Code, sign in and reminders"),
    ("reset", "Clear the chat memory"),
    ("whoami", "Your Telegram user ID"),
]

HELP_TEXT = """Hi, I am your gym coach.

• Just write to me, or use /ask, with any training question.
• /today shows today's workout, /day fri any day's, and /week the whole week.
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


async def send_view(bot, chat_id: int, view: str | list[str], reply_markup=None) -> list:
    """Text goes through the Markdown to HTML path; a list is ready made HTML blocks."""
    if isinstance(view, list):
        return await send_blocks(bot, chat_id, view, reply_markup=reply_markup)
    return await send_text(bot, chat_id, view, reply_markup=reply_markup)


async def reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str | list[str], reply_markup=None):
    return await send_view(context.bot, update.effective_chat.id, text, reply_markup=reply_markup)


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Only ALLOWED_USER_IDS in private chats get through. Everyone else learns their own ID.

    Every path that does not return raises ApplicationHandlerStop, even when the reply
    to a stranger fails, so no other handler ever runs for them.
    """
    coach = coach_of(context)
    user = update.effective_user
    chat = update.effective_chat
    private = chat is not None and chat.type == ChatType.PRIVATE
    fresh = update.message is not None or update.callback_query is not None  # not edits
    if user is not None and user.id in coach.cfg.allowed_ids and private and fresh:
        return
    try:
        if user is not None and private and user.id not in coach.cfg.allowed_ids:
            if update.callback_query:
                await update.callback_query.answer("This is a private bot.")
            elif update.message:
                recent = context.application.bot_data.setdefault("stranger_replies", {})
                stamp = coach.now().timestamp()
                if stamp - recent.get(user.id, 0) > 30:
                    recent[user.id] = stamp
                    log.info("Message from a user who is not allowed: %s", user.id)
                    await context.bot.send_message(
                        chat.id,
                        f"Sorry, this is a private bot. Your Telegram user ID is <code>{user.id}</code>.",
                        parse_mode=ParseMode.HTML,
                    )
    except Exception as exc:  # noqa: BLE001 - never let a failed reply open the gate
        log.warning("Could not answer a user who is not allowed: %s", exc)
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
    spark = weekly_sparkline(ratings, coach.today())
    if spark:
        lines.append(spark)
    return "\n".join(lines)


def shoulder_csv(coach: Coach) -> bytes:
    import csv
    import io

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["date", "time", "rating (0 = no pain / 10 = worst)", "note"])
    for r in coach.store.ratings():
        writer.writerow([r.get("date"), r.get("time", ""), r["rating"], r.get("note", "")])
    return out.getvalue().encode("utf-8-sig")  # opens cleanly in Excel and Numbers


def progress_text(coach: Coach) -> str:
    history = progress_history(coach.store.logs())
    if not history:
        return (
            "No weights in your logs yet. Log them like this and I will track them:\n"
            "/log rows 22kg 3x10, floor press 14kg 3x8 felt easy"
        )
    lines = ["**Progress from your logs** (first → latest)"]
    for _, entries in sorted(history.items(), key=lambda kv: kv[1][-1]["date"], reverse=True):
        first, last = entries[0], entries[-1]
        name = last["name"][:1].upper() + last["name"][1:]
        if first["kg"] is not None and last["kg"] is not None and len(entries) > 1:
            change = last["kg"] - first["kg"]
            sign = "+" if change > 0 else ""
            detail = f"{first['kg']:g} → {last['kg']:g} kg ({sign}{change:g} kg)"
        else:
            detail = _fmt_item(last)
        sets = f", last {last['sets']} x {last['reps']}" if last["sets"] and first["kg"] is not None and len(entries) > 1 else ""
        count = f"{len(entries)} session{'s' if len(entries) > 1 else ''}"
        lines.append(f"• {name}: {detail}{sets} · {count}, latest {fmt_day(last['date'])}")
    return "\n".join(lines)


async def cmd_progress(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, progress_text(coach_of(context)))


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
        m = re.fullmatch(r"(\d{1,2})(?!\d|[.,]\d)(?:\s*/\s*10)?\s*[,.;:]?\s*(.*)", arg, re.DOTALL)
        if not m or int(m.group(1)) > 10:
            await reply(update, context, "Send /shoulder on its own for your log, or /shoulder 3 to save a rating. Use a whole number from 0 to 10.")
            return
        now = coach.now()
        rating = int(m.group(1))
        coach.store.add_rating(now.date(), now, rating, m.group(2).strip() or "manual")
        await reply(update, context, f"Left shoulder {rating}/10 saved.\n\n" + rating_feedback(coach, rating))
        return
    await reply(update, context, shoulder_log_text(coach))
    if coach.store.ratings():
        await context.bot.send_document(
            update.effective_chat.id,
            document=InputFile(shoulder_csv(coach), filename=f"shoulder-ratings-{coach.today().isoformat()}.csv"),
            caption="All your ratings as a spreadsheet file, for your physio.",
        )


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    coach = coach_of(context)
    try:
        _, day_raw, value = query.data.split(":")
        day, rating = date.fromisoformat(day_raw), int(value)
        if not 0 <= rating <= 10:
            raise ValueError(rating)
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
    # Save before the first await, so a double tap sees the first tap's answer.
    coach.store.set_session(day, "done" if answer == "done" else "skipped", now, "button")
    await query.answer()
    if answer == "done":
        with contextlib.suppress(BadRequest):
            await query.edit_message_text(f"✅ {fmt_day(day)} marked as done. Nice work.")
        await send_text(context.bot, chat_id, RATING_QUESTION, reply_markup=rating_keyboard(day))
        return
    with contextlib.suppress(BadRequest):
        await query.edit_message_text(f"⏭ {fmt_day(day)} marked as skipped.")
    today = coach.today()
    if previous == "skipped" or monday_of(day) != monday_of(today) or max(day, today).weekday() >= 6:
        return
    if not coach.store.load_plan(monday_of(day)):
        return
    if coach.store.sessions().get(day.isoformat(), {}).get("status") != "skipped":
        return  # changed to Done in the meantime
    await send_text(context.bot, chat_id, "No problem. I am adjusting the rest of your week so you do not double up. This takes a minute or two.")
    try:
        async with typing(context.bot, chat_id):
            result = await coach.adjust_after_skip(day)
    except ClaudeError as exc:
        await send_text(context.bot, chat_id, f"I saved the skip but could not adjust the plan. {exc.user_message}")
        return
    if result:
        await send_text(context.bot, chat_id, coach.rest_of_week(result, max(day, today)))


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, coach_of(context).today_view())


async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = args_text(update).lower().strip(" .")
    weekday = DAY_LOOKUP.get(arg)
    if weekday is None:
        await reply(update, context, "Which day? Send /day and a day, like /day fri or /day monday.")
        return
    await reply(update, context, coach_of(context).any_day_view(weekday))


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, coach_of(context).week_view())


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
    await reply(update, context, coach.plan_view(result))


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


def away_text(coach: Coach) -> str:
    today = coach.today()
    periods = [p for p in coach.store.away() if p["end"] >= today.isoformat()]
    lines = []
    if periods:
        lines.append("**Days away**")
        for p in periods:
            start, end = date.fromisoformat(p["start"]), date.fromisoformat(p["end"])
            span = fmt_day(start) if start == end else f"{fmt_day(start)} to {fmt_day(end)}"
            lines.append(f"• {span}" + (f" ({p['note']})" if p.get("note") else ""))
    else:
        lines.append("No days away saved.")
    holidays_ahead = [(d, label) for d, label in coach.days_off(today, today + timedelta(days=60)) if label.startswith("public")]
    if holidays_ahead:
        lines += ["", "**Public holidays in the next 60 days**"]
        lines += [f"• {fmt_day(d)}: {label.removeprefix('public holiday (').rstrip(')')}" for d, label in holidays_ahead]
    lines += ["", "Add days like this: /away 8 Oct to 10 Oct Bangkok trip, or /away thu fri. Clear them with /away clear."]
    return "\n".join(lines)


async def cmd_away(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    arg = args_text(update)
    today = coach.today()
    if not arg:
        await reply(update, context, away_text(coach))
        return
    if arg.lower() in ("clear", "none", "cancel"):
        coach.store.set_away([p for p in coach.store.away() if p["end"] < today.isoformat()])
        await reply(update, context, "Days away cleared.")
        return
    try:
        start, end, note = parse_away(arg, today)
    except ValueError:
        await reply(update, context, "I could not read those dates. Try /away 8 Oct to 10 Oct Bangkok trip, /away thu fri, or /away tomorrow hotel gym only.")
        return
    periods = [p for p in coach.store.away() if p["end"] >= today.isoformat()]
    periods.append({"start": start.isoformat(), "end": end.isoformat(), "note": note})
    coach.store.set_away(periods)
    span = fmt_day(start) if start == end else f"{fmt_day(start)} to {fmt_day(end)}"
    text = f"Saved: away {span}" + (f" ({note})" if note else "") + "."
    this_week = monday_of(today)
    if start <= this_week + timedelta(days=6) and coach.store.load_plan(this_week):
        text += " This week's plan was built before, so send /plan to rebuild it with a hotel gym or bodyweight version on those days."
    else:
        text += " The plan for that week will use a hotel gym or bodyweight version on those days."
    await reply(update, context, text)


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
    version = await coach.runner.version()
    signed_in, auth = await coach.runner.auth_status()
    cfg = coach.cfg
    lines = [
        "**Claude Code**",
        f"• Version: {version}",
        f"• Sign in: {'✅ ' if signed_in else '⚠️ '}{auth}",
        f"• Models: questions {cfg.model_ask}, plans {cfg.model_plan}",
    ]
    last = coach.runner.last_call
    if last:
        what = {"ask": "question", "day": "workout", "repair": "self repair"}.get(last["kind"], "plan")
        if last["ok"]:
            lines.append(f"• Last Claude call: ✅ worked, {fmt_when(last['at'])} ({what})")
        else:
            lines.append(f"• Last Claude call: ⚠️ failed, {fmt_when(last['at'])} ({what}): {last['message']}")
    else:
        lines.append("• Last Claude call: none since the bot started")
    if cfg.token_created:
        expires = coach.token_expires()
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
        ((when, JOB_LABELS[job.name]) for job in jobs if job.name in JOB_LABELS and (when := job_next(job))),
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
    lines = ["", "**Garmin**", f"• {icon} Profile {coach.cfg.garmin_profile}: {summary.short}"]
    health = coach.store.read_json("health.json", {})
    now = coach.now()
    starts = [t for t in health.get("starts", []) if now - datetime.fromisoformat(t) < timedelta(days=7)]
    lines += ["", "**Self repair**"]
    if health.get("started_at"):
        lines.append(f"• Running since {fmt_when(datetime.fromisoformat(health['started_at']))}, "
                     f"{len(starts)} start{'s' if len(starts) != 1 else ''} in the last 7 days")
    problem = health.get("last_problem")
    if problem:
        lines.append(f"• Last problem: {fmt_when(datetime.fromisoformat(problem['at']))} in {problem['where']}, "
                     f"remedy: {problem['remedy']}")
    else:
        lines.append("• No problems so far")
    return lines


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with typing(context.bot, update.effective_chat.id):
        text = await status_text(coach_of(context), context.application)
    await reply(update, context, text)


async def cmd_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, context, "I do not know that command.\n\n" + HELP_TEXT)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if update is None and isinstance(error, NetworkError):
        log.warning("Telegram network problem, retrying: %s", error)
        return
    if isinstance(error, (RetryAfter, Forbidden)):  # Telegram asked to slow down, or the chat blocked the bot
        log.warning("Telegram refused a message: %s", error)
        return
    log.error("Unhandled error", exc_info=error)
    if isinstance(update, Update) and update.effective_chat:
        with contextlib.suppress(Exception):
            await context.bot.send_message(
                update.effective_chat.id,
                "Sorry, something went wrong on my side. I am looking into it and will tell you what I find.",
            )
    repair = getattr(context, "application", None) and context.application.bot_data.get("repair")
    if repair and error is not None:
        job = getattr(context, "job", None)
        try:
            await repair.handle(error, describe_source(update, job), job)
        except Exception:  # noqa: BLE001 - self repair must never take the bot down
            log.exception("Self repair failed")


def describe_source(update: object, job) -> str:
    if job is not None:
        return f"the {JOB_LABELS.get(job.name, job.name)} reminder"
    if isinstance(update, Update):
        if update.callback_query:
            return f"a button ({(update.callback_query.data or '').split(':')[0]})"
        text = (update.effective_message.text or "") if update.effective_message else ""
        if text.startswith("/"):
            return f"the {text.split()[0].split('@')[0]} command"
        return "a chat message"
    return "the bot"


# ---------------------------------------------------------------------------
# Self repair: stay alive, fix what can be fixed, and tell the owner
# ---------------------------------------------------------------------------

WATCHDOG_LIMIT = 600  # seconds without a heartbeat before the bot restarts itself
REMEDIES = {
    "none": "Change nothing. The error was a one off, or only a code update can fix it.",
    "retry": "Run the failed reminder again in 2 minutes. Only for errors in a scheduled reminder.",
    "repair_file": "A data file is damaged: move it aside and restore its last good backup. Put its path, relative to the data folder, in file.",
    "clear_memory": "The chat memory is damaged or too large: clear it.",
    "clean_work": "Empty Claude Code's work folder (/work).",
    "rebuild_plan": "This week's plan is damaged or unreadable: build it again.",
    "restart": "Restart the bot. Docker starts it again within a minute.",
}
REPAIR_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string", "description": "what went wrong and why, in one or two sentences"},
        "remedy": {"type": "string", "enum": list(REMEDIES)},
        "file": {"type": "string", "description": "for repair_file only"},
        "message": {"type": "string", "description": "one to three short plain sentences for the owner"},
        "code_fix": {"type": "string", "description": "the smallest code change that stops it happening again, or empty"},
    },
    "required": ["diagnosis", "remedy", "message"],
}
REPAIR_MARK = "SELF REPAIR"
MAX_DIAGNOSES_PER_DAY = 6
RESTART_REQUESTED = False  # set by a self repair restart, so main() exits with an error code


def heartbeat_age() -> float | None:
    try:
        return time.time() - HEARTBEAT.stat().st_mtime
    except OSError:
        return None


def start_watchdog(store: "Store", limit: float = WATCHDOG_LIMIT, interval: float = 60, exit_fn=os._exit) -> threading.Thread:
    """A thread outside the event loop. If the heartbeat job stops for `limit` seconds, the
    loop is stuck, so the process exits and Docker's restart policy starts it again."""

    started = time.time()

    def loop() -> None:
        while True:
            time.sleep(interval)
            age = heartbeat_age()
            age = min(age if age is not None else float("inf"), time.time() - started)  # an old file does not count
            if age > limit:
                log.critical("No heartbeat for %.0f seconds, the bot looks stuck. Restarting.", age)
                with contextlib.suppress(Exception):
                    note_exit(store, f"it stopped responding for {round(age / 60)} minutes")
                exit_fn(1)
                return

    thread = threading.Thread(target=loop, name="watchdog", daemon=True)
    thread.start()
    return thread


def note_exit(store: "Store", reason: str) -> None:
    """Why the bot is about to stop, for the message after the restart."""
    health = store.read_json("health.json", {})
    health["exit_reason"] = reason
    store.write_json("health.json", health)


class SelfRepair:
    """Unexpected errors go to `claude -p` (no tools) for a diagnosis. Claude picks one remedy
    from REMEDIES, the bot carries it out and tells the owner. Claude never changes files
    or code itself; a code fix is only suggested in the message."""

    def __init__(self, application: Application, coach: Coach):
        self.app = application
        self.ctx = SimpleNamespace(bot=application.bot, application=application, job=None)  # for owner_send
        self.coach = coach
        self.store = coach.store
        self.restart = lambda: application.stop_running()

    def health(self) -> dict:
        return self.store.read_json("health.json", {})

    def save_health(self, health: dict) -> None:
        self.store.write_json("health.json", health)

    @staticmethod
    def signature(error: BaseException) -> str:
        frames = [f for f in traceback.extract_tb(error.__traceback__) if f.filename.endswith("bot.py")]
        where = f"{frames[-1].name}:{frames[-1].lineno}" if frames else "?"
        return f"{type(error).__name__}@{where}"

    def details(self, error: BaseException) -> tuple[str, str]:
        """(traceback, source lines around the bot.py frames), secrets removed."""
        secrets = self.coach.cfg.secrets
        tb = redact("".join(traceback.format_exception(type(error), error, error.__traceback__)), secrets)[-4000:]
        frames = [f for f in traceback.extract_tb(error.__traceback__) if f.filename.endswith("bot.py")][-3:]
        source = []
        with contextlib.suppress(OSError):
            lines = Path(__file__).read_text(encoding="utf-8").splitlines()
            for frame in frames:
                lo, hi = max(1, frame.lineno - 8), min(len(lines), frame.lineno + 6)
                body = "\n".join(f"{n}{'>' if n == frame.lineno else ' '} {lines[n - 1]}" for n in range(lo, hi + 1))
                source.append(f"{frame.name}(), bot.py lines {lo} to {hi}:\n{body}")
        return tb, redact("\n\n".join(source), secrets)

    def data_files(self) -> str:
        root = self.store.root
        names = []
        for path in sorted(root.rglob("*")):
            if path.is_file() and "history" not in path.parts and "broken" not in path.parts:
                names.append(f"{path.relative_to(root)} ({path.stat().st_size} bytes)")
        return ", ".join(names[:60]) or "none"

    async def diagnose(self, error: BaseException, where: str) -> dict:
        tb, source = self.details(error)
        system = (
            "You are the on call engineer for a small Python Telegram bot: python-telegram-bot 22 with "
            "its job queue, calling `claude -p` for its AI, running in Docker on a home NAS with its "
            "files in /data. You cannot run commands or change files. The bot carries out the one "
            "remedy you choose."
        )
        message = "\n".join([
            REPAIR_MARK,
            f"The bot hit an error in {where}. Diagnose it and choose one remedy.",
            "",
            f"Error: {type(error).__name__}: {redact(str(error), self.coach.cfg.secrets)[:500]}",
            "",
            "Traceback:",
            tb,
            "",
            "Source around the failing lines:",
            source or "(not available)",
            "",
            "Recent warnings and errors from the log:",
            "\n".join(RECENT_LOG.lines) or "(none)",
            "",
            f"Files in the data folder: {self.data_files()}",
            "",
            "Remedies:",
            *[f"- {name}: {text}" for name, text in REMEDIES.items()],
            "",
            "Choose the smallest remedy that fixes it. Choose restart only when the bot looks stuck "
            "or broken in memory. When the cause is a bug in the code, choose none (or retry when a "
            "second try can work) and put the smallest code change in code_fix. message is for the "
            "bot's owner, who is not a programmer: plain words, no code.",
        ])
        reply = await self.coach.runner.run(system, message, "repair", REPAIR_SCHEMA)
        data = json.loads(reply)
        if not isinstance(data, dict) or data.get("remedy") not in REMEDIES:
            raise ValueError("unexpected reply")
        return data

    async def handle(self, error: BaseException, where: str, job=None) -> None:
        now = self.coach.now()
        sig = self.signature(error)
        self.store._append_jsonl("errors.jsonl", {
            "at": now.isoformat(timespec="seconds"), "where": where, "signature": sig,
            "error": redact(f"{type(error).__name__}: {error}", self.coach.cfg.secrets)[:500],
        })
        health = self.health()
        seen = {k: v for k, v in health.get("diagnosed", {}).items()
                if now - datetime.fromisoformat(v) < timedelta(days=7)}
        health["diagnosed"] = seen
        last = seen.get(sig)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=12):
            return  # the same problem was diagnosed recently; it is in errors.jsonl
        today = now.date().isoformat()
        count = health.get("diagnoses_today", {})
        if count.get("date") != today:
            count = {"date": today, "count": 0}
        if count["count"] >= MAX_DIAGNOSES_PER_DAY:
            return
        count["count"] += 1
        health["diagnoses_today"] = count
        seen[sig] = now.isoformat(timespec="seconds")
        self.save_health(health)
        head = f"🩺 **Self repair**\nSomething went wrong in {where} ({type(error).__name__})."
        if not self.coach.cfg.self_repair:
            self.record(now, where, sig, "none", "")
            await self.notify(f"{head}\nThe details are saved in data/errors.jsonl.")
            return
        try:
            result = await self.diagnose(error, where)
        except Exception as exc:  # noqa: BLE001 - the owner still hears about the first error
            if isinstance(exc, ClaudeError):
                reason = exc.user_message
            elif isinstance(exc, ValueError):
                reason = "its answer was unreadable."
            else:
                reason = f"that failed too ({type(exc).__name__})."
                log.exception("The self repair diagnosis failed")
            self.record(now, where, sig, "none", "")
            await self.notify(f"{head}\nI could not ask Claude to look at it: {reason} "
                                       "The details are saved in data/errors.jsonl.")
            return
        done, restart = await self.apply(result, job)
        self.record(now, where, sig, result["remedy"], result.get("diagnosis", ""))
        text = f"{head}\n**What Claude found:** {result.get('diagnosis', '').strip()}\n**What I did:** {done}"
        if result.get("message", "").strip():
            text += f"\n\n{result['message'].strip()}"
        blocks = [to_html(text)]
        if result.get("code_fix", "").strip():  # for a programmer, so it stays folded
            blocks.append("<b>🛠 Suggested code change</b> <i>for the next update, tap to open</i>\n"
                          f"<blockquote expandable>{_h(result['code_fix'].strip()[:1500])}</blockquote>")
        await self.notify(blocks)
        if restart:
            global RESTART_REQUESTED
            RESTART_REQUESTED = True
            note_exit(self.store, f"self repair: {result.get('diagnosis', '')[:150]}")
            self.restart()

    async def notify(self, view: str | list[str]) -> None:
        """Tell the owner, and copy it to REPAIR_ALERT_CHAT (the NAS Doctor topic) when set."""
        await owner_send(self.ctx, view)
        target = self.coach.cfg.repair_alert_chat
        if target is None:
            return
        body = "\n\n".join(view) if isinstance(view, list) else to_html(view)
        try:
            await self.ctx.bot.send_message(target[0], f"<b>gym-coach-bot</b>\n{body}"[:4096],
                                            parse_mode=ParseMode.HTML, message_thread_id=target[1])
        except Exception as exc:  # noqa: BLE001 - the owner already has it
            log.warning("Could not copy the self repair alert to REPAIR_ALERT_CHAT: %s", exc)

    def record(self, now: datetime, where: str, sig: str, remedy: str, diagnosis: str) -> None:
        health = self.health()
        health["last_problem"] = {"at": now.isoformat(timespec="minutes"), "where": where, "signature": sig,
                                  "remedy": remedy, "diagnosis": diagnosis[:300]}
        self.save_health(health)

    async def apply(self, result: dict, job) -> tuple[str, bool]:
        """Carry out the chosen remedy. Returns (what was done, restart now)."""
        remedy = result["remedy"]
        store, coach = self.store, self.coach
        if remedy == "retry":
            if job is None or self.app.job_queue is None:
                return "Nothing. It was not a reminder, so there is nothing to run again.", False
            self.app.job_queue.run_once(job.callback, when=120, data=job.data, name=f"{job.name}_retry",
                                        chat_id=job.chat_id, user_id=job.user_id)
            return "I will run it again in 2 minutes.", False
        if remedy == "repair_file":
            name = (result.get("file") or "").strip().lstrip("/").removeprefix("data/")
            path = (store.root / name).resolve()
            if not name or store.root.resolve() not in path.parents or path.suffix != ".json" or not path.is_file():
                return f"Nothing. I could not find a data file called {name or 'that'}.", False
            store.repair_file(path)
            restored = store.repairs[-1]["restored"] if store.repairs else False
            store.repairs.clear()
            return (f"I restored {name} from its last good copy." if restored
                    else f"{name} had no good copy, so it starts empty. The damaged one is in data/broken."), False
        if remedy == "clear_memory":
            store.write_json("memory.json", {})
            return "I cleared the chat memory.", False
        if remedy == "clean_work":
            removed = clean_folder(coach.cfg.work_dir, older_than=0)
            return f"I emptied the work folder ({removed} items).", False
        if remedy == "rebuild_plan":
            health = self.health()
            monday = monday_of(coach.today())
            if health.get("rebuilt_plan") == monday.isoformat():
                return "Nothing. I already rebuilt this week's plan once, so please send /plan.", False
            health["rebuilt_plan"] = monday.isoformat()
            self.save_health(health)
            try:
                await coach.build_week(monday, kind="repair", wait=True)
            except ClaudeError as exc:
                return f"I tried to rebuild this week's plan, but it failed: {exc.user_message}", False
            return "I rebuilt this week's plan. Send /week to see it.", False
        if remedy == "restart":
            now = coach.now()
            starts = [datetime.fromisoformat(t) for t in self.health().get("starts", [])]
            if sum(1 for t in starts if now - t < timedelta(hours=6)) >= 3:
                return "Nothing. I already restarted 3 times in 6 hours, so a restart will not help.", False
            return "I am restarting. I will be back within a minute.", True
        if result.get("code_fix", "").strip():
            return "Nothing. I can't fix this on my own; it needs the code change below.", False
        return "Nothing. It looks like a one off, so there is nothing to fix.", False

    # -- the self check every 30 minutes --------------------------------------

    def self_check(self) -> list[tuple[str, str]]:
        """Deterministic checks and fixes. Returns (key, message) for problems to report."""
        store, cfg = self.store, self.coach.cfg
        problems: list[tuple[str, str]] = []
        with contextlib.suppress(OSError):
            free = shutil.disk_usage(store.root).free
            if free < 200 * 1024 * 1024:
                problems.append(("disk", f"💾 The NAS disk that holds ./data has only {free // (1024 * 1024)} MB free. "
                                         "I stop being able to save your logs when it is full."))
        try:
            probe = store.root / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            problems.append(("write", f"💾 I cannot save files in ./data ({exc.strerror or exc}). Check the folder "
                                      "permissions and PUID/PGID in bot.env."))
        for path in [*store.root.glob("*.json"), *(store.root / "plans").glob("*.json")]:
            store.read_json(store.name_of(path), None)  # a damaged file is repaired as it is read
        for fix in store.repairs:
            if fix["restored"]:
                problems.append((f"file:{fix['file']}", f"🩹 {fix['file']} was damaged, so I restored its last good copy."))
            else:
                problems.append((f"file:{fix['file']}", f"🩹 {fix['file']} was damaged and had no good copy, so it "
                                                        "starts empty. The damaged one is in data/broken."))
        store.repairs.clear()
        clean_folder(cfg.work_dir, older_than=3600)
        for leftover in Path(tempfile.gettempdir()).glob("coach-system-*.md"):
            with contextlib.suppress(OSError):
                if time.time() - leftover.stat().st_mtime > 3600:
                    leftover.unlink()
        return problems


def clean_folder(folder: Path, older_than: float) -> int:
    """Remove what is in the folder (files and folders older than `older_than` seconds)."""
    removed = 0
    if not folder.is_dir():
        return 0
    for item in folder.iterdir():
        with contextlib.suppress(OSError):
            if time.time() - item.lstat().st_mtime < older_than:
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed += 1
    return removed


async def job_self_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    repair: SelfRepair | None = context.application.bot_data.get("repair")
    if repair is None:
        return
    problems = repair.self_check()
    if not problems:
        return
    health = repair.health()
    told = health.setdefault("told", {})
    today = repair.coach.today().isoformat()
    new = [(key, text) for key, text in problems if told.get(key) != today or key.startswith("file:")]
    for key, _ in new:
        told[key] = today
    repair.save_health(health)
    if new:
        await owner_send(context, "\n\n".join(text for _, text in new))


async def job_restart_notice(context: ContextTypes.DEFAULT_TYPE) -> None:
    """After an unexpected stop, tell the owner the bot is back and why it stopped."""
    reason = (context.job.data or {}).get("reason") if context.job else None
    text = "♻️ I restarted after an unexpected stop"
    text += f" ({reason})." if reason else ", possibly a crash, a memory limit or a NAS restart."
    text += " I am running again, and anything I missed is being caught up."
    await owner_send(context, text)


def mark_start(store: Store, now: datetime) -> dict | None:
    """Record this start. Returns {"reason": ...} when the last run did not stop cleanly."""
    health = store.read_json("health.json", {})
    unexpected = None
    if health.get("running") or health.get("exit_reason"):
        unexpected = {"reason": health.get("exit_reason")}
    starts = [t for t in health.get("starts", []) if now - datetime.fromisoformat(t) < timedelta(days=7)]
    health.update(running=True, started_at=now.isoformat(timespec="seconds"),
                  starts=[*starts, now.isoformat(timespec="seconds")][-50:])
    health.pop("exit_reason", None)
    store.write_json("health.json", health)
    return unexpected


async def post_shutdown(application: Application) -> None:
    """A clean stop (docker stop, a NAS shutdown): no restart message next time."""
    coach: Coach = application.bot_data["coach"]
    health = coach.store.read_json("health.json", {})
    health["running"] = False
    health["stopped_at"] = coach.now().isoformat(timespec="seconds")
    coach.store.write_json("health.json", health)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([BotCommand(name, desc) for name, desc in COMMANDS])
    coach: Coach = application.bot_data["coach"]
    beat()
    unexpected = mark_start(coach.store, coach.now())
    if unexpected is not None and application.job_queue:
        application.job_queue.run_once(job_restart_notice, 10, data=unexpected, name="restart_notice")
    for leftover in Path(tempfile.gettempdir()).glob("coach-system-*.md"):
        with contextlib.suppress(OSError):
            leftover.unlink()  # left behind if the container was killed mid call
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
    application.add_handler(CommandHandler("day", cmd_day))
    application.add_handler(CommandHandler("plan", cmd_plan, **slow))
    application.add_handler(CommandHandler("nextweek", cmd_nextweek, **slow))
    application.add_handler(CommandHandler("log", cmd_log))
    application.add_handler(CommandHandler("done", cmd_done))
    application.add_handler(CommandHandler("shoulder", cmd_shoulder))
    application.add_handler(CommandHandler("progress", cmd_progress))
    application.add_handler(CallbackQueryHandler(on_rate, pattern=r"^rate:"))
    application.add_handler(CallbackQueryHandler(on_check, pattern=r"^chk:", **slow))
    application.add_handler(CallbackQueryHandler(on_alt, pattern=r"^alt:", **slow))
    application.add_handler(CommandHandler("injury", cmd_injury))
    application.add_handler(CommandHandler("away", cmd_away))
    application.add_handler(CommandHandler("profile", cmd_profile))
    application.add_handler(CommandHandler("status", cmd_status, **slow))
    application.add_handler(CommandHandler("reset", cmd_reset))
    application.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    application.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text, **slow)
    )
    application.add_error_handler(on_error, block=not concurrent)  # a diagnosis takes a minute


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

JOB_LABELS = {
    "daily_workout": "Daily workout",
    "session_reminder": "Session reminder",
    "session_reminder_fri": "Session reminder",
    "evening_check": "Did you train? check",
    "checkin": "Sunday check in",
    "weekly_plan": "Next week's plan",
    "token_check": "Token expiry check",
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


async def owner_send(context: ContextTypes.DEFAULT_TYPE, text: str | list[str], reply_markup=None) -> list:
    coach = coach_of(context)
    if coach.cfg.owner_id is None:
        log.warning("No ALLOWED_USER_IDS, so reminders have nowhere to go")
        return []
    return await send_view(context.bot, coach.cfg.owner_id, text, reply_markup=reply_markup)


def alt_keyboard(day: date) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🪶 Lighter version", callback_data=f"alt:{day.isoformat()}:light"),
                InlineKeyboardButton("⏱ 30 minute version", callback_data=f"alt:{day.isoformat()}:short"),
            ]
        ]
    )


async def job_daily_workout(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every morning: that day's workout, or that it is a rest day."""
    await send_day_session(context, "☀️ Good morning. Today's workout", "☀️ Good morning. Rest day today.")


async def job_session_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Today's session before the gym."""
    await send_day_session(context, "🏋️ Today's session", "Rest day today.")


async def send_day_session(context: ContextTypes.DEFAULT_TYPE, heading: str, rest_heading: str) -> None:
    coach = coach_of(context)
    today = coach.today()
    monday = monday_of(today)
    if coach.store.sessions().get(today.isoformat(), {}).get("status") == "done":
        return
    if not coach.store.load_plan(monday):
        if not coach.program_start():
            await owner_send(context, "No training plan yet. Send /plan to build your first week.")
            return
        if coach.program_start() > monday:
            return  # the programme starts next week (first plan built with /nextweek)
        if today.weekday() > 4:
            return  # like the catch up: no Claude build for a week that is nearly over
        try:
            await coach.build_week(monday, kind="build", wait=True, keep_if=lambda meta: True)
        except ClaudeError as exc:
            await owner_send(context, f"This week has no plan and I could not build one. {exc.user_message} Send /plan to try again.")
            return
    day = parse_plan(coach.store.load_plan(monday) or "").days.get(today.weekday())
    rest = bool(day and day.is_rest)
    note = coach.recovery_note(today, rest_day=rest)
    cards = coach.day_view(today, rest_heading if rest else heading, [note] if note else None) if day else None
    if cards:
        await owner_send(context, cards, reply_markup=None if rest else alt_keyboard(today))
        return
    markup = None
    if day is None:
        text = coach.today_text()
    elif day.is_rest:
        text = f"{rest_heading}\n\n{day.text}"
    else:
        text = f"{heading}\n\n{day.text}"
        markup = alt_keyboard(today)
    off = coach.day_off_line(today)
    if off and day is not None:
        text = f"{off}\n{text}"
    if note:
        text += "\n\n" + note
    await owner_send(context, text, reply_markup=markup)


async def on_alt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """🪶 Lighter or ⏱ 30 minute version of a day's session, written by the coach."""
    query = update.callback_query
    coach = coach_of(context)
    try:
        _, day_raw, kind = query.data.split(":")
        day = date.fromisoformat(day_raw)
    except ValueError:
        await query.answer()
        return
    if day != coach.today():
        await query.answer(f"That button was for {fmt_day(day)}. Send /today for today's session.", show_alert=True)
        return
    busy = context.application.bot_data.setdefault("alt_busy", set())
    if query.data in busy:
        await query.answer("Already asking your coach…")
        return
    busy.add(query.data)
    try:
        await _answer_alt(update, context, coach, day, kind)
    finally:
        busy.discard(query.data)


async def _answer_alt(update, context, coach, day, kind) -> None:
    query = update.callback_query
    await query.answer("Asking your coach…")
    chat_id = update.effective_chat.id
    try:
        async with typing(context.bot, chat_id):
            result = await coach.alt_session(day, kind)
    except ClaudeError as exc:
        await reply(update, context, exc.user_message)
        return
    if result:
        new, question, warnings = result
        label = "⏱ 30 minute version" if kind == "short" else "🪶 Lighter version"
        blocks = day_card_blocks(new, f"{label} of today's session", day)
        if warnings:
            blocks.insert(-1, to_html("⚠️ Please check:\n" + "\n".join(f"• {w}" for w in warnings)))
        await send_blocks(context.bot, chat_id, blocks)
        text = plan_to_text({"split_explanation": "", "days": [new], "notes": []})
        coach.store.add_memory(chat_id, question, text[:3000], coach.now())  # for follow up questions
        return
    plan = coach.store.load_plan(monday_of(day)) or ""
    focus = getattr(parse_plan(plan).days.get(day.weekday()), "focus", "")
    when = "today" if day == coach.today() else DAY_NAMES[day.weekday()]
    session = f"{when}'s session" + (f" ({focus})" if focus else "")
    if kind == "short":
        question = f"I only have 30 minutes {when}. Give me a 30 minute version of {session}."
    else:
        question = (
            f"Give me a lighter version of {session}, based on my recovery and my shoulder. "
            "Keep the same format, with sets x reps, rest, a cue and a video line per exercise."
        )
    await answer_question(update, context, question)


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
    if not plan and (not coach.program_start() or before_programme(coach, monday_of(today))):
        return
    day = parse_plan(plan or "").days.get(today.weekday())
    if day and day.is_rest:
        return
    workouts = coach.garmin.workouts(today) if coach.cfg.garmin_auto_done else []
    if workouts:
        coach.store.set_session(today, "done", coach.now(), "garmin")
        seen = ", ".join(f"a {w['minutes']} min {w['type'].replace('_', ' ')} session" for w in workouts)
        await owner_send(
            context,
            f"✅ Your watch shows {seen} today, so I marked today as done.\n\n{RATING_QUESTION}",
            reply_markup=rating_keyboard(today),
        )
        return
    focus = f" ({day.focus})" if day else ""
    await owner_send(context, f"Did you train today{focus}?", reply_markup=check_keyboard(today))


async def job_checkin(context: ContextTypes.DEFAULT_TYPE) -> None:
    coach = coach_of(context)
    now = coach.now()
    if before_programme(coach, monday_of(now.date()) + timedelta(days=7)):
        return
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


def before_programme(coach: Coach, monday: date) -> bool:
    """True for a week before PROGRAM_START (or before the week of the first /nextweek)."""
    start = coach.program_start()
    return bool(start and monday < start)


async def build_next_week(context: ContextTypes.DEFAULT_TYPE, monday: date) -> None:
    """Build next week's plan with the check in answer and send an overview."""
    coach = coach_of(context)
    if before_programme(coach, monday):
        return
    sunday = monday - timedelta(days=1)
    st = coach.store.state().get("checkin") or {}
    answered_at = st.get("answered_at") if st.get("sunday") == sunday.isoformat() else None

    def keep(meta: dict) -> bool:  # a plan built after the check in answer is kept
        return answered_at is None or meta.get("built_at", "") >= answered_at

    notes = coach.store.load_plan_meta(monday).get("notes", "")  # e.g. from /nextweek on Saturday
    try:
        result = await coach.build_week(monday, notes, kind="scheduled", wait=True, keep_if=keep)
    except ClaudeError as exc:
        await owner_send(context, f"I could not build next week's plan. {exc.user_message} Send /nextweek to try again.")
        return
    heading = "🗓 Next week's plan was already built" if result.reused else "🗓 Next week is ready"
    text = coach.overview(result, f"{heading}. {coach.week_label(monday)}")
    text += "\n\n" + coach.week_in_numbers(monday - timedelta(days=7))
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


async def job_heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    beat()


def beat() -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT.write_text(now_in(ZoneInfo("UTC")).isoformat(timespec="seconds"))


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
    start = coach.program_start()
    if today.weekday() <= 4 and not coach.store.load_plan(monday) and start and start <= monday:
        await owner_send(context, "I was offline when this week's plan was due, so I am building it now.")
        try:
            result = await coach.build_week(monday, kind="build", wait=True, keep_if=lambda meta: True)
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

    if cfg.daily_workout_time and cfg.daily_workout_days:
        jq.run_daily(job_daily_workout, at(cfg.daily_workout_time), days=ptb_days(cfg.daily_workout_days), name="daily_workout", **daily)
    if cfg.reminder_mon_thu:
        jq.run_daily(job_session_reminder, at(cfg.reminder_mon_thu), days=ptb_days([0, 1, 2, 3]), name="session_reminder", **daily)
    if cfg.reminder_fri:
        jq.run_daily(job_session_reminder, at(cfg.reminder_fri), days=ptb_days([4]), name="session_reminder_fri", **daily)
    if cfg.check_time and cfg.training_days:
        jq.run_daily(job_evening_check, at(cfg.check_time), days=ptb_days(cfg.training_days), name="evening_check", **daily)
    jq.run_daily(job_checkin, at(cfg.checkin_time), days=ptb_days([6]), name="checkin", **daily)
    jq.run_daily(job_weekly_plan, at(cfg.plan_time), days=ptb_days([6]), name="weekly_plan", **daily)
    jq.run_daily(job_token_check, at(cfg.token_check_time), name="token_check", **daily)
    jq.run_once(job_catch_up, 20, name="catch_up")
    jq.run_repeating(job_heartbeat, interval=60, first=1, name="heartbeat")
    jq.run_repeating(job_self_check, interval=1800, first=120, name="self_check")


def build_application(
    cfg: Config, coach: Coach | None = None, request=None, updates_request=None, concurrent: bool = True
) -> Application:
    builder = (
        ApplicationBuilder().token(cfg.telegram_token).defaults(Defaults(tzinfo=cfg.tz))
        .post_init(post_init).post_shutdown(post_shutdown)
    )
    if cfg.telegram_base_url:
        base = cfg.telegram_base_url.rstrip("/")
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
    if request is not None:
        builder = builder.request(request).get_updates_request(updates_request or request)
    application = builder.build()
    application.bot_data["coach"] = coach = coach or Coach(cfg)
    application.bot_data["repair"] = SelfRepair(application, coach)
    add_handlers(application, concurrent)
    schedule_jobs(application)
    return application


def health_check() -> int:
    """Exit code for Docker's HEALTHCHECK: 0 while the heartbeat is fresh."""
    import time

    try:
        age = time.time() - HEARTBEAT.stat().st_mtime
    except OSError:
        print("no heartbeat yet")
        return 1
    print(f"heartbeat {age:.0f}s ago")
    return 0 if age < HEARTBEAT_MAX_AGE else 1


def main() -> None:
    import sys

    if "--health" in sys.argv:
        raise SystemExit(health_check())
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"Settings problem in bot.env: {exc}") from None
    setup_logging(cfg)
    try:
        application = build_application(cfg)
        start_watchdog(application.bot_data["coach"].store)
        application.run_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
            bootstrap_retries=-1,  # keep retrying if Telegram is unreachable at start up
        )
    except Exception:  # noqa: BLE001 - log through the redacting formatter, then let Docker restart us
        log.exception("The bot stopped because of an error. Docker will restart it.")
        with contextlib.suppress(Exception):
            note_exit(Store(cfg.data_dir), "an error stopped it")
        raise SystemExit(1) from None
    if RESTART_REQUESTED:
        log.warning("Restarting for self repair. Docker starts the bot again.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
