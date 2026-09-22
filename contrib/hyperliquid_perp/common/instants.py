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

:func:`whole_hours_label` renders a span (not an instant — it lives here
because it is the same dependency-free bottom layer) the way an
operator-facing message states a window ("6h", "4h"). Two modules derive
such a label from a constant
(the reconciler's fill-backfill window, the freshness guard's decision cycle)
and each had grown its own copy of the same guard: a span that is not whole
hours must refuse at import rather than render truncated, because "5h" over a
5h30m window understates the bound the message is describing.

:func:`gap_label` renders the OTHER operator-facing span: not a bound stated
in a message, but the measured distance between two venue stamps — how stale a
feed is, how far a handoff document sits from the bar it is read against. It
lives beside :func:`whole_hours_label` for the same reason. Its callers are
the two age refusals in ``domains.perp.macro_trend``, the two in
``domains.perp.research_signal`` — siblings by design,
which had nonetheless arrived at two different answers to the one question: a
fixed ``%.1fh``, which prints a real gap as ``0.0h``, and the unit-picking
rule below, which does not (issue #284) — and the last-fill line of
``domains.perp.prompt_context``, which had the same fixed ``%.1f`` hours in
PROMPT text and so told the model a fill under three minutes old was "0.0
hours" before the as-of (issue #288; shipped on its own because moving a
prompt byte is a paper-run segmentation point).

One OTHER rendering is named below because it was weighed against this one
and left where it is. That is all this list is. It is not a survey of every
duration the package prints — several modules render a span in a shape of
their own, and nothing here has counted them, so a sweep for a rendering
defect starts from a grep, not from this paragraph:

- ``domains.perp.freshness``'s ``_format_duration_ms`` prints a compound
  ``14h 12m 30s`` and is deliberately NOT converged onto this: its sentences
  carry an age and the limit it is read against side by side and want the two
  in ONE shape, which answers "how do I state an age next to its limit?"
  rather than "what single figure do I state a gap as?". Converging them
  would rewrite live freshness refusal text to settle a question it has not
  got.

