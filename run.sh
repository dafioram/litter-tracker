#!/usr/bin/env bash
# Runs Litter Tracker without Docker, using the settings in .env if it exists.
# Install dependencies first with ./python_install.sh
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
    set -a
    . ./.env
    set +a
fi

exec "${PYTHON:-python3}" app.py
