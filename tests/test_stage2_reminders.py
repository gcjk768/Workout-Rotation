"""Stage 2: logging, /done and shoulder ratings, reminders, check ins and skipped sessions."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import bot
from conftest import OWNER, press, send


def job_context(app):
    return SimpleNamespace(bot=app.bot, application=app, job=None)


def keyboard_data(params: dict) -> list[str]:
    markup = params.get("reply_markup")
    if not markup:
        return []
    if isinstance(markup, str):
        import json

        markup = json.loads(markup)
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


async def start_programme(app, claude, clock):
    """Build this week's plan on Wednesday 30 Sep 2026 (week 1)."""
    clock.set(2026, 9, 30, 12, 0)
    await send(app, "/plan")
    app.tg.clear()


# ---------------------------------------------------------------------------
# /log, /done, /shoulder
# ---------------------------------------------------------------------------


async def test_log_saves_free_text_with_the_date(app, claude, clock):
    texts = await send(app, "/log rows 22kg 3x10, floor press 14kg 3x8 felt easy")
    assert texts == ["Logged for Wed 30 Sep. Send /done when you finish the session."]
    logs = app.bot_data["coach"].store.logs()
    assert logs == [{"date": "2026-09-30", "time": "12:00", "text": "rows 22kg 3x10, floor press 14kg 3x8 felt easy"}]
    listing = (await send(app, "/log"))[0]
    assert "Wed 30 Sep: rows 22kg 3x10, floor press 14kg 3x8 felt easy" in listing
    await send(app, "/ask how heavy next week?")
    system = claude.last()["system"]
    assert "My workout logs from the last 14 days:\nWed 30 Sep 12:00: rows 22kg 3x10" in system


async def test_log_escapes_html(app):
    await send(app, "/log bench <b>test</b> & more")
    listing = (await send(app, "/log"))[0]
    assert "&lt;b&gt;test&lt;/b&gt; &amp; more" in listing


async def test_done_asks_for_a_rating_with_buttons(app, claude, clock):
    texts = await send(app, "/done")
    assert texts[0].startswith("✅ Nice work. Today's session is marked done.")
    assert "0 (no pain) to 10 (worst pain)" in texts[0]
    buttons = keyboard_data(app.tg.sent()[-1])
    assert buttons == [f"rate:2026-09-30:{n}" for n in range(11)]
    coach = app.bot_data["coach"]
    assert coach.store.sessions()["2026-09-30"]["status"] == "done"

    calls = await press(app, "rate:2026-09-30:3")
    names = [name for name, _ in calls]
    assert "answerCallbackQuery" in names and "editMessageText" in names
    edit = [p for name, p in calls if name == "editMessageText"][0]
    assert edit["text"] == "Left shoulder 3/10 saved for Wed 30 Sep."
    follow = [p for name, p in calls if name == "sendMessage"][0]["text"]
    assert follow.startswith("Trend: Latest 3/10 on Wed 30 Sep.")
    assert coach.store.ratings() == [{"date": "2026-09-30", "time": "12:00", "rating": 3, "note": "after session"}]
    today = (await send(app, "/today"))[0]
    assert "Send /plan" in today  # no plan yet, but the status is kept


async def test_done_with_text_also_logs(app):
    await send(app, "/done 45 minutes, felt strong")
    assert app.bot_data["coach"].store.logs()[0]["text"] == "45 minutes, felt strong"


async def test_high_rating_gets_physio_advice(app):
    calls = await press(app, "rate:2026-09-30:8")
    follow = [p for name, p in calls if name == "sendMessage"][0]["text"]
    assert "check with your physio" in follow
    assert "stop training it and see a doctor or physio" in follow


async def test_shoulder_log_and_manual_rating(app, clock):
    assert "No shoulder ratings yet" in (await send(app, "/shoulder"))[0]
    texts = await send(app, "/shoulder 4 after basketball")
    assert texts[0].startswith("Left shoulder 4/10 saved.")
    clock.set(2026, 10, 1, 21, 5)
    await send(app, "/shoulder 2/10")
    log_text = (await send(app, "/shoulder"))[0]
    assert "<b>Left shoulder ratings</b> (0 = no pain, 10 = worst pain)" in log_text
    assert "Wed 30 Sep 2026, 12:00: 4/10 (after basketball)" in log_text
    assert "Thu 1 Oct 2026, 21:05: 2/10 (manual)" in log_text
    assert "Trend: Latest 2/10 on Thu 1 Oct." in log_text
    bad = await send(app, "/shoulder eleven")
    assert "0 to 10" in bad[0]
    assert "0 to 10" in (await send(app, "/shoulder 11"))[0]


