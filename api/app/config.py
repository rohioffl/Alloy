"""Configuration, paths, and environment for the Central Monitoring API.

Data files and env vars are kept compatible with the existing deployment at
/var/lib/port-monitor and /etc/port-monitor.
"""

import json
import os
import time

DATA_DIR = os.environ.get("MONITOR_DATA_DIR", "/var/lib/port-monitor")
CONFIG_FILE = os.environ.get("MONITOR_CONFIG", "/etc/port-monitor/config.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
TAXONOMY_FILE = os.path.join(DATA_DIR, "taxonomy.json")
GRAFANA_ORGS_FILE = os.path.join(DATA_DIR, "grafana_orgs.json")
ALERT_RECIPIENTS_FILE = os.path.join(DATA_DIR, "alert_recipients.json")
ALERT_GROUPS_FILE = os.path.join(DATA_DIR, "alert_groups.json")
# Admin contact point that always receives every alert (catch-all / CC).
ADMIN_RECEIVER = os.environ.get("MONITOR_ADMIN_RECEIVER", "email-zentra")
LISTEN_HOST = os.environ.get("MONITOR_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("MONITOR_PORT", "9099"))
PROMETHEUS_URL = os.environ.get("MONITOR_PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")

DEFAULT_CLIENT = "Unassigned"
DEFAULT_ACCOUNT = "default"
# Reserved recipient key for the admin/fallback contact point (root catch-all).
ADMIN_KEY = "__admin__"


def TIMESTAMP():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_config():
    cfg = {
        "public_url": os.environ.get("MONITOR_PUBLIC_URL", ""),
        "grafana_url": os.environ.get("MONITOR_GRAFANA_URL", "http://localhost:3000"),
        "grafana_admin_user": os.environ.get("MONITOR_GRAFANA_ADMIN_USER", "admin"),
        "grafana_admin_password": os.environ.get("MONITOR_GRAFANA_ADMIN_PASS", "admin"),
        "install_token": os.environ.get("MONITOR_INSTALL_TOKEN", ""),
        "api_key": os.environ.get("MONITOR_API_KEY", ""),
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def install_token():
    return (load_config().get("install_token") or "").strip()


def api_key():
    return (load_config().get("api_key") or "").strip()
