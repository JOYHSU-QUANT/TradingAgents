"""The generator (plan PR 2's acceptance): one seed, one world; the venue's shape; the effect where the rule holds.

What is pinned, in the plan's words: the seed reproduces the world
bit-for-bit; a candle has the venue's shape (``close_time`` a millisecond
before the next open, OHLC consistent, each bar opening at the last
close); the funding grid has no hole and no repeat; the daily bars are
aggregated from the same path; the generator reads nothing ahead (a
shorter world is a prefix of a longer one, and funding is drawn apart
from the price); and in a conditional world the effect sits on exactly
the bars the research package's own replay says the planted rule holds —
from the bar after the decision, never on it.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from decimal import Decimal

import pytest

from contrib.autoresearch.constants import FUNDING_INTERVAL_MS, MS_PER_DAY, bar_span_ms
from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import Side, parse_spec
from contrib.autoresearch.evaluator import replay_sides
from contrib.autoresearch.features import FeatureFrame, SeriesBundle
from contrib.autoresearch.gaps import scan_bars, scan_stamps
from contrib.autoresearch.upstream import from_epoch_ms
from contrib.autoresearch.vocabulary import FeatureRef, parse_feature_name
from contrib.discovery.manifest import Bars, SeedSet, WorldKind
from contrib.discovery.worlds import (
    SETTLEMENT_LAG_MS,
    SyntheticHistory,
    WorldError,
    generate,
)

from .conftest import DAY_MS, PLANTED, START_MS, STEP_MS, example_manifest, small_manifest

PER_BAR = STEP_MS // FUNDING_INTERVAL_MS
PER_DAY = DAY_MS // STEP_MS


def null(**changes):
    """A small null world; the frame is never built for one, so this is instant."""
    return generate(small_manifest(**changes))


def _log_return(bar) -> float:
    return math.log(float(bar.close) / float(bar.open))


# -- the seed -------------------------------------------------------------------------


def test_the_same_seed_gives_the_same_world_and_another_seed_another():
    first, again = null(), null()
    assert (first.bars, first.daily, first.funding) == (again.bars, again.daily, again.funding)
    assert null(seed=1).bars != first.bars
    # Development seed 0 and test seed 0 are two worlds (two ids).
    assert null(seed_set=SeedSet.TEST).bars != first.bars


def test_a_shorter_world_of_the_same_seed_is_a_prefix_of_a_longer_one():
    # No bar's draw depends on a later bar's: the generator's own
    # no-look-ahead guarantee, read as a prefix.
    short, long = null(count=60), null(count=90)
    assert long.bars[:60] == short.bars
    assert long.daily[: 60 // PER_DAY] == short.daily
    assert long.funding[: 60 * PER_BAR + 1] == short.funding


def test_funding_is_drawn_apart_from_the_price():
    # The funding series does not read the price path: a different walk
    # leaves every settlement where it was.
    quiet, wild = null(), null(vol=0.03, drift=0.002)
    assert wild.bars != quiet.bars
    assert wild.funding == quiet.funding


def test_a_null_world_is_the_stated_martingale_and_carries_no_effect():
    # ``log P[t] = log P[t-1] + drift + vol·Z - vol²/2`` with ``Z`` from the
    # price stream of the world's id: the formula the README states, pinned
    # against the stream it is drawn from.
    made = null(vol=0.02, drift=0.0005)
    stream = random.Random(f"{made.world.id}/price")
    for bar in made.bars:
        z = stream.gauss(0.0, 1.0)
        assert _log_return(bar) == pytest.approx(0.0005 + 0.02 * z - 0.02 * 0.02 / 2, abs=1e-9)
    assert all(side is None for side in made.planted_sides)


# -- the shape ------------------------------------------------------------------------


def test_bars_have_the_venue_s_shape_on_the_world_s_grid():
    made = null(count=60)
    bars = made.bars
    assert len(bars) == 60
    for index, bar in enumerate(bars):
        assert bar.open_time == START_MS + index * STEP_MS
        assert bar.close_time == bar.open_time + bar_span_ms(STEP_MS)
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        assert bar.low > 0
        assert bar.volume == Decimal(1)
    assert bars[0].open == Decimal("30000.0")
    # One continuous path: each bar opens at the previous close, as the
    # same decimal, so there is no close-to-open gap for a rule to earn.
    assert all(later.open == earlier.close for earlier, later in zip(bars, bars[1:], strict=False))
    assert scan_bars("bars", STEP_MS, 0, bars).complete


def test_daily_bars_are_the_complete_days_of_the_same_path():
    made = null(count=10 * PER_DAY + 3)
    assert len(made.daily) == 10  # the trailing three bars are not a closed day
    for day, candle in enumerate(made.daily):
        own = made.bars[day * PER_DAY : (day + 1) * PER_DAY]
        assert candle.open_time == START_MS + day * MS_PER_DAY
        assert candle.close_time == candle.open_time + bar_span_ms(MS_PER_DAY)
        assert candle.open == own[0].open
        assert candle.close == own[-1].close
        assert candle.high == max(bar.high for bar in own)
        assert candle.low == min(bar.low for bar in own)
        assert candle.volume == sum(bar.volume for bar in own)
    assert scan_bars("daily", MS_PER_DAY, 0, made.daily).complete


def test_funding_is_hourly_from_the_first_open_through_the_settlement_due_at_the_last_close():
    made = null(count=60)
    expected = [
        START_MS + k * FUNDING_INTERVAL_MS + SETTLEMENT_LAG_MS for k in range(60 * PER_BAR + 1)
    ]
    assert [point.time for point in made.funding] == expected
    assert made.funding[-1].time == made.bars[-1].close_time + 1 + SETTLEMENT_LAG_MS
    assert made.clock_ms == made.funding[-1].time + 1
    assert all(point.premium is None for point in made.funding)
    # i.i.d. with the stated scale: the rates are the funding stream times sigma.
    stream = random.Random(f"{made.world.id}/funding")
    for point in made.funding:
        assert float(point.rate) == pytest.approx(1e-4 * stream.gauss(0.0, 1.0), abs=1e-12)
    assert scan_stamps("funding", FUNDING_INTERVAL_MS, 0, expected).complete


# -- the effect -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def null_twin(example_world):
    """The null world of the example world's seed: the same draws, nothing planted."""
    return generate(example_manifest(kind=WorldKind.NULL_V1))


