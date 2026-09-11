"""What the spec parser accepts, and — mostly — what it refuses and how it says so.

The refusals are the substance of this file. A parser for a language a model
writes in is a filter on what a trial may be spent on, so each test here names
one shape that PARSES as grammar and would be meaningless as a hypothesis: a
comparison across units, an equality on a float, a knob nothing reads, a
window reaching into the future.

Messages are asserted on, not just the exception type. The sentence is what
the next round of the hypothesis loop is shown (plan §3.11), so a refusal that
did not say which edit to make would spend the trial and teach nothing.
"""

from __future__ import annotations

import json
import re
import textwrap
from pathlib import Path

import pytest

from contrib.autoresearch import dsl
from contrib.autoresearch.dsl import (
    LIVE_MARGIN_CAP,
    MAX_HOLD_BARS,
    Condition,
    Family,
    Op,
    Side,
    Sizing,
    SizingMode,
    SpecError,
    StrategySpec,
    describe_spec,
    load_spec,
    parse_spec,
)
from contrib.autoresearch.vocabulary import MAX_OFFSET_BARS, FeatureKind, FeatureRef


def _base(**overrides) -> dict:
    """The smallest spec that parses: one long entry and a size."""
    body = {
        "family": "breakout",
        "entry": {"long": [{"left": "close", "op": ">", "right": "donchian_high_20"}]},
        "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.25},
    }
    body.update(overrides)
    return body


def _message(payload: object) -> str:
    with pytest.raises(SpecError) as caught:
        parse_spec(payload)
    return str(caught.value)


# -- what a whole spec becomes ---------------------------------------------


def test_the_documented_example_is_a_spec_this_parser_accepts():
    """The worked example in the module docstring, parsed rather than admired.

    Read out of the source, so the two cannot drift: an example a reader
    copies is the first thing tried against ``validate-spec``, and the first
    version of this one was refused by the rule its own paragraph teaches (a
    ``params`` entry no condition referenced).
    """
    source = Path(dsl.__file__).read_text(encoding="utf-8")
    block = source[source.index('    {\n      "family"') :]
    document = block[: block.index("\n    }") + len("\n    }")]
    # The example carries ``# optional`` markers, which JSON does not have.
    # Stripping them is the only liberty taken with it.
    without_notes = "\n".join(re.sub(r"\s+#.*$", "", line) for line in document.splitlines())
    spec = load_spec(textwrap.dedent(without_notes))
    assert spec.family is Family.BREAKOUT
    assert spec.max_bars == 30
    assert dict(spec.params) == {"cool_off": 45.0}


def test_a_full_spec_parses_into_the_rules_it_describes():
    spec = parse_spec(
        _base(
            family="funding_filter",
            entry={
                "long": [
                    {"left": "close", "op": ">", "right": "donchian_high_20"},
                    {"left": "close", "op": ">", "right": {"feature": "close", "offset": 1}},
                ],
                "short": [{"left": "rsi_14", "op": ">", "right": {"param": "hot"}}],
            },
            exit={"long": [{"left": "close", "op": "<", "right": "ema_20"}], "max_bars": 30},
            filters=[{"left": "funding_zscore_30", "op": "<", "right": 2.0}],
            params={"hot": 70},
        )
    )
    assert spec.family is Family.FUNDING_FILTER
    assert len(spec.entries(Side.LONG)) == 2
    assert spec.entries(Side.SHORT)[0].op is Op.GT
    assert spec.entries(Side.SHORT)[0].right == 70.0
    assert spec.entries(Side.SHORT)[0].right_param == "hot"
    assert spec.exits(Side.SHORT) == ()
    assert spec.max_bars == 30
    assert spec.sizing.mode is SizingMode.FIXED_MARGIN_FRACTION


def test_the_feature_set_is_derived_from_the_conditions_and_the_sizing():
    """Including the one no condition mentions: ``vol_target``'s own lookback."""
    spec = parse_spec(
        _base(
            family="vol_targeting",
            sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 20},
        )
    )
    assert [str(ref) for ref in spec.features] == ["close", "donchian_high_20", "realized_vol_20"]


