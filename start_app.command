#!/bin/zsh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

SHARED_VENV="../foreshadowing-video-search/.venv"
if [ -x "$SHARED_VENV/bin/python" ]; then
  PYTHON_BIN="$SHARED_VENV/bin/python"
else
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  PYTHON_BIN=".venv/bin/python"
fi

exec "$PYTHON_BIN" -m streamlit run app.py --browser.gatherUsageStats false
