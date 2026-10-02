"""The Obsidian vault (NAS vault standard): movement log format, memory in the prompt, best effort."""

from __future__ import annotations

import re

import pytest

import bot
from conftest import press, send

LINE_RE = re.compile(r"^- \d{2}:\d{2} \S+ \*\*[^*]+\*\*( · [^\n]+)?$")


@pytest.fixture
def env(env, monkeypatch, tmp_path):
    monkeypatch.setenv("VAULT_DIR", str(tmp_path / "vault"))
    return env


def activity(cfg, day: str) -> list[str]:
    path = cfg.data_dir.parent / "vault" / "Activity" / day[:4] / day[5:7] / f"{day}.md"
    return [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]


async def test_activity_lines_and_entity_history(app, cfg, clock):
    clock.set(2026, 9, 30, 19, 5)
    await send(app, "/log rows 22kg 3x10, floor press 14kg 3x8 felt easy")
    await send(app, "/done")
    await press(app, "rate:2026-09-30:3")
    lines = activity(cfg, "2026-09-30")
    assert all(LINE_RE.match(ln) for ln in lines), lines
    assert lines[0] == ("- 19:05 🏋️ **workout logged** · rows 22kg 3x10, floor press 14kg 3x8 felt easy"
                        " · [[2026-09-30 Wednesday]]")
    assert lines[1] == "- 19:05 ✅ **workout done** · Wed 30 Sep · via command · [[2026-09-30 Wednesday]]"
    assert lines[2].startswith("- 19:05 🩹 **shoulder rated** · 3/10")
    vault = cfg.data_dir.parent / "vault"
    row = (vault / "Exercises" / "Row.md").read_text(encoding="utf-8")
    assert row.startswith("---\ntags: [active]\nupdated: 2026-09-30\n---\n# Row\n")
    assert "## History\n- 2026-09-30 19:05 22 kg 3 x 10 · [[2026-09-30 Wednesday]]\n" in row
    clock.set(2026, 10, 2, 19, 0)
    await send(app, "/log rows 24kg 3x10")
    history = (vault / "Exercises" / "Row.md").read_text(encoding="utf-8").split("## History\n")[1]
    assert history.splitlines()[1] == "- 2026-10-02 19:00 24 kg 3 x 10 · [[2026-10-02 Friday]]"  # appended
    workout = (vault / "Workouts" / "2026-09-30 Wednesday.md").read_text(encoding="utf-8")
    assert "🏋️ logged" in workout and "✅ done (via command)" in workout and "left shoulder 3/10" in workout
    home = (vault / "Home.md").read_text(encoding="utf-8")
    assert "[[2026-10-02]]" in home and "[[Row]]" in home and "[[2026-09-30 Wednesday]]" in home
    assert "`Activity/2026/10/`" in home  # the current month folder


async def test_memory_is_capped_newest_first_and_reaches_the_prompt(app, cfg, clock, claude):
    coach = app.bot_data["coach"]
    clock.set(2026, 9, 28, 8, 0)
    coach.vault.event("⏭️", "workout skipped", "Mon 28 Sep · via button", "2026-09-28 Monday")
    clock.set(2026, 9, 30, 8, 0)
    for i in range(300):  # far more than 4,000 characters
        coach.vault.event("📨", "workout sent", f"filler {i:03d}")
    memory = coach.vault.memory()
    assert len(memory) <= 4000
    assert memory.index("filler 299") < memory.index("filler 298")  # newest first
    assert "filler 000" not in memory  # the oldest fall off the cap
    await send(app, "/coach what did I skip?")
    system = claude.last()["system"]
    assert "My gym vault, what you already did and learned (newest first)" in system
    assert memory in system


async def test_memory_carries_last_lifts_and_skips(app, cfg, clock, claude):
    clock.set(2026, 9, 28, 21, 0)
    await send(app, "/log goblet squat 20kg 3x10")
    await press(app, "chk:2026-09-29:skip")
    clock.set(2026, 9, 30, 12, 0)
    memory = app.bot_data["coach"].vault.memory()
    assert "Goblet squat: 2026-09-28 21:00 20 kg 3 x 10" in memory
    assert "workout skipped** · Tue 29 Sep · via button" in memory


async def test_vault_errors_never_raise(app, cfg, clock, tmp_path, caplog):
    (tmp_path / "vault").write_text("a file where the vault folder should be")  # every write fails
    texts = await send(app, "/log rows 22kg 3x10")
    texts += await send(app, "/done")
    assert any("Logged for" in t for t in texts) and any("marked done" in t for t in texts)
    assert app.bot_data["coach"].store.logs()  # the real data is still saved
    vault = app.bot_data["coach"].vault
    assert vault.memory() == "" and vault.event("x", "y") == ""
    assert "Vault event failed" in caplog.text


def test_vault_off_without_vault_dir(tmp_path):
    vault = bot.Vault(None, bot.ZoneInfo("Asia/Singapore"))
    assert vault.event("📨", "workout sent") == "" and vault.memory() == ""
    vault.logged(bot.datetime(2026, 9, 30, 19, 0), "rows 22kg 3x10")  # no error, nothing written
    assert not list(tmp_path.iterdir())


def test_secrets_never_reach_the_vault(tmp_path, clock):
    vault = bot.Vault(tmp_path, clock.tz, ["sk-ant-oat01-SECRETSECRET"])
    line = vault.event("🩺", "self repair", "token sk-ant-oat01-SECRETSECRET leaked")
    assert "SECRET" not in line and "SECRET" not in (tmp_path / "Activity" / "2026" / "09" / "2026-09-30.md").read_text("utf-8")


def test_flat_activity_notes_migrate_into_year_month_folders(tmp_path, clock):
    old = tmp_path / "Activity"
    old.mkdir()
    (old / "2026-09-30.md").write_text("# old\n- 08:00 x **y**\n", encoding="utf-8")
    (old / "notes.md").write_text("the owner's own note stays", encoding="utf-8")
    vault = bot.Vault(tmp_path, clock.tz)  # migrates at start up
    assert (old / "2026" / "09" / "2026-09-30.md").read_text(encoding="utf-8").startswith("# old")
    assert not (old / "2026-09-30.md").exists() and (old / "notes.md").exists()
    vault.event("📨", "workout sent")
    assert "- 2026-09-30 08:00 x **y**" in vault.memory()  # old lines are still memory
    assert "[[2026-09-30]]" in (tmp_path / "Home.md").read_text(encoding="utf-8")


async def test_plan_writes_workout_notes_and_morning_send_is_logged(app, cfg, clock, claude):
    clock.set(2026, 9, 28, 9, 0)  # Monday
    await send(app, "/plan")
    vault = cfg.data_dir.parent / "vault"
    monday = (vault / "Workouts" / "2026-09-28 Monday.md").read_text(encoding="utf-8")
    assert "# Monday 28 September 2026 · " in monday and "## Plan\n" in monday
    assert "📋 plan saved (build)" in monday.split("## History\n")[1]
    assert any("📋 **plan saved (build)** · week 1" in ln for ln in activity(cfg, "2026-09-28"))
    clock.set(2026, 9, 29, 7, 0)
    await bot.job_daily_workout(bot.SimpleNamespace(application=app, bot=app.bot, job=None))
    assert any("📨 **workout sent**" in ln and "[[2026-09-29 Tuesday]]" in ln for ln in activity(cfg, "2026-09-29"))
    replanned = (vault / "Workouts" / "2026-09-28 Monday.md").read_text(encoding="utf-8")
    assert replanned.count("## History") == 1