def test_a_repeated_feature_is_listed_once_and_an_offset_counts_as_its_own():
    spec = parse_spec(
        _base(
            entry={
                "long": [
                    {"left": "close", "op": ">", "right": "donchian_high_20"},
                    {"left": "close", "op": ">", "right": {"feature": "close", "offset": 2}},
                ]
            }
        )
    )
    assert [str(ref) for ref in spec.features] == ["close", "close[2]", "donchian_high_20"]


def test_a_spec_reads_back_as_the_rules_the_evaluator_will_act_on():
    spec = parse_spec(
        _base(
            family="vol_targeting",
            exit={"long": [{"left": "rsi_14", "op": "<", "right": {"param": "cool"}}]},
            filters=[{"left": "regime", "op": "==", "right": "trending"}],
            params={"cool": 45},
            sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 20},
        )
    )
    rendered = "\n".join(describe_spec(spec))
    assert "enter long when: close > donchian_high_20" in rendered
    assert "exit long when: rsi_14 < cool (45.0)" in rendered  # the name AND the number
    assert "only while: regime == trending" in rendered
    assert "never above 0.6 of equity" in rendered  # the live clamp, filled in as the default


def test_conditions_in_one_list_are_read_as_all_of_them_together():
    """``AND`` is stated in the rendering rather than left for a reader to assume."""
    spec = parse_spec(
        _base(
            entry={
                "long": [
                    {"left": "close", "op": ">", "right": "ema_20"},
                    {"left": "rsi_14", "op": "<", "right": 70},
                ]
            }
        )
    )
    assert "close > ema_20 AND rsi_14 < 70.0" in "\n".join(describe_spec(spec))


# -- leakage (plan §3.6a) --------------------------------------------------


def test_a_condition_reaching_into_the_future_is_refused():
    """The leakage test the plan names: a spec reading the next bar cannot parse."""
    message = _message(
        _base(
            entry={
                "long": [
                    {
                        "left": {"feature": "close", "offset": -1},
                        "op": ">",
                        "right": "donchian_high_20",
                    }
                ]
            }
        )
    )
    assert "has not closed" in message


def test_a_future_offset_on_the_right_hand_side_is_refused_too():
    """Both sides go through the same construction, so both meet the same guard."""
    message = _message(
        _base(
            entry={
                "long": [{"left": "close", "op": ">", "right": {"feature": "close", "offset": -3}}]
            }
        )
    )
    assert "has not closed" in message


def test_a_lag_within_the_bound_is_accepted_so_the_refusal_is_of_direction_only():
    spec = parse_spec(
        _base(
            entry={
                "long": [
                    {
                        "left": "close",
                        "op": ">",
                        "right": {"feature": "close", "offset": MAX_OFFSET_BARS},
                    }
                ]
            }
        )
    )
    assert spec.entries(Side.LONG)[0].right.offset == MAX_OFFSET_BARS


def test_an_offset_that_is_not_a_whole_number_of_bars_is_refused():
    """And every offset refusal says WHERE in the document it happened.

    All three offset rules belong to ``FeatureRef``, which cannot know that.
    While the parser re-checked this one itself, its sentence was the only one
    of the three carrying a path — so the two an LLM actually meets (a
    negative offset, one past the bound) arrived with nothing to locate them.
    """
    message = _message(
        _base(entry={"long": [{"left": {"feature": "close", "offset": 1.5}, "op": ">", "right": 1}]})
    )
    assert message.startswith("spec.entry.long[0].left: ")
    assert "whole number of bars" in message
    assert _message(
        _base(entry={"long": [{"left": {"feature": "close", "offset": -1}, "op": ">", "right": 1}]})
    ).startswith("spec.entry.long[0].left: ")


# -- comparisons that parse and mean nothing -------------------------------


def test_comparing_two_features_of_different_units_is_refused():
    message = _message(_base(entry={"long": [{"left": "close", "op": ">", "right": "rsi_14"}]}))
    assert "measured in price" in message
    assert "well-formed and meaningless" in message


def test_equality_on_a_computed_number_is_refused_as_a_rule_that_never_fires():
    message = _message(_base(entry={"long": [{"left": "ema_20", "op": "==", "right": 30000}]}))
    assert "never fires" in message
    assert "'>'" in message  # and it says which operators to use instead


def test_the_regime_is_compared_with_equality_and_nothing_else():
    accepted = parse_spec(_base(filters=[{"left": "regime", "op": "!=", "right": "volatile"}]))
    assert accepted.filters[0].op is Op.NE
    message = _message(_base(filters=[{"left": "regime", "op": ">", "right": "trending"}]))
    assert "the regime is a label" in message


