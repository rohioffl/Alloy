#!/usr/bin/env bash
# =============================================================================
# Grafana Alloy - Complete Node Server Setup
#
# Single script that installs and configures Grafana Alloy as the ONLY
# monitoring agent on a node server. No separate prometheus, node_exporter,
# process-exporter, or blackbox_exporter needed.
#
# What this does:
#   1. Installs Grafana Alloy (if not present)
#   2. Generates config for host metrics, process monitoring, port probing, logs
#   3. Enables Prometheus remote_write receiver (if local Prometheus exists)
#   4. Starts Alloy as root (needed for process monitoring)
#   5. Deploys Grafana dashboards (if Grafana API is reachable)
#
# Usage:
#   sudo REMOTE_WRITE_URL=http://<prometheus>:9090/api/v1/write \
#        LOKI_URL=http://<grafana>:3000/loki/api/v1/push \
#        GRAFANA_URL=http://<grafana>:3000 \
#        GRAFANA_API_KEY=glsa_xxxxx \
#        bash install-alloy.sh
#
# Environment variables:
#   REMOTE_WRITE_URL   - Prometheus remote_write endpoint (required)
#   LOKI_URL           - Loki push endpoint (optional)
#   GRAFANA_URL        - Grafana base URL for dashboard deployment (optional)
#   GRAFANA_API_KEY    - Grafana service account token (optional)
#   ENV_LABEL          - Environment label, default: prod
#   PROBE_TARGETS      - Comma-separated host:port to probe, or "auto" (default)
#   PROCESS_NAMES      - Comma-separated process names, or "auto" (default)
#   SKIP_DASHBOARDS    - Set to 1 to skip dashboard deployment
#
# Uninstall:
#   sudo bash install-alloy.sh --uninstall
# =============================================================================

set -euo pipefail

# ---- Config ------------------------------------------------------------------
REMOTE_WRITE_URL="${REMOTE_WRITE_URL:-http://localhost:9090/api/v1/write}"
LOKI_URL="${LOKI_URL:-http://localhost:3000/loki/api/v1/push}"
GRAFANA_URL="${GRAFANA_URL:-http://localhost:3000}"
GRAFANA_API_KEY="${GRAFANA_API_KEY:-}"
ENV_LABEL="${ENV_LABEL:-prod}"
PROBE_TARGETS="${PROBE_TARGETS:-auto}"    # "auto" = discover from ss/netstat
PROCESS_NAMES="${PROCESS_NAMES:-auto}"    # "auto" = discover from running services
SKIP_DASHBOARDS="${SKIP_DASHBOARDS:-0}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- Pretty logging ----------------------------------------------------------
C_BLUE="\033[1;34m"; C_GREEN="\033[1;32m"; C_YELLOW="\033[1;33m"
C_RED="\033[1;31m"; C_RST="\033[0m"
log()  { printf "${C_BLUE}==>${C_RST} %s\n" "$*"; }
ok()   { printf "${C_GREEN} ✔${C_RST} %s\n" "$*"; }
warn() { printf "${C_YELLOW} !${C_RST} %s\n" "$*"; }
die()  { printf "${C_RED} ✘ %s${C_RST}\n" "$*" >&2; exit 1; }

# ---- Uninstall ---------------------------------------------------------------
uninstall() {
  log "Uninstalling Grafana Alloy..."
  systemctl disable --now alloy 2>/dev/null || true
  rm -rf /etc/alloy/config.alloy
  rm -rf /etc/systemd/system/alloy.service.d
  rm -rf /var/lib/alloy/data
  systemctl daemon-reload
  ok "Alloy stopped and config removed"
  echo "  Note: The alloy package is still installed. Remove with:"
  echo "    apt remove alloy  OR  dnf remove alloy"
  exit 0
}

# ---- Parse args --------------------------------------------------------------
for arg in "${@:-}"; do
  case "$arg" in
    -u|--uninstall) uninstall ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
  esac
