#!/bin/bash
# Daily backup of the self-hosted inbox-zero Postgres database.
#
# The database runs inside the `inbox-zero-services-db-1` Docker container
# (Postgres 16, exposed on 127.0.0.1:5433). We exec into the container to
# run pg_dump and stream the result through gzip on the host.
#
# Driven by ~/Library/LaunchAgents/com.jasonbates.inbox-zero-backup.plist
# (template at scripts/com.jasonbates.inbox-zero-backup.plist.template).
#
# Output: ~/backups/inbox-zero/inbox-zero-YYYY-MM-DD-HHMM.sql.gz
# Retention: deletes anything older than 30 days.

set -euo pipefail

# launchd does not inherit a shell PATH; pin the tools we need.
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

BACKUP_DIR="$HOME/backups/inbox-zero"
CONTAINER="inbox-zero-services-db-1"
DB_USER="postgres"
DB_NAME="inboxzero"
DB_PASS="password"
DATE=$(date +%Y-%m-%d-%H%M)
TARGET="$BACKUP_DIR/inbox-zero-$DATE.sql.gz"

mkdir -p "$BACKUP_DIR"

# Fail loudly if the container isn't running rather than producing an empty dump.
if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "[$(date)] ERROR: container $CONTAINER is not running" >&2
  exit 1
fi

# --clean --if-exists so the dump can restore cleanly into a fresh DB.
docker exec -e PGPASSWORD="$DB_PASS" "$CONTAINER" \
  pg_dump -U "$DB_USER" -d "$DB_NAME" --clean --if-exists \
  | gzip -9 > "$TARGET"

# Verify the dump is non-trivial. Schema-only dumps of inbox-zero are ~30KB
# gzipped; a real dump with data is several MB. 10KB is a safe floor.
size=$(stat -f%z "$TARGET")
if [ "$size" -lt 10240 ]; then
  echo "[$(date)] ERROR: backup unexpectedly small ($size bytes): $TARGET" >&2
  exit 1
fi

# Retention: delete dumps older than 30 days.
find "$BACKUP_DIR" -name "inbox-zero-*.sql.gz" -mtime +30 -delete 2>/dev/null || true

echo "[$(date)] backup ok: $TARGET ($size bytes)"
