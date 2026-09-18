"""What the evaluator does with a spec, bar by bar, and what it refuses to do.

Three groups. The ARITHMETIC: one long trade computed by hand, every cost
term of it, on a series whose opens differ from its closes so "filled at the
next open" is a number and not a sentence. The SEMANTICS plan §10.1 left to
this PR: an empty exit holds, an opposite entry reverses, a filter gates
entries only, a missing feature does not fire and is counted. And the
REFUSALS: a hole in the window, a feature still warming up at its first bar,
a funding series that does not cover it.

The leakage property (plan §3.6b) is tested the way ``test_features.py``
tests it for features — evaluate on the whole history, evaluate on the
history cut off at ``t``, demand the same decisions — plus the narrower
fact that the rule a cheat would need cannot be constructed at all.
"""

from __future__ import annotations

import dataclasses
import math
from decimal import Decimal

import pytest

from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import Condition, Op, Side, SpecError, StrategySpec, parse_spec
from contrib.autoresearch.evaluator import (
    MS_PER_YEAR,
    UNLABELLED,
    EvaluationError,
    ExitReason,
    SegmentResult,
    describe_result,
    evaluate_segment,
    evaluate_split,
    load_bundle,
    replay_position,
)
from contrib.autoresearch.features import (
    LIVE_CANDLE_LOOKBACK,
    MIN_INDICATOR_LOOKBACK,
    FeatureError,
    FeatureFrame,
    SeriesBundle,
)
from contrib.autoresearch.split import Segment, SegmentName, Split
from contrib.autoresearch.upstream import FundingPoint, MarketRegime, interval_to_ms
from contrib.autoresearch.vocabulary import FeatureKind, FeatureRef

from .conftest import ANCHOR_MS, MS_PER_HOUR, candles

_STEP = 4 * MS_PER_HOUR
_DAY = 24 * MS_PER_HOUR

# Round numbers, so every expectation below is arithmetic a reader can redo.
_COSTS = CostModel(taker_fee_rate=0.001, slippage_bps=10, leverage=1)
_FREE = CostModel(taker_fee_rate=0, slippage_bps=0, leverage=1)


# How late the venue posts a settlement after its hour, in the shape this suite
# stamps them: inside the 2-99 ms measured on mainnet (see ``constants``).
_JITTER_MS = 57


def _funding(bars: int, *, rate: float = 0.0001, hours: int | None = None) -> list[FundingPoint]:
    """Hourly settlements from an hour after the anchor, covering ``bars`` 4h bars exactly.

    Stamped the way the venue stamps them: ``_JITTER_MS`` after the hour. Starts
    an hour in because the span a measurement checks for coverage runs from
    just past the first open to just past the last close - the settlement due
    ON the first open is the bar before's, and the one due at the last close
    posts after it and is the span's last. From there the four a bar pays sit
    just inside its ``(open, close]``: its own open hour's (every bar after the
    first), then one, two and three hours later; the one due at its close
    posts into the next bar. A stamp exactly ON an hour is a shape the venue
    has never produced, and one the charging rule leaves in the crack between
    two venue bars (see ``_Settlements``), so no fixture here stamps one.
    """
    count = bars * 4 if hours is None else hours
    return [
        FundingPoint(time=ANCHOR_MS + (i + 1) * MS_PER_HOUR + _JITTER_MS, rate=Decimal(str(rate)))
        for i in range(count)
    ]


def _bundle(closes, *, rate: float = 0.0001, funding=None, daily=(), **kwargs) -> SeriesBundle:
    bars = candles(closes, **kwargs)
    if funding is None:
        funding = _funding(len(closes), rate=rate)
    return SeriesBundle(bars, daily=daily, funding=funding)


def _segment(first: int, stop: int, *, step: int = _STEP) -> Segment:
    return Segment(SegmentName.TRAIN, ANCHOR_MS + first * step, ANCHOR_MS + stop * step)


def _spec(**overrides) -> StrategySpec:
    body = {
        "family": "breakout",
        "entry": {"long": [{"left": "close", "op": ">", "right": 100}]},
        "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
    }
    body.update(overrides)
    return parse_spec(body)


def _run(
    spec, bundle, first=0, stop=None, costs=_COSTS, interval="4h", lookback=LIVE_CANDLE_LOOKBACK
) -> SegmentResult:
    stop = len(bundle.bars) if stop is None else stop
    step = interval_to_ms(interval)
    frame = FeatureFrame(bundle, indicator_lookback=lookback)
    return evaluate_segment(spec, frame, _segment(first, stop, step=step), costs, interval=interval)


def _equity_before(result: SegmentResult, index: int) -> float:
    return math.prod(1 + r for r in result.net_bar_returns[:index])


# -- the arithmetic, by hand -----------------------------------------------


def test_one_long_trade_is_filled_at_the_next_open_and_costed_term_by_term():
    """Decided at the close of bar 1, filled at the open of bar 2; the exit rule
    fires at the close of bar 4 and fills at the open of bar 5. Every number
    below is the fill model (plan §3.6) and the cost model (§3.7) written out."""
    closes = [90, 110, 120, 130, 95, 100]
    opens = [90, 105, 118, 125, 100, 98]
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": 100}]})
    result = _run(spec, _bundle(closes, opens=opens))

    size = 0.5 / 118  # notional 0.5 of equity 1.0, at the open of bar 2
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert (trade.entry_index, trade.exit_index) == (2, 5)
    assert (trade.entry_price, trade.exit_price) == (118.0, 98.0)
    assert trade.exit_reason is ExitReason.EXIT_RULE
    assert trade.size == pytest.approx(size)
    assert trade.gross_pnl == pytest.approx(size * (98 - 118))
    # Fee and slippage on the mid notional of both fills, 0.1% each.
    assert trade.fees == pytest.approx(0.001 * (0.5 + size * 98))
    assert trade.slippage == pytest.approx(trade.fees)
    # Four settlements at 0.0001 per bar held (bars 2, 3, 4), on the notional
    # at each bar's close; a long at a positive rate PAYS.
    assert trade.funding == pytest.approx(4 * 0.0001 * size * (120 + 130 + 95))
    assert trade.net_pnl == pytest.approx(
        trade.gross_pnl - trade.fees - trade.slippage - trade.funding
    )
    assert trade.bars_held == 3

    assert result.bars == 6
    assert result.exposure == pytest.approx(3 / 6)
    assert result.net.total_return == pytest.approx(trade.net_pnl)
    assert result.net.hit_rate == 0.0
    assert result.fees_paid == pytest.approx(trade.fees)
    assert result.funding_paid == pytest.approx(trade.funding)
    # The bar returns say WHEN: nothing until the fill, the whole move after.
    assert result.gross_bar_returns[:2] == (0.0, 0.0)
    assert result.gross_bar_returns[2] == pytest.approx(size * (120 - 118))
    assert result.gross_bar_returns[5] == pytest.approx(
        size * (98 - 95) / _equity_before(result, 5)
    )
    assert sum(1 for r in result.gross_bar_returns if r != 0) == 4


