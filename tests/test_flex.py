"""Sick days and days off, /swap, /add and the FoodLens numbers."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from types import SimpleNamespace

import pytest

import bot
from conftest import press, send
from test_stage2_reminders import keyboard_data

MONDAY = date(2026, 9, 28)  # the test clock starts on Wednesday 30 Sep 2026, 12:00
WED, THU, SAT = date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 3)


@pytest.fixture
def env(env, monkeypatch, tmp_path):
    monkeypatch.setenv("STRUCTURED_PLANS", "on")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", "1")
    monkeypatch.setenv("FOODLENS_DB", str(tmp_path / "foodlens.db"))
    return {**env, "STRUCTURED_PLANS": "on", "FOODLENS_DB": str(tmp_path / "foodlens.db")}


def ctx(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


def focus(app, weekday: int) -> str:
    return app.bot_data["coach"].store.load_plan_data(MONDAY)["days"][weekday]["focus"]


# ---------------------------------------------------------------------------
# 1. Sick days and days off
# ---------------------------------------------------------------------------

async def test_sick_today_is_a_rest_day_with_no_reminders(app, claude, clock):
    await send(app, "/plan")
    coach = app.bot_data["coach"]
    text = (await send(app, "/sick"))[0]
    assert text.startswith("🛌 <b>REST DAYS</b> · saved") and "Saved: sick today." in text
    assert "do not count as skipped" in text and "go lighter" in text
    assert coach.store.away() == [{"start": "2026-09-30", "end": "2026-09-30", "note": "", "kind": "sick"}]
    assert coach.store.sessions()["2026-09-30"]["status"] == "off"
    assert "🤒 Sick day today: rest, no training." in (await send(app, "/today"))[0]

    before = len(app.tg.sent())
    await bot.job_session_reminder(ctx(app))   # the nudge before the gym stays quiet
    await bot.job_evening_check(ctx(app))      # and nobody asks "did you train?"
    assert len(app.tg.sent()) == before
    await bot.job_daily_workout(ctx(app))      # the morning card is one short rest card, no workout buttons
    morning = app.tg.sent()[before:]
    assert len(morning) == 1 and morning[0]["text"].startswith("🛌 <b>REST DAYS</b> · Wed 30 Sep")
    assert "Sick day today" in morning[0]["text"] and keyboard_data(morning[0]) == []

    cleared = (await send(app, "/sick clear"))[0]
    assert "cleared" in cleared and coach.store.away() == [] and "2026-09-30" not in coach.store.sessions()
    before = len(app.tg.sent())
    await bot.job_evening_check(ctx(app))
    assert "Did you train today" in app.tg.sent()[before]["text"]


async def test_sick_for_three_days_then_an_easy_first_session_back(app, claude, clock):
    await send(app, "/plan")
    coach = app.bot_data["coach"]
    assert "Saved: sick Wed 30 Sep to Fri 2 Oct." in (await send(app, "/sick 3"))[0]
    assert coach.rest_on(date(2026, 10, 2))["kind"] == "sick" and coach.rest_on(SAT) is None
    request = coach.plan_request(MONDAY, "")
    assert "Wed 30 Sep: sick, full rest with no training" in request
    assert "Where a day says full rest, plan a rest day with no training at all." in request
    next_week = coach.plan_request(date(2026, 10, 5), "")
    assert "I was sick until Fri 2 Oct. Make my first two sessions after that lighter" in next_week
    assert "I am sick, from Wed 30 Sep to Fri 2 Oct." in coach.system_prompt()

    clock.set(2026, 10, 5, 7, 0)  # Monday: back, nothing done since
    assert "First session back after being sick (until Fri 2 Oct)" in coach.back_note(coach.today())
    assert "I was sick until Fri 2 Oct. Ease me back in" in coach.system_prompt()
    await send(app, "/done")
    assert coach.back_note(coach.today()) is None  # only for the first session


async def test_off_days_and_bad_input(app, clock):
    coach = app.bot_data["coach"]
    text = (await send(app, "/off sat to sun family trip"))[0]
    assert "Saved: off Sat 3 Oct to Sun 4 Oct (family trip). Enjoy the break." in text
    assert coach.rest_on(SAT)["kind"] == "off" and "2026-09-30" not in coach.store.sessions()
    assert "🏖 <b>Sat 3 Oct to Sun 4 Oct</b> · day off · family trip" in (await send(app, "/away"))[0]
    assert "I could not read that" in (await send(app, "/sick whenever"))[0]
    assert "I could not read that" in (await send(app, "/sick 99"))[0]


async def test_sick_button_on_the_morning_card(app, claude, clock):
    await send(app, "/plan")
    before = len(app.tg.sent())
    await bot.job_daily_workout(ctx(app))
    assert keyboard_data(app.tg.sent()[-1]) == ["alt:2026-09-30:light", "alt:2026-09-30:short",
                                               "swp:2026-09-30:menu", "off:2026-09-30:sick"]
    calls = await press(app, "off:2026-09-30:sick")
    assert "Saved: sick today." in [p for name, p in calls if name == "sendMessage"][0]["text"]
    await press(app, "off:2026-09-30:sick")  # a second tap adds nothing
    assert len(app.bot_data["coach"].store.away()) == 1
    assert len(app.tg.sent()) > before


# ---------------------------------------------------------------------------
# 2 and 3. Swap workouts, add a workout on a rest day
# ---------------------------------------------------------------------------

async def test_swap_today_with_another_day_trades_the_sessions(app, claude, clock):
    await send(app, "/plan")
    assert (focus(app, 2), focus(app, 3)) == ("Legs and core", "Upper body and rehab")
    wed = app.bot_data["coach"].store.load_plan_data(MONDAY)["days"][2]
    calls = len(claude.all())
    texts = await send(app, "/swap thu")
    assert texts[0].startswith("🔁 <b>SWAP WORKOUT</b> · done")
    assert "Today is now <b>Upper body and rehab</b>. Thursday takes <b>Legs and core</b>." in texts[0]
    assert "WEDNESDAY 30 SEP</b> · Upper body and rehab" in texts[1]  # today's new card follows
    assert (focus(app, 2), focus(app, 3)) == ("Upper body and rehab", "Legs and core")
    moved = app.bot_data["coach"].store.load_plan_data(MONDAY)["days"][3]
    assert moved["sections"] == wed["sections"] and moved["day"] == "Thursday"  # the whole session moved
    assert len(claude.all()) == calls  # a trade needs no Claude call
    assert "📅 Thursday: Legs and core" in app.bot_data["coach"].store.load_plan(MONDAY)  # the text plan follows


async def test_swap_by_body_part_finds_the_day_or_writes_a_new_session(app, claude, clock):
    await send(app, "/plan")
    calls = len(claude.all())
    await send(app, "/swap upper")  # Thursday has it: the two days trade
    assert (focus(app, 2), focus(app, 3)) == ("Upper body and rehab", "Legs and core") and len(claude.all()) == calls
    assert "Today is already Upper body and rehab." in (await send(app, "/swap upper"))[0]

    texts = await send(app, "/swap arms")  # nowhere this week: the coach writes it
    request = claude.plan_calls()[-1]["stdin"]
    assert "Give me a full arms session for today." in request and "Return only this one day, Wednesday." in request
    assert focus(app, 2) == "Arms"
    joined = "\n\n".join(texts)
    assert "Writing a arms session for today" in joined and "today is now Arms" in joined
    assert "Upper body and rehab is off this week's plan now." in joined and "/add sat upper body and rehab" in joined


async def test_swap_rules_and_two_named_days(app, claude, clock):
    await send(app, "/plan")
    assert "only swap today and later days" in (await send(app, "/swap mon fri"))[0]
    assert "same day" in (await send(app, "/swap wed"))[0]
    await send(app, "/swap thu fri")
    assert (focus(app, 3), focus(app, 4)) == ("Legs and run, or swim", "Upper body and rehab")
    await send(app, "/swap sat")  # today's session moves to Saturday, today becomes rest
    days = app.bot_data["coach"].store.load_plan_data(MONDAY)["days"]
    assert days[2]["rest_day"] and days[5]["focus"] == "Legs and core" and not days[5]["rest_day"]
    await send(app, "/done")
    assert "already marked done" in (await send(app, "/swap fri"))[0]


async def test_swap_menu_buttons(app, claude, clock):
    await send(app, "/plan")
    await send(app, "/swap")
    menu = app.tg.sent()[-1]
    assert "Today is <b>Legs and core</b>. What do you want to do instead?" in menu["text"]
    data = keyboard_data(menu)
    assert data[:4] == [f"swp:2026-09-30:{d}" for d in (3, 4, 5, 6)]
    assert "swp:2026-09-30:pArms" in data and "swp:2026-09-30:pLegs" not in data  # legs is today
    await press(app, "swp:2026-09-30:3")
    assert (focus(app, 2), focus(app, 3)) == ("Upper body and rehab", "Legs and core")
    calls = await press(app, "swp:2026-09-29:3")  # an old button
    assert any(name == "answerCallbackQuery" and p.get("show_alert") for name, p in calls)


async def test_add_a_workout_on_a_rest_day(app, claude, clock):
    await send(app, "/plan")
    assert "Tell me the day" in (await send(app, "/add"))[0]
    assert "Thursday already has Upper body and rehab." in (await send(app, "/add thu arms"))[0]
    texts = await send(app, "/add sat arms")
    assert "Give me a full arms session for Saturday 3 Oct." in claude.plan_calls()[-1]["stdin"]
    day = app.bot_data["coach"].store.load_plan_data(MONDAY)["days"][5]
    assert day["focus"] == "Arms" and day["rest_day"] is False and day["sections"]
    assert "Arms added on Sat 3 Oct" in "\n\n".join(texts)
    assert "📅 Saturday: Arms" in app.bot_data["coach"].store.load_plan(MONDAY)
    await send(app, "/add sun")  # no body part: the one that sits out this week
    assert focus(app, 6) == app.bot_data["coach"].sitting_out(MONDAY)
    clock.set(2026, 10, 3, 9, 0)  # Saturday is a training day now: it gets the evening question
    before = len(app.tg.sent())
    await bot.job_evening_check(ctx(app))
    assert "Did you train today (Arms)?" in app.tg.sent()[before]["text"]


# ---------------------------------------------------------------------------
# 4. Food from FoodLens
# ---------------------------------------------------------------------------

def foodlens(path: str, meals: list[tuple[str, str, list[dict]]], profile: tuple | None = None) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE meal (id INTEGER PRIMARY KEY, day DATE, slot VARCHAR, status VARCHAR);"
        "CREATE TABLE meal_item (id INTEGER PRIMARY KEY, meal_id INTEGER, name VARCHAR, nutrients JSON);"
        "CREATE TABLE profile (id INTEGER PRIMARY KEY, sex VARCHAR, age INTEGER, height_cm FLOAT, weight_kg FLOAT,"
        " activity VARCHAR, goal VARCHAR, overrides JSON);")
    conn.execute("CREATE TABLE water_log (id INTEGER PRIMARY KEY, day DATE, cups FLOAT)")
    for day, status, items in meals:
        meal = conn.execute("INSERT INTO meal (day, slot, status) VALUES (?, 'lunch', ?)", (day, status)).lastrowid
        for item in items:
            conn.execute("INSERT INTO meal_item (meal_id, name, nutrients) VALUES (?, 'x', ?)", (meal, json.dumps(item)))
    if profile:
        conn.execute("INSERT INTO profile (sex, age, height_cm, weight_kg, activity, goal, overrides) VALUES (?,?,?,?,?,?,?)", profile)
    conn.commit()
    conn.close()


def item(kcal, protein, carbs=50, fat=20) -> dict:
    return {"kcal": kcal, "protein_g": protein, "carbs_g": carbs, "fat_g": fat, "sodium_mg": 900}


async def test_food_totals_come_from_foodlens(app, cfg, clock):
    foodlens(cfg.foodlens_db, [
        ("2026-09-30", "logged", [item(600, 30), item(250, 12.4)]),
        ("2026-09-30", "logged", [item(700, 45)]),
        ("2026-09-30", "deleted", [item(9999, 999)]),   # a deleted meal never counts
        ("2026-09-28", "logged", [item(1800, 90)]),
    ])
    coach = app.bot_data["coach"]
    # no FoodLens profile: its default 2,000 kcal; protein is 1.6 g per kg of the 78 kg in bot.env
    assert coach.food_targets() == {"kcal": 2000, "protein_g": 125}
    assert coach.food_line(date(2026, 9, 30)) == "🍽 Food logged: 1,550 of 2,000 kcal · protein 87 of 125 g · 2 meals"
    assert coach.food_line(date(2026, 9, 29)) is None
    text = (await send(app, "/food"))[0]
    assert text.startswith("🍽 <b>FOOD INTAKE</b> · Wed 30 Sep")
    assert "<b>Today</b> · 1,550 of 2,000 kcal · protein 87 of 125 g · 2 meals" in text
    assert "About 38 g of protein to go today." in text
    assert "• Mon 28 Sep: 1,800 kcal · protein 90 g · 1 meals" in text
    assert "2 days logged, average 1,675 kcal and 89 g protein a day" in text
    prompt = coach.system_prompt()
    assert "My food from my FoodLens tracker, last 7 days" in prompt and "Wed 30 Sep: 1550 kcal, 87 g protein" in prompt
    assert "Food (FoodLens): 2 days logged" in coach.week_in_numbers(MONDAY)
    await send(app, "/plan")
    before = len(app.tg.sent())
    await bot.job_evening_check(ctx(app))
    assert "🍽 Food logged: 1,550 of 2,000 kcal" in app.tg.sent()[before]["text"]


async def test_food_targets_follow_the_foodlens_profile(app, cfg):
    foodlens(cfg.foodlens_db, [], ("male", 35, 175.0, 78.0, "moderate", "lose", json.dumps({"kcal": 2100})))
    coach = app.bot_data["coach"]
    assert coach.food.targets() == {"kcal": 2100, "protein_g": 94}  # the override wins, protein 1.2 g/kg for "lose"
    assert coach.food_targets()["protein_g"] == 125                   # the training target is higher
    assert "No meals logged in FoodLens in the last 7 days." in (await send(app, "/food"))[0]


async def test_no_foodlens_database_changes_nothing(app, claude, clock):
    coach = app.bot_data["coach"]
    assert coach.food_line(coach.today()) is None and coach.food_context(coach.today()) is None
    assert "I cannot read FoodLens's database." in (await send(app, "/food"))[0]
    assert "FoodLens" not in coach.system_prompt()


# ---------------------------------------------------------------------------
# One topic shared with FoodLens (SHARED_TOPIC=on)
# ---------------------------------------------------------------------------

def _in_topic(coach, shared: bool, reply_from=None, private: bool = False):
    coach.cfg.shared_topic = shared
    reply = SimpleNamespace(from_user=SimpleNamespace(id=reply_from[0], is_bot=reply_from[1])) if reply_from else None
    update = SimpleNamespace(effective_message=SimpleNamespace(reply_to_message=reply, text="150"),
                             effective_chat=SimpleNamespace(type="private" if private else "supergroup"))
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"coach": coach}), bot=SimpleNamespace(id=42))
    return update, context


def test_shared_topic_leaves_replies_to_the_other_bot_alone(coach):
    assert bot.foreign_reply(*_in_topic(coach, True, (777, True)))        # a reply to FoodLens: not for the coach
    assert not bot.foreign_reply(*_in_topic(coach, True, (42, True)))     # a reply to the coach itself
    assert not bot.foreign_reply(*_in_topic(coach, True, (5, False)))     # a reply to a person
    assert not bot.foreign_reply(*_in_topic(coach, True))                 # plain text is still a question
    assert not bot.foreign_reply(*_in_topic(coach, False, (777, True)))   # the switch is off: as before
    assert not bot.foreign_reply(*_in_topic(coach, True, (777, True), private=True))
    assert bot.in_shared_topic(*_in_topic(coach, True)) and not bot.in_shared_topic(*_in_topic(coach, True, private=True))


async def test_unknown_command_still_answers_in_a_private_chat(app):
    assert "I do not know that command" in (await send(app, "/ate kaya toast"))[0]



# ---------------------------------------------------------------------------
# The workout reacts to food and drink
# ---------------------------------------------------------------------------

def water(path: str, rows: list[tuple[str, float]]) -> None:
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO water_log (day, cups) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


async def test_a_low_fuel_day_changes_the_workout_card_and_the_coach(app, cfg, claude, clock):
    # yesterday (Tue 29 Sep): two meals, 1,100 kcal and 40 g protein of 2,000 / 125, and 2 cups of water
    foodlens(cfg.foodlens_db, [("2026-09-29", "logged", [item(600, 25)]), ("2026-09-29", "logged", [item(500, 15)])])
    water(cfg.foodlens_db, [("2026-09-29", 1.0), ("2026-09-29", 1.0)])
    coach = app.bot_data["coach"]
    assert coach.fuel_check(coach.today()) == ["you ate little yesterday (1,100 of 2,000 kcal)",
                                               "protein was low yesterday (40 of 125 g)",
                                               "only 2 cups of water logged yesterday"]
    await send(app, "/plan")
    before = len(app.tg.sent())
    await bot.job_daily_workout(ctx(app))
    card = "\n\n".join(m["text"] for m in app.tg.sent()[before:])
    assert "⛽ <b>Fuel check</b> · You ate little yesterday (1,100 of 2,000 kcal)" in card
    assert "drink 2 to 3 cups of water before you start" in card and "use the 🪶 Lighter version today" in card
    prompt = coach.system_prompt()
    assert "How my food and drink change my training." in prompt and "Treat today as a low fuel day." in prompt
    assert "Water logged: Tue 29 Sep 2 cups" in prompt
    assert "My food and drink log is in your context." in coach.plan_request(MONDAY, "")
    await press(app, "alt:2026-09-30:light")  # the lighter version is written with that context
    assert "Treat today as a low fuel day." in claude.plan_calls()[-1]["system"]
    assert "⛽" in (await send(app, "/food"))[0]


async def test_a_well_fed_day_or_a_half_logged_day_changes_nothing(app, cfg, claude, clock):
    foodlens(cfg.foodlens_db, [("2026-09-29", "logged", [item(900, 60)]), ("2026-09-29", "logged", [item(1000, 55)]),
                               ("2026-09-28", "logged", [item(300, 5)])])   # Monday: one meal only, not judged
    water(cfg.foodlens_db, [("2026-09-29", 7.0)])
    coach = app.bot_data["coach"]
    assert coach.fuel_check(coach.today()) == [] and coach.fuel_note(coach.today()) is None
    assert "yesterday's intake gives no reason to hold back" in coach.system_prompt()
    clock.set(2026, 9, 29, 7, 0)  # Tuesday morning looks back at Monday's single meal
    assert coach.fuel_check(coach.today()) == []
    await send(app, "/plan")
    before = len(app.tg.sent())
    await bot.job_daily_workout(ctx(app))
    assert "⛽" not in "\n\n".join(m["text"] for m in app.tg.sent()[before:])


async def test_no_food_log_means_no_fuel_rule(app, claude, clock):
    coach = app.bot_data["coach"]
    assert coach.fuel_rule(coach.today()) is None and coach.fuel_note(coach.today()) is None
    assert "food and drink" not in coach.plan_request(MONDAY, "")

