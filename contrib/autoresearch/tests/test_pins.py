"""Plan §3.2's pins: the three borrowed analytics, held to what they answer today.

A research result is only comparable with what the paper trader saw because
this package computes its features with the trader's OWN functions. That is a
claim about code in another package, and it goes stale silently: a refactor
upstream that shifted a warm-up rule, a regime threshold or a z-score window
would break nothing here — it would quietly make trials measured before it
incomparable with trials measured after it.

So each of the three is pinned twice over: its SIGNATURE, because this package
calls it positionally, and a FIXED INPUT against a FIXED OUTPUT, because that
is the only way a change of behaviour behind an unchanged signature shows up.
A failure here is not automatically a bug upstream. It is a statement that the
research numbers either side of the change are not the same measurement, and
the response is to re-run the experiments, not to edit the expected value
until it passes.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from contrib.autoresearch.costs import LIVE_LEVERAGE, LIVE_SLIPPAGE_BPS, LIVE_TAKER_FEE_RATE
from contrib.autoresearch.dsl import LIVE_MARGIN_CAP
from contrib.autoresearch.features import (
    _INDICATOR_NAMES,
    LIVE_CANDLE_LOOKBACK,
    MIN_INDICATOR_LOOKBACK,
)
from contrib.autoresearch.upstream import (
    REGIME_INDICATORS,
    Candle,
    FundingPoint,
    MarketDataConfig,
    MarketRegime,
    context_analytics,
    supported_indicators,
)
from contrib.autoresearch.vocabulary import FeatureKind, periods_for

from .conftest import ANCHOR_MS, MS_PER_HOUR, candles, funding_points

_ANALYTICS = context_analytics()
_STEP_MS = 4 * 60 * 60_000

# Enough bars for ema_50 to have warmed up, shaped so the indicators have
# something to say: a steady drift with a three-bar zig-zag on top.
_CLOSES = [30000 + 100 * i + (300 if i % 3 == 0 else -200 if i % 3 == 1 else 0) for i in range(60)]


def _pinned_series(closes: list[int]) -> list[Candle]:
    """The fixture the numbers below were measured on, band included.

    The ±50 high/low band is not decoration: ``atr_14`` is an average TRUE
    RANGE, so the pinned value belongs to this band and no other. Spelled out
    here rather than taken from the shared factory's default, because a pin
    whose input can drift is a pin that gets re-pinned instead of read.
    """
    return candles(
        closes, highs=[close + 50 for close in closes], lows=[close - 50 for close in closes]
    )


# -- signatures ------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "parameters"),
    [
        ("compute_indicators", ["candles", "names"]),
        ("classify_regime", ["indicators", "reference_price"]),
        ("funding_zscore", ["history", "current", "as_of_ms", "window_days"]),
    ],
)
def test_the_borrowed_analytics_still_take_what_this_package_passes(name, parameters):
    """Names AND order, because every call site here passes them positionally.

    A reordering upstream would keep every call valid and change what was
    measured — a z-score window handed a timestamp, an indicator engine handed
    its names as candles — so the pin is on the sequence, not on the set.
    """
    signature = inspect.signature(getattr(_ANALYTICS, name))
    assert list(signature.parameters) == parameters
    assert all(
        parameter.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
        for parameter in signature.parameters.values()
    )


def test_the_analytics_bundle_holds_the_upstream_functions_themselves():
    """Identity, not equality: a local re-implementation would satisfy equality.

    ``tests/test_upstream.py`` makes this check for everything re-exported at
    module scope; these three are imported inside a call (to keep pandas out
    of the store commands), so they need it made here.
    """
    from contrib.hyperliquid_perp.domains.perp.context_builder import (
        classify_regime,
        funding_zscore,
    )
    from contrib.hyperliquid_perp.domains.perp.indicators import compute_indicators

    assert _ANALYTICS.compute_indicators is compute_indicators
    assert _ANALYTICS.classify_regime is classify_regime
    assert _ANALYTICS.funding_zscore is funding_zscore


# -- compute_indicators ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("rsi_14", 63.41716045443686),
        ("ema_20", 34988.56503786863),
        ("ema_50", 34077.79803561745),
        ("atr_14", 413.8489161075816),
        ("macd", 632.3280899381207),
    ],
)
def test_the_indicator_engine_still_computes_what_it_computed(name, expected):
    values = _ANALYTICS.compute_indicators(_pinned_series(_CLOSES), [name])
    assert values[name] == pytest.approx(expected, rel=1e-12)


def test_the_indicator_engine_still_answers_none_below_its_warm_up():
    """``None``, never a warm-up artefact — the rule this package's ``None`` means.

    Ten bars is under every minimum in the engine's table. If a refactor ever
    returned a seeded number here instead, features would start carrying
    values during warm-up and every backtest would trade on them.
    """
    values = _ANALYTICS.compute_indicators(_pinned_series(_CLOSES[:10]), list(_INDICATOR_NAMES))
    assert set(values) == set(_INDICATOR_NAMES)
    assert all(value is None for value in values.values())


def test_every_engine_backed_feature_period_is_one_the_engine_supports():
    """The vocabulary's claim that ``ema_20`` is computable, checked against upstream.

    This is the pin that stops the vocabulary from growing a name the engine
    answers ``None`` for at every bar — a strategy that never fires, scored as
    one that was tried and found wanting.
    """
    engine_backed = {
        f"{kind.value}_{period}"
        for kind in (FeatureKind.EMA, FeatureKind.RSI, FeatureKind.ATR)
        for period in periods_for(kind)
    }
    assert engine_backed <= set(supported_indicators())


def test_the_regime_trio_is_computed_whether_or_not_a_spec_asks_for_it():
    """The regime is a feature of its own, so its inputs cannot be optional."""
    assert set(REGIME_INDICATORS) <= set(_INDICATOR_NAMES)


# -- the two live NUMBERS this package reasons against ---------------------


def test_the_indicator_window_is_the_one_the_live_fetch_asks_for():
    """Read off the live config's own default, so the two cannot drift apart.

    Every indicator feature is computed over this many closed bars. If the
    live path started fetching a different number, research indicators would
    stop being the ones the trader's context was built from — silently, since
    an EMA over a different window is a perfectly plausible EMA.
    """
    assert LIVE_CANDLE_LOOKBACK == MarketDataConfig().candle_lookback == 200


def test_the_vol_target_cap_is_still_what_risk_gate_would_allow():
    """``LIVE_MARGIN_CAP`` is written as a number, so the number is pinned here.

    It is not imported at run time: reaching for ``risk_gate`` would put its
    import cost on every store command for one constant. That trade is only
    safe while something checks the constant, which is this.
    """
    from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskConfig

    assert LIVE_MARGIN_CAP * 100 == RiskConfig().max_target_margin_pct == 60


def test_the_cost_defaults_are_the_paper_run_s_own():
    """Three more numbers written down rather than imported, for the same reason.

    The evaluator's default cost model claims to be the paper trader's fee,
    slippage and leverage. If any of those config defaults moved, research
    net returns would quietly stop being comparable with the paper ledger.
    """
    from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskConfig
    from contrib.hyperliquid_perp.paper.config import FillModelConfig, PaperExecutionConfig

    assert Decimal(str(LIVE_TAKER_FEE_RATE)) == PaperExecutionConfig().taker_fee_rate
    assert Decimal(str(LIVE_SLIPPAGE_BPS)) == FillModelConfig().slippage_bps
    assert Decimal(str(LIVE_LEVERAGE)) == RiskConfig().leverage


def test_the_funding_sign_is_the_paper_ledger_s():
    """A long at a positive rate PAYS — issue #134's one formula, pinned here
    because the evaluator charges the same sign in its own float lane."""
    from contrib.hyperliquid_perp.domains.perp.margin import funding_cost

    assert funding_cost(Decimal(1), Decimal("0.0001")) > 0
    assert funding_cost(Decimal(-1), Decimal("0.0001")) < 0


def test_the_studied_intervals_are_spelled_the_venue_s_way():
    """``STUDIED_INTERVALS`` is a literal so ``constants`` stays import-free; this is
    what keeps it a subset of the venue's vocabulary."""
    from contrib.autoresearch.constants import STUDIED_INTERVALS
    from contrib.autoresearch.upstream import CandleInterval

    assert set(STUDIED_INTERVALS) <= {member.value for member in CandleInterval}
    assert STUDIED_INTERVALS == ("4h", "1d")