def test_the_regime_is_compared_against_a_label_it_actually_has():
    message = _message(_base(filters=[{"left": "regime", "op": "==", "right": "bullish"}]))
    assert "unsupported market regime 'bullish'" in message
    assert "spec.filters[0].right" in message


def test_the_regime_is_not_compared_against_a_number():
    message = _message(_base(filters=[{"left": "regime", "op": "==", "right": 1}]))
    assert "the regime is compared with one of" in message


# -- shapes that are not the language --------------------------------------


def test_a_declared_feature_list_is_refused_because_the_set_is_derived():
    message = _message(_base(features=["close", "ema_20"]))
    assert "derived from the conditions, not declared" in message


def test_an_unknown_key_anywhere_is_named_with_what_that_level_accepts():
    assert "unknown key(s) ['stop_loss']" in _message(_base(stop_loss=0.02))
    assert "unknown key(s) ['when']" in _message(
        _base(entry={"long": [{"left": "close", "op": ">", "right": 1, "when": "always"}]})
    )
    assert "unknown key(s) ['both']" in _message(
        _base(entry={"long": [{"left": "close", "op": ">", "right": 1}], "both": []})
    )


def test_a_missing_required_key_is_named():
    assert "missing required key(s) ['sizing']" in _message(
        {"family": "breakout", "entry": {"long": [{"left": "close", "op": ">", "right": 1}]}}
    )
    assert "missing required key(s) ['right']" in _message(
        _base(entry={"long": [{"left": "close", "op": ">"}]})
    )


def test_an_unknown_family_or_operator_lists_the_ones_there_are():
    assert "unsupported strategy family 'carry_trade'" in _message(_base(family="carry_trade"))
    assert "unsupported comparison operator '=~'" in _message(
        _base(entry={"long": [{"left": "close", "op": "=~", "right": 1}]})
    )


def test_a_condition_list_that_is_not_a_list_is_refused():
    assert "expected a list of conditions" in _message(
        _base(entry={"long": {"left": "close", "op": ">", "right": 1}})
    )
    assert "expected a list of conditions" in _message(_base(filters="regime == trending"))


def test_something_that_is_not_an_object_at_all_is_refused():
    assert "expected an object, got list" in _message(["close > ema_20"])


def test_a_threshold_that_is_a_boolean_is_not_a_number():
    """``isinstance(True, int)`` is true, so this would otherwise mean ``1.0``."""
    assert "expected a number, got True" in _message(
        _base(entry={"long": [{"left": "close", "op": ">", "right": True}]})
    )


# -- params ----------------------------------------------------------------


def test_a_parameter_nothing_refers_to_is_refused():
    message = _message(_base(params={"unused": 5}))
    assert "declared and referred to by no condition" in message
    assert '{"param": "unused"}' in message


def test_a_reference_to_a_parameter_that_was_never_declared_is_refused():
    message = _message(
        _base(entry={"long": [{"left": "rsi_14", "op": "<", "right": {"param": "floor"}}]})
    )
    assert "no parameter named 'floor' is declared" in message


def test_a_parameter_name_that_is_not_a_name_is_refused():
    assert "is not a usable parameter name" in _message(_base(params={"Floor Level": 5}))


def test_a_parameter_whose_value_is_not_a_number_is_refused():
    assert "spec.params.floor: expected a number" in _message(_base(params={"floor": "low"}))


# -- structure -------------------------------------------------------------


def test_a_spec_that_can_never_take_a_position_is_refused():
    assert "has to be able to take a position" in _message(_base(entry={}))


def test_an_exit_for_a_side_that_never_enters_is_refused():
    message = _message(_base(exit={"short": [{"left": "close", "op": ">", "right": "ema_20"}]}))
    assert "there are no short entries" in message


def test_a_hold_limit_outside_its_bounds_is_refused():
    assert f"between 1 and {MAX_HOLD_BARS}" in _message(_base(exit={"max_bars": 0}))
    assert f"between 1 and {MAX_HOLD_BARS}" in _message(_base(exit={"max_bars": MAX_HOLD_BARS + 1}))
    assert "expected a whole number, got 12.5" in _message(_base(exit={"max_bars": 12.5}))


