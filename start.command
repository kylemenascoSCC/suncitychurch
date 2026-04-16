#!/bin/bash
# Double-click this file to start the SLM voting app.
# It will install dependencies on the first run, then boot the server
# and open your browser.

set -e
cd "$(dirname "$0")"

# Pick a python
if command -v python3 >/dev/null 2>&1; then
  PY=python3
elif command -v python >/dev/null 2>&1; then
  PY=python
else
  echo ""
  echo "❌ Python isn't installed on this Mac."
  echo "   Install it from https://www.python.org/downloads/ and try again."
  read -p "Press Enter to close..."
  exit 1
fi

# Create & use a local virtualenv so we don't pollute system python
if [ ! -d ".venv" ]; then
  echo "→ First-time setup: creating a Python environment..."
  $PY -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

# Install deps (quiet, only when needed)
echo "→ Making sure dependencies are installed..."
pip install --quiet --disable-pip-version-check -r requirements.txt

# Load .env so ASANA_TOKEN etc. are exported
if [ ! -f ".env" ]; then
  echo "❌ .env file is missing. Aborting."
  read -p "Press Enter to close..."
  exit 1
fi
set -a
# shellcheck disable=SC1091
source .env
set +a

PORT="${PORT:-5000}"

echo ""
echo "────────────────────────────────────────────────────"
echo "  SLM Voting is starting on http://localhost:${PORT}"
echo ""
echo "  Admin:  http://localhost:${PORT}/admin/login"
echo "  Voters: http://localhost:${PORT}/"
echo ""
echo "  Keep this window open while the app is running."
echo "  Close this window (or press Ctrl+C) to stop it."
echo "────────────────────────────────────────────────────"
echo ""

# Open browser to admin after a short delay
( sleep 2 && open "http://localhost:${PORT}/admin/login" ) &

exec python app.py
