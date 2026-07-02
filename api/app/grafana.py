"""Grafana Admin API helpers + client dashboard builder.

Used to create/delete per-client orgs (org-per-client multi-tenancy) with
isolated read-only dashboards, all driven from the embedded UI.
"""

import base64
from datetime import datetime, timedelta, timezone
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .config import load_config


def _grafana_req(method, path, body=None, headers=None):
    """Make a Grafana Admin API request using stored credentials."""
    cfg = load_config()
    base = (cfg.get("grafana_url") or "http://localhost:3000").rstrip("/")
    user = cfg.get("grafana_admin_user") or "admin"
    pw = cfg.get("grafana_admin_password") or "admin"
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
    }
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}
    except Exception as ex:
        return 0, {"error": str(ex)}


def create_silence(matchers, duration_minutes=60, comment="", created_by="Zentra Dashboard", until_off=False):
    """Create a Grafana-managed Alertmanager silence."""
    cleaned = []
    for matcher in matchers:
        name = str(matcher.get("name", "")).strip()
        value = str(matcher.get("value", "")).strip()
        if not name or not value:
            continue
        cleaned.append({
            "name": name,
            "value": value,
            "isRegex": bool(matcher.get("isRegex", False)),
            "isEqual": matcher.get("isEqual", True) is not False,
        })
    if not cleaned:
        return False, {"error": "at least one matcher is required"}
    if until_off:
        minutes = 5256000  # 10 years; Grafana silences require a concrete end time.
    else:
        try:
            minutes = int(duration_minutes)
        except Exception:
            minutes = 60
        minutes = max(5, min(minutes, 10080))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = {
        "matchers": cleaned,
        "startsAt": now.isoformat().replace("+00:00", "Z"),
        "endsAt": (now + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "createdBy": created_by,
        "comment": comment or "Silenced from Zentra dashboard",
    }
    status, data = _grafana_req("POST", "/api/alertmanager/grafana/api/v2/silences", body)
    if isinstance(data, dict):
        data.setdefault("startsAt", body["startsAt"])
        data.setdefault("endsAt", body["endsAt"])
        data.setdefault("until_off", bool(until_off))
    return status in (200, 201, 202), data


def list_silences(active_only=True):
    status, data = _grafana_req("GET", "/api/alertmanager/grafana/api/v2/silences")
    if status != 200 or not isinstance(data, list):
        return False, data
    if active_only:
        data = [s for s in data if (s.get("status") or {}).get("state") == "active"]
    return True, data


def delete_silence(silence_id):
    sid = urllib.parse.quote(str(silence_id or ""), safe="")
    if not sid:
        return False, {"error": "silence id is required"}
    status, data = _grafana_req("DELETE", f"/api/alertmanager/grafana/api/v2/silence/{sid}")
    return status in (200, 202), data


def _grafana_switch_org(org_id):
    """Switch admin's active org."""
    return _grafana_req("POST", f"/api/user/using/{org_id}")


def _grafana_create_org(name):
    status, data = _grafana_req("POST", "/api/orgs", {"name": name})
    return data.get("orgId"), data.get("message", str(data))


def _grafana_delete_org(org_id):
    return _grafana_req("DELETE", f"/api/orgs/{org_id}")


def _grafana_list_orgs():
    _, data = _grafana_req("GET", "/api/orgs")
    return data if isinstance(data, list) else []


def _grafana_add_datasource(org_id, name, ds_type, url, is_default=False):
    _grafana_switch_org(org_id)
    _, data = _grafana_req("POST", "/api/datasources", {
        "name": name, "type": ds_type,
        "access": "proxy", "url": url, "isDefault": is_default
    })
    return data.get("datasource", {}).get("uid", ""), data.get("message", str(data))


def _grafana_deploy_dashboard(org_id, dashboard_json):
    _grafana_switch_org(org_id)
    _, data = _grafana_req("POST", "/api/dashboards/db", dashboard_json)
    return data.get("status") == "success", data.get("url", data.get("message", str(data)))


def _grafana_create_user(login, name, password):
    _, data = _grafana_req("POST", "/api/admin/users", {
        "login": login, "name": name, "password": password
    })
    return data.get("id"), data.get("message", str(data))


def _grafana_add_user_to_org(org_id, login, role="Viewer"):
    _, data = _grafana_req("POST", f"/api/orgs/{org_id}/users", {
        "loginOrEmail": login, "role": role
    })
    return data.get("message", str(data))


def _grafana_remove_user_from_org(org_id, user_id):
    return _grafana_req("DELETE", f"/api/orgs/{org_id}/users/{user_id}")


def _grafana_get_user_id(login):
    _, data = _grafana_req("GET", f"/api/users/lookup?loginOrEmail={login}")
    return data.get("id")


def _grafana_list_org_users(org_id):
    _, data = _grafana_req("GET", f"/api/orgs/{org_id}/users")
    return data if isinstance(data, list) else []


def _client_slug(client):
    return client.lower().replace(' ', '-')


import os as _os


def _resolve_dashboards_dir():
    """Single source of truth: repo-root dashboards/, with bundled copy for Docker."""
    env = _os.environ.get("MONITOR_DASHBOARDS_DIR", "").strip()
    if env and _os.path.isdir(env):
        return env
    here = _os.path.dirname(__file__)
    for path in (
        _os.path.abspath(_os.path.join(here, "..", "..", "dashboards")),
        _os.path.join(here, "dashboards"),
    ):
        if _os.path.isdir(path) and any(f.endswith(".json") for f in _os.listdir(path)):
            return path
    return _os.path.join(here, "dashboards")


_DASHBOARDS_DIR = _resolve_dashboards_dir()
_ALERTS_DIR = _os.path.join(_os.path.dirname(__file__), "alerts")

# main drill-down template files -> client-scoped uid suffix
_DRILLDOWNS = [
    ("summary.json", "dd-summary"),
    ("cpu.json", "dd-cpu"),
    ("memory.json", "dd-mem"),
    ("disk.json", "dd-disk"),
    ("network.json", "dd-net"),
    ("ports.json", "dd-port"),
    ("processes.json", "dd-proc"),
]

# main uid token -> client suffix (used to rewrite the dashboard uid + nav links)
_UID_TOKENS = {
    "alloy-drilldown": "dd-summary",
    "alloy-dd-proc": "dd-proc",
    "alloy-dd-port": "dd-port",
    "alloy-dd-cpu": "dd-cpu",
    "alloy-dd-mem": "dd-mem",
    "alloy-dd-disk": "dd-disk",
    "alloy-dd-net": "dd-net",
}


def _client_drilldown_vars(client, prom_uid, inf_uid, with_port=False, with_process=False):
    inf = {"type": "yesoreyeram-infinity-datasource", "uid": inf_uid}
    prom = {"type": "prometheus", "uid": prom_uid}
    base = "http://localhost:9099"
    vlist = [
        {"name": "client", "type": "constant", "query": client,
         "current": {"text": client, "value": client}, "hide": 2},
        {"name": "account", "type": "query", "label": "Account", "datasource": inf,
         "includeAll": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"},
         "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
            "url": f"{base}/client-accounts?client={client}"}, "queryType": "infinity", "type": "infinity"},
         "refresh": 1, "sort": 0},
        {"name": "host", "type": "query", "label": "Host", "datasource": inf, "includeAll": False,
         "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
            "url": f"{base}/client-hosts?client={client}&account=${{account}}"}, "queryType": "infinity", "type": "infinity"},
         "refresh": 1, "sort": 0},
    ]
    if with_port:
        vlist.append(
            {"name": "port", "type": "query", "label": "Port", "datasource": inf,
             "includeAll": True, "multi": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"},
             "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
                "url": f"{base}/api/v1/variables/ports?host=${{host}}"}, "queryType": "infinity", "type": "infinity"},
             "refresh": 2, "sort": 1})
    if with_process:
        vlist.append(
            {"name": "process", "type": "query", "label": "Process", "datasource": prom,
             "includeAll": True, "multi": True, "allValue": ".*",
             "current": {"text": "All", "value": "$__all"},
             "definition": 'label_values(namedprocess_namegroup_cpu_seconds_total{host="$host"}, groupname)',
             "query": 'label_values(namedprocess_namegroup_cpu_seconds_total{host="$host"}, groupname)',
             "refresh": 2, "sort": 1})
    return vlist


