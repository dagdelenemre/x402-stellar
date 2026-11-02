"""Run a standalone x402 facilitator for Stellar testnet on http://127.0.0.1:4021.

    python examples/facilitator_server.py

Resource servers can then use ``HTTPFacilitatorClient("http://127.0.0.1:4021")``.
"""

from __future__ import annotations

import uvicorn
from stellar_sdk import SorobanServer

from _common import NETWORK, RPC_URL, load_accounts
from x402_stellar import ExactStellarFacilitator
from x402_stellar.facilitator_app import create_facilitator_app

facilitator = ExactStellarFacilitator(load_accounts()["facilitator"], SorobanServer(RPC_URL), networks=(NETWORK,))
uvicorn.run(create_facilitator_app(facilitator), host="127.0.0.1", port=4021)
