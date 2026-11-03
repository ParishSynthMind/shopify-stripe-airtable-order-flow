"""Creates the Orders table (all fields) in an existing Airtable base.

    python scripts/setup_airtable.py --list-bases
    python scripts/setup_airtable.py            # uses AIRTABLE_BASE_ID

Airtable's API can create tables in any base the token can see, but creating
a whole new base needs a workspace id, so make one blank base by hand first.
"""
import argparse

from _env import settings
from order_flow.airtable import AirtableClient

p = argparse.ArgumentParser()
p.add_argument("--list-bases", action="store_true")
args = p.parse_args()
s = settings()
c = AirtableClient(s.airtable_token, s.airtable_base_id, s.airtable_table)
if args.list_bases:
    for b in c.list_bases():
        print(b["id"], b["name"], b.get("permissionLevel"))
    raise SystemExit
existing = {t["name"]: t for t in c.list_tables()}
if s.airtable_table in existing:
    print(f"Table '{s.airtable_table}' already exists ({existing[s.airtable_table]['id']}), nothing to do.")
else:
    t = c.create_table()
    print(f"Created table '{t['name']}' ({t['id']}) with {len(t['fields'])} fields.")
