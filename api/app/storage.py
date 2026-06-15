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

def prom_hosts():
    try:
        import urllib.request

        with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/label/host/values", timeout=2) as r:
            return json.loads(r.read()).get("data", [])
    except Exception:
        return []


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
    """Keep taxonomy in sync with all registered servers (clients/accounts always listable)."""
    tax = load_taxonomy()
    clients = set(tax.get("clients", []))
    accounts = dict(tax.get("accounts", {}))
    for n in load_nodes():
        c, a, _ = normalize_metadata(n.get("client", ""), n.get("account", ""), "")
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
    tax = load_taxonomy()
    return sorted({node_client(n) for n in nodes} | set(tax.get("clients", [])))


def list_accounts_for_client(client=""):
    sync_taxonomy_from_nodes()
    client = (client or "").strip()
    if client in ("", ".*", "$__all", "All"):
        nodes = load_nodes()
        tax = load_taxonomy()
        from_nodes = {node_account(n) for n in nodes}
        from_tax = {a for accs in tax.get("accounts", {}).values() for a in accs}
        return sorted(from_nodes | from_tax | {DEFAULT_ACCOUNT})
    client = client or DEFAULT_CLIENT
    nodes = load_nodes()
    from_nodes = {node_account(n) for n in nodes if node_client(n) == client}
    tax = load_taxonomy()
    from_tax = set(tax.get("accounts", {}).get(client, []))
    return sorted(from_nodes | from_tax | {DEFAULT_ACCOUNT})


def taxonomy_overview():
    sync_taxonomy_from_nodes()
    nodes = load_nodes()
    clients = []
    for c in list_all_clients():
        accs = []
        for a in list_accounts_for_client(c):
            accs.append({
                "name": a,
                "server_count": sum(1 for n in nodes if node_client(n) == c and node_account(n) == a),
            })
        clients.append({
            "name": c,
            "server_count": sum(1 for n in nodes if node_client(n) == c),
            "accounts": accs,
        })
    return {"clients": clients, "total_servers": len(nodes)}


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
    count = sum(1 for n in nodes if node_client(n) == name)
    if count and not merge_into:
        return f"{count} server(s) still use this client \u2014 pick \u201cmerge into\u201d or reassign them first"
    if merge_into:
        _migrate_servers(name, merge_into.strip())
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
    if count and not merge_into:
        return f"{count} server(s) use this account \u2014 merge into another account first"
    if merge_into:
        _migrate_servers(client, client, account, merge_into.strip())
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


# ---- alert groups (named set of servers + recipients) -----------------------

def _normalize_group(g):
    if not isinstance(g, dict):
        return None
    gid = (g.get("id") or "").strip()
    name = (g.get("name") or "").strip()
    hosts = [h for h in (g.get("hosts") or []) if isinstance(h, str) and h.strip()]
    hosts = sorted(dict.fromkeys(h.strip() for h in hosts))
    recips = _normalize_recipient({"recipients": _coerce_recipients(g.get("recipients", []))})["recipients"]
    return {
        "id": gid,
        "name": name,
        "hosts": hosts,
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
