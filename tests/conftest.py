import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from order_flow.alerts import Alerter
from order_flow.app import WebhookApp
from order_flow.config import Settings
from order_flow.http import ApiError
from order_flow.models import Review
from order_flow.notifiers import ConsoleNotifier
from order_flow.pipeline import OrderPipeline
from order_flow.store import EventStore

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def sample(name):
    return (SAMPLES / name).read_bytes()


class FakeAirtable:
    """Behaves like Airtable's upsert: same Order Key -> same record."""
    def __init__(self, fail_times=0, status=503):
        self.rows, self.calls, self.fail_times, self.status = {}, 0, fail_times, status

    def upsert_order(self, fields):
        self.calls += 1
        if self.fail_times:
            self.fail_times -= 1
            raise ApiError("Airtable", self.status, "boom")
        key = fields["Order Key"]
        created = key not in self.rows
        if created:
            self.rows[key] = {"id": f"rec{len(self.rows) + 1:03d}", "fields": {}}
        self.rows[key]["fields"].update(fields)
        return {"record_id": self.rows[key]["id"], "created": created}


class FakeReviewer:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def review(self, order, signals):
        self.calls.append((order, signals))
        if self.fail:
            raise RuntimeError("anthropic is down")
        risky = bool(signals)
        return Review(note=f"Test note for {order.order_name}", risk="High" if risky else "Low",
                      flagged=risky, reasons=list(signals))


class FakeStripe:
    def __init__(self, charge):
        self.charge, self.calls = charge, []

    def get_charge(self, cid):
        self.calls.append(cid)
        return self.charge

    def get_payment_intent(self, pid):
        return {"id": pid, "latest_charge": "ch_from_pi"}


@pytest.fixture
def settings():
    return Settings(shopify_shop="demo.myshopify.com", shopify_webhook_secret="shpss_test_secret",
                    stripe_webhook_secret="whsec_test_secret", subject_prefix="[TEST] ")


@pytest.fixture
def rig(settings):
    def make(airtable=None, reviewer=None, stripe=None):
        store = EventStore()
        notifier = ConsoleNotifier()
        alert_notifier = ConsoleNotifier()
        alerter = Alerter(notifier=alert_notifier)
        at = airtable or FakeAirtable()
        rv = reviewer or FakeReviewer()
        pipe = OrderPipeline(rv, at, notifier, alerter, store, subject_prefix=settings.subject_prefix,
                             sleep=lambda s: None)
        app = WebhookApp(settings, pipe, store, stripe=stripe, sync=True)
        return type("Rig", (), dict(app=app, store=store, notifier=notifier, alerter=alerter,
                                    airtable=at, reviewer=rv))
    return make
