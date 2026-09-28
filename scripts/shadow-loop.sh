#!/bin/bash
# Supervised entry point for the Inbox Zero shadow evaluation loop.
#
# launchd runs this with KeepAlive (see com.jasonbates.inbox-zero-shadow.plist.template).
# The loop itself polls every five minutes; this wrapper only fixes the working
# directory, PATH, and the TypeSafe key, which comes from the Keychain via `secrets`.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
# The TypeSafe key comes from the Keychain via `secrets` (1Password holds the master copy).
export TYPESAFE_API_KEY="${TYPESAFE_API_KEY:-$("$HOME/.local/bin/secrets" get TYPESAFE_API_KEY)}"
: "${TYPESAFE_API_KEY:?TYPESAFE_API_KEY missing: run secrets sync}"
echo "$(date -u +%FT%TZ) shadow-loop starting"
exec uv run shadow run --interval "${SHADOW_INTERVAL:-300}"
