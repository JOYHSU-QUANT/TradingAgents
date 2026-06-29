"""Tests for market_data helpers that need no network (interval math)."""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import interval_to_ms


def test_interval_to_ms_known_intervals():
    assert interval_to_ms("4h") == 4 * 60 * 60_000
    assert interval_to_ms("1d") == 24 * 60 * 60_000
    assert interval_to_ms("1m") == 60_000


def test_interval_to_ms_unknown_raises_valueerror():
    # A typo like "4H" (wrong case) must raise a clear ValueError rather than
    # silently selecting a wrong interval.
    with pytest.raises(ValueError):
        interval_to_ms("4H")
