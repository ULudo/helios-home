#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NODE_BIN="$ROOT/.tooling/node-v24.14.0-linux-x64/bin"

export PATH="$NODE_BIN:$PATH"

cd "$ROOT/apps/web-ui"
exec npm run build "$@"

