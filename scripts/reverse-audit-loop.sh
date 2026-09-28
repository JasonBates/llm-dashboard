#!/bin/zsh
# Runs the explicitly initialized audit until its fixed deadline or budget stop.
set -euo pipefail
source "$HOME/.zshenv"
cd "$(dirname "$0")/.."
umask 077
exec "$HOME/.local/bin/secrets" run TYPESAFE_API_KEY -- uv run python reverse_audit.py run
