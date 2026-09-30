"""Improvements: safety review, Garmin workouts, holidays and travel, progress and reports."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

import bot
from conftest import send


def ctx(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


@pytest.fixture
def review_coach(env, clock, claude):
    return bot.Coach(bot.Config.from_env({**env, "SAFETY_REVIEW": "on"}))


# ---------------------------------------------------------------------------
# Safety review
# ---------------------------------------------------------------------------


async def test_safety_review_runs_on_a_small_model_without_tools(review_coach, claude):
    result = await review_coach.build_week(date(2026, 9, 28))
    review = claude.review_calls()
    assert len(review) == 1
    argv = review[0]["argv"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == ""
    assert "Leave out overhead pressing, dips, upright rows" in review[0]["system"]
    assert "📅 Monday: Push" in review[0]["stdin"]
    assert len(claude.plan_calls()) == 1 and result.warnings == [] and result.meta["safety_review"] == []


async def test_safety_review_problem_triggers_the_one_fix(review_coach, claude):
    claude.enqueue({"review": True, "result": "PROBLEM: Monday: 1. Heavy push up variation: loads the left shoulder heavily"})
    await review_coach.build_week(date(2026, 9, 28))
    calls = claude.plan_calls()
    assert len(calls) == 2
    assert "Safety review: Monday: 1. Heavy push up variation: loads the left shoulder heavily" in calls[1]["stdin"]
    assert len(claude.review_calls()) == 1  # the fixed plan is not reviewed again
    meta = review_coach.store.load_plan_meta(date(2026, 9, 28))
    assert meta["fixed_once"] is True and meta["safety_review"]


async def test_failed_safety_review_does_not_block_the_plan(review_coach, claude):
    claude.enqueue({"review": True, "mode": "auth"})
    result = await review_coach.build_week(date(2026, 9, 28))
    assert review_coach.store.load_plan(date(2026, 9, 28)) and result.warnings == []


async def test_no_safety_review_when_the_injury_check_is_off(env, clock, claude):
    coach = bot.Coach(bot.Config.from_env({**env, "SAFETY_REVIEW": "on", "INJURY_CHECK": "off"}))
    await coach.build_week(date(2026, 9, 28))
    assert claude.review_calls() == []


# ---------------------------------------------------------------------------
# Garmin workouts at the 9pm check
# ---------------------------------------------------------------------------


def garmin_with(cfg, day, activities):
    import json
    import sample_garmin
    from pathlib import Path

    Path(cfg.garmin_db).parent.mkdir(parents=True, exist_ok=True)
    conn = sample_garmin.build(cfg.garmin_db, day, keep_open=True)
    conn.execute(
        "UPDATE daily_snapshots SET activities_json=? WHERE profile='Me' AND day=?",
        (json.dumps(activities), day.isoformat()),
    )
    conn.close()


async def test_watch_workout_marks_the_day_done(app, claude, clock, cfg):
    await send(app, "/plan")
    garmin_with(cfg, date(2026, 9, 30), [
        {"type": "strength_training", "name": "Strength", "duration_s": 52 * 60, "source": "recorded"},
        {"type": "running", "name": "Walk", "duration_s": 5 * 60, "source": "recorded"},  # too short
    ])
    clock.set(2026, 9, 30, 21, 0)
    await bot.job_evening_check(ctx(app))
    sent = app.tg.sent()[-1]
    assert sent["text"].startswith("✅ Your watch shows a 52 min strength training session today, so I marked today as done.")
    assert "rate:2026-09-30:3" in str(sent["reply_markup"])
    assert app.bot_data["coach"].store.sessions()["2026-09-30"]["source"] == "garmin"


async def test_auto_detected_moves_do_not_count(app, claude, clock, cfg):
    await send(app, "/plan")
    garmin_with(cfg, date(2026, 9, 30), [
        {"type": "running", "name": "Move IQ", "duration_s": 40 * 60, "source": "auto_detected"},
        {"type": "yoga", "name": "Yoga", "duration_s": 40 * 60, "source": "recorded"},
    ])
    clock.set(2026, 9, 30, 21, 0)
    await bot.job_evening_check(ctx(app))
    assert app.tg.texts()[-1] == "Did you train today (Legs and core)?"


async def test_auto_done_can_be_turned_off(env, clock, claude, cfg):
    garmin_with(cfg, date(2026, 9, 30), [{"type": "lap_swimming", "duration_s": 1800, "source": "recorded"}])
    coach = bot.Coach(bot.Config.from_env({**env, "GARMIN_AUTO_DONE": "off"}))
    assert coach.cfg.garmin_auto_done is False
    assert coach.garmin.workouts(date(2026, 9, 30))[0]["minutes"] == 30


# ---------------------------------------------------------------------------
# Public holidays and days away
# ---------------------------------------------------------------------------

TODAY = date(2026, 9, 30)  # a Wednesday


@pytest.mark.parametrize(
    "text,start,end,note",
    [
        ("8 Oct to 9 Oct Bangkok trip", date(2026, 10, 8), date(2026, 10, 9), "Bangkok trip"),
        ("2026-10-08 - 2026-10-10", date(2026, 10, 8), date(2026, 10, 10), ""),
        ("thu fri work trip", date(2026, 10, 1), date(2026, 10, 2), "work trip"),
        ("tomorrow hotel gym only", date(2026, 10, 1), date(2026, 10, 1), "hotel gym only"),
        ("Oct 12 conference", date(2026, 10, 12), date(2026, 10, 12), "conference"),
        ("5/1 new year trip", date(2027, 1, 5), date(2027, 1, 5), "new year trip"),
        ("28 Dec to 2 Jan family", date(2026, 12, 28), date(2027, 1, 2), "family"),
        ("8th October - 10th October, KL", date(2026, 10, 8), date(2026, 10, 10), "KL"),
        ("Friday", date(2026, 10, 2), date(2026, 10, 2), ""),
    ],
)
def test_away_dates(text, start, end, note):
    assert bot.parse_away(text, TODAY) == (start, end, note)


@pytest.mark.parametrize("text", ["blah", "8 Oct to", "10 Oct to 8 Oct", "1 Oct to 30 Dec", "32/13"])
def test_away_dates_that_make_no_sense(text):
    with pytest.raises(ValueError):
        bot.parse_away(text, TODAY)


def test_singapore_public_holidays():
    assert bot.public_holiday("SG", date(2026, 8, 9)) == "National Day"
    assert bot.public_holiday("SG", date(2026, 8, 10)) == "National Day (observed)"
    assert bot.public_holiday("SG", date(2026, 8, 11)) is None
    assert bot.public_holiday("", date(2026, 8, 9)) is None


async def test_away_command_and_plan_request(app, claude, clock):
    texts = await send(app, "/away 1 Oct to 2 Oct Bangkok trip")
    assert texts[0].startswith("Saved: away Thu 1 Oct to Fri 2 Oct (Bangkok trip).")
    await send(app, "/plan")
    request = claude.plan_calls()[-1]["stdin"]
    assert "Days off this week. On these days give a hotel gym or bodyweight version" in request
    assert "Thu 1 Oct: away (Bangkok trip)\nFri 2 Oct: away (Bangkok trip)" in request
    listing = (await send(app, "/away"))[0]
    assert "• Thu 1 Oct to Fri 2 Oct (Bangkok trip)" in listing
    clock.set(2026, 10, 1, 12, 0)
    today = (await send(app, "/today"))[0]
    assert today.startswith("✈️ You are away today (Bangkok trip).")
    await send(app, "/ask what can I do in the hotel?")
    assert "Days off in the next two weeks" in claude.last()["system"]
    assert (await send(app, "/away clear")) == ["Days away cleared."]
    assert "No days away saved." in (await send(app, "/away"))[0]


async def test_public_holidays_reach_the_plan(coach, claude, clock):
    coach.store.update_state(program_start="2026-11-02")
    await coach.build_week(date(2026, 11, 2))
    request = claude.plan_calls()[-1]["stdin"]
    assert "Sun 8 Nov: public holiday (Deepavali)" in request
    await coach.build_week(date(2026, 11, 9))
    assert "Mon 9 Nov: public holiday (Deepavali (observed))" in claude.plan_calls()[-1]["stdin"]


async def test_holidays_can_be_turned_off(env, clock, claude):
    coach = bot.Coach(bot.Config.from_env({**env, "HOLIDAYS_COUNTRY": ""}))
    assert coach.days_off(date(2026, 8, 9), date(2026, 8, 10)) == []


# ---------------------------------------------------------------------------
# Progress from logs, the physio report and last week in numbers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,items",
    [
        ("rows 22kg 3x10, floor press 14kg 3x8 felt easy", [("rows", 22.0, 3, 10), ("floor press", 14.0, 3, 8)]),
        ("bench 3x10 @ 20 kg; push ups 3x12", [("bench", 20.0, 3, 10), ("push ups", None, 3, 12)]),
        ("45 lb landmine press 3x8", [("landmine press", 20.4, 3, 8)]),
        ("20kg rows 3x10 felt good", [("rows", 20.0, 3, 10)]),
        ("ran 5 km easy", []),
    ],
)
def test_log_items(text, items):
    assert [(i["name"], i["kg"], i["sets"], i["reps"]) for i in bot.parse_log_items(text)] == items


async def test_progress_command_and_context(app, claude, clock):
    clock.set(2026, 9, 21, 19, 0)
    await send(app, "/log rows 20kg 3x10, floor press 12kg 3x8")
    clock.set(2026, 9, 29, 19, 0)
    await send(app, "/log row 22kg 3x10, push ups 3x12")
    text = (await send(app, "/progress"))[0]
    assert "Row: 20 → 22 kg (+2 kg), last 3 x 10 · 2 sessions, latest Tue 29 Sep" in text
    assert "Push ups: 3 x 12 · 1 session" in text
    await send(app, "/ask what weight next?")
    system = claude.last()["system"]
    assert "Weights and reps from my logs, last 8 weeks" in system
    assert "• row: 20 kg 3 x 10 (Mon 21 Sep), 22 kg 3 x 10 (Tue 29 Sep)" in system


async def test_progress_without_weights(app):
    assert "No weights in your logs yet" in (await send(app, "/progress"))[0]


async def test_shoulder_sends_csv_and_sparkline(app, clock):
    for day, rating in ((14, 4), (22, 3), (29, 2)):
        clock.set(2026, 9, day, 21, 0)
        await send(app, f"/shoulder {rating}")
    clock.set(2026, 9, 30, 12, 0)
    before = len(app.tg.calls)
    await send(app, "/shoulder")
    calls = app.tg.calls[before:]
    text = [p for n, p in calls if n == "sendMessage"][0]["text"]
    assert "(weekly average, last 8 weeks, oldest first)" in text
    doc = [p for n, p in calls if n == "sendDocument"]
    assert len(doc) == 1 and "physio" in doc[0]["caption"]


def test_sparkline_and_csv(coach, clock):
    for d, r in (("2026-09-14", 4), ("2026-09-22", 3), ("2026-09-29", 2)):
        coach.store.add_rating(date.fromisoformat(d), coach.now(), r, "")
    spark = bot.weekly_sparkline(coach.store.ratings(), date(2026, 9, 30), weeks=4)
    assert spark.startswith("·▄▃▂")
    csv = bot.shoulder_csv(coach).decode("utf-8-sig").splitlines()
    assert csv[0] == "date,time,rating (0 = no pain / 10 = worst),note" and csv[1].startswith("2026-09-14,")


async def test_sunday_overview_has_last_week_in_numbers(app, claude, clock, cfg):
    import sample_garmin
    from pathlib import Path

    Path(cfg.garmin_db).parent.mkdir(parents=True, exist_ok=True)
    sample_garmin.build(cfg.garmin_db, date(2026, 10, 4))
    await send(app, "/plan")
    await send(app, "/done")
    await send(app, "/log rows 22kg 3x10")
    await send(app, "/shoulder 3")
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(ctx(app))
    text = app.tg.texts()[-1]
    assert "<b>Last week in numbers</b>\n• Sessions: 1 done, 0 skipped (5 training days planned)" in text
    assert "• Workout logs: 1" in text and "• Left shoulder: average 3.0" in text
    assert "• Runs: 2, 10.7 km" in text and "• Sleep: 7.4 h a night on average" in text
