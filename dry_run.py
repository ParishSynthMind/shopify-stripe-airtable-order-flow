"""Whole flow on the sample payloads with no accounts at all.

    python dry_run.py --offline   # rule check instead of Claude, fake Airtable, email printed
    python dry_run.py             # same, but a real Claude call (needs ANTHROPIC_API_KEY)

Klaviyo runs in dry-run mode: the profile and event calls are built, checked
and printed, and nothing is sent.

Each sample is signed the way Shopify or Stripe would sign it, verified,
processed, and then delivered a second time to show the retry guard.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from order_flow.ai import AnthropicReviewer, RulesReviewer  # noqa: E402
from order_flow.alerts import Alerter  # noqa: E402
from order_flow.app import WebhookApp  # noqa: E402
from order_flow.config import Settings  # noqa: E402
from order_flow.klaviyo import KlaviyoClient, KlaviyoSync  # noqa: E402
from order_flow.notifiers import ConsoleNotifier  # noqa: E402
from order_flow.pipeline import OrderPipeline  # noqa: E402
from order_flow.signatures import shopify_signature, stripe_header  # noqa: E402
from order_flow.store import EventStore  # noqa: E402


class MemoryAirtable:
    def __init__(self):
        self.rows = {}

    def upsert_order(self, fields):
        key = fields["Order Key"]
        created = key not in self.rows
        self.rows.setdefault(key, {"id": f"recDRY{len(self.rows) + 1:03d}", "fields": {}})["fields"].update(fields)
        return {"record_id": self.rows[key]["id"], "created": created}


class SampleStripe:
    def get_charge(self, charge_id):
        return json.loads((ROOT / "samples" / "stripe_charge_normal.json").read_text())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--offline", action="store_true")
    args = p.parse_args()
    s = Settings(shopify_webhook_secret="dry-run-shopify-secret", stripe_webhook_secret="whsec_dry_run",
                 subject_prefix="[DRY RUN] ")
    store, airtable, notifier = EventStore(), MemoryAirtable(), ConsoleNotifier()
    reviewer = RulesReviewer() if args.offline else AnthropicReviewer()
    klaviyo = KlaviyoClient("", dry_run=True)
    pipe = OrderPipeline(reviewer, airtable, notifier, Alerter(), store, subject_prefix=s.subject_prefix,
                         klaviyo=KlaviyoSync(klaviyo, store))
    app = WebhookApp(s, pipe, store, stripe=SampleStripe(), sync=True)

    deliveries = []
    for name in ("shopify_orders_paid.json", "shopify_orders_paid_risky.json"):
        body = (ROOT / "samples" / name).read_bytes()
        h = {"X-Shopify-Topic": "orders/paid", "X-Shopify-Event-Id": name,
             "X-Shopify-Hmac-Sha256": shopify_signature(s.shopify_webhook_secret, body)}
        deliveries.append(("/webhooks/shopify", h, body))
    body = (ROOT / "samples" / "stripe_payment_intent_succeeded.json").read_bytes()
    deliveries.append(("/webhooks/stripe", {"Stripe-Signature": stripe_header(s.stripe_webhook_secret, body)}, body))
    body = (ROOT / "samples" / "stripe_checkout_session_expired.json").read_bytes()
    deliveries.append(("/webhooks/stripe", {"Stripe-Signature": stripe_header(s.stripe_webhook_secret, body)}, body))

    for path, h, body in deliveries:
        print(f">>> POST {path}: {app.handle('POST', path, h, body)}")
    print(">>> retry of the first delivery:", app.handle("POST", *deliveries[0]))
    bad = dict(deliveries[0][1], **{"X-Shopify-Hmac-Sha256": "forged"})
    print(">>> forged signature:", app.handle("POST", deliveries[0][0], bad, deliveries[0][2]))
    print(f"\nAirtable rows: {len(airtable.rows)}, emails sent: {len(notifier.sent)}")
    for row in airtable.rows.values():
        f = row["fields"]
        print(f"  {row['id']}  {f['Order']:<9} {f['Total']:>8.2f} {f['Currency']}  risk={f['Risk']:<6} "
              f"flagged={f['Flagged']}  by={f['Reviewed By']}")
    print(f"\nKlaviyo calls built (dry run, not sent): {len(klaviyo.dry_run_log)}")
    for call in klaviyo.dry_run_log:
        a = call["body"]["data"]["attributes"]
        if call["path"] == "/events":
            print(f"  POST /events   {a['metric']['data']['attributes']['name']:<16} value={a['value']:>8.2f} "
                  f"{a['value_currency']}  unique_id={a['unique_id']}")
        else:
            print(f"  POST /profile-import  Order Count={a['properties']['Order Count']}  "
                  f"Last Order ID={a['properties']['Last Order ID']}")


if __name__ == "__main__":
    main()
