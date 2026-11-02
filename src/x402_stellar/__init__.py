"""x402 payments on Stellar for Python.

- :class:`ExactStellarClient` / :class:`X402Client`: pay for HTTP resources (agents, scripts).
- :class:`PaymentMiddleware` / :class:`PaidRoute`: charge for routes in FastAPI/Starlette.
- :class:`ExactStellarFacilitator`: verify and settle payments yourself.
- :class:`HTTPFacilitatorClient`: use a remote facilitator instead.
- :mod:`x402_stellar.sdk`: plug Stellar into the official x402 SDK (``pip install x402-stellar[sdk]``).
"""

from .client import ExactStellarClient
from .constants import STELLAR_PUBNET, STELLAR_TESTNET, USDC_PUBNET, USDC_TESTNET
from .errors import PaymentCreationError, PaymentRejectedError, SimulationFailedError, X402StellarError
from .facilitator import ExactStellarFacilitator
from .http import HTTPFacilitatorClient, X402Client, read_payment_response

__version__ = "0.1.0"


def __getattr__(name: str):  # server pieces need starlette; load them only when asked
    if name in ("PaidRoute", "PaymentMiddleware"):
        from . import server

        return getattr(server, name)
    raise AttributeError(name)

__all__ = [
    "ExactStellarClient",
    "ExactStellarFacilitator",
    "HTTPFacilitatorClient",
    "PaidRoute",
    "PaymentCreationError",
    "PaymentMiddleware",
    "PaymentRejectedError",
    "STELLAR_PUBNET",
    "STELLAR_TESTNET",
    "SimulationFailedError",
    "USDC_PUBNET",
    "USDC_TESTNET",
    "X402Client",
    "X402StellarError",
    "read_payment_response",
]
