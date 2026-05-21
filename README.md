# Grafana Alloy — Server Monitoring (Production)

Centralized monitoring with **Grafana Alloy** on each node and a **Central Monitoring API** on the Grafana server for server names and port probes.

**Repository:** https://github.com/rohioffl/Alloy

---

## Architecture

```
Central Server                          Node (Alloy)
─────────────────                       ─────────────
Prometheus :9090  ◄── remote_write ───  unix + process + blackbox metrics
Grafana :3000                             │
Central API :9099 ◄── GET /targets ───  polls every 30s for port list
```

**Dashboards:**

| Dashboard | Purpose |
|-----------|---------|
| **All Servers** | Server list + **Clients & Accounts** org manager (rename/merge); IP read-only |
| **Server Drill-Down (Summary)** | Health overview per host |
| **CPU / Memory / Disks / Network / Ports / Processes** | Deep dives per host |

---

## Production setup (central server)

Run once on the Grafana/Prometheus host:

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/setup-server.sh -o setup-server.sh
sudo bash setup-server.sh \
  -grafana-url=http://localhost:3000 \
  -grafana-key=glsa_xxxxx \
  -api-public-url=http://YOUR_PUBLIC_IP:9099
```

This installs:

- Prometheus `remote_write` receiver
- Central Monitoring API with **install token** + **API key** in `/etc/port-monitor/config.json` (mode `600`)
- All Grafana dashboards in folder **Monitoring**
- **Alloy on the central host** (so Grafana server appears in metrics)
- Grafana HTML panels enabled (for embedded inventory iframe)

At the end, the script prints an **install token**. Copy it — every node install needs it.

Read tokens anytime on the central server:

```bash
sudo cat /etc/port-monitor/config.json
# install_token  → node installs (install-alloy.sh)
# api_key        → optional automation / curl (not required for Grafana UI edits)
```

**Security:** Restrict port **9099** to your VPC/office (security group / `ufw`). Do not expose it to the public internet without a VPN.

---

## Why tokens?

| Token | Header | Who uses it | Why it exists |
|-------|--------|-------------|----------------|
| **Install token** | `X-Install-Token` | `install-alloy.sh` on each node | Only machines that know this secret can call `POST /api/v1/servers/register`. That adds the host to the inventory (hostname, IP) so it shows up in Grafana dropdowns and port targeting. Without it, anyone who can reach `:9099` could register fake servers. |
| **API key** | `X-Monitor-Key` or `Authorization: Bearer` | Scripts, `curl`, automation | Protects manual API operations. **Not** required when you edit name/client/account/ports from the Grafana dashboards — those paths are allowed from the browser without the key. Use the API key for bulk imports, CI, or server-side scripts. |

**You do not pass client, account, or display name at install time.** New nodes register as **Unassigned / default**; rename them in Grafana (see below).

---

## Install a node (from GitHub)

Replace `CENTRAL_IP` and `YOUR_INSTALL_TOKEN` with values from your central server.

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh | \
  sudo bash -s -- \
    -remote-write=http://CENTRAL_IP:9090/api/v1/write \
    -install-token=YOUR_INSTALL_TOKEN
```

### Download then run

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/install-alloy.sh -o install-alloy.sh
sudo bash install-alloy.sh \
  -remote-write=http://CENTRAL_IP:9090/api/v1/write \
  -install-token=YOUR_INSTALL_TOKEN
```

### Optional flags

| Flag | Default | Description |
|------|---------|-------------|
| `-remote-write=URL` | required | Central Prometheus `.../api/v1/write` |
| `-install-token=TOKEN` | — | From central `config.json` or `setup-server.sh` output |
| `-api-url=URL` | `http://CENTRAL_HOST:9099` | Central Monitoring API (use if API is not on :9099) |
| `-loki=URL` | central Grafana Loki | Log shipping (optional) |
| `-processes=NAMES` | `auto` | Comma-separated process names to monitor |
| `-uninstall` | — | Remove Alloy agent only (does not remove central API) |

Optional `-client`, `-account`, and `-name` exist but **prefer setting those in Grafana** after install.

Within ~30 seconds the node sends metrics and registers with the API. Open **All Servers** or **Server Drill-Down** and assign **Client**, **Account**, and **Display name**.

---

## Set client, account & display name (in Grafana)

**Recommended:** open **All Servers** (`/d/alloy-servers/all-servers`).

**Servers tab**

1. Click a row → edit **Display name**, **Client**, **Account** (dropdown or “+ Add new…”)
2. **IP is read-only** (set at Alloy install; re-run `install-alloy.sh` on the node to refresh)
3. **Save**

**Clients & Accounts tab**

- Add/rename/merge/delete clients and accounts
- Renaming or merging **reassigns all servers** under that client/account

Or per host in **Server Drill-Down (Summary)**:

1. Select **Host** in the top toolbar
2. Scroll to **Client · Account · Display Name — edit here**
3. Save, then refresh (F5) so Client/Account dropdowns update

No SSH or install flags needed for naming.

---

## Manage ports

### Grafana

1. **Server Drill-Down · Ports** — select host, expand **Manage Monitored Ports**

### API (optional — use API key)

```bash
API=http://CENTRAL_IP:9099
KEY=your_api_key_from_config_json

curl -X PUT "$API/api/v1/servers/HOSTNAME" \
  -H "Content-Type: application/json" \
  -H "X-Monitor-Key: $KEY" \
  -d '{"name":"My Server","client":"acme","account":"production"}'

curl -X POST "$API/api/v1/servers/HOSTNAME/ports" \
  -H "Content-Type: application/json" \
  -H "X-Monitor-Key: $KEY" \
  -d '{"name":"redis","port":"6379","module":"tcp_connect"}'
```

Docs: `GET http://CENTRAL_IP:9099/api/v1/docs`

---

## File layout

```
monitoring/
├── setup-server.sh        # Central server (run once)
├── install-alloy.sh       # Node agent (run on every server)
├── port-monitor-api.py    # Central API
├── scripts/
│   ├── patch-alloy-port-probes.sh  # Fix per-port labels on existing nodes
│   └── fix-dashboards.py           # Normalize dashboard placeholders
└── dashboards/            # Grafana dashboards
```

---

## Troubleshooting

```bash
systemctl status port-monitor-api alloy prometheus grafana-server
journalctl -u port-monitor-api -f
curl http://localhost:9099/api/v1/health
curl http://localhost:9099/api/v1/servers
```

```bash
# Metrics flowing?
curl -s 'http://localhost:9090/api/v1/query?query=up{job="integrations/unix"}'
```

**Dropdowns empty (Client/Host)?** Grafana needs the **Infinity** datasource pointing at `http://127.0.0.1:9099` (no trailing slash). Re-run `setup-server.sh` or check **Connections → Data sources → Port Monitor API**.

**Ports show one line / `integrations/unix`?** On each node, patch Alloy so port probes keep a `port` label:

```bash
curl -fsSL https://raw.githubusercontent.com/rohioffl/Alloy/main/scripts/patch-alloy-port-probes.sh | sudo bash
```

Then wait ~30s and refresh the Ports dashboard.
