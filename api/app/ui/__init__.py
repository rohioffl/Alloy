"""Embedded UI template loader and placeholder substitution."""

import json
from pathlib import Path

_UI_DIR = Path(__file__).resolve().parent


def _api_script() -> str:
    return (
        "const API=(location.pathname.indexOf('/monitor-api')===0?'/monitor-api':'');\n"
    )


def render(template_name: str, **kwargs) -> str:
    path = _UI_DIR / template_name
    text = path.read_text(encoding="utf-8")
    text = text.replace("__SCRIPT__", _api_script())
    combo_path = _UI_DIR / "combo.js"
    if combo_path.exists():
        text = text.replace("__COMBO_JS__", combo_path.read_text(encoding="utf-8"))
    else:
        text = text.replace("__COMBO_JS__", "")
    host = kwargs.get("host", "")
    text = text.replace("HOST_PARAM", json.dumps(host))
    return text
