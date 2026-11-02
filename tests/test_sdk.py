"""The Stellar mechanism plugged into the official x402 Python SDK (``pip install x402``)."""

from __future__ import annotations

import asyncio
import copy

import httpx
import pytest
from fastapi import FastAPI

pytest.importorskip("x402")

from x402 import x402Client, x402ClientSync, x402Facilitator, x402FacilitatorSync, x402ResourceServer  # noqa: E402
from x402.http import decode_payment_response_header  # noqa: E402
from x402.http.clients.httpx import x402AsyncTransport  # noqa: E402
from x402.http.middleware.fastapi import payment_middleware  # noqa: E402
from x402.schemas import AssetAmount, PaymentPayload, PaymentRequired, PaymentRequirements, SupportedKind  # noqa: E402

from x402_stellar import USDC_TESTNET  # noqa: E402
from x402_stellar.sdk import (  # noqa: E402
    ExactStellarClientScheme,
    ExactStellarServerScheme,
    register_exact_stellar_client,
    register_exact_stellar_facilitator,
    register_exact_stellar_server,
)

from .conftest import AMOUNT, XLM_SAC  # noqa: E402


@pytest.fixture
def sdk_facilitator(facilitator_kp, rpc) -> x402FacilitatorSync:
    return register_exact_stellar_facilitator(x402FacilitatorSync(), facilitator_kp, rpc, sleep=lambda _: None)


def payment_required(requirements: dict) -> PaymentRequired:
    return PaymentRequired(accepts=[PaymentRequirements.model_validate(requirements)])


def allow_xlm(client):
    """XLM is not a default asset, so the SDK's spend controls need an explicit opt-in."""
    return client.set_spend_controls({"allowed_assets": [{"network": "stellar:testnet", "asset": XLM_SAC}]})


# ---------------------------------------------------------------- client side


def test_sdk_client_builds_a_payload_our_facilitator_accepts(payer, rpc, facilitator, requirements):
    client = allow_xlm(register_exact_stellar_client(x402ClientSync(), payer, rpc=rpc))
    payload = client.create_payment_payload(payment_required(requirements))

    assert isinstance(payload, PaymentPayload)
    assert payload.x402_version == 2
    assert payload.accepted.network == "stellar:testnet"
    wire = payload.model_dump(by_alias=True, mode="json")
    assert facilitator.verify(wire, requirements) == {"isValid": True, "payer": payer.public_key}


def test_sdk_client_spending_cap(payer, rpc, requirements):
    client = allow_xlm(register_exact_stellar_client(x402ClientSync(), payer, rpc=rpc, max_amount=int(AMOUNT) - 1))
    with pytest.raises(Exception, match="exceeds this client's limit"):
        client.create_payment_payload(payment_required(requirements))


def test_sdk_spend_controls_refuse_unlisted_tokens(payer, rpc, requirements):
    client = register_exact_stellar_client(x402ClientSync(), payer, rpc=rpc)
    with pytest.raises(Exception, match="rejected by spend_controls"):
        client.create_payment_payload(payment_required(requirements))


def test_sdk_spend_controls_know_stellar_usdc(payer, rpc, requirements):
    client = register_exact_stellar_client(x402ClientSync(), payer, rpc=rpc)
    cheap = {**requirements, "asset": USDC_TESTNET, "amount": "100000"}  # $0.01
    assert client.create_payment_payload(payment_required(cheap)).payload["transaction"]

    expensive = {**requirements, "asset": USDC_TESTNET, "amount": str(1_000 * 10**7)}  # $1,000
    with pytest.raises(Exception, match="max_amount_per_payment"):
        client.create_payment_payload(payment_required(expensive))


def test_pubnet_needs_an_explicit_rpc(payer, requirements):
    scheme = ExactStellarClientScheme(payer)
    pubnet = PaymentRequirements.model_validate({**requirements, "network": "stellar:pubnet"})
    with pytest.raises(ValueError, match="Pass an rpc for stellar:pubnet"):
        scheme.create_payment_payload(pubnet)


# ----------------------------------------------------------- facilitator side


def test_supported_advertises_stellar(sdk_facilitator, facilitator_kp):
    supported = sdk_facilitator.get_supported().model_dump(by_alias=True)
    assert supported["kinds"] == [
        {"x402Version": 2, "scheme": "exact", "network": "stellar:testnet", "extra": {"areFeesSponsored": True}}
    ]
    assert supported["signers"] == {"stellar:*": [facilitator_kp.public_key]}


