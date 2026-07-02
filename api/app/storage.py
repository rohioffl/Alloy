"""Data storage and domain helpers.

JSON files are kept fully compatible with the original implementation:
  /var/lib/port-monitor/nodes.json
  /var/lib/port-monitor/ports/<host>.json
  /var/lib/port-monitor/taxonomy.json
  /var/lib/port-monitor/grafana_orgs.json
"""

import fcntl
import json
import os
import re

from .config import (
    DATA_DIR,
    DEFAULT_CUSTOMER,
    DEFAULT_ENVIRONMENT,
    NODES_FILE,
    PROMETHEUS_URL,
    TAXONOMY_FILE,
    TIMESTAMP,
)
from .config import ALERT_RECIPIENTS_FILE
from .config import ALERT_GROUPS_FILE
from .config import ALERT_SETTINGS_FILE
from .config import KUMA_SITES_FILE


# ---- low-level JSON I/O ------------------------------------------------------

def _lock_file(f, exclusive=False):
    fcntl.flock(f.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        _lock_file(f)
        return json.load(f)


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        _lock_file(f, exclusive=True)
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


# ---- ports -------------------------------------------------------------------

def get_port_file(host):
    return os.path.join(DATA_DIR, "ports", f"{host}.json")


def load_ports(host):
    return _read_json(get_port_file(host), [])


def save_ports(host, targets):
    _write_json(get_port_file(host), targets)


def sanitize_port_name(name):
    """Prometheus/Alloy label-safe port name (used as blackbox job -> port label)."""
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (name or "").strip())
    return s.strip("_") or "port"


def alloy_targets(host):
    """Blackbox target list for Alloy (label-safe port names)."""
    return [
        {
            "name": sanitize_port_name(t["name"]),
            "address": t["address"],
            "module": t.get("module", "tcp_connect"),
        }
        for t in load_ports(host)
    ]


def probe_address(host, port, addr=None):
    """Build blackbox target address. Alloy probes from the node itself, so a
    bare port defaults to localhost (works regardless of DNS/registered IP)."""
    if addr and str(addr).strip():
        return str(addr).strip()
    nodes = load_nodes()
    node = find_node(nodes, host) or {}
    ip = (node.get("ip") or "").strip() or "localhost"
    return f"{ip}:{port}"


# ---- nodes -------------------------------------------------------------------

def load_nodes():
    return _read_json(NODES_FILE, [])


def save_nodes(nodes):
    _write_json(NODES_FILE, nodes)


def find_node(nodes, host):
    for n in nodes:
        if n["hostname"] == host:
            return n
    return None


def normalize_host(hostname):
    return (hostname or "").strip()



def _read_customer(d):
    return (d.get("customer") or d.get("client") or "").strip()


def _read_environment(d):
    return (d.get("environment") or d.get("account") or "").strip()


def _write_metadata_fields(d, customer, environment):
    d["customer"] = customer
    d["environment"] = environment
    d.pop("client", None)
    d.pop("account", None)


def _migrate_legacy_fields(d):
    changed = False
    if "client" in d:
        if "customer" not in d:
            d["customer"] = d["client"]
        d.pop("client", None)
        changed = True
    if "account" in d:
        if "environment" not in d:
            d["environment"] = d["account"]
        d.pop("account", None)
        changed = True
    return changed

def normalize_metadata(customer="", environment="", name="", **legacy):
    """Apply defaults so new installs always appear in Grafana dropdowns."""
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    c = (customer or "").strip() or DEFAULT_CUSTOMER
    a = (environment or "").strip() or DEFAULT_ENVIRONMENT
    return c, a, (name or "").strip()


def node_customer(n):
    return _read_customer(n) or DEFAULT_CUSTOMER


def node_environment(n):
    return _read_environment(n) or DEFAULT_ENVIRONMENT


def host_display(n):
    name = n.get("name") or n["hostname"]
    ip = n.get("ip", "")
    return f"{name} ({ip})" if ip else name


def ensure_server(host, create=False, defaults=None):
    nodes = load_nodes()
    existing = find_node(nodes, host)
    if existing:
        return existing, nodes, False
    if not create:
        return None, nodes, False
    d = defaults or {}
    c, a, nm = normalize_metadata(
        _read_customer(d), _read_environment(d), d.get("name", "")
    )
    entry = {
        "hostname": host,
        "ip": d.get("ip", ""),
        "name": nm or host,
        "customer": c,
        "environment": a,
        "registered": TIMESTAMP(),
        "last_seen": TIMESTAMP(),
    }
    nodes.append(entry)
    return entry, nodes, True


# ---- prometheus --------------------------------------------------------------

def _prom_query(expr, timeout=3):
    import urllib.request
    import urllib.parse
    q = urllib.parse.quote(expr)
    with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/query?query={q}", timeout=timeout) as r:
        return json.loads(r.read()).get("data", {}).get("result", [])


def prom_hosts():
    """Hosts that are actually running a node agent (up{job=integrations/unix}).

    Deliberately derived from real node metrics, NOT the global `host` label
    values — otherwise the monitor_node_info metadata metric (which we generate
    from the registry) would re-register hosts forever in a feedback loop.
    """
    try:
        result = _prom_query('up{job="integrations/unix"}')
        hosts = {s.get("metric", {}).get("host") for s in result}
        return sorted(h for h in hosts if h)
    except Exception:
        return []


# ---- Uptime Kuma sites -------------------------------------------------------

def load_kuma_site_meta():
    raw = _read_json(KUMA_SITES_FILE, {})
    if isinstance(raw, dict):
        return raw
    return {}


def save_kuma_site_meta(data):
    _write_json(KUMA_SITES_FILE, data)


def _kuma_target(metric):
    if metric.get("monitor_url"):
        return metric.get("monitor_url", "")
    host = metric.get("monitor_hostname", "")
    port = metric.get("monitor_port", "")
    if host and port:
        return f"{host}:{port}"
    return host or port or ""


