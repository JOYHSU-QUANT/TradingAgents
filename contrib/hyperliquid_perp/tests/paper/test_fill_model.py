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


# -- §5.2.1 maker post / fill rules -----------------------------------------------


def test_maker_post_price_sits_on_the_passive_side_of_the_modelled_touch():
    from contrib.hyperliquid_perp.paper.fill_model import maker_post_price

    mid, tick = D("50000"), D("0.1")
    buy = maker_post_price(mid, "buy", D("1"), tick)
    sell = maker_post_price(mid, "sell", D("1"), tick)
    assert buy == D("49995") and sell == D("50005")  # 1 bps each side
    assert maker_post_price(mid, "buy", D(0), tick) == mid  # zero half-spread: at the mid
    with pytest.raises(ValueError):
        maker_post_price(D(0), "buy", D(1), tick)
    with pytest.raises(ValueError):
        maker_post_price(mid, "buy", D(-1), tick)


def test_maker_would_fill_needs_the_mid_to_trade_through_by_a_tick():
    from contrib.hyperliquid_perp.paper.fill_model import maker_would_fill

    tick = D("0.1")
    post = D("49995")
    assert not maker_would_fill(post, "buy", post, tick)  # a touch is not a fill
    assert not maker_would_fill(post - D("0.05"), "buy", post, tick)
    assert maker_would_fill(post - tick, "buy", post, tick)
    assert maker_would_fill(post - D(50), "buy", post, tick)
    ask = D("50005")
    assert not maker_would_fill(ask, "sell", ask, tick)
    assert maker_would_fill(ask + tick, "sell", ask, tick)
    with pytest.raises(ValueError):
        maker_would_fill(post, "buy", post, D(0))


def test_maker_post_price_rounds_toward_the_passive_side():
    from contrib.hyperliquid_perp.paper.fill_model import maker_post_price

    mid, tick = D("50000"), D("1")
    assert maker_post_price(mid, "buy", D("0.3"), tick) == D("49998")  # raw 49998.5 rounds down
    assert maker_post_price(mid, "sell", D("0.3"), tick) == D("50002")  # raw 50001.5 rounds up
