"""Phase 2 structured target contract — parsing and cross-field validation.

The engine's ``final_trade_decision`` must carry a JSON block matching the
structured target schema (``docs/DESIGN.md`` Part 2). This module owns:

- :class:`TargetDecision` — the validated decision value object;
- :func:`parse_target_decision` — extract + parse + validate the JSON block,
  **fail-closed**: any invalid output (no JSON, bad types, missing/extra
  fields, illegal cross-field combination, margin off the step grid,
  confidence out of ``0–1``) collapses to ``maintain_current`` with the
  original response preserved, never silently rounded or guessed
  (phase2-spec.md §2.4: no silent rounding, no inference from old ratings or
  a previous round's target);
- :class:`DecisionConfig` — the ``decision:`` config block (margin grid,
  deadband, ``min_confidence``, ``resize_min_confidence``);
- :func:`decision_format_instructions` — the output-format contract appended
  to the engine context by ``integration/trading_graph.py``.

Everything here is pure (no I/O, no clock); amounts are :class:`~decimal.Decimal`.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from ...common.config_coercion import config_overrides, decimal_from_yaml, int_from_yaml

__all__ = [
    "DecisionConfig",
    "DecisionMode",
    "ParsedDecision",
    "TargetDecision",
    "TargetSide",
    "decision_format_instructions",
    "extract_json_block",
    "format_fingerprint",
    "parse_target_decision",
    "validate_target_decision",
]


class DecisionMode(str, Enum):
    """Whether the AI establishes a new target or keeps the current position."""

    SET_TARGET = "set_target"
    MAINTAIN_CURRENT = "maintain_current"


class TargetSide(str, Enum):
    """Final desired position direction; ``FLAT`` is an explicit close-all target."""

    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


# The exact top-level key set the contract allows. Extra keys are rejected
# (not ignored): an unknown key usually means the model drifted from the schema,
# and silently dropping it would mask that drift from the validation counters.
_REQUIRED_KEYS = frozenset(
    {
        "decision_mode",
        "target_side",
        "requested_target_margin_pct",
        "confidence",
        "rationale",
        "key_risks",
    }
)

_MAX_KEY_RISKS = 3


@dataclass(frozen=True)
class DecisionConfig:
    """Typed view of the YAML ``decision:`` block (phase2-spec.md §2.4 defaults).

    The margin grid is ``{min, min+step, …, max}`` — integers ``0–100`` by
    default. ``rebalance_deadband_pct``, ``min_confidence`` and
    ``resize_min_confidence`` are consumed by the RiskGate but live here with
    the rest of the decision-contract knobs so the block round-trips as one
    unit. ``resize_min_confidence`` is the higher bar a same-side resize must
    clear before it may trade (churn control — rationale in phase2-spec §2.4);
    it must not sit below ``min_confidence``, which would be dead config — the
    base gate fires first.
    """

    ai_target_margin_min_pct: int = 0
    ai_target_margin_max_pct: int = 100
    target_margin_step_pct: int = 1
    rebalance_deadband_pct: Decimal = Decimal("1")
    min_confidence: Decimal = Decimal("0.3")
    resize_min_confidence: Decimal = Decimal("0.7")

    def __post_init__(self) -> None:
        if self.target_margin_step_pct < 1:
            raise ValueError(
                f"target_margin_step_pct must be >= 1, got {self.target_margin_step_pct}"
            )
        if not 0 <= self.ai_target_margin_min_pct < self.ai_target_margin_max_pct <= 100:
            raise ValueError(
                "need 0 <= ai_target_margin_min_pct < ai_target_margin_max_pct <= 100, "
                f"got min={self.ai_target_margin_min_pct} max={self.ai_target_margin_max_pct}"
            )
        # The grid must actually reach ``ai_target_margin_max_pct``: the prompt
        # advertises ``max`` as a legal value, but if ``step`` does not divide the
        # span the top grid point is the largest multiple below it (e.g. min=0,
        # max=100, step=7 tops out at 98), so a model requesting the advertised
        # max is rejected off-grid and fails closed. Reject the misaligned config
        # at load rather than silently shrink the reachable ceiling.
        span = self.ai_target_margin_max_pct - self.ai_target_margin_min_pct
        if span % self.target_margin_step_pct != 0:
            raise ValueError(
                "the margin grid must reach ai_target_margin_max_pct: "
                f"(max - min) = {span} is not a multiple of "
                f"target_margin_step_pct = {self.target_margin_step_pct}"
            )
        if self.rebalance_deadband_pct < 0:
            raise ValueError(
                f"rebalance_deadband_pct must be >= 0, got {self.rebalance_deadband_pct}"
            )
        if not Decimal(0) <= self.min_confidence <= Decimal(1):
            raise ValueError(f"min_confidence must be in [0, 1], got {self.min_confidence}")
        if not Decimal(0) <= self.resize_min_confidence <= Decimal(1):
            raise ValueError(
                f"resize_min_confidence must be in [0, 1], got {self.resize_min_confidence}"
            )
        if self.resize_min_confidence < self.min_confidence:
            raise ValueError(
                f"resize_min_confidence ({self.resize_min_confidence}) must be >= "
                f"min_confidence ({self.min_confidence}): a lower resize bar can never "
                "fire because the base gate rejects first. resize_min_confidence "
                "defaults to 0.7 when absent — raising min_confidence above that "
                "requires setting decision: resize_min_confidence explicitly too"
            )

    @classmethod
    def from_dict(cls, cfg: dict | None) -> DecisionConfig:
        """Parse the raw YAML block; absent or null keys use the field defaults."""
        return cls(
            **config_overrides(
                cfg,
                {
                    "ai_target_margin_min_pct": int_from_yaml,
                    "ai_target_margin_max_pct": int_from_yaml,
                    "target_margin_step_pct": int_from_yaml,
                    "rebalance_deadband_pct": decimal_from_yaml,
                    "min_confidence": decimal_from_yaml,
                    "resize_min_confidence": decimal_from_yaml,
                },
            )
        )


@dataclass(frozen=True)
class TargetDecision:
    """A structured target decision, either as parsed (valid) or fail-closed.

    A fail-closed instance is always ``maintain_current`` with every optional
    field ``None`` — construction does not re-validate cross-field rules (that
    is :func:`validate_target_decision`'s job, and the parse seam guarantees
    only validated instances carry ``set_target``).
    """

    decision_mode: DecisionMode
    target_side: TargetSide | None
    requested_target_margin_pct: int | None
    confidence: Decimal | None
    rationale: str
    key_risks: tuple[str, ...]

    @classmethod
    def fail_closed(cls) -> TargetDecision:
        """The maintain-current decision every invalid output collapses to."""
        return cls(
            decision_mode=DecisionMode.MAINTAIN_CURRENT,
            target_side=None,
            requested_target_margin_pct=None,
            confidence=None,
            rationale="",
            key_risks=(),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict; Decimals become strings so no precision is lost.

        The decision's own wire format (mirrors ``RiskGateResult.to_dict``) so
        the audit layer composes domain-owned dicts instead of re-serializing
        fields it does not own.
        """
        return {
            "decision_mode": self.decision_mode.value,
            "target_side": self.target_side.value if self.target_side is not None else None,
            "requested_target_margin_pct": self.requested_target_margin_pct,
            "confidence": str(self.confidence) if self.confidence is not None else None,
            "rationale": self.rationale,
            "key_risks": list(self.key_risks),
        }


@dataclass(frozen=True)
class ParsedDecision:
    """Outcome of parsing one engine response.

    ``decision`` is the validated :class:`TargetDecision` when ``is_valid``,
    otherwise the fail-closed maintain-current stand-in. ``raw_response``
    preserves the original engine text verbatim for the audit trail (a non-str
    under engine schema drift is preserved as its ``repr``) — an invalid output
    is recorded, never reconstructed.
    """

    decision: TargetDecision
    is_valid: bool
    invalid_reason: str | None
    raw_response: str

    def __post_init__(self) -> None:
        # ``is_valid`` and ``invalid_reason`` are two halves of one fact: a valid
        # parse names no reason, an invalid one must. ``evaluate()`` trusts
        # ``is_valid`` before re-checking the decision, so a contradictory pair
        # (invalid with no reason) would yield a fail-closed record whose
        # ``risk_reason`` is null. Enforce the coupling at construction.
        if self.is_valid == (self.invalid_reason is not None):
            raise ValueError("is_valid must be True with no invalid_reason, or False with one")
        # An invalid parse must carry the fail-closed maintain-current stand-in,
        # never a live ``set_target``: the audit layer serializes ``decision``
        # unconditionally, so an invalid record paired with a sized target would
        # misreport the rejected round as having asked to open a position. The
        # parse seam only ever pairs invalid with ``fail_closed()``; pin the
        # invariant here so a hand-built instance cannot break it (mirrors the
        # cross-field guards on ``RiskGateResult``/``CurrentPositionState``).
        if not self.is_valid and self.decision != TargetDecision.fail_closed():
            raise ValueError(
                "an invalid parse must carry the fail-closed maintain_current decision"
            )


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json_block(text: str) -> str | None:
    """Pull the decision JSON out of the engine's free-text response.

    Prefers the **last** fenced ``` block whose body parses as a JSON object
    (agents often quote earlier examples; the final answer comes last), then
    falls back to the last balanced ``{...}`` region in the raw text. Returns
    the JSON source string, or ``None`` when no parseable object is present —
    the caller fail-closes on ``None``.
    """
    fenced = [m.group(1) for m in _FENCED_JSON_RE.finditer(text)]
    for candidate in reversed(fenced):
        if _parses_as_object(candidate):
            return candidate.strip()
    # No usable fenced block — scan for balanced top-level {...} spans.
    for candidate in reversed(_balanced_object_spans(text)):
        if _parses_as_object(candidate):
            return candidate
    return None


def _parses_as_object(source: str) -> bool:
    try:
        return isinstance(json.loads(source), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def _balanced_object_spans(text: str) -> list[str]:
    """All parseable JSON-object substrings, in order of appearance.

    Attempts a real JSON parse (``raw_decode``) at every ``{`` so an unmatched
    brace in surrounding prose (e.g. "if we break resistance {see chart") can
    never desync the scan and swallow a valid decision object later in the text.
    """
    decoder = json.JSONDecoder()
    spans: list[str] = []
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            spans.append(text[i:end])
            i = text.find("{", end)
        else:
            i = text.find("{", i + 1)
    return spans


# --------------------------------------------------------------------------
# Field + cross-field validation
# --------------------------------------------------------------------------


def _as_decimal(value: object) -> Decimal | None:
    """A finite Decimal for a JSON number, else ``None``. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Any int/float str-parses into Decimal; inf/nan become non-finite and are
    # rejected here, which carries the fail-closed guarantee on its own.
    result = Decimal(str(value))
    return result if result.is_finite() else None


def _is_quoted_number(value: object) -> bool:
    """True for a string that would have been a number without its quotes.

    ``_as_decimal`` refuses strings on purpose and that does not change here:
    a quoted figure stays a format failure and the decision stays fail-closed.
    What changes is only which tag records it. One tag covered three very
    different outputs — a leftover placeholder, a ``maintain_current`` that
    quoted its own ``"null"``, and a figure the model typed into the margin
    field with quotes around it — and only the last is a cycle where the model
    put a size there at all. The proposal rate is the single number this prompt
    change is judged on, so a tag that cannot separate "asked for 35%" from
    "echoed the schema" makes the before/after unreadable exactly where it has
    to be read.

    Like its netting-set sibling ``margin_off_step_grid``, this fires before
    ``validate_target_decision`` and so reads no ``decision_mode``: a
    ``maintain_current`` that quoted its margin as ``"0"`` lands here too. The
    set therefore counts "the model supplied a figure", which is the proxy the
    proposal rate has always used, not a proven ``set_target``.
    """
    if not isinstance(value, str):
        return False
    try:
        parsed = Decimal(value.strip())
    except (InvalidOperation, ValueError):
        return False
    # "nan"/"inf" parse but are not figures anyone asked for; they belong with
    # the unreadable strings, not with a size the model chose.
    return parsed.is_finite()


def _validate_margin_grid(margin: Decimal, config: DecisionConfig) -> str | None:
    """Range + integer-step-grid check; returns an error tag or ``None``.

    No silent rounding (phase2-spec.md §2.4): a value off the grid is rejected,
    because rounding would let the recorded ``requested`` diverge from the value
    actually used and mask AI output-quality problems.
    """
    low = Decimal(config.ai_target_margin_min_pct)
    high = Decimal(config.ai_target_margin_max_pct)
    if not low <= margin <= high:
        return "margin_out_of_range"
    if (margin - low) % Decimal(config.target_margin_step_pct) != 0:
        return "margin_off_step_grid"
    return None


def validate_target_decision(decision: TargetDecision, config: DecisionConfig) -> str | None:
    """Cross-field validation of an already-typed decision; ``None`` means valid.

    Shared by the parse seam below and by the RiskGate (which re-validates its
    input so the deterministic flip re-run cannot act on a hand-built invalid
    decision). The rules are DESIGN Part 2's legal-combination table:

    - ``set_target + long/short`` needs margin on the grid and ``> 0``;
    - ``set_target + flat`` needs margin exactly ``0``;
    - ``maintain_current`` must carry no target at all.
    """
    mode, side = decision.decision_mode, decision.target_side
    margin = decision.requested_target_margin_pct

    if decision.confidence is not None and not Decimal(0) <= decision.confidence <= Decimal(1):
        return "confidence_out_of_range"

    if mode is DecisionMode.MAINTAIN_CURRENT:
        if side is not None or margin is not None:
            return "maintain_current_with_target"
        return None

    # set_target
    if side is None:
        return "set_target_without_side"
    if margin is None:
        return "set_target_without_margin"
    if decision.confidence is None:
        return "set_target_without_confidence"
    if side is TargetSide.FLAT:
        # flat means margin exactly 0 and is always legal — the grid governs
        # directional allocations only, so a nonzero ai_target_margin_min_pct
        # must never make closing a position impossible.
        if margin != 0:
            return "flat_with_nonzero_margin"
        return None
    grid_error = _validate_margin_grid(Decimal(margin), config)
    if grid_error is not None:
        return grid_error
    if margin == 0:
        return "directional_side_with_zero_margin"
    return None


def parse_target_decision(raw: object, config: DecisionConfig) -> ParsedDecision:
    """Parse one engine response into a :class:`ParsedDecision`, fail-closed.

    ``raw`` is whatever ``final_state["final_trade_decision"]`` held — a non-str
    (engine schema drift) or empty text fails closed like any other invalid
    output; a non-str's ``repr`` is preserved as the raw response so the audit
    record can distinguish "engine returned nothing" from "engine returned a
    differently-shaped object". ``invalid_reason`` is a stable machine tag (e.g.
    ``invalid_output``, ``margin_off_step_grid``) recorded alongside the
    preserved raw response.
    """
    is_str = isinstance(raw, str)
    raw_text = raw if is_str else ("" if raw is None else repr(raw))

    def _invalid(reason: str) -> ParsedDecision:
        return ParsedDecision(
            decision=TargetDecision.fail_closed(),
            is_valid=False,
            invalid_reason=reason,
            raw_response=raw_text,
        )

    if not is_str or not raw_text.strip():
        # A non-str always fails closed here, *before* extraction — its repr may
        # embed a JSON-looking span that must never be parsed as a live decision.
        return _invalid("invalid_output")

    source = extract_json_block(raw_text)
    if source is None:
        return _invalid("invalid_output")
    payload = json.loads(source)  # extract_json_block guarantees this is a dict

    keys = set(payload)
    if keys - _REQUIRED_KEYS:
        return _invalid("unexpected_fields")
    if _REQUIRED_KEYS - keys:
        return _invalid("missing_fields")

    try:
        mode = DecisionMode(payload["decision_mode"])
    except ValueError:
        return _invalid("invalid_decision_mode")

    raw_side = payload["target_side"]
    side: TargetSide | None
    if raw_side is None:
        side = None
    else:
        try:
            side = TargetSide(raw_side)
        except ValueError:
            return _invalid("invalid_target_side")

    raw_margin = payload["requested_target_margin_pct"]
    margin: int | None
    if raw_margin is None:
        margin = None
    else:
        margin_dec = _as_decimal(raw_margin)
        if margin_dec is None:
            # Same refusal, a tag that says which refusal. See _is_quoted_number:
            # a quoted figure is a size the model actually put in this field,
            # wearing the wrong type, and it has to be countable apart from an
            # echoed placeholder.
            if _is_quoted_number(raw_margin):
                return _invalid("margin_quoted_number")
            return _invalid("margin_not_numeric")
        if margin_dec != margin_dec.to_integral_value():
            # The default grid is integer percent; a fractional value is off-grid
            # by construction and must not be rounded (spec §2.4).
            return _invalid("margin_off_step_grid")
        margin = int(margin_dec)

    raw_confidence = payload["confidence"]
    confidence: Decimal | None
    if raw_confidence is None:
        # ``confidence`` may be null only for maintain_current (nothing to gate);
        # validate_target_decision rejects a set_target without it.
        confidence = None
    else:
        confidence = _as_decimal(raw_confidence)
        if confidence is None:
            # The margin's sibling. NOT evidence of a proposal, though: margin
            # is coerced first and a null margin SKIPS that block rather than
            # failing it, so this tag is reachable with nothing proposed and
            # must stay out of the proposal-rate correction set.
            if _is_quoted_number(raw_confidence):
                return _invalid("confidence_quoted_number")
            return _invalid("confidence_not_numeric")

    rationale = payload["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        return _invalid("missing_rationale")

    raw_risks = payload["key_risks"]
    if not isinstance(raw_risks, list) or not 1 <= len(raw_risks) <= _MAX_KEY_RISKS:
        # At least one risk is required (an empty list slips through a bare
        # ``len > MAX`` check because ``all([])`` is vacuously True) and at most
        # ``_MAX_KEY_RISKS``: a valid decision must name a concrete risk, not an
        # empty gesture, for the audit trail to be meaningful.
        return _invalid("invalid_key_risks")
    if not all(isinstance(r, str) and r.strip() for r in raw_risks):
        return _invalid("invalid_key_risks")

    decision = TargetDecision(
        decision_mode=mode,
        target_side=side,
        requested_target_margin_pct=margin,
        confidence=confidence,
        rationale=rationale.strip(),
        key_risks=tuple(r.strip() for r in raw_risks),
    )
    reason = validate_target_decision(decision, config)
    if reason is not None:
        return _invalid(reason)
    return ParsedDecision(
        decision=decision, is_valid=True, invalid_reason=None, raw_response=raw_text
    )


# --------------------------------------------------------------------------
# Output-format contract appended to the engine context
# --------------------------------------------------------------------------


def format_fingerprint(format_text: str) -> str:
    """The third prompt segmentation key: a content digest of the format block.

    ``prompt_version`` marks a code change by hand and ``context_shape`` marks
    the context's section structure; neither covers the FORMAT half of the
    prompt, which :func:`decision_format_instructions` renders from the live
    config — the legal grid and the effective ceiling are numbers in that
    text, and a ``decision:`` / ``risk:`` YAML edit of those changes what the
    model reads with no deploy and nothing to bump (issue #129). So this key
    is a digest of the RENDERED text, not a shape: any change to those
    numbers, or to the block's wording, moves it. Since prompt v5 the gate
    thresholds are not in the text, so editing them does not move it. It is
    the same digest the version-pin test computes (the first 16 hex
    characters of SHA-256 over UTF-8), so the two agree on what "the block
    changed" means. Stored on ``ai_inputs.format_fingerprint`` (schema v11) and in the
    payload JSON; ``GROUP BY prompt_version, context_shape,
    format_fingerprint`` is the full segmentation.
    """
    return hashlib.sha256(format_text.encode("utf-8")).hexdigest()[:16]


def decision_format_instructions(config: DecisionConfig, *, max_pct: int | None = None) -> str:
    """The output-format contract text injected at the tail of the engine context.

    Rendered from the live :class:`DecisionConfig` so the legal margin grid
    and the effective ceiling in the prompt can never drift from what the
    parser and RiskGate actually enforce. The three gate thresholds the same
    config carries — ``min_confidence``, the same-side
    ``resize_min_confidence`` bar and the rebalance deadband — are
    deliberately NOT rendered since prompt v5: as numbers they were anchors
    the model steered to, not information (see the CHANGELOG entry for
    ``phase2-target-v5`` for the paper-BTC-2 figures). The block states the
    three rules qualitatively — pinned number-free by
    ``test_format_instructions_carry_no_gate_threshold_number`` and, over the
    whole injected prompt, by
    ``test_run_engine_prompt_carries_no_gate_threshold_number`` — and the
    gate enforces the numbers unchanged; a rejection still reaches the audit
    row. The deadband sentence itself is a deliberately simplified contract
    for the normal case, twice over. The model is not told how wide "only
    marginally" is, so it cannot know whether a single grid step is a
    cost-free reaffirmation or a trade held to the resize bar — accepted,
    since telling it is exactly the anchor v5 removes. And the gate skips
    the deadband entirely on an unreadable ``margin_pct`` or a leverage
    mismatch (the target then trades, or hits the resize bar); those
    degraded states are rare in Phase 2 and non-actionable for the model:
    the daemon lanes' v4 ``Position:`` section is built from the local
    books, which never carry an unreadable margin or a foreign leverage, and
    the one-shot CLI (which CAN hit both, off an exchange-reported position)
    stays position-blind — so the prompt does not carry the conditional.
    ``max_pct`` (when given) advertises the *effective* ceiling —
    ``risk_gate.effective_max_target_margin_pct``, the
    grid ceiling capped by ``risk.max_target_margin_pct`` — so the model is
    never told a margin is legal that the gate deterministically clamps; a
    ``clamped`` audit record then means the risk gate genuinely intervened, not
    business as usual. Requests above ``max_pct`` but on the grid stay
    schema-valid (they clamp rather than fail closed).

    The resize clause says only that its bar is higher, never higher THAN
    which action's: the gate's exemption ranking (open/flip/flat need just
    ``min_confidence``) is deliberately not spelled out. Through prompt v3 the reason was that a
    position-blind model could not tell which of its targets was a resize,
    so the comparison was non-actionable. Since v4 the context carries the
    model's own position (``prompt_context``'s ``Position:`` section, with
    the round-trip cost of every legal move), so the model CAN tell — which
    is exactly why the ranking stays out: spelled out, the one thing it
    would teach is that a full close is the one exposure change guaranteed
    past the gate, funneling mid-confidence de-risking into fee-heavier
    flat exits. The position section states facts and prices and no gate
    rule at all (pinned by
    ``test_the_position_section_never_names_a_gate_threshold``).

    The schema block carries **placeholders, not a worked example**, because
    models return the example as their answer — measured, not feared: 74% of
    ``paper-BTC``'s outputs echoed the previous example's decision fields
    verbatim (see the CHANGELOG entry for ``phase2-target-v3``). The invariant
    that replaced it: the four typed fields (``decision_mode``,
    ``target_side``, ``requested_target_margin_pct``, ``confidence``) must hold
    placeholders their own coercion rejects, and ``decision_mode`` is the
    earliest **of those four** the parser coerces, so a whole-block echo that
    keeps the block's quoting lands on ``invalid_decision_mode`` rather than on
    a tag naming the wrong field.

    Not *always*, and the exceptions matter to anyone reading the tag as
    evidence. The key-set checks run before any coercion, so an echo that adds
    or drops a field lands on ``unexpected_fields`` / ``missing_fields`` first;
    and an echo that unquotes the two numeric placeholders — which this block's
    own text asks for — stops being valid JSON and lands on ``invalid_output``.
    The implication runs the other way too, and is the one that bites: the tag
    is many-to-one. ``invalid_decision_mode`` is emitted for *any* mode outside
    the two enum members, so a hallucinated ``"hold"`` and a verbatim echo are
    indistinguishable in the stored row. Treat the tag as a proxy for echoing,
    never as proof of it.
    ``rationale`` and ``key_risks`` take free text, so their placeholders are
    legal strings a partial echo can carry into the audit record — cosmetic,
    since a decision is only directional once ``decision_mode`` and
    ``target_side`` are both values the model chose.

    This preserves what the old ``maintain_current`` example was for:
    :func:`extract_json_block` takes the last parseable object, so a block that
    reads as a directional decision would become a live sized target if echoed.
    Fail-closed reaches the same harmless no-op — but countable.

    No placeholder advertises ``|null``, though the parser accepts null for
    ``target_side``, ``requested_target_margin_pct`` and ``confidence``. The
    rules below say when the first two are null and deliberately never offer it
    for ``confidence`` — the prompt is narrower than the contract there, which
    the next sentence is about. Offering it *inside a quoted
    placeholder* invites the substitution that keeps the quotes — the failure
    this whole change exists to remove, and one that costs a real proposal
    rather than an echo. For ``confidence`` the narrowing is wanted on its own
    terms as well: every *parsed* cycle should carry a number rather than opt
    out of the one signal saying how strongly the model held its view.

    That is only true of parsed cycles, and the gap matters when reading a
    before/after: at the parse seam ``risk_gate`` fails closed without passing
    a confidence, so the row stores NULL, and switching to this block deletes
    the old echo's ``0.55`` spike from the histogram **whether or not the model
    changed its behaviour**. Every reachable fail-closed row stores NULL: the one
    branch that would pass a parsed confidence through is ``risk_gate``'s
    re-validation of an already-valid parse, and reaching it requires that
    re-validation to FAIL — on the same ``ParsedDecision`` against the same
    config object that just accepted it, so it cannot. (That re-validation runs
    on every ``evaluate``, not only on the flip re-run; the config is built once
    per process and reaches both the parse and the gate on all three wirings.)
    Nothing on the flip legs writes an ``ai_outputs`` row in any case. A confidence
    distribution compared across the version boundary must therefore render the
    NULL bucket as its own visible share of all cycles; on its own, "the spike
    is gone" is an artifact of the prompt change, not evidence about conviction.

    The quotes are load-bearing in both directions: they are an artifact on the
    two numeric fields and on a null ``target_side``, never on the rest, so the
    text names what to unquote rather than telling the model to strip quotes
    generally. Both mistakes cost the whole
    output, but they cost different things afterwards. A quoted number is
    tagged ``margin_quoted_number`` / ``confidence_quoted_number``, and because
    the gate discards what the model asked for, the row stores
    ``requested_target_margin_pct`` as NULL — so a model that HAS started
    proposing would read as one that has not, on the very metric this change is
    judged by, unless those cycles are netted back in.

    Reading the proposal rate therefore needs a correction, and the obvious
    ones are wrong. ``margin_not_numeric`` / ``confidence_not_numeric`` are
    ambiguous rather than evidence of a proposal: they cover the strings that
    are not figures at all — an echoed placeholder, or a ``maintain_current``
    that quoted its ``"null"`` — and margin is coerced before confidence, so
    the second proves nothing about the first either. Nor is the rule "any tag
    after margin coercion succeeds" — a null margin SKIPS the coercion rather
    than failing it, so ``invalid_key_risks`` and ``missing_rationale`` are
    reachable with nothing proposed at all.

    The predicate that works is narrower: tags that can only follow a margin
    the model actually supplied as a figure. There are six, and each is worth
    naming because the set is not guessable —
    ``margin_off_step_grid`` and ``margin_out_of_range`` (the value itself was
    rejected), ``flat_with_nonzero_margin`` and
    ``directional_side_with_zero_margin`` (cross-field checks that sit past the
    ``set_target_without_margin`` guard, so a null margin cannot reach them),
    ``set_target_without_confidence`` (same guard), and ``margin_quoted_number``
    (a figure supplied with quotes around it — the one member that fails before
    the coercion rather than after it). Those are the cycles to net back in.
    ``confidence_quoted_number`` is NOT one of them: margin is coerced first and
    a null margin skips that block, so it is reachable with nothing proposed.
    An unquoted ``decision_mode`` costs the diagnosis instead: the block stops
    parsing at all and is recorded as a bare ``invalid_output``.
    """
    lo = config.ai_target_margin_min_pct
    hi = config.ai_target_margin_max_pct if max_pct is None else max_pct
    step = config.target_margin_step_pct
    return f"""## Required final decision output format

