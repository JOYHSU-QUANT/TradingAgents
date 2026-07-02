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
    TargetSide,
    decision_format_instructions,
    extract_json_block,
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


def test_invalid_margin_not_numeric():
    parsed = parse_target_decision(_text(requested_target_margin_pct="35"), _CFG)
    _assert_fail_closed(parsed, "margin_not_numeric")


def test_invalid_margin_boolean_is_not_numeric():
    parsed = parse_target_decision(_text(requested_target_margin_pct=True), _CFG)
    _assert_fail_closed(parsed, "margin_not_numeric")


def test_invalid_confidence_not_numeric():
    parsed = parse_target_decision(_text(confidence="high"), _CFG)
    _assert_fail_closed(parsed, "confidence_not_numeric")


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


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------


def test_extract_prefers_last_fenced_object():
    first = '{"decision_mode": "maintain_current"}'
    last = '{"decision_mode": "set_target"}'
    text = f"Example:\n```json\n{first}\n```\nFinal:\n```json\n{last}\n```"
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


def test_format_instructions_example_echo_is_harmless():
    # Models echo format examples; the instructions' own example must parse to
    # a maintain_current (a no-op), never to a live directional target.
    parsed = parse_target_decision(decision_format_instructions(_CFG), _CFG)
    assert parsed.is_valid
    assert parsed.decision.decision_mode.value == "maintain_current"
    assert parsed.decision.target_side is None


# --------------------------------------------------------------------------
# Format instructions + config validation
# --------------------------------------------------------------------------


def test_format_instructions_reflect_config():
    cfg = DecisionConfig(
        ai_target_margin_min_pct=0,
        ai_target_margin_max_pct=80,
        target_margin_step_pct=5,
        min_confidence=Decimal("0.4"),
    )
    text = decision_format_instructions(cfg)
    assert "from 0 to 80 in steps of 5" in text
    assert "0.4" in text
    # The instructions' own example must survive the parser it feeds.
    assert parse_target_decision(text, cfg).is_valid


def test_decision_config_rejects_bad_grid():
    with pytest.raises(ValueError, match="ai_target_margin_min_pct"):
        DecisionConfig(ai_target_margin_min_pct=50, ai_target_margin_max_pct=40)
    with pytest.raises(ValueError, match="step"):
        DecisionConfig(target_margin_step_pct=0)
    with pytest.raises(ValueError, match="min_confidence"):
        DecisionConfig(min_confidence=Decimal("1.5"))


def test_decision_config_from_dict_nulls_fall_back():
    cfg = DecisionConfig.from_dict(
        {"ai_target_margin_max_pct": None, "min_confidence": None, "target_margin_step_pct": 2}
    )
    assert cfg.ai_target_margin_max_pct == 100
    assert cfg.min_confidence == Decimal("0.3")
    assert cfg.target_margin_step_pct == 2
