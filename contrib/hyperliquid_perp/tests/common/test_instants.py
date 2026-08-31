"""``common.instants`` — the store's timestamp decoder and the whole-hours label."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.common.instants import parse_instant, whole_hours_label


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
