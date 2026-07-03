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


def test_low_confidence_set_target_fails_closed():
    result = _evaluate(_parsed(margin=40, confidence="0.29"))
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
    assert result.risk_reason == risk_gate.RISK_REASON_LOW_CONFIDENCE
    assert result.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_INVALID_FAIL_CLOSED
    # The audit row still shows what the AI asked for; no sized target leaves.
    assert result.requested_target_margin_pct == 40
    assert result.approved_target_margin_pct is None
    assert result.target_margin is None


def test_confidence_at_threshold_passes():
    result = _evaluate(_parsed(margin=40, confidence="0.3"))
    assert result.risk_action is RiskAction.APPROVED


def test_low_confidence_does_not_gate_maintain_current():
    # The threshold only blocks "high target, low conviction" contradictions;
    # maintain_current has no target to block.
    result = _evaluate(_parsed(mode="maintain_current", side=None, margin=None, confidence="0.1"))
    assert result.risk_action is RiskAction.APPROVED
    assert result.no_order_reason == risk_gate.NO_ORDER_MAINTAIN_CURRENT


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
    current = _long_state(margin_pct="60", notional="600")
    result = _evaluate(_parsed(margin=100), current=current)
    assert result.risk_action is RiskAction.CLAMPED
    assert result.approved_target_margin_pct == 60
    assert result.requested_target_margin_pct == 100
    assert result.order_created is False
    assert result.no_order_reason == risk_gate.NO_ORDER_WITHIN_DEADBAND


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
    # so the (safety-neutral) optimisation is skipped and the order executes.
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


def test_zero_equity_directional_target_fails_closed():
    result = _evaluate(_parsed(margin=40), equity=Decimal(0))
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
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


def test_no_capacity_fails_closed_instead_of_full_close():
    # Cross margin fully committed elsewhere: the cap snaps to 0. A directional
    # target must fail closed — not approve long+0, which would emit an order
    # that closes the whole position on a "stay long" request.
    current = _long_state(margin_pct="35", notional="350")
    result = _evaluate(_parsed(margin=40), current=current, other_used_margin=Decimal("1000"))
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
    assert result.risk_reason == risk_gate.RISK_REASON_AVAILABLE_MARGIN
    assert result.order_created is False
    assert result.target_margin is None


def test_cap_below_grid_minimum_fails_closed_not_snapped_up():
    # min=10 but only 5% of equity is genuinely available: snapping up to the
    # grid minimum would approve margin that does not exist.
    cfg = DecisionConfig(ai_target_margin_min_pct=10)
    result = _evaluate(_parsed(margin=40), cfg=cfg, other_used_margin=Decimal("950"))
    assert result.risk_action is RiskAction.INVALID_FAIL_CLOSED
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
    )
    state = current_position_state(position, _EQUITY, mark_price=Decimal("60000"))
    assert state.side is TargetSide.LONG
    assert state.signed_notional == Decimal("600")  # size * mark, not entry
    assert state.margin_pct == Decimal("35")  # committed margin / equity


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
    # directional target would clamp to 0 and fail closed — reject the pairing.
    # (a) cap below a non-zero grid minimum.
    with pytest.raises(ValueError, match="fail closed"):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=5),
            DecisionConfig(ai_target_margin_min_pct=10, ai_target_margin_max_pct=100),
        )
    # (b) grid starts at 0 but the cap is below one step (snaps to 0).
    with pytest.raises(ValueError, match="fail closed"):
        risk_gate.validate_risk_decision_config(
            RiskConfig(max_target_margin_pct=3),
            DecisionConfig(
                ai_target_margin_min_pct=0, ai_target_margin_max_pct=100, target_margin_step_pct=5
            ),
        )
    # A cap that snaps onto a real grid value is fine (no raise).
    risk_gate.validate_risk_decision_config(RiskConfig(), DecisionConfig())


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

    # A fail-closed result must name why, symmetric with the CLAMPED guard — else a
    # contradictory ParsedDecision could produce a fail-closed row with a null reason.
    failed = _evaluate(_invalid_parsed())
    with pytest.raises(ValueError, match="name why"):
        replace(failed, risk_reason=None)


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


def test_result_to_dict_is_json_ready():
    import json

    result = _evaluate(_parsed(margin=61))
    payload = result.to_dict()
    json.dumps(payload)  # no Decimal leak
    assert payload["risk_action"] == "clamped"
    assert payload["requested_target_margin_pct"] == 61
    assert payload["approved_target_margin_pct"] == 60
    assert payload["target_margin"] == "600"
