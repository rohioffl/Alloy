# Grafana Alloy — Server Monitoring

Single-script setup for **Grafana Alloy** as a standalone monitoring agent on Linux servers.  
Replaces node_exporter, process-exporter, blackbox_exporter, and promtail in one binary.

---

## Overview

Alloy runs as the **only agent** on each node and handles:

- **Host metrics** — CPU, memory, disk, network, load, filesystem, systemd
- **Process monitoring** — per-process CPU, memory, threads, open FDs
- **Port probing** — TCP/HTTP checks, managed dynamically from Grafana
- **Log collection** — syslog, auth.log shipped to Loki
- **Remote write** — all metrics pushed to central Prometheus

---

## Architecture

```
┌─────────────────── Node Server ──────────────────────┐
│                                                       │
│   Grafana Alloy (:12345)                             │
│     ├─ prometheus.exporter.unix      → host metrics  │
│     ├─ prometheus.exporter.process   → process stats │
│     ├─ prometheus.exporter.blackbox  → port probes   │
│     ├─ loki.source.file             → log lines      │
│     │                                                 │
│     ├─ prometheus.remote_write ───→ Prometheus       │
│     └─ loki.write ───────────────→ Loki             │
│                                                       │
│   Port Monitor API (:9099)                           │
│     ├─ Add/remove ports from Grafana dashboard       │
│     ├─ Rewrites Alloy blackbox config                │
│     └─ Restarts Alloy on changes                     │
│                                                       │
└───────────────────────────────────────────────────────┘
```

---

## Quick Start

```bash
sudo REMOTE_WRITE_URL=http://<prometheus>:9090/api/v1/write \
     GRAFANA_URL=http://<grafana>:3000 \
     GRAFANA_API_KEY=glsa_xxxxx \
     bash install-alloy.sh
```

---

## What the Script Does

| Step | Action |
|------|--------|
| 1 | Installs Grafana Alloy package (apt/dnf/yum) |
| 2 | Auto-discovers listening ports and running processes |
| 3 | Generates `/etc/alloy/config.alloy` |
| 4 | Enables Prometheus `--web.enable-remote-write-receiver` (if local) |
| 5 | Starts Alloy as root (needed for `/proc` access) |
| 6 | Starts Port Monitor API on :9099 |
| 7 | Deploys Grafana dashboards via API |

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOTE_WRITE_URL` | `http://localhost:9090/api/v1/write` | Prometheus remote_write endpoint |
| `LOKI_URL` | `http://localhost:3000/loki/api/v1/push` | Loki push API |
| `GRAFANA_URL` | `http://localhost:3000` | Grafana base URL |
| `GRAFANA_API_KEY` | — | Service account token (needed for dashboards) |
| `ENV_LABEL` | `prod` | Value for the `env` label on all metrics |
| `PROBE_TARGETS` | `auto` | `auto` = discover from `ss`, or comma-separated `host:port` |
| `PROCESS_NAMES` | `auto` | `auto` = discover from running services, or comma-separated |
| `SKIP_DASHBOARDS` | `0` | Set to `1` to skip dashboard deployment |

---

## Port Management (from Grafana)

Ports are managed **directly from the Grafana Ports dashboard** — no SSH or config editing needed.

The **Port Monitor API** (:9099) provides:
- A web UI embedded in the Grafana dashboard
- REST API for automation

### From the Dashboard

1. Open **Server Drill-Down · Ports** dashboard
2. Expand **⚙️ Manage Monitored Ports** section
3. Enter a **Name** (e.g. `redis`) and **Port** (e.g. `6379`)
4. Click **+ Add** — Alloy starts probing within ~20 seconds
5. Click **Remove** on any row to stop monitoring

### From CLI / Automation

```bash
# Add a port
curl -X POST http://<server>:9099/ports \
  -H "Content-Type: application/json" \
  -d '{"name":"redis","port":"6379"}'

# Remove a port
curl -X DELETE http://<server>:9099/ports/redis

# List all ports
curl http://<server>:9099/ports

# Web UI
open http://<server>:9099/
```

---

## Process Monitoring

Processes are monitored by matching binary names in `/proc`. The install script auto-discovers common services:

- alloy, grafana, prometheus, sshd, systemd
- nginx, docker, containerd, mysql, postgres, redis, node, java, python

To add a custom process, edit `/etc/alloy/config.alloy`:

```river
matcher {
  name = "myapp"
  comm = ["myapp"]
}
```

Then restart: `sudo systemctl restart alloy`

---

## Dashboards

Seven interconnected dashboards with tabbed navigation:

| Dashboard | Key Panels |
|-----------|-----------|
| **Summary** | Availability, CPU/Mem/Disk %, Ports Up/Down, SLA, System Info, Port table, Process table, Server Details |
| **CPU** | Usage %, per-core, load average, context switches, interrupts |
| **Memory** | Used/available, swap, page faults, breakdown |
| **Disks** | Filesystem %, throughput, IOPS, latency, inode usage |
| **Network** | Traffic (bps), packets/s, errors/drops, TCP connections |
| **Ports** | Status table, timeline, latency + **Add/Remove UI** |
| **Processes** | Top CPU/memory, threads, open FDs per process |

All dashboards use a `$host` variable for multi-server selection.

---

## File Structure

```
monitoring/
├── install-alloy.sh           # Complete setup script
├── port-monitor-api.py        # Port management API + UI
├── README.md                  # This file
└── dashboards/
    ├── summary.json           # Summary drill-down
    ├── cpu.json               # CPU drill-down
    ├── memory.json            # Memory drill-down
    ├── disk.json              # Disk drill-down
    ├── network.json           # Network drill-down
    ├── ports.json             # Ports drill-down (with management UI)
    └── processes.json         # Processes drill-down
```

---

## Files on the Server

```
/etc/alloy/config.alloy                              # Alloy config (generated)
/etc/alloy/probe-targets.json                        # Port targets (managed by API)
/etc/systemd/system/alloy.service.d/override.conf    # Run-as-root override
/etc/systemd/system/port-monitor-api.service         # Port API systemd unit
/var/lib/alloy/data/                                 # WAL and state
```

---

## Uninstall

```bash
sudo bash install-alloy.sh --uninstall
```

---

## Troubleshooting

```bash
# Services
systemctl status alloy
systemctl status port-monitor-api
journalctl -u alloy -f

# Alloy UI
curl http://localhost:12345

# Port API
curl http://localhost:9099/ports

# Verify metrics
curl -s 'http://<prom>:9090/api/v1/query?query=node_cpu_seconds_total{job="integrations/unix"}'
curl -s 'http://<prom>:9090/api/v1/query?query=namedprocess_namegroup_num_procs'
curl -s 'http://<prom>:9090/api/v1/query?query=probe_success'
```

---

## What Alloy Replaces

| Before (5 binaries) | After (1 binary + 1 API) |
|---------------------|--------------------------|
| node_exporter :9100 | `prometheus.exporter.unix` |
| process-exporter :9256 | `prometheus.exporter.process` |
| blackbox_exporter :9115 | `prometheus.exporter.blackbox` |
| promtail | `loki.source.file` |
| local prometheus :9090 | `prometheus.remote_write` |
| manual config edits | Port Monitor API :9099 |
| 5 systemd units | 2 systemd units |
| 5 config files | 1 config file + 1 JSON |