def test_a_conditional_world_is_its_null_twin_plus_beta_on_the_bars_the_rule_holds(
    example_world, null_twin
):
    beta = example_world.world.planted.beta
    assert null_twin.funding == example_world.funding
    assert null_twin.world.id == example_world.world.id
    carried = [side for side in example_world.planted_sides if side is not None]
    assert carried and set(carried) == {Side.LONG}  # the example plants a long-only rule
    for bar, twin, side in zip(
        example_world.bars, null_twin.bars, example_world.planted_sides, strict=True
    ):
        assert bar.open_time == twin.open_time
        expected = 0.0 if side is None else (beta if side is Side.LONG else -beta)
        assert _log_return(bar) - _log_return(twin) == pytest.approx(expected, abs=1e-9)
    # Still one continuous path after the effect.
    bars = example_world.bars
    assert all(later.open == earlier.close for earlier, later in zip(bars, bars[1:], strict=False))


def test_the_effect_begins_the_bar_after_the_decision_and_never_on_it(example_world, null_twin):
    # The research package's own replay over the pre-effect path is the
    # definition: the side it holds after the decision at bar ``i`` is the
    # side bar ``i + 1`` carries.
    planted = example_world.world.planted
    frame = FeatureFrame(
        SeriesBundle(null_twin.bars, null_twin.daily, null_twin.funding),
        indicator_lookback=example_manifest().indicator_lookback,
    )
    replayed = replay_sides(planted.spec, frame, CostModel(), since_ms=null_twin.bars[0].open_time)
    assert example_world.planted_sides == (None, *replayed.sides[:-1])
    # And by hand, at the first bar that carries it: the rule's entry held
    # at the close BEFORE it, under its gate, and nothing was held before.
    first = next(i for i, side in enumerate(example_world.planted_sides) if side is not None)
    assert first >= 1 and example_world.planted_sides[first - 1] is None
    entry = frame.value_at(FeatureRef(*parse_feature_name("funding_zscore_14")), first - 1)
    gate = frame.value_at(FeatureRef(*parse_feature_name("realized_vol_20")), first - 1)
    assert entry is not None and entry < -1.0
    assert gate is not None and gate > 0.004
    # The bar the decision was taken at carries nothing: its return is the twin's.
    assert _log_return(example_world.bars[first - 1]) == pytest.approx(
        _log_return(null_twin.bars[first - 1]), abs=1e-12
    )


