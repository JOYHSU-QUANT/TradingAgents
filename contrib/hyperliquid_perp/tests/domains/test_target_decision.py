"""Tests for the Phase 2 structured target contract (parse + cross-field rules).

Covers DESIGN Part 2's legal-combination table, every invalid combination's
fail-closed shape, the JSON extraction fallbacks, and the no-silent-rounding
rule from phase2-spec.md §2.4.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    DecisionMode,
    ParsedDecision,
    TargetDecision,
    TargetSide,
    decision_format_instructions,
    extract_json_block,
    format_fingerprint,
    parse_target_decision,
)

_CFG = DecisionConfig()


def _payload(**overrides):
    """A fully valid set_target payload; override fields per test."""
    base = {
        "decision_mode": "set_target",
        "target_side": "long",
        "requested_target_margin_pct": 35,
        "confidence": 0.78,
        "rationale": "Trend and funding support a long.",
        "key_risks": ["Funding is rising", "Volatility elevated"],
    }
    base.update(overrides)
    return base


def _text(**overrides) -> str:
    """The payload wrapped in a fenced block, as the engine should emit it."""
    return f"Final decision:\n\n```json\n{json.dumps(_payload(**overrides))}\n```\n"


def _assert_fail_closed(parsed, reason: str) -> None:
    """Every invalid output must collapse to the same maintain-current shape."""
    assert parsed.is_valid is False
    assert parsed.invalid_reason == reason
    d = parsed.decision
    assert d.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert d.target_side is None
    assert d.requested_target_margin_pct is None
    assert d.confidence is None


# --------------------------------------------------------------------------
# The four legal combinations
# --------------------------------------------------------------------------


@pytest.mark.parametrize("side,margin", [("long", 1), ("long", 100), ("short", 1), ("short", 100)])
def test_valid_set_target_directional(side, margin):
    parsed = parse_target_decision(
        _text(target_side=side, requested_target_margin_pct=margin), _CFG
    )
    assert parsed.is_valid
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    assert parsed.decision.target_side is TargetSide(side)
    assert parsed.decision.requested_target_margin_pct == margin
    assert parsed.decision.confidence == Decimal("0.78")


def test_valid_set_target_flat_zero_margin():
    parsed = parse_target_decision(_text(target_side="flat", requested_target_margin_pct=0), _CFG)
    assert parsed.is_valid
    assert parsed.decision.target_side is TargetSide.FLAT
    assert parsed.decision.requested_target_margin_pct == 0


def test_valid_maintain_current_null_side_null_margin():
    parsed = parse_target_decision(
        _text(decision_mode="maintain_current", target_side=None, requested_target_margin_pct=None),
        _CFG,
    )
    assert parsed.is_valid
    assert parsed.decision.decision_mode is DecisionMode.MAINTAIN_CURRENT
    assert parsed.decision.target_side is None
    assert parsed.decision.requested_target_margin_pct is None


def test_valid_parse_preserves_raw_response():
    text = _text()
    parsed = parse_target_decision(text, _CFG)
    assert parsed.raw_response == text


# --------------------------------------------------------------------------
# Invalid combinations — one case each, all fail-closed (DESIGN Part 2)
# --------------------------------------------------------------------------


def test_invalid_long_with_zero_margin():
    parsed = parse_target_decision(_text(requested_target_margin_pct=0), _CFG)
    _assert_fail_closed(parsed, "directional_side_with_zero_margin")


def test_invalid_short_with_zero_margin():
    parsed = parse_target_decision(_text(target_side="short", requested_target_margin_pct=0), _CFG)
    _assert_fail_closed(parsed, "directional_side_with_zero_margin")


def test_invalid_flat_with_nonzero_margin():
    parsed = parse_target_decision(_text(target_side="flat", requested_target_margin_pct=10), _CFG)
    _assert_fail_closed(parsed, "flat_with_nonzero_margin")


def test_invalid_maintain_current_with_target():
    parsed = parse_target_decision(_text(decision_mode="maintain_current"), _CFG)
    _assert_fail_closed(parsed, "maintain_current_with_target")


def test_invalid_set_target_without_side():
    parsed = parse_target_decision(_text(target_side=None), _CFG)
    _assert_fail_closed(parsed, "set_target_without_side")


def test_invalid_set_target_without_margin():
    parsed = parse_target_decision(_text(requested_target_margin_pct=None), _CFG)
    _assert_fail_closed(parsed, "set_target_without_margin")


def test_invalid_margin_negative():
    parsed = parse_target_decision(_text(requested_target_margin_pct=-5), _CFG)
    _assert_fail_closed(parsed, "margin_out_of_range")


def test_invalid_margin_above_100():
    parsed = parse_target_decision(_text(requested_target_margin_pct=101), _CFG)
    _assert_fail_closed(parsed, "margin_out_of_range")


def test_invalid_margin_fractional_is_not_rounded():
    # 35.5 is off the integer grid — it must fail closed, never round to 35/36
    # (spec §2.4: silent rounding would desync requested vs used and hide AI
    # output-quality problems).
    parsed = parse_target_decision(_text(requested_target_margin_pct=35.5), _CFG)
    _assert_fail_closed(parsed, "margin_off_step_grid")


def test_invalid_margin_off_configured_step_grid():
    cfg = DecisionConfig(target_margin_step_pct=5)
    parsed = parse_target_decision(_text(requested_target_margin_pct=33), cfg)
    _assert_fail_closed(parsed, "margin_off_step_grid")


def test_valid_margin_on_configured_step_grid():
    cfg = DecisionConfig(target_margin_step_pct=5)
    parsed = parse_target_decision(_text(requested_target_margin_pct=35), cfg)
    assert parsed.is_valid


def test_flat_zero_is_legal_even_with_nonzero_grid_minimum():
    # The grid minimum governs directional allocations only — with min=20 a
    # flat close (margin 0) must still validate, or the AI could never exit a
    # position under that config.
    cfg = DecisionConfig(ai_target_margin_min_pct=20)
    parsed = parse_target_decision(_text(target_side="flat", requested_target_margin_pct=0), cfg)
    assert parsed.is_valid


def test_directional_below_grid_minimum_is_invalid():
    cfg = DecisionConfig(ai_target_margin_min_pct=20)
    parsed = parse_target_decision(_text(requested_target_margin_pct=10), cfg)
    _assert_fail_closed(parsed, "margin_out_of_range")


@pytest.mark.parametrize("value", ["<integer 5-60>", "null", "high", "", "nan", "Infinity"])
def test_invalid_margin_not_numeric(value):
    # What is left on this tag once quoted figures move off it: the strings
    # that are not figures at all — an echoed placeholder, a quoted null,
    # prose, empty. "nan"/"Infinity" belong here too: Decimal parses them, so
    # only the finiteness guard keeps them off the proposal-rate netting set,
    # and dropping that guard would silently start counting them as proposals.
    parsed = parse_target_decision(_text(requested_target_margin_pct=value), _CFG)
    _assert_fail_closed(parsed, "margin_not_numeric")


@pytest.mark.parametrize("value", ["35", " 35 ", "35.0"])
def test_a_quoted_margin_figure_is_tagged_apart_from_an_echo(value):
    # Still discarded, still fail-closed — only the tag differs. A proposal the
    # model typed as a string has to be countable apart from an echoed
    # placeholder: the proposal rate is the metric this prompt change is judged
    # by, and a fail-closed row stores requested_target_margin_pct as NULL
    # either way, so the tag is the only surviving evidence.
    parsed = parse_target_decision(_text(requested_target_margin_pct=value), _CFG)
    _assert_fail_closed(parsed, "margin_quoted_number")


def test_invalid_margin_boolean_is_not_numeric():
    parsed = parse_target_decision(_text(requested_target_margin_pct=True), _CFG)
    _assert_fail_closed(parsed, "margin_not_numeric")


@pytest.mark.parametrize("value", ["high", "<0.0-1.0>", "null", "nan", "Infinity"])
def test_invalid_confidence_not_numeric(value):
    parsed = parse_target_decision(_text(confidence=value), _CFG)
    _assert_fail_closed(parsed, "confidence_not_numeric")


def test_a_quoted_confidence_figure_gets_its_own_tag():
    # A numeric-looking string is what catches a coercion that starts
    # str-parsing gracefully (`Decimal(str(value))` accepts it, where "high"
    # would raise and merely turn the test red for the wrong reason). The
    # format contract still discards it; only the tag is new.
    parsed = parse_target_decision(_text(confidence="0.78"), _CFG)
    _assert_fail_closed(parsed, "confidence_quoted_number")


def test_a_quoted_confidence_does_not_prove_a_proposal():
    # Why this tag stays OUT of the proposal-rate correction set while its
    # margin sibling goes in: margin is coerced first and a null margin SKIPS
    # that block rather than failing it, so a maintain_current that proposed
    # nothing still reaches confidence_quoted_number. Netting it in would count
    # non-proposals as proposals on the one metric that decides this change.
    parsed = parse_target_decision(
        _text(
            decision_mode="maintain_current",
            target_side=None,
            requested_target_margin_pct=None,
            confidence="0.78",
        ),
        _CFG,
    )
    _assert_fail_closed(parsed, "confidence_quoted_number")


def test_invalid_confidence_boolean_is_not_numeric():
    parsed = parse_target_decision(_text(confidence=True), _CFG)
    _assert_fail_closed(parsed, "confidence_not_numeric")


def test_nonfinite_margin_fails_closed():
    # json.loads accepts the nonstandard Infinity/NaN literals as floats; the
    # finiteness check must fail them closed instead of feeding Decimal math.
    parsed = parse_target_decision(_text(requested_target_margin_pct=float("inf")), _CFG)
    _assert_fail_closed(parsed, "margin_not_numeric")


def test_nan_confidence_fails_closed():
    parsed = parse_target_decision(_text(confidence=float("nan")), _CFG)
    _assert_fail_closed(parsed, "confidence_not_numeric")


def test_maintain_current_with_out_of_range_confidence_fails_closed():
    # The confidence-range check runs before the mode dispatch, so even a
    # maintain_current carrying a nonsense confidence is rejected.
    parsed = parse_target_decision(
        _text(
            decision_mode="maintain_current",
            target_side=None,
            requested_target_margin_pct=None,
            confidence=1.5,
        ),
        _CFG,
    )
    _assert_fail_closed(parsed, "confidence_out_of_range")


def test_invalid_confidence_above_one():
    parsed = parse_target_decision(_text(confidence=1.2), _CFG)
    _assert_fail_closed(parsed, "confidence_out_of_range")


def test_invalid_confidence_negative():
    parsed = parse_target_decision(_text(confidence=-0.1), _CFG)
    _assert_fail_closed(parsed, "confidence_out_of_range")


def test_invalid_set_target_without_confidence():
    parsed = parse_target_decision(_text(confidence=None), _CFG)
    _assert_fail_closed(parsed, "set_target_without_confidence")


def test_invalid_unknown_decision_mode():
    parsed = parse_target_decision(_text(decision_mode="close_all"), _CFG)
    _assert_fail_closed(parsed, "invalid_decision_mode")


def test_invalid_unknown_target_side():
    parsed = parse_target_decision(_text(target_side="neutral"), _CFG)
    _assert_fail_closed(parsed, "invalid_target_side")


def test_invalid_empty_rationale():
    parsed = parse_target_decision(_text(rationale="  "), _CFG)
    _assert_fail_closed(parsed, "missing_rationale")


def test_invalid_too_many_key_risks():
    parsed = parse_target_decision(_text(key_risks=["a", "b", "c", "d"]), _CFG)
    _assert_fail_closed(parsed, "invalid_key_risks")


def test_invalid_non_string_key_risk():
    parsed = parse_target_decision(_text(key_risks=["a", 2]), _CFG)
    _assert_fail_closed(parsed, "invalid_key_risks")


def test_invalid_empty_key_risks():
    # At least one risk is required — an empty list must not slip through the
    # ``all([])``-is-True gap (a valid decision has to name a concrete risk).
    parsed = parse_target_decision(_text(key_risks=[]), _CFG)
    _assert_fail_closed(parsed, "invalid_key_risks")


# --------------------------------------------------------------------------
# Parse failures — no JSON / missing fields / extra fields
# --------------------------------------------------------------------------


def test_no_json_block_fails_closed():
    parsed = parse_target_decision("I think we should buy a lot.", _CFG)
    _assert_fail_closed(parsed, "invalid_output")
    assert parsed.raw_response == "I think we should buy a lot."


def test_empty_output_fails_closed():
    parsed = parse_target_decision("", _CFG)
    _assert_fail_closed(parsed, "invalid_output")


def test_non_string_output_fails_closed():
    # Engine schema drift can hand back None / a list; never crash, fail closed.
    parsed = parse_target_decision(None, _CFG)
    _assert_fail_closed(parsed, "invalid_output")
    assert parsed.raw_response == ""


def test_non_string_output_preserves_repr_in_raw_response():
    # A non-None non-str keeps a repr in the audit record so a post-mortem can
    # tell "engine returned nothing" from "engine returned a different shape".
    drifted = {"chunks": ["not", "text"]}
    parsed = parse_target_decision(drifted, _CFG)
    _assert_fail_closed(parsed, "invalid_output")
    assert parsed.raw_response == repr(drifted)


def test_non_string_output_with_embedded_json_still_fails_closed():
    # Even when the repr of a drifted object embeds a fully valid decision JSON,
    # it must never be parsed as a live decision — non-str always fails closed.
    embedded = [f"```json\n{json.dumps(_payload())}\n```"]
    parsed = parse_target_decision(embedded, _CFG)
    _assert_fail_closed(parsed, "invalid_output")


def test_missing_field_fails_closed():
    payload = _payload()
    del payload["confidence"]
    text = f"```json\n{json.dumps(payload)}\n```"
    parsed = parse_target_decision(text, _CFG)
    _assert_fail_closed(parsed, "missing_fields")


def test_extra_field_fails_closed():
    # A field outside the contract signals schema drift; it must not be
    # silently dropped (e.g. the old rating pipeline sneaking back in).
    payload = _payload(rating="Buy")
    text = f"```json\n{json.dumps(payload)}\n```"
    parsed = parse_target_decision(text, _CFG)
    _assert_fail_closed(parsed, "unexpected_fields")


def test_malformed_json_fails_closed():
    parsed = parse_target_decision('```json\n{"decision_mode": set_target}\n```', _CFG)
    _assert_fail_closed(parsed, "invalid_output")


def test_missing_fields_never_inferred_from_prose():
    # The prose says "long, 40%", but the JSON carries no target — nothing may
    # be inferred from free text or previous rounds (DESIGN Part 2).
    payload = {"decision_mode": "set_target", "target_side": None}
    text = f"Go long with 40% margin!\n```json\n{json.dumps(payload)}\n```"
    parsed = parse_target_decision(text, _CFG)
    _assert_fail_closed(parsed, "missing_fields")


def test_parsed_decision_rejects_contradictory_validity():
    # is_valid and invalid_reason are two halves of one fact — evaluate() trusts
    # is_valid before re-checking, so a contradictory pair must die at construction.
    stub = TargetDecision.fail_closed()
    with pytest.raises(ValueError, match="is_valid"):
        # invalid, but no reason recorded
        ParsedDecision(decision=stub, is_valid=False, invalid_reason=None, raw_response="raw")
    with pytest.raises(ValueError, match="is_valid"):
        # valid, yet carrying a rejection reason
        ParsedDecision(
            decision=stub, is_valid=True, invalid_reason="invalid_output", raw_response="raw"
        )


def test_parsed_decision_rejects_invalid_with_live_target():
    # An invalid parse must carry the fail-closed stand-in, never a sized
    # set_target — the audit layer serializes ``decision`` unconditionally, so a
    # live target behind is_valid=False would misreport the rejected round.
    live = TargetDecision(
        decision_mode=DecisionMode.SET_TARGET,
        target_side=TargetSide.LONG,
        requested_target_margin_pct=50,
        confidence=Decimal("0.9"),
        rationale="hand-built",
        key_risks=("r",),
    )
    with pytest.raises(ValueError, match="fail-closed"):
        ParsedDecision(decision=live, is_valid=False, invalid_reason="x", raw_response="raw")


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------


def test_extract_prefers_last_fenced_object():
    first = '{"decision_mode": "maintain_current"}'
    last = '{"decision_mode": "set_target"}'
    text = f"Example:\n```json\n{first}\n```\nFinal:\n```json\n{last}\n```"
    assert extract_json_block(text) == last


def test_bare_brace_fallback_prefers_last_object():
    # The unfenced fallback follows the same "final answer comes last" rule as
    # the fenced path.
    first = '{"decision_mode": "maintain_current"}'
    last = '{"decision_mode": "set_target"}'
    text = f"Draft: {first}\nFinal: {last}"
    assert extract_json_block(text) == last


def test_extract_falls_back_to_bare_braces():
    obj = json.dumps(_payload())
    text = f"My final decision is {obj} — end of answer."
    assert extract_json_block(text) == obj
    parsed = parse_target_decision(text, _CFG)
    assert parsed.is_valid


def test_extract_ignores_non_object_fenced_blocks():
    text = '```json\n[1, 2, 3]\n```\nand {"decision_mode": "maintain_current"}'
    assert extract_json_block(text) == '{"decision_mode": "maintain_current"}'


def test_extract_returns_none_when_nothing_parses():
    assert extract_json_block("no json here { broken") is None


def test_extract_survives_unmatched_brace_in_prose():
    # An unmatched "{" in earlier prose must not desync the scan and swallow
    # the valid decision object that follows.
    obj = json.dumps(_payload())
    text = f"if we break resistance {{see chart, then act.\nFinal: {obj}"
    assert extract_json_block(text) == obj
    assert parse_target_decision(text, _CFG).is_valid


def test_format_instructions_schema_block_echo_fails_closed():
    # Models echo format examples, so the schema block must not be a valid
    # answer: an echo fails closed and is tagged, making it countable instead of
    # counted as a decision (see decision_format_instructions).
    parsed = parse_target_decision(decision_format_instructions(_CFG), _CFG)
    _assert_fail_closed(parsed, "invalid_decision_mode")


def test_format_instructions_schema_block_has_no_copyable_answer():
    # The counterpart to the echo test: pin the placeholders themselves, so
    # putting a concrete value back into a typed field fails here and not only
    # through the echo path. Written as literals on purpose — deriving them from
    # the module under test would let that regression pass. The margin
    # placeholder renders the live bounds (here the effective cap 60) so they
    # cannot drift from the rules text below it.
    text = decision_format_instructions(DecisionConfig(), max_pct=60)
    block = extract_json_block(text)
    assert block is not None
    payload = json.loads(block)
    assert payload["decision_mode"] == "<set_target|maintain_current>"
    assert payload["target_side"] == "<long|short|flat>"
    assert payload["requested_target_margin_pct"] == "<integer 0-60>"
    # No quoted placeholder offers `|null`. Inside quotes it invites
    # substituting in place and leaving them, and a quoted value is the one
    # mistake that costs a REAL proposal rather than an echo: the row stores
    # requested_target_margin_pct as NULL, which is exactly what "the model
    # still is not proposing" looks like on the metric this change is judged by.
    for _f in ("decision_mode", "target_side", "requested_target_margin_pct", "confidence"):
        assert "null" not in payload[_f], _f
    assert payload["confidence"] == "<0.0-1.0>"
    # key_risks is pinned too, and for a reason the typed fields don't have: the
    # legal range is 1-3, so a second slot whose cap has no named subject reads
    # as a cap on *that slot* and invites a 4-entry answer, which is discarded
    # whole as invalid_key_risks. The cap must stay attached to the array.
    assert payload["key_risks"] == ["<risk>", "<risk 2 — optional; 3 entries total maximum>"]
    normalized = " ".join(text.split())
    # Fail-closed survives without any instruction at all, so the suite would
    # stay green if the directive to substitute went missing — and the change
    # would then do nothing but hand the model an unexplained block. Pin it.
    assert "schema, not an answer" in normalized
    assert "every `<...>` placeholder MUST be replaced" in normalized
    # The two numeric fields are quoted only to keep the block valid JSON, which
    # invites substituting in place and keeping the quotes — a shape the parser
    # rejects (test_invalid_margin_not_numeric, test_invalid_confidence_not_numeric).
    # Quoting a null lands per field: target_side on invalid_target_side,
    # requested_target_margin_pct on margin_not_numeric.
    # Pin both halves by their *contents*, not just their opening clause. A
    # blanket "unquote" would strip decision_mode's quotes, and an unparseable
    # block is recorded as a bare invalid_output — the same lost cycle, minus
    # the tag naming the field that broke. In the other direction, losing a
    # field from the null carve-out steers that field's null into
    # invalid_target_side / maintain_current_with_target, and quoting it lands
    # on the per-field tags noted above. Each is a discarded cycle, the very thing this
    # contract is being reshaped to avoid, and each has at some point left every
    # other test green.
    # Pinned as narrow needles rather than whole sentences: a sentence verbatim
    # would also fail on a harmless rewording of its opening clause, and then a
    # deletion and a reword are indistinguishable from the failure.
    assert (
        'The "requested_target_margin_pct" and "confidence" placeholders are quoted only'
        in normalized
    )
    assert "write those two as bare JSON numbers" in normalized
    # The two carve-outs a blanket "everything else is a string" got wrong:
    # target_side's real value is null on maintain_current (the majority
    # outcome), and key_risks is an array whose ENTRIES are the strings.
    # Quoting a null target_side is invalid_target_side — the whole output
    # discarded, on the highest-volume path in the contract.
    # SLICED to the carve-out sentence before matching. Bare `in normalized`
    # needles were vacuous here: "decision_mode" and "target_side" both appear
    # in the leftover-placeholder sentence a paragraph earlier, so dropping
    # either from this list — the exact regression the list guards — left the
    # test green. Verified by mutation.
    # Both boundaries asserted BEFORE slicing on them. `split(x)[0]` returns the
    # whole string when x is absent, so an unpinned right needle fails OPEN: a
    # cosmetic reword of the closing sentence silently widened `carve` to
    # include the Rules bullets, which carry "decision_mode" /
    # "requested_target_margin_pct" / "rationale" verbatim — and every needle
    # below went vacuous again behind a slice that was supposed to fix exactly
    # that. Verified by mutation.
    _open, _close = "write those two as bare JSON numbers.", "A quoted number"
    assert _open in normalized
    assert _close in normalized
    carve = normalized.split(_open)[1].split(_close)[0]
    # And the slice really is the one sentence, not a runaway.
    assert len(carve) < 400
    # The null half must name BOTH nullable fields. Losing
    # requested_target_margin_pct sends a maintain_current to
    # maintain_current_with_target or margin_not_numeric; losing target_side
    # sends it to invalid_target_side. Either discards the whole output on the
    # highest-volume path in the contract.
    assert '"target_side" and' in carve
    assert '"requested_target_margin_pct"' in carve
    assert "write the JSON literal null, without quotes" in carve
    # And the quoted half must cover every remaining real value, including
    # target_side when it names a side (unquoted there, the block stops parsing
    # at all → a bare invalid_output) and the ENTRIES of key_risks, which is an
    # array rather than a string.
    assert '"decision_mode"' in carve
    assert '"target_side" when it names' in carve
    assert '"rationale"' in carve
    assert 'every entry of "key_risks"' in carve
    # The consequence names the RECORD, not the position. Both are true, but
    # the measured v2 failure was a model that preferred a costless no-op, and
    # the old wording advertised non-substitution as a route to exactly that —
    # in the most salient position in the contract. It also misdescribed what a
    # post-mortem finds, which is risk_action = invalid_fail_closed.
    assert "the cycle is recorded as a model-format failure" in normalized
    assert "the whole output is discarded and treated as maintain_current" not in normalized


def test_free_text_placeholders_stay_legal_on_purpose():
    # The mirror of the test below: rationale and key_risks are NOT type-illegal,
    # so a model that fills the four typed fields itself and leaves these two
    # echoed still produces a live sized target carrying a placeholder rationale.
    # That is the documented, accepted carve-out (see decision_format_instructions)
    # — direction and size were still the model's own choices. Pinned so that
    # tightening it, or widening the prompt's four-field promise to all six,
    # is a deliberate edit rather than a silent drift.
    parsed = parse_target_decision(
        _text(
            rationale="<one short paragraph explaining the decision>",
            key_risks=["<risk>", "<risk 2 — optional; 3 entries total maximum>"],
        ),
        _CFG,
    )
    assert parsed.is_valid
    assert parsed.decision.decision_mode is DecisionMode.SET_TARGET
    assert parsed.decision.target_side is TargetSide.LONG
    assert parsed.decision.rationale.startswith("<one short paragraph")


@pytest.mark.parametrize(
    ("field", "placeholder", "reason"),
    [
        ("decision_mode", "<set_target|maintain_current>", "invalid_decision_mode"),
        ("target_side", "<long|short|flat>", "invalid_target_side"),
        # Only type-illegality is under test, so the rendered grid is irrelevant
        # here; this spells out _CFG's own (0-100) to stay self-consistent.
        ("requested_target_margin_pct", "<integer 0-100>", "margin_not_numeric"),
        ("confidence", "<0.0-1.0>", "confidence_not_numeric"),
    ],
)
def test_leftover_placeholder_in_a_typed_field_fails_closed(field, placeholder, reason):
    # The prompt tells the model that a leftover placeholder in any of these
    # four fields discards the whole output. That promise is only true because
    # each one is typed at parse time — a partial echo (model fills some fields,
    # copies the rest) must not slip through as a live target.
    _assert_fail_closed(parse_target_decision(_text(**{field: placeholder}), _CFG), reason)


# --------------------------------------------------------------------------
# Format instructions + config validation
# --------------------------------------------------------------------------


def test_format_instructions_reflect_config():
    # The grid is the ONE thing the config still puts in the text (prompt v5;
    # the gate rules are stated without numbers — see the test below).
    cfg = DecisionConfig(
        ai_target_margin_min_pct=0,
        ai_target_margin_max_pct=80,
        target_margin_step_pct=5,
    )
    text = decision_format_instructions(cfg)
    assert "from 0 to 80 in steps of 5" in text
    # The schema block renders the same *bounds* it advertises in prose (the
    # step lives only in the prose bullet). Its unparseability is
    # config-independent — the other three typed placeholders are constant
    # literals — so that is pinned once, in the echo test above.
    assert '"requested_target_margin_pct": "<integer 0-80>"' in text


def test_format_instructions_advertise_effective_cap():
    # main.py passes the effective ceiling (grid max capped by the risk
    # allocation cap) so the model is never told a margin is legal that the
    # gate deterministically clamps: under the defaults 60, not 100.
    text = decision_format_instructions(DecisionConfig(), max_pct=60)
    assert "from 0 to 60 in steps of 1" in text
    assert "from 0 to 100" not in text


def test_format_fingerprint_follows_the_numbers_the_model_is_shown():
    # The third segmentation key (issue #129) is a digest of the RENDERED
    # block: the same config renders the same value, and any number the
    # config puts in the text — the grid step from ``decision:``, or the
    # effective ceiling ``risk.max_target_margin_pct`` caps the grid to —
    # moves it, with nothing to deploy or bump.
    base = format_fingerprint(decision_format_instructions(DecisionConfig(), max_pct=60))
    assert base == format_fingerprint(decision_format_instructions(DecisionConfig(), max_pct=60))
    assert len(base) == 16
    int(base, 16)  # hex, as the column and the RUNBOOK say
    edited = DecisionConfig(target_margin_step_pct=5)
    assert format_fingerprint(decision_format_instructions(edited, max_pct=60)) != base
    assert format_fingerprint(decision_format_instructions(DecisionConfig(), max_pct=40)) != base
    # Since prompt v5 the gate thresholds are not in the text, so editing them
    # moves NOTHING here — the key follows what the model reads, and the model
    # no longer reads them. (The gate change itself is still a run-id change
    # under the RUNBOOK's rule; that is a different key.)
    thresholds = DecisionConfig(
        min_confidence=Decimal("0.5"),
        resize_min_confidence=Decimal("0.9"),
        rebalance_deadband_pct=Decimal("3"),
    )
    assert format_fingerprint(decision_format_instructions(thresholds, max_pct=60)) == base


def test_format_instructions_carry_no_gate_threshold_number():
    # Prompt v5 (the marginal-cost plan's PR-B; the CHANGELOG entry for
    # ``phase2-target-v5`` carries the paper-BTC-2 evidence): the thresholds
    # are the gate's, not the model's — rendered as numbers they were
    # anchors — so the block states the three rules and keeps every number
    # out. Distinctive non-default values that appear nowhere else in the
    # block (the grid prints 0/60/5, ``_MAX_KEY_RISKS`` prints 3), derived
    # from the config rather than retyped, so all three are really checked.
    cfg = DecisionConfig(
        min_confidence=Decimal("0.37"),
        rebalance_deadband_pct=Decimal("2.5"),
        resize_min_confidence=Decimal("0.83"),
        target_margin_step_pct=5,
    )
    text = decision_format_instructions(cfg, max_pct=60)
    assert "from 0 to 60 in steps of 5" in text  # the grid still renders
    for value in (cfg.min_confidence, cfg.resize_min_confidence, cfg.rebalance_deadband_pct):
        assert str(value) not in text, value
    # The three rules, and the one sentence that stops the model from using
    # size to compensate for a modest confidence (phase2-spec §2.4), are
    # pinned by wording here — the only pin they have.
    normalized = " ".join(text.split())
    assert "whose confidence is too low is rejected" in normalized
    assert "creates no order (a cost-free reaffirmation)" in normalized
    assert "held to a higher confidence bar and is rejected below it" in normalized
    assert "confidence is recorded but never scales the size" in normalized
    # The gate's exemption ranking stays out too (2026-07 review decision,
    # reaffirmed for v4 and v5): the resize clause says its bar is higher,
    # never higher THAN which action's — comparing bars only teaches that a
    # full close is the guaranteed way past the gate, and since v4 the model
    # sees its own position, so it could act on that (see the docstring).
    # Pinned as the literal comparison phrase; the concept itself is prose.
    assert "higher bar than" not in normalized


def test_decision_config_rejects_bad_grid():
    with pytest.raises(ValueError, match="ai_target_margin_min_pct"):
        DecisionConfig(ai_target_margin_min_pct=50, ai_target_margin_max_pct=40)
    with pytest.raises(ValueError, match="step"):
        DecisionConfig(target_margin_step_pct=0)
    with pytest.raises(ValueError, match="min_confidence"):
        DecisionConfig(min_confidence=Decimal("1.5"))
    with pytest.raises(ValueError, match="rebalance_deadband_pct"):
        DecisionConfig(rebalance_deadband_pct=Decimal("-1"))


def test_decision_config_rejects_bad_resize_confidence():
    with pytest.raises(ValueError, match="resize_min_confidence"):
        DecisionConfig(resize_min_confidence=Decimal("1.5"))
    # A resize bar below the base bar is rejected at load (see __post_init__).
    with pytest.raises(ValueError, match="resize_min_confidence"):
        DecisionConfig(min_confidence=Decimal("0.5"), resize_min_confidence=Decimal("0.4"))


def test_decision_config_equal_confidence_bars_accepted():
    # Equal bars are the documented off-switch (SETUP.md / example yaml:
    # resize_min_confidence == min_confidence disables the extra resize bar).
    # Only a strictly lower resize bar is dead config — a `<` → `<=` slip in
    # __post_init__ would break the escape hatch with no test failing.
    cfg = DecisionConfig(min_confidence=Decimal("0.5"), resize_min_confidence=Decimal("0.5"))
    assert cfg.resize_min_confidence == cfg.min_confidence


def test_decision_config_from_dict_parses_resize_confidence():
    cfg = DecisionConfig.from_dict({"resize_min_confidence": 0.8})
    assert cfg.resize_min_confidence == Decimal("0.8")


def test_decision_config_rejects_grid_step_not_reaching_max():
    # step must divide (max - min) so the advertised max is actually on the grid;
    # otherwise a model that requests the max fails closed off-grid.
    with pytest.raises(ValueError, match="multiple of"):
        DecisionConfig(
            ai_target_margin_min_pct=0, ai_target_margin_max_pct=100, target_margin_step_pct=7
        )
    # An aligned custom grid is accepted (25/50/75/100).
    assert (
        DecisionConfig(
            ai_target_margin_max_pct=100, target_margin_step_pct=25
        ).ai_target_margin_max_pct
        == 100
    )


def test_decision_config_from_dict_nulls_fall_back():
    cfg = DecisionConfig.from_dict(
        {
            "ai_target_margin_max_pct": None,
            "min_confidence": None,
            "resize_min_confidence": None,
            "target_margin_step_pct": 2,
        }
    )
    assert cfg.ai_target_margin_max_pct == 100
    assert cfg.min_confidence == Decimal("0.3")
    # Pins the built-in 0.7 default the spec's live-inheritance note and the
    # __post_init__ error message both advertise for an absent/null key.
    assert cfg.resize_min_confidence == Decimal("0.7")
    assert cfg.target_margin_step_pct == 2


def test_config_from_dict_rejects_yaml_booleans_and_fractions():
    # YAML 1.1 reads no/yes as booleans; int()/Decimal() would silently turn
    # them into 0/1 (or die opaquely) — each must fail loud, naming the key.
    with pytest.raises(ValueError, match="ai_target_margin_min_pct"):
        DecisionConfig.from_dict({"ai_target_margin_min_pct": False})
    with pytest.raises(ValueError, match="target_margin_step_pct"):
        DecisionConfig.from_dict({"target_margin_step_pct": 5.9})  # no silent truncation
    with pytest.raises(ValueError, match="min_confidence"):
        DecisionConfig.from_dict({"min_confidence": True})
    with pytest.raises(ValueError, match="rebalance_deadband_pct"):
        DecisionConfig.from_dict({"rebalance_deadband_pct": "not-a-number"})


def test_config_from_dict_accepts_integral_float():
    # 80.0 in YAML is unambiguous for an int field; only fractions are rejected.
    cfg = DecisionConfig.from_dict({"ai_target_margin_max_pct": 80.0})
    assert cfg.ai_target_margin_max_pct == 80


def test_config_from_dict_rejects_unknown_key():
    # A typo'd key must fail loud, not silently leave the field on its default.
    with pytest.raises(ValueError, match="unknown config key"):
        DecisionConfig.from_dict({"min_confidance": 0.5})


def test_config_from_dict_rejects_non_scalar_value():
    # A YAML indentation slip turns a scalar into a list; a bare int() would raise
    # a TypeError that escapes the config-error handler — normalise to ValueError.
    with pytest.raises(ValueError, match="target_margin_step_pct"):
        DecisionConfig.from_dict({"target_margin_step_pct": [2]})


def test_config_from_dict_rejects_non_mapping_block():
    # A whole block that isn't a mapping (`decision: 0.3`) must be a named
    # ValueError, not a TypeError from set(cfg) that escapes the exit-1 handler.
    with pytest.raises(ValueError, match="mapping"):
        DecisionConfig.from_dict("0.3")


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda t: t, "invalid_decision_mode"),
        (
            lambda t: t.replace('"<integer 0-100>"', "<integer 0-100>").replace(
                '"<0.0-1.0>"', "<0.0-1.0>"
            ),
            "invalid_output",
        ),
    ],
    ids=["verbatim", "numeric-placeholders-unquoted"],
)
def test_the_echo_tag_boundary_is_pinned(mutate, reason):
    # RUNBOOK §5 and §7 hand operators a differential keyed on risk_reason, so
    # which tag each echo shape produces is a documented contract rather than an
    # accident. The unquoted variant is the one this block's own text invites:
    # it asks for the two numeric fields as bare JSON numbers, so a model that
    # obeys that clause while still echoing breaks the JSON entirely and loses
    # the tag that names the offending field.
    text = mutate(decision_format_instructions(_CFG))
    _assert_fail_closed(parse_target_decision(text, _CFG), reason)


def test_an_echo_and_a_hallucinated_mode_are_indistinguishable():
    # invalid_decision_mode is emitted for ANY mode outside the two enum
    # members, so the runbooks must not read the tag backwards as "this was an
    # echo". The discriminating content is the SHARED TAG below: each half is
    # already pinned elsewhere (the echo by test_the_echo_tag_boundary_is_pinned,
    # the hallucination by test_invalid_unknown_decision_mode), and what is new
    # is only their conjunction — it fails the moment the parser distinguishes
    # the two shapes. The decision equality that follows is a tautology by
    # contrast (ParsedDecision.__post_init__ forces every invalid parse to carry
    # fail_closed()), kept only to say the stored row is identical too. Nothing
    # else survives to tell them apart: both daemons clear pending_raw_response
    # on finalize.
    echo = parse_target_decision(decision_format_instructions(_CFG), _CFG)
    hallucinated = parse_target_decision(_text(decision_mode="hold"), _CFG)
    assert echo.invalid_reason == hallucinated.invalid_reason == "invalid_decision_mode"
    assert echo.decision == hallucinated.decision
