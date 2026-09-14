"""Plan §6.6: the scorer gives bad strategies bad scores before anything is optimised against it.

Measured on SYNTHETIC markets, because on those the right answer is known in
advance. Each bar's close is the last one times ``1 + drift + u`` with ``u``
uniform and symmetric, so with no drift the price is a martingale: a rule
that cannot see the next bar has an expected gross return of exactly zero,
whatever it reads. On real history no baseline has a known answer, which is
why ``calibrate`` prints a table for an operator rather than asserting one.

The three baselines the language can express are the documents in
``baselines.py`` — measured here as they are measured there. The fourth,
seeded random entries, cannot be written in a language with nothing random
in it, so the rules are drawn here: a random feature, a random threshold, a
random hold, on a fresh random market each.

The bundles stay under the 200-bar indicator window, so no engine walk runs
and a measurement costs milliseconds; none of these rules reads an indicator.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from decimal import Decimal

import pytest

from contrib.autoresearch.baselines import baseline_specs, describe_calibration
from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import parse_spec
from contrib.autoresearch.evaluator import SegmentResult, evaluate_segment
from contrib.autoresearch.features import FeatureFrame, SeriesBundle
from contrib.autoresearch.metrics import SegmentMetrics
from contrib.autoresearch.split import Segment, SegmentName
from contrib.autoresearch.upstream import FundingPoint

from .conftest import ANCHOR_MS, MS_PER_HOUR, candles

_STEP = 4 * MS_PER_HOUR
_BARS = 180
_FIRST = 30  # past every warm-up the random rules can have (ret_12, and a 12-bar hold)
_FREE = CostModel(taker_fee_rate=0, slippage_bps=0)
_LIVE = CostModel()


def _market(seed: int, *, drift: float = 0.0) -> SeriesBundle:
    rng = random.Random(seed)
    closes = [30000.0]
    for _ in range(_BARS - 1):
        closes.append(round(closes[-1] * (1 + drift + rng.uniform(-0.01, 0.01)), 6))
    opens = [closes[0], *closes[:-1]]
    funding = [
        FundingPoint(time=ANCHOR_MS + (i + 1) * MS_PER_HOUR, rate=Decimal("0"))
        for i in range(_BARS * 4)
    ]
    return SeriesBundle(candles(closes, opens=opens), funding=funding)


def _measure(spec, bundle: SeriesBundle, costs: CostModel) -> SegmentResult:
    window = Segment(
        SegmentName.VALIDATION, ANCHOR_MS + _FIRST * _STEP, ANCHOR_MS + len(bundle.bars) * _STEP
    )
    return evaluate_segment(spec, FeatureFrame(bundle), window, costs, interval="4h")


def _baseline(name: str):
    return dict(baseline_specs())[name]


def _random_rule(rng: random.Random):
    feature = rng.choice(("ret_1", "ret_3", "ret_6", "ret_12"))
    threshold = round(rng.uniform(0, 0.01), 5)
    return parse_spec(
        {
            "family": "breakout",
            "entry": {
                "long": [{"left": feature, "op": ">", "right": threshold}],
                "short": [{"left": feature, "op": "<", "right": -threshold}],
            },
            "exit": {"max_bars": rng.randint(1, 12)},
            "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
        }
    )


@pytest.fixture(scope="module")
def random_entries() -> list[SegmentResult]:
    rng = random.Random(20260914)
    return [_measure(_random_rule(rng), _market(seed), _LIVE) for seed in range(120)]


def test_seeded_random_rules_on_a_driftless_market_have_a_median_gross_sharpe_of_zero(
    random_entries,
):
    traded = [result for result in random_entries if result.trades]
    # Over rules that never traded the median would be zero by construction.
    assert len(traded) >= 110
    sharpes = [result.gross.sharpe for result in traded]
    spread = statistics.stdev(sharpes)
    # The scorer does tell the runs apart: a spread of zero would pass the
    # median check below while measuring nothing.
    assert spread > 1
    # A median of n draws has a standard error of about 1.25 σ / √n; three of
    # those is the tolerance. A rule that could see the next bar would sit
    # tens of standard errors out.
    assert abs(statistics.median(sharpes)) < 3 * 1.25 * spread / math.sqrt(len(sharpes))


def test_the_same_random_rules_keep_less_net_than_gross(random_entries):
    traded = [result for result in random_entries if result.trades]
    assert all(result.net.total_return < result.gross.total_return for result in traded)
    assert statistics.median(r.net.sharpe for r in traded) < statistics.median(
        r.gross.sharpe for r in traded
    )


def test_buy_and_hold_earns_the_window_s_price_move_exposed_on_all_but_its_first_bar():
    bundle = _market(3, drift=0.001)
    result = _measure(_baseline("buy_and_hold"), bundle, _FREE)
    bars = bundle.bars
    assert len(result.trades) == 1
    assert result.exposure == pytest.approx((result.bars - 1) / result.bars)
    # All of equity at the open of the window's second bar, flattened at its
    # last close: on a costless run the compounded bar returns are exactly
    # the ratio of those two prices.
    assert result.gross.total_return == pytest.approx(
        float(bars[-1].close) / float(bars[_FIRST + 1].open) - 1
    )
    assert result.gross.sharpe > 0
    live = _measure(_baseline("buy_and_hold"), bundle, _LIVE)
    assert live.net.total_return < live.gross.total_return


def test_always_flat_scores_zero_everywhere_and_never_nan():
    metrics = SegmentMetrics.from_result(_measure(_baseline("always_flat"), _market(5), _LIVE))
    assert (metrics.trades, metrics.exposure, metrics.turnover) == (0, 0, 0)
    for tally in (metrics.gross, metrics.net):
        assert (tally.total_return, tally.sharpe, tally.max_drawdown, tally.hit_rate) == (
            0,
            0,
            0,
            0,
        )
    # And it is a record a ledger can hold: no NaN, no infinity.
    json.dumps(metrics.to_dict(), allow_nan=False)


def test_a_high_turnover_noise_rule_pays_for_every_round_trip():
    result = _measure(_baseline("high_turnover_noise"), _market(11), _LIVE)
    # In, out, flat for a bar, in again: half the bars, a round trip every two.
    assert 0.45 <= result.exposure <= 0.55
    assert len(result.trades) == pytest.approx((result.bars - 1) / 2, abs=1)
    cost = result.fees_paid + result.slippage_paid
    # About 75 round trips of the whole account at 9.5 bps a fill.
    assert cost > 0.1
    assert result.net.total_return < result.gross.total_return - 0.1


def test_the_baselines_are_documents_in_the_language_and_render_as_a_table():
    rows = [
        (name, [SegmentMetrics.from_result(_measure(spec, _market(7), _LIVE))])
        for name, spec in baseline_specs()
    ]
    lines = describe_calibration(rows)
    assert len(lines) == 3
    assert lines[1].startswith("always_flat validation: gross +0.00%, net +0.00%")
    assert "0 trades" in lines[1]
