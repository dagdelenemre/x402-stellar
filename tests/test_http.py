"""End to end over HTTP, in process: agent -> paid FastAPI route -> facilitator."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from x402_stellar import (
    ExactStellarClient,
    HTTPFacilitatorClient,
    PaidRoute,
    PaymentCreationError,
    PaymentMiddleware,
    X402Client,
    read_payment_response,
)
from x402_stellar.facilitator_app import create_facilitator_app
from x402_stellar.utils import decode_header

from .conftest import AMOUNT, XLM_SAC


def make_app(facilitator, payee) -> FastAPI:
    app = FastAPI()

    @app.get("/weather")
    def weather():
        return {"city": "Samsun", "forecast": "rainy"}

    @app.get("/free")
    def free():
        return {"ok": True}

    app.add_middleware(
        PaymentMiddleware,
        routes={"GET /weather": PaidRoute(pay_to=payee.public_key, amount=AMOUNT, asset=XLM_SAC, description="Weather")},
        facilitator=facilitator,
    )
    return app


def test_unpaid_request_gets_402_with_requirements(facilitator, payee):
    response = TestClient(make_app(facilitator, payee)).get("/weather")
    assert response.status_code == 402
    required = decode_header(response.headers["PAYMENT-REQUIRED"])
    assert required["x402Version"] == 2
    (accepts,) = required["accepts"]
    assert accepts["payTo"] == payee.public_key
    assert accepts["amount"] == AMOUNT
    assert accepts["extra"] == {"areFeesSponsored": True}


def test_free_routes_are_untouched(facilitator, payee):
    assert TestClient(make_app(facilitator, payee)).get("/free").json() == {"ok": True}


def test_agent_pays_and_gets_content(facilitator, payee, client, rpc, payer):
    app = make_app(facilitator, payee)
    with X402Client(client, http=TestClient(app)) as agent:
        response = agent.get("/weather")
    assert response.status_code == 200
    assert response.json()["city"] == "Samsun"
    receipt = read_payment_response(response)
    assert receipt["success"] is True
    assert receipt["payer"] == payer.public_key
    assert receipt["transaction"] == rpc.sent[0].hash_hex()


def test_spending_cap_blocks_expensive_payment(facilitator, payee, payer, rpc):
    capped = ExactStellarClient(payer, rpc, max_amount=int(AMOUNT) - 1, allowed_assets=[XLM_SAC])
    with X402Client(capped, http=TestClient(make_app(facilitator, payee))) as agent, pytest.raises(PaymentCreationError):
        agent.get("/weather")
    assert rpc.sent == []


def test_payment_for_cheaper_price_is_refused(facilitator, payee, client, rpc):
    app = make_app(facilitator, payee)
    cheap = {
        "scheme": "exact",
        "network": "stellar:testnet",
        "amount": "1",
        "asset": XLM_SAC,
        "payTo": payee.public_key,
        "maxTimeoutSeconds": 60,
        "extra": {"areFeesSponsored": True},
    }
    from x402_stellar.utils import encode_header

    header = encode_header({"x402Version": 2, "accepted": cheap, "payload": client.create_payment_payload(cheap)})
    response = TestClient(app).get("/weather", headers={"PAYMENT-SIGNATURE": header})
    assert response.status_code == 402
    assert response.json()["error"] == "Payment does not match the price"
    assert rpc.sent == []


def test_remote_facilitator_over_http(facilitator, payee, client, rpc):
    remote = HTTPFacilitatorClient("http://facilitator", http=TestClient(create_facilitator_app(facilitator)))
    assert remote.supported()["kinds"][0]["network"] == "stellar:testnet"
    with X402Client(client, http=TestClient(make_app(remote, payee))) as agent:
        response = agent.get("/weather")
    assert response.status_code == 200
    assert read_payment_response(response)["success"] is True
    assert len(rpc.sent) == 1


def test_usd_price_maps_to_usdc(payee):
    requirements = PaidRoute(pay_to=payee.public_key, price="$0.01").requirements()
    assert requirements["amount"] == "100000"  # USDC has 7 decimals on Stellar
    assert requirements["asset"] == "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"