def _kuma_live_sites():
    live = {}
    try:
        status_result = _prom_query('monitor_status{job="uptime-kuma"}')
    except Exception:
        status_result = []
    for row in status_result:
        metric = row.get("metric", {}) or {}
        monitor_name = (metric.get("monitor_name") or "").strip()
        if not monitor_name:
            continue
        try:
            status = float((row.get("value") or [None, None])[1])
        except Exception:
            status = None
        live[monitor_name] = {
            "monitor_name": monitor_name,
            "monitor_type": metric.get("monitor_type", ""),
            "monitor_url": metric.get("monitor_url", ""),
            "monitor_hostname": metric.get("monitor_hostname", ""),
            "monitor_port": metric.get("monitor_port", ""),
            "target": _kuma_target(metric),
            "status": status,
            "current": True,
        }
    try:
        rt_result = _prom_query('monitor_response_time{job="uptime-kuma"}')
    except Exception:
        rt_result = []
    for row in rt_result:
        metric = row.get("metric", {}) or {}
        monitor_name = (metric.get("monitor_name") or "").strip()
        if not monitor_name:
            continue
        site = live.setdefault(monitor_name, {
            "monitor_name": monitor_name,
            "monitor_type": metric.get("monitor_type", ""),
            "monitor_url": metric.get("monitor_url", ""),
            "monitor_hostname": metric.get("monitor_hostname", ""),
            "monitor_port": metric.get("monitor_port", ""),
            "target": _kuma_target(metric),
            "status": None,
            "current": True,
        })
        try:
            site["response_time"] = float((row.get("value") or [None, None])[1])
        except Exception:
            site["response_time"] = None
    return live


def sync_kuma_sites():
    """Discover Uptime Kuma monitors from Prometheus and retain local metadata."""
    meta = load_kuma_site_meta()
    live = _kuma_live_sites()
    changed = False
    for monitor_name in live:
        if monitor_name not in meta:
            c, a, _ = normalize_metadata("", "", "")
            meta[monitor_name] = {
                "monitor_name": monitor_name,
                "name": monitor_name,
                "customer": c,
                "environment": a,
                "registered": TIMESTAMP(),
                "last_seen": TIMESTAMP(),
            }
            changed = True
        elif not meta[monitor_name].get("last_seen"):
            meta[monitor_name]["last_seen"] = TIMESTAMP()
            changed = True
    if changed:
        save_kuma_site_meta(meta)
        for item in meta.values():
            record_taxonomy(_read_customer(item), _read_environment(item))
    return meta, live