def test_a_settlement_posted_just_after_a_close_belongs_to_the_next_bar():
    """The venue stamps a settlement a few ms after the hour. A position filled
    at that hour's open exists when it posts; one flattened at that hour's
    close does not — and it is the rule the feature module reads by."""
    closes = [110, 120, 130]
    result = _run(_spec(), _bundle(closes, rate=0.001), costs=_FREE)
    trade = result.trades[0]
    # Filled at bar 1's open: the settlement due at bar 0's close posts 58 ms
    # after it (a millisecond past the close is bar 1's open, then the jitter)
    # and is paid; so is the one due at bar 1's close. The one due at bar 2's
    # close, where the position was flattened, posts after that close and is
    # not. Four settlements a bar — and none reported missing, since the
    # span's last one exists, it just posts after the close.
    size = 0.5 / 120
    assert trade.funding == pytest.approx(0.001 * size * (4 * 120 + 4 * 130))
    assert result.funding_settlements_missing == 0


def test_a_short_at_a_positive_funding_rate_receives():
    closes = [110, 90, 80, 85]
    spec = _spec(entry={"short": [{"left": "close", "op": "<", "right": 100}]})
    result = _run(spec, _bundle(closes))
    trade = result.trades[0]
    assert trade.side.value == "short"
    assert trade.funding < 0
    assert result.funding_paid == pytest.approx(trade.funding)
    # Filled at bar 2's open (= its close here), held through bars 2 and 3,
    # flattened at bar 3's close.
    size = 0.5 / 80
    assert trade.funding == pytest.approx(-4 * 0.0001 * size * (80 + 85))
    assert trade.gross_pnl == pytest.approx(-size * (85 - 80))


def test_gross_is_the_price_move_and_net_is_what_is_left_after_every_cost():
    """A rule that trades every bar on a flat market: gross exactly zero, net exactly the costs."""
    closes = [100.0] * 8
    spec = _spec(entry={"long": [{"left": "close", "op": ">", "right": 50}]}, exit={"max_bars": 1})
    result = _run(spec, _bundle(closes, rate=0.0))
    assert result.gross.total_return == 0.0
    assert result.net.total_return < 0
    assert result.net.total_return == pytest.approx(-(result.fees_paid + result.slippage_paid))
    assert all(trade.bars_held == 1 for trade in result.trades)
    assert result.turnover > 1


def test_equity_compounds_so_the_second_trade_is_sized_off_what_the_first_left():
    closes = [90, 110, 120, 90, 110, 120]
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": 100}]})
    result = _run(spec, _bundle(closes, rate=0.0), costs=_FREE)
    first, second = result.trades
    assert first.notional == pytest.approx(0.5)
    assert first.net_pnl < 0
    assert second.notional == pytest.approx(0.5 * (1 + first.net_pnl))


# -- the semantics plan §10.1 left to the evaluator ----------------------------


def test_an_empty_exit_holds_until_the_window_ends():
    closes = [110, 120, 130, 140, 150]
    result = _run(_spec(), _bundle(closes))
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.SEGMENT_END
    assert (trade.entry_index, trade.exit_index) == (1, 4)
    assert trade.bars_held == 4
    assert trade.exit_price == 150.0
    # The flatten is a fill: it pays.
    assert trade.fees == pytest.approx(0.001 * (0.5 + trade.size * 150))


def test_an_opposite_entry_reverses_the_position():
    closes = [110, 90, 110, 90, 110]
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 100}],
            "short": [{"left": "close", "op": "<", "right": 100}],
        }
    )
    result = _run(spec, _bundle(closes))
    sides = [trade.side.value for trade in result.trades]
    reasons = [trade.exit_reason for trade in result.trades]
    assert sides == ["long", "short", "long", "short"]
    assert reasons == [ExitReason.REVERSAL] * 3 + [ExitReason.SEGMENT_END]
    # Each reversal closes and opens at ONE open: the exit of one trade is
    # the entry bar of the next.
    for earlier, later in zip(result.trades, result.trades[1:], strict=False):
        assert earlier.exit_index == later.entry_index
    assert result.exposure == pytest.approx(4 / 5)


def test_an_exit_rule_that_fires_with_the_opposite_entry_is_the_reason_recorded():
    closes = [110, 90, 90]
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 100}],
            "short": [{"left": "close", "op": "<", "right": 100}],
        },
        exit={"long": [{"left": "close", "op": "<", "right": 95}]},
    )
    result = _run(spec, _bundle(closes))
    assert [t.exit_reason for t in result.trades] == [
        ExitReason.EXIT_RULE,
        ExitReason.SEGMENT_END,
    ]
    assert result.trades[1].side.value == "short"


def test_max_bars_closes_at_the_open_after_that_many_closes():
    closes = [110, 120, 130, 140, 150, 160]
    result = _run(_spec(exit={"max_bars": 2}), _bundle(closes))
    trade = result.trades[0]
    assert trade.exit_reason is ExitReason.MAX_BARS
    assert (trade.entry_index, trade.exit_index) == (1, 3)
    assert trade.bars_held == 2
    # ... and it re-enters the bar after, since the entry still holds.
    assert result.trades[1].entry_index == 4


def test_a_filter_gates_entries_and_does_not_close_a_position():
    """``ret_1 > 0`` blocks the entry at bar 1, admits it at bar 2, and turning
    false afterwards leaves the position where it is."""
    closes = [105, 104, 110, 109, 108, 107]
    spec = _spec(filters=[{"left": "ret_1", "op": ">", "right": 0}])
    result = _run(spec, _bundle(closes), first=1)
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_index == 3
    assert trade.exit_reason is ExitReason.SEGMENT_END


def test_a_filter_gates_the_reversal_too():
    closes = [105, 110, 90, 80, 70]
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 100}],
            "short": [{"left": "close", "op": "<", "right": 100}],
        },
        filters=[{"left": "ret_1", "op": ">", "right": 0}],
    )
    result = _run(spec, _bundle(closes), first=1)
    # Long enters at bar 1 (ret up, close 110); every later bar is down, so
    # the short entry fires but the filter refuses it — no reversal.
    assert [t.side.value for t in result.trades] == ["long"]
    assert result.trades[0].exit_reason is ExitReason.SEGMENT_END


def test_a_rule_whose_feature_has_no_value_does_not_fire_and_is_counted():
    """The daily backdrop goes stale after a day; the exit reading it goes silent.

    Silent means HOLD, not flatten, and the bars are counted: an exit rule
    fail-closing on the ``None`` an entry rule fails open on would be two
    readings of one value.
    """
    closes = [110.0] * 10
    daily = candles([50.0], start_ms=ANCHOR_MS + _STEP - _DAY, step_ms=_DAY)
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": "close_1d"}]})
    result = _run(spec, _bundle(closes, daily=daily))
    assert len(result.trades) == 1
    assert result.trades[0].exit_reason is ExitReason.SEGMENT_END
    # Bars 6, 7, 8 decide with ``close_1d`` stale (bar 9 is the last and does
    # not decide); bars 1..5 read the daily close of 50 and 110 < 50 is false.
    assert result.bars_unevaluable == 3
    assert "3 bars where a consulted rule had no value" in "\n".join(result.describe())


