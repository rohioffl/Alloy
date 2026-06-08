"""Central Monitoring API (FastAPI) — server registry, naming, port probe targets,
client/account taxonomy, and per-client Grafana org provisioning.

Runs on the central Grafana/Prometheus server (:9099). Serves both the REST API
and the embedded management UI (same origin, so Grafana iframes work directly).
"""

import os

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .config import (
    DATA_DIR,
    CONFIG_FILE,
    GRAFANA_ORGS_FILE,
    NODES_FILE,
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

@app.get("/", response_class=HTMLResponse)
async def ui_index():
    return HTMLResponse(ui.render("index.html"))


@app.get("/nodes-only", response_class=HTMLResponse)
async def ui_nodes(host: str = ""):
    return HTMLResponse(ui.render("nodes.html", host=host))


@app.get("/ports-only", response_class=HTMLResponse)
async def ui_ports():
    return HTMLResponse(ui.render("ports.html"))


@app.get("/inventory", response_class=HTMLResponse)
async def ui_inventory():
    return HTMLResponse(ui.render("inventory.html"))


@app.get("/servers", response_class=HTMLResponse)
async def ui_servers():
    return HTMLResponse(ui.render("inventory.html"))


# ---- health / docs / config -------------------------------------------------

API_DOCS = {
    "title": "Central Monitoring API",
    "version": "v1",
    "base": "/api/v1",
    "endpoints": [
        {"method": "GET", "path": "/api/v1/docs", "description": "This documentation"},
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
        {"method": "GET", "path": "/api/v1/taxonomy", "description": "Clients and accounts overview"},
    ],
}


@app.get("/health")
@app.get("/api/v1/health")
async def health():
    return J({"status": "ok"})


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
    return J({"message": f"Client org for '{client}' deleted"})
