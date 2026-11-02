"""Client side of the x402 `exact` scheme on Stellar.

The client builds a Soroban `transfer(from, to, amount)` invocation on the
token contract, signs only the authorization entry (never the transaction),
and hands the XDR to the resource server. The facilitator pays the fees.
"""

from __future__ import annotations

from typing import Any, Collection, Literal, Protocol

from stellar_sdk import Account, Address, Keypair, TransactionBuilder, scval, xdr
from stellar_sdk.auth import authorize_entry
from stellar_sdk.soroban_rpc import SimulateTransactionResponse

from .constants import (
    ANY_ASSET,
    DEFAULT_MAX_AMOUNT_PER_PAYMENT,
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_USDC,
    NULL_ACCOUNT,
    SCHEME_EXACT,
)
from .errors import PaymentCreationError, SimulationFailedError
from .utils import (
    is_stellar_network,
    is_valid_asset,
    is_valid_destination,
    ledger_offset,
    network_passphrase,
)


class SorobanRpc(Protocol):
    """The subset of `stellar_sdk.SorobanServer` this package relies on."""

    def get_latest_ledger(self) -> Any: ...

    def simulate_transaction(self, transaction_envelope: Any, *args: Any, **kwargs: Any) -> SimulateTransactionResponse: ...


def address_credentials(credentials: xdr.SorobanCredentials) -> xdr.SorobanAddressCredentials | None:
    """Return address credentials for the legacy and CAP-71 V2 arms, else None."""
    kind = credentials.type
    if kind == xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS:
        return credentials.address
    if kind == xdr.SorobanCredentialsType.SOROBAN_CREDENTIALS_ADDRESS_V2:
        return credentials.address_v2
    return None


