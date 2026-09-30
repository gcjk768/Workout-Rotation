# Gym coach bot

A private Telegram bot that coaches you like a personal trainer. It runs in Docker on your UGREEN NAS and uses Claude Code in headless mode (`claude -p`) for all its AI. Your Claude subscription pays for it, so no API billing is needed.

- Answers training questions (`/ask`, or just write to it), with web search for real videos.
- Builds a full 7-day plan every week. The split stays the same while the exercises change. The main equipment rotates (dumbbells, cables, machines, barbell and kettlebells). Effort follows a 4-week wave: three building weeks, then a deload.
- Protects your left shoulder. Every new plan is checked for movements your injury rules leave out. If one slips in, Claude fixes the plan once before it's saved.
- Reminds you before the gym and checks at 9pm whether you trained. On Sunday it asks how the week went, then builds next week's plan from your answer.
- Reads your sleep, resting heart rate, HRV, body battery and runs from your garmin-monitor app, and warns you when your recovery looks low.

## Files

| File | What it is |
|---|---|
| `bot.py` | The bot |
| `requirements.txt` | Python packages |
| `Dockerfile` | Python 3.12 slim image with Claude Code (native installer) |
| `compose.yaml` | The container, the `./data` folder and a read-only mount of garmin-monitor's data |
| `bot.env.example` | Every personal setting. Copy it to `bot.env` |
| `tests/` | Tests with a fake `claude`, a fake Telegram and sample Garmin data |

## Setup on the NAS (DXP4800 Pro, UGOS Pro)

1. **Get a Claude token.** On your own computer (with Claude Code installed), run `claude setup-token` and sign in with your Claude subscription. Copy the token, which starts with `sk-ant-oat01-`, and note today's date. The token lasts one year.
2. **Create the Telegram bot.** In Telegram, message **@BotFather**, send `/newbot`, and copy the bot token.
3. **Copy this folder to the NAS**, for example to `/volume1/docker/gym-coach` using the UGOS Files app or `scp`.
4. **Fill in `bot.env`.** Copy `bot.env.example` to `bot.env` in the same folder, then set:
   - `TELEGRAM_BOT_TOKEN`
   - `CLAUDE_CODE_OAUTH_TOKEN`
   - `CLAUDE_TOKEN_CREATED`, the date from step 1

   Leave `ALLOWED_USER_IDS` empty for now. Check the other settings too: about you, reminder times and basketball days.
5. **Check the Garmin path.** `compose.yaml` mounts `/volume1/docker/garmin-monitor/data` read-only. Change that line if garmin-monitor lives somewhere else. In `bot.env`, `GARMIN_PROFILE` must match the profile name in garmin-monitor's `config.yaml` (default `Me`).
6. **Start it.** Enable SSH in UGOS (Control Panel → Terminal), connect, and run:
   ```sh
   cd /volume1/docker/gym-coach
   sudo docker compose up -d --build
   ```
7. **Send `/whoami` to your bot.** Until your ID is allowed, it replies with your Telegram user ID.
8. **Add your ID** to `ALLOWED_USER_IDS` in `bot.env`, then run `sudo docker compose up -d` again to apply the change. A plain `restart` does not reload `bot.env`. The first ID in the list gets the reminders.
9. **Send `/plan`.** The first plan takes a minute or two. The week you send it becomes week 1.

**Back up `./data`.** It holds your plans, logs, shoulder ratings and check-ins. Add `/volume1/docker/gym-coach/data` to your NAS backup task (Sync & Backup app). `bot.env` holds your tokens, so keep a private copy somewhere safe.

## Test Claude Code inside the container

```sh
# version and sign in
sudo docker exec gym-coach-bot claude --version
sudo docker exec gym-coach-bot claude auth status

# one real call, the same way the bot makes them (from the empty /work folder)
sudo docker exec -w /work gym-coach-bot sh -c \
  'echo "Reply with five words about squats" | claude -p --output-format json --no-session-persistence --permission-mode dontAsk --model sonnet --tools ""'

# bot logs (tokens are never written to them)
sudo docker logs --tail 100 gym-coach-bot
```

In the JSON reply, `"is_error": false` and a `result` text mean everything works. `/status` in Telegram shows the version, sign-in, the last plan and the next reminders. `claude auth status` only proves a token is set, not that it's still valid, so `/status` also shows whether the last real Claude call worked.

## Commands

