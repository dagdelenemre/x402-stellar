"""Regression tests for the findings of the security review (see SECURITY.md)."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from stellar_sdk import Address, Keypair, TransactionEnvelope, scval, xdr

from x402_stellar import ExactStellarClient, ExactStellarFacilitator, PaidRoute, PaymentCreationError, PaymentMiddleware
from x402_stellar.facilitator_app import create_facilitator_app
from x402_stellar.utils import encode_header

from .conftest import AMOUNT, PASSPHRASE, USDC, XLM_SAC
from .fake_rpc import FakeRpc


class RewritingRpc(FakeRpc):
    """A malicious RPC: in recording mode it asks the payer to authorize a different call."""

    def __init__(self, rewrite, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.rewrite = rewrite

    def simulate_transaction(self, envelope: Any, *args: Any, **kwargs: Any):
        response = super().simulate_transaction(envelope, *args, **kwargs)
        if envelope.transaction.operations[0].auth:  # enforcing mode: leave it alone
            return response
        entry = xdr.SorobanAuthorizationEntry.from_xdr(response.results[0].auth[0])
        self.rewrite(entry)
        response.results[0].auth = [entry.to_xdr()]
        return response


def _steal(entry: xdr.SorobanAuthorizationEntry) -> None:
    args = entry.root_invocation.function.contract_fn.args
    args[1] = scval.to_address(Keypair.random().public_key)  # someone else's account
    args[2] = scval.to_int128(10**15)  # and much more money


def _nest(entry: xdr.SorobanAuthorizationEntry) -> None:
    entry.root_invocation.sub_invocations.append(copy.deepcopy(entry.root_invocation))


# --- client: never sign what the RPC asks blindly (finding 1) -------------------


@pytest.mark.parametrize("rewrite", [_steal, _nest], ids=["different-transfer", "sub-invocation"])
def test_client_refuses_to_sign_an_entry_the_rpc_rewrote(payer, requirements, rewrite):
    rpc = RewritingRpc(rewrite, passphrase=PASSPHRASE)
    client = ExactStellarClient(payer, rpc, allowed_assets=[XLM_SAC])
    with pytest.raises(PaymentCreationError, match="something other than this payment"):
        client.create_payment_payload(requirements)


# --- client: safe defaults (finding 2) ---------------------------------------


def test_default_client_pays_only_usdc(payer, rpc, requirements):
    client = ExactStellarClient(payer, rpc)
    with pytest.raises(PaymentCreationError, match="not in this client's allowed_assets"):
        client.create_payment_payload(requirements)  # XLM
    assert client.create_payment_payload({**requirements, "asset": USDC})["transaction"]


def test_default_client_caps_a_payment_at_one_dollar(payer, rpc, requirements):
    client = ExactStellarClient(payer, rpc)
    assert client.create_payment_payload({**requirements, "asset": USDC, "amount": "10000000"})["transaction"]
    with pytest.raises(PaymentCreationError, match="exceeds this client's limit"):
        client.create_payment_payload({**requirements, "asset": USDC, "amount": "10000001"})


def test_a_bare_address_is_not_treated_as_a_substring_set(payer, rpc, requirements):
    client = ExactStellarClient(payer, rpc, allowed_assets=XLM_SAC)
    assert client.create_payment_payload(requirements)["transaction"]
    with pytest.raises(PaymentCreationError, match="allowed_assets"):
        client.create_payment_payload({**requirements, "asset": USDC})


# --- facilitator: fee sponsorship is not open to everyone (finding 3) ---------


def test_facilitator_sponsors_only_listed_assets(facilitator_kp, rpc, payment, requirements):
    only_usdc = ExactStellarFacilitator(facilitator_kp, rpc, assets=[USDC], sleep=lambda _: None)
    assert only_usdc.verify(payment, requirements)["invalidReason"] == "invalid_exact_stellar_payload_wrong_asset"
    assert only_usdc.settle(payment, requirements)["success"] is False
    assert rpc.sent == []


def test_facilitator_app_requires_the_api_key(facilitator, payment, requirements, rpc):
    app = TestClient(create_facilitator_app(facilitator, api_key="s3cret"))
    body = {"x402Version": 2, "paymentPayload": payment, "paymentRequirements": requirements}

    assert app.post("/settle", json=body).status_code == 401
    assert app.post("/settle", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert app.get("/supported").status_code == 401
    assert rpc.sent == []

    ok = app.post("/settle", json=body, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and ok.json()["success"] is True


# --- malformed input fails closed instead of crashing (finding 5) -------------


def test_facilitator_app_rejects_non_object_parts(facilitator):
    app = TestClient(create_facilitator_app(facilitator))
    for body in ({"paymentPayload": [], "paymentRequirements": {}}, {"paymentPayload": {}, "paymentRequirements": "x"}):
        assert app.post("/settle", json=body).status_code == 400
        assert app.post("/verify", json=body).status_code == 400


def test_facilitator_never_raises_on_wrong_types(facilitator):
    assert facilitator.verify("garbage", [])["invalidReason"] == "invalid_exact_stellar_payload_malformed"  # type: ignore[arg-type]
    settled = facilitator.settle("garbage", [])  # type: ignore[arg-type]
    assert settled["success"] is False
    assert facilitator.verify({"x402Version": 2, "accepted": []}, {})["invalidReason"] == "unsupported_scheme"


def test_middleware_answers_402_to_a_non_object_accepted(facilitator, payee):
    app = _starlette_app(facilitator, payee, calls=[])
    header = encode_header({"x402Version": 2, "accepted": ["not", "an", "object"], "payload": {}})
    response = TestClient(app).get("/weather", headers={"PAYMENT-SIGNATURE": header})
    assert response.status_code == 402
    assert response.json()["error"] == "Malformed payment header"


# --- middleware: HEAD is charged like GET (finding 4) -------------------------


def _starlette_app(facilitator, payee, calls: list) -> Starlette:
    async def weather(request):
        calls.append(request.method)
        return JSONResponse({"secret": "paid data"})

    route = PaidRoute(pay_to=payee.public_key, amount=AMOUNT, asset=XLM_SAC)
    return Starlette(
        routes=[Route("/weather", weather, methods=["GET"])],
        middleware=[Middleware(PaymentMiddleware, routes={"GET /weather": route}, facilitator=facilitator)],
    )


def test_head_request_does_not_run_a_paid_handler_for_free(facilitator, payee):
    calls: list = []
    response = TestClient(_starlette_app(facilitator, payee, calls)).head("/weather")
    assert response.status_code == 402
    assert calls == []


def test_signed_entry_authorizes_exactly_the_transfer(payment, requirements, payer):
    """The entry the client signed is bound to transfer(payer, payTo, amount) on the asset."""
    op = TransactionEnvelope.from_xdr(payment["payload"]["transaction"], PASSPHRASE).transaction.operations[0]
    fn = op.auth[0].root_invocation.function.contract_fn
    assert Address.from_xdr_sc_address(fn.contract_address).address == XLM_SAC
    assert fn.function_name.sc_symbol == b"transfer"
    assert [scval.from_address(a).address for a in fn.args[:2]] == [payer.public_key, requirements["payTo"]]
    assert scval.from_int128(fn.args[2]) == int(AMOUNT)
    assert op.auth[0].root_invocation.sub_invocations == []
