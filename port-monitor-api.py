#!/usr/bin/env python3
"""
Port Monitor API - Manages Alloy probe targets dynamically.

Endpoints:
  GET  /ports              - List monitored ports
  POST /ports              - Add: {"name":"ssh","address":"localhost:22","module":"tcp_connect"}
  DELETE /ports/<name>     - Remove by name
  GET  /                   - Management UI
  GET  /health             - Health check

Listens on :9099
"""

import json
import os
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

TARGETS_FILE = "/etc/alloy/probe-targets.json"
ALLOY_CONFIG = "/etc/alloy/config.alloy"
LISTEN_PORT = 9099

MANAGEMENT_HTML = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Port Monitor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: Inter, system-ui, sans-serif; background: #0b0c0e; color: #e0e0e0; padding: 24px; }
h1 { display: none; }
.form { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; align-items: center; }
input, select { padding: 8px 12px; border-radius: 4px; border: 1px solid #444; background: #1a1a2e; color: #fff; font-size: 13px; }
input:focus { outline: none; border-color: #f59e0b; }
.btn { padding: 8px 16px; border: none; border-radius: 4px; font-weight: 600; cursor: pointer; font-size: 13px; }
.btn-add { background: #22c55e; color: #fff; }
.btn-add:hover { background: #16a34a; }
.btn-del { background: #ef4444; color: #fff; font-size: 11px; padding: 4px 10px; }
.btn-del:hover { background: #dc2626; }
#status { margin-left: 8px; font-size: 12px; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 6px 12px; border-bottom: 1px solid #222; font-size: 13px; }
th { color: #888; font-weight: 500; }
</style></head><body>
<h1>Manage Monitored Ports</h1>
<div class="form">
  <input id="name" placeholder="Name (e.g. redis)" style="width:120px" />
  <input id="port" placeholder="Port (e.g. 8080)" style="width:100px" type="number" />
  <select id="module"><option value="tcp_connect">TCP</option><option value="http_2xx">HTTP</option></select>
  <button class="btn btn-add" onclick="addPort()">+ Add</button>
  <span id="status"></span>
</div>
<table><thead><tr><th>Name</th><th>Port</th><th>Type</th><th></th></tr></thead><tbody id="list"></tbody></table>
<script>
const API = window.location.origin;
function load() {
  fetch(API+"/ports").then(r=>r.json()).then(d=>{
    const tb = document.getElementById("list");
    tb.innerHTML = "";
    d.ports.forEach(p => {
      const port = p.address.split(":").pop();
      const tr = document.createElement("tr");
      tr.innerHTML = "<td>"+p.name+"</td><td>"+port+"</td><td>"+p.module+"</td><td><button class=\\"btn btn-del\\" onclick=\\"del('"+p.name+"')\\">Remove</button></td>";
      tb.appendChild(tr);
    });
  });
}
function addPort() {
  const name = document.getElementById("name").value.trim();
  const port = document.getElementById("port").value.trim();
  const module = document.getElementById("module").value;
  const st = document.getElementById("status");
  if (!port) { st.textContent="Enter port number"; st.style.color="#f59e0b"; return; }
  if (!name) { st.textContent="Enter a name"; st.style.color="#f59e0b"; return; }
  fetch(API+"/ports",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name:name,port:port,module:module})})
    .then(r=>r.json()).then(d=>{
      if(d.error){st.textContent="Error: "+d.error;st.style.color="#ef4444";}
      else{st.textContent=d.message;st.style.color="#22c55e";document.getElementById("name").value="";document.getElementById("port").value="";}
      load();
    }).catch(e=>{st.textContent="Error: "+e;st.style.color="#ef4444";});
}
function del(name) {
  fetch(API+"/ports/"+encodeURIComponent(name),{method:"DELETE"}).then(r=>r.json()).then(d=>{
    document.getElementById("status").textContent=d.message||d.error;
    load();
  });
}
document.getElementById("port").addEventListener("keydown",e=>{if(e.key==="Enter")addPort();});
load();
</script></body></html>'''


def load_targets():
    if os.path.exists(TARGETS_FILE):
        with open(TARGETS_FILE) as f:
            return json.load(f)
    return []


def save_targets(targets):
    os.makedirs(os.path.dirname(TARGETS_FILE), exist_ok=True)
    with open(TARGETS_FILE, "w") as f:
        json.dump(targets, f, indent=2)


def regenerate_alloy_config(targets):
    """Rewrite the blackbox exporter block in Alloy config and restart."""
    if not os.path.exists(ALLOY_CONFIG):
        return False

    with open(ALLOY_CONFIG) as f:
        config = f.read()

    # Build target blocks
    target_blocks = ""
    for t in targets:
        name = t.get("name", t["address"].replace(":", "_").replace(".", "_"))
        addr = t["address"]
        module = t.get("module", "tcp_connect")
        target_blocks += f'  target {{\n    name    = "{name}"\n    address = "{addr}"\n    module  = "{module}"\n  }}\n'

    new_block = (
        'prometheus.exporter.blackbox "endpoints" {\n'
        '  config = "{ modules: { tcp_connect: { prober: tcp, timeout: 5s }, '
        'http_2xx: { prober: http, timeout: 5s, http: { preferred_ip_protocol: ip4, follow_redirects: true } } } }"\n\n'
        + target_blocks +
        '}'
    )

    # Find the block start
    start_marker = 'prometheus.exporter.blackbox "endpoints" {'
    start_idx = config.find(start_marker)

    if start_idx == -1:
        # Block doesn't exist yet - insert before relabel section
        marker = 'prometheus.relabel "add_host_label"'
        ins_idx = config.find(marker)
        if ins_idx == -1:
            return False
        insert_text = (
            '// PORT / ENDPOINT MONITORING\n\n'
            + new_block + '\n\n'
            'prometheus.scrape "blackbox_metrics" {\n'
            '  targets         = prometheus.exporter.blackbox.endpoints.targets\n'
            '  forward_to      = [prometheus.relabel.add_host_label.receiver]\n'
            '  scrape_interval = "15s"\n'
            '}\n\n'
        )
        new_config = config[:ins_idx] + insert_text + config[ins_idx:]
    else:
        # Find matching closing brace by counting
        brace_count = 0
        end_idx = start_idx
        for i in range(start_idx, len(config)):
            if config[i] == '{':
                brace_count += 1
            elif config[i] == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_idx = i + 1
                    break

        new_config = config[:start_idx] + new_block + config[end_idx:]

    with open(ALLOY_CONFIG, "w") as f:
        f.write(new_config)

    # Restart Alloy
    subprocess.run(["systemctl", "restart", "alloy"], capture_output=True)
    return True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._json(200, {})

    def do_GET(self):
        if self.path == "/":
            self._html(MANAGEMENT_HTML)
        elif self.path == "/ports":
            self._json(200, {"ports": load_targets(), "count": len(load_targets())})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/ports":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body) if body else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return

            address = data.get("address", "").strip()
            name = data.get("name", "").strip()
            module = data.get("module", "tcp_connect")

            # Accept just a port number
            port = str(data.get("port", "")).strip()
            if not address and port:
                address = f"localhost:{port}"

            if not address:
                self._json(400, {"error": "provide 'port' number or 'address'"})
                return

            if address.isdigit():
                address = f"localhost:{address}"

            if not name:
                # Auto-name from port number
                port_num = address.split(":")[-1] if ":" in address else address
                name = f"port_{port_num}"

            targets = load_targets()

            for t in targets:
                if t.get("name") == name:
                    self._json(409, {"error": f"name '{name}' already exists"})
                    return
                if t["address"] == address:
                    self._json(409, {"error": f"'{address}' already monitored"})
                    return

            targets.append({"name": name, "address": address, "module": module})
            save_targets(targets)
            regenerate_alloy_config(targets)
            self._json(201, {"message": f"Added '{name}' ({address})", "ports": targets})
        else:
            self._json(404, {"error": "not found"})

    def do_DELETE(self):
        if self.path.startswith("/ports/"):
            identifier = unquote(self.path[7:])
            targets = load_targets()
            original = len(targets)

            targets = [t for t in targets if
                       t.get("name") != identifier and
                       t.get("address") != identifier and
                       not t.get("address", "").endswith(f":{identifier}")]

            if len(targets) == original:
                self._json(404, {"error": f"'{identifier}' not found"})
                return

            save_targets(targets)
            regenerate_alloy_config(targets)
            self._json(200, {"message": f"Removed '{identifier}'", "ports": targets})
        else:
            self._json(404, {"error": "not found"})


if __name__ == "__main__":
    if not os.path.exists(TARGETS_FILE):
        save_targets([])

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), Handler)
    print(f"Port Monitor API on :{LISTEN_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
