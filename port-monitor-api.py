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
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

DATA_DIR = os.environ.get("MONITOR_DATA_DIR", "/var/lib/port-monitor")
CONFIG_FILE = os.environ.get("MONITOR_CONFIG", "/etc/port-monitor/config.json")
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
LISTEN_PORT = int(os.environ.get("MONITOR_PORT", "9099"))
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

        with urllib.request.urlopen("http://localhost:9090/api/v1/label/host/values", timeout=2) as r:
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
<div class="card">
<h2>Add server manually</h2>
<div class="row">
<div><label>Hostname *</label><input id="add-host" placeholder="e.g. prod-web-01"/></div>
<div><label>Display name</label><input id="add-name" placeholder="e.g. Production Web"/></div>
<div><label>IP address</label><input id="add-ip" placeholder="e.g. 10.0.1.5"/></div>
<div><label>Client</label><input id="add-client" placeholder="e.g. acme-corp"/></div>
<div><label>Account</label><input id="add-account" placeholder="e.g. production"/></div>
</div>
<button class="btn btn-primary" onclick="addServer()">+ Add server</button>
<span id="add-status"></span>
</div>
<div class="card"><h2>Registered servers</h2><div id="tree"></div></div>
<div class="card">
<h2>Edit selected server</h2>
<div class="row">
<div><label>Hostname</label><select id="edit-host" style="width:100%"></select></div>
<div><label>Display name</label><input id="edit-name"/></div>
<div><label>IP</label><input id="edit-ip"/></div>
<div><label>Client</label><input id="edit-client"/></div>
<div><label>Account</label><input id="edit-account"/></div>
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
  document.getElementById('tree').innerHTML=html||'<p style="color:#666">No servers yet. Add one above or install Alloy on a node.</p>';
  const sel=document.getElementById('edit-host');
  const prev=sel.value;
  sel.innerHTML='<option value="">— select —</option>';
  nodes.forEach(n=>{const o=document.createElement('option');o.value=n.hostname;o.textContent=n.hostname+(n.name?' ('+n.name+')':'');sel.appendChild(o);});
  if(prev)sel.value=prev;
}

function selectServer(h){
  document.getElementById('edit-host').value=h;
  j(API+'/api/v1/servers/'+encodeURIComponent(h)).then(n=>{
    document.getElementById('edit-name').value=n.name||'';
    document.getElementById('edit-client').value=n.client||'';
    document.getElementById('edit-account').value=n.account||'';
    document.getElementById('edit-ip').value=n.ip||'';
  });
}