def test_every_clause_is_consulted_so_the_silent_count_does_not_depend_on_order():
    """An AND that stopped at the first false clause would count a silent second
    clause only on the bars where the first happened to hold."""
    daily = candles([50.0], start_ms=ANCHOR_MS + _STEP - _DAY, step_ms=_DAY)
    closes = [90.0] * 10  # the first clause is false at every bar
    spec = _spec(
        entry={
            "long": [
                {"left": "close", "op": ">", "right": 100},
                {"left": "close_1d", "op": ">", "right": 10},
            ]
        }
    )
    result = _run(spec, _bundle(closes, daily=daily))
    assert result.bars_unevaluable == 3  # bars 6, 7, 8: close_1d stale, and counted anyway


def test_a_vol_targeted_rule_needs_its_vol_at_the_first_bar_too():
    closes = [110.0] * 12
    spec = _spec(
        family="vol_targeting",
        sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 10},
    )
    with pytest.raises(
        EvaluationError, match="realized_vol_10 has no value at the first bar .*10 bars in"
    ):
        _run(spec, _bundle(closes))


def test_a_vol_targeted_rule_sizes_by_the_realized_vol_it_reads():
    swinging = [100.0]
    for index in range(12):
        swinging.append(swinging[-1] * (1.1 if index % 2 == 0 else 0.9))
    spec = _spec(
        family="vol_targeting",
        entry={"long": [{"left": "close", "op": ">", "right": 1}]},
        sizing={"mode": "vol_target", "target_vol": 0.05, "lookback": 10, "max_fraction": 0.6},
    )
    frame = FeatureFrame(_bundle(swinging, rate=0.0))
    realized = frame.value_at(FeatureRef(FeatureKind.REALIZED_VOL, 10), 10)
    result = evaluate_segment(spec, frame, _segment(10, 13), _FREE, interval="4h")
    assert 0.05 / realized < 0.6
    assert result.trades[0].notional == pytest.approx(0.05 / realized)


def test_a_vol_target_is_capped_at_the_margin_cap_times_leverage():
    steady = [100.0 * 1.001**i for i in range(13)]  # tiny (not zero) vol: the target wants more
    spec = _spec(
        family="vol_targeting",
        entry={"long": [{"left": "close", "op": ">", "right": 1}]},
        sizing={"mode": "vol_target", "target_vol": 0.05, "lookback": 10, "max_fraction": 0.5},
    )
    costs = CostModel(leverage=3, slippage_bps=0, taker_fee_rate=0)
    result = _run(spec, _bundle(steady, rate=0.0), first=10, costs=costs)
    assert result.trades[0].notional == pytest.approx(0.5 * 3)


def test_long_and_short_firing_together_enter_nothing_and_are_counted():
    closes = [100.0] * 5
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 50}],
            "short": [{"left": "close", "op": "<", "right": 150}],
        }
    )
    result = _run(spec, _bundle(closes))
    assert result.trades == ()
    assert result.bars_conflicting == 4
    assert "4 bars where long and short both fired" in "\n".join(result.describe())


def test_the_counts_are_taken_on_bars_a_filter_blocked_too():
    """The counts are properties of the signal, not of the gate: a filter that
    is false does not stop the entries being read, so a conflict under it is
    still a conflict and an unreadable entry under it is still counted — else
    moving a clause from ``entry.long`` to ``filters`` would change the counts
    of a long-only rule it did not change."""
    never = [{"left": "close", "op": ">", "right": 500}]
    both = {
        "long": [{"left": "close", "op": ">", "right": 50}],
        "short": [{"left": "close", "op": "<", "right": 150}],
    }
    blocked = _run(_spec(entry=both, filters=never), _bundle([100.0] * 5))
    assert blocked.trades == () and blocked.bars_conflicting == 4
    # The daily backdrop goes stale after bar 5, as in
    # test_a_rule_whose_feature_has_no_value_does_not_fire_and_is_counted: the
    # entry reading it is silent at bars 6, 7, 8 — under a filter too.
    daily = candles([50.0], start_ms=ANCHOR_MS + _STEP - _DAY, step_ms=_DAY)
    stale = _spec(entry={"long": [{"left": "close_1d", "op": ">", "right": 10}]}, filters=never)
    silent = _run(stale, _bundle([110.0] * 10, daily=daily))
    assert silent.trades == () and silent.bars_unevaluable == 3


def test_both_entries_firing_while_held_is_a_conflict_too_not_a_reversal():
    """The same signal state means the same thing flat or held."""
    closes = [110, 100, 100, 100]
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 50}],
            "short": [{"left": "close", "op": "<", "right": 105}],
        }
    )
    result = _run(spec, _bundle(closes))
    # Bar 0: only long fires (110 is not < 105) -> long. Bars 1, 2: both fire ->
    # no reversal, counted; the long is held to the window's end.
    assert [t.side.value for t in result.trades] == ["long"]
    assert result.trades[0].exit_reason is ExitReason.SEGMENT_END
    assert result.bars_conflicting == 2


def test_a_same_side_entry_while_held_does_not_pyramid():
    closes = [110, 120, 130, 140]
    result = _run(_spec(), _bundle(closes))
    assert len(result.trades) == 1
    assert result.trades[0].notional == pytest.approx(0.5)


# -- the statistics ------------------------------------------------------------


def test_an_always_flat_strategy_scores_zeros_and_never_nan():
    closes = [100, 101, 102, 103]
    spec = _spec(entry={"long": [{"left": "close", "op": ">", "right": 1e9}]})
    result = _run(spec, _bundle(closes))
    for tally in (result.gross, result.net):
        assert dataclasses.astuple(tally) == (0.0, 0.0, 0.0, 0.0)
    assert (result.exposure, result.turnover, result.fees_paid, result.funding_paid) == (
        0,
        0,
        0,
        0,
    )
    assert result.trades == ()
    numbers = [value for value in dataclasses.astuple(result) if isinstance(value, float)]
    assert numbers and all(math.isfinite(value) for value in numbers)


def test_sharpe_is_the_bar_mean_over_the_bar_deviation_annualised_by_the_interval():
    closes = [90, 110, 120, 130, 95, 100, 120, 130]
    opens = [90, 105, 118, 125, 100, 98, 121, 128]
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": 100}]})
    result = _run(spec, _bundle(closes, opens=opens))
    returns = result.net_bar_returns
    mean = sum(returns) / len(returns)
    deviation = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    assert result.bars_per_year == MS_PER_YEAR / _STEP == 2190
    assert result.net.sharpe == pytest.approx(mean / deviation * math.sqrt(2190))
    daily_funding = _funding(len(closes), hours=len(closes) * 24)
    daily = _run(
        spec,
        _bundle(closes, opens=opens, step_ms=_DAY, funding=daily_funding),
        interval="1d",
    )
    assert daily.bars_per_year == 365


