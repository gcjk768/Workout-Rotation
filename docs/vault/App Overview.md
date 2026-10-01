---
tags: [active]
updated: 2026-10-02
---
# App Overview
- [[bot.py|Workout-Rotation/bot.py]]: the whole bot (python-telegram-bot + JobQueue reminders, plan builder, injury check, `claude -p` runner, self repair, Garmin reader). Weekly split: `week_split` (Mon–Wed body parts rotate weekly). Home chat: `BOT_CHAT` topic via `topic()`/`gate`. Telegram output: card style via `header()`/`card()` + `SECTION_TITLES`; every message goes through `send_blocks` (escape, block-safe chunking, plain-text fallback).
- Obsidian vault (movement log + memory): `Vault` in [[bot.py|Workout-Rotation/bot.py]], `VAULT_DIR=/vault` → NAS `/volume1/James/Obsidian/Gym Coach` (`Activity/`, `Workouts/`, `Exercises/`, `Home.md`). Written from `Store` hooks + `send_day_session`/`Coach.ask`/`cmd_status`/`SelfRepair.record`; read back by `Vault.memory()` in `Coach.system_prompt`. [[test_vault|Workout-Rotation/tests/test_vault.py]].
- [[compose.yaml|Workout-Rotation/compose.yaml]]: container `gym-coach-bot`, `./data`, garmin-monitor data read-only, the Obsidian vault at `/vault`, `pull_policy: build`.
- [[Dockerfile|Workout-Rotation/Dockerfile]] + [[entrypoint.sh|Workout-Rotation/entrypoint.sh]]: Python 3.12 slim + Claude Code, runs as PUID/PGID, health check via `bot.py --health`.
- [[.gitattributes|Workout-Rotation/.gitattributes]]: forces LF; CRLF breaks entrypoint.sh and the fake claude on Windows checkouts.
- [[tests|Workout-Rotation/tests/]]: fake claude/Telegram/Garmin. [[test_card_style|Workout-Rotation/tests/test_card_style.py]] covers escaping, chunking and the fallback. Linux only; on Windows run them in `python:3.12-slim` via Docker.
- NAS: stack `/volume1/docker/gym-coach`; watched by nas-doctor (see README "NAS Doctor").
