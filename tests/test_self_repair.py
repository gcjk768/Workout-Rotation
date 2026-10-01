"""Self repair: damaged files, a stuck bot, and unexpected errors diagnosed by claude -p."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden, RetryAfter

import bot
from conftest import BOT_TOKEN, OWNER, FakeTelegram, message_update
from fake_bot_api import check_message


def failing() -> Exception:
    """A real error raised inside bot.py, with its traceback."""
    try:
        bot.fmt_day(None)
    except Exception as exc:  # noqa: BLE001
        return exc
    raise AssertionError("fmt_day(None) did not fail")


def ctx(app, error, job=None):
    return SimpleNamespace(bot=app.bot, application=app, error=error, job=job)


def repair_calls(claude):
    return [c for c in claude.all() if c["stdin"].startswith("SELF REPAIR")]


def remedy(**fields):
    base = {"diagnosis": "Something broke.", "remedy": "none", "file": "", "message": "", "code_fix": ""}
    return {"structured": {**base, **fields}}


# ---------------------------------------------------------------------------
# Data files
# ---------------------------------------------------------------------------


def test_damaged_file_is_restored_from_its_backup(coach):
    store = coach.store
    store.write_json("state.json", {"split": "old"})
    store.write_json("state.json", {"split": "new"})
    assert json.loads((store.root / "state.json.bak").read_text()) == {"split": "old"}
    (store.root / "state.json").write_text('{"split": "ne')  # a power cut mid write
    assert store.state() == {"split": "old"}
    assert json.loads((store.root / "state.json").read_text()) == {"split": "old"}
    broken = list((store.root / "broken").iterdir())
    assert len(broken) == 1 and broken[0].read_text() == '{"split": "ne'
    assert store.repairs == [{"file": "state.json", "restored": True}]


def test_damaged_file_without_backup_starts_empty(coach):
    store = coach.store
    (store.root / "away.json").write_text("not json")
    assert store.read_json("away.json", []) == []
    assert store.repairs == [{"file": "away.json", "restored": False}]
    assert not (store.root / "away.json").exists()


def test_a_damaged_file_never_becomes_the_backup(coach):
    store = coach.store
    store.write_json("memory.json", {"a": 1})
    store.write_json("memory.json", {"a": 2})
    (store.root / "memory.json").write_text("{broken")
    store.write_json("memory.json", {"a": 3})  # written over the damaged file without reading it
    assert json.loads((store.root / "memory.json.bak").read_text()) == {"a": 1}


# ---------------------------------------------------------------------------
# Unexpected errors: diagnosis and remedy
# ---------------------------------------------------------------------------


async def test_unexpected_error_is_diagnosed_and_repaired(app, claude, clock):
    store = app.bot_data["coach"].store
    store.write_json("away.json", [{"from": "2026-10-08"}])
    store.write_json("away.json", [{"from": "2026-10-09"}])
    (store.root / "away.json").write_text("[{broken")
    claude.enqueue(remedy(diagnosis="away.json is damaged.", remedy="repair_file", file="data/away.json",
                          message="Your travel days were restored.", code_fix="Guard fmt_day against None."))
    update = message_update(app, "/today")
    await bot.on_error(update, ctx(app, failing()))
    call = repair_calls(claude)[-1]
    argv = call["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == bot.REPAIR_SCHEMA
    assert argv[argv.index("--tools") + 1] == "" and "WebSearch" not in argv
    stdin = call["stdin"]
    assert "The bot hit an error in the /today command" in stdin and "TypeError" in stdin
    assert "Source around the failing lines:\nfmt_day(), bot.py lines" in stdin and '>     return f"{d:%a}' in stdin
    assert "- repair_file:" in stdin and "away.json (" in stdin
    texts = app.tg.texts()
    assert texts[0].startswith("⚠️ <b>PROBLEM</b>\n\nSorry, something went wrong on my side. I am looking into it")
    report = texts[-1]
    assert report.startswith("🛠 <b>SELF REPAIR</b> · the /today command\n\nSomething went wrong in the /today command (TypeError).")
    assert "🔍 <b>What Claude found</b> · away.json is damaged." in report
    assert "🔧 <b>What I did</b> · I restored away.json from its last good copy." in report
    assert "Your travel days were restored." in report
    assert ("━━━━━━━━━━━━━━━━\n🧑‍💻 <b>Suggested code change</b> · <i>for the next update, tap to open</i>\n"
            "<blockquote expandable>Guard fmt_day against None.</blockquote>") in report
    assert check_message({"text": report, "parse_mode": "HTML"}) is None
    assert store.read_json("away.json", None) == [{"from": "2026-10-08"}]
    health = store.read_json("health.json", {})
    assert health["last_problem"]["remedy"] == "repair_file" and health["last_problem"]["where"] == "the /today command"


async def test_the_same_error_is_diagnosed_once(app, claude, clock):
    await bot.on_error(None, ctx(app, failing()))
    await bot.on_error(None, ctx(app, failing()))
    assert len(repair_calls(claude)) == 1
    rows = app.bot_data["coach"].store._read_jsonl("errors.jsonl")
    assert len(rows) == 2 and rows[0]["signature"] == rows[1]["signature"] == "TypeError@fmt_day:" + rows[0]["signature"].split(":")[-1]
    clock.set(2026, 10, 1, 13, 0)  # a day later it is looked at again
    await bot.on_error(None, ctx(app, failing()))
    assert len(repair_calls(claude)) == 2


async def test_at_most_six_diagnoses_a_day(app, claude, clock):
    repair = app.bot_data["repair"]
    for n in range(8):
        error = failing()
        repair.signature = lambda e, n=n: f"sig{n}"  # eight different problems
        await repair.handle(error, "the bot")
    assert len(repair_calls(claude)) == bot.MAX_DIAGNOSES_PER_DAY


async def test_secrets_never_reach_claude(app, claude, clock, cfg):
    error = failing()
    error.args = (f"token {cfg.telegram_token} and sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE",)
    await bot.on_error(None, ctx(app, error))
    stdin = repair_calls(claude)[-1]["stdin"]
    assert BOT_TOKEN not in stdin and "FAKEFAKEFAKE" not in stdin
    assert BOT_TOKEN not in json.dumps(app.bot_data["coach"].store._read_jsonl("errors.jsonl"))


async def test_owner_is_told_when_claude_cannot_look(app, claude, clock):
    claude.enqueue({"mode": "auth"}, {"mode": "auth"})
    await bot.on_error(None, ctx(app, failing()))
    text = app.tg.texts()[-1]
    assert "Something went wrong in the bot (TypeError)" in text and "I could not ask Claude to look at it" in text
    assert "data/errors.jsonl" in text


async def test_retry_runs_the_reminder_again(app, claude, clock):
    job = SimpleNamespace(name="daily_workout", callback=bot.job_daily_workout, data=None, chat_id=None, user_id=None)
    claude.enqueue(remedy(remedy="retry"))
    await bot.on_error(None, ctx(app, failing(), job))
    assert "in the Daily workout reminder" in app.tg.texts()[-1] and "run it again in 2 minutes" in app.tg.texts()[-1]
    assert any(j.name == "daily_workout_retry" for j in app.job_queue.jobs())
    claude.enqueue(remedy(remedy="retry"))
    app.bot_data["repair"].signature = lambda e: "other"
    await bot.on_error(message_update(app, "/week"), ctx(app, failing()))
    assert "It was not a reminder" in app.tg.texts()[-1]


async def test_repair_file_stays_inside_the_data_folder(app, claude, clock, tmp_path):
    outside = tmp_path / "bot.json"
    outside.write_text("{}")
    claude.enqueue(remedy(remedy="repair_file", file="../bot.json"))
    await bot.on_error(None, ctx(app, failing()))
    assert "I could not find a data file called ../bot.json" in app.tg.texts()[-1] and outside.exists()


async def test_clear_memory_and_clean_work(app, claude, clock):
    coach = app.bot_data["coach"]
    coach.store.add_memory(OWNER, "q", "a", coach.now())
    work = coach.cfg.work_dir
    work.mkdir(parents=True, exist_ok=True)
    (work / "left.txt").write_text("x")
    repair = app.bot_data["repair"]
    done, restart = await repair.apply({"remedy": "clear_memory"}, None)
    assert coach.store.memory(OWNER) == [] and not restart
    done, _ = await repair.apply({"remedy": "clean_work"}, None)
    assert done == "I emptied the work folder (1 items)." and not any(work.iterdir())


async def test_rebuild_plan_once_a_week(app, claude, clock):
    repair = app.bot_data["repair"]
    done, _ = await repair.apply({"remedy": "rebuild_plan"}, None)
    assert done.startswith("I rebuilt this week's plan") and app.bot_data["coach"].store.load_plan(date(2026, 9, 28))
    assert app.bot_data["coach"].store.load_plan_meta(date(2026, 9, 28))["kind"] == "repair"
    done, _ = await repair.apply({"remedy": "rebuild_plan"}, None)
    assert "already rebuilt" in done and len(claude.plan_calls()) == 1


async def test_restart_is_limited(app, claude, clock):
    repair = app.bot_data["repair"]
    restarts = []
    repair.restart = lambda: restarts.append(1)
    claude.enqueue(remedy(remedy="restart", diagnosis="The job queue stopped."))
    await bot.on_error(None, ctx(app, failing()))
    assert restarts == [1] and "I am restarting" in app.tg.texts()[-1]
    assert bot.RESTART_REQUESTED is True
    store = repair.store
    assert store.read_json("health.json", {})["exit_reason"] == "self repair: The job queue stopped."
    bot.RESTART_REQUESTED = False
    now = repair.coach.now()
    store.write_json("health.json", {"starts": [(now - timedelta(hours=h)).isoformat() for h in (1, 2, 3)]})
    done, restart = await repair.apply({"remedy": "restart"}, None)
    assert not restart and "already restarted 3 times" in done


async def test_telegram_refusals_are_not_diagnosed(app, claude, clock):
    await bot.on_error(None, ctx(app, RetryAfter(5)))
    await bot.on_error(None, ctx(app, Forbidden("bot was blocked by the user")))
    assert repair_calls(claude) == [] and app.tg.sent() == []


async def test_a_failing_repair_never_raises(app, claude, clock, monkeypatch):
    async def boom(*args):
        raise OSError("disk full")
    monkeypatch.setattr(app.bot_data["repair"], "handle", boom)
    await bot.on_error(None, ctx(app, failing()))  # logged, not raised


# ---------------------------------------------------------------------------
# Self check, restarts and the watchdog
# ---------------------------------------------------------------------------


async def test_self_check_reports_once_a_day(app, claude, clock, monkeypatch):
    usage = SimpleNamespace(total=10**12, used=10**12, free=50 * 1024 * 1024)
    monkeypatch.setattr(bot.shutil, "disk_usage", lambda path: usage)
    coach = app.bot_data["coach"]
    coach.store.write_json("sessions.json", {"a": 1})
    coach.store.write_json("sessions.json", {"a": 2})
    (coach.store.root / "sessions.json").write_text("{")
    work = coach.cfg.work_dir
    work.mkdir(parents=True, exist_ok=True)
    old = work / "old.txt"
    old.write_text("x")
    os.utime(old, (time.time() - 7200, time.time() - 7200))
    (work / "new.txt").write_text("x")
    await bot.job_self_check(SimpleNamespace(bot=app.bot, application=app, job=None))
    text = app.tg.texts()[-1]
    assert "only 50 MB free" in text and "sessions.json was damaged, so I restored its last good copy" in text
    assert not old.exists() and (work / "new.txt").exists()
    sent = len(app.tg.sent())
    await bot.job_self_check(SimpleNamespace(bot=app.bot, application=app, job=None))
    assert len(app.tg.sent()) == sent  # the disk warning is not repeated the same day


async def test_restart_notice_after_an_unexpected_stop(app, clock):
    coach = app.bot_data["coach"]
    assert bot.mark_start(coach.store, coach.now()) is None  # the first start
    await bot.post_shutdown(app)
    assert bot.mark_start(coach.store, coach.now()) is None  # after a clean stop
    assert bot.mark_start(coach.store, coach.now()) == {"reason": None}  # killed without a clean stop
    bot.note_exit(coach.store, "it stopped responding for 10 minutes")
    await bot.post_shutdown(app)
    unexpected = bot.mark_start(coach.store, coach.now())
    assert unexpected == {"reason": "it stopped responding for 10 minutes"}
    job = SimpleNamespace(data=unexpected, name="restart_notice")
    await bot.job_restart_notice(SimpleNamespace(bot=app.bot, application=app, job=job))
    assert app.tg.texts()[-1].startswith("♻️ <b>BACK ONLINE</b>\n\nI restarted after an unexpected stop (it stopped responding for 10 minutes).")
    assert len(coach.store.read_json("health.json", {})["starts"]) == 4


async def test_post_init_schedules_the_restart_notice(app, clock):
    coach = app.bot_data["coach"]
    coach.store.write_json("health.json", {"running": True})
    await bot.post_init(app)
    assert any(j.name == "restart_notice" for j in app.job_queue.jobs())


def test_watchdog_restarts_a_stuck_bot(coach, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "HEARTBEAT", tmp_path / "beat")
    bot.HEARTBEAT.write_text("x")
    old = time.time() - 3600
    os.utime(bot.HEARTBEAT, (old, old))
    exits = []
    bot.start_watchdog(coach.store, limit=0.2, interval=0.05, exit_fn=exits.append)
    deadline = time.time() + 3
    while not exits and time.time() < deadline:
        time.sleep(0.05)
    assert exits == [1]
    assert "stopped responding" in coach.store.read_json("health.json", {})["exit_reason"]


def test_watchdog_leaves_a_healthy_bot_alone(coach, tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "HEARTBEAT", tmp_path / "beat")
    exits = []
    bot.start_watchdog(coach.store, limit=0.3, interval=0.05, exit_fn=exits.append)
    for _ in range(12):
        bot.beat()
        time.sleep(0.05)
    assert exits == []
    monkeypatch.setattr(bot, "HEARTBEAT", tmp_path / "gone")  # stops the thread from firing later
    bot.beat()


async def test_status_shows_self_repair(app, clock):
    coach = app.bot_data["coach"]
    bot.mark_start(coach.store, coach.now())
    lines = "\n".join(bot.status_extra(coach))
    assert "**Self repair**" in lines and "1 start in the last 7 days" in lines and "No problems so far" in lines


async def test_self_repair_off_still_tells_the_owner(env, clock, claude):
    tg = FakeTelegram()
    app = bot.build_application(bot.Config.from_env({**env, "SELF_REPAIR": "off"}), request=tg, concurrent=False)
    await app.initialize()
    try:
        await bot.on_error(None, ctx(app, failing()))
        assert repair_calls(claude) == []
        assert tg.texts()[-1].endswith("<i>The details are saved in data/errors.jsonl.</i>")
    finally:
        await app.shutdown()


async def test_what_i_did_for_a_code_bug(app, claude, clock):
    repair = app.bot_data["repair"]
    done, _ = await repair.apply({"remedy": "none", "code_fix": "Guard it."}, None)
    assert done == "Nothing. I can't fix this on my own; it needs the code change below."
    done, _ = await repair.apply({"remedy": "none", "code_fix": ""}, None)
    assert done == "Nothing. It looks like a one off, so there is nothing to fix."


async def test_repair_alerts_are_mirrored_to_the_alert_chat(env, clock, claude):
    tg = FakeTelegram()
    cfg = bot.Config.from_env({**env, "REPAIR_ALERT_CHAT": "-1002069000031/2930"})
    app = bot.build_application(cfg, request=tg, concurrent=False)
    await app.initialize()
    try:
        claude.enqueue(remedy(diagnosis="A bug.", code_fix="Guard it."))
        await bot.on_error(None, ctx(app, failing()))
        mirror = [p for p in tg.sent() if int(p["chat_id"]) == -1002069000031]
        assert len(mirror) == 1 and int(mirror[0]["message_thread_id"]) == 2930
        assert mirror[0]["text"].startswith("🏋️ <b>gym-coach-bot</b>\n\n🛠 <b>SELF REPAIR</b>")
        assert "<b>What Claude found</b> · A bug." in mirror[0]["text"] and "Guard it." in mirror[0]["text"]
        assert check_message({"text": mirror[0]["text"], "parse_mode": "HTML"}) is None
        assert any(int(p["chat_id"]) == OWNER and "SELF REPAIR" in p["text"] for p in tg.sent())
        tg.blocked_chats.add(-1002069000031)  # a failed mirror must not break anything
        claude.enqueue(remedy(diagnosis="Another."))
        await bot.on_error(None, ctx(app, ValueError("other")))
    finally:
        await app.shutdown()


def test_repair_alert_chat_must_be_chat_slash_topic(env):
    with pytest.raises(bot.ConfigError):
        bot.Config.from_env({**env, "REPAIR_ALERT_CHAT": "not a chat"})
    assert bot.Config.from_env(env).repair_alert_chat is None