done

# ---- Preflight ---------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
[ -d /run/systemd/system ] || die "systemd is required."

# ---- Step 1: Install Alloy ---------------------------------------------------
log "Step 1: Installing Grafana Alloy..."

if command -v alloy >/dev/null 2>&1; then
  ok "Alloy already installed: $(alloy --version 2>&1 | head -1)"
else
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y -qq apt-transport-https software-properties-common >/dev/null 2>&1
    mkdir -p /etc/apt/keyrings/
    wget -q -O - https://apt.grafana.com/gpg.key | gpg --dearmor > /etc/apt/keyrings/grafana.gpg
    echo "deb [signed-by=/etc/apt/keyrings/grafana.gpg] https://apt.grafana.com stable main" > /etc/apt/sources.list.d/grafana.list
    apt-get update -qq >/dev/null 2>&1
    apt-get install -y -qq alloy >/dev/null 2>&1
  elif command -v dnf >/dev/null 2>&1 || command -v yum >/dev/null 2>&1; then
    cat > /etc/yum.repos.d/grafana.repo << 'REPOEOF'
[grafana]
name=grafana
baseurl=https://rpm.grafana.com
repo_gpgcheck=1
enabled=1
gpgcheck=1
gpgkey=https://rpm.grafana.com/gpg.key
sslverify=1
sslcacert=/etc/pki/tls/certs/ca-bundle.crt
REPOEOF
    if command -v dnf >/dev/null 2>&1; then
      dnf install -y -q alloy >/dev/null 2>&1
    else
      yum install -y -q alloy >/dev/null 2>&1
    fi
  else
    die "Unsupported package manager. Install Alloy manually: https://grafana.com/docs/alloy/latest/set-up/install/"
  fi
  ok "Alloy installed"
fi

# ---- Step 2: Generate Alloy config ------------------------------------------
log "Step 2: Generating Alloy configuration..."

# ---- Auto-discover listening ports -------------------------------------------
discover_ports() {
  local ports=""
  if command -v ss >/dev/null 2>&1; then
    ports=$(ss -tlnp 2>/dev/null | awk 'NR>1 {print $4}' | grep -oE '[0-9]+$' | sort -un)
  elif command -v netstat >/dev/null 2>&1; then
    ports=$(netstat -tlnp 2>/dev/null | awk 'NR>2 {print $4}' | grep -oE '[0-9]+$' | sort -un)
  fi

  # Filter out ephemeral ports (>32767) and known noisy ones
  local result=""
  for port in $ports; do
    [ "$port" -gt 32767 ] && continue
    result="${result}localhost:${port},"
  done
  echo "${result%,}"  # trim trailing comma
}

# ---- Auto-discover running processes -----------------------------------------
discover_processes() {
  # Get unique binary names from running processes, exclude kernel threads and short-lived
  local procs=""
  procs=$(ps -eo comm= 2>/dev/null | sort -u | grep -vE '^\[|^$' | \
    grep -iE 'alloy|grafana|prometheus|node_exporter|nginx|apache|httpd|mysql|mysqld|postgres|redis|mongo|docker|containerd|sshd|systemd|java|python|node|php|haproxy|caddy|traefik|consul|vault|nomad|etcd|kubelet|coredns' | \
    head -20)

  # If nothing matched the filter, grab top processes by name
  if [ -z "$procs" ]; then
    procs=$(ps -eo comm= 2>/dev/null | sort | uniq -c | sort -rn | head -15 | awk '{print $2}' | grep -vE '^\[|^(sh|bash|sleep|cat|grep|awk|sed|tee|tail|head)$')
  fi

  echo "$procs"
}

# Resolve PROBE_TARGETS
if [ "$PROBE_TARGETS" = "auto" ]; then
  log "  Auto-discovering listening ports..."
  PROBE_TARGETS=$(discover_ports)
  if [ -z "$PROBE_TARGETS" ]; then
    PROBE_TARGETS="localhost:22"
    warn "  No ports discovered, falling back to :22"
  else
    ok "  Discovered ports: $PROBE_TARGETS"
  fi
