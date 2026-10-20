#!/usr/bin/env sh
# Starts the Discord bot and the stats website (one process). Run: ./start.sh
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "First run: setting up Python packages..."
    python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt
fi
exec .venv/bin/python bot.py
