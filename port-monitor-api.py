#!/usr/bin/env python3
"""
Central Monitoring API — server registry, naming, and port probe targets.

Runs on the central Grafana/Prometheus server (:9099).

REST API (preferred):
  GET    /api/v1/docs
  GET    /api/v1/servers
  POST   /api/v1/servers              manual add: {hostname, name, client, account, ip}
  GET    /api/v1/servers/<host>
  PUT    /api/v1/servers/<host>       rename / set metadata
  DELETE /api/v1/servers/<host>
  POST   /api/v1/servers/register       auto-register from install-alloy.sh
  GET    /api/v1/servers/<host>/ports
  POST   /api/v1/servers/<host>/ports   {name, port, address?, module?}
  DELETE /api/v1/servers/<host>/ports/<name>
  GET    /api/v1/servers/<host>/targets Alloy blackbox target list
  GET    /api/v1/variables/clients|accounts|hosts

Legacy paths (unchanged for Alloy + older dashboards):
  GET /targets/<host>  GET/POST /ports/<host>  DELETE /ports/<host>/<name>
  GET/PUT/DELETE /nodes/<host>  POST /nodes/register
  GET /clients-list  GET /accounts-list  GET /hosts-list  GET /nodes  GET /health

Data: /var/lib/port-monitor/nodes.json, /var/lib/port-monitor/ports/<host>.json
Config: /etc/port-monitor/config.json  {"public_url": "http://x.x.x.x:9099"}
"""

import fcntl
import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

