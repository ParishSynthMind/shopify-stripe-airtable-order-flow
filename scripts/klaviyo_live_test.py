"""Live Klaviyo test through the real order flow, Stripe in test mode.

    python scripts/klaviyo_live_test.py --public-url https://<tunnel> --emails a@x,b@x,c@x,d@x
    python scripts/klaviyo_live_test.py --public-url https://<tunnel> --emails e@x --flow-check

Use test inboxes you own (plus-addressing works well). The addresses are
masked as "test inbox N" in the results file, which is safe to commit.

Default run:
1. Starts the service (Airtable + Klaviyo on), creates a TEMPORARY Stripe
   webhook endpoint (secret in memory only).
2. Four real test-mode PaymentIntents: inboxes 1, 2, 3, then inbox 1 again,
   so inbox 1's profile should show Order Count 2.
3. A Shopify orders/paid sample for inbox 4, signed with the app secret and
   posted through the public URL.
4. A real Stripe Checkout session for inbox 2, expired through the API, so
   Stripe delivers checkout.session.expired -> 'Started Checkout'.
5. Asks Stripe to redeliver the first payment (local guard skips it), then
   sends the first order's Placed Order straight to Klaviyo twice more,
   bypassing the local guard, to prove Klaviyo's unique_id drops repeats.
6. Reads the profiles and events back from Klaviyo and writes
   live-run/klaviyo-results.json.

--flow-check: one more paid order for the given inbox only, after a Klaviyo
flow on 'Placed Order' has been switched on, to see the flow email arrive.
"""
import argparse
import json
import logging
import os
import threading
import time
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

from _env import ROOT, settings
from live_test import wait_for
from order_flow import normalize
from order_flow.app import STRIPE_ABANDONED, STRIPE_EVENTS
from order_flow.klaviyo import PLACED_ORDER, STARTED_CHECKOUT, KlaviyoClient
from order_flow.server import build_app, make_handler
from order_flow.signatures import shopify_signature
from order_flow.stripe_client import API, StripeClient

ITEMS = {
    "A": [{"title": "Cold Brew Kit (TEST)", "quantity": 1, "unit_price": 45.0}],
    "B": [{"title": "Pour Over Set (TEST)", "quantity": 1, "unit_price": 60.0},
          {"title": "Filter Papers x100 (TEST)", "quantity": 2, "unit_price": 6.5}],
    "C": [{"title": "Espresso Grinder (TEST)", "quantity": 1, "unit_price": 129.0}],
    "D": [{"title": "Coffee Beans 1kg (TEST)", "quantity": 2, "unit_price": 24.0}],
    "F": [{"title": "Travel Mug (TEST)", "quantity": 1, "unit_price": 19.0}],
}


def pay(stripe, label, inbox, email, name):
    items = ITEMS[label]
    amount = int(round(sum(i["quantity"] * i["unit_price"] for i in items) * 100))
    meta = {"order_name": f"KLV-TEST-{label}-{int(time.time()) % 100000}", "items": json.dumps(items),
            "note": "Klaviyo live test, fake order"}
    pi = stripe.create_test_payment(amount, "usd", "pm_card_visa", meta, email,
                                    {"name": name, "line1": "1 Test Street", "city": "Dubai", "country": "AE"},
                                    idempotency_key=str(uuid.uuid4()))
    print(f"[{label}] {inbox}: PaymentIntent {pi['id']} {pi['status']} {amount / 100:.2f} USD")
    return pi


def abandoned_checkout(stripe, email):
    form = {"mode": "payment", "success_url": "https://example.com/thanks", "customer_email": email,
            "line_items[0][price_data][currency]": "usd",
            "line_items[0][price_data][product_data][name]": "Cold Brew Kit (TEST)",
            "line_items[0][price_data][unit_amount]": 4500, "line_items[0][quantity]": 1,
            "metadata[order_name]": "KLV-TEST-CART", "metadata[items]": json.dumps(ITEMS["A"]),
            "after_expiration[recovery][enabled]": "true"}
    session = stripe._send("Stripe", "POST", f"{API}/checkout/sessions", stripe._headers(), form=form)
    stripe._send("Stripe", "POST", f"{API}/checkout/sessions/{session['id']}/expire", stripe._headers())
    print(f"[cart] Checkout session {session['id']} created and expired")
    return session