:func:`seconds_span` is the ONE convergence of a ``*_seconds`` constructor
argument onto a span. Four live constructors take one (the backfill lookback,
the stream's stale and silent thresholds, the kill switch's tick-gap
promise), and each once guarded it with a bare ``<= 0`` that could not see
what it let through: a ``bool`` was silently a one-second window, a ``str``
died at the comparison, a float NaN or infinity died inside ``timedelta``
with a message naming nothing, and a value beyond ``timedelta``'s range (or
under its microsecond, which rounds to a zero-width span) overflowed or
rounded there. The backfiller grew the full check first (issue #169); here
it is shared, so every such argument is refused by name the same way
(issue #224). The YAML loaders already coerce their ``*_seconds`` keys, so
in practice this refuses a caller that constructs directly.

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

import math
import numbers
from datetime import datetime, timedelta, timezone
from decimal import Decimal

__all__ = [
    "Seconds",
    "delta_ms",
    "epoch_ms",
    "from_epoch_ms",
    "gap_label",
    "parse_instant",
    "seconds_span",
    "whole_hours_label",
]

# The shape a ``*_seconds`` constructor argument is declared in: what
# :func:`seconds_span` accepts, stated once beside it so no constructor's
# signature admits less than the guard does (``Decimal`` is the shape a
# config number arrives in; a numpy scalar is a ``Real`` and passes too).
Seconds = float | Decimal

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


def gap_label(ms: int) -> str:
    """A duration rendered at a scale that never reads as no duration at all.

    A fixed unit makes a small gap vanish: at hours to one decimal, a bar one
    millisecond early prints as ``0.0h`` — no gap, in a sentence about a gap.
    So the unit is the LARGEST whose figure is at least 1.0, and milliseconds
    are printed as an integer when even seconds would not reach that.
    (``value >= 1.0`` is the same test as ``ms >= scale``; either spelling is
    fine.)

    Both halves of that are load-bearing, and this helper got each of them
    wrong once before settling here:

    - **The SECONDS tier.** The first version went milliseconds, minutes,
      hours, so 1.5 s landed in minutes and printed ``0.0 min`` — the same
      vanishing gap one unit down from where it was first found.
    - **The unrounded comparison.** The second version selected on
      ``round(value, 1) >= 1.0``, which promotes from 0.95 of a unit upward,
      so a stalled feed anywhere from just past 57 minutes to just under an
      hour printed ``1.0h`` — overstating the one number that sizes the
      outage by up to about 5%. (Just PAST: ``round(0.95, 1)`` is 0.9, so
      exactly 57 minutes still printed ``57.0 min``.)

    The cost of not rounding is that the top of a unit is not normalised:
    59.999 s prints as ``60.0 s`` rather than ``1.0 min``. That is
    unidiomatic, and the figure is still rounded to the printed grid (a tenth
    of a second here). What it does not do is promote a figure into a unit it
    has not reached, which is the error that misleads.

    What this does NOT fix, because no choice of unit can: a value just past a
    bound still prints as that bound. 24h + 1 ms is ``24.0h`` at any sane
    precision, and in a sentence saying the 24h limit was exceeded that reads
    as a contradiction. A caller whose sentence names the bound closes that by
    printing the EXCESS as a second figure — ``24.0h ... 1 ms past the 24h`` —
    which this helper only supplies the formatting for. A caller whose
    sentence names no bound (``research_signal``'s future-side refusal says
    only that no closed bar can sit there) has nothing to contradict and
    prints one figure.

    The ladder stops at hours on purpose, with no day tier, and NOT because
    the figures stay small: a ``1d`` research document is a supported shape,
    and the first age its bound refuses already renders ``48.0h``. It stops
    there because the module that does print days
    (``freshness._format_duration_ms``, above) opens that band at 48h for a
    reason of its own, and two ladders in one package disagreeing about where
    a day begins is worse than one of them counting hours past 24. ``48.0h``
    is legible; ``2d 0h`` beside a ``30h 0m 0s`` from the other renderer is
    not.

    Takes a duration in milliseconds because that is the form both callers
    hold one in: each subtracts one venue stamp from another — a candle's
    ``close_time``, a handoff document's ``as_of_ms`` — and those are integer
    milliseconds the whole way, never a float of seconds.
    Negative input is the caller's to flip — the two directions are separate
    sentences with separate causes, so which one is being told is decided
    where the sign is read, not here. That precondition is NOT enforced by a
    raise, and deliberately: every call site is an argument to a WARNING on a
    path that is already refusing something, and those refusals cost their
    caller a prompt SECTION while an exception escaping there would cost the
    whole decision cycle. A forgotten flip therefore prints a raw millisecond
    count — ugly, and caught by a test at each live call site — rather than
    ending a run over a log line.
    """
    for scale, unit in ((3_600_000, "h"), (60_000, " min"), (1000, " s")):
        value = ms / scale
        if value >= 1.0:
            return f"{value:.1f}{unit}"
    return f"{ms} ms"


def seconds_span(name: str, value: object) -> timedelta:
    """``value`` seconds as a positive, finite span; refused by name otherwise.

    ``TypeError`` for anything that is not a number of seconds — a ``bool``
    (an ``int`` to ``isinstance``, never a duration), a ``str``, ``None`` —
    and ``ValueError`` for a number that is not a usable span: NaN, an
    infinity, zero or negative, beyond ``timedelta``'s range, or so small
    that ``timedelta`` rounds it to nothing. ``numbers.Real`` and ``Decimal``
    both pass (a numpy scalar is Real; ``Decimal`` is the shape a config
    number arrives in, and is not Real), converged through ``float`` so a
    ``Decimal`` NaN or a value too large for a float are refused with the
    others rather than dying on the conversion. A span the caller's own
    datetime arithmetic cannot honour — thousands of years — is not refused
    here; no wiring passes one.
    """
    if isinstance(value, bool) or not isinstance(value, (numbers.Real, Decimal)):
        raise TypeError(f"{name} must be a number of seconds, got {type(value).__name__}")
    try:
        seconds = float(value)
    except (OverflowError, ValueError):  # too large for a float; a signaling NaN
        seconds = math.nan
    if (
        not math.isfinite(seconds)
        or not 0 < seconds < timedelta.max.total_seconds()
        or timedelta(seconds=seconds) <= timedelta(0)
    ):
        raise ValueError(
            f"{name} must be > 0 and finite, within timedelta's range, got {value}"
        )
    return timedelta(seconds=seconds)


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

    No awareness guard of its own, unlike :func:`epoch_ms`: both operands
    are instants the caller already holds as aware UTC (the context's
    ``as_of`` and the clock readings the freshness guard compares, the epoch
    here), and a naive/aware mix fails on the subtraction itself. The guard
    that names a caller-supplied instant belongs where one is supplied.
    """
    return (later - earlier) // _ONE_MS


def epoch_ms(moment: datetime, *, what: str) -> int:
    """``moment`` as epoch milliseconds — the venue's time form — exactly.

    :func:`delta_ms` against the epoch, so a millisecond the exchange sent
    (a candle's ``close_time``, a funding settlement, the exchange clock)
    that was decoded by :func:`from_epoch_ms` comes back out as the same
    integer, and a window end computed from it neither drops a bar the
    exchange has closed (1ms short) nor admits one it has not. A naive
    ``moment`` is refused, naming ``what`` the caller handed in — required,
    as on :func:`whole_hours_label`, so no refusal is anonymous: the
    subtraction would raise anyway, but about mixing offsets, not about
    which clock; and through the float route a naive value was not refused
    at all but read in the host's local zone, silently off by the UTC
    offset (the audit log's rationale).
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

    That range is published as ``constants.MIN_EPOCH_MS`` /
    ``constants.MAX_EPOCH_MS`` for the callers that must refuse an undecodable
    stamp BEFORE it reaches here: ``OverflowError`` is neither an
    ``ExchangeError`` nor a ``ValueError``, so it slips through every
    venue-failure and malformed-row handler between the wire and this call and
    ends the run (issue #191). ``domains/perp/schema``'s two venue-stamp DTOs
    hold the bound at construction; a drift pin in ``tests/common/test_instants``
    keeps the published range equal to what this function actually accepts, so
    the two can never disagree about where the edge is.
    """
    if isinstance(ms, bool) or not isinstance(ms, int):
        raise TypeError(f"epoch milliseconds must be an int, got {type(ms).__name__}")
    return _EPOCH + timedelta(milliseconds=ms)
