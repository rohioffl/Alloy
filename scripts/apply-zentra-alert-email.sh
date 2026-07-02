#!/usr/bin/env bash
# Replace Grafana internal alert email templates so HTML notifications render correctly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
CONTAINER="${GRAFANA_CONTAINER:-grafana}"
HTML="${ROOT}/alert/grafana_email_wrapper.html"
TXT="${ROOT}/alert/grafana_email_wrapper.txt"

if [[ ! -f "$HTML" || ! -f "$TXT" ]]; then
  echo "Missing alert/grafana_email_wrapper.{html,txt}" >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Grafana container '$CONTAINER' is not running." >&2
  exit 1
fi

docker cp "$HTML" "${CONTAINER}:/usr/share/grafana/public/emails/ng_alert_notification.html"
docker cp "$TXT" "${CONTAINER}:/usr/share/grafana/public/emails/ng_alert_notification.txt"
echo "Applied Zentra alert email templates to ${CONTAINER}."
