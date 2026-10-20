#!/usr/bin/env bash
# First-time setup: creates .env from .env.template, generates the secrets and creates the local folders.
# Safe to re-run: values that are already set are never overwritten.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.template .env
  echo "created .env"
fi
chmod 600 .env

set_env() {  # set_env KEY VALUE: fills KEY in .env only when it is empty
  if grep -qE "^$1=$" .env; then
    sed -i "s|^$1=$|$1=$2|" .env
    echo "set $1 in .env"
  fi
}
rand() { od -An -vtx1 -N24 /dev/urandom | tr -d ' \n'; }

set_env LISTEN_PROXY_SECRET "$(rand)"
set_env POSTGRES_PASSWORD "$(rand)"

# Created here, not by docker, so they belong to you and not to root.
mkdir -p data/library data/postgres models config/caddy
echo "Done. Next: scripts/make-admin.sh you@example.org (the first administrator), then scripts/fetch-models.sh"
