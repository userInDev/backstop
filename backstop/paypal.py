"""The PayPal boundary.

Every call out of this module lands in exactly one of three buckets, and the
mapping is the only interesting thing in the file:

    2xx                      -> return the body
    4xx                      -> PayPalRefused   (an ANSWER: nothing moved)
    5xx, timeout, transport  -> PayPalUnknown   (no answer: unknowable)

PayPal gives you `PayPal-Request-Id` for idempotency and we send it. It is
not sufficient, and saying why is half the point of this project: that header
protects PAYPAL from processing a duplicate request. It does nothing for our
own uncertainty. When the response never arrives we still do not know whether
the refund exists, and an agent that does not know will do something wrong
next unless something stops it. That something is backstop/core.py.
"""

from __future__ import annotations

import os

import httpx

from .errors import PayPalRefused, PayPalUnknown
from .money import from_provider, to_provider

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class PayPal:
    def __init__(self, client_id=None, client_secret=None, base_url=None, timeout=DEFAULT_TIMEOUT, chaos=None):
        self.client_id = client_id or os.getenv("PAYPAL_CLIENT_ID", "")
        self.client_secret = client_secret or os.getenv("PAYPAL_CLIENT_SECRET", "")
        self.base_url = (base_url or os.getenv("PAYPAL_BASE_URL") or "https://api-m.sandbox.paypal.com").rstrip("/")
        self.timeout = timeout
        # "refund" => really send the refund, then throw the response away.
        self.chaos = chaos if chaos is not None else os.getenv("BACKSTOP_CHAOS", "")
        self._token = None

    # ---- plumbing -------------------------------------------------------

    def _token_value(self) -> str:
        if self._token:
            return self._token
        try:
            r = httpx.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                timeout=self.timeout,
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise PayPalUnknown(f"token: no answer ({type(e).__name__})") from e
        if r.status_code >= 500:
            raise PayPalUnknown(f"token: {r.status_code}")
        if r.status_code >= 400:
            raise PayPalRefused(f"token: {r.status_code} {r.text[:200]}", r.status_code, r.text)
        self._token = r.json()["access_token"]
        return self._token

    def _call(self, method: str, path: str, *, json_body=None, request_id=None, discard_response=False):
        headers = {"Authorization": f"Bearer {self._token_value()}", "Content-Type": "application/json"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        url = f"{self.base_url}{path}"
        try:
            r = httpx.request(method, url, headers=headers, json=json_body, timeout=self.timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise PayPalUnknown(f"{method} {path}: no answer ({type(e).__name__})") from e

        if discard_response:
            # The request was really sent. We are deliberately refusing to look
            # at what came back, which is exactly what a dropped response or a
            # killed process leaves us with.
            raise PayPalUnknown(f"{method} {path}: chaos, response discarded")

        if r.status_code >= 500:
            raise PayPalUnknown(f"{method} {path}: {r.status_code}")
        if r.status_code >= 400:
            raise PayPalRefused(f"{method} {path}: {r.status_code} {r.text[:300]}", r.status_code, r.text)
        return r.json() if r.content else {}

    # ---- orders ---------------------------------------------------------

    def create_order(self, *, amount_minor: int, currency: str, items=None, reference_id="default"):
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": reference_id,
                "amount": {"currency_code": currency, "value": to_provider(amount_minor, currency)},
            }],
        }
        return self._call("POST", "/v2/checkout/orders", json_body=body)

    def capture_order(self, order_id: str, *, request_id=None):
        return self._call("POST", f"/v2/checkout/orders/{order_id}/capture", json_body={}, request_id=request_id)

    def get_order(self, order_id: str):
        return self._call("GET", f"/v2/checkout/orders/{order_id}")

    def get_capture(self, capture_id: str):
        return self._call("GET", f"/v2/payments/captures/{capture_id}")

    # ---- the live read --------------------------------------------------

    def live_refunds(self, order_id: str) -> list[dict]:
        """Every refund PayPal itself believes exists on this order.

        This is the authority, not our table. A refund issued by hand in the
        PayPal dashboard leaves no row in our database and would be invisible
        to any local-only calculation.
        """
        order = self.get_order(order_id)
        out = []
        for pu in order.get("purchase_units", []):
            out.extend((pu.get("payments") or {}).get("refunds") or [])
        return out

    def refundable_balance_minor(self, *, order_id: str, capture_id: str, currency: str) -> int:
        capture = self.get_capture(capture_id)
        captured = from_provider(capture["amount"]["value"], currency)
        refunded = 0
        for r in self.live_refunds(order_id):
            amt = r.get("amount") or {}
            if amt.get("value") is not None:
                refunded += from_provider(amt["value"], currency)
        return captured - refunded

    # ---- the money move -------------------------------------------------

    def refund_capture(self, *, capture_id: str, amount_minor: int, currency: str, attempt_id: str, note=None):
        """`attempt_id` goes out twice, and both matter.

        As PayPal-Request-Id it is the idempotency key. As custom_id it is the
        handle the reconciler uses later to ask PayPal "did my attempt happen?"
        without that question being ambiguous.
        """
        body = {
            "amount": {"value": to_provider(amount_minor, currency), "currency_code": currency},
            "custom_id": attempt_id,
        }
        if note:
            body["note_to_payer"] = note[:255]
        discard = (self.chaos or "").lower() == "refund"
        return self._call(
            "POST",
            f"/v2/payments/captures/{capture_id}/refund",
            json_body=body,
            request_id=attempt_id,
            discard_response=discard,
        )
