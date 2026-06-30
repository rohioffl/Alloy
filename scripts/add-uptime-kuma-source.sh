#!/usr/bin/env bash
# Add Uptime Kuma as a Prometheus scrape source and sync Grafana assets.

set -euo pipefail

UPTIME_KUMA_URL="${UPTIME_KUMA_URL:-}"
UPTIME_KUMA_API_KEY="${UPTIME_KUMA_API_KEY:-}"
PROM_CONFIG="${PROM_CONFIG:-/etc/prometheus/prometheus.yml}"
PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"
MONITOR_API_URL="${MONITOR_API_URL:-http://127.0.0.1:9099}"
MONITOR_API_KEY="${MONITOR_API_KEY:-}"

for arg in "$@"; do
  case "$arg" in
    -url=*|-uptime-kuma-url=*) UPTIME_KUMA_URL="${arg#*=}" ;;
    -api-key=*|-uptime-kuma-key=*) UPTIME_KUMA_API_KEY="${arg#*=}" ;;
    -prom-config=*) PROM_CONFIG="${arg#*=}" ;;
    -prom-url=*) PROM_URL="${arg#*=}" ;;
    -api-url=*) MONITOR_API_URL="${arg#*=}" ;;
    -monitor-key=*) MONITOR_API_KEY="${arg#*=}" ;;
    -help|--help|-h)
      sed -n '1,36p' "$0" 2>/dev/null || true
      exit 0
      ;;
    *) ;;
  esac
done

log()  { printf "\033[1;34m==>\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m OK\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m !!\033[0m %s\n" "$*"; }
die()  { printf "\033[1;31m ERROR %s\033[0m\n" "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root (sudo)."
[ -n "$UPTIME_KUMA_URL" ] || die "Missing -url=http://UPTIME_KUMA:3001"
[ -f "$PROM_CONFIG" ] || die "Prometheus config not found: $PROM_CONFIG"

KUMA_PARSED=$(python3 - "$UPTIME_KUMA_URL" <<'PY'
import sys
from urllib.parse import urlparse

raw = (sys.argv[1] or "").strip()
if "://" not in raw:
    raw = "http://" + raw
p = urlparse(raw)
target = p.netloc
if not target:
    target = p.path.split("/", 1)[0]
path = p.path if p.netloc else ""
if not path or path == "/":
    path = "/metrics"
scheme = p.scheme or "http"
if scheme not in ("http", "https"):
    scheme = "http"
print(f"{scheme}|{target}|{path}")
PY
)
IFS='|' read -r KUMA_SCHEME KUMA_TARGET KUMA_METRICS_PATH <<< "$KUMA_PARSED"
[ -n "$KUMA_TARGET" ] || die "Invalid Uptime Kuma URL: $UPTIME_KUMA_URL"

log "Ensuring Prometheus source job=uptime-kuma (${KUMA_SCHEME}://${KUMA_TARGET}${KUMA_METRICS_PATH})"
KUMA_AUTH_BLOCK=""
if [ -n "$UPTIME_KUMA_API_KEY" ]; then
  KUMA_API_KEY_YAML=$(printf "%s" "$UPTIME_KUMA_API_KEY" | sed "s/'/''/g")
  KUMA_AUTH_BLOCK="
    basic_auth:
      password: '${KUMA_API_KEY_YAML}'"
fi

TMP_PROM=$(mktemp)
cp "$PROM_CONFIG" "$TMP_PROM"
python3 - "$TMP_PROM" "$KUMA_SCHEME" "$KUMA_METRICS_PATH" "$KUMA_TARGET" "$UPTIME_KUMA_API_KEY" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
scheme = sys.argv[2]
metrics_path = sys.argv[3]
target = sys.argv[4]
api_key = sys.argv[5]
safe_key = api_key.replace("'", "''")
lines = path.read_text().splitlines()
out = []
i = 0
replaced = False
while i < len(lines):
    line = lines[i]
    if line.startswith("  - job_name: uptime-kuma"):
        replaced = True
        out.extend([
            "  - job_name: uptime-kuma",
            "    scrape_interval: 30s",
            f"    scheme: {scheme}",
            f"    metrics_path: {metrics_path}",
            "    static_configs:",
            f"      - targets: ['{target}']",
        ])
        if api_key:
            out.extend([
                "    basic_auth:",
                f"      password: '{safe_key}'",
            ])
        i += 1
        while i < len(lines):
            nxt = lines[i]
            if nxt.startswith("  - job_name: ") or (nxt.startswith("- job_name: ") and not nxt.startswith("  - job_name: ")):
                break
            i += 1
        continue
    out.append(line)
    i += 1
if not replaced:
    out.extend([
        "",
        "  - job_name: uptime-kuma",
        "    scrape_interval: 30s",
        f"    scheme: {scheme}",
        f"    metrics_path: {metrics_path}",
        "    static_configs:",
        f"      - targets: ['{target}']",
    ])
    if api_key:
        out.extend([
            "    basic_auth:",
            f"      password: '{safe_key}'",
        ])
path.write_text("\n".join(out) + "\n")
PY

if command -v promtool >/dev/null 2>&1 && ! promtool check config "$TMP_PROM" >/dev/null 2>&1; then
  rm -f "$TMP_PROM"
  die "Prometheus config validation failed; source not added"
fi
cat "$TMP_PROM" > "$PROM_CONFIG"
rm -f "$TMP_PROM"
curl -s -X POST "${PROM_URL%/}/-/reload" >/dev/null 2>&1 || systemctl reload prometheus 2>/dev/null || true
ok "Prometheus source configured"

if [ -z "$MONITOR_API_KEY" ] && [ -f /etc/port-monitor/config.json ]; then
  MONITOR_API_KEY=$(python3 -c "import json; print(json.load(open('/etc/port-monitor/config.json')).get('api_key',''))" 2>/dev/null || true)
fi

if [ -n "$MONITOR_API_KEY" ]; then
  log "Syncing Grafana dashboards and alert rules..."
  body=$(mktemp)
  status=$(curl -s -o "$body" -w "%{http_code}" -X POST "${MONITOR_API_URL%/}/api/v1/grafana/sync" \
    -H "X-Monitor-Key: ${MONITOR_API_KEY}" 2>/dev/null || echo "000")
  if [ "$status" -ge 200 ] 2>/dev/null && [ "$status" -lt 300 ] 2>/dev/null; then
    ok "Grafana sync complete"
  else
    warn "Grafana sync returned ${status}: $(cat "$body" 2>/dev/null)"
  fi
  rm -f "$body"
else
  warn "MONITOR_API_KEY not found; run POST /api/v1/grafana/sync after deploying the API package"
fi
