#!/usr/bin/env bash
# =============================================================================
# Grafana Server Setup — Central Monitoring Server
#
# Sets up the central monitoring server with:
#   - Port Monitor API (:9099) — manages probe targets for all nodes
#   - Grafana dashboards — deploys all drill-down dashboards
#   - Prometheus remote_write receiver — accepts metrics from nodes
#
# Run this ONCE on your central Grafana/Prometheus server.
#
# Usage:
#   wget https://raw.githubusercontent.com/rohioffl/Alloy/main/setup-server.sh
#   sudo bash setup-server.sh -grafana-url=http://localhost:3000 \
#                              -grafana-key=glsa_xxxxx
#
# Flags:
#   -grafana-url=URL     Grafana base URL (default: http://localhost:3000)
#   -grafana-key=TOKEN   Grafana service account token (required for dashboards)
#   -help                Show this help
# =============================================================================

set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_API_KEY="${GRAFANA_API_KEY:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || echo /tmp)"

for arg in "$@"; do
  case "$arg" in
    -grafana-url=*)  GRAFANA_URL="${arg#*=}" ;;
    -grafana-key=*)  GRAFANA_API_KEY="${arg#*=}" ;;
    -help|--help|-h) sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
    *) ;;
  esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m ✔\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m !\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m ✘ %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."

# ---- Step 1: Enable Prometheus remote_write receiver -------------------------
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

# ---- Step 2: Install Port Monitor API ----------------------------------------
log "Step 2: Installing Port Monitor API..."

API_SRC="${SCRIPT_DIR}/port-monitor-api.py"
API_DEST="/opt/port-monitor-api.py"

if [ -f "$API_SRC" ]; then
  cp "$API_SRC" "$API_DEST"
else
  curl -fsSL "https://raw.githubusercontent.com/rohioffl/Alloy/main/port-monitor-api.py" -o "$API_DEST"
fi

mkdir -p /var/lib/port-monitor

cat > /etc/systemd/system/port-monitor-api.service << 'EOF'
[Unit]
Description=Port Monitor API — Central server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 /opt/port-monitor-api.py
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now port-monitor-api
sleep 2

if systemctl is-active --quiet port-monitor-api; then
  ok "Port Monitor API running on :9099"
else
  die "Port Monitor API failed. Check: journalctl -u port-monitor-api -n 20"
fi

# ---- Step 3: Deploy Grafana dashboards ---------------------------------------
log "Step 3: Deploying Grafana dashboards..."

if [ -z "$GRAFANA_API_KEY" ]; then
  warn "No -grafana-key provided — skipping dashboard deployment"
  warn "Run with: sudo bash setup-server.sh -grafana-key=glsa_xxxxx"
else
  DASHBOARD_DIR="${SCRIPT_DIR}/dashboards"

  if [ ! -d "$DASHBOARD_DIR" ]; then
    warn "dashboards/ folder not found — cloning from GitHub..."
    git clone --depth=1 https://github.com/rohioffl/Alloy.git /tmp/alloy-setup 2>/dev/null
    DASHBOARD_DIR="/tmp/alloy-setup/dashboards"
  fi

  # Create Monitoring folder
  curl -s -X POST "$GRAFANA_URL/api/folders" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $GRAFANA_API_KEY" \
    -d '{"uid":"monitoring","title":"Monitoring"}' >/dev/null 2>&1 || true

  # Deploy each dashboard
  for f in "$DASHBOARD_DIR"/*.json; do
    [ -f "$f" ] || continue
    result=$(curl -s -X POST "$GRAFANA_URL/api/dashboards/db" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $GRAFANA_API_KEY" \
      -d @"$f" 2>/dev/null)
    status=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null || echo "error")
    uid=$(echo "$result" | python3 -c "import sys,json; print(json.load(sys.stdin).get('uid',''))" 2>/dev/null || echo "")
    if [ "$status" = "success" ]; then
      ok "Dashboard: $uid"
    else
      warn "Failed: $(basename "$f")"
    fi
  done

  # Enable HTML in Grafana text panels (needed for Port Manager iframe)
  GRAFANA_INI="/etc/grafana/grafana.ini"
  if [ -f "$GRAFANA_INI" ]; then
    if grep -q "^;disable_sanitize_html" "$GRAFANA_INI"; then
      sed -i 's/^;disable_sanitize_html = false/disable_sanitize_html = true/' "$GRAFANA_INI"
      systemctl restart grafana-server 2>/dev/null || true
      ok "Grafana HTML panels enabled"
    elif ! grep -q "^disable_sanitize_html = true" "$GRAFANA_INI"; then
      echo "disable_sanitize_html = true" >> "$GRAFANA_INI"
      systemctl restart grafana-server 2>/dev/null || true
      ok "Grafana HTML panels enabled"
    else
      ok "Grafana HTML panels already enabled"
    fi
  fi
fi

# ---- Done --------------------------------------------------------------------
SERVER_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")
echo ""
log "Central server setup complete!"
echo ""
echo "  Port Monitor API:  http://${SERVER_IP}:9099/"
echo "  Grafana:           ${GRAFANA_URL}"
echo "  Prometheus:        http://${SERVER_IP}:9090"
echo ""
echo "  Install Alloy on nodes:"
echo "    curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \\"
echo "      sudo bash -s -- -remote-write=http://${SERVER_IP}:9090/api/v1/write"
echo ""
