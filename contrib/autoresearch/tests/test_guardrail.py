"""The guardrail rule: the committed spec, its name, and its side at every close."""

from __future__ import annotations

import json

import pytest

from contrib.autoresearch.dsl import Side, spec_hash
from contrib.autoresearch.guardrail import (
    DEFAULT_RULE,
    RULE_INTERVAL,
    GuardrailError,
    build_timeline,
    load_rule,
)
from contrib.autoresearch.upstream import from_epoch_ms

from .conftest import ANCHOR_MS, MS_PER_HOUR, bars, candles, funding_points

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
    return build_timeline(store, rule, coin="btc")


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
    # The warm-up is not history: the report counts the three decided bars,
    # from the first of them, and none of them as carried.
    first = f"{from_epoch_ms(timeline.close_times[9]):%Y-%m-%d %H:%M}"
    assert timeline.describe()[-1].startswith(f"rule history: 3 BTC 4h bars decided, closing {first} to ")
    assert timeline.carried == 0


def test_a_bar_the_rule_could_not_evaluate_has_no_reading(store, tmp_path):
    # Settlements for the first eight hours only: the rule reads the funding
    # rate, so it is asked at bars 0 and 1 and cannot be asked at bars 2 and
    # 3, where it stays long because it could not have left. The live path
    # writes no signal off such a bar, and neither does this.
    frozen = {
        "family": "breakout",
        "entry": {"long": [{"left": "funding_rate", "op": ">", "right": -1}]},
        "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
    }
    store.upsert_funding("BTC", funding_points(8, start_ms=ANCHOR_MS + MS_PER_HOUR))
    timeline = _timeline(store, _rule(tmp_path, frozen), closes=[100, 101, 102, 103])
    assert timeline.unevaluable == (False, False, True, True)
    assert timeline.sides == (Side.LONG,) * 4
    assert (timeline.first_decided, timeline.carried) == (0, 2)
    closes = timeline.close_times
    assert timeline.reading_at(closes[1]).side is Side.LONG
    assert timeline.reading_at(closes[2]) is None
    assert timeline.reading_at(closes[3]) is None
    assert timeline.describe()[-1] == (
        "  on 2 of them the rule could not be evaluated and kept the side it was on; an instant "
        "read off one of those has no reading"
    )


def test_the_committed_rule_takes_a_side_once_its_channel_has_warmed_up(store):
    # The rule that will actually be read, on history long enough to answer
    # it. Each bar closes one above the last and its high is its close, so
    # from the first bar with 120 bars behind it every close is above the
    # highest high of those 120: the rule goes long there and stays long.
    closes = [100 + i for i in range(125)]
    store.upsert_candles("BTC", RULE_INTERVAL, candles(closes, highs=closes, lows=closes))
    timeline = build_timeline(store, load_rule(), coin="BTC")
    assert timeline.interval == RULE_INTERVAL == "4h"
    assert timeline.first_decided == 120
    assert timeline.sides == (None,) * 120 + (Side.LONG,) * 5
    assert timeline.carried == 0
    assert timeline.reading_at(timeline.close_times[119]) is None
    assert timeline.reading_at(timeline.close_times[120]).side is Side.LONG


def test_the_committed_rule_leaves_on_the_55_bar_channel_and_goes_short_on_the_120(store):
    # The other half of the rule. 122 rising bars (closes 100..221, long from
    # bar 120), then three closes read against the channels behind each:
    # - 180: above the lowest low of the 55 bars before it (167), so still
    #   long. A 20-bar exit channel (low 202) would have left here.
    # - 150: below the 55-bar low (now 168), so flat; above the 120-bar low
    #   (103), so not short.
    # - 95: below the 120-bar low (104), so short.
    closes = [100 + i for i in range(122)] + [180, 150, 95]
    store.upsert_candles("BTC", RULE_INTERVAL, candles(closes, highs=closes, lows=closes))
    timeline = build_timeline(store, load_rule(), coin="BTC")
    assert timeline.sides[120:] == (Side.LONG, Side.LONG, Side.LONG, None, Side.SHORT)


def test_the_committed_rule_goes_short_on_a_falling_channel(store):
    # The mirror of the rising series: each close one below the last.
    closes = [300 - i for i in range(125)]
    store.upsert_candles("BTC", RULE_INTERVAL, candles(closes, highs=closes, lows=closes))
    timeline = build_timeline(store, load_rule(), coin="BTC")
    assert timeline.sides == (None,) * 120 + (Side.SHORT,) * 5


def test_a_store_that_cannot_answer_is_refused_with_the_rule_named(store, tmp_path):
    rule = _rule(tmp_path)
    with pytest.raises(GuardrailError, match=r"cannot say which side band@\w{8} held on btc"):
        build_timeline(store, rule, coin="btc")  # no bars at all
    store.upsert_candles("BTC", "4h", bars(8, skip=[4]))
    with pytest.raises(GuardrailError, match="are not a grid"):
        build_timeline(store, rule, coin="btc")


def test_a_store_too_short_for_the_rule_is_refused(store, tmp_path):
    # The committed rule reads a 120-bar channel; eight bars cannot answer it,
    # and the feature stack says so before any bar is replayed.
    store.upsert_candles("BTC", "4h", candles(_CLOSES))
    with pytest.raises(GuardrailError, match="donchian_high_55 has no value at any"):
        build_timeline(store, load_rule(), coin="BTC")


def test_a_rule_no_bar_could_evaluate_is_refused(store, tmp_path):
    # A lagged reference has a column but nothing to read one bar back from
    # the first bar, so on a one-bar store the rule is never asked anything.
    lagged = json.loads(json.dumps(_BAND))
    previous_close = {"feature": "close", "offset": 1}
    lagged["entry"] = {"long": [{"left": "close", "op": ">", "right": previous_close}]}
    del lagged["exit"]
    store.upsert_candles("BTC", "4h", candles([100]))
    with pytest.raises(GuardrailError, match="could not be evaluated on any of the 1 BTC 4h"):
        build_timeline(store, _rule(tmp_path, lagged), coin="BTC")


def test_describe_names_the_rule_and_the_share_of_each_side(store, tmp_path):
    timeline = _timeline(store, _rule(tmp_path))
    lines = timeline.describe()
    assert lines[0] == f"guardrail rule: {timeline.rule.rule_id}"
    assert "  enter long when: close > 102.0" in lines
    assert lines[-1].startswith("rule history: 8 BTC 4h bars decided, closing ")
    assert lines[-1].endswith("UTC (long 38%, flat 38%, short 25%)")
