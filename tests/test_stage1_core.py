"""Stage 1: settings, formatting, plan parsing, the Claude runner and the core commands."""

from __future__ import annotations

import html
import json
import logging
import re
from datetime import date, timedelta
from html.parser import HTMLParser

import pytest

import bot
from conftest import BOT_TOKEN, OWNER, STRANGER, send



# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_config_reads_bot_env(cfg):
    assert cfg.allowed_ids == [OWNER, 222222]
    assert cfg.owner_id == OWNER
    assert str(cfg.tz) == "Asia/Singapore"
    assert cfg.model_ask == "sonnet" and cfg.model_plan == "sonnet"
    assert cfg.equipment_rotation == ["dumbbells", "cables", "machines", "barbell and kettlebells"]
    assert cfg.training_days == [0, 1, 2, 3, 4]
    assert cfg.reminder_mon_thu.strftime("%H:%M") == "17:30"
    assert cfg.reminder_fri.strftime("%H:%M") == "17:00"
    assert cfg.check_time.strftime("%H:%M") == "21:00"
    assert cfg.checkin_time.strftime("%H:%M") == "18:00"
    assert cfg.plan_time.strftime("%H:%M") == "20:00"
    assert cfg.token_created == date(2026, 1, 15)
    assert cfg.basketball_days == []
    assert "overhead * press" in cfg.blocked_movements


def test_config_parsing_helpers():
    assert bot.parse_days("Mon-Fri") == [0, 1, 2, 3, 4]
    assert bot.parse_days("Sat, sunday") == [5, 6]
    assert bot.parse_days("Fri-Mon") == [0, 4, 5, 6]
    assert bot.parse_days("") == []
    with pytest.raises(bot.ConfigError):
        bot.parse_days("Funday")
    assert bot.parse_time("7:05", "00:00").strftime("%H:%M") == "07:05"
    with pytest.raises(bot.ConfigError):
        bot.parse_time("25:00", "00:00")
    assert bot.parse_list(' "a, b ,c" ') == ["a", "b", "c"]


def test_config_requires_token_and_numeric_ids(env):
    with pytest.raises(bot.ConfigError):
        bot.Config.from_env({**env, "TELEGRAM_BOT_TOKEN": ""})
    with pytest.raises(bot.ConfigError):
        bot.Config.from_env({**env, "ALLOWED_USER_IDS": "me"})


def test_empty_blocked_list_turns_the_check_off(env):
    cfg = bot.Config.from_env({**env, "INJURY_BLOCKED_MOVEMENTS": ""})
    assert cfg.blocked_movements == []
    cfg = bot.Config.from_env({**env, "PROGRAM_START": "2026-10-01"})
    assert cfg.program_start == date(2026, 9, 28)  # snapped to Monday


def test_logging_hides_tokens(cfg, capsys):
    bot.setup_logging(cfg)
    assert logging.getLogger("httpx").level == logging.WARNING
    logging.getLogger("coach").warning(
        "POST https://api.telegram.org/bot%s/getUpdates and %s", BOT_TOKEN, "sk-ant-oat01-FAKEFAKEFAKEFAKEFAKE"
    )
    err = capsys.readouterr().err
    assert BOT_TOKEN not in err and "FAKEFAKE" not in err
    assert "***" in err
    logging.getLogger().handlers[:] = []


# ---------------------------------------------------------------------------
# Telegram formatting
# ---------------------------------------------------------------------------


