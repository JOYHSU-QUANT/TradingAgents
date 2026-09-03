"""``common.instants`` — the store's timestamp decoder, the whole-hours label, epoch ms."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.common.instants import (
    delta_ms,
    epoch_ms,
    from_epoch_ms,
    parse_instant,
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
        assert epoch_ms(moment) == ms, ms
    # ...and the other direction, from a millisecond-aligned instant.
    assert from_epoch_ms(epoch_ms(_NOW)) == _NOW
    assert epoch_ms(_NOW) == 1_788_163_200_000


def test_epoch_ms_floors_a_sub_millisecond_instant():
    # ``delta_ms`` semantics: the microsecond part floors, so an instant half
    # a millisecond before a boundary is the earlier millisecond, not the
    # later one — the window end is never ahead of the clock it was cut at.
    assert epoch_ms(_NOW + timedelta(microseconds=500)) == 1_788_163_200_000
    assert epoch_ms(_NOW - timedelta(microseconds=500)) == 1_788_163_199_999


def test_epoch_ms_refuses_a_naive_instant_naming_what_was_handed_in():
    # A naive instant would be read in the host's local zone — silently off
    # by the UTC offset — so it is refused by name, and the name is the
    # caller's: the market-data reader pins its own wording through ``what``.
    naive = datetime(2026, 8, 31, 8, 0)
    with pytest.raises(ValueError, match="^instant must be timezone-aware"):
        epoch_ms(naive)
    with pytest.raises(ValueError, match="^market data window end must be timezone-aware"):
        epoch_ms(naive, what="market data window end")


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
