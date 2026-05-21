#!/usr/bin/env bash
# =============================================================================
# Grafana Server Setup — Central Monitoring Server (production)
#
# Sets up:
#   - Prometheus remote_write receiver
#   - Central Monitoring API (:9099) with install token
#   - Grafana dashboards (8 total: All Servers + drill-down suite)
#
# Usage:
#   sudo bash setup-server.sh \
#     -grafana-url=http://localhost:3000 \
#     -grafana-key=glsa_xxxxx \
#     -api-public-url=http://YOUR_PUBLIC_IP:9099
# =============================================================================

set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_API_KEY="${GRAFANA_API_KEY:-}"
API_PUBLIC_URL="${API_PUBLIC_URL:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo /tmp)"

for arg in "$@"; do
  case "$arg" in
    -grafana-url=*)     GRAFANA_URL="${arg#*=}" ;;
    -grafana-key=*)     GRAFANA_API_KEY="${arg#*=}" ;;
    -api-public-url=*)  API_PUBLIC_URL="${arg#*=}" ;;
    -help|--help|-h) sed -n '2,18p' "$0" 2>/dev/null || true; exit 0 ;;
    *) ;;
  esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m ✔\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m !\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m ✘ %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# ---- Step 1: Prometheus remote_write -----------------------------------------
log "Step 1: Enabling Prometheus remote_write receiver..."

PROM_OVERRIDE="/etc/systemd/system/prometheus.service.d/remote-write.conf"
if systemctl is-active --quiet prometheus 2>/dev/null; then
  if ! grep -q "enable-remote-write-receiver" /etc/systemd/system/prometheus.service 2>/dev/null && \
     ! grep -q "enable-remote-write-receiver" "$PROM_OVERRIDE" 2>/dev/null; then
    mkdir -p "$(dirname "$PROM_OVERRIDE")"
    cat > "$PROM_OVERRIDE" << 'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus/metrics2 \
  --web.console.templates=/etc/prometheus/consoles \
  --web.console.libraries=/etc/prometheus/console_libraries \
  --web.listen-address=:9090 \
  --web.enable-lifecycle \
  --web.enable-admin-api \
  --web.enable-remote-write-receiver
EOF
    systemctl daemon-reload
    systemctl restart prometheus
    ok "Prometheus remote_write receiver enabled"
  else
    ok "Prometheus already has remote_write receiver"
  fi
else
  warn "Prometheus not running — skipping remote_write setup"
fi

# ---- Step 2: Central Monitoring API ------------------------------------------
log "Step 2: Installing Central Monitoring API..."

API_SRC="${SCRIPT_DIR}/port-monitor-api.py"
API_DEST="/opt/port-monitor-api.py"
[ -f "$API_SRC" ] && cp "$API_SRC" "$API_DEST" || \
  curl -fsSL "https://raw.githubusercontent.com/rohioffl/Alloy/main/port-monitor-api.py" -o "$API_DEST"

mkdir -p /var/lib/port-monitor/ports /etc/port-monitor
chmod 700 /etc/port-monitor

SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")
API_PUBLIC_URL="${API_PUBLIC_URL:-http://${SERVER_IP}:9099}"

# Preserve existing tokens on re-run
INSTALL_TOKEN=""
API_KEY=""
if [ -f /etc/port-monitor/config.json ]; then
  INSTALL_TOKEN=$(python3 -c "import json; print(json.load(open('/etc/port-monitor/config.json')).get('install_token',''))" 2>/dev/null || true)
  API_KEY=$(python3 -c "import json; print(json.load(open('/etc/port-monitor/config.json')).get('api_key',''))" 2>/dev/null || true)
fi
[ -z "$INSTALL_TOKEN" ] && INSTALL_TOKEN=$(openssl rand -hex 24)
[ -z "$API_KEY" ] && API_KEY=$(openssl rand -hex 24)

cat > /etc/port-monitor/config.json << EOF
{
  "public_url": "${API_PUBLIC_URL}",
  "grafana_url": "${GRAFANA_URL%/}",
  "install_token": "${INSTALL_TOKEN}",
  "api_key": "${API_KEY}"
}
EOF
chmod 600 /etc/port-monitor/config.json

cat > /etc/systemd/system/port-monitor-api.service << EOF
[Unit]
Description=Central Monitoring API
After=network-online.target prometheus.service
Wants=network-online.target

