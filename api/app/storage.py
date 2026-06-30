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
    DEFAULT_ACCOUNT,
    DEFAULT_CLIENT,
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


def normalize_metadata(client="", account="", name=""):
    """Apply defaults so new installs always appear in Grafana dropdowns."""
    c = (client or "").strip() or DEFAULT_CLIENT
    a = (account or "").strip() or DEFAULT_ACCOUNT
    return c, a, (name or "").strip()


def node_client(n):
    return (n.get("client") or "").strip() or DEFAULT_CLIENT


def node_account(n):
    return (n.get("account") or "").strip() or DEFAULT_ACCOUNT


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
    c, a, nm = normalize_metadata(d.get("client", ""), d.get("account", ""), d.get("name", ""))
    entry = {
        "hostname": host,
        "ip": d.get("ip", ""),
        "name": nm or host,
        "client": c,
        "account": a,
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
                "client": c,
                "account": a,
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
            record_taxonomy(item.get("client", ""), item.get("account", ""))
    return meta, live


def list_kuma_sites(client="", account=""):
    meta, live = sync_kuma_sites()
    sites = []
    for monitor_name in sorted(set(meta) | set(live), key=str.lower):
        m = meta.get(monitor_name, {})
        l = live.get(monitor_name, {})
        c, a, nm = normalize_metadata(m.get("client", ""), m.get("account", ""), m.get("name", ""))
        site = {
            "monitor_name": monitor_name,
            "name": nm or monitor_name,
            "client": c,
            "account": a,
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
    return filter_kuma_sites(sites, client, account)


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
        data.get("client", existing.get("client", "")),
        data.get("account", existing.get("account", "")),
        data.get("name", existing.get("name", monitor_name)),
    )
    existing["name"] = nm or monitor_name
    existing["client"] = c
    existing["account"] = a
    existing["last_seen"] = TIMESTAMP()
    save_kuma_site_meta(meta)
    record_taxonomy(c, a)
    return find_kuma_site(monitor_name), None


def filter_kuma_sites(sites, client="", account=""):
    if client and client not in ("", ".*", "$__all", "All"):
        sites = [s for s in sites if s.get("client") == client]
    if account and account not in ("", ".*", "$__all", "All"):
        sites = [s for s in sites if s.get("account") == account]
    return sites


def client_kuma_site_map():
    out = {}
    for s in list_kuma_sites():
        c = (s.get("client") or "").strip() or DEFAULT_CLIENT
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
            "client": c,
            "account": a,
            "registered": TIMESTAMP(),
            "last_seen": TIMESTAMP(),
        })
        known.add(h)
        changed = True
    if changed:
        save_nodes(nodes)
    return nodes


# ---- taxonomy (clients & accounts) ------------------------------------------

def load_taxonomy():
    default = {"clients": [DEFAULT_CLIENT], "accounts": {DEFAULT_CLIENT: [DEFAULT_ACCOUNT]}}
    tax = _read_json(TAXONOMY_FILE, default)
    tax.setdefault("clients", default["clients"])
    tax.setdefault("accounts", default["accounts"])
    return tax


def save_taxonomy(tax):
    _write_json(TAXONOMY_FILE, tax)


def sync_taxonomy_from_nodes():
    """Keep taxonomy in sync with registered servers and Uptime Kuma sites."""
    tax = load_taxonomy()
    clients = set(tax.get("clients", []))
    accounts = dict(tax.get("accounts", {}))
    for n in load_nodes():
        c, a, _ = normalize_metadata(n.get("client", ""), n.get("account", ""), "")
        clients.add(c)
        accounts.setdefault(c, [])
        if a not in accounts[c]:
            accounts[c] = sorted(set(accounts[c]) | {a})
    for s in load_kuma_site_meta().values():
        c, a, _ = normalize_metadata(s.get("client", ""), s.get("account", ""), "")
        clients.add(c)
        accounts.setdefault(c, [])
        if a not in accounts[c]:
            accounts[c] = sorted(set(accounts[c]) | {a})
    tax["clients"] = sorted(clients)
    tax["accounts"] = {k: sorted(set(v)) for k, v in accounts.items()}
    save_taxonomy(tax)


