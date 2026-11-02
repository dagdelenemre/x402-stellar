"""Facilitator side of the x402 `exact` scheme on Stellar.

Implements the verification rules and settlement flow from
``specs/schemes/exact/scheme_exact_stellar.md`` in the x402 repository, with the
same reason codes as the TypeScript reference (``@x402/stellar``).

The facilitator sponsors fees, so every check here exists to make sure it can
never be tricked into moving its own funds or paying for anything other than
the exact transfer the resource server asked for.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Collection, Protocol

from stellar_sdk import (
    Address,
    InvokeHostFunction,
    Keypair,
    TransactionBuilder,
    TransactionEnvelope,
    scval,
    xdr,
)
from stellar_sdk.soroban_rpc import SendTransactionStatus, SimulateTransactionResponse

from .client import address_credentials
from .constants import (
    DEFAULT_INCLUSION_FEE_STROOPS,
    DEFAULT_MAX_TRANSACTION_FEE_STROOPS,
    DEFAULT_TIMEOUT_SECONDS,
    SCHEME_EXACT,
    SIGNATURE_EXPIRATION_LEDGER_TOLERANCE,
    X402_VERSION,
)
from .utils import is_stellar_network, ledger_offset, network_passphrase

log = logging.getLogger(__name__)


class FacilitatorRpc(Protocol):
    """The subset of `stellar_sdk.SorobanServer` the facilitator uses."""

    def get_latest_ledger(self) -> Any: ...

    def simulate_transaction(self, transaction_envelope: Any, *args: Any, **kwargs: Any) -> SimulateTransactionResponse: ...

    def load_account(self, account_id: str) -> Any: ...

    def send_transaction(self, transaction_envelope: Any) -> Any: ...

    def get_transaction(self, transaction_hash: str) -> Any: ...


class _Invalid(Exception):
    def __init__(self, reason: str, payer: str | None = None, message: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.payer = payer
        self.message = message


def _invalid(reason: str, payer: str | None = None, message: str | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {"isValid": False, "invalidReason": reason}
    if payer:
        response["payer"] = payer
    if message:
        response["invalidMessage"] = message
    return response


def _account_id(muxed: Any) -> str | None:
    if muxed is None:
        return None
    return getattr(muxed, "account_id", None) or str(muxed)


class ExactStellarFacilitator:
    """Verifies and settles x402 `exact` payments on Stellar.

    Args:
        keypair: The facilitator's keypair. It is the transaction source and pays fees.
        rpc: A `stellar_sdk.SorobanServer` for the network being served.
        networks: CAIP-2 networks this instance serves (all must share the RPC).
        assets: Token contracts this facilitator sponsors fees for. ``None`` (default)
            accepts any SEP-41 token; set it when untrusted parties can reach the
            facilitator, so it never runs arbitrary contracts at its own expense.
        max_transaction_fee_stroops: Safety ceiling; verification fails above it.
        inclusion_fee_stroops: Inclusion bid on top of the simulated resource fee (>= 100).
        ledger_seconds: Estimated ledger close time used for signature expiry checks.
        poll_interval: Seconds between confirmation polls.
    """

    scheme = SCHEME_EXACT

    def __init__(
        self,
        keypair: Keypair,
        rpc: FacilitatorRpc,
        *,
        networks: tuple[str, ...] = ("stellar:testnet",),
        assets: Collection[str] | None = None,
        max_transaction_fee_stroops: int = DEFAULT_MAX_TRANSACTION_FEE_STROOPS,
        inclusion_fee_stroops: int = DEFAULT_INCLUSION_FEE_STROOPS,
        ledger_seconds: float | None = None,
        poll_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if inclusion_fee_stroops < 100:
            raise ValueError("inclusion_fee_stroops must be at least 100 (spec requirement)")
        for network in networks:
            network_passphrase(network)
        self.keypair = keypair
        self.rpc = rpc
        self.networks = tuple(networks)
        self.assets = frozenset((assets,) if isinstance(assets, str) else assets) if assets is not None else None
        self.max_transaction_fee_stroops = max_transaction_fee_stroops
        self.inclusion_fee_stroops = inclusion_fee_stroops
        self.ledger_seconds = ledger_seconds
        self.poll_interval = poll_interval
        self._sleep = sleep

    @property
    def address(self) -> str:
        return self.keypair.public_key

    def supported(self) -> dict[str, Any]:
        """Body for ``GET /supported``."""
        return {
            "kinds": [
                {
                    "x402Version": X402_VERSION,
                    "scheme": SCHEME_EXACT,
                    "network": network,
                    "extra": {"areFeesSponsored": True},
                }
                for network in self.networks
            ],
            "extensions": [],
            "signers": {"stellar:*": [self.address]},
        }

    # ------------------------------------------------------------------ verify

    def verify(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        """Return an x402 ``VerifyResponse`` dict. Never raises for bad input."""
        return self._verify(payload, requirements)[0]

    def _verify(
        self, payload: dict[str, Any], requirements: dict[str, Any]
    ) -> tuple[dict[str, Any], SimulateTransactionResponse | None]:
        payer: str | None = None
        try:
            payer, simulation = self._check(payload, requirements)
            return {"isValid": True, "payer": payer}, simulation
        except _Invalid as err:
            return _invalid(err.reason, err.payer or payer, err.message), None
        except Exception:  # noqa: BLE001 - a facilitator must answer, not crash
            log.exception("Unexpected verification error")
            return _invalid("unexpected_verify_error", payer), None

    def _check(
        self, payload: dict[str, Any], requirements: dict[str, Any]
    ) -> tuple[str, SimulateTransactionResponse]:
        # 1. Protocol
        if not isinstance(payload, dict) or not isinstance(requirements, dict):
            raise _Invalid("invalid_exact_stellar_payload_malformed")
        if payload.get("x402Version") != X402_VERSION:
            raise _Invalid("invalid_x402_version")
        accepted = payload.get("accepted")
        accepted = accepted if isinstance(accepted, dict) else {}
        if accepted.get("scheme") != SCHEME_EXACT or requirements.get("scheme") != SCHEME_EXACT:
            raise _Invalid("unsupported_scheme")
        network = requirements.get("network")
        if accepted.get("network") != network:
            raise _Invalid("network_mismatch")
        if not is_stellar_network(str(network)) or network not in self.networks:
            raise _Invalid("invalid_network")
        if self.assets is not None and requirements.get("asset") not in self.assets:
            raise _Invalid("invalid_exact_stellar_payload_wrong_asset")
        passphrase = network_passphrase(network)

        inner = payload.get("payload") or {}
        tx_xdr = inner.get("transaction") if isinstance(inner, dict) else None
        if not isinstance(tx_xdr, str):
            raise _Invalid("invalid_exact_stellar_payload_malformed")
        try:
            envelope = TransactionEnvelope.from_xdr(tx_xdr, passphrase)
        except Exception:  # noqa: BLE001
            raise _Invalid("invalid_exact_stellar_payload_malformed") from None
        transaction = envelope.transaction

        # 2. Structure: exactly one invokeHostFunction calling transfer on the asset
        if len(transaction.operations) != 1:
            raise _Invalid("invalid_exact_stellar_payload_wrong_operation")
        operation = transaction.operations[0]
        if not isinstance(operation, InvokeHostFunction):
            raise _Invalid("invalid_exact_stellar_payload_wrong_operation")

        # 4a. Facilitator safety: never accept our own account as a source
        if self.address in {_account_id(transaction.source), _account_id(operation.source)}:
            raise _Invalid("invalid_exact_stellar_payload_unsafe_tx_or_op_source")

        function = operation.host_function
        if function.type != xdr.HostFunctionType.HOST_FUNCTION_TYPE_INVOKE_CONTRACT:
            raise _Invalid("invalid_exact_stellar_payload_wrong_operation")
        invoke = function.invoke_contract
        contract = Address.from_xdr_sc_address(invoke.contract_address).address
        if contract != requirements.get("asset"):
            raise _Invalid("invalid_exact_stellar_payload_wrong_asset")
        if invoke.function_name.sc_symbol != b"transfer" or len(invoke.args) != 3:
            raise _Invalid("invalid_exact_stellar_payload_wrong_function_name")

        try:
            payer = scval.from_address(invoke.args[0]).address
            recipient = scval.from_address(invoke.args[1]).address
            amount = scval.from_int128(invoke.args[2])
        except Exception:  # noqa: BLE001
            raise _Invalid("invalid_exact_stellar_payload_malformed") from None

        if payer == self.address:
            raise _Invalid("invalid_exact_stellar_payload_facilitator_is_payer")
        if recipient != requirements.get("payTo"):
            raise _Invalid("invalid_exact_stellar_payload_wrong_recipient", payer)
        expected_amount = int(requirements.get("amount", -1))
        if amount != expected_amount:
            raise _Invalid("invalid_exact_stellar_payload_wrong_amount", payer)

        # 5. Re-simulate against current ledger state
        simulation = self.rpc.simulate_transaction(envelope)
        if simulation.error or not simulation.transaction_data:
            raise _Invalid("invalid_exact_stellar_payload_simulation_failed", payer, simulation.error)

        fee = int(simulation.min_resource_fee or 0) + self.inclusion_fee_stroops
        if fee > self.max_transaction_fee_stroops:
            raise _Invalid(
                "invalid_exact_stellar_payload_fee_exceeds_maximum",
                payer,
                f"simulation-derived fee {fee} stroops exceeds ceiling "
                f"{self.max_transaction_fee_stroops} stroops",
            )

        # 4b. Only the expected balance change may happen
        self._check_events(simulation.events or [], payer, recipient, expected_amount, contract)

        # 3. Authorization entries
        timeout = int(requirements.get("maxTimeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
        current = self.rpc.get_latest_ledger().sequence
        max_ledger = current + ledger_offset(timeout, self.ledger_seconds)
        self._check_auth(operation, payer, max_ledger)

        return payer, simulation

    def _check_events(
        self, events: list[str], payer: str, recipient: str, amount: int, asset: str
    ) -> None:
        transfers: list[tuple[str, str, int]] = []
        for raw in events:
            try:
                event = xdr.DiagnosticEvent.from_xdr(raw).event
            except Exception:  # noqa: BLE001
                raise _Invalid("unexpected_verify_error", payer) from None
            if event.type != xdr.ContractEventType.CONTRACT:
                continue  # diagnostic fn_call / fn_return entries
            topics = event.body.v0.topics
            if len(topics) < 3 or topics[0].type != xdr.SCValType.SCV_SYMBOL:
                raise _Invalid("invalid_exact_stellar_payload_event_not_transfer", payer)
            if topics[0].sym.sc_symbol != b"transfer":
                raise _Invalid("invalid_exact_stellar_payload_event_not_transfer", payer)
            if event.contract_id is None:
                raise _Invalid("invalid_exact_stellar_payload_event_missing_contract_id", payer)
            emitter = Address.from_xdr_sc_address(
                xdr.SCAddress(xdr.SCAddressType.SC_ADDRESS_TYPE_CONTRACT, contract_id=event.contract_id)
            ).address
            if emitter != asset:
                raise _Invalid("invalid_exact_stellar_payload_event_wrong_asset", payer)
            try:
                data = event.body.v0.data
                if data.type == xdr.SCValType.SCV_MAP:  # CAP-67 muxed form: {amount, to_muxed_id}
                    data = next(e.val for e in data.map.sc_map if e.key.sym.sc_symbol == b"amount")
                transfers.append(
                    (
                        scval.from_address(topics[1]).address,
                        scval.from_address(topics[2]).address,
                        scval.from_int128(data),
                    )
                )
            except Exception:  # noqa: BLE001
                raise _Invalid("unexpected_verify_error", payer) from None

        if not transfers:
            raise _Invalid("invalid_exact_stellar_payload_no_transfer_events", payer)
        if len(transfers) > 1:
            raise _Invalid("invalid_exact_stellar_payload_multiple_transfers", payer)
        sender, receiver, value = transfers[0]
        if sender != payer:
            raise _Invalid("invalid_exact_stellar_payload_event_wrong_from", payer)
        if receiver != recipient:
            raise _Invalid("invalid_exact_stellar_payload_event_wrong_to", payer)
        if value != amount:
            raise _Invalid("invalid_exact_stellar_payload_event_wrong_amount", payer)

    def _check_auth(self, operation: InvokeHostFunction, payer: str, max_ledger: int) -> None:
        if not operation.auth:
            raise _Invalid("invalid_exact_stellar_payload_no_auth_entries", payer)
        signed: set[str] = set()
        pending: set[str] = set()
        for entry in operation.auth:
            creds = address_credentials(entry.credentials)
            if creds is None:
                raise _Invalid("invalid_exact_stellar_payload_unsupported_credential_type", payer)
            signer = Address.from_xdr_sc_address(creds.address).address
            if signer == self.address:
                raise _Invalid("invalid_exact_stellar_payload_facilitator_in_auth", payer)
            if creds.signature_expiration_ledger.uint32 > max_ledger + SIGNATURE_EXPIRATION_LEDGER_TOLERANCE:
                raise _Invalid("invalid_exact_stellar_signature_expiration_too_far", payer)
            if entry.root_invocation.sub_invocations:
                raise _Invalid("invalid_exact_stellar_payload_has_subinvocations", payer)
            if creds.signature.type == xdr.SCValType.SCV_VOID:
                pending.add(signer)
            else:
                signed.add(signer)
        if payer not in signed:
            raise _Invalid("invalid_exact_stellar_payload_missing_payer_signature", payer)
        if pending:
            raise _Invalid("invalid_exact_stellar_payload_unexpected_pending_signatures", payer)

    # ------------------------------------------------------------------ settle

    def settle(self, payload: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
        """Verify again (the spec forbids trusting an earlier verify), then submit."""
        accepted = payload.get("accepted") if isinstance(payload, dict) else None
        network = (accepted.get("network") if isinstance(accepted, dict) else None) or (
            requirements.get("network") if isinstance(requirements, dict) else None
        )
        result, simulation = self._verify(payload, requirements)
        payer = result.get("payer")

        def failure(reason: str, tx_hash: str = "", message: str | None = None, **extra: Any) -> dict[str, Any]:
            response: dict[str, Any] = {
                "success": False,
                "errorReason": reason,
                "transaction": tx_hash,
                "network": network,
            }
            if payer:
                response["payer"] = payer
            if message:
                response["errorMessage"] = message
            if extra:
                response["extra"] = extra
            return response

        if not result["isValid"] or simulation is None:
            return failure(result.get("invalidReason", "verification_failed"), message=result.get("invalidMessage"))

        tx_hash = ""
        try:
            passphrase = network_passphrase(requirements["network"])
            client_tx = TransactionEnvelope.from_xdr(payload["payload"]["transaction"], passphrase).transaction
            client_op: InvokeHostFunction = client_tx.operations[0]

            # Rebuild with the facilitator as source; fee and resources come from
            # the fresh simulation, never from the client.
            account = self.rpc.load_account(self.address)
            timeout = int(requirements.get("maxTimeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)
            rebuilt = (
                TransactionBuilder(account, passphrase, base_fee=self.inclusion_fee_stroops)
                .append_operation(InvokeHostFunction(host_function=client_op.host_function, auth=client_op.auth))
                .set_soroban_data(simulation.transaction_data)
                .set_timeout(timeout)
                .build()
            )
            rebuilt.sign(self.keypair)

            sent = self.rpc.send_transaction(rebuilt)
            if sent.status != SendTransactionStatus.PENDING:
                retryable = sent.status == SendTransactionStatus.TRY_AGAIN_LATER
                return failure(
                    "settle_exact_stellar_transaction_submission_retryable"
                    if retryable
                    else "settle_exact_stellar_transaction_submission_failed",
                    message=f"sendTransaction returned {sent.status}",
                    rpcStatus=str(sent.status),
                    retryable=retryable,
                )

            tx_hash = sent.hash
            if not self._wait_for(tx_hash, attempts=timeout):
                return failure("settle_exact_stellar_transaction_failed", tx_hash)

            return {"success": True, "transaction": tx_hash, "network": network, "payer": payer}
        except Exception:  # noqa: BLE001
            log.exception("Unexpected settlement error")
            return failure("unexpected_settle_error", tx_hash)

    def _wait_for(self, tx_hash: str, attempts: int) -> bool:
        for _ in range(max(1, attempts)):
            try:
                status = str(self.rpc.get_transaction(tx_hash).status).upper()
            except Exception:  # noqa: BLE001 - transient RPC failure, keep polling
                status = "NOT_FOUND"
            if status.endswith("SUCCESS"):
                return True
            if status.endswith("FAILED"):
                return False
            self._sleep(self.poll_interval)
        return False