def test_a_spec_with_no_exit_block_at_all_is_a_position_that_is_never_closed():
    """Legitimate, and deliberately so: buy-and-hold is one of PR A4's baselines."""
    spec = parse_spec(_base())
    assert spec.exits(Side.LONG) == ()
    assert spec.max_bars is None


# -- the family claims that are checkable ----------------------------------


def test_a_regime_filter_has_to_read_the_regime():
    assert "no condition here refers to 'regime'" in _message(_base(family="regime_filter"))
    parsed = parse_spec(
        _base(family="regime_filter", filters=[{"left": "regime", "op": "==", "right": "ranging"}])
    )
    assert FeatureKind.REGIME in {ref.kind for ref in parsed.features}


def test_a_funding_filter_has_to_read_funding():
    assert "no condition here refers to a funding feature" in _message(
        _base(family="funding_filter")
    )


def test_vol_targeting_has_to_size_by_volatility():
    message = _message(_base(family="vol_targeting"))
    assert "sizes by volatility" in message
    assert "'fixed_margin_fraction'" in message


def test_breakout_and_mean_reversion_carry_no_structural_check():
    """Stated as a test because it is a decision, not an omission.

    The same conditions can be either idea, so a check invented for them would
    refuse honest specs and prove nothing. What follows from it is a warning
    for the trial penalty in plan §3.10: relabelling a spec is free, so the
    penalty cannot key on this field alone.
    """
    conditions = [{"left": "rsi_14", "op": "<", "right": 30}]
    for family in ("breakout", "mean_reversion"):
        spec = parse_spec(_base(family=family, entry={"long": conditions}))
        assert spec.family.value == family


# -- sizing ----------------------------------------------------------------


def test_a_margin_fraction_is_a_fraction_of_the_account():
    assert "above 0 and at most 1" in _message(
        _base(sizing={"mode": "fixed_margin_fraction", "fraction": 1.5})
    )
    assert "above 0 and at most 1" in _message(
        _base(sizing={"mode": "fixed_margin_fraction", "fraction": 0})
    )


def test_a_sizing_block_carries_only_its_own_modes_fields():
    """A knob read by nothing is a knob whose author believes it is working."""
    assert "unknown key(s) ['fraction']" in _message(
        _base(
            family="vol_targeting",
            sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 20, "fraction": 0.25},
        )
    )


def test_a_volatility_lookback_has_to_be_one_the_vocabulary_computes():
    """Otherwise the sizing rule asks the frame for a series that does not exist."""
    message = _message(
        _base(
            family="vol_targeting",
            sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 33},
        )
    )
    assert "realized_vol_N" in message
    assert "[10, 20, 50] bars" in message


@pytest.mark.parametrize("target", [0, 0.0005, 0.6], ids=["zero", "tiny", "annualised"])
def test_a_volatility_target_has_to_be_a_plausible_per_bar_deviation(target):
    """0.60 is the one that matters: an annualised figure, written per bar.

    It parses, it passes the family check, and it pins the size to the cap at
    every single bar — a fixed-size strategy wearing a vol-targeting label and
    being scored as one.
    """
    message = _message(
        _base(
            family="vol_targeting",
            sizing={"mode": "vol_target", "target_vol": target, "lookback": 20},
        )
    )
    assert "PER-BAR return deviation" in message
    assert "0.001..0.1" in message


def test_a_volatility_cap_defaults_to_what_the_live_path_would_allow():
    """Not to the whole account: a size RiskGate clamps is a size never run at."""
    spec = parse_spec(
        _base(
            family="vol_targeting",
            sizing={"mode": "vol_target", "target_vol": 0.02, "lookback": 20},
        )
    )
    assert spec.sizing.max_fraction == LIVE_MARGIN_CAP


def test_an_unknown_sizing_mode_lists_the_two_there_are():
    assert "unsupported sizing mode 'kelly'" in _message(_base(sizing={"mode": "kelly"}))
    assert "needs a 'mode'" in _message(_base(sizing={"fraction": 0.25}))


# -- text in ---------------------------------------------------------------


def test_json_text_parses_the_same_way_an_object_does():
    assert load_spec(json.dumps(_base())).family is Family.BREAKOUT


