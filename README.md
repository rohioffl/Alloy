# Alloy — Grafana Server Monitoring Platform

[![CI](https://github.com/rohioffl/Alloy/actions/workflows/ci.yml/badge.svg)](https://github.com/rohioffl/Alloy/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Stack](https://img.shields.io/badge/stack-Grafana%20%7C%20Prometheus%20%7C%20Alloy-orange)](#architecture)
[![Version](https://img.shields.io/github/v/release/rohioffl/Alloy)](https://github.com/rohioffl/Alloy/releases)

Centralized server monitoring with **Grafana Alloy** on each node and a **Central Monitoring API** for server inventory, port probing, and client/account management.

## Table of Contents

- [Architecture](#architecture)
- [Dashboards](#dashboards)
- [Production Setup — Central Server](#production-setup--central-server)
- [Install a Node](#install-a-node)
- [Token Reference](#token-reference)
- [Manage Ports](#manage-ports)
- [File Layout](#file-layout)
- [Troubleshooting](#troubleshooting)

## Architecture

```
Central Server                          Node (Alloy Agent)
─────────────────                       ──────────────────
Prometheus :9090  ◄── remote_write ───  unix + process + blackbox metrics
Grafana :3000                                    │
Central API :9099 ◄── GET /targets ───  polls every 30s for port list
```

## Dashboards

| Dashboard | Purpose |
|-----------|---------|
| **All Servers** | Server list + Clients & Accounts org manager |
| **Server Drill-Down (Summary)** | Health overview per host |
| **CPU / Memory / Disks / Network / Ports / Processes** | Deep dives per host |

## Production Setup — Central Server

Run once on the Grafana/Prometheus host:

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/setup-server.sh -o setup-server.sh
sudo bash setup-server.sh \
  -grafana-url=http://localhost:3000 \
  -grafana-key=glsa_xxxxx \
  -api-public-url=http://YOUR_PUBLIC_IP:9099
```

This installs Prometheus, Central Monitoring API, all Grafana dashboards, and Alloy on the central host.
Copy the **install token** printed at the end — every node install needs it.

**Security:** Restrict port `9099` to your VPC (security group / `ufw`).

## Install a Node

```bash
# One-liner (recommended)
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
  sudo bash -s -- \
    -remote-write=http://CENTRAL_IP:9090/api/v1/write \
    -install-token=YOUR_INSTALL_TOKEN
```

### Optional flags

| Flag | Description |
|------|-------------|
| `-remote-write=URL` | Central Prometheus remote_write endpoint |
| `-install-token=TOKEN` | From central `config.json` |
| `-api-url=URL` | Central Monitoring API (default `:9099`) |
| `-processes=NAMES` | Comma-separated process names to monitor |
| `-uninstall` | Remove Alloy agent only |

Within ~30 seconds the node appears in Grafana. Assign Client, Account, and Display Name in the **All Servers** dashboard.

## Token Reference

| Token | Header | Used By | Purpose |
|-------|--------|---------|---------|
| Install token | `X-Install-Token` | `install-alloy.sh` | Register nodes with the Central API |
| API key | `X-Monitor-Key` | Scripts, automation | Bulk imports, CI, server-side operations |

## Manage Ports

### Via Grafana (recommended)

1. Open **Server Drill-Down → Ports**
2. Select host → expand **Manage Monitored Ports**

### Via API

```bash
API=http://CENTRAL_IP:9099
KEY=your_api_key

curl -X POST "$API/api/v1/servers/HOSTNAME/ports" \
  -H "X-Monitor-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"name":"redis","port":"6379","module":"tcp_connect"}'
```

Full docs: `GET http://CENTRAL_IP:9099/api/v1/docs`

## File Layout

```
monitoring/
├── setup-server.sh        # Central server (run once)
├── install-alloy.sh       # Node agent (run on every server)
├── port-monitor-api.py    # Central API service
├── scripts/
│   ├── patch-alloy-port-probes.sh  # Fix per-port labels
│   └── fix-dashboards.py           # Normalize dashboard placeholders
└── dashboards/            # Grafana dashboard JSON files
```

## Troubleshooting

```bash
# Check service status
systemctl status port-monitor-api alloy prometheus grafana-server

# View API logs
journalctl -u port-monitor-api -f

# Health check
curl http://localhost:9099/api/v1/health

# Verify metrics flowing
curl -s 'http://localhost:9090/api/v1/query?query=up{job="integrations/unix"}'
```

**Dropdowns empty?** Check Grafana → Connections → Data sources → Port Monitor API points to `http://127.0.0.1:9099` (no trailing slash).

**Ports show one line?** Patch Alloy port probes:
```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/scripts/patch-alloy-port-probes.sh | sudo bash
```

---

**Author:** Rohit P T | Cloud Automation Engineer @ Ankercloud
