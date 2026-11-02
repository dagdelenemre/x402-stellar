"""Plug Stellar into the official x402 Python SDK (``pip install x402``).

The official SDK ships EVM, SVM and TVM mechanisms. This module adds Stellar the
same way they are built: scheme classes that satisfy ``SchemeNetworkClient``,
``SchemeNetworkServer`` and ``SchemeNetworkFacilitator``, plus ``register_*``
helpers. The protocol work is done by :class:`ExactStellarClient` and
:class:`ExactStellarFacilitator`; this layer only converts between the SDK's
pydantic models and the camelCase wire dicts those classes use::

    from x402 import x402Client
    from x402_stellar.sdk import register_exact_stellar_client

    client = register_exact_stellar_client(x402Client(), keypair)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, TypeVar

try:
    from x402.schemas import (
        AssetAmount,
        Network,
        PaymentPayload,
        PaymentRequirements,
        Price,
        SettleResponse,
        SupportedKind,
        VerifyResponse,
    )
    from x402.schemas.helpers import convert_to_token_amount, parse_money
except ImportError as e:  # pragma: no cover - exercised only without the extra
    raise ImportError(
        "x402_stellar.sdk needs the official x402 SDK. Install with: pip install x402-stellar[sdk]"
    ) from e

from stellar_sdk import Keypair, SorobanServer

from .client import ExactStellarClient, SorobanRpc
from .constants import (
    ANY_ASSET,
    DEFAULT_TESTNET_RPC_URL,
    DEFAULT_TOKEN_DECIMALS,
    DEFAULT_USDC,
    SCHEME_EXACT,
    STELLAR_TESTNET,
    STELLAR_WILDCARD,
)
from .facilitator import ExactStellarFacilitator, FacilitatorRpc

if TYPE_CHECKING:
    from x402 import (
        x402Client,
        x402ClientSync,
        x402Facilitator,
        x402FacilitatorSync,
        x402ResourceServer,
        x402ResourceServerSync,
    )

ClientT = TypeVar("ClientT", "x402Client", "x402ClientSync")
ServerT = TypeVar("ServerT", "x402ResourceServer", "x402ResourceServerSync")
FacilitatorT = TypeVar("FacilitatorT", "x402Facilitator", "x402FacilitatorSync")

MoneyParser = Callable[[str, str], "AssetAmount | None"]


def _wire(model: Any) -> dict[str, Any]:
    """SDK pydantic model -> the camelCase dict that travels over HTTP."""
    return model.model_dump(by_alias=True, mode="json")


def _default_asset(network: str, symbol: str | None = None) -> dict[str, Any]:
    """Same table as ``@x402/stellar``: USDC is the only default asset."""
    asset = DEFAULT_USDC.get(network)
    if asset is None:
        raise ValueError(f"No default asset configured for network {network}")
    if symbol and symbol.upper() != "USDC":
        raise ValueError(f"No {symbol} default asset configured for network {network}")
    return {"asset": asset, "decimals": DEFAULT_TOKEN_DECIMALS, "symbol": "USDC"}


def find_default_asset(asset: str, network: str) -> dict[str, Any] | None:
    """Default-asset lookup used by the SDK's client spend controls.

    Assets found here are paid without opt-in (capped in USD); any other token,
    native XLM included, must be listed in ``spend_controls.allowed_assets``.
    """
    if DEFAULT_USDC.get(str(network)) != asset:
        return None
    return _default_asset(str(network))


# ---------------------------------------------------------------------- client


class ExactStellarClientScheme:
    """Client side (``SchemeNetworkClient``): signs Stellar payments for ``x402Client``.

    Args:
        keypair: The payer's Stellar keypair.
        rpc: Soroban RPC to simulate against. Defaults to the public testnet RPC;
            ``stellar:pubnet`` needs one passed explicitly (there is no free default).
        max_amount: Optional cap per payment, in the token's smallest unit.
        upgraded_auth: Ask simulation for CAP-71 V2 credentials.
    """

    scheme = SCHEME_EXACT
    find_default_asset = staticmethod(find_default_asset)

    def __init__(
        self,
        keypair: Keypair,
        rpc: SorobanRpc | None = None,
        *,
        max_amount: int | None = None,
        ledger_seconds: float | None = None,
        upgraded_auth: bool = False,
    ) -> None:
        self._keypair = keypair
        self._rpc = rpc
        # The SDK's spend controls already allowlist assets and cap amounts in USD,
        # so the core client only applies the optional raw-unit cap given here.
        self._options = {
            "max_amount": max_amount,
            "allowed_assets": ANY_ASSET,
            "ledger_seconds": ledger_seconds,
            "upgraded_auth": upgraded_auth,
        }
        self._clients: dict[str, ExactStellarClient] = {}

    @property
    def address(self) -> str:
        return self._keypair.public_key

    def _client(self, network: str) -> ExactStellarClient:
        if network not in self._clients:
            rpc = self._rpc
            if rpc is None:
                if network != STELLAR_TESTNET:
                    raise ValueError(f"Pass an rpc for {network}; only stellar:testnet has a default")
                rpc = SorobanServer(DEFAULT_TESTNET_RPC_URL)
            self._clients[network] = ExactStellarClient(self._keypair, rpc, **self._options)
        return self._clients[network]

    def create_payment_payload(self, requirements: PaymentRequirements) -> dict[str, Any]:
        """Return the inner payload ``{"transaction": "<base64 XDR>"}``; x402Client wraps it."""
        return self._client(str(requirements.network)).create_payment_payload(_wire(requirements))


# ---------------------------------------------------------------------- server


class ExactStellarServerScheme:
    """Resource-server side (``SchemeNetworkServer``): prices routes for ``x402ResourceServer``.

    Mirrors ``ExactStellarScheme`` in ``@x402/stellar/exact/server``.
    """

    scheme = SCHEME_EXACT
    default_asset_transfer_method = "default"
    payment_flows = {
        "default": {"supported": ("authorization", "upfront"), "default": "authorization"},
    }

    def __init__(self) -> None:
        self._money_parsers: list[MoneyParser] = []

    def register_money_parser(self, parser: MoneyParser) -> ExactStellarServerScheme:
        """Add a custom Money -> AssetAmount parser; the USDC default is always the last fallback."""
        self._money_parsers.append(parser)
        return self

    def get_asset_decimals(self, asset: str, network: Network) -> int | None:
        found = find_default_asset(asset, str(network))
        return found["decimals"] if found is not None else None

    def parse_price(self, price: Price, network: Network) -> AssetAmount:
        if isinstance(price, dict) and "amount" in price:
            if not price.get("asset"):
                raise ValueError(f"Asset address must be specified for AssetAmount on network {network}")
            return AssetAmount(amount=price["amount"], asset=price["asset"], extra=price.get("extra") or {})
        if isinstance(price, AssetAmount):
            if not price.asset:
                raise ValueError(f"Asset address must be specified for AssetAmount on network {network}")
            return price

        parsed = parse_money(price)
        for parser in self._money_parsers:
            result = parser(parsed["amount"], str(network))
            if result is not None:
                return result

        asset = _default_asset(str(network), parsed.get("symbol"))
        return AssetAmount(
            amount=convert_to_token_amount(parsed["amount"], asset["decimals"]),
            asset=asset["asset"],
            extra={},
        )

    def enhance_payment_requirements(
        self,
        requirements: PaymentRequirements,
        supported_kind: SupportedKind,
        extension_keys: list[str],
    ) -> PaymentRequirements:
        """Copy ``areFeesSponsored`` from what the facilitator advertises."""
        _ = extension_keys
        sponsored = (supported_kind.extra or {}).get("areFeesSponsored")
        if isinstance(sponsored, bool):
            requirements.extra = {**(requirements.extra or {}), "areFeesSponsored": sponsored}
        return requirements


# ----------------------------------------------------------------- facilitator


class ExactStellarFacilitatorScheme:
    """Facilitator side (``SchemeNetworkFacilitator``): verifies and settles for ``x402Facilitator``."""

    scheme = SCHEME_EXACT
    caip_family = STELLAR_WILDCARD

    def __init__(self, facilitator: ExactStellarFacilitator) -> None:
        self._facilitator = facilitator

    def get_extra(self, network: Network) -> dict[str, Any] | None:
        _ = network
        return {"areFeesSponsored": True}

    def get_signers(self, network: Network) -> list[str]:
        _ = network
        return [self._facilitator.address]

    def verify(
        self, payload: PaymentPayload, requirements: PaymentRequirements, context: Any = None
    ) -> VerifyResponse:
        _ = context
        return VerifyResponse.model_validate(self._facilitator.verify(_wire(payload), _wire(requirements)))

    def settle(
        self, payload: PaymentPayload, requirements: PaymentRequirements, context: Any = None
    ) -> SettleResponse:
        _ = context
        return SettleResponse.model_validate(self._facilitator.settle(_wire(payload), _wire(requirements)))


# -------------------------------------------------------------------- register


def register_exact_stellar_client(
    client: ClientT,
    keypair: Keypair,
    networks: str | list[str] | None = None,
    *,
    rpc: SorobanRpc | None = None,
    max_amount: int | None = None,
    policies: list | None = None,
) -> ClientT:
    """Register Stellar ``exact`` payments on an ``x402Client`` (default: ``stellar:*``)."""
    scheme = ExactStellarClientScheme(keypair, rpc, max_amount=max_amount)
    if isinstance(networks, str):
        networks = [networks]
    for network in networks or [STELLAR_WILDCARD]:
        client.register(network, scheme)
    for policy in policies or []:
        client.register_policy(policy)
    return client


def register_exact_stellar_server(server: ServerT, networks: str | list[str] | None = None) -> ServerT:
    """Register Stellar ``exact`` pricing on an ``x402ResourceServer`` (default: ``stellar:*``)."""
    scheme = ExactStellarServerScheme()
    if isinstance(networks, str):
        networks = [networks]
    for network in networks or [STELLAR_WILDCARD]:
        server.register(network, scheme)
    return server


def register_exact_stellar_facilitator(
    facilitator: FacilitatorT,
    keypair: Keypair,
    rpc: FacilitatorRpc,
    networks: str | list[str] = STELLAR_TESTNET,
    **options: Any,
) -> FacilitatorT:
    """Register Stellar ``exact`` verify/settle on an ``x402Facilitator``.

    ``options`` go to :class:`ExactStellarFacilitator` (fee ceiling, inclusion fee, ...).
    """
    if isinstance(networks, str):
        networks = [networks]
    core = ExactStellarFacilitator(keypair, rpc, networks=tuple(networks), **options)
    facilitator.register(list(networks), ExactStellarFacilitatorScheme(core))
    return facilitator


__all__ = [
    "ExactStellarClientScheme",
    "ExactStellarFacilitatorScheme",
    "ExactStellarServerScheme",
    "find_default_asset",
    "register_exact_stellar_client",
    "register_exact_stellar_facilitator",
    "register_exact_stellar_server",
]
