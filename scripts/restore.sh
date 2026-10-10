#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh. Usage: scripts/restore.sh /vault/backups/listen-vault/<run>
# Stops the stack's app services, restores database, Authelia state and documents, then starts it again.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
RUN=${1:?backup run directory}
CONFIG_DIR=${LISTEN_CONFIG_DIR:-/vault/configs/listen-vault}
DATA_DIR=${LISTEN_DATA_DIR:-/vault/listen-vault}

read -r -p "This replaces the current library and login data with $RUN. Continue? [y/N] " ok
[ "$ok" = y ] || exit 0

docker compose stop api worker authelia
docker compose up -d postgres
gunzip -c "$RUN/db.sql.gz" | docker compose exec -T postgres psql -q -U listen -d listen
tar -xzf "$RUN/authelia.tar.gz" -C "$CONFIG_DIR"
mv "$CONFIG_DIR/authelia/db.backup.sqlite3" "$CONFIG_DIR/authelia/db.sqlite3"
rm -rf "$DATA_DIR/docs" && tar -xf "$RUN/documents.tar" -C "$DATA_DIR"
docker compose up -d
echo "Restored from $RUN"
