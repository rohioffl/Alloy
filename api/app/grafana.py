"""Grafana Admin API helpers + client dashboard builder.

Used to create/delete per-client orgs (org-per-client multi-tenancy) with
isolated read-only dashboards, all driven from the embedded UI.
"""

import base64
import json
import urllib.error
import urllib.request

from .config import load_config


def _grafana_req(method, path, body=None):
    """Make a Grafana Admin API request using stored credentials."""
    cfg = load_config()
    base = (cfg.get("grafana_url") or "http://localhost:3000").rstrip("/")
    user = cfg.get("grafana_admin_user") or "admin"
    pw = cfg.get("grafana_admin_password") or "admin"
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode(),
    }
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())
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


def _build_client_dashboard(client, prom_uid, inf_uid):
    """Build client-scoped 'My Servers' dashboard JSON."""
    uid = f"client-{client.lower().replace(' ', '-')}-summary"
    safe = client.replace('"', '\"')
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
                 "targets":[{"expr":f'max(avg_over_time(up{{job="integrations/unix",client="{safe}",host="$host"}}[$__range])) * 100',"refId":"A"}]},
                {"type":"stat","id":2,"title":"CPU","gridPos":{"h":4,"w":4,"x":4,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":f'100 - (avg(rate(node_cpu_seconds_total{{client="{safe}",host="$host",mode="idle"}}[5m])) * 100)',"refId":"A"}]},
                {"type":"stat","id":3,"title":"Memory","gridPos":{"h":4,"w":4,"x":8,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":f'(1 - (node_memory_MemAvailable_bytes{{client="{safe}",host="$host"}} / node_memory_MemTotal_bytes{{client="{safe}",host="$host"}})) * 100',"refId":"A"}]},
                {"type":"stat","id":4,"title":"Disk /","gridPos":{"h":4,"w":4,"x":12,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","decimals":1,"thresholds":{"mode":"absolute","steps":[{"color":"green","value":None},{"color":"yellow","value":70},{"color":"red","value":90}]}}},
                 "options":{"colorMode":"value","graphMode":"area","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":f'100 - ((node_filesystem_avail_bytes{{client="{safe}",host="$host",mountpoint="/",fstype!="tmpfs"}} / node_filesystem_size_bytes{{client="{safe}",host="$host",mountpoint="/",fstype!="tmpfs"}}) * 100)',"refId":"A"}]},
                {"type":"stat","id":5,"title":"Ports Up","gridPos":{"h":4,"w":4,"x":16,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"color":{"fixedColor":"green","mode":"fixed"},"unit":"short"}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":f'count(probe_success{{client="{safe}",host="$host",job="blackbox"}} == 1) or on() vector(0)',"refId":"A"}]},
                {"type":"stat","id":6,"title":"Ports Down","gridPos":{"h":4,"w":4,"x":20,"y":0},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"color":{"fixedColor":"red","mode":"fixed"},"unit":"short"}},
                 "options":{"colorMode":"value","graphMode":"none","reduceOptions":{"calcs":["lastNotNull"]},"textMode":"value"},
                 "targets":[{"expr":f'count(probe_success{{client="{safe}",host="$host",job="blackbox"}} == 0) or on() vector(0)',"refId":"A"}]},
                {"type":"timeseries","id":11,"title":"CPU Over Time","gridPos":{"h":8,"w":12,"x":0,"y":4},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","color":{"mode":"palette-classic"},"custom":{"fillOpacity":20,"lineWidth":2}}},
                 "options":{"legend":{"displayMode":"list","placement":"bottom"}},
                 "targets":[{"expr":f'100 - (avg(rate(node_cpu_seconds_total{{client="{safe}",host="$host",mode="idle"}}[5m])) * 100)',"legendFormat":"CPU %","refId":"A"}]},
                {"type":"timeseries","id":12,"title":"Memory Over Time","gridPos":{"h":8,"w":12,"x":12,"y":4},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"unit":"percent","color":{"mode":"palette-classic"},"custom":{"fillOpacity":20,"lineWidth":2}}},
                 "options":{"legend":{"displayMode":"list","placement":"bottom"}},
                 "targets":[{"expr":f'(1 - (node_memory_MemAvailable_bytes{{client="{safe}",host="$host"}} / node_memory_MemTotal_bytes{{client="{safe}",host="$host"}})) * 100',"legendFormat":"Mem %","refId":"A"}]},
                {"type":"table","id":13,"title":"Monitored Ports","gridPos":{"h":8,"w":12,"x":0,"y":12},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"custom":{"align":"left"},"mappings":[{"options":{"0":{"color":"red","text":"DOWN"},"1":{"color":"green","text":"UP"}},"type":"value"}]}},
                 "options":{"cellHeight":"sm","showHeader":True},
                 "targets":[{"expr":f'label_replace(probe_success{{client="{safe}",host="$host",job="blackbox"}},"port","$1","port","(?:integrations/blackbox/)?(.+)")',"format":"table","instant":True,"refId":"A"}],
                 "transformations":[{"id":"organize","options":{"excludeByName":{"Time":True,"__name__":True,"account":True,"client":True,"host":True,"instance":True,"job":True},"renameByName":{"Value":"Status","port":"Port"}}}]},
                {"type":"table","id":14,"title":"Top Processes","gridPos":{"h":8,"w":12,"x":12,"y":12},
                 "datasource":{"type":"prometheus","uid":prom_uid},
                 "fieldConfig":{"defaults":{"custom":{"align":"left"}},"overrides":[{"matcher":{"id":"byName","options":"CPU %"},"properties":[{"id":"unit","value":"percent"},{"id":"decimals","value":2}]},{"matcher":{"id":"byName","options":"Memory"},"properties":[{"id":"unit","value":"decbytes"}]}]},
                 "options":{"cellHeight":"sm","showHeader":True,"sortBy":[{"desc":True,"displayName":"CPU %"}]},
                 "targets":[
                     {"expr":f'topk(10, sum by (groupname)(rate(namedprocess_namegroup_cpu_seconds_total{{client="{safe}",host="$host"}}[5m]))*100)',"format":"table","instant":True,"refId":"A"},
                     {"expr":f'sum by (groupname)(namedprocess_namegroup_memory_bytes{{client="{safe}",host="$host",memtype="resident"}})',"format":"table","instant":True,"refId":"B"}
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