def test_max_drawdown_is_peak_to_trough_on_the_compounded_path():
    closes = [90, 110, 120, 130, 95, 100]
    opens = [90, 105, 118, 125, 100, 98]
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": 100}]})
    result = _run(spec, _bundle(closes, opens=opens), costs=_FREE)
    path, peak, worst = [1.0], 1.0, 0.0
    for r in result.gross_bar_returns:
        path.append(path[-1] * (1 + r))
        peak = max(peak, path[-1])
        worst = max(worst, (peak - path[-1]) / peak)
    assert result.gross.max_drawdown == pytest.approx(worst)
    assert worst > 0


def test_hit_rate_counts_trades_that_made_money_net_and_gross_separately():
    """A trade that made a hair gross and lost it to costs: gross hit, net miss."""
    closes = [90, 110, 110, 100, 100]
    opens = [90, 110, 110, 110, 110.02]
    highs = [max(o, c) + 1 for o, c in zip(opens, closes, strict=True)]
    spec = _spec(exit={"long": [{"left": "close", "op": "<", "right": 105}]})
    result = _run(spec, _bundle(closes, opens=opens, highs=highs))
    trade = result.trades[0]
    assert (trade.entry_price, trade.exit_price) == (110.0, 110.02)
    assert trade.gross_pnl > 0 > trade.net_pnl
    assert (result.gross.hit_rate, result.net.hit_rate) == (1.0, 0.0)


def test_turnover_is_notional_filled_over_mean_equity():
    closes = [110, 120, 130]
    result = _run(_spec(), _bundle(closes), costs=_FREE)
    trade = result.trades[0]
    filled = trade.notional + trade.size * 130
    equity_path = []
    equity = 1.0
    for r in result.net_bar_returns:
        equity *= 1 + r
        equity_path.append(equity)
    assert result.turnover == pytest.approx(filled / (sum(equity_path) / len(equity_path)))


def test_regime_buckets_partition_the_window_s_bars_and_its_net_return():
    closes = [
        30000 + 100 * i + (300 if i % 3 == 0 else -200 if i % 3 == 1 else 0) for i in range(70)
    ]
    result = _run(_spec(), _bundle(closes), first=52, lookback=MIN_INDICATOR_LOOKBACK)
    labels = {bucket.label for bucket in result.regime_buckets}
    assert labels and labels <= {regime.value for regime in MarketRegime}
    assert sum(bucket.bars for bucket in result.regime_buckets) == result.bars
    assert sum(bucket.net_return for bucket in result.regime_buckets) == pytest.approx(
        sum(result.net_bar_returns)
    )


def test_a_bar_s_return_is_filed_under_the_regime_known_when_its_position_was_chosen():
    """The label at a bar's own close is computed over that bar's move; the one
    the position was chosen under is the close before."""
    closes = [30000 + 60 * i for i in range(60)] + [30000 - 900 * i for i in range(1, 21)]
    frame = FeatureFrame(_bundle(closes), indicator_lookback=MIN_INDICATOR_LOOKBACK)
    result = evaluate_segment(_spec(), frame, _segment(52, 80), _COSTS, interval="4h")
    labels = frame.series(FeatureRef(FeatureKind.REGIME))
    assert any(labels[i] is not labels[i - 1] for i in range(52, 80))  # the fixture flips
    expected: dict[str, float] = {}
    for index, net in zip(range(52, 80), result.net_bar_returns, strict=True):
        key = labels[index - 1].value
        expected[key] = expected.get(key, 0.0) + net
    assert {b.label: b.net_return for b in result.regime_buckets} == pytest.approx(expected)


def test_a_bundle_too_short_for_the_regime_reports_its_bars_unlabelled():
    closes = [110, 120, 130]
    assert len(closes) < MIN_INDICATOR_LOOKBACK
    result = _run(_spec(), _bundle(closes))
    assert [(b.label, b.bars) for b in result.regime_buckets] == [(UNLABELLED, 3)]
    # Past the classifier's own warm-up but short of the frame's window: still
    # unlabelled, and not the frame's refusal of a column with no value anywhere.
    closes = [30000 + 100 * i for i in range(MIN_INDICATOR_LOOKBACK + 10)]
    result = _run(_spec(), _bundle(closes))
    assert [(b.label, b.bars) for b in result.regime_buckets] == [(UNLABELLED, len(closes))]


def test_the_first_labelled_close_files_only_the_bar_after_it():
    closes = [30000 + 100 * i for i in range(MIN_INDICATOR_LOOKBACK + 1)]
    result = _run(_spec(), _bundle(closes), lookback=MIN_INDICATOR_LOOKBACK)
    labelled = [b for b in result.regime_buckets if b.label != UNLABELLED]
    assert labelled and sum(b.bars for b in labelled) == 1
    assert sum(b.bars for b in result.regime_buckets) == result.bars


def test_a_span_a_millisecond_short_of_its_hours_still_expects_every_settlement():
    """A venue span of N bars is N × 4 hours less a millisecond; rounded, it
    expects N × 4 settlements, and one deleted is one missing. Floored it would
    expect one fewer and read the deletion as complete."""
    points = _funding(5)
    del points[7]
    result = _run(_spec(), SeriesBundle(candles([110.0] * 5), funding=points))
    assert result.funding_settlements_missing == 1


def test_a_run_that_loses_everything_is_marked_ruined_and_stops():
    closes = [100, 90, 80, 70, 60]
    opens = [100, 100, 90, 80, 70]
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": 50}]},
        sizing={"mode": "fixed_margin_fraction", "fraction": 1.0},
    )
    result = _run(spec, _bundle(closes, opens=opens), costs=CostModel(leverage=20))
    assert result.ruined
    assert result.trades[-1].exit_reason is ExitReason.RUIN
    # Filled at bar 1's open and ruined at its close: held through one close.
    assert result.trades[-1].bars_held == 1
    assert result.exposure == pytest.approx(1 / 5)
    assert len(result.net_bar_returns) == result.bars
    assert result.net_bar_returns[-1] == 0.0
    assert result.net.total_return <= -1
    assert "RUINED" in result.describe()[0]


def test_a_ruin_realised_by_the_exit_fill_is_a_ruin_too():
    """The wipe-out lands on the fill at bar 2's open — a pending close, so
    nothing is held when the bar is booked. It is still the account reaching
    zero: the run stops, and no entry is sized off negative equity."""
    closes = [110, 110, 50, 150, 150]
    opens = [110, 112, 20, 140, 150]
    spec = _spec(
        exit={"max_bars": 1},
        sizing={"mode": "fixed_margin_fraction", "fraction": 1.0},
    )
    # Bar 3 fires the entry again; sized off the negative equity the old
    # reading left, it would be a "long" of negative size at bar 4's open.
    result = _run(
        spec,
        _bundle(closes, opens=opens),
        costs=CostModel(leverage=10, taker_fee_rate=0.001, slippage_bps=0),
    )
    assert result.ruined
    assert [t.exit_reason for t in result.trades] == [ExitReason.MAX_BARS]
    assert result.net_bar_returns[2] < -1
    assert result.net_bar_returns[3:] == (0.0, 0.0)
    assert len(result.trades) == 1


