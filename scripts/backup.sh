#!/usr/bin/env bash
# Back up the database and the library folder (documents, text and audio).
# Usage: scripts/backup.sh [target-dir]   (default ./backups; keeps the last 7 runs)
# Skip the audio with NO_AUDIO=1; it can be regenerated from the documents.
set -euo pipefail
cd "$(dirname "$0")/.."
COMPOSE="docker compose"

TARGET=${1:-backups}
RUN="$TARGET/$(date +%Y-%m-%d_%H%M%S)"
mkdir -p "$RUN"

echo "database -> $RUN/db.sql.gz"
$COMPOSE exec -T postgres pg_dump -U listen -d listen --clean --if-exists | gzip > "$RUN/db.sql.gz"

echo "library -> $RUN/library.tar"
EXCLUDE=()
[ "${NO_AUDIO:-0}" = 1 ] && EXCLUDE=(--exclude='docs/*/audio')
tar -cf "$RUN/library.tar" "${EXCLUDE[@]}" -C data/library docs

chmod -R go-rwx "$RUN"
ls -1dt "$TARGET"/*/ | tail -n +8 | xargs -r rm -rf
du -sh "$RUN"
