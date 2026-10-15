# Project guidance

This repository is a Chinese-language computer component price monitor. Keep monetary values as Decimal-derived strings; never silently compare different currencies or present fixture prices as live data.

Use the existing checkout. Each Codex cloud task already has an isolated environment; do not create a worktree unless the user asks.

Setup: `bash scripts/install.sh` (frozen uv lock). Run: `bash scripts/start.sh` (loopback, one worker). Checks: `.venv/bin/python -m unittest discover -s tests -v` and a local request to `/api/health`.

Keep credentials, `.env`, SQLite databases, logs, and virtual environments out of Git. Do not disable TLS verification or bypass a retailer's access controls. Report actual site compatibility separately from fixture-based tests.

The UI is under `static/`; backend, persistence, and extraction are under `pricemonitor/`. The scheduled collector runs only while the application process is alive. External notification delivery and an always-on deployment need their own explicit setup.
