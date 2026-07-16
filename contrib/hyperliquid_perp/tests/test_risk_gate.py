"""Tests for the Phase 2 RiskGate (pure sizing + clamps + deadband).

Numbers follow phase2-spec.md §2 / phase2-execution.md §6.2: with
``account_equity = 1000`` and ``leverage = 1``, an approved 60% target maps to
600 USDC margin and 600 USDC notional.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp import risk_gate
from contrib.hyperliquid_perp.domains.perp.risk_gate import (
    CurrentPositionState,
    RiskAction,
    RiskConfig,
    current_position_state,
)
from contrib.hyperliquid_perp.domains.perp.schema import PerpPosition
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
)

_RISK = RiskConfig()
_DECISION_CFG = DecisionConfig()
_EQUITY = Decimal("1000")


def _parsed(
    mode="set_target",
    side="long",
    margin=35,
    confidence="0.78",
) -> ParsedDecision:
    decision = TargetDecision(
        decision_mode=DecisionMode(mode),
        target_side=TargetSide(side) if side else None,
        requested_target_margin_pct=margin,
        confidence=Decimal(confidence) if confidence is not None else None,
        rationale="test rationale",
        key_risks=("risk",),
    )
    return ParsedDecision(decision=decision, is_valid=True, invalid_reason=None, raw_response="raw")


def _invalid_parsed(reason="invalid_output") -> ParsedDecision:
    return ParsedDecision(
        decision=TargetDecision.fail_closed(),
        is_valid=False,
        invalid_reason=reason,
        raw_response="raw",
    )


def _evaluate(parsed, *, current=None, equity=_EQUITY, risk=_RISK, cfg=_DECISION_CFG, **kwargs):
    return risk_gate.evaluate(
        parsed,
        account_equity=equity,
        current=current or CurrentPositionState.flat(),
        risk=risk,
        decision_cfg=cfg,
        **kwargs,
    )


def _long_state(margin_pct: str, notional: str) -> CurrentPositionState:
    return CurrentPositionState(
        side=TargetSide.LONG,
        signed_notional=Decimal(notional),
        margin_pct=Decimal(margin_pct),
    )


# --------------------------------------------------------------------------
# Approve + sizing math (execution §6.2 example numbers)
# --------------------------------------------------------------------------


def test_approved_long_sizing_numbers():
    result = _evaluate(_parsed(margin=20))
    assert result.risk_action is RiskAction.APPROVED
    assert result.approved_target_margin_pct == 20
    assert result.target_margin == Decimal("200")  # 1000 * 20 / 100
    assert result.target_notional == Decimal("200")  # leverage 1
    assert result.target_signed_notional == Decimal("200")
    assert result.delta_notional == Decimal("200")  # from flat
    assert result.order_created is True
    assert result.no_order_reason is None


def test_approved_sizing_with_leverage_five():
    # §6.2 worked example: 20% at 5x -> margin 200, notional 1000.
    risk = RiskConfig(leverage=Decimal(5))
    result = _evaluate(_parsed(margin=20), risk=risk)
    assert result.target_margin == Decimal("200")
    assert result.target_notional == Decimal("1000")
    assert result.configured_leverage == Decimal(5)


def test_short_target_signed_notional_is_negative():
    result = _evaluate(_parsed(side="short", margin=30))
    assert result.target_signed_notional == Decimal("-300")
    assert result.delta_notional == Decimal("-300")


def test_confidence_is_recorded_but_never_scales_sizing():
    # Same margin, very different confidences -> identical sizing (spec §2.4).
    low = _evaluate(_parsed(margin=40, confidence="0.31"))
    high = _evaluate(_parsed(margin=40, confidence="0.99"))
    assert low.target_margin == high.target_margin == Decimal("400")
    assert low.confidence == Decimal("0.31")
    assert high.confidence == Decimal("0.99")


# --------------------------------------------------------------------------
# Clamp to max_target_margin_pct (spec §2.3)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("requested", [61, 75, 100])
def test_clamp_above_max_keeps_both_values(requested):
    result = _evaluate(_parsed(margin=requested))
    assert result.risk_action is RiskAction.CLAMPED
    assert result.risk_reason == risk_gate.RISK_REASON_MAX_TARGET_MARGIN
    assert result.requested_target_margin_pct == requested  # preserved
    assert result.approved_target_margin_pct == 60  # clamped
    assert result.target_margin == Decimal("600")
    assert result.order_created is True


def test_exactly_max_is_approved_not_clamped():
    result = _evaluate(_parsed(margin=60))
    assert result.risk_action is RiskAction.APPROVED
    assert result.approved_target_margin_pct == 60


# --------------------------------------------------------------------------
# min_confidence gate (spec §2.4)
# --------------------------------------------------------------------------


def test_low_confidence_set_target_is_rejected():
    # A schema-valid but under-confident set_target is REJECTED (normal risk
    # operation), not INVALID_FAIL_CLOSED (the contract-violation/drift tag).
    result = _evaluate(_parsed(margin=40, confidence="0.29"))
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE
    assert result.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_REJECTED
    # The audit row still shows what the AI asked for; no sized target leaves.
    assert result.requested_target_margin_pct == 40
    assert result.approved_target_margin_pct is None
    assert result.target_margin is None


def test_confidence_at_threshold_passes():
    result = _evaluate(_parsed(margin=40, confidence="0.3"))
    assert result.risk_action is RiskAction.APPROVED


def test_low_confidence_gates_flat_target_too():
    # Decided semantics: min_confidence gates FLAT as well — an under-confident
    # "close the position" request is risk-REJECTED and the position stays open.
    # Every other gate step (grid, equity, approved==0, deadband) exempts FLAT;
    # this asymmetry is deliberate: closing a position needs conviction too.
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(side="flat", margin=0, confidence="0.1"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_REJECTED


def test_low_confidence_does_not_gate_maintain_current():
    # The threshold only blocks "high target, low conviction" contradictions;
    # maintain_current has no target to block.
    result = _evaluate(_parsed(mode="maintain_current", side=None, margin=None, confidence="0.1"))
    assert result.risk_action is RiskAction.APPROVED
    assert result.no_order_reason == risk_gate.NO_ORDER_MAINTAIN_CURRENT


# --------------------------------------------------------------------------
# resize_min_confidence gate (spec §2.4): same-side resizes need the higher bar
# --------------------------------------------------------------------------


def test_same_side_resize_below_resize_bar_is_rejected():
    # 0.65 clears min_confidence (0.3) but not resize_min_confidence (0.7):
    # exactly the churn profile the baseline segment paid fees for.
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(margin=45, confidence="0.65"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE_RESIZE
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_REJECTED
    assert result.requested_target_margin_pct == 45  # audit keeps the ask
    assert result.approved_target_margin_pct is None


def test_same_side_resize_at_resize_bar_passes():
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(margin=45, confidence="0.7"), current=current)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True


def test_open_from_flat_keeps_base_confidence_bar():
    # Opening is a directional decision, not a resize: 0.65 from flat passes.
    result = _evaluate(_parsed(margin=45, confidence="0.65"))
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True


def test_flip_keeps_base_confidence_bar():
    # A flip changes side, so the resize bar does not apply.
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(side="short", margin=20, confidence="0.65"), current=current)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True


def test_flat_close_keeps_base_confidence_bar():
    # An explicit close is exempt: a flat account's side is None, never FLAT,
    # so `side is current.side` cannot match — 0.4 clears the base gate.
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(side="flat", margin=0, confidence="0.4"), current=current)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True


def test_within_deadband_reaffirmation_is_exempt_from_resize_bar():
    # The resize bar only gates targets that would actually trade: a same-side
    # target inside the deadband creates no order and pays no fees, so it stays
    # an APPROVED within_deadband no-op even at low confidence — rejecting it
    # would conflate cost-free reaffirmations with real churn in the analytics.
    current = _long_state(margin_pct="35.5", notional="355")
    result = _evaluate(_parsed(margin=35, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_WITHIN_DEADBAND


def test_no_equity_outranks_resize_bar():
    # A dead account is the structural problem; the resize bar must not mask it.
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(margin=20, confidence="0.5"), current=current, equity=Decimal(0))
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_NO_EQUITY


def test_same_side_reduction_below_resize_bar_is_rejected():
    # Reductions are gated on purpose (spec §2.4: the baseline churn's first
    # leg was always a mid-confidence reduce). This locks the debated call so a
    # well-intentioned "de-risking should be easy" exemption can't sneak back.
    current = _long_state(margin_pct="45", notional="450")
    result = _evaluate(_parsed(margin=20, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE_RESIZE
    assert result.order_created is False


def test_clamped_same_side_resize_below_resize_bar_rejects_and_drops_clamp():
    # requested 90 clamps to the 60 cap and still clears the deadband from 20,
    # so an order would be created — the resize bar then rejects. Per the
    # REJECTED contract the clamp verdict and approved value are discarded:
    # audit keeps only the raw ask (cap-binding stats exclude these rows).
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(margin=90, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE_RESIZE
    assert result.requested_target_margin_pct == 90
    assert result.approved_target_margin_pct is None


def test_clamped_same_side_resize_at_resize_bar_stays_clamped():
    # The remaining cell of the clamp x resize matrix: requested 90 clamps to
    # the 60 cap, clears the deadband from 20, and confidence 0.7 clears the
    # resize bar — the clamp verdict, both margin values and the order must all
    # survive the resize check untouched (a confident size-up past the cap on
    # an existing position is the common production shape of this path).
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(margin=90, confidence="0.7"), current=current)
    assert result.risk_action is RiskAction.CLAMPED
    assert result.requested_target_margin_pct == 90
    assert result.approved_target_margin_pct == 60
    assert result.order_created is True


def test_zero_delta_reaffirmation_is_exempt_from_resize_bar():
    # zero_delta is a distinct no-order path from within_deadband (an unknown
    # margin_pct skips the deadband branch; the exact-delta check fires): like
    # the deadband case it costs nothing, so low confidence must not reject it.
    current = CurrentPositionState(
        side=TargetSide.LONG, signed_notional=Decimal("350"), margin_pct=None
    )
    result = _evaluate(_parsed(margin=35, confidence="0.4"), current=current)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_ZERO_DELTA


def test_unknown_margin_resize_below_resize_bar_is_rejected():
    # Unknown margin_pct skips the deadband branch entirely, so for this state
    # the resize bar is the only guard left against low-confidence churn: a
    # same-side, non-zero-delta target must still be rejected below the bar.
    # Locks the gate against a refactor that reuses the deadband's
    # `margin_pct is not None` guard on the resize check.
    current = CurrentPositionState(
        side=TargetSide.LONG, signed_notional=Decimal("355"), margin_pct=None
    )
    result = _evaluate(_parsed(margin=20, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE_RESIZE
    assert result.order_created is False


def test_leverage_mismatch_convergence_order_is_gated_by_resize_bar():
    # With the deadband disabled on a leverage mismatch, a same-side target at
    # the same margin% still creates a convergence order — which must clear the
    # resize bar like any other order-creating resize (spec §2.4 interaction notes).
    # Identical numbers with matching leverage would be an APPROVED
    # within-deadband no-op instead.
    current = CurrentPositionState(
        side=TargetSide.LONG,
        signed_notional=Decimal("500"),
        margin_pct=Decimal("10"),
        leverage=Decimal(5),
    )
    result = _evaluate(_parsed(margin=10, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE_RESIZE


def test_equal_thresholds_off_switch_disables_resize_bar():
    # resize_min_confidence == min_confidence is the documented off-switch
    # (SETUP.md / example yaml): with the bars equal, a same-side resize at
    # exactly the shared bar trades like any other set_target — the resize
    # gate adds nothing on top of the base gate.
    cfg = DecisionConfig(min_confidence=Decimal("0.3"), resize_min_confidence=Decimal("0.3"))
    current = _long_state(margin_pct="20", notional="200")
    result = _evaluate(_parsed(margin=45, confidence="0.3"), current=current, cfg=cfg)
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True


def test_zero_capacity_reject_outranks_resize_bar():
    # A same-side resize with no cross-margin capacity left: the zero-capacity
    # fail-close returns before the resize bar, so the binding constraint —
    # the structural problem — wins the audit reason over low_confidence_resize
    # (same precedence guarantee as test_no_equity_outranks_resize_bar).
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(
        _parsed(margin=40, confidence="0.5"), current=current, other_used_margin=Decimal("1000")
    )
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_AVAILABLE_MARGIN


# --------------------------------------------------------------------------
# Deadband (spec §2.4): same-side only; flip and flat are exempt
# --------------------------------------------------------------------------


def test_same_side_within_deadband_creates_no_order():
    current = _long_state(margin_pct="35.5", notional="355")
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_WITHIN_DEADBAND
    assert result.risk_action is RiskAction.APPROVED  # approved, just no order
    assert result.delta_notional == Decimal("-5")  # still reported


def test_clamped_target_can_still_land_within_deadband():
    # A request clamped down to max (100 -> 60) whose approved allocation sits
    # within the deadband of the current same-side position: risk_action stays
    # CLAMPED (the clamp really happened) while order_created is False. Both the
    # "clamped" verdict and the "no order" outcome must be reported together.
    # Confidence 0.5 also locks the spec §2.4 interaction note: order necessity
    # is judged on the clamped value, so a request absorbed into the deadband
    # never reaches the resize bar even below it.
    current = _long_state(margin_pct="60", notional="600")
    result = _evaluate(_parsed(margin=100, confidence="0.5"), current=current)
    assert result.risk_action is RiskAction.CLAMPED
    assert result.approved_target_margin_pct == 60
    assert result.requested_target_margin_pct == 100
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_WITHIN_DEADBAND


def test_deadband_boundary_at_exact_distance_creates_order():
    # The deadband comparison is strict `<`: a same-side distance exactly equal
    # to rebalance_deadband_pct (default 1) is NOT "within" it — order created.
    current = _long_state(margin_pct="34", notional="340")
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is True
    assert result.no_order_reason is None
    assert result.delta_notional == Decimal("10")


def test_same_side_outside_deadband_creates_order():
    current = _long_state(margin_pct="30", notional="300")
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is True
    assert result.delta_notional == Decimal("50")


def test_flip_executes_even_with_tiny_difference():
    # Long 35% -> short 35%: the margin distance is 0, but a flip must never
    # be swallowed by the deadband.
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(side="short", margin=35), current=current)
    assert result.order_created is True
    assert result.no_order_reason is None
    assert result.delta_notional == Decimal("-700")


def test_flat_executes_even_from_tiny_position():
    # Long 0.4% -> flat: |0 - 0.4| < deadband, but flat closes must execute.
    current = _long_state(margin_pct="0.4", notional="4")
    result = _evaluate(_parsed(side="flat", margin=0), current=current)
    assert result.order_created is True
    assert result.target_signed_notional == Decimal("0")
    assert result.delta_notional == Decimal("-4")


def test_flat_when_already_flat_is_noop():
    result = _evaluate(_parsed(side="flat", margin=0))
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_ALREADY_FLAT
    assert result.delta_notional == Decimal("0")


def test_deadband_skipped_when_current_margin_unknown():
    # Unknown margin_pct on a sized position: the deadband cannot be evaluated,
    # so the (safety-neutral) optimisation is skipped and, at this confidence
    # (helper default 0.78, above the resize bar), the order executes.
    current = CurrentPositionState(
        side=TargetSide.LONG, signed_notional=Decimal("355"), margin_pct=None
    )
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is True


def test_same_target_zero_delta_creates_no_order():
    current = CurrentPositionState(
        side=TargetSide.LONG, signed_notional=Decimal("350"), margin_pct=None
    )
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_ZERO_DELTA


def test_deadband_disabled_on_leverage_mismatch():
    # A manually opened 5x position under risk.leverage=1: margin 10% carries
    # ~50%-of-equity notional, while a 10% target at 1x means 10% notional.
    # Equal margin%% must NOT be swallowed by the deadband — at this confidence
    # (helper default 0.78, above the resize bar) the order executes and
    # delta_notional converges the true exposure to the target.
    current = CurrentPositionState(
        side=TargetSide.LONG,
        signed_notional=Decimal("500"),
        margin_pct=Decimal("10"),
        leverage=Decimal(5),
    )
    result = _evaluate(_parsed(margin=10), current=current)
    assert result.order_created is True
    assert result.no_order_reason is None
    assert result.delta_notional == Decimal("-400")  # 100 target - 500 actual


def test_deadband_applies_when_leverage_matches():
    # Same-side rebalance inside the band with a verified matching leverage:
    # the churn optimisation still applies.
    current = CurrentPositionState(
        side=TargetSide.LONG,
        signed_notional=Decimal("355"),
        margin_pct=Decimal("35.5"),
        leverage=Decimal(1),
    )
    result = _evaluate(_parsed(margin=35), current=current)
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_WITHIN_DEADBAND


# --------------------------------------------------------------------------
# maintain_current and fail-closed passthrough shapes (data §7)
# --------------------------------------------------------------------------


def test_maintain_current_output_shape():
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(mode="maintain_current", side=None, margin=None), current=current)
    assert result.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert result.target_side is None
    assert result.requested_target_margin_pct is None
    assert result.approved_target_margin_pct is None
    assert result.target_margin is None
    assert result.target_notional is None
    assert result.target_signed_notional is None
    assert result.delta_notional == Decimal("0")
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_MAINTAIN_CURRENT
    assert result.current_signed_notional == Decimal("350")


def test_invalid_parse_passes_through_fail_closed():
    result = _evaluate(_invalid_parsed("margin_off_step_grid"))
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
    assert result.risk_reason == "margin_off_step_grid"
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_INVALID_FAIL_CLOSED
    assert result.target_margin is None


def test_gate_revalidates_hand_built_decision():
    # A "valid" ParsedDecision wrapping an illegal combination (long + 0) must
    # fail closed here — the flip re-run cannot trust its caller.
    bad = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide.LONG,
        requested_target_margin_pct=0,
        confidence=Decimal("0.9"),
        rationale="r",
        key_risks=(),
    )
    parsed = ParsedDecision(decision=bad, is_valid=True, invalid_reason=None, raw_response="raw")
    result = _evaluate(parsed)
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
    assert result.risk_reason == "directional_side_with_zero_margin"


def test_zero_equity_directional_target_is_rejected():
    result = _evaluate(_parsed(margin=40), equity=Decimal(0))
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_NO_EQUITY
    assert result.order_created is False


@pytest.mark.parametrize("equity", [Decimal(0), Decimal("-5")])
def test_flat_close_executes_with_zero_or_negative_equity(equity):
    # Closing must never be blocked by a broken/zero equity read: the equity
    # gate exempts flat, and the zero-capacity fail-close applies only to
    # directional targets. A position that cannot be closed is the worst
    # possible failure mode of a tightened guard.
    result = _evaluate(
        _parsed(side="flat", margin=0), current=_long_state("35", "350"), equity=equity
    )
    assert result.risk_action is RiskAction.APPROVED
    assert result.order_created is True
    assert result.target_signed_notional == Decimal(0)
    assert result.delta_notional == Decimal("-350")


def test_clamp_chain_composes_to_tightest_cap():
    # All three caps simultaneously tighter than the request: allocation 80,
    # available margin 40 ((1000-600)/1000), leverage headroom 25
    # ((2*1000-1500)/2/1000). The chain must compose against the shrinking
    # approved value and record the tightest cap as the binding constraint.
    risk = RiskConfig(leverage=Decimal(2), max_target_margin_pct=80)
    result = _evaluate(
        _parsed(margin=90),
        risk=risk,
        other_used_margin=Decimal("600"),
        other_positions_notional=Decimal("1500"),
    )
    assert result.risk_action is RiskAction.CLAMPED
    assert result.approved_target_margin_pct == 25
    assert result.risk_reason == risk_gate.RISK_REASON_EFFECTIVE_LEVERAGE


# --------------------------------------------------------------------------
# Independent available-margin / effective-leverage checks (spec §2.3)
# --------------------------------------------------------------------------


def test_available_margin_check_is_independent_of_allocation_cap():
    # 40% requested is inside the 60% cap, but other symbols already commit
    # 700/1000 of equity — only 30% is genuinely available.
    result = _evaluate(_parsed(margin=40), other_used_margin=Decimal("700"))
    assert result.risk_action is RiskAction.CLAMPED
    assert result.risk_reason == risk_gate.RISK_REASON_AVAILABLE_MARGIN
    assert result.approved_target_margin_pct == 30
    assert result.target_margin == Decimal("300")


def test_effective_leverage_check_is_independent_of_allocation_cap():
    # 50% at 1x is inside both the cap and available margin, but other
    # positions already carry 800 notional -> only 200 notional of leverage
    # headroom remains (equity 1000 * leverage 1).
    result = _evaluate(_parsed(margin=50), other_positions_notional=Decimal("800"))
    assert result.risk_action is RiskAction.CLAMPED
    assert result.risk_reason == risk_gate.RISK_REASON_EFFECTIVE_LEVERAGE
    assert result.approved_target_margin_pct == 20
    assert result.target_notional == Decimal("200")


def test_negative_other_state_is_rejected():
    # Either cross-margin input going negative is a corrupt account read.
    with pytest.raises(ValueError, match="must be >= 0"):
        _evaluate(_parsed(), other_used_margin=Decimal("-1"))
    with pytest.raises(ValueError, match="must be >= 0"):
        _evaluate(_parsed(), other_positions_notional=Decimal("-1"))


def test_no_capacity_is_rejected_instead_of_full_close():
    # Cross margin fully committed elsewhere: the cap snaps to 0. A directional
    # target must be rejected — not approved as long+0, which would emit an
    # order that closes the whole position on a "stay long" request.
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(margin=40), current=current, other_used_margin=Decimal("1000"))
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_AVAILABLE_MARGIN
    assert result.order_created is False
    assert result.target_margin is None


def test_cap_below_grid_minimum_rejects_not_snapped_up():
    # min=10 but only 5% of equity is genuinely available: snapping up to the
    # grid minimum would approve margin that does not exist.
    cfg = DecisionConfig(ai_target_margin_min_pct=10)
    result = _evaluate(_parsed(margin=40), cfg=cfg, other_used_margin=Decimal("950"))
    assert result.risk_action is RiskAction.REJECTED
    assert result.risk_reason == risk_gate.RISK_REASON_AVAILABLE_MARGIN


def test_max_clamp_snaps_to_decision_grid():
    # step=5, max=63: the allocation clamp must land on the grid (60), never
    # leak an off-grid approved value the parser itself would reject.
    cfg = DecisionConfig(target_margin_step_pct=5)
    risk = RiskConfig(max_target_margin_pct=63)
    result = _evaluate(_parsed(margin=75), cfg=cfg, risk=risk)
    assert result.risk_action is RiskAction.CLAMPED
    assert result.approved_target_margin_pct == 60


# --------------------------------------------------------------------------
# current_position_state + config parsing
# --------------------------------------------------------------------------


def test_current_position_state_uses_mark_and_committed_margin():
    position = PerpPosition(
        coin="BTC",
        size=Decimal("0.01"),
        entry_price=Decimal("59000"),
        unrealized_pnl=Decimal("10"),
        margin_used=Decimal("350"),
        leverage=Decimal(5),
    )
    state = current_position_state(position, _EQUITY, mark_price=Decimal("60000"))
    assert state.side is TargetSide.LONG
    assert state.signed_notional == Decimal("600")  # size * mark, not entry
    assert state.margin_pct == Decimal("35")  # committed margin / equity
    assert state.leverage == Decimal(5)  # real leverage passed through for the deadband guard


def test_current_position_state_short_and_unknown_margin():
    position = PerpPosition(
        coin="BTC",
        size=Decimal("-0.01"),
        entry_price=Decimal("59000"),
        unrealized_pnl=Decimal("0"),
        margin_used=None,
    )
    state = current_position_state(position, _EQUITY, mark_price=Decimal("60000"))
    assert state.side is TargetSide.SHORT
    assert state.signed_notional == Decimal("-600")
    assert state.margin_pct is None  # unknown, never guessed


def test_current_position_state_flat():
    state = current_position_state(None, _EQUITY, mark_price=Decimal("60000"))
    assert state.side is None
    assert state.signed_notional == Decimal("0")


def test_risk_config_rejects_bad_values():
    with pytest.raises(ValueError, match="leverage"):
        RiskConfig(leverage=Decimal(0))
    with pytest.raises(ValueError, match="cross"):
        RiskConfig(margin_mode="isolated")
    with pytest.raises(ValueError, match="max_target_margin_pct"):
        RiskConfig(max_target_margin_pct=0)


def test_risk_config_from_dict_nulls_fall_back():
    cfg = RiskConfig.from_dict({"leverage": None, "max_target_margin_pct": None})
    assert cfg.leverage == Decimal(1)
    assert cfg.max_target_margin_pct == 60


def test_risk_config_from_dict_rejects_yaml_boolean_and_normalises_margin_mode():
    # `max_target_margin_pct: yes` (YAML bool) must fail loud, not become 1.
    with pytest.raises(ValueError, match="max_target_margin_pct"):
        RiskConfig.from_dict({"max_target_margin_pct": True})
    # The YAML string form normalises to the enum and stays str-comparable.
    cfg = RiskConfig.from_dict({"margin_mode": "cross"})
    assert cfg.margin_mode is risk_gate.MarginMode.CROSS
    assert cfg.margin_mode == "cross"


def test_risk_config_from_dict_rejects_unknown_key():
    # A typo like `max_target_margin_pt` silently reverts the cap to the default
    # (60) — reject it loudly so a safety limit is never dropped unnoticed.
    with pytest.raises(ValueError, match="unknown config key"):
        RiskConfig.from_dict({"max_target_margin_pt": 20})


def test_risk_config_from_dict_rejects_non_scalar_value():
    # A list value (YAML indentation slip) must surface as a named ValueError,
    # not a TypeError that escapes main's config-error handler.
    with pytest.raises(ValueError, match="max_target_margin_pct"):
        RiskConfig.from_dict({"max_target_margin_pct": [60]})


def test_risk_config_from_dict_rejects_non_mapping_block():
    # A whole block that isn't a mapping (`risk: 60`) must be a named ValueError,
    # not a TypeError from set(cfg) that escapes the exit-1 config-error handler.
    with pytest.raises(ValueError, match="mapping"):
        RiskConfig.from_dict(60)


def test_validate_risk_decision_config_rejects_unusable_pair():
    # Each block is individually valid, but the cap snaps below the grid so every
    # directional target would clamp to 0 and be rejected — reject the pairing.
    # (a) cap below a non-zero grid minimum.
    with pytest.raises(ValueError, match="no legal directional grid value"):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=5),
            DecisionConfig(ai_target_margin_min_pct=10, ai_target_margin_max_pct=100),
        )
    # (b) grid starts at 0 but the cap is below one step (snaps to 0).
    with pytest.raises(ValueError, match="no legal directional grid value"):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=3),
            DecisionConfig(
                ai_target_margin_min_pct=0, ai_target_margin_max_pct=100, target_margin_step_pct=5
            ),
        )
    # A cap that lands on a real grid value is fine (no raise).
    risk_gate.validate_risk_decision_config(RiskConfig(), DecisionConfig())


def test_validate_risk_decision_config_rejects_off_grid_cap():
    # step=25, cap=60: the effective cap would silently tighten to 50 — the
    # operator configured 60 and could never deploy more. Reject the
    # misalignment at load instead of silently shrinking the cap.
    with pytest.raises(ValueError, match="not on the decision grid"):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=60),
            DecisionConfig(target_margin_step_pct=25),
        )


def test_validate_risk_decision_config_allows_cap_at_or_above_grid_ceiling():
    # A cap at or above the grid ceiling can never bind (the effective limit is
    # min(grid ceiling, cap)), so it is legal slack regardless of grid alignment —
    # lowering only the grid ceiling must not force a matching cap edit. 70 is
    # deliberately off the 0/20/40/60 grid; 60 pins the == boundary.
    for cap in (60, 70, 80, 100):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=cap),
            DecisionConfig(ai_target_margin_max_pct=60, target_margin_step_pct=20),
        )


def test_effective_max_target_margin_pct_is_min_of_grid_and_cap():
    # Defaults: grid max 100, cap 60 -> the prompt must advertise 60.
    assert risk_gate.effective_max_target_margin_pct(RiskConfig(), DecisionConfig()) == 60
    # Cap at/above the grid ceiling: the grid ceiling stays the advertised max.
    assert (
        risk_gate.effective_max_target_margin_pct(
            RiskConfig(max_target_margin_pct=100),
            DecisionConfig(ai_target_margin_max_pct=40),
        )
        == 40
    )


def test_risk_gate_result_rejects_illegal_combinations():
    # evaluate() only builds legal shapes; a hand-built inconsistent result
    # (PR 3 fixture, flip re-run) must die at construction, not in sizing math.
    from dataclasses import replace

    ok = _evaluate(_parsed(margin=20))
    with pytest.raises(ValueError, match="no_order_reason"):
        replace(ok, order_created=True, no_order_reason="x")
    with pytest.raises(ValueError, match="sized target"):
        replace(ok, target_margin=None)
    # Genuinely clamped (approved 60 < requested 61) so dropping risk_reason
    # isolates the reason-coupling check, not the strict-reduce invariant below.
    clamped = _evaluate(_parsed(margin=61))
    with pytest.raises(ValueError, match="risk_reason"):
        replace(clamped, risk_reason=None)

    maintain = _evaluate(_parsed(mode="maintain_current", side=None, margin=None, confidence=None))
    with pytest.raises(ValueError, match="sized target"):
        replace(maintain, target_margin=Decimal(1))
    with pytest.raises(ValueError, match="never creates an order"):
        replace(maintain, order_created=True, no_order_reason=None)

    # A rejected/fail-closed result must name why, symmetric with the CLAMPED guard —
    # else a contradictory ParsedDecision could produce such a row with a null reason.
    failed = _evaluate(_invalid_parsed())
    with pytest.raises(ValueError, match="name why"):
        replace(failed, risk_reason=None)
    rejected = _evaluate(_parsed(margin=40, confidence="0.1"))
    with pytest.raises(ValueError, match="name why"):
        replace(rejected, risk_reason=None)
    # And neither REJECTED nor INVALID_FAIL_CLOSED may ride on a sized set_target.
    ok_sized = _evaluate(_parsed(margin=20))
    with pytest.raises(ValueError, match="collapse to maintain_current"):
        replace(ok_sized, risk_action=RiskAction.REJECTED, risk_reason="low_confidence")


def test_risk_gate_result_rejects_sign_and_margin_inconsistencies():
    # Completed invariants (mirroring CurrentPositionState): the signed notional's
    # sign must match target_side, risk can only shrink the request, and a flat
    # target carries zero margin/notional. A hand-built flip re-run (PR 3) that
    # violates any of these must die at construction, not in order-sizing math.
    from dataclasses import replace

    ok = _evaluate(_parsed(margin=20))  # LONG, approved == requested == 20, +notional
    with pytest.raises(ValueError, match="short target must carry a negative"):
        replace(ok, target_side=TargetSide.SHORT)
    with pytest.raises(ValueError, match="flat target carries zero"):
        replace(ok, target_side=TargetSide.FLAT)
    with pytest.raises(ValueError, match="approved margin can never exceed requested"):
        replace(ok, approved_target_margin_pct=99)
    with pytest.raises(ValueError, match="keeps approved == requested"):
        replace(ok, approved_target_margin_pct=10)

    clamped = _evaluate(_parsed(margin=61))  # requested 61 -> approved 60, CLAMPED
    with pytest.raises(ValueError, match="strictly reduce"):
        replace(clamped, approved_target_margin_pct=61)


def test_risk_gate_result_rejects_cross_field_math_mismatch():
    # The sized fields are one quantity in three encodings (margin, notional,
    # signed notional) plus its delta against the current position; a hand-built
    # result (PR 3 flip re-run) whose encodings disagree must die at
    # construction, not corrupt order sizing downstream. Each replace() below
    # breaks exactly one identity, leaving the others intact.
    from dataclasses import replace

    ok = _evaluate(_parsed(margin=20))  # LONG on a flat book: delta == signed notional
    with pytest.raises(ValueError, match="configured_leverage"):
        replace(ok, target_margin=ok.target_margin * 2)
    with pytest.raises(ValueError, match="magnitude"):
        replace(
            ok,
            target_signed_notional=ok.target_signed_notional * 2,
            delta_notional=ok.target_signed_notional * 2 - ok.current_signed_notional,
        )
    with pytest.raises(ValueError, match="delta_notional must equal"):
        replace(ok, delta_notional=ok.delta_notional + 1)
    # FLAT completion: zeroing the pct fields and signed notional is not enough —
    # the margin/notional encodings must be zero too.
    with pytest.raises(ValueError, match="flat target carries zero"):
        replace(
            ok,
            target_side=TargetSide.FLAT,
            requested_target_margin_pct=0,
            approved_target_margin_pct=0,
            target_signed_notional=Decimal(0),
            delta_notional=Decimal(0) - ok.current_signed_notional,
        )


def test_current_position_state_rejects_inconsistent_fields():
    with pytest.raises(ValueError, match="flat"):
        CurrentPositionState(side=None, signed_notional=Decimal(1), margin_pct=None)
    with pytest.raises(ValueError, match="long/short"):
        CurrentPositionState(side=TargetSide.FLAT, signed_notional=Decimal(0), margin_pct=None)
    # side/sign disagreement corrupts delta_notional sizing — long is positive, short
    # negative — so reject a mismatch (and a zero) at construction.
    with pytest.raises(ValueError, match="long position must have positive"):
        CurrentPositionState(
            side=TargetSide.LONG, signed_notional=Decimal(-500), margin_pct=Decimal(10)
        )
    with pytest.raises(ValueError, match="short position must have negative"):
        CurrentPositionState(
            side=TargetSide.SHORT, signed_notional=Decimal(500), margin_pct=Decimal(10)
        )
    # margin_pct is a magnitude (>= 0).
    with pytest.raises(ValueError, match="margin_pct"):
        CurrentPositionState(
            side=TargetSide.LONG, signed_notional=Decimal(500), margin_pct=Decimal(-1)
        )
    # A flat position carries no leverage, and a known leverage must be > 0.
    with pytest.raises(ValueError, match="flat"):
        CurrentPositionState(
            side=None, signed_notional=Decimal(0), margin_pct=None, leverage=Decimal(1)
        )
    with pytest.raises(ValueError, match="leverage"):
        CurrentPositionState(
            side=TargetSide.LONG,
            signed_notional=Decimal(500),
            margin_pct=Decimal(10),
            leverage=Decimal(0),
        )


def test_result_to_dict_is_json_ready():
    import json

    result = _evaluate(_parsed(margin=61))
    payload = result.to_dict()
    json.dumps(payload)  # no Decimal leak
    assert payload["risk_action"] == "clamped"
    assert payload["requested_target_margin_pct"] == 61
    assert payload["approved_target_margin_pct"] == 60
    assert payload["target_margin"] == "600"


def test_result_to_dict_is_json_ready_for_no_order_variants():
    import json

    # The audit record serializes to_dict() for every outcome, so the
    # None-heavy shapes (rejected / invalid_fail_closed / maintain) must be
    # JSON-ready too, not just the sized happy path.
    variants = {
        "rejected": _evaluate(_parsed(margin=40, confidence="0.29")),
        "invalid_fail_closed": _evaluate(_invalid_parsed()),
        "approved": _evaluate(
            _parsed(mode="maintain_current", side=None, margin=None, confidence=None)
        ),
    }
    for action, result in variants.items():
        payload = result.to_dict()
        json.dumps(payload)  # no Decimal leak on the None-populated shape
        assert payload["risk_action"] == action
        assert payload["target_margin"] is None
        assert payload["approved_target_margin_pct"] is None