class ExactStellarClient:
    """Creates x402 `exact` payment payloads on Stellar.

    Args:
        keypair: The payer's Stellar keypair (G-account).
        rpc: A `stellar_sdk.SorobanServer` (or anything with the same methods).
        max_amount: Spending cap per payment, in the token's smallest unit. Payments
            above it are refused before anything is signed. Defaults to 10,000,000
            ($1 in USDC); ``None`` removes the cap.
        allowed_assets: Token contracts this client will pay with. Defaults to the
            network's USDC only, so a server cannot ask for some other token the
            payer happens to hold. ``"any"`` disables the check; use it only when
            another layer restricts assets (as the official x402 SDK's spend controls do).
        ledger_seconds: Estimated ledger close time used to bound signature expiry.
    """

    scheme = SCHEME_EXACT

    def __init__(
        self,
        keypair: Keypair,
        rpc: SorobanRpc,
        *,
        max_amount: int | None = DEFAULT_MAX_AMOUNT_PER_PAYMENT,
        allowed_assets: Collection[str] | Literal["any"] | None = None,
        ledger_seconds: float | None = None,
        upgraded_auth: bool = False,
    ) -> None:
        self.keypair = keypair
        self.rpc = rpc
        self.max_amount = max_amount
        if isinstance(allowed_assets, str) and allowed_assets != ANY_ASSET:
            allowed_assets = (allowed_assets,)  # a bare address, not a substring set
        self.allowed_assets = allowed_assets
        self.ledger_seconds = ledger_seconds
        self.upgraded_auth = upgraded_auth

    @property
    def address(self) -> str:
        return self.keypair.public_key

    def supports(self, requirements: dict[str, Any]) -> bool:
        return requirements.get("scheme") == SCHEME_EXACT and is_stellar_network(
            str(requirements.get("network", ""))
        )

    def create_payment_payload(self, requirements: dict[str, Any]) -> dict[str, Any]:
        """Return the scheme payload: ``{"transaction": "<base64 XDR>"}``."""
        self._validate(requirements)

        network = requirements["network"]
        passphrase = network_passphrase(network)
        asset = requirements["asset"]
        pay_to = requirements["payTo"]
        amount = int(requirements["amount"])
        timeout = int(requirements.get("maxTimeoutSeconds") or DEFAULT_TIMEOUT_SECONDS)

        latest = self.rpc.get_latest_ledger().sequence
        max_ledger = latest + ledger_offset(timeout, self.ledger_seconds)

        envelope = (
            TransactionBuilder(Account(NULL_ACCOUNT, 0), passphrase, base_fee=100)
            .append_invoke_contract_function_op(
                contract_id=asset,
                function_name="transfer",
                parameters=[
                    scval.to_address(self.address),  # from
                    scval.to_address(pay_to),  # to
                    scval.to_int128(amount),  # amount
                ],
            )
            .set_timeout(timeout)
            .build()
        )

        # 1) Recording-mode simulation tells us which auth entries are needed.
        recording = self._simulate(envelope)
        results = recording.results or []
        if len(results) != 1:
            raise PaymentCreationError("Simulation did not return a single invocation result")

        operation = envelope.transaction.operations[0]
        payment_call = operation.host_function.invoke_contract.to_xdr()
        signed: list[xdr.SorobanAuthorizationEntry] = []
        for raw in results[0].auth or []:
            entry = xdr.SorobanAuthorizationEntry.from_xdr(raw)
            creds = address_credentials(entry.credentials)
            if creds is None:
                raise PaymentCreationError("Unexpected source-account authorization entry")
            signer = Address.from_xdr_sc_address(creds.address).address
            if signer != self.address:
                raise PaymentCreationError(
                    f"Payment needs a signature from {signer}, expected only {self.address}"
                )
            # The entry comes from the RPC. Sign it only if it authorizes exactly the
            # transfer built above: a signed entry is a bearer authorization, and an
            # untrusted RPC could otherwise slip in any call the payer can authorize.
            root = entry.root_invocation
            if (
                root.function.type != xdr.SorobanAuthorizedFunctionType.SOROBAN_AUTHORIZED_FUNCTION_TYPE_CONTRACT_FN
                or root.function.contract_fn.to_xdr() != payment_call
                or root.sub_invocations
            ):
                raise PaymentCreationError("Simulation asked to authorize something other than this payment")
            signed.append(authorize_entry(entry, self.keypair, max_ledger, passphrase))
        if not signed:
            raise PaymentCreationError("Simulation returned no authorization entry for the payer")

        operation.auth = signed

        # 2) Enforcing-mode simulation with the signed entry: validates the
        #    signature and yields resources that include signature verification.
        enforcing = self._simulate(envelope)
        envelope.transaction.soroban_data = xdr.SorobanTransactionData.from_xdr(enforcing.transaction_data)
        envelope.transaction.fee = 100 + int(enforcing.min_resource_fee or 0)

        return {"transaction": envelope.to_xdr()}

    # ------------------------------------------------------------------ helpers

    def _simulate(self, envelope: Any) -> SimulateTransactionResponse:
        response = self.rpc.simulate_transaction(envelope, use_upgraded_auth=self.upgraded_auth)
        if response.error:
            raise SimulationFailedError(response.error)
        if response.restore_preamble:
            raise SimulationFailedError("Ledger entries are archived and need a restore first")
        return response

    def _validate(self, requirements: dict[str, Any]) -> None:
        if requirements.get("scheme") != SCHEME_EXACT:
            raise PaymentCreationError(f"Unsupported scheme: {requirements.get('scheme')}")
        if not is_stellar_network(str(requirements.get("network"))):
            raise PaymentCreationError(f"Unsupported network: {requirements.get('network')}")
        if not is_valid_destination(str(requirements.get("payTo", ""))):
            raise PaymentCreationError(f"Invalid payTo address: {requirements.get('payTo')}")
        asset = str(requirements.get("asset", ""))
        if not is_valid_asset(asset):
            raise PaymentCreationError(f"Invalid asset contract: {asset}")
        if self.allowed_assets != ANY_ASSET:
            allowed = self.allowed_assets if self.allowed_assets is not None else {DEFAULT_USDC.get(requirements["network"])}
            if asset not in allowed:
                raise PaymentCreationError(f"Asset {asset} is not in this client's allowed_assets")
        amount = str(requirements.get("amount", ""))
        if not amount.isdigit() or int(amount) <= 0:
            raise PaymentCreationError(f"Invalid amount: {amount!r}")
        if self.max_amount is not None and int(amount) > self.max_amount:
            raise PaymentCreationError(
                f"Requested amount {amount} exceeds this client's limit of {self.max_amount}"
            )
        if not (requirements.get("extra") or {}).get("areFeesSponsored"):
            raise PaymentCreationError("The exact scheme on Stellar requires areFeesSponsored=true")
