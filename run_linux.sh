#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "[LAPO] Initializing Linux launcher..."

# 1. Detect Python 3.10+
PY_BIN=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            PY_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PY_BIN" ]; then
    echo "[ERROR] Python 3.10 or later is required but was not found in PATH." >&2
    exit 1
fi

# 2. Create local virtual environment if missing
if [ ! -x ".venv/bin/python" ]; then
    echo "[LAPO] Creating virtual environment in .venv ..."
    "$PY_BIN" -m venv .venv
fi

VENV_PY=".venv/bin/python"

# 3. Install / verify dependencies
if ! "$VENV_PY" -c "import requests, jsonschema" 2>/dev/null; then
    echo "[LAPO] Installing requirements from requirements.txt ..."
    "$VENV_PY" -m pip install --upgrade pip
    "$VENV_PY" -m pip install -r requirements.txt
fi

# 4. Verify imports
"$VENV_PY" -c "import lapo, requests, jsonschema"

# 5. If headless and no args passed, show CLI help instead of crashing on missing DISPLAY
if [ $# -eq 0 ] && [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "[LAPO] Headless environment detected (no DISPLAY). Showing CLI usage:"
    exec "$VENV_PY" lapo_app.py --help
fi

echo "[LAPO] Starting Local AI Project Orchestrator..."
exec "$VENV_PY" lapo_app.py "$@"