"""The bar-level simulation: a spec, a history, a window in, a scored trial out.

Plan §3.6 fixes the one rule everything here follows — a decision is taken at
a bar's CLOSE and filled at the NEXT bar's open — and the shape of this module
is that rule made unavoidable rather than checked afterwards. The loop reads
features at ``t`` through columns the frame computed (a reference cannot
carry a future offset; the vocabulary refuses one at construction), and turns
what it read into a *pending* order that the loop fills when it reaches
``t + 1``. There is no code path that prices a decision at the close it was
taken on, so there is nothing for a test to catch — though
``tests/test_evaluator.py`` still measures it, by hand-computing one trade and
by evaluating a truncated history and demanding the same decisions.

What a spec MEANS at the seams the parser left open (plan §10.1) is decided
here, once, and stated in :func:`_decide`:

- **An empty ``exit`` is "hold"**: the position is held until an opposite
  entry reverses it or the window ends. Not "exit when the entry condition
  stops holding" — that would turn every breakout spec without an exit into
  one that leaves a bar after it entered, and the parser already bounds
  ``max_bars`` on the reasoning that "never exit" is a thing this evaluator
  treats as one.
- **An opposite entry reverses.** A held long that sees its short entry fire
  is closed and a short is opened at the same fill. It is the most-signalled
  reading and the one that makes an always-in rule writable at all.
- **Filters gate entries only.** A filter going false does not close a
  position; a rule that wants that writes it under ``exit``, where the
  read-back shows it.
- **A missing feature is a rule that does not fire.** ``None`` at a bar
  means "not available here" (warm-up, a settlement that has not happened),
  which is the reading :mod:`.features` documents for entries; applying a
  different one to exits would make an exit rule fail-closed on the same
  ``None`` an entry rule fails-open on. The bars where a consulted condition
  could not be evaluated are COUNTED (``bars_unevaluable``), so a rule that
  went silent for a month shows up as a number rather than as a hold.
- **Both entries firing at once is a conflict, held or flat.** Nothing is
  opened, nothing is reversed, and the bar is counted
  (``bars_conflicting``). The same signal state has to mean the same thing
  whatever the position is, or a rule reads as a reversal rule only while
  it happens to be in a trade.

Two things the window itself guarantees. The window is the measured span
for EVERY spec (plan §10.2) — exposure and hit rate are ratios over the same
denominator whichever hypothesis is scored — so a store that only partly
covers it, and a spec whose features have no value at its first bar, are
both refused by name rather than measured over less. And a position still
open at the window's last bar is flattened at that bar's close: the window
never reads a bar that belongs to the next one, which is the property the
holdout lock (:mod:`.split`) rests on.

Costs follow plan §3.7. Gross is the price move, mid to mid; net subtracts
the fee and slippage of every fill (charged on the mid notional, see
:meth:`CostModel.fill_cost`) and the funding of every settlement that landed
while a position was held — signed the way the paper ledger signs it, so a
long at a positive rate PAYS. Both are reported, because a strategy whose
gross is good and whose net is not is the single most common thing a
hypothesis loop produces, and a report that showed one number would be
scoring the other.

What is deliberately NOT here: a stop-loss or take-profit (the DSL refuses
them, and the reason is this fill model — an intrabar stop is where a
backtest most easily cheats), a liquidation engine (a run whose equity
reaches zero is marked ``ruined`` and stops, which is the honest reading of
a strategy that lost everything), and pyramiding (a same-side entry while
held is ignored).
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final

from .constants import (
    CANDLE_STAMP_TOLERANCE_MS,
    FUNDING_INTERVAL_MS,
    FUNDING_STAMP_TOLERANCE_MS,
    MS_PER_DAY,
)
from .costs import CostModel
from .dsl import Condition, Op, Side, SizingMode, StrategySpec, describe_spec
from .features import FeatureFrame, FeatureValue, SeriesBundle, window_is_covered
from .gaps import scan_stamps
from .split import Segment, Split, studied_interval
from .store import ResearchStore
from .upstream import (
    REGIME_INDICATORS,
    Candle,
    CandleInterval,
    MarketRegime,
    VocabEnum,
    from_epoch_ms,
    interval_to_ms,
    required_candles,
)
from .vocabulary import FeatureKind, FeatureRef

__all__ = [
    "MS_PER_YEAR",
    "STARTING_EQUITY",
    "UNLABELLED",
    "EvaluationError",
    "ExitReason",
    "RegimeBucket",
    "SegmentResult",
    "SplitResult",
    "Tally",
    "Trade",
    "describe_result",
    "evaluate_segment",
    "evaluate_split",
    "load_bundle",
]

# The annualisation base. Stated as a constant so the report can print the
# factor it used rather than leave a reader to guess whether a "Sharpe of
# 1.4" was scaled by the bar count of a 365- or a 252-day year — a crypto
# venue trades every day, so it is 365.
MS_PER_YEAR: Final = 365 * MS_PER_DAY

# Every run starts here, so every money figure in a result is a FRACTION of
# starting equity and reads as a return without a conversion.
STARTING_EQUITY: Final = 1.0

# The label the regime bucket files a bar under when the regime is not
# available there (the indicator warm-up at the head of a short bundle).
UNLABELLED: Final = "unlabelled"

_REGIME_REF: Final = FeatureRef(FeatureKind.REGIME)

# The bar at which the regime can first be labelled: the warm-up of the trio
# the classifier reads — NOT the vocabulary-wide indicator floor, which only
# equals it today because ``ema_50`` happens to be both the regime's slowest
# input and the slowest indicator in the vocabulary.
_REGIME_WARMUP: Final = required_candles(REGIME_INDICATORS)


class EvaluationError(RuntimeError):
    """This window cannot be measured on this history, and the message says why.

    Distinct from a bad SPEC (``SpecError``, the document's fault) and from a
    feature the bundle cannot answer at all (``FeatureError``): this is the
    pairing of a legal spec with a legal bundle over a window the two do not
    cover — a hole in the bars, a warm-up not finished by the window's first
    bar, a funding series that does not reach it. The response is to fetch
    or to move the window, never to edit the spec.
    """


class ExitReason(VocabEnum, noun="exit reason"):
    """Why a trade closed. Carried on the trade so a report can say which rule spoke."""

    EXIT_RULE = "exit_rule"
    MAX_BARS = "max_bars"
    REVERSAL = "reversal"
    SEGMENT_END = "segment_end"
    RUIN = "ruin"


@dataclass(frozen=True)
class Trade:
    """One round trip, mid to mid, with its costs beside it.

    ``gross_pnl`` is ``signed size × (exit − entry)``: every bar's marking
    telescopes to that, whichever way the trade ended, so it is derived from
    the two prices rather than accumulated beside them — two records of one
    number were one bar's move apart the moment either was edited alone.
    """

    side: Side
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    size: float
    notional: float  # at entry, mid
    fees: float
    slippage: float
    funding: float  # positive = paid
    exit_reason: ExitReason

    @property
    def signed_size(self) -> float:
        return self.size if self.side is Side.LONG else -self.size

    @property
    def gross_pnl(self) -> float:
        return self.signed_size * (self.exit_price - self.entry_price)

    @property
    def net_pnl(self) -> float:
        return self.gross_pnl - self.fees - self.slippage - self.funding

    @property
    def bars_held(self) -> int:
        """Bars whose close the position was held through."""
        return (
            self.exit_index
            - self.entry_index
            + (1 if self.exit_reason in (ExitReason.SEGMENT_END, ExitReason.RUIN) else 0)
        )

    def __str__(self) -> str:
        return (
            f"{self.side.value} {self.bars_held} bars @ {self.entry_price:g} -> "
            f"{self.exit_price:g}: gross {self.gross_pnl:+.4f} net {self.net_pnl:+.4f} "
            f"({self.exit_reason.value})"
        )


@dataclass(frozen=True)
class Tally:
    """The four statistics computed once GROSS and once NET (plan §3.9).

    Both are built from per-bar returns on the SAME denominator — the equity
    actually deployed, which is the net path — so the gross figures are "the
    price move, on the capital the strategy really had", compounded. A
    strategy that has lost half its equity to costs and then makes a bar's
    move of one hundredth of its starting stake is booked as a 2% gross bar,
    not 1%: that is the return the position earned, and it is why
    ``gross.total_return`` is not the plain sum of the trades' ``gross_pnl``.
    """

    total_return: float
    sharpe: float
    max_drawdown: float
    hit_rate: float

    def describe(self, label: str) -> str:
        return (
            f"{label}: return {self.total_return:+.2%}, sharpe {self.sharpe:.2f}, "
            f"max drawdown {self.max_drawdown:.2%}, hit rate {self.hit_rate:.0%}"
        )


@dataclass(frozen=True)
class RegimeBucket:
    """Net bar returns summed over the bars carrying one regime label (plan §3.9)."""

    label: str
    bars: int
    net_return: float


@dataclass(frozen=True)
class SegmentResult:
    """Everything one window said about one spec under one cost model."""

    segment: Segment
    bars: int
    bars_per_year: float
    gross: Tally
    net: Tally
    trades: tuple[Trade, ...]
    exposure: float
    turnover: float
    fees_paid: float
    slippage_paid: float
    funding_paid: float
    bars_unevaluable: int
    bars_conflicting: int
    funding_settlements_missing: int
    regime_buckets: tuple[RegimeBucket, ...]
    ruined: bool
    gross_bar_returns: tuple[float, ...]
    net_bar_returns: tuple[float, ...]

    def describe(self) -> list[str]:
        lines = [
            f"{self.segment}: {self.bars} bars, {len(self.trades)} trades, "
            f"exposure {self.exposure:.0%}, turnover {self.turnover:.2f}x"
            + (" — RUINED" if self.ruined else ""),
            "  " + self.gross.describe("gross"),
            "  " + self.net.describe("net  "),
            f"  costs: fees {self.fees_paid:.4f}, slippage {self.slippage_paid:.4f}, "
            f"funding {self.funding_paid:+.4f} (positive = paid)",
        ]
        if self.regime_buckets:
            lines.append(
                "  by regime (net bar returns summed): "
                + ", ".join(
                    f"{bucket.label} {bucket.net_return:+.2%} over {bucket.bars} bars"
                    for bucket in self.regime_buckets
                )
            )
        notes = []
        if self.bars_unevaluable:
            notes.append(f"{self.bars_unevaluable} bars where a consulted rule had no value")
        if self.bars_conflicting:
            notes.append(f"{self.bars_conflicting} bars where long and short both fired")
        if self.funding_settlements_missing:
            notes.append(f"{self.funding_settlements_missing} funding settlements missing")
        if notes:
            lines.append("  note: " + "; ".join(notes))
        return lines


@dataclass(frozen=True)
class SplitResult:
    """A spec measured on the windows it was allowed to see."""

    spec: StrategySpec
    costs: CostModel
    split: Split
    indicator_lookback: int
    train: SegmentResult
    validation: SegmentResult
    holdout: SegmentResult | None

    @property
    def results(self) -> tuple[SegmentResult, ...]:
        measured = (self.train, self.validation)
        return measured if self.holdout is None else (*measured, self.holdout)


# -- the loop ----------------------------------------------------------------


@dataclass
class _Open:
    """The position currently held, and the costs it has run up so far."""

    side: Side
    entry_index: int
    entry_price: float
    size: float
    notional: float
    fees: float = 0.0
    slippage: float = 0.0
    funding: float = 0.0

    @property
    def signed_size(self) -> float:
        return self.size if self.side is Side.LONG else -self.size

    def closed(self, exit_index: int, exit_price: float, reason: ExitReason) -> Trade:
        return Trade(
            side=self.side,
            entry_index=self.entry_index,
            exit_index=exit_index,
            entry_price=self.entry_price,
            exit_price=exit_price,
            size=self.size,
            notional=self.notional,
            fees=self.fees,
            slippage=self.slippage,
            funding=self.funding,
            exit_reason=reason,
        )


@dataclass(frozen=True)
class _Pending:
    """What the decision at ``t`` asks the fill at ``t + 1`` to do."""

    close: ExitReason | None = None
    open_side: Side | None = None
    open_notional: float = 0.0


@dataclass
class _Bar:
    """The running totals for one bar of the loop."""

    gross: float = 0.0
    cost: float = 0.0


class _Reader:
    """One spec's conditions over one frame, with its columns resolved once.

    Resolved once because the frame's public verb re-runs its empty-column
    guard on every call — 4.8 µs against 0.12 µs for the tuple read behind
    it, measured — and the loop reads several operands per bar for thousands
    of bars, per spec. The guard can only say something new once per column,
    and :func:`_require_measurable` has already given it that chance.
    """

    def __init__(self, frame: FeatureFrame, spec: StrategySpec) -> None:
        self.spec = spec
        self.columns: dict[tuple[FeatureKind, int | None], tuple[FeatureValue, ...]] = {
            (ref.kind, ref.period): frame.series(ref) for ref in spec.features
        }
        self.unevaluable = False
        self.sizing_ref = spec.sizing.refs[0] if spec.sizing.refs else None

    def read(self, ref: FeatureRef, index: int) -> FeatureValue:
        """``ref``'s value for the decision at ``bars[index]``'s close — the offset applied here."""
        shifted = index - ref.offset
        return None if shifted < 0 else self.columns[(ref.kind, ref.period)][shifted]

    def holds(self, conditions: Sequence[Condition], index: int) -> bool:
        """ANDed, and an empty list is ``False`` — no rule, no signal.

        Callers that want "no filter means pass" say so themselves; making
        the empty case ``True`` here would make an empty ``entry.short`` fire
        at every bar.
        """
        if not conditions:
            return False
        # Every condition is read, not short-circuited: ``bars_unevaluable``
        # is a property of the rule and the history, and a lazy AND would
        # make it depend on the order the author wrote the clauses in.
        outcomes = [self._holds(condition, index) for condition in conditions]
        return all(outcomes)

    def passes(self, index: int) -> bool:
        """The filter reading: nothing declared is nothing gating."""
        return not self.spec.filters or self.holds(self.spec.filters, index)

    def _holds(self, condition: Condition, index: int) -> bool:
        left = self.read(condition.left, index)
        right = condition.right
        if isinstance(right, FeatureRef):
            right = self.read(right, index)
        if left is None or right is None:
            self.unevaluable = True
            return False
        if isinstance(right, MarketRegime):
            return (left is right) if condition.op is Op.EQ else (left is not right)
        return _COMPARE[condition.op](left, right)

    def notional(self, index: int, equity: float, costs: CostModel) -> float | None:
        """What a firing rule asks for at this bar, in quote currency; ``None`` if it cannot size."""
        sizing = self.spec.sizing
        if sizing.mode is SizingMode.FIXED_MARGIN_FRACTION:
            assert sizing.fraction is not None
            return sizing.fraction * costs.leverage * equity
        assert sizing.target_vol is not None and sizing.max_fraction is not None
        assert self.sizing_ref is not None
        realized = self.read(self.sizing_ref, index)
        if realized is None or realized <= 0:
            # A vol-targeted rule that cannot read the vol cannot say how much
            # it wants, which is the same fact as a condition it cannot
            # evaluate. A vol of exactly zero — ten identical closes, a stuck
            # feed — is that case too, not a licence to size to the cap.
            self.unevaluable = True
            return None
        cap = sizing.max_fraction * costs.leverage
        return min(sizing.target_vol / float(realized), cap) * equity

    def entry(self, index: int, equity: float, costs: CostModel) -> tuple[_Pending | None, bool]:
        """Which entry fires at this bar, sized; the bool says both did.

        The one place the "filters pass → entry holds → size it → a rule that
        cannot size does not fire" chain is written, so the flat decision and
        the reversal decision cannot read it differently.
        """
        if not self.passes(index):
            return None, False
        long_fires = self.holds(self.spec.entry_long, index)
        short_fires = self.holds(self.spec.entry_short, index)
        if long_fires and short_fires:
            return None, True
        if not (long_fires or short_fires):
            return None, False
        notional = self.notional(index, equity, costs)
        if notional is None:
            return None, False
        side = Side.LONG if long_fires else Side.SHORT
        return _Pending(open_side=side, open_notional=notional), False


_COMPARE: Final = {
    Op.GT: lambda a, b: a > b,
    Op.GE: lambda a, b: a >= b,
    Op.LT: lambda a, b: a < b,
    Op.LE: lambda a, b: a <= b,
}


def _decide(
    reader: _Reader, index: int, held: _Open | None, equity: float, costs: CostModel
) -> tuple[_Pending | None, bool]:
    """The decision at ``bars[index]``'s close; the bool says long and short both fired.

    This is where the plan §10.1 readings live (see the module docstring):
    an empty exit holds, an opposite entry reverses, filters gate entries,
    ``None`` does not fire, and both entries at once is a conflict.
    """
    entry, conflict = reader.entry(index, equity, costs)
    if held is None:
        return entry, conflict
    close: ExitReason | None = None
    if reader.holds(reader.spec.exits(held.side), index):
        close = ExitReason.EXIT_RULE
    elif reader.spec.max_bars is not None and index - held.entry_index + 1 >= reader.spec.max_bars:
        close = ExitReason.MAX_BARS
    if entry is not None and entry.open_side is not held.side:
        return replace(entry, close=close or ExitReason.REVERSAL), conflict
    # A same-side entry while held is not pyramided; after an exit decision
    # it is not re-entered at the same bar either — the next bar decides.
    return (_Pending(close=close) if close is not None else None), conflict


def evaluate_segment(
    spec: StrategySpec,
    frame: FeatureFrame,
    segment: Segment,
    costs: CostModel,
    *,
    interval: str,
) -> SegmentResult:
    """Score ``spec`` over ``segment`` of ``frame``'s history under ``costs``.

    ``interval`` is the bars' cadence, stated by the caller (a split carries
    it) rather than inferred from the bars: it is what the window is checked
    for holes against, and inferring it from the first two bars would assume
    the regularity being checked.
    """
    step = interval_to_ms(studied_interval(interval))
    bars = frame.bundle.bars
    first, stop = segment.bar_range([bar.open_time for bar in bars])
    settlements = _Settlements(frame.bundle)
    _require_measurable(spec, frame, segment, first, stop, step, settlements)

    reader = _Reader(frame, spec)
    equity = STARTING_EQUITY
    held: _Open | None = None
    pending: _Pending | None = None
    previous_close = 0.0
    trades: list[Trade] = []
    gross_returns: list[float] = []
    net_returns: list[float] = []
    equity_path: list[float] = []
    unevaluable = conflicting = 0
    ruined = False

    for index in range(first, stop):
        bar = bars[index]
        open_price, close_price = float(bar.open), float(bar.close)
        running = _Bar()
        last = index == stop - 1

        # 1. Fill what the previous close decided, at THIS bar's open.
        if pending is not None:
            if pending.close is not None:
                assert held is not None
                running.gross += held.signed_size * (open_price - previous_close)
                trades.append(_close(held, index, open_price, pending.close, costs, running))
                held = None
            if pending.open_side is not None:
                assert held is None
                held = _open(
                    pending.open_side, pending.open_notional, index, open_price, costs, running
                )
            pending = None

        # 2. Mark the held position to this bar's close, and settle its funding.
        if held is not None:
            basis = open_price if held.entry_index == index else previous_close
            running.gross += held.signed_size * (close_price - basis)
            paid = settlements.due(bar, held.signed_size * close_price)
            held.funding += paid
            running.cost += paid
            if last:
                # The window's last bar: flatten at its close so the window
                # reads nothing of the next one. Costed like any other fill.
                trades.append(
                    _close(held, index, close_price, ExitReason.SEGMENT_END, costs, running)
                )
                held = None

        # 3. Book the bar.
        before = equity
        if held is not None and before + running.gross - running.cost <= 0:
            # Ruin: the position is closed at this close like any other fill
            # — its costs are real — and the window is over. The bars after
            # it are booked flat; the equity path is not padded, so the mean
            # equity turnover is measured against is the equity that traded.
            ruined = True
            trades.append(_close(held, index, close_price, ExitReason.RUIN, costs, running))
            held = None
        equity += running.gross - running.cost
        gross_returns.append(running.gross / before)
        net_returns.append((running.gross - running.cost) / before)
        equity_path.append(equity)
        previous_close = close_price
        if ruined:
            remaining = stop - index - 1
            gross_returns += [0.0] * remaining
            net_returns += [0.0] * remaining
            break

        # 4. Decide at this close, for the next open — never on the last bar.
        if not last:
            reader.unevaluable = False
            pending, both = _decide(reader, index, held, equity, costs)
            unevaluable += reader.unevaluable
            conflicting += both

    # Every position ends as a trade (the window flattens, ruin closes), so
    # what was held, filled and paid is read off the trades rather than kept
    # in accumulators beside them — one record of each number.
    count = stop - first
    bars_per_year = MS_PER_YEAR / step
    # Turnover is measured against the equity that TRADED: the ruin bar's
    # equity is at or below zero and is not a stake anything was filled on.
    traded = equity_path[:-1] if ruined else equity_path
    mean_equity = statistics.fmean(traded) if traded else 0.0
    filled = sum(t.notional + t.size * t.exit_price for t in trades)
    return SegmentResult(
        segment=segment,
        bars=count,
        bars_per_year=bars_per_year,
        gross=_tally(gross_returns, [t.gross_pnl for t in trades], bars_per_year),
        net=_tally(net_returns, [t.net_pnl for t in trades], bars_per_year),
        trades=tuple(trades),
        exposure=sum(t.bars_held for t in trades) / count,
        turnover=filled / mean_equity if mean_equity > 0 else 0.0,
        fees_paid=sum(t.fees for t in trades),
        slippage_paid=sum(t.slippage for t in trades),
        funding_paid=sum(t.funding for t in trades),
        bars_unevaluable=unevaluable,
        bars_conflicting=conflicting,
        funding_settlements_missing=settlements.missing(bars[first], bars[stop - 1]),
        regime_buckets=_regime_buckets(frame, first, stop, net_returns),
        ruined=ruined,
        gross_bar_returns=tuple(gross_returns),
        net_bar_returns=tuple(net_returns),
    )


def _open(
    side: Side, notional: float, index: int, price: float, costs: CostModel, running: _Bar
) -> _Open:
    fee, slip = costs.fill_cost(notional)
    running.cost += fee + slip
    return _Open(
        side=side,
        entry_index=index,
        entry_price=price,
        size=notional / price,
        notional=notional,
        fees=fee,
        slippage=slip,
    )


def _close(
    held: _Open, index: int, price: float, reason: ExitReason, costs: CostModel, running: _Bar
) -> Trade:
    notional = held.size * price
    fee, slip = costs.fill_cost(notional)
    held.fees += fee
    held.slippage += slip
    running.cost += fee + slip
    return held.closed(index, price, reason)


class _Settlements:
    """The funding settlements a held position pays, bar by bar.

    A settlement belongs to the bar whose ``(open, close]`` it falls in, on
    EXACT edges — the same rule :mod:`.features` reads settlements by (its
    rate is the last one stamped at or before the close; its ``funding_cum``
    sums ``(previous close, close]``). One rule, because a spec that reads
    ``funding_cum_1`` and pays funding over the same bar must be looking at
    the same four settlements. It is also the physically right rule for the
    fill model: the venue stamps a settlement a few ms AFTER the hour, and a
    position filled at that hour's open exists when it posts, while one
    flattened at that hour's close does not. A first cut pushed both edges
    back by the posting jitter, which put the jitter-late settlement into
    the bar just closed — the opposite of what the feature module says, on
    every bar of the real store.

    Signed the way the paper ledger signs it: positive is PAID (a long at a
    positive rate pays), which is what the running cost adds.

    Coverage is counted by stamps, the way the feature module counts a
    window's occupancy, so an extra off-grid stamp inside the span could in
    principle stand in for a missing hour. The store's primary key is the
    stamp, so it cannot be an exact duplicate; the gap scan is what names an
    off-grid one.
    """

    def __init__(self, bundle: SeriesBundle) -> None:
        self.points = bundle.funding
        self.times = [point.time for point in self.points]

    def _slice(self, bar: Candle, slack: int = 0) -> tuple[int, int]:
        return (
            bisect_right(self.times, bar.open_time + slack),
            bisect_right(self.times, bar.close_time + slack),
        )

    def due(self, bar: Candle, signed_notional: float) -> float:
        lo, hi = self._slice(bar)
        return sum(signed_notional * float(self.points[i].rate) for i in range(lo, hi))

    def held(self, first_bar: Candle, last_bar: Candle) -> tuple[int, int]:
        """``(stored, expected)`` hourly settlements over the span of those bars.

        This asks whether the settlements EXIST, not which bar pays them, so
        its edges carry the venue's posting jitter: the settlement due at the
        span's last close posts a few ms after it, and on exact edges every
        real window would read as missing exactly one. The charging edges
        in :meth:`due` stay exact for the reason the class docstring gives.
        """
        lo, _ = self._slice(first_bar, FUNDING_STAMP_TOLERANCE_MS)
        _, hi = self._slice(last_bar, FUNDING_STAMP_TOLERANCE_MS)
        # Rounded, because a venue close is one millisecond BEFORE the next
        # open (see ``constants``): floored, a span of N hours less 1 ms
        # expected N - 1 settlements, and a window with one genuinely absent
        # read as complete.
        span = last_bar.close_time - first_bar.open_time
        return hi - lo, round(span / FUNDING_INTERVAL_MS)

    def missing(self, first_bar: Candle, last_bar: Candle) -> int:
        """How many of the settlements the window's span should hold are not there."""
        stored, expected = self.held(first_bar, last_bar)
        return max(0, expected - stored)


def _tally(returns: Sequence[float], trade_pnls: Sequence[float], bars_per_year: float) -> Tally:
    """The four statistics over one return series. Zero, never NaN, when there is nothing to say.

    An always-flat strategy has a return series of zeros and no trades; plan
    §6.6 wants that reported as zeros so a report reads "did nothing" rather
    than failing to render. ``sharpe`` is 0 when the deviation is 0 for the
    same reason: a series that never moved has no risk-adjusted anything.
    """
    path = [STARTING_EQUITY]
    for r in returns:
        path.append(path[-1] * (1.0 + r))
    total = path[-1] / STARTING_EQUITY - 1.0
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.0
    sharpe = (
        0.0 if deviation == 0 else statistics.fmean(returns) / deviation * math.sqrt(bars_per_year)
    )
    peak, drawdown = path[0], 0.0
    for value in path[1:]:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak - value) / peak if peak > 0 else 0.0)
    hit = sum(1 for pnl in trade_pnls if pnl > 0) / len(trade_pnls) if trade_pnls else 0.0
    return Tally(total_return=total, sharpe=sharpe, max_drawdown=drawdown, hit_rate=hit)


def _regime_buckets(
    frame: FeatureFrame, first: int, stop: int, net_returns: Sequence[float]
) -> tuple[RegimeBucket, ...]:
    """Net return per regime label over the window, in the label's declared order.

    The regime column is asked for only when the bundle is long enough for
    the engine to have warmed up somewhere in it. Shorter than that, every
    bar is ``None`` by warm-up and the frame would refuse the column as one
    it cannot answer — which is the right refusal for a SPEC feature and the
    wrong one for a report dimension; here those bars are simply unlabelled.

    Asking for the column is what makes a frame pay the indicator walk
    whether or not the spec reads an indicator (about 12 s for 5000 bars on
    this box, once per frame — an experiment shares one). A report dimension
    that appeared only for the specs that happened to warm it would not be a
    dimension; the cost is stated in the README's trade-offs instead.
    """
    if len(frame.bundle.bars) >= _REGIME_WARMUP:
        labels = frame.series(_REGIME_REF)
    else:
        labels = (None,) * len(frame.bundle.bars)
    bars: Counter[str] = Counter()
    totals: defaultdict[str, float] = defaultdict(float)
    for index, net in zip(range(first, stop), net_returns, strict=True):
        label = labels[index]
        key = UNLABELLED if label is None else label.value
        bars[key] += 1
        totals[key] += net
    order = [regime.value for regime in MarketRegime] + [UNLABELLED]
    return tuple(
        RegimeBucket(label=key, bars=bars[key], net_return=totals[key])
        for key in order
        if key in bars
    )


