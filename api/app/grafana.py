"""Grafana Admin API helpers + client dashboard builder.

Used to create/delete per-client orgs (org-per-client multi-tenancy) with
isolated read-only dashboards, all driven from the embedded UI.
"""

import base64
import json
import urllib.error
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

_DASHBOARDS_DIR = _os.path.join(_os.path.dirname(__file__), "dashboards")

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


def _client_drilldown_vars(client, inf_uid, with_port=False):
    inf = {"type": "yesoreyeram-infinity-datasource", "uid": inf_uid}
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
        d["templating"] = {"list": _client_drilldown_vars(client, inf_uid, with_port=(suffix == "dd-port"))}
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
                stat(2, 4, "Up", "green", f'count(max by (host) (up{{{sel}}}) == 1) or on() vector(0)'),
                stat(3, 8, "Down", "red", f'count(max by (host) (up{{{sel}}}) == 0) or on() vector(0)'),
                stat(4, 12, "Critical (CPU>90%)", "red", f'count((100 - (avg by (host) (rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])) * 100) > 90)) or on() vector(0)'),
                stat(5, 16, "Warning (CPU>70%)", "orange", f'count((100 - (avg by (host) (rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])) * 100) > 70 < 90)) or on() vector(0)'),
                stat(6, 20, "Ports Up", "green", f'count(probe_success{{{bsel}}} == 1) or on() vector(0)'),
                {"datasource": pds, "description": "Your servers with live health status. Click a host to drill down.",
                 "fieldConfig": {"defaults": {"custom": {"align": "left", "filterable": True}}, "overrides": [
                     {"matcher": {"id": "byName", "options": "Status"}, "properties": [{"id": "mappings", "value": [{"options": {"0": {"color": "red", "index": 0, "text": "\ud83d\udd34 DOWN"}, "1": {"color": "green", "index": 1, "text": "\ud83d\udfe2 UP"}}, "type": "value"}]}, {"id": "custom.cellOptions", "value": {"type": "color-text"}}, {"id": "custom.width", "value": 110}]},
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
                     {"expr": f'max by (host) (up{{{sel}}})', "format": "table", "instant": True, "refId": "Status"},
                     {"expr": f'100 - (avg by (host) (rate(node_cpu_seconds_total{{mode="idle",{sel}}}[5m])) * 100)', "format": "table", "instant": True, "refId": "CPU"},
                     {"expr": f'(1 - (max by (host) (node_memory_MemAvailable_bytes{{{sel}}}) / max by (host) (node_memory_MemTotal_bytes{{{sel}}}))) * 100', "format": "table", "instant": True, "refId": "Memory"},
                     {"expr": f'100 - ((max by (host) (node_filesystem_avail_bytes{{{sel},mountpoint="/",fstype!="tmpfs"}}) / max by (host) (node_filesystem_size_bytes{{{sel},mountpoint="/",fstype!="tmpfs"}})) * 100)', "format": "table", "instant": True, "refId": "Disk"},
                     {"expr": f'max by (host) (node_load1{{{sel}}})', "format": "table", "instant": True, "refId": "Load"},
                     {"expr": f'time() - max by (host) (node_boot_time_seconds{{{sel}}})', "format": "table", "instant": True, "refId": "Uptime"}
                 ],
                 "title": "Server Status",
                 "transformations": [
                     {"id": "joinByField", "options": {"byField": "host", "mode": "outer"}},
                     {"id": "organize", "options": {"excludeByName": {"Time 1": True, "Time 2": True, "Time 3": True, "Time 4": True, "Time 5": True, "Time 6": True}, "indexByName": {"host": 0, "Value #Status": 1, "Value #CPU": 2, "Value #Memory": 3, "Value #Disk": 4, "Value #Load": 5, "Value #Uptime": 6}, "renameByName": {"host": "Host", "Value #Status": "Status", "Value #CPU": "CPU %", "Value #Memory": "Memory %", "Value #Disk": "Disk %", "Value #Load": "Load", "Value #Uptime": "Uptime"}}}
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
                 "targets":[{"expr":'count(probe_success{host="$host",job="blackbox"} == 1) or on() vector(0)',"refId":"A"}]},
                {"type":"stat","id":6,"title":"Ports Down","gridPos":{"h":4,"w":4,"x":20,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"color":{"fixedColor":"red","mode":"fixed"},"unit":"short"}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":'count(probe_success{host="$host",job="blackbox"} == 0) or on() vector(0)',"refId":"A"}]},
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
                 "targets":[{"expr":'label_replace(probe_success{host="$host",job="blackbox"},"port","$1","port","(?:integrations/blackbox/)?(.+)")',"format":"table","instant":True,"refId":"A"}],
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


# ---- alerting: per-client email contact points + notification routing -------

import re as _re

_PROV_HDR = {"X-Disable-Provenance": "true"}  # keep resources editable in the UI

# Reserved recipient key whose route matches every alert (all clients).
ALL_CLIENTS_KEY = "*"
ALL_CLIENTS_RECEIVER = "all-clients"


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
    body = {"name": name, "type": "email",
            "settings": {"addresses": addresses, "singleEmail": False}}
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


def rebuild_notification_policy(active, host_map, admin_receiver, group_routes=None):
    """Rebuild the notification policy tree.

    - admin_receiver always receives every alert (match-all child, continue=true)
    - `active` is {client: [enabled_emails]} (client/all-clients recipients)
    - `group_routes` is an optional list of {"receiver": name, "hosts": [...]}
      for alert groups spanning arbitrary servers.
    Every route uses continue=true, so a host belonging to several scopes
    (its client + one or more groups) notifies all of them.
    Returns (ok, message).
    """
    routes = [
        {"receiver": admin_receiver, "object_matchers": [], "continue": True},
    ]
    for client, emails in sorted(active.items()):
        if not emails:
            continue
        if client == ALL_CLIENTS_KEY:
            matchers = []  # match every alert
        else:
            hosts = host_map.get(client, [])
            if not hosts:
                continue
            matchers = [["host", "=~", _host_regex(hosts)]]
        routes.append({
            "receiver": _client_receiver_name(client),
            "object_matchers": matchers,
            "continue": True,
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        })
    for gr in (group_routes or []):
        hosts = gr.get("hosts") or []
        if not hosts or not gr.get("receiver"):
            continue
        routes.append({
            "receiver": gr["receiver"],
            "object_matchers": [["host", "=~", _host_regex(hosts)]],
            "continue": True,
            "group_wait": "30s",
            "group_interval": "5m",
            "repeat_interval": "4h",
        })
    policy = {
        "receiver": admin_receiver,
        "group_by": ["grafana_folder", "alertname"],
        "group_wait": "30s",
        "group_interval": "5m",
        "repeat_interval": "4h",
        "routes": routes,
    }
    status, data = _grafana_req("PUT", "/api/v1/provisioning/policies", policy, _PROV_HDR)
    return status in (200, 202), data.get("message", str(data))
