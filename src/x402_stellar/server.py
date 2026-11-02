"""Resource-server side: an ASGI middleware that puts x402 payments in front of routes.

Works with FastAPI, Starlette or any ASGI app::

    app = FastAPI()
    app.add_middleware(
        PaymentMiddleware,
        routes={"GET /weather": PaidRoute(price="$0.001", pay_to="G...")},
        facilitator=HTTPFacilitatorClient("https://..."),
    )
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool

from .constants import (
    DEFAULT_TIMEOUT_SECONDS,
    PAYMENT_REQUIRED_HEADER,
    PAYMENT_RESPONSE_HEADER,
    PAYMENT_SIGNATURE_HEADER,
    SCHEME_EXACT,
    STELLAR_TESTNET,
    X402_VERSION,
)
from .http import Facilitator
from .utils import decode_header, encode_header, is_valid_asset, is_valid_destination, parse_usd_price

ASGIApp = Callable[[dict, Callable, Callable], Awaitable[None]]

_MATCHED_FIELDS = ("scheme", "network", "asset", "amount", "payTo")


@dataclass
class PaidRoute:
    """Price of a route.

    Give either ``price`` in USD (paid in USDC, e.g. ``"$0.01"``) or an explicit
    ``amount`` in the token's smallest unit together with ``asset`` (a SEP-41
    contract address).
    """

    pay_to: str
    price: str | float | None = None
    amount: str | int | None = None
    asset: str | None = None
    network: str = STELLAR_TESTNET
    description: str = ""
    mime_type: str = "application/json"
    max_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    extra: dict[str, Any] = field(default_factory=dict)

    def requirements(self) -> dict[str, Any]:
        if not is_valid_destination(self.pay_to):
            raise ValueError(f"Invalid pay_to address: {self.pay_to}")
        if self.amount is not None:
            if not self.asset or not is_valid_asset(self.asset):
                raise ValueError("An explicit amount needs a SEP-41 asset contract address")
            amount, asset = str(int(self.amount)), self.asset
        elif self.price is not None:
            parsed = parse_usd_price(self.price, self.network)
            amount, asset = parsed["amount"], parsed["asset"]
        else:
            raise ValueError("A paid route needs either price or amount")
        return {
            "scheme": SCHEME_EXACT,
            "network": self.network,
            "amount": amount,
            "asset": asset,
            "payTo": self.pay_to,
            "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": {"areFeesSponsored": True, **self.extra},
        }


class PaymentMiddleware:
    """Answers 402 until a valid payment is attached, then verifies, settles and serves."""

    def __init__(self, app: ASGIApp, *, routes: dict[str, PaidRoute], facilitator: Facilitator) -> None:
        self.app = app
        self.facilitator = facilitator
        self.routes = {self._key(*key.split(" ", 1)): route for key, route in routes.items()}

    @staticmethod
    def _key(method: str, path: str) -> str:
        return f"{method.upper()} {path.rstrip('/') or '/'}"

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Starlette answers HEAD with the GET handler, so HEAD must be charged like GET.
        method = "GET" if scope["method"] == "HEAD" else scope["method"]
        route = self.routes.get(self._key(method, scope["path"]))
        if route is None:
            await self.app(scope, receive, send)
            return

        requirements = route.requirements()
        resource = {
            "url": self._url(scope),
            "description": route.description,
            "mimeType": route.mime_type,
        }
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        signature = headers.get(PAYMENT_SIGNATURE_HEADER.lower())
        if not signature:
            await self._payment_required(send, requirements, resource, "Payment required")
            return

        try:
            payment = decode_header(signature)
        except Exception:  # noqa: BLE001
            await self._payment_required(send, requirements, resource, "Malformed payment header")
            return

        accepted = payment.get("accepted")
        if not isinstance(accepted, dict):
            await self._payment_required(send, requirements, resource, "Malformed payment header")
            return
        if any(str(accepted.get(f)) != str(requirements[f]) for f in _MATCHED_FIELDS):
            await self._payment_required(send, requirements, resource, "Payment does not match the price")
            return

        verification = await run_in_threadpool(self.facilitator.verify, payment, requirements)
        if not verification.get("isValid"):
            await self._payment_required(send, requirements, resource, verification.get("invalidReason", "invalid"))
            return

        settlement = await run_in_threadpool(self.facilitator.settle, payment, requirements)
        if not settlement.get("success"):
            await self._payment_required(send, requirements, resource, settlement.get("errorReason", "settle_failed"))
            return

        encoded = encode_header(settlement).encode("latin-1")

        async def send_with_receipt(message: dict) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [
                        *message.get("headers", []),
                        (PAYMENT_RESPONSE_HEADER.lower().encode("latin-1"), encoded),
                        (b"access-control-expose-headers", PAYMENT_RESPONSE_HEADER.encode("latin-1")),
                    ],
                }
            await send(message)

        await self.app(scope, receive, send_with_receipt)

    @staticmethod
    def _url(scope: dict) -> str:
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
        return f"{scope.get('scheme', 'http')}://{host}{scope['path']}" if host else scope["path"]

    @staticmethod
    async def _payment_required(send: Callable, requirements: dict, resource: dict, error: str) -> None:
        body = {"x402Version": X402_VERSION, "error": error, "resource": resource, "accepts": [requirements]}
        await send(
            {
                "type": "http.response.start",
                "status": 402,
                "headers": [
                    (b"content-type", b"application/json"),
                    (PAYMENT_REQUIRED_HEADER.lower().encode("latin-1"), encode_header(body).encode("latin-1")),
                    (b"access-control-expose-headers", PAYMENT_REQUIRED_HEADER.encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": json.dumps(body).encode("utf-8")})
