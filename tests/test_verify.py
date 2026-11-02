"""Facilitator verification: the happy path and every safety rule from the spec."""

from __future__ import annotations

import copy
from typing import Any

from stellar_sdk import Address, Keypair, MuxedAccount, TransactionEnvelope, scval, xdr
from stellar_sdk.auth import authorize_entry

from x402_stellar import ExactStellarClient, ExactStellarFacilitator

from .conftest import PASSPHRASE, TEST_ASSETS, USDC, XLM_SAC
from .fake_rpc import transfer_event


def envelope(payment: dict[str, Any]) -> TransactionEnvelope:
    return TransactionEnvelope.from_xdr(payment["payload"]["transaction"], PASSPHRASE)


def with_envelope(payment: dict[str, Any], env: TransactionEnvelope) -> dict[str, Any]:
    tampered = copy.deepcopy(payment)
    tampered["payload"]["transaction"] = env.to_xdr()
    return tampered


def pay_for(client: ExactStellarClient, requirements: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """A payment built honestly for *different* requirements than the server's."""
    other = {**requirements, **changes}
    return {"x402Version": 2, "accepted": requirements, "payload": client.create_payment_payload(other)}


def reason(facilitator: ExactStellarFacilitator, payment: dict, requirements: dict) -> str | None:
    return facilitator.verify(payment, requirements).get("invalidReason")


def test_valid_payment_is_accepted(facilitator, payment, requirements, payer):
    assert facilitator.verify(payment, requirements) == {"isValid": True, "payer": payer.public_key}


def test_payload_is_signed_auth_entry_not_transaction(payment, payer):
    env = envelope(payment)
    assert env.signatures == []  # the client never signs the transaction
    (entry,) = env.transaction.operations[0].auth
    creds = entry.credentials.address
    assert Address.from_xdr_sc_address(creds.address).address == payer.public_key
    assert creds.signature.type != xdr.SCValType.SCV_VOID
    assert creds.signature_expiration_ledger.uint32 == 1_000 + 12  # 60s / 5s per ledger


# --- protocol -----------------------------------------------------------------


def test_rejects_wrong_version(facilitator, payment, requirements):
    assert reason(facilitator, {**payment, "x402Version": 1}, requirements) == "invalid_x402_version"


def test_rejects_network_mismatch(facilitator, payment, requirements):
    accepted = {**payment["accepted"], "network": "stellar:pubnet"}
    assert reason(facilitator, {**payment, "accepted": accepted}, requirements) == "network_mismatch"


def test_rejects_unsupported_scheme(facilitator, payment, requirements):
    assert reason(facilitator, payment, {**requirements, "scheme": "upto"}) == "unsupported_scheme"


def test_rejects_malformed_xdr(facilitator, payment, requirements):
    bad = copy.deepcopy(payment)
    bad["payload"]["transaction"] = "not-xdr"
    assert reason(facilitator, bad, requirements) == "invalid_exact_stellar_payload_malformed"


# --- transaction structure ----------------------------------------------------


def test_rejects_wrong_recipient(facilitator, client, requirements):
    payment = pay_for(client, requirements, payTo=Keypair.random().public_key)
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_wrong_recipient"


def test_rejects_wrong_amount(facilitator, client, requirements):
    payment = pay_for(client, requirements, amount="1")
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_wrong_amount"


def test_rejects_wrong_asset(facilitator, client, requirements):
    payment = pay_for(client, requirements, asset=USDC)
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_wrong_asset"


def test_rejects_extra_operation(facilitator, payment, requirements):
    env = envelope(payment)
    env.transaction.operations.append(env.transaction.operations[0])
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_wrong_operation"


# --- facilitator safety -------------------------------------------------------


def test_rejects_facilitator_as_transaction_source(facilitator, payment, requirements, facilitator_kp):
    env = envelope(payment)
    env.transaction.source = MuxedAccount(facilitator_kp.public_key)
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_unsafe_tx_or_op_source"


def test_rejects_facilitator_as_operation_source(facilitator, payment, requirements, facilitator_kp):
    env = envelope(payment)
    env.transaction.operations[0].source = MuxedAccount(facilitator_kp.public_key)
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_unsafe_tx_or_op_source"


def test_rejects_facilitator_as_payer(facilitator, facilitator_kp, rpc, requirements):
    sneaky = ExactStellarClient(facilitator_kp, rpc, allowed_assets=TEST_ASSETS)
    payment = {"x402Version": 2, "accepted": requirements, "payload": sneaky.create_payment_payload(requirements)}
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_facilitator_is_payer"


def test_rejects_facilitator_in_auth_entries(facilitator, facilitator_kp, payment, requirements):
    env = envelope(payment)
    op = env.transaction.operations[0]
    extra = copy.deepcopy(op.auth[0])
    extra.credentials.address.address = Address(facilitator_kp.public_key).to_xdr_sc_address()
    op.auth.append(authorize_entry(extra, facilitator_kp, 1_005, PASSPHRASE))
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_facilitator_in_auth"


def test_rejects_unexpected_balance_changes(facilitator, rpc, payment, requirements, facilitator_kp):
    def drain(asset: str, sender: str, receiver: str, amount: int) -> list[str]:
        return [
            transfer_event(asset, sender, receiver, amount),
            transfer_event(asset, facilitator_kp.public_key, sender, 10**9),
        ]

    rpc.events_override = drain
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_multiple_transfers"


def test_rejects_event_from_another_token(facilitator, rpc, payment, requirements):
    rpc.events_override = lambda asset, s, r, a: [transfer_event(USDC, s, r, a)]
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_event_wrong_asset"


def test_rejects_event_with_different_amount(facilitator, rpc, payment, requirements):
    rpc.events_override = lambda asset, s, r, a: [transfer_event(asset, s, r, a * 2)]
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_event_wrong_amount"


def test_rejects_missing_transfer_event(facilitator, rpc, payment, requirements):
    rpc.events_override = lambda *args: []
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_no_transfer_events"


# --- authorization entries ----------------------------------------------------


def test_rejects_signature_valid_too_long(facilitator, payer, rpc, requirements):
    slow = ExactStellarClient(payer, rpc, ledger_seconds=0.5, allowed_assets=TEST_ASSETS)  # 120 ledgers, not 12
    payment = {"x402Version": 2, "accepted": requirements, "payload": slow.create_payment_payload(requirements)}
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_signature_expiration_too_far"


def test_rejects_sub_invocations(facilitator, payer, payment, requirements):
    env = envelope(payment)
    entry = env.transaction.operations[0].auth[0]
    entry.root_invocation.sub_invocations.append(copy.deepcopy(entry.root_invocation))
    env.transaction.operations[0].auth = [authorize_entry(entry, payer, 1_005, PASSPHRASE)]
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_has_subinvocations"


def test_rejects_source_account_credentials(facilitator, payment, requirements):
    env = envelope(payment)
    entry = env.transaction.operations[0].auth[0]
    entry.credentials = xdr.SorobanCredentials(xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_SOURCE_ACCOUNT)
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_unsupported_credential_type"


def test_rejects_forged_signature(facilitator, payment, requirements):
    env = envelope(payment)
    entry = env.transaction.operations[0].auth[0]
    forged = authorize_entry(entry, Keypair.random(), 1_005, PASSPHRASE)  # signed by someone else
    env.transaction.operations[0].auth = [forged]
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_simulation_failed"


def test_accepts_cap71_v2_credentials(facilitator, payer, payment, requirements):
    env = envelope(payment)
    entry = env.transaction.operations[0].auth[0]
    creds = entry.credentials.address
    entry.credentials = xdr.SorobanCredentials(
        xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS_V2, address_v2=creds
    )
    # V2 signs an address-bound preimage, so the legacy signature no longer verifies...
    assert reason(facilitator, with_envelope(payment, env), requirements) == "invalid_exact_stellar_payload_simulation_failed"
    # ...but a V2 entry signed properly is accepted (spec: facilitators MUST accept both arms).
    env.transaction.operations[0].auth = [authorize_entry(entry, payer, 1_005, PASSPHRASE)]
    assert facilitator.verify(with_envelope(payment, env), requirements)["isValid"] is True


# --- simulation & fees --------------------------------------------------------


def test_rejects_failed_simulation(facilitator, rpc, payment, requirements):
    rpc.simulation_error = "HostError: balance too low"
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_simulation_failed"


def test_rejects_fee_above_ceiling(facilitator, rpc, payment, requirements):
    rpc.resource_fee = 60_000
    assert reason(facilitator, payment, requirements) == "invalid_exact_stellar_payload_fee_exceeds_maximum"


def test_never_raises_on_garbage(facilitator, requirements):
    assert reason(facilitator, {"x402Version": 2, "accepted": requirements, "payload": None}, requirements) == (
        "invalid_exact_stellar_payload_malformed"
    )
    assert reason(facilitator, {}, requirements) == "invalid_x402_version"


def test_transfer_args_are_exact(payment, requirements, payer):
    invoke = envelope(payment).transaction.operations[0].host_function.invoke_contract
    assert Address.from_xdr_sc_address(invoke.contract_address).address == XLM_SAC
    assert [scval.from_address(a).address for a in invoke.args[:2]] == [payer.public_key, requirements["payTo"]]
    assert scval.from_int128(invoke.args[2]) == int(requirements["amount"])
