"""python -m order_flow.server  ->  listens on HOST:PORT (default 127.0.0.1:8000).

POST /webhooks/shopify   Shopify orders/paid
POST /webhooks/stripe    Stripe payment_intent.succeeded / checkout.session.completed
                         (and checkout.session.expired when Klaviyo is configured)
GET  /health
"""
from __future__ import annotations

import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .ai import AnthropicReviewer, RulesReviewer
from .airtable import AirtableClient
from .alerts import Alerter
from .app import MAX_BODY, WebhookApp
from .config import Settings, load_dotenv
from .klaviyo import KlaviyoClient, KlaviyoSync
from .notifiers import from_env as notifier_from_env
from .pipeline import OrderPipeline
from .store import EventStore
from .stripe_client import StripeClient


def build_app(settings: Settings, notifier=None) -> WebhookApp:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    store = EventStore(settings.db_path)
    notifier = notifier or notifier_from_env()
    reviewer = RulesReviewer() if settings.ai_mode == "rules" else AnthropicReviewer(settings.anthropic_model)
    klaviyo = None
    if settings.klaviyo_enabled:
        klaviyo = KlaviyoSync(KlaviyoClient(settings.klaviyo_api_key, settings.klaviyo_revision,
                                            dry_run=settings.klaviyo_dry_run), store)
    pipeline = OrderPipeline(
        reviewer=reviewer,
        airtable=AirtableClient(settings.airtable_token, settings.airtable_base_id, settings.airtable_table),
        notifier=notifier,
        alerter=Alerter(settings.alert_webhook_url, notifier, settings.subject_prefix),
        store=store, subject_prefix=settings.subject_prefix, high_value=settings.high_value,
        klaviyo=klaviyo,
    )
    stripe = StripeClient(settings.stripe_secret_key) if settings.stripe_secret_key else None
    return WebhookApp(settings, pipeline, store, stripe=stripe)


def make_handler(app: WebhookApp):
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _dispatch(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._respond(413, {"error": "body too large"})
            body = self.rfile.read(length) if length else b""
            status, payload = app.handle(method, self.path.split("?")[0], dict(self.headers), body)
            self._respond(status, payload)

        def do_POST(self):
            self._dispatch("POST")

        def do_GET(self):
            self._dispatch("GET")

        def log_message(self, fmt, *args):  # route access logs through logging, no bodies
            logging.getLogger("order_flow.http").info(fmt, *args)

    return Handler


def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    app = build_app(settings)
    app.start_worker()
    server = ThreadingHTTPServer((settings.host, settings.port), make_handler(app))
    logging.info("listening on http://%s:%d", settings.host, settings.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