async function addServer(){
  const body={hostname:document.getElementById('add-host').value.trim(),name:document.getElementById('add-name').value.trim(),ip:document.getElementById('add-ip').value.trim(),client:document.getElementById('add-client').value.trim(),account:document.getElementById('add-account').value.trim()};
  if(!body.hostname){setSt('add-status','Hostname required',false);return;}
  const d=await j(API+'/api/v1/servers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  setSt('add-status',d.message||d.error,!d.error);
  if(!d.error){['add-host','add-name','add-ip','add-client','add-account'].forEach(id=>document.getElementById(id).value='');loadTree();}
}

async function saveServer(){
  const h=document.getElementById('edit-host').value;
  if(!h){setSt('status','Select a server',false);return;}
  const body={name:document.getElementById('edit-name').value.trim(),client:document.getElementById('edit-client').value.trim(),account:document.getElementById('edit-account').value.trim(),ip:document.getElementById('edit-ip').value.trim()};
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
<div><label>Client</label><input id="client" list="clients" placeholder="e.g. acme-corp"/></div>
<div><label>Account</label><input id="account" list="accounts" placeholder="e.g. production"/></div>
</div>
<datalist id="clients"></datalist>
<datalist id="accounts"></datalist>
<div>
<button class="btn" onclick="save()">Save to monitoring</button>
<span id="status"></span>
<span class="hint" id="hint"></span>
</div>
<script>
__SCRIPT__
const HOST=HOST_PARAM||new URLSearchParams(location.search).get('host')||'';
async function loadLists(){
  const clients=await fetch(API+'/api/v1/variables/clients').then(r=>r.json()).catch(()=>[]);
  const dlC=document.getElementById('clients');dlC.innerHTML='';
  clients.forEach(c=>{const o=document.createElement('option');o.value=c.__value;dlC.appendChild(o);});
  const acc=await fetch(API+'/api/v1/variables/accounts?client=').then(r=>r.json()).catch(()=>[]);
  const dlA=document.getElementById('accounts');dlA.innerHTML='';
  acc.forEach(a=>{const o=document.createElement('option');o.value=a.__value;dlA.appendChild(o);});
}
async function load(){
  document.getElementById('hostname').value=HOST||'';
  if(!HOST){
    document.getElementById('status').textContent='Select a Host in the Grafana dropdown above';
    document.getElementById('status').style.color='#f59e0b';
    return;
  }
  await loadLists();
  const n=await fetch(API+'/api/v1/servers/'+encodeURIComponent(HOST)).then(r=>r.json());
  document.getElementById('name').value=n.name||'';
  document.getElementById('client').value=n.client||'';
  document.getElementById('account').value=n.account||'';
}
async function save(){
  if(!HOST)return;
  const body={
    name:document.getElementById('name').value.trim(),
    client:document.getElementById('client').value.trim(),
    account:document.getElementById('account').value.trim()
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
#edit-status,#add-status{font-size:12px;margin-top:8px}
.empty{color:#666;padding:24px;text-align:center}
</style></head><body>
<h2>All servers</h2>
<p class="sub">Registered and unassigned nodes — click a row to edit client, account, and display name.</p>
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
<div class="field"><label>Client</label><input id="e-client" list="dl-clients" placeholder="e.g. acme"/></div>
<div class="field"><label>Account</label><input id="e-account" list="dl-accounts" placeholder="e.g. production"/></div>
<div class="field"><label>IP address</label><input id="e-ip" placeholder="e.g. 10.0.1.5"/></div>
<button class="btn btn-primary" onclick="saveEdit()">Save</button>
<div id="edit-status"></div>
</div>
<hr style="border:none;border-top:1px solid #222;margin:16px 0"/>
<h3>Add server manually</h3>
<div class="field"><label>Hostname *</label><input id="a-host" placeholder="hostname"/></div>
<div class="field"><label>Display name</label><input id="a-name" placeholder="optional"/></div>
<div class="field"><label>Client</label><input id="a-client" list="dl-clients"/></div>
<div class="field"><label>Account</label><input id="a-account" list="dl-accounts"/></div>
<div class="field"><label>IP</label><input id="a-ip" placeholder="optional"/></div>
<button class="btn btn-primary" onclick="addServer()">+ Add server</button>
<div id="add-status"></div>
</div>
</div>
<datalist id="dl-clients"></datalist>
<datalist id="dl-accounts"></datalist>
<script>
__SCRIPT__
let servers=[], selected=null;
async function load(){
  const d=await fetch(API+'/api/v1/servers').then(r=>r.json());
  servers=d.servers||[];
  const clients=[...new Set(servers.map(s=>s.client||'Unassigned'))].sort();
  const fc=document.getElementById('filter-client');
  const prevC=fc.value;fc.innerHTML='<option value="">All clients</option>';
  clients.forEach(c=>{const o=document.createElement('option');o.value=c;o.textContent=c;fc.appendChild(o);});
  if(prevC)fc.value=prevC;
  const accounts=[...new Set(servers.filter(s=>!fc.value||s.client===fc.value).map(s=>s.account||'default'))].sort();
  const fa=document.getElementById('filter-account');
  const prevA=fa.value;fa.innerHTML='<option value="">All accounts</option>';
  accounts.forEach(a=>{const o=document.createElement('option');o.value=a;o.textContent=a;fa.appendChild(o);});
  if(prevA)fa.value=prevA;
  const dlC=document.getElementById('dl-clients');dlC.innerHTML='';
  clients.forEach(c=>{const o=document.createElement('option');o.value=c;dlC.appendChild(o);});
  const dlA=document.getElementById('dl-accounts');dlA.innerHTML='';
  accounts.forEach(a=>{const o=document.createElement('option');o.value=a;dlA.appendChild(o);});
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
function selectServer(host){
  selected=host;
  render();
  const s=servers.find(x=>x.hostname===host);
  if(!s)return;
  document.getElementById('edit-form').style.display='block';
  document.getElementById('edit-hint').textContent='Editing '+host;
  document.getElementById('e-host').value=s.hostname;
  document.getElementById('e-name').value=s.name||'';
  document.getElementById('e-client').value=s.client||'';
  document.getElementById('e-account').value=s.account||'';
  document.getElementById('e-ip').value=s.ip||'';
}
async function saveEdit(){
  const host=document.getElementById('e-host').value;
  const st=document.getElementById('edit-status');
  const body={name:document.getElementById('e-name').value.trim(),client:document.getElementById('e-client').value.trim(),account:document.getElementById('e-account').value.trim(),ip:document.getElementById('e-ip').value.trim()};
  const d=await fetch(API+'/api/v1/servers/'+encodeURIComponent(host),{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  st.textContent=d.message||d.error;st.style.color=d.error?'#ef4444':'#22c55e';
  if(!d.error)load().then(()=>selectServer(host));
}
async function addServer(){
  const st=document.getElementById('add-status');
  const body={hostname:document.getElementById('a-host').value.trim(),name:document.getElementById('a-name').value.trim(),client:document.getElementById('a-client').value.trim(),account:document.getElementById('a-account').value.trim(),ip:document.getElementById('a-ip').value.trim()};
  if(!body.hostname){st.textContent='Hostname required';st.style.color='#f59e0b';return;}
  const d=await fetch(API+'/api/v1/servers',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}).then(r=>r.json());
  st.textContent=d.message||d.error;st.style.color=d.error?'#ef4444':'#22c55e';
  if(!d.error){['a-host','a-name','a-client','a-account','a-ip'].forEach(id=>document.getElementById(id).value='');load().then(()=>selectServer(body.hostname));}
}
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
        # Grafana (:3000) saving to API (:9099) — allow browser cross-origin
        if origin:
            return origin
        return "*"

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

    def _require_write_auth(self, path=""):
        """API key only for destructive/manual API calls — not Grafana dashboard edits."""
        expected = api_key()
        if not expected:
            return True
        if self._is_metadata_path(path) or self._is_port_path(path):
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
        body = content.replace("__SCRIPT__", script).encode()
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
            return self._json(200, load_config())

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
                return self._json(200, load_ports(host))

        # ---- v1 variables ----
        if path == "/api/v1/variables/clients":
            clients = sorted({node_client(n) for n in load_nodes()})
            return self._json(200, [{"__text": c, "__value": c} for c in clients])
        if path == "/api/v1/variables/accounts":
            nodes = filter_nodes(load_nodes(), client=flat.get("client", ""))
            accounts = sorted({node_account(n) for n in nodes})
            return self._json(200, [{"__text": a, "__value": a} for a in accounts])
        if path == "/api/v1/variables/hosts":
            return self._json(200, self._hosts_variable(flat))

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
            clients = sorted({node_client(n) for n in load_nodes()})
            return self._json(200, [{"__text": c, "__value": c} for c in clients])
        if path == "/accounts-list":
            nodes = filter_nodes(load_nodes(), client=flat.get("client", ""))
            accounts = sorted({node_account(n) for n in nodes})
            return self._json(200, [{"__text": a, "__value": a} for a in accounts])
        if path.startswith("/targets/"):
            return self._json(200, load_ports(unquote(path[9:])))
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
        code = 201 if auto else 201
        return self._json(code, {"message": f"{'Registered' if auto else 'Added'} {host}", "hostname": host})

    def do_PUT(self):
        path, _ = self._path_query()
        data = self._body()

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
        if "ip" in data:
            existing["ip"] = data["ip"]
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
        return self._json(200, {"message": f"Saved {existing['hostname']}", "hostname": existing["hostname"]})

    def _add_port(self, host, data):
        host = normalize_host(host)
        if not host:
            return self._json(400, {"error": "hostname required"})

        _, nodes, created = ensure_server(host, create=True)
        if created:
            save_nodes(nodes)

        name = (data.get("name") or "").strip()
        port = str(data.get("port", "")).strip()
        addr = (data.get("address") or "").strip()
        module = data.get("module", "tcp_connect")

        if not addr and not port:
            return self._json(400, {"error": "port or address required"})
        if not name:
            name = f"port_{port or addr.split(':')[-1]}"

        address = probe_address(host, port, addr)
        targets = load_ports(host)
        if any(t["name"] == name for t in targets):
            return self._json(409, {"error": f"port '{name}' already exists"})
        targets.append({"name": name, "address": address, "module": module})
        save_ports(host, targets)
        return self._json(201, {"message": f"Added '{name}' → {address}", "name": name, "address": address})

    def do_DELETE(self):
        path, _ = self._path_query()
        parts = [unquote(p) for p in path.split("/") if p]

        if len(parts) >= 4 and parts[0] == "api" and parts[1] == "v1" and parts[2] == "servers":
            host = parts[3]
            if len(parts) == 6 and parts[4] == "ports":
                return self._delete_port(host, parts[5])
            if len(parts) == 4:
                if not self._require_write_auth(path):
                    return self._json(401, {"error": "invalid or missing X-Monitor-Key"})
                return self._delete_server(host)

        if len(parts) == 2 and parts[0] == "nodes":
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
        filtered = [t for t in targets if t["name"] != name]
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
