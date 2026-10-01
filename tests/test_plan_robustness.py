"""Robustness of plan parsing, the injury check and the flows around them (from the bug hunt)."""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

import bot
from conftest import callback_update, send

BLOCKED = bot.parse_list(bot.DEFAULT_BLOCKED)
ALLOWED = bot.parse_list(bot.DEFAULT_ALLOWED)


def blocked(line: str) -> list[dict]:
    return bot.find_blocked(f"📅 Monday: Push\n{line}", BLOCKED, ALLOWED)


@pytest.mark.parametrize(
    "line",
    [
        "1. Overhead cable tricep extension: 3 x 12",
        "1. Overhead dumbbell triceps extension: 3 x 12",
        "1. Seated overhead DB press: 3 x 10",
        "1. Overhead kettlebell press: 3 x 8",
        "1. Upright cable row: 3 x 12",
        "1. Arnold press: 3 x 10. Video: [yt: arnold press proper form]",
        "Warm up: 5 min bike, empty bar overhead press",
        "Finisher: bench dips 3 x max",
        "A1. Barbell bench press: 5 x 5",
        "1a. Barbell bench press: 5 x 5",
        "1️⃣ Overhead press: 3 x 8",
        "**Overhead press**: 3 x 8",
        "Option B (no pool): barbell bench press 4 x 6",
        "1. Swap push ups for dips: 3 x 10",
        "1. Dumbbell pullover and barbell bench press superset",
        "1. Push ups, not dips, then barbell overhead press: 3 x 8",
        "1. Landmine and barbell push press: 3 x 5",
    ],
)
def test_blocked_variants_are_caught(line):
    assert blocked(line), line


@pytest.mark.parametrize(
    "line",
    [
        "1. Neutral grip dumbbell bench press: 3 x 10",
        "1. Push ups (not dips): 3 x 10",
        "Note: no overhead pressing and no dips yet.",
        "1. Single arm shoulder friendly landmine press: 3 x 10",
        "Instead of bench press, do floor press.",
        "1. Machine chest press: 3 x 10",
        "Warm up: 5 minutes bike, band pull aparts, arm circles.",
    ],
)
def test_safe_lines_pass(line):
    assert blocked(line) == [], line


@pytest.mark.parametrize(
    "header,focus",
    [
        ("📅️ Monday: Push", "Push"),
        ("🗓️ Monday: Push", "Push"),
        ("📅 Mon: Push", "Push"),
        ("- 📅 Monday: Push", "Push"),
        ("📅 Monday, 5 Oct: Push", "Push"),
        ("📅 Monday 5 October – Push", "Push"),
    ],
)
def test_header_variants(header, focus):
    parsed = bot.parse_plan(f"{header}\n1. Row: 3 x 10")
    assert parsed.days[0].focus == focus


def test_second_header_for_the_same_day_is_kept_and_checked():
    plan = "📅 Friday: Swim\n1. Kick sets: 8 x 50 m\n📅 Friday (if you do not swim): Legs\n1. Barbell push press: 3 x 5"
    parsed = bot.parse_plan(plan)
    assert "Barbell push press" in parsed.days[4].text
    assert bot.find_blocked(plan, BLOCKED, ALLOWED)[0]["term"] == "push press"
    assert (4, "Barbell push press") in bot.plan_exercises(plan)


@pytest.mark.parametrize(
    "focus,rest",
    [
        ("Rest day, off from the gym", True),
        ("Rest, easy swim optional", True),
        ("Rest (warm walk)", True),
        ("Rest day, feedback and planning", True),
        ("Basketball or rest", True),
        ("Cardio and mobility, or rest", False),
        ("Plyometrics and agility, or rest", False),
        ("Push", False),
    ],
)
def test_rest_days(focus, rest):
    assert bot.is_rest_focus(focus) is rest


@pytest.mark.parametrize(
    "focus",
    ["Basketball, keep the legs fresh", "Basketball, protect the shoulder", "Rest (warm bath and a walk)", "Rest, slower walk"],
)
def test_no_false_missing_exercise_warnings(coach, focus):
    plan = "\n".join(f"📅 {d}: " + (focus if d == "Saturday" else "Rest") for d in bot.DAY_NAMES)
    assert coach.plan_problems(plan) == []


async def test_a_worse_fix_is_not_saved(coach, claude):
    good_but_one_dip = "\n".join(
        f"📅 {d}: Push\n1. {'Bench dips' if d == 'Thursday' else 'Cable row'}: 3 x 10" for d in bot.DAY_NAMES
    )
    claude.enqueue({"result": good_but_one_dip}, {"result": "📅 Thursday: Push\n1. Push ups: 3 x 10"})
    result = await coach.build_week(date(2026, 9, 28))
    saved = coach.store.load_plan(date(2026, 9, 28))
    assert bot.parse_plan(saved).missing_days == []
    assert "Claude's fixed version was worse, so I kept the first version." in result.warnings


