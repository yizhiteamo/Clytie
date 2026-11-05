#!/bin/sh
cd "$(dirname "$0")/.." || exit 1
export PYTHONUTF8=1
if [ -x .venv/bin/python ]; then
    exec .venv/bin/python -m clytie supervisor start
fi
exec python3 -m clytie supervisor start
