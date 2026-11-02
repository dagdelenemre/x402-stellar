"""Small helpers: validation, amounts, header encoding."""

from __future__ import annotations

import base64
import json
import math
import re
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

from .constants import (
    DEFAULT_ESTIMATED_LEDGER_SECONDS,
    DEFAULT_TOKEN_DECIMALS,
    DEFAULT_USDC,
    NETWORK_PASSPHRASES,
)

_DESTINATION_RE = re.compile(r"^(?:[GC][ABCD][A-Z2-7]{54}|M[ABCD][A-Z2-7]{67})$")
_ASSET_RE = re.compile(r"^C[ABCD][A-Z2-7]{54}$")


def is_stellar_network(network: str) -> bool:
    return network in NETWORK_PASSPHRASES


def network_passphrase(network: str) -> str:
    try:
        return NETWORK_PASSPHRASES[network]
    except KeyError:
        raise ValueError(f"Unknown Stellar network: {network}") from None


def is_valid_destination(address: str) -> bool:
    """G-account, C-account or M-account (muxed)."""
    return bool(_DESTINATION_RE.match(address))


def is_valid_asset(address: str) -> bool:
    """SEP-41 token contract address (C-account)."""
    return bool(_ASSET_RE.match(address))


def to_token_amount(decimal_amount: str | int | float, decimals: int = DEFAULT_TOKEN_DECIMALS) -> str:
    """Convert a human amount ("0.01") into the token's smallest unit ("100000")."""
    try:
        value = Decimal(str(decimal_amount))
    except InvalidOperation:
        raise ValueError(f"Invalid amount: {decimal_amount!r}") from None
    if value <= 0:
        raise ValueError(f"Amount must be positive: {decimal_amount!r}")
    units = (value * (Decimal(10) ** decimals)).quantize(Decimal(1), rounding=ROUND_DOWN)
    if units <= 0:
        raise ValueError(f"Amount {decimal_amount!r} is below the token's smallest unit")
    return str(int(units))


def parse_usd_price(price: str | int | float, network: str) -> dict[str, Any]:
    """Turn "$0.01" (or 0.01) into an asset amount in USDC on the given network."""
    raw = str(price).strip().lstrip("$").strip()
    asset = DEFAULT_USDC.get(network)
    if asset is None:
        raise ValueError(f"No default USDC asset for network {network}")
    return {"amount": to_token_amount(raw), "asset": asset}


def ledger_offset(max_timeout_seconds: int, ledger_seconds: float | None = None) -> int:
    seconds = ledger_seconds or DEFAULT_ESTIMATED_LEDGER_SECONDS
    return math.ceil(max_timeout_seconds / seconds)


def encode_header(data: dict[str, Any]) -> str:
    """Base64(JSON) as used by the x402 v2 HTTP headers."""
    return base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii")


def decode_header(value: str) -> dict[str, Any]:
    decoded = json.loads(base64.b64decode(value.encode("ascii")).decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("x402 header must decode to a JSON object")
    return decoded
