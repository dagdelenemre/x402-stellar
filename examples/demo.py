"""One-command testnet demo: a Python agent pays a FastAPI API with x402 on Stellar.

    python examples/demo.py

What happens:
  1. Three testnet accounts are created and funded by Friendbot (first run only).
  2. A paid API starts on http://127.0.0.1:8402 — GET /weather costs 0.1 XLM.
     It settles payments with a facilitator running in the same process.
  3. A Python "agent" calls the API, gets HTTP 402, signs a payment, retries,
     and receives the data plus an on-chain receipt.
"""

from __future__ import annotations

import sys
import threading
import time

import uvicorn
from fastapi import FastAPI
from stellar_sdk import SorobanServer

from _common import NETWORK, RPC_URL, XLM_SAC, explorer, load_accounts, xlm_balance
from x402_stellar import (
    ExactStellarClient,
    ExactStellarFacilitator,
    PaidRoute,
    PaymentMiddleware,
    X402Client,
    read_payment_response,
)

PORT = 8402
PRICE_STROOPS = 1_000_000  # 0.1 XLM (7 decimals)


def build_api(seller_address: str, facilitator: ExactStellarFacilitator) -> FastAPI:
    app = FastAPI(title="Paid weather API")

    @app.get("/weather")
    def weather() -> dict:
        return {"city": "Samsun", "forecast": "Light rain, 14°C", "source": "x402-stellar demo"}

    app.add_middleware(
        PaymentMiddleware,
        routes={
            "GET /weather": PaidRoute(
                pay_to=seller_address,
                amount=PRICE_STROOPS,
                asset=XLM_SAC,
                network=NETWORK,
                description="Weather for Samsun",
            )
        },
        facilitator=facilitator,
    )
    return app


def main() -> None:
    print("\n1) Testnet accounts")
    accounts = load_accounts()
    for role, kp in accounts.items():
        print(f"   {role:<12} {kp.public_key}")

    rpc = SorobanServer(RPC_URL)
    facilitator = ExactStellarFacilitator(
        accounts["facilitator"],
        rpc,
        networks=(NETWORK,),
        # Spec default is 50,000 stroops. Raised for the demo so a busy testnet
        # does not fail the first run; the actual fee is printed below.
        max_transaction_fee_stroops=500_000,
    )

    print(f"\n2) Starting the paid API on http://127.0.0.1:{PORT}/weather (0.1 XLM per call)")
    server = uvicorn.Server(
        uvicorn.Config(build_api(accounts["seller"].public_key, facilitator), port=PORT, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)

    agent_addr, seller_addr = accounts["agent"].public_key, accounts["seller"].public_key
    before = {"agent": xlm_balance(rpc, agent_addr), "seller": xlm_balance(rpc, seller_addr)}

    print("\n3) The agent calls the API")
    # Pays only in XLM (the default is USDC only) and never more than 0.5 XLM.
    agent = ExactStellarClient(accounts["agent"], rpc, max_amount=5_000_000, allowed_assets=[XLM_SAC])
    started = time.monotonic()
    with X402Client(agent) as http:
        response = http.get(f"http://127.0.0.1:{PORT}/weather")
    elapsed = time.monotonic() - started
    receipt = read_payment_response(response) or {}

    print(f"   HTTP {response.status_code} in {elapsed:.1f}s")
    print(f"   data:    {response.json()}")
    if not receipt.get("success"):
        print(f"   payment failed: {receipt}")
        server.should_exit = True
        sys.exit(1)

    after = {"agent": xlm_balance(rpc, agent_addr), "seller": xlm_balance(rpc, seller_addr)}
    tx = rpc.get_transaction(receipt["transaction"])
    print(f"   payer:   {receipt['payer']}")
    print(f"   tx:      {explorer(receipt['transaction'])}")
    print("\n4) Balances (XLM)")
    print(f"   agent   {before['agent']:.7f} -> {after['agent']:.7f}  (paid the price, no fees)")
    print(f"   seller  {before['seller']:.7f} -> {after['seller']:.7f}")
    if getattr(tx, "envelope_xdr", None):
        from stellar_sdk import TransactionEnvelope
        from _common import PASSPHRASE

        fee = TransactionEnvelope.from_xdr(tx.envelope_xdr, PASSPHRASE).transaction.fee
        print(f"   network fee bid by the facilitator: {fee} stroops ({fee / 10**7:.7f} XLM)")

    server.should_exit = True
    print("\nDone. The agent paid 0.1 XLM over HTTP, the facilitator paid the network fee.\n")


if __name__ == "__main__":
    main()
