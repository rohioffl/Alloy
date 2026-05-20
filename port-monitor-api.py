#!/usr/bin/env python3
"""
Port & Node Monitor API — Central server only.

PORT ENDPOINTS:
  GET  /targets/<host>        - Alloy pulls probe targets (JSON list)
  GET  /ports/<host>          - List ports for a host
  POST /ports/<host>          - Add: {"name":"redis","port":"6379"}
  DELETE /ports/<host>/<name> - Remove port

NODE ENDPOINTS:
  GET  /nodes                 - List all nodes with metadata
  POST /nodes/register        - Auto-register: {"hostname":"x","ip":"1.2.3.4"}
  PUT  /nodes/<host>          - Edit: {"account":"master","name":"My Server","ip":"1.2.3.4"}
  DELETE /nodes/<host>        - Remove node

OTHER:
  GET  /hosts                 - List hosts with port counts
  GET  /                      - Management UI
  GET  /health                - Health check

Data stored in /var/lib/port-monitor/
"""

import json, os, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

DATA_DIR = "/var/lib/port-monitor"
NODES_FILE = os.path.join(DATA_DIR, "nodes.json")
LISTEN_PORT = 9099

# ---- Separate HTML pages for embedding ----------------------------------------

NODES_HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Edit Server</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:Inter,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:16px}
input,select{padding:8px 10px;border-radius:4px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:13px;width:100%}
input:focus,select:focus{outline:none;border-color:#f59e0b}
label{font-size:11px;color:#888;margin-bottom:4px;display:block}
.row{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:12px}
.btn{padding:8px 20px;border:none;border-radius:4px;font-weight:600;cursor:pointer;font-size:13px;background:#22c55e;color:#fff}
.btn:hover{background:#16a34a}
#status{font-size:12px;margin-left:12px}
.field{margin-bottom:2px}
.combo{position:relative}
.combo input{position:absolute;top:0;left:0;opacity:0;pointer-events:none}
.combo select{width:100%}
.combo.custom select{display:none}
.combo.custom input{position:static;opacity:1;pointer-events:auto}
.toggle{font-size:10px;color:#f59e0b;cursor:pointer;margin-top:2px;display:inline-block}
</style></head><body>
<div class="row">
<div class="field"><label>Machine Name</label><input id="name" placeholder="e.g. Staging Django"/></div>
<div class="field">
  <label>Client</label>
  <div class="combo" id="client-combo">
    <select id="client-select" onchange="if(this.value==='__new__'){toggleCustom('client',true)}"></select>
    <input id="client-input" placeholder="Type new client"/>
  </div>
  <span class="toggle" onclick="toggleCustom('client')">+ new</span>
</div>
<div class="field">
  <label>Account</label>
  <div class="combo" id="account-combo">
    <select id="account-select" onchange="if(this.value==='__new__'){toggleCustom('account',true)}"></select>
    <input id="account-input" placeholder="Type new account"/>
  </div>
  <span class="toggle" onclick="toggleCustom('account')">+ new</span>
</div>
</div>
<div style="display:flex;align-items:center">
<button class="btn" onclick="save()">Save</button>
<span id="status"></span>
</div>
<script>
const A=window.location.origin;
function getHost(){
  const h = "HOST_PARAM";
  if(h) return h;
  const p = new URLSearchParams(window.location.search);
  return p.get("host")||"";
}
function toggleCustom(field, show){
  const combo = document.getElementById(field+"-combo");
  if(show === undefined) show = !combo.classList.contains("custom");
  combo.classList.toggle("custom", show);
  if(show) document.getElementById(field+"-input").focus();
}
function getVal(field){
  const combo = document.getElementById(field+"-combo");
  if(combo.classList.contains("custom")) return document.getElementById(field+"-input").value.trim();
  const sel = document.getElementById(field+"-select");
  return sel.value === "__new__" ? "" : sel.value;
}
function populateDropdowns(nodes, current){
  // Get unique clients and accounts
  const clients = [...new Set(nodes.map(n=>n.client||"").filter(x=>x))];
  const accounts = [...new Set(nodes.map(n=>n.account||"").filter(x=>x))];
  
  const cs = document.getElementById("client-select");
  cs.innerHTML = "<option value=''>-- select --</option>";
  clients.forEach(c=>{cs.innerHTML += "<option value='"+c+"'>"+c+"</option>"});
  cs.innerHTML += "<option value='__new__'>+ Add new...</option>";
  
  const as = document.getElementById("account-select");
  as.innerHTML = "<option value=''>-- select --</option>";
  accounts.forEach(a=>{as.innerHTML += "<option value='"+a+"'>"+a+"</option>"});
  as.innerHTML += "<option value='__new__'>+ Add new...</option>";
  
  // Set current values
  if(current){
    document.getElementById("name").value = current.name||"";
    if(current.client && clients.includes(current.client)){
      cs.value = current.client;
    } else if(current.client){
      document.getElementById("client-input").value = current.client;
      toggleCustom("client", true);
    }
    if(current.account && accounts.includes(current.account)){
      as.value = current.account;
    } else if(current.account){
      document.getElementById("account-input").value = current.account;
      toggleCustom("account", true);
    }
  }
}
function load(){
  const h=getHost();
  fetch(A+"/nodes").then(r=>r.json()).then(d=>{
    const nodes = d.nodes||[];
    const current = nodes.find(n=>n.hostname===h);
    populateDropdowns(nodes, current);
  });
}
function save(){
  const h=getHost();
  const st=document.getElementById("status");
  if(!h){st.textContent="No host selected";st.style.color="#f59e0b";return;}
  const name=document.getElementById("name").value.trim();
  const client=getVal("client");
  const account=getVal("account");
  fetch(A+"/nodes/"+encodeURIComponent(h),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,client,account})})
    .then(r=>r.json()).then(d=>{st.textContent=d.message||d.error;st.style.color=d.error?"#ef4444":"#22c55e";});
}
load();
</script></body></html>'''

PORTS_HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Manage Ports</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{font-family:Inter,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:16px}
h2{font-size:14px;margin:0 0 10px;color:#fff}
input,select{padding:7px 10px;border-radius:4px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:12px}
.btn{padding:7px 12px;border:none;border-radius:4px;font-weight:600;cursor:pointer;font-size:12px}
.btn-add{background:#22c55e;color:#fff}.btn-del{background:#ef4444;color:#fff;padding:3px 8px;font-size:11px}
table{width:100%;border-collapse:collapse;margin-top:6px}th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #1a1a2e;font-size:12px}
th{color:#888}#p-status{font-size:11px;margin-left:8px}</style></head><body>
<div style="display:flex;gap:8px;margin-bottom:12px;align-items:center">
<label style="color:#888;font-size:12px">Host:</label>
<select id="p-host" onchange="loadPorts()" style="min-width:160px"></select>
</div>
<div style="display:flex;gap:6px;margin-bottom:10px;align-items:center">
<input id="p-name" placeholder="Name" style="width:110px"/>
<input id="p-port" placeholder="Port" style="width:80px" type="number"/>
<select id="p-module"><option value="tcp_connect">TCP</option><option value="http_2xx">HTTP</option></select>
<button class="btn btn-add" onclick="addPort()">+ Add</button>
<span id="p-status"></span>
</div>
<table><thead><tr><th>Name</th><th>Port</th><th>Type</th><th></th></tr></thead><tbody id="port-list"></tbody></table>
<script>
const A=window.location.origin;
function loadHosts(){fetch(A+"/nodes").then(r=>r.json()).then(d=>{const s=document.getElementById("p-host"),p=s.value;s.innerHTML="";(d.nodes||[]).forEach(n=>{const o=document.createElement("option");o.value=n.hostname;o.textContent=n.hostname+(n.name?" ("+n.name+")":"");s.appendChild(o)});if(p)s.value=p;loadPorts();})}
function loadPorts(){const h=document.getElementById("p-host").value;if(!h)return;fetch(A+"/ports/"+encodeURIComponent(h)).then(r=>r.json()).then(d=>{const t=document.getElementById("port-list");t.innerHTML="";(d.ports||[]).forEach(p=>{const port=p.address.split(":").pop();t.innerHTML+="<tr><td>"+p.name+"</td><td>"+port+"</td><td>"+p.module+"</td><td><button class='btn btn-del' onclick='delPort(\\""+p.name+"\\")'>Remove</button></td></tr>"});})}
function addPort(){const h=document.getElementById("p-host").value,n=document.getElementById("p-name").value.trim(),p=document.getElementById("p-port").value.trim(),m=document.getElementById("p-module").value,st=document.getElementById("p-status");if(!h||!n||!p){st.textContent="Fill all fields";st.style.color="#f59e0b";return}fetch(A+"/ports/"+encodeURIComponent(h),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:n,port:p,module:m})}).then(r=>r.json()).then(d=>{st.textContent=d.message||d.error;st.style.color=d.error?"#ef4444":"#22c55e";loadPorts();document.getElementById("p-name").value="";document.getElementById("p-port").value="";})}
function delPort(n){const h=document.getElementById("p-host").value;fetch(A+"/ports/"+encodeURIComponent(h)+"/"+encodeURIComponent(n),{method:"DELETE"}).then(r=>r.json()).then(()=>loadPorts())}
loadHosts();
</script></body></html>'''

# ---- Data helpers ------------------------------------------------------------

def get_port_file(host): return os.path.join(DATA_DIR, "ports", f"{host}.json")

def load_ports(host):
    p = get_port_file(host)
    return json.load(open(p)) if os.path.exists(p) else []

def save_ports(host, targets):
    d = os.path.join(DATA_DIR, "ports")
    os.makedirs(d, exist_ok=True)
    json.dump(targets, open(get_port_file(host), "w"), indent=2)

def load_nodes():
    return json.load(open(NODES_FILE)) if os.path.exists(NODES_FILE) else []

def save_nodes(nodes):
    os.makedirs(DATA_DIR, exist_ok=True)
    json.dump(nodes, open(NODES_FILE, "w"), indent=2)

def find_node(nodes, host):
    for n in nodes:
        if n["hostname"] == host:
            return n
    return None

# ---- HTML UI -----------------------------------------------------------------

HTML = '''<!DOCTYPE html><html><head><meta charset="utf-8"><title>Monitoring Manager</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:Inter,sans-serif;background:#0b0c0e;color:#e0e0e0;padding:20px}
h1{font-size:18px;margin-bottom:16px;color:#fff}
h2{font-size:14px;margin:16px 0 8px;color:#ccc}
h3{font-size:12px;margin:8px 0 4px;color:#888}
input,select{padding:7px 10px;border-radius:4px;border:1px solid #444;background:#1a1a2e;color:#fff;font-size:12px}
input:focus{outline:none;border-color:#f59e0b}
.btn{padding:6px 12px;border:none;border-radius:4px;font-weight:600;cursor:pointer;font-size:11px}
.btn-add{background:#22c55e;color:#fff}.btn-del{background:#ef4444;color:#fff}.btn-edit{background:#3b82f6;color:#fff}
table{width:100%;border-collapse:collapse;margin-top:4px}
th,td{text-align:left;padding:5px 8px;border-bottom:1px solid #1a1a2e;font-size:12px}
th{color:#666;font-weight:500}
.card{background:#111318;border:1px solid #222;border-radius:6px;padding:12px;margin-bottom:12px}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.card-title{font-size:13px;font-weight:600;color:#fff}
.badge{font-size:10px;background:#333;color:#aaa;padding:2px 6px;border-radius:3px}
.indent{margin-left:20px}
.tabs{display:flex;gap:16px;margin-bottom:16px;border-bottom:1px solid #333;padding-bottom:8px}
.tab{color:#888;cursor:pointer;padding:4px 0;font-size:13px}.tab.active{color:#fff;border-bottom:2px solid #f59e0b}
.section{display:none}.section.active{display:block}
.form-row{display:flex;gap:6px;margin-bottom:8px;align-items:center;flex-wrap:wrap}
#status{font-size:11px;margin-left:8px}
</style></head><body>
<h1>Monitoring Manager</h1>
<div class="tabs">
<span class="tab active" onclick="showTab('overview')">Overview</span>
<span class="tab" onclick="showTab('ports')">Ports</span>
</div>

<div id="overview-section" class="section active">
<div id="tree"></div>
<h2 style="margin-top:20px">Add / Edit</h2>
<div class="form-row">
<select id="edit-host" style="min-width:150px"><option value="">Select server...</option></select>
<input id="edit-name" placeholder="Machine Name" style="width:130px"/>
<input id="edit-client" placeholder="Client" style="width:110px"/>
<input id="edit-account" placeholder="Account" style="width:110px"/>
<input id="edit-ip" placeholder="IP" style="width:100px"/>
<button class="btn btn-add" onclick="saveEdit()">Save</button>
<button class="btn btn-del" onclick="delNode()">Delete</button>
<span id="status"></span>
</div>
</div>

<div id="ports-section" class="section">
<div class="form-row">
<label style="color:#888;font-size:11px">Host:</label>
<select id="p-host" onchange="loadPorts()" style="min-width:160px"></select>
</div>
<div class="form-row">
<input id="p-name" placeholder="Name" style="width:100px"/>
<input id="p-port" placeholder="Port" style="width:80px" type="number"/>
<select id="p-module"><option value="tcp_connect">TCP</option><option value="http_2xx">HTTP</option></select>
<button class="btn btn-add" onclick="addPort()">+ Add</button>
<span id="p-status"></span>
</div>
<table><thead><tr><th>Name</th><th>Port</th><th>Type</th><th></th></tr></thead><tbody id="port-list"></tbody></table>
</div>

<script>
const A=window.location.origin;
function showTab(t){
  document.querySelectorAll(".tab").forEach(e=>e.classList.toggle("active",e.textContent.toLowerCase()===t));
  document.querySelectorAll(".section").forEach(e=>e.classList.remove("active"));
  document.getElementById(t+"-section").classList.add("active");
  if(t==="ports")loadHostSelect();
}

// OVERVIEW - Tree view: Client > Account > Servers
function loadTree(){
  fetch(A+"/nodes").then(r=>r.json()).then(d=>{
    const nodes=d.nodes||[];
    // Build hierarchy
    const tree={};
    nodes.forEach(n=>{
      const client=n.client||"Unassigned";
      const account=n.account||"default";
      if(!tree[client])tree[client]={};
      if(!tree[client][account])tree[client][account]=[];
      tree[client][account].push(n);
    });
    // Render
    let html="";
    Object.keys(tree).sort().forEach(client=>{
      const accounts=tree[client];
      const serverCount=Object.values(accounts).reduce((s,a)=>s+a.length,0);
      html+="<div class='card'><div class='card-header'><span class='card-title'>"+client+"</span><span class='badge'>"+serverCount+" servers</span></div>";
      Object.keys(accounts).sort().forEach(account=>{
        const servers=accounts[account];
        html+="<div class='indent'><h3>"+account+" <span class='badge'>"+servers.length+"</span></h3>";
        html+="<table><thead><tr><th>Hostname</th><th>Name</th><th>IP</th><th>Registered</th></tr></thead><tbody>";
        servers.forEach(s=>{
          html+="<tr style='cursor:pointer' onclick='selectNode(\""+s.hostname+"\")'><td>"+s.hostname+"</td><td>"+(s.name||"-")+"</td><td>"+(s.ip||"-")+"</td><td>"+(s.registered||"").split("T")[0]+"</td></tr>";
        });
        html+="</tbody></table></div>";
      });
      html+="</div>";
    });
    document.getElementById("tree").innerHTML=html||"<p style='color:#666'>No nodes registered. Install Alloy on a server to add it.</p>";
    // Populate edit dropdown
    const sel=document.getElementById("edit-host");
    const prev=sel.value;
    sel.innerHTML="<option value=''>Select server...</option>";
    nodes.forEach(n=>{sel.innerHTML+="<option value='"+n.hostname+"'>"+n.hostname+(n.name?" ("+n.name+")":"")+"</option>"});
    if(prev)sel.value=prev;
  });
}

function selectNode(h){
  document.getElementById("edit-host").value=h;
  fetch(A+"/nodes").then(r=>r.json()).then(d=>{
    const n=(d.nodes||[]).find(x=>x.hostname===h);
    if(n){
      document.getElementById("edit-name").value=n.name||"";
      document.getElementById("edit-client").value=n.client||"";
      document.getElementById("edit-account").value=n.account||"";
      document.getElementById("edit-ip").value=n.ip||"";
    }
  });
}

function saveEdit(){
  const h=document.getElementById("edit-host").value;
  const st=document.getElementById("status");
  if(!h){st.textContent="Select a server";st.style.color="#f59e0b";return}
  const data={
    name:document.getElementById("edit-name").value.trim(),
    client:document.getElementById("edit-client").value.trim(),
    account:document.getElementById("edit-account").value.trim(),
    ip:document.getElementById("edit-ip").value.trim()
  };
  fetch(A+"/nodes/"+encodeURIComponent(h),{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)})
    .then(r=>r.json()).then(d=>{st.textContent=d.message||d.error;st.style.color=d.error?"#f44":"#4f4";loadTree();});
}

function delNode(){
  const h=document.getElementById("edit-host").value;
  if(!h||!confirm("Remove "+h+"?"))return;
  fetch(A+"/nodes/"+encodeURIComponent(h),{method:"DELETE"}).then(r=>r.json()).then(()=>{loadTree();document.getElementById("status").textContent="Removed";});
}

// PORTS
function loadHostSelect(){fetch(A+"/nodes").then(r=>r.json()).then(d=>{const s=document.getElementById("p-host"),p=s.value;s.innerHTML="";(d.nodes||[]).forEach(n=>{const o=document.createElement("option");o.value=n.hostname;o.textContent=n.hostname+(n.name?" ("+n.name+")":"");s.appendChild(o)});if(p)s.value=p;loadPorts();})}
function loadPorts(){const h=document.getElementById("p-host").value;if(!h)return;fetch(A+"/ports/"+encodeURIComponent(h)).then(r=>r.json()).then(d=>{const t=document.getElementById("port-list");t.innerHTML="";(d.ports||[]).forEach(p=>{const port=p.address.split(":").pop();t.innerHTML+="<tr><td>"+p.name+"</td><td>"+port+"</td><td>"+p.module+"</td><td><button class='btn btn-del' onclick='delPort(\""+p.name+"\")'>Remove</button></td></tr>"});})}
function addPort(){const h=document.getElementById("p-host").value,n=document.getElementById("p-name").value.trim(),p=document.getElementById("p-port").value.trim(),m=document.getElementById("p-module").value,st=document.getElementById("p-status");if(!h||!n||!p){st.textContent="Fill all";return}fetch(A+"/ports/"+encodeURIComponent(h),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:n,port:p,module:m})}).then(r=>r.json()).then(d=>{st.textContent=d.message||d.error;loadPorts();document.getElementById("p-name").value="";document.getElementById("p-port").value="";})}
function delPort(n){const h=document.getElementById("p-host").value;fetch(A+"/ports/"+encodeURIComponent(h)+"/"+encodeURIComponent(n),{method:"DELETE"}).then(r=>r.json()).then(()=>loadPorts())}

document.getElementById("edit-host").addEventListener("change",function(){selectNode(this.value)});
loadTree();
</script></body></html>'''

# ---- HTTP Handler ------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _j(self, code, data):
        b = json.dumps(data).encode()
        self.send_response(code)
        for h, v in [("Content-Type","application/json"),("Access-Control-Allow-Origin","*"),("Access-Control-Allow-Methods","GET,POST,PUT,DELETE,OPTIONS"),("Access-Control-Allow-Headers","Content-Type"),("Content-Length",str(len(b)))]:
            self.send_header(h, v)
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        l = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(l)) if l else {}

    def do_OPTIONS(self): self._j(200, {})

    def do_GET(self):
        # Split path from query string
        full = self.path
        p = full.split("?")[0].rstrip("/") or "/"

        if p == "/":
            b = HTML.replace("HIDE_TAB_SCRIPT", "").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/nodes-only":
            # Get host from query param
            qs = full.split("?")[1] if "?" in full else ""
            host_param = ""
            for part in qs.split("&"):
                if part.startswith("host="):
                    host_param = unquote(part[5:])
            b = NODES_HTML.replace("HOST_PARAM", host_param).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/ports-only":
            b = PORTS_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif p == "/health":
            self._j(200, {"status": "ok"})
        elif p == "/nodes":
            # Support filtering by client and account query params
            qs = full.split("?")[1] if "?" in full else ""
            filter_client = ""
            filter_account = ""
            for part in qs.split("&"):
                if part.startswith("client="):
                    filter_client = unquote(part[7:])
                elif part.startswith("account="):
                    filter_account = unquote(part[8:])
            nodes = load_nodes()
            if filter_client:
                nodes = [n for n in nodes if n.get("client","") == filter_client]
            if filter_account:
                nodes = [n for n in nodes if n.get("account","") == filter_account]
            self._j(200, {"nodes": nodes})
        elif p == "/hosts":
            nodes = load_nodes()
            hosts = [{"host": n["hostname"], "count": len(load_ports(n["hostname"]))} for n in nodes]
            self._j(200, {"hosts": hosts})
        elif p == "/hosts-list":
            qs = full.split("?")[1] if "?" in full else ""
            filter_client = ""
            filter_account = ""
            for part in qs.split("&"):
                if part.startswith("client="):
                    filter_client = unquote(part[7:])
                elif part.startswith("account="):
                    filter_account = unquote(part[8:])
            # Ignore wildcard/empty filters
            if filter_client in ("", ".*", "$__all", "All"):
                filter_client = ""
            if filter_account in ("", ".*", "$__all", "All"):
                filter_account = ""
            nodes = load_nodes()
            if filter_client:
                nodes = [n for n in nodes if n.get("client","") == filter_client]
            if filter_account:
                nodes = [n for n in nodes if n.get("account","") == filter_account]
            result = []
            for n in nodes:
                name = n.get("name") or n["hostname"]
                ip = n.get("ip", "")
                display = f"{name} ({ip})" if ip else name
                result.append({"__text": display, "__value": n["hostname"]})
            self._j(200, result)
        elif p == "/clients-list":
            nodes = load_nodes()
            clients = sorted(set(n.get("client","") for n in nodes if n.get("client")))
            self._j(200, [{"__text": c, "__value": c} for c in clients])
        elif p == "/accounts-list":
            qs = full.split("?")[1] if "?" in full else ""
            filter_client = ""
            for part in qs.split("&"):
                if part.startswith("client="):
                    filter_client = unquote(part[7:])
            if filter_client in ("", ".*", "$__all", "All"):
                filter_client = ""
            nodes = load_nodes()
            if filter_client:
                nodes = [n for n in nodes if n.get("client","") == filter_client]
            accounts = sorted(set(n.get("account","") for n in nodes if n.get("account")))
            self._j(200, [{"__text": a, "__value": a} for a in accounts])
        elif p.startswith("/targets/"):
            self._j(200, load_ports(unquote(p[9:])))
        elif p.startswith("/metadata/"):
            host = unquote(p[10:])
            nodes = load_nodes()
            node = find_node(nodes, host) or {}
            self._j(200, {"account": node.get("account", ""), "name": node.get("name", ""), "ip": node.get("ip", "")})
        elif p.startswith("/ports/"):
            host = unquote(p[7:])
            self._j(200, {"host": host, "ports": load_ports(host), "count": len(load_ports(host))})
        else:
            self._j(404, {"error": "not found"})

    def do_POST(self):
        parts = self.path.rstrip("/").split("/")
        if parts == ["", "nodes", "register"]:
            # Auto-register from install script
            d = self._body()
            hostname = d.get("hostname", "").strip()
            ip = d.get("ip", "").strip()
            if not hostname:
                self._j(400, {"error": "need hostname"})
                return
            nodes = load_nodes()
            existing = find_node(nodes, hostname)
            if existing:
                existing["ip"] = ip or existing.get("ip", "")
                if d.get("account"): existing["account"] = d["account"]
                if d.get("name"): existing["name"] = d["name"]
                existing["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            else:
                nodes.append({"hostname": hostname, "ip": ip, "account": d.get("account",""), "name": d.get("name",""), "registered": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "last_seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            save_nodes(nodes)
            self._j(201, {"message": f"Registered {hostname}"})
        elif len(parts) == 3 and parts[1] == "ports":
            host = unquote(parts[2])
            d = self._body()
            name = d.get("name", "").strip()
            port = str(d.get("port", "")).strip()
            addr = d.get("address", "").strip()
            module = d.get("module", "tcp_connect")
            if not addr and port:
                addr = f"localhost:{port}"
            if not addr:
                self._j(400, {"error": "need port"})
                return
            if not name:
                name = f"port_{addr.split(':')[-1]}"
            t = load_ports(host)
            if any(x["name"] == name for x in t):
                self._j(409, {"error": f"'{name}' exists"})
                return
            t.append({"name": name, "address": addr, "module": module})
            save_ports(host, t)
            self._j(201, {"message": f"Added '{name}' on {host}"})
        else:
            self._j(404, {"error": "not found"})

    def do_PUT(self):
        parts = self.path.rstrip("/").split("/")
        if len(parts) == 3 and parts[1] == "nodes":
            host = unquote(parts[2])
            d = self._body()
            nodes = load_nodes()
            existing = find_node(nodes, host)
            if not existing:
                # Create new
                existing = {"hostname": host, "registered": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                nodes.append(existing)
            if "ip" in d: existing["ip"] = d["ip"]
            if "account" in d: existing["account"] = d["account"]
            if "name" in d: existing["name"] = d["name"]
            if "client" in d: existing["client"] = d["client"]
            if "hostname" in d and d["hostname"] != host:
                existing["hostname"] = d["hostname"]
            save_nodes(nodes)
            self._j(200, {"message": f"Saved {existing['hostname']}"})
        else:
            self._j(404, {"error": "not found"})

    def do_DELETE(self):
        parts = self.path.rstrip("/").split("/")
        if len(parts) == 3 and parts[1] == "nodes":
            host = unquote(parts[2])
            nodes = load_nodes()
            nodes = [n for n in nodes if n["hostname"] != host]
            save_nodes(nodes)
            # Also remove port file
            pf = get_port_file(host)
            if os.path.exists(pf):
                os.remove(pf)
            self._j(200, {"message": f"Removed {host}"})
        elif len(parts) == 4 and parts[1] == "ports":
            host = unquote(parts[2])
            name = unquote(parts[3])
            t = load_ports(host)
            t2 = [x for x in t if x["name"] != name]
            if len(t2) == len(t):
                self._j(404, {"error": "not found"})
                return
            save_ports(host, t2)
            self._j(200, {"message": f"Removed '{name}'"})
        else:
            self._j(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "ports"), exist_ok=True)
    s = HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"Port & Node Monitor on :{LISTEN_PORT}")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
