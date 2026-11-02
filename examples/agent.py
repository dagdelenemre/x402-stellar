"""Pay any x402 endpoint on Stellar testnet from Python.

    python examples/agent.py http://127.0.0.1:8402/weather
"""

from __future__ import annotations

import sys

from stellar_sdk import SorobanServer

from _common import RPC_URL, XLM_SAC, explorer, load_accounts
from x402_stellar import USDC_TESTNET, ExactStellarClient, X402Client, read_payment_response

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8402/weather"
agent = ExactStellarClient(
    load_accounts()["agent"], SorobanServer(RPC_URL), max_amount=5_000_000, allowed_assets=[XLM_SAC, USDC_TESTNET]
)

with X402Client(agent) as http:
    response = http.get(url)

receipt = read_payment_response(response) or {}
print(response.status_code, response.json())
if receipt.get("transaction"):
    print("paid:", explorer(receipt["transaction"]))