DATA_DIR = os.environ.get("MONITOR_DATA_DIR", "/var/lib/port-monitor")
CONFIG_FILE = os.environ.get("MONITOR_CONFIG", "/etc/port-monitor/config.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
TAXONOMY_FILE = os.path.join(DATA_DIR, "taxonomy.json")
LISTEN_PORT = int(os.environ.get("MONITOR_PORT", "9099"))
PROMETHEUS_URL = os.environ.get("MONITOR_PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
TIMESTAMP = lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_config():
    cfg = {
        "public_url": os.environ.get("MONITOR_PUBLIC_URL", ""),
        "grafana_url": os.environ.get("MONITOR_GRAFANA_URL", ""),
        "install_token": os.environ.get("MONITOR_INSTALL_TOKEN", ""),
        "api_key": os.environ.get("MONITOR_API_KEY", ""),
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    return cfg


def install_token():
    return (load_config().get("install_token") or "").strip()


def api_key():
    return (load_config().get("api_key") or "").strip()


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


def get_port_file(host):
    return os.path.join(DATA_DIR, "ports", f"{host}.json")


def load_ports(host):
    return _read_json(get_port_file(host), [])


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


def save_ports(host, targets):
    _write_json(get_port_file(host), targets)


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


def sanitize_port_name(name):
    """Prometheus/Alloy label-safe port name (used as blackbox job → port label)."""
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (name or "").strip())
    return s.strip("_") or "port"


def probe_address(host, port, addr=None):
    """Build blackbox target address using the server's registered IP."""
    if addr and str(addr).strip():
        return str(addr).strip()
    nodes = load_nodes()
    node = find_node(nodes, host) or {}
    ip = (node.get("ip") or "").strip() or host
    return f"{ip}:{port}"


def prom_hosts():
    try:
        import urllib.request

        with urllib.request.urlopen(f"{PROMETHEUS_URL}/api/v1/label/host/values", timeout=2) as r:
            return json.loads(r.read()).get("data", [])
    except Exception:
        return []


DEFAULT_CLIENT = "Unassigned"
DEFAULT_ACCOUNT = "default"


def normalize_metadata(client="", account="", name=""):
    """Apply defaults so new installs always appear in Grafana dropdowns."""
    c = (client or "").strip() or DEFAULT_CLIENT
    a = (account or "").strip() or DEFAULT_ACCOUNT
    return c, a, (name or "").strip()


def node_client(n):
    return (n.get("client") or "").strip() or DEFAULT_CLIENT


def node_account(n):
    return (n.get("account") or "").strip() or DEFAULT_ACCOUNT


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
        return f"{count} server(s) still use this client — pick “merge into” or reassign them first"
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
        return f"{count} server(s) use this account — merge into another account first"
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


# ---- Embedded management UI ---------------------------------------------------

def _api_base_script():
    return "const API=window.location.origin;"


COMBO_JS = r"""
async function fetchClients(){
  const r=await fetch(API+'/api/v1/variables/clients').then(x=>x.json()).catch(()=>[]);
  return r.map(x=>x.__value||x);
}
async function fetchAccounts(client){
  const u=API+'/api/v1/variables/accounts?client='+encodeURIComponent(client||'');
  const r=await fetch(u).then(x=>x.json()).catch(()=>[]);
  return r.map(x=>x.__value||x);
}
function fillCombo(selId,newId,values,current){
  const sel=document.getElementById(selId),inp=document.getElementById(newId);
  sel.innerHTML='';
  (values||[]).forEach(v=>{const o=document.createElement('option');o.value=v;o.textContent=v;sel.appendChild(o);});
  const o=document.createElement('option');o.value='__new__';o.textContent='+ Add new…';sel.appendChild(o);
  if(current&&(values||[]).includes(current)){sel.value=current;inp.style.display='none';inp.value='';}
  else if(current){sel.value='__new__';inp.style.display='block';inp.value=current;}
  else{sel.value=(values&&values[0])||'__new__';inp.style.display=sel.value==='__new__'?'block':'none';}
}
function onCombo(selId,newId,after){
  const sel=document.getElementById(selId),inp=document.getElementById(newId);
  if(sel.value==='__new__'){inp.style.display='block';inp.focus();}
  else{inp.style.display='none';inp.value='';if(after)after();}
}
function comboVal(selId,newId){
  const sel=document.getElementById(selId);
  if(sel.value==='__new__')return document.getElementById(newId).value.trim();
  return sel.value;
}
"""


HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Monitoring Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:20px;max-width:1100px;margin:0 auto}
h1{font-size:20px;margin-bottom:4px;color:#fff}
.sub{font-size:12px;color:#666;margin-bottom:18px}
.tabs{display:flex;gap:20px;margin-bottom:16px;border-bottom:1px solid #333;padding-bottom:8px}
.tab{color:#888;cursor:pointer;padding:4px 0;font-size:14px}.tab.active{color:#fff;border-bottom:2px solid #f59e0b;font-weight:600}
.section{display:none}.section.active{display:block}
.card{background:#111318;border:1px solid #222;border-radius:8px;padding:14px;margin-bottom:14px}
.card h2{font-size:13px;color:#aaa;margin-bottom:10px;font-weight:500}
input,select{padding:8px 10px;border-radius:6px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:13px}
input:focus,select:focus{outline:none;border-color:#f59e0b}
label{font-size:11px;color:#888;display:block;margin-bottom:4px}
.row{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-bottom:10px}
.btn{padding:8px 16px;border:none;border-radius:6px;font-weight:600;cursor:pointer;font-size:13px}
.btn-primary{background:#22c55e;color:#fff}.btn-danger{background:#ef4444;color:#fff}.btn-secondary{background:#333;color:#ccc}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #1a1a2e}
th{color:#666;font-weight:500}
.badge{font-size:10px;background:#333;color:#aaa;padding:2px 8px;border-radius:4px}
#status,#p-status{font-size:12px;margin-left:10px}
.indent{margin-left:16px;margin-top:8px}
.click-row{cursor:pointer}.click-row:hover{background:#1a1a2e}
</style></head><body>
<h1>Central Monitoring API</h1>
<p class="sub">Name servers, organize by client/account, and manage port probes — no SSH required.</p>
<div class="tabs">
<span class="tab active" data-tab="servers">Servers</span>
<span class="tab" data-tab="ports">Ports</span>
<span class="tab" data-tab="api">API Docs</span>
</div>

<div id="servers" class="section active">
<div class="card"><h2>Registered servers</h2><p class="sub" style="margin-bottom:10px">Install Alloy on a node to register it automatically.</p><div id="tree"></div></div>
<div class="card">
<h2>Edit selected server</h2>
<div class="row">
<div><label>Hostname</label><select id="edit-host" style="width:100%"></select></div>
<div><label>Display name</label><input id="edit-name"/></div>
<div><label>IP (from install)</label><input id="edit-ip" readonly title="Set when Alloy registers; re-run install-alloy.sh to refresh"/></div>
<div><label>Client</label><select id="edit-client-sel" style="width:100%" onchange="onCombo('edit-client-sel','edit-client-new',refreshEditAccounts)"></select><input id="edit-client-new" style="display:none;margin-top:6px;width:100%" placeholder="New client name"/></div>
<div><label>Account</label><select id="edit-account-sel" style="width:100%" onchange="onCombo('edit-account-sel','edit-account-new')"></select><input id="edit-account-new" style="display:none;margin-top:6px;width:100%" placeholder="New account name"/></div>
</div>
<button class="btn btn-primary" onclick="saveServer()">Save</button>
<button class="btn btn-danger" onclick="deleteServer()">Delete</button>
<span id="status"></span>
</div>
</div>

<div id="ports" class="section">
<div class="card">
<h2>Add port probe</h2>
<div class="row">
<div><label>Server</label><select id="p-host" onchange="loadPorts()" style="width:100%"></select></div>
<div><label>Name *</label><input id="p-name" placeholder="redis"/></div>
<div><label>Port *</label><input id="p-port" type="number" placeholder="6379"/></div>
<div><label>Check type</label><select id="p-module"><option value="tcp_connect">TCP</option><option value="http_2xx">HTTP</option></select></div>
</div>
<p style="font-size:11px;color:#666;margin-bottom:10px">Probes use the server's registered IP. Alloy on the node picks up changes within ~30s.</p>
<button class="btn btn-primary" onclick="addPort()">+ Add port</button>
<span id="p-status"></span>
</div>
<div class="card"><h2>Monitored ports</h2>
<table><thead><tr><th>Name</th><th>Address</th><th>Type</th><th></th></tr></thead><tbody id="port-list"></tbody></table>
</div>
</div>

<div id="api" class="section">
<div class="card"><h2>REST API</h2><pre id="api-docs" style="font-size:12px;color:#ccc;white-space:pre-wrap;line-height:1.5"></pre></div>
</div>

<script>
__SCRIPT__
document.querySelectorAll('.tab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x===t));
  document.querySelectorAll('.section').forEach(s=>s.classList.remove('active'));
  document.getElementById(t.dataset.tab).classList.add('active');
  if(t.dataset.tab==='ports')loadHostSelect();
  if(t.dataset.tab==='api')loadDocs();
});
const qs=new URLSearchParams(location.search);
if(qs.get('tab')==='ports'){document.querySelector('[data-tab=ports]').click();}
if(qs.get('host')){setTimeout(()=>{const s=document.getElementById('p-host');if(s){s.value=qs.get('host');loadPorts();}},300);}

async function j(url,opt){const r=await fetch(url,opt);return r.json();}
function setSt(id,msg,ok){const e=document.getElementById(id);e.textContent=msg;e.style.color=ok?'#22c55e':'#ef4444';}

async function loadTree(){
  const d=await j(API+'/api/v1/servers');
  const nodes=d.servers||[];
  const tree={};
  nodes.forEach(n=>{
    const c=n.client||'Unassigned',a=n.account||'default';
    if(!tree[c])tree[c]={};if(!tree[c][a])tree[c][a]=[];
    tree[c][a].push(n);
  });
  let html='';
  Object.keys(tree).sort().forEach(client=>{
    const accounts=tree[client];
    const cnt=Object.values(accounts).reduce((s,a)=>s+a.length,0);
    html+=`<div class="card"><strong>${client}</strong> <span class="badge">${cnt} servers</span>`;
    Object.keys(accounts).sort().forEach(account=>{
      html+=`<div class="indent"><h2 style="font-size:12px;color:#888;margin:8px 0">${account}</h2><table><thead><tr><th>Hostname</th><th>Name</th><th>IP</th><th>Ports</th></tr></thead><tbody>`;
      accounts[account].forEach(s=>{
        html+=`<tr class="click-row" onclick="selectServer('${s.hostname}')"><td>${s.hostname}</td><td>${s.name||'-'}</td><td>${s.ip||'-'}</td><td>${s.port_count||0}</td></tr>`;
      });
      html+='</tbody></table></div>';
    });
    html+='</div>';
  });
  document.getElementById('tree').innerHTML=html||'<p style="color:#666">No servers yet. Install Alloy on a node with install-alloy.sh.</p>';
  const sel=document.getElementById('edit-host');
  const prev=sel.value;
  sel.innerHTML='<option value="">— select —</option>';
  nodes.forEach(n=>{const o=document.createElement('option');o.value=n.hostname;o.textContent=n.hostname+(n.name?' ('+n.name+')':'');sel.appendChild(o);});
  if(prev)sel.value=prev;
}

async function refreshEditAccounts(){
  const c=comboVal('edit-client-sel','edit-client-new');
  const acc=await fetchAccounts(c);
  fillCombo('edit-account-sel','edit-account-new',acc,document.getElementById('edit-account-sel').dataset.current||'');
}

function selectServer(h){
  document.getElementById('edit-host').value=h;
  j(API+'/api/v1/servers/'+encodeURIComponent(h)).then(async n=>{
    document.getElementById('edit-name').value=n.name||'';
    document.getElementById('edit-ip').value=n.ip||'';
    const clients=await fetchClients();
    fillCombo('edit-client-sel','edit-client-new',clients,n.client||'');
    const acc=await fetchAccounts(n.client||'');
    document.getElementById('edit-account-sel').dataset.current=n.account||'';
    fillCombo('edit-account-sel','edit-account-new',acc,n.account||'');
  });
}

async function saveServer(){
  const h=document.getElementById('edit-host').value;
  if(!h){setSt('status','Select a server',false);return;}
  const body={name:document.getElementById('edit-name').value.trim(),client:comboVal('edit-client-sel','edit-client-new'),account:comboVal('edit-account-sel','edit-account-new')};
  const d=await j(API+'/api/v1/servers/'+encodeURIComponent(h),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  setSt('status',d.message||d.error,!d.error);loadTree();
}

async function deleteServer(){
  const h=document.getElementById('edit-host').value;
  if(!h||!confirm('Delete '+h+' and all its ports?'))return;
  await j(API+'/api/v1/servers/'+encodeURIComponent(h),{method:'DELETE'});
  setSt('status','Removed',true);loadTree();
}

async function loadHostSelect(){
  const d=await j(API+'/api/v1/servers');
  const s=document.getElementById('p-host'),p=s.value||qs.get('host')||'';
  s.innerHTML='';
  (d.servers||[]).forEach(n=>{const o=document.createElement('option');o.value=n.hostname;o.textContent=n.hostname+(n.name?' ('+n.name+')':'');s.appendChild(o);});
  if(p)s.value=p;loadPorts();
}

async function loadPorts(){
  const h=document.getElementById('p-host').value;if(!h)return;
  const d=await j(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports');
  const t=document.getElementById('port-list');t.innerHTML='';
  (d.ports||[]).forEach(p=>{
    t.innerHTML+=`<tr><td>${p.name}</td><td>${p.address}</td><td>${p.module}</td><td><button class="btn btn-danger" style="padding:4px 10px;font-size:11px" onclick="delPort('${p.name}')">Remove</button></td></tr>`;
  });
}

async function addPort(){
  const h=document.getElementById('p-host').value,n=document.getElementById('p-name').value.trim(),p=document.getElementById('p-port').value.trim(),m=document.getElementById('p-module').value;
  if(!h||!n||!p){setSt('p-status','Server, name, and port required',false);return;}
  const d=await j(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,port:p,module:m})});
  setSt('p-status',d.message||d.error,!d.error);
  if(!d.error){document.getElementById('p-name').value='';document.getElementById('p-port').value='';loadPorts();}
}

async function delPort(n){
  const h=document.getElementById('p-host').value;
  await j(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports/'+encodeURIComponent(n),{method:'DELETE'});
  loadPorts();
}

async function loadDocs(){
  const d=await j(API+'/api/v1/docs');
  document.getElementById('api-docs').textContent=JSON.stringify(d,null,2);
}

document.getElementById('edit-host').onchange=function(){if(this.value)selectServer(this.value);};
__COMBO_JS__
loadTree();
</script></body></html>"""

NODES_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Server Settings</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:18px}
h3{font-size:14px;font-weight:600;color:#fff;margin-bottom:4px}
p{font-size:11px;color:#666;margin-bottom:14px;line-height:1.4}
label{font-size:11px;color:#888;margin-bottom:4px;display:block}
input,select{padding:9px 11px;border-radius:6px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:13px;width:100%}
input:read-only{opacity:.7}
.row{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:12px;margin-bottom:14px}
.btn{padding:9px 18px;border:none;border-radius:6px;font-weight:600;cursor:pointer;font-size:13px;background:#22c55e;color:#fff}
.btn:hover{background:#16a34a}
.hint{font-size:11px;color:#f59e0b;margin-left:10px}
#status{font-size:12px;margin-left:10px}
</style></head><body>
<h3>Server settings</h3>
<p>Set display name, client, and account here — no install flags needed. Select a host in the Grafana toolbar above first.</p>
<div class="row">
<div><label>Hostname</label><input id="hostname" readonly/></div>
<div><label>Display name</label><input id="name" placeholder="e.g. Production Web 01"/></div>
<div><label>Client</label><select id="client-sel" style="width:100%" onchange="onCombo('client-sel','client-new',refreshNodeAccounts)"></select><input id="client-new" style="display:none;margin-top:6px;width:100%" placeholder="New client name"/></div>
<div><label>Account</label><select id="account-sel" style="width:100%" onchange="onCombo('account-sel','account-new')"></select><input id="account-new" style="display:none;margin-top:6px;width:100%" placeholder="New account name"/></div>
</div>
<div>
<button class="btn" onclick="save()">Save to monitoring</button>
<span id="status"></span>
<span class="hint" id="hint"></span>
</div>
<script>
__SCRIPT__
const HOST=HOST_PARAM||new URLSearchParams(location.search).get('host')||'';
async function refreshNodeAccounts(){
  const c=comboVal('client-sel','client-new');
  const acc=await fetchAccounts(c);
  fillCombo('account-sel','account-new',acc,document.getElementById('account-sel').dataset.current||'');
}
async function load(){
  document.getElementById('hostname').value=HOST||'';
  if(!HOST){
    document.getElementById('status').textContent='Select a Host in the Grafana dropdown above';
    document.getElementById('status').style.color='#f59e0b';
    return;
  }
  const n=await fetch(API+'/api/v1/servers/'+encodeURIComponent(HOST)).then(r=>r.json());
  document.getElementById('name').value=n.name||'';
  const clients=await fetchClients();
  fillCombo('client-sel','client-new',clients,n.client||'');
  const acc=await fetchAccounts(n.client||'');
  document.getElementById('account-sel').dataset.current=n.account||'';
  fillCombo('account-sel','account-new',acc,n.account||'');
}
async function save(){
  if(!HOST)return;
  const body={
    name:document.getElementById('name').value.trim(),
    client:comboVal('client-sel','client-new'),
    account:comboVal('account-sel','account-new')
  };
  const st=document.getElementById('status');
  const hint=document.getElementById('hint');
  const d=await fetch(API+'/api/v1/servers/'+encodeURIComponent(HOST),{
    method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)
  }).then(r=>r.json());
  if(d.error){st.textContent=d.error;st.style.color='#ef4444';hint.textContent='';return;}
  st.textContent='Saved';st.style.color='#22c55e';
  hint.textContent='Refresh dashboard (F5) to update Client/Account filters';
}
__COMBO_JS__
load();
</script></body></html>"""

PORTS_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>Manage Ports</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:Inter,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:16px}
input,select{padding:7px 10px;border-radius:4px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:12px}
.btn{padding:7px 12px;border:none;border-radius:4px;font-weight:600;cursor:pointer;font-size:12px;background:#22c55e;color:#fff}
.btn-del{background:#ef4444;color:#fff;padding:3px 8px;font-size:11px}
table{width:100%;border-collapse:collapse;margin-top:6px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #1a1a2e;font-size:12px}
th{color:#888}#p-status{font-size:11px;margin-left:8px}</style></head><body>
<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
<label style="color:#888;font-size:12px">Server:</label><select id="p-host" onchange="loadPorts()" style="min-width:200px"></select>
</div>
<div style="display:flex;gap:6px;margin-bottom:10px;align-items:center">
<input id="p-name" placeholder="Name" style="width:110px"/>
<input id="p-port" placeholder="Port" style="width:80px" type="number"/>
<select id="p-module"><option value="tcp_connect">TCP</option><option value="http_2xx">HTTP</option></select>
<button class="btn" onclick="addPort()">+ Add</button><span id="p-status"></span>
</div>
<table><thead><tr><th>Name</th><th>Address</th><th>Type</th><th></th></tr></thead><tbody id="port-list"></tbody></table>
<script>
__SCRIPT__
const qs=new URLSearchParams(location.search);
async function loadHosts(){
  const d=await fetch(API+'/api/v1/servers').then(r=>r.json());
  const s=document.getElementById('p-host'),p=qs.get('host')||s.value;
  s.innerHTML='';(d.servers||[]).forEach(n=>{const o=document.createElement('option');o.value=n.hostname;o.textContent=n.hostname+(n.name?' ('+n.name+')':'');s.appendChild(o);});
  if(p)s.value=p;loadPorts();
}
async function loadPorts(){
  const h=document.getElementById('p-host').value;if(!h)return;
  const d=await fetch(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports').then(r=>r.json());
  const t=document.getElementById('port-list');t.innerHTML='';
  (d.ports||[]).forEach(p=>{t.innerHTML+=`<tr><td>${p.name}</td><td>${p.address}</td><td>${p.module}</td><td><button class='btn-del' onclick="delPort('${p.name}')">Remove</button></td></tr>`;});
}
async function addPort(){
  const h=document.getElementById('p-host').value,n=document.getElementById('p-name').value.trim(),p=document.getElementById('p-port').value.trim(),m=document.getElementById('p-module').value,st=document.getElementById('p-status');
  if(!h||!n||!p){st.textContent='Fill all fields';st.style.color='#f59e0b';return;}
  const d=await fetch(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n,port:p,module:m})}).then(r=>r.json());
  st.textContent=d.message||d.error;st.style.color=d.error?'#ef4444':'#22c55e';if(!d.error){loadPorts();document.getElementById('p-name').value='';document.getElementById('p-port').value='';}
}
async function delPort(n){
  const h=document.getElementById('p-host').value;
  await fetch(API+'/api/v1/servers/'+encodeURIComponent(h)+'/ports/'+encodeURIComponent(n),{method:'DELETE'});
  loadPorts();
}
loadHosts();
</script></body></html>"""

INVENTORY_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>All Servers</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,system-ui,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:18px}
h2{font-size:16px;color:#fff;margin-bottom:4px}
.sub{font-size:12px;color:#666;margin-bottom:14px}
.toolbar{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
input,select{padding:8px 10px;border-radius:6px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:13px}
input.search{min-width:200px}
table{width:100%;border-collapse:collapse;font-size:13px;margin-bottom:16px}
th,td{text-align:left;padding:9px 11px;border-bottom:1px solid #222}
th{color:#888;font-weight:500;font-size:11px;text-transform:uppercase}
tr.data-row{cursor:pointer}tr.data-row:hover{background:#1a1a2e}
tr.selected{background:#1f2937}
.badge{font-size:10px;padding:2px 8px;border-radius:4px;background:#333;color:#aaa}
.badge-unassigned{background:#422006;color:#fbbf24}
.layout{display:grid;grid-template-columns:1fr 340px;gap:16px}
@media(max-width:900px){.layout{grid-template-columns:1fr}}
.panel{background:#111318;border:1px solid #222;border-radius:8px;padding:14px}
.panel h3{font-size:13px;color:#fff;margin-bottom:12px}
label{font-size:11px;color:#888;display:block;margin-bottom:4px}
.field{margin-bottom:10px}
.field input,.field select{width:100%}
.btn{padding:8px 16px;border:none;border-radius:6px;font-weight:600;cursor:pointer;font-size:13px}
.btn-primary{background:#22c55e;color:#fff}
.btn-secondary{background:#333;color:#ccc;margin-left:6px}
#edit-status,#org-status{font-size:12px;margin-top:8px}
.empty{color:#666;padding:24px;text-align:center}
.mtabs{display:flex;gap:16px;margin-bottom:16px;border-bottom:1px solid #333;padding-bottom:8px}
.mtab{color:#888;cursor:pointer;font-size:14px;padding:4px 0}.mtab.active{color:#fff;border-bottom:2px solid #f59e0b;font-weight:600}
.org-grid{display:grid;grid-template-columns:220px 1fr;gap:16px}
.org-list{list-style:none;max-height:420px;overflow:auto}
.org-list li{padding:8px 10px;border-radius:6px;cursor:pointer;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.org-list li:hover,.org-list li.active{background:#1f2937}
.org-list .cnt{font-size:10px;color:#666}
.btn-sm{padding:5px 10px;font-size:11px;margin-right:6px;margin-top:6px}
.btn-danger{background:#ef4444;color:#fff}
input[readonly]{opacity:.75;cursor:not-allowed}
.hint-ip{font-size:10px;color:#666;margin-top:2px}
</style></head><body>
<h2>Monitoring — All servers</h2>
<div class="mtabs">
<span class="mtab active" data-v="servers">Servers</span>
<span class="mtab" data-v="org">Clients &amp; Accounts</span>
</div>
<div id="view-servers">
<p class="sub">From Alloy install. IP is fixed at install — re-run <code>install-alloy.sh</code> on the node to refresh it.</p>
<div class="toolbar">
<input class="search" id="search" placeholder="Search hostname or name..." oninput="render()"/>
<select id="filter-client" onchange="render()"><option value="">All clients</option></select>
<select id="filter-account" onchange="render()"><option value="">All accounts</option></select>
<button class="btn btn-secondary" onclick="load()">Refresh</button>
<span id="count" style="font-size:12px;color:#666;margin-left:8px"></span>
</div>
<div class="layout">
<div>
<table>
<thead><tr><th>Hostname</th><th>Display name</th><th>Client</th><th>Account</th><th>IP</th><th>Ports</th></tr></thead>
<tbody id="rows"></tbody>
</table>
<div id="empty" class="empty" style="display:none">No servers match. Install Alloy on a node or adjust filters.</div>
</div>
<div class="panel">
<h3 id="edit-title">Edit server</h3>
<p class="sub" style="margin-bottom:10px" id="edit-hint">Select a server from the list</p>
<div id="edit-form" style="display:none">
<div class="field"><label>Hostname</label><input id="e-host" readonly/></div>
<div class="field"><label>Display name</label><input id="e-name" placeholder="e.g. Production API"/></div>
<div class="field"><label>Client</label><select id="e-client-sel" onchange="onCombo('e-client-sel','e-client-new',refreshEditAccounts)"></select><input id="e-client-new" style="display:none;margin-top:6px" placeholder="New client name"/></div>
<div class="field"><label>Account</label><select id="e-account-sel" onchange="onCombo('e-account-sel','e-account-new')"></select><input id="e-account-new" style="display:none;margin-top:6px" placeholder="New account name"/></div>
<div class="field"><label>IP address</label><input id="e-ip" readonly/><p class="hint-ip">Detected at install (primary interface)</p></div>
<button class="btn btn-primary" onclick="saveEdit()">Save</button>
<div id="edit-status"></div>
</div>
</div>
</div>
</div>
<div id="view-org" style="display:none">
<p class="sub">Manage the client → account hierarchy used in Grafana filters. Rename or merge to reassign all servers at once.</p>
<div class="org-grid">
<div class="panel">
<h3>Clients</h3>
<button class="btn btn-primary btn-sm" onclick="orgAddClient()">+ New client</button>
<ul class="org-list" id="org-clients"></ul>
</div>
<div class="panel">
<h3 id="org-detail-title">Select a client</h3>
<div id="org-detail" style="display:none">
<div style="margin-bottom:12px">
<button class="btn btn-secondary btn-sm" onclick="orgRenameClient()">Rename</button>
<button class="btn btn-secondary btn-sm" onclick="orgMergeClient()">Merge into…</button>
<button class="btn btn-danger btn-sm" onclick="orgDeleteClient()">Delete</button>
</div>
<h3 style="font-size:12px;color:#888;margin:12px 0 8px">Accounts</h3>
<button class="btn btn-primary btn-sm" onclick="orgAddAccount()">+ New account</button>
<table style="margin-top:10px"><thead><tr><th>Account</th><th>Servers</th><th></th></tr></thead><tbody id="org-accounts"></tbody></table>
</div>
<div id="org-status"></div>
</div>
</div>
</div>
<script>
__SCRIPT__
__COMBO_JS__
document.querySelectorAll('.mtab').forEach(t=>t.onclick=()=>{
  document.querySelectorAll('.mtab').forEach(x=>x.classList.toggle('active',x===t));
  document.getElementById('view-servers').style.display=t.dataset.v==='servers'?'block':'none';
  document.getElementById('view-org').style.display=t.dataset.v==='org'?'block':'none';
  if(t.dataset.v==='org')loadOrg();
});
let servers=[], selected=null, orgData=null, orgClient=null;
async function refreshEditAccounts(){
  const c=comboVal('e-client-sel','e-client-new');
  const acc=await fetchAccounts(c);
  fillCombo('e-account-sel','e-account-new',acc,document.getElementById('e-account-sel').dataset.current||'');
}
async function load(){
  const d=await fetch(API+'/api/v1/servers').then(r=>r.json());
  servers=d.servers||[];
  const clients=await fetchClients();
  const fc=document.getElementById('filter-client');
  const prevC=fc.value;fc.innerHTML='<option value="">All clients</option>';
  clients.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;fc.appendChild(o);});
  if(prevC)fc.value=prevC;
  const accounts=await fetchAccounts(fc.value||'');
  const fa=document.getElementById('filter-account');
  const prevA=fa.value;fa.innerHTML='<option value="">All accounts</option>';
  accounts.forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=a;fa.appendChild(o);});
  if(prevA)fa.value=prevA;
  render();
}
function filtered(){
  const q=document.getElementById('search').value.trim().toLowerCase();
  const c=document.getElementById('filter-client').value;
  const a=document.getElementById('filter-account').value;
  return servers.filter(s=>{
    if(c&&s.client!==c)return false;
    if(a&&s.account!==a)return false;
    if(!q)return true;
    const hay=(s.hostname+' '+(s.name||'')+' '+(s.ip||'')).toLowerCase();
    return hay.includes(q);
  });
}
function render(){
  const list=filtered();
  document.getElementById('count').textContent=list.length+' server(s)';
  const tb=document.getElementById('rows');
  const empty=document.getElementById('empty');
  tb.innerHTML='';
  if(!list.length){empty.style.display='block';return;}
  empty.style.display='none';
  list.forEach(s=>{
    const tr=document.createElement('tr');
    tr.className='data-row'+(selected===s.hostname?' selected':'');
    const cu=s.client||'Unassigned';
    const badge=cu==='Unassigned'?'badge badge-unassigned':'badge';
    tr.innerHTML=`<td><code>${s.hostname}</code></td><td>${s.name||'—'}</td><td><span class="${badge}">${cu}</span></td><td>${s.account||'—'}</td><td>${s.ip||'—'}</td><td>${s.port_count||0}</td>`;
    tr.onclick=()=>selectServer(s.hostname);
    tb.appendChild(tr);
  });
}
async function selectServer(host){
  selected=host;
  render();
  const s=servers.find(x=>x.hostname===host);
  if(!s)return;
  document.getElementById('edit-form').style.display='block';
  document.getElementById('edit-hint').textContent='Editing '+host;
  document.getElementById('e-host').value=s.hostname;
  document.getElementById('e-name').value=s.name||'';
  document.getElementById('e-ip').value=s.ip||'';
  const clients=await fetchClients();
  fillCombo('e-client-sel','e-client-new',clients,s.client||'');
  const acc=await fetchAccounts(s.client||'');
  document.getElementById('e-account-sel').dataset.current=s.account||'';
  fillCombo('e-account-sel','e-account-new',acc,s.account||'');
}
async function saveEdit(){
  const host=document.getElementById('e-host').value;
  const st=document.getElementById('edit-status');
  const body={name:document.getElementById('e-name').value.trim(),client:comboVal('e-client-sel','e-client-new'),account:comboVal('e-account-sel','e-account-new')};
  const d=await fetch(API+'/api/v1/servers/'+encodeURIComponent(host),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  st.textContent=d.message||d.error;st.style.color=d.error?'#ef4444':'#22c55e';
  if(!d.error)load().then(()=>selectServer(host));
}
function orgStatus(msg,ok){const e=document.getElementById('org-status');e.textContent=msg;e.style.color=ok?'#22c55e':'#ef4444';}
async function loadOrg(){
  orgData=await fetch(API+'/api/v1/taxonomy').then(r=>r.json());
  const ul=document.getElementById('org-clients');ul.innerHTML='';
  (orgData.clients||[]).forEach(c=>{
    const li=document.createElement('li');
    li.className=orgClient===c.name?'active':'';
    li.innerHTML=`<span>${c.name}</span><span class="cnt">${c.server_count}</span>`;
    li.onclick=()=>{orgClient=c.name;loadOrg();};
    ul.appendChild(li);
  });
  if(!orgClient&&orgData.clients&&orgData.clients.length)orgClient=orgData.clients[0].name;
  const cur=(orgData.clients||[]).find(x=>x.name===orgClient);
  document.getElementById('org-detail').style.display=cur?'block':'none';
  document.getElementById('org-detail-title').textContent=cur?('Client: '+cur.name):'Select a client';
  const tb=document.getElementById('org-accounts');tb.innerHTML='';
  if(cur)(cur.accounts||[]).forEach(a=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`<td>${a.name}</td><td>${a.server_count}</td><td class="org-acc-actions"></td>`;
    const td=tr.querySelector('.org-acc-actions');
    [['Rename','rename'],['Merge','merge'],['Delete','del']].forEach(([label,act])=>{
      const b=document.createElement('button');
      b.className=(act==='del'?'btn btn-danger btn-sm':'btn btn-secondary btn-sm');
      b.textContent=label;
      b.dataset.act=act;
      b.dataset.account=a.name;
      b.onclick=()=>orgAccountAction(act,a.name);
      td.appendChild(b);
    });
    tb.appendChild(tr);
  });
}
function orgAccountAction(act,name){
  if(act==='rename')return orgRenameAccount(name);
  if(act==='merge')return orgMergeAccount(name);
  return orgDeleteAccount(name);
}
async function orgAddClient(){
  const n=prompt('New client name:');if(!n)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){orgClient=d.name||n.trim();await loadOrg();await load();}
}
async function orgRenameClient(){
  if(!orgClient)return;
  const n=prompt('Rename client to:',orgClient);if(!n||n===orgClient)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_name:n})}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){orgClient=n.trim();await loadOrg();await load();}
}
async function orgMergeClient(){
  if(!orgClient)return;
  const t=prompt('Merge all servers from "'+orgClient+'" into client:');if(!t)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient)+'?merge_into='+encodeURIComponent(t),{method:'DELETE'}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){orgClient=t.trim();await loadOrg();await load();}
}
async function orgDeleteClient(){
  if(!orgClient)return;
  if(!confirm('Delete client "'+orgClient+'"? Servers must be merged or reassigned first.'))return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient),{method:'DELETE'}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){orgClient=null;await loadOrg();await load();}
}
async function orgAddAccount(){
  if(!orgClient)return;
  const n=prompt('New account name under '+orgClient+':');if(!n)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient)+'/accounts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:n})}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){await loadOrg();await load();}
}
async function orgRenameAccount(oldName){
  const n=prompt('Rename account to:',oldName);if(!n||n===oldName)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient)+'/accounts/'+encodeURIComponent(oldName),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({new_name:n})}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){await loadOrg();await load();}
}
async function orgMergeAccount(oldName){
  const t=prompt('Merge account "'+oldName+'" into:');if(!t)return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient)+'/accounts/'+encodeURIComponent(oldName)+'?merge_into='+encodeURIComponent(t),{method:'DELETE'}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){await loadOrg();await load();}
}
async function orgDeleteAccount(name){
  if(!confirm('Delete account "'+name+'"?'))return;
  const d=await fetch(API+'/api/v1/taxonomy/clients/'+encodeURIComponent(orgClient)+'/accounts/'+encodeURIComponent(name),{method:'DELETE'}).then(r=>r.json());
  orgStatus(d.message||d.error,!d.error);if(!d.error){await loadOrg();await load();}
}
document.getElementById('filter-client').onchange=async function(){await load();};
load();
</script></body></html>"""

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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _cors_origin(self):
        origin = (self.headers.get("Origin") or "").strip().rstrip("/")
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

    def _auth_header(self):
        key = self.headers.get("X-Monitor-Key", "")
        if key:
            return key
        auth = self.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        return ""

    def _require_install_token(self):
        expected = install_token()
        if not expected:
            return True
        return self.headers.get("X-Install-Token", "") == expected

    def _is_metadata_path(self, path):
        parts = path.strip("/").split("/")
        if path.startswith("/api/v1/servers/") and len(parts) == 3:
            return parts[2] not in ("register",)
        if path.startswith("/nodes/") and len(parts) == 2:
            return True
        return False

    def _is_port_path(self, path):
        return "/ports" in path

    def _is_taxonomy_path(self, path):
        return path.startswith("/api/v1/taxonomy")

    def _require_write_auth(self, path=""):
        """API key for destructive ops; metadata/ports open for Grafana iframe (same-origin/CORS)."""
        expected = api_key()
        if not expected:
            return True
        if self._is_metadata_path(path) or self._is_port_path(path):
            return True
        if self._is_taxonomy_path(path) and self.command == "DELETE":
            return self._auth_header() == expected
        if self._is_taxonomy_path(path):
            return True
        return self._auth_header() == expected

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        for k, v in [
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", self._cors_origin()),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, X-Monitor-Key, X-Install-Token, Authorization"),
            ("Content-Length", str(len(body))),
        ]:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _html(self, content):
        script = _api_base_script()
        body = content.replace("__SCRIPT__", script).replace("__COMBO_JS__", COMBO_JS).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def _path_query(self):
        parsed = urlparse(self.path)
        return parsed.path.rstrip("/") or "/", parse_qs(parsed.query)

    def do_OPTIONS(self):
        self._json(200, {})

    def do_GET(self):
        path, qs = self._path_query()
        flat = {k: v[0] for k, v in qs.items()}

        if path == "/":
            return self._html(HTML)
        if path == "/nodes-only":
            host = flat.get("host", "")
            return self._html(NODES_HTML.replace("HOST_PARAM", json.dumps(host)))
        if path == "/ports-only":
            return self._html(PORTS_HTML)
        if path in ("/inventory", "/servers"):
            return self._html(INVENTORY_HTML)
        if path in ("/health", "/api/v1/health"):
            return self._json(200, {"status": "ok"})
        if path == "/api/v1/docs":
            docs = dict(API_DOCS)
            docs["public_url"] = load_config().get("public_url", "")
            return self._json(200, docs)
        if path == "/api/v1/config":
            cfg = load_config()
            return self._json(200, {
                "public_url": cfg.get("public_url", ""),
                "grafana_url": cfg.get("grafana_url", ""),
                "has_install_token": bool(cfg.get("install_token")),
                "has_api_key": bool(cfg.get("api_key")),
            })

        # ---- v1 servers ----
        if path == "/api/v1/servers":
            nodes = load_nodes()
            servers = []
            for n in nodes:
                item = dict(n)
                item["port_count"] = len(load_ports(n["hostname"]))
                servers.append(item)
            return self._json(200, {"servers": servers, "count": len(servers)})

        if path.startswith("/api/v1/servers/"):
            parts = path.split("/")
            host = unquote(parts[4]) if len(parts) > 4 else ""
            if len(parts) == 5 and parts[4] not in ("register",):
                node = find_node(load_nodes(), host)
                if not node:
                    return self._json(404, {"error": f"server '{host}' not found"})
                out = dict(node)
                out["port_count"] = len(load_ports(host))
                return self._json(200, out)
            if len(parts) == 6 and parts[5] == "ports":
                return self._json(200, {"host": host, "ports": load_ports(host), "count": len(load_ports(host))})
            if len(parts) == 6 and parts[5] == "targets":
                return self._json(200, alloy_targets(host))

        # ---- taxonomy (clients & accounts) ----
        if path == "/api/v1/taxonomy":
            return self._json(200, taxonomy_overview())

        # ---- v1 variables ----
        if path == "/api/v1/variables/clients":
            clients = list_all_clients()
            return self._json(200, [{"__text": c, "__value": c} for c in clients])
        if path == "/api/v1/variables/accounts":
            accounts = list_accounts_for_client(flat.get("client", ""))
            return self._json(200, [{"__text": a, "__value": a} for a in accounts])
        if path == "/api/v1/variables/hosts":
            return self._json(200, self._hosts_variable(flat))
        if path == "/api/v1/variables/ports":
            host = normalize_host(flat.get("host", ""))
            if not host:
                return self._json(200, [])
            targets = load_ports(host)
            return self._json(200, [{"__text": t["name"], "__value": sanitize_port_name(t["name"])} for t in targets])

        # ---- legacy ----
        if path == "/nodes":
            nodes = filter_nodes(load_nodes(), flat.get("client", ""), flat.get("account", ""))
            return self._json(200, {"nodes": nodes})
        if path == "/hosts":
            nodes = load_nodes()
            return self._json(200, {"hosts": [{"host": n["hostname"], "count": len(load_ports(n["hostname"]))} for n in nodes]})
        if path == "/hosts-list":
            return self._json(200, self._hosts_variable(flat, include_unregistered=flat.get("include_unregistered", "").lower() in ("1", "true", "yes")))
        if path == "/clients-list":
            clients = list_all_clients()
            return self._json(200, [{"__text": c, "__value": c} for c in clients])
        if path == "/accounts-list":
            accounts = list_accounts_for_client(flat.get("client", ""))
            return self._json(200, [{"__text": a, "__value": a} for a in accounts])
        if path.startswith("/targets/"):
            return self._json(200, alloy_targets(unquote(path[9:])))
        if path.startswith("/metadata/"):
            host = unquote(path[10:])
            node = find_node(load_nodes(), host) or {}
            return self._json(200, {"hostname": host, "account": node.get("account", ""), "name": node.get("name", ""), "ip": node.get("ip", ""), "client": node.get("client", "")})
        if path.startswith("/ports/"):
            host = unquote(path[7:])
            return self._json(200, {"host": host, "ports": load_ports(host), "count": len(load_ports(host))})

        return self._json(404, {"error": "not found"})

    def _hosts_variable(self, qs, include_unregistered=False):
        client = qs.get("client", "")
        account = qs.get("account", "")
        nodes = list(load_nodes())
        if include_unregistered:
            registered = {n["hostname"] for n in nodes}
            for h in prom_hosts():
                if h and h not in registered:
                    nodes.append({"hostname": h, "ip": "", "client": "", "account": "", "name": ""})
        nodes = filter_nodes(nodes, client, account)
        return [{"__text": host_display(n), "__value": n["hostname"]} for n in sorted(nodes, key=lambda x: host_display(x))]

    def do_POST(self):
        path, _ = self._path_query()
        data = self._body()

        if path in ("/nodes/register", "/api/v1/servers/register"):
            if not self._require_install_token():
                return self._json(401, {"error": "invalid or missing X-Install-Token"})
            return self._register_server(data, auto=True)
        if path == "/api/v1/servers":
            if not self._require_write_auth(path):
                return self._json(401, {"error": "invalid or missing X-Monitor-Key"})
            return self._register_server(data, auto=False)

        if path == "/api/v1/taxonomy/clients":
            c, err = add_taxonomy_client(data.get("name", ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(201, {"message": f"Client '{c}' added", "name": c})

        parts = [unquote(p) for p in path.split("/") if p]
        if len(parts) == 6 and parts[:3] == ["api", "v1", "taxonomy"] and parts[3] == "clients" and parts[5] == "accounts":
            acc, err = add_taxonomy_account(parts[4], data.get("name", ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(201, {"message": f"Account '{acc}' added under '{parts[4]}'", "name": acc})

        if path.startswith("/api/v1/servers/") and path.endswith("/ports"):
            host = unquote(path.split("/")[4])
            return self._add_port(host, data)

        if path.startswith("/ports/"):
            return self._add_port(unquote(path.split("/")[2]), data)

        return self._json(404, {"error": "not found"})

    def _register_server(self, data, auto=False):
        host = normalize_host(data.get("hostname"))
        if not host:
            return self._json(400, {"error": "hostname is required"})

        nodes = load_nodes()
        existing = find_node(nodes, host)
        if existing:
            if not auto:
                return self._json(409, {"error": f"server '{host}' already exists — use PUT to update"})
            if data.get("ip"):
                existing["ip"] = data["ip"]
            c, a, nm = normalize_metadata(
                data.get("client") or existing.get("client", ""),
                data.get("account") or existing.get("account", ""),
                data.get("name") or existing.get("name", ""),
            )
            if data.get("ip"):
                existing["ip"] = data["ip"]
            existing["client"] = c
            existing["account"] = a
            if nm:
                existing["name"] = nm
            existing["last_seen"] = TIMESTAMP()
            save_nodes(nodes)
            record_taxonomy(existing["client"], existing["account"])
            return self._json(200, {"message": f"Updated {host}", "hostname": host})

        c, a, nm = normalize_metadata(data.get("client", ""), data.get("account", ""), data.get("name", ""))
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
        save_nodes(nodes)
        record_taxonomy(c, a)
        code = 201 if auto else 201
        return self._json(code, {"message": f"{'Registered' if auto else 'Added'} {host}", "hostname": host})

    def do_PUT(self):
        path, _ = self._path_query()
        data = self._body()

        parts = [unquote(p) for p in path.split("/") if p]
        if len(parts) == 5 and parts[:3] == ["api", "v1", "taxonomy"] and parts[3] == "clients":
            err = rename_taxonomy_client(parts[4], data.get("new_name", ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"message": f"Client renamed to '{data.get('new_name')}'"})
        if len(parts) == 7 and parts[:3] == ["api", "v1", "taxonomy"] and parts[3] == "clients" and parts[5] == "accounts":
            err = rename_taxonomy_account(parts[4], parts[6], data.get("new_name", ""))
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"message": f"Account renamed to '{data.get('new_name')}'"})

        if path.startswith("/api/v1/servers/"):
            host = unquote(path.split("/")[4])
        elif path.startswith("/nodes/"):
            host = unquote(path.split("/")[2])
        else:
            return self._json(404, {"error": "not found"})

        nodes = load_nodes()
        existing = find_node(nodes, host)
        if not existing:
            existing = {"hostname": host, "registered": TIMESTAMP()}
            nodes.append(existing)
        # IP is set only at Alloy install/register — not editable from dashboards
        c, a, nm = normalize_metadata(
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
            existing["hostname"] = normalize_host(data["hostname"])
        save_nodes(nodes)
        record_taxonomy(existing.get("client", ""), existing.get("account", ""))
        return self._json(200, {"message": f"Saved {existing['hostname']}", "hostname": existing["hostname"]})

    def _add_port(self, host, data):
        host = normalize_host(host)
        if not host:
            return self._json(400, {"error": "hostname required"})

        _, nodes, created = ensure_server(host, create=True)
        if created:
            save_nodes(nodes)

        name = sanitize_port_name((data.get("name") or "").strip())
        port = str(data.get("port", "")).strip()
        addr = (data.get("address") or "").strip()
        module = data.get("module", "tcp_connect")

        if not addr and not port:
            return self._json(400, {"error": "port or address required"})
        if not name:
            name = sanitize_port_name(f"port_{port or addr.split(':')[-1]}")

        address = probe_address(host, port, addr)
        targets = load_ports(host)
        if any(sanitize_port_name(t["name"]) == name for t in targets):
            return self._json(409, {"error": f"port '{name}' already exists"})
        targets.append({"name": name, "address": address, "module": module})
        save_ports(host, targets)
        return self._json(201, {"message": f"Added '{name}' → {address}", "name": name, "address": address})

    def do_DELETE(self):
        path, flat = self._path_query()
        parts = [unquote(p) for p in path.split("/") if p]
        merge_into = (flat.get("merge_into") or [""])[0]

        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "servers":
            host = parts[3]
            if len(parts) == 6 and parts[4] == "ports":
                return self._delete_port(host, parts[5])
            if len(parts) == 4:
                if not self._require_write_auth(path):
                    return self._json(401, {"error": "invalid or missing X-Monitor-Key"})
                return self._delete_server(host)

        if len(parts) == 5 and parts[:3] == ["api", "v1", "taxonomy"] and parts[3] == "clients":
            err = delete_taxonomy_client(parts[4], merge_into=merge_into or None)
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"message": "Client removed"})
        if len(parts) == 7 and parts[:3] == ["api", "v1", "taxonomy"] and parts[3] == "clients" and parts[5] == "accounts":
            err = delete_taxonomy_account(parts[4], parts[6], merge_into=merge_into or None)
            if err:
                return self._json(400, {"error": err})
            return self._json(200, {"message": "Account removed"})

        if len(parts) == 2 and parts[0] == "nodes":
            if not self._require_write_auth(path):
                return self._json(401, {"error": "invalid or missing X-Monitor-Key"})
            return self._delete_server(parts[1])
        if len(parts) == 3 and parts[0] == "ports":
            return self._delete_port(parts[1], parts[2])

        return self._json(404, {"error": "not found"})

    def _delete_server(self, host):
        nodes = load_nodes()
        before = len(nodes)
        nodes = [n for n in nodes if n["hostname"] != host]
        if len(nodes) == before:
            return self._json(404, {"error": "not found"})
        save_nodes(nodes)
        pf = get_port_file(host)
        if os.path.exists(pf):
            os.remove(pf)
        return self._json(200, {"message": f"Removed {host}"})

    def _delete_port(self, host, name):
        targets = load_ports(host)
        filtered = [t for t in targets if sanitize_port_name(t["name"]) != sanitize_port_name(name)]
        if len(filtered) == len(targets):
            return self._json(404, {"error": "not found"})
        save_ports(host, filtered)
        return self._json(200, {"message": f"Removed '{name}'"})


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


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "ports"), exist_ok=True)
    os.makedirs(os.path.dirname(CONFIG_FILE) or ".", exist_ok=True)
    if not os.path.exists(NODES_FILE):
        _write_json(NODES_FILE, [])
    else:
        migrate_nodes()
    server = HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"Central Monitoring API on :{LISTEN_PORT}")
    print(f"  UI:  http://0.0.0.0:{LISTEN_PORT}/")
    print(f"  API: http://0.0.0.0:{LISTEN_PORT}/api/v1/docs")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
