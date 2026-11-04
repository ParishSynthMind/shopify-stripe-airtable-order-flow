"""Klaviyo client and sync, with the HTTP transport replaced by a script of
canned responses. No live calls."""
import json
import logging

import pytest

from conftest import sample
from order_flow.klaviyo import (KlaviyoClient, KlaviyoError, KlaviyoSync, ValidationError, placed_order_event,
                                profile_attributes, readable_error, started_checkout_event)
from order_flow.models import LineItem, Order
from order_flow.signatures import shopify_signature, stripe_header
from order_flow.store import EventStore

KEY = "pk_test_not_a_real_key_123"


class Transport:
    """Plays back (status, headers, body) tuples and records every request."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append({"method": method, "url": url, "headers": headers,
                           "body": json.loads(body) if body else None})
        status, headers_out, payload = self.responses.pop(0) if self.responses else (202, {}, b"")
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        return status, headers_out, payload


def ok_profile(pid="01PROFILE"):
    return (201, {}, {"data": {"type": "profile", "id": pid}})


def client(transport, **kw):
    sleeps = []
    c = KlaviyoClient(KEY, transport=transport, sleep=sleeps.append, **kw)
    return c, sleeps


ORDER = Order(source="Stripe", order_key="stripe:pi_1", order_name="WEB-1", total=45.0, currency="USD",
              customer_name="Pat Q Example", customer_email=" Pat.Example@Example.com ",
              items=[LineItem("Cold Brew Kit", 1, 45.0)], created_at="2026-11-04T07:00:00+00:00", test=True)


def order(**changes):
    return Order(**{**ORDER.__dict__, **changes})


# --- payloads ------------------------------------------------------------------

def test_profile_has_name_order_facts_and_no_consent_fields():
    attrs = profile_attributes(ORDER, 3)
    assert attrs["email"] == "pat.example@example.com"
    assert attrs["first_name"] == "Pat" and attrs["last_name"] == "Q Example"
    assert attrs["properties"]["Order Count"] == 3 and attrs["properties"]["Last Order ID"] == "WEB-1"
    assert attrs["properties"]["Last Order Value"] == 45.0
    assert "subscriptions" not in json.dumps(attrs)


def test_placed_order_event_shape_and_unique_id_tied_to_order():
    e = placed_order_event(ORDER)
    assert e["metric"]["data"]["attributes"]["name"] == "Placed Order"
    assert e["profile"]["data"]["attributes"] == {"email": "pat.example@example.com"}
    assert e["value"] == 45.0 and e["value_currency"] == "USD"
    assert e["unique_id"] == "placed-order:stripe:pi_1"
    assert e["properties"]["Items"] == [{"ProductName": "Cold Brew Kit", "Quantity": 1,
                                         "ItemPrice": 45.0, "RowTotal": 45.0}]
    assert e["time"] == "2026-11-04T07:00:00+00:00"
    assert placed_order_event(ORDER)["unique_id"] == e["unique_id"]   # same order, same id, every time


def test_started_checkout_event_records_consent_without_subscribing():
    e = started_checkout_event(order(order_key="stripe:cs_9"), "https://example.com/r", "opt_in")
    assert e["metric"]["data"]["attributes"]["name"] == "Started Checkout"
    assert e["unique_id"] == "started-checkout:stripe:cs_9"
    assert e["properties"]["StripePromotionsConsent"] == "opt_in"
    assert e["properties"]["CheckoutURL"] == "https://example.com/r"
    assert started_checkout_event(ORDER)["properties"]["StripePromotionsConsent"] == "not collected"


@pytest.mark.parametrize("changes, message", [
    ({"customer_email": ""}, "no customer email"),
    ({"customer_email": "not-an-email"}, "not a valid"),
    ({"customer_email": "a@b"}, "not a valid"),
    ({"total": -1.0}, "total"),
    ({"total": float("nan")}, "total"),
    ({"currency": "usd!"}, "currency"),
    ({"items": [LineItem("x", -2, 1.0)]}, "line item"),
])
def test_strict_validation(changes, message):
    with pytest.raises(ValidationError, match=message):
        placed_order_event(order(**changes))


def test_long_titles_and_huge_baskets_are_capped():
    big = order(items=[LineItem("T" * 1000, 1, 1.0)] * 150)
    e = placed_order_event(big)
    assert len(e["properties"]["Items"]) == 100 and len(e["properties"]["ItemNames"][0]) == 255


# --- client --------------------------------------------------------------------

def test_headers_revision_user_agent_and_jsonapi_body():
    t = Transport(ok_profile(), (202, {}, b""))
    c, _ = client(t)
    KlaviyoSync(c).placed_order(ORDER)
    profile_call, event_call = t.calls
    h = profile_call["headers"]
    assert h["Authorization"] == f"Klaviyo-API-Key {KEY}" and h["revision"] == "2026-10-15"
    assert h["User-Agent"].startswith("shopify-stripe-airtable-order-flow/")
    assert h["content-type"] == "application/vnd.api+json"
    assert profile_call["url"] == "https://a.klaviyo.com/api/profile-import"
    assert profile_call["body"]["data"]["type"] == "profile"
    assert event_call["url"] == "https://a.klaviyo.com/api/events"
    assert event_call["body"]["data"]["type"] == "event"


def test_429_waits_for_retry_after_then_succeeds():
    throttled = (429, {"Retry-After": "3"},
                 {"errors": [{"code": "throttled", "detail": "Expected available in 3 seconds."}]})
    t = Transport(throttled, throttled, ok_profile())
    c, sleeps = client(t)
    assert c.upsert_profile({"email": "a@example.com"}) == "01PROFILE"
    assert sleeps == [3.0, 3.0] and len(t.calls) == 3


def test_retry_after_is_capped_and_bad_header_falls_back_to_backoff():
    t = Transport((429, {"retry-after": "9999"}, b""), (429, {"Retry-After": "soon"}, b""), ok_profile())
    c, sleeps = client(t, max_wait=30, backoff=0.5)
    c.upsert_profile({"email": "a@example.com"})
    assert sleeps == [30.0, 1.0]


def test_429_gives_up_after_max_attempts_with_readable_error():
    throttled = (429, {"Retry-After": "1"}, {"errors": [{"code": "throttled", "detail": "slow down"}]})
    c, sleeps = client(Transport(*[throttled] * 4))
    with pytest.raises(KlaviyoError, match="429: throttled: slow down") as e:
        c.create_event({})
    assert e.value.retryable and len(sleeps) == 3


def test_5xx_and_network_errors_back_off_then_succeed():
    t = Transport((503, {}, b"upstream"), (599, {}, {"errors": [{"code": "network", "detail": "timed out"}]}),
                  (202, {}, b""))
    c, sleeps = client(t, backoff=1.0)
    c.create_event({})
    assert sleeps == [1.0, 2.0]


def test_4xx_is_not_retried_and_message_is_readable_without_the_email():
    bad = (400, {}, {"errors": [{"code": "invalid", "title": "Invalid input.",
                                 "detail": "pat.example@example.com is not a valid email address",
                                 "source": {"pointer": "/data/attributes/email"}}]})
    t = Transport(bad)
    c, sleeps = client(t)
    with pytest.raises(KlaviyoError) as e:
        c.upsert_profile({"email": "x"})
    msg = str(e.value)
    assert msg == "Klaviyo 400: invalid: <email> is not a valid email address (at /data/attributes/email)"
    assert not e.value.retryable and sleeps == [] and len(t.calls) == 1


def test_401_403_readable():
    c, _ = client(Transport((403, {}, {"errors": [{"code": "permission_denied",
                                                   "detail": "This API key is missing required scopes."}]})))
    with pytest.raises(KlaviyoError, match="403: permission_denied: This API key is missing required scopes"):
        c.create_event({})


def test_non_json_error_body():
    assert readable_error(502, b"<html>Bad gateway</html>") == "<html>Bad gateway</html>"
    assert readable_error(500, b"") == "no response body"


def test_key_never_in_repr_or_errors():
    t = Transport((401, {}, {"errors": [{"code": "not_authenticated",
                                          "detail": "Incorrect authentication credentials."}]}))
    c, _ = client(t)
    assert KEY not in repr(c)
    with pytest.raises(KlaviyoError) as e:
        c.create_event({})
    assert KEY not in str(e.value)


def test_missing_key_refused_unless_dry_run():
    with pytest.raises(ValueError):
        KlaviyoClient("")
    KlaviyoClient("", dry_run=True)


def test_dry_run_builds_calls_but_sends_nothing():
    t = Transport()
    c = KlaviyoClient("", dry_run=True, transport=t)
    result = KlaviyoSync(c).placed_order(ORDER)
    assert t.calls == [] and result["dry_run"] is True
    assert [x["path"] for x in c.dry_run_log] == ["/profile-import", "/events"]
    assert c.dry_run_log[1]["body"]["data"]["attributes"]["unique_id"] == "placed-order:stripe:pi_1"


def test_read_helpers_follow_pagination_and_filters():
    t = Transport((200, {}, {"data": [{"id": "m1", "attributes": {"name": "Viewed"}}],
                             "links": {"next": "https://a.klaviyo.com/api/metrics?page%5Bcursor%5D=2"}}),
                  (200, {}, {"data": [{"id": "m2", "attributes": {"name": "Placed Order"}}], "links": {}}),
                  (200, {}, {"data": [{"id": "p1"}]}), (200, {}, {"data": [{"id": "e1"}]}))
    c, _ = client(t)
    assert c.metric_id("Placed Order") == "m2"
    assert t.calls[1]["url"].endswith("page%5Bcursor%5D=2")
    assert c.find_profile("a@example.com")["id"] == "p1"
    assert "equals%28email%2C%22a%40example.com%22%29" in t.calls[2]["url"]
    assert c.profile_events("p1", "m2") == [{"id": "e1"}]


# --- order count -----------------------------------------------------------------

def test_order_count_from_local_store_is_idempotent_and_uses_hash_only():
    store = EventStore()
    sync = KlaviyoSync(KlaviyoClient("", dry_run=True), store)
    assert sync.order_history(ORDER)[0] == 1
    assert sync.order_history(ORDER)[0] == 1                     # same order again: still 1
    assert sync.order_history(order(order_key="stripe:pi_2"))[0] == 2
    assert sync.order_history(order(customer_email="pat.example@example.com", order_key="stripe:pi_3"))[0] == 3
    dump = "\n".join(store._db.iterdump())
    assert "example.com" not in dump.lower()                     # no email stored, only a hash


def test_shopify_orders_count_wins():
    sync = KlaviyoSync(KlaviyoClient("", dry_run=True), EventStore())
    assert sync.order_history(order(customer_order_count=7))[0] == 7


def test_replayed_older_order_does_not_roll_back_last_order():
    """Found in the live run: re-sending order A after order D set 'Last Order' back to A."""
    c = KlaviyoClient("", dry_run=True)
    sync = KlaviyoSync(c, EventStore())
    a = order(order_key="stripe:pi_A", order_name="A", total=45.0, created_at="2026-11-04T06:23:56+00:00")
    d = order(order_key="stripe:pi_D", order_name="D", total=48.0, created_at="2026-11-04T10:24:05+04:00")
    for o in (a, d, a):
        sync.placed_order(o)
    last = c.dry_run_log[-2]["body"]["data"]["attributes"]["properties"]      # profile call of the replay
    assert last["Last Order ID"] == "D" and last["Last Order Value"] == 48.0 and last["Order Count"] == 2
    assert c.dry_run_log[-1]["body"]["data"]["attributes"]["unique_id"] == "placed-order:stripe:pi_A"


def test_time_zones_compared_as_instants():
    c = KlaviyoClient("", dry_run=True)
    sync = KlaviyoSync(c, EventStore())
    later_utc = order(order_key="k1", order_name="later", created_at="2026-11-04T07:00:00Z")
    earlier_dubai = order(order_key="k2", order_name="earlier", created_at="2026-11-04T10:30:00+04:00")
    sync.placed_order(later_utc)
    sync.placed_order(earlier_dubai)
    assert c.dry_run_log[-2]["body"]["data"]["attributes"]["properties"]["Last Order ID"] == "later"


def test_invalid_order_is_not_recorded_in_history():
    store = EventStore()
    sync = KlaviyoSync(KlaviyoClient("", dry_run=True), store)
    with pytest.raises(ValidationError):
        sync.placed_order(order(total=-5.0))
    assert store._db.execute("SELECT COUNT(*) FROM customer_orders").fetchone()[0] == 0


# --- through the webhook app ---------------------------------------------------------

@pytest.fixture
def krig(rig):
    def make(*responses, **kw):
        t = Transport(*responses)
        r = rig(**kw)
        c = KlaviyoClient(KEY, transport=t, sleep=lambda s: None)
        r.app.pipeline.klaviyo = KlaviyoSync(c, r.store)
        r.transport = t
        return r
    return make


def shopify_post(r, body, event_id="evt-1"):
    h = {"X-Shopify-Hmac-Sha256": shopify_signature("shpss_test_secret", body), "X-Shopify-Topic": "orders/paid",
         "X-Shopify-Shop-Domain": "demo.myshopify.com", "X-Shopify-Event-Id": event_id}
    return r.app.handle("POST", "/webhooks/shopify", h, body)


def stripe_post(r, event):
    body = json.dumps(event).encode() if isinstance(event, dict) else event
    headers = {"Stripe-Signature": stripe_header("whsec_test_secret", body)}
    return r.app.handle("POST", "/webhooks/stripe", headers, body)


def test_paid_shopify_order_sends_profile_and_placed_order_once(krig):
    r = krig(ok_profile(), (202, {}, b""))
    body = sample("shopify_orders_paid.json")
    shopify_post(r, body)
    shopify_post(r, body)                                         # webhook retried
    assert [c["url"].rsplit("/", 1)[-1] for c in r.transport.calls] == ["profile-import", "events"]
    ev = r.transport.calls[1]["body"]["data"]["attributes"]
    assert ev["unique_id"] == "placed-order:shopify:820982911946154508" and ev["value"] == 84.0
    assert r.store.klaviyo_status("shopify:evt-1")["status"] == "done"


def test_stripe_payment_intent_and_checkout_for_same_payment_one_klaviyo_event(krig):
    r = krig(ok_profile(), (202, {}, b""))
    stripe_post(r, sample("stripe_payment_intent_succeeded.json"))
    stripe_post(r, {"id": "evt_cs_1", "type": "checkout.session.completed", "data": {"object": {
        "id": "cs_1", "payment_intent": "pi_sample_0001", "payment_status": "paid", "amount_total": 4500,
        "currency": "usd"}}})
    assert len([c for c in r.transport.calls if c["url"].endswith("/events")]) == 1


def test_replayed_order_resends_same_unique_id(krig):
    """If the team email fails after Klaviyo, the replay sends the event again
    with the same unique_id, which Klaviyo drops, so no duplicate."""
    r = krig(ok_profile(), (202, {}, b""), ok_profile(), (202, {}, b""))
    real_send, fails = r.notifier.send, [1]

    def flaky(*a, **k):
        if fails:
            fails.pop()
            raise RuntimeError("smtp down")
        return real_send(*a, **k)
    r.notifier.send = flaky
    stripe_post(r, sample("stripe_payment_intent_succeeded.json"))
    assert r.store.status("stripe:evt_sample_0001")["status"] == "failed"
    r.app.replay_failed()
    ids = [c["body"]["data"]["attributes"]["unique_id"] for c in r.transport.calls if c["url"].endswith("/events")]
    assert ids == ["placed-order:stripe:pi_sample_0001"] * 2 and len(r.notifier.sent) == 1


def test_klaviyo_down_does_not_block_order_and_is_replayable(krig):
    r = krig(*[(503, {}, b"down")] * 4)
    body = sample("shopify_orders_paid.json")
    shopify_post(r, body)
    assert r.store.status("shopify:evt-1")["status"] == "done"       # row + team email still happened
    assert len(r.airtable.rows) == 1 and len(r.notifier.sent) == 1
    assert r.store.klaviyo_status("shopify:evt-1")["status"] == "failed"
    assert any("Klaviyo sync failed" in a[0] for a in r.alerter.sent)
    r.transport.responses = [ok_profile(), (202, {}, b"")]
    assert r.app.replay_klaviyo() == ["done"]
    assert r.store.klaviyo_status("shopify:evt-1")["status"] == "done" and r.store.klaviyo_failed() == []
    assert len(r.notifier.sent) == 1                                  # replay did not re-email the team


def test_order_without_email_skips_klaviyo_but_order_goes_through(krig):
    r = krig()
    payload = json.loads(sample("shopify_orders_paid.json"))
    payload["email"] = ""
    (payload.get("customer") or {}).pop("email", None)
    shopify_post(r, json.dumps(payload).encode())
    assert r.transport.calls == [] and len(r.airtable.rows) == 1
    st = r.store.klaviyo_status("shopify:evt-1")
    assert st["status"] == "skipped" and "no customer email" in st["detail"]


def test_logs_and_alerts_carry_no_email_address(krig, caplog):
    bad = (400, {}, {"errors": [{"code": "invalid", "detail": "jane@example.com is invalid"}]})
    r = krig(bad)
    with caplog.at_level(logging.DEBUG):
        shopify_post(r, sample("shopify_orders_paid.json"))
    text = caplog.text + json.dumps(r.alerter.sent) + json.dumps(r.store.klaviyo_status("shopify:evt-1"))
    assert "@" not in text and KEY not in text


EXPIRED = {"id": "evt_exp_1", "type": "checkout.session.expired", "data": {"object": {
    "id": "cs_exp_1", "object": "checkout.session", "mode": "payment", "status": "expired",
    "payment_status": "unpaid", "amount_total": 4500, "currency": "usd",
    "customer_email": "pat.example@example.com", "customer_details": None,
    "after_expiration": {"recovery": {"url": "https://buy.stripe.com/r/test_123"}},
    "consent": {"promotions": "opt_in"},
    "metadata": {"order_name": "WEB-CART-1",
                 "items": json.dumps([{"title": "Cold Brew Kit", "quantity": 1, "unit_price": 45.0}])}}}}


def test_expired_checkout_becomes_started_checkout_only(krig):
    r = krig((202, {}, b""))
    status, resp = stripe_post(r, EXPIRED)
    assert status == 200 and resp["queued"] == "stripe:evt_exp_1"
    stripe_post(r, EXPIRED)                                       # retried delivery
    assert len(r.transport.calls) == 1
    ev = r.transport.calls[0]["body"]["data"]["attributes"]
    assert ev["metric"]["data"]["attributes"]["name"] == "Started Checkout"
    assert ev["unique_id"] == "started-checkout:stripe:cs_exp_1"
    assert ev["properties"]["CheckoutURL"].startswith("https://buy.stripe.com/")
    assert ev["properties"]["StripePromotionsConsent"] == "opt_in"
    assert r.airtable.calls == 0 and r.notifier.sent == [] and r.reviewer.calls == []
    assert r.store.status("stripe:evt_exp_1")["status"] == "done"


def test_expired_checkout_ignored_without_klaviyo(rig):
    r = rig()
    status, resp = stripe_post(r, EXPIRED)
    assert status == 200 and resp == {"ignored": "checkout.session.expired"}


def test_expired_subscription_checkout_ignored(krig):
    r = krig()
    event = json.loads(json.dumps(EXPIRED))
    event["data"]["object"]["mode"] = "subscription"
    assert stripe_post(r, event)[1] == {"ignored": "checkout session not abandoned"}


def test_expired_checkout_klaviyo_failure_is_replayable(krig):
    r = krig(*[(500, {}, b"")] * 4)
    stripe_post(r, EXPIRED)
    assert r.store.klaviyo_status("stripe:evt_exp_1")["status"] == "failed"
    r.transport.responses = [(202, {}, b"")]
    assert r.app.replay_klaviyo() == ["done"]
    assert r.transport.calls[-1]["body"]["data"]["attributes"]["unique_id"] == "started-checkout:stripe:cs_exp_1"


def test_worker_thread_handles_abandoned_jobs(krig):
    r = krig((202, {}, b""))
    r.app.sync = False
    r.app.start_worker()
    stripe_post(r, EXPIRED)
    r.app.jobs.join()
    assert r.store.klaviyo_status("stripe:evt_exp_1")["status"] == "done"


def test_build_app_wires_klaviyo_from_settings(tmp_path):
    from order_flow.config import Settings
    from order_flow.server import build_app
    s = Settings(db_path=str(tmp_path / "e.sqlite3"), ai_mode="rules", klaviyo_dry_run=True)
    assert build_app(s).pipeline.klaviyo.client.dry_run is True
    assert build_app(Settings(db_path=str(tmp_path / "f.sqlite3"), ai_mode="rules")).pipeline.klaviyo is None
    assert "KLAVIYO" not in repr(Settings(klaviyo_api_key="pk_secret")) and "pk_secret" not in repr(
        Settings(klaviyo_api_key="pk_secret"))


def test_settings_from_env():
    from order_flow.config import Settings
    s = Settings.from_env({"KLAVIYO_PRIVATE_API_KEY": "pk_x", "KLAVIYO_DRY_RUN": "1"})
    assert s.klaviyo_enabled and s.klaviyo_dry_run and s.klaviyo_revision == "2026-10-15"
    assert not Settings.from_env({}).klaviyo_enabled
