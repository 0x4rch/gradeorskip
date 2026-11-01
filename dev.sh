#!/usr/bin/env bash
# Start GradeOrSkip locally. Usage: ./dev.sh [port]   (default 8790)
set -euo pipefail
cd "$(dirname "$0")"
PORT="${1:-${PORT:-8790}}"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/uvicorn gradeorskip.app:app --host 127.0.0.1 --port "$PORT" ${RELOAD:+--reload}
