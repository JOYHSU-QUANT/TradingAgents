"""Phase 2 RiskGate — deterministic, pure (no I/O, no clock), all-Decimal.

Takes one :class:`~.target_decision.ParsedDecision` plus the current account /
position state and produces the sized, risk-checked outcome whose fields align
with ``phase2-data.md`` §7 (``ai_outputs``):

- step / range / type validation of the requested margin (integer grid);
- the ``min_confidence`` gate (**set_target only** — confidence never scales
  sizing, phase2-spec.md §2.4);
- clamp to ``max_target_margin_pct`` with both requested and approved values
  preserved (``risk_action = clamped``, phase2-spec.md §2.3);
- the rebalance deadband — same-side targets within
  ``rebalance_deadband_pct`` of the current margin allocation create no order
  (``no_order_reason = within_deadband``); flips and flat closes are exempt;
- independent ``effective_leverage`` and available-margin checks, so the gate
  never relies on the margin-allocation cap alone (spec §2.3).

The gate is re-run deterministically (never re-asking the AI) before a flip's
open leg in PR 3, which is why it re-validates its input decision instead of
trusting the parse seam.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from enum import Enum
from typing import Any

from .schema import PerpPosition
from .target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetSide,
    config_overrides,
    decimal_from_yaml,
    validate_target_decision,
)

__all__ = [
    "CurrentPositionState",
    "RiskAction",
    "RiskConfig",
    "RiskGateResult",
    "current_position_state",
    "evaluate",
]

# Stable machine tags for ai_outputs.risk_reason / no_order_reason.
RISK_REASON_LOW_CONFIDENCE = "low_confidence"
RISK_REASON_MAX_TARGET_MARGIN = "exceeds_max_target_margin_pct"
RISK_REASON_EFFECTIVE_LEVERAGE = "effective_leverage_cap"
RISK_REASON_AVAILABLE_MARGIN = "insufficient_available_margin"
RISK_REASON_NO_EQUITY = "no_account_equity"

NO_ORDER_MAINTAIN_CURRENT = "maintain_current"
NO_ORDER_WITHIN_DEADBAND = "within_deadband"
NO_ORDER_INVALID_FAIL_CLOSED = "invalid_fail_closed"
NO_ORDER_ALREADY_FLAT = "already_flat"
NO_ORDER_ZERO_DELTA = "zero_delta"


class RiskAction(str, Enum):
    """How the gate disposed of the requested target (phase2-data.md §7)."""

    APPROVED = "approved"
    CLAMPED = "clamped"
    INVALID_FAIL_CLOSED = "invalid_fail_closed"


@dataclass(frozen=True)
class RiskConfig:
    """Typed view of the YAML ``risk:`` block (phase2-spec.md §2 defaults)."""

    leverage: Decimal = Decimal(1)
    margin_mode: str = "cross"
    max_target_margin_pct: int = 60

    def __post_init__(self) -> None:
        if self.leverage <= 0:
            raise ValueError(f"risk.leverage must be > 0, got {self.leverage}")
        if self.margin_mode != "cross":
            # Phase 2 is cross-only (spec §2.2); reject rather than silently run
            # isolated-margin sizing math that does not exist yet.
            raise ValueError(
                f"risk.margin_mode must be 'cross' in Phase 2, got {self.margin_mode!r}"
            )
        if not 0 < self.max_target_margin_pct <= 100:
            raise ValueError(
                f"risk.max_target_margin_pct must be in (0, 100], got {self.max_target_margin_pct}"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> RiskConfig:
        """Parse the raw YAML block; absent or null keys use the field defaults."""
        return cls(
            **config_overrides(
                cfg,
                {
                    "leverage": decimal_from_yaml,
                    "margin_mode": str,
                    "max_target_margin_pct": int,
                },
            )
        )


@dataclass(frozen=True)
class CurrentPositionState:
    """The slice of live account state the gate needs, precomputed by the caller.

    ``side`` is ``None`` when flat. ``margin_pct`` is the committed margin as a
    percent of account equity (the established exposure basis — committed
    margin, not gross notional); ``None`` when a sized position carries no
    usable ``margin_used``, in which case the deadband cannot be evaluated and
    is skipped (the order executes — the deadband is a churn optimisation, not
    a safety check).
    """

    side: TargetSide | None
    signed_notional: Decimal
    margin_pct: Decimal | None

    @classmethod
    def flat(cls) -> CurrentPositionState:
        return cls(side=None, signed_notional=Decimal(0), margin_pct=None)


def current_position_state(
    position: PerpPosition | None, account_equity: Decimal, mark_price: Decimal
) -> CurrentPositionState:
    """Derive the gate's position inputs from a live position read.

    ``signed_notional = size * mark_price`` (execution §6.1 — sizing and
    valuation use **mark**, never mid). ``margin_pct`` comes from the position's
    committed margin; unknown margin on a sized position yields ``None`` rather
    than a guess.
    """
    if position is None:
        return CurrentPositionState.flat()
    signed_notional = position.size * mark_price
    margin_pct: Decimal | None = None
    if position.margin_used is not None and position.margin_used > 0 and account_equity > 0:
        margin_pct = position.margin_used / account_equity * 100
    return CurrentPositionState(
        side=TargetSide.LONG if position.is_long else TargetSide.SHORT,
        signed_notional=signed_notional,
        margin_pct=margin_pct,
    )


@dataclass(frozen=True)
class RiskGateResult:
    """The gate's outcome, field-aligned with ``phase2-data.md`` §7 ``ai_outputs``.

    ``requested_target_margin_pct`` / ``approved_target_margin_pct`` are both
    kept whenever a clamp occurred (spec §2.3). All target/delta fields are
    ``None``/``0`` for ``maintain_current`` and for fail-closed outcomes: a
    rejected decision must never carry a sized target downstream.
    """

    decision_mode: DecisionMode
    target_side: TargetSide | None
    requested_target_margin_pct: int | None
    approved_target_margin_pct: int | None
    risk_action: RiskAction
    risk_reason: str | None
    order_created: bool
    no_order_reason: str | None
    target_margin: Decimal | None
    target_notional: Decimal | None
    target_signed_notional: Decimal | None
    current_signed_notional: Decimal
    delta_notional: Decimal
    configured_leverage: Decimal
    confidence: Decimal | None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict; Decimals become strings so no precision is lost."""

        def _s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "decision_mode": self.decision_mode.value,
            "target_side": self.target_side.value if self.target_side else None,
            "requested_target_margin_pct": self.requested_target_margin_pct,
            "approved_target_margin_pct": self.approved_target_margin_pct,
            "risk_action": self.risk_action.value,
            "risk_reason": self.risk_reason,
            "order_created": self.order_created,
            "no_order_reason": self.no_order_reason,
            "target_margin": _s(self.target_margin),
            "target_notional": _s(self.target_notional),
            "target_signed_notional": _s(self.target_signed_notional),
            "current_signed_notional": str(self.current_signed_notional),
            "delta_notional": str(self.delta_notional),
            "configured_leverage": str(self.configured_leverage),
            "confidence": _s(self.confidence),
        }


