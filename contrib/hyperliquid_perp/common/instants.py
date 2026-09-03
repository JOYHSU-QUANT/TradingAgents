"""Instants and spans as the store and the operator see them.

:func:`parse_instant` decodes the repository's timestamp form. It writes every
instant as an ISO-8601 UTC string, and readers across four layers decode it:
the scheduler, the run lock, reconciliation, the paper and live validators,
the CLI's lease probes, the no-decision policy. The decoder lived on
``paper.scheduler``, which every one of them then had to import — pulling the
paper engine into modules as far from it as ``live.validation`` and the
keyless CLI lease checks, for one function. Here, at the bottom of the import
graph, it costs nothing to reach (issue #122); ``paper.scheduler`` re-exports
it for the callers that always found it there.

:func:`whole_hours_label` renders a span the way an operator-facing message
states a window ("6h", "4h"). Two modules derive such a label from a constant
(the reconciler's fill-backfill window, the freshness guard's decision cycle)
and each had grown its own copy of the same guard: a span that is not whole
hours must refuse at import rather than render truncated, because "5h" over a
5h30m window understates the bound the message is describing.

:func:`epoch_ms`, :func:`from_epoch_ms` and :func:`delta_ms` are the ONE
implementation of the venue's time form. Hyperliquid stamps everything —
candle closes, funding settlements, fills, the exchange clock, the kill
switch's deadline — as integer epoch milliseconds, and a dozen call sites
across the exchange adapter, the live engine, the audit log and the context
builder each converted on their own, by two different routes: some through
``timedelta`` floor division (exact), most through a float
(``int(dt.timestamp() * 1000)`` / ``datetime.fromtimestamp(ms / 1000)``),
whose exactness at a given magnitude is an accident of float formatting, not
a property of the code (issue #157). Every conversion here is integer
arithmetic on ``timedelta``'s microseconds, so it is exact by construction:
``from_epoch_ms(epoch_ms(t)) == t`` for any millisecond-aligned aware ``t``
and ``epoch_ms(from_epoch_ms(n)) == n`` for any integer ``n``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["delta_ms", "epoch_ms", "from_epoch_ms", "parse_instant", "whole_hours_label"]

_HOUR = timedelta(hours=1)
_ONE_MS = timedelta(milliseconds=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def parse_instant(text: str) -> datetime:
    """Decode a stored ISO-8601 UTC timestamp (the repository's storage form)."""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:  # the write boundary never stores naive stamps
        raise ValueError(f"stored timestamp {text!r} is naive; the store is corrupt")
    return value


def whole_hours_label(span: timedelta, *, what: str) -> str:
    """``span`` as ``"6h"``; ``ValueError`` naming ``what`` if it is not whole hours.

    Meant for module-level constants and construction-time bindings, so the
    raise lands at import or at start-up, before the first cycle — a retuned
    window that is no longer whole hours is a change the message rendering it
    has to be rewritten for, not rounded past.
    """
    if span % _HOUR:
        raise ValueError(
            f"{what} must be a whole number of hours; the operator-facing label "
            f"renders it as hours (got {span})"
        )
    return f"{span // _HOUR}h"


def delta_ms(later: datetime, earlier: datetime) -> int:
    """``later - earlier`` in whole milliseconds, by integer arithmetic.

    Every bound the freshness guard checks is compared in milliseconds
    against stamps the exchange sent as integer ms, so the subtraction must
    be exact: ``int(delta.total_seconds() * 1000)`` goes through a float and
    reads some deltas 1ms short (e.g. 65788957ms → 65788956.99999999 →
    65788956; 0.43% of 3M random deltas under three days, measured
    2026-08-31 and pinned by a test), which would let a context sitting
    exactly on a limit pass or refuse by rounding rather than by the limit.
    Floors (``//``) rather than truncates, so a sub-millisecond negative reads
    as ``-1``, not ``0`` — the inputs that can carry sub-ms fractions (host
    clock readings) only ever feed a skew note and minutes-wide fallback
    bounds, where that millisecond changes nothing.
    """
    return (later - earlier) // _ONE_MS


def epoch_ms(moment: datetime, *, what: str = "instant") -> int:
    """``moment`` as epoch milliseconds — the venue's time form — exactly.

    :func:`delta_ms` against the epoch, so a millisecond the exchange sent
    (a candle's ``close_time``, a funding settlement, the exchange clock)
    that was decoded by :func:`from_epoch_ms` comes back out as the same
    integer, and a window end computed from it neither drops a bar the
    exchange has closed (1ms short) nor admits one it has not. A naive
    ``moment`` is refused, naming ``what`` the caller handed in — the
    subtraction would raise anyway, but about mixing offsets, not about the
    clock; and a naive value would otherwise be read in the host's local
    zone, silently off by the UTC offset (the audit log's rationale).
    """
    if moment.tzinfo is None:
        raise ValueError(f"{what} must be timezone-aware (UTC)")
    return delta_ms(moment, _EPOCH)


def from_epoch_ms(ms: int) -> datetime:
    """Epoch milliseconds — the venue's time form — as an aware UTC datetime.

    The inverse of :func:`epoch_ms`, by the same integer arithmetic (an epoch
    offset built from ``ms`` whole milliseconds, never ``ms / 1000`` through
    a float). Takes an ``int`` only — a ``float`` would smuggle the float
    route back in, and a ``bool`` is an ``int`` to ``isinstance`` but never a
    timestamp — and refuses anything else by name; the wire-boundary callers
    (the fill parser, the account clock, the l2Book stamp) convert their raw
    field with ``int()`` first and translate the failure to their own
    malformed-response error, so the ``TypeError`` here is for a caller bug,
    not for bad data. An out-of-range value raises ``OverflowError`` from
    ``timedelta`` itself, as the float route did from ``fromtimestamp``.
    """
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise TypeError(f"epoch milliseconds must be an int, got {type(ms).__name__}")
    return _EPOCH + timedelta(milliseconds=ms)
