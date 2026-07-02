#!/usr/bin/env python3
"""One-time renames for customer/environment terminology in dashboard JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboards"

REPLACEMENTS = [
    ('"name": "client"', '"name": "customer"'),
    ('"name": "account"', '"name": "environment"'),
    ('"label": "Client"', '"label": "Customer"'),
    ('"label": "Account"', '"label": "Environment"'),
    ('var-client', 'var-customer'),
    ('var-account', 'var-environment'),
    ('${client}', '${customer}'),
    ('${account}', '${environment}'),
    ('/client-accounts', '/customer-environments'),
    ('/client-hosts', '/customer-hosts'),
    ('/clients-list', '/customers-list'),
    ('/accounts-list', '/environments-list'),
    ('monitor_node_info{client=', 'monitor_node_info{customer='),
    ('monitor_kuma_site_info{client=', 'monitor_kuma_site_info{customer='),
    ('client=~"$client"', 'customer=~"$customer"'),
    ('account=~"$account"', 'environment=~"$environment"'),
    ('client=~\\"$client\\"', 'customer=~\\"$customer\\"'),
    ('account=~\\"$account\\"', 'environment=~\\"$environment\\"'),
    (',client"', ',customer"'),
    (',account"', ',environment"'),
    ('"client"', '"customer"'),
    ('"account"', '"environment"'),
    ('client-', 'customer-'),
    ('Client - ', 'Customer - '),
    ('label_values({__name__=~\\"monitor_node_info|monitor_kuma_site_info\\"}, client)',
     'label_values({__name__=~\\"monitor_node_info|monitor_kuma_site_info\\"}, customer)'),
    ('label_values({__name__=~\\"monitor_node_info|monitor_kuma_site_info\\",client=~\\"$client\\"}, account)',
     'label_values({__name__=~\\"monitor_node_info|monitor_kuma_site_info\\",customer=~\\"$customer\\"}, environment)'),
]

# Fix over-replacement in URLs that should stay as english words
FIXUPS = [
    ('Customer Orgs', 'Customer Orgs'),
]


def main():
    for path in sorted(DASH.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        original = text
        for old, new in REPLACEMENTS:
            text = text.replace(old, new)
        # PromQL label selectors
        text = re.sub(r'\bclient="', 'customer="', text)
        text = re.sub(r'\baccount="', 'environment="', text)
        text = re.sub(r'group_left\(ip,name,client,account\)', 'group_left(ip,name,customer,environment)', text)
        text = re.sub(r'group_left\(site,name,client,account\)', 'group_left(site,name,customer,environment)', text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            print("updated", path.name)


if __name__ == "__main__":
    main()
