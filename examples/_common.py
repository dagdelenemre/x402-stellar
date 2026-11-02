"""Shared helpers for the testnet examples. Testnet only: these keys hold play money."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from stellar_sdk import Account, Asset, Keypair, Network, SorobanServer, TransactionBuilder, scval

RPC_URL = "https://soroban-testnet.stellar.org"
FRIENDBOT_URL = "https://friendbot.stellar.org"
PASSPHRASE = Network.TESTNET_NETWORK_PASSPHRASE
NETWORK = "stellar:testnet"
# Native XLM through its Stellar Asset Contract (SEP-41). No trustline or faucet
# needed beyond Friendbot, which makes it the simplest token for a first demo.
XLM_SAC = Asset.native().contract_id(PASSPHRASE)
ACCOUNTS_FILE = Path(__file__).with_name("testnet_accounts.json")
ROLES = ("agent", "seller", "facilitator")


def load_accounts() -> dict[str, Keypair]:
    """Create (once) and fund three testnet accounts, then reuse them."""
    if ACCOUNTS_FILE.exists():
        secrets = json.loads(ACCOUNTS_FILE.read_text())
        return {role: Keypair.from_secret(secrets[role]) for role in ROLES}

    accounts = {role: Keypair.random() for role in ROLES}
    with httpx.Client(timeout=60) as http:
        for role, kp in accounts.items():
            print(f"  Friendbot is funding the {role} account {kp.public_key[:8]}...")
            response = http.get(FRIENDBOT_URL, params={"addr": kp.public_key})
            if response.status_code != 200 and "createAccountAlreadyExist" not in response.text:
                raise SystemExit(f"Friendbot failed for {role}: {response.status_code} {response.text[:200]}")
    ACCOUNTS_FILE.write_text(json.dumps({role: kp.secret for role, kp in accounts.items()}, indent=2))
    return accounts


def xlm_balance(rpc: SorobanServer, address: str) -> float:
    """Read a balance through the token contract (same path x402 uses)."""
    tx = (
        TransactionBuilder(Account(address, 0), PASSPHRASE, base_fee=100)
        .append_invoke_contract_function_op(XLM_SAC, "balance", [scval.to_address(address)])
        .set_timeout(30)
        .build()
    )
    result = rpc.simulate_transaction(tx)
    if result.error or not result.results:
        raise RuntimeError(f"balance lookup failed: {result.error}")
    from stellar_sdk import xdr

    return scval.from_int128(xdr.SCVal.from_xdr(result.results[0].xdr)) / 10**7


def explorer(tx_hash: str) -> str:
    return f"https://stellar.expert/explorer/testnet/tx/{tx_hash}"