def rating(day: str, value: int) -> dict:
    return {"date": day, "time": "21:00", "rating": value, "note": ""}


def test_shoulder_trend_wording():
    today = date(2026, 10, 30)
    assert "No shoulder ratings yet" in bot.shoulder_trend([], today)
    improving = [rating("2026-10-05", 5), rating("2026-10-08", 5), rating("2026-10-20", 2), rating("2026-10-28", 2)]
    text = bot.shoulder_trend(improving, today)
    assert "Last 2 weeks average 2.0 from 2 ratings, down from 5.0, so it is improving." in text
    rising = [rating("2026-10-05", 1), rating("2026-10-20", 2), rating("2026-10-25", 3), rating("2026-10-28", 4)]
    text = bot.shoulder_trend(rising, today)
    assert "up from 1.0, so the pain is rising." in text and "went up each time" in text
    assert bot.shoulder_rising(rising, today)
    same = [rating("2026-10-10", 3), rating("2026-10-25", 3)]
    assert "about the same" in bot.shoulder_trend(same, today)


async def test_profile_shows_shoulder_trend(app):
    await press(app, "rate:2026-09-30:3")
    profile = (await send(app, "/profile"))[0]
    assert "<b>Left shoulder</b>\nLatest 3/10 on Wed 30 Sep." in profile


async def test_system_prompt_has_sessions_ratings_and_logs(app, claude, clock):
    await send(app, "/done")
    await press(app, "rate:2026-09-30:3")
    await send(app, "/log goblet squat 16kg 3x10")
    await send(app, "/ask anything")
    system = claude.last()["system"]
    assert "Sessions this week so far: Wednesday done." in system
    assert "My left shoulder ratings from the last 14 days (0 = no pain, 10 = worst pain):\nWed 30 Sep 12:00: 3/10 (after session)" in system
    assert "Shoulder trend: Latest 3/10" in system
    assert "goblet squat 16kg 3x10" in system


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


def next_fire(app, name: str, after: datetime) -> datetime:
    jobs = [j for j in app.job_queue.jobs() if j.name == name]
    assert jobs, name
    return jobs[0].job.trigger.get_next_fire_time(None, after)


async def test_reminder_times_in_singapore(app, cfg):
    tz = cfg.tz
    wed_noon = datetime(2026, 9, 30, 12, 0, tzinfo=tz)
    thu_evening = datetime(2026, 10, 1, 18, 0, tzinfo=tz)
    assert next_fire(app, "session_reminder", wed_noon) == datetime(2026, 9, 30, 17, 30, tzinfo=tz)
    assert next_fire(app, "session_reminder", thu_evening) == datetime(2026, 10, 5, 17, 30, tzinfo=tz)
    assert next_fire(app, "session_reminder_fri", wed_noon) == datetime(2026, 10, 2, 17, 0, tzinfo=tz)
    assert next_fire(app, "evening_check", wed_noon) == datetime(2026, 9, 30, 21, 0, tzinfo=tz)
    fri_late = datetime(2026, 10, 2, 22, 0, tzinfo=tz)
    assert next_fire(app, "evening_check", fri_late) == datetime(2026, 10, 5, 21, 0, tzinfo=tz)
    assert next_fire(app, "checkin", wed_noon) == datetime(2026, 10, 4, 18, 0, tzinfo=tz)
    assert next_fire(app, "weekly_plan", wed_noon) == datetime(2026, 10, 4, 20, 0, tzinfo=tz)
    assert next_fire(app, "token_check", wed_noon) == datetime(2026, 10, 1, 10, 0, tzinfo=tz)


async def test_custom_reminder_times(env, clock):
    cfg = bot.Config.from_env({**env, "REMINDER_TIME_MON_THU": "16:45", "PLAN_TIME": "19:30", "TRAINING_DAYS": "Mon-Thu"})
    application = bot.build_application(cfg, request=__import__("conftest").FakeTelegram())
    tz = cfg.tz
    after = datetime(2026, 9, 30, 12, 0, tzinfo=tz)
    assert next_fire(application, "session_reminder", after) == datetime(2026, 9, 30, 16, 45, tzinfo=tz)
    assert next_fire(application, "weekly_plan", after) == datetime(2026, 10, 4, 19, 30, tzinfo=tz)
    fri = datetime(2026, 10, 1, 22, 0, tzinfo=tz)
    assert next_fire(application, "evening_check", fri) == datetime(2026, 10, 5, 21, 0, tzinfo=tz)


