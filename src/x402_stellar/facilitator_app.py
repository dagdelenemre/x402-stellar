"""A minimal x402 v2 facilitator HTTP service (``/verify``, ``/settle``, ``/supported``).

Run it yourself instead of relying on a hosted facilitator::

    app = create_facilitator_app(ExactStellarFacilitator(keypair, SorobanServer(rpc_url)), api_key="...")
    uvicorn.run(app, port=4021)

The facilitator pays the network fee of every payment it settles. Without
``api_key``, anyone who can reach ``/settle`` can make it pay fees (for example
for 1-stroop transfers to themselves), so either set a key or bind to localhost.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool

from .facilitator import ExactStellarFacilitator


def create_facilitator_app(facilitator: ExactStellarFacilitator, *, api_key: str | None = None) -> FastAPI:
    """Build the app. With ``api_key``, every route needs ``Authorization: Bearer <api_key>``."""

    async def require_api_key(authorization: str | None = Header(default=None)) -> None:
        if api_key is None:
            return
        expected = f"Bearer {api_key}".encode()
        if authorization is None or not hmac.compare_digest(authorization.encode(), expected):
            raise HTTPException(status_code=401, detail="Missing or invalid API key")

    app = FastAPI(title="x402 Stellar facilitator", dependencies=[Depends(require_api_key)])

    def parts(body: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        payload, requirements = body.get("paymentPayload"), body.get("paymentRequirements")
        if not isinstance(payload, dict) or not isinstance(requirements, dict):
            raise HTTPException(status_code=400, detail="paymentPayload and paymentRequirements must be JSON objects")
        return payload, requirements

    @app.get("/supported")
    def supported() -> dict[str, Any]:
        return facilitator.supported()

    @app.post("/verify")
    async def verify(body: dict[str, Any]) -> dict[str, Any]:
        return await run_in_threadpool(facilitator.verify, *parts(body))

    @app.post("/settle")
    async def settle(body: dict[str, Any]) -> dict[str, Any]:
        return await run_in_threadpool(facilitator.settle, *parts(body))

    return app
