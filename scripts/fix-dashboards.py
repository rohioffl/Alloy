#!/usr/bin/env python3
"""Normalize dashboard placeholders, templating, and nav links."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "dashboards"

STANDARD_TEMPLATING = [
    {
        "allValue": ".*",
        "current": {"text": "All", "value": "$__all"},
        "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "__INFINITY_DS_UID__"},
        "includeAll": True,
        "label": "Client",
        "name": "client",
        "query": {
            "infinityQuery": {
                "refId": "variable",
                "source": "url",
                "type": "json",
                "url": "/api/v1/variables/clients",
            },
            "queryType": "infinity",
            "type": "infinity",
        },
        "refresh": 1,
        "type": "query",
    },
    {
        "allValue": ".*",
        "current": {"text": "All", "value": "$__all"},
        "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "__INFINITY_DS_UID__"},
        "includeAll": True,
        "label": "Account",
        "name": "account",
        "query": {
            "infinityQuery": {
                "refId": "variable",
                "source": "url",
                "type": "json",
                "url": "/api/v1/variables/accounts?client=${client}",
            },
            "queryType": "infinity",
            "type": "infinity",
        },
        "refresh": 1,
        "type": "query",
    },
    {
        "datasource": {"type": "yesoreyeram-infinity-datasource", "uid": "__INFINITY_DS_UID__"},
        "includeAll": False,
        "label": "Host",
        "name": "host",
        "query": {
            "infinityQuery": {
                "refId": "variable",
                "source": "url",
                "type": "json",
                "url": "/api/v1/variables/hosts?client=${client}&account=${account}",
            },
            "queryType": "infinity",
            "type": "infinity",
        },
        "refresh": 1,
        "sort": 0,
        "type": "query",
    },
]

HOST_ONLY = {"cpu.json", "memory.json", "disk.json", "network.json", "processes.json"}


def patch_prom_uid(obj) -> None:
    if isinstance(obj, dict):
        if obj.get("type") == "prometheus" and "uid" in obj:
            obj["uid"] = "__PROM_DS_UID__"
        for v in obj.values():
            patch_prom_uid(v)
    elif isinstance(obj, list):
        for i in obj:
            patch_prom_uid(i)


def fix_summary_ports(dash: dict) -> None:
    for panel in dash.get("panels", []):
        for t in panel.get("targets") or []:
            expr = t.get("expr", "")
            if "probe_success" in expr and 'job="blackbox"' not in expr:
                t["expr"] = expr.replace(
                    'probe_success{host="$host"}',
                    'probe_success{host="$host",job="blackbox"}',
                )
        for tr in panel.get("transformations") or []:
            if tr.get("id") != "organize":
                continue
            opts = tr.setdefault("options", {})
            rn = opts.setdefault("renameByName", {})
            if rn.pop("job", None) == "Port" or "port" not in rn:
                rn["port"] = "Port"
            opts.setdefault("excludeByName", {})["job"] = True


def main():
    for path in sorted(ROOT.glob("*.json")):
        raw = path.read_text()
        data = json.loads(raw)
        dash = data["dashboard"]
        patch_prom_uid(dash)

        if path.name == "summary.json":
            fix_summary_ports(dash)
        if path.name in HOST_ONLY:
            dash["templating"] = {"list": list(STANDARD_TEMPLATING)}

        compact = path.name != "summary.json"
        text = json.dumps(data, indent=2 if not compact else None, separators=(",", ":") if compact else None)
        text = text.replace(
            "var-host=${host}&${__url_time_range}",
            "var-client=${client}&var-account=${account}&var-host=${host}&${__url_time_range}",
        )
        path.write_text(text + ("\n" if path.name == "summary.json" else ""))
        print("ok", path.name)


if __name__ == "__main__":
    main()
