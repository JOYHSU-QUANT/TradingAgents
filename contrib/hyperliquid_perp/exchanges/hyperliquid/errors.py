"""Domain errors for the Hyperliquid exchange layer.

The rest of the module catches these instead of leaking SDK / HTTP exceptions,
so callers never depend on the SDK's exception types. Raw HL responses are
translated here and in :mod:`mapper`.
"""

from __future__ import annotations


class ExchangeError(Exception):
    """Base class for every error this exchange layer raises."""


class ExchangeRequestError(ExchangeError):
    """A request to Hyperliquid failed (network, HTTP, or SDK-level error).

    Wraps the underlying exception so callers don't import SDK internals.
    """


class UnknownCoinError(ExchangeError):
    """The requested coin is not present in the exchange's perp universe."""


class MalformedResponseError(ExchangeError):
    """Hyperliquid returned a response missing fields the mapper requires."""
