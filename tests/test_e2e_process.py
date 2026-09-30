"""End to end: the real `python bot.py` process, long polling a stand-in Bot API.

This runs main(), run_polling, the job queue, the command menu, the allow-list, a fake
`claude` on PATH and sample Garmin data, then stops the bot with SIGTERM like Docker does.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import e2e_scenario
import sample_garmin
from fake_bot_api import FakeBotAPI

ROOT = Path(__file__).resolve().parent.parent


def test_real_bot_process(tmp_path):
    api = FakeBotAPI(e2e_scenario.TOKEN)
    port = api.start()
    garmin = tmp_path / "garmin"
    garmin.mkdir()
    today = datetime.now(ZoneInfo("Asia/Singapore")).date()
    sample_garmin.build(str(garmin / "monitor.db"), today)
    env = {
        "PATH": f"{ROOT / 'tests' / 'bin'}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "TELEGRAM_BOT_TOKEN": e2e_scenario.TOKEN,
        "TELEGRAM_BASE_URL": f"http://127.0.0.1:{port}",
        "ALLOWED_USER_IDS": str(e2e_scenario.OWNER),
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-E2EFAKEE2EFAKEE2EFAKE",
        "CLAUDE_TOKEN_CREATED": "2026-01-15",
        "DATA_DIR": str(tmp_path / "data"),
        "CLAUDE_WORK_DIR": str(tmp_path / "work"),
        "GARMIN_DB": str(garmin / "monitor.db"),
        "FAKE_CLAUDE_LOG": str(tmp_path / "claude.jsonl"),
        "TZ": "Asia/Singapore",
    }
    log_path = tmp_path / "bot.log"
    with log_path.open("w") as log:
        proc = subprocess.Popen([sys.executable, str(ROOT / "bot.py")], env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        passed = e2e_scenario.run(api)
        assert len(passed) >= 12
        proc.send_signal(signal.SIGTERM)  # what `docker stop` sends
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        api.stop()
    output = log_path.read_text()
    assert "Coach bot ready" in output
    assert e2e_scenario.TOKEN not in output and "E2EFAKE" not in output
    assert "Traceback" not in output, output[-2000:]
    assert (tmp_path / "data" / "plans").is_dir() and list((tmp_path / "work").iterdir()) == []
