"""Re-runs every event the service could not finish (Airtable down, email down...).

    python scripts/replay_failed.py
"""
import logging

from _env import settings
from order_flow.server import build_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
app = build_app(settings())
results = app.replay_failed()
print(f"replayed {len(results)}: " + ", ".join(r["status"] for r in results) if results else "nothing to replay")