def _no_target_result(
    *,
    risk_action: RiskAction,
    risk_reason: str | None,
    no_order_reason: str,
    current: CurrentPositionState,
    risk: RiskConfig,
    requested: int | None = None,
    target_side: TargetSide | None = None,
    confidence: Decimal | None = None,
) -> RiskGateResult:
    """A no-target outcome: maintain-current shape, no sized target, no order.

    The one constructor behind both the valid ``maintain_current`` result and
    every fail-closed rejection, so a future ``RiskGateResult`` field is
    threaded through a single site.
    """
    return RiskGateResult(
        decision_mode=DecisionMode.MAINTAIN_CURRENT,
        target_side=target_side,
        requested_target_margin_pct=requested,
        approved_target_margin_pct=None,
        risk_action=risk_action,
        risk_reason=risk_reason,
        order_created=False,
        no_order_reason=no_order_reason,
        target_margin=None,
        target_notional=None,
        target_signed_notional=None,
        current_signed_notional=current.signed_notional,
        delta_notional=Decimal(0),
        configured_leverage=risk.leverage,
        confidence=confidence,
    )


def _fail_closed(
    *,
    risk_reason: str | None,
    current: CurrentPositionState,
    risk: RiskConfig,
    requested: int | None = None,
    target_side: TargetSide | None = None,
    confidence: Decimal | None = None,
) -> RiskGateResult:
    """The maintain-current outcome every rejected decision collapses to.

    ``requested``/``target_side``/``confidence`` are preserved when known (e.g.
    a low-confidence rejection) so the audit row still shows what the AI asked
    for, but no sized target ever leaves a fail-closed result.
    """
    return _no_target_result(
        risk_action=RiskAction.INVALID_FAIL_CLOSED,
        risk_reason=risk_reason,
        no_order_reason=NO_ORDER_INVALID_FAIL_CLOSED,
        current=current,
        risk=risk,
        requested=requested,
        target_side=target_side,
        confidence=confidence,
    )


