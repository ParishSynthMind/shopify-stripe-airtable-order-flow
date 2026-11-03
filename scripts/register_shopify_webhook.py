"""Subscribes the app to Shopify's orders/paid topic.

    python scripts/register_shopify_webhook.py --endpoint https://<public-url>/webhooks/shopify

Needs SHOPIFY_SHOP, SHOPIFY_CLIENT_ID and SHOPIFY_WEBHOOK_SECRET (the app's
client secret, which is also what Shopify signs webhooks with). Order topics
carry customer data, so the app must have protected customer data access
approved in the Dev Dashboard first; otherwise Shopify refuses the subscription.
"""
import argparse
import os

from _env import settings
from order_flow.shopify_client import ShopifyClient

p = argparse.ArgumentParser()
p.add_argument("--endpoint", required=True)
args = p.parse_args()
s = settings()
c = ShopifyClient(s.shopify_shop, os.environ["SHOPIFY_CLIENT_ID"], s.shopify_webhook_secret)
result = c.subscribe_orders_paid(args.endpoint)
if result["userErrors"]:
    raise SystemExit(f"Shopify refused: {result['userErrors']}")
print("Subscribed:", result["webhookSubscription"])
