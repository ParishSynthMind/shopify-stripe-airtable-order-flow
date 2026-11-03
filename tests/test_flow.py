"""End to end through the webhook app with every outside service faked."""
import json
import time

from conftest import FakeAirtable, FakeReviewer, FakeStripe, sample
from order_flow.signatures import shopify_signature, stripe_header


def shopify_headers(body, secret="shpss_test_secret", event_id="evt-1", topic="orders/paid"):
    return {"X-Shopify-Hmac-Sha256": shopify_signature(secret, body), "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": "demo.myshopify.com", "X-Shopify-Event-Id": event_id}


def stripe_headers(body, secret="whsec_test_secret", ts=None):
    return {"Stripe-Signature": stripe_header(secret, body, ts)}


def test_shopify_paid_order_lands_once_and_emails(rig):
    r = rig()
    body = sample("shopify_orders_paid.json")
    status, resp = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert status == 200 and resp["queued"] == "shopify:evt-1"
    row = r.airtable.rows["shopify:820982911946154508"]["fields"]
    assert row["Order"] == "#1001" and row["Risk"] == "Low" and row["AI Note"].startswith("Test note")
    assert len(r.notifier.sent) == 1
    assert r.notifier.sent[0]["subject"].startswith("[TEST] New paid order #1001")
    assert r.store.status("shopify:evt-1")["status"] == "done"


def test_shopify_retry_same_event_is_skipped(rig):
    r = rig()
    body = sample("shopify_orders_paid.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    status, resp = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert status == 200 and resp["skipped"] == "duplicate_event"
    assert r.airtable.calls == 1 and len(r.notifier.sent) == 1 and len(r.reviewer.calls) == 1


def test_second_event_for_same_order_is_skipped(rig):
    r = rig()
    body = sample("shopify_orders_paid.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body, event_id="a"), body)
    _, resp = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body, event_id="b"), body)
    assert resp["skipped"] == "duplicate_order" and len(r.airtable.rows) == 1


def test_shopify_bad_signature_is_401_and_nothing_written(rig):
    r = rig()
    body = sample("shopify_orders_paid.json")
    status, _ = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body, secret="wrong"), body)
    assert status == 401 and r.airtable.calls == 0 and not r.notifier.sent


def test_shopify_other_topic_and_other_shop(rig):
    r = rig()
    body = sample("shopify_orders_paid.json")
    status, resp = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body, topic="orders/create"), body)
    assert status == 200 and resp == {"ignored": "orders/create"}
    h = shopify_headers(body)
    h["X-Shopify-Shop-Domain"] = "someone-else.myshopify.com"
    assert r.app.handle("POST", "/webhooks/shopify", h, body)[0] == 403


def test_risky_order_flagged_in_row_and_subject(rig):
    r = rig()
    body = sample("shopify_orders_paid_risky.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    row = r.airtable.rows["shopify:820982911946154999"]["fields"]
    assert row["Flagged"] is True and row["Risk"] == "High" and "Bulk quantity" in row["Risk Reasons"]
    assert "FLAGGED" in r.notifier.sent[0]["subject"]


def test_stripe_payment_intent_enriched_with_charge(rig):
    charge = json.loads(sample("stripe_charge_normal.json"))
    r = rig(stripe=FakeStripe(charge))
    body = sample("stripe_payment_intent_succeeded.json")
    status, resp = r.app.handle("POST", "/webhooks/stripe", stripe_headers(body), body)
    assert status == 200 and resp["queued"] == "stripe:evt_sample_0001"
    assert r.app.stripe.calls == ["ch_sample_0001"]
    row = r.airtable.rows["stripe:pi_sample_0001"]["fields"]
    assert row["Total"] == 45.0 and row["Source"] == "Stripe" and row["Test"] is True


def test_stripe_checkout_and_payment_intent_for_same_payment_make_one_row(rig):
    r = rig()
    pi_body = sample("stripe_payment_intent_succeeded.json")
    r.app.handle("POST", "/webhooks/stripe", stripe_headers(pi_body), pi_body)
    cs = {"id": "evt_cs_1", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "payment_intent": "pi_sample_0001", "payment_status": "paid",
        "amount_total": 4500, "currency": "usd"}}}
    cs_body = json.dumps(cs).encode()
    _, resp = r.app.handle("POST", "/webhooks/stripe", stripe_headers(cs_body), cs_body)
    assert resp["skipped"] == "duplicate_order" and len(r.airtable.rows) == 1


