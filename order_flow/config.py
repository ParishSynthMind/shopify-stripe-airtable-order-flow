"""Settings from environment variables (or a local .env, which is git-ignored)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


@dataclass(repr=False)
class Settings:
    shopify_shop: str = ""
    shopify_webhook_secret: str = ""     # the app's client secret
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""      # whsec_..., one per endpoint
    stripe_tolerance: int = 300
    airtable_token: str = ""
    airtable_base_id: str = ""
    airtable_table: str = "Orders"
    anthropic_model: str = "claude-sonnet-5-5"
    ai_mode: str = "claude"              # claude | rules
    subject_prefix: str = ""
    high_value: float = 500.0
    alert_webhook_url: str = ""
    klaviyo_api_key: str = ""            # private key; Klaviyo sync is off when empty and not dry-run
    klaviyo_revision: str = "2026-10-15"
    klaviyo_dry_run: bool = False        # build and log the Klaviyo calls, send nothing
    db_path: str = "data/events.sqlite3"
    host: str = "127.0.0.1"
    port: int = 8000

    @classmethod
    def from_env(cls, env=os.environ) -> "Settings":
        return cls(
            shopify_shop=env.get("SHOPIFY_SHOP", ""),
            shopify_webhook_secret=env.get("SHOPIFY_WEBHOOK_SECRET", ""),
            stripe_secret_key=env.get("STRIPE_SECRET_KEY", ""),
            stripe_webhook_secret=env.get("STRIPE_WEBHOOK_SECRET", ""),
            stripe_tolerance=int(env.get("STRIPE_TOLERANCE_SECONDS", "300")),
            airtable_token=env.get("AIRTABLE_TOKEN", ""),
            airtable_base_id=env.get("AIRTABLE_BASE_ID", ""),
            airtable_table=env.get("AIRTABLE_TABLE", "Orders"),
            anthropic_model=env.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            ai_mode=env.get("AI_MODE", "claude"),
            subject_prefix=env.get("SUBJECT_PREFIX", ""),
            high_value=float(env.get("HIGH_VALUE_THRESHOLD", "500")),
            alert_webhook_url=env.get("ALERT_WEBHOOK_URL", ""),
            klaviyo_api_key=env.get("KLAVIYO_PRIVATE_API_KEY", ""),
            klaviyo_revision=env.get("KLAVIYO_REVISION", "2026-10-15"),
            klaviyo_dry_run=env.get("KLAVIYO_DRY_RUN", "").lower() in ("1", "true", "yes"),
            db_path=env.get("EVENT_DB", "data/events.sqlite3"),
            host=env.get("HOST", "127.0.0.1"),
            port=int(env.get("PORT", "8000")),
        )

    @property
    def klaviyo_enabled(self) -> bool:
        return bool(self.klaviyo_api_key) or self.klaviyo_dry_run

    def __repr__(self) -> str:  # settings hold secrets; never print them
        return f"Settings(shop={self.shopify_shop!r}, ai_mode={self.ai_mode!r}, klaviyo={self.klaviyo_enabled})"
