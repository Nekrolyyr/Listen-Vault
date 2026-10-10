#!/usr/bin/env bash
# Writes the Authelia users file with the single listen-vault user.
# Usage: scripts/add-user.sh [email]    (prompts for the password; it is only stored as an argon2 hash)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

CONFIG_DIR=${LISTEN_CONFIG_DIR:-/vault/configs/listen-vault}
USERS="$CONFIG_DIR/authelia/users.yml"
USER_NAME=${LISTEN_USER:?LISTEN_USER missing in .env}
EMAIL=${1:-$USER_NAME@listen.vault.akiba-z.ch}

if [ -f "$USERS" ]; then
  read -r -p "$USERS exists. Replace it? [y/N] " ok
  [ "$ok" = y ] || exit 0
fi

read -r -s -p "Password for $USER_NAME: " PW; echo
read -r -s -p "Repeat password: " PW2; echo
[ "$PW" = "$PW2" ] || { echo "Passwords differ" >&2; exit 1; }
[ ${#PW} -ge 12 ] || { echo "Use at least 12 characters" >&2; exit 1; }

if command -v authelia >/dev/null; then
  HASH=$(authelia crypto hash generate argon2 --password "$PW" | sed -n 's/^Digest: //p')
else
  HASH=$(docker run --rm authelia/authelia:4.39.28 authelia crypto hash generate argon2 --password "$PW" | sed -n 's/^Digest: //p')
fi
[ -n "$HASH" ] || { echo "Hashing failed" >&2; exit 1; }

umask 077
cat > "$USERS" <<EOF
users:
  $USER_NAME:
    disabled: false
    displayname: "$USER_NAME"
    password: "$HASH"
    email: "$EMAIL"
    groups: []
EOF
echo "Wrote $USERS"
