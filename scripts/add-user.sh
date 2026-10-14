#!/usr/bin/env bash
# Sets the password of the web app login (LISTEN_USER in .env). Writes config/caddy/users.caddy, which
# holds only a bcrypt hash made by Caddy's own hash command. The password itself is not stored.
# Usage: scripts/add-user.sh
set -euo pipefail
cd "$(dirname "$0")/.."
[ -f .env ] || { echo "No .env yet. Run scripts/init-secrets.sh first." >&2; exit 1; }
set -a; . ./.env; set +a

USER_NAME=${LISTEN_USER:?LISTEN_USER is empty in .env}
USERS=config/caddy/users.caddy

if [ -f "$USERS" ]; then
  read -r -p "$USERS exists. Replace it? [y/N] " ok
  [ "$ok" = y ] || exit 0
fi

read -r -s -p "Password for $USER_NAME: " PW; echo
read -r -s -p "Repeat password: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "Passwords differ" >&2; exit 1; }
[ ${#PW} -ge 12 ] || { echo "Use at least 12 characters" >&2; exit 1; }

HASH=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$PW")
[ -n "$HASH" ] || { echo "Hashing failed" >&2; exit 1; }

umask 077
cat > "$USERS" <<EOF
basic_auth {
  $USER_NAME $HASH
}
EOF
echo "Wrote $USERS. Start the stack with: docker compose up -d --build"
