#!/usr/bin/env bash
# Publishes the web-panel host names on the LAN via mDNS (avahi), so
# http://clickbridge.local and http://zwiftclickpair.local resolve from
# phones/other devices without editing their hosts files.
# (avahi-publish keeps the registrations alive until it is stopped.)
set -e
IP=$(hostname -I | awk '{print $1}')
avahi-publish -a -R clickbridge.local "$IP" &
avahi-publish -a -R zwiftclickpair.local "$IP" &
wait
