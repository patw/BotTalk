#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Make sure uv (user-local) is on PATH
export PATH="$HOME/.local/bin:$PATH"

# Load .env so the app sees BOTTALK_* variables (host/port are read before
# the app's own .env loader runs, so export them here)
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source <(grep -v '^\s*#' "$SCRIPT_DIR/.env" | grep -v '^\s*$' | sed 's/[[:space:]]*#.*//')
    set +o allexport
fi

HOST="${BOTTALK_HOST:-127.0.0.1}"
PORT="${BOTTALK_PORT:-8000}"

echo "Starting BotTalk on $HOST:$PORT"

exec uv run main.py
