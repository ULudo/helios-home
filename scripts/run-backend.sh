#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "$ROOT"
exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app --app-dir "$ROOT/apps/edge-api" --host "$HOST" --port "$PORT" "$@"

