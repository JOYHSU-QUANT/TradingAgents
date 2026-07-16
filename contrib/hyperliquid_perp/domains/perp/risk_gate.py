"""Phase 2 RiskGate — deterministic, pure (no I/O, no clock), all-Decimal.

Takes one :class:`~.target_decision.ParsedDecision` plus the current account /
position state and produces the sized, risk-checked outcome whose fields align
with ``phase2-data.md`` §7 (``ai_outputs``):

- step / range / type validation of the requested margin (integer grid);
- the ``min_confidence`` gate (**set_target only** — confidence never scales
  sizing, phase2-spec.md §2.4), plus the higher ``resize_min_confidence`` bar
  for same-side resizes (churn control, spec §2.4);
- clamp to ``max_target_margin_pct`` with both requested and approved values
  preserved (``risk_action = clamped``, phase2-spec.md §2.3);
- the rebalance deadband — same-side targets less than
  ``rebalance_deadband_pct`` away from the current margin allocation create no
  order (``no_order_reason = within_deadband``); flips and flat closes are exempt;
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

from .config_coercion import config_overrides, decimal_from_yaml, int_from_yaml
from .schema import PerpPosition
from .target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetSide,
    validate_target_decision,
)

__all__ = [
    "CurrentPositionState",
    "MarginMode",
    "RiskAction",
    "RiskConfig",
    "RiskGateResult",
    "current_position_state",
    "effective_max_target_margin_pct",
    "evaluate",
]

# Stable machine tags for ai_outputs.risk_reason / no_order_reason.
RISK_REASON_LOW_CONFIDENCE = "low_confidence"
RISK_REASON_LOW_CONFIDENCE_RESIZE = "low_confidence_resize"
RISK_REASON_MAX_TARGET_MARGIN = "exceeds_max_target_margin_pct"
RISK_REASON_EFFECTIVE_LEVERAGE = "effective_leverage_cap"
RISK_REASON_AVAILABLE_MARGIN = "insufficient_available_margin"
RISK_REASON_NO_EQUITY = "no_account_equity"

NO_ORDER_MAINTAIN_CURRENT = "maintain_current"
NO_ORDER_WITHIN_DEADBAND = "within_deadband"
NO_ORDER_INVALID_FAIL_CLOSED = "invalid_fail_closed"
NO_ORDER_REJECTED = "rejected"
NO_ORDER_ALREADY_FLAT = "already_flat"
NO_ORDER_ZERO_DELTA = "zero_delta"


class RiskAction(str, Enum):
    """How the gate disposed of the requested target (phase2-data.md §7).

    ``INVALID_FAIL_CLOSED`` is reserved for contract violations — output the
    parse seam or re-validation could not accept. ``REJECTED`` is a
    schema-valid ``set_target`` the gate refused (``risk_reason`` names why:
    low confidence — the base bar or the higher same-side-resize bar — no
    equity, no grid capacity under the binding cap).
    Keeping them distinct lets alerting treat REJECTED as normal operation
    and INVALID_FAIL_CLOSED as the model-drift alarm.
    """

    APPROVED = "approved"
    CLAMPED = "clamped"
    REJECTED = "rejected"
    INVALID_FAIL_CLOSED = "invalid_fail_closed"


class MarginMode(str, Enum):
    """The exchange margin modes this module can size for.

    Phase 2 is cross-only (spec §2.2); ``ISOLATED`` is added when its sizing
    math exists, so an isolated config can never silently run cross math.
    """

    CROSS = "cross"


@dataclass(frozen=True)
class RiskConfig:
    """Typed view of the YAML ``risk:`` block (phase2-spec.md §2 defaults)."""

    leverage: Decimal = Decimal(1)
    margin_mode: MarginMode = MarginMode.CROSS
    max_target_margin_pct: int = 60

    def __post_init__(self) -> None:
        if self.leverage <= 0:
            raise ValueError(f"risk.leverage must be > 0, got {self.leverage}")
        try:
            # Accept the YAML string form and normalise it to the enum, so the
            # field is always a MarginMode after construction.
            object.__setattr__(self, "margin_mode", MarginMode(self.margin_mode))
        except ValueError:
            raise ValueError(
                f"risk.margin_mode must be 'cross' in Phase 2, got {self.margin_mode!r}"
            ) from None
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
                    "max_target_margin_pct": int_from_yaml,
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
    is skipped — order necessity then falls to the zero-delta check and the
    resize confidence bar (the deadband is a churn optimisation, not a safety
    check). ``leverage`` is the position's *actual* leverage from the
    exchange (``None`` when unreported): margin%% only tracks notional when it
    matches the configured ``risk.leverage``, so ``evaluate`` disables the
    deadband on a known mismatch (e.g. a manually opened position) rather than
    report a 5x-larger true exposure as "target achieved".
    """

    side: TargetSide | None
    signed_notional: Decimal
    margin_pct: Decimal | None
    leverage: Decimal | None = None

    def __post_init__(self) -> None:
        if self.side is None:
            if (
                self.signed_notional != 0
                or self.margin_pct is not None
                or self.leverage is not None
            ):
                raise ValueError("a flat position carries no notional, margin_pct, or leverage")
        elif self.side is TargetSide.FLAT:
            # A *position* is long/short or absent; FLAT is a target, not a state.
            raise ValueError("position side must be long/short, or None when flat")
        else:
            # ``signed_notional`` feeds ``delta_notional`` sizing in ``evaluate``; a
            # side/sign disagreement (hand-built by PR 3's flip re-run) would silently
            # corrupt the order size rather than fail loud. Long is positive, short
            # negative — and ``size != 0`` (PerpPosition) makes zero impossible.
            if self.side is TargetSide.LONG and self.signed_notional <= 0:
                raise ValueError("a long position must have positive signed_notional")
            if self.side is TargetSide.SHORT and self.signed_notional >= 0:
                raise ValueError("a short position must have negative signed_notional")
        # ``margin_pct`` is a committed-margin percentage — a magnitude, never
        # negative (mirrors the >= 0 magnitude guards on AccountSnapshot).
        if self.margin_pct is not None and self.margin_pct < 0:
            raise ValueError("margin_pct is a magnitude and must be >= 0")
        if self.leverage is not None and self.leverage <= 0:
            raise ValueError("position leverage must be > 0 when known")

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
    than a guess. ``leverage`` is passed through from the exchange read so the
    deadband can verify the margin%%-tracks-notional assumption.
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
        leverage=position.leverage,
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

    def __post_init__(self) -> None:
        """Reject illegal field combinations at construction.

        ``evaluate()`` only builds legal shapes, but PR 3's execution engine
        consumes this type directly and the flip re-run hand-builds inputs —
        an inconsistent result must die here, not deep in order-sizing math.
        """
        if self.order_created == (self.no_order_reason is not None):
            raise ValueError("exactly one of order_created / no_order_reason must be set")
        sized = (self.target_margin, self.target_notional, self.target_signed_notional)
        if self.decision_mode is DecisionMode.SET_TARGET:
            if (
                self.target_side is None
                or self.requested_target_margin_pct is None
                or self.approved_target_margin_pct is None
                or self.confidence is None
                or any(v is None for v in sized)
            ):
                raise ValueError("a set_target result must carry a fully sized target")
            if self.risk_action in (RiskAction.INVALID_FAIL_CLOSED, RiskAction.REJECTED):
                raise ValueError("a rejected/fail-closed result must collapse to maintain_current")
            # The signed notional's sign must agree with target_side, mirroring
            # CurrentPositionState — PR 3's flip re-run hand-builds this result and
            # a sign mismatch would silently corrupt order sizing downstream.
            signed = self.target_signed_notional
            if self.target_side is TargetSide.LONG and signed <= 0:
                raise ValueError("a long target must carry a positive signed notional")
            if self.target_side is TargetSide.SHORT and signed >= 0:
                raise ValueError("a short target must carry a negative signed notional")
            if self.target_side is TargetSide.FLAT and (
                signed != 0
                or self.requested_target_margin_pct != 0
                or self.approved_target_margin_pct != 0
                or self.target_margin != 0
                or self.target_notional != 0
            ):
                raise ValueError("a flat target carries zero signed notional and zero margin")
            # The sized fields are one quantity in three encodings, so they must
            # agree exactly — evaluate() computes them with pure Decimal math and
            # PR 3's hand-built flip legs get no rounding slack either.
            if self.target_notional != self.target_margin * self.configured_leverage:
                raise ValueError("target_notional must equal target_margin * configured_leverage")
            if abs(self.target_signed_notional) != self.target_notional:
                raise ValueError("target_signed_notional magnitude must equal target_notional")
            if self.delta_notional != self.target_signed_notional - self.current_signed_notional:
                raise ValueError(
                    "delta_notional must equal target_signed_notional - current_signed_notional"
                )
            # Risk can only shrink the request, never grow it, and the action must
            # match the numeric relationship it claims.
            if self.approved_target_margin_pct > self.requested_target_margin_pct:
                raise ValueError("approved margin can never exceed requested")
            if (
                self.risk_action is RiskAction.CLAMPED
                and self.approved_target_margin_pct >= self.requested_target_margin_pct
            ):
                raise ValueError("a clamped result must strictly reduce the approved margin")
            if (
                self.risk_action is RiskAction.APPROVED
                and self.approved_target_margin_pct != self.requested_target_margin_pct
            ):
                raise ValueError("an approved (unclamped) result keeps approved == requested")
        else:  # MAINTAIN_CURRENT — valid maintain or any fail-closed rejection
            if (
                self.approved_target_margin_pct is not None
                or any(v is not None for v in sized)
                or self.delta_notional != 0
            ):
                raise ValueError("a maintain_current result must not carry a sized target")
            if self.order_created:
                raise ValueError("maintain_current never creates an order")
            if self.risk_action is RiskAction.CLAMPED:
                raise ValueError("clamping applies only to set_target results")
        if self.risk_action is RiskAction.CLAMPED and self.risk_reason is None:
            raise ValueError("a clamped result must name the binding cap in risk_reason")
        if self.risk_action is RiskAction.APPROVED and self.risk_reason is not None:
            raise ValueError("an approved result carries no risk_reason")
        if (
            self.risk_action in (RiskAction.INVALID_FAIL_CLOSED, RiskAction.REJECTED)
            and self.risk_reason is None
        ):
            # Symmetric with the CLAMPED guard: a rejected/fail-closed audit row must
            # record *why*. A contradictory ``ParsedDecision`` (is_valid False,
            # invalid_reason None) would otherwise reach here with a null reason.
            raise ValueError("a rejected/fail-closed result must name why in risk_reason")

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict; Decimals become strings so no precision is lost."""

        def _s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {
            "decision_mode": self.decision_mode.value,
            "target_side": self.target_side.value if self.target_side is not None else None,
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

    The one constructor behind the valid ``maintain_current`` result, every
    healthy risk rejection (REJECTED), and every fail-closed contract violation,
    so a future ``RiskGateResult`` field is threaded through a single site.
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


def _rejected(
    *,
    risk_reason: str | None,
    current: CurrentPositionState,
    risk: RiskConfig,
    requested: int | None = None,
    target_side: TargetSide | None = None,
    confidence: Decimal | None = None,
) -> RiskGateResult:
    """A schema-valid ``set_target`` the gate refused — maintain-current, no order.

    Distinct from :func:`_fail_closed` so audit consumers can split "the model
    broke the contract" (``invalid_fail_closed``, the drift alarm) from "the
    model followed the contract but risk said no" (normal operation).
    """
    return _no_target_result(
        risk_action=RiskAction.REJECTED,
        risk_reason=risk_reason,
        no_order_reason=NO_ORDER_REJECTED,
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


def validate_risk_decision_config(risk: RiskConfig, decision: DecisionConfig) -> None:
    """Reject a risk/decision pair whose combination silently misbehaves.

    ``risk.max_target_margin_pct`` and the ``decision`` margin grid each validate
    in isolation, but their *combination* can go quietly wrong two ways. The
    allocation cap is snapped down to the grid in :func:`evaluate`, so a cap that
    snaps to ``0`` — below the grid minimum, or below one step when the grid
    starts at 0 — clamps every directional target to ``approved == 0`` and
    rejects it: directional trading is bricked with no error at load. And a cap
    that is merely *off-grid* (e.g. ``60`` on a step-25 grid) would silently
    tighten to the next grid value below it (``50``) — the operator configured 60
    and never gets more than 50. Both are config mistakes; reject them here, at
    the config seam, before any LLM spend (mirrors ``validate_target_decision``'s
    role for a parsed decision, and the grid-must-reach-max check in
    ``DecisionConfig``).

    A cap at or above the grid ceiling is legal slack, not a mistake: the
    effective limit is ``min(grid ceiling, cap)``, so such a cap never binds and
    its grid alignment is moot — lowering only the grid ceiling must not force a
    matching cap edit. Alignment is enforced exactly when the cap can bind.
    """
    if risk.max_target_margin_pct >= decision.ai_target_margin_max_pct:
        return
    snapped = _snap_down_to_grid(Decimal(risk.max_target_margin_pct), decision)
    if snapped <= 0:
        raise ValueError(
            f"risk.max_target_margin_pct ({risk.max_target_margin_pct}) leaves no legal "
            "directional grid value: it snaps below the decision grid "
            f"(ai_target_margin_min_pct={decision.ai_target_margin_min_pct}, "
            f"target_margin_step_pct={decision.target_margin_step_pct}), so every long/short "
            "target would clamp to 0 and be rejected"
        )
    if snapped != risk.max_target_margin_pct:
        raise ValueError(
            f"risk.max_target_margin_pct ({risk.max_target_margin_pct}) is not on the "
            f"decision grid (ai_target_margin_min_pct={decision.ai_target_margin_min_pct}, "
            f"target_margin_step_pct={decision.target_margin_step_pct}): the effective cap "
            f"would silently be {snapped}. Align the cap to the grid."
        )


def effective_max_target_margin_pct(risk: RiskConfig, decision: DecisionConfig) -> int:
    """The largest margin%% a ``set_target`` can actually be approved at.

    The smaller of the decision grid ceiling and the allocation cap. Rendered
    into the prompt (``decision_format_instructions``) so the model is never
    advertised a ceiling the gate deterministically clamps — under the defaults
    (grid max 100, cap 60) a confident model would otherwise emit a steady
    stream of ``clamped`` records and "clamped" would stop meaning "the risk
    gate intervened". Assumes the pair passed
    :func:`validate_risk_decision_config` (any cap that can bind is on-grid).
    """
    return min(decision.ai_target_margin_max_pct, risk.max_target_margin_pct)


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
        # Preserve what the (hand-built) decision asked for, like every other
        # rejection path, so the flip re-run's audit row is not all-None.
        return _fail_closed(
            risk_reason=invalid,
            current=current,
            risk=risk,
            requested=decision.requested_target_margin_pct,
            target_side=decision.target_side,
            confidence=decision.confidence,
        )

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
        return _rejected(
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
        return _rejected(
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
    # Every cap is snapped *down* to the decision grid (so the approved value
    # always sits on it, even when the cap itself is off-grid), then applied in
    # order against the shrinking ``approved`` value. A cap overwrites the reason
    # only when it *strictly* reduces ``approved`` (an equal cap leaves both the
    # value and the reason untouched — so an unclamped request never records a
    # reason or gets marked CLAMPED). When several caps tie at the same binding
    # value the earliest in list order keeps the reason; the approved value is
    # identical either way, so this is audit attribution only.
    caps: list[tuple[int, str]] = [
        (
            _snap_down_to_grid(Decimal(risk.max_target_margin_pct), decision_cfg),
            RISK_REASON_MAX_TARGET_MARGIN,
        )
    ]
    if account_equity > 0:
        # Available margin: this target's margin plus what other symbols already
        # commit must fit inside equity (cross margin, spec §2.3 independence).
        available = account_equity - other_used_margin
        caps.append(
            (
                _snap_down_to_grid(max(available, Decimal(0)) / account_equity * 100, decision_cfg),
                RISK_REASON_AVAILABLE_MARGIN,
            )
        )

        # Effective leverage: projected total notional over equity must stay
        # within the configured leverage (execution §6.1), independent of the
        # allocation cap.
        headroom_notional = account_equity * risk.leverage - other_positions_notional
        caps.append(
            (
                _snap_down_to_grid(
                    max(headroom_notional, Decimal(0)) / risk.leverage / account_equity * 100,
                    decision_cfg,
                ),
                RISK_REASON_EFFECTIVE_LEVERAGE,
            )
        )

    approved = requested
    risk_action = RiskAction.APPROVED
    risk_reason: str | None = None
    for cap, reason in caps:
        if approved > cap:
            approved = cap
            risk_action = RiskAction.CLAMPED
            risk_reason = reason

    if side is not TargetSide.FLAT and approved == 0:
        # The clamp chain found no legal grid capacity for a directional target
        # (allocation cap / available margin / leverage headroom below the grid
        # minimum). Approving long/short + 0 would violate the contract
        # invariant and turn a "stay long" request into a full close — reject
        # with the binding constraint instead.
        return _rejected(
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
    elif (
        side is current.side
        and current.margin_pct is not None
        and (current.leverage is None or current.leverage == risk.leverage)
    ):
        # Deadband applies only to same-side rebalances (spec §2.4); flips and
        # flat closes execute no matter how small the difference is. It compares
        # margin percentages, which only track notional when the position's real
        # leverage matches the configured ``risk.leverage`` the target is sized
        # with — on a known mismatch (e.g. a manually opened 5x position under
        # ``leverage: 1``) the deadband is disabled so the convergence order is
        # not swallowed here (the resize bar below still applies) and
        # ``delta_notional`` converges the true exposure to the target. An
        # unknown leverage (``None``) keeps the deadband: it is a churn
        # optimisation, and Phase-2-opened positions always match.
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

    # 8. A same-side resize that would actually trade must clear the higher
    # resize bar (spec §2.4): every executed rebalance pays fees, so a
    # mid-conviction size tweak on an existing position is churn, not signal.
    # Deliberately last — a within-deadband or zero-delta reaffirmation costs
    # nothing and stays a no-op under its existing verdict (APPROVED, or
    # CLAMPED when a cap pulled the request into the deadband), and a dead
    # account already reported
    # no_account_equity above. ``side is current.side`` is exactly the
    # same-direction case (a flat account's ``current.side`` is ``None``, never
    # FLAT), so opening from flat, flips — including the flip re-run's second
    # leg, which starts from flat — and explicit flat closes all keep the base
    # gate. Reductions are gated on purpose: the baseline churn's first leg was
    # always a mid-confidence reduce; urgent de-risking stays available via the
    # flat close and SL/TP.
    if order_created and side is current.side and confidence < decision_cfg.resize_min_confidence:
        return _rejected(
            risk_reason=RISK_REASON_LOW_CONFIDENCE_RESIZE,
            current=current,
            risk=risk,
            requested=requested,
            target_side=side,
            confidence=confidence,
        )

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
