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
- [Client Access (Multi-Tenancy)](#client-access-multi-tenancy)
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

## Client Access (Multi-Tenancy)

Give each client a **read-only Grafana login** that shows **only their own servers**. This uses one Grafana instance with a separate **Organization per client**. Clients are Viewers and cannot see or query other clients' data.

```
Main Org (you)                    Client Org "Acme"            Client Org "Globex"
──────────────                    ─────────────────            ───────────────────
All dashboards + manager          "My Servers" dashboard       "My Servers" dashboard
Assign server → client            Viewer users only            Viewer users only
See everything                    client="acme" hardcoded      client="globex" hardcoded
        │
        └── all read from one shared Prometheus
```

### Concept

- A server's **client** is set in the Main Org (All Servers page or API). This is just a label in the Central API.
- Each client gets a Grafana **Org** with its own datasources and a **My Servers** dashboard whose queries are hardcoded to that client.
- Client users are **Viewers** in only their org — they cannot edit queries, so they cannot escape their client filter.

### Step 1 — Assign a server to a client

Via the Main Org UI: open **All Servers**, click the server, set **Client**, **Account**, and **Name**, then Save.

Or via API:

```bash
API=http://CENTRAL_IP:9099

curl -X PUT "$API/api/v1/servers/HOSTNAME" \
  -H "Content-Type: application/json" \
  -d '{"client":"acme","account":"prod","name":"Acme Web 1"}'
```

Verify the client now has servers:

```bash
curl "$API/client-hosts?client=acme&account=.*"
curl "$API/client-accounts?client=acme"
```

### Step 2 — Create the client Org

Grafana org/user management needs Grafana **admin** basic auth (default `admin:admin`).

```bash
GRAFANA=http://CENTRAL_IP:3000
ADMIN='admin:admin'

# Create the org → note the returned orgId
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/orgs" \
  -H "Content-Type: application/json" \
  -d '{"name":"Client - acme"}'
# => {"orgId": 3, ...}
ORGID=3
```

### Step 3 — Add datasources to the client Org

```bash
# Point admin at the new org first
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/user/using/$ORGID"

# Prometheus (note the returned uid)
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/datasources" \
  -H "Content-Type: application/json" \
  -d '{"name":"Prometheus","type":"prometheus","access":"proxy","url":"http://localhost:9090","isDefault":true}'

# Infinity (for client-scoped dropdowns)
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/datasources" \
  -H "Content-Type: application/json" \
  -d '{"name":"Port Monitor API","type":"yesoreyeram-infinity-datasource","access":"proxy","url":"http://localhost:9099"}'
```

Record both returned **uids** — you'll put them in the dashboard JSON.

### Step 4 — Deploy the client dashboard

1. Copy `client-dashboards/internal-summary.json` to `client-dashboards/acme-summary.json`.
2. Edit the copy and replace:
   - `client-internal-summary` → `client-acme-summary` (the `uid`)
   - every `internal` → `acme` (the constant variable + all query filters + endpoint URLs)
   - the Prometheus datasource `uid` → your client org's Prometheus uid
   - the Infinity datasource `uid` → your client org's Infinity uid
3. Deploy into the client org:

```bash
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/user/using/$ORGID" >/dev/null
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/dashboards/db" \
  -H "Content-Type: application/json" \
  -d @client-dashboards/acme-summary.json
```

### Step 5 — Create the client (Viewer) user

```bash
# Create the user
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/admin/users" \
  -H "Content-Type: application/json" \
  -d '{"name":"Acme Client","login":"acme-client","password":"ChangeMe@2026"}'
# => {"id": 4, ...}
USERID=4

# Add to the client org as Viewer
curl -s -u "$ADMIN" -X POST "$GRAFANA/api/orgs/$ORGID/users" \
  -H "Content-Type: application/json" \
  -d '{"loginOrEmail":"acme-client","role":"Viewer"}'

# Remove from Main Org (so they cannot see everything)
curl -s -u "$ADMIN" -X DELETE "$GRAFANA/api/orgs/1/users/$USERID"
```

The client now logs in at `http://CENTRAL_IP:3000` with `acme-client` / their password and sees only the **My Servers** dashboard scoped to `acme`.

### Client-scoped API endpoints

These return only live-assigned servers for a client (used by client dashboards):

| Endpoint | Returns |
|----------|---------|
| `GET /client-accounts?client=X` | Accounts that client actually has servers in |
| `GET /client-hosts?client=X&account=Y` | Servers for that client (and optional account) |

### Reassign or remove a client's server

Change the server's client in the Main Org (or via the API `PUT` above). The client dashboards update within ~30s — no node reinstall needed. Setting client back to `Unassigned` removes it from any client view.

### Example already built

| Item | Value |
|------|-------|
| Org | `Client - internal` (id 2) |
| Dashboard | `My Servers` → `/d/client-internal-summary` |
| Login | `internal-client` / `Internal@2026` (Viewer) |

## File Layout

```
monitoring/
├── setup-server.sh        # Central server (run once)
├── install-alloy.sh       # Node agent (run on every server)
├── api/                   # Central Monitoring API (FastAPI + uvicorn)
│   ├── app/
│   │   ├── main.py        # FastAPI app — all routes
│   │   ├── config.py      # env vars / paths / config loader
│   │   ├── storage.py     # nodes/ports/taxonomy JSON I/O + Prometheus sync
│   │   ├── grafana.py     # Grafana Admin API helpers + client dashboard builder
│   │   └── ui/            # embedded HTML/JS templates (served from :9099)
│   ├── requirements.txt
│   └── run.py             # local entrypoint (uvicorn)
├── scripts/
│   ├── patch-alloy-port-probes.sh  # Fix per-port labels
│   └── fix-dashboards.py           # Normalize dashboard placeholders
├── dashboards/            # Main Org dashboard JSON files
└── client-dashboards/     # Per-client "My Servers" dashboards (templated)
```

The Central Monitoring API runs as a **FastAPI** app under **uvicorn** (systemd
service `port-monitor-api`, `ExecStart=/opt/port-monitor-api/.venv/bin/uvicorn app.main:app`).
It uses the data files (`/var/lib/port-monitor/*`), env vars, and serves the
embedded UI from `:9099`. See `api/README.md` for details.

## Alerting

Email alerts are delivered via **AWS SES SMTP** (configured in `grafana.ini`,
region `ap-south-1`). Five global alert rules live in the **Monitoring** folder
and are fully editable in Grafana's native Alerting UI (`/alerting/list`):

| Rule | Condition | For | Severity |
|------|-----------|-----|----------|
| Node Down | `up{job="integrations/unix"} == 0` | 2m | critical |
| Port Down | `probe_success{job="blackbox"} == 0` | 2m | critical |
| High CPU Usage | CPU > 90% | 5m | warning |
| High Memory Usage | Memory > 90% | 5m | warning |
| Low Disk Space | `/` usage > 90% | 5m | warning |

The main **Fleet Overview** dashboard has an "Active Alerts" panel for an
at-a-glance view.

### Rich alert info (name / IP / client / account)

The API publishes authoritative node metadata at `:9099/metrics` as
`monitor_node_info{host,hostname,name,ip,client,account}`, scraped by Prometheus
(job `monitor-api`). Every alert rule joins it via
`... * on(host) group_left(ip,name,client,account) monitor_node_info`, so alert
emails always show the server's **display name, IP, client, and account** —
sourced from the central API, independent of (and overriding) any drift in the
node-side metric labels. Alert annotations render a full block:
`Server / Host / IP / Client / Account`.

### Per-client alert recipients

Set from **All Servers → Clients & Accounts → Alert recipient email**. Each
client can have **one or more** recipient emails (comma-separated); the admin
(contact point `email-zentra`) always receives every alert as a fallback.

Routing is by **host membership** (not the metric `client` label): when you
save a client's email, the API creates a `client-<slug>` contact point and a
notification-policy route matching `host =~ "(their hosts)"`. The host regex is
regenerated automatically whenever servers are assigned/reassigned/removed, so
routing stays correct without touching nodes. Data: `/var/lib/port-monitor/alert_recipients.json`.

Relevant endpoints:
- `GET /api/v1/alert-recipients`
- `GET /api/v1/clients/{client}/alert-email`
- `PUT /api/v1/clients/{client}/alert-email` `{emails, enabled}` (emails = list or comma-separated string)
- `GET/PUT /api/v1/alert-recipients/all-clients` `{recipients, enabled}` — recipients that receive **every** alert across all clients (NOC/ops). Set via the "All-clients alert recipients" card on the Clients & Accounts tab.
- `GET/PUT /api/v1/alert-recipients/admin` — the admin/fallback contact point (`email-zentra`); always receives every alert.

### Alert groups (multiple servers, shared recipients)

A reusable **alert group** is a named set of servers (across any client/account) with its own recipient list — like Site24x7's User Alert Group + Monitor Group. Managed from **All Servers → Alert Groups**. A server can belong to several groups; each group's enabled recipients are notified for that server's alerts (alongside admin and any per-client recipient), routed by `host=~"(group hosts)"`. Data: `/var/lib/port-monitor/alert_groups.json`.

- `GET /api/v1/alert-groups`
- `POST /api/v1/alert-groups` `{name, hosts, recipients, enabled}`
- `GET/PUT/DELETE /api/v1/alert-groups/{id}`

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