async def test_status_lists_next_reminders(app):
    await app.job_queue.start()
    try:
        status = (await send(app, "/status"))[0]
    finally:
        await app.job_queue.stop()
    assert "<b>Next reminders</b>" in status
    for label in ("Session reminder", "Did you train? check", "Sunday check in", "Next week's plan", "Token expiry check"):
        assert label in status


# ---------------------------------------------------------------------------
# Session reminder and the 9pm check
# ---------------------------------------------------------------------------


async def test_session_reminder_sends_today(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 9, 30, 17, 30)
    await bot.job_session_reminder(job_context(app))
    text = app.tg.texts()[-1]
    assert text.startswith("🏋️ Today's session\n\n📅 Wednesday: Legs and core")
    assert "📅 Thursday" not in text
    clock.set(2026, 10, 2, 17, 0)
    await bot.job_session_reminder(job_context(app))
    assert app.tg.texts()[-1].startswith("🏋️ Today's session\n\n📅 Friday: Legs and run, or swim")


async def test_session_reminder_skipped_when_done(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 9, 30, 17, 30)
    await send(app, "/done")
    app.tg.clear()
    await bot.job_session_reminder(job_context(app))
    assert app.tg.texts() == []


async def test_session_reminder_builds_a_missing_plan(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 5, 17, 30)  # Monday, the Sunday build did not happen
    await bot.job_session_reminder(job_context(app))
    assert app.bot_data["coach"].store.load_plan(date(2026, 10, 5))
    assert app.tg.texts()[-1].startswith("🏋️ Today's session\n\n📅 Monday: Push")


async def test_session_reminder_before_any_plan(app, claude, clock):
    clock.set(2026, 9, 30, 17, 30)
    await bot.job_session_reminder(job_context(app))
    assert app.tg.texts() == ["No training plan yet. Send /plan to build your first week."]
    assert claude.all() == []


async def test_evening_check_buttons_and_done(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 9, 30, 21, 0)
    await bot.job_evening_check(job_context(app))
    sent = app.tg.sent()[-1]
    assert sent["chat_id"] == OWNER
    assert sent["text"] == "Did you train today (Legs and core)?"
    assert keyboard_data(sent) == ["chk:2026-09-30:done", "chk:2026-09-30:skip"]
    calls = await press(app, "chk:2026-09-30:done")
    edits = [p["text"] for n, p in calls if n == "editMessageText"]
    assert edits == ["✅ Wed 30 Sep marked as done. Nice work."]
    rating_msg = [p for n, p in calls if n == "sendMessage"][0]
    assert keyboard_data(rating_msg)[0] == "rate:2026-09-30:0"
    assert app.bot_data["coach"].store.sessions()["2026-09-30"]["status"] == "done"
    app.tg.clear()
    await bot.job_evening_check(job_context(app))
    assert app.tg.texts() == []  # already answered


async def test_evening_check_skips_rest_days_and_done_days(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 3, 21, 0)  # Saturday is a rest day in the fake plan
    await bot.job_evening_check(job_context(app))
    assert app.tg.texts() == []
    clock.set(2026, 10, 1, 19, 0)
    await send(app, "/done")
    app.tg.clear()
    clock.set(2026, 10, 1, 21, 0)
    await bot.job_evening_check(job_context(app))
    assert app.tg.texts() == []


async def test_skipped_session_rewrites_the_rest_of_the_week(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 9, 30, 21, 0)
    await bot.job_evening_check(job_context(app))
    calls = await press(app, "chk:2026-09-30:skip")
    texts = [p["text"] for n, p in calls if n == "sendMessage"]
    assert texts[0].startswith("No problem. I am adjusting the rest of your week")
    final = texts[-1]
    assert final.startswith("Here is the rest of your week, adjusted:")
    assert "📅 Thu:" in final and "📅 Mon:" not in final and "📅 Wed:" not in final
    coach = app.bot_data["coach"]
    assert coach.store.sessions()["2026-09-30"]["status"] == "skipped"
    request = claude.plan_calls()[-1]["stdin"]
    assert "I skipped today's session (Wednesday: Legs and core)." in request
    assert "Adjust the rest of this week, Thursday to Sunday" in request
    assert "Keep Monday to Wednesday exactly as written" in request
    assert "📅 Monday: Push" in request  # the current plan is included
    plan = coach.store.load_plan(date(2026, 9, 28))
    assert "ADJUSTED" in plan and "📅 Wednesday: Legs and core (skipped)" in plan
    assert coach.store.load_plan_meta(date(2026, 9, 28))["kind"] == "adjusted"
    assert list((coach.store.root / "plans" / "history").glob("2026-09-28_*.md"))
    await send(app, "/ask what now?")
    assert "Sessions this week so far: Wednesday skipped." in claude.last()["system"]