def record_taxonomy(client="", account=""):
    c, a, _ = normalize_metadata(client, account, "")
    tax = load_taxonomy()
    clients = set(tax.get("clients", []))
    clients.add(c)
    tax["clients"] = sorted(clients)
    accounts = tax.setdefault("accounts", {})
    accts = set(accounts.get(c, []))
    accts.add(a)
    accounts[c] = sorted(accts)
    save_taxonomy(tax)


def list_all_clients():
    sync_taxonomy_from_nodes()
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    tax = load_taxonomy()
    site_clients = {normalize_metadata(s.get("client", ""), "", "")[0] for s in sites.values()}
    return sorted({node_client(n) for n in nodes} | site_clients | set(tax.get("clients", [])))


def list_accounts_for_client(client=""):
    sync_taxonomy_from_nodes()
    client = (client or "").strip()
    if client in ("", ".*", "$__all", "All"):
        nodes = load_nodes()
        sites = load_kuma_site_meta()
        tax = load_taxonomy()
        from_nodes = {node_account(n) for n in nodes}
        from_sites = {normalize_metadata("", s.get("account", ""), "")[1] for s in sites.values()}
        from_tax = {a for accs in tax.get("accounts", {}).values() for a in accs}
        return sorted(from_nodes | from_sites | from_tax | {DEFAULT_ACCOUNT})
    client = client or DEFAULT_CLIENT
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    from_nodes = {node_account(n) for n in nodes if node_client(n) == client}
    from_sites = {
        normalize_metadata("", s.get("account", ""), "")[1]
        for s in sites.values()
        if normalize_metadata(s.get("client", ""), "", "")[0] == client
    }
    tax = load_taxonomy()
    from_tax = set(tax.get("accounts", {}).get(client, []))
    return sorted(from_nodes | from_sites | from_tax | {DEFAULT_ACCOUNT})


def taxonomy_overview():
    sync_taxonomy_from_nodes()
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    clients = []
    for c in list_all_clients():
        accs = []
        for a in list_accounts_for_client(c):
            accs.append({
                "name": a,
                "server_count": sum(1 for n in nodes if node_client(n) == c and node_account(n) == a),
                "site_count": sum(
                    1 for s in sites.values()
                    if normalize_metadata(s.get("client", ""), "", "")[0] == c
                    and normalize_metadata("", s.get("account", ""), "")[1] == a
                ),
            })
        clients.append({
            "name": c,
            "server_count": sum(1 for n in nodes if node_client(n) == c),
            "site_count": sum(
                1 for s in sites.values()
                if normalize_metadata(s.get("client", ""), "", "")[0] == c
            ),
            "accounts": accs,
        })
    return {"clients": clients, "total_servers": len(nodes), "total_sites": len(sites)}


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


