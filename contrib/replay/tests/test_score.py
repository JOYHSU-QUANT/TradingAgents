"""The scorecard's arithmetic, pinned to numbers derived by hand from the fixture table.

Every expected value below is written as the expression a reader can check
against ``conftest.ROWS`` — the marks, margins and fees it is built from —
rather than as the number the code happened to print.
"""

from __future__ import annotations

import statistics

import pytest

from contrib.replay.score import (
    Answer,
    HitStats,
    Outcome,
    Question,
    ScoreError,
    bar_open_ms,
    build_split,
    csv_table,
    paired_hits,
    score_run,
    sign_test,
)
from contrib.replay.upstream import CostModel, FillRole, SegmentName, Split, TargetSide

from .conftest import (
    ANCHOR_MS,
    RESEARCH_CLOSES,
    STEP_MS,
    at_ms,
    fixture_answers,
    fixture_questions,
    input_id,
)

TAKER = CostModel()  # the paper run's own: 0.045% taker fee, 5 bps slippage
MAKER = CostModel(fill_role=FillRole.MAKER)  # 0.015% maker fee, the same slippage
TAKER_COST = 0.00045 + 0.0005  # per unit of turnover, as a fraction of equity
MAKER_COST = 0.00015 + 0.0005

# The one-bar return of each answered row: later mark over this row's mark.
NEXT_RETURN = {
    0: 101 / 100 - 1,
    1: 99 / 101 - 1,
    2: 102 / 99 - 1,
    3: 104 / 102 - 1,  # 104 is the research store's close for the missing slot 4
    5: 105 / 103 - 1,
    6: 104 / 105 - 1,
    7: 106 / 104 - 1,
    8: 108 / 106 - 1,
    9: 107 / 108 - 1,
    10: 109 / 107 - 1,
}
# The median of the ten absolute one-bar moves: the 5th and 6th sorted are
# rows 8 and 7.
FLAT_BAND = (abs(NEXT_RETURN[8]) + abs(NEXT_RETURN[7])) / 2


def _card(costs: CostModel = TAKER, research=RESEARCH_CLOSES, **kwargs):
    return score_run(
        fixture_questions(),
        fixture_answers(),
        step_ms=STEP_MS,
        costs=costs,
        research_closes=research,
        **kwargs,
    )


def _row(card, slot: int):
    return next(row for row in card.rows if row.question.input_id == input_id(slot))


# -- bars and the split ----------------------------------------------------


def test_bar_open_ms_floors_an_instant_onto_the_grid():
    assert bar_open_ms(at_ms(3), STEP_MS) == ANCHOR_MS + 2 * STEP_MS  # 1 ms before the boundary
    assert bar_open_ms(ANCHOR_MS + 3 * STEP_MS, STEP_MS) == ANCHOR_MS + 3 * STEP_MS  # on it
    assert bar_open_ms(ANCHOR_MS + 3 * STEP_MS + 1, STEP_MS) == ANCHOR_MS + 3 * STEP_MS


# -- pairing by the decision instant --------------------------------------------