async def test_skip_adjustment_is_checked_for_injury_moves(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 9, 30, 21, 0)
    bad = "\n".join(
        [f"📅 {d}: Push" + ("\n1. Bench dips: 3 x 10" if d == "Thursday" else "") for d in bot.DAY_NAMES]
    )
    claude.enqueue({"result": bad})
    await press(app, "chk:2026-09-30:skip")
    calls = claude.plan_calls()
    assert "Your plan below has problems" in calls[-1]["stdin"]
    assert "Bench dips" in calls[-1]["stdin"]


async def test_skip_error_is_reported(app, claude, clock):
    await start_programme(app, claude, clock)
    claude.enqueue({"mode": "limit"})
    calls = await press(app, "chk:2026-09-30:skip")
    texts = [p["text"] for n, p in calls if n == "sendMessage"]
    assert "I saved the skip but could not adjust the plan" in texts[-1]
    assert app.bot_data["coach"].store.sessions()["2026-09-30"]["status"] == "skipped"


async def test_skip_without_a_plan_does_not_call_claude(app, claude, clock):
    await press(app, "chk:2026-09-30:skip")
    assert claude.all() == []


# ---------------------------------------------------------------------------
# Sunday check in and next week's plan
# ---------------------------------------------------------------------------


async def test_checkin_next_message_is_saved_and_used(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 18, 0)
    await bot.job_checkin(job_context(app))
    question = app.tg.texts()[-1]
    assert question.startswith("🗓 Weekly check in")
    assert "before 20:00" in question
    calls_before = len(claude.all())
    clock.set(2026, 10, 4, 18, 40)
    texts = await send(app, "Energy good, legs sore, shoulder 3")
    assert texts == ["Thanks, I saved your check in. Next week's plan gets built at 20:00."]
    assert len(claude.all()) == calls_before  # not sent to Claude
    coach = app.bot_data["coach"]
    assert coach.store.load_checkin(date(2026, 10, 4)) == "Energy good, legs sore, shoulder 3"
    await send(app, "What should I eat tonight?")
    assert claude.last()["stdin"].endswith("What should I eat tonight?")

    clock.set(2026, 10, 4, 20, 0)
    app.tg.clear()
    await bot.job_weekly_plan(job_context(app))
    request = claude.plan_calls()[-1]["stdin"]
    assert "My Sunday check in answer: Energy good, legs sore, shoulder 3" in request
    assert "Week number: 2" in request and "Main equipment this week: cables" in request
    overview = app.tg.texts()[-1]
    assert overview.startswith("🗓 Next week is ready. Week 2 · cables")
    assert "📅 Mon: Push\nCables move W2 D1 N1, Cables move W2 D1 N2, Band external rotation" in overview
    assert "Left shoulder: " in overview
    assert "Send /week to see the full plan." in overview
    assert "did not get a check in" not in overview
    assert coach.store.plan_path(date(2026, 10, 5)).exists()


async def test_reply_to_checkin_is_saved_even_later(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 18, 0)
    await bot.job_checkin(job_context(app))
    checkin_id = app.bot_data["coach"].store.state()["checkin"]["message_id"]
    clock.set(2026, 10, 4, 18, 5)
    await send(app, "/ask quick question first")  # commands are not captured
    clock.set(2026, 10, 4, 21, 0)
    texts = await send(app, "Tired this week, shoulder 5", reply_to=checkin_id)
    assert "already built" in texts[0]
    assert app.bot_data["coach"].store.load_checkin(date(2026, 10, 4)) == "Tired this week, shoulder 5"
    texts = await send(app, "Not a check in")  # after 20:00 a plain message goes to Claude
    assert claude.last()["stdin"].endswith("Not a check in")


