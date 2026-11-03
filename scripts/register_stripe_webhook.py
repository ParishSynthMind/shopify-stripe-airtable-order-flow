"""Points a Stripe webhook endpoint at this service and saves its signing
secret to .env (the value is never printed).

    python scripts/register_stripe_webhook.py --endpoint https://<public-url>/webhooks/stripe
    python scripts/register_stripe_webhook.py --list
"""
import argparse

from _env import set_env_value, settings
from order_flow.app import STRIPE_EVENTS
from order_flow.stripe_client import StripeClient

p = argparse.ArgumentParser()
p.add_argument("--endpoint")
p.add_argument("--list", action="store_true")
p.add_argument("--delete")
args = p.parse_args()
c = StripeClient(settings().stripe_secret_key)
if args.list:
    for w in c.list_webhook_endpoints():
        print(w["id"], w["url"], w["status"], ",".join(w["enabled_events"]))
elif args.delete:
    print(c.delete_webhook_endpoint(args.delete).get("deleted"))
else:
    w = c.create_webhook_endpoint(args.endpoint, sorted(STRIPE_EVENTS))
    set_env_value("STRIPE_WEBHOOK_SECRET", w["secret"])
    print(f"Created {w['id']} -> {w['url']}; signing secret saved to .env")
