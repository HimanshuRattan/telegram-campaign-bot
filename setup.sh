#!/usr/bin/env bash
# One-time setup: installs dependencies, asks for your credentials, and checks them.
# Safe to run again; it skips anything that's already set.
set -euo pipefail
cd "$(dirname "$0")"

PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and run ./setup.sh again."
  exit 1
fi

echo "Installing dependencies (takes a minute or two)..."
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

[ -f .env ] || cp .env.example .env

current() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }

set_env() {
  .venv/bin/python - "$1" "$2" <<'EOF'
import pathlib, re, sys
key, value = sys.argv[1], sys.argv[2].strip()
env = pathlib.Path(".env")
text = env.read_text()
line = f"{key}={value}"
if re.search(rf"^{key}=", text, flags=re.M):
    text = re.sub(rf"^{key}=.*$", lambda _: line, text, flags=re.M)
else:
    text = text.rstrip("\n") + f"\n{line}\n"
env.write_text(text)
EOF
}

# Dragging a file into Terminal pastes its path with spaces escaped and a trailing space.
clean_path() {
  local p="$1"
  p="${p%"${p##*[![:space:]]}"}"
  p="${p//\\ / }"
  p="${p#[\"\']}"
  p="${p%[\"\']}"
  printf '%s' "$p"
}

ask() {
  if [ -n "$(current "$1")" ]; then
    echo "  $1 is already set."
    return
  fi
  read -r -p "$2: " value
  set_env "$1" "$value"
}

echo
echo "Now your credentials. Paste each one and press Enter."
echo
ask TELEGRAM_BOT_TOKEN "Telegram bot token (from @BotFather)"
if [ -z "$(current OPENROUTER_API_KEY)" ] && [ -z "$(current ANTHROPIC_API_KEY)" ]; then
  ask OPENROUTER_API_KEY "OpenRouter API key (from openrouter.ai/keys)"
fi

if [ ! -f service-account.json ]; then
  echo
  echo "Google service account key: run ./google-setup.sh to create one automatically,"
  read -r -p "or drag the downloaded .json key file here and press Enter: " key_path
  key_path=$(clean_path "$key_path")
  cp "$key_path" service-account.json
  chmod 600 service-account.json
fi
email=$(.venv/bin/python -c 'import json; print(json.load(open("service-account.json"))["client_email"])')
echo
echo "  The bot's Google account is: $email"
echo "  Share your Google Sheet and docs with that email as Editor (untick 'Notify people')."

if [ -z "$(current GOOGLE_SHEET_ID)" ]; then
  echo
  read -r -p "Paste your Google Sheet's link: " sheet_url
  sheet_id=$(echo "$sheet_url" | sed -E 's#.*/d/([a-zA-Z0-9_-]+).*#\1#')
  set_env GOOGLE_SHEET_ID "$sheet_id"
fi

echo
.venv/bin/python -m bot.check || {
  echo "Fix the items marked [fix] above, then run ./setup.sh again."
  echo "('No inbox group linked yet' is expected before the first start.)"
  exit 0
}
