---
tags: [active]
updated: 2026-09-30
---
# Changelog
## 2026-09-30
- feat: legs and running split — Thursday legs only, Friday run (swim can replace the run), never combined; run goals 2.4 km < 12 min or 5 km < 35 min. `COACH_PROMPT` + `STRUCTURED_RULES` in `bot.py`.
- feat: `REPAIR_ALERT_CHAT` (chat/topic) mirrors 🩺 self-repair alerts, e.g. to NAS Doctor topic 2930 — `SelfRepair.notify` in `bot.py`; a failed mirror only logs a warning.
- test: on Windows, `tests/conftest.py` exits with the Docker test command (Windows can't exec the shebang stub `tests/bin/claude` → 117 bogus failures). README "Run the tests" documents it.
- fix: `.gitattributes` forces LF (Windows checkout made entrypoint.sh/tests/bin/claude CRLF → 115 test failures, broken image).
- feat: NAS Doctor runbook in README; `pull_policy: build` in compose.
- docs: vault created.