# -- refusals -------------------------------------------------------------------------


def _with_world(manifest, **changes):
    return replace(manifest, world=replace(manifest.world, **changes))


def test_the_generator_refuses_what_it_does_not_make():
    with pytest.raises(WorldError, match="generates version"):
        generate(_with_world(small_manifest(), generator_version="2"))
    daily = _with_world(small_manifest(), bars=Bars(interval="1d", count=60, warmup_bars=0))
    with pytest.raises(WorldError, match="lays the path on 4h bars"):
        generate(daily)
    # A conditional world too short for its own rule's window: the rule
    # would read as never firing, so it is refused rather than planted.
    with pytest.raises(WorldError, match="cannot be read on this world"):
        generate(small_manifest(WorldKind.CONDITIONAL_V1, count=30))


def test_a_rule_that_never_holds_a_position_plants_nothing_and_is_refused():
    # The rule can be read (its features have values) and its gate never
    # passes on this seed: a null world under the conditional kind.
    manifest = small_manifest(WorldKind.CONDITIONAL_V1, count=300)
    never = parse_spec(
        {**PLANTED, "filters": [{"left": "realized_vol_20", "op": ">", "right": 1.0}]}
    )
    idle = _with_world(manifest, planted=replace(manifest.world.planted, spec=never))
    with pytest.raises(WorldError, match="never holds a position on this seed"):
        generate(idle)


def test_a_history_holds_its_own_shape():
    made = null(count=60)
    with pytest.raises(WorldError, match="bars for a world of"):
        replace(made, bars=made.bars[:-1])
    with pytest.raises(WorldError, match="planted sides for"):
        replace(made, planted_sides=made.planted_sides[:-1])
    with pytest.raises(WorldError, match="carries no effect"):
        replace(made, planted_sides=(Side.LONG, *made.planted_sides[1:]))


# -- the port -------------------------------------------------------------------------


def test_the_port_serves_the_world_as_the_venue_reader_would():
    made = null(count=60)
    market = SyntheticHistory(made)
    coin = made.world.id
    clock = market.get_exchange_time(coin)
    assert clock == from_epoch_ms(made.clock_ms)
    # Closed as of ``end``, inclusive, the newest ``lookback`` of them.
    assert market.get_candles(coin, "4h", 10, end=clock) == list(made.bars[-10:])
    assert market.get_candles(coin, "4h", 1000, end=clock) == list(made.bars)
    assert market.get_candles(coin, "4h", 0, end=clock) == list(made.bars)
    at_close = from_epoch_ms(made.bars[5].close_time)
    assert market.get_candles(coin, "4h", 100, end=at_close) == list(made.bars[:6])
    just_before = from_epoch_ms(made.bars[5].close_time - 1)
    assert market.get_candles(coin, "4h", 100, end=just_before) == list(made.bars[:5])
    assert market.get_candles(coin, "1d", 100, end=clock) == list(made.daily)
    # Settlements inside the trailing window, both edges inclusive.
    day = market.get_funding_history(coin, 1, end=clock)
    assert day == [p for p in made.funding if made.clock_ms - MS_PER_DAY <= p.time <= made.clock_ms]
    assert len(day) == 24
    assert market.get_funding_history(coin, 400, end=clock) == list(made.funding)


def test_the_port_answers_for_its_own_coin_only():
    market = SyntheticHistory(null(count=6))
    clock = market.get_exchange_time(market.history.world.id)
    for ask in (
        lambda: market.get_exchange_time("BTC"),
        lambda: market.get_candles("BTC", "4h", 10, end=clock),
        lambda: market.get_funding_history("BTC", 1, end=clock),
    ):
        with pytest.raises(WorldError, match="no history for 'BTC'"):
            ask()
