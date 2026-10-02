"""The guardrail rule: the committed spec, its name, and its side at every close."""

from __future__ import annotations

import json

import pytest

from contrib.autoresearch.dsl import Side, spec_hash
from contrib.autoresearch.guardrail import (
    DEFAULT_RULE,
    GuardrailError,
    build_timeline,
    load_rule,
)

from .conftest import MS_PER_HOUR, bars, candles

_STEP = 4 * MS_PER_HOUR

# Long above 102, short below 98, flat in between: a rule whose side can be
# read off a list of closes by eye, with no warm-up.
_BAND = {
    "family": "breakout",
    "entry": {
        "long": [{"left": "close", "op": ">", "right": 102}],
        "short": [{"left": "close", "op": "<", "right": 98}],
    },
    "exit": {
        "long": [{"left": "close", "op": "<", "right": 101}],
        "short": [{"left": "close", "op": ">", "right": 99}],
    },
    "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
}
_CLOSES = [100, 105, 106, 100, 95, 96, 100, 103]
_SIDES = (None, Side.LONG, Side.LONG, None, Side.SHORT, Side.SHORT, None, Side.LONG)


def _rule(tmp_path, document=_BAND, name="band.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return load_rule(path)


def _timeline(store, rule, closes=_CLOSES):
    store.upsert_candles("BTC", "4h", candles(closes))
    return build_timeline(store, rule, coin="btc", interval="4h")


# -- the rule -----------------------------------------------------------------


def test_the_committed_rule_is_the_one_that_was_decided_on():
    # Pinned on purpose. The rule was admitted on the terms "written down
    # before any result, never tuned afterwards" (2026-10-02), and this is the
    # half of that a test can hold: editing the spec file has to edit this
    # line too, in the same diff, where a reviewer sees it.
    rule = load_rule()
    assert DEFAULT_RULE.name == "btc-20d-breakout.json"
    assert rule.rule_id == "btc-20d-breakout@ca50e67a"
    assert spec_hash(rule.spec) == (
        "ca50e67ab728ea9e371334c38a07cd32fd666ee6b50937f24875534843cef791"
    )


def test_a_rule_is_named_by_its_file_and_its_spec(tmp_path):
    rule = _rule(tmp_path)
    assert rule.rule_id == f"band@{spec_hash(rule.spec)[:8]}"
    edited = json.loads(json.dumps(_BAND))
    edited["entry"]["long"][0]["right"] = 103
    assert _rule(tmp_path, edited).rule_id != rule.rule_id


def test_a_rule_file_that_cannot_be_read_or_replayed_is_refused_by_name(tmp_path):
    with pytest.raises(GuardrailError, match="could not read the guardrail rule"):
        load_rule(tmp_path / "missing.json")
    with pytest.raises(GuardrailError, match="is not a rule this package can replay"):
        _rule(tmp_path, {"family": "breakout"})


# -- its side at every close ---------------------------------------------------


def test_the_timeline_holds_the_side_after_every_bar(store, tmp_path):
    timeline = _timeline(store, _rule(tmp_path))
    assert timeline.sides == _SIDES
    assert (timeline.coin, timeline.interval) == ("BTC", "4h")
    assert (timeline.first_decided, timeline.carried) == (0, 0)


def test_a_reading_is_the_side_after_the_newest_bar_closed_by_then(store, tmp_path):
    timeline = _timeline(store, _rule(tmp_path))
    closes = timeline.close_times
    at_close = timeline.reading_at(closes[1])
    assert (at_close.side, at_close.close_time) == (Side.LONG, closes[1])
    # Inside the next bar the newest CLOSED bar is still bar 1.
    assert timeline.reading_at(closes[2] - 1).close_time == closes[1]
    assert timeline.reading_at(closes[3]).side is None  # flat is a reading
    # Before any bar has closed there is nothing to read.
    assert timeline.reading_at(closes[0] - 1) is None


def test_a_reading_past_the_end_of_the_store_goes_stale_after_two_bars(store, tmp_path):
    # The live reader's own bound on the research signal, borrowed: a side
    # decided more than two bars ago is not one the trader would be shown.
    timeline = _timeline(store, _rule(tmp_path))
    last = timeline.close_times[-1]
    assert timeline.reading_at(last + 2 * _STEP).side is Side.LONG
    assert timeline.reading_at(last + 2 * _STEP + 1) is None


def test_a_reading_before_the_rule_could_be_evaluated_is_none(store, tmp_path):
    warming = json.loads(json.dumps(_BAND))
    warming["entry"] = {"long": [{"left": "close", "op": ">", "right": "sma_10"}]}
    warming["exit"] = {"long": [{"left": "close", "op": "<", "right": "sma_10"}]}
    timeline = _timeline(store, _rule(tmp_path, warming), closes=[100 + i for i in range(12)])
    # ``sma_10`` first has a value at the tenth bar; a side before it is the
    # replay's starting state, not something the rule decided.
    assert timeline.first_decided == 9
    assert timeline.reading_at(timeline.close_times[8]) is None
    assert timeline.reading_at(timeline.close_times[9]).side is Side.LONG


def test_a_store_that_cannot_answer_is_refused_with_the_rule_named(store, tmp_path):
    rule = _rule(tmp_path)
    with pytest.raises(GuardrailError, match=r"cannot say which side band@\w{8} held on btc"):
        build_timeline(store, rule, coin="btc", interval="4h")  # no bars at all
    store.upsert_candles("BTC", "4h", bars(8, skip=[4]))
    with pytest.raises(GuardrailError, match="are not a grid"):
        build_timeline(store, rule, coin="btc", interval="4h")


def test_a_store_too_short_for_the_rule_is_refused(store, tmp_path):
    # The committed rule reads a 120-bar channel; eight bars cannot answer it,
    # and the feature stack says so before any bar is replayed.
    store.upsert_candles("BTC", "4h", candles(_CLOSES))
    with pytest.raises(GuardrailError, match="donchian_high_55 has no value at any"):
        build_timeline(store, load_rule(), coin="BTC", interval="4h")


def test_a_rule_no_bar_could_evaluate_is_refused(store, tmp_path):
    # A lagged reference has a column but nothing to read one bar back from
    # the first bar, so on a one-bar store the rule is never asked anything.
    lagged = json.loads(json.dumps(_BAND))
    lagged["entry"] = {"long": [{"left": "close", "op": ">", "right": {"feature": "close", "offset": 1}}]}
    del lagged["exit"]
    store.upsert_candles("BTC", "4h", candles([100]))
    with pytest.raises(GuardrailError, match="could not be evaluated on any of the 1 BTC 4h"):
        build_timeline(store, _rule(tmp_path, lagged), coin="BTC", interval="4h")


def test_describe_names_the_rule_and_the_share_of_each_side(store, tmp_path):
    timeline = _timeline(store, _rule(tmp_path))
    lines = timeline.describe()
    assert lines[0] == f"guardrail rule: {timeline.rule.rule_id}"
    assert "  enter long when: close > 102.0" in lines
    assert lines[-1].startswith("rule history: 8 BTC 4h bars decided, closing ")
    assert lines[-1].endswith("UTC (long 38%, flat 38%, short 25%)")
