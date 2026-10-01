---
tags: [active]
updated: 2026-10-01
---
# App Overview
- [[bot.py|Workout-Rotation/bot.py]]: the whole bot (python-telegram-bot + JobQueue reminders, plan builder, injury check, `claude -p` runner, self repair, Garmin reader). Weekly split: `week_split` (Mon–Wed body parts rotate weekly). Home chat: `BOT_CHAT` topic via `topic()`/`gate`.
- [[compose.yaml|Workout-Rotation/compose.yaml]]: container `gym-coach-bot`, `./data`, garmin-monitor data read-only, `pull_policy: build`.
- [[Dockerfile|Workout-Rotation/Dockerfile]] + [[entrypoint.sh|Workout-Rotation/entrypoint.sh]]: Python 3.12 slim + Claude Code, runs as PUID/PGID, health check via `bot.py --health`.
- [[.gitattributes|Workout-Rotation/.gitattributes]]: forces LF; CRLF breaks entrypoint.sh and the fake claude on Windows checkouts.
- [[tests|Workout-Rotation/tests/]]: fake claude/Telegram/Garmin. Linux only; on Windows run them in `python:3.12-slim` via Docker.
- NAS: stack `/volume1/docker/gym-coach`; watched by nas-doctor (see README "NAS Doctor").
