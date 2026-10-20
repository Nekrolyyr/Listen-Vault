#!/usr/bin/env bash
# Creates an administrator account, or makes an existing account one, and sets its password. The first
# account of an existing library is claimed this way too (see docs/multi-user-plan.md).
# Usage: scripts/make-admin.sh you@example.org
set -euo pipefail
cd "$(dirname "$0")/.."
[ $# -eq 1 ] || { echo "Usage: $0 <email>" >&2; exit 2; }
# No -T: the password is read from the terminal.
docker compose -f docker-compose.yml exec api python -m listen.cli make-admin "$1"
