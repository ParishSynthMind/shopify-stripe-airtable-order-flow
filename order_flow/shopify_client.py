"""Shopify Admin API: token by client credentials, GraphQL, webhook subscription."""
from __future__ import annotations

from typing import Callable, Optional

from .http import request

API_VERSION = "2025-10"


class ShopifyClient:
    def __init__(self, shop_domain: str, client_id: str, client_secret: str,
                 send: Optional[Callable] = None):
        self.shop, self.client_id, self.client_secret = shop_domain, client_id, client_secret
        self._send = send or request
        self._token: Optional[str] = None

    def token(self) -> str:
        """Apps installed on a store you own can mint a 24 hour Admin API token
        with the client credentials grant. Minted at runtime, never stored."""
        if not self._token:
            resp = self._send("Shopify", "POST", f"https://{self.shop}/admin/oauth/access_token", {},
                              form={"client_id": self.client_id, "client_secret": self.client_secret,
                                    "grant_type": "client_credentials"})
            self._token = resp["access_token"]
        return self._token

    def graphql(self, query: str, variables: Optional[dict] = None) -> dict:
        resp = self._send("Shopify", "POST", f"https://{self.shop}/admin/api/{API_VERSION}/graphql.json",
                          {"X-Shopify-Access-Token": self.token()},
                          json_body={"query": query, "variables": variables or {}})
        if resp.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {resp['errors']}")
        return resp["data"]

    def shop_info(self) -> dict:
        return self.graphql("{ shop { name currencyCode plan { displayName } } "
                            "currentAppInstallation { accessScopes { handle } } }")

    def recent_orders(self, first: int = 5) -> list:
        data = self.graphql("query($n:Int!){ orders(first:$n, reverse:true){ nodes { id name "
                            "displayFinancialStatus totalPriceSet { shopMoney { amount currencyCode } } } } }",
                            {"n": first})
        return data["orders"]["nodes"]

    def subscribe_orders_paid(self, callback_url: str) -> dict:
        data = self.graphql(
            "mutation($u:URL!){ webhookSubscriptionCreate(topic: ORDERS_PAID, "
            "webhookSubscription:{callbackUrl:$u, format:JSON}){ webhookSubscription{ id topic } "
            "userErrors{ field message } } }", {"u": callback_url})
        return data["webhookSubscriptionCreate"]
