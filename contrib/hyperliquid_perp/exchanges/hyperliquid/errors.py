"""Domain errors for the Hyperliquid exchange layer.

The rest of the module catches these instead of leaking SDK / HTTP exceptions,
so callers never depend on the SDK's exception types. Raw HL responses are
translated here and in :mod:`mapper`.
"""

from __future__ import annotations

from typing import Any


class ExchangeError(Exception):
    """Base class for every error this exchange layer raises."""


class ExchangeRequestError(ExchangeError):
    """A request to Hyperliquid failed (network, HTTP, or SDK-level error).

    Wraps the underlying exception so callers don't import SDK internals.
    """


class ExchangeThrottledError(ExchangeRequestError):
    """The exchange refused to SERVE the request: rate limited or overloaded.

    A subclass of :class:`ExchangeRequestError` so every existing
    ``except ExchangeRequestError`` keeps working unchanged. The distinction is
    for the callers that must tell "temporarily could not send" from "sent, and
    the exchange said no".

    §17.2's stop-loss repair ladder is why this exists. It counts attempts and,
    on exhaustion, market-closes a healthy position and latches the run for a
    human. Three fixed-delay attempts inside one 15-second rate-limit window all
    fail, so a throttle read exactly like "this stop can never be placed" — in
    the violent move the stop exists for, which is precisely when a venue
    throttles (2026-07-31 partial-failure review).
    """


class UnknownCoinError(ExchangeError):
    """The requested coin is not present in the exchange's perp universe."""


class OrderIdempotencyContradiction(ExchangeError):
    """The exchange's answer contradicts our durable evidence about a cloid.

    Raised by the §8.3 protocol when the two cannot both be true — the exchange
    calls a cloid a duplicate but its own orderStatus does not know it (rule 5),
    or orderStatus answers ``unknownOid`` for a cloid we hold proof it received
    (rule 10). Either way an order MAY be live under that cloid, so no resend is
    permitted and a human must look before anything else goes out.

    Deliberately NOT an :class:`ExchangeRequestError`: that class means "the
    request failed, try it again", and the obvious ``except ExchangeError:
    retry`` idiom would turn a possible double position into one more send.
    Callers that retry transport failures must catch ``ExchangeRequestError``,
    never the base class — this is the error that has to reach an operator.
    """


class MalformedResponseError(ExchangeError):
    """Hyperliquid returned a response missing fields the mapper requires.

    Optionally CARRIES the offending round-trip in :attr:`payload`, so the
    layer that owns evidence storage can persist it even though the layer that
    detected the malformation does not know where evidence goes. The rejecting
    parsers sit under :mod:`signed_client`, which has no ``payload_dir``; the
    caller that does (``live.orders``) only ever saw the exception. So a
    misrouted order ack — the one case where the response names a STRANGER's
    oid — left nothing behind but ``str(exc)`` on the attempt row, while the
    fills and mapper paths both keep the payload they refuse.

    A class-level default rather than an ``__init__`` parameter: every raise
    site constructs this with a bare message, and none of them should have to
    change to gain the attribute. ``None`` means "no evidence attached", which
    is also all a ``None`` payload would be worth.
    """

    payload: Any = None

    def attach_payload(self, payload: Any) -> None:
        """Record the whole round-trip this error was raised inside of.

        Called on the way OUT, by the frame that holds the complete response —
        so if nested frames ever both attach, the outermost (largest) scope
        wins, which is the one worth keeping as evidence. Never raises and
        never inspects the payload: this runs on a failure path, and an
        evidence-gathering step that can itself fail would replace a
        diagnosable venue fault with a serialisation bug.
        """
        self.payload = payload
