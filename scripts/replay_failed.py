"""Re-runs every event the service could not finish (Airtable down, email down...).

    python scripts/replay_failed.py             # whole failed orders
    python scripts/replay_failed.py --klaviyo   # only the Klaviyo step for orders that otherwise went through
"""
import logging
import sys

from _env import settings
from order_flow.server import build_app

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
app = build_app(settings())
if "--klaviyo" in sys.argv:
    results = app.replay_klaviyo()
    print(f"Klaviyo replayed {len(results)}: " + ", ".join(results) if results else "no Klaviyo sync to replay")
else:
    results = app.replay_failed()
    print(f"replayed {len(results)}: " + ", ".join(r["status"] for r in results) if results else "nothing to replay")
