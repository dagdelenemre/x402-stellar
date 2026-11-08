# x402-stellar

x402 payments on Stellar for Python.

[x402](https://x402.org) turns HTTP `402 Payment Required` into a real payment flow, so APIs can charge per request and AI agents can pay without accounts or API keys. Stellar supports x402 in the official TypeScript SDK (`@x402/stellar`), but the official Python SDK only ships EVM, Solana and TON, and many agent frameworks are Python. This package fills that gap.

- **Pay** from any Python script or agent: `X402Client` handles the 402, signs, retries.
- **Charge** from FastAPI/Starlette: `PaymentMiddleware` puts a price on routes.
- **Settle** yourself: `ExactStellarFacilitator` verifies and submits payments, or point at any x402 v2 facilitator with `HTTPFacilitatorClient`.

It implements the [`exact` scheme on Stellar](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_stellar.md) (x402 v2): the client signs only a Soroban authorization entry for `transfer(from, to, amount)` on a SEP-41 token, and the facilitator pays the network fee.

> **Status: early release (0.1), testnet.** Self-reviewed ([SECURITY.md](https://github.com/dagdelenemre/x402-stellar/blob/main/SECURITY.md)), not audited. Do not use with real funds yet.

## Install

```bash
pip install x402-stellar             # pay with an agent, run a facilitator
pip install "x402-stellar[server]"   # + FastAPI/Starlette payment middleware
pip install "x402-stellar[sdk]"      # + adapter for the official x402 Python SDK
```

From a checkout, for the examples and tests: `pip install -e ".[demo,dev]"`.

## Pay for something (agent side)

```python
from stellar_sdk import Keypair, SorobanServer
from x402_stellar import ExactStellarClient, X402Client, read_payment_response

agent = ExactStellarClient(
    Keypair.from_secret("S..."),
    SorobanServer("https://soroban-testnet.stellar.org"),
    max_amount=5_000_000,  # refuse anything above 0.5 USDC (7 decimals); default is $1
    # allowed_assets=[...],  # default: the network's USDC only
)
with X402Client(agent) as http:
    response = http.get("https://api.example.com/weather")
print(response.json(), read_payment_response(response))
```

## Charge for a route (server side)

```python
from fastapi import FastAPI
from x402_stellar import HTTPFacilitatorClient, PaidRoute, PaymentMiddleware

app = FastAPI()

@app.get("/weather")
def weather():
    return {"forecast": "sunny"}

app.add_middleware(
    PaymentMiddleware,
    routes={"GET /weather": PaidRoute(pay_to="G...", price="$0.001")},  # paid in USDC
    facilitator=HTTPFacilitatorClient("http://127.0.0.1:4021"),
)
```

## Run your own facilitator

```python
from stellar_sdk import Keypair, SorobanServer
from x402_stellar import ExactStellarFacilitator
from x402_stellar.facilitator_app import create_facilitator_app

facilitator = ExactStellarFacilitator(
    Keypair.from_secret("S..."),
    SorobanServer("https://soroban-testnet.stellar.org"),
    assets=["CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"],  # sponsor fees for USDC only
)
app = create_facilitator_app(facilitator, api_key="...")  # serves /verify, /settle, /supported
```

The facilitator pays every settlement's network fee: give it an `api_key` (or keep it on localhost) and an `assets` list. See [SECURITY.md](https://github.com/dagdelenemre/x402-stellar/blob/main/SECURITY.md).

## Use it with the official x402 Python SDK

Already on `pip install x402`? Stellar plugs in the same way the built-in EVM/SVM/TVM mechanisms do:

```bash
pip install "x402-stellar[sdk]"
```

```python
from stellar_sdk import Keypair, SorobanServer
from x402 import x402Client, x402Facilitator, x402ResourceServer
from x402_stellar.sdk import (
    register_exact_stellar_client,
    register_exact_stellar_facilitator,
    register_exact_stellar_server,
)

rpc = SorobanServer("https://soroban-testnet.stellar.org")

# Agent: pays on any stellar:* network.
agent = register_exact_stellar_client(x402Client(), Keypair.from_secret("S..."), rpc=rpc)

# Facilitator: verifies and settles (or use a remote one with x402.http.HTTPFacilitatorClient).
facilitator = register_exact_stellar_facilitator(x402Facilitator(), Keypair.from_secret("S..."), rpc)

# Resource server: prices routes for the official FastAPI/Flask middleware.
server = register_exact_stellar_server(x402ResourceServer(facilitator))
```

Everything else (FastAPI/Flask middleware, httpx/requests clients, hooks, spend controls) is the official SDK. Its default spend controls allow Stellar USDC capped in USD; any other token, native XLM included, needs an `allowed_assets` entry. `examples/sdk_demo.py` runs the whole flow on testnet with only official SDK components.

## What the facilitator checks

The facilitator enforces every MUST rule in the spec and returns the same reason codes as `@x402/stellar`:

- one `invokeHostFunction` calling `transfer` on the required asset, to the required recipient, for the exact amount
- its own account never appears as transaction source, operation source, payer or auth signer
- only address credentials (legacy and CAP-71 V2), no sub-invocations, signature expiry bounded by `maxTimeoutSeconds`
- a fresh simulation must succeed and emit exactly one matching transfer event, and nothing else that moves balances
- settlement re-verifies from scratch, rebuilds with the facilitator as source, takes fee and resources from the new simulation (never from the client), and refuses fees above a ceiling (default 50,000 stroops)

## Testnet demo

```bash
python examples/demo.py
```

Creates three Friendbot-funded accounts (agent, seller, facilitator), starts a paid API on port 8402, and has the agent pay 0.1 XLM for `GET /weather`. Prints the response, the on-chain transaction link and balances.

`python examples/sdk_demo.py` does the same on port 8403 using the official x402 SDK's facilitator, resource server, FastAPI middleware and httpx client.

## Tests

```bash
pip install -e ".[dev]" && pytest
```

65 tests run against an in-memory Soroban RPC that checks ed25519 signatures on auth entries, covering the happy path, each attack the spec guards against, the official x402 SDK integration end to end, and regression tests for the security review ([SECURITY.md](https://github.com/dagdelenemre/x402-stellar/blob/main/SECURITY.md)).

## Roadmap

- [x] `exact` scheme: client, resource server middleware, facilitator
- [x] Verified end-to-end on testnet (standalone and through the official x402 SDK)
- [x] Plugs into the official x402 Python SDK (`x402_stellar.sdk`)
- [ ] Mainnet
- [ ] Cross-stack tests against `@x402/stellar` (TS) facilitator and server
- [ ] Fee-bump settlement and multiple facilitator signers
- [ ] Contribute as `x402[stellar]` to the official Python SDK

## License

MIT
