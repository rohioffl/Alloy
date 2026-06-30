"""HTML/JS template loading + placeholder substitution.

Templates live as standalone .html/.js files next to this module. The original
implementation embedded the same markup as Python string constants; behaviour
is preserved exactly:
  __SCRIPT__   -> "const API=window.location.origin;"
  __COMBO_JS__ -> contents of combo.js
  HOST_PARAM   -> JSON-encoded host (nodes template only)
"""

import json
import os

_UI_DIR = os.path.dirname(__file__)


def _read(name):
    with open(os.path.join(_UI_DIR, name), encoding="utf-8") as f:
        return f.read()


_COMBO_JS = _read("combo.js")
# When the UI is embedded at /monitor-api/inventory (nginx → :9099), fetches must use
# that prefix — window.location.origin alone sends /api/v1/* to Grafana (404 / sync error).
_API_BASE_SCRIPT = """const API=(function(){
  var o=window.location.origin,p=window.location.pathname;
  if(p.indexOf('/monitor-api/')===0||p==='/monitor-api')return o+'/monitor-api';
  return o;
})();"""


def render(name, host=None):
    """Load a template by file name and apply the standard substitutions."""
    content = _read(name)
    if host is not None:
        content = content.replace("HOST_PARAM", json.dumps(host))
    return (
        content
        .replace("__SCRIPT__", _API_BASE_SCRIPT)
        .replace("__COMBO_JS__", _COMBO_JS)
    )