def read_back(k, email, order_keys, timeout=150):
    """Polls Klaviyo until every expected order shows up (events land a few seconds after the 202)."""
    end = time.time() + timeout
    while True:
        profile = k.find_profile(email)
        found = {}
        if profile:
            for metric in (PLACED_ORDER, STARTED_CHECKOUT):
                mid = k.metric_id(metric)
                for e in (k.profile_events(profile["id"], mid) if mid else []):
                    p = e["attributes"]["event_properties"]
                    found.setdefault(p.get("OrderKey"), []).append(
                        {"metric": metric, "event_id": e["id"], "OrderId": p.get("OrderId"),
                         "value": p.get("$value"), "items": p.get("ItemNames"), "datetime": e["attributes"]["datetime"],
                         "consent_recorded": p.get("StripePromotionsConsent")})
        if (profile and all(key in found for key in order_keys)) or time.time() > end:
            return profile, found
        time.sleep(5)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--public-url", required=True)
    p.add_argument("--emails", required=True, help="comma separated test inboxes you own")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--flow-check", action="store_true")
    p.add_argument("--results", default=None)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    emails = [e.strip() for e in args.emails.split(",") if e.strip()]
    inbox = {e: f"test inbox {i + 1}" for i, e in enumerate(emails)}
    results_path = args.results or str(ROOT / "live-run" / ("klaviyo-flow-check.json" if args.flow_check
                                                            else "klaviyo-results.json"))

    s = settings()
    if not s.stripe_secret_key.startswith("sk_test_"):
        raise SystemExit("STRIPE_SECRET_KEY must be a test key (sk_test_...)")
    if not s.klaviyo_api_key:
        raise SystemExit("KLAVIYO_PRIVATE_API_KEY is not set")
    s.port = args.port
    stripe = StripeClient(s.stripe_secret_key)
    endpoint = stripe.create_webhook_endpoint(args.public_url.rstrip("/") + "/webhooks/stripe",
                                              sorted(STRIPE_EVENTS | {STRIPE_ABANDONED}))
    s.stripe_webhook_secret = endpoint["secret"]   # memory only
    out = {"date": time.strftime("%Y-%m-%d %H:%M:%S %Z"), "klaviyo_revision": s.klaviyo_revision,
           "stripe_endpoint": endpoint["id"], "orders": [], "profiles": []}
    try:
        app = build_app(s)
        app.start_worker()
        server = ThreadingHTTPServer(("127.0.0.1", s.port), make_handler(app))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        print(f"service up on :{s.port}, Stripe endpoint {endpoint['id']}")

        plan = [("F", emails[0])] if args.flow_check else \
            [("A", emails[0]), ("B", emails[1]), ("C", emails[2]), ("D", emails[0])]
        keys, expected, pis = [], {}, []
        for n, (label, email) in enumerate(plan):
            pi = pay(stripe, label, inbox[email], email, f"Test OrderFlow-{label}")
            pis.append(pi)
            keys.append(f"stripe:{pi['id']}")
            expected.setdefault(email, []).append(f"stripe:{pi['id']}")
            if n == 0 and not args.flow_check:
                time.sleep(4)   # let order A land first, so the count on inbox 1 goes 1 then 2

        if not args.flow_check:
            payload = json.loads((ROOT / "samples" / "shopify_orders_paid.json").read_text())
            payload["id"] = int(time.time() * 1000)          # a fresh order id for this run
            payload["email"] = emails[3]
            payload["customer"]["email"] = emails[3]
            payload["customer"]["first_name"], payload["customer"]["last_name"] = "Test", "OrderFlow-Shopify"
            body = json.dumps(payload).encode()
            req = urllib.request.Request(
                args.public_url.rstrip("/") + "/webhooks/shopify", data=body, method="POST",
                headers={"Content-Type": "application/json", "X-Shopify-Topic": "orders/paid",
                         "X-Shopify-Shop-Domain": s.shopify_shop, "X-Shopify-Event-Id": str(uuid.uuid4()),
                         "X-Shopify-Hmac-Sha256": shopify_signature(s.shopify_webhook_secret, body),
                         "User-Agent": "order-flow-live-test"})
            with urllib.request.urlopen(req, timeout=15) as r:
                print("[shopify, signed sample] ->", r.status, r.read().decode())
            keys.append(f"shopify:{payload['id']}")
            expected.setdefault(emails[3], []).append(f"shopify:{payload['id']}")

            session = abandoned_checkout(stripe, emails[1])
            cart_key = f"stripe:{session['id']}"
            expected[emails[1]].append(cart_key)

        rows = wait_for(app.store, keys, timeout=120)
        print("orders processed:", [(r[0], r[1]) for r in rows])
        if not args.flow_check:
            end = time.time() + 120
            while time.time() < end and not app.store._db.execute(
                    "SELECT 1 FROM klaviyo_sync WHERE kind='checkout'").fetchone():
                time.sleep(3)

            # Retry proof 1: Stripe redelivers the first payment; the local guard skips it.
            events = stripe._send("Stripe", "GET", f"{API}/events?type=payment_intent.succeeded&limit=20",
                                  stripe._headers())["data"]
            evt = next(e for e in events if e["data"]["object"]["id"] == pis[0]["id"])
            stripe._send("Stripe", "POST", f"{API}/events/{evt['id']}/retry", stripe._headers(),
                         form={"webhook_endpoint": endpoint["id"]})
            time.sleep(12)
            out["stripe_redelivery"] = app.store._db.execute(
                "SELECT event_id, status FROM events WHERE order_key=?", (keys[0],)).fetchall()
            # Retry proof 2: the same order straight to Klaviyo twice more, no local guard in the way.
            order_a = normalize.from_stripe_payment_intent(pis[0])
            for _ in range(2):
                app.pipeline.klaviyo.placed_order(order_a)
            out["direct_resends_of_first_order"] = 2
        out["klaviyo_sync_rows"] = [dict(zip(("event_id", "order_key", "kind", "status", "detail"), r)) for r in
                                    app.store._db.execute("SELECT event_id, order_key, kind, status, detail "
                                                          "FROM klaviyo_sync ORDER BY updated_at").fetchall()]
        server.shutdown()
    finally:
        stripe.delete_webhook_endpoint(endpoint["id"])
        print("temporary Stripe endpoint deleted")

    k = KlaviyoClient(s.klaviyo_api_key, s.klaviyo_revision)
    time.sleep(10)
    for email, order_keys in expected.items():
        profile, found = read_back(k, email, order_keys)
        _, full = k.call("GET", f"/profiles/{profile['id']}?additional-fields%5Bprofile%5D=subscriptions") \
            if profile else (0, {})
        attrs = (full.get("data") or {}).get("attributes") or {}
        out["profiles"].append({
            "inbox": inbox[email], "profile_id": profile["id"] if profile else None,
            "name": f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip(),
            "properties": attrs.get("properties"),
            "email_marketing_consent": ((attrs.get("subscriptions") or {}).get("email") or {})
            .get("marketing", {}).get("consent"),
            "events_per_order": {key: found.get(key, []) for key in order_keys},
        })
    out["first_order_key"] = None if args.flow_check else keys[0]
    os.makedirs(os.path.dirname(results_path), exist_ok=True)
    text = json.dumps(out, indent=2, default=str)
    for email, label in inbox.items():
        text = text.replace(email, f"<{label}>")
    with open(results_path, "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()
