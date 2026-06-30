#!/usr/bin/env bash
# Enable AWS SES SMTP for Grafana alert emails and verify delivery.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ROOT}/.env"
COMPOSE="docker compose -f ${ROOT}/docker-compose.full.yml"

usage() {
  cat <<'EOF'
Configure AWS SES for Zentra alert emails.

Prerequisites (AWS Console):
  1. SES → Verified identities: verify your domain (e.g. ankercloud.com) or FROM email
  2. SES → SMTP settings → Create SMTP credentials (save username + password)
  3. If SES is in sandbox mode, also verify each recipient email you want to test

Usage:
  ./scripts/configure-ses.sh                         # validate .env and enable SMTP
  ./scripts/configure-ses.sh --test you@mail.com     # send test email via SES SMTP
  ./scripts/configure-ses.sh --set-admin-from-env    # push ALERT_ADMIN_EMAIL to Grafana

Required in .env:
  SMTP_USER          SES SMTP username (starts with AKIA...)
  SMTP_PASSWORD      SES SMTP password
  SMTP_FROM          Verified sender, e.g. alerts@ankercloud.com

Optional in .env:
  SMTP_HOST          default: email-smtp.eu-central-1.amazonaws.com:587 (must match SES credential region)
  SMTP_FROM_NAME     default: Zentra Monitoring
  ALERT_ADMIN_EMAIL  admin alert recipient (contact point email-zentra)
EOF
}

TEST_EMAIL=""
SET_ADMIN_FROM_ENV=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --test) TEST_EMAIL="${2:-}"; shift 2 ;;
    --set-admin-from-env) SET_ADMIN_FROM_ENV=true; shift ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing ${ENV_FILE}. Copy .env.example to .env first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source <(grep -E '^(SMTP_|GRAFANA_ADMIN_|MONITOR_API_KEY|ALERT_ADMIN_EMAIL|GRAFANA_ROOT_URL)' "$ENV_FILE" | grep -v '^#' | sed 's/^/export /')

missing=()
[[ -z "${SMTP_USER:-}" || "$SMTP_USER" == your-ses-smtp-username ]] && missing+=("SMTP_USER")
[[ -z "${SMTP_PASSWORD:-}" || "$SMTP_PASSWORD" == your-ses-smtp-password ]] && missing+=("SMTP_PASSWORD")
[[ -z "${SMTP_FROM:-}" || "$SMTP_FROM" == alerts@yourdomain.com ]] && missing+=("SMTP_FROM")

if ((${#missing[@]} > 0)); then
  echo "Set these in ${ENV_FILE} before running:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  echo >&2
  echo "AWS SES → SMTP settings → Create SMTP credentials" >&2
  echo "AWS SES → Verified identities → verify ${SMTP_FROM:-your domain}" >&2
  exit 1
fi

if ! grep -q '^SMTP_ENABLED=true' "$ENV_FILE"; then
  if grep -q '^SMTP_ENABLED=' "$ENV_FILE"; then
    sed -i 's/^SMTP_ENABLED=.*/SMTP_ENABLED=true/' "$ENV_FILE"
  else
    echo "SMTP_ENABLED=true" >> "$ENV_FILE"
  fi
  echo "Enabled SMTP_ENABLED=true in .env"
fi

echo "Restarting Grafana with SES SMTP..."
$COMPOSE up -d grafana

echo "Waiting for Grafana..."
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:3000/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASS="${GRAFANA_ADMIN_PASS:-admin}"

echo "Syncing alert templates..."
if [[ -n "${MONITOR_API_KEY:-}" ]]; then
  curl -sf -X POST "http://127.0.0.1:9099/api/v1/grafana/alerts/sync" \
    -H "X-Monitor-Key: ${MONITOR_API_KEY}" >/dev/null || true
fi

if [[ "$SET_ADMIN_FROM_ENV" == true && -n "${ALERT_ADMIN_EMAIL:-}" && -n "${MONITOR_API_KEY:-}" ]]; then
  curl -sf -X PUT "http://127.0.0.1:9099/api/v1/alert-recipients/admin" \
    -H "Content-Type: application/json" \
    -H "X-Monitor-Key: ${MONITOR_API_KEY}" \
    -d "{\"recipients\":\"${ALERT_ADMIN_EMAIL}\",\"enabled\":true}" >/dev/null || true
  echo "Admin alert recipient set to: ${ALERT_ADMIN_EMAIL}"
fi

if [[ -n "$TEST_EMAIL" ]]; then
  echo "Sending test email to ${TEST_EMAIL} via SES..."
  SMTP_HOST="${SMTP_HOST:-email-smtp.eu-central-1.amazonaws.com:587}"
  SMTP_FROM_NAME="${SMTP_FROM_NAME:-Zentra Monitoring}"
  LOGO_URL="${MONITOR_PUBLIC_URL:-https://zentra.ankercloud.com/monitor-api}"
  LOGO_URL="${LOGO_URL%/}/alert/logo-email.png"
  SES_TEST_HTML="${ROOT}/alert/ses-test.html"
  export SMTP_HOST SMTP_USER SMTP_PASSWORD SMTP_FROM SMTP_FROM_NAME TEST_EMAIL LOGO_URL SES_TEST_HTML
  python3 <<'PY'
import os, smtplib, sys
from email.message import EmailMessage
from pathlib import Path

host, port = os.environ["SMTP_HOST"].rsplit(":", 1)
to = os.environ["TEST_EMAIL"]
from_addr = os.environ["SMTP_FROM"]
from_name = os.environ.get("SMTP_FROM_NAME", "Zentra Monitoring")
html = Path(os.environ["SES_TEST_HTML"]).read_text().replace("__LOGO_URL__", os.environ["LOGO_URL"])
text = (
    "SES SMTP is working from Zentra Monitoring.\n\n"
    "This message is auto-generated. Please do not reply.\n"
    f"{from_addr}\n"
)

msg = EmailMessage()
msg["From"] = f"{from_name} <{from_addr}>"
msg["To"] = to
msg["Subject"] = "[Zentra] SES test"
msg.set_content(text)
msg.add_alternative(html, subtype="html")

try:
    with smtplib.SMTP(host, int(port), timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        smtp.send_message(msg)
except Exception as exc:
    print(f"Test failed: {exc}", file=sys.stderr)
    sys.exit(1)
print(f"Test email sent to {to}. Check inbox/spam.")
PY
fi

echo
echo "SES configured."
echo "  From: ${SMTP_FROM_NAME:-Zentra Monitoring} <${SMTP_FROM}>"
echo "  Host: ${SMTP_HOST:-email-smtp.eu-central-1.amazonaws.com:587}"
echo
echo "Next: set alert recipients in Zentra UI (Clients & Accounts → Admin alert email)"
echo "Test: ./scripts/configure-ses.sh --test your@email.com"