Your FINAL trade decision MUST end with exactly one JSON object in a fenced
code block, with exactly these six fields and no others:

```json
{{
  "decision_mode": "<set_target|maintain_current>",
  "target_side": "<long|short|flat>",
  "requested_target_margin_pct": "<integer {lo}-{hi}>",
  "confidence": "<0.0-1.0>",
  "rationale": "<one short paragraph explaining the decision>",
  "key_risks": ["<risk>", "<risk 2 — optional; {_MAX_KEY_RISKS} entries total maximum>"]
}}
```

That block is a schema, not an answer: every `<...>` placeholder MUST be
replaced with a real value of your own (a placeholder marked optional may
instead be dropped, along with the array entry holding it). A leftover
placeholder in "decision_mode", "target_side", "requested_target_margin_pct" or
"confidence" is not a legal value, so the whole output is discarded and the
cycle is recorded as a model-format failure.

The "requested_target_margin_pct" and "confidence" placeholders are quoted only
so the block above stays valid JSON: write those two as bare JSON numbers.
Where the rules below call for null — that is "target_side" and
"requested_target_margin_pct" — write the JSON literal null, without quotes.
Everything else keeps its quotes: "decision_mode", "target_side" when it names
a side, "rationale", and every entry of "key_risks". A quoted number ("35") or
a quoted null ("null") is discarded exactly like a leftover placeholder.

Rules (violations are discarded and recorded as a model-format failure — they
are never repaired or rounded):

- "decision_mode": "set_target" to establish a new target, or
  "maintain_current" to keep the current position unchanged.
- "target_side": "long" / "short" / "flat" for set_target; null for
  maintain_current. "flat" means close everything.
- "requested_target_margin_pct": integer percent of account equity to commit
  as margin, from {lo} to {hi} in steps of {step}. Use a value > 0 with
  long/short, exactly 0 with flat, and null with maintain_current.
- "confidence": a number between 0 and 1. A set_target whose confidence is
  too low is rejected. A same-side target only marginally away from the
  current margin creates no order (a cost-free reaffirmation); a same-side
  resize that would actually trade is held to a higher confidence bar and is
  rejected below it — every executed rebalance pays fees. Make your
  conviction consistent with the margin you request — confidence is recorded
  but never scales the size.
- "rationale": non-empty. "key_risks": 1 to {_MAX_KEY_RISKS} short strings (at least one).

Legal combinations — anything else is invalid:

- set_target + long/short + margin > 0
- set_target + flat + margin 0
- maintain_current + target_side null + margin null
"""
