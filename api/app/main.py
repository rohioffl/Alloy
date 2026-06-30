"""Central Monitoring API (FastAPI) — server registry, naming, port probe targets,
client/account taxonomy, and per-client Grafana org provisioning.

Runs on the central Grafana/Prometheus server (:9099). Serves both the REST API
and the embedded management UI (same origin, so Grafana iframes work directly).
"""

import os
import html
import json

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from .config import (
    DATA_DIR,
    CONFIG_FILE,
    GRAFANA_ORGS_FILE,
    NODES_FILE,
    ADMIN_RECEIVER,
    ADMIN_KEY,
    api_key,
    install_token,
    load_config,
)
from . import grafana as gf
from . import storage as st
from . import ui

app = FastAPI(title="Central Monitoring API", version="v1", docs_url=None, redoc_url=None)


# ---- startup ----------------------------------------------------------------

@app.on_event("startup")
def _startup():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "ports"), exist_ok=True)
    os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
    if not os.path.exists(NODES_FILE):
        st._write_json(NODES_FILE, [])
    else:
        st.migrate_nodes()


# ---- CORS (mirrors original dynamic allow-origin) ---------------------------

def _cors_origin(request: Request) -> str:
    origin = (request.headers.get("origin") or "").strip().rstrip("/")
    cfg = load_config()
    allowed = {
        (cfg.get("public_url") or "").strip().rstrip("/"),
        (cfg.get("grafana_url") or "").strip().rstrip("/"),
    }
    allowed.discard("")
    if origin and origin in allowed:
        return origin
    if not origin and allowed:
        return next(iter(allowed))
    return ""


@app.middleware("http")
async def _cors_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        response = JSONResponse({})
    else:
        response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = _cors_origin(request)
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-Monitor-Key, X-Install-Token, Authorization"
    )
    return response


# ---- helpers ----------------------------------------------------------------

def J(data, code=200):
    return JSONResponse(data, status_code=code)


async def _body(request: Request):
    try:
        return await request.json()
    except Exception:
        return {}


def _auth_header(request: Request) -> str:
    key = request.headers.get("x-monitor-key", "")
    if key:
        return key
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _require_install_token(request: Request) -> bool:
    expected = install_token()
    if not expected:
        return True
    return request.headers.get("x-install-token", "") == expected


def _require_write_auth(request: Request) -> bool:
    """API key for destructive (server delete / manual add) ops."""
    expected = api_key()
    if not expected:
        return True
    return _auth_header(request) == expected


def _sync_alerting():
    """Ensure each client's email contact point reflects its enabled addresses
    and rebuild the notification policy (host-membership routing). Safe to call
    often; never raises into the request path."""
    try:
        gf._grafana_switch_org(1)  # alerting lives in the main org
        data = st.load_alert_recipients()
        admin_info = data.pop(ADMIN_KEY, None)  # admin handled separately (root catch-all)
        host_map = st.client_host_map()
        site_map = st.client_kuma_site_map()
        active = {}
        # 1. upsert contact points for clients that have >=1 enabled address
        for client, info in data.items():
            emails = st.enabled_emails(info)
            if emails:
                active[client] = emails
                gf._upsert_email_contact_point(gf._client_receiver_name(client), emails)
        # 2. rebuild policy (routes only reference active receivers)
        group_routes = []
        active_group_receivers = set()
        for g in st.load_alert_groups():
            if not g.get("enabled") or (not g.get("hosts") and not g.get("sites")):
                continue
            emails = st.enabled_emails(g)
            if not emails:
                continue
            rname = gf._group_receiver_name(g["id"])
            gf._upsert_email_contact_point(rname, emails)
            group_routes.append({"receiver": rname, "hosts": g.get("hosts", []), "sites": g.get("sites", [])})
            active_group_receivers.add(rname)
        ok, msg = gf.rebuild_notification_policy(active, host_map, ADMIN_RECEIVER, group_routes, site_map=site_map)
        # 2b. admin/fallback contact point reflects its enabled addresses
        if admin_info is not None:
            gf._upsert_email_contact_point(ADMIN_RECEIVER, st.enabled_emails(admin_info))
        # 3. remove managed contact points no longer referenced
        active_receivers = {gf._client_receiver_name(c) for c in active} | active_group_receivers
        for cp in gf._list_contact_points():
            name = cp.get("name", "")
            managed = name == gf.ALL_CLIENTS_RECEIVER or name.startswith("client-") or name.startswith("group-")
            if managed and name not in active_receivers:
                try:
                    gf._delete_email_contact_point(name)
                except Exception:
                    pass
        return ok, msg
    except Exception as ex:  # never break the caller
        return False, str(ex)


def _hosts_variable(client="", account="", include_unregistered=False):
    nodes = list(st.load_nodes())
    if include_unregistered:
        registered = {n["hostname"] for n in nodes}
        for h in st.prom_hosts():
            if h and h not in registered:
                nodes.append({"hostname": h, "ip": "", "client": "", "account": "", "name": ""})
    nodes = st.filter_nodes(nodes, client, account)
    return [{"__text": st.host_display(n), "__value": n["hostname"]}
            for n in sorted(nodes, key=lambda x: st.host_display(x))]


# ---- embedded UI ------------------------------------------------------------

_NOCACHE = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


@app.get("/", response_class=HTMLResponse)
async def ui_index():
    return HTMLResponse(ui.render("index.html"), headers=_NOCACHE)


