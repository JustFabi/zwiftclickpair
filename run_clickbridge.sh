#!/usr/bin/env bash
# Starts the OpenBikeControl TCP server (the actual bridge work).
# Keep this window open while riding — or install the systemd unit instead:
#   sudo install -m 644 clickbridge.service /etc/systemd/system/
#   sudo systemctl enable --now clickbridge
cd "$(dirname "$0")"

if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
else
    PY=python3
fi
exec "$PY" -m clickbridge "$@"
