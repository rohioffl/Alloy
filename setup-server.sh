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

# ---- Step 2: Central Monitoring API (FastAPI + uvicorn) ----------------------
log "Step 2: Installing Central Monitoring API (FastAPI)..."

API_SRC_DIR="${SCRIPT_DIR}/api"
API_DEST_DIR="/opt/port-monitor-api"

mkdir -p "$API_DEST_DIR"
if [ -d "$API_SRC_DIR/app" ]; then
  cp -r "$API_SRC_DIR/app" "$API_DEST_DIR/"
  cp "$API_SRC_DIR/requirements.txt" "$API_DEST_DIR/" 2>/dev/null || true
  cp "$API_SRC_DIR/run.py" "$API_DEST_DIR/" 2>/dev/null || true
else
  # Fallback: pull the package from the repo (raw can't list dirs, so use a tarball)
  warn "Local api/ folder not found — fetching from GitHub"
  TMP_TGZ=$(mktemp)
  curl -fsSL "https://github.com/rohioffl/Alloy/archive/refs/heads/main.tar.gz" -o "$TMP_TGZ"
  tar -xzf "$TMP_TGZ" -C /tmp
  cp -r /tmp/Alloy-main/api/app "$API_DEST_DIR/"
  cp /tmp/Alloy-main/api/requirements.txt "$API_DEST_DIR/" 2>/dev/null || true
  cp /tmp/Alloy-main/api/run.py "$API_DEST_DIR/" 2>/dev/null || true
  rm -rf "$TMP_TGZ" /tmp/Alloy-main
fi

mkdir -p /var/lib/port-monitor/ports /etc/port-monitor
chmod 700 /etc/port-monitor

# Python venv with FastAPI + uvicorn
if ! python3 -c "import venv" 2>/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
  PYVER=$(python3 -c "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}')")
  apt-get update -qq 2>/dev/null || true
  apt-get install -y "python${PYVER}-venv" python3-pip >/dev/null 2>&1 || \
    apt-get install -y python3-venv python3-pip >/dev/null 2>&1 || \
    warn "Could not install python venv package — install python3-venv manually"
fi
python3 -m venv "$API_DEST_DIR/.venv"
"$API_DEST_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$API_DEST_DIR/.venv/bin/pip" install --quiet -r "$API_DEST_DIR/requirements.txt"
ok "FastAPI dependencies installed in $API_DEST_DIR/.venv"

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
Description=Central Monitoring API (FastAPI)
After=network-online.target prometheus.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${API_DEST_DIR}
Environment=MONITOR_CONFIG=/etc/port-monitor/config.json
Environment=MONITOR_PUBLIC_URL=${API_PUBLIC_URL}
Environment=MONITOR_GRAFANA_URL=${GRAFANA_URL%/}
Environment=MONITOR_INSTALL_TOKEN=${INSTALL_TOKEN}
Environment=MONITOR_API_KEY=${API_KEY}
ExecStart=${API_DEST_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9099
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
ok "Central Monitoring API (FastAPI) on :9099"

# ---- Step 2b: Alloy on central host (metrics for this server) ----------------
log "Step 2b: Installing Alloy on central host..."

INSTALL_SCRIPT="${SCRIPT_DIR}/install-alloy.sh"
if [ -f "$INSTALL_SCRIPT" ]; then
  bash "$INSTALL_SCRIPT" \
    -remote-write=http://127.0.0.1:9090/api/v1/write \
    -api-url="http://127.0.0.1:9099" \
    -loki="${GRAFANA_URL%/}/loki/api/v1/push" \
    -install-token="${INSTALL_TOKEN}" \
    -client=internal \
    -account=default || warn "Alloy install had warnings — check: journalctl -u alloy -n 30"
  systemctl enable alloy 2>/dev/null || true
  systemctl is-active --quiet alloy && ok "Alloy running on central host" || warn "Alloy not running — run install-alloy.sh manually"
else
  warn "install-alloy.sh not found — skip central Alloy agent"
fi

# ---- Step 3: Grafana dashboards ----------------------------------------------
log "Step 3: Deploying Grafana dashboards..."

INFINITY_UID=""
PROM_UID="PBFA97CFB590B2093"
API_DS_URL="http://127.0.0.1:9099"
if [ -n "$GRAFANA_API_KEY" ]; then
  GAPI="${GRAFANA_URL%/}/api"
  AUTH=(-H "Authorization: Bearer ${GRAFANA_API_KEY}")

  PROM_UID=$(curl -s "${GAPI}/datasources/name/Prometheus" "${AUTH[@]}" 2>/dev/null | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('uid','') or 'PBFA97CFB590B2093')" 2>/dev/null || echo "PBFA97CFB590B2093")

  INFINITY_UID=$(curl -s "${GAPI}/datasources/name/Port%20Monitor%20API" "${AUTH[@]}" 2>/dev/null | \
    python3 -c "import sys,json; print(json.load(sys.stdin).get('uid',''))" 2>/dev/null || true)
  if [ -z "$INFINITY_UID" ]; then
    log "Creating Infinity datasource (Port Monitor API)..."
    INFINITY_UID=$(curl -s -X POST "${GAPI}/datasources" "${AUTH[@]}" \
      -H "Content-Type: application/json" \
      -d "{\"name\":\"Port Monitor API\",\"type\":\"yesoreyeram-infinity-datasource\",\"access\":\"proxy\",\"url\":\"${API_DS_URL}\",\"isDefault\":false}" 2>/dev/null | \
      python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('datasource',{}).get('uid') or d.get('uid',''))" 2>/dev/null || true)
  fi
  [ -z "$INFINITY_UID" ] && INFINITY_UID="cfmmi8ef0wxz4a" && warn "Infinity datasource missing — install yesoreyeram-infinity-datasource plugin in Grafana"

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
        -e "s|__INFINITY_DS_UID__|${INFINITY_UID}|g" \
        -e "s|__PROM_DS_UID__|${PROM_UID}|g" "$f" > "$tmpdash"
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
