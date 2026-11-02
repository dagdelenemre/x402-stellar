"""Network identifiers, defaults and protocol constants for x402 on Stellar."""

from __future__ import annotations

from stellar_sdk import Network

X402_VERSION = 2
SCHEME_EXACT = "exact"

# CAIP-2 identifiers (https://namespaces.chainagnostic.org/stellar/caip2)
STELLAR_PUBNET = "stellar:pubnet"
STELLAR_TESTNET = "stellar:testnet"
STELLAR_WILDCARD = "stellar:*"

NETWORK_PASSPHRASES: dict[str, str] = {
    STELLAR_PUBNET: Network.PUBLIC_NETWORK_PASSPHRASE,
    STELLAR_TESTNET: Network.TESTNET_NETWORK_PASSPHRASE,
}

DEFAULT_TESTNET_RPC_URL = "https://soroban-testnet.stellar.org"

# Circle USDC (SEP-41 Stellar Asset Contract addresses)
USDC_PUBNET = "CCW67TSZV3SSS2HXMBQ5JFGCKJNXKZM7UQUWUZPUTHXSTZLEO7SJMI75"
USDC_TESTNET = "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA"
DEFAULT_USDC: dict[str, str] = {STELLAR_PUBNET: USDC_PUBNET, STELLAR_TESTNET: USDC_TESTNET}
DEFAULT_TOKEN_DECIMALS = 7
# Client spending cap per payment unless the caller sets one: 1.0 of a 7-decimal
# token, i.e. $1 in USDC, the same default as the official x402 SDK.
DEFAULT_MAX_AMOUNT_PER_PAYMENT = 10_000_000
ANY_ASSET = "any"

# Simulation needs a source account but never checks its sequence number, so the
# client builds against this all-zero account. The facilitator replaces it anyway.
NULL_ACCOUNT = "GAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWHF"

DEFAULT_TIMEOUT_SECONDS = 60
DEFAULT_ESTIMATED_LEDGER_SECONDS = 5
DEFAULT_MAX_TRANSACTION_FEE_STROOPS = 50_000
DEFAULT_INCLUSION_FEE_STROOPS = 100
SIGNATURE_EXPIRATION_LEDGER_TOLERANCE = 2

# HTTP transport (x402 v2)
PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"