def test_stripe_unpaid_checkout_and_other_events_ignored(rig):
    r = rig()
    for event in ({"id": "e1", "type": "checkout.session.completed",
                   "data": {"object": {"id": "cs", "payment_status": "unpaid"}}},
                  {"id": "e2", "type": "customer.created", "data": {"object": {}}}):
        body = json.dumps(event).encode()
        status, resp = r.app.handle("POST", "/webhooks/stripe", stripe_headers(body), body)
        assert status == 200 and "ignored" in resp
    assert r.airtable.calls == 0


def test_stripe_bad_and_stale_signatures_rejected(rig):
    r = rig()
    body = sample("stripe_payment_intent_succeeded.json")
    assert r.app.handle("POST", "/webhooks/stripe", stripe_headers(body, secret="whsec_nope"), body)[0] == 400
    stale = stripe_headers(body, ts=int(time.time()) - 3600)
    assert r.app.handle("POST", "/webhooks/stripe", stale, body)[0] == 400
    assert r.airtable.calls == 0


def test_airtable_transient_error_is_retried(rig):
    r = rig(airtable=FakeAirtable(fail_times=2, status=503))
    body = sample("shopify_orders_paid.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert r.airtable.calls == 3 and r.store.status("shopify:evt-1")["status"] == "done"


def test_airtable_down_alerts_keeps_event_and_retry_succeeds(rig):
    r = rig(airtable=FakeAirtable(fail_times=3, status=500))
    body = sample("shopify_orders_paid.json")
    status, _ = r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert status == 200
    st = r.store.status("shopify:evt-1")
    assert st["status"] == "failed" and "500" in st["error"]
    assert r.alerter.sent and "kept for replay" in r.alerter.sent[0][0]
    assert not r.notifier.sent
    assert r.store.failed()[0]["body"] == body
    # Shopify redelivers the same event: a failed event may be claimed again.
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert r.store.status("shopify:evt-1")["status"] == "done" and len(r.airtable.rows) == 1


def test_airtable_permanent_error_not_retried(rig):
    r = rig(airtable=FakeAirtable(fail_times=5, status=422))
    body = sample("shopify_orders_paid.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert r.airtable.calls == 1 and r.store.status("shopify:evt-1")["status"] == "failed"


def test_ai_down_falls_back_to_rules_and_alerts(rig):
    r = rig(reviewer=FakeReviewer(fail=True))
    body = sample("shopify_orders_paid_risky.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    row = r.airtable.rows["shopify:820982911946154999"]["fields"]
    assert row["Reviewed By"] == "rules" and row["Flagged"] is True
    assert any("AI review unavailable" in a[0] for a in r.alerter.sent)


def test_notifier_failure_marks_failed_for_replay(rig):
    r = rig()

    def broken(*a, **k):
        raise RuntimeError("smtp down")
    r.notifier.send = broken
    body = sample("shopify_orders_paid.json")
    r.app.handle("POST", "/webhooks/shopify", shopify_headers(body), body)
    assert r.store.status("shopify:evt-1")["status"] == "failed"
    assert len(r.airtable.rows) == 1  # row is in; a replay upserts the same row


def test_health_and_routing(rig):
    r = rig()
    assert r.app.handle("GET", "/health", {}, b"")[0] == 200
    assert r.app.handle("POST", "/nope", {}, b"{}")[0] == 404
    assert r.app.handle("PUT", "/webhooks/stripe", {}, b"{}")[0] == 405
    assert r.app.handle("POST", "/webhooks/stripe", {}, b"x" * 1_000_001)[0] == 413


def test_replay_failed_events(rig):
    r = rig(airtable=FakeAirtable(fail_times=3, status=500))
    body = sample("stripe_payment_intent_succeeded.json")
    r.app.handle("POST", "/webhooks/stripe", stripe_headers(body), body)
    assert r.store.status("stripe:evt_sample_0001")["status"] == "failed"
    results = r.app.replay_failed()
    assert [x["status"] for x in results] == ["done"] and r.store.failed() == []
    assert len(r.notifier.sent) == 1
