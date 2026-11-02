"""Exceptions raised on the client side.

The facilitator never raises for a bad payment: it returns
``{"isValid": False, "invalidReason": ...}`` with the reason codes used by the
TypeScript reference implementation, so both stacks report the same errors.
"""


class X402StellarError(Exception):
    """Base error for this package."""


class PaymentCreationError(X402StellarError):
    """The client refused to (or could not) build a payment."""


class SimulationFailedError(PaymentCreationError):
    """Soroban RPC simulation returned an error."""

    def __init__(self, cause: str) -> None:
        super().__init__(f"Stellar simulation failed: {cause}" if cause else "Stellar simulation failed")
        self.cause = cause


class PaymentRejectedError(X402StellarError):
    """The server still answered 402 after a payment was attached."""