async def test_double_tap_on_skipped_adjusts_once(app, claude, clock):
    await send(app, "/plan")
    app.tg.delay = 0.05
    await asyncio.gather(
        app.process_update(callback_update(app, "chk:2026-09-30:skip")),
        app.process_update(callback_update(app, "chk:2026-09-30:skip")),
    )
    assert len(claude.plan_calls()) == 2  # the first plan, plus one adjustment


async def test_late_skip_keeps_days_that_already_passed(app, claude, clock):
    clock.set(2026, 9, 28, 12, 0)
    await send(app, "/plan")
    clock.set(2026, 9, 30, 9, 0)  # Monday's button tapped on Wednesday
    await app.process_update(callback_update(app, "chk:2026-09-28:skip"))
    request = claude.plan_calls()[-1]["stdin"]
    assert "I skipped Monday's session (Monday: Push)." in request
    assert "Adjust the rest of this week, Thursday to Sunday" in request
    assert "Keep Monday to Wednesday exactly as written" in request


async def test_no_auto_build_for_the_week_before_the_programme(app, claude, clock):
    clock.set(2026, 10, 1, 12, 0)  # Thursday
    await send(app, "/nextweek")  # the programme starts on Monday 5 October
    from types import SimpleNamespace

    clock.set(2026, 10, 1, 17, 30)
    await bot.job_session_reminder(SimpleNamespace(bot=app.bot, application=app, job=None))
    assert app.bot_data["coach"].store.plan_mondays() == [date(2026, 10, 5)]


async def test_sunday_rebuild_keeps_nextweek_notes(app, claude, clock):
    from types import SimpleNamespace

    ctx = SimpleNamespace(bot=app.bot, application=app, job=None)
    await send(app, "/plan")
    clock.set(2026, 10, 3, 10, 0)
    await send(app, "/nextweek travelling Thu and Fri, hotel gym only")
    clock.set(2026, 10, 4, 18, 0)
    await bot.job_checkin(ctx)
    clock.set(2026, 10, 4, 18, 30)
    await send(app, "Tired but fine")
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(ctx)
    request = claude.plan_calls()[-1]["stdin"]
    assert "My notes for this plan: travelling Thu and Fri, hotel gym only" in request
    assert "My Sunday check in answer: Tired but fine" in request


def test_code_spans_do_not_break_html():
    assert bot.to_html("Use `**` for bold, like **this**") == "Use <code>**</code> for bold, like <b>this</b>"
    assert bot.to_html("`[yt: squat]`") == "<code>[yt: squat]</code>"


async def test_shoulder_needs_a_whole_number(app):
    texts = await send(app, "/shoulder 3.5")
    assert "whole number" in texts[0]
    assert app.bot_data["coach"].store.ratings() == []


def test_torn_log_line_does_not_swallow_the_next(coach, clock):
    path = coach.store.root / "logs.jsonl"
    path.write_text('{"date": "2026-09-29", "time": "20:00", "text": "rows 20kg"}\n{"date": "2026-09-30", "te')
    coach.store.add_log(coach.now(), "squats 40kg")
    assert [r["text"] for r in coach.store.logs()] == ["rows 20kg", "squats 40kg"]


@pytest.mark.parametrize(
    "line,entry",
    [
        # shapes seen in a real Claude plan
        ("6. Rehab: Band external rotation at the side: 2 x 15 each side, rest 30s", ("Band external rotation at the side", True)),
        ("2. Single arm cable chest press, standing: 3 x 10 each side, rest 75s", ("Single arm cable chest press", False)),
        ("5. Run: 20 to 25 min (about 4 km) on the treadmill or outside", ("Run", False)),
        ("1. Warm up kick with board: 4 x 50 m, rest 20s", ("Warm up kick with board", False)),
        ("3. Superset: goblet squat 3 x 10", ("goblet squat", False)),
    ],
)
def test_exercise_entries(line, entry):
    assert bot.exercise_entry(line) == entry


async def test_rehab_moves_may_repeat_and_are_tagged(coach, claude):
    plan = "\n".join(
        f"📅 {d}: Push\n1. Cable row: 3 x 10\n2. Rehab: Band external rotation: 2 x 15" for d in bot.DAY_NAMES
    )
    coach.store.save_plan(date(2026, 9, 21), plan, {}, "x")
    assert coach.previous_exercises(date(2026, 9, 28)) == ["Cable row"]
    result = bot.PlanResult(date(2026, 9, 21), plan, {}, [])
    assert "Cable row, Band external rotation (rehab)" in "\n".join(coach.overview(result))
