import json

from conftest import sample
from order_flow import normalize
from order_flow.risk import rules_review, signals


def test_shopify_order_shape():
    o = normalize.from_shopify(json.loads(sample("shopify_orders_paid.json")))
    assert o.order_key == "shopify:820982911946154508"
    assert o.order_name == "#1001" and o.total == 84.0 and o.currency == "USD"
    assert o.customer_name == "Jane Example" and o.shipping_country == "AE"
    assert [i.quantity for i in o.items] == [1, 2] and o.test is True


def test_stripe_payment_intent_with_charge():
    event = json.loads(sample("stripe_payment_intent_succeeded.json"))
    charge = json.loads(sample("stripe_charge_normal.json"))
    o = normalize.from_stripe_payment_intent(event["data"]["object"], charge)
    assert o.order_key == "stripe:pi_sample_0001" and o.total == 45.0
    assert o.items[0].title == "Cold Brew Kit" and o.payment_risk_level == "normal"
    assert o.card_country == "AE" and o.test is True


def test_zero_decimal_currency():
    o = normalize.from_stripe_payment_intent({"id": "pi_1", "amount_received": 5000, "currency": "jpy"})
    assert o.total == 5000.0


def test_checkout_session_keyed_on_payment_intent():
    s = {"id": "cs_1", "payment_intent": "pi_9", "amount_total": 1999, "currency": "usd",
         "customer_details": {"email": "a@example.com", "name": "A", "address": {"country": "US"}}}
    assert normalize.from_stripe_checkout_session(s).order_key == "stripe:pi_9"


def test_bad_items_metadata_does_not_crash():
    o = normalize.from_stripe_payment_intent({"id": "pi_1", "amount": 100, "currency": "usd",
                                              "metadata": {"items": "not json"}})
    assert o.items[0].title == "not json"


def test_risky_order_signals():
    o = normalize.from_shopify(json.loads(sample("shopify_orders_paid_risky.json")))
    found = signals(o)
    text = " ".join(found)
    assert "High value" in text and "differs from billing" in text and "Bulk quantity" in text
    assert "First order" in text
    r = rules_review(o, found)
    assert r.risk == "High" and r.flagged and r.reviewer == "rules"


def test_normal_order_has_no_signals():
    o = normalize.from_shopify(json.loads(sample("shopify_orders_paid.json")))
    assert signals(o) == []
    assert rules_review(o, []).risk == "Low"
