#!/usr/bin/env bash
# System-level install (run once, needs sudo):
#   - unblocks on-board Bluetooth
#   - installs + enables the clickbridge systemd unit
#   - installs + enables the Apache vhost for the control panel
# Environment-side things (venv, composer) are done by ./install.sh first.
set -euo pipefail
cd "$(dirname "$0")"

rfkill unblock bluetooth 2>/dev/null || true

install -m 644 clickbridge.service /etc/systemd/system/clickbridge.service
install -m 644 clickbridge-alias.service /etc/systemd/system/clickbridge-alias.service
systemctl daemon-reload
systemctl enable --now clickbridge clickbridge-alias

if command -v apache2 >/dev/null 2>&1; then
    install -m 644 zwiftclickpair.conf /etc/apache2/sites-available/zwiftclickpair.conf
    a2ensite zwiftclickpair
    systemctl reload apache2
fi

echo "Done. Panel: http://clickbridge.local   Bridge logs: journalctl -u clickbridge -f"
