#!/bin/bash
# Starts the bot with settings from .env; caffeinate keeps the Mac from idle-sleeping while it runs.
cd "$(dirname "$0")"
set -a; source .env; set +a
exec /usr/bin/caffeinate -i .venv/bin/python bot.py
