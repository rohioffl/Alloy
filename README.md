# Grafana Alloy — Server Monitoring

Lightweight monitoring stack using **Grafana Alloy** as the only agent on each node.  
No node_exporter, process-exporter, blackbox_exporter, or promtail needed.

---

## Architecture

```
┌──────────────────── Central Server ─────────────────────────┐
│                                                              │
│  Prometheus (:9090)  ← receives metrics from all nodes      │
│  Grafana (:3000)     ← dashboards                           │
│  Port Monitor API (:9099) ← manages probe targets per node  │
│                                                              │
└──────────────────────────────────────────────────────────────┘
         ▲ remote_write metrics          ▲ GET /targets/<host>
         │                               │ (every 30s)
┌────────┴──────── Node Server ──────────┴────────────────────┐
│                                                              │
│  Grafana Alloy (:12345)                                     │
│    ├─ Host metrics    (CPU, mem, disk, net, load)           │
│    ├─ Process monitor (per-process CPU/mem/threads)         │
│    ├─ Port probing    (pulls targets from central API)      │
│    └─ Log collection  (syslog, auth.log → Loki)            │
│                                                              │
└──────────────────────────────────────────────────────────────┘
```

---

## Setup

### Step 1 — Central Server (run once)

```bash
git clone https://github.com/rohioffl/Alloy.git /tmp/alloy-setup
sudo bash /tmp/alloy-setup/setup-server.sh \
  -grafana-url=http://localhost:3000 \
  -grafana-key=glsa_xxxxx
```

This sets up:
- Prometheus `--web.enable-remote-write-receiver`
- Port Monitor API on `:9099`
- All Grafana dashboards deployed

### Step 2 — Each Node Server

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
  sudo bash -s -- -remote-write=http://<central-ip>:9090/api/v1/write
```

The node appears in Grafana within 30 seconds.

---

## Scripts

| Script | Purpose |
|--------|---------|
| `setup-server.sh` | Run once on central server — sets up API, dashboards, Prometheus |
| `install-alloy.sh` | Run on each node — installs Alloy and connects to central |
| `port-monitor-api.py` | Port Monitor API (auto-installed by setup-server.sh) |

---

## Install Flags

### `setup-server.sh`

| Flag | Default | Description |
|------|---------|-------------|
| `-grafana-url=URL` | `http://localhost:3000` | Grafana base URL |
| `-grafana-key=TOKEN` | — | Service account token (required for dashboards) |

### `install-alloy.sh`

| Flag | Default | Description |
|------|---------|-------------|
| `-remote-write=URL` | `http://localhost:9090/api/v1/write` | Prometheus remote_write endpoint |
| `-loki=URL` | `http://localhost:3000/loki/api/v1/push` | Loki push endpoint |
| `-processes=NAMES` | `auto` | Comma-separated process names or `auto` |
| `-uninstall` | — | Remove Alloy completely |

---

## Port Management

Ports are managed from the **Grafana Ports dashboard** — no SSH needed.

Each node's Alloy polls `http://<central>:9099/targets/<hostname>` every 30 seconds.  
When you add/remove a port in the dashboard, the node picks it up within 30 seconds.

### From Grafana Dashboard
1. Open **Server Drill-Down · Ports**
2. Expand **⚙️ Manage Monitored Ports**
3. Select the host, enter Name + Port, click **+ Add**

### From CLI
```bash
# Add a port for a specific host
curl -X POST http://<central>:9099/ports/<hostname> \
  -H "Content-Type: application/json" \
  -d '{"name":"redis","port":"6379"}'

# Remove a port
curl -X DELETE http://<central>:9099/ports/<hostname>/redis

# List ports for a host
curl http://<central>:9099/ports/<hostname>

# List all hosts
curl http://<central>:9099/hosts
```

---

## Dashboards

| Dashboard | Description |
|-----------|-------------|
| **Summary** | Availability, CPU/Mem/Disk, Ports, System Info, Top Processes, Server Details |
| **CPU** | Usage %, per-core, load average, context switches |
| **Memory** | RAM, swap, page faults, breakdown |
| **Disks** | Filesystem usage, IOPS, latency, inode |
| **Network** | Traffic, packets, errors, TCP connections |
| **Ports** | Status table, timeline, latency + Add/Remove UI |
| **Processes** | Top CPU/memory, threads, FDs per process |

All dashboards use a `$host` variable — select any monitored server.

---

## File Structure

```
monitoring/
├── setup-server.sh        # Central server setup (run once)
├── install-alloy.sh       # Node agent installer
├── port-monitor-api.py    # Port Monitor API
├── README.md
└── dashboards/
    ├── summary.json
    ├── cpu.json
    ├── memory.json
    ├── disk.json
    ├── network.json
    ├── ports.json
    └── processes.json
```

---

## Uninstall Node

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
  sudo bash -s -- -uninstall
```

---

## Troubleshooting

```bash
# Node — check Alloy
systemctl status alloy
journalctl -u alloy -f

# Central — check Port API
systemctl status port-monitor-api
curl http://localhost:9099/hosts

# Verify node metrics in Prometheus
curl -s 'http://localhost:9090/api/v1/query?query=up{job="integrations/unix"}'

# Verify port probes
curl -s 'http://localhost:9090/api/v1/query?query=probe_success'
```

---

## What Alloy Replaces

| Before | After |
|--------|-------|
| node_exporter | `prometheus.exporter.unix` |
| process-exporter | `prometheus.exporter.process` |
| blackbox_exporter | `prometheus.exporter.blackbox` |
| promtail | `loki.source.file` |
| manual port config | Port Monitor API + Grafana UI |