| Command | What it does |
|---|---|
| `/ask <question>` | Ask the coach. In a private chat, a plain message works too. It remembers your last 6 questions. |
| `/today` | Today's session from this week's plan |
| `/week` | This week's plan (on Saturday and Sunday, next week's if it's built) |
| `/plan [notes]` | Rebuild this week's plan with your notes, for example `/plan travelling Thu and Fri` |
| `/nextweek [notes]` | Build next week's plan now |
| `/log <what you did>` | Save it with today's date, for example `/log rows 22kg 3x10, floor press 14kg 3x8 felt easy`. `/log` alone lists the last 2 weeks. |
| `/done` | Mark today's session finished, then rate your left shoulder 0 to 10 with the buttons |
| `/shoulder` | Your full shoulder rating log for your physio. `/shoulder 3` saves a rating. |
| `/injury [notes]` | Show or replace your injury notes. `/injury none` clears them. |
| `/profile` | What the coach knows about you, including your shoulder trend and Garmin data |
| `/status` | Claude Code version, sign-in, the last plan built and the next reminders |
| `/reset` | Clear the chat memory |
| `/whoami` | Your Telegram user ID |

Shoulder ratings: 0 means no pain and 10 means the worst pain, so a rising trend means the shoulder is getting worse.

## Reminders (Singapore time, set in `bot.env`)

| When | What |
|---|---|
| Mon to Thu 17:30, Fri 17:00 | Today's session, with a warning if Garmin shows poor recovery |
| 21:00 on training days | "Did you train today?" with Done and Skipped buttons, unless you already sent `/done`. **Skipped** rewrites the rest of the week so you don't double up. The old version is kept in `data/plans/history/`. |
| Sunday 18:00 | Check-in: energy, soreness, shoulder. Your reply to that message, or your next message before 20:00, is saved. |
| Sunday 20:00 | Builds next week's plan from your check-in (or without one) and sends an overview with your shoulder trend |
| Daily 10:00 | From 30 days before your Claude token expires: a reminder to run `claude setup-token` again |

If the bot was off at a reminder time, it catches up when it starts. It sends a missed Sunday check-in, or builds next week's or this week's missing plan.

## How it works

- **Claude calls.** Each call runs `claude -p` as a background process from the empty `/work` folder:
  - Every call uses `--output-format json --no-session-persistence --permission-mode dontAsk --model <model>`.
  - The system prompt comes from `--system-prompt-file` and your message goes in on stdin.
  - Questions add `--tools WebSearch --allowedTools WebSearch --max-turns 10` and time out after 4 minutes.
  - Plans add `--tools "" --max-turns 3` and time out after 10 minutes.
  - `--bare` is never used, because bare mode ignores the subscription token.
- **System prompt.** Your coach prompt is filled from `bot.env`. The bot then adds today's date, your injury notes, this week's plan, sessions done or skipped, 14 days of logs and shoulder ratings, the latest check-in and 7 days of Garmin data.
- **Plans.** Each plan is saved as `data/plans/<Monday>.md`, with a `.json` file next to it that records the week number, equipment, effort and any warnings.
  - Each day starts with a line like `📅 Monday: Push`, which is how `/today` finds the right day.
  - The split chosen in week 1 is saved and sent back to Claude every week.
  - Exercises from the two previous weeks are listed so none repeat. Rehab exercises may repeat.
- **Injury check.** The plan checker uses `INJURY_BLOCKED_MOVEMENTS`. Empty that setting once your physio clears you.
- **Garmin.** garmin-monitor's `monitor.db` is opened read-only and never written to.
- **Log safety.** Tokens are never logged. The `httpx` logger is set to WARNING because it prints the bot token in request URLs, and every log line is filtered for secrets.

## Everyday maintenance

- **Renew the Claude token** once a year (the bot reminds you):
  1. Run `claude setup-token`.
  2. Update `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_TOKEN_CREATED` in `bot.env`.
  3. Run `sudo docker compose up -d`.
- **Update the bot:** copy the new files over the old ones, then run `sudo docker compose up -d --build`.
- **API key instead of the subscription:** set `ANTHROPIC_API_KEY` and leave `CLAUDE_CODE_OAUTH_TOKEN` empty.

## Troubleshooting

- **The bot says the token needs renewing:** the Claude token expired or is wrong. See "Renew the Claude token" above.
- **No reply at all:** check that your ID is in `ALLOWED_USER_IDS`, then look at `sudo docker logs gym-coach-bot`.
- **Garmin shows "not available" in `/status`:** check the mount path in `compose.yaml`, that `monitor.db` exists in that folder, and that `GARMIN_PROFILE` matches.

## Run the tests

The tests need no real tokens and no network. A fake `claude` in `tests/bin`, a fake Telegram API and sample Garmin data stand in for the real services.

```sh
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest
```

To try the Garmin summary with sample data: `python tests/sample_garmin.py /tmp/monitor.db 2026-09-30`.
