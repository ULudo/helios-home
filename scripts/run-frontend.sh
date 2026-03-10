#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-5173}"
NODE_BIN="$ROOT/.tooling/node-v24.14.0-linux-x64/bin"

export PATH="$NODE_BIN:$PATH"

cd "$ROOT/apps/web-ui"
exec npm run dev -- --host "$HOST" --port "$PORT" "$@"

