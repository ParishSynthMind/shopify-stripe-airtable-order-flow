"""Tiny JSON-over-HTTPS helper on the standard library, so the only package
this project needs is the Anthropic SDK."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional


class ApiError(Exception):
    def __init__(self, service: str, status: int, body: str):
        super().__init__(f"{service} API error {status}: {body[:300]}")
        self.service, self.status, self.body = service, status, body

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


def request(service: str, method: str, url: str, headers: dict,
            json_body: Optional[dict] = None, form: Optional[dict] = None, timeout: float = 20) -> dict:
    data = None
    headers = dict(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(service, e.code, e.read().decode(errors="replace")) from None
    except urllib.error.URLError as e:
        raise ApiError(service, 599, str(e.reason)) from None
    return json.loads(raw) if raw else {}