def test_malformed_json_is_a_spec_failure_with_the_decoders_own_sentence():
    """A model emitting a trailing comma has spent the trial like any other refusal."""
    with pytest.raises(SpecError) as caught:
        load_spec('{"family": "breakout",}')
    assert "spec is not valid JSON" in str(caught.value)


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_jsons_non_standard_infinities_are_refused_by_name(literal):
    """``json.loads`` accepts these; a threshold cannot be either of them."""
    text = json.dumps(_base()).replace('"donchian_high_20"', literal)
    with pytest.raises(SpecError) as caught:
        load_spec(text)
    assert "JSON extension" in str(caught.value)


def test_a_number_too_large_for_a_float_is_refused_rather_than_read_as_infinity():
    """``1e400`` decodes to ``inf`` with no error of its own."""
    text = json.dumps(_base()).replace('"donchian_high_20"', "1e400")
    with pytest.raises(SpecError) as caught:
        load_spec(text)
    assert "expected a finite number" in str(caught.value)


def test_an_integer_too_large_for_a_float_is_refused_rather_than_raised_on():
    """``float()`` on a 400-digit int raises OverflowError — outside the refusal lane.

    JSON puts no limit on an integer literal, and ``OverflowError`` is an
    ``ArithmeticError``, so it escaped the CLI as a traceback and would reach
    the hypothesis loop as a crash rather than as a note to show the model.
    """
    text = json.dumps(_base()).replace('"donchian_high_20"', "9" * 400)
    with pytest.raises(SpecError) as caught:
        load_spec(text)
    assert "is too large to be a number" in str(caught.value)


def test_a_key_written_twice_is_refused_rather_than_resolved_to_the_last_one():
    """``json.loads`` keeps the second, so the OPPOSITE rule would be scored.

    The same argument the unknown-key refusal makes — a key whose author
    believes it is doing something — one layer lower, where the loss happens.
    """
    text = json.dumps(_base()).replace('"op": ">"', '"op": ">", "op": "<"')
    with pytest.raises(SpecError) as caught:
        load_spec(text)
    assert "appears twice in the same object" in str(caught.value)


# -- the numeric half of the unit rule -------------------------------------


@pytest.mark.parametrize(
    ("left", "threshold", "scale"),
    [
        ("rsi_14", 150, "oscillator"),
        ("ret_6", -2, "return"),
        ("close", -1, "price"),
        ("atr_14", -5, "price_span"),
    ],
)
def test_a_threshold_the_feature_could_never_cross_is_refused(left, threshold, scale):
    """The numeric twin of ``close > rsi_14``: a rule that cannot fire.

    An oscillator is defined on 0..100, a price cannot be negative, and a
    return cannot be below -100% — so each of these is a hypothesis with no
    hypothesis in it, and would otherwise be scored as one that was tried.
    """
    message = _message(_base(entry={"long": [{"left": left, "op": ">", "right": threshold}]}))
    assert f"measured in {scale}" in message
    assert "never cross" in message


@pytest.mark.parametrize("left", ["funding_zscore_30", "funding_rate", "funding_cum_24"])
def test_a_feature_with_no_definitional_bound_takes_any_finite_threshold(left):
    """A z-score of 245 is a real reading, so ±10 was a cap with no arithmetic in it.

    Measured on ordinary hourly funding, a single spike scores in the
    hundreds — and that is exactly where a ``funding_filter`` hypothesis
    lives, so capping it would have refused the family's own signal with a
    sentence claiming the threshold could never be crossed.
    """
    spec = parse_spec(
        _base(
            family="funding_filter",
            entry={"long": [{"left": left, "op": ">", "right": 250}]},
        )
    )
    assert spec.entries(Side.LONG)[0].right == 250.0


def test_a_threshold_merely_on_the_wrong_scale_is_not_caught():
    """Recorded as a limit, so the guard above is not read as more than it is.

    ``rsi_14 < 0.3`` is a model treating a 0..100 oscillator as 0..1. It is
    inside the bounds, it never fires, and only the hypothesis loop's failure
    summary will say so. Tightening to catch it would need plausibility
    ranges, and a range that refuses an unusual-but-real threshold costs more
    than the trial it saves.
    """
    spec = parse_spec(_base(entry={"long": [{"left": "rsi_14", "op": "<", "right": 0.3}]}))
    assert spec.entries(Side.LONG)[0].right == 0.3


