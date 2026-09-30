---
tags: [active]
updated: 2026-09-30
---
# Changelog
## 2026-09-30
- test: on Windows, `tests/conftest.py` exits with the Docker test command (Windows can't exec the shebang stub `tests/bin/claude` → 117 bogus failures). README "Run the tests" documents it.
- fix: `.gitattributes` forces LF (Windows checkout made entrypoint.sh/tests/bin/claude CRLF → 115 test failures, broken image).
- feat: NAS Doctor runbook in README; `pull_policy: build` in compose.
- docs: vault created.