def _build_client_drilldowns(client, prom_uid, inf_uid):
    """Transform the main-org drill-down dashboards into client-scoped copies.

    The main drill-down panels already filter only by host="$host"; the client and
    account variables merely drive the host dropdown. So per client org we:
      * point datasources at the org's Prometheus + Infinity UIDs,
      * rescope the client/account/host variables to this client (via the API),
      * rewrite the dashboard uid and the nav-bar links to client-scoped uids.
    Returns a list of dashboard payloads ready for POST /api/dashboards/db.
    """
    slug = _client_slug(client)
    payloads = []
    for fname, suffix in _DRILLDOWNS:
        path = _os.path.join(_DASHBOARDS_DIR, fname)
        if not _os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        raw = raw.replace("__PROM_DS_UID__", prom_uid).replace("__INFINITY_DS_UID__", inf_uid)
        # rewrite every main uid token -> client-scoped uid (covers own uid + nav links)
        for token, sfx in _UID_TOKENS.items():
            raw = raw.replace(token, f"client-{slug}-{sfx}")
        obj = json.loads(raw)
        d = obj["dashboard"]
        d["templating"] = {"list": _client_drilldown_vars(
            client,
            prom_uid,
            inf_uid,
            with_port=(suffix == "dd-port"),
            with_process=(suffix == "dd-proc"),
        )}
        # client viewers are read-only: strip the embedded server-editor row (id 90)
        d["panels"] = [p for p in d.get("panels", []) if p.get("id") != 90]
        d.pop("id", None)
        obj.pop("folderUid", None)
        obj["overwrite"] = True
        payloads.append(obj)
    return payloads