def test_a_threshold_that_arrived_through_a_parameter_is_checked_the_same_way():
    message = _message(
        _base(
            entry={"long": [{"left": "rsi_14", "op": "<", "right": {"param": "ceiling"}}]},
            params={"ceiling": 150},
        )
    )
    assert "'ceiling' (150)" in message


def test_a_price_level_is_not_compared_against_a_distance():
    """``atr_14`` is quote currency like a close is, and is not a level.

    This passed the first cut of the unit table and is true at essentially
    every bar — the same defect as ``close > rsi_14``, one unit later.
    """
    message = _message(_base(entry={"long": [{"left": "close", "op": ">", "right": "atr_14"}]}))
    assert "measured in price and atr_14 in price_span" in message


def test_one_settlement_is_not_compared_against_a_sum_of_them():
    """Measured on BTC-shaped funding, this fired at 276 bars out of 276."""
    message = _message(
        _base(
            family="funding_filter",
            entry={"long": [{"left": "funding_cum_24", "op": ">", "right": "funding_rate"}]},
        )
    )
    assert "measured in rate_sum and funding_rate in rate" in message


# -- invariants that belong to the types, not to the parser ----------------


def test_a_condition_built_in_code_is_checked_the_way_a_parsed_one_is():
    """PR A3 mutates specs and PR A4 reloads them; neither goes through the parser.

    ``FeatureRef`` already works this way for periods and offsets. A rule that
    holds only for the documents the parser happens to see is not a rule about
    conditions.
    """
    price = FeatureRef(FeatureKind.CLOSE)
    with pytest.raises(SpecError, match="well-formed and meaningless"):
        Condition(left=price, op=Op.GT, right=FeatureRef(FeatureKind.RSI, 14))
    with pytest.raises(SpecError, match="never fires"):
        Condition(left=price, op=Op.EQ, right=30000.0)
    with pytest.raises(SpecError, match="names the parameter a NUMBER came from"):
        Condition(
            left=price, op=Op.GT, right=FeatureRef(FeatureKind.EMA, 20), right_param="floor"
        )


def test_a_threshold_built_in_code_is_checked_against_the_features_scale_too():
    """A threshold is the first field a later phase perturbs when it mutates a spec.

    While this check lived on the JSON path alone, ``rsi_14 > 150`` built in
    code round-tripped as a valid spec and would be scored as a rule that was
    tried.
    """
    with pytest.raises(SpecError, match="never cross"):
        Condition(left=FeatureRef(FeatureKind.RSI, 14), op=Op.GT, right=150.0)
    with pytest.raises(SpecError, match="finite number"):
        Condition(left=FeatureRef(FeatureKind.CLOSE), op=Op.GT, right=float("inf"))
    # And the two shapes that escaped as a ``TypeError`` — outside the lane
    # the CLI catches and the hypothesis loop turns into a note, which is the
    # same escape the huge-integer overflow made at the parser.
    with pytest.raises(SpecError, match="expected a number, got 'x'"):
        Condition(left=FeatureRef(FeatureKind.CLOSE), op=Op.GT, right="x")
    with pytest.raises(SpecError, match="expected a number, got True"):
        Condition(left=FeatureRef(FeatureKind.RSI, 14), op=Op.GT, right=True)
    # Including the one that escaped as an OverflowError while this guard and
    # the parser's were two guards instead of one.
    with pytest.raises(SpecError, match="too large to be a number"):
        Condition(left=FeatureRef(FeatureKind.CLOSE), op=Op.GT, right=10**400)


def test_a_sizing_built_in_code_cannot_carry_the_other_modes_fields():
    with pytest.raises(SpecError, match="does not read 'fraction'"):
        Sizing(mode=SizingMode.VOL_TARGET, target_vol=0.02, vol_lookback=20, fraction=0.25)
    with pytest.raises(SpecError, match="needs 'fraction'"):
        Sizing(mode=SizingMode.FIXED_MARGIN_FRACTION)
    with pytest.raises(SpecError, match="needs 'max_fraction'"):
        Sizing(mode=SizingMode.VOL_TARGET, target_vol=0.02, vol_lookback=20)
    for broken in (
        {"mode": SizingMode.FIXED_MARGIN_FRACTION, "fraction": "x"},
        {"mode": SizingMode.VOL_TARGET, "target_vol": "x", "vol_lookback": 20, "max_fraction": 0.5},
    ):
        # Refused as a spec failure, not as a ``TypeError`` from the first
        # comparison that meets the string.
        with pytest.raises(SpecError, match="expected a number, got 'x'"):
            Sizing(**broken)


