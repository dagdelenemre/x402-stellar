"""Testnet demo on the official x402 Python SDK, with Stellar plugged in.

    pip install -e ".[sdk,demo]"
    python examples/sdk_demo.py

Same flow as demo.py, but every moving part is the official SDK (``pip install x402``):
x402Facilitator, x402ResourceServer, the FastAPI payment middleware and the httpx
client. x402-stellar only contributes the Stellar mechanism via ``register_*``.
"""

from __future__ import annotations

import asyncio
import threading
import time

import uvicorn
from fastapi import FastAPI
from stellar_sdk import SorobanServer
from x402 import x402Client, x402Facilitator, x402ResourceServer
from x402.http import decode_payment_response_header
from x402.http.clients import x402HttpxClient
from x402.http.middleware.fastapi import payment_middleware

from _common import NETWORK, RPC_URL, XLM_SAC, explorer, load_accounts, xlm_balance
from x402_stellar.sdk import (
    register_exact_stellar_client,
    register_exact_stellar_facilitator,
    register_exact_stellar_server,
)

PORT = 8403
PRICE_STROOPS = "1000000"  # 0.1 XLM (7 decimals)


def build_api(seller_address: str, server: x402ResourceServer) -> FastAPI:
    app = FastAPI(title="Paid weather API (official x402 SDK)")
    routes = {
        "GET /weather": {
            "accepts": {
                "scheme": "exact",
                "network": NETWORK,
                "payTo": seller_address,
                "price": {"amount": PRICE_STROOPS, "asset": XLM_SAC},
            },
            "description": "Weather for Samsun",
        }
    }
    middleware = payment_middleware(routes, server)

    @app.middleware("http")
    async def x402(request, call_next):
        return await middleware(request, call_next)

    @app.get("/weather")
    def weather() -> dict:
        return {"city": "Samsun", "forecast": "Light rain, 14 C", "source": "official x402 SDK + x402-stellar"}

    return app


async def pay(agent: x402Client) -> tuple:
    async with x402HttpxClient(agent, timeout=120) as http:
        started = time.monotonic()
        response = await http.get(f"http://127.0.0.1:{PORT}/weather")
        return response, time.monotonic() - started


def main() -> None:
    print("\n1) Testnet accounts")
    accounts = load_accounts()
    for role, kp in accounts.items():
        print(f"   {role:<12} {kp.public_key}")

    rpc = SorobanServer(RPC_URL)
    facilitator = register_exact_stellar_facilitator(
        x402Facilitator(),
        accounts["facilitator"],
        rpc,
        NETWORK,
        # Spec default is 50,000 stroops; raised so a busy testnet does not fail the demo.
        max_transaction_fee_stroops=500_000,
    )
    server = register_exact_stellar_server(x402ResourceServer(facilitator))

    print(f"\n2) Official x402 FastAPI middleware on http://127.0.0.1:{PORT}/weather (0.1 XLM per call)")
    uv = uvicorn.Server(uvicorn.Config(build_api(accounts["seller"].public_key, server), port=PORT, log_level="warning"))
    threading.Thread(target=uv.run, daemon=True).start()
    while not uv.started:
        time.sleep(0.1)

    agent_addr, seller_addr = accounts["agent"].public_key, accounts["seller"].public_key
    before = {"agent": xlm_balance(rpc, agent_addr), "seller": xlm_balance(rpc, seller_addr)}

    print("\n3) The agent (official x402Client + x402HttpxClient) calls the API")
    agent = register_exact_stellar_client(x402Client(), accounts["agent"], rpc=rpc)
    # XLM is not a default asset, so the SDK's spend controls need an explicit opt-in.
    agent.set_spend_controls(
        {"allowed_assets": [{"network": NETWORK, "asset": XLM_SAC, "max_amount_per_payment": "5000000"}]}
    )
    response, elapsed = asyncio.run(pay(agent))

    print(f"   HTTP {response.status_code} in {elapsed:.1f}s")
    if response.status_code != 200 or "PAYMENT-RESPONSE" not in response.headers:
        print(f"   payment failed: {response.headers.get('PAYMENT-REQUIRED') or response.text}")
        uv.should_exit = True
        raise SystemExit(1)
    receipt = decode_payment_response_header(response.headers["PAYMENT-RESPONSE"])
    print(f"   data:    {response.json()}")
    print(f"   payer:   {receipt.payer}")
    print(f"   tx:      {explorer(receipt.transaction)}")

    after = {"agent": xlm_balance(rpc, agent_addr), "seller": xlm_balance(rpc, seller_addr)}
    print("\n4) Balances (XLM)")
    print(f"   agent   {before['agent']:.7f} -> {after['agent']:.7f}")
    print(f"   seller  {before['seller']:.7f} -> {after['seller']:.7f}")

    uv.should_exit = True
    print("\nDone. Official x402 SDK end to end, Stellar mechanism from x402-stellar.\n")


if __name__ == "__main__":
    main()