fi

# Resolve PROCESS_NAMES
PROCESS_MATCHERS=""
if [ "$PROCESS_NAMES" = "auto" ]; then
  log "  Auto-discovering running processes..."
  DISCOVERED_PROCS=$(discover_processes)
  if [ -n "$DISCOVERED_PROCS" ]; then
    ok "  Discovered: $(echo $DISCOVERED_PROCS | tr '\n' ' ')"
    while IFS= read -r proc; do
      [ -z "$proc" ] && continue
      PROCESS_MATCHERS="${PROCESS_MATCHERS}
  matcher {
    name = \"${proc}\"
    comm = [\"${proc}\"]
  }"
    done <<< "$DISCOVERED_PROCS"
  fi
else
  # Manual list: comma-separated
  IFS=',' read -ra PROC_LIST <<< "$PROCESS_NAMES"
  for proc in "${PROC_LIST[@]}"; do
    proc=$(echo "$proc" | xargs)
    PROCESS_MATCHERS="${PROCESS_MATCHERS}
  matcher {
    name = \"${proc}\"
    comm = [\"${proc}\"]
  }"
  done
fi

# Always add the catch-all
PROCESS_MATCHERS="${PROCESS_MATCHERS}
  matcher {
    name    = \"all\"
    cmdline = [\".+\"]
  }"

# Build probe targets config
PROBE_CONFIG=""
IFS=',' read -ra TARGETS <<< "$PROBE_TARGETS"
for target in "${TARGETS[@]}"; do
  target=$(echo "$target" | xargs)
  [ -z "$target" ] && continue
  name=$(echo "$target" | tr ':.' '_')
  PROBE_CONFIG="${PROBE_CONFIG}
  target {
    name    = \"${name}\"
    address = \"${target}\"
    module  = \"tcp_connect\"
  }"
done

mkdir -p /etc/alloy
cat > /etc/alloy/config.alloy << ALLOYEOF
// Grafana Alloy - Standalone Node Agent
// Generated by install-alloy.sh on $(date -Iseconds)
// Remote write: ${REMOTE_WRITE_URL}

// ============================================================================
// HOST METRICS (replaces node_exporter)
// ============================================================================

prometheus.exporter.unix "node" {
  set_collectors = ["cpu", "diskstats", "filesystem", "loadavg", "meminfo", "netdev", "netstat", "os", "stat", "time", "uname", "vmstat", "systemd", "processes"]
}

prometheus.scrape "node_metrics" {
  targets         = prometheus.exporter.unix.node.targets
  forward_to      = [prometheus.relabel.add_host_label.receiver]
  scrape_interval = "15s"
}

// ============================================================================
// PROCESS MONITORING (replaces process-exporter)
// ============================================================================

discovery.process "all" {
  refresh_interval = "15s"
}

prometheus.exporter.process "default" {
  track_children = false
  track_threads  = true
${PROCESS_MATCHERS}
}

prometheus.scrape "process_metrics" {
  targets         = prometheus.exporter.process.default.targets
  forward_to      = [prometheus.relabel.add_host_label.receiver]
  scrape_interval = "15s"
}

// ============================================================================
// PORT / ENDPOINT MONITORING (replaces blackbox_exporter)
// ============================================================================

prometheus.exporter.blackbox "endpoints" {
  config = "{ modules: { tcp_connect: { prober: tcp, timeout: 5s }, http_2xx: { prober: http, timeout: 5s, http: { preferred_ip_protocol: ip4, follow_redirects: true } } } }"
${PROBE_CONFIG}
}

prometheus.scrape "blackbox_metrics" {
  targets         = prometheus.exporter.blackbox.endpoints.targets
  forward_to      = [prometheus.relabel.add_host_label.receiver]
  scrape_interval = "15s"
}

// ============================================================================
// RELABELING - Add host label to all metrics
// ============================================================================

prometheus.relabel "add_host_label" {
  forward_to = [prometheus.remote_write.central.receiver]

  rule {
    target_label = "host"
    replacement  = constants.hostname
  }
}

// ============================================================================
// REMOTE WRITE - Ship metrics to central Prometheus
// ============================================================================

prometheus.remote_write "central" {
  endpoint {
    url = "${REMOTE_WRITE_URL}"
    queue_config {
      capacity             = 10000
      max_shards           = 10
      max_samples_per_send = 2000
      batch_send_deadline  = "5s"
    }
  }
  external_labels = {
    env = "${ENV_LABEL}",
  }
}

// ============================================================================
// LOG COLLECTION (replaces promtail)
// ============================================================================

local.file_match "system_logs" {
  path_targets = [
    {__path__ = "/var/log/syslog", job = "syslog"},
    {__path__ = "/var/log/auth.log", job = "authlog"},
  ]
}

loki.source.file "system_logs" {
  targets    = local.file_match.system_logs.targets
  forward_to = [loki.relabel.add_host.receiver]
}

loki.relabel "add_host" {
  forward_to = [loki.write.central.receiver]

  rule {
    target_label = "host"
    replacement  = constants.hostname
  }
}

loki.write "central" {
  endpoint {
    url = "${LOKI_URL}"
  }
  external_labels = {
    env = "${ENV_LABEL}",
  }
}
ALLOYEOF

ok "Config written to /etc/alloy/config.alloy"

# ---- Step 3: Enable Prometheus remote_write receiver -------------------------
log "Step 3: Checking Prometheus remote_write receiver..."

if systemctl is-active --quiet prometheus 2>/dev/null; then
  PROM_SERVICE="/etc/systemd/system/prometheus.service"
  if [ -f "$PROM_SERVICE" ] && ! grep -q "enable-remote-write-receiver" "$PROM_SERVICE"; then
    sed -i 's|--web.enable-lifecycle|--web.enable-lifecycle \\\n  --web.enable-remote-write-receiver|' "$PROM_SERVICE"
    systemctl daemon-reload
    systemctl restart prometheus
    ok "Enabled --web.enable-remote-write-receiver on Prometheus"
  elif [ ! -f "$PROM_SERVICE" ]; then
    # Try creating override for package-managed prometheus
    if [ -f /lib/systemd/system/prometheus.service ] && ! grep -q "enable-remote-write-receiver" /lib/systemd/system/prometheus.service; then
      mkdir -p /etc/systemd/system/prometheus.service.d
      if [ ! -f /etc/systemd/system/prometheus.service.d/remote-write.conf ]; then
        cat > /etc/systemd/system/prometheus.service.d/remote-write.conf << 'PROMEOF'
[Service]
ExecStart=
ExecStart=/usr/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus/metrics2 \
  --web.console.templates=/etc/prometheus/consoles \
  --web.console.libraries=/etc/prometheus/console_libraries \
  --web.listen-address=:9090 \
  --web.enable-lifecycle \
  --web.enable-remote-write-receiver
PROMEOF
        systemctl daemon-reload
        systemctl restart prometheus
        ok "Created Prometheus override with remote-write-receiver"
      fi
    else
      ok "Prometheus already has remote-write-receiver"
    fi
  else
    ok "Prometheus already has remote-write-receiver"
  fi

  # Add Alloy scrape target to Prometheus
  PROM_CONFIG="/etc/prometheus/prometheus.yml"
  if [ -f "$PROM_CONFIG" ] && ! grep -q "job_name.*alloy" "$PROM_CONFIG"; then
    cat >> "$PROM_CONFIG" << 'SCRAPEEOF'

  - job_name: alloy
    static_configs:
      - targets: ['localhost:12345']
    metrics_path: /metrics
SCRAPEEOF
    curl -s -X POST http://localhost:9090/-/reload >/dev/null 2>&1 || systemctl reload prometheus 2>/dev/null || true
    ok "Added Alloy scrape target to prometheus.yml"
  fi
else
  warn "Local Prometheus not found — Alloy will remote_write to: $REMOTE_WRITE_URL"
fi

# ---- Step 4: Start Alloy as root --------------------------------------------
log "Step 4: Starting Alloy..."

mkdir -p /etc/systemd/system/alloy.service.d
cat > /etc/systemd/system/alloy.service.d/override.conf << 'EOF'
[Service]
User=root
Group=root
EOF

rm -rf /var/lib/alloy/data
mkdir -p /var/lib/alloy/data

systemctl daemon-reload
systemctl enable alloy >/dev/null 2>&1
systemctl restart alloy

sleep 5
if systemctl is-active --quiet alloy; then
  ok "Alloy is running"
else
  die "Alloy failed to start. Check: journalctl -u alloy -n 50"
fi

# ---- Step 5: Deploy dashboards ----------------------------------------------
if [ "$SKIP_DASHBOARDS" = "1" ] || [ -z "$GRAFANA_API_KEY" ]; then
  if [ -z "$GRAFANA_API_KEY" ]; then
    warn "GRAFANA_API_KEY not set — skipping dashboard deployment"
  else
    warn "SKIP_DASHBOARDS=1 — skipping dashboard deployment"
  fi
else
  log "Step 5: Deploying Grafana dashboards..."

  DASHBOARD_DIR="${SCRIPT_DIR}/dashboards"
  if [ -d "$DASHBOARD_DIR" ]; then
    # Create folder
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

    # Deploy summary dashboard
    SUMMARY_DASH="${SCRIPT_DIR}/dashboards/../alloy-drilldown.json"
    if [ -f "$SUMMARY_DASH" ]; then
      curl -s -X POST "$GRAFANA_URL/api/dashboards/db" \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $GRAFANA_API_KEY" \
        -d @"$SUMMARY_DASH" >/dev/null 2>&1 && ok "Summary dashboard deployed"
    fi
  else
    warn "No dashboards/ directory found at $DASHBOARD_DIR"
  fi
fi

# ---- Step 6: Verify ---------------------------------------------------------
log "Step 6: Verifying..."
sleep 3

if curl -fsS --max-time 5 "http://localhost:12345/metrics" >/dev/null 2>&1; then
  ok "Alloy metrics endpoint responding on :12345"
else
  warn "Alloy metrics endpoint not responding yet"
fi

# Check remote_write
if curl -s -o /dev/null -w "%{http_code}" -X POST "${REMOTE_WRITE_URL}" 2>/dev/null | grep -q "400\|415"; then
  ok "Remote write endpoint accepting connections"
fi

# ---- Done --------------------------------------------------------------------
echo ""
log "Setup complete!"
echo ""
echo "  Alloy UI:         http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):12345"
echo "  Remote Write:     ${REMOTE_WRITE_URL}"
echo "  Loki Push:        ${LOKI_URL}"
echo "  Config:           /etc/alloy/config.alloy"
echo ""
echo "  What Alloy monitors:"
echo "    ✔ Host metrics     (CPU, memory, disk, network, load, filesystem)"
echo "    ✔ Processes        (auto-discovered from running services)"
echo "    ✔ Ports            (auto-discovered: ${PROBE_TARGETS})"
echo "    ✔ Logs             (syslog, auth.log)"
echo ""
echo "  Grafana dashboards (if deployed):"
echo "    • Server Drill-Down (Summary)"
echo "    • Server Drill-Down · CPU"
echo "    • Server Drill-Down · Memory"
echo "    • Server Drill-Down · Disks"
echo "    • Server Drill-Down · Network"
echo "    • Server Drill-Down · Ports"
echo "    • Server Drill-Down · Processes"
echo ""
echo "  Uninstall:"
echo "    sudo bash $(realpath "$0") --uninstall"
echo ""
