"""Tests for the injectable clock (execution §1.1 / §5.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from contrib.hyperliquid_perp.paper.clock import ManualClock, WallClock

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)


def test_wall_clock_is_utc_aware():
    now = WallClock().now()
    assert now.tzinfo is not None


def test_manual_clock_advance():
    clock = ManualClock(_T0)
    assert clock.now() == _T0
    clock.advance(30)
    assert clock.now() == _T0 + timedelta(seconds=30)


def test_manual_clock_set_forward():
    clock = ManualClock(_T0)
    target = _T0 + timedelta(hours=1)
    clock.set(target)
    assert clock.now() == target


def test_manual_clock_rejects_backward():
    clock = ManualClock(_T0)
    clock.advance(60)
    with pytest.raises(ValueError):
        clock.set(_T0)
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_manual_clock_rejects_naive_start():
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 7, 6, 12, 0))


def test_manual_clock_normalises_to_utc():
    other = timezone(timedelta(hours=5))
    clock = ManualClock(datetime(2026, 7, 6, 17, 0, tzinfo=other))
    assert clock.now() == _T0
