#!/usr/bin/env bash
# Starts the bot. Keep this window open; press Ctrl+C to stop.
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ] || [ ! -f .env ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
echo "Starting the bot. Keep this window open, and press Ctrl+C to stop it."
if command -v caffeinate >/dev/null 2>&1; then
  # macOS: stop the Mac from sleeping while the bot runs (closing the lid still sleeps it).
  exec caffeinate -i .venv/bin/python -m bot
fi
exec .venv/bin/python -m bot