def test_the_indicator_window_floor_is_the_engine_s_own_warm_up():
    """Derived from the borrowed table, so an ``ema_100`` added upstream and to
    the vocabulary raises the floor without an edit here."""
    from contrib.autoresearch.upstream import required_candles

    assert MIN_INDICATOR_LOOKBACK == required_candles(list(_INDICATOR_NAMES)) == 50


# -- classify_regime -------------------------------------------------------

# The thresholds this table straddles are upstream's: ATR at or above 4% of
# price is volatile, and an EMA separation at or above 0.75% of price with
# price/EMA20/EMA50 aligned is trending. Pinned at the boundary in both
# directions, so a retune upstream shows up here as a red test rather than as
# a research regime filter that silently starts labelling a different market.
@pytest.mark.parametrize(
    ("atr", "ema20", "ema50", "expected"),
    [
        pytest.param(1200.0, 29700.0, 29400.0, MarketRegime.VOLATILE, id="atr-at-4pct"),
        pytest.param(1199.0, 29700.0, 29400.0, MarketRegime.TRENDING, id="just-under-4pct"),
        pytest.param(100.0, 29775.0, 29550.0, MarketRegime.TRENDING, id="sep-at-0.75pct"),
        pytest.param(100.0, 29776.0, 29552.0, MarketRegime.RANGING, id="just-under-0.75pct"),
        pytest.param(100.0, 30300.0, 30600.0, MarketRegime.TRENDING, id="aligned-downwards"),
        pytest.param(100.0, 29400.0, 29700.0, MarketRegime.RANGING, id="separated-but-crossed"),
    ],
)
def test_the_regime_classifier_still_labels_these_markets_the_same_way(atr, ema20, ema50, expected):
    indicators = {"atr_14": atr, "ema_20": ema20, "ema_50": ema50}
    assert _ANALYTICS.classify_regime(indicators, Decimal(30000)) is expected