def test_a_ruin_on_the_window_s_last_bar_reads_ruin_not_segment_end():
    closes = [110, 110, 20]
    result = _run(
        _spec(sizing={"mode": "fixed_margin_fraction", "fraction": 1.0}),
        _bundle(closes),
        costs=CostModel(leverage=20),
    )
    assert result.ruined
    assert result.trades[-1].exit_reason is ExitReason.RUIN
    assert "RUINED" in result.describe()[0]


def test_the_flatten_s_own_fee_can_be_what_ruins_the_run():
    """Flat market, absurd fee: the position is solvent at the last close and
    the forced flatten's fee is what empties the account. The trade closed
    because the window ended; the run is still ruined."""
    closes = [110, 110, 110]
    result = _run(
        _spec(sizing={"mode": "fixed_margin_fraction", "fraction": 1.0}),
        _bundle(closes, rate=0.0),
        costs=CostModel(leverage=20, taker_fee_rate=0.03, slippage_bps=0),
    )
    # Notional 20 at fee 3%: 0.6 to open, 0.6 to flatten — 1.2 of equity 1.0.
    assert result.trades[-1].exit_reason is ExitReason.SEGMENT_END
    assert result.ruined
    assert result.net.total_return == pytest.approx(-1.2)


def test_a_trade_carries_its_invariants():
    from contrib.autoresearch.dsl import Side
    from contrib.autoresearch.evaluator import Trade

    body = {
        "entry_index": 1,
        "exit_index": 3,
        "entry_price": 100.0,
        "exit_price": 110.0,
        "size": 1.0,
        "notional": 100.0,
        "fees": 0.1,
        "slippage": 0.1,
        "funding": 0.0,
    }
    trade = Trade(side="long", exit_reason="exit_rule", **body)
    assert trade.side is Side.LONG and trade.exit_reason is ExitReason.EXIT_RULE
    assert trade.gross_pnl == pytest.approx(10.0)
    with pytest.raises(ValueError, match="before it entered"):
        Trade(side="long", exit_reason="exit_rule", **{**body, "exit_index": 0})
    with pytest.raises(ValueError, match="Trade.size must be a number > 0"):
        Trade(side="long", exit_reason="exit_rule", **{**body, "size": 0.0})
    with pytest.raises(ValueError, match="unsupported side 'up'"):
        Trade(side="up", exit_reason="exit_rule", **body)
    with pytest.raises(ValueError, match="Trade.entry_price: expected a finite number"):
        Trade(side="long", exit_reason="exit_rule", **{**body, "entry_price": math.inf})
    # Funding is signed — a short receives — but it has to be a number.
    assert Trade(side="short", exit_reason="exit_rule", **{**body, "funding": -0.5}).funding == -0.5
    for funding in (math.nan, True, "0.1", 10**400):
        with pytest.raises(ValueError, match="Trade.funding must be a finite number"):
            Trade(side="long", exit_reason="exit_rule", **{**body, "funding": funding})
    bad_indices = (("entry_index", 1.5), ("entry_index", True), ("entry_index", -1), ("exit_index", 3.5))
    for name, index in bad_indices:
        with pytest.raises(ValueError, match=f"Trade.{name} must be a bar index"):
            Trade(side="long", exit_reason="exit_rule", **{**body, name: index})


# -- the refusals ------------------------------------------------------------


def test_a_window_with_a_hole_in_it_is_refused_by_name():
    bars = candles([100, 110, 120, 130, 140])
    del bars[2]
    bundle = SeriesBundle(bars, funding=_funding(5))
    with pytest.raises(EvaluationError, match="has a hole: .* 1 bars missing"):
        evaluate_segment(_spec(), FeatureFrame(bundle), _segment(0, 5), _COSTS, interval="4h")


def test_a_window_the_store_only_partly_covers_is_refused_rather_than_shrunk():
    """The window is the measured span: a history that begins inside it, or ends
    before it, would be scored over fewer bars than the report names."""
    bundle = _bundle([110, 120, 130, 140])
    with pytest.raises(EvaluationError, match=r"stored history begins at .* fewer bars"):
        evaluate_segment(_spec(), FeatureFrame(bundle), _segment(-2, 4), _COSTS, interval="4h")
    with pytest.raises(EvaluationError, match=r"stored history ends at .* fewer bars"):
        evaluate_segment(_spec(), FeatureFrame(bundle), _segment(0, 6), _COSTS, interval="4h")


def test_a_window_off_its_grid_is_refused_as_the_scanner_would_report_it():
    """One definition of a hole: a stamp the gap scan calls off-grid is refused here too."""
    bars = candles([100, 110, 120, 130])
    nudged = candles([125], start_ms=bars[2].open_time + 60_000)[0]
    bundle = SeriesBundle([bars[0], bars[1], nudged, bars[3]], funding=_funding(4))
    with pytest.raises(EvaluationError, match="not on the 14400000 ms grid .*1 off-grid"):
        evaluate_segment(_spec(), FeatureFrame(bundle), _segment(0, 4), _COSTS, interval="4h")


def test_a_window_whose_edge_is_off_the_store_s_grid_is_refused_by_that_name():
    """``by_shares`` snaps its cuts; a hand-built or ledger-read segment need not
    be snapped, and the refusal has to blame the edge, not the store."""
    bundle = _bundle([110, 120, 130, 140])
    segment = Segment(SegmentName.TRAIN, ANCHOR_MS, ANCHOR_MS + 2 * _STEP + _STEP // 2)
    with pytest.raises(EvaluationError, match=r"its end .* not on the store's 14400000 ms grid"):
        evaluate_segment(_spec(), FeatureFrame(bundle), segment, _COSTS, interval="4h")


def test_a_window_of_one_bar_is_refused():
    with pytest.raises(EvaluationError, match="at least two"):
        _run(_spec(), _bundle([100, 110]), first=1)


def test_a_feature_still_warming_up_at_the_first_bar_is_refused_naming_when_it_is_ready():
    closes = [100, 110, 120, 130, 140, 150]
    spec = _spec(entry={"long": [{"left": "ret_3", "op": ">", "right": 0}]})
    with pytest.raises(EvaluationError, match=r"ret_3 has no value at the first bar .*3 bars in"):
        _run(spec, _bundle(closes))
    assert _run(spec, _bundle(closes), first=3).trades


def test_an_offset_counts_towards_the_warm_up():
    closes = [100, 110, 120, 130]
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": {"feature": "close", "offset": 2}}]}
    )
    with pytest.raises(EvaluationError, match=r"close\[2\] has no value at the first bar"):
        _run(spec, _bundle(closes))


def test_a_bar_whose_close_disagrees_with_the_interval_is_refused_by_the_window_check_too():
    """``evaluate_segment`` is reachable without ``require_clean_history``; it shares the scan."""
    bars = candles([110, 120, 130, 140])
    bars[2] = dataclasses.replace(bars[2], close_time=bars[2].open_time + MS_PER_HOUR - 1)
    with pytest.raises(EvaluationError, match=r"1 bar\(s\) whose close disagrees .*lasting 3599999 ms"):
        _run(_spec(), SeriesBundle(bars, funding=_funding(4)))


