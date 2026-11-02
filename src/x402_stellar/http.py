"""HTTP transport for x402 v2: a paying httpx client and a remote facilitator client."""

from __future__ import annotations

from typing import Any, Protocol

import httpx

from .client import ExactStellarClient
from .constants import (
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    X402_VERSION,
)
from .errors import PaymentCreationError, PaymentRejectedError
from .utils import decode_header, encode_header


class Facilitator(Protocol):
    """Anything that can verify and settle: a local facilitator or a remote one."""

    def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]: ...

    def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]: ...


class HTTPFacilitatorClient:
    """Talks to any x402 v2 facilitator over HTTP (``/verify``, ``/settle``, ``/supported``).

    Works with this package's facilitator app and with hosted facilitators such as
    Built on Stellar (pass its API key).
    """

    def __init__(
        self,
        url: str,
        *,
        api_key: str | None = None,
        timeout: float = 90.0,
        http: httpx.Client | None = None,
    ) -> None:
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = http or httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=timeout)

    def _post(self, path: str, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        body = {
            "x402Version": payload.get("x402Version", X402_VERSION),
            "paymentPayload": payload,
            "paymentRequirements": requirements,
        }
        response = self._http.post(path, json=body)
        response.raise_for_status()
        return response.json()

    def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        return self._post("/verify", payload, requirements)

    def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        return self._post("/settle", payload, requirements)

    def supported(self) -> dict[str, Any]:
        response = self._http.get("/supported")
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._http.close()


def read_payment_required(response: httpx.Response) -> dict[str, Any]:
    header = response.headers.get(PAYMENT_REQUIRED_HEADER)
    if header:
        return decode_header(header)
    body = response.json()
    if isinstance(body, dict) and "accepts" in body:
        return body
    raise PaymentCreationError("402 response carries no x402 payment requirements")


def read_payment_response(response: httpx.Response) -> dict[str, Any] | None:
    header = response.headers.get(PAYMENT_RESPONSE_HEADER)
    return decode_header(header) if header else None


class X402Client:
    """An httpx wrapper that pays x402 challenges on Stellar and retries once.

    Example::

        with X402Client(ExactStellarClient(keypair, SorobanServer(rpc_url))) as http:
            response = http.get("http://localhost:8000/weather")
            print(response.json(), read_payment_response(response))
    """

    def __init__(self, scheme: ExactStellarClient, http: httpx.Client | None = None) -> None:
        self.scheme = scheme
        self.http = http or httpx.Client(timeout=120.0)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        response = self.http.request(method, url, **kwargs)
        if response.status_code != 402:
            return response

        required = read_payment_required(response)
        requirements = next((r for r in required.get("accepts", []) if self.scheme.supports(r)), None)
        if requirements is None:
            raise PaymentCreationError("Server offers no payment option on Stellar (exact scheme)")

        payment = {
            "x402Version": X402_VERSION,
            "resource": required.get("resource"),
            "accepted": requirements,
            "payload": self.scheme.create_payment_payload(requirements),
        }
        headers = dict(kwargs.pop("headers", None) or {})
        headers[PAYMENT_SIGNATURE_HEADER] = encode_header(payment)
        paid = self.http.request(method, url, headers=headers, **kwargs)
        if paid.status_code == 402:
            reason = read_payment_required(paid).get("error", "payment rejected")
            raise PaymentRejectedError(f"Server rejected the payment: {reason}")
        return paid

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> X402Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
