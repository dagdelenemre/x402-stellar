from __future__ import annotations

import copy
from typing import Any

import pytest
from stellar_sdk import Asset, Keypair, Network

from x402_stellar import ExactStellarClient, ExactStellarFacilitator

from .fake_rpc import FakeRpc

PASSPHRASE = Network.TESTNET_NETWORK_PASSPHRASE
XLM_SAC = Asset.native().contract_id(PASSPHRASE)
USDC = "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"
TEST_ASSETS = (XLM_SAC, USDC)  # the default client only pays USDC; tests mostly use XLM
AMOUNT = "1000000"  # 0.1 XLM in stroops


@pytest.fixture
def payer() -> Keypair:
    return Keypair.random()


@pytest.fixture
def payee() -> Keypair:
    return Keypair.random()


@pytest.fixture
def facilitator_kp() -> Keypair:
    return Keypair.random()


@pytest.fixture
def rpc() -> FakeRpc:
    return FakeRpc(passphrase=PASSPHRASE)


@pytest.fixture
def requirements(payee: Keypair) -> dict[str, Any]:
    return {
        "scheme": "exact",
        "network": "stellar:testnet",
        "amount": AMOUNT,
        "asset": XLM_SAC,
        "payTo": payee.public_key,
        "maxTimeoutSeconds": 60,
        "extra": {"areFeesSponsored": True},
    }


@pytest.fixture
def client(payer: Keypair, rpc: FakeRpc) -> ExactStellarClient:
    return ExactStellarClient(payer, rpc, allowed_assets=TEST_ASSETS)


@pytest.fixture
def facilitator(facilitator_kp: Keypair, rpc: FakeRpc) -> ExactStellarFacilitator:
    return ExactStellarFacilitator(facilitator_kp, rpc, sleep=lambda _: None)


@pytest.fixture
def payment(client: ExactStellarClient, requirements: dict[str, Any]) -> dict[str, Any]:
    return {
        "x402Version": 2,
        "resource": {"url": "http://test/weather", "description": "", "mimeType": "application/json"},
        "accepted": copy.deepcopy(requirements),
        "payload": client.create_payment_payload(requirements),
    }
