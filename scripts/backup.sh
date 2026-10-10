#!/usr/bin/env bash
# Back up listen-vault: Postgres dump, Authelia state (users + passkey registrations), and documents.
# Usage: scripts/backup.sh [target-dir]   (default /vault/backups/listen-vault; keeps the last 7 runs)
# Audio files are included; skip them with NO_AUDIO=1 (they can be regenerated from the documents).
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

CONFIG_DIR=${LISTEN_CONFIG_DIR:-/vault/configs/listen-vault}
DATA_DIR=${LISTEN_DATA_DIR:-/vault/listen-vault}
TARGET=${1:-/vault/backups/listen-vault}
RUN="$TARGET/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$RUN"

echo "database -> $RUN/db.sql.gz"
docker compose exec -T postgres pg_dump -U listen -d listen --clean --if-exists | gzip > "$RUN/db.sql.gz"

echo "authelia state -> $RUN/authelia.tar.gz"
# Stop Authelia for the copy so the sqlite file is consistent (logins pause for a second or two).
docker compose stop authelia >/dev/null
cp "$CONFIG_DIR/authelia/db.sqlite3" "$CONFIG_DIR/authelia/db.backup.sqlite3"
docker compose start authelia >/dev/null
tar -czf "$RUN/authelia.tar.gz" -C "$CONFIG_DIR" authelia/users.yml authelia/db.backup.sqlite3 authelia/secrets
rm -f "$CONFIG_DIR/authelia/db.backup.sqlite3"

echo "documents -> $RUN/documents.tar"
EXCLUDE=()
[ "${NO_AUDIO:-0}" = 1 ] && EXCLUDE=(--exclude='docs/*/audio')
tar -cf "$RUN/documents.tar" "${EXCLUDE[@]}" -C "$DATA_DIR" docs

chmod -R go-rwx "$RUN"
ls -1dt "$TARGET"/*/ | tail -n +8 | xargs -r rm -rf
du -sh "$RUN"
