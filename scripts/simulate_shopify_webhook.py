"""Sends a sample orders/paid payload to the service, signed exactly the way
Shopify signs it (base64 HMAC-SHA256 of the raw body with the app secret).
Useful before the real subscription exists, and for replaying fixtures.

    python scripts/simulate_shopify_webhook.py samples/shopify_orders_paid.json --url http://127.0.0.1:8000/webhooks/shopify
"""
import argparse
import json
import urllib.request
import uuid

from _env import settings
from order_flow.signatures import shopify_signature

p = argparse.ArgumentParser()
p.add_argument("payload")
p.add_argument("--url", default="http://127.0.0.1:8000/webhooks/shopify")
p.add_argument("--event-id", default=None)
args = p.parse_args()
s = settings()
body = open(args.payload, "rb").read()
req = urllib.request.Request(args.url, data=body, method="POST", headers={
    "Content-Type": "application/json",
    "X-Shopify-Topic": "orders/paid",
    "X-Shopify-Shop-Domain": s.shopify_shop,
    "X-Shopify-Event-Id": args.event_id or str(uuid.uuid4()),
    "X-Shopify-Hmac-Sha256": shopify_signature(s.shopify_webhook_secret, body),
})
with urllib.request.urlopen(req, timeout=10) as r:
    print(r.status, json.loads(r.read()))
