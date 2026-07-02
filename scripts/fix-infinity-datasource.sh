#!/usr/bin/env bash
# Fix Infinity "Port Monitor API" datasource so dashboard variables (Client/Account/Host) load.
set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://127.0.0.1:3000}"
GRAFANA_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_PASS="${GRAFANA_ADMIN_PASS:-admin}"
INFINITY_UID="${INFINITY_DS_UID:-P79940EC37D9FBFF2}"

payload=$(cat <<'JSON'
{
  "name": "Port Monitor API",
  "type": "yesoreyeram-infinity-datasource",
  "access": "proxy",
  "url": "http://monitor-api:9099",
  "jsonData": {
    "auth_method": "none",
    "allowedHosts": [
      "http://monitor-api:9099",
      "monitor-api:9099",
      "monitor-api",
      "http://localhost:9099",
      "localhost:9099",
      "127.0.0.1:9099"
    ]
  }
}
JSON
)

echo "Updating Infinity datasource (${INFINITY_UID})..."
curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
  -X PUT "${GRAFANA_URL}/api/datasources/uid/${INFINITY_UID}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" >/dev/null

curl -sf -u "${GRAFANA_USER}:${GRAFANA_PASS}" \
  -X POST "${GRAFANA_URL}/api/datasources/uid/${INFINITY_UID}/health" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'), d.get('message',''))"

echo "Done. Hard-refresh the dashboard (Ctrl+Shift+R) to clear variable warnings."
