"""Proves the failure path against the real accounts: points the service at a
table that does not exist, delivers a signed Shopify sample, checks the event
is kept as failed and an alert is sent, then "fixes" the table and replays.

    python scripts/failure_drill.py
"""
import logging
import os

from _env import ROOT, settings
from order_flow.server import build_app
from order_flow.signatures import shopify_signature

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
s = settings()
real_table = s.airtable_table
s.db_path = "data/failure-drill.sqlite3"
if os.path.exists(s.db_path):
    os.remove(s.db_path)
s.airtable_table = real_table + "_DOES_NOT_EXIST"
app = build_app(s)
app.sync = True
body = (ROOT / "samples" / "shopify_orders_paid_risky.json").read_bytes()
h = {"X-Shopify-Topic": "orders/paid", "X-Shopify-Shop-Domain": s.shopify_shop, "X-Shopify-Event-Id": "failure-drill-1",
     "X-Shopify-Hmac-Sha256": shopify_signature(s.shopify_webhook_secret, body)}
print("delivery:", app.handle("POST", "/webhooks/shopify", h, body))
print("status after failure:", app.store.status("shopify:failure-drill-1"))
app.pipeline.airtable.table = real_table
print("replay:", [(r["status"], r.get("record_id")) for r in app.replay_failed()])
print("status after replay:", app.store.status("shopify:failure-drill-1"))