def _build_client_fleet_dashboard(client, prom_uid, inf_uid):
    """Build a client-scoped 'Fleet Overview - All Servers' dashboard.

    Servers are scoped by *host membership from the central API* (the authoritative
    record of which servers belong to a client), not by the metric `client` label.
    This means assigning a server to a client in the management UI takes effect
    immediately, without re-installing Alloy on the node. The `host` variable is
    populated from /client-hosts?client=<client>, so selecting "All" expands to
    exactly this client's servers.
    """
    slug = _client_slug(client)
    uid = f"client-{slug}-fleet"
    pds = {"type": "prometheus", "uid": prom_uid}
    inf = {"type": "yesoreyeram-infinity-datasource", "uid": inf_uid}
    sel = 'job="integrations/unix",host=~"$host"'
    bsel = 'job="blackbox",host=~"$host"'
    node_up_expr = f'max by (host) (up{{{sel}}})'
    port_down_expr = f'max by (host) ((label_replace(probe_success{{{bsel}}}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)") * on(host,port) group_left() monitor_port_info) == bool 0)'
    disk_filter = f'{sel},fstype!~"tmpfs|devtmpfs|overlay|squashfs|aufs|nsfs|proc|sysfs|devpts|cgroup2?|pstore|securityfs|tracefs|debugfs|fusectl|mqueue|hugetlbfs|configfs|ramfs",mountpoint!~"/run($|/.*)|/var/lib/docker($|/.*)|/var/lib/containerd($|/.*)|/snap($|/.*)"'
    network_filter = f'{sel},device!~"lo|docker.*|veth.*|br-.*|flannel.*|cali.*|tun.*|tap.*"'
    process_regex = "alloy|grafana|prometheus|sshd|nginx|gunicorn|celery|redis-server|mongod|docker|dockerd|containerd|caddy|postgres|postgresql|mysql|mysqld"
    process_down_expr = (
        f'count by (host) ((count_over_time(namedprocess_namegroup_num_procs{{{sel},groupname=~"{process_regex}"}}[1h]) > 0) '
        f'unless on(host,groupname) (namedprocess_namegroup_num_procs{{{sel},groupname=~"{process_regex}"}} > 0))'
    )
    cpu_expr = f'100 - (avg by (host) (rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])) * 100)'
    mem_expr = f'1 - (max by (host) (node_memory_MemAvailable_bytes{{{sel}}}) / max by (host) (node_memory_MemTotal_bytes{{{sel}}}))'
    mem_expr = f'({mem_expr}) * 100'
    disk_expr = f'max by (host) (100 - ((node_filesystem_avail_bytes{{{disk_filter}}} / node_filesystem_size_bytes{{{disk_filter}}}) * 100))'
    network_expr = (
        f'max by (host) (rate(node_network_receive_errs_total{{{network_filter}}}[5m]) '
        f'+ rate(node_network_transmit_errs_total{{{network_filter}}}[5m]) '
        f'+ rate(node_network_receive_drop_total{{{network_filter}}}[5m]) '
        f'+ rate(node_network_transmit_drop_total{{{network_filter}}}[5m]))'
    )
    critical_expr = f'(({port_down_expr} > bool 0) or (({process_down_expr}) > bool 0) or (({cpu_expr}) >= bool 90) or (({mem_expr}) >= bool 90) or (({disk_expr}) >= bool 90) or (({network_expr}) >= bool 10))'
    warning_expr = f'((({cpu_expr}) >= bool 70) or (({mem_expr}) >= bool 70) or (({disk_expr}) >= bool 70) or (({network_expr}) >= bool 1))'
    critical_flag_expr = f'(({critical_expr}) or on(host) (0 * {node_up_expr}))'
    warning_flag_expr = f'(({warning_expr}) or on(host) (0 * {node_up_expr}))'
    server_status_expr = f'({node_up_expr} * (1 + ({warning_flag_expr} * (1 - {critical_flag_expr})) + (2 * {critical_flag_expr})))'

    def stat(pid, x, title, color, expr, unit="short"):
        return {"datasource": pds, "fieldConfig": {"defaults": {"color": {"fixedColor": color, "mode": "fixed"}, "unit": unit}},
                "gridPos": {"h": 4, "w": 4, "x": x, "y": 0}, "id": pid,
                "options": {"colorMode": "background", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]}, "textMode": "value"},
                "targets": [{"expr": expr, "refId": "A"}], "title": title, "type": "stat"}

    return {
        "dashboard": {
            "uid": uid,
            "title": "Fleet Overview - All Servers",
            "tags": ["fleet", "overview", "client", slug],
            "refresh": "30s",
            "time": {"from": "now-5m", "to": "now"},
            "templating": {"list": [
                {"name": "client", "type": "constant", "query": client,
                 "current": {"text": client, "value": client}, "hide": 2},
                {"name": "account", "type": "query", "label": "Account", "datasource": inf,
                 "includeAll": True, "allValue": ".*",
                 "current": {"text": "All", "value": "$__all"},
                 "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
                   "url": f"http://localhost:9099/client-accounts?client={client}"},
                   "queryType": "infinity", "type": "infinity"},
                 "refresh": 1, "sort": 0},
                {"name": "host", "type": "query", "label": "Server", "datasource": inf,
                 "includeAll": True,
                 "current": {"text": "All", "value": "$__all"},
                 "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
                   "url": f"http://localhost:9099/client-hosts?client={client}&account=${{account}}"},
                   "queryType": "infinity", "type": "infinity"},
                 "refresh": 1, "sort": 0},
            ]},
            "panels": [
                stat(1, 0, "Total Servers", "blue", f'count(max by (host) (up{{{sel}}})) or on() vector(0)'),
                stat(2, 4, "Up", "green", f'count(({server_status_expr}) == 1) or on() vector(0)'),
                stat(3, 8, "Down", "red", f'count(({server_status_expr}) == 0) or on() vector(0)'),
                stat(4, 12, "Critical Servers", "red", f'count(({server_status_expr}) == 3) or on() vector(0)'),
                stat(5, 16, "Warning Servers", "orange", f'count(({server_status_expr}) == 2) or on() vector(0)'),
                stat(6, 20, "Ports Up", "green", f'count((label_replace(probe_success{{{bsel}}}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)") * on(host,port) group_left() monitor_port_info) == 1) or on() vector(0)'),
                {"datasource": pds, "description": "Your servers with live health status. Click a host to drill down.",
                 "fieldConfig": {"defaults": {"custom": {"align": "left", "filterable": True}}, "overrides": [
                     {"matcher": {"id": "byName", "options": "Status"}, "properties": [{"id": "mappings", "value": [{"options": {"0": {"color": "red", "index": 0, "text": "\ud83d\udd34 DOWN"}, "1": {"color": "green", "index": 1, "text": "\ud83d\udfe2 UP"}, "2": {"color": "orange", "index": 2, "text": "\ud83d\udfe0 WARNING"}, "3": {"color": "red", "index": 3, "text": "\ud83d\udd34 CRITICAL"}}, "type": "value"}]}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "custom.width", "value": 120}]},
                     {"matcher": {"id": "byName", "options": "CPU %"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}, {"id": "custom.cellOptions", "value": {"mode": "gradient", "type": "gauge"}}, {"id": "max", "value": 100}, {"id": "min", "value": 0}, {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 70}, {"color": "red", "value": 90}]}}]},
                     {"matcher": {"id": "byName", "options": "Memory %"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}, {"id": "custom.cellOptions", "value": {"mode": "gradient", "type": "gauge"}}, {"id": "max", "value": 100}, {"id": "min", "value": 0}, {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 70}, {"color": "red", "value": 90}]}}]},
                     {"matcher": {"id": "byName", "options": "Disk %"}, "properties": [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}, {"id": "custom.cellOptions", "value": {"mode": "gradient", "type": "gauge"}}, {"id": "max", "value": 100}, {"id": "min", "value": 0}, {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 70}, {"color": "red", "value": 90}]}}]},
                     {"matcher": {"id": "byName", "options": "Load"}, "properties": [{"id": "decimals", "value": 2}]},
                     {"matcher": {"id": "byName", "options": "Uptime"}, "properties": [{"id": "unit", "value": "s"}]},
                     {"matcher": {"id": "byName", "options": "Host"}, "properties": [{"id": "links", "value": [{"title": "Drill down to $__value.raw", "url": f"/d/client-{slug}-summary/my-servers?var-host=${{__value.raw}}"}]}]}
                 ]},
                 "gridPos": {"h": 18, "w": 24, "x": 0, "y": 4}, "id": 10,
                 "options": {"cellHeight": "md", "footer": {"show": False}, "showHeader": True, "sortBy": [{"desc": False, "displayName": "Status"}]},
                 "targets": [
                     {"expr": server_status_expr, "format": "table", "instant": True, "refId": "Status"},
                     {"expr": f'100 - (avg by (host) (rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])) * 100)', "format": "table", "instant": True, "refId": "CPU"},
                     {"expr": f'(1 - (max by (host) (node_memory_MemAvailable_bytes{{{sel}}}) / max by (host) (node_memory_MemTotal_bytes{{{sel}}}))) * 100', "format": "table", "instant": True, "refId": "Memory"},
                     {"expr": f'max by (host) (100 - ((node_filesystem_avail_bytes{{{disk_filter}}} / node_filesystem_size_bytes{{{disk_filter}}}) * 100))', "format": "table", "instant": True, "refId": "Disk"},
                     {"expr": f'sum by (host) (node_filesystem_size_bytes{{{disk_filter}}} - node_filesystem_avail_bytes{{{disk_filter}}}) / 1024 / 1024 / 1024', "format": "table", "instant": True, "refId": "DiskUsed"},
                     {"expr": f'sum by (host) (node_filesystem_size_bytes{{{disk_filter}}}) / 1024 / 1024 / 1024', "format": "table", "instant": True, "refId": "DiskTotal"},
                     {"expr": f'max by (host) (node_load1{{{sel}}})', "format": "table", "instant": True, "refId": "Load"},
                     {"expr": f'time() - max by (host) (node_boot_time_seconds{{{sel}}})', "format": "table", "instant": True, "refId": "Uptime"}
                 ],
                 "title": "Server Status",
                 "transformations": [
                     {"id": "joinByField", "options": {"byField": "host", "mode": "outer"}},
                     {"id": "organize", "options": {"excludeByName": {"Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True, "Time 5": True, "Time 6": True, "Time 7": True, "Time 8": True}, "indexByName": {"host": 0, "Value #Status": 1, "Value #CPU": 2, "Value #Memory": 3, "Value #Disk": 4, "Value #DiskUsed": 5, "Value #DiskTotal": 6, "Value #Load": 7, "Value #Uptime": 8}, "renameByName": {"host": "Host", "Value #Status": "Status", "Value #CPU": "CPU %", "Value #Memory": "Memory %", "Value #Disk": "Disk %", "Value #DiskUsed": "Disk Used GB", "Value #DiskTotal": "Disk Total GB", "Value #Load": "Load", "Value #Uptime": "Uptime"}}}
                 ], "type": "table"}
            ]
        },
        "overwrite": True
    }


