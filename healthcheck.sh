#!/bin/bash
set -eu

HEARTBEAT_FILE="${HEARTBEAT_FILE:-/config/state/heartbeat}"
OUTAGE_STATE_FILE="${OUTAGE_STATE_FILE:-/config/state/outage-state}"
HEALTH_MAX_SYNC_AGE_SECONDS="${HEALTH_MAX_SYNC_AGE_SECONDS:-900}"

now="$(date +%s)"

read_epoch() {
  file="$1"
  [ -r "$file" ] || return 1
  value="$(sed -n '1p' "$file")"
  case "$value" in
    ''|*[!0-9]*) return 1 ;;
    *) printf '%s\n' "$value" ;;
  esac
}

heartbeat="$(read_epoch "$HEARTBEAT_FILE")" || exit 1
last_success="$(read_epoch "$OUTAGE_STATE_FILE")" || exit 1

heartbeat_age=$((now - heartbeat))
success_age=$((now - last_success))
[ "$heartbeat_age" -ge 0 ] || heartbeat_age=0
[ "$success_age" -ge 0 ] || success_age=0

[ "$heartbeat_age" -le "$HEALTH_MAX_SYNC_AGE_SECONDS" ] || exit 1
[ "$success_age" -le "$HEALTH_MAX_SYNC_AGE_SECONDS" ] || exit 1
