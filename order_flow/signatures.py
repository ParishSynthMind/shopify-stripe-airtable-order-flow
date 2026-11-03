"""Webhook signature checks for Shopify and Stripe.

Both must be checked against the raw request bytes exactly as received.
Parsing the JSON and serialising it again changes whitespace and key order,
and the signature will no longer match.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Optional

STRIPE_DEFAULT_TOLERANCE = 300  # seconds, the same default Stripe's own libraries use


class SignatureError(Exception):
    """Raised when a webhook signature is missing, malformed, stale or wrong."""


# --- Shopify -----------------------------------------------------------------
# X-Shopify-Hmac-Sha256 is base64(HMAC-SHA256(raw_body, app_client_secret)).

def shopify_signature(secret: str, raw_body: bytes) -> str:
    digest = hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def verify_shopify(secret: str, raw_body: bytes, header_value: Optional[str]) -> None:
    if not secret:
        raise SignatureError("Shopify secret is not configured")
    if not header_value:
        raise SignatureError("missing X-Shopify-Hmac-Sha256 header")
    expected = shopify_signature(secret, raw_body)
    if not hmac.compare_digest(expected, header_value.strip()):
        raise SignatureError("Shopify HMAC does not match")


# --- Stripe ------------------------------------------------------------------
# Stripe-Signature: t=<unix time>,v1=<hex HMAC-SHA256("<t>.<raw body>", whsec)>[,v1=...]
# More than one v1 can appear while a secret is being rolled, so any match passes.

def stripe_signature(secret: str, raw_body: bytes, timestamp: int) -> str:
    signed = str(timestamp).encode() + b"." + raw_body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def stripe_header(secret: str, raw_body: bytes, timestamp: Optional[int] = None) -> str:
    """Builds a header the way Stripe does. Used by tests and the local simulator."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={stripe_signature(secret, raw_body, ts)}"


def verify_stripe(secret: str, raw_body: bytes, header_value: Optional[str],
                  tolerance: int = STRIPE_DEFAULT_TOLERANCE,
                  now: Optional[float] = None) -> int:
    """Returns the signed timestamp if valid, raises SignatureError otherwise."""
    if not secret:
        raise SignatureError("Stripe webhook secret is not configured")
    if not header_value:
        raise SignatureError("missing Stripe-Signature header")
    timestamp = None
    candidates = []
    for part in header_value.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                raise SignatureError("bad timestamp in Stripe-Signature") from None
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise SignatureError("Stripe-Signature has no timestamp or no v1 signature")
    current = time.time() if now is None else now
    if tolerance and abs(current - timestamp) > tolerance:
        raise SignatureError("Stripe signature timestamp is outside the tolerance window")
    expected = stripe_signature(secret, raw_body, timestamp)
    if not any(hmac.compare_digest(expected, c) for c in candidates):
        raise SignatureError("Stripe signature does not match")
    return timestamp