def test_a_window_the_funding_series_does_not_cover_is_refused():
    closes = [110, 120, 130, 140, 150]
    with pytest.raises(EvaluationError, match="has no settlements"):
        _run(_spec(), SeriesBundle(candles(closes)))
    # Two of five bars' settlements present: 8 of 20, far under coverage.
    with pytest.raises(
        EvaluationError,
        match="should hold 20 hourly funding settlements and the store has 8",
    ):
        _run(_spec(), _bundle(closes, funding=_funding(5, hours=8)))


def test_a_funding_stamp_off_the_hourly_grid_is_refused_rather_than_counted():
    """Counted alone, a stray stamp fills in for a missing hour: the report said
    nothing was missing while the stray rate was charged as that hour's carry."""
    closes = [110, 120, 130, 140, 150]
    points = _funding(5)
    missing = points.pop(5)
    stray = FundingPoint(time=missing.time - 30 * 60_000, rate=Decimal("0.01"))
    points.insert(5, stray)
    with pytest.raises(EvaluationError, match=r"not on the hourly grid .*1 off-grid"):
        _run(_spec(), _bundle(closes, funding=points))
    second = FundingPoint(time=_funding(5)[8].time + 2_000, rate=Decimal("0.01"))
    doubled = [p for p in _funding(5) if p.time != missing.time] + [second]
    with pytest.raises(EvaluationError, match=r"not on the hourly grid .*1 duplicate"):
        _run(_spec(), _bundle(closes, funding=sorted(doubled, key=lambda p: p.time)))


def test_a_settlement_or_two_missing_is_reported_rather_than_refused():
    closes = [110, 120, 130, 140, 150]
    points = _funding(5)
    del points[7]
    result = _run(_spec(), _bundle(closes, funding=points))
    assert result.funding_settlements_missing == 1
    assert "1 funding settlements missing" in "\n".join(result.describe())


def test_a_feature_the_bundle_cannot_answer_at_all_is_the_frame_s_refusal_not_this_one():
    spec = _spec(filters=[{"left": "close_1d", "op": ">", "right": 0}])
    with pytest.raises(FeatureError, match="no daily bars"):
        _run(spec, _bundle([110, 120]))


# -- no look-ahead (plan §3.6b) ------------------------------------------------


def _wander(count: int) -> list[float]:
    """A deterministic, non-monotonic series — a fixed LCG so the test is the same every run."""
    state, closes = 12345, [100.0]
    for _ in range(count - 1):
        state = (1103515245 * state + 12345) % 2**31
        closes.append(round(closes[-1] * (1 + ((state % 2001) - 1000) / 20000), 4))
    return closes


@pytest.mark.parametrize("cut", [14, 20, 27, 33])
def test_nothing_decided_before_the_cut_changes_when_the_future_is_taken_away(cut):
    """The same window, on the whole history and on the history ending at ``cut``.

    Every decision, fill and cost inside the window must be identical: the
    bars after it did not exist in one run and must not have mattered in the
    other. The window ends at ``cut`` in both, so the flatten at its last
    close is the same fill.
    """
    closes = _wander(40)
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": "sma_10"}]},
        exit={"long": [{"left": "close", "op": "<", "right": "sma_10"}]},
    )
    whole = _run(spec, _bundle(closes), first=10, stop=cut)
    truncated = _run(spec, _bundle(closes[:cut], funding=_funding(cut)), first=10, stop=cut)
    if cut > 20:
        assert whole.trades  # the property is vacuous on a window that never traded
    assert whole.trades == truncated.trades
    assert whole.net_bar_returns == truncated.net_bar_returns
    assert whole.gross_bar_returns == truncated.gross_bar_returns


def test_the_rule_a_cheat_would_need_cannot_be_built():
    """The evaluator reads only what a ``StrategySpec`` can say, and it cannot say ``t + 1``."""
    with pytest.raises(SpecError, match="has not closed when the decision is made"):
        Condition(
            left=FeatureRef(FeatureKind.CLOSE, offset=-1),
            op=Op.GT,
            right=FeatureRef(FeatureKind.CLOSE),
        )
    with pytest.raises(SpecError, match="has not closed"):
        _spec(
            entry={
                "long": [{"left": "close", "op": ">", "right": {"feature": "close", "offset": -1}}]
            }
        )


def test_the_position_exists_from_the_bar_after_the_decision():
    """Every open is the previous close, so a fill at the decision bar's close
    and a fill at the next open are the same PRICE — what differs is the bar
    the position is marked from, and that is what the returns pin."""
    closes = [90, 100, 110, 120, 130]
    opens = [90, 90, 100, 110, 120]
    spec = _spec(entry={"long": [{"left": "close", "op": ">", "right": 95}]})
    result = _run(spec, _bundle(closes, opens=opens, rate=0.0), costs=_FREE)
    trade = result.trades[0]
    assert (trade.entry_index, trade.entry_price) == (2, 100.0)
    assert result.gross_bar_returns[1] == 0.0
    assert result.gross_bar_returns[2] == pytest.approx(0.5 / 100 * (110 - 100))


# -- a whole split, and the lock ---------------------------------------------


def _split_over(bars: int) -> Split:
    return Split.by_shares("4h", start_ms=ANCHOR_MS, end_ms=ANCHOR_MS + bars * _STEP)


def test_evaluate_split_withholds_the_holdout_unless_told_otherwise():
    closes = _wander(30)
    frame = FeatureFrame(_bundle(closes))
    split = _split_over(30)
    locked = evaluate_split(_spec(), frame, split, _COSTS)
    assert locked.holdout is None
    assert [r.segment.name for r in locked.results] == [
        SegmentName.TRAIN,
        SegmentName.VALIDATION,
    ]
    assert "holdout: withheld (not promoted)" in describe_result(locked)
    opened = evaluate_split(_spec(), frame, split, _COSTS, holdout=True)
    assert opened.holdout is not None
    assert opened.holdout.segment is split.holdout
    assert opened.train == locked.train


def test_each_window_starts_flat_and_ends_flat():
    """Windows are independent measurements: a position open at the end of
    train is flattened there, not carried into validation."""
    closes = [110.0] * 30
    frame = FeatureFrame(_bundle(closes))
    result = evaluate_split(_spec(), frame, _split_over(30), _COSTS, holdout=True)
    open_times = [bar.open_time for bar in frame.bundle.bars]
    for segment_result in result.results:
        assert len(segment_result.trades) == 1
        trade = segment_result.trades[0]
        assert trade.exit_reason is ExitReason.SEGMENT_END
        first, stop = segment_result.segment.bar_range(open_times)
        assert (trade.entry_index, trade.exit_index) == (first + 1, stop - 1)


