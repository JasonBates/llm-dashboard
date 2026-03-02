#!/bin/bash
# Resolve the real path of this script (follows symlinks/aliases)
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0" 2>/dev/null || echo "$0")")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure uv is on PATH
export PATH="$HOME/.local/bin:$PATH"

# Kill any existing instance on port 8765
lsof -ti:8765 | xargs kill 2>/dev/null
sleep 1

uv run server.py --port 8765 &
SERVER_PID=$!

sleep 2
open http://127.0.0.1:8765

# Keep terminal open while server runs
wait $SERVER_PID
