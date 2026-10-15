#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="${UV_CACHE_DIR:-$PWD/.cache/uv}"
uv sync --frozen
mkdir -p data logs