def test_the_report_states_the_parameters_the_numbers_depend_on():
    closes = _wander(30)
    frame = FeatureFrame(_bundle(closes), indicator_lookback=120)
    lines = describe_result(evaluate_split(_spec(), frame, _split_over(30), _COSTS))
    text = "\n".join(lines)
    assert lines[0] == "family: breakout"
    assert _COSTS.describe() in text
    assert "indicator window: 120 bars; sharpe annualised by sqrt(2190 bars/year)" in text
    assert "split (4h): train:" in text
    assert "gross: return" in text and "net  : return" in text


def test_the_day_closing_with_the_bar_is_loaded_and_the_millisecond_loses_no_settlement(store):
    """The venue closes a bar one millisecond before the next open. The expected
    settlement count must not lose one to that millisecond, and the daily bar
    whose close is exactly the last 4h close must still be loaded."""
    bars = candles([110.0] * 12)
    daily = candles([1000.0, 1001.0], start_ms=ANCHOR_MS, step_ms=_DAY)
    store.upsert_candles("BTC", "4h", bars)
    store.upsert_candles("BTC", "1d", daily)
    store.upsert_funding("BTC", _funding(12))
    # Bound at the fifth bar: the first day is still open, so no daily bar...
    five = load_bundle(store, coin="BTC", interval="4h", until_ms=bars[4].open_time)
    assert five.daily == ()
    # ...and at the sixth its close is EXACTLY the last 4h close (both a
    # millisecond short of the next open), so the day is loaded.
    six = load_bundle(store, coin="BTC", interval="4h", until_ms=bars[5].open_time)
    assert len(six.daily) == 1 and six.daily[0].close_time == six.bars[-1].close_time
    result = evaluate_segment(_spec(), FeatureFrame(six), _segment(0, 6), _COSTS, interval="4h")
    assert result.funding_settlements_missing == 0
    # Filled at bar 1's open and held through five closes: four settlements each.
    assert result.trades[0].funding == pytest.approx(5 * 4 * 0.0001 * result.trades[0].size * 110)


def test_a_daily_experiment_reads_its_backdrop_from_the_bars_it_already_has(store):
    daily = candles([1000 + i for i in range(12)], start_ms=ANCHOR_MS, step_ms=_DAY)
    store.upsert_candles("BTC", "1d", daily)
    store.upsert_funding("BTC", _funding(12, hours=12 * 24))
    bundle = load_bundle(store, coin="BTC", interval="1d", until_ms=ANCHOR_MS + 9 * _DAY)
    assert len(bundle.bars) == 10
    assert bundle.daily == bundle.bars
    assert load_bundle(store, coin="BTC", interval="1d").daily == tuple(daily)


def test_a_ruined_run_pays_for_its_last_fill_and_measures_turnover_on_the_equity_that_traded():
    closes = [100, 90, 80, 70, 60]
    opens = [100, 100, 90, 80, 70]
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": 50}]},
        sizing={"mode": "fixed_margin_fraction", "fraction": 1.0},
    )
    result = _run(
        spec,
        _bundle(closes, opens=opens),
        costs=CostModel(leverage=20, taker_fee_rate=0.001, slippage_bps=0),
    )
    trade = result.trades[-1]
    assert trade.exit_reason is ExitReason.RUIN
    assert trade.fees == pytest.approx(0.001 * (20 + trade.size * 90))
    # Filled 20 in and 0.2 × 90 out, over the one bar of equity (1.0) that
    # traded — not over the whole path, whose mean is negative.
    assert result.turnover == pytest.approx(38.0)


def test_a_zero_realized_vol_cannot_be_targeted():
    flat = [100.0] * 13
    spec = _spec(
        family="vol_targeting",
        entry={"long": [{"left": "close", "op": ">", "right": 1}]},
        sizing={"mode": "vol_target", "target_vol": 0.05, "lookback": 10},
    )
    result = _run(spec, _bundle(flat, rate=0.0), first=10, costs=_FREE)
    assert result.trades == ()
    assert result.bars_unevaluable == 2


def test_load_bundle_reads_nothing_past_the_bound(store):
    """The holdout lock at the store: rows past ``loadable_until`` are never read."""
    store.upsert_candles("BTC", "4h", candles(_wander(30)))
    store.upsert_candles(
        "BTC", "1d", candles([1000, 1001, 1002, 1003, 1004], start_ms=ANCHOR_MS, step_ms=_DAY)
    )
    store.upsert_funding("BTC", _funding(30))
    split = _split_over(30)
    bundle = load_bundle(store, coin="BTC", interval="4h", until_ms=split.loadable_until())
    assert bundle.bars[-1].open_time == split.validation.end_ms - _STEP
    assert bundle.bars[-1].open_time < split.holdout.start_ms
    # The settlement due at the bound's last close posts after it and is read
    # - a fact about that close - and the one due an hour later is not.
    assert bundle.funding[-1].time == bundle.bars[-1].close_time + 1 + _JITTER_MS
    # A day that CLOSES inside the holdout is not held, even if it opened before.
    assert bundle.daily[-1].close_time <= bundle.bars[-1].close_time
    assert len(bundle.daily) == 4
    everything = load_bundle(store, coin="BTC", interval="4h")
    assert len(everything.bars) == 30
    assert len(everything.funding) == 120
    # A second post in every hour, ``_JITTER_MS`` after the first: counted, that store
    # read as covered and charged each hour's carry twice. The scan refuses it.
    late_points = [FundingPoint(time=p.time + _JITTER_MS, rate=p.rate) for p in _funding(30)]
    store.upsert_funding("BTC", late_points)
    doubled = load_bundle(store, coin="BTC", interval="4h", until_ms=split.loadable_until())
    with pytest.raises(EvaluationError, match=r"not on the hourly grid \(24 duplicate"):
        evaluate_segment(
            _spec(), FeatureFrame(doubled), split.validation, _COSTS, interval="4h"
        )
    # Measured on the bounded window: no settlement missing, which a bound at
    # the exact close got wrong.
    result = evaluate_segment(_spec(), FeatureFrame(bundle), split.validation, _COSTS, interval="4h")
    assert result.funding_settlements_missing == 0
    with pytest.raises(EvaluationError, match="holds no 1d bars for ETH"):
        load_bundle(store, coin="ETH", interval="1d")


def test_the_indicator_window_actually_changes_what_the_engine_is_shown():
    """Pinned against the engine over the last fifty bars: a frame told fifty
    must give the number fifty bars give, and not the number all 130 bars do."""
    from contrib.autoresearch.upstream import context_analytics

    closes = [30000 + (i * 37) % 1000 for i in range(130)]
    bundle = _bundle(closes, rate=0.0)
    narrow = FeatureFrame(bundle, indicator_lookback=50)
    ref = FeatureRef(FeatureKind.EMA, 50)
    engine = context_analytics().compute_indicators
    assert narrow.series(ref)[-1] == pytest.approx(
        engine(bundle.bars[-50:], ["ema_50"])["ema_50"], rel=1e-12
    )
    # ...and not the engine's number over all 130 bars, which is a different EMA.
    assert engine(bundle.bars, ["ema_50"])["ema_50"] != pytest.approx(
        narrow.series(ref)[-1], rel=1e-6
    )


