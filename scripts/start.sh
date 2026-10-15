#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn pricemonitor.app:app --host 127.0.0.1 --port "${SHOP_PORT:-8000}" --workers 1