@app.get("/nodes-only", response_class=HTMLResponse)
async def ui_nodes(host: str = ""):
    return HTMLResponse(ui.render("nodes.html", host=host), headers=_NOCACHE)


@app.get("/ports-only", response_class=HTMLResponse)
async def ui_ports():
    return HTMLResponse(ui.render("ports.html"), headers=_NOCACHE)


@app.get("/inventory", response_class=HTMLResponse)
async def ui_inventory():
    return HTMLResponse(ui.render("inventory.html"), headers=_NOCACHE)


@app.get("/servers", response_class=HTMLResponse)
async def ui_servers():
    return HTMLResponse(ui.render("inventory.html"), headers=_NOCACHE)


def _silence_page(alertname="", host="", monitor_name="", port=""):
    esc = html.escape
    target = monitor_name or host or "selected target"
    scope_bits = []
    if alertname:
        scope_bits.append(f"Alert: {alertname}")
    if monitor_name:
        scope_bits.append(f"Site: {monitor_name}")
    if host:
        scope_bits.append(f"Host: {host}")
    if port:
        scope_bits.append(f"Port: {port}")
    scope = " · ".join(scope_bits) or "No scope selected"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ margin:0; font-family:Inter,system-ui,-apple-system,Segoe UI,sans-serif; background:#07111f; color:#e5eefb; }}
    .wrap {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; border:1px solid #1d3550; border-radius:8px; background:#0b1726; box-sizing:border-box; min-height:86px; }}
    .title {{ font-size:14px; font-weight:700; margin-bottom:5px; }}
    .scope {{ font-size:12px; color:#9fb3ca; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:760px; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:8px; justify-content:flex-end; }}
    button {{ border:1px solid #27506f; background:#10243a; color:#dff6ff; border-radius:7px; padding:8px 11px; font-weight:700; cursor:pointer; }}
    button:hover {{ border-color:#18d6e8; color:#fff; }}
    button.primary {{ background:#0e7490; border-color:#22d3ee; color:#fff; }}
    #msg {{ font-size:12px; color:#8bdcff; min-width:170px; text-align:right; }}
    .err {{ color:#fecdd3 !important; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div>
      <div class="title">Silence notifications for {esc(target)}</div>
      <div class="scope">{esc(scope)}</div>
    </div>
    <div class="actions">
      <button onclick="silence(60)">1h</button>
      <button class="primary" onclick="silence(240)">4h</button>
      <button onclick="silence(1440)">24h</button>
      <button onclick="silence(10080)">7d</button>
      <span id="msg"></span>
    </div>
  </div>
  <script>
    const payload = {{
      alertname: {json.dumps(alertname)},
      host: {json.dumps(host)},
      monitor_name: {json.dumps(monitor_name)},
      port: {json.dumps(port)}
    }};
    async function silence(minutes) {{
      const msg = document.getElementById('msg');
      msg.className = '';
      msg.textContent = 'Creating...';
      try {{
        const r = await fetch('/api/v1/silences', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{...payload, duration_minutes: minutes}})
        }});
        const data = await r.json();
        if (!r.ok || !data.ok) throw new Error(data.error || data.message || 'Failed');
        msg.textContent = 'Silenced until ' + new Date(data.endsAt).toLocaleString();
      }} catch (e) {{
        msg.className = 'err';
        msg.textContent = e.message || 'Failed';
      }}
    }}
  </script>
</body>
</html>"""


@app.get("/silence", response_class=HTMLResponse)
async def ui_silence(alertname: str = "", host: str = "", monitor_name: str = "", port: str = ""):
    return HTMLResponse(_silence_page(alertname, host, monitor_name, port), headers=_NOCACHE)


@app.get("/alert/logo.png")
async def alert_logo():
    candidates = [
        os.environ.get("MONITOR_ALERT_LOGO_PATH", ""),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "alert", "logo.png"),
        "/opt/port-monitor-api/alert/logo.png",
        "/home/ubuntu/monitoring/alert/logo.png",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return J({"error": "alert logo not found"}, 404)


@app.get("/alert/logo-email.png")
async def alert_email_logo():
    candidates = [
        os.environ.get("MONITOR_ALERT_EMAIL_LOGO_PATH", ""),
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "alert", "logo-email.png"),
        "/opt/port-monitor-api/alert/logo-email.png",
        "/home/ubuntu/monitoring/alert/logo-email.png",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return FileResponse(path, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})
    return J({"error": "alert email logo not found"}, 404)


# ---- health / docs / config -------------------------------------------------

API_DOCS = {
    "title": "Central Monitoring API",
    "version": "v1",
    "base": "/api/v1",
    "endpoints": [
        {"method": "GET", "path": "/api/v1/docs", "description": "This documentation"},
        {"method": "GET", "path": "/api/v1/command-center?client=&account=", "description": "Unified servers, Uptime Kuma sites, client/account health, and alert threshold summary"},
        {"method": "GET", "path": "/api/v1/servers", "description": "List all registered servers"},
        {"method": "POST", "path": "/api/v1/servers", "body": {"hostname": "required", "name": "", "client": "", "account": "", "ip": ""}, "description": "Add server manually"},
        {"method": "GET", "path": "/api/v1/servers/{hostname}", "description": "Get one server"},
        {"method": "PUT", "path": "/api/v1/servers/{hostname}", "body": {"name": "", "client": "", "account": "", "ip": ""}, "description": "Update display name and metadata"},
        {"method": "DELETE", "path": "/api/v1/servers/{hostname}", "description": "Remove server and its ports"},
        {"method": "POST", "path": "/api/v1/servers/register", "body": {"hostname": "required", "ip": "", "name": "", "client": "", "account": ""}, "description": "Auto-register from Alloy installer"},
        {"method": "GET", "path": "/api/v1/servers/{hostname}/ports", "description": "List port probes"},
        {"method": "POST", "path": "/api/v1/servers/{hostname}/ports", "body": {"name": "required", "port": "required", "address": "optional", "module": "tcp_connect|http_2xx"}, "description": "Add port (address defaults to server IP:port)"},
        {"method": "DELETE", "path": "/api/v1/servers/{hostname}/ports/{name}", "description": "Remove port"},
        {"method": "GET", "path": "/api/v1/servers/{hostname}/targets", "description": "Blackbox targets for Alloy"},
        {"method": "GET", "path": "/api/v1/variables/clients", "description": "Grafana client dropdown"},
        {"method": "GET", "path": "/api/v1/variables/accounts?client=", "description": "Grafana account dropdown"},
        {"method": "GET", "path": "/api/v1/variables/hosts?client=&account=", "description": "Grafana host dropdown"},
        {"method": "GET", "path": "/api/v1/variables/ports?host=", "description": "Grafana port dropdown"},
        {"method": "GET", "path": "/api/v1/uptime-kuma/sites", "description": "List Uptime Kuma monitors discovered from Prometheus"},
        {"method": "PUT", "path": "/api/v1/uptime-kuma/sites/{monitor_name}", "body": {"name": "", "client": "", "account": ""}, "description": "Update Uptime Kuma monitor display metadata"},
        {"method": "GET", "path": "/api/v1/uptime-kuma/sites/{monitor_name}/groups", "description": "List alert groups for a Uptime Kuma monitor"},
        {"method": "PUT", "path": "/api/v1/uptime-kuma/sites/{monitor_name}/groups", "body": {"group_ids": []}, "description": "Set alert groups for a Uptime Kuma monitor"},
        {"method": "GET", "path": "/api/v1/alert-settings", "description": "Get built-in alert thresholds and durations for the admin UI"},
        {"method": "PUT", "path": "/api/v1/alert-settings", "body": {"rules": {"high_cpu": {"enabled": True, "warning_threshold": 70, "critical_threshold": 90, "duration_minutes": 10}}}, "description": "Update built-in alert thresholds and sync Grafana"},
        {"method": "GET", "path": "/api/v1/taxonomy", "description": "Clients and accounts overview"},
        {"method": "POST", "path": "/api/v1/grafana/sync", "description": "Sync bundled dashboards, alert rules, and alert routing into Grafana"},
        {"method": "POST", "path": "/api/v1/grafana/dashboards/sync", "description": "Sync bundled dashboard JSON into Grafana"},
        {"method": "POST", "path": "/api/v1/grafana/alerts/sync", "description": "Sync source-defined alert rules and notification routing into Grafana"},
    ],
}


@app.get("/health")
@app.get("/api/v1/health")
async def health():
    return J({"status": "ok"})


def _esc_label(v):
    return str(v or "").replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


@app.get("/metrics")
async def metrics():
    """Prometheus exposition of authoritative node metadata, so alerts can be
    enriched with display name / IP / client / account via a group_left join."""
    lines = [
        "# HELP monitor_node_info Authoritative node metadata from the central monitoring API",
        "# TYPE monitor_node_info gauge",
    ]
    for n in st.sync_prom_hosts():
        host = _esc_label(n.get("hostname"))
        labels = (
            f'host="{host}",hostname="{host}",'
            f'name="{_esc_label(n.get("name") or n.get("hostname"))}",'
            f'ip="{_esc_label(n.get("ip"))}",'
            f'client="{_esc_label(st.node_client(n))}",'
            f'account="{_esc_label(st.node_account(n))}"'
        )
        lines.append(f"monitor_node_info{{{labels}}} 1")
    lines.extend([
        "# HELP monitor_kuma_site_info Authoritative Uptime Kuma site metadata from the central monitoring API",
        "# TYPE monitor_kuma_site_info gauge",
    ])
    for s in st.list_kuma_sites():
        monitor_name = _esc_label(s.get("monitor_name"))
        labels = (
            f'monitor_name="{monitor_name}",site="{monitor_name}",'
            f'name="{_esc_label(s.get("name") or s.get("monitor_name"))}",'
            f'client="{_esc_label(s.get("client"))}",'
            f'account="{_esc_label(s.get("account"))}",'
            f'target="{_esc_label(s.get("target"))}",'
            f'monitor_type="{_esc_label(s.get("monitor_type"))}"'
        )
        lines.append(f"monitor_kuma_site_info{{{labels}}} 1")
    lines.extend([
        "# HELP monitor_port_info Authoritative monitored port metadata from the central monitoring API",
        "# TYPE monitor_port_info gauge",
    ])
    for n in st.sync_prom_hosts():
        host = n.get("hostname")
        if not host:
            continue
        for p in st.load_ports(host):
            port_name = st.sanitize_port_name(p.get("name", ""))
            labels = (
                f'host="{_esc_label(host)}",hostname="{_esc_label(host)}",'
                f'port="{_esc_label(port_name)}",'
                f'name="{_esc_label(p.get("name") or port_name)}",'
                f'address="{_esc_label(p.get("address"))}",'
                f'module="{_esc_label(p.get("module", "tcp_connect"))}",'
                f'client="{_esc_label(st.node_client(n))}",'
                f'account="{_esc_label(st.node_account(n))}"'
            )
            lines.append(f"monitor_port_info{{{labels}}} 1")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/api/v1/docs")
async def docs():
    out = dict(API_DOCS)
    out["public_url"] = load_config().get("public_url", "")
    return J(out)


@app.get("/api/v1/config")
async def config():
    cfg = load_config()
    return J({
        "public_url": cfg.get("public_url", ""),
        "grafana_url": cfg.get("grafana_url", ""),
        "has_install_token": bool(cfg.get("install_token")),
        "has_api_key": bool(cfg.get("api_key")),
    })


@app.post("/api/v1/silences")
async def create_silence_ep(request: Request):
    data = await _body(request)
    alertname = str(data.get("alertname", "")).strip()
    host = str(data.get("host", "")).strip()
    monitor_name = str(data.get("monitor_name", "")).strip()
    port = str(data.get("port", "")).strip()
    matchers = []
    if alertname:
        matchers.append({"name": "alertname", "value": alertname})
    if monitor_name:
        matchers.append({"name": "monitor_name", "value": monitor_name})
    if host:
        matchers.append({"name": "host", "value": host})
    if port:
        matchers.append({"name": "port", "value": port})
    if not matchers:
        return J({"ok": False, "error": "choose an alert, site, or host to silence"}, 400)
    until_off = bool(data.get("until_off"))
    duration = data.get("duration_minutes", 60)
    comment_target = monitor_name or host or alertname
    ok, result = gf.create_silence(
        matchers,
        duration_minutes=duration,
        comment=f"Silenced from Zentra dashboard for {comment_target}" + (" until manually turned off" if until_off else ""),
        until_off=until_off,
    )
    if not ok:
        return J({"ok": False, "error": result.get("error") or result.get("message") or str(result)}, 500)
    return J({
        "ok": True,
        "silenceID": result.get("silenceID"),
        "matchers": matchers,
        "endsAt": result.get("endsAt"),
        "until_off": until_off,
        "duration_minutes": duration,
    })


@app.get("/api/v1/silences")
async def list_silences_ep():
    ok, data = gf.list_silences(active_only=True)
    if not ok:
        return J({"ok": False, "error": data.get("error") if isinstance(data, dict) else str(data)}, 500)
    return J({"ok": True, "silences": data, "count": len(data)})


@app.delete("/api/v1/silences/{silence_id}")
async def delete_silence_ep(silence_id: str):
    ok, data = gf.delete_silence(silence_id)
    if not ok:
        return J({"ok": False, "error": data.get("error") if isinstance(data, dict) else str(data)}, 500)
    return J({"ok": True, "message": data.get("message", "silence deleted") if isinstance(data, dict) else "silence deleted"})


@app.get("/api/v1/command-center")
async def command_center(client: str = "", account: str = ""):
    return J(st.command_center(client, account))


@app.post("/api/v1/grafana/dashboards/sync")
async def sync_grafana_dashboards(request: Request):
    if not _require_write_auth(request):
        return J({"error": "invalid or missing X-Monitor-Key"}, 401)
    results = gf.sync_main_dashboards()
    return J({"dashboards": results, "ok": all(r.get("ok") for r in results)})


@app.post("/api/v1/grafana/alerts/sync")
async def sync_grafana_alerts(request: Request):
    if not _require_write_auth(request):
        return J({"error": "invalid or missing X-Monitor-Key"}, 401)
    rules = gf.sync_alert_rules()
    routing_ok, routing_msg = _sync_alerting()
    return J({
        "alert_rules": rules,
        "routing": {"ok": routing_ok, "message": routing_msg},
        "ok": all(r.get("ok") for r in rules) and routing_ok,
    })


@app.post("/api/v1/grafana/sync")
async def sync_grafana_all(request: Request):
    if not _require_write_auth(request):
        return J({"error": "invalid or missing X-Monitor-Key"}, 401)
    dashboards = gf.sync_main_dashboards()
    rules = gf.sync_alert_rules()
    routing_ok, routing_msg = _sync_alerting()
    return J({
        "dashboards": dashboards,
        "alert_rules": rules,
        "routing": {"ok": routing_ok, "message": routing_msg},
        "ok": all(r.get("ok") for r in dashboards) and all(r.get("ok") for r in rules) and routing_ok,
    })


# ---- v1 servers -------------------------------------------------------------

@app.get("/api/v1/servers")
async def list_servers():
    nodes = st.sync_prom_hosts()
    servers = []
    for n in nodes:
        item = dict(n)
        item["port_count"] = len(st.load_ports(n["hostname"]))
        servers.append(item)
    return J({"servers": servers, "count": len(servers)})


@app.get("/api/v1/servers/{host}")
async def get_server(host: str):
    if host == "register":
        return J({"error": "not found"}, 404)
    node = st.find_node(st.load_nodes(), host)
    if not node:
        return J({"error": f"server '{host}' not found"}, 404)
    out = dict(node)
    out["port_count"] = len(st.load_ports(host))
    return J(out)


@app.get("/api/v1/servers/{host}/ports")
async def get_server_ports(host: str):
    ports = st.load_ports(host)
    return J({"host": host, "ports": ports, "count": len(ports)})


@app.get("/api/v1/servers/{host}/targets")
async def get_server_targets(host: str):
    return J(st.alloy_targets(host))


@app.get("/api/v1/servers/{host}/groups")
async def get_host_groups(host: str):
    return J({"host": host, "group_ids": st.host_group_ids(host)})


@app.put("/api/v1/servers/{host}/groups")
async def set_host_groups_ep(host: str, request: Request):
    data = await _body(request)
    ids = st.set_host_groups(host, data.get("group_ids", []))
    ok, msg = _sync_alerting()
    return J({"host": host, "group_ids": ids, "synced": ok, "message": msg})


@app.post("/api/v1/servers/register")
@app.post("/nodes/register")
async def register_server(request: Request):
    if not _require_install_token(request):
        return J({"error": "invalid or missing X-Install-Token"}, 401)
    data = await _body(request)
    return _register(data, auto=True)


@app.post("/api/v1/servers")
async def add_server(request: Request):
    if not _require_write_auth(request):
        return J({"error": "invalid or missing X-Monitor-Key"}, 401)
    data = await _body(request)
    return _register(data, auto=False)


@app.put("/api/v1/servers/{host}")
@app.put("/nodes/{host}")
async def update_server(host: str, request: Request):
    from .config import TIMESTAMP
    data = await _body(request)
    nodes = st.load_nodes()
    existing = st.find_node(nodes, host)
    if not existing:
        existing = {"hostname": host, "registered": TIMESTAMP()}
        nodes.append(existing)
    # IP is set only at Alloy install/register — not editable from dashboards
    c, a, nm = st.normalize_metadata(
        data.get("client", existing.get("client", "")),
        data.get("account", existing.get("account", "")),
        data.get("name", existing.get("name", "")),
    )
    if "client" in data or not existing.get("client"):
        existing["client"] = c
    if "account" in data or not existing.get("account"):
        existing["account"] = a
    if "name" in data or not existing.get("name"):
        existing["name"] = nm or existing["hostname"]
    if data.get("hostname") and data["hostname"] != host:
        existing["hostname"] = st.normalize_host(data["hostname"])
    st.save_nodes(nodes)
    st.record_taxonomy(existing.get("client", ""), existing.get("account", ""))
    _sync_alerting()
    return J({"message": f"Saved {existing['hostname']}", "hostname": existing["hostname"]})


@app.delete("/api/v1/servers/{host}")
@app.delete("/nodes/{host}")
async def delete_server(host: str, request: Request):
    if not _require_write_auth(request):
        return J({"error": "invalid or missing X-Monitor-Key"}, 401)
    return _delete_server(host)


def _register(data, auto=False):
    from .config import TIMESTAMP
    host = st.normalize_host(data.get("hostname"))
    if not host:
        return J({"error": "hostname is required"}, 400)

    nodes = st.load_nodes()
    existing = st.find_node(nodes, host)
    if existing:
        if not auto:
            return J({"error": f"server '{host}' already exists — use PUT to update"}, 409)
        if data.get("ip"):
            existing["ip"] = data["ip"]
        c, a, nm = st.normalize_metadata(
            data.get("client") or existing.get("client", ""),
            data.get("account") or existing.get("account", ""),
            data.get("name") or existing.get("name", ""),
        )
        existing["client"] = c
        existing["account"] = a
        if nm:
            existing["name"] = nm
        existing["last_seen"] = TIMESTAMP()
        st.save_nodes(nodes)
        st.record_taxonomy(existing["client"], existing["account"])
        _sync_alerting()
        return J({"message": f"Updated {host}", "hostname": host})

    c, a, nm = st.normalize_metadata(data.get("client", ""), data.get("account", ""), data.get("name", ""))
    entry = {
        "hostname": host,
        "ip": data.get("ip", ""),
        "name": nm or host,
        "client": c,
        "account": a,
        "registered": TIMESTAMP(),
        "last_seen": TIMESTAMP(),
    }
    nodes.append(entry)
    st.save_nodes(nodes)
    st.record_taxonomy(c, a)
    _sync_alerting()
    return J({"message": f"{'Registered' if auto else 'Added'} {host}", "hostname": host}, 201)


def _delete_server(host):
    nodes = st.load_nodes()
    before = len(nodes)
    nodes = [n for n in nodes if n["hostname"] != host]
    if len(nodes) == before:
        return J({"error": "not found"}, 404)
    st.save_nodes(nodes)
    pf = st.get_port_file(host)
    if os.path.exists(pf):
        os.remove(pf)
    _sync_alerting()
    return J({"message": f"Removed {host}"})


# ---- ports (v1 + legacy) ----------------------------------------------------

@app.post("/api/v1/servers/{host}/ports")
async def add_port_v1(host: str, request: Request):
    return _add_port(host, await _body(request))


@app.post("/ports/{host}")
async def add_port_legacy(host: str, request: Request):
    return _add_port(host, await _body(request))


@app.delete("/api/v1/servers/{host}/ports/{name}")
async def delete_port_v1(host: str, name: str):
    return _delete_port(host, name)


@app.delete("/ports/{host}/{name}")
async def delete_port_legacy(host: str, name: str):
    return _delete_port(host, name)


def _add_port(host, data):
    host = st.normalize_host(host)
    if not host:
        return J({"error": "hostname required"}, 400)

    _, nodes, created = st.ensure_server(host, create=True)
    if created:
        st.save_nodes(nodes)

    name = st.sanitize_port_name((data.get("name") or "").strip())
    port = str(data.get("port", "")).strip()
    addr = (data.get("address") or "").strip()
    module = data.get("module", "tcp_connect")

    if not addr and not port:
        return J({"error": "port or address required"}, 400)
    if not name:
        name = st.sanitize_port_name(f"port_{port or addr.split(':')[-1]}")

    address = st.probe_address(host, port, addr)
    targets = st.load_ports(host)
    if any(st.sanitize_port_name(t["name"]) == name for t in targets):
        return J({"error": f"port '{name}' already exists"}, 409)
    targets.append({"name": name, "address": address, "module": module})
    st.save_ports(host, targets)
    return J({"message": f"Added '{name}' → {address}", "name": name, "address": address}, 201)


def _delete_port(host, name):
    targets = st.load_ports(host)
    filtered = [t for t in targets if st.sanitize_port_name(t["name"]) != st.sanitize_port_name(name)]
    if len(filtered) == len(targets):
        return J({"error": "not found"}, 404)
    st.save_ports(host, filtered)
    return J({"message": f"Removed '{name}'"})


# ---- taxonomy ---------------------------------------------------------------

@app.get("/api/v1/taxonomy")
async def taxonomy():
    return J(st.taxonomy_overview())


@app.post("/api/v1/taxonomy/clients")
async def add_client(request: Request):
    data = await _body(request)
    c, err = st.add_taxonomy_client(data.get("name", ""))
    if err:
        return J({"error": err}, 400)
    return J({"message": f"Client '{c}' added", "name": c}, 201)


@app.put("/api/v1/taxonomy/clients/{client}")
async def rename_client(client: str, request: Request):
    data = await _body(request)
    err = st.rename_taxonomy_client(client, data.get("new_name", ""))
    if err:
        return J({"error": err}, 400)
    return J({"message": f"Client renamed to '{data.get('new_name')}'"})


@app.delete("/api/v1/taxonomy/clients/{client}")
async def delete_client(client: str, merge_into: str = ""):
    err = st.delete_taxonomy_client(client, merge_into=merge_into or None)
    if err:
        return J({"error": err}, 400)
    return J({"message": "Client removed"})


@app.post("/api/v1/taxonomy/clients/{client}/accounts")
async def add_account(client: str, request: Request):
    data = await _body(request)
    acc, err = st.add_taxonomy_account(client, data.get("name", ""))
    if err:
        return J({"error": err}, 400)
    return J({"message": f"Account '{acc}' added under '{client}'", "name": acc}, 201)


@app.put("/api/v1/taxonomy/clients/{client}/accounts/{account}")
async def rename_account(client: str, account: str, request: Request):
    data = await _body(request)
    err = st.rename_taxonomy_account(client, account, data.get("new_name", ""))
    if err:
        return J({"error": err}, 400)
    return J({"message": f"Account renamed to '{data.get('new_name')}'"})


@app.delete("/api/v1/taxonomy/clients/{client}/accounts/{account}")
async def delete_account(client: str, account: str, merge_into: str = ""):
    err = st.delete_taxonomy_account(client, account, merge_into=merge_into or None)
    if err:
        return J({"error": err}, 400)
    return J({"message": "Account removed"})


# ---- v1 variables -----------------------------------------------------------

@app.get("/api/v1/variables/clients")
async def var_clients():
    return J([{"__text": c, "__value": c} for c in st.list_all_clients()])


@app.get("/api/v1/variables/accounts")
async def var_accounts(client: str = ""):
    return J([{"__text": a, "__value": a} for a in st.list_accounts_for_client(client)])


@app.get("/api/v1/variables/hosts")
async def var_hosts(client: str = "", account: str = ""):
    return J(_hosts_variable(client, account))


@app.get("/api/v1/variables/ports")
async def var_ports(host: str = ""):
    host = st.normalize_host(host)
    if not host:
        return J([])
    targets = st.load_ports(host)
    return J([{"__text": t["name"], "__value": st.sanitize_port_name(t["name"])} for t in targets])


# ---- Uptime Kuma sites ------------------------------------------------------

@app.get("/api/v1/uptime-kuma/sites")
async def list_kuma_sites(client: str = "", account: str = ""):
    sites = st.list_kuma_sites(client, account)
    return J({"sites": sites, "count": len(sites)})


@app.get("/api/v1/uptime-kuma/sites/{monitor_name}")
async def get_kuma_site(monitor_name: str):
    site = st.find_kuma_site(monitor_name)
    if not site:
        return J({"error": "not found"}, 404)
    return J(site)


@app.put("/api/v1/uptime-kuma/sites/{monitor_name}")
async def update_kuma_site(monitor_name: str, request: Request):
    data = await _body(request)
    site, err = st.update_kuma_site(monitor_name, data)
    if err:
        return J({"error": err}, 400)
    ok, msg = _sync_alerting()
    return J({"message": f"Saved {monitor_name}", "site": site, "synced": ok, "message_detail": msg})


@app.get("/api/v1/uptime-kuma/sites/{monitor_name}/groups")
async def get_kuma_site_groups(monitor_name: str):
    return J({"monitor_name": monitor_name, "group_ids": st.kuma_site_group_ids(monitor_name)})


@app.put("/api/v1/uptime-kuma/sites/{monitor_name}/groups")
async def set_kuma_site_groups_ep(monitor_name: str, request: Request):
    data = await _body(request)
    ids = st.set_kuma_site_groups(monitor_name, data.get("group_ids", []))
    ok, msg = _sync_alerting()
    return J({"monitor_name": monitor_name, "group_ids": ids, "synced": ok, "message": msg})


# ---- alert settings ---------------------------------------------------------

@app.get("/api/v1/alert-settings")
async def get_alert_settings():
    return J(st.load_alert_settings())


@app.put("/api/v1/alert-settings")
async def put_alert_settings(request: Request):
    data = await _body(request)
    settings = st.update_alert_settings(data)
    rules = gf.sync_alert_rules()
    return J({
        "settings": settings,
        "alert_rules": rules,
        "synced": all(r.get("ok") for r in rules),
    })


# ---- legacy -----------------------------------------------------------------

@app.get("/nodes")
async def legacy_nodes(client: str = "", account: str = ""):
    nodes = st.filter_nodes(st.sync_prom_hosts(), client, account)
    return J({"nodes": nodes})


@app.get("/hosts")
async def legacy_hosts():
    nodes = st.load_nodes()
    return J({"hosts": [{"host": n["hostname"], "count": len(st.load_ports(n["hostname"]))} for n in nodes]})


@app.get("/client-accounts")
async def client_accounts(client: str = ""):
    c = (client or "").strip()
    nodes = [n for n in st.load_nodes() if (n.get("client") or "").strip() == c]
    accts = sorted({(n.get("account") or "").strip() for n in nodes if (n.get("account") or "").strip()})
    return J([{"__text": a, "__value": a} for a in accts])


@app.get("/client-hosts")
async def client_hosts(client: str = "", account: str = ""):
    c = (client or "").strip()
    a = (account or "").strip()
    if a in (".*", "$__all", "All"):
        a = ""
    nodes = [n for n in st.load_nodes() if (n.get("client") or "").strip() == c]
    if a:
        nodes = [n for n in nodes if (n.get("account") or "").strip() == a]
    return J([{"__text": st.host_display(n), "__value": n["hostname"]}
              for n in sorted(nodes, key=lambda x: st.host_display(x))])


@app.get("/hosts-list")
async def hosts_list(client: str = "", account: str = "", include_unregistered: str = ""):
    inc = include_unregistered.lower() in ("1", "true", "yes")
    return J(_hosts_variable(client, account, include_unregistered=inc))


@app.get("/clients-list")
async def clients_list():
    return J([{"__text": c, "__value": c} for c in st.list_all_clients()])


@app.get("/accounts-list")
async def accounts_list(client: str = ""):
    return J([{"__text": a, "__value": a} for a in st.list_accounts_for_client(client)])


@app.get("/targets/{host}")
async def legacy_targets(host: str):
    return J(st.alloy_targets(host))


@app.get("/metadata/{host}")
async def legacy_metadata(host: str):
    node = st.find_node(st.load_nodes(), host) or {}
    return J({"hostname": host, "account": node.get("account", ""), "name": node.get("name", ""),
              "ip": node.get("ip", ""), "client": node.get("client", "")})


@app.get("/ports/{host}")
async def legacy_ports(host: str):
    ports = st.load_ports(host)
    return J({"host": host, "ports": ports, "count": len(ports)})


# ---- alert recipients (per-client email) ------------------------------------

@app.get("/api/v1/alert-recipients")
async def alert_recipients_all():
    return J(st.load_alert_recipients())


@app.get("/api/v1/alert-recipients/admin")
async def get_admin_recipient():
    info = st.load_alert_recipients().get(ADMIN_KEY)
    if info is None:
        # first time: seed from the existing admin contact point so zentra@ shows up
        addrs = gf._get_contact_point_addresses(ADMIN_RECEIVER)
        info = st.set_alert_recipient(ADMIN_KEY, [{"email": a, "enabled": True} for a in addrs])
    return J({"recipients": info.get("recipients", [])})


@app.put("/api/v1/alert-recipients/admin")
async def set_admin_recipient(request: Request):
    data = await _body(request)
    recipients = data.get("recipients")
    if recipients is None:
        recipients = data.get("emails", data.get("email", ""))
    info = st.set_alert_recipient(ADMIN_KEY, recipients)
    ok, msg = _sync_alerting()
    return J({"recipients": info.get("recipients", []), "synced": ok, "message": msg})


@app.get("/api/v1/alert-recipients/all-clients")
async def get_all_clients_recipient():
    info = st.get_alert_recipient(gf.ALL_CLIENTS_KEY)
    return J({"recipients": info.get("recipients", [])})


@app.put("/api/v1/alert-recipients/all-clients")
async def set_all_clients_recipient(request: Request):
    data = await _body(request)
    recipients = data.get("recipients")
    if recipients is None:
        recipients = data.get("emails", data.get("email", ""))
    info = st.set_alert_recipient(gf.ALL_CLIENTS_KEY, recipients)
    ok, msg = _sync_alerting()
    return J({"recipients": info.get("recipients", []), "synced": ok, "message": msg})


@app.get("/api/v1/clients/{client}/alert-email")
async def get_alert_email(client: str):
    info = st.get_alert_recipient(client)
    return J({"client": client, "recipients": info.get("recipients", [])})


@app.put("/api/v1/clients/{client}/alert-email")
async def set_alert_email(client: str, request: Request):
    # Open for the same-origin admin inventory UI, consistent with the taxonomy/
    # server-edit endpoints. The All Servers page is main-org admin only, and :9099
    # is expected to be network-restricted.
    data = await _body(request)
    recipients = data.get("recipients")
    if recipients is None:  # legacy callers
        recipients = data.get("emails", data.get("email", ""))
    info = st.set_alert_recipient(client, recipients)
    ok, msg = _sync_alerting()
    return J({"client": client, "recipients": info.get("recipients", []),
              "synced": ok, "message": msg})


# ---- alert groups (named set of servers + recipients) -----------------------

@app.get("/api/v1/alert-groups")
async def list_alert_groups():
    return J({"groups": st.load_alert_groups()})


@app.get("/api/v1/alert-groups/{group_id}")
async def get_alert_group_ep(group_id: str):
    g = st.get_alert_group(group_id)
    if not g:
        return J({"error": "not found"}, 404)
    return J(g)


@app.post("/api/v1/alert-groups")
async def create_alert_group(request: Request):
    data = await _body(request)
    data.pop("id", None)  # force create
    g, err = st.upsert_alert_group(data)
    if err:
        return J({"error": err}, 400)
    ok, msg = _sync_alerting()
    return J({"group": g, "synced": ok, "message": msg}, 201)


@app.put("/api/v1/alert-groups/{group_id}")
async def update_alert_group(group_id: str, request: Request):
    data = await _body(request)
    data["id"] = group_id
    g, err = st.upsert_alert_group(data)
    if err:
        return J({"error": err}, 400)
    ok, msg = _sync_alerting()
    return J({"group": g, "synced": ok, "message": msg})


@app.delete("/api/v1/alert-groups/{group_id}")
async def remove_alert_group(group_id: str):
    if not st.delete_alert_group(group_id):
        return J({"error": "not found"}, 404)
    ok, msg = _sync_alerting()
    return J({"message": "Group removed", "synced": ok})


# ---- grafana orgs -----------------------------------------------------------

@app.get("/api/v1/grafana-orgs")
async def grafana_orgs_list():
    return J({"orgs": gf._grafana_list_orgs()})


@app.get("/api/v1/grafana-orgs/status")
async def grafana_orgs_status():
    orgs = gf._grafana_list_orgs()
    saved = st._read_json(GRAFANA_ORGS_FILE, {})
    nodes = st.load_nodes()
    result = []
    for o in orgs:
        if o["id"] == 1:
            continue
        client_name = o["name"].replace("Client - ", "") if o["name"].startswith("Client - ") else o["name"]
        sc = sum(1 for n in nodes if (n.get("client") or "").strip() == client_name)
        cfg = saved.get(client_name, {})
        login = cfg.get("login", "")
        dash = cfg.get("dashboard_url", "")
        if not login:
            users = gf._grafana_list_org_users(o["id"])
            viewer = next((u for u in users if u.get("role") == "Viewer"), None)
            if viewer:
                login = viewer.get("login", "")
        if not dash:
            dash = f"/d/client-{client_name.lower().replace(' ', '-')}-summary/my-servers"
        result.append({"org_id": o["id"], "org_name": o["name"], "client": client_name,
                       "server_count": sc, "login": login, "dashboard_url": dash})
    return J({"orgs": result})


@app.post("/api/v1/grafana-orgs")
async def grafana_orgs_create(request: Request):
    data = await _body(request)
    client = (data.get("client") or "").strip()
    password = (data.get("password") or "ChangeMe@2026").strip()
    if not client:
        return J({"error": "client required"}, 400)
    org_name = f"Client - {client}"
    org_id, msg = gf._grafana_create_org(org_name)
    if not org_id:
        return J({"error": f"create org: {msg}"}, 500)
    gf._grafana_switch_org(org_id)
    prom_uid, _ = gf._grafana_add_datasource(org_id, "Prometheus", "prometheus", "http://localhost:9090", True)
    inf_uid, _ = gf._grafana_add_datasource(org_id, "Port Monitor API", "yesoreyeram-infinity-datasource", "http://localhost:9099")
    dash = gf._build_client_dashboard(client, prom_uid, inf_uid)
    ok, url = gf._grafana_deploy_dashboard(org_id, dash)
    fleet = gf._build_client_fleet_dashboard(client, prom_uid, inf_uid)
    gf._grafana_deploy_dashboard(org_id, fleet)
    for dd in gf._build_client_drilldowns(client, prom_uid, inf_uid):
        gf._grafana_deploy_dashboard(org_id, dd)
    login = f"{client.lower().replace(' ', '-')}-client"
    user_id, umsg = gf._grafana_create_user(login, f"{client} Client", password)
    if user_id:
        gf._grafana_add_user_to_org(org_id, login, "Viewer")
        gf._grafana_remove_user_from_org(1, user_id)
    cfg = {"org_id": org_id, "prom_uid": prom_uid, "inf_uid": inf_uid,
           "login": login, "password": password, "dashboard_url": url}
    existing = st._read_json(GRAFANA_ORGS_FILE, {})
    existing[client] = cfg
    st._write_json(GRAFANA_ORGS_FILE, existing)
    gf._grafana_switch_org(1)
    return J({"message": f"Client org '{org_name}' created", "org_id": org_id,
              "login": login, "password": password, "dashboard_url": url}, 201)


@app.delete("/api/v1/grafana-orgs/{client}")
async def grafana_orgs_delete(client: str):
    existing = st._read_json(GRAFANA_ORGS_FILE, {})
    org_cfg = existing.get(client)
    if not org_cfg:
        orgs = gf._grafana_list_orgs()
        org_name = f"Client - {client}"
        found = next((o for o in orgs if o["name"] == org_name), None)
        if not found:
            return J({"error": f"No org found for client '{client}'"}, 404)
        org_cfg = {"org_id": found["id"]}
    org_id = org_cfg.get("org_id")
    login = org_cfg.get("login")
    if login:
        uid = gf._grafana_get_user_id(login)
        if uid:
            gf._grafana_req("DELETE", f"/api/admin/users/{uid}")
    gf._grafana_delete_org(org_id)
    existing.pop(client, None)
    st._write_json(GRAFANA_ORGS_FILE, existing)
    gf._grafana_switch_org(1)
    return J({"message": f"Client org for '{client}' deleted"})
