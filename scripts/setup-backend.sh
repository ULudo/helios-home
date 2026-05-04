#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
PYTHON_VERSION="$("$PYTHON" - <<'PY'
import sys
print(f"{sys.version_info.major}.{sys.version_info.minor}")
PY
)"
TARGET="$ROOT/.venv/lib/python${PYTHON_VERSION}/site-packages"
EXTRAS="${HELIOS_BACKEND_EXTRAS:-[dev]}"

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi

mkdir -p "$TARGET"

"$PYTHON" -m pip install \
  --break-system-packages \
  --upgrade \
  --target "$TARGET" \
  "$ROOT$EXTRAS"
