"""An in-memory stand-in for Soroban RPC.

It behaves like the real thing where it matters for x402: recording-mode
simulation returns an unsigned auth entry for the payer, enforcing-mode
simulation checks the ed25519 signature on that entry, and a SAC-style
``transfer`` event is emitted. Tests can tamper with any of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable

from stellar_sdk import Account, Address, Keypair, SorobanDataBuilder, StrKey, scval, xdr
from stellar_sdk.auth import authorization_payload_hash, build_authorization_preimage
from stellar_sdk.soroban_rpc import SendTransactionStatus, SimulateTransactionResponse

LATEST_LEDGER = 1_000
RESOURCE_FEE = 5_000


def transfer_event(asset: str, sender: str, receiver: str, amount: int) -> str:
    contract_id = xdr.ContractID(xdr.Hash(StrKey.decode_contract(asset)))
    event = xdr.ContractEvent(
        ext=xdr.ExtensionPoint(0),
        contract_id=contract_id,
        type=xdr.ContractEventType.CONTRACT,
        body=xdr.ContractEventBody(
            0,
            v0=xdr.ContractEventV0(
                topics=[
                    scval.to_symbol("transfer"),
                    scval.to_address(sender),
                    scval.to_address(receiver),
                    scval.to_string("native"),
                ],
                data=scval.to_int128(amount),
            ),
        ),
    )
    return xdr.DiagnosticEvent(in_successful_contract_call=True, event=event).to_xdr()


def fn_call_event() -> str:
    event = xdr.ContractEvent(
        ext=xdr.ExtensionPoint(0),
        contract_id=None,
        type=xdr.ContractEventType.DIAGNOSTIC,
        body=xdr.ContractEventBody(0, v0=xdr.ContractEventV0(topics=[scval.to_symbol("fn_call")], data=scval.to_void())),
    )
    return xdr.DiagnosticEvent(in_successful_contract_call=True, event=event).to_xdr()


def _sim(data: dict[str, Any]) -> SimulateTransactionResponse:
    return SimulateTransactionResponse.model_validate(data)


@dataclass
class FakeRpc:
    passphrase: str
    latest_ledger: int = LATEST_LEDGER
    resource_fee: int = RESOURCE_FEE
    # Hooks to make the "network" misbehave in tests.
    events_override: Callable[[str, str, str, int], list[str]] | None = None
    simulation_error: str | None = None
    send_status: Any = SendTransactionStatus.PENDING
    final_status: str = "SUCCESS"
    sent: list[Any] = field(default_factory=list)

    def get_latest_ledger(self) -> Any:
        return SimpleNamespace(sequence=self.latest_ledger)

    def load_account(self, account_id: str) -> Account:
        return Account(account_id, 42)

    def simulate_transaction(self, envelope: Any, *args: Any, **kwargs: Any) -> SimulateTransactionResponse:
        if self.simulation_error:
            return _sim({"error": self.simulation_error, "latestLedger": self.latest_ledger})

        op = envelope.transaction.operations[0]
        invoke = op.host_function.invoke_contract
        asset = Address.from_xdr_sc_address(invoke.contract_address).address
        sender = scval.from_address(invoke.args[0]).address
        receiver = scval.from_address(invoke.args[1]).address
        amount = scval.from_int128(invoke.args[2])

        if op.auth:  # enforcing mode: every signature must be valid
            for entry in op.auth:
                creds = entry.credentials.address or entry.credentials.address_v2
                if creds is None:  # source-account credentials: nothing to check here
                    continue
                if creds.signature.type == xdr.SCValType.SCV_VOID:
                    return _sim({"error": "HostError: missing signature", "latestLedger": self.latest_ledger})
                preimage = build_authorization_preimage(entry, creds.signature_expiration_ledger.uint32, self.passphrase)
                payload = authorization_payload_hash(preimage)
                sig_map = {e.key.sym.sc_symbol: e.val for e in creds.signature.vec.sc_vec[0].map.sc_map}
                public_key = sig_map[b"public_key"].bytes.sc_bytes
                signature = sig_map[b"signature"].bytes.sc_bytes
                try:
                    Keypair.from_raw_ed25519_public_key(public_key).verify(payload, signature)
                except Exception:
                    return _sim({"error": "HostError: invalid signature", "latestLedger": self.latest_ledger})
                if Keypair.from_raw_ed25519_public_key(public_key).public_key != Address.from_xdr_sc_address(creds.address).address:
                    return _sim({"error": "HostError: wrong signer", "latestLedger": self.latest_ledger})
            auth: list[str] = []
        else:  # recording mode: tell the caller what needs signing
            entry = xdr.SorobanAuthorizationEntry(
                credentials=xdr.SorobanCredentials(
                    xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS,
                    address=xdr.SorobanAddressCredentials(
                        address=Address(sender).to_xdr_sc_address(),
                        nonce=xdr.Int64(7),
                        signature_expiration_ledger=xdr.Uint32(0),
                        signature=scval.to_void(),
                    ),
                ),
                root_invocation=xdr.SorobanAuthorizedInvocation(
                    function=xdr.SorobanAuthorizedFunction(
                        xdr.SorobanAuthorizedFunctionType.SOROBAN_AUTHORIZED_FUNCTION_TYPE_CONTRACT_FN,
                        contract_fn=invoke,
                    ),
                    sub_invocations=[],
                ),
            )
            auth = [entry.to_xdr()]

        events = (
            self.events_override(asset, sender, receiver, amount)
            if self.events_override
            else [fn_call_event(), transfer_event(asset, sender, receiver, amount)]
        )
        return _sim(
            {
                "transactionData": SorobanDataBuilder().set_resource_fee(self.resource_fee).build().to_xdr(),
                "minResourceFee": str(self.resource_fee),
                "events": events,
                "results": [{"auth": auth, "xdr": scval.to_void().to_xdr()}],
                "latestLedger": self.latest_ledger,
            }
        )

    def send_transaction(self, envelope: Any) -> Any:
        self.sent.append(envelope)
        return SimpleNamespace(status=self.send_status, hash=envelope.hash_hex())

    def get_transaction(self, tx_hash: str) -> Any:
        return SimpleNamespace(status=self.final_status)
