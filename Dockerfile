# Telegram gym coach bot with Claude Code (claude -p) for the AI.
FROM python:3.12-slim

# Pin Claude Code with --build-arg CLAUDE_CODE_VERSION=2.1.285 (empty = the installer's default).
ARG CLAUDE_CODE_VERSION=
# The user the bot runs as. PUID/PGID in bot.env can change it at start up.
ARG PUID=1000
ARG PGID=1000

ENV TZ=Asia/Singapore \
    DISABLE_AUTOUPDATER=1 \
    PATH=/home/coach/.local/bin:/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd -g "$PGID" coach && useradd -m -u "$PUID" -g "$PGID" -s /bin/bash coach

# Native Claude Code installer, for the coach user. It signs in at run time from
# CLAUDE_CODE_OAUTH_TOKEN (or ANTHROPIC_API_KEY) in bot.env, so no login happens here.
USER coach
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} && claude --version
USER root

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py entrypoint.sh ./
# /work stays empty: Claude Code runs from there so no project files are loaded.
RUN mkdir -p /data /work && chmod 755 entrypoint.sh

# Healthy while the bot's event loop is alive (it touches a heartbeat file every minute).
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 CMD ["python", "/app/bot.py", "--health"]
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "bot.py"]