def test_a_volatility_lookback_is_judged_by_the_vocabulary_that_owns_periods():
    """``10.0 in (10, 20, 50)`` is true, which is why membership was not enough.

    A float lookback passed the old check and then made ``Sizing.refs`` raise
    from a property — the failure the guard was added to prevent, one type
    along.
    """
    with pytest.raises(SpecError) as caught:
        Sizing(mode=SizingMode.VOL_TARGET, target_vol=0.02, vol_lookback=10.0, max_fraction=0.5)
    # The sentence is the sizing rule's, since that is the field the author
    # wrote; the vocabulary's own reason is the cause it was raised from.
    assert "one of [10, 20, 50] bars, got 10.0" in str(caught.value)
    assert "a period is a whole number" in str(caught.value.__cause__)


def test_a_spec_built_in_code_meets_the_structural_rules_too():
    sizing = Sizing(mode=SizingMode.FIXED_MARGIN_FRACTION, fraction=0.25)
    entry = Condition(
        left=FeatureRef(FeatureKind.CLOSE), op=Op.GT, right=FeatureRef(FeatureKind.EMA, 20)
    )
    with pytest.raises(SpecError, match="take a position"):
        StrategySpec(
            family=Family.BREAKOUT,
            entry_long=(),
            entry_short=(),
            exit_long=(),
            exit_short=(),
            filters=(),
            sizing=sizing,
        )
    with pytest.raises(SpecError, match="'regime_filter' names a rule"):
        StrategySpec(
            family=Family.REGIME_FILTER,
            entry_long=(entry,),
            entry_short=(),
            exit_long=(),
            exit_short=(),
            filters=(),
            sizing=sizing,
        )
    # And the hold bound, which exists so ``max_bars`` cannot become a second
    # spelling of "never exit" — a later phase mutating a hold length does not
    # pass the parser that used to be the only place this was checked.
    def _held_for(max_bars):
        return StrategySpec(
            family=Family.BREAKOUT,
            entry_long=(entry,),
            entry_short=(),
            exit_long=(),
            exit_short=(),
            filters=(),
            sizing=sizing,
            max_bars=max_bars,
        )

    with pytest.raises(SpecError, match=f"between 1 and {MAX_HOLD_BARS}"):
        _held_for(10**9)
    # And its type, for the reason the thresholds' is checked: 12.5 bars is
    # not a hold, ``True`` is not one either, and a string left the bound
    # comparison as a TypeError outside the refusal lane.
    for wrong in (12.5, True, "x"):
        with pytest.raises(SpecError, match="a whole number of bars"):
            _held_for(wrong)


def test_two_specs_that_say_the_same_thing_land_in_one_place_in_a_set():
    """A ledger deduplicating hypotheses reaches for a set first (plan PR A4).

    ``params`` was a dict, which made ``hash(spec)`` raise on a frozen type
    that advertises hashability.
    """
    written = {
        "long": [{"left": "rsi_14", "op": "<", "right": {"param": "floor"}}]
    }
    first = parse_spec(_base(params={"floor": 30}, entry=written))
    same = parse_spec(_base(params={"floor": 30}, entry=written))
    other = parse_spec(_base(entry={"long": [{"left": "rsi_14", "op": "<", "right": 40}]}))
    assert len({first, same, other}) == 2


def test_a_parameter_value_is_checked_on_the_type_as_well():
    """A param is printed back beside the number it stands for.

    So one carrying a string while its condition carries the real threshold is
    a report that disagrees with what was measured — and a spec built in code
    does not pass the parser that used to be the only thing checking this.
    """
    sizing = Sizing(mode=SizingMode.FIXED_MARGIN_FRACTION, fraction=0.25)
    entry = Condition(
        left=FeatureRef(FeatureKind.RSI, 14), op=Op.LT, right=30.0, right_param="oversold"
    )
    with pytest.raises(SpecError, match="expected a number, got 'x'"):
        StrategySpec(
            family=Family.MEAN_REVERSION,
            entry_long=(entry,),
            entry_short=(),
            exit_long=(),
            exit_short=(),
            filters=(),
            sizing=sizing,
            params=(("oversold", "x"),),
        )