@pytest.mark.parametrize("missing", REGIME_INDICATORS)
def test_the_regime_classifier_still_defaults_to_ranging_without_its_inputs(missing):
    """The default this package deliberately does NOT pass through.

    Pinned because :func:`~contrib.autoresearch.features._regime_of` is built
    on it: that function reports ``None`` during warm-up precisely because
    this answer is ``RANGING`` and the live path's guard refuses the cycle
    rather than show it. If upstream ever started refusing here instead, the
    mirror would be doing the job twice.
    """
    indicators = {"atr_14": 100.0, "ema_20": 29700.0, "ema_50": 29400.0}
    indicators[missing] = None
    assert _ANALYTICS.classify_regime(indicators, Decimal(30000)) is MarketRegime.RANGING


# -- funding_zscore --------------------------------------------------------


def test_the_funding_zscore_still_standardises_the_same_way():
    points = funding_points(30)
    as_of = ANCHOR_MS + 30 * MS_PER_HOUR
    score, samples = _ANALYTICS.funding_zscore(points, Decimal("0.0005"), as_of, 7)
    assert samples == 30
    assert score == pytest.approx(3.918936656304748, rel=1e-12)
    below, _ = _ANALYTICS.funding_zscore(points, Decimal("0.0001"), as_of, 7)
    assert below == pytest.approx(-0.6247580176717713, rel=1e-12)


def test_the_funding_zscore_still_refuses_a_window_it_cannot_describe():
    """Two ``None``s that are not NaN, and the sample count that explains them."""
    as_of = ANCHOR_MS + 30 * MS_PER_HOUR
    score, samples = _ANALYTICS.funding_zscore(funding_points(5), Decimal("0.0001"), as_of, 7)
    assert (score, samples) == (None, 5)
    flat = [
        FundingPoint(time=ANCHOR_MS + index * MS_PER_HOUR, rate=Decimal("0.00001"))
        for index in range(30)
    ]
    assert _ANALYTICS.funding_zscore(flat, Decimal("0.0005"), as_of, 7) == (None, 30)


def test_the_funding_zscore_window_still_excludes_the_instant_it_is_asked_about():
    """``cutoff <= t < as_of`` — the half-open window the sliced call relies on.

    :mod:`~contrib.autoresearch.features` hands this function a slice of the
    settlement history rather than all of it, and the slice is cut on exactly
    this boundary. If upstream widened the window to include ``as_of``, the
    slice would stop matching what the function selects for itself, and the
    z-scores would change without anything failing.
    """
    points = funding_points(30)
    as_of = points[-1].time  # the newest settlement sits exactly ON the boundary
    _score, samples = _ANALYTICS.funding_zscore(points, Decimal("0.0005"), as_of, 7)
    assert samples == 29
