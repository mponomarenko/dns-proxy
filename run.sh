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
# MAX_SYNC_OUTAGE_SECONDS: exit after this long without a successful sync (default: 3600)
# MIN_MDNS_HOSTS: fail a sync when fewer than this many unique mDNS hosts are discovered (default: 1)
# AVAHI_DISABLE_AUTOSTART: set to 1 to disable auto-start of avahi-daemon and dbus-daemon
# DNS_OVERRIDES_FILE: path to hosts-format overrides file (default: /config/overrides)
#                     format: "IP hostname" per line, like /etc/hosts
# DNS_STATIC_HOSTS:   comma-separated static entries, e.g. "10.0.0.50=compute.home,10.0.0.51=other.home"
#                     takes precedence over Avahi discovery; merged with DNS_OVERRIDES_FILE


# On Host Machine, run this:
# sudo apt-get update
# sudo apt-get install -y avahi-daemon dbus
# sudo systemctl enable --now avahi-daemon


set -eu

PIHOLE_API="${PIHOLE_API:-http://10.0.0.2/api}"
PIHOLE_TOKEN="${PIHOLE_TOKEN:?Missing PIHOLE_TOKEN}"
DOMAIN_SUFFIX="${DOMAIN_SUFFIX:-local}"
INTERVAL="${INTERVAL:-300}"
MAX_SYNC_OUTAGE_SECONDS="${MAX_SYNC_OUTAGE_SECONDS:-3600}"
MIN_MDNS_HOSTS="${MIN_MDNS_HOSTS:-1}"

echo "[STARTUP] Avahi to Pi-hole sync container started"
echo "[CONFIG] PIHOLE_API=$PIHOLE_API"
echo "[CONFIG] DOMAIN_SUFFIX=$DOMAIN_SUFFIX"
echo "[CONFIG] INTERVAL=${INTERVAL}s"
echo "[CONFIG] MAX_SYNC_OUTAGE_SECONDS=${MAX_SYNC_OUTAGE_SECONDS}s"
echo "[CONFIG] MIN_MDNS_HOSTS=${MIN_MDNS_HOSTS}"

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
last_success_epoch="$(date +%s)"
outage_started_epoch=""
while true; do
  echo "[INFO] Syncing mDNS hostnames..."

  if /usr/local/bin/sync.py; then
    if [ -n "$outage_started_epoch" ]; then
      now_epoch="$(date +%s)"
      log "[INFO] Sync recovered after $((now_epoch - outage_started_epoch))s outage"
      outage_started_epoch=""
    fi
    last_success_epoch="$(date +%s)"
  else
    now_epoch="$(date +%s)"
    if [ -z "$outage_started_epoch" ]; then
      outage_started_epoch="$now_epoch"
    fi
    outage_seconds=$((now_epoch - last_success_epoch))
    log "[WARN] Sync failed; outage=${outage_seconds}s/${MAX_SYNC_OUTAGE_SECONDS}s; retrying"
    if [ "$outage_seconds" -ge "$MAX_SYNC_OUTAGE_SECONDS" ]; then
      log "[ERROR] No successful Pi-hole sync for ${outage_seconds}s; exiting"
      exit 1
    fi
  fi

  echo "[SLEEP] Sleeping for $INTERVAL seconds..."
  sleep "$INTERVAL"
done
