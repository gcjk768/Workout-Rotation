"""Fixes from the third review: injury check, daily message, buttons, settings, formatting."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import bot
from conftest import callback_update, press, send

BLOCKED = bot.parse_list(bot.DEFAULT_BLOCKED)
ALLOWED = bot.parse_list(bot.DEFAULT_ALLOWED)


def hits(line: str):
    return bot.find_blocked(f"📅 Monday: Push\n{line}", BLOCKED, ALLOWED)


@pytest.mark.parametrize(
    "line",
    [
        "1. Bench dips: no added weight, 3 x 10, rest 60s",
        "1. Barbell bench press: not to failure, 4 x 6",
        "1. Arnold press: skip if the shoulder hurts, 3 x 10",
        "1. Dips: never to failure, 3 x 8",
        "1. Upright row: don't go above chest height, 3 x 12",
        "**1. Bench dips:** no added weight, 3 x 10",
        "1️⃣ Bench dips: no added weight, 3 x 10",
        "1. Overhead kettlebell carries: 3 x 20 m",
        "1. Kettlebell clean & press: 3 x 5",
        "Finisher: no rest between rounds, 3 rounds of push ups and bench dips",
        "1. OHP: 5 x 5",
        "1. Cable crossover: 3 x 12",
        "Hotel gym: no cables, so dumbbell push press 3 x 8",
        "1. Incline barbell press: 4 x 6",
    ],
)
def test_names_and_variants_are_caught(line):
    assert hits(line), line


@pytest.mark.parametrize(
    "line",
    [
        "1. Pec deck rear delt fly: 3 x 12",
        "1. Rear delt fly on the pec deck: 3 x 12",
        "1. Bent over dumbbell fly for the rear delts: 3 x 12",
        "1. Landmine press (shoulder friendly overhead press alternative): 3 x 8",
        "1. Floor press, kinder to the shoulder than bench press: 3 x 10",
        "Note: don’t do dips until your physio clears you.",
        "1. Countermovement jump: fast dip, then jump, 3 x 5",
        "Cool down: 5 min easy upright rowing on the erg",
        "1. Dumbbell neutral grip incline bench press: 3 x 10",
        "1. Machine chest press: 3 x 10 (like a bench press, but the handles are fixed)",
        "1. Crossover dribbles: 3 x 30 s",
    ],
)
def test_more_safe_lines(line):
    assert hits(line) == [], line


def test_injury_settings(env):
    cfg = bot.Config.from_env({**env, "INJURY_CHECK": "off"})
    assert cfg.blocked_movements == []
    cfg = bot.Config.from_env({**env, "INJURY_EXTRA_BLOCKED": "burpee", "INJURY_EXTRA_ALLOWED": "burpee broad jump"})
    assert cfg.blocked_movements[-1] == "burpee" and "overhead * press" in cfg.blocked_movements
    assert "burpee broad jump" in cfg.allowed_movements and "hip dip" in cfg.allowed_movements
    # an older bot.env with its own allowed list still gets the built-in ones
    cfg = bot.Config.from_env({**env, "INJURY_ALLOWED_MOVEMENTS": "reverse * fly"})
    assert "snatch grip" in cfg.allowed_movements


def ctx(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


async def test_no_plan_build_on_weekend_mornings(app, claude, clock):
    await send(app, "/plan")  # week of 28 Sep
    calls = len(claude.all())
    clock.set(2026, 10, 10, 7, 0)  # Saturday of the next week, which has no plan
    await bot.job_daily_workout(ctx(app))
    assert len(claude.all()) == calls
    clock.set(2026, 10, 5, 7, 0)  # a Monday still builds the missing week
    await bot.job_daily_workout(ctx(app))
    assert app.bot_data["coach"].store.load_plan(bot.date(2026, 10, 5))


async def test_old_alt_button_does_not_ask_claude(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 1, 9, 0)
    calls = len(claude.all())
    sent = await press(app, "alt:2026-09-30:light")
    assert len(claude.all()) == calls
    answer = [p for n, p in sent if n == "answerCallbackQuery"][0]
    assert "That button was for Wed 30 Sep" in answer["text"] and answer["show_alert"] is True


async def test_alt_double_tap_asks_once(app, claude, clock):
    await send(app, "/plan")
    app.tg.delay = 0.05
    calls = len(claude.all())
    await asyncio.gather(*(app.process_update(callback_update(app, "alt:2026-09-30:short")) for _ in range(3)))
    assert len(claude.all()) == calls + 1


async def test_rest_day_recovery_note(app, claude, clock, cfg):
    import sample_garmin
    from pathlib import Path

    Path(cfg.garmin_db).parent.mkdir(parents=True, exist_ok=True)
    sample_garmin.build(cfg.garmin_db, bot.date(2026, 10, 4), poor_recovery_today=True)
    await send(app, "/plan")
    clock.set(2026, 10, 4, 7, 0)  # Sunday, a rest day
    await bot.job_daily_workout(ctx(app))
    text = app.tg.texts()[-1]
    assert "Good timing for a rest day" in text and "Lighter version" not in text


def test_nested_links_keep_their_text():
    assert bot.to_html("[[yt: squat]](https://a.b/c)") == '<a href="https://a.b/c">▶️ squat</a>'