def test_the_indicator_window_is_a_frame_parameter_with_a_floor():
    bundle = _bundle([110.0] * 3)
    assert FeatureFrame(bundle).indicator_lookback == LIVE_CANDLE_LOOKBACK
    assert FeatureFrame(bundle, indicator_lookback=MIN_INDICATOR_LOOKBACK).indicator_lookback == 50
    with pytest.raises(FeatureError, match="below the 50 bars the engine needs"):
        FeatureFrame(bundle, indicator_lookback=49)
    with pytest.raises(FeatureError, match="whole number of bars"):
        FeatureFrame(bundle, indicator_lookback=200.0)


# -- the report's own words -------------------------------------------------


def test_a_trade_reads_back_with_its_reason_and_both_pnls():
    closes = [110, 120, 130]
    result = _run(_spec(), _bundle(closes), costs=_FREE)
    text = str(result.trades[0])
    assert text.startswith("long 2 bars @ 120 -> 130: gross +")
    assert text.endswith("(segment_end)")
    assert "net +" in text


def test_the_evaluator_refuses_an_interval_this_package_does_not_study():
    from contrib.autoresearch.split import SplitError

    frame = FeatureFrame(_bundle([110, 120, 130]))
    with pytest.raises(SplitError, match="not '1h'"):
        evaluate_segment(_spec(), frame, _segment(0, 3), _COSTS, interval="1h")


# -- where the decisions leave the rule (the signal's primitive) ----------------


def _replay(spec, bundle, *, since=0, costs=_FREE, lookback=LIVE_CANDLE_LOOKBACK):
    frame = FeatureFrame(bundle, indicator_lookback=lookback)
    return replay_position(spec, frame, costs, since_ms=ANCHOR_MS + since * _STEP)


def test_the_replay_decides_on_the_last_bar_where_the_scored_loop_will_not():
    # The whole reason this primitive exists. The window is an island, so
    # ``evaluate_segment`` never decides at its final bar — a fill there would
    # belong to the next window. A signal wants exactly that decision, and the
    # bar it would fill at is the future.
    bundle = _bundle([100, 100, 100, 105])
    assert _run(_spec(), bundle, costs=_FREE).trades == ()
    replayed = _replay(_spec(), bundle)
    assert replayed.side is Side.LONG
    assert replayed.last_close_time == bundle.bars[-1].close_time
    assert replayed.last_open_time == bundle.bars[-1].open_time
    assert replayed.bars == 4


def test_the_replay_keeps_a_position_the_scored_window_would_have_flattened():
    # Same series, two readings: the window closes what it holds at its last
    # bar (an exposure figure has to end somewhere), while the rule itself is
    # still long — which is what the live path is being told.
    bundle = _bundle([105, 106, 107])
    result = _run(_spec(), bundle, costs=_FREE)
    assert [trade.exit_reason for trade in result.trades] == [ExitReason.SEGMENT_END]
    assert _replay(_spec(), bundle).side is Side.LONG


def test_the_replay_reverses_on_the_opposite_entry_like_the_scored_loop():
    spec = _spec(
        entry={
            "long": [{"left": "close", "op": ">", "right": 102}],
            "short": [{"left": "close", "op": "<", "right": 98}],
        }
    )
    assert _replay(spec, _bundle([100, 105, 100])).side is Side.LONG
    assert _replay(spec, _bundle([100, 105, 95])).side is Side.SHORT


def test_the_replay_reports_flat_after_an_exit_decision():
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": 102}]},
        exit={"long": [{"left": "close", "op": "<", "right": 101}]},
    )
    assert _replay(spec, _bundle([100, 105, 106])).side is Side.LONG
    assert _replay(spec, _bundle([100, 105, 100])).side is None


def test_the_replay_counts_max_bars_from_the_entry_it_replayed():
    # ``max_bars`` is why the replay has to start where the rule's own history
    # does rather than at a recent tail: the count runs from an entry that may
    # be far behind the newest bar.
    spec = _spec(exit={"max_bars": 2})
    # Entered at bar 1 (bar 0 decided it), so bar 2 is the second bar held and
    # the hold expires there. One bar earlier it is still open; one bar later
    # the rule has re-entered, which is the loop's own rule and not this one.
    assert _replay(spec, _bundle([105, 106])).side is Side.LONG
    assert _replay(spec, _bundle([105, 106, 107])).side is None
    assert _replay(spec, _bundle([105, 106, 107, 108])).side is Side.LONG


def test_a_replay_that_starts_after_the_entry_cannot_see_the_position():
    # The documented cost of ``since_ms``, pinned so nobody "optimises" the
    # start forward: the same series answers differently when the replay
    # begins after the bar that opened the position.
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": 102}]},
        exit={"long": [{"left": "close", "op": "<", "right": 90}]},
    )
    bundle = _bundle([105, 100, 100, 100])
    assert _replay(spec, bundle, since=0).side is Side.LONG
    assert _replay(spec, bundle, since=2).side is None


def test_the_replay_flags_a_last_bar_whose_condition_it_could_not_evaluate():
    # The signal's fail-closed trigger. A ``None`` feature is "does not fire",
    # which over a window is a property worth counting and at the newest bar
    # is the opposite: the rule was never asked, so the side it shows is one
    # it took earlier. The producer refuses on this flag.
    spec = _spec(entry={"long": [{"left": "funding_rate", "op": ">", "right": -1}]})
    covered = _bundle([100, 101, 102, 103])
    assert _replay(spec, covered).last_bar_unevaluable is False
    assert _replay(spec, covered).side is Side.LONG
    # Funding that stops two bars early: the rate is stale past one interval,
    # so the last bar reads ``None`` and the flag is raised.
    short = _bundle([100, 101, 102, 103], funding=_funding(4, hours=4))
    replayed = _replay(spec, short)
    assert replayed.last_bar_unevaluable is True
    assert replayed.bars_unevaluable > 0


def test_the_replay_models_no_equity_so_a_ruined_rule_still_shows_a_side():
    # The documented limitation, pinned rather than left in prose: the same
    # spec and history that the scored loop marks ``ruined`` still leaves the
    # replay with a side, because nothing here tracks an account.
    ruinous = CostModel(taker_fee_rate=0.9, slippage_bps=1000, leverage=1)
    spec = _spec(
        entry={"long": [{"left": "close", "op": ">", "right": 99}]},
        exit={"long": [{"left": "close", "op": ">", "right": 0}]},
        sizing={"mode": "fixed_margin_fraction", "fraction": 0.6},
    )
    # An odd bar count, so the churn's last decision is an entry rather than
    # the exit that follows it: the point is that a side survives the ruin,
    # not that the rule happens to be in one.
    bundle = _bundle([100, 101, 102, 103, 104, 105, 106, 107, 108])
    assert _run(spec, bundle, costs=ruinous).ruined
    assert _replay(spec, bundle, costs=ruinous).side is Side.LONG


def test_the_replay_refuses_a_start_past_every_bar_it_was_given():
    with pytest.raises(EvaluationError, match="nothing to replay"):
        _replay(_spec(), _bundle([100, 101, 102]), since=3)
