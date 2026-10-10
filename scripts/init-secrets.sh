#!/usr/bin/env bash
# Creates the host folders, Authelia secrets and a .env with random secrets.
# Safe to re-run: existing secrets and .env values are never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env
set -a; . ./.env; set +a

CONFIG_DIR=${LISTEN_CONFIG_DIR:-/vault/configs/listen-vault}
DATA_DIR=${LISTEN_DATA_DIR:-/vault/listen-vault}

for d in "$CONFIG_DIR/authelia/secrets" "$CONFIG_DIR/postgres" "$CONFIG_DIR/models" "$DATA_DIR"; do
  mkdir -p "$d" || { echo "Cannot create $d. Run once: sudo mkdir -p $CONFIG_DIR $DATA_DIR && sudo chown -R $(id -un): $CONFIG_DIR $DATA_DIR" >&2; exit 1; }
done
chmod 700 "$CONFIG_DIR/authelia/secrets"

rand() { head -c 48 /dev/urandom | base64 | tr -d '\n/+=' | head -c 64; }

for s in jwt session storage; do
  f="$CONFIG_DIR/authelia/secrets/$s"
  [ -s "$f" ] || { rand > "$f"; chmod 600 "$f"; echo "created $f"; }
done

set_env() {  # set_env KEY — fills KEY in .env only when empty
  if grep -qE "^$1=$" .env; then
    sed -i "s|^$1=$|$1=$(rand)|" .env
    echo "generated $1 in .env"
  fi
}
set_env LISTEN_PROXY_SECRET
set_env POSTGRES_PASSWORD
chmod 600 .env
echo "Done. Next: scripts/add-user.sh, then scripts/fetch-models.sh"