[Service]
Type=simple
Environment=MONITOR_CONFIG=/etc/port-monitor/config.json
Environment=MONITOR_PUBLIC_URL=${API_PUBLIC_URL}
Environment=MONITOR_GRAFANA_URL=${GRAFANA_URL%/}
Environment=MONITOR_INSTALL_TOKEN=${INSTALL_TOKEN}
Environment=MONITOR_API_KEY=${API_KEY}
ExecStart=/usr/bin/python3 /opt/port-monitor-api.py
Restart=on-failure
RestartSec=5s
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now port-monitor-api
sleep 2
systemctl is-active --quiet port-monitor-api || die "API failed: journalctl -u port-monitor-api -n 20"
ok "Central Monitoring API on :9099"

# ---- Step 3: Grafana dashboards ----------------------------------------------
log "Step 3: Deploying Grafana dashboards..."

INFINITY_UID="cfmmi8ef0wxz4a"
if [ -n "$GRAFANA_API_KEY" ]; then
  INFINITY_UID=$(curl -s "${GRAFANA_URL%/}/api/datasources/name/Port%20Monitor%20API" \
    -H "Authorization: Bearer ${GRAFANA_API_KEY}" 2>/dev/null | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('uid','') or 'cfmmi8ef0wxz4a')" 2>/dev/null || echo "cfmmi8ef0wxz4a")
  [ -z "$INFINITY_UID" ] && INFINITY_UID="cfmmi8ef0wxz4a"

  DASHBOARD_DIR="${SCRIPT_DIR}/dashboards"
  [ -d "$DASHBOARD_DIR" ] || DASHBOARD_DIR="/tmp/alloy-setup/dashboards"

  curl -s -X POST "${GRAFANA_URL%/}/api/folders" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${GRAFANA_API_KEY}" \
    -d '{"uid":"monitoring","title":"Monitoring"}' >/dev/null 2>&1 || true

  for f in "$DASHBOARD_DIR"/*.json; do
    [ -f "$f" ] || continue
    tmpdash=$(mktemp)
    sed -e "s|__MONITOR_API_PUBLIC_URL__|${API_PUBLIC_URL}|g" \
        -e "s|__INFINITY_DS_UID__|${INFINITY_UID}|g" "$f" > "$tmpdash"
    result=$(curl -s -X POST "${GRAFANA_URL%/}/api/dashboards/db" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer ${GRAFANA_API_KEY}" \
      -d @"$tmpdash")
    rm -f "$tmpdash"
    uid=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uid','error'))" 2>/dev/null)
    status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null)
    [ "$status" = "success" ] && ok "Dashboard: $uid" || warn "Failed: $(basename "$f")"
  done

  GRAFANA_INI="/etc/grafana/grafana.ini"
  if [ -f "$GRAFANA_INI" ] && ! grep -q "^disable_sanitize_html = true" "$GRAFANA_INI"; then
    grep -q "^;disable_sanitize_html" "$GRAFANA_INI" && \
      sed -i 's/^;disable_sanitize_html = false/disable_sanitize_html = true/' "$GRAFANA_INI" || \
      echo "disable_sanitize_html = true" >> "$GRAFANA_INI"
    systemctl restart grafana-server 2>/dev/null || true
    ok "Grafana HTML panels enabled (required for embedded server editor)"
  fi
else
  warn "No -grafana-key — skipping dashboard deploy"
fi

# ---- Done --------------------------------------------------------------------
echo ""
log "Production central server ready"
echo ""
echo "  Grafana:        ${GRAFANA_URL}"
echo "  Prometheus:     http://${SERVER_IP}:9090"
echo "  Monitoring API: ${API_PUBLIC_URL}/"
echo ""
echo "  Install token (save this — required for new nodes):"
echo "    ${INSTALL_TOKEN}"
echo ""
echo "  API key (for curl / automation — optional):"
echo "    ${API_KEY}"
echo ""
echo "  Install Alloy on a node (set name/client/account in Grafana after):"
echo "    curl -fsSL .../install-alloy.sh | sudo bash -s -- \\"
echo "      -remote-write=http://${SERVER_IP}:9090/api/v1/write \\"
echo "      -install-token=${INSTALL_TOKEN}"
echo ""
echo "  Then in Grafana → Server Drill-Down → section"
echo "  'Client · Account · Display Name — edit here'"
echo ""
echo "  Security: restrict port 9099 to your VPC/office (ufw/security group)."
echo "  Dashboards: All Servers + Server Drill-Down (7 panels) — no separate admin dashboard."
echo ""
