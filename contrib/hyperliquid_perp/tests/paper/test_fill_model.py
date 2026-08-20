"""Tests for the simulated fill-price model (execution §5.2 / §6.4)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.paper.fill_model import fill_price
from contrib.hyperliquid_perp.persistence.models import Side

D = Decimal


def test_buy_fills_above_mid():
    # 50000 * (1 + 5/10000) = 50025
    assert fill_price(D(50000), Side.BUY, D(5)) == D("50025")


def test_sell_fills_below_mid():
    # 50000 * (1 - 5/10000) = 49975
    assert fill_price(D(50000), Side.SELL, D(5)) == D("49975")


def test_zero_slippage_fills_at_mid():
    assert fill_price(D(50000), Side.BUY, D(0)) == D(50000)
    assert fill_price(D(50000), Side.SELL, D(0)) == D(50000)


def test_accepts_string_side():
    assert fill_price(D(100), "buy", D(0)) == D(100)


def test_rejects_non_positive_mid():
    with pytest.raises(ValueError):
        fill_price(D(0), Side.BUY, D(5))
    with pytest.raises(ValueError):
        fill_price(D(-1), Side.BUY, D(5))


def test_rejects_negative_slippage():
    with pytest.raises(ValueError):
        fill_price(D(100), Side.BUY, D(-1))
