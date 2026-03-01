#!/bin/bash
cd "$(dirname "$0")"
~/.local/bin/uv run server.py --port 8765 &
sleep 2
open http://127.0.0.1:8765
wait
