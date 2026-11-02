"""Facilitator settlement: rebuild, sign, submit, confirm."""

from __future__ import annotations

from stellar_sdk.soroban_rpc import SendTransactionStatus

from .conftest import PASSPHRASE


def test_settles_with_facilitator_as_source(facilitator, rpc, payment, requirements, facilitator_kp, payer):
    result = facilitator.settle(payment, requirements)

    assert result["success"] is True
    assert result["payer"] == payer.public_key
    assert result["network"] == "stellar:testnet"
    (sent,) = rpc.sent
    assert result["transaction"] == sent.hash_hex()
    tx = sent.transaction
    assert tx.source.account_id == facilitator_kp.public_key
    assert tx.fee == 100 + rpc.resource_fee  # inclusion buffer + simulated resource fee
    assert tx.soroban_data.resource_fee.int64 == rpc.resource_fee
    assert len(sent.signatures) == 1
    facilitator_kp.verify(sent.hash(), sent.signatures[0].signature)
    # the client's operation and signed auth entry are carried over untouched
    from stellar_sdk import TransactionEnvelope

    client_op = TransactionEnvelope.from_xdr(payment["payload"]["transaction"], PASSPHRASE).transaction.operations[0]
    assert tx.operations[0].host_function == client_op.host_function
    assert tx.operations[0].auth == client_op.auth


def test_settle_reverifies_and_refuses_bad_payment(facilitator, rpc, payment, requirements):
    result = facilitator.settle(payment, {**requirements, "amount": "5"})
    assert result["success"] is False
    assert result["errorReason"] == "invalid_exact_stellar_payload_wrong_amount"
    assert rpc.sent == []


def test_submission_error_is_terminal(facilitator, rpc, payment, requirements):
    rpc.send_status = SendTransactionStatus.ERROR
    result = facilitator.settle(payment, requirements)
    assert result["errorReason"] == "settle_exact_stellar_transaction_submission_failed"
    assert result["extra"]["retryable"] is False


def test_try_again_later_is_retryable(facilitator, rpc, payment, requirements):
    rpc.send_status = SendTransactionStatus.TRY_AGAIN_LATER
    result = facilitator.settle(payment, requirements)
    assert result["errorReason"] == "settle_exact_stellar_transaction_submission_retryable"
    assert result["extra"]["retryable"] is True


def test_failed_transaction_reports_hash(facilitator, rpc, payment, requirements):
    rpc.final_status = "FAILED"
    result = facilitator.settle(payment, requirements)
    assert result["success"] is False
    assert result["errorReason"] == "settle_exact_stellar_transaction_failed"
    assert result["transaction"] == rpc.sent[0].hash_hex()


def test_supported_advertises_fee_sponsorship(facilitator, facilitator_kp):
    (kind,) = facilitator.supported()["kinds"]
    assert kind == {"x402Version": 2, "scheme": "exact", "network": "stellar:testnet", "extra": {"areFeesSponsored": True}}
    assert facilitator.supported()["signers"]["stellar:*"] == [facilitator_kp.public_key]
