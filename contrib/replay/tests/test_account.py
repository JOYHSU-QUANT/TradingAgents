"""The simulated account's arithmetic, pinned to numbers derived by hand from small paths.

Every expected figure is the expression a reader can check against the
paper lane's formulas (``runtime/accounting``, ``paper/fill_model``), not a
number the code printed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.replay.account import (
    AccountError,
    Bar,
    Decision,
    Funding,
    Series,
    Target,
    simulate,
)
from contrib.replay.upstream import CostModel, FillRole, SegmentName, TargetSide

from .conftest import STEP_MS, terms

HOUR = 3_600_000
T0 = 1_800_000_000_000 - 1_800_000_000_000 % STEP_MS
TAKER = CostModel()  # 0.045% fee, 5 bps slippage
MAKER = CostModel(fill_role=FillRole.MAKER)  # 0.015% fee


def _simulate(decisions, *, terms, bars, funding):
    return simulate(decisions, terms=terms, series=Series.of(bars, funding))


def decision(
    slot: int,
    mark: str,
    target: Target | None,
    *,
    leverage: str = "1",
    segment: SegmentName | None = SegmentName.TRAIN,
) -> Decision:
    return Decision(
        input_id=f"q{slot}",
        at_ms=T0 + slot * STEP_MS + 5_000,  # five seconds into the bar, as the scheduler lands
        segment=segment,
        mark=Decimal(mark),
        leverage=Decimal(leverage),
        target=target,
    )


def bar(slot: int, close: str, *, high: str | None = None, low: str | None = None) -> Bar:
    c = Decimal(close)
    return Bar(
        open_time=T0 + slot * STEP_MS,
        close_time=T0 + (slot + 1) * STEP_MS - 1,
        high=c + 2 if high is None else Decimal(high),
        low=c - 2 if low is None else Decimal(low),
        close=c,
    )


LONG_50 = Target(TargetSide.LONG, Decimal(50))
FLAT = Target(TargetSide.FLAT, Decimal(0))


# -- taker fills ----------------------------------------------------------------------------


def test_a_taker_open_pays_slippage_and_the_fee_and_marks_to_the_next_question():
    path = _simulate(
        [decision(0, "100", LONG_50), decision(1, "110", None)],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "110")],
        funding=[],
    )
    first, last = path.steps
    # 50% of 1000 at leverage 1 is 500 notional: 5 BTC at mark 100, filled
    # 5 bps above the mark, fee 0.045% of the filled notional.
    assert (first.filled, first.fill_price) == (True, Decimal("100.05"))
    assert first.size == Decimal(5)
    assert first.fee == Decimal(5) * Decimal("100.05") * Decimal("0.00045")
    assert first.equity == Decimal(1000)  # read before the order
    assert first.wallet == Decimal(1000) - first.fee
    # Next question: the 5 BTC bought at 100.05 are marked at 110.
    assert last.equity == first.wallet + Decimal(5) * (Decimal(110) - Decimal("100.05"))
    assert first.ret == pytest.approx(float(last.equity / Decimal(1000)) - 1.0)
    assert last.ret is None  # the last question has no later mark
    assert path.total_return == pytest.approx(float(last.equity / 1000) - 1.0)
    assert (path.orders, path.fills, path.unfilled, path.ruined) == (1, 1, 0, False)


def test_a_reduce_realises_pnl_and_a_flat_target_closes_the_book():
    path = _simulate(
        [
            decision(0, "100", LONG_50),
            decision(1, "120", FLAT),
            decision(2, "130", None),
        ],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "120"), bar(2, "130")],
        funding=[],
    )
    opened, closed, after = path.steps
    # Sold 5 BTC at 120 less 5 bps: realised 5 x (119.94 - 100.05), fee on the sale.
    assert (closed.filled, closed.fill_price, closed.size, closed.entry) == (
        True,
        Decimal("119.94"),
        Decimal(0),
        None,
    )
    realised = Decimal(5) * (Decimal("119.94") - Decimal("100.05"))
    assert closed.fee == Decimal(5) * Decimal("119.94") * Decimal("0.00045")
    assert closed.wallet == opened.wallet + realised - closed.fee
    # Flat from here: the equity no longer moves with the mark, and the
    # closing decision's return is what its own fill cost (slippage and fee).
    assert after.equity == closed.wallet
    assert closed.ret == pytest.approx(float(after.equity / closed.equity) - 1.0)
    assert closed.ret < 0


def test_a_repeated_target_re_sizes_to_equity_the_way_the_gate_does():
    path = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", None), decision(2, "100", LONG_50)],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "100"), bar(2, "100")],
        funding=[],
    )
    # Equity fell by the first fee, so 50% of it is a sliver less than the
    # 5 BTC held: the gate's sizing makes that a (tiny) sell.
    held = path.steps[2]
    assert held.filled is True and held.size < Decimal(5)
    assert path.steps[1].filled is None  # nothing asked, nothing resting


# -- maker posts ----------------------------------------------------------------------------


def test_a_maker_post_fills_only_when_the_bar_trades_through_it_by_a_tick():
    # A buy posts at 100 x (1 - 0.00005) = 99.995, floored to the 1 tick: 99.
    # It fills when the bar's low reaches 99 - 1 = 98.
    untouched = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", None)],
        terms=terms(MAKER),
        bars=[bar(0, "100", low="99"), bar(1, "100", low="99")],
        funding=[],
    )
    assert [s.filled for s in untouched.steps] == [False, False]
    assert [s.carried for s in untouched.steps] == [False, True]  # the post rests, retried
    assert untouched.unfilled == 2 and untouched.fills == 0
    assert untouched.orders == 1  # one order, tried twice
    reached = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", None)],
        terms=terms(MAKER),
        bars=[bar(0, "100", low="98"), bar(1, "100")],
        funding=[],
    )
    first = reached.steps[0]
    assert (first.filled, first.fill_price, first.size) == (True, Decimal(99), Decimal(5))
    assert first.fee == Decimal(5) * Decimal(99) * Decimal("0.00015")
    assert reached.steps[1].filled is None  # nothing resting any more


def test_a_new_target_replaces_a_resting_post():
    path = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", FLAT), decision(2, "100", None)],
        terms=terms(MAKER),
        bars=[bar(0, "100", low="99"), bar(1, "100"), bar(2, "100")],
        funding=[],
    )
    assert path.steps[0].filled is False
    # Flat over a flat book: the new target asks nothing, and the old post is gone.
    assert path.steps[1].target == FLAT and path.steps[1].filled is None
    assert path.steps[2].target is None and path.steps[2].carried is False


def test_a_post_is_judged_against_the_bar_the_split_places_the_question_in():
    on_the_edge = Decision("edge", T0 + STEP_MS - 1, None, Decimal(100), Decimal(1), LONG_50)
    # The last millisecond of bar 0: bar 0 (low 98) decides, not bar 1 (low 99).
    path = _simulate(
        [on_the_edge],
        terms=terms(MAKER),
        bars=[bar(0, "100", low="98"), bar(1, "100", low="99")],
        funding=[],
    )
    assert path.steps[0].filled is True
    # A decision before any stored bar opens is judged against the first to open.
    early = Decision("early", T0 - 5_000, None, Decimal(100), Decimal(1), LONG_50)
    path = _simulate([early], terms=terms(MAKER), bars=[bar(0, "100", low="99")], funding=[])
    assert path.steps[0].filled is False


def test_a_maker_run_without_the_bar_is_refused_by_name():
    with pytest.raises(AccountError, match="q0: no bar in the research store covers"):
        _simulate([decision(0, "100", LONG_50)], terms=terms(MAKER), bars=[], funding=[])


# -- funding --------------------------------------------------------------------------------


def test_funding_is_settled_hourly_on_the_last_close_and_missing_hours_are_counted():
    # Long 5 BTC from question 0; four hourly settlements fall in (q0, q1],
    # all before bar 0 closes, so each is valued at the mark the position
    # was opened at (no bar has closed yet).
    stamps = [T0 + h * HOUR + 7 for h in range(1, 5)]  # ms after the hour, as the venue posts
    path = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", None)],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "100")],
        funding=[Funding(t, Decimal("0.0001")) for t in stamps],
    )
    paid = path.steps[1].funding
    # funding_pnl = -(signed notional x rate): a long pays at a positive rate.
    assert paid == -(Decimal(5) * Decimal(100) * Decimal("0.0001")) * 4
    assert path.steps[1].funding_missing == 0
    assert path.steps[1].wallet == path.steps[0].wallet + paid
    short = _simulate(
        [decision(0, "100", LONG_50), decision(1, "100", None)],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "100")],
        funding=[Funding(stamps[0], Decimal("0.0001"))],
    )
    assert short.steps[1].funding_missing == 3  # four expected, one stored
    assert short.funding_missing == 3
    assert "3 funding settlement(s) missing" in short.describe()[0]


def test_funding_values_the_position_at_the_last_close_once_a_bar_has_closed():
    # The settlement after bar 0 closed (at 120) is valued at 120, not at the mark of 100.
    stamp = T0 + STEP_MS + HOUR + 7
    path = _simulate(
        [decision(0, "100", LONG_50), decision(2, "120", None)],
        terms=terms(),
        bars=[bar(0, "120"), bar(1, "120"), bar(2, "120")],
        funding=[Funding(stamp, Decimal("0.0001"))],
    )
    assert path.steps[1].funding == -(Decimal(5) * Decimal(120) * Decimal("0.0001"))
    assert path.steps[1].funding_missing == 7  # eight hours between the two questions


def test_a_flat_book_owes_no_funding_and_counts_nothing_missing():
    path = _simulate(
        [decision(0, "100", None), decision(1, "100", None)],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "100")],
        funding=[],
    )
    assert (path.steps[1].funding, path.steps[1].funding_missing) == (Decimal(0), 0)


# -- ruin and refusals ----------------------------------------------------------------------


def test_a_blown_account_stops_the_path_and_says_so():
    # Short the whole equity at leverage 10 at 100: 100 BTC; marked at 130
    # the unrealised loss is 100 x 30 = 3000 on a 1000 account.
    path = _simulate(
        [
            decision(0, "100", Target(TargetSide.SHORT, Decimal(100)), leverage="10"),
            decision(1, "130", None),
            decision(2, "140", None),
        ],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "130"), bar(2, "140")],
        funding=[],
    )
    assert path.ruined
    assert len(path.steps) == 2  # the path stopped at the question that found it empty
    assert path.steps[1].equity <= 0
    assert path.steps[0].ret is not None and path.steps[0].ret < -1.0
    assert "RUINED" in path.describe()[1]


def test_returns_can_be_read_per_segment():
    path = _simulate(
        [
            decision(0, "100", LONG_50, segment=SegmentName.TRAIN),
            decision(1, "110", None, segment=SegmentName.VALIDATION),
            decision(2, "120", None, segment=SegmentName.VALIDATION),
        ],
        terms=terms(),
        bars=[bar(0, "100"), bar(1, "110"), bar(2, "120")],
        funding=[],
    )
    assert len(path.returns()) == 2
    assert len(path.returns(SegmentName.TRAIN)) == 1
    assert len(path.returns(SegmentName.VALIDATION)) == 1
    assert path.returns(SegmentName.HOLDOUT) == []


@pytest.mark.parametrize(
    ("decisions", "message"),
    [
        ([decision(0, "100", None), decision(0, "100", None)], "decided twice"),
        (
            [
                decision(0, "100", None),
                Decision("other", T0 + 5_000, None, Decimal(100), Decimal(1), None),
            ],
            "decided at the same instant",
        ),
    ],
)
def test_a_path_the_account_cannot_walk_is_refused(decisions, message):
    with pytest.raises(AccountError, match=message):
        _simulate(decisions, terms=terms(), bars=[], funding=[])


def test_the_records_hold_their_own_invariants():
    with pytest.raises(AccountError, match="flat carries none"):
        Target(TargetSide.FLAT, Decimal(10))
    with pytest.raises(AccountError, match="flat carries none"):
        Target(TargetSide.LONG, Decimal(0))
    with pytest.raises(AccountError, match="low 101 <= close 100"):
        Bar(T0, T0 + 1, Decimal(102), Decimal(101), Decimal(100))
    with pytest.raises(AccountError, match="tick must be > 0"):
        terms(tick="0")
    with pytest.raises(AccountError, match="mark must be > 0"):
        decision(0, "0", None)


def test_the_terms_describe_the_fill_model_in_one_line():
    assert (
        terms()
        .describe()
        .startswith(
            "account: opens at 1000 USDC; taker fills at 0.00045 fee, filled at mark -/+ 5 bps"
        )
    )
    assert "posted at mark -/+ 0.5 bps on a 1 tick" in terms(MAKER).describe()
    assert "no stop-loss, no take-profit" in terms().describe()