def list_kuma_sites(customer="", environment="", **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    meta, live = sync_kuma_sites()
    sites = []
    for monitor_name in sorted(set(meta) | set(live), key=str.lower):
        m = meta.get(monitor_name, {})
        l = live.get(monitor_name, {})
        c, a, nm = normalize_metadata(_read_customer(m), _read_environment(m), m.get("name", ""))
        site = {
            "monitor_name": monitor_name,
            "name": nm or monitor_name,
            "customer": c,
            "environment": a,
            "monitor_type": l.get("monitor_type") or m.get("monitor_type", ""),
            "monitor_url": l.get("monitor_url") or m.get("monitor_url", ""),
            "monitor_hostname": l.get("monitor_hostname") or m.get("monitor_hostname", ""),
            "monitor_port": l.get("monitor_port") or m.get("monitor_port", ""),
            "target": l.get("target") or m.get("target", ""),
            "status": l.get("status"),
            "response_time": l.get("response_time"),
            "current": bool(l),
            "registered": m.get("registered", ""),
            "last_seen": m.get("last_seen", ""),
        }
        sites.append(site)
    return filter_kuma_sites(sites, customer, environment)


def find_kuma_site(monitor_name):
    monitor_name = (monitor_name or "").strip()
    for site in list_kuma_sites():
        if site["monitor_name"] == monitor_name:
            return site
    return None


def update_kuma_site(monitor_name, data):
    monitor_name = (monitor_name or "").strip()
    if not monitor_name:
        return None, "monitor name is required"
    meta, _ = sync_kuma_sites()
    existing = meta.setdefault(monitor_name, {
        "monitor_name": monitor_name,
        "registered": TIMESTAMP(),
    })
    c, a, nm = normalize_metadata(
        _read_customer(data) or _read_customer(existing),
        _read_environment(data) or _read_environment(existing),
        data.get("name", existing.get("name", monitor_name)),
    )
    existing["name"] = nm or monitor_name
    _write_metadata_fields(existing, c, a)
    existing["last_seen"] = TIMESTAMP()
    save_kuma_site_meta(meta)
    record_taxonomy(c, a)
    return find_kuma_site(monitor_name), None


def filter_kuma_sites(sites, customer="", environment="", **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    if customer and customer not in ("", ".*", "$__all", "All"):
        sites = [s for s in sites if _read_customer(s) == customer]
    if environment and environment not in ("", ".*", "$__all", "All"):
        sites = [s for s in sites if _read_environment(s) == environment]
    return sites


def customer_kuma_site_map():
    out = {}
    for s in list_kuma_sites():
        c = _read_customer(s) or DEFAULT_CUSTOMER
        out.setdefault(c, []).append(s["monitor_name"])
    return out


def sync_prom_hosts():
    """Auto-register any host seen in Prometheus that is not yet in nodes.json."""
    try:
        hosts = prom_hosts()
    except Exception:
        return load_nodes()
    nodes = load_nodes()
    known = {n["hostname"] for n in nodes}
    changed = False
    for h in hosts:
        if not h or h in known:
            continue
        # skip obvious non-host instances
        if ":" in h:
            continue
        c, a, _ = normalize_metadata("", "", "")
        nodes.append({
            "hostname": h,
            "ip": "",
            "name": h,
            "customer": c,
            "environment": a,
            "registered": TIMESTAMP(),
            "last_seen": TIMESTAMP(),
        })
        known.add(h)
        changed = True
    if changed:
        save_nodes(nodes)
    return nodes


# ---- taxonomy (customers & environments) ------------------------------------

def load_taxonomy():
    default = {"customers": [DEFAULT_CUSTOMER], "environments": {DEFAULT_CUSTOMER: [DEFAULT_ENVIRONMENT]}}
    tax = _read_json(TAXONOMY_FILE, default)
    changed = False
    if "clients" in tax:
        if "customers" not in tax:
            tax["customers"] = tax["clients"]
        del tax["clients"]
        changed = True
    if "accounts" in tax:
        if "environments" not in tax:
            tax["environments"] = tax["accounts"]
        del tax["accounts"]
        changed = True
    tax.setdefault("customers", default["customers"])
    tax.setdefault("environments", default["environments"])
    if changed:
        save_taxonomy(tax)
    return tax


def save_taxonomy(tax):
    _write_json(TAXONOMY_FILE, tax)


def sync_taxonomy_from_nodes():
    """Keep taxonomy in sync with registered servers and Uptime Kuma sites."""
    tax = load_taxonomy()
    customers = set(tax.get("customers", []))
    environments = dict(tax.get("environments", {}))
    for n in load_nodes():
        c, a, _ = normalize_metadata(_read_customer(n), _read_environment(n), "")
        customers.add(c)
        environments.setdefault(c, [])
        if a not in environments[c]:
            environments[c] = sorted(set(environments[c]) | {a})
    for s in load_kuma_site_meta().values():
        c, a, _ = normalize_metadata(_read_customer(s), _read_environment(s), "")
        customers.add(c)
        environments.setdefault(c, [])
        if a not in environments[c]:
            environments[c] = sorted(set(environments[c]) | {a})
    tax["customers"] = sorted(customers)
    tax["environments"] = {k: sorted(set(v)) for k, v in environments.items()}
    save_taxonomy(tax)


def record_taxonomy(customer="", environment="", **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    c, a, _ = normalize_metadata(customer, environment, "")
    tax = load_taxonomy()
    customers = set(tax.get("customers", []))
    customers.add(c)
    tax["customers"] = sorted(customers)
    environments = tax.setdefault("environments", {})
    envs = set(environments.get(c, []))
    envs.add(a)
    environments[c] = sorted(envs)
    save_taxonomy(tax)


def list_all_customers():
    sync_taxonomy_from_nodes()
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    tax = load_taxonomy()
    site_customers = {normalize_metadata(_read_customer(s), "", "")[0] for s in sites.values()}
    return sorted({node_customer(n) for n in nodes} | site_customers | set(tax.get("customers", [])))


def list_environments_for_customer(customer="", **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    sync_taxonomy_from_nodes()
    customer = (customer or "").strip()
    if customer in ("", ".*", "$__all", "All"):
        nodes = load_nodes()
        sites = load_kuma_site_meta()
        tax = load_taxonomy()
        from_nodes = {node_environment(n) for n in nodes}
        from_sites = {normalize_metadata("", _read_environment(s), "")[1] for s in sites.values()}
        from_tax = {a for accs in tax.get("environments", {}).values() for a in accs}
        return sorted(from_nodes | from_sites | from_tax | {DEFAULT_ENVIRONMENT})
    customer = customer or DEFAULT_CUSTOMER
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    from_nodes = {node_environment(n) for n in nodes if node_customer(n) == customer}
    from_sites = {
        normalize_metadata("", _read_environment(s), "")[1]
        for s in sites.values()
        if normalize_metadata(_read_customer(s), "", "")[0] == customer
    }
    tax = load_taxonomy()
    from_tax = set(tax.get("environments", {}).get(customer, []))
    return sorted(from_nodes | from_sites | from_tax | {DEFAULT_ENVIRONMENT})


def taxonomy_overview():
    sync_taxonomy_from_nodes()
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    customers = []
    for c in list_all_customers():
        accs = []
        for a in list_environments_for_customer(c):
            accs.append({
                "name": a,
                "server_count": sum(1 for n in nodes if node_customer(n) == c and node_environment(n) == a),
                "site_count": sum(
                    1 for s in sites.values()
                    if normalize_metadata(_read_customer(s), "", "")[0] == c
                    and normalize_metadata("", _read_environment(s), "")[1] == a
                ),
            })
        customers.append({
            "name": c,
            "server_count": sum(1 for n in nodes if node_customer(n) == c),
            "site_count": sum(
                1 for s in sites.values()
                if normalize_metadata(_read_customer(s), "", "")[0] == c
            ),
            "environments": accs,
        })
    return {"customers": customers, "total_servers": len(nodes), "total_sites": len(sites)}


# ---- command center ---------------------------------------------------------

def _metric_value_map(expr, label="host"):
    out = {}
    try:
        rows = _prom_query(expr)
    except Exception:
        rows = []
    for row in rows:
        metric = row.get("metric", {}) or {}
        key = metric.get(label)
        if not key:
            continue
        try:
            out[key] = float((row.get("value") or [None, None])[1])
        except Exception:
            pass
    return out


def _port_summary_map():
    def norm_port_label(value):
        value = str(value or "")
        prefix = "integrations/blackbox/"
        if value.startswith(prefix):
            value = value[len(prefix):]
        return sanitize_port_name(value)

    allowed = {}
    for node in load_nodes():
        host = node.get("hostname")
        if not host:
            continue
        names = {
            sanitize_port_name(p.get("name", ""))
            for p in load_ports(host)
            if p.get("name")
        }
        if names:
            allowed[host] = names
    out = {}
    try:
        rows = _prom_query('probe_success{job="blackbox"}')
    except Exception:
        rows = []
    for row in rows:
        metric = row.get("metric", {}) or {}
        host = metric.get("host")
        port = norm_port_label(metric.get("port", ""))
        if not host:
            continue
        if port not in allowed.get(host, set()):
            continue
        item = out.setdefault(host, {"up": 0, "down": 0, "total": 0})
        item["total"] += 1
        try:
            value = float((row.get("value") or [None, None])[1])
        except Exception:
            value = None
        if value == 1:
            item["up"] += 1
        elif value == 0:
            item["down"] += 1
    return out


DISK_FS_FILTER = (
    'job="integrations/unix",'
    'fstype!~"tmpfs|devtmpfs|overlay|squashfs|aufs|nsfs|proc|sysfs|devpts|cgroup2?|pstore|securityfs|tracefs|debugfs|fusectl|mqueue|hugetlbfs|configfs|ramfs",'
    'mountpoint!~"/run($|/.*)|/var/lib/docker($|/.*)|/var/lib/containerd($|/.*)|/snap($|/.*)"'
)

NETWORK_IFACE_FILTER = 'device!~"lo|docker.*|veth.*|br-.*|flannel.*|cali.*|tun.*|tap.*"'
PROCESS_DOWN_DEFAULT_REGEX = (
    "alloy|grafana|prometheus|sshd|nginx|gunicorn|celery|redis-server|mongod|"
    "docker|dockerd|containerd|caddy|postgres|postgresql|mysql|mysqld"
)


def _disk_capacity_map():
    sizes = _metric_value_map(f"sum by (host) (node_filesystem_size_bytes{{{DISK_FS_FILTER}}})")
    used = _metric_value_map(
        f"sum by (host) (node_filesystem_size_bytes{{{DISK_FS_FILTER}}} - node_filesystem_avail_bytes{{{DISK_FS_FILTER}}})"
    )
    out = {}
    for host, total_bytes in sizes.items():
        if not total_bytes or total_bytes <= 0:
            continue
        used_bytes = used.get(host, 0.0)
        out[host] = {
            "used_bytes": used_bytes,
            "total_bytes": total_bytes,
            "used_gb": used_bytes / (1024 ** 3),
            "total_gb": total_bytes / (1024 ** 3),
        }
    return out


def _process_down_map(process_regex=PROCESS_DOWN_DEFAULT_REGEX):
    expr = (
        'count by (host) ('
        f'(count_over_time(namedprocess_namegroup_num_procs{{job="integrations/unix",groupname=~"{process_regex}"}}[1h]) > 0) '
        f'unless on(host,groupname) (namedprocess_namegroup_num_procs{{job="integrations/unix",groupname=~"{process_regex}"}} > 0)'
        ')'
    )
    return _metric_value_map(expr)


def _severity_rank(severity):
    return {"critical": 3, "warning": 2, "unknown": 1, "ok": 0}.get(severity, 0)


def _worse_status(a, b):
    return a if _severity_rank(a) >= _severity_rank(b) else b


def command_center(customer="", environment="", **legacy):
    """Unified operational summary for the modern inventory dashboard."""
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    settings = load_alert_settings()
    rules = settings.get("rules", {})
    cpu_rule = rules.get("high_cpu", {})
    mem_rule = rules.get("high_memory", {})
    disk_rule = rules.get("low_disk", {})
    network_rule = rules.get("network_errors", {})
    process_rule = rules.get("process_down", {})
    cpu_warn = float(cpu_rule.get("warning_threshold", 70))
    cpu_crit = float(cpu_rule.get("critical_threshold", 90))
    mem_warn = float(mem_rule.get("warning_threshold", 70))
    mem_crit = float(mem_rule.get("critical_threshold", 90))
    disk_warn = float(disk_rule.get("warning_threshold", 70))
    disk_crit = float(disk_rule.get("critical_threshold", 90))
    network_warn = float(network_rule.get("warning_threshold", 1))
    network_crit = float(network_rule.get("critical_threshold", 10))
    process_regex = str(process_rule.get("process_regex") or PROCESS_DOWN_DEFAULT_REGEX)

    nodes = filter_nodes(sync_prom_hosts(), customer, environment)
    sites = list_kuma_sites(customer, environment)
    up = _metric_value_map('max by (host) (up{job="integrations/unix"})')
    cpu = _metric_value_map(
        '100 - (avg by (host) (rate(node_cpu_seconds_total{job="integrations/unix",mode="idle"}[5m])) * 100)'
    )
    mem = _metric_value_map(
        '(1 - (max by (host) (node_memory_MemAvailable_bytes{job="integrations/unix"}) '
        '/ max by (host) (node_memory_MemTotal_bytes{job="integrations/unix"}))) * 100'
    )
    disk = _metric_value_map(
        f'max by (host) (100 - ((node_filesystem_avail_bytes{{{DISK_FS_FILTER}}} / node_filesystem_size_bytes{{{DISK_FS_FILTER}}}) * 100))'
    )
    disk_capacity = _disk_capacity_map()
    network_errors = _metric_value_map(
        f'max by (host) (rate(node_network_receive_errs_total{{job="integrations/unix",{NETWORK_IFACE_FILTER}}}[5m]) '
        f'+ rate(node_network_transmit_errs_total{{job="integrations/unix",{NETWORK_IFACE_FILTER}}}[5m]) '
        f'+ rate(node_network_receive_drop_total{{job="integrations/unix",{NETWORK_IFACE_FILTER}}}[5m]) '
        f'+ rate(node_network_transmit_drop_total{{job="integrations/unix",{NETWORK_IFACE_FILTER}}}[5m]))'
    )
    process_down = _process_down_map(process_regex)
    ports = _port_summary_map()

    totals = {
        "servers": len(nodes),
        "sites": len(sites),
        "assets": len(nodes) + len(sites),
        "up": 0,
        "down": 0,
        "ok": 0,
        "warning": 0,
        "critical": 0,
        "unknown": 0,
        "ports_down": 0,
        "processes_down": 0,
        "response_time_sum": 0.0,
        "response_time_count": 0,
    }
    customers = {}
    assets = []

    def add_bucket(asset_customer, asset_environment, asset_type, severity):
        c = asset_customer or DEFAULT_CUSTOMER
        a = asset_environment or DEFAULT_ENVIRONMENT
        citem = customers.setdefault(c, {
            "name": c,
            "servers": 0,
            "sites": 0,
            "assets": 0,
            "up": 0,
            "down": 0,
            "ok": 0,
            "warning": 0,
            "critical": 0,
            "unknown": 0,
            "status": "ok",
            "environments": {},
        })
        aitem = citem["environments"].setdefault(a, {
            "name": a,
            "servers": 0,
            "sites": 0,
            "assets": 0,
            "up": 0,
            "down": 0,
            "ok": 0,
            "warning": 0,
            "critical": 0,
            "unknown": 0,
            "status": "ok",
        })
        for item in (citem, aitem):
            item["assets"] += 1
            item[asset_type + "s"] += 1
            item[severity] += 1
            if severity in ("ok", "warning"):
                item["up"] += 1
            elif severity == "critical":
                item["down"] += 1
            item["status"] = _worse_status(item["status"], severity)

    for n in nodes:
        host = n.get("hostname")
        p = ports.get(host, {"up": 0, "down": 0, "total": 0})
        status = up.get(host)
        current_cpu = cpu.get(host)
        current_mem = mem.get(host)
        current_disk = disk.get(host)
        current_disk_capacity = disk_capacity.get(host, {})
        current_network_errors = network_errors.get(host)
        current_process_down = int(process_down.get(host, 0) or 0)
        severity = "ok"
        reason = "healthy"
        if status is None:
            severity, reason = "unknown", "no node data"
        elif status == 0:
            severity, reason = "critical", "node down"
        elif p.get("down", 0) > 0:
            severity, reason = "critical", f"{p.get('down')} port(s) down"
        elif current_process_down > 0:
            severity, reason = "critical", f"{current_process_down} process(es) down"
        elif (
            (current_cpu is not None and current_cpu >= cpu_crit)
            or (current_mem is not None and current_mem >= mem_crit)
            or (current_disk is not None and current_disk >= disk_crit)
            or (current_network_errors is not None and current_network_errors >= network_crit)
        ):
            severity, reason = "critical", "resource critical"
        elif (
            (current_cpu is not None and current_cpu >= cpu_warn)
            or (current_mem is not None and current_mem >= mem_warn)
            or (current_disk is not None and current_disk >= disk_warn)
            or (current_network_errors is not None and current_network_errors >= network_warn)
        ):
            severity, reason = "warning", "resource warning"
        totals[severity] += 1
        if severity in ("ok", "warning"):
            totals["up"] += 1
        elif severity == "critical":
            totals["down"] += 1
        totals["ports_down"] += p.get("down", 0)
        totals["processes_down"] += current_process_down
        add_bucket(node_customer(n), node_environment(n), "server", severity)
        assets.append({
            "type": "server",
            "id": host,
            "name": n.get("name") or host,
            "customer": node_customer(n),
            "environment": node_environment(n),
            "target": n.get("ip") or host,
            "status": "up" if status == 1 else ("down" if status == 0 else "unknown"),
            "severity": severity,
            "reason": reason,
            "cpu": current_cpu,
            "memory": current_mem,
            "disk": current_disk,
            "disk_used_gb": current_disk_capacity.get("used_gb"),
            "disk_total_gb": current_disk_capacity.get("total_gb"),
            "disk_used_bytes": current_disk_capacity.get("used_bytes"),
            "disk_total_bytes": current_disk_capacity.get("total_bytes"),
            "network_error_rate": current_network_errors,
            "processes_down": current_process_down,
            "ports_up": p.get("up", 0),
            "ports_down": p.get("down", 0),
            "ports_total": p.get("total", 0),
            "dashboard_url": f"/d/alloy-drilldown/server-drill-down-alloy?var-host={host}",
            "edit_tab": "servers",
        })

    for s in sites:
        value = s.get("status")
        severity = "unknown"
        reason = "no Kuma data"
        if value == 1:
            severity, reason = "ok", "healthy"
            totals["up"] += 1
        elif value == 0:
            severity, reason = "critical", "site down"
            totals["down"] += 1
        else:
            totals["unknown"] += 1
        if severity in ("ok", "critical"):
            totals[severity] += 1
        rt = s.get("response_time")
        if rt is not None:
            totals["response_time_sum"] += float(rt)
            totals["response_time_count"] += 1
        add_bucket(_read_customer(s), _read_environment(s), "site", severity)
        monitor_name = s.get("monitor_name")
        assets.append({
            "type": "site",
            "id": monitor_name,
            "name": s.get("name") or monitor_name,
            "customer": _read_customer(s) or DEFAULT_CUSTOMER,
            "environment": _read_environment(s) or DEFAULT_ENVIRONMENT,
            "target": s.get("target") or s.get("monitor_url") or "",
            "status": "up" if value == 1 else ("down" if value == 0 else "unknown"),
            "severity": severity,
            "reason": reason,
            "response_time": rt,
            "monitor_type": s.get("monitor_type", ""),
            "dashboard_url": f"/d/uptime-kuma-site/uptime-kuma-site?var-monitor={monitor_name}",
            "edit_tab": "kuma",
        })

    response_avg = None
    if totals["response_time_count"]:
        response_avg = totals["response_time_sum"] / totals["response_time_count"]
    totals.pop("response_time_sum", None)
    totals.pop("response_time_count", None)
    totals["avg_response_time"] = response_avg
    known_assets = totals["assets"] - totals["unknown"]
    totals["availability_percent"] = (
        round((totals["up"] / known_assets) * 100, 2) if known_assets > 0 else None
    )

    customers_out = []
    for c in customers.values():
        c["environments"] = sorted(c["environments"].values(), key=lambda x: x["name"].lower())
        customers_out.append(c)
    customers_out.sort(key=lambda x: (_severity_rank(x["status"]) * -1, x["name"].lower()))
    assets.sort(key=lambda x: (_severity_rank(x["severity"]) * -1, x["type"], x["name"].lower()))

    return {
        "totals": totals,
        "customers": customers_out,
        "assets": assets,
        "alert_settings": settings,
    }


def _migrate_servers(customer_from, customer_to, environment_from=None, environment_to=None):
    customer_to = (customer_to or "").strip() or DEFAULT_CUSTOMER
    environment_to = (environment_to or "").strip() or DEFAULT_ENVIRONMENT
    nodes = load_nodes()
    moved = 0
    for n in nodes:
        if node_customer(n) != customer_from:
            continue
        if environment_from is not None and node_environment(n) != environment_from:
            continue
        n["customer"] = customer_to
        n["environment"] = environment_to
        n.pop("client", None)
        n.pop("account", None)
        moved += 1
    if moved:
        save_nodes(nodes)
        record_taxonomy(customer_to, environment_to)
    return moved


def _migrate_kuma_sites(customer_from, customer_to, environment_from=None, environment_to=None):
    customer_to = (customer_to or "").strip() or DEFAULT_CUSTOMER
    environment_to = (environment_to or "").strip() or DEFAULT_ENVIRONMENT
    sites = load_kuma_site_meta()
    moved = 0
    for s in sites.values():
        c, a, _ = normalize_metadata(_read_customer(s), _read_environment(s), "")
        if c != customer_from:
            continue
        if environment_from is not None and a != environment_from:
            continue
        s["customer"] = customer_to
        s["environment"] = environment_to
        s.pop("client", None)
        s.pop("account", None)
        moved += 1
    if moved:
        save_kuma_site_meta(sites)
        record_taxonomy(customer_to, environment_to)
    return moved


def add_taxonomy_customer(name):
    raw = (name or "").strip()
    if not raw:
        return None, "customer name is required"
    c, _, _ = normalize_metadata(raw, "", "")
    if raw.lower() == "unassigned":
        c = DEFAULT_CUSTOMER
    tax = load_taxonomy()
    customers = set(tax.get("customers", []))
    customers.add(c)
    tax["customers"] = sorted(customers)
    tax.setdefault("environments", {}).setdefault(c, [])
    if DEFAULT_ENVIRONMENT not in tax["environments"][c]:
        tax["environments"][c] = sorted(set(tax["environments"][c]) | {DEFAULT_ENVIRONMENT})
    save_taxonomy(tax)
    return c, None


def rename_taxonomy_customer(old, new):
    old = (old or "").strip()
    new = (new or "").strip()
    if not new:
        return "new name required"
    if old == new:
        return None
    c_new, _, _ = normalize_metadata(new, "", "")
    _migrate_servers(old, c_new)
    _migrate_kuma_sites(old, c_new)
    tax = load_taxonomy()
    if old in tax.get("environments", {}):
        tax["environments"][c_new] = sorted(set(tax["environments"].get(c_new, [])) | set(tax["environments"].pop(old)))
    customers = {c_new if x == old else x for x in tax.get("customers", [])}
    tax["customers"] = sorted(customers | {c_new})
    save_taxonomy(tax)
    return None


def delete_taxonomy_customer(name, merge_into=None):
    name = (name or "").strip()
    if name in (DEFAULT_CUSTOMER,):
        return "cannot delete default customer"
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    count = sum(1 for n in nodes if node_customer(n) == name)
    site_count = sum(1 for s in sites.values() if normalize_metadata(_read_customer(s), "", "")[0] == name)
    if (count or site_count) and not merge_into:
        return f"{count} server(s) and {site_count} site(s) still use this customer \u2014 pick \u201cmerge into\u201d or reassign them first"
    if merge_into:
        _migrate_servers(name, merge_into.strip())
        _migrate_kuma_sites(name, merge_into.strip())
    tax = load_taxonomy()
    tax.get("environments", {}).pop(name, None)
    tax["customers"] = [c for c in tax.get("customers", []) if c != name]
    save_taxonomy(tax)
    return None


def add_taxonomy_environment(customer, environment, **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    c, a, _ = normalize_metadata(customer, environment, "")
    tax = load_taxonomy()
    tax.setdefault("customers", [])
    if c not in tax["customers"]:
        tax["customers"] = sorted(set(tax["customers"]) | {c})
    envs = set(tax.setdefault("environments", {}).get(c, []))
    envs.add(a)
    tax["environments"][c] = sorted(envs)
    save_taxonomy(tax)
    return a, None


def rename_taxonomy_environment(customer, old_env, new_env, **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    customer = (customer or "").strip() or DEFAULT_CUSTOMER
    old_env = (old_env or "").strip()
    new_env = (new_env or "").strip()
    if not new_env:
        return "new name required"
    _, a_new, _ = normalize_metadata(customer, new_env, "")
    nodes = load_nodes()
    for n in nodes:
        if node_customer(n) == customer and node_environment(n) == old_env:
            n["environment"] = a_new
    save_nodes(nodes)
    sites = load_kuma_site_meta()
    for s in sites.values():
        c, a, _ = normalize_metadata(_read_customer(s), _read_environment(s), "")
        if c == customer and a == old_env:
            s["environment"] = a_new
    save_kuma_site_meta(sites)
    tax = load_taxonomy()
    envs = set(tax.setdefault("environments", {}).get(customer, []))
    envs.discard(old_env)
    envs.add(a_new)
    tax["environments"][customer] = sorted(envs)
    save_taxonomy(tax)
    record_taxonomy(customer, a_new)
    return None


def delete_taxonomy_environment(customer, environment, merge_into=None, **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    customer = (customer or "").strip() or DEFAULT_CUSTOMER
    environment = (environment or "").strip()
    if environment in (DEFAULT_ENVIRONMENT,) and customer == DEFAULT_CUSTOMER:
        return "cannot delete default environment under Unassigned"
    nodes = load_nodes()
    count = sum(1 for n in nodes if node_customer(n) == customer and node_environment(n) == environment)
    sites = load_kuma_site_meta()
    site_count = sum(
        1 for s in sites.values()
        if normalize_metadata(_read_customer(s), "", "")[0] == customer
        and normalize_metadata("", _read_environment(s), "")[1] == environment
    )
    if (count or site_count) and not merge_into:
        return f"{count} server(s) and {site_count} site(s) use this environment \u2014 merge into another environment first"
    if merge_into:
        _migrate_servers(customer, customer, environment, merge_into.strip())
        _migrate_kuma_sites(customer, customer, environment, merge_into.strip())
    tax = load_taxonomy()
    envs = [a for a in tax.get("environments", {}).get(customer, []) if a != environment]
    tax.setdefault("environments", {})[customer] = envs
    save_taxonomy(tax)
    return None


def filter_nodes(nodes, customer="", environment="", **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    if not environment and legacy.get("account"):
        environment = legacy["account"]
    if customer and customer not in ("", ".*", "$__all", "All"):
        if customer == DEFAULT_CUSTOMER:
            nodes = [n for n in nodes if node_customer(n) == DEFAULT_CUSTOMER]
        else:
            nodes = [n for n in nodes if node_customer(n) == customer]
    if environment and environment not in ("", ".*", "$__all", "All"):
        if environment == DEFAULT_ENVIRONMENT:
            nodes = [n for n in nodes if node_environment(n) == DEFAULT_ENVIRONMENT]
        else:
            nodes = [n for n in nodes if node_environment(n) == environment]
    return nodes


def migrate_nodes():
    """Backfill empty customer/environment and migrate legacy client/account keys."""
    nodes = load_nodes()
    changed = False
    for n in nodes:
        if _migrate_legacy_fields(n):
            changed = True
        c, a, _ = normalize_metadata(_read_customer(n), _read_environment(n))
        if n.get("customer", "") != c:
            n["customer"] = c
            changed = True
        if n.get("environment", "") != a:
            n["environment"] = a
            changed = True
        n.pop("client", None)
        n.pop("account", None)
        if not n.get("name"):
            n["name"] = n["hostname"]
            changed = True
    if changed:
        save_nodes(nodes)

    meta = load_kuma_site_meta()
    kuma_changed = False
    for s in meta.values():
        if _migrate_legacy_fields(s):
            kuma_changed = True
        c, a, _ = normalize_metadata(_read_customer(s), _read_environment(s))
        if s.get("customer", "") != c:
            s["customer"] = c
            kuma_changed = True
        if s.get("environment", "") != a:
            s["environment"] = a
            kuma_changed = True
        s.pop("client", None)
        s.pop("account", None)
    if kuma_changed:
        save_kuma_site_meta(meta)


# ---- alert recipients (per-customer email) --------------------------------

def parse_emails(value):
    """Accept a list or a comma/semicolon/space/newline-separated string and
    return a clean, de-duplicated list of addresses (order preserved)."""
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[,;\s]+", value or "")
    out = []
    for p in parts:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p)
    return out


def _coerce_recipients(value):
    """Accept many shapes and return a list of {'email','enabled'} dicts:
    - "a@x.com, b@y.com"            -> both enabled
    - ["a@x.com", "b@y.com"]        -> both enabled
    - [{"email","enabled"}, ...]    -> as given
    """
    if isinstance(value, str):
        return [{"email": e, "enabled": True} for e in parse_emails(value)]
    if isinstance(value, list):
        out = []
        for r in value:
            if isinstance(r, str):
                out.append({"email": r.strip(), "enabled": True})
            elif isinstance(r, dict):
                out.append({"email": (r.get("email") or "").strip(),
                            "enabled": bool(r.get("enabled", True))})
        return out
    return []


def _normalize_recipient(info):
    """Normalize a stored record to {'recipients': [{'email','enabled'}]}.
    Backward compatible with the old {'email': str} and {'emails': [...], 'enabled': bool}."""
    if not isinstance(info, dict):
        return {"recipients": []}
    recips = info.get("recipients")
    if recips is None:
        emails = info.get("emails")
        if emails is None and info.get("email"):
            emails = parse_emails(info.get("email"))
        emails = parse_emails(emails or [])
        en = bool(info.get("enabled", True))
        recips = [{"email": e, "enabled": en} for e in emails]
    out, seen = [], set()
    for r in _coerce_recipients(recips):
        e = r["email"]
        if e and e not in seen:
            seen.add(e)
            out.append({"email": e, "enabled": bool(r["enabled"])})
    return {"recipients": out}


def load_alert_recipients():
    """{customer: {'recipients': [{'email','enabled'}]}}"""
    raw = _read_json(ALERT_RECIPIENTS_FILE, {})
    return {c: _normalize_recipient(v) for c, v in raw.items()}


def save_alert_recipients(data):
    _write_json(ALERT_RECIPIENTS_FILE, data)


def get_alert_recipient(customer, **legacy):
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    return load_alert_recipients().get(customer, {"recipients": []})


def enabled_emails(info):
    """Return the list of enabled addresses from a normalized record."""
    return [r["email"] for r in (info or {}).get("recipients", []) if r.get("enabled")]


def set_alert_recipient(customer, recipients, **legacy):
    """recipients: list of {email,enabled} | list of str | comma string."""
    if not customer and legacy.get("client"):
        customer = legacy["client"]
    customer = (customer or "").strip()
    data = load_alert_recipients()
    norm = _normalize_recipient({"recipients": _coerce_recipients(recipients)})
    if not norm["recipients"]:
        data.pop(customer, None)
    else:
        data[customer] = norm
    save_alert_recipients(data)
    return data.get(customer, {"recipients": []})


def customer_host_map():
    """Authoritative customer -> [hostnames] mapping from the node registry."""
    out = {}
    for n in load_nodes():
        c = node_customer(n)
        out.setdefault(c, []).append(n["hostname"])
    return out


# ---- built-in alert settings ------------------------------------------------

_ALERT_RULE_DEFAULTS = {
    "node_down": {
        "enabled": True,
        "duration_minutes": 2,
        "severity": "critical",
    },
    "port_down": {
        "enabled": True,
        "duration_minutes": 2,
        "severity": "critical",
    },
    "process_down": {
        "enabled": True,
        "duration_minutes": 2,
        "severity": "critical",
        "process_regex": PROCESS_DOWN_DEFAULT_REGEX,
    },
    "uptime_kuma_source_down": {
        "enabled": False,
        "duration_minutes": 2,
        "severity": "critical",
    },
    "uptime_kuma_monitor_down": {
        "enabled": True,
        "duration_minutes": 2,
        "severity": "critical",
    },
    "high_cpu": {
        "enabled": True,
        "warning_threshold": 70,
        "critical_threshold": 90,
        "duration_minutes": 5,
    },
    "high_memory": {
        "enabled": True,
        "warning_threshold": 70,
        "critical_threshold": 90,
        "duration_minutes": 5,
    },
    "low_disk": {
        "enabled": True,
        "warning_threshold": 70,
        "critical_threshold": 90,
        "duration_minutes": 5,
    },
    "network_errors": {
        "enabled": True,
        "warning_threshold": 1,
        "critical_threshold": 10,
        "duration_minutes": 5,
    },
}


def _alert_settings_defaults():
    return json.loads(json.dumps({"rules": _ALERT_RULE_DEFAULTS}))


def _minutes_to_duration(minutes, default_minutes=5):
    try:
        value = int(minutes)
    except Exception:
        value = default_minutes
    value = max(1, min(value, 1440))
    return f"{value}m"


def _normalize_alert_rule(rule_id, value):
    base = dict(_ALERT_RULE_DEFAULTS[rule_id])
    if isinstance(value, dict):
        base.update(value)
    base["enabled"] = bool(base.get("enabled", True))
    base["duration_minutes"] = max(1, min(int(base.get("duration_minutes", 5) or 5), 1440))
    if "severity" in _ALERT_RULE_DEFAULTS[rule_id]:
        sev = str(base.get("severity", "warning") or "warning").strip().lower()
        if sev not in ("critical", "warning", "info"):
            sev = _ALERT_RULE_DEFAULTS[rule_id].get("severity", "warning")
        base["severity"] = sev
    if "warning_threshold" in _ALERT_RULE_DEFAULTS[rule_id]:
        raw_warning = base.get("warning_threshold")
        raw_critical = base.get("critical_threshold")
        legacy_threshold = base.get("threshold")
        try:
            warning = float(
                _ALERT_RULE_DEFAULTS[rule_id]["warning_threshold"]
                if raw_warning is None else raw_warning
            )
        except Exception:
            warning = float(_ALERT_RULE_DEFAULTS[rule_id]["warning_threshold"])
        try:
            critical = float(
                legacy_threshold if raw_critical is None and legacy_threshold is not None
                else (_ALERT_RULE_DEFAULTS[rule_id]["critical_threshold"] if raw_critical is None else raw_critical)
            )
        except Exception:
            critical = float(_ALERT_RULE_DEFAULTS[rule_id]["critical_threshold"])
        warning = max(0.0, min(warning, 100.0))
        critical = max(0.0, min(critical, 100.0))
        if warning >= critical:
            warning = max(0.0, critical - 1.0)
        base["warning_threshold"] = warning
        base["critical_threshold"] = critical
        base.pop("threshold", None)
        base.pop("severity", None)
    return base


def load_alert_settings():
    raw = _read_json(ALERT_SETTINGS_FILE, {})
    out = _alert_settings_defaults()
    rules = raw.get("rules", {}) if isinstance(raw, dict) else {}
    for rule_id in _ALERT_RULE_DEFAULTS:
        out["rules"][rule_id] = _normalize_alert_rule(rule_id, rules.get(rule_id, {}))
    return out


def save_alert_settings(data):
    _write_json(ALERT_SETTINGS_FILE, data)


def update_alert_settings(data):
    current = load_alert_settings()
    rules = data.get("rules", {}) if isinstance(data, dict) else {}
    for rule_id in _ALERT_RULE_DEFAULTS:
        if rule_id in rules:
            current["rules"][rule_id] = _normalize_alert_rule(rule_id, rules.get(rule_id, {}))
    save_alert_settings(current)
    return current


# ---- alert groups (named set of servers + recipients) -----------------------

def _normalize_group(g):
    if not isinstance(g, dict):
        return None
    gid = (g.get("id") or "").strip()
    name = (g.get("name") or "").strip()
    hosts = [h for h in (g.get("hosts") or []) if isinstance(h, str) and h.strip()]
    hosts = sorted(dict.fromkeys(h.strip() for h in hosts))
    sites = [s for s in (g.get("sites") or []) if isinstance(s, str) and s.strip()]
    sites = sorted(dict.fromkeys(s.strip() for s in sites))
    recips = _normalize_recipient({"recipients": _coerce_recipients(g.get("recipients", []))})["recipients"]
    return {
        "id": gid,
        "name": name,
        "hosts": hosts,
        "sites": sites,
        "recipients": recips,
        "enabled": bool(g.get("enabled", True)),
    }


def load_alert_groups():
    raw = _read_json(ALERT_GROUPS_FILE, {"groups": []})
    groups = []
    for g in raw.get("groups", []):
        ng = _normalize_group(g)
        if ng and ng["id"]:
            groups.append(ng)
    return groups


def save_alert_groups(groups):
    _write_json(ALERT_GROUPS_FILE, {"groups": groups})


def get_alert_group(group_id):
    return next((g for g in load_alert_groups() if g["id"] == group_id), None)


def _new_group_id():
    import uuid
    return "grp-" + uuid.uuid4().hex[:8]


def upsert_alert_group(group):
    """Create (no id) or update (with id) a group. Returns the saved group."""
    groups = load_alert_groups()
    ng = _normalize_group(group) or {}
    if not ng.get("name"):
        return None, "group name is required"
    if not ng.get("id"):
        ng["id"] = _new_group_id()
        groups.append(ng)
    else:
        found = False
        for i, g in enumerate(groups):
            if g["id"] == ng["id"]:
                groups[i] = ng
                found = True
                break
        if not found:
            groups.append(ng)
    save_alert_groups(groups)
    return ng, None


def delete_alert_group(group_id):
    groups = load_alert_groups()
    remaining = [g for g in groups if g["id"] != group_id]
    if len(remaining) == len(groups):
        return False
    save_alert_groups(remaining)
    return True


def host_group_ids(host):
    """Group ids that currently include this host."""
    return [g["id"] for g in load_alert_groups() if host in g.get("hosts", [])]


def set_host_groups(host, group_ids):
    """Set the exact set of groups this host belongs to (add/remove accordingly)."""
    want = set(group_ids or [])
    groups = load_alert_groups()
    changed = False
    for g in groups:
        in_group = host in g.get("hosts", [])
        should = g["id"] in want
        if should and not in_group:
            g["hosts"] = sorted(set(g.get("hosts", [])) | {host})
            changed = True
        elif not should and in_group:
            g["hosts"] = [h for h in g.get("hosts", []) if h != host]
            changed = True
    if changed:
        save_alert_groups(groups)
    return [g["id"] for g in groups if host in g.get("hosts", [])]


def kuma_site_group_ids(monitor_name):
    """Group ids that currently include this Uptime Kuma monitor."""
    return [g["id"] for g in load_alert_groups() if monitor_name in g.get("sites", [])]


def set_kuma_site_groups(monitor_name, group_ids):
    """Set the exact set of groups this Uptime Kuma monitor belongs to."""
    want = set(group_ids or [])
    groups = load_alert_groups()
    changed = False
    for g in groups:
        in_group = monitor_name in g.get("sites", [])
        should = g["id"] in want
        if should and not in_group:
            g["sites"] = sorted(set(g.get("sites", [])) | {monitor_name})
            changed = True
        elif not should and in_group:
            g["sites"] = [s for s in g.get("sites", []) if s != monitor_name]
            changed = True
    if changed:
        save_alert_groups(groups)
    return [g["id"] for g in groups if monitor_name in g.get("sites", [])]


# ---- backward compatibility aliases -----------------------------------------

node_client = node_customer
node_account = node_environment
client_host_map = customer_host_map
client_kuma_site_map = customer_kuma_site_map
list_all_clients = list_all_customers
list_accounts_for_client = list_environments_for_customer
add_taxonomy_client = add_taxonomy_customer
rename_taxonomy_client = rename_taxonomy_customer
delete_taxonomy_client = delete_taxonomy_customer
add_taxonomy_account = add_taxonomy_environment
rename_taxonomy_account = rename_taxonomy_environment
delete_taxonomy_account = delete_taxonomy_environment