def _build_client_dashboard(client, prom_uid, inf_uid):
    """Build client-scoped 'My Servers' dashboard JSON.

    Scoped by host membership from the central API (the `host` variable is
    populated from /client-hosts?client=<client>), so it does not depend on the
    metric `client` label and works the moment a server is assigned in the UI.
    """
    uid = f"client-{client.lower().replace(' ', '-')}-summary"
    return {
        "dashboard": {
            "uid": uid,
            "title": "My Servers",
            "tags": ["client", client.lower()],
            "refresh": "30s",
            "time": {"from": "now-6h", "to": "now"},
            "templating": {"list": [
                {"name": "client", "type": "constant", "query": client,
                 "current": {"text": client, "value": client}, "hide": 2},
                {"name": "account", "type": "query", "label": "Account",
                 "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": inf_uid},
                 "includeAll": True, "allValue": ".*",
                 "current": {"text": "All", "value": "$__all"},
                 "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
                   "url": f"http://localhost:9099/client-accounts?client={client}"},
                   "queryType": "infinity", "type": "infinity"},
                 "refresh": 1, "sort": 0},
                {"name": "host", "type": "query", "label": "Server",
                 "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": inf_uid},
                 "includeAll": False,
                 "query": {"infinityQuery": {"refId": "variable", "source": "url", "type": "json",
                   "url": f"http://localhost:9099/client-hosts?client={client}&account=${{account}}"},
                   "queryType": "infinity", "type": "infinity"},
                 "refresh": 1, "sort": 0},
            ]},
            "panels": [
                {"type":"stat","id":1,"title":"Availability","gridPos":{"h":4,"w":4,"x":0,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":2,"max":100,"min":0,"thresholds":{"mode":"absolute","steps":[{"color":"red","value":None},{"color":"orange","value":95},{"color":"green","value":99}]}}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'max(avg_over_time(up{job="integrations/unix",host="$host"}[$__range])) * 100',"refId":"A"}]},
                {"type":"stat","id":2,"title":"CPU","gridPos":{"h":4,"w":4,"x":4,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'100 - (avg(rate(node_cpu_seconds_total{host="$host",mode="idle"}[5m])) * 100)',"refId":"A"}]},
                {"type":"stat","id":3,"title":"Memory","gridPos":{"h":4,"w":4,"x":8,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'(1 - (node_memory_MemAvailable_bytes{host="$host"} / node_memory_MemTotal_bytes{host="$host"})) * 100',"refId":"A"}]},
                {"type":"stat","id":4,"title":"Disk /","gridPos":{"h":4,"w":4,"x":12,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'100 - ((node_filesystem_avail_bytes{host="$host",mountpoint="/",fstype!="tmpfs"} / node_filesystem_size_bytes{host="$host",mountpoint="/",fstype!="tmpfs"}) * 100)',"refId":"A"}]},
                {"type":"stat","id":5,"title":"Ports Up","gridPos":{"h":4,"w":4,"x":16,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"color":{"fixedColor":"green","mode":"fixed"},"unit":"short"}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'count((label_replace(probe_success{host="$host",job="blackbox"}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)") * on(host,port) group_left() monitor_port_info) == 1) or on() vector(0)',"refId":"A"}]},
                {"type":"stat","id":6,"title":"Ports Down","gridPos":{"h":4,"w":4,"x":20,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"color":{"fixedColor":"red","mode":"fixed"},"unit":"short"}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'count((label_replace(probe_success{host="$host",job="blackbox"}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)") * on(host,port) group_left() monitor_port_info) == 0) or on() vector(0)',"refId":"A"}]},
                {"type":"timeseries","id":11,"title":"CPU Over Time","gridPos":{"h":8,"w":12,"x":0,"y":4},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","color":{"mode":"palette-classic"},"custom":{"fillOpacity":20,"lineWidth":2}}},
                 "options":{"legend":{"displayMode":"list","placement":"bottom"}},
                 "targets":[{"expr":'100 - (avg(rate(node_cpu_seconds_total{host="$host",mode="idle"}[5m])) * 100)',"legendFormat":"CPU %","refId":"A"}]},
                {"type":"timeseries","id":12,"title":"Memory Over Time","gridPos":{"h":8,"w":12,"x":12,"y":4},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","color":{"mode":"palette-classic"},"custom":{"fillOpacity":20,"lineWidth":2}}},
                 "options":{"legend":{"displayMode":"list","placement":"bottom"}},
                 "targets":[{"expr":'(1 - (node_memory_MemAvailable_bytes{host="$host"} / node_memory_MemTotal_bytes{host="$host"})) * 100',"legendFormat":"Mem %","refId":"A"}]},
                {"type":"table","id":13,"title":"Monitored Ports","gridPos":{"h":8,"w":12,"x":0,"y":12},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"custom":{"align":"left"},"mappings":[{"options":{"0":{"color":"red","text":"DOWN"},"1":{"color":"green","text":"UP"}},"type":"value"}]}},
                 "options":{"cellHeight":"sm","showHeader":True},
                 "targets":[{"expr":'label_replace(probe_success{host="$host",job="blackbox"}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)") * on(host,port) group_left() monitor_port_info',"format":"table","instant":True,"refId":"A"}],
                 "transformations":[{"id":"organize","options":{"excludeByName":{"Time":True,"__name__":True,"account":True,"client":True,"host":True,"instance":True,"job":True},"renameByName":{"Value":"Status","port":"Port"}}}]},
                {"type":"table","id":14,"title":"Top Processes","gridPos":{"h":8,"w":12,"x":12,"y":12},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"custom":{"align":"left"}},"overrides":[{"matcher":{"id":"byName","options":"CPU %"},"properties":[{"id":"unit","value":"percent"},{"id":"decimals","value":2}]},{"matcher":{"id":"byName","options":"Memory"},"properties":[{"id":"unit","value":"decbytes"}]}]},
                 "options":{"cellHeight":"sm","showHeader":True,"sortBy":[{"desc":True,"displayName":"CPU %"}]},
                 "targets":[
                     {"expr":'topk(10, sum by (groupname)(rate(namedprocess_namegroup_cpu_seconds_total{host="$host"}[5m]))*100)',"format":"table","instant":True,"refId":"A"},
                     {"expr":'sum by (groupname)(namedprocess_namegroup_memory_bytes{host="$host",memtype="resident"})',"format":"table","instant":True,"refId":"B"}
                 ],
                 "transformations":[
                     {"id":"joinByField","options":{"byField":"groupname","mode":"outer"}},
                     {"id":"organize","options":{"excludeByName":{"Time 1":True,"Time 2":True},"indexByName":{"groupname":0,"Value #A":1,"Value #B":2},"renameByName":{"Value #A":"CPU %","Value #B":"Memory","groupname":"Process"}}},
                     {"id":"sortBy","options":{"sort":[{"desc":True,"field":"CPU %"}]}}
                 ]},
            ]
        },
        "overwrite": True
    }


