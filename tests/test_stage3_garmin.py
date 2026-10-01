"""Stage 3: reading garmin-monitor's database and using it in the coach."""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot
import sample_garmin
from conftest import send


@pytest.fixture
def garmin_db(cfg):
    path = Path(cfg.garmin_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def build(path: Path, today=date(2026, 9, 30), **kwargs):
    return sample_garmin.build(str(path), today, **kwargs)


def summary(cfg, today=date(2026, 9, 30), profile=None, days=7):
    reader = bot.GarminReader(cfg.garmin_db, profile or cfg.garmin_profile)
    return reader.summary(today, days, cfg.tz)


def test_summary_has_sleep_rhr_hrv_body_battery_and_runs(cfg, garmin_db):
    build(garmin_db)
    s = summary(cfg)
    assert s.ok
    text = s.context
    assert text.startswith("Garmin data from my watch, last 7 days (oldest first):")
    assert "Thu 24 Sep: sleep 7.4 h (score 82), resting HR 52 (7 day avg 52), HRV 62 (weekly avg 60, balanced), body battery high 88, low 20" in text
    assert "Wed 30 Sep:" in text and "Wed 23 Sep" not in text  # only 7 days
    assert "Runs in these 7 days: 2, 10.7 km in total." in text
    assert "• Fri 25 Sep: Treadmill Running, 4.5 km in 25:00 (5:33 /km), avg HR 146" in text
    assert "• Sat 26 Sep: Singapore Running, 6.2 km in 34:10 (5:31 /km), avg HR 150" in text
    assert "Walk detected" not in text  # auto detected moves are not runs
    assert "Other activities: Thu 24 Sep: strength training 52 min, avg HR 108; Fri 25 Sep: lap swimming 30 min" in text
    assert "Recovery today looks fine." in text
    assert "resting HR 70" not in text  # Dad's rows are ignored
    assert s.recovery_flags == []
    assert s.short == "latest data Wed 30 Sep, watch last synced Wed 30 Sep 08:45. Recovery today looks fine"


def test_poor_recovery_is_flagged(cfg, garmin_db):
    build(garmin_db, poor_recovery_today=True)
    s = summary(cfg)
    assert s.recovery_flags == [
        "short sleep (5.2 h)",
        "body battery only reached 35",
        "HRV 48, below your usual 60",
        "resting HR 58, 6 above your 7 day average",
    ]
    assert "Recovery today looks low: short sleep (5.2 h), body battery only reached 35" in s.context


def test_old_data_is_called_out(cfg, garmin_db):
    build(garmin_db)
    s = summary(cfg, today=date(2026, 10, 3))
    assert "No Garmin data for today yet. The latest is from Wed 30 Sep, so it may be out of date." in s.context
    assert s.recovery_flags == []


def test_missing_database_and_profile(cfg, garmin_db):
    s = summary(cfg)
    assert not s.ok and s.context == "Garmin data: not available right now."
    assert s.short == f"no database at {cfg.garmin_db}"
    build(garmin_db)
    s = summary(cfg, profile="Bob")
    assert not s.ok
    assert s.short == "no data for profile 'Bob' in the last 7 days (profiles found: Dad, Me)"


def test_broken_database_does_not_crash(cfg, garmin_db):
    garmin_db.write_text("this is not sqlite")
    s = summary(cfg)
    assert not s.ok and "could not read" in s.short


def test_reads_live_wal_data_while_garmin_monitor_writes(cfg, garmin_db):
    writer = build(garmin_db, keep_open=True)  # like garmin-monitor running
    try:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE daily_snapshots SET resting_hr = 49 WHERE profile='Me' AND day='2026-09-30'")
        assert Path(str(garmin_db) + "-wal").exists()
        s = summary(cfg)
        assert "Wed 30 Sep: sleep 7.4 h (score 82), resting HR 49" in s.context
    finally:
        writer.close()


def test_falls_back_to_immutable_on_a_read_only_mount(cfg, garmin_db, monkeypatch):
    """After a clean shutdown a read only mount cannot create the -shm file."""
    build(garmin_db)
    real_connect = sqlite3.connect
    tried = []

    class ReadOnlyFailure:
        def __init__(self, conn):
            self.conn = conn

        def __setattr__(self, name, value):
            if name == "conn":
                object.__setattr__(self, name, value)
            else:
                setattr(self.conn, name, value)

        def execute(self, *args):
            raise sqlite3.OperationalError("attempt to write a readonly database")

        def close(self):
            self.conn.close()

    def fake_connect(database, *args, **kwargs):
        tried.append(database.split("?", 1)[1])
        conn = real_connect(database, *args, **kwargs)
        return conn if "immutable=1" in database else ReadOnlyFailure(conn)

    monkeypatch.setattr(bot.sqlite3, "connect", fake_connect)
    s = summary(cfg)
    assert s.ok and "Runs in these 7 days: 2" in s.context
    assert tried[:2] == ["mode=ro", "mode=ro&immutable=1"]


def test_reader_never_writes(cfg, garmin_db):
    build(garmin_db)
    before = garmin_db.read_bytes()
    reader = bot.GarminReader(str(garmin_db), "Me")
    with pytest.raises(sqlite3.OperationalError):
        reader._query("DELETE FROM daily_snapshots")
    assert garmin_db.read_bytes() == before


async def test_garmin_summary_reaches_claude(app, claude, garmin_db):
    build(garmin_db)
    await send(app, "/ask should I run today?")
    system = claude.last()["system"]
    assert "Garmin data from my watch, last 7 days" in system
    assert "Runs in these 7 days: 2, 10.7 km in total." in system
    await send(app, "/plan")
    assert "Garmin data from my watch" in claude.plan_calls()[-1]["system"]


async def test_session_reminder_warns_on_poor_recovery(app, claude, clock, garmin_db):
    build(garmin_db, poor_recovery_today=True)
    await send(app, "/plan")
    clock.set(2026, 9, 30, 17, 30)
    await bot.job_session_reminder(SimpleNamespace(bot=app.bot, application=app, job=None))
    text = app.tg.texts()[-1]
    assert text.startswith("🏋️ <b>TODAY'S SESSION</b>")
    assert "⚠️ Your Garmin data says recovery looks low today: short sleep (5.2 h)" in text
    assert "tap Lighter version below" in text


async def test_session_reminder_has_no_warning_when_recovered(app, claude, clock, garmin_db):
    build(garmin_db)
    await send(app, "/plan")
    clock.set(2026, 9, 30, 17, 30)
    await bot.job_session_reminder(SimpleNamespace(bot=app.bot, application=app, job=None))
    assert "⚠️" not in app.tg.texts()[-1]


async def test_profile_and_status_show_garmin(app, garmin_db):
    build(garmin_db)
    profile = (await send(app, "/profile"))[0]
    assert "<b>Garmin</b>\nLatest data Wed 30 Sep, watch last synced Wed 30 Sep 08:45. Recovery today looks fine." in profile
    status = (await send(app, "/status"))[0]
    assert "• ✅ Profile Me: latest data Wed 30 Sep" in status


async def test_status_when_garmin_is_missing(app):
    status = (await send(app, "/status"))[0]
    assert "• ⚠️ Profile Me: no database at" in status