def test_sdk_facilitator_verifies_and_settles(sdk_facilitator, payment, requirements, rpc, payer):
    payload = PaymentPayload.model_validate(payment)
    reqs = PaymentRequirements.model_validate(requirements)

    verified = sdk_facilitator.verify(payload, reqs)
    assert verified.is_valid is True
    assert verified.payer == payer.public_key

    settled = sdk_facilitator.settle(payload, reqs)
    assert settled.success is True
    assert settled.transaction == rpc.sent[0].hash_hex()
    assert settled.network == "stellar:testnet"


def test_sdk_facilitator_keeps_our_reason_codes(sdk_facilitator, payment, requirements, rpc):
    tampered = copy.deepcopy(requirements)
    tampered["amount"] = str(int(AMOUNT) + 1)
    payload = PaymentPayload.model_validate({**payment, "accepted": tampered})

    verified = sdk_facilitator.verify(payload, PaymentRequirements.model_validate(tampered))
    assert verified.is_valid is False
    assert verified.invalid_reason == "invalid_exact_stellar_payload_wrong_amount"

    settled = sdk_facilitator.settle(payload, PaymentRequirements.model_validate(tampered))
    assert settled.success is False
    assert settled.error_reason == "invalid_exact_stellar_payload_wrong_amount"
    assert rpc.sent == []


# ---------------------------------------------------------------- server side


def test_usd_price_maps_to_usdc():
    asset = ExactStellarServerScheme().parse_price("$0.01", "stellar:testnet")
    assert (asset.amount, asset.asset) == ("100000", USDC_TESTNET)


def test_explicit_asset_amount_passes_through():
    scheme = ExactStellarServerScheme()
    assert scheme.parse_price({"amount": AMOUNT, "asset": XLM_SAC}, "stellar:testnet").asset == XLM_SAC
    with pytest.raises(ValueError, match="Asset address must be specified"):
        scheme.parse_price({"amount": AMOUNT}, "stellar:testnet")


def test_custom_money_parser_runs_before_usdc():
    scheme = ExactStellarServerScheme().register_money_parser(
        lambda amount, network: AssetAmount(amount="7", asset=XLM_SAC) if network == "stellar:testnet" else None
    )
    assert scheme.parse_price("$1", "stellar:testnet").asset == XLM_SAC
    with pytest.raises(ValueError, match="No default asset"):
        scheme.parse_price("$1", "stellar:futurenet")


def test_fee_sponsorship_comes_from_the_facilitator(requirements):
    scheme = ExactStellarServerScheme()
    bare = PaymentRequirements.model_validate({**requirements, "extra": {}})
    kind = SupportedKind(x402_version=2, scheme="exact", network="stellar:testnet", extra={"areFeesSponsored": True})
    assert scheme.enhance_payment_requirements(bare, kind, []).extra == {"areFeesSponsored": True}


# ------------------------------------------------------------------ end to end


def test_end_to_end_with_official_sdk_only(payer, payee, facilitator_kp, rpc):
    """Official FastAPI middleware + official httpx client + official facilitator, Stellar inside."""
    facilitator = register_exact_stellar_facilitator(x402Facilitator(), facilitator_kp, rpc, sleep=lambda _: None)
    server = register_exact_stellar_server(x402ResourceServer(facilitator))
    routes = {
        "GET /weather": {
            "accepts": {
                "scheme": "exact",
                "network": "stellar:testnet",
                "payTo": payee.public_key,
                "price": {"amount": AMOUNT, "asset": XLM_SAC},
            },
            "description": "Weather",
        }
    }

    app = FastAPI()
    middleware = payment_middleware(routes, server)

    @app.middleware("http")
    async def x402(request, call_next):
        return await middleware(request, call_next)

    @app.get("/weather")
    def weather():
        return {"city": "Samsun"}

    agent = allow_xlm(register_exact_stellar_client(x402Client(), payer, rpc=rpc))

    async def run() -> tuple[httpx.Response, httpx.Response]:
        asgi = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=asgi, base_url="http://test") as plain:
            unpaid = await plain.get("/weather")
        async with httpx.AsyncClient(transport=x402AsyncTransport(agent, asgi), base_url="http://test") as paying:
            paid = await paying.get("/weather")
        return unpaid, paid

    unpaid, paid = asyncio.run(run())

    assert unpaid.status_code == 402
    assert paid.status_code == 200
    assert paid.json() == {"city": "Samsun"}
    receipt = decode_payment_response_header(paid.headers["PAYMENT-RESPONSE"])
    assert receipt.success is True
    assert receipt.payer == payer.public_key
    assert receipt.transaction == rpc.sent[0].hash_hex()
    assert len(rpc.sent) == 1
