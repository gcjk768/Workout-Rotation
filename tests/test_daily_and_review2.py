"""The daily workout message, the lighter / 30 minute buttons, and second review fixes."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import bot
from conftest import FakeTelegram, callback_update, press, send

from test_stage2_reminders import keyboard_data, next_fire


def ctx(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


# ---------------------------------------------------------------------------
# Daily workout
# ---------------------------------------------------------------------------


async def test_daily_workout_every_morning(app, claude, clock, cfg):
    tz = cfg.tz
    assert next_fire(app, "daily_workout", datetime(2026, 9, 30, 12, 0, tzinfo=tz)) == datetime(2026, 10, 1, 7, 0, tzinfo=tz)
    assert next_fire(app, "daily_workout", datetime(2026, 10, 3, 8, 0, tzinfo=tz)) == datetime(2026, 10, 4, 7, 0, tzinfo=tz)  # Sunday too
    await send(app, "/plan")
    clock.set(2026, 10, 1, 7, 0)  # Thursday
    await bot.job_daily_workout(ctx(app))
    sent = app.tg.sent()[-1]
    assert sent["text"].startswith("☀️ <b>GOOD MORNING</b> · Today's workout\n\n📅 Thursday: Upper body and rehab")
    assert keyboard_data(sent) == ["alt:2026-10-01:light", "alt:2026-10-01:short"]


async def test_daily_workout_on_a_rest_day(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 4, 7, 0)  # Sunday is a rest day in the fake plan
    await bot.job_daily_workout(ctx(app))
    sent = app.tg.sent()[-1]
    assert sent["text"].startswith("☀️ <b>GOOD MORNING</b> · Rest day today\n\n📅 Sunday: Rest")
    assert keyboard_data(sent) == []


async def test_lighter_and_short_buttons_ask_the_coach(app, claude, clock):
    await send(app, "/plan")
    calls = await press(app, "alt:2026-09-30:light")
    assert "Give me a lighter version of today's session (Legs and core)" in claude.last()["stdin"]
    assert any("Coach says" in p.get("text", "") for n, p in calls if n == "sendMessage")
    await press(app, "alt:2026-09-30:short")
    assert "I only have 30 minutes today. Give me a 30 minute version of today's session (Legs and core)." in claude.last()["stdin"]
    assert claude.last()["argv"][claude.last()["argv"].index("--tools") + 1] == "WebSearch"


async def test_pre_gym_reminder_has_the_buttons_too(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 9, 30, 17, 30)
    await bot.job_session_reminder(ctx(app))
    assert keyboard_data(app.tg.sent()[-1]) == ["alt:2026-09-30:light", "alt:2026-09-30:short"]


async def test_empty_times_turn_reminders_off(env, clock):
    cfg = bot.Config.from_env({**env, "DAILY_WORKOUT_TIME": "", "CHECK_TIME": "", "REMINDER_TIME_FRI": ""})
    application = bot.build_application(cfg, request=FakeTelegram())
    names = {j.name for j in application.job_queue.jobs()}
    assert "daily_workout" not in names and "evening_check" not in names and "session_reminder_fri" not in names
    assert {"session_reminder", "checkin", "weekly_plan", "token_check"} <= names
    cfg = bot.Config.from_env({**env, "DAILY_WORKOUT_TIME": "06:15", "DAILY_WORKOUT_DAYS": "Mon-Fri"})
    application = bot.build_application(cfg, request=FakeTelegram())
    after = datetime(2026, 10, 2, 12, 0, tzinfo=cfg.tz)  # Friday noon: next is Monday
    assert next_fire(application, "daily_workout", after) == datetime(2026, 10, 5, 6, 15, tzinfo=cfg.tz)


# ---------------------------------------------------------------------------
# Second review fixes
# ---------------------------------------------------------------------------

BLOCKED = bot.parse_list(bot.DEFAULT_BLOCKED)
ALLOWED = bot.parse_list(bot.DEFAULT_ALLOWED)


def hits(line: str):
    return bot.find_blocked(f"📅 Monday: Push\n{line}", BLOCKED, ALLOWED)


@pytest.mark.parametrize(
    "line",
    [
        "Shoulder note: no overhead pressing, dips or upright rows this week.",
        "Avoid: overhead press, dips, upright rows",
        "Leave out: overhead pressing, dips",
        "Swaps for your shoulder: floor press for bench press, landmine press for overhead press.",
        "1. Seated cable row: 3 x 12, sit upright and row to your belly button",
        "Warm up: arm circles, reach overhead and press the wall",
        "**Cue**: press up and forward, like a half overhead press.",
        "Form cue: not an overhead press.",
        "Cue - no dips",
        "1. Reverse pec deck: 3 x 12",
        "1. Rear flyes: 3 x 12",
        "1. Side plank hip dips: 3 x 10",
        "1. Countermovement jumps: quick dip then jump, 3 x 5",
        "1. Snatch grip RDL: 3 x 8",
        "Why: this replaces overhead pressing while the shoulder heals.",
    ],
)
def test_no_false_alarms_on_safe_lines(line):
    assert hits(line) == []


@pytest.mark.parametrize(
    "line",
    [
        "1. Close grip push ups (or bench dips if easy): 3 x 10",
        "1. Dumbbell floor press (or barbell bench press if pain free)",
        "Warm up: 5 min row, light overhead pressing with an empty bar",
        "Not feeling the shoulder? Finish with bench dips",
        "Finisher: no more than 2 sets of bench dips",
        "1. Swap push ups for dips: 3 x 10",
    ],
)
def test_blocked_moves_in_brackets_and_ing_forms(line):
    assert hits(line)


async def test_future_programme_start_blocks_sunday_jobs_and_checks(env, clock, claude):
    cfg = bot.Config.from_env({**env, "PROGRAM_START": "2026-10-19"})
    application = bot.build_application(cfg, request=FakeTelegram(), concurrent=False)
    await application.initialize()
    try:
        c = ctx(application)
        for sunday in (date(2026, 10, 4), date(2026, 10, 11)):
            clock.set(sunday.year, sunday.month, sunday.day, 18, 0)
            await bot.job_checkin(c)
            clock.set(sunday.year, sunday.month, sunday.day, 20, 0)
            await bot.job_weekly_plan(c)
        clock.set(2026, 10, 14, 21, 0)
        await bot.job_evening_check(c)
        coach = application.bot_data["coach"]
        assert coach.store.plan_mondays() == [] and claude.all() == []
        clock.set(2026, 10, 18, 20, 0)  # the Sunday before the start builds week 1
        await bot.job_weekly_plan(c)
        assert coach.store.plan_mondays() == [date(2026, 10, 19)]
        assert coach.store.load_plan_meta(date(2026, 10, 19))["week"] == 1
    finally:
        await application.shutdown()


async def test_shoulder_rating_with_a_note(app):
    texts = await send(app, "/shoulder 4, sore after bench")
    assert texts[0].startswith("🩹 <b>LEFT SHOULDER</b> · 4/10 saved")
    assert app.bot_data["coach"].store.ratings()[0]["note"] == "sore after bench"
    assert "whole number" in (await send(app, "/shoulder 3,5"))[0]


def test_code_inside_links_does_not_leak():
    for text in ("Video: [yt: `goblet squat` proper form]", "[`squat`](https://www.youtube.com/watch?v=abcdefg)"):
        assert "\x00" not in bot.to_html(text)
    assert "search_query=goblet+squat+proper+form" in bot.to_html("Video: [yt: `goblet squat` proper form]")


async def test_skipped_then_done_does_not_rewrite(app, claude, clock):
    await send(app, "/plan")
    app.tg.delay = 0.05
    await asyncio.gather(
        app.process_update(callback_update(app, "chk:2026-09-30:skip")),
        app.process_update(callback_update(app, "chk:2026-09-30:done")),
    )
    await asyncio.sleep(0.3)
    coach = app.bot_data["coach"]
    assert coach.store.sessions()["2026-09-30"]["status"] == "done"
    assert len(claude.plan_calls()) == 1  # no adjustment for a session that was done