async def test_weekly_plan_without_checkin(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(job_context(app))
    assert "I did not send a Sunday check in answer this week." in claude.plan_calls()[-1]["stdin"]
    assert "I did not get a check in answer this week, so I built it without one." in app.tg.texts()[-1]


async def test_weekly_plan_keeps_a_plan_built_after_the_answer(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 18, 0)
    await bot.job_checkin(job_context(app))
    clock.set(2026, 10, 4, 18, 30)
    await send(app, "All good")
    clock.set(2026, 10, 4, 19, 0)
    await send(app, "/nextweek")
    plan_calls = len(claude.plan_calls())
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(job_context(app))
    assert len(claude.plan_calls()) == plan_calls
    assert app.tg.texts()[-1].startswith("🗓 Next week's plan was already built.")


async def test_weekly_plan_failure_is_reported(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 20, 0)
    claude.enqueue({"mode": "auth"})
    await bot.job_weekly_plan(job_context(app))
    assert "I could not build next week's plan" in app.tg.texts()[-1]
    assert "claude setup-token" in app.tg.texts()[-1]


async def test_week_on_sunday_evening_shows_next_week(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 20, 0)
    await bot.job_weekly_plan(job_context(app))
    week = (await send(app, "/week"))[0]
    assert "Week 2 · cables" in week


# ---------------------------------------------------------------------------
# Token reminder and catch up
# ---------------------------------------------------------------------------


async def test_token_reminder_schedule(app, clock):
    coach = app.bot_data["coach"]  # token created 2026-01-15, expires 2027-01-15
    ctx = job_context(app)
    clock.set(2026, 12, 10, 10, 0)
    await bot.job_token_check(ctx)
    assert app.tg.texts() == []
    clock.set(2026, 12, 16, 10, 0)  # 30 days left
    await bot.job_token_check(ctx)
    assert app.tg.texts()[-1].startswith("🔑 Your Claude Code token expires in 30 days, on Fri 15 Jan.")
    assert "claude setup-token" in app.tg.texts()[-1]
    count = len(app.tg.texts())
    clock.set(2026, 12, 17, 10, 0)
    await bot.job_token_check(ctx)
    assert len(app.tg.texts()) == count  # weekly until the last week
    clock.set(2026, 12, 23, 10, 0)
    await bot.job_token_check(ctx)
    assert "23 days" in app.tg.texts()[-1]
    count = len(app.tg.texts())
    for day in (9, 10, 11):  # daily in the final week
        clock.set(2027, 1, day, 10, 0)
        await bot.job_token_check(ctx)
    assert len(app.tg.texts()) == count + 3
    assert "expires in 4 days" in app.tg.texts()[-1]
    clock.set(2027, 1, 20, 10, 0)
    await bot.job_token_check(ctx)
    assert "probably expired on Fri 15 Jan" in app.tg.texts()[-1]
    assert coach.store.state()["token_reminder"]["created"] == "2026-01-15"


async def test_token_reminder_off_without_date(env, clock):
    cfg = bot.Config.from_env({**env, "CLAUDE_TOKEN_CREATED": ""})
    assert bot.Coach(cfg).token_reminder() is None


async def test_catch_up_builds_missed_week(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 6, 9, 0)  # Tuesday, no plan for this week
    await bot.job_catch_up(job_context(app))
    texts = app.tg.texts()
    assert texts[0] == "I was offline when this week's plan was due, so I am building it now."
    assert texts[-1].startswith("🗓 This week's plan. Week 2 · cables")


async def test_catch_up_on_sunday_night_builds_next_week(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 21, 30)
    await bot.job_catch_up(job_context(app))
    assert app.tg.texts()[0] == "I was offline at plan time, so I am building next week's plan now."
    assert app.bot_data["coach"].store.load_plan(date(2026, 10, 5))


async def test_catch_up_sends_a_missed_checkin(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 18, 45)
    await bot.job_catch_up(job_context(app))
    assert app.tg.texts()[0].startswith("🗓 Weekly check in")


async def test_catch_up_does_nothing_before_the_first_plan(app, claude, clock):
    clock.set(2026, 10, 6, 9, 0)
    await bot.job_catch_up(job_context(app))
    assert app.tg.texts() == [] and claude.all() == []


async def test_catch_up_does_nothing_when_up_to_date(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 1, 9, 0)
    await bot.job_catch_up(job_context(app))
    assert app.tg.texts() == []


async def test_checkin_only_captures_the_owner(app, claude, clock):
    await start_programme(app, claude, clock)
    clock.set(2026, 10, 4, 18, 0)
    await bot.job_checkin(job_context(app))
    clock.set(2026, 10, 4, 18, 30)
    await send(app, "Hi coach, second account here", user_id=222222)
    assert claude.last()["stdin"].endswith("Hi coach, second account here")
    assert app.bot_data["coach"].store.load_checkin(date(2026, 10, 4)) is None
    texts = await send(app, "Energy fine")
    assert texts[0].startswith("Thanks, I saved your check in")
