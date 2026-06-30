# Central Monitoring API (FastAPI)

FastAPI service for server inventory, port probing, client/account taxonomy,
and per-client Grafana org provisioning. Same routes, data files, and embedded
UI as before — structured into a small package so it is easier to extend and
serve under uvicorn.

## Layout

```
api/
├── app/
│   ├── __init__.py
│   ├── config.py        # env vars, paths, load_config()
│   ├── storage.py       # nodes/ports/taxonomy JSON I/O + Prometheus sync
│   ├── grafana.py       # Grafana Admin API helpers + client dashboard builder
│   ├── main.py          # FastAPI app: all routes
│   └── ui/
│       ├── __init__.py  # template loader + placeholder substitution
│       ├── combo.js     # shared client/account combo helpers
│       ├── index.html   # full manager UI  (served at /)
│       ├── nodes.html   # server settings panel  (/nodes-only)
│       ├── ports.html   # port manager panel  (/ports-only)
│       └── inventory.html # All Servers + Clients/Accounts + Client Orgs  (/inventory, /servers)
├── requirements.txt
└── run.py               # local entrypoint (uvicorn)
```

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py            # serves on :9099
```

Or directly with uvicorn:

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 9099
```

## Run with Docker

From the repository root:

```bash
docker compose up -d --build
curl http://localhost:9099/health
```

The compose file runs only the Central Monitoring API. Prometheus and Grafana
are expected to run on the Docker host, so the default URLs use
`host.docker.internal` and map it through Docker's `host-gateway`.

Before using this in production, change these environment variables in
`docker-compose.yml`:

| Variable | Purpose |
|----------|---------|
| `MONITOR_PUBLIC_URL` | Public URL nodes and Grafana should use for the API |
| `MONITOR_PROMETHEUS_URL` | Prometheus URL reachable from inside the container |
| `MONITOR_GRAFANA_URL` | Grafana URL reachable from inside the container |
| `MONITOR_INSTALL_TOKEN` | Token required for node registration |
| `MONITOR_API_KEY` | API key required for destructive/admin operations |

Persistent API data is stored in the `monitor-data` volume and config files in
the `monitor-config` volume.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MONITOR_DATA_DIR` | `/var/lib/port-monitor` | nodes.json, ports/, taxonomy.json, grafana_orgs.json |
| `MONITOR_CONFIG` | `/etc/port-monitor/config.json` | public_url, grafana_url, tokens |
| `MONITOR_HOST` | `0.0.0.0` | bind host |
| `MONITOR_PORT` | `9099` | bind port |
| `MONITOR_PROMETHEUS_URL` | `http://127.0.0.1:9090` | host auto-discovery |
| `MONITOR_GRAFANA_URL` | `http://localhost:3000` | Grafana base for org provisioning |
| `MONITOR_GRAFANA_ADMIN_USER` / `MONITOR_GRAFANA_ADMIN_PASS` | `admin` / `admin` | Grafana Admin API auth |
| `MONITOR_INSTALL_TOKEN` | _empty_ | required by `/api/v1/servers/register` when set |
| `MONITOR_API_KEY` | _empty_ | required for destructive ops when set |

Data files are byte-compatible with the original implementation, so you can
point this at an existing `/var/lib/port-monitor` with no migration.

## Deployment

`setup-server.sh` installs this package to `/opt/port-monitor-api/`, creates a
virtualenv at `/opt/port-monitor-api/.venv`, and runs it under systemd with
uvicorn. The embedded UI is still served from `:9099` (same origin) so the
Grafana iframes keep working unchanged.
