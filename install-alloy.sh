#!/usr/bin/env bash
# =============================================================================
# Grafana Alloy - Node Agent Installer
#
# Installs Alloy on any Linux node and connects it to your central Prometheus.
# The node automatically appears in your Grafana dashboards.
#
# Usage:
#   wget https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh
#   sudo bash install-alloy.sh -remote-write=http://<prometheus>:9090/api/v1/write
#
# One-liner:
#   curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
#     sudo bash -s -- -remote-write=http://<prometheus>:9090/api/v1/write
#
# Flags:
#   -remote-write=URL    Prometheus remote_write endpoint (required)
#   -loki=URL            Loki push endpoint (optional)
#   -processes=NAMES     Comma-separated process names or "auto" (default: auto)
#   -uninstall           Remove Alloy and stop services
#   -help                Show this help
# =============================================================================

set -euo pipefail

# ---- Defaults ----------------------------------------------------------------
REMOTE_WRITE_URL="${REMOTE_WRITE_URL:-}"
LOKI_URL="${LOKI_URL:-}"
PROCESS_NAMES="${PROCESS_NAMES:-auto}"

# ---- Parse flags -------------------------------------------------------------
for arg in "$@"; do
  case "$arg" in
    -remote-write=*)  REMOTE_WRITE_URL="${arg#*=}" ;;
    -loki=*)          LOKI_URL="${arg#*=}" ;;
    -processes=*)     PROCESS_NAMES="${arg#*=}" ;;
    -uninstall|--uninstall|-u) DO_UNINSTALL=1 ;;
    -help|--help|-h) sed -n '2,22p' "$0" 2>/dev/null || true; exit 0 ;;
    *) ;;
  esac
done

REMOTE_WRITE_URL="${REMOTE_WRITE_URL:-http://localhost:9090/api/v1/write}"
LOKI_URL="${LOKI_URL:-http://localhost:3000/loki/api/v1/push}"

# ---- Logging -----------------------------------------------------------------
log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m ✔\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m !\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m ✘ %s\033[0m\n" "$*" >&2; exit 1; }

# ---- Uninstall ---------------------------------------------------------------
uninstall() {
  log "Uninstalling Grafana Alloy completely..."
  systemctl disable --now alloy 2>/dev/null || true
  systemctl disable --now port-monitor-api 2>/dev/null || true
  rm -f /etc/alloy/config.alloy /etc/alloy/probe-targets.json
  rm -rf /etc/alloy
  rm -rf /etc/systemd/system/alloy.service.d
  rm -f /etc/systemd/system/port-monitor-api.service
  rm -rf /var/lib/alloy
  rm -f /etc/apt/sources.list.d/grafana.list
  rm -f /etc/apt/keyrings/grafana.gpg
  rm -f /etc/yum.repos.d/grafana.repo
  # Remove package
  if command -v apt-get >/dev/null 2>&1; then
    apt-get remove -y --purge alloy >/dev/null 2>&1 || true
    apt-get autoremove -y >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf remove -y alloy >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum remove -y alloy >/dev/null 2>&1 || true
  fi
  systemctl daemon-reload
  ok "Alloy completely removed (package, config, data, repos)"
  exit 0
}
[ "${DO_UNINSTALL:-0}" = "1" ] && uninstall

# ---- Preflight ---------------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
[ -d /run/systemd/system ] || die "systemd is required."

# ---- Step 1: Install Alloy ---------------------------------------------------
log "Installing Grafana Alloy..."

if command -v alloy >/dev/null 2>&1; then
  ok "Already installed: $(alloy --version 2>&1 | head -1)"
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
    die "Unsupported package manager. Install manually: https://grafana.com/docs/alloy/latest/set-up/install/"
  fi
  ok "Alloy installed"
fi

# ---- Step 2: Auto-discover --------------------------------------------------
log "Discovering processes..."

discover_processes() {
  ps -eo comm= 2>/dev/null | sort -u | grep -vE '^\[|^$' | \
    grep -iE 'alloy|grafana|prometheus|node_exporter|nginx|apache|httpd|mysql|mysqld|postgres|redis|mongo|docker|containerd|sshd|systemd|java|python|node|php|haproxy|caddy|traefik|consul|vault|nomad|etcd|kubelet|coredns' | \
    head -20
}

PROCESS_MATCHERS=""
if [ "$PROCESS_NAMES" = "auto" ]; then
  DISCOVERED_PROCS=$(discover_processes)
  if [ -n "$DISCOVERED_PROCS" ]; then
    ok "Processes: $(echo $DISCOVERED_PROCS | tr '\n' ' ')"
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

# ---- Step 3: Write Alloy config ----------------------------------------------
log "Writing Alloy config..."
mkdir -p /etc/alloy

cat > /etc/alloy/config.alloy << ALLOYEOF
// Grafana Alloy - Node Agent
// Generated on $(date -Iseconds)
// Remote write: ${REMOTE_WRITE_URL}

// HOST METRICS
prometheus.exporter.unix "node" {
  set_collectors = ["cpu", "diskstats", "filesystem", "loadavg", "meminfo", "netdev", "netstat", "os", "stat", "time", "uname", "vmstat", "systemd", "processes"]
}

prometheus.scrape "node_metrics" {
  targets         = prometheus.exporter.unix.node.targets
  forward_to      = [prometheus.relabel.add_host_label.receiver]
  scrape_interval = "15s"
}

// PROCESS MONITORING
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

// PORT MONITORING (managed via Port Monitor API :9099)
prometheus.exporter.blackbox "endpoints" {
  config = "{ modules: { tcp_connect: { prober: tcp, timeout: 5s }, http_2xx: { prober: http, timeout: 5s, http: { preferred_ip_protocol: ip4, follow_redirects: true } } } }"
}

prometheus.scrape "blackbox_metrics" {
  targets         = prometheus.exporter.blackbox.endpoints.targets
  forward_to      = [prometheus.relabel.add_host_label.receiver]
  scrape_interval = "15s"
}

// RELABELING
prometheus.relabel "add_host_label" {
  forward_to = [prometheus.remote_write.central.receiver]

  rule {
    target_label = "host"
    replacement  = constants.hostname
  }
}

// REMOTE WRITE
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
}

// LOG COLLECTION
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
}
ALLOYEOF

ok "Config: /etc/alloy/config.alloy"

# ---- Step 4: Start Alloy ----------------------------------------------------
log "Starting Alloy..."

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

# ---- Step 5: Verify ----------------------------------------------------------
log "Verifying..."

if curl -fsS --max-time 5 "http://localhost:12345/metrics" >/dev/null 2>&1; then
  ok "Alloy responding on :12345"
fi

if curl -s -o /dev/null -w "%{http_code}" -X POST "${REMOTE_WRITE_URL}" 2>/dev/null | grep -q "400\|415"; then
  ok "Remote write endpoint reachable"
fi

# ---- Done --------------------------------------------------------------------
echo ""
log "Done! This node will appear in Grafana within 30 seconds."
echo ""
echo "  Alloy UI:       http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):12345"
echo "  Remote Write:   ${REMOTE_WRITE_URL}"
echo "  Config:         /etc/alloy/config.alloy"
echo ""
echo "  Ports: Add from Grafana Ports dashboard (no ports monitored by default)"
echo ""
echo "  Uninstall:  curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | sudo bash -s -- -uninstall"
echo ""
