# Telegram gym coach bot with Claude Code (claude -p) for the AI.
FROM python:3.12-slim

ENV TZ=Asia/Singapore \
    DISABLE_AUTOUPDATER=1 \
    PATH=/root/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates bash tzdata \
    && rm -rf /var/lib/apt/lists/*

# Native Claude Code installer. It signs in at run time from CLAUDE_CODE_OAUTH_TOKEN
# (or ANTHROPIC_API_KEY) in bot.env, so no login happens during the build.
RUN curl -fsSL https://claude.ai/install.sh | bash && claude --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# /work stays empty: Claude Code runs from there so no project files are loaded.
RUN mkdir -p /data /work

CMD ["python", "bot.py"]
