# Grafana Alloy — Server Monitoring (Production)

Centralized monitoring with **Grafana Alloy** on each node and a **Central Monitoring API** on the Grafana server for server names and port probes.

---

## Architecture

```
Central Server                          Node (Alloy)
─────────────────                       ─────────────
Prometheus :9090  ◄── remote_write ───  unix + process + blackbox metrics
Grafana :3000                             │
Central API :9099 ◄── GET /targets ───  polls every 30s for port list
```

**Dashboards (7 only — no separate admin dashboard):**

| Dashboard | Purpose |
|-----------|---------|
| **All Servers** | Full list (assigned + Unassigned) — edit name, client, account inline |
| **Server Drill-Down (Summary)** | Health overview per host |
| **CPU / Memory / Disks / Network / Ports / Processes** | Deep dives per host |

---

## Production setup (central server)

```bash
sudo bash setup-server.sh \
  -grafana-url=http://localhost:3000 \
  -grafana-key=glsa_xxxxx \
  -api-public-url=http://YOUR_PUBLIC_IP:9099
```

This installs:

- Prometheus `remote_write` receiver
- Central Monitoring API with **install token** + **API key** (`/etc/port-monitor/config.json`, mode `600`)
- All 8 Grafana dashboards in folder **Monitoring**
- Grafana HTML panels enabled (for embedded server editor)

**Save the install token** printed at the end — required for every new node.

**Security:** Restrict port **9099** to your VPC/office (security group / `ufw`). Do not expose it to the public internet without a VPN.

---

## Install a node

```bash
curl -fsSL .../install-alloy.sh | sudo bash -s -- \
  -remote-write=http://CENTRAL_IP:9090/api/v1/write \
  -install-token=YOUR_INSTALL_TOKEN
```

| Flag | Default | Description |
|------|---------|-------------|
| `-remote-write=URL` | required | Prometheus endpoint |
| `-install-token=TOKEN` | — | From `setup-server.sh` (required in production) |

Optional install flags (`-client`, `-account`, `-name`) exist but **you should set these in Grafana** instead (see below).

New nodes appear as **Unassigned / default** within ~30 seconds. Then name them in the dashboard.

---

## Set client, account & display name (in Grafana)

**Recommended:** open **All Servers** — full list with inline editor.

1. Open **All Servers** (`/d/alloy-servers/all-servers`)
2. Click any row (including **Unassigned**)
3. Edit **Display name**, **Client**, **Account**, **IP** → **Save**
4. Or use **Server Drill-Down (Summary)** per host:

1. Open **Server Drill-Down (Summary)**
2. Select **Host** in the top toolbar
3. Scroll to **Client · Account · Display Name — edit here**
4. Fill in **Display name**, **Client**, **Account** → click **Save to monitoring**
5. Press **F5** to refresh — Client/Account dropdowns update

No SSH or install flags needed for naming.

---

## Manage ports

### Grafana

1. **Server Drill-Down · Ports** — select host, expand **⚙️ Manage Monitored Ports**

### API

```bash
API=http://CENTRAL_IP:9099
KEY=your_api_key_from_setup

# Rename / set client & account
curl -X PUT "$API/api/v1/servers/HOSTNAME" \
  -H "Content-Type: application/json" \
  -H "X-Monitor-Key: $KEY" \
  -d '{"name":"My Server","client":"acme","account":"production"}'

# Add port (probes server registered IP)
curl -X POST "$API/api/v1/servers/HOSTNAME/ports" \
  -H "Content-Type: application/json" \
  -H "X-Monitor-Key: $KEY" \
  -d '{"name":"redis","port":"6379","module":"tcp_connect"}'
```

Docs: `GET /api/v1/docs`

---

## File layout

```
monitoring/
├── setup-server.sh        # Central server (run once)
├── install-alloy.sh       # Node agent
├── port-monitor-api.py    # Central API
└── dashboards/          # 7 Grafana dashboards
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
