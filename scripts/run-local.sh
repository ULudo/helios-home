#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

export HELIOS_LOCAL_SCAN_ENABLED="${HELIOS_LOCAL_SCAN_ENABLED:-true}"
export HELIOS_BROADCAST_DISCOVERY_ENABLED="${HELIOS_BROADCAST_DISCOVERY_ENABLED:-true}"
export HELIOS_MODBUS_LIVE_ENABLED="${HELIOS_MODBUS_LIVE_ENABLED:-true}"
export VITE_API_BASE="${VITE_API_BASE:-http://${BACKEND_HOST}:${BACKEND_PORT}/api/v1}"

cleanup() {
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill "$BACKEND_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

cd "$ROOT"
HOST="$BACKEND_HOST" PORT="$BACKEND_PORT" ./scripts/run-backend.sh &
BACKEND_PID=$!

echo "Helios backend:  http://${BACKEND_HOST}:${BACKEND_PORT}"
echo "Helios frontend: http://${FRONTEND_HOST}:${FRONTEND_PORT}"
echo "Press Ctrl+C to stop both processes."

HOST="$FRONTEND_HOST" PORT="$FRONTEND_PORT" ./scripts/run-frontend.sh
