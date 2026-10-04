"""Pool readings: what one may hold, what the data checks flag, and the bar they make."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.uniswap_v3.constants import ETHEREUM_MAINNET, POOLS, TOKENS
from contrib.uniswap_v3.domain.bars import (
    BarFlag,
    BarSettings,
    Finality,
    PoolBar,
    assemble_bar,
    pool_bar_flags,
)
from contrib.uniswap_v3.domain.prices import (
    MAX_SQRT_RATIO,
    MIN_SQRT_RATIO,
    price_from_sqrt_price_x96,
)
from contrib.uniswap_v3.tests.fakes.node import (
    BTC_TICK as _BTC_TICK,
    DAY,
    DEFAULT_TICK as _ETH_TICK,
    FIRST_DAY,
    pool_bar as _reading,
)

_USDC = TOKENS[ETHEREUM_MAINNET]["USDC"]
_WETH = TOKENS[ETHEREUM_MAINNET]["WETH"]
_WBTC = TOKENS[ETHEREUM_MAINNET]["WBTC"]
_USDC_WETH = POOLS[ETHEREUM_MAINNET]["USDC/WETH-500"]
_WBTC_WETH = POOLS[ETHEREUM_MAINNET]["WBTC/WETH-500"]
_SETTINGS = BarSettings()


# --- BarSettings -----------------------------------------------------------


def test_the_default_bar_is_one_day_checked_against_a_thirty_minute_twap():
    assert BarSettings() == BarSettings(
        interval_seconds=86_400,
        twap_window_seconds=1_800,
        max_twap_deviation=Decimal("0.05"),
        max_move=Decimal("0.5"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("interval_seconds", 0),
        ("interval_seconds", True),
        ("interval_seconds", 3600.0),
        ("twap_window_seconds", 0),
        ("twap_window_seconds", 2**32),
        ("max_twap_deviation", Decimal(0)),
        ("max_twap_deviation", 0.05),
        ("max_twap_deviation", Decimal("NaN")),
        ("max_move", Decimal("-0.5")),
        ("max_move", "0.5"),
    ],
)
def test_bar_settings_refuse_a_value_that_is_not_a_length_or_a_limit(field, value):
    with pytest.raises(ValueError, match=field):
        BarSettings(**{field: value})


# --- PoolBar ---------------------------------------------------------------


def test_a_reading_is_pending_until_it_is_told_otherwise():
    fields = {
        name: getattr(_reading(), name)
        for name in PoolBar.__dataclass_fields__
        if name != "finality"
    }
    assert PoolBar(**fields).finality is Finality.PENDING


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"chain_id": -1}, "chain_id"),
        ({"chain_id": True}, "chain_id"),
        ({"pool": "0x1234"}, "pool must be 0x"),
        ({"interval_seconds": 0}, "interval_seconds"),
        ({"time": FIRST_DAY + 1}, "multiple of the 86400-second interval"),
        ({"time": 0}, "time must be a positive multiple"),
        ({"close_block": -1}, "close_block"),
        ({"close_block_time": FIRST_DAY}, "the last before the boundary"),
        ({"close_block_hash": "0x" + "AB" * 32}, "close_block_hash"),
        ({"close_block_hash": "0x1234"}, "close_block_hash"),
        ({"sqrt_price_x96": MIN_SQRT_RATIO - 1}, "sqrt_price_x96"),
        ({"sqrt_price_x96": MAX_SQRT_RATIO}, "sqrt_price_x96"),
        ({"sqrt_price_x96": "79228162514264337593543950336"}, "sqrt_price_x96"),
        ({"tick": 887_273}, "tick must be"),
        ({"twap_tick": -887_273}, "twap_tick must be"),
        ({"twap_tick": None}, "twap_tick must be"),
        ({"twap_window_seconds": 0}, "twap_window_seconds"),
        ({"base_fee_wei": None}, "base_fee_wei"),
        ({"finality": "final"}, "finality must be a Finality"),
    ],
)
def test_a_reading_refuses_a_field_that_cannot_be_right(changes, message):
    reading = _reading()
    with pytest.raises(ValueError, match=message):
        replace(reading, **changes)


# --- pool_bar_flags --------------------------------------------------------


def test_a_reading_that_agrees_with_its_twap_and_its_neighbour_has_no_flags():
    previous = _reading(time=FIRST_DAY - DAY)
    assert pool_bar_flags(_USDC_WETH, _reading(), previous, _SETTINGS) == frozenset()
    assert pool_bar_flags(_USDC_WETH, _reading(), None, _SETTINGS) == frozenset()


def test_a_close_further_from_the_twap_than_the_limit_is_flagged():
    # 1.0001 ** 487 is 4.99% and 1.0001 ** 489 is 5.01%.
    inside = _reading(twap_tick=_ETH_TICK - 487)
    outside = _reading(twap_tick=_ETH_TICK - 489)
    below = _reading(twap_tick=_ETH_TICK + 520)
    assert pool_bar_flags(_USDC_WETH, inside, None, _SETTINGS) == frozenset()
    assert pool_bar_flags(_USDC_WETH, outside, None, _SETTINGS) == {BarFlag.TWAP_DEVIATION}
    assert pool_bar_flags(_USDC_WETH, below, None, _SETTINGS) == {BarFlag.TWAP_DEVIATION}
    loose = replace(_SETTINGS, max_twap_deviation=Decimal("0.06"))
    assert pool_bar_flags(_USDC_WETH, outside, None, loose) == frozenset()


def test_a_reading_off_the_final_chain_is_flagged():
    reorged = _reading(finality=Finality.REORGED)
    assert pool_bar_flags(_USDC_WETH, reorged, None, _SETTINGS) == {BarFlag.REORGED}
    pending = _reading(finality=Finality.PENDING)
    assert pool_bar_flags(_USDC_WETH, pending, None, _SETTINGS) == frozenset()


def test_a_reading_whose_neighbour_is_not_one_interval_earlier_is_a_gap():
    two_days_before = _reading(time=FIRST_DAY - 2 * DAY, tick=_ETH_TICK + 9_000)
    # The move across the gap is not measured: it is the gap that is flagged.
    assert pool_bar_flags(_USDC_WETH, _reading(), two_days_before, _SETTINGS) == {BarFlag.GAP}


def test_a_move_beyond_the_limit_since_the_bar_before_is_flagged():
    # 1.0001 ** 4054 is a 49.99% rise and 1.0001 ** 4056 a 50.02% one.
    previous = _reading(time=FIRST_DAY - DAY)
    inside = _reading(tick=_ETH_TICK + 4_054)
    outside = _reading(tick=_ETH_TICK + 4_056)
    assert pool_bar_flags(_USDC_WETH, inside, previous, _SETTINGS) == frozenset()
    assert pool_bar_flags(_USDC_WETH, outside, previous, _SETTINGS) == {BarFlag.LARGE_MOVE}


@pytest.mark.parametrize(
    "previous",
    [
        _reading(time=FIRST_DAY),
        _reading(time=FIRST_DAY + DAY),
        _reading(time=FIRST_DAY - DAY, chain_id=5),
        _reading(time=FIRST_DAY - 3_600, interval_seconds=3_600),
    ],
)
def test_the_reading_before_must_be_earlier_in_the_same_series(previous):
    with pytest.raises(ValueError, match="does not come before"):
        pool_bar_flags(_USDC_WETH, _reading(), previous, _SETTINGS)


def test_the_checks_refuse_a_reading_taken_over_another_twap_window():
    with pytest.raises(ValueError, match="a TWAP over 600 seconds, and the settings ask for 1800"):
        pool_bar_flags(_USDC_WETH, _reading(twap_window_seconds=600), None, _SETTINGS)
    shorter = replace(_SETTINGS, twap_window_seconds=600)
    assert pool_bar_flags(_USDC_WETH, _reading(twap_window_seconds=600), None, shorter) == set()


def test_the_checks_refuse_a_reading_of_another_pool():
    with pytest.raises(ValueError, match="not of"):
        pool_bar_flags(_WBTC_WETH, _reading(), None, _SETTINGS)
    with pytest.raises(ValueError, match="not of"):
        pool_bar_flags(
            _USDC_WETH, _reading(), _reading(_WBTC_WETH, time=FIRST_DAY - DAY), _SETTINGS
        )


# --- assemble_bar ----------------------------------------------------------


def _both() -> tuple[PoolBar, PoolBar]:
    return _reading(), _reading(_WBTC_WETH, tick=_BTC_TICK)


def test_a_bar_prices_every_token_in_the_quote_token():
    eth_reading, btc_reading = _both()
    bar = assemble_bar(_USDC, (_USDC_WETH, _WBTC_WETH), (eth_reading, btc_reading), suspect=False)

    eth = price_from_sqrt_price_x96(_USDC_WETH, eth_reading.sqrt_price_x96, base=_WETH)
    btc_in_eth = price_from_sqrt_price_x96(_WBTC_WETH, btc_reading.sqrt_price_x96, base=_WBTC)
    assert dict(bar.prices) == {"WETH": eth, "WBTC": btc_in_eth * eth}
    assert Decimal(1_990) < eth < Decimal(2_010)
    assert Decimal(14) < btc_in_eth < Decimal(16)
    assert (bar.time, bar.close_block, bar.base_fee_wei) == (FIRST_DAY, 999, 7 * 10**9)
    assert bar.suspect is False


def test_the_order_of_the_pools_does_not_change_the_prices():
    eth_reading, btc_reading = _both()
    one = assemble_bar(_USDC, (_USDC_WETH, _WBTC_WETH), (eth_reading, btc_reading), suspect=False)
    other = assemble_bar(_USDC, (_WBTC_WETH, _USDC_WETH), (btc_reading, eth_reading), suspect=False)
    assert dict(one.prices) == dict(other.prices)


def test_a_bar_can_be_quoted_in_any_token_the_pools_reach():
    eth_reading, btc_reading = _both()
    bar = assemble_bar(_WETH, (_USDC_WETH, _WBTC_WETH), (eth_reading, btc_reading), suspect=False)
    assert dict(bar.prices) == {
        "USDC": price_from_sqrt_price_x96(_USDC_WETH, eth_reading.sqrt_price_x96, base=_USDC),
        "WBTC": price_from_sqrt_price_x96(_WBTC_WETH, btc_reading.sqrt_price_x96, base=_WBTC),
    }


def test_the_callers_verdict_is_carried_onto_the_bar():
    assert assemble_bar(_USDC, (_USDC_WETH, _WBTC_WETH), _both(), suspect=True).suspect is True


@pytest.mark.parametrize(
    "changes", [{"close_block": 998}, {"close_block_hash": "0x" + "ab" * 32}]
)
def test_readings_that_disagree_on_the_close_block_make_the_bar_suspect(changes):
    eth_reading, btc_reading = _both()
    bar = assemble_bar(
        _USDC, (_USDC_WETH, _WBTC_WETH), (eth_reading, replace(btc_reading, **changes)), suspect=False
    )
    assert bar.suspect is True
    assert bar.close_block == 999


def test_a_pool_the_quote_token_cannot_be_reached_from_is_refused():
    with pytest.raises(ValueError, match=r"no pool joins \['WBTC', 'WETH'\] to the quote token USDC"):
        assemble_bar(_USDC, (_WBTC_WETH,), (_reading(_WBTC_WETH, tick=_BTC_TICK),), suspect=False)


def test_a_bar_needs_one_reading_per_pool_all_at_one_boundary():
    eth_reading, btc_reading = _both()
    pools = (_USDC_WETH, _WBTC_WETH)
    with pytest.raises(ValueError, match="one reading per pool"):
        assemble_bar(_USDC, pools, (eth_reading,), suspect=False)
    with pytest.raises(ValueError, match="one reading per pool"):
        assemble_bar(_USDC, (), (), suspect=False)
    with pytest.raises(ValueError, match="not of"):
        assemble_bar(_USDC, pools, (btc_reading, eth_reading), suspect=False)
    later = _reading(_WBTC_WETH, tick=_BTC_TICK, time=FIRST_DAY + DAY)
    with pytest.raises(ValueError, match="not of one boundary"):
        assemble_bar(_USDC, pools, (eth_reading, later), suspect=False)
