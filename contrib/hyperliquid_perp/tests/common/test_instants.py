"""``common.instants`` — the store's timestamp decoder, the span renderings, the span
guards, epoch ms."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.common.constants import MAX_EPOCH_MS, MIN_EPOCH_MS
from contrib.hyperliquid_perp.common.instants import (
    delta_ms,
    epoch_ms,
    from_epoch_ms,
    gap_label,
    parse_instant,
    seconds_span,
    whole_hours_label,
)

_NOW = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)


def test_parse_instant_round_trips_the_stores_form():
    stamp = datetime(2026, 8, 31, 12, 0, 5, tzinfo=timezone.utc)
    assert parse_instant(stamp.isoformat()) == stamp


def test_parse_instant_refuses_a_naive_stamp():
    # The write boundary never stores a naive stamp, so one in the store is
    # corruption, not a timezone to assume: every reader compares the result
    # against aware instants, and a naive value would raise deep inside that
    # arithmetic instead of here, where the message names the store.
    with pytest.raises(ValueError, match="naive; the store is corrupt"):
        parse_instant("2026-08-31T12:00:05")


def test_whole_hours_label_renders_a_span_as_hours():
    assert whole_hours_label(timedelta(hours=4), what="x") == "4h"
    assert whole_hours_label(timedelta(hours=6), what="x") == "6h"
    assert whole_hours_label(timedelta(days=1), what="x") == "24h"


def test_whole_hours_label_refuses_a_fractional_hour_naming_the_constant():
    # Shared by the reconciler's lookback label and the freshness guard's
    # cycle label, both module-level: floor division would render 5h30m as
    # "5h" and understate the bound the message describes, so the helper
    # refuses — and names WHICH constant, since the raise lands at import.
    with pytest.raises(ValueError, match="my.CONSTANT must be a whole number of hours"):
        whole_hours_label(timedelta(hours=5, minutes=30), what="my.CONSTANT")


@pytest.mark.parametrize(
    ("ms", "expected"),
    [
        # Both sides of every unit boundary. The boundaries ARE 1000 / 60_000
        # / 3_600_000 because the rule compares the unrounded figure; each
        # pair below is the last value that keeps the smaller unit and the
        # first that earns the larger one.
        (1, "1 ms"),
        (999, "999 ms"),
        (1_000, "1.0 s"),
        # 0.025 minutes. A threshold on the raw value rendered this "0.0 min"
        # — the defect the helper exists to prevent, one unit down from where
        # it was first found.
        (1_500, "1.5 s"),
        (59_999, "60.0 s"),
        (60_000, "1.0 min"),
        # Rounding BEFORE the comparison promotes from 0.95 of a unit up, so
        # it would render these two as "1.0 min" and "1.0h" — overstating a
        # stalled feed's age by about 5% in the WARNING that sizes it. (The
        # promotion starts at 57_001 ms and 3_420_001 ms, not at a round 57
        # seconds or 57 minutes: round(0.95, 1) is 0.9.)
        (57_001, "57.0 s"),
        (3_421_000, "57.0 min"),
        (3_599_999, "60.0 min"),
        (3_600_000, "1.0h"),
        (86_400_001, "24.0h"),
    ],
)
def test_a_gap_is_rendered_in_the_largest_unit_that_reaches_one(ms, expected):
    # Exact strings, not a property. The property this helper exists for
    # ("never prints as 0.0 of a unit") is satisfied by the second rule this
    # helper was written with — the one that selected on the rounded figure,
    # caught in review before it shipped — so asserting the property alone is
    # exactly what would have let that rule through.
    assert gap_label(ms) == expected


def test_a_value_this_helper_has_no_unit_for_is_rendered_rather_than_refused():
    # A precondition this helper deliberately does NOT enforce by a raise,
    # pinned here so a later consistency pass cannot quietly add the guard.
    # Every call site is an argument to a WARNING on a path that is already
    # refusing something, and those refusals cost a prompt SECTION — an
    # exception escaping there would cost the whole decision cycle instead.
    # So a caller that forgot to flip its sign gets an ugly millisecond count
    # in a log line, which a test at each live call site catches, rather than
    # a dead run.
    assert gap_label(-1) == "-1 ms"
    assert gap_label(-3_600_000) == "-3600000 ms"
    assert gap_label(0) == "0 ms"


def test_seconds_span_converges_a_number_of_seconds_onto_a_span():
    # Every numeric shape a ``*_seconds`` argument arrives in — an int, a
    # float, the ``Decimal`` a config number is — lands on the same span.
    assert seconds_span("x", 3600) == timedelta(hours=1)
    assert seconds_span("x", 0.5) == timedelta(milliseconds=500)
    assert seconds_span("x", Decimal("3600")) == timedelta(hours=1)


def test_seconds_span_refuses_by_name_what_a_bare_comparison_let_through():
    # The table the four live constructors used to guard on their own with a
    # bare ``<= 0`` (issue #224; the backfiller had grown this check first,
    # issue #169): what is not a number of seconds at all is a TypeError, and
    # a number that is not a usable span — NaN, an infinity, non-positive,
    # beyond ``timedelta``'s range, under its microsecond — a ValueError, each
    # naming the argument the caller handed in.
    for not_a_number in ("3600", True, None):
        with pytest.raises(TypeError, match="^lookback_seconds must be a number of seconds, got"):
            seconds_span("lookback_seconds", not_a_number)
    for not_a_span in (
        Decimal("NaN"),
        Decimal("sNaN"),  # float() refuses a signaling NaN with its own ValueError
        float("nan"),
        float("inf"),
        10**400,  # too large for a float
        10**20,  # a float, but beyond timedelta's range
        Decimal("1e15"),
        1e-7,  # positive, but timedelta rounds it to a zero-width window
        0,
        Decimal("-1"),
    ):
        with pytest.raises(ValueError, match="^lookback_seconds must be > 0 and finite"):
            seconds_span("lookback_seconds", not_a_span)


# ---------------------------------------------------------------------------
# epoch ms — the venue's time form, one implementation (issue #157)
# ---------------------------------------------------------------------------


def test_delta_ms_is_exact_where_the_float_route_reads_a_millisecond_short():
    # The reason ``delta_ms`` exists, pinned on a value where the float
    # route actually fails: 65788957ms (a shade over 18h) comes out of
    # ``int(delta.total_seconds() * 1000)`` as 65788956. The freshness guard
    # compares ages against ms limits, so a boundary case would otherwise
    # pass or refuse by rounding. Both sides of the pin are asserted so the
    # test cannot go quietly vacuous if Python's float formatting changes.
    later = _NOW + timedelta(milliseconds=65_788_957)
    delta = later - _NOW
    assert int(delta.total_seconds() * 1000) == 65_788_956  # the float route's error
    assert delta_ms(later, _NOW) == 65_788_957
    # ...and the floor semantics the docstring states for a sub-ms negative.
    assert delta_ms(_NOW, _NOW + timedelta(microseconds=500)) == -1


def test_epoch_ms_round_trips_every_millisecond_exactly():
    # Exact by construction, not by magnitude: the venue's stamps
    # (``close_time``, funding ``time``, the l2Book clock) go through
    # ``from_epoch_ms`` and back through ``epoch_ms`` at window ends, and a
    # millisecond lost either way drops a bar the exchange has closed. Swept
    # across the magnitudes ``datetime`` can hold — the epoch, a 2026 stamp,
    # the last millisecond of year 9999 — and a dense run of neighbours
    # around one stamp, where the float route's rounding is hit or miss.
    stamps = [0, 1, 999, 1_000, 1_787_369_175_468, 4_102_444_800_000, 253_402_300_799_999]
    stamps += range(1_788_163_200_000, 1_788_163_200_000 + 2_000)
    for ms in stamps:
        moment = from_epoch_ms(ms)
        assert moment.tzinfo is timezone.utc
        assert epoch_ms(moment, what="x") == ms, ms
    # ...and the other direction, from a millisecond-aligned instant.
    assert from_epoch_ms(epoch_ms(_NOW, what="x")) == _NOW
    assert epoch_ms(_NOW, what="x") == 1_788_163_200_000


def test_epoch_ms_floors_a_sub_millisecond_instant():
    # ``delta_ms`` semantics: the microsecond part floors, so an instant half
    # a millisecond before a boundary is the earlier millisecond, not the
    # later one — the window end is never ahead of the clock it was cut at.
    assert epoch_ms(_NOW + timedelta(microseconds=500), what="x") == 1_788_163_200_000
    assert epoch_ms(_NOW - timedelta(microseconds=500), what="x") == 1_788_163_199_999


def test_epoch_ms_refuses_a_naive_instant_naming_what_was_handed_in():
    # A naive instant would be read in the host's local zone — silently off
    # by the UTC offset — so it is refused by name, and the name is the
    # caller's, REQUIRED (no anonymous refusal): each caller pins its own
    # wording through ``what``. The label below is this test's own sample, not
    # a live one — the two windowed reads now pass "candle window end" and
    # "funding history window end" and pin those in ``test_market_data.py``.
    naive = datetime(2026, 8, 31, 8, 0)
    with pytest.raises(ValueError, match="^market data window end must be timezone-aware"):
        epoch_ms(naive, what="market data window end")
    with pytest.raises(TypeError):
        epoch_ms(naive)  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [1_788_163_200_000.0, True, "1788163200000", None])
def test_from_epoch_ms_takes_an_int_only(bad):
    # A float would bring the float route back through the one door meant
    # to close it; a bool is an int to ``isinstance`` but never a stamp. The
    # wire-boundary callers ``int()`` their raw field first, so this is a
    # caller bug, refused by type — not bad data, which they translate.
    with pytest.raises(TypeError, match="epoch milliseconds must be an int"):
        from_epoch_ms(bad)


def test_from_epoch_ms_overflows_like_the_float_route_did():
    # An out-of-range value raises what ``fromtimestamp`` did, so the
    # wire-boundary ``except`` clauses that list OverflowError still hold.
    with pytest.raises(OverflowError):
        from_epoch_ms(10**20)


def test_the_published_epoch_ms_range_is_exactly_what_the_decoder_accepts():
    # The DRIFT PIN behind ``constants.MIN_EPOCH_MS`` / ``MAX_EPOCH_MS``
    # (issue #191). ``domains.perp.schema`` refuses a venue stamp outside that
    # range so an ``OverflowError`` — which is neither an ``ExchangeError`` nor
    # a ``ValueError``, and so escapes every handler between the wire and the
    # decode — can never be raised in production. That guard is only as good as
    # the constants agreeing with the decoder, and the two are declared in
    # different modules (``schema`` sits inside the config loader's locked
    # import closure and cannot reach ``instants``).
    #
    # Both edges, from both sides: each bound decodes, and one millisecond
    # further out does not. A bound transcribed as a literal, or drifting a
    # millisecond if ``datetime``'s range ever moved, fails here rather than
    # in a paper run.
    assert from_epoch_ms(MAX_EPOCH_MS).year == 9999
    assert from_epoch_ms(MIN_EPOCH_MS).year == 1
    with pytest.raises(OverflowError):
        from_epoch_ms(MAX_EPOCH_MS + 1)
    with pytest.raises(OverflowError):
        from_epoch_ms(MIN_EPOCH_MS - 1)


def test_the_epoch_ms_range_round_trips_through_epoch_ms():
    # The bounds are stamps, not merely numbers the decoder tolerates: the
    # module's own round-trip identity has to hold at the very edges, or the
    # guard would be admitting a value the rest of the pipeline cannot carry.
    for edge in (MIN_EPOCH_MS, MAX_EPOCH_MS):
        assert epoch_ms(from_epoch_ms(edge), what="range edge") == edge