# ---- main-org dashboard / alert-rule sync ----------------------------------

def _datasource_uid_by_name(name, fallback=""):
    status, data = _grafana_req("GET", f"/api/datasources/name/{urllib.parse.quote(name)}")
    if status == 200 and isinstance(data, dict):
        return data.get("uid") or fallback
    return fallback


def _ensure_monitoring_folder():
    status, data = _grafana_req("GET", "/api/folders/monitoring")
    if status == 200:
        return True, "exists"
    status, data = _grafana_req("POST", "/api/folders", {"uid": "monitoring", "title": "Monitoring"})
    return status in (200, 201, 412), data.get("message", str(data))


def sync_main_dashboards():
    """Deploy every dashboard JSON bundled with the API package.

    Custom dashboards can be added by dropping a Grafana dashboard payload into
    the repo-root `dashboards/` directory. Placeholders are replaced at deploy time:
      __PROM_DS_UID__, __INFINITY_DS_UID__, __MONITOR_API_PUBLIC_URL__.
    """
    cfg = load_config()
    prom_uid = _datasource_uid_by_name("Prometheus", "PBFA97CFB590B2093")
    inf_uid = _datasource_uid_by_name("Port Monitor API", "cfmmi8ef0wxz4a")
    public_url = (cfg.get("public_url") or "http://localhost:9099").rstrip("/")
    _grafana_switch_org(1)
    _ensure_monitoring_folder()
    results = []
    for fname in sorted(_os.listdir(_DASHBOARDS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = _os.path.join(_DASHBOARDS_DIR, fname)
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        raw = (
            raw.replace("__PROM_DS_UID__", prom_uid)
            .replace("__INFINITY_DS_UID__", inf_uid)
            .replace("__MONITOR_API_PUBLIC_URL__", public_url)
        )
        payload = json.loads(raw)
        payload.setdefault("folderUid", "monitoring")
        payload["overwrite"] = True
        ok, msg = _grafana_deploy_dashboard(1, payload)
        results.append({
            "file": fname,
            "uid": payload.get("dashboard", {}).get("uid", ""),
            "title": payload.get("dashboard", {}).get("title", ""),
            "ok": ok,
            "message": msg,
        })
    return results


def _prom_expr(ref_id, expr, from_seconds=600):
    return {
        "refId": ref_id,
        "datasourceUid": _datasource_uid_by_name("Prometheus", "PBFA97CFB590B2093"),
        "queryType": "",
        "relativeTimeRange": {"from": from_seconds, "to": 0},
        "model": {
            "expr": expr,
            "instant": True,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "refId": ref_id,
        },
    }


def _reduce_expr(ref_id, expression):
    return {
        "refId": ref_id,
        "datasourceUid": "__expr__",
        "queryType": "",
        "relativeTimeRange": {"from": 0, "to": 0},
        "model": {
            "expression": expression,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "reducer": "last",
            "refId": ref_id,
            "type": "reduce",
        },
    }


def _threshold_expr(ref_id, expression, evaluator_type, threshold):
    return {
        "refId": ref_id,
        "datasourceUid": "__expr__",
        "queryType": "",
        "relativeTimeRange": {"from": 0, "to": 0},
        "model": {
            "conditions": [{
                "evaluator": {"params": [threshold], "type": evaluator_type},
                "operator": {"type": "and"},
                "query": {"params": [expression]},
                "reducer": {"params": [], "type": "last"},
                "type": "query",
            }],
            "expression": expression,
            "intervalMs": 1000,
            "maxDataPoints": 43200,
            "refId": ref_id,
            "type": "threshold",
        },
    }


def _alert_rule(uid, title, group, expr, evaluator_type, threshold, duration,
                severity, summary, description, no_data="OK", exec_err="OK",
                is_paused=False):
    return {
        "uid": uid,
        "title": title,
        "folderUID": "monitoring",
        "ruleGroup": group,
        "condition": "C",
        "data": [
            _prom_expr("A", expr),
            _reduce_expr("B", "A"),
            _threshold_expr("C", "B", evaluator_type, threshold),
        ],
        "noDataState": no_data,
        "execErrState": exec_err,
        "for": duration,
        "keep_firing_for": "0s",
        "annotations": {"summary": summary, "description": description},
        "labels": {"severity": severity},
        "isPaused": bool(is_paused),
    }


def _builtin_alert_rules():
    from . import storage as st

    settings = st.load_alert_settings().get("rules", {})

    def cfg(rule_id):
        return settings.get(rule_id, {})

    def dur(rule_id, default_minutes):
        try:
            minutes = int(cfg(rule_id).get("duration_minutes", default_minutes))
        except Exception:
            minutes = default_minutes
        minutes = max(1, min(minutes, 1440))
        return f"{minutes}m"

    def sev(rule_id, default):
        value = str(cfg(rule_id).get("severity", default) or default).strip().lower()
        return value if value in ("critical", "warning", "info") else default

    def threshold(rule_id, key, default):
        try:
            return float(cfg(rule_id).get(key, default))
        except Exception:
            return float(default)

    def enabled(rule_id):
        return bool(cfg(rule_id).get("enabled", True))

    host_context = (
        "Server:  {{ $labels.name }}\n"
        "Host:    {{ $labels.host }}\n"
        "IP:      {{ $labels.ip }}\n"
        "Customer: {{ $labels.client }}\n"
        "Account: {{ $labels.account }}"
    )
    node_status = (
        '(max by (host)(up{job="integrations/unix"}) or on(host) (0 * monitor_node_info)) '
        '* on(host) group_left(ip,name,client,account) monitor_node_info'
    )
    cpu_base = '(100 - (avg by (host)(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100))'
    mem_base = '(max by (host)((1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100))'
    disk_fs_filter = 'fstype!~"tmpfs|devtmpfs|overlay|squashfs|aufs|nsfs|proc|sysfs|devpts|cgroup2?|pstore|securityfs|tracefs|debugfs|fusectl|mqueue|hugetlbfs|configfs|ramfs",mountpoint!~"/run($|/.*)|/var/lib/docker($|/.*)|/var/lib/containerd($|/.*)|/snap($|/.*)"'
    disk_base = f'(max by (host,mountpoint)(100 - ((node_filesystem_avail_bytes{{{disk_fs_filter}}} / node_filesystem_size_bytes{{{disk_fs_filter}}}) * 100)))'
    network_iface_filter = 'device!~"lo|docker.*|veth.*|br-.*|flannel.*|cali.*|tun.*|tap.*"'
    network_base = (
        f'max by (host,device) (rate(node_network_receive_errs_total{{job="integrations/unix",{network_iface_filter}}}[5m]) '
        f'+ rate(node_network_transmit_errs_total{{job="integrations/unix",{network_iface_filter}}}[5m]) '
        f'+ rate(node_network_receive_drop_total{{job="integrations/unix",{network_iface_filter}}}[5m]) '
        f'+ rate(node_network_transmit_drop_total{{job="integrations/unix",{network_iface_filter}}}[5m]))'
    )
    process_regex = str(cfg("process_down").get(
        "process_regex",
        "alloy|grafana|prometheus|sshd|nginx|gunicorn|celery|redis-server|mongod|docker|dockerd|containerd|caddy|postgres|postgresql|mysql|mysqld",
    ) or "").replace("\\", "\\\\").replace('"', '\\"')
    process_down_base = (
        'count by (host,groupname) ('
        f'(count_over_time(namedprocess_namegroup_num_procs{{job="integrations/unix",groupname=~"{process_regex}"}}[1h]) > 0) '
        f'unless on(host,groupname) (namedprocess_namegroup_num_procs{{job="integrations/unix",groupname=~"{process_regex}"}} > 0)'
        ')'
    )
    rules = [
        _alert_rule(
            "efohj7qoihzi8b", "Node Down", "availability", node_status, "lt", 1, dur("node_down", 2),
            sev("node_down", "critical"), "{{ $labels.name }} ({{ $labels.host }}) is DOWN",
            host_context + "\n\nThe node is reporting up=0 or has stopped sending metrics.",
            no_data="Alerting", exec_err="Alerting", is_paused=not enabled("node_down"),
        ),
        _alert_rule(
            "afohj8eu0nklcb", "Port Down", "availability",
            'max by (host,port)(label_replace(probe_success{job="blackbox"}, "port", "$1", "port", "(?:integrations/blackbox/)?(.+)")) * on(host,port) group_left() monitor_port_info * on(host) group_left(ip,name,client,account) monitor_node_info',
            "lt", 1, dur("port_down", 2), sev("port_down", "critical"),
            "Port {{ $labels.port }} DOWN on {{ $labels.name }} ({{ $labels.host }})",
            host_context + "\nPort:    {{ $labels.port }}\n\nThe port probe is failing (probe_success=0).",
            is_paused=not enabled("port_down"),
        ),
        _alert_rule(
            "process-down-critical", "Process Down", "availability",
            f'({process_down_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", 0, dur("process_down", 2), sev("process_down", "critical"),
            "Process {{ $labels.groupname }} DOWN on {{ $labels.name }} ({{ $labels.host }})",
            host_context + "\nProcess: {{ $labels.groupname }}\n\nThe process was present in the last hour but is no longer running.",
            is_paused=not enabled("process_down"),
        ),
        _alert_rule(
            "uptime-kuma-source-down", "Site Monitoring Source Down", "uptime-kuma",
            'max by (instance, job) (up{job="uptime-kuma"})',
            "lt", 1, dur("uptime_kuma_source_down", 2), sev("uptime_kuma_source_down", "critical"),
            "Site monitoring source {{ $labels.instance }} is DOWN",
            "Source: {{ $labels.instance }}\n"
            "Service: {{ $labels.job }}\n\n"
            "Prometheus cannot scrape Uptime Kuma metrics. Check the /metrics API key in Prometheus.",
            is_paused=not enabled("uptime_kuma_source_down"),
        ),
        _alert_rule(
            "uptime-kuma-monitor-down", "Site Monitor Down", "uptime-kuma",
            'min by (monitor_name) (monitor_status{job="uptime-kuma"}) * on(monitor_name) group_left(site,name,client,account) monitor_kuma_site_info',
            "lt", 1, dur("uptime_kuma_monitor_down", 2), sev("uptime_kuma_monitor_down", "critical"),
            "Site monitor {{ $labels.name }} is DOWN",
            "Monitor: {{ $labels.name }}\n"
            "Customer: {{ $labels.client }}\n"
            "Account: {{ $labels.account }}\n\n"
            "The site monitor is reporting DOWN.",
            is_paused=not enabled("uptime_kuma_monitor_down"),
        ),
    ]
    cpu_warning = threshold("high_cpu", "warning_threshold", 70)
    cpu_critical = threshold("high_cpu", "critical_threshold", 90)
    rules.append(_alert_rule(
            "ffohj93hcl2iof", "High CPU Usage Warning", "resources",
            f'(({cpu_base} > bool {cpu_warning:g}) * ({cpu_base} < bool {cpu_critical:g}) * {cpu_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", cpu_warning, dur("high_cpu", 5), "warning",
            "High CPU warning on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\n\nCPU usage is between {cpu_warning:g}% and {cpu_critical:g}% for {cfg('high_cpu').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("high_cpu"),
        ))
    rules.append(_alert_rule(
            "high-cpu-critical", "High CPU Usage Critical", "resources",
            f'({cpu_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", cpu_critical, dur("high_cpu", 5), "critical",
            "High CPU critical on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\n\nCPU usage has been above {cpu_critical:g}% for {cfg('high_cpu').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("high_cpu"),
        ))
    mem_warning = threshold("high_memory", "warning_threshold", 70)
    mem_critical = threshold("high_memory", "critical_threshold", 90)
    rules.append(_alert_rule(
            "bfohj9rdcmk8wf", "High Memory Usage Warning", "resources",
            f'(({mem_base} > bool {mem_warning:g}) * ({mem_base} < bool {mem_critical:g}) * {mem_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", mem_warning, dur("high_memory", 5), "warning",
            "High memory warning on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\n\nMemory usage is between {mem_warning:g}% and {mem_critical:g}% for {cfg('high_memory').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("high_memory"),
        ))
    rules.append(_alert_rule(
            "high-memory-critical", "High Memory Usage Critical", "resources",
            f'({mem_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", mem_critical, dur("high_memory", 5), "critical",
            "High memory critical on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\n\nMemory usage has been above {mem_critical:g}% for {cfg('high_memory').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("high_memory"),
        ))
    disk_warning = threshold("low_disk", "warning_threshold", 70)
    disk_critical = threshold("low_disk", "critical_threshold", 90)
    rules.append(_alert_rule(
            "efohjag79sf0ga", "Low Disk Space Warning", "resources",
            f'(({disk_base} > bool {disk_warning:g}) * ({disk_base} < bool {disk_critical:g}) * {disk_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", disk_warning, dur("low_disk", 5), "warning",
            "Low disk warning on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\nFilesystem: {{{{ $labels.mountpoint }}}}\n\nDisk usage on this filesystem is between {disk_warning:g}% and {disk_critical:g}% for {cfg('low_disk').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("low_disk"),
        ))
    rules.append(_alert_rule(
            "low-disk-critical", "Low Disk Space Critical", "resources",
            f'({disk_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", disk_critical, dur("low_disk", 5), "critical",
            "Low disk critical on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\nFilesystem: {{{{ $labels.mountpoint }}}}\n\nDisk usage on this filesystem has been above {disk_critical:g}% for {cfg('low_disk').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}%).",
            is_paused=not enabled("low_disk"),
        ))
    network_warning = threshold("network_errors", "warning_threshold", 1)
    network_critical = threshold("network_errors", "critical_threshold", 10)
    rules.append(_alert_rule(
            "network-errors-warning", "Network Errors Warning", "resources",
            f'(({network_base} > bool {network_warning:g}) * ({network_base} < bool {network_critical:g}) * {network_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", network_warning, dur("network_errors", 5), "warning",
            "Network errors warning on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\nDevice: {{{{ $labels.device }}}}\n\nNetwork errors/drops are between {network_warning:g}/s and {network_critical:g}/s for {cfg('network_errors').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}/s).",
            is_paused=not enabled("network_errors"),
        ))
    rules.append(_alert_rule(
            "network-errors-critical", "Network Errors Critical", "resources",
            f'({network_base}) * on(host) group_left(ip,name,client,account) monitor_node_info',
            "gt", network_critical, dur("network_errors", 5), "critical",
            "Network errors critical on {{ $labels.name }} ({{ $labels.host }})",
            host_context + f"\nDevice: {{{{ $labels.device }}}}\n\nNetwork errors/drops have been above {network_critical:g}/s for {cfg('network_errors').get('duration_minutes', 5)} minutes (current: {{{{ $values.B }}}}/s).",
            is_paused=not enabled("network_errors"),
        ))
    return rules


def _custom_alert_rules():
    """Load optional custom alert rule JSON payloads from app/alerts."""
    if not _os.path.isdir(_ALERTS_DIR):
        return []
    prom_uid = _datasource_uid_by_name("Prometheus", "PBFA97CFB590B2093")
    rules = []
    for fname in sorted(_os.listdir(_ALERTS_DIR)):
        if not fname.endswith(".json"):
            continue
        path = _os.path.join(_ALERTS_DIR, fname)
        with open(path, encoding="utf-8") as f:
            raw = f.read().replace("__PROM_DS_UID__", prom_uid)
        payload = json.loads(raw)
        if isinstance(payload, list):
            rules.extend(payload)
        else:
            rules.append(payload)
    return rules


def sync_alert_rules():
    """Create/update built-in and optional custom alert rules in Grafana."""
    _grafana_switch_org(1)
    _ensure_monitoring_folder()
    sync_notification_template()
    results = []
    for rule in _builtin_alert_rules() + _custom_alert_rules():
        uid = rule.get("uid")
        if not uid:
            results.append({"title": rule.get("title", ""), "ok": False, "message": "missing uid"})
            continue
        existing_status, _ = _grafana_req("GET", f"/api/v1/provisioning/alert-rules/{uid}")
        if existing_status == 200:
            status, data = _grafana_req("PUT", f"/api/v1/provisioning/alert-rules/{uid}", rule, _PROV_HDR)
        else:
            status, data = _grafana_req("POST", "/api/v1/provisioning/alert-rules", rule, _PROV_HDR)
        if isinstance(data, dict):
            message = data.get("message") or data.get("uid") or data.get("title") or "synced"
        else:
            message = str(data)
        results.append({
            "uid": uid,
            "title": rule.get("title", ""),
            "ok": status in (200, 201, 202),
            "status": status,
            "message": message,
        })
    return results


# ---- alerting: per-client email contact points + notification routing -------

import re as _re

_PROV_HDR = {"X-Disable-Provenance": "true"}  # keep resources editable in the UI

# Reserved recipient key whose route matches every alert (all clients).
ALL_CLIENTS_KEY = "*"
ALL_CLIENTS_RECEIVER = "all-clients"
ALERT_TEMPLATE_NAME = "zentra.email"


def _monitor_public_url():
    from .config import load_config
    cfg = load_config()
    return (cfg.get("public_url") or "http://localhost:9099").rstrip("/")


def _alert_templates_dir():
    here = os.path.dirname(__file__)
    for candidate in (
        os.path.join(os.path.dirname(here), "alert"),
        os.path.join(here, "..", "..", "alert"),
        "/opt/port-monitor-api/alert",
        "/home/ubuntu/monitoring/alert",
    ):
        path = os.path.abspath(candidate)
        if os.path.isdir(path):
            return path
    return os.path.abspath(os.path.join(here, "..", "..", "alert"))


def _read_alert_template_file(filename, fallback=""):
    path = os.path.join(_alert_templates_dir(), filename)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return fallback


def _zentra_notification_template():
    logo_url = f"{_monitor_public_url()}/alert/logo-email.png"
    html_body = _read_alert_template_file(
        "ng_alert_notification.html",
        fallback="<p>Zentra alert notification</p>",
    ).replace("__LOGO_URL__", logo_url)
    text_body = _read_alert_template_file(
        "ng_alert_notification.txt",
        fallback="Zentra alert notification\n",
    )
    return (
        '{{ define "zentra.email.subject" }}[Zentra] {{ .Status | toUpper }}'
        '{{ if .CommonLabels.alertname }} {{ .CommonLabels.alertname }}{{ end }}'
        '{{ if .CommonLabels.severity }} · {{ .CommonLabels.severity | toUpper }}{{ end }}{{ end }}\n'
        '{{ define "zentra.email.html" }}' + html_body + '{{ end }}\n'
        '{{ define "zentra.email.message" }}' + text_body + '{{ end }}'
    )


def sync_notification_template():
    body = {"name": ALERT_TEMPLATE_NAME, "template": _zentra_notification_template()}
    status, data = _grafana_req("PUT", f"/api/v1/provisioning/templates/{ALERT_TEMPLATE_NAME}", body, _PROV_HDR)
    return status in (200, 201, 202), data.get("version") or data.get("message") or str(data)


def _email_contact_settings(addresses):
    return {
        "addresses": addresses,
        "singleEmail": False,
        "subject": '{{ template "zentra.email.subject" . }}',
        "message": '{{ template "zentra.email.html" . }}',
    }


def _client_receiver_name(client):
    if client == ALL_CLIENTS_KEY:
        return ALL_CLIENTS_RECEIVER
    return f"client-{_client_slug(client)}"


def _group_receiver_name(group_id):
    return f"group-{group_id}"


def _list_contact_points():
    _, data = _grafana_req("GET", "/api/v1/provisioning/contact-points")
    return data if isinstance(data, list) else []


def _get_contact_point_addresses(name):
    """Return the list of addresses currently on a named email contact point."""
    cp = next((c for c in _list_contact_points() if c.get("name") == name), None)
    if not cp:
        return []
    addr = (cp.get("settings", {}) or {}).get("addresses", "") or ""
    return [a.strip() for a in _re.split(r"[;,\s]+", addr) if a.strip()]


def _upsert_email_contact_point(name, emails):
    """Create or update an email contact point. `emails` may be a list or a
    delimited string; multiple addresses are joined with ';' for Grafana.
    Returns (ok, uid_or_msg)."""
    if isinstance(emails, (list, tuple)):
        addresses = ";".join(e.strip() for e in emails if e and e.strip())
    else:
        addresses = (emails or "").strip()
    sync_notification_template()
    body = {
        "name": name,
        "type": "email",
        "disableResolveMessage": False,
        "settings": _email_contact_settings(addresses),
    }
    existing = next((c for c in _list_contact_points() if c.get("name") == name), None)
    if existing and existing.get("uid"):
        body["uid"] = existing["uid"]
        status, data = _grafana_req(
            "PUT", f"/api/v1/provisioning/contact-points/{existing['uid']}", body, _PROV_HDR)
        return status in (200, 202), existing["uid"]
    status, data = _grafana_req("POST", "/api/v1/provisioning/contact-points", body, _PROV_HDR)
    return status in (200, 201, 202), data.get("uid", str(data))


def _delete_email_contact_point(name):
    existing = next((c for c in _list_contact_points() if c.get("name") == name), None)
    if existing and existing.get("uid"):
        _grafana_req("DELETE", f"/api/v1/provisioning/contact-points/{existing['uid']}", None, _PROV_HDR)


def _host_regex(hosts):
    """Anchored alternation of escaped hostnames for a label `host =~` matcher."""
    return "(" + "|".join(_re.escape(h) for h in sorted(set(hosts)) if h) + ")"


def rebuild_notification_policy(active, host_map, admin_receiver, group_routes=None, site_map=None):
    """Rebuild the notification policy tree.

    - admin_receiver always receives every alert (match-all child, continue=true)
    - `active` is {client: [enabled_emails]} (client/all-clients recipients)
    - `site_map` is {client: [Uptime Kuma monitor_name]} for site alerts
    - `group_routes` is an optional list of {"receiver": name, "hosts": [...]}
      for alert groups spanning arbitrary servers.
    Every route uses continue=true, so a host belonging to several scopes
    (its client + one or more groups) notifies all of them.
    Returns (ok, message).
    """
    routes = [
        {"receiver": admin_receiver, "object_matchers": [], "continue": True},
    ]
    site_map = site_map or {}
    for client, emails in sorted(active.items()):
        if not emails:
            continue
        if client == ALL_CLIENTS_KEY:
            matchers = []  # match every alert
            routes.append({
                "receiver": _client_receiver_name(client),
                "object_matchers": matchers,
                "continue": True,
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            })
            continue
        hosts = host_map.get(client, [])
        if hosts:
            routes.append({
                "receiver": _client_receiver_name(client),
                "object_matchers": [["host", "=~", _host_regex(hosts)]],
                "continue": True,
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            })
        sites = site_map.get(client, [])
        if sites:
            routes.append({
                "receiver": _client_receiver_name(client),
                "object_matchers": [["monitor_name", "=~", _host_regex(sites)]],
                "continue": True,
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            })
    for gr in (group_routes or []):
        hosts = gr.get("hosts") or []
        sites = gr.get("sites") or []
        if (not hosts and not sites) or not gr.get("receiver"):
            continue
        if hosts:
            routes.append({
                "receiver": gr["receiver"],
                "object_matchers": [["host", "=~", _host_regex(hosts)]],
                "continue": True,
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            })
        if sites:
            routes.append({
                "receiver": gr["receiver"],
                "object_matchers": [["monitor_name", "=~", _host_regex(sites)]],
                "continue": True,
                "group_wait": "30s",
                "group_interval": "5m",
                "repeat_interval": "4h",
            })
    policy = {
        "receiver": admin_receiver,
        "group_by": ["grafana_folder", "alertname", "host", "port", "monitor_name", "mountpoint", "groupname", "device"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": routes,
    }
    status, data = _grafana_req("PUT", "/api/v1/provisioning/policies", policy, _PROV_HDR)
    return status in (200, 202), data.get("message", str(data))
