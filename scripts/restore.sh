#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh. Usage: scripts/restore.sh backups/<run>
# Stops the api and worker, restores the database and the library folder, then starts everything again.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.yml"
RUN=${1:?backup run directory}

read -r -p "This replaces the current library and database with $RUN. Continue? [y/N] " ok
[ "$ok" = y ] || exit 0

$COMPOSE stop api worker
$COMPOSE up -d postgres
gunzip -c "$RUN/db.sql.gz" | $COMPOSE exec -T postgres psql -q -U listen -d listen
rm -rf data/library/docs && tar -xf "$RUN/library.tar" -C data/library
$COMPOSE up -d
echo "Restored from $RUN"