def _snap_down_to_grid(pct: Decimal, decision_cfg: DecisionConfig) -> int:
    """Largest grid value <= ``pct``, or ``0`` when no grid value fits under it.

    Used only for gate-side *reductions* (the allocation / leverage /
    available-margin caps), where rounding down is the conservative direction —
    this is not the forbidden "silent rounding" of an AI request, which is
    always rejected. ``0`` is the no-capacity sentinel: snapping up to the grid
    minimum here would approve margin that does not exist, so the caller fails
    a directional target closed instead.
    """
    low = Decimal(decision_cfg.ai_target_margin_min_pct)
    step = Decimal(decision_cfg.target_margin_step_pct)
    if pct < low:
        return 0
    steps = ((pct - low) / step).to_integral_value(rounding=ROUND_FLOOR)
    return int(low + steps * step)


def evaluate(
    parsed: ParsedDecision,
    *,
    account_equity: Decimal,
    current: CurrentPositionState,
    risk: RiskConfig,
    decision_cfg: DecisionConfig,
    other_used_margin: Decimal = Decimal(0),
    other_positions_notional: Decimal = Decimal(0),
) -> RiskGateResult:
    """Gate one parsed decision against account state; pure and deterministic.

    ``other_used_margin`` / ``other_positions_notional`` describe cross-margin
    state committed to *other* symbols, so the available-margin and
    effective-leverage checks stay independent of the per-target allocation cap
    (spec §2.3); Phase 2 runs a single coin, so they default to zero.
    """
    if other_used_margin < 0 or other_positions_notional < 0:
        raise ValueError("other_used_margin / other_positions_notional must be >= 0")

    decision = parsed.decision

    # 1. Parse-seam verdict, then re-validate the decision object itself: the
    # flip re-run (PR 3) feeds decisions back through this gate, and it must
    # fail closed on a hand-built invalid combination, not act on it.
    if not parsed.is_valid:
        return _fail_closed(risk_reason=parsed.invalid_reason, current=current, risk=risk)
    invalid = validate_target_decision(decision, decision_cfg)
    if invalid is not None:
        return _fail_closed(risk_reason=invalid, current=current, risk=risk)

    # 2. A valid maintain_current: keep the position, no target, no order.
    if decision.decision_mode is DecisionMode.MAINTAIN_CURRENT:
        return _no_target_result(
            risk_action=RiskAction.APPROVED,
            risk_reason=None,
            no_order_reason=NO_ORDER_MAINTAIN_CURRENT,
            current=current,
            risk=risk,
            confidence=decision.confidence,
        )

    # set_target from here on; validate_target_decision guarantees these.
    side = decision.target_side
    requested = decision.requested_target_margin_pct
    confidence = decision.confidence
    assert side is not None and requested is not None and confidence is not None

    # 3. min_confidence gates set_target only; it never scales sizing (§2.4).
    if confidence < decision_cfg.min_confidence:
        return _fail_closed(
            risk_reason=RISK_REASON_LOW_CONFIDENCE,
            current=current,
            risk=risk,
            requested=requested,
            target_side=side,
            confidence=confidence,
        )

    # 4. Sizing needs positive equity; a zero/unknown account cannot host a
    # directional target. (An explicit flat close of nothing is a no-op below.)
    if account_equity <= 0 and side is not TargetSide.FLAT:
        return _fail_closed(
            risk_reason=RISK_REASON_NO_EQUITY,
            current=current,
            risk=risk,
            requested=requested,
            target_side=side,
            confidence=confidence,
        )

    # 5. Clamp chain. Start from the allocation cap (§2.3), then apply the two
    # independent account-state checks — the approved value is the most
    # conservative of the three, snapped *down* to the grid, with the binding
    # constraint recorded.
    approved = requested
    risk_action = RiskAction.APPROVED
    risk_reason: str | None = None

    if approved > risk.max_target_margin_pct:
        # Snapped like the other caps so the approved value always sits on the
        # decision grid even when max_target_margin_pct itself is off it.
        approved = _snap_down_to_grid(Decimal(risk.max_target_margin_pct), decision_cfg)
        risk_action = RiskAction.CLAMPED
        risk_reason = RISK_REASON_MAX_TARGET_MARGIN

    if account_equity > 0:
        # Available margin: this target's margin plus what other symbols already
        # commit must fit inside equity (cross margin, spec §2.3 independence).
        available = account_equity - other_used_margin
        available_pct_cap = _snap_down_to_grid(
            max(available, Decimal(0)) / account_equity * 100, decision_cfg
        )
        if approved > available_pct_cap:
            approved = available_pct_cap
            risk_action = RiskAction.CLAMPED
            risk_reason = RISK_REASON_AVAILABLE_MARGIN

        # Effective leverage: projected total notional over equity must stay
        # within the configured leverage (execution §6.1), independent of the
        # allocation cap.
        max_total_notional = account_equity * risk.leverage
        headroom_notional = max_total_notional - other_positions_notional
        leverage_pct_cap = _snap_down_to_grid(
            max(headroom_notional, Decimal(0)) / risk.leverage / account_equity * 100,
            decision_cfg,
        )
        if approved > leverage_pct_cap:
            approved = leverage_pct_cap
            risk_action = RiskAction.CLAMPED
            risk_reason = RISK_REASON_EFFECTIVE_LEVERAGE

    if side is not TargetSide.FLAT and approved == 0:
        # The clamp chain found no legal grid capacity for a directional target
        # (allocation cap / available margin / leverage headroom below the grid
        # minimum). Approving long/short + 0 would violate the contract
        # invariant and turn a "stay long" request into a full close — fail
        # closed with the binding constraint instead.
        return _fail_closed(
            risk_reason=risk_reason,
            current=current,
            risk=risk,
            requested=requested,
            target_side=side,
            confidence=confidence,
        )

    # 6. Size the approved target (all mark-based, execution §6.2).
    target_margin = account_equity * Decimal(approved) / 100 if account_equity > 0 else Decimal(0)
    target_notional = target_margin * risk.leverage
    direction = {
        TargetSide.LONG: Decimal(1),
        TargetSide.SHORT: Decimal(-1),
        TargetSide.FLAT: Decimal(0),
    }[side]
    target_signed_notional = direction * target_notional
    delta_notional = target_signed_notional - current.signed_notional

    # 7. Order-necessity rules.
    order_created = True
    no_order_reason: str | None = None
    if side is TargetSide.FLAT and current.side is None:
        # An explicit flat against an already-flat account is a no-op (DESIGN
        # Part 2) — not a deadband case, so record its own reason.
        order_created = False
        no_order_reason = NO_ORDER_ALREADY_FLAT
    elif side is current.side and current.margin_pct is not None:
        # Deadband applies only to same-side rebalances (spec §2.4); flips and
        # flat closes execute no matter how small the difference is.
        if abs(Decimal(approved) - current.margin_pct) < decision_cfg.rebalance_deadband_pct:
            order_created = False
            no_order_reason = NO_ORDER_WITHIN_DEADBAND
    if order_created and delta_notional == 0:
        # Nothing to trade even outside the deadband (e.g. exact same target on
        # an unknown-margin position) — creating a zero-quantity order would be
        # meaningless noise downstream. A dedicated tag keeps deadband analytics
        # honest: this is an exact-delta no-op, not churn suppression.
        order_created = False
        no_order_reason = NO_ORDER_ZERO_DELTA

    return RiskGateResult(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=side,
        requested_target_margin_pct=requested,
        approved_target_margin_pct=approved,
        risk_action=risk_action,
        risk_reason=risk_reason,
        order_created=order_created,
        no_order_reason=no_order_reason,
        target_margin=target_margin,
        target_notional=target_notional,
        target_signed_notional=target_signed_notional,
        current_signed_notional=current.signed_notional,
        delta_notional=delta_notional,
        configured_leverage=risk.leverage,
        confidence=confidence,
    )
