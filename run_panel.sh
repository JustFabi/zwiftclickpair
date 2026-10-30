#!/usr/bin/env bash
# Panel convenience launcher (Linux).
#
# On this Raspberry Pi the panel is served permanently by the Apache vhost at
#   http://clickbridge.local            (from the Pi itself or the LAN)
# and the bridge runs as the systemd unit  clickbridge.service.
# This script is for setups without that (it starts bridge + artisan serve).
cd "$(dirname "$0")"

if [ ! -d panel/vendor ]; then
    echo "First run: ./install.sh  (composer install lives in there)" >&2
    exit 1
fi

if pgrep -f "clickbridge" >/dev/null; then
    echo "Bridge already running."
elif command -v systemctl >/dev/null && systemctl is-enabled --quiet clickbridge 2>/dev/null; then
    systemctl restart clickbridge
else
    (./run_clickbridge.sh >/tmp/clickbridge.log 2>&1 &)
    echo "Bridge started in background, log: /tmp/clickbridge.log"
fi

echo "Panel:  http://localhost:8080  (or http://<this-machine>:8080)"
(cd panel && php artisan serve --host=0.0.0.0 --port=8080)