def test_a_cycle_that_drifted_across_a_bar_boundary_still_pairs_with_the_next_decision():
    """Run 3 on 2026-09-23: decisions at 03:53 and 08:01 — one bar apart, two closed bars apart.

    Paired on the closed bars' stamps the second decision would have sat two
    bars on and the research close at the 04:00 boundary would have been
    served as the "4h" mark of a decision made seven minutes earlier.
    """
    minute = 60_000
    t0 = ANCHOR_MS + 233 * minute  # 03:53 past a boundary
    t1 = t0 + STEP_MS + 8 * minute  # 08:01: 4h08m later, across the next boundary
    t2 = t1 + STEP_MS
    questions = [
        _question(input_id="a", at_ms=t0, mark=100.0),
        _question(input_id="b", at_ms=t1, mark=101.0),
        _question(input_id="c", at_ms=t2, mark=102.0),
    ]
    boundary_close = {ANCHOR_MS + STEP_MS - 1: 999.0}  # the 04:00 close, 7 minutes after t0
    card = score_run(questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=boundary_close)
    first = card.rows[0].outcomes[1]
    assert (first.later_mark, first.source, first.ret) == (101.0, "store", pytest.approx(0.01))
    assert card.rows[1].outcomes[1].later_mark == 102.0
    # And a decision more than half a bar past the target is not "one bar on".
    late = [questions[0], _question(input_id="b", at_ms=t0 + STEP_MS + STEP_MS // 2 + 1, mark=101.0)]
    assert score_run(late, [], step_ms=STEP_MS, costs=TAKER).rows[0].outcomes[1].later_mark is None


def test_a_gap_is_filled_by_the_nearest_close_within_half_a_bar_or_not_at_all():
    questions = [_question(input_id="a", at_ms=at_ms(0)), _question(input_id="b", at_ms=at_ms(2), mark=102.0)]
    target = at_ms(0) + STEP_MS
    near = {target + STEP_MS // 2: 101.0}  # exactly half a bar away: still paired
    far = {target + STEP_MS // 2 + 1: 101.0}  # one millisecond further: not
    assert score_run(questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=near).rows[0].outcomes[1].source == "research"
    assert score_run(questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=far).rows[0].outcomes[1].source is None


def test_two_questions_within_the_tolerance_are_refused_as_unpairable():
    """Refused at exactly half a bar apart (the pairing is inclusive too), accepted one ms further."""
    at_half = [_question(input_id="a", at_ms=at_ms(0)), _question(input_id="b", at_ms=at_ms(0) + STEP_MS // 2)]
    with pytest.raises(ScoreError, match="a and b: two questions within 2h"):
        score_run(at_half, [], step_ms=STEP_MS, costs=TAKER)
    past_half = [at_half[0], _question(input_id="b", at_ms=at_ms(0) + STEP_MS // 2 + 1)]
    assert len(score_run(past_half, [], step_ms=STEP_MS, costs=TAKER).rows) == 2
    with pytest.raises(ScoreError, match="tolerance_ms"):
        score_run(past_half, [], step_ms=STEP_MS, costs=TAKER, tolerance_ms=STEP_MS)


def test_the_lock_applies_to_the_candidate_chosen_and_never_falls_through():
    """A store question inside the tolerance but past the bound is 'unavailable', not the close beside it.

    Twelve bars cut 7 / 3 / 2, so the locked bound is the last validation
    bar's close, B. The question ``q`` (one hour into the last validation
    bar) wants its one-bar mark at B + 1h; the holdout question ``h`` sits
    30 minutes past that target and a research close sits at B, an hour
    before it. ``h`` is the nearer candidate and is past the bound, so the
    answer is "unavailable" — a lock applied per series would have served
    the close, which is inside the bound.
    """
    hour = 3_600_000
    bound = ANCHOR_MS + 9 * STEP_MS - 1
    split = Split.by_shares("4h", start_ms=ANCHOR_MS - STEP_MS, end_ms=ANCHOR_MS + 11 * STEP_MS)
    assert split.loadable_until() == bound
    questions = [
        *(_question(input_id=f"s{i}", at_ms=at_ms(i), mark=100.0 + i) for i in range(8)),
        _question(input_id="q", at_ms=bound - STEP_MS + hour, mark=100.0),
        _question(input_id="h", at_ms=bound + hour + hour // 2, mark=110.0),
        _question(input_id="s11", at_ms=at_ms(11), mark=111.0),
    ]
    at_bound = {bound: 555.0}
    locked = score_run(questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=at_bound, split=split)
    assert [r.question.input_id for r in locked.rows][-1] == "q"
    assert locked.rows[-1].outcomes[1].later_mark is None
    opened = score_run(
        questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=at_bound, split=split, holdout=True
    )
    assert next(r for r in opened.rows if r.question.input_id == "q").outcomes[1].later_mark == 110.0


def test_the_nearer_of_two_research_closes_is_served():
    questions = [_question(input_id="a", at_ms=at_ms(0)), _question(input_id="b", at_ms=at_ms(3), mark=103.0)]
    target = at_ms(0) + STEP_MS
    closes = {target - 90 * 60_000: 111.0, target + 30 * 60_000: 222.0}
    assert score_run(questions, [], step_ms=STEP_MS, costs=TAKER, research_closes=closes).rows[0].outcomes[1].later_mark == 222.0


# -- the two readings ---------------------------------------------------------


@pytest.mark.parametrize(
    ("slot", "ai_side", "ai_exposure", "executed_side", "executed_exposure", "flip"),
    [
        (0, "long", 0.3, "long", 0.3, False),  # approved target from flat
        (1, "long", 0.3, "long", 0.3, False),  # maintain: the position's own side
        (2, "short", -0.4, "short", -0.2, True),  # clamped 40 -> 20, long -> short
        (3, None, None, "short", -0.2, False),  # fail-closed: no call, position unchanged
        (5, "flat", 0.0, "flat", 0.0, False),  # flat target, closed the short
        (6, "long", 0.3, "flat", 0.0, False),  # rejected: the model asked, nothing moved
        (7, "long", 0.3, "long", 0.28, False),  # approved inside the deadband: no order
        (8, None, None, "long", 0.28, False),  # truncated: fail-closed
        (9, "short", -1.0, "short", -1.0, True),  # 50% at leverage 2, long -> short
        (10, "short", -0.5, "short", -0.5, False),  # maintain a short
        (11, None, None, "short", -0.5, False),  # unanswered
    ],
)
def test_each_row_is_read_from_the_model_and_from_the_rule(
    slot, ai_side, ai_exposure, executed_side, executed_exposure, flip
):
    row = _row(_card(), slot)
    assert (row.ai_side, row.ai_exposure) == (
        None if ai_side is None else TargetSide(ai_side),
        ai_exposure,
    )
    assert (row.executed_side, row.executed_exposure) == (
        TargetSide(executed_side),
        executed_exposure,
    )
    assert row.flip is flip
    assert row.answered is (slot != 11)


# -- one bar on ------------------------------------------------------------


@pytest.mark.parametrize(
    ("slot", "ai_hit", "executed_hit"),
    [
        (0, True, True),
        (1, False, False),
        (2, False, False),
        (3, None, False),
        (5, False, False),  # flat, and the move (1.94%) is just over the band
        (6, False, True),  # the model's long missed; the unchanged flat position hit
        (7, True, True),
        (8, None, True),
        (9, True, True),
        (10, False, False),
    ],
)
def test_the_one_bar_return_and_both_hits_per_row(slot, ai_hit, executed_hit):
    outcome = _row(_card(), slot).outcome(1)
    assert outcome.ret == pytest.approx(NEXT_RETURN[slot])
    assert (outcome.ai_hit, outcome.executed_hit) == (ai_hit, executed_hit)


def test_the_flat_band_is_the_median_absolute_one_bar_move():
    card = _card()
    assert card.flat_bands[1] == pytest.approx(FLAT_BAND)
    assert abs(NEXT_RETURN[5]) > FLAT_BAND > abs(NEXT_RETURN[6])


def test_the_missing_cycle_is_marked_from_the_research_close():
    with_research = _row(_card(), 3).outcome(1)
    assert (with_research.later_mark, with_research.source) == (104.0, "research")
    without = _row(_card(research=None), 3).outcome(1)
    assert (without.later_mark, without.source, without.ret) == (None, None, None)
    assert _card().summary().horizons[0].marks_from_research == 1


def test_the_last_row_has_no_later_mark():
    assert _row(_card(), 11).outcome(1) == Outcome(1, None, None, None, None, None, None, None)


def test_a_rejection_is_read_as_the_side_the_gate_refused():
    """The store's shape for a rejection: maintain_current with the refused target kept."""
    rejected = Answer(
        input_id="q",
        decision_mode="maintain_current",
        target_side="short",
        requested_margin_pct=40.0,
        approved_margin_pct=None,
        risk_action="rejected",
        risk_reason="low_confidence",
        confidence=0.25,
        order_created=False,
        no_order_reason="rejected",
    )
    assert rejected.asked_target
    question = _question(current_side="long", current_margin_pct=30.0)
    card = score_run([question, _question(input_id="next", at_ms=at_ms(1), mark=90.0)], [rejected], step_ms=STEP_MS, costs=TAKER)
    row = card.rows[0]
    assert (row.ai_side, row.ai_exposure) == (TargetSide.SHORT, -0.4)
    assert (row.executed_side, row.executed_exposure) == (TargetSide.LONG, 0.3)
    outcome = row.outcome(1)
    assert (outcome.ai_hit, outcome.executed_hit) == (True, False)
    assert outcome.ai_pnl == pytest.approx(-0.4 * (90 / 100 - 1) - 0.7 * TAKER_COST)
    summary = card.summary()
    assert (summary.set_target, summary.rejected, summary.maintain_current) == (1, 1, 0)
    assert dict(summary.horizons[0].two_by_two) == {"both": 0, "ai_only": 1, "rule_only": 0, "neither": 0}
    assert [(b.bucket, b.hits.n, b.hits.hits) for b in summary.horizons[0].calibration] == [(2, 1, 1)]


def test_a_zero_move_hits_nothing_but_flat_and_the_flat_band_is_strict():
    """``long`` needs a rise, ``short`` a fall; ``flat`` needs a move strictly under the band."""
    questions = [
        _question(input_id="a", at_ms=at_ms(0), mark=100.0, current_side="flat", current_margin_pct=0.0),
        _question(input_id="b", at_ms=at_ms(1), mark=100.0, current_side="flat", current_margin_pct=0.0),
        _question(input_id="c", at_ms=at_ms(2), mark=101.0, current_side="flat", current_margin_pct=0.0),
        _question(input_id="d", at_ms=at_ms(3), mark=103.0, current_side="flat", current_margin_pct=0.0),
    ]
    # One-bar moves: 0, +1%, +1.98% -> band = median = 1%.
    answers = [
        _answer(input_id="a", target_side="long"),
        _answer(input_id="b", target_side="flat", requested_margin_pct=0.0, approved_margin_pct=0.0),
        _answer(input_id="c", target_side="short"),
    ]
    card = score_run(questions, answers, step_ms=STEP_MS, costs=TAKER)
    assert card.flat_bands[1] == pytest.approx(101 / 100 - 1)
    by_id = {row.question.input_id: row.outcome(1) for row in card.rows}
    assert by_id["a"].ai_hit is False  # a zero move is not a rise
    assert by_id["b"].ai_hit is False  # a move exactly on the band is not under it
    assert by_id["c"].ai_hit is False
    # ... nor is a zero move a fall.
    short_on_flat = score_run(
        questions[:2], [_answer(input_id="a", target_side="short")], step_ms=STEP_MS, costs=TAKER
    )
    assert short_on_flat.rows[0].outcome(1).ai_hit is False


def test_a_horizon_no_question_reaches_has_no_band_and_judges_no_flat_call():
    questions = [
        _question(input_id="a", at_ms=at_ms(0), current_side="flat", current_margin_pct=0.0),
        _question(input_id="b", at_ms=at_ms(1), mark=101.0, current_side="flat", current_margin_pct=0.0),
    ]
    flat = _answer(input_id="a", target_side="flat", requested_margin_pct=0.0, approved_margin_pct=0.0)
    card = score_run(questions, [flat], step_ms=STEP_MS, costs=TAKER)
    assert card.flat_bands[6] is None
    assert card.rows[0].outcome(6).ai_hit is None  # no later mark at 24h either
    lines = card.summary().describe(card)
    assert "  flat band n/a; answered rows marked from the research store: 0" in lines


def test_the_top_confidence_bucket_is_closed_at_one():
    card = score_run(
        [_question(), _question(input_id="next", at_ms=at_ms(1), mark=101.0)],
        [_answer(confidence=1.0)],
        step_ms=STEP_MS,
        costs=TAKER,
    )
    (bucket,) = card.summary().horizons[0].calibration
    assert (bucket.bucket, bucket.hits, bucket.label) == (9, HitStats(1, 1), "[0.9, 1.0]")


# -- P&L ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("slot", "ai_pnl", "executed_pnl"),
    [
        # 30% from flat: turnover 0.3 on both readings.
        (0, 0.3 * NEXT_RETURN[0] - 0.3 * TAKER_COST, 0.3 * NEXT_RETURN[0] - 0.3 * TAKER_COST),
        # maintain: no turnover, no cost.
        (1, 0.3 * NEXT_RETURN[1], 0.3 * NEXT_RETURN[1]),
        # asked -40 from +30 (turnover 0.7); got -20 (turnover 0.5).
        (2, -0.4 * NEXT_RETURN[2] - 0.7 * TAKER_COST, -0.2 * NEXT_RETURN[2] - 0.5 * TAKER_COST),
        # fail-closed: the position rides on, uncharged.
        (3, None, -0.2 * NEXT_RETURN[3]),
        # flat target closes a 20% short: turnover 0.2, no exposure.
        (5, -0.2 * TAKER_COST, -0.2 * TAKER_COST),
        # rejected: the model's 30% would have paid its entry; nothing moved.
        (6, 0.3 * NEXT_RETURN[6] - 0.3 * TAKER_COST, 0.0),
        # inside the deadband: the model's 30% vs the 28% held, no order.
        (7, 0.3 * NEXT_RETURN[7] - 0.02 * TAKER_COST, 0.28 * NEXT_RETURN[7]),
        (8, None, 0.28 * NEXT_RETURN[8]),
        # leverage 2: -50% margin is -1.0 exposure, from +0.56 (28% x 2).
        (9, -1.0 * NEXT_RETURN[9] - 1.56 * TAKER_COST, -1.0 * NEXT_RETURN[9] - 1.56 * TAKER_COST),
        (10, -0.5 * NEXT_RETURN[10], -0.5 * NEXT_RETURN[10]),
    ],
)
def test_pnl_is_exposure_times_return_less_the_turnover_cost(slot, ai_pnl, executed_pnl):
    outcome = _row(_card(), slot).outcome(1)
    assert outcome.ai_pnl == (None if ai_pnl is None else pytest.approx(ai_pnl))
    assert outcome.executed_pnl == pytest.approx(executed_pnl)


def test_maker_costs_change_the_fee_and_nothing_else():
    taker, maker = _card(TAKER), _card(MAKER)
    assert _row(maker, 0).outcome(1).executed_pnl == pytest.approx(
        0.3 * NEXT_RETURN[0] - 0.3 * MAKER_COST
    )
    assert _row(maker, 0).outcome(1).executed_pnl > _row(taker, 0).outcome(1).executed_pnl
    # No turnover, no fee: the maintain scores the same under both.
    assert _row(maker, 1).outcome(1).executed_pnl == _row(taker, 1).outcome(1).executed_pnl


# -- six bars on -----------------------------------------------------------


def test_six_bars_on_reads_the_mark_six_bars_later_or_nothing():
    card = _card()
    first = _row(card, 0).outcome(6)
    assert (first.later_mark, first.ret, first.ai_hit) == (105.0, pytest.approx(105 / 100 - 1), True)
    assert _row(card, 3).outcome(6).later_mark == 108.0  # slot 9
    assert _row(card, 5).outcome(6).later_mark == 109.0  # slot 11
    assert _row(card, 6).outcome(6).later_mark is None  # slot 12 is past the run
    assert card.horizon_label(1) == "4h" and card.horizon_label(6) == "24h"


def test_the_six_bar_hits_and_the_band_at_that_horizon():
    card = _card()
    # Five rows reach six bars on: 0 (+5%), 1 (+2.97%), 2 (+7.07%), 3 (+5.88%), 5 (+5.83%).
    # The median is row 5's own move, so its flat call sits ON the band and misses.
    assert card.flat_bands[6] == pytest.approx(109 / 103 - 1)
    assert _row(card, 5).outcome(6).ai_hit is False
    h = card.summary().horizons[1]
    assert (h.bars, h.label) == (6, "24h")
    assert h.executed == HitStats(5, 2)  # rows 0 and 1 (long) hit; 2, 3 (short) and 5 (flat) miss
    assert h.ai == HitStats(4, 2)
    assert dict(h.executed_by_mode) == {
        "set_target": HitStats(3, 1),
        "maintain_current": HitStats(1, 1),
        "fail_closed": HitStats(1, 0),
    }
    assert dict(h.two_by_two) == {"both": 2, "ai_only": 0, "rule_only": 0, "neither": 2}
    assert h.executed_pnl.overlapping and not card.summary().horizons[0].executed_pnl.overlapping
    assert str(h.executed_pnl).endswith("(overlapping, ranking only)")


# -- summary ----------------------------------------------------------------------


def test_the_summary_counts_the_fixture():
    s = _card().summary()
    assert (s.questions, s.answered, s.unanswered) == (11, 10, 1)
    assert (s.fail_closed, dict(s.fail_closed_by_reason)) == (
        2,
        {"invalid_output": 1, "truncated_output": 1},
    )
    assert (s.clamped, s.rejected, s.flips) == (1, 1, 2)
    assert (s.set_target, s.maintain_current) == (6, 2)
    assert s.reports_present is None
    assert dict(s.segments) == {}


def test_the_one_bar_hit_rates_and_the_two_by_two():
    h = _card().summary().horizons[0]
    assert (h.bars, h.label) == (1, "4h")
    assert h.executed == HitStats(10, 5)
    assert h.ai == HitStats(8, 3)
    assert dict(h.executed_by_mode) == {
        "set_target": HitStats(6, 4),
        "maintain_current": HitStats(2, 0),
        "fail_closed": HitStats(2, 1),
    }
    assert dict(h.two_by_two) == {"both": 3, "ai_only": 0, "rule_only": 1, "neither": 4}


def test_the_confidence_buckets_hold_the_set_target_rows_only():
    h = _card().summary().horizons[0]
    assert [(b.bucket, b.hits.n, b.hits.hits) for b in h.calibration] == [
        (2, 1, 0),  # row 6 at 0.2
        (5, 1, 0),  # row 5 at 0.5
        (7, 1, 0),  # row 2 at 0.7
        (8, 1, 1),  # row 0 at 0.8
        (9, 2, 2),  # rows 7 and 9 at 0.9 and 0.95
    ]
    assert [b.label for b in h.calibration][-2:] == ["[0.8, 0.9)", "[0.9, 1.0]"]


def test_the_one_bar_pnl_totals():
    h = _card().summary().horizons[0]
    executed = [
        0.3 * NEXT_RETURN[0] - 0.3 * TAKER_COST,
        0.3 * NEXT_RETURN[1],
        -0.2 * NEXT_RETURN[2] - 0.5 * TAKER_COST,
        -0.2 * NEXT_RETURN[3],
        -0.2 * TAKER_COST,
        0.0,
        0.28 * NEXT_RETURN[7],
        0.28 * NEXT_RETURN[8],
        -1.0 * NEXT_RETURN[9] - 1.56 * TAKER_COST,
        -0.5 * NEXT_RETURN[10],
    ]
    assert (h.executed_pnl.n, h.executed_pnl.total) == (10, pytest.approx(sum(executed)))
    assert h.executed_pnl.mean == pytest.approx(sum(executed) / 10)
    model = [
        0.3 * NEXT_RETURN[0] - 0.3 * TAKER_COST,
        0.3 * NEXT_RETURN[1],
        -0.4 * NEXT_RETURN[2] - 0.7 * TAKER_COST,
        -0.2 * TAKER_COST,
        0.3 * NEXT_RETURN[6] - 0.3 * TAKER_COST,
        0.3 * NEXT_RETURN[7] - 0.02 * TAKER_COST,
        -1.0 * NEXT_RETURN[9] - 1.56 * TAKER_COST,
        -0.5 * NEXT_RETURN[10],
    ]
    assert (h.ai_pnl.n, h.ai_pnl.total) == (8, pytest.approx(sum(model)))


def test_the_baselines_trade_the_cap_and_pay_their_own_turnover():
    card = _card()
    h = card.summary().horizons[0]
    by_name = {b.name: b for b in h.baselines}
    assert list(by_name) == ["buy_hold", "flat", "research_bias"]
    # Always long at 60% (120% on the leverage-2 row): entry paid once, the
    # leverage step paid on row 9 and unwound on row 10.
    buy_hold = [
        0.6 * NEXT_RETURN[0] - 0.6 * TAKER_COST,
        0.6 * NEXT_RETURN[1],
        0.6 * NEXT_RETURN[2],
        0.6 * NEXT_RETURN[3],
        0.6 * NEXT_RETURN[5],
        0.6 * NEXT_RETURN[6],
        0.6 * NEXT_RETURN[7],
        0.6 * NEXT_RETURN[8],
        1.2 * NEXT_RETURN[9] - 0.6 * TAKER_COST,
        0.6 * NEXT_RETURN[10] - 0.6 * TAKER_COST,
    ]
    assert by_name["buy_hold"].hits == HitStats(10, 7)
    assert by_name["buy_hold"].pnl.total == pytest.approx(sum(buy_hold))
    assert by_name["flat"].hits == HitStats(10, 5)
    assert (by_name["flat"].pnl.total, by_name["flat"].pnl.sharpe) == (0.0, 0.0)
    # The radar's bias at the cap; row 3 has none and is skipped with the
    # short held, so row 5's flat pays 0.6 to close it.
    bias = [
        0.6 * NEXT_RETURN[0] - 0.6 * TAKER_COST,
        0.6 * NEXT_RETURN[1],
        -0.6 * NEXT_RETURN[2] - 1.2 * TAKER_COST,
        -0.6 * TAKER_COST,
        0.6 * NEXT_RETURN[6] - 0.6 * TAKER_COST,
        0.6 * NEXT_RETURN[7],
        -0.6 * NEXT_RETURN[8] - 1.2 * TAKER_COST,
        -1.2 * NEXT_RETURN[9] - 0.6 * TAKER_COST,
        -0.6 * NEXT_RETURN[10] - 0.6 * TAKER_COST,
    ]
    assert by_name["research_bias"].hits == HitStats(9, 3)
    assert by_name["research_bias"].pnl.total == pytest.approx(sum(bias))


def test_a_baseline_pays_the_turnover_of_a_row_it_could_not_score_on_the_next_scored_row():
    """Slot 3 is missing, so the bias flip on slot 2 has no later mark; its 1.2 turnover is owed to slot 4."""
    def q(slot: int, mark: float, bias: str) -> Question:
        return _question(
            input_id=f"s{slot}", at_ms=at_ms(slot), mark=mark, current_side="flat",
            current_margin_pct=0.0, research_bias=bias,
        )

    questions = [q(0, 100.0, "long"), q(1, 101.0, "long"), q(2, 102.0, "short"), q(4, 103.0, "short"), q(5, 104.0, "short")]
    answers = [_answer(input_id=f"s{slot}", target_side="flat", requested_margin_pct=0.0, approved_margin_pct=0.0) for slot in (0, 1, 2, 4, 5)]
    card = score_run(questions, answers, step_ms=STEP_MS, costs=TAKER)
    bias = next(b for b in card.summary().horizons[0].baselines if b.name == "research_bias")
    r0, r1, r4 = 101 / 100 - 1, 102 / 101 - 1, 104 / 103 - 1
    expected = [
        0.6 * r0 - 0.6 * TAKER_COST,  # entered long at the cap
        0.6 * r1,  # held
        # slot 2: flipped to short (turnover 1.2) but has no next decision; slot 3 is missing
        -0.6 * r4 - 1.2 * TAKER_COST,  # slot 4 pays the flip it inherited
    ]
    assert (bias.pnl.n, bias.pnl.total) == (3, pytest.approx(sum(expected)))


def test_sharpe_is_annualised_by_the_horizons_in_a_year():
    from contrib.autoresearch.evaluator import MS_PER_YEAR

    card = _card()
    assert card.bars_per_year == 365 * 24 / 4
    assert card.bars_per_year * STEP_MS == MS_PER_YEAR  # the evaluator's own year
    h = card.summary().horizons[0]
    values = [
        row.outcome(1).executed_pnl for row in card.rows if row.outcome(1).executed_pnl is not None
    ]
    expected = statistics.fmean(values) / statistics.stdev(values) * (365 * 24 / 4) ** 0.5
    assert h.executed_pnl.sharpe == pytest.approx(expected)


def test_describe_prints_one_fact_per_line():
    card = _card()
    lines = card.summary().describe(card)
    assert lines[0] == "decisions: 11 questions, 10 answered, 1 unanswered"
    assert lines[1] == "regimes (prompt_version/model/context_shape): phase2-target-v6/test-model/perp 11"
    assert lines[2] == "fail-closed: 2/10 (20.0%) (invalid_output 1, truncated_output 1)"
    assert lines[3] == (
        "asked a target: 6 (set_target, or rejected), clamped 1/6 (16.7%), "
        "rejected 1/6 (16.7%); maintained: 2"
    )
    assert lines[4] == "flips: 2/10 (20.0%)"
    assert lines[5] == (
        "costs: taker fills at 0.00045 fee + 5 bps slippage; exposure = margin x configured leverage"
    )
    assert "  executed hit 50.0% (5/10); model hit 37.5% (3/8)" in lines
    assert "  model vs rule: both hit 3, model only 0, rule only 1, neither 4" in lines
    assert all(line.isascii() for line in lines)


# -- the split and the holdout lock ---------------------------------------------


def test_build_split_spans_the_first_open_to_the_last_close():
    split = build_split(fixture_questions(), interval="4h", step_ms=STEP_MS)
    assert split.train.start_ms == ANCHOR_MS - STEP_MS
    assert split.holdout.end_ms == ANCHOR_MS + 11 * STEP_MS
    # Twelve bars: 7 / 3 / 2.
    assert (split.train.end_ms - split.train.start_ms) // STEP_MS == 7
    assert (split.validation.end_ms - split.validation.start_ms) // STEP_MS == 3
    assert split.loadable_until() == ANCHOR_MS + 9 * STEP_MS - 1  # row 9's own instant
    assert split.loadable_until(holdout=True) == ANCHOR_MS + 11 * STEP_MS - 1


def test_a_stamp_on_the_bar_boundary_belongs_to_the_bar_opening_there():
    """The split's edges are bar opens; a question is placed by the bar it falls in, floored."""
    questions = fixture_questions()
    split = build_split(questions, interval="4h", step_ms=STEP_MS)
    # One millisecond later than the fixture's stamps: every question moves
    # into the NEXT bar, so the segments shift by one row and the last
    # question falls off the span.
    on_boundary = [
        _question(
            input_id=q.input_id, at_ms=q.at_ms + 1, mark=q.mark,
            current_side="flat", current_margin_pct=0.0,
        )
        for q in questions[:-1]
    ]
    card = score_run(on_boundary, [], step_ms=STEP_MS, costs=TAKER, split=split)
    # Slots 0-3 and 5 open in the train bars (slot 4 is the missing cycle), 6-8 in validation.
    assert [row.segment.value for row in card.rows] == ["train"] * 5 + ["validation"] * 3
    assert [row.question.input_id for row in card.rows] == [input_id(s) for s in range(9) if s != 4]
    beyond = _question(input_id="beyond", at_ms=at_ms(12), mark=100.0)
    with pytest.raises(ScoreError, match="no segment of the split"):
        score_run([*questions, beyond], [], step_ms=STEP_MS, costs=TAKER, split=split)


def test_opening_the_holdout_does_not_move_the_flat_band():
    split = build_split(fixture_questions(), interval="4h", step_ms=STEP_MS)
    locked, opened = _card(split=split), _card(split=split, holdout=True)
    assert opened.flat_bands == locked.flat_bands
    # Rows 9 and 10 gain a later mark once the holdout is open, but the
    # band is still the train + validation median.
    assert _row(opened, 9).outcome(1).ret is not None and _row(locked, 9).outcome(1).ret is None


def test_the_holdout_is_neither_scored_nor_read_unless_asked():
    split = build_split(fixture_questions(), interval="4h", step_ms=STEP_MS)
    card = _card(split=split)
    assert [row.question.input_id for row in card.rows] == [input_id(s) for s in range(10) if s != 4]
    assert dict(card.summary().segments) == {"train": 6, "validation": 3}
    assert _row(card, 9).segment is SegmentName.VALIDATION
    # Row 9's next decision is slot 10 — the holdout's first bar — so it is not read.
    assert _row(card, 9).outcome(1).ret is None
    # Row 3's sixth bar is slot 9, the last validation bar: readable.
    assert _row(card, 3).outcome(6).later_mark == 108.0
    # Row 5's sixth bar is slot 11: not.
    assert _row(card, 5).outcome(6).later_mark is None
    assert not card.holdout_read

    opened = _card(split=split, holdout=True)
    assert len(opened.rows) == 11
    assert dict(opened.summary().segments) == {"train": 6, "validation": 3, "holdout": 2}
    assert _row(opened, 9).outcome(1).ret == pytest.approx(NEXT_RETURN[9])
    assert _row(opened, 10).segment is SegmentName.HOLDOUT
    assert "segments (questions): train 6, validation 3, holdout 2 -- HOLDOUT READ" in (
        opened.summary().describe(opened)
    )


# -- refusals ---------------------------------------------------------------------


def _question(**overrides) -> Question:
    fields = {
        "input_id": "q",
        "at_ms": at_ms(0),
        "mark": 100.0,
        "current_side": "long",
        "current_margin_pct": 30.0,
        "leverage": 1.0,
        "max_margin_pct": 60.0,
    }
    return Question(**{**fields, **overrides})


def _answer(**overrides) -> Answer:
    fields = {
        "input_id": "q",
        "decision_mode": "set_target",
        "target_side": "long",
        "requested_margin_pct": 30.0,
        "approved_margin_pct": 30.0,
        "risk_action": "approved",
        "risk_reason": None,
        "confidence": 0.8,
        "order_created": True,
        "no_order_reason": None,
    }
    return Answer(**{**fields, **overrides})


def test_a_losing_position_may_carry_more_than_100_percent_margin():
    """Imputed from the books, ``current_margin_pct`` exceeds 100 when equity has shrunk."""
    assert _question(current_margin_pct=130.0).current_exposure == pytest.approx(1.3)
    for cap in (130.0, 0.0):
        with pytest.raises(ScoreError, match=r"max_margin_pct is a percent in \(0, 100\]"):
            _question(max_margin_pct=cap)


@pytest.mark.parametrize(
    "overrides",
    [
        {"current_side": "flat", "current_margin_pct": 10.0},
        {"current_side": "short", "current_margin_pct": 0.0},
        {"mark": 0.0},
        {"mark": float("nan")},
        {"leverage": 0.0},
        {"current_margin_pct": -1.0},
        {"at_ms": 1.5},
        {"current_side": "sideways"},
    ],
    ids=[
        "flat-with-margin",
        "sized-without-margin",
        "zero-mark",
        "nan-mark",
        "zero-leverage",
        "negative-margin",
        "float-at",
        "bad-side",
    ],
)
def test_a_question_that_cannot_be_scored_is_refused(overrides):
    with pytest.raises((ScoreError, ValueError)):
        _question(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_side": None},
        {"requested_margin_pct": None},
        {"approved_margin_pct": None},
        {"approved_margin_pct": 40.0},
        {"risk_action": "clamped", "risk_reason": "exceeds_max_target_margin_pct"},
        {"approved_margin_pct": 20.0},
        {"decision_mode": "maintain_current", "risk_action": "clamped", "order_created": False},
        {"decision_mode": "maintain_current", "order_created": False, "no_order_reason": "x"},
        {"risk_action": "rejected", "risk_reason": "low_confidence", "order_created": False},
        {"risk_action": "invalid_fail_closed", "risk_reason": "invalid_output"},
        {
            "decision_mode": "maintain_current",
            "risk_action": "rejected",
            "risk_reason": "low_confidence",
            "target_side": None,
            "requested_margin_pct": None,
            "approved_margin_pct": None,
            "order_created": False,
            "no_order_reason": "rejected",
        },
        {
            "decision_mode": "maintain_current",
            "approved_margin_pct": None,
            "order_created": False,
            "no_order_reason": "maintain_current",
        },
        {"no_order_reason": "within_deadband"},
        {"order_created": False},
        {"confidence": 1.5},
        {"requested_margin_pct": 120.0},
    ],
    ids=[
        "target-without-side",
        "target-without-requested",
        "target-without-approved",
        "approved-above-requested",
        "clamped-without-reduction",
        "approved-not-equal-requested",
        "clamped-maintain",
        "maintain-with-approved",
        "rejected-as-set-target",
        "fail-closed-as-set-target",
        "rejection-without-the-refused-target",
        "maintain-asking-for-a-target",
        "order-and-reason",
        "neither-order-nor-reason",
        "confidence-over-1",
        "margin-over-100",
    ],
)
def test_an_answer_that_contradicts_the_gate_is_refused(overrides):
    with pytest.raises(ScoreError):
        _answer(**overrides)


def test_score_run_refuses_a_question_or_answer_seen_twice_and_an_orphan_answer():
    q, a = _question(), _answer()
    with pytest.raises(ScoreError, match="asked twice"):
        score_run([q, q], [a], step_ms=STEP_MS, costs=TAKER)
    with pytest.raises(ScoreError, match="answered twice"):
        score_run([q], [a, a], step_ms=STEP_MS, costs=TAKER)
    with pytest.raises(ScoreError, match="without a question"):
        score_run([q], [_answer(input_id="other")], step_ms=STEP_MS, costs=TAKER)
    with pytest.raises(ScoreError, match="two questions within"):
        score_run([q, _question(input_id="r", at_ms=at_ms(0) + 1)], [], step_ms=STEP_MS, costs=TAKER)


# -- the paired comparison ----------------------------------------------------------


@pytest.mark.parametrize(
    ("wins", "losses", "p"),
    [(7, 1, 2 * 9 / 256), (0, 0, 1.0), (5, 5, 1.0), (10, 0, 2 / 1024)],
)
def test_sign_test_is_the_two_sided_exact_binomial(wins, losses, p):
    assert sign_test(wins, losses) == pytest.approx(p)


def test_paired_hits_drops_a_question_either_side_could_not_read():
    paired = paired_hits([True, False, None, True, False], [False, False, True, True, True])
    assert (paired.n, paired.a_only, paired.b_only, paired.p_value) == (4, 1, 1, 1.0)
    with pytest.raises(ScoreError):
        paired_hits([True], [True, False])


# -- CSV ------------------------------------------------------------------------------


def test_csv_table_has_one_row_per_question_with_the_horizons_by_label():
    card = _card()
    header, rows = csv_table(card)
    assert header[:4] == ["input_id", "attempt_id", "at", "segment"]
    assert "return_4h" in header and "executed_pnl_24h" in header
    assert {"no_order_reason", "account_equity", "research_strategy_id"} <= set(header)
    assert len(rows) == 11 and all(len(row) == len(header) for row in rows)
    by_id = {row[0]: dict(zip(header, row, strict=True)) for row in rows}
    assert by_id[input_id(3)]["later_mark_source_4h"] == "research"
    assert by_id[input_id(11)]["decision_mode"] is None
    assert by_id[input_id(7)]["no_order_reason"] == "within_deadband"
    assert by_id[input_id(0)]["at"].endswith("+00:00")
    # One whole row, so the header and the values cannot drift apart: slot 2
    # (clamped 40 -> 20, long -> short) has every pair of sibling columns distinct.
    row2 = by_id[input_id(2)]
    assert {k: row2[k] for k in header[:29]} == {
        "input_id": input_id(2),
        "attempt_id": row2["attempt_id"],
        "at": row2["at"],
        "segment": None,
        "prompt_version": "phase2-target-v6",
        "model": "test-model",
        "context_shape": "perp",
        "research_bias": "short",
        "research_strategy_id": "btc-4h-maker#9",
        "reports_present": None,
        "mark": 99.0,
        "account_equity": 1000.0,
        "current_side": "long",
        "current_margin_pct": 30.0,
        "leverage": 1.0,
        "decision_mode": "set_target",
        "target_side": "short",
        "requested_margin_pct": 40.0,
        "approved_margin_pct": 20.0,
        "risk_action": "clamped",
        "risk_reason": "exceeds_max_target_margin_pct",
        "confidence": 0.7,
        "order_created": True,
        "no_order_reason": None,
        "ai_side": "short",
        "executed_side": "short",
        "ai_exposure": -0.4,
        "executed_exposure": -0.2,
        "flip": True,
    }
    assert (row2["later_mark_4h"], row2["later_mark_source_4h"]) == (102.0, "store")
    assert row2["ai_pnl_4h"] == pytest.approx(-0.4 * NEXT_RETURN[2] - 0.7 * TAKER_COST)
    assert row2["executed_pnl_4h"] == pytest.approx(-0.2 * NEXT_RETURN[2] - 0.5 * TAKER_COST)
    assert (row2["ai_hit_24h"], row2["executed_hit_24h"]) == (False, False)
