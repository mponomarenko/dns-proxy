#!/bin/bash

# Copyright 2025 Mike Ponomarenko
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Required:
# PIHOLE_API: full URL to Pi-hole API, e.g. http://192.168.1.10/admin/api.php
# PIHOLE_TOKEN: your Pi-hole API token
# DOMAIN_SUFFIX: default is 'local'
# INTERVAL: sleep time in seconds between syncs (default: 300)
# AVAHI_DISABLE_AUTOSTART: set to 1 to disable auto-start of avahi-daemon and dbus-daemon


# On Host Machine, run this:
# sudo apt-get update
# sudo apt-get install -y avahi-daemon dbus
# sudo systemctl enable --now avahi-daemon


set -eu

PIHOLE_API="${PIHOLE_API:-http://10.0.0.2/api}"
PIHOLE_TOKEN="${PIHOLE_TOKEN:?Missing PIHOLE_TOKEN}"
DOMAIN_SUFFIX="${DOMAIN_SUFFIX:-local}"
INTERVAL="${INTERVAL:-300}"

echo "[STARTUP] Avahi to Pi-hole sync container started"
echo "[CONFIG] PIHOLE_API=$PIHOLE_API"
echo "[CONFIG] DOMAIN_SUFFIX=$DOMAIN_SUFFIX"
echo "[CONFIG] INTERVAL=${INTERVAL}s"

log(){ printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }

dbus_ok() {
  command -v dbus-send >/dev/null 2>&1 || return 1
  dbus-send --system --print-reply \
    --dest=org.freedesktop.DBus / org.freedesktop.DBus.ListNames \
    >/dev/null 2>&1
}

avahi_ok() {
  command -v dbus-send >/dev/null 2>&1 || return 1
  dbus-send --system --print-reply \
    --dest=org.freedesktop.Avahi / org.freedesktop.Avahi.Server.GetAPIVersion \
    >/dev/null 2>&1
}

start_dbus() {
  mkdir -p /var/run/dbus
  log "[DBUS] starting system bus"
  dbus-daemon --system --nofork --nopidfile >/var/log/dbus.log 2>&1 &
  DBUS_PID=$!
  # wait up to ~5s for DBus to come up
  i=0; while ! dbus_ok; do
    i=$((i+1)); [ "$i" -gt 20 ] && { log "[DBUS] failed to come up"; break; }
    sleep 0.25
  done
}

start_avahi() {
  mkdir -p /run/avahi-daemon
  log "[AVAHI] starting avahi-daemon"
  # foreground would be fine, but daemonize to simplify logging
  avahi-daemon -D
  # wait up to ~5s for Avahi to register on DBus
  i=0; while ! avahi_ok; do
    i=$((i+1)); [ "$i" -gt 20 ] && { log "[AVAHI] did not register on DBus"; break; }
    sleep 0.25
  done
}

cleanup() {
  # stop avahi first (if we started it)
  if avahi_ok; then avahi-daemon -k || true; fi
  # stop private dbus if we launched it
  if [ "${DBUS_PID:-}" ]; then kill "$DBUS_PID" 2>/dev/null || true; fi
}
trap cleanup INT TERM EXIT

# Optional kill switch (set AVAHI_DISABLE_AUTOSTART=1 to skip auto-start)
if [ "${AVAHI_DISABLE_AUTOSTART:-0}" = "1" ]; then
  log "[AVAHI] autostart disabled; running app directly"
fi

# 1) Ensure required binaries exist
for b in dbus-daemon avahi-daemon; do
  command -v "$b" >/dev/null 2>&1 || { log "Missing $b. Install it in the image."; exit 1; }
done

# 2) Make sure we have a usable system bus
if ! dbus_ok; then
  # If the host DBus socket isn't mounted, start our own
  [ -S /var/run/dbus/system_bus_socket ] || start_dbus
fi

# 3) Make sure Avahi is available on the system bus
avahi_ok || start_avahi


# 4) Run the sync loop
while true; do
  echo "[INFO] Syncing mDNS hostnames..."

  /usr/local/bin/sync.py

  echo "[SLEEP] Sleeping for $INTERVAL seconds..."
  sleep "$INTERVAL"
done