# -- fitness of the window ----------------------------------------------------


def _require_measurable(
    spec: StrategySpec,
    frame: FeatureFrame,
    segment: Segment,
    first: int,
    stop: int,
    step: int,
    settlements: _Settlements,
) -> None:
    """Refuse a window this history cannot honestly score, naming what is missing.

    Plan §3.4: never evaluate across a hole. And plan §10.2: the window is
    the measured span, so a store that covers only part of it — or a feature
    still warming up at its first bar — is a strategy scored over less than
    the span the report prints. Refused, with the bar it first has a value
    at, so the operator can move the window or fetch older history.
    """
    bars = frame.bundle.bars
    if stop - first < 2:
        raise EvaluationError(
            f"{segment} holds {stop - first} of this bundle's bars; a window needs at least "
            f"two — a decision, and the bar it fills in"
        )
    stamps = [bar.open_time for bar in bars[first:stop]]
    missing_edges = []
    if stamps[0] != segment.start_ms:
        missing_edges.append(f"begins at {from_epoch_ms(stamps[0]).isoformat()}")
    if stamps[-1] + step != segment.end_ms:
        missing_edges.append(f"ends at {from_epoch_ms(stamps[-1] + step).isoformat()}")
    if missing_edges:
        raise EvaluationError(
            f"{segment}: the stored history {' and '.join(missing_edges)}, so the window "
            f"would be measured over fewer bars than it names. Fetch the missing span, or "
            f"cut the split to the history the store has."
        )
    # The gap scanner's verdict, not a second definition of a hole: ``gaps``
    # and this refusal have to agree about the same store.
    report = scan_stamps(str(segment), step, CANDLE_STAMP_TOLERANCE_MS, stamps)
    if report.duplicate_ms or report.misaligned_ms:
        # Named before any hole: a stamp the scanner could not place on the
        # grid leaves its slot empty, so the same series also reads as having
        # a hole there, and the hole is the consequence rather than the fact.
        raise EvaluationError(
            f"{segment} is not on the {step} ms grid ({len(report.duplicate_ms)} duplicate "
            f"slot(s), {len(report.misaligned_ms)} off-grid stamp(s)); a re-fetch does not "
            f"repair this — run `gaps` to see which stamps."
        )
    if report.gaps:
        gap = report.gaps[0]
        raise EvaluationError(
            f"{segment} has a hole: {from_epoch_ms(gap.after_ms).isoformat()} is followed by "
            f"{from_epoch_ms(gap.before_ms).isoformat()}, {gap.missing} bars missing. Read as "
            f"a grid, a hole is one enormous return between two adjacent-looking bars — run "
            f"`gaps`, then `fetch` the window."
        )
    if not frame.bundle.funding:
        raise EvaluationError(
            "the cost model settles funding hourly and this bundle has no settlements — "
            "fetch without --skip-funding before measuring on it"
        )
    stored, expected = settlements.held(bars[first], bars[stop - 1])
    if not window_is_covered(stored, expected):
        raise EvaluationError(
            f"{segment} should hold {expected} hourly funding settlements and the store has "
            f"{stored}; the carry of a position held through those hours would be "
            f"understated without looking wrong. Run `gaps`, then `fetch` the window."
        )
    for ref in spec.features:
        # The column once (its guard fires once), then a scan bounded by the
        # window: the message says where inside the window the feature wakes
        # up, and reads nothing past it.
        column = frame.series(ref)
        ready = next(
            (
                i
                for i in range(first, stop)
                if i - ref.offset >= 0 and column[i - ref.offset] is not None
            ),
            None,
        )
        if ready == first:
            continue
        when = (
            f"it first has one at {from_epoch_ms(bars[ready].open_time).isoformat()} "
            f"({ready - first} bars in)"
            if ready is not None
            else "and has none anywhere inside the window either"
        )
        raise EvaluationError(
            f"{ref} has no value at the first bar of {segment} "
            f"({from_epoch_ms(bars[first].open_time).isoformat()}); {when}. The window is "
            f"the measured span, so a rule that cannot fire at its start would be scored over "
            f"bars it never saw — start the window later, or fetch older history."
        )


