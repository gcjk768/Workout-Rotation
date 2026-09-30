"""Structured plans: Claude returns data, the bot shows one workout card per day by body part."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

import bot
from conftest import OWNER, press, send
from fake_bot_api import check_message

MONDAY = date(2026, 9, 28)


@pytest.fixture
def env(env, monkeypatch):
    """The default setup: structured plans on, and the fake claude returns structured data."""
    monkeypatch.setenv("STRUCTURED_PLANS", "on")
    monkeypatch.setenv("FAKE_CLAUDE_STRUCTURED", "1")
    return {**env, "STRUCTURED_PLANS": "on"}


def ctx(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


def html_messages(app, since: int = 0) -> list[dict]:
    return [p for p in app.tg.sent()[since:] if p.get("parse_mode") == "HTML"]


def assert_telegram_accepts(messages: list[dict]) -> None:
    for params in messages:
        assert check_message(params) is None, (check_message(params), params["text"][:300])


def exercise(name, **extra):
    item = {"name": name, "sets": 3, "reps": "10", "load": "10 kg", "rest_s": 90, "effort": "RPE 7",
            "muscles": "chest", "cue": "Slow down", "video": f"{name} proper form"}
    item.update(extra)
    return item


def week(**overrides) -> dict:
    days = []
    for name in bot.DAY_NAMES:
        days.append({"day": name, "focus": "Rest" if name in ("Saturday", "Sunday") else "Push",
                     "rest_day": name in ("Saturday", "Sunday"), "minutes": 50, "warm_up": ["Bike"],
                     "sections": [] if name in ("Saturday", "Sunday") else
                     [{"body_part": "Chest", "exercises": [exercise(f"{name} press")]}],
                     "cool_down": ["Stretch"], "note": ""})
    data = {"split_explanation": "", "days": days, "notes": ["Sleep well."]}
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# Plan data and the text version
# ---------------------------------------------------------------------------


def test_normalize_tidies_claude_data():
    raw = week()
    raw["days"][0]["sections"][0]["exercises"].append({"name": "  Cable   fly ", "sets": "3", "reps": 12,
                                                      "rest_s": "60.0", "video": "[yt: cable fly form]",
                                                      "cue": "**Squeeze** at the top"})
    raw["days"][0]["sections"].append({"body_part": "Empty", "exercises": [{"name": ""}]})
    raw["days"].append({"day": "Monday", "focus": "Duplicate"})  # a second Monday is ignored
    raw["days"].append({"day": "Funday", "focus": "Nope"})
    data = bot.normalize_plan(json.dumps(raw))
    assert [d["day"] for d in data["days"]] == bot.DAY_NAMES
    monday = data["days"][0]
    assert monday["focus"] == "Push" and [s["body_part"] for s in monday["sections"]] == ["Chest"]
    fly = monday["sections"][0]["exercises"][1]
    assert fly["name"] == "Cable fly" and fly["sets"] == 3 and fly["reps"] == "12" and fly["rest_s"] == 60
    assert fly["video"] == "cable fly form" and fly["cue"] == "Squeeze at the top" and fly["swap"] == ""


@pytest.mark.parametrize("reply", ["📅 Monday: Push", "", "{not json", '{"days": "none"}', "[]", '{"days": []}'])
def test_normalize_rejects_what_is_not_a_plan(reply):
    assert bot.normalize_plan(reply) is None


def test_the_text_version_keeps_the_checks_working():
    data = bot.normalize_plan(week())
    data["days"][0]["sections"].append(
        {"body_part": "Shoulder rehab", "exercises": [bot.normalize_plan(week())["days"][0]["sections"][0]["exercises"][0]
                                                      | {"name": "Band external rotation"}]}
    )
    text = bot.plan_to_text(data)
    parsed = bot.parse_plan(text)
    assert sorted(parsed.days) == list(range(7)) and parsed.days[0].focus == "Push"
    assert "1. Monday press: 3 x 10, 10 kg, rest 90s, RPE 7" in text
    assert "2. Rehab: Band external rotation" in text and "Video: [yt: Monday press proper form]" in text
    assert (0, "Band external rotation") not in bot.plan_exercises(text, include_rehab=False)
    assert (0, "Monday press") in bot.plan_exercises(text)
    assert parsed.notes.startswith("📝 Notes") and "Sleep well." in parsed.notes


# ---------------------------------------------------------------------------
# Building plans
# ---------------------------------------------------------------------------


async def test_plan_is_built_as_structured_data(coach, claude):
    result = await coach.build_week(MONDAY)
    call = claude.plan_calls()[0]
    argv = call["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == bot.PLAN_SCHEMA
    assert argv[argv.index("--max-turns") + 1] == "3"  # the structured reply needs a second turn
    assert "Group each training day into sections by body part" in call["stdin"]
    assert "split_explanation" in call["stdin"] and "📅 Monday: Push" not in call["stdin"]
    assert result.data and result.meta["structured"] is True
    saved = coach.store.load_plan_data(MONDAY)
    assert saved == result.data and [s["body_part"] for s in saved["days"][4]["sections"]] == [
        "Swim", "Legs (if you do not swim)", "Run"]
    text = coach.store.load_plan(MONDAY)
    assert text.startswith("Push/pull suits") and "📅 Friday: Legs and run, or swim" in text
    assert coach.store.state()["split"]["0"] == "Push"


async def test_injury_problem_sends_the_data_back_for_one_fix(coach, claude):
    data = week()
    data["days"][1]["sections"][0]["exercises"][0]["name"] = "Barbell overhead press"
    claude.enqueue({"structured": data})
    result = await coach.build_week(MONDAY)
    first, fix = claude.plan_calls()
    assert "--json-schema" in fix["argv"] and "overhead press is a movement my injury rules leave out" in fix["stdin"]
    assert '"name": "Barbell overhead press"' in fix["stdin"]
    assert result.meta["fixed_once"] and result.data["notes"][-1] == "FIXED"


async def test_structured_failure_falls_back_to_the_text_format(coach, claude):
    claude.enqueue({"mode": "structured_fail"})
    result = await coach.build_week(MONDAY)
    first, second = claude.plan_calls()
    assert "--json-schema" in first["argv"] and "--json-schema" not in second["argv"]
    assert "📅 Monday: Push" in second["stdin"]  # the text format rules
    assert result.data is None and result.meta["structured"] is False
    assert coach.store.load_plan_data(MONDAY) is None and "📅 Monday" in coach.store.load_plan(MONDAY)


@pytest.mark.parametrize("mode", ["auth", "limit"])
async def test_no_text_fallback_for_sign_in_or_limits(coach, claude, mode):
    claude.enqueue({"mode": mode}, {"mode": mode})
    with pytest.raises(bot.ClaudeError):
        await coach.build_week(MONDAY)
    assert len(claude.plan_calls()) == 1


async def test_a_text_reply_is_still_used(coach, claude):
    claude.enqueue({"structured": False})  # a Claude Code that answers in text anyway
    result = await coach.build_week(MONDAY)
    assert result.data is None and "📅 Monday" in result.text
    await coach.build_week(MONDAY)  # a structured rebuild
    assert coach.store.load_plan_data(MONDAY)
    claude.enqueue({"structured": False})
    await coach.build_week(MONDAY)  # and back to text: the old data must not linger
    assert coach.store.load_plan_data(MONDAY) is None
    history = sorted(p.name for p in (coach.cfg.data_dir / "plans" / "history").iterdir())
    assert any(n.endswith(".plan.json") for n in history)


async def test_structured_plans_off_uses_the_text_format(env, clock, claude):
    coach = bot.Coach(bot.Config.from_env({**env, "STRUCTURED_PLANS": "off"}))
    result = await coach.build_week(MONDAY)
    assert "--json-schema" not in claude.plan_calls()[0]["argv"] and result.data is None


# ---------------------------------------------------------------------------
# Workout cards in Telegram
# ---------------------------------------------------------------------------


async def test_today_is_a_workout_card_grouped_by_body_part(app, claude, clock):
    await send(app, "/plan")
    before = len(app.tg.sent())
    await send(app, "/today")  # Wednesday 30 September: Legs and core
    messages = html_messages(app, before)
    assert_telegram_accepts(messages)
    text = "\n\n".join(m["text"] for m in messages)
    assert text.startswith("<b>📅 Wednesday 30 Sep · Legs and core</b>\n<i>Week 1 · dumbbells")
    assert "⏱ About 55 min · 🎯 Chest, Arms, Shoulder rehab · 7 sets" in text
    assert "<b>🔥 WARM UP</b>\n• 5 minutes easy bike" in text
    assert "<b>🫸 CHEST</b>\n\n<b>1 · Dumbbells move W1 D3 N1</b>\n<code>3 × 10</code> · <b>12.5 kg</b> · rest 1 min 30 s · RPE 7" in text
    assert "<b>🩹 SHOULDER REHAB</b> <i>(confirm with your physio)</i>" in text
    assert "<blockquote expandable>💡 Elbows &lt; 45 degrees, ribs down\n🦾 Left arm: Light, 5 kg, stop if it hurts" in text
    assert '▶️ <a href="https://www.youtube.com/results?search_query=Band+external+rotation+proper+form">' in text
    assert "🎯 chest &amp; front delts\n🐢 Tempo 3-1-1\n🔁 Swap: Push up on the bench</blockquote>" in text
    assert "<b>🧊 COOL DOWN</b>" in text and text.endswith("send /done when you finish.")
    for m in messages:  # search links get no preview
        preview = m["link_preview_options"]
        assert (json.loads(preview) if isinstance(preview, str) else preview)["is_disabled"] is True


async def test_day_command_shows_any_day(app, claude, clock):
    await send(app, "/plan")
    before = len(app.tg.sent())
    await send(app, "/day fri")
    messages = html_messages(app, before)
    assert_telegram_accepts(messages)
    text = "\n\n".join(m["text"] for m in messages)
    assert text.startswith("<b>📅 Friday 2 Oct · Legs and run, or swim</b>")
    assert "<b>🏊 SWIM</b>" in text and "<b>🦵 LEGS (IF YOU DO NOT SWIM)</b>" in text and "<b>🏃 RUN</b>" in text
    assert "<b>A1 · Dumbbells move W1 D5 N1</b>" in text and "<b>A2 · Dumbbells move W1 D5 N2</b>" in text
    assert "<code>1 × 3 km</code> · <b>6:00 per km</b>" in text
    assert (await send(app, "/day someday"))[-1].startswith("Which day?")
    assert (await send(app, "/day"))[-1].startswith("Which day?")


async def test_day_command_looks_ahead_on_weekends(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 3, 12, 0)  # Saturday
    await send(app, "/nextweek")
    replies = await send(app, "/day mon")
    assert replies[0].startswith("<b>📅 Monday 5 Oct")
    replies = await send(app, "/day sun")
    assert replies[0].startswith("<b>📅 Sunday 4 Oct")


async def test_week_is_a_short_summary_by_body_part(app, claude, clock):
    await send(app, "/plan")
    await send(app, "/done")
    before = len(app.tg.sent())
    await send(app, "/week")
    messages = html_messages(app, before)
    assert_telegram_accepts(messages)
    text = "\n\n".join(m["text"] for m in messages)
    assert text.startswith("<b>🗓 Week 1 · dumbbells")
    assert "<b>📅 Wednesday · Legs and core</b> ✅ · ⏱ 55 min" in text
    assert "🫸 <b>Chest</b>: Dumbbells move W1 D3 N1 3×10 @ 12.5 kg" in text
    assert "🏃 <b>Run</b>: Easy run 1×3 km" in text  # no @ for a pace without kg
    assert "🧘 <b>Mobility</b>: Cat cow 2×10" in text
    assert "<blockquote expandable>🧠 Push/pull suits" in text
    assert text.endswith("Send /today for today's full workout, or /day fri for any day.")


async def test_plan_reply_is_the_summary_with_warnings(app, claude, clock):
    data = week()
    data["days"][1]["sections"][0]["exercises"][0]["name"] = "Dips"
    claude.enqueue({"structured": data}, {"structured": data})  # the fix keeps the dips
    before = len(app.tg.sent())
    await send(app, "/plan")
    text = "\n\n".join(m["text"] for m in html_messages(app, before))
    assert "<b>📅 Tuesday · Push</b>" in text and "⚠️ Please check:" in text and "dip is a movement" in text
    assert text.endswith("or /day fri for any day.")


async def test_morning_message_is_the_card_with_buttons(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 1, 7, 0)  # Thursday
    before = len(app.tg.sent())
    await bot.job_daily_workout(ctx(app))
    messages = app.tg.sent()[before:]
    assert_telegram_accepts(messages)
    assert messages[0]["text"].startswith("<b>☀️ Good morning. Today's workout</b>\n\n<b>📅 Thursday 1 Oct")
    markup = json.loads(messages[-1]["reply_markup"]) if isinstance(messages[-1]["reply_markup"], str) else messages[-1]["reply_markup"]
    data = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert data == ["alt:2026-10-01:light", "alt:2026-10-01:short"]
    clock.set(2026, 10, 4, 7, 0)  # Sunday, a rest day
    before = len(app.tg.sent())
    await bot.job_daily_workout(ctx(app))
    rest = app.tg.sent()[before:]
    assert rest[0]["text"].startswith("<b>☀️ Good morning. Rest day today.</b>\n\n<b>📅 Sunday 4 Oct · Rest</b>")
    assert "reply_markup" not in rest[-1] and "/done when you finish" not in rest[-1]["text"]


async def test_card_notes_status_and_days_off(coach, claude, clock):
    await coach.build_week(MONDAY)
    coach.store.set_session(date(2026, 9, 30), "done", coach.now(), "test")
    blocks = coach.day_view(date(2026, 9, 30), "Heading & more", ["⚠️ Your Garmin data says recovery looks low"])
    assert blocks[0] == "<b>Heading &amp; more</b>\n✅ Already marked done today.\n⚠️ Your Garmin data says recovery looks low"
    coach.store.set_session(date(2026, 10, 2), "skipped", coach.now(), "test")
    assert coach.day_view(date(2026, 10, 2))[0] == "⏭ Marked as skipped on Friday."
    assert coach.day_view(date(2026, 10, 5)) is None  # next week has no plan yet


async def test_html_rejected_falls_back_to_plain_text_with_links(app, claude, clock):
    await send(app, "/plan")
    app.tg.reject_html = True
    before = len(app.tg.sent())
    await send(app, "/today")
    plain = [p for p in app.tg.sent()[before:] if "parse_mode" not in p]
    text = "\n".join(p["text"] for p in plain)
    assert "<b>" not in text and "📅 Wednesday 30 Sep · Legs and core" in text
    assert "Elbows < 45 degrees" in text
    assert "Form video: Band external rotation proper form: https://www.youtube.com/results?search_query=" in text


def test_long_days_are_split_between_messages():
    data = bot.normalize_plan(week())
    long_cue = "Keep the ribs down and the elbows tucked & move slowly " * 5
    data["days"][0]["sections"] = [
        {"body_part": part, "exercises": [
            {**data["days"][1]["sections"][0]["exercises"][0], "name": f"{part} move {k}", "cue": long_cue[:300]}
            for k in range(6)]}
        for part in ("Chest", "Back", "Legs", "Core")
    ]
    blocks = bot.day_card_blocks(data["days"][0], "Week 1", MONDAY)
    messages = bot.pack_blocks(blocks)
    assert len(messages) > 1
    for message in messages:
        assert check_message({"text": message, "parse_mode": "HTML"}) is None
        assert len(bot.ENTITY_RE.findall(message)) <= 90
    starts = [m.split("\n", 1)[0] for m in messages[1:]]
    assert all(s.startswith("<b>") for s in starts)  # a message never starts inside a card
    assert sum(m.count("<b>🔙 BACK</b>\n\n<b>7 · Back move 0</b>") for m in messages) == 1


def test_body_part_emojis():
    assert bot.body_part_emoji("Shoulder rehab") == "🩹"
    assert bot.body_part_emoji("Legs (if you do not swim)") == "🦵"
    assert bot.body_part_emoji("Back and biceps") == "🔙"
    assert bot.body_part_emoji("Court skills") == "🏀"
    assert bot.body_part_emoji("Something else") == "🏋️"


# ---------------------------------------------------------------------------
# A skipped session
# ---------------------------------------------------------------------------


async def test_skip_keeps_past_days_and_rewrites_the_rest(app, claude, clock):
    await send(app, "/plan")
    coach = app.bot_data["coach"]
    before = coach.store.load_plan_data(MONDAY)
    await press(app, "chk:2026-09-30:skip")
    request = claude.plan_calls()[-1]["stdin"]
    assert "I skipped today's session (Wednesday: Legs and core)" in request
    assert "add (skipped) at the end of the Wednesday focus" in request and '"day": "Monday"' in request
    after = coach.store.load_plan_data(MONDAY)
    assert after["days"][0] == before["days"][0] and after["days"][1] == before["days"][1]
    assert after["days"][2]["focus"] == "Legs and core (skipped)" and after["days"][2]["note"] == ""
    assert after["days"][3]["note"] == "Adjusted after the skip." and after["notes"][-1] == "ADJUSTED"
    assert "📅 Wednesday: Legs and core (skipped)" in coach.store.load_plan(MONDAY)
    last = app.tg.texts()[-1]
    assert "Here is the rest of your week, adjusted:" in last and "📅 Thu: Upper body and rehab" in last
    assert "🫸 Chest: Dumbbells move W1 D4 N1" in last and "📅 Wed" not in last


async def test_sunday_overview_lists_body_parts(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(ctx(app))
    text = app.tg.texts()[-1]
    assert "🗓 Next week is ready. Week 2" in text
    assert "📅 Fri: Legs and run, or swim" in text and "🏊 Swim: Easy freestyle" in text
    assert "🩹 Shoulder rehab: Band external rotation" in text
    assert OWNER == app.bot_data["coach"].cfg.owner_id


def test_rest_day_flag_shows_in_the_focus():
    raw = week()
    raw["days"][5]["focus"] = "Active recovery"
    data = bot.normalize_plan(raw)
    assert data["days"][5]["focus"] == "Active recovery (rest day)"
    assert bot.parse_plan(bot.plan_to_text(data)).days[5].is_rest
    raw["days"][6]["focus"] = "Basketball or rest"
    assert bot.normalize_plan(raw)["days"][6]["focus"] == "Basketball or rest"


async def test_a_direct_video_link_gets_a_preview(app, claude, clock):
    data = week()
    link = "https://www.youtube.com/watch?v=abcdefghijk&t=30"
    data["days"][2]["sections"][0]["exercises"][0]["video"] = link
    claude.enqueue({"structured": data})
    await send(app, "/plan")
    before = len(app.tg.sent())
    await send(app, "/today")
    message = app.tg.sent()[before]
    assert '<a href="https://www.youtube.com/watch?v=abcdefghijk&amp;t=30">Form video</a>' in message["text"]
    preview = message["link_preview_options"]
    preview = json.loads(preview) if isinstance(preview, str) else preview
    assert preview["url"] == link and not preview.get("is_disabled")
