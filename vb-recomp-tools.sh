#!/bin/sh
# VBRecomp Tools launcher.
#
# Uses the app-local virtual environment (.venv next to this script)
# when present, bootstrapping it on first run. The import check runs
# BEFORE the venv is trusted: a venv whose interpreter exists but
# whose packages never installed (an earlier interrupted first run)
# would otherwise be "used" forever and the GUI would fail with
# ModuleNotFoundError on every launch.
set -e
cd "$(dirname "$0")"
VENV=".venv"
PY="$VENV/bin/python"

import_ok() {
    [ -x "$PY" ] && "$PY" -c "import PySide6, PIL" 2>/dev/null
}

if ! import_ok; then
    SYS_PY="$(command -v python3 || true)"
    if [ -z "$SYS_PY" ]; then
        echo "python3 not found - install Python 3.10+ first." >&2
        exit 1
    fi
    if [ ! -x "$PY" ]; then
        "$SYS_PY" -m venv "$VENV" || {
            echo "Could not create the virtual environment." >&2
            echo "On Debian/Ubuntu: sudo apt install -y python3-venv" >&2
            exit 1
        }
    fi
    "$PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
    "$PY" -m pip install -r requirements.txt
    if ! import_ok; then
        echo "Dependencies still not importable after install -" >&2
        echo "check the pip output above and your network connection." >&2
        exit 1
    fi
fi
exec "$PY" app/main.py "$@"
