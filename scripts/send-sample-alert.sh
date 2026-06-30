#!/usr/bin/env bash
# Send a sample Zentra alert email (HTML) via SES SMTP.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${ROOT}/.env"
SAMPLE_HTML="${ROOT}/alert/sample-alert.html"
TO="${1:-rohit.pt@ankercloud.com}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing ${ENV_FILE}" >&2
  exit 1
fi

if [[ ! -f "$SAMPLE_HTML" ]]; then
  echo "Missing ${SAMPLE_HTML}" >&2
  exit 1
fi

# shellcheck disable=SC1091
source <(grep -E '^(SMTP_|MONITOR_PUBLIC_URL)' "$ENV_FILE" | grep -v '^#' | sed 's/^/export /')

LOGO_URL="${MONITOR_PUBLIC_URL:-https://zentra.ankercloud.com/monitor-api}"
LOGO_URL="${LOGO_URL%/}/alert/logo-email.png"

export TO SMTP_HOST SMTP_USER SMTP_PASSWORD SMTP_FROM
export SMTP_FROM_NAME="${SMTP_FROM_NAME:-Zentra Monitoring}"
export LOGO_URL SAMPLE_HTML

python3 <<'PY'
import os, smtplib, sys
from email.message import EmailMessage
from pathlib import Path

html = Path(os.environ["SAMPLE_HTML"]).read_text().replace("__LOGO_URL__", os.environ["LOGO_URL"])

text = """Zentra Monitoring Alert
Status: firing - Process Down
Severity: critical
Summary: Process docker DOWN on stg-django (stg-django)

Name: stg-django
Process: docker
Host: stg-django
Customer: internal
IP: 172.31.21.169
Account: Infra-pulse

Firing
Started: 2026-06-30 12:00:00 +0000 UTC
Last started: 2026-06-30 12:00:00 +0000 UTC
Duration: 2h 15m 0s

Zentra Monitoring notification.
This message is auto-generated. Please do not reply.
zentra@ankercloud.com
"""

to = os.environ["TO"]
from_addr = os.environ["SMTP_FROM"]
from_name = os.environ.get("SMTP_FROM_NAME", "Zentra Monitoring")
host, port = os.environ["SMTP_HOST"].rsplit(":", 1)

msg = EmailMessage()
msg["From"] = f"{from_name} <{from_addr}>"
msg["To"] = to
msg["Subject"] = "[Zentra] FIRING Process Down · CRITICAL"
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
    print(f"Failed to send sample alert: {exc}", file=sys.stderr)
    sys.exit(1)

print(f"Sample alert sent to {to}")
PY
