---
tags: [active]
updated: 2026-10-02
---
# Changelog
## 2026-10-02
- feat: vault Activity notes live in year/month folders: `Activity/YYYY/MM/YYYY-MM-DD.md` (`Vault.event`, `_activity_notes`, `Home.md` names the current month folder). `Vault.migrate()` runs at start up and moves old flat `Activity/YYYY-MM-DD.md` notes into `YYYY/MM/` (never deletes; James's other notes stay). Memory reads the nested layout. Test: `test_flat_activity_notes_migrate_into_year_month_folders`.
- chore: local main was 2 commits behind origin/main (card style + vault); the NAS copy of bot.py/compose.yaml/entrypoint.sh matched origin/main exactly, so nothing to fold in.
## 2026-10-02
- feat: Obsidian vault (NAS standard "movement log + memory") at `/volume1/James/Obsidian/Gym Coach`, mounted at `/vault`, `VAULT_DIR` in `bot.env` (empty = off). `Vault` class in `bot.py`: `Activity/YYYY-MM-DD.md` gets `- HH:MM emoji **what** · detail · [[note]]` for plans saved, workouts sent/done/skipped, logs, shoulder ratings, `/coach` + `/gymstatus` answers and self repairs; `Workouts/<date> <Weekday>.md` (plan + `## History` of results) and `Exercises/<name>.md` (weight progression in `## History`); `Home.md` MOC. Hooks sit in `Store` (`add_log`, `set_session`, `add_rating`, `save_plan`), so every caller logs.
- feat: memory. `Coach.system_prompt` appends `Vault.memory()`, a ~4,000 char excerpt, newest first (Activity up to 3/5 of it, then latest workouts and exercise progression), so plans, `/coach` and lighter/short sessions know the last lifts and skips.
- Best effort: every vault method is wrapped by `best_effort` (logs a warning, returns ""); secrets are redacted; files chmod 664 / folders 775, chown to PUID/PGID when root; `entrypoint.sh` creates and chowns only the vault folder. Tests: `tests/test_vault.py`.
## 2026-10-01
- feat: every Telegram message uses the HTML card style: header `emoji <b>TITLE</b> · subtitle` (fixed emoji per message type in `SECTION_TITLES`, built by `header()`/`card()` in `bot.py`), one block per item, `━━━━━━━━━━━━━━━━` between body-part sections, hints in `<i>`, background (split reasoning, plan notes, last week in numbers) folded in `<blockquote expandable>` at the end. Workout cards: one block per exercise `🫸 <b>1 · Bench</b> · <code>3 × 10</code> @ <b>12.5 kg</b>`, then a ⏸ rest/effort line and a ▶️ Form video link.
- refactor: one send path. `send_view` → `send_blocks` for everything (old `send_text` removed); text and Claude answers go through `text_blocks` (escaped by `to_html`). `pack_blocks` cuts only between blocks, or inside an oversized block at a line outside every tag (`split_block`). Telegram 400 → resent as plain text (`html_to_plain`). The repair mirror to `REPAIR_ALERT_CHAT` uses it too (was a raw `[:4096]` cut that could split a tag). Tests: `tests/test_card_style.py`.
- test: `e2e_scenario.py` command menu updated to `/coach` + `/gymstatus` (was failing since those renames).
- feat: `/gymstatus` replaces `/status` in the menu (Garmin and the trading desk also had `/status`). James Channel shows every bot's commands in one `/` menu (no per-topic scope in Telegram), so names must be unique across bots. `/status` still works.
- feat: `/coach <question>` replaces `/ask` in the menu (James Channel shares one `/` menu across every bot (Telegram has no per-topic command scope), so three bots' `/ask` collided.) `/ask` still works (`CommandHandler(["coach", "ask"])` in `bot.py`).
- feat: Thursday is legs OR a run (plan gives both, you pick one), like Friday's run or swim.
- feat: one body part per upper body day: `UPPER_BODY_ROTATION` = Chest, Back, Shoulders, Arms. Four parts slide across Mon–Wed, so one sits out each week (week 1 C/B/S, week 2 B/S/A, week 3 S/A/C, week 4 A/C/B).
- feat: weekly body-part rotation. Mon–Wed = chest and triceps / back and biceps / shoulders and core, shifting one day each week (`week_split` + `UPPER_BODY_ROTATION` in `bot.py`). Thu = legs then an easy run; Fri = run or swim. Replaces the "Claude picks a fixed split in week 1" logic (`state["split"]` no longer written).
- feat: `BOT_CHAT` (group/topic, e.g. James Channel `-1002069000031/3038`). Scheduled sends and replies go to that topic (`topic()` adds `message_thread_id` in every send); `gate` only lets ALLOWED_USER_IDS through in that topic or a DM, so other bots' topics never trigger it. No duplicate 🩺 alert when `REPAIR_ALERT_CHAT` is the same topic.
## 2026-09-30
- feat: legs and running split — Thursday legs only, Friday run (swim can replace the run), never combined; run goals 2.4 km < 12 min or 5 km < 35 min. `COACH_PROMPT` + `STRUCTURED_RULES` in `bot.py`.
- feat: `REPAIR_ALERT_CHAT` (chat/topic) mirrors 🩺 self-repair alerts, e.g. to NAS Doctor topic 2930 — `SelfRepair.notify` in `bot.py`; a failed mirror only logs a warning.
- test: on Windows, `tests/conftest.py` exits with the Docker test command (Windows can't exec the shebang stub `tests/bin/claude` → 117 bogus failures). README "Run the tests" documents it.
- fix: `.gitattributes` forces LF (Windows checkout made entrypoint.sh/tests/bin/claude CRLF → 115 test failures, broken image).
- feat: NAS Doctor runbook in README; `pull_policy: build` in compose.
- docs: vault created.
