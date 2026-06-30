#!/usr/bin/env bash
# Patch /etc/alloy/config.alloy so each port probe keeps a distinct "port" label
# (fixes Grafana showing only one port / "integrations/unix" in Ports dashboard).
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root: sudo bash $0"; exit 1; }
CFG=/etc/alloy/config.alloy
[ -f "$CFG" ] || { echo "No $CFG — run install-alloy.sh first"; exit 1; }

if grep -q 'prometheus.relabel "blackbox_labels"' "$CFG"; then
  echo "Already patched: blackbox_labels present"
  exit 0
fi

cp -a "$CFG" "${CFG}.bak.$(date +%s)"

python3 << 'PY'
import pathlib
import re

cfg = pathlib.Path("/etc/alloy/config.alloy")
text = cfg.read_text()

text = text.replace(
    "prometheus.scrape \"blackbox_metrics\" {\n"
    "  targets         = prometheus.exporter.blackbox.endpoints.targets\n"
    "  forward_to      = [prometheus.relabel.add_host_label.receiver]",
    "prometheus.scrape \"blackbox_metrics\" {\n"
    "  targets         = prometheus.exporter.blackbox.endpoints.targets\n"
    "  forward_to      = [prometheus.relabel.blackbox_labels.receiver]",
)

block = '''
prometheus.relabel "blackbox_labels" {
  forward_to = [prometheus.remote_write.central.receiver]

  rule {
    target_label = "host"
    replacement  = constants.hostname
  }
  rule {
    source_labels = ["job"]
    regex         = "(?:integrations/blackbox/)?(.+)"
    target_label  = "port"
    replacement   = "$1"
  }
  rule {
    target_label = "job"
    replacement  = "blackbox"
  }
}

'''

if 'prometheus.relabel "blackbox_labels"' not in text:
    text = re.sub(
        r'(// RELABELING|prometheus\.relabel "add_host_label")',
        block + r'\1',
        text,
        count=1,
    )

cfg.write_text(text)
print("Patched", cfg)
PY

systemctl restart alloy
echo "Alloy restarted — port probes will use port=<name> labels within ~30s"
