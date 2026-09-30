"""Builds a sample garmin-monitor database (same daily_snapshots table) for tests.

Run it by hand to try the bot without a watch:
    python tests/sample_garmin.py /tmp/monitor.db 2026-09-30
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

# Copied from garmin-monitor's storage.py so the bot is tested against the real layout.
SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_snapshots (
    profile TEXT NOT NULL,
    day TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    tz TEXT,
    total_steps INTEGER, step_goal INTEGER, distance_m REAL,
    total_kcal REAL, active_kcal REAL,
    min_hr INTEGER, max_hr INTEGER, resting_hr INTEGER, resting_hr_7d_avg INTEGER,
    sleep_seconds INTEGER, sleep_score INTEGER, sleep_deep_seconds INTEGER,
    sleep_light_seconds INTEGER, sleep_rem_seconds INTEGER, sleep_awake_seconds INTEGER,
    sleep_start TEXT, sleep_end TEXT,
    hrv_last_night REAL, hrv_weekly_avg REAL, hrv_status TEXT,
    avg_stress INTEGER, max_stress INTEGER,
    high_stress_seconds INTEGER, rest_stress_seconds INTEGER,
    body_battery_high INTEGER, body_battery_low INTEGER, body_battery_latest INTEGER,
    moderate_intensity_min INTEGER, vigorous_intensity_min INTEGER,
    active_seconds INTEGER, highly_active_seconds INTEGER, sedentary_seconds INTEGER,
    floors_up REAL,
    avg_spo2 REAL, lowest_spo2 INTEGER,
    avg_waking_respiration REAL, avg_sleep_respiration REAL,
    readiness_score INTEGER, readiness_level TEXT,
    abnormal_hr_alerts INTEGER,
    activities_count INTEGER, activities_json TEXT,
    last_sync TEXT,
    errors_json TEXT,
    raw_json TEXT,
    PRIMARY KEY (profile, day)
);
CREATE TABLE IF NOT EXISTS hr_samples (
    profile TEXT NOT NULL,
    ts TEXT NOT NULL,
    hr INTEGER NOT NULL,
    PRIMARY KEY (profile, ts)
);
"""


def activity(day: date, hour: int, kind: str, name: str, minutes: float, km: float | None, avg_hr: int, aid: int,
             source: str = "recorded") -> dict:
    start = datetime(day.year, day.month, day.day, hour, 0, tzinfo=timezone.utc)
    return {
        "name": name,
        "type": kind,
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=minutes)).isoformat(),
        "duration_s": minutes * 60,
        "avg_hr": avg_hr,
        "max_hr": avg_hr + 25,
        "distance_m": km * 1000 if km else None,
        "calories": 300.0,
        "source": source,
        "activity_id": aid,
    }


def build(path: str, today: date, poor_recovery_today: bool = False, days: int = 10,
          keep_open: bool = False) -> sqlite3.Connection | None:
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        acts = []
        if day.weekday() in (1, 3):
            acts.append(activity(day, 10, "strength_training", "Strength", 52, None, 108, 1000 + i))
        if day.weekday() == 5:
            acts.append(activity(day, 1, "running", "Singapore Running", 34 + 1 / 6, 6.2, 150, 2000 + i))
        if day.weekday() == 4:
            acts.append(activity(day, 10, "treadmill_running", "Treadmill Running", 25, 4.5, 146, 3000 + i))
            acts.append(activity(day, 11, "lap_swimming", "Pool Swim", 30, 1.0, 120, 4000 + i))
        if day.weekday() == 6:
            acts.append(activity(day, 3, "running", "Walk detected", 12, 1.0, 100, 5000 + i, "auto_detected"))
        is_today = day == today
        low = poor_recovery_today and is_today
        row = {
            "profile": "Me",
            "day": day.isoformat(),
            "fetched_at": datetime(day.year, day.month, day.day, 0, 5, tzinfo=timezone.utc).isoformat(),
            "tz": "Asia/Singapore",
            "resting_hr": 58 if low else 52,
            "resting_hr_7d_avg": 52,
            "sleep_seconds": int((5.2 if low else 7.4) * 3600),
            "sleep_score": 55 if low else 82,
            "hrv_last_night": 48.0 if low else 62.0,
            "hrv_weekly_avg": 60.0,
            "hrv_status": "UNBALANCED" if low else "BALANCED",
            "body_battery_high": 35 if low else 88,
            "body_battery_low": 20,
            "body_battery_latest": 30 if low else 70,
            "activities_count": len(acts),
            "activities_json": json.dumps(acts),
            "last_sync": datetime(day.year, day.month, day.day, 0, 45, tzinfo=timezone.utc).isoformat(),
            "errors_json": "{}",
            "raw_json": json.dumps({"big": "x" * 2000}),
        }
        cols = ", ".join(row)
        conn.execute(f"INSERT OR REPLACE INTO daily_snapshots ({cols}) VALUES ({', '.join('?' * len(row))})",
                     tuple(row.values()))
        dad = dict(row, profile="Dad", resting_hr=70, activities_json="[]")
        conn.execute(f"INSERT OR REPLACE INTO daily_snapshots ({cols}) VALUES ({', '.join('?' * len(dad))})",
                     tuple(dad.values()))
    if keep_open:
        return conn
    conn.close()
    return None


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "monitor.db"
    day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
    build(target, day, poor_recovery_today="--poor" in sys.argv)
    print(f"Wrote sample Garmin data to {target} ending {day}")
