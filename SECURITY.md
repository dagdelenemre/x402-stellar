# Security

> **Status: prototype, testnet only, not audited.** The notes below come from a self-review
> (2 November 2026), not from an independent audit. Do not use with real funds yet.

## Threat model

| Party | Controls | Must never be able to |
|---|---|---|
| **Payer (agent)** | the payment payload, every byte of the transaction XDR | make the facilitator move its own funds, pay for anything but the exact transfer, or pay less than the price |
| **Resource server** | the payment requirements (asset, amount, payTo, timeout) | make an agent pay in a token or amount it did not agree to |
| **Facilitator** | settlement; pays the network fee | (trusted by the resource server to report settlement honestly) |
| **Soroban RPC** | simulation results, including the auth entries a client is asked to sign | make a client sign an authorization for anything other than the payment |

Assets at stake: the payer's token balance, the facilitator's XLM (fees) and sequence number, and the
resource server's revenue (content must not be served unpaid).

### Trust assumptions (by design, not fixable in this package)

- The **resource server trusts its facilitator**: a dishonest facilitator can report `success` without
  settling. This is how x402 works; run your own facilitator or use one you trust.
- The **facilitator trusts its RPC** for simulation results and events.
- A **signed auth entry is a bearer authorization**. Whoever holds the payload can submit it once,
  until its expiration ledger. The client keeps that window to `maxTimeoutSeconds`.

## Findings of the self-review (all fixed, each with a regression test in `tests/test_security.py`)

Each fix was checked by removing it again: the matching test fails without it.

1. **[Medium] Client signed whatever authorization the RPC returned** — `client.py`
   The client only checked that the auth entry was for its own address. A malicious or compromised
   RPC could return an entry for a different call (e.g. a transfer of the whole balance to the
   attacker); the client would sign it and hand it to the resource server. The client now signs an
   entry only if its root invocation is byte-for-byte the `transfer(from, to, amount)` it built,
   with no sub-invocations.

2. **[Medium] Unsafe client defaults: any token, no spending cap** — `client.py`
   By default an agent would pay any amount in any SEP-41 token the server named. Defaults now match
   the official x402 SDK: only the network's USDC, at most 10,000,000 units ($1) per payment.
   Other tokens need `allowed_assets=[...]`; `max_amount=None` removes the cap explicitly.

3. **[Medium] Public facilitator app could be made to pay fees for anyone** — `facilitator_app.py`, `facilitator.py`
   `/settle` had no authentication, so anyone who could reach it could have the facilitator pay network
   fees (e.g. for 1-stroop transfers to themselves) and run arbitrary token contracts at its expense.
   Added `create_facilitator_app(..., api_key=...)` (Bearer, constant-time comparison) and
   `ExactStellarFacilitator(..., assets=[...])` to sponsor only listed tokens. The example server
   binds to `127.0.0.1`.

4. **[Low] `HEAD` ran paid Starlette handlers for free** — `server.py`
   Starlette serves `HEAD` with the `GET` handler; the middleware only matched `GET`, so the handler
   ran without payment (body stripped, side effects not). `HEAD` is now charged like `GET`.
   FastAPI was not affected (it answers `HEAD` with 405).

5. **[Low] Malformed input caused 500s instead of clean rejections** — `server.py`, `facilitator.py`, `facilitator_app.py`
   Non-object `accepted`, `paymentPayload` or `paymentRequirements` raised `AttributeError`.
   They now fail closed with 402 / 400 / an `invalid_*` reason.

## Known limitations (not fixed yet)

- **Concurrent settlements share one facilitator account.** Two settles at once can load the same
  sequence number; one fails with `tx_bad_seq` (no fee is charged for that). Fix planned: multiple
  signers / channel accounts, or fee-bump settlement as in `@x402/stellar`.
- **Settlement timeout is ambiguous.** If confirmation polling times out, settle reports failure even
  though the transaction may still land. A client that then pays again could pay twice. The auth
  entry's nonce prevents the *same* payload from settling twice.
- **No request size limit** in `facilitator_app`. Put it behind a reverse proxy with body limits.
- **The client does not clamp `maxTimeoutSeconds`.** A server asking for a very long timeout gets a
  long-lived authorization (for the agreed transfer only).
- **Example keys are stored in plain text** in `examples/testnet_accounts.json` (testnet, gitignored).
- `HTTPFacilitatorClient` sends its API key to whatever URL it is given; use `https://`.

## What the facilitator checks

Every MUST rule of the [`exact` scheme on Stellar](https://github.com/x402-foundation/x402/blob/main/specs/schemes/exact/scheme_exact_stellar.md),
with the same reason codes as `@x402/stellar`; see `tests/test_verify.py` for one attack test per rule.

## Reporting

Please report issues privately to the maintainer rather than in a public issue.