class TagChecker(HTMLParser):
    allowed = {"b", "i", "a", "code", "pre", "u", "s"}

    def __init__(self):
        super().__init__()
        self.stack = []

    def handle_starttag(self, tag, attrs):
        assert tag in self.allowed, tag
        self.stack.append(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, (tag, self.stack)
        self.stack.pop()


def assert_valid_html(text: str) -> None:
    checker = TagChecker()
    checker.feed(text)
    checker.close()
    assert not checker.stack


def test_bold_bullets_and_escaping():
    out = bot.to_html("**Push day** for A&B <3\n- first\n* second\n  + nested\n• already")
    assert out.startswith("<b>Push day</b> for A&amp;B &lt;3")
    assert "\n• first\n• second\n  • nested\n• already" in out
    assert_valid_html(out)


def test_yt_tags_become_search_links():
    out = bot.to_html("Video: [yt: goblet squat proper form] and [YT:Pallof press]")
    assert (
        '<a href="https://www.youtube.com/results?search_query=goblet+squat+proper+form">'
        "▶️ goblet squat proper form</a>" in out
    )
    assert "search_query=Pallof+press" in out
    assert_valid_html(out)
    plain = bot.to_plain("Video: [yt: goblet squat proper form]")
    assert "https://www.youtube.com/results?search_query=goblet+squat+proper+form" in plain


def test_yt_tag_with_special_characters_is_encoded():
    out = bot.to_html("[yt: 90/90 hip switch & stretch]")
    assert "search_query=90%2F90+hip+switch+%26+stretch" in out
    assert_valid_html(out)


def test_headings_rules_markdown_links_and_code():
    out = bot.to_html("## Monday\n---\nSee [this video](https://www.youtube.com/watch?v=abc123XYZ) `3x10`")
    assert out.startswith("<b>Monday</b>")
    assert "---" not in out
    assert '<a href="https://www.youtube.com/watch?v=abc123XYZ">this video</a>' in out
    assert "<code>3x10</code>" in out
    assert_valid_html(out)


def test_video_link_detection():
    assert bot.find_video_link("watch https://www.youtube.com/watch?v=dQw4w9WgXcQ now") == (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    assert bot.find_video_link("https://youtu.be/dQw4w9WgXcQ") == "https://youtu.be/dQw4w9WgXcQ"
    assert bot.find_video_link("https://www.youtube.com/shorts/abcdefg")
    assert bot.find_video_link("[yt: squat]") is None
    assert bot.find_video_link(bot.to_html("[yt: squat]")) is None
    assert bot.find_video_link("https://www.youtube.com/results?search_query=squat") is None


def test_long_text_is_split_under_the_limit():
    paragraph = "**Bold line** with [yt: goblet squat proper form] & more <text>.\n" * 12
    text = "\n\n".join(paragraph for _ in range(20))
    chunks = bot.split_text(text)
    assert len(chunks) > 1
    for chunk in chunks:
        rendered = bot.to_html(chunk)
        assert bot._tg_len(rendered) <= 4096
        assert_valid_html(rendered)
    joined = "".join(chunks).replace("\n", "").replace(" ", "")
    assert joined == text.replace("\n", "").replace(" ", "")


def test_single_huge_line_is_still_split():
    chunks = bot.split_text("word " * 3000)
    assert all(bot._tg_len(bot.to_html(c)) <= 4096 for c in chunks)
    chunks = bot.split_text("x" * 10000)
    assert all(bot._tg_len(bot.to_html(c)) <= 4096 for c in chunks)


# ---------------------------------------------------------------------------
# Plan parsing and the injury check
# ---------------------------------------------------------------------------

SAMPLE_PLAN = """I compared both splits and picked push/pull.

📅 Monday: Push
Warm up: 5 minutes bike.
1. Single arm dumbbell floor press: 3 x 10, rest 90s
Cue: elbow at 45 degrees.
Video: [yt: single arm dumbbell floor press proper form]
2. **Landmine press** (instead of overhead press): 3 x 8, rest 90s
Video: [yt: landmine press proper form]
📝 Rehab note inside Monday stays in Monday.

**📅 Tuesday (6 Oct): Pull**
1. Chest supported row 3x10
2. Band reverse fly: 2 x 15

📅 Wednesday: Rest or light mobility
Walk.

📅 Thursday: Legs and run
1. Leg press: 4 x 10
📅 Friday: Swim, or legs
1. Kick sets: 10 x 50 m
📅 Saturday - Basketball or rest
📅 Sunday: Rest

📝 Notes: sleep 7 to 8 hours.
Protein 125 g a day."""


def test_parse_plan_days_focus_and_notes():
    parsed = bot.parse_plan(SAMPLE_PLAN)
    assert parsed.missing_days == []
    assert parsed.preface.startswith("I compared")
    assert parsed.days[0].focus == "Push"
    assert parsed.days[1].focus == "Pull"
    assert parsed.days[5].focus == "Basketball or rest"
    assert "Rehab note inside Monday" in parsed.days[0].text
    assert parsed.notes.startswith("📝 Notes")
    assert "Protein" in parsed.notes and "Protein" not in parsed.days[6].text
    assert parsed.days[2].is_rest and parsed.days[6].is_rest
    assert not parsed.days[0].is_rest and not parsed.days[4].is_rest


def test_exercise_names():
    names = [n for _, n in bot.plan_exercises(SAMPLE_PLAN)]
    assert names[:3] == ["Single arm dumbbell floor press", "Landmine press", "Chest supported row"]
    assert "Leg press" in names and "Kick sets" in names


def test_missing_days_are_reported():
    parsed = bot.parse_plan("📅 Monday: Push\n1. Row: 3 x 10")
    assert parsed.missing_days == DAYS_WITHOUT_MONDAY


DAYS_WITHOUT_MONDAY = ["Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def test_injury_check_ignores_swaps_and_allowed_moves(cfg):
    assert bot.find_blocked(SAMPLE_PLAN, cfg.blocked_movements, cfg.allowed_movements) == []


@pytest.mark.parametrize(
    "line,term",
    [
        ("1. Barbell overhead press: 3 x 8", "overhead * press"),
        ("2. Bench dips: 3 x 12", "dip"),
        ("**3. Upright rows** 3x10", "upright * row"),
        ("• Behind the neck pulldown: 3 x 10", "behind the neck"),
        ("4. Wide grip bench press: 5 x 5", "wide grip * bench"),
        ("5. Cable chest flyes: 3 x 12", "fly"),
        ("6. Seated dumbbell shoulder press: 3 x 10", "shoulder press"),
    ],
)
def test_injury_check_catches_blocked_moves(cfg, line, term):
    plan = f"📅 Monday: Push\n{line}\nVideo: [yt: something proper form]"
    hits = bot.find_blocked(plan, cfg.blocked_movements, cfg.allowed_movements)
    assert len(hits) == 1 and hits[0]["term"] == term and hits[0]["day"] == "Monday"


@pytest.mark.parametrize(
    "line",
    [
        "1. Reverse cable fly: 2 x 15",
        "1. Rear delt dumbbell fly: 2 x 15",
        "1. Single arm dumbbell bench press, light on the left: 3 x 10",
        "1. Landmine press, swap for overhead press: 3 x 8",
        "1. Half kneeling cable press: 3 x 10",
        "Cue: never press overhead.",
        "Video: [yt: overhead press alternatives]",
    ],
)
def test_injury_check_allows_safe_lines(cfg, line):
    plan = f"📅 Monday: Push\n{line}"
    assert bot.find_blocked(plan, cfg.blocked_movements, cfg.allowed_movements) == []


def test_effort_wave_and_rotation(coach):
    coach.store.update_state(program_start="2026-09-28")
    weeks = [coach.week_number(date(2026, 9, 28) + timedelta(weeks=k)) for k in range(6)]
    assert weeks == [1, 2, 3, 4, 5, 6]
    assert [coach.equipment_for(w) for w in weeks] == [
        "dumbbells", "cables", "machines", "barbell and kettlebells", "dumbbells", "cables",
    ]
    assert [bot.is_deload(w) for w in weeks] == [False, False, False, True, False, False]
    assert bot.effort_for(4).startswith("Deload")
    assert bot.effort_for(8).startswith("Deload")


# ---------------------------------------------------------------------------
# Claude runner
# ---------------------------------------------------------------------------


async def test_ask_uses_the_exact_claude_flags(coach, claude, cfg):
    answer = await coach.ask(OWNER, "Is 30 minutes enough today?")
    assert "Coach says" in answer
    call = claude.last()
    argv = call["argv"]
    assert argv[0] == "-p"
    assert "--bare" not in argv
    joined = " ".join(argv)
    assert "--output-format json --no-session-persistence --permission-mode dontAsk --model sonnet" in joined
    assert "--tools WebSearch --allowedTools WebSearch --max-turns 10" in joined
    assert call["stdin"] == "Is 30 minutes enough today?"
    assert call["cwd"] == str(cfg.work_dir) and call["cwd_files"] == []
    assert call["env"] == {"oauth": True, "api_key": False, "telegram": False, "autoupdater_off": True}
    # the system prompt file is removed after the call
    assert not __import__("os").path.exists(call["prompt_file"])


async def test_system_prompt_has_about_me_and_context(coach, claude):
    await coach.ask(OWNER, "hi")
    system = claude.last()["system"]
    assert system.startswith("You are an experienced strength and conditioning coach")
    assert "Age: 31\nHeight: 183 cm\nWeight: 78 kg" in system
    assert "Training experience: intermediate" in system
    assert "Time per session: 60 minutes, including changing and showering" in system
    assert "I play basketball with no fixed day" in system
    assert "Today is Wednesday 30 September 2026, 12:00 Singapore time" in system
    assert "Left shoulder micro tear" in system
    assert "This week's plan: none saved yet." in system
    assert "[" not in system.split("Video guides")[0].replace("[yt:", "")  # no unfilled placeholders


async def test_plan_calls_have_no_tools(coach, claude):
    await coach.build_week(date(2026, 9, 28))
    argv = claude.last()["argv"]
    i = argv.index("--tools")
    assert argv[i + 1] == "" and argv[i + 2:i + 4] == ["--max-turns", "3"]
    assert "--allowedTools" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"


async def test_models_are_separate(env, clock, claude):
    cfg = bot.Config.from_env({**env, "CLAUDE_MODEL_ASK": "haiku", "CLAUDE_MODEL_PLAN": "opus"})
    coach = bot.Coach(cfg)
    await coach.ask(OWNER, "hi")
    assert claude.last()["argv"][claude.last()["argv"].index("--model") + 1] == "haiku"
    await coach.build_week(date(2026, 9, 28))
    assert claude.last()["argv"][claude.last()["argv"].index("--model") + 1] == "opus"


async def test_api_key_is_used_when_there_is_no_oauth_token(env, clock, claude, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-FAKEKEYFAKEKEY")
    coach = bot.Coach(bot.Config.from_env())
    await coach.ask(OWNER, "hi")
    assert claude.last()["env"]["oauth"] is False and claude.last()["env"]["api_key"] is True


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("error", "Claude Code reported an error: API Error: 500"),
        ("auth", "token probably needs renewing"),
        ("limit", "usage limit"),
        ("maxturns", "ran out of steps"),
        ("nonjson", "could not read"),
    ],
)
async def test_claude_failures_become_plain_messages(coach, claude, mode, expected):
    claude.enqueue({"mode": mode}, {"mode": mode})  # brief errors are retried once
    with pytest.raises(bot.ClaudeError) as err:
        await coach.ask(OWNER, "hi")
    assert expected in err.value.user_message
    assert coach.store.memory(OWNER) == []


async def test_claude_timeout(coach, claude):
    coach.cfg.ask_timeout = 1
    claude.enqueue({"mode": "sleep", "seconds": 5})
    with pytest.raises(bot.ClaudeError) as err:
        await coach.ask(OWNER, "hi")
    assert "longer than" in err.value.user_message


async def test_json_list_output_is_accepted(coach, claude):
    claude.enqueue({"mode": "list", "result": "From a list"})
    assert await coach.ask(OWNER, "hi") == "From a list"


async def test_missing_claude_binary(env, clock, tmp_path):
    cfg = bot.Config.from_env({**env, "CLAUDE_BIN": str(tmp_path / "nope")})
    with pytest.raises(bot.ClaudeError) as err:
        await bot.Coach(cfg).ask(OWNER, "hi")
    assert "not installed" in err.value.user_message


async def test_memory_keeps_last_six_turns(coach, claude):
    for i in range(8):
        await coach.ask(OWNER, f"question {i}")
    turns = coach.store.memory(OWNER)
    assert [t["q"] for t in turns] == [f"question {i}" for i in range(2, 8)]
    stdin = claude.last()["stdin"]
    assert "Our recent conversation, oldest first" in stdin
    assert "Me: question 1" in stdin and "Me: question 6" in stdin and "Me: question 0" not in stdin
    assert stdin.endswith("My new message:\nquestion 7")
    assert coach.store.memory(222222) == []


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


async def test_first_plan_sets_week_one_and_the_rotating_split(coach, claude):
    result = await coach.build_week(date(2026, 9, 28))
    path = coach.store.plan_path(date(2026, 9, 28))
    assert path.name == "2026-09-28.md" and path.exists()
    meta = coach.store.load_plan_meta(date(2026, 9, 28))
    assert meta["week"] == 1 and meta["equipment"] == "dumbbells" and meta["effort"].startswith("Building week 1")
    request = claude.last()["stdin"]
    assert "Week number: 1" in request and "Main equipment this week: dumbbells" in request
    assert "Monday: Chest\nTuesday: Back\nWednesday: Shoulders\n" in request
    assert "Thursday: Legs or run\nFriday: Run or swim" in request
    assert "I did not send a Sunday check in answer" in request
    assert "📅 Monday: Push" in request  # format rules are included
    state = coach.store.state()
    assert state["program_start"] == "2026-09-28"
    assert result.warnings == []


async def test_following_weeks_rotate_and_avoid_repeats(coach, claude):
    await coach.build_week(date(2026, 9, 28))
    await coach.build_week(date(2026, 10, 5))
    request = claude.last()["stdin"]
    assert "Week number: 2" in request and "Main equipment this week: cables" in request
    assert "Monday: Back\nTuesday: Shoulders\nWednesday: Arms\n" in request
    assert "Dumbbells move W1 D1 N1" in request  # last week's exercises are listed
    await coach.build_week(date(2026, 10, 12))
    request = claude.last()["stdin"]
    assert "Main equipment this week: machines" in request
    assert "Dumbbells move W1 D1 N1" in request and "Cables move W2 D1 N1" in request
    await coach.build_week(date(2026, 10, 19))
    request = claude.last()["stdin"]
    assert "barbell and kettlebells" in request and "Deload week" in request
    assert "Dumbbells move W1" not in request  # only the previous two weeks


async def test_blocked_movement_triggers_one_fix(coach, claude):
    bad = bot.clean_reply(
        "📅 Monday: Push\n1. Barbell overhead press: 3 x 8\n📅 Tuesday: Pull\n📅 Wednesday: Rest\n"
        "📅 Thursday: Legs\n📅 Friday: Swim\n📅 Saturday: Rest\n📅 Sunday: Rest\n📝 Notes"
    )
    claude.enqueue({"result": bad})
    result = await coach.build_week(date(2026, 9, 28))
    calls = claude.plan_calls()
    assert len(calls) == 2
    fix = calls[1]["stdin"]
    assert "Your plan below has problems" in fix
    assert 'Monday, "1. Barbell overhead press: 3 x 8": overhead press' in fix
    assert "1. Barbell overhead press" in fix  # the plan itself is included
    assert "FIXED" in coach.store.load_plan(date(2026, 9, 28))
    assert coach.store.load_plan_meta(date(2026, 9, 28))["fixed_once"] is True
    assert result.warnings == []


async def test_missing_days_trigger_a_fix_and_a_warning_if_still_wrong(coach, claude):
    claude.enqueue({"result": "📅 Monday: Push\n1. Row: 3 x 10"}, {"result": "📅 Monday: Push\n1. Row: 3 x 10"})
    result = await coach.build_week(date(2026, 9, 28))
    assert len(claude.plan_calls()) == 2  # only one fix attempt
    assert "missing these days" in claude.plan_calls()[1]["stdin"]
    assert result.warnings and "missing" in result.warnings[0]


async def test_clean_plan_needs_no_fix(coach, claude):
    await coach.build_week(date(2026, 9, 28))
    assert len(claude.plan_calls()) == 1


async def test_rebuild_keeps_a_backup(coach, claude):
    monday = date(2026, 9, 28)
    await coach.build_week(monday)
    await coach.build_week(monday, "I have a cold, keep it easy")
    history = list((coach.store.root / "plans" / "history").glob("2026-09-28_*.md"))
    assert len(history) == 1
    assert "My notes for this plan: I have a cold, keep it easy" in claude.last()["stdin"]


# ---------------------------------------------------------------------------
# Telegram commands through the real handlers
# ---------------------------------------------------------------------------


async def test_command_menu_is_registered(app):
    await bot.post_init(app)
    commands = [c["command"] for c in app.tg.sent("setMyCommands")[0]["commands"]]
    assert commands == ["coach", "today", "week", "day", "plan", "nextweek", "log", "done", "shoulder", "progress",
        "injury", "away", "profile", "gymstatus", "reset", "whoami"]


async def test_strangers_only_get_their_id(app, claude):
    texts = await send(app, "/today", user_id=STRANGER)
    assert texts == [f"🔒 <b>PRIVATE BOT</b>\n\nSorry, this is a private bot. Your Telegram user ID is <code>{STRANGER}</code>."]
    texts = await send(app, "hello coach", user_id=STRANGER)
    assert texts == []  # rate limited, and nothing reaches Claude
    assert claude.all() == []


async def test_whoami(app):
    assert await send(app, "/whoami") == [f"🪪 <b>YOUR ID</b>\n\nYour Telegram user ID is <code>{OWNER}</code>."]


async def test_ask_command_and_plain_messages(app, claude):
    texts = await send(app, "/ask What should I eat before training?")
    assert len(texts) == 1
    assert texts[0].startswith("💬 <b>COACH</b>\n\n<b>Coach says:</b> keep it light today.")
    assert "• Goblet squat 3 x 10" in texts[0]
    assert 'href="https://www.youtube.com/results?search_query=goblet+squat+proper+form"' in texts[0]
    sent = app.tg.sent()[-1]
    assert sent["parse_mode"] == "HTML"
    assert sent["link_preview_options"] == {"is_disabled": True}
    assert claude.last()["stdin"] == "What should I eat before training?"
    await send(app, "and after?")
    assert "Me: What should I eat before training?" in claude.last()["stdin"]
    assert "typing" in [p.get("action") for _, p in app.tg.calls if _ == "sendChatAction"]


async def test_ask_without_question(app, claude):
    texts = await send(app, "/ask")
    assert "followed by your question" in texts[0]
    assert claude.all() == []


async def test_plain_messages_in_groups_are_ignored(app, claude):
    texts = await send(app, "hello", chat_id=-100123, chat_type="group")
    assert texts == [] and claude.all() == []


async def test_direct_video_link_gets_a_preview(app, claude):
    claude.enqueue({"result": "Here is a good one: https://www.youtube.com/watch?v=abcdefghijk"})
    await send(app, "/ask video for face pulls")
    assert app.tg.sent()[-1]["link_preview_options"] == {"url": "https://www.youtube.com/watch?v=abcdefghijk"}


async def test_html_rejection_falls_back_to_plain_text(app, claude):
    app.tg.reject_html = True
    await send(app, "/ask anything")
    last = app.tg.sent()[-1]
    assert "parse_mode" not in last
    assert last["text"].startswith("💬 COACH\n\nCoach says: keep it light today.")
    assert "https://www.youtube.com/results?search_query=goblet+squat+proper+form" in last["text"]


async def test_long_answers_are_split(app, claude):
    claude.enqueue({"result": "\n\n".join(f"Paragraph {i}: " + "word " * 150 for i in range(12))})
    texts = await send(app, "/ask tell me everything")
    assert len(texts) >= 2
    assert all(bot._tg_len(t) <= 4096 for t in texts)


async def test_claude_error_reaches_the_user(app, claude):
    claude.enqueue({"mode": "auth"})
    texts = await send(app, "/ask hi")
    assert "claude setup-token" in texts[-1]


async def test_reset(app, claude):
    await send(app, "/ask hi")
    assert app.bot_data["coach"].store.memory(OWNER)
    assert await send(app, "/reset") == ["🧹 <b>CHAT MEMORY</b>\n\nChat memory cleared."]
    assert app.bot_data["coach"].store.memory(OWNER) == []


async def test_today_week_plan_and_nextweek(app, claude, clock):
    assert "Send /plan" in (await send(app, "/today"))[0]
    texts = await send(app, "/plan")
    assert "Building this week's plan" in texts[0]
    assert texts[1].startswith("🗓 <b>WEEK PLAN</b> · Week 1 · dumbbells · building week 1 of 3 · Mon 28 Sep to Sun 4 Oct")
    today = await send(app, "/today")
    assert today[0].startswith("📅 <b>WORKOUT</b> · Wed 30 Sep\n\n📅 Wednesday: Legs and core")
    assert "📅 Thursday" not in today[0]
    week = await send(app, "/week")
    assert "📅 Monday: Push" in week[0] and "📝 Notes" in "".join(week)

    clock.set(2026, 10, 3, 10, 0)  # Saturday: next week's plan does not exist yet
    week = await send(app, "/week")
    joined = "".join(week)
    assert "Week 1" in joined and "/nextweek" in joined
    texts = await send(app, "/nextweek focus on jumping")
    assert "Week 2 · cables" in texts[1]
    assert "My notes for this plan: focus on jumping" in claude.last()["stdin"]
    week = await send(app, "/week")
    assert "Week 2 · cables" in week[0]
    today = await send(app, "/today")
    assert today[0].startswith("📅 <b>WORKOUT</b> · Sat 3 Oct\n\n📅 Saturday")


async def test_plan_error_is_reported(app, claude):
    claude.enqueue({"mode": "error"}, {"mode": "error"})
    texts = await send(app, "/plan")
    assert "I could not build the plan. Claude Code reported an error" in texts[-1]


async def test_injury_command(app, claude):
    texts = await send(app, "/injury")
    assert "Left shoulder micro tear" in texts[0] and "· from bot.env" in texts[0]
    await send(app, "/injury Physio cleared light pressing on 1 Oct")
    texts = await send(app, "/injury")
    assert "Physio cleared light pressing" in texts[0] and "updated 2026-09-30" in texts[0]
    await send(app, "/ask hi")
    assert "Physio cleared light pressing" in claude.last()["system"]
    assert await send(app, "/injury none") == ["🤕 <b>INJURY NOTES</b> · cleared\n\nInjury notes cleared."]
    await send(app, "/ask hi")
    assert "My latest injury notes (updated 2026-09-30): none." in claude.last()["system"]


async def test_profile_and_status(app, claude):
    await send(app, "/plan")
    profile = (await send(app, "/profile"))[0]
    assert "Age 31, height 183 cm, weight 78 kg" in profile
    assert "Split this week: Monday: Chest, Tuesday: Back, Wednesday: Shoulders," in profile
    assert "Left shoulder micro tear" in profile
    status = (await send(app, "/status"))[0]
    assert "Version: <code>2.1.999 (Claude Code)</code>" in status
    assert "✅ signed in with oauth_token" in status
    assert "Token created 2026-01-15, expires about 2027-01-15" in status
    assert "Last plan built Wed 30 Sep 12:00" in status


async def test_status_shows_sign_in_problem(app, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "out")
    status = (await send(app, "/status"))[0]
    assert "⚠️ not signed in" in status


async def test_unknown_command(app):
    texts = await send(app, "/dance")
    assert "I do not know that command" in texts[0]


def read_env_file(path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def test_bot_env_example_parses(tmp_path):
    from pathlib import Path

    values = read_env_file(Path(__file__).resolve().parent.parent / "bot.env.example")
    values["TELEGRAM_BOT_TOKEN"] = BOT_TOKEN
    values["ALLOWED_USER_IDS"] = "111"
    cfg = bot.Config.from_env(values)
    assert cfg.blocked_movements == bot.parse_list(bot.DEFAULT_BLOCKED)
    assert cfg.allowed_movements == bot.parse_list(bot.DEFAULT_ALLOWED)
    assert cfg.equipment_rotation == bot.parse_list(bot.DEFAULT_ROTATION)
    assert cfg.garmin_db == "/garmin/monitor.db" and cfg.garmin_profile == "Me"
    assert cfg.secrets == [BOT_TOKEN]  # empty token lines are not treated as secrets


# ---------------------------------------------------------------------------
# Fixes from the requirements review
# ---------------------------------------------------------------------------


async def test_next_week_build_tells_claude_which_week_it_is_planning(coach, claude, clock):
    await coach.build_week(date(2026, 9, 28))
    clock.set(2026, 10, 4, 20, 0)  # Sunday of week 1, building week 2
    await coach.build_week(date(2026, 10, 5))
    system = claude.last()["system"]
    assert "The current week is week 1 of my programme" in system
    assert "Its main equipment is dumbbells" in system
    assert "You are now building the plan for a different week: week 2, the week of Monday 5 October 2026. Its main equipment is cables." in system
    await coach.ask(OWNER, "hi")
    assert "different week" not in claude.last()["system"]


async def test_strength_day_without_numbered_exercises_is_fixed(coach, claude):
    no_numbers = "\n".join(
        [f"📅 {d}: " + ("Push\n• Dumbbell press 3 x 10" if d == "Monday" else "Rest") for d in bot.DAY_NAMES]
    )
    claude.enqueue({"result": no_numbers})
    await coach.build_week(date(2026, 9, 28))
    assert len(claude.plan_calls()) == 2
    assert "Monday (Push) has no numbered exercises" in claude.plan_calls()[1]["stdin"]


def test_basketball_and_swim_days_need_no_numbers(coach):
    plan = "\n".join(f"📅 {d}: " + {"Friday": "Swim", "Saturday": "Basketball"}.get(d, "Rest") for d in bot.DAY_NAMES)
    assert coach.plan_problems(plan) == []


def test_landmine_push_press_is_allowed(cfg):
    plan = "📅 Monday: Push\n1. Half kneeling landmine push press: 3 x 8"
    assert bot.find_blocked(plan, cfg.blocked_movements, cfg.allowed_movements) == []


async def test_status_shows_the_last_claude_call(app, claude):
    status = (await send(app, "/status"))[0]
    assert "Last Claude call: none since the bot started" in status
    await send(app, "/ask hi")
    status = (await send(app, "/status"))[0]
    assert "Last Claude call: ✅ worked, Wed 30 Sep 12:00 (question)" in status
    claude.enqueue({"mode": "auth"})
    await send(app, "/ask hi")
    status = (await send(app, "/status"))[0]
    assert "Last Claude call: ⚠️ failed, Wed 30 Sep 12:00 (question): Claude Code could not sign in" in status


async def test_week_on_saturday_explains_first(app, claude, clock):
    await send(app, "/plan")
    clock.set(2026, 10, 3, 10, 0)
    week = "".join(await send(app, "/week"))
    assert week.startswith("🗓 <b>WEEK PLAN</b>") and "Next week's plan is not built yet." in week
    assert "Here is this week's plan until then." in week


async def test_network_errors_get_a_plain_message(coach, claude):
    # the exact shape Claude Code 2.1 prints when it cannot connect
    refused = {"mode": "error_result", "result": "API Error: Connection refused — a firewall or proxy may be blocking it (ECONNREFUSED)"}
    claude.enqueue(refused, refused)
    with pytest.raises(bot.ClaudeError) as err:
        await coach.ask(OWNER, "hi")
    assert "could not reach Anthropic's servers" in err.value.user_message



async def test_brief_failures_are_retried_once(coach, claude):
    claude.enqueue({"mode": "error"})  # a 500 once, then fine
    assert "Coach says" in await coach.ask(OWNER, "hi")
    assert len(claude.all()) == 2
    assert coach.runner.last_call["ok"] is True


@pytest.mark.parametrize("mode", ["auth", "limit", "maxturns", "nonjson"])
async def test_lasting_failures_are_not_retried(coach, claude, mode):
    claude.enqueue({"mode": mode})
    with pytest.raises(bot.ClaudeError):
        await coach.ask(OWNER, "hi")
    assert len(claude.all()) == 1