def command_center(client="", account=""):
    """Unified operational summary for the modern inventory dashboard."""
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

    nodes = filter_nodes(sync_prom_hosts(), client, account)
    sites = list_kuma_sites(client, account)
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
    clients = {}
    assets = []

    def add_bucket(asset_client, asset_account, asset_type, severity):
        c = asset_client or DEFAULT_CLIENT
        a = asset_account or DEFAULT_ACCOUNT
        citem = clients.setdefault(c, {
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
            "accounts": {},
        })
        aitem = citem["accounts"].setdefault(a, {
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
        add_bucket(node_client(n), node_account(n), "server", severity)
        assets.append({
            "type": "server",
            "id": host,
            "name": n.get("name") or host,
            "client": node_client(n),
            "account": node_account(n),
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
        add_bucket(s.get("client"), s.get("account"), "site", severity)
        monitor_name = s.get("monitor_name")
        assets.append({
            "type": "site",
            "id": monitor_name,
            "name": s.get("name") or monitor_name,
            "client": s.get("client") or DEFAULT_CLIENT,
            "account": s.get("account") or DEFAULT_ACCOUNT,
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

    clients_out = []
    for c in clients.values():
        c["accounts"] = sorted(c["accounts"].values(), key=lambda x: x["name"].lower())
        clients_out.append(c)
    clients_out.sort(key=lambda x: (_severity_rank(x["status"]) * -1, x["name"].lower()))
    assets.sort(key=lambda x: (_severity_rank(x["severity"]) * -1, x["type"], x["name"].lower()))

    return {
        "totals": totals,
        "clients": clients_out,
        "assets": assets,
        "alert_settings": settings,
    }


def _migrate_servers(client_from, client_to, account_from=None, account_to=None):
    client_to = (client_to or "").strip() or DEFAULT_CLIENT
    account_to = (account_to or "").strip() or DEFAULT_ACCOUNT
    nodes = load_nodes()
    moved = 0
    for n in nodes:
        if node_client(n) != client_from:
            continue
        if account_from is not None and node_account(n) != account_from:
            continue
        n["client"] = client_to
        n["account"] = account_to
        moved += 1
    if moved:
        save_nodes(nodes)
        record_taxonomy(client_to, account_to)
    return moved


def _migrate_kuma_sites(client_from, client_to, account_from=None, account_to=None):
    client_to = (client_to or "").strip() or DEFAULT_CLIENT
    account_to = (account_to or "").strip() or DEFAULT_ACCOUNT
    sites = load_kuma_site_meta()
    moved = 0
    for s in sites.values():
        c, a, _ = normalize_metadata(s.get("client", ""), s.get("account", ""), "")
        if c != client_from:
            continue
        if account_from is not None and a != account_from:
            continue
        s["client"] = client_to
        s["account"] = account_to
        moved += 1
    if moved:
        save_kuma_site_meta(sites)
        record_taxonomy(client_to, account_to)
    return moved


def add_taxonomy_client(name):
    raw = (name or "").strip()
    if not raw:
        return None, "client name is required"
    c, _, _ = normalize_metadata(raw, "", "")
    if raw.lower() == "unassigned":
        c = DEFAULT_CLIENT
    tax = load_taxonomy()
    clients = set(tax.get("clients", []))
    clients.add(c)
    tax["clients"] = sorted(clients)
    tax.setdefault("accounts", {}).setdefault(c, [])
    if DEFAULT_ACCOUNT not in tax["accounts"][c]:
        tax["accounts"][c] = sorted(set(tax["accounts"][c]) | {DEFAULT_ACCOUNT})
    save_taxonomy(tax)
    return c, None


def rename_taxonomy_client(old, new):
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
    if old in tax.get("accounts", {}):
        tax["accounts"][c_new] = sorted(set(tax["accounts"].get(c_new, [])) | set(tax["accounts"].pop(old)))
    clients = {c_new if x == old else x for x in tax.get("clients", [])}
    tax["clients"] = sorted(clients | {c_new})
    save_taxonomy(tax)
    return None


def delete_taxonomy_client(name, merge_into=None):
    name = (name or "").strip()
    if name in (DEFAULT_CLIENT,):
        return "cannot delete default client"
    nodes = load_nodes()
    sites = load_kuma_site_meta()
    count = sum(1 for n in nodes if node_client(n) == name)
    site_count = sum(1 for s in sites.values() if normalize_metadata(s.get("client", ""), "", "")[0] == name)
    if (count or site_count) and not merge_into:
        return f"{count} server(s) and {site_count} site(s) still use this client \u2014 pick \u201cmerge into\u201d or reassign them first"
    if merge_into:
        _migrate_servers(name, merge_into.strip())
        _migrate_kuma_sites(name, merge_into.strip())
    tax = load_taxonomy()
    tax.get("accounts", {}).pop(name, None)
    tax["clients"] = [c for c in tax.get("clients", []) if c != name]
    save_taxonomy(tax)
    return None


def add_taxonomy_account(client, account):
    c, a, _ = normalize_metadata(client, account, "")
    tax = load_taxonomy()
    tax.setdefault("clients", [])
    if c not in tax["clients"]:
        tax["clients"] = sorted(set(tax["clients"]) | {c})
    accts = set(tax.setdefault("accounts", {}).get(c, []))
    accts.add(a)
    tax["accounts"][c] = sorted(accts)
    save_taxonomy(tax)
    return a, None


def rename_taxonomy_account(client, old_acc, new_acc):
    client = (client or "").strip() or DEFAULT_CLIENT
    old_acc = (old_acc or "").strip()
    new_acc = (new_acc or "").strip()
    if not new_acc:
        return "new name required"
    _, a_new, _ = normalize_metadata(client, new_acc, "")
    nodes = load_nodes()
    for n in nodes:
        if node_client(n) == client and node_account(n) == old_acc:
            n["account"] = a_new
    save_nodes(nodes)
    sites = load_kuma_site_meta()
    for s in sites.values():
        c, a, _ = normalize_metadata(s.get("client", ""), s.get("account", ""), "")
        if c == client and a == old_acc:
            s["account"] = a_new
    save_kuma_site_meta(sites)
    tax = load_taxonomy()
    accts = set(tax.setdefault("accounts", {}).get(client, []))
    accts.discard(old_acc)
    accts.add(a_new)
    tax["accounts"][client] = sorted(accts)
    save_taxonomy(tax)
    record_taxonomy(client, a_new)
    return None


def delete_taxonomy_account(client, account, merge_into=None):
    client = (client or "").strip() or DEFAULT_CLIENT
    account = (account or "").strip()
    if account in (DEFAULT_ACCOUNT,) and client == DEFAULT_CLIENT:
        return "cannot delete default account under Unassigned"
    nodes = load_nodes()
    count = sum(1 for n in nodes if node_client(n) == client and node_account(n) == account)
    sites = load_kuma_site_meta()
    site_count = sum(
        1 for s in sites.values()
        if normalize_metadata(s.get("client", ""), "", "")[0] == client
        and normalize_metadata("", s.get("account", ""), "")[1] == account
    )
    if (count or site_count) and not merge_into:
        return f"{count} server(s) and {site_count} site(s) use this account \u2014 merge into another account first"
    if merge_into:
        _migrate_servers(client, client, account, merge_into.strip())
        _migrate_kuma_sites(client, client, account, merge_into.strip())
    tax = load_taxonomy()
    accts = [a for a in tax.get("accounts", {}).get(client, []) if a != account]
    tax.setdefault("accounts", {})[client] = accts
    save_taxonomy(tax)
    return None


def filter_nodes(nodes, client="", account=""):
    if client and client not in ("", ".*", "$__all", "All"):
        if client == DEFAULT_CLIENT:
            nodes = [n for n in nodes if node_client(n) == DEFAULT_CLIENT]
        else:
            nodes = [n for n in nodes if node_client(n) == client]
    if account and account not in ("", ".*", "$__all", "All"):
        if account == DEFAULT_ACCOUNT:
            nodes = [n for n in nodes if node_account(n) == DEFAULT_ACCOUNT]
        else:
            nodes = [n for n in nodes if node_account(n) == account]
    return nodes


def migrate_nodes():
    """Backfill empty client/account on existing registrations."""
    nodes = load_nodes()
    changed = False
    for n in nodes:
        c, a, _ = normalize_metadata(n.get("client", ""), n.get("account", ""))
        if n.get("client", "") != c:
            n["client"] = c
            changed = True
        if n.get("account", "") != a:
            n["account"] = a
            changed = True
        if not n.get("name"):
            n["name"] = n["hostname"]
            changed = True
    if changed:
        save_nodes(nodes)


# ---- alert recipients (per-client email) ------------------------------------

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
    """{client: {'recipients': [{'email','enabled'}]}}"""
    raw = _read_json(ALERT_RECIPIENTS_FILE, {})
    return {c: _normalize_recipient(v) for c, v in raw.items()}


def save_alert_recipients(data):
    _write_json(ALERT_RECIPIENTS_FILE, data)


def get_alert_recipient(client):
    return load_alert_recipients().get(client, {"recipients": []})


def enabled_emails(info):
    """Return the list of enabled addresses from a normalized record."""
    return [r["email"] for r in (info or {}).get("recipients", []) if r.get("enabled")]


def set_alert_recipient(client, recipients):
    """recipients: list of {email,enabled} | list of str | comma string."""
    client = (client or "").strip()
    data = load_alert_recipients()
    norm = _normalize_recipient({"recipients": _coerce_recipients(recipients)})
    if not norm["recipients"]:
        data.pop(client, None)
    else:
        data[client] = norm
    save_alert_recipients(data)
    return data.get(client, {"recipients": []})


def client_host_map():
    """Authoritative client -> [hostnames] mapping from the node registry."""
    out = {}
    for n in load_nodes():
        c = node_client(n)
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
        "enabled": True,
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