# -- over a whole split ----------------------------------------------------------


def evaluate_split(
    spec: StrategySpec,
    frame: FeatureFrame,
    split: Split,
    costs: CostModel,
    *,
    holdout: bool = False,
) -> SplitResult:
    """Score ``spec`` on train and validation — and on the holdout only when told to.

    The keyword is the lock (plan §3.8, §6.5): a caller that has not
    promoted a trial does not pass it, and the result then carries no holdout
    figure to print, average, or rank on. PR A4's ledger owns WHO may pass it.
    ``Split.segments`` is what withholds the window; this unpacks whatever it
    handed over.
    """
    train, validation, *rest = [
        evaluate_segment(spec, frame, segment, costs, interval=split.interval)
        for segment in split.segments(holdout=holdout)
    ]
    return SplitResult(
        spec=spec,
        costs=costs,
        split=split,
        indicator_lookback=frame.indicator_lookback,
        train=train,
        validation=validation,
        holdout=rest[0] if rest else None,
    )


def load_bundle(
    store: ResearchStore, *, coin: str, interval: str, until_ms: int | None = None
) -> SeriesBundle:
    """The history a split is measured on, read from the store — and no further than ``until_ms``.

    ``until_ms`` is an ``open_time`` bound on the decision bars (what
    :meth:`Split.loadable_until` hands over). The daily backdrop and the
    settlements are read to the CLOSE of the last bar that bound admitted —
    taken from that bar, not derived from the bound, since the bound may sit
    anywhere between two opens — because that close is the instant its
    features are aligned at. A daily bar is kept only if its OWN close has
    been reached — filtered on that stamp, not derived from its open, since a
    venue close is a millisecond short of the next open — so a day that
    closes inside the next window is not held even though it opened inside
    this one. The settlements are read up to the one DUE at that close,
    which the venue posts a few ms after it, so the bound carries the posting
    jitter; without it every bounded bundle read as missing its last
    settlement. That settlement is a fact about the close just reached; the
    one after it is a fact about the next window, and is not read. The
    holdout lock is the reason.
    """
    key = studied_interval(interval)
    daily_key = CandleInterval.D1.value
    bars = list(store.iter_candles(coin, key, until_ms=until_ms))
    if not bars:
        raise EvaluationError(f"the store holds no {key} bars for {coin} in that span")
    last_close = bars[-1].close_time
    if key == daily_key:
        # The backdrop IS the decision series: a prefix of what was just
        # read, not a second scan of the same rows.
        daily = bars
    else:
        daily = list(store.iter_candles(coin, daily_key, until_ms=until_ms and last_close))
    if until_ms is not None:
        daily = [bar for bar in daily if bar.close_time <= last_close]
    funding_until = None if until_ms is None else last_close + FUNDING_STAMP_TOLERANCE_MS
    return SeriesBundle(
        bars, daily=daily, funding=list(store.iter_funding(coin, until_ms=funding_until))
    )


# -- rendering -------------------------------------------------------------------


def describe_result(result: SplitResult) -> list[str]:
    """The result as lines to print — what PR A4's ``report`` command will show."""
    lines = list(describe_spec(result.spec))
    lines.append(result.costs.describe())
    lines.append(
        f"indicator window: {result.indicator_lookback} bars; sharpe annualised by "
        f"sqrt({result.train.bars_per_year:.0f} bars/year)"
    )
    lines += result.split.describe()
    for segment_result in result.results:
        lines += segment_result.describe()
    if result.holdout is None:
        lines.append(f"{result.split.holdout.name.value}: withheld (not promoted)")
    return lines


# Import-time check, in the style of the sibling modules: every ordering
# operator the DSL can produce has a comparison here, or a rule would reach a
# trial as a ``KeyError``.
if set(_COMPARE) != set(Op) - {Op.EQ, Op.NE}:
    raise RuntimeError("every ordering operator needs a comparison")
