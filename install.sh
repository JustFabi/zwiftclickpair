#!/usr/bin/env bash
# First-time setup (Linux / Raspberry Pi): venv + Python deps + panel deps.
# Needs: python3 (3.12+), php 8.2+, composer. Sudo needed for the systemd unit.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

# Panel: app key, env, database.
if [ ! -f panel/.env ]; then
    cp panel/.env.example panel/.env
fi
(cd panel && composer install --no-interaction && php artisan key:generate --force)
touch panel/database/database.sqlite
(cd panel && php artisan migrate --force)

# Apache vhost + systemd unit live in setup_vhost.sh (needs sudo):
#   sudo ./setup_vhost.sh
echo "Setup complete. Start the bridge with:  ./run_clickbridge.sh"
