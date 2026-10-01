---
tags: [active]
updated: 2026-10-01
---
# Changelog
## 2026-10-01
- feat: `/coach <question>` replaces `/ask` in the menu (the owner Channel shares one `/` menu across every bot (Telegram has no per-topic command scope), so three bots' `/ask` collided.) `/ask` still works (`CommandHandler(["coach", "ask"])` in `bot.py`).
- feat: Thursday is legs OR a run (plan gives both, you pick one), like Friday's run or swim.
- feat: one body part per upper body day: `UPPER_BODY_ROTATION` = Chest, Back, Shoulders, Arms. Four parts slide across Mon–Wed, so one sits out each week (week 1 C/B/S, week 2 B/S/A, week 3 S/A/C, week 4 A/C/B).
- feat: weekly body-part rotation. Mon–Wed = chest and triceps / back and biceps / shoulders and core, shifting one day each week (`week_split` + `UPPER_BODY_ROTATION` in `bot.py`). Thu = legs then an easy run; Fri = run or swim. Replaces the "Claude picks a fixed split in week 1" logic (`state["split"]` no longer written).
- feat: `BOT_CHAT` (group/topic, e.g. the owner Channel `<TELEGRAM_CHAT_ID>/3038`). Scheduled sends and replies go to that topic (`topic()` adds `message_thread_id` in every send); `gate` only lets ALLOWED_USER_IDS through in that topic or a DM, so other bots' topics never trigger it. No duplicate 🩺 alert when `REPAIR_ALERT_CHAT` is the same topic.
## 2026-09-30
- feat: legs and running split — Thursday legs only, Friday run (swim can replace the run), never combined; run goals 2.4 km < 12 min or 5 km < 35 min. `COACH_PROMPT` + `STRUCTURED_RULES` in `bot.py`.
- feat: `REPAIR_ALERT_CHAT` (chat/topic) mirrors 🩺 self-repair alerts, e.g. to NAS Doctor topic 2930 — `SelfRepair.notify` in `bot.py`; a failed mirror only logs a warning.
- test: on Windows, `tests/conftest.py` exits with the Docker test command (Windows can't exec the shebang stub `tests/bin/claude` → 117 bogus failures). README "Run the tests" documents it.
- fix: `.gitattributes` forces LF (Windows checkout made entrypoint.sh/tests/bin/claude CRLF → 115 test failures, broken image).
- feat: NAS Doctor runbook in README; `pull_policy: build` in compose.
- docs: vault created.
