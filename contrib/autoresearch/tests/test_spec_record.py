"""A spec as the ledger keeps it: a document that reads back to the same spec, and the hash of its rule.

Two different jobs, and the tests keep them apart. The DOCUMENT must round
trip exactly — a trial's spec is read back from it for every report and for
the promote re-measurement, so a document that parsed to a slightly different
spec would promote a rule that was never measured. The HASH must identify the
RULE — blind to what does not change the trades (plan §10.7), and sensitive
to everything that does, or the ledger would either count one rule as many
trials or file two rules as one.
"""

from __future__ import annotations

import pytest

from contrib.autoresearch.dsl import parse_spec, spec_hash, spec_to_document

_SIZING = {"mode": "fixed_margin_fraction", "fraction": 0.25}

DOCUMENTS = {
    "the readme example": {
        "family": "breakout",
        "entry": {"long": [{"left": "close", "op": ">", "right": "donchian_high_20"}]},
        "exit": {
            "long": [{"left": "rsi_14", "op": "<", "right": {"param": "cool_off"}}],
            "max_bars": 30,
        },
        "filters": [{"left": "regime", "op": "==", "right": "trending"}],
        "sizing": _SIZING,
        "params": {"cool_off": 45},
    },
    "both sides, with offsets": {
        "family": "mean_reversion",
        "entry": {
            "long": [{"left": "close", "op": "<", "right": {"feature": "close", "offset": 3}}],
            "short": [{"left": "rsi_14", "op": ">", "right": 70}],
        },
        "exit": {"short": [{"left": "rsi_14", "op": "<", "right": 50}]},
        "sizing": _SIZING,
    },
    "a vol target on the default cap": {
        "family": "vol_targeting",
        "entry": {"long": [{"left": "funding_zscore_30", "op": "<", "right": -2}]},
        "sizing": {"mode": "vol_target", "target_vol": 0.02, "lookback": 20},
    },
    "a regime read with an offset": {
        "family": "regime_filter",
        "entry": {
            "short": [
                {"left": {"feature": "regime", "offset": 1}, "op": "!=", "right": "ranging"}
            ]
        },
        "sizing": _SIZING,
    },
}


@pytest.mark.parametrize("name", DOCUMENTS)
def test_a_spec_reads_back_from_its_document_as_the_same_spec(name):
    spec = parse_spec(DOCUMENTS[name])
    assert parse_spec(spec_to_document(spec)) == spec


def test_a_default_the_parser_filled_in_is_written_out():
    """So a spec read back after the default moves is still the spec that was measured."""
    document = spec_to_document(parse_spec(DOCUMENTS["a vol target on the default cap"]))
    assert document["sizing"] == {
        "mode": "vol_target",
        "target_vol": 0.02,
        "lookback": 20,
        "max_fraction": 0.6,
    }


def _rule(**overrides) -> dict:
    body = {
        "family": "breakout",
        "entry": {
            "long": [
                {"left": "close", "op": ">", "right": "sma_20"},
                {"left": "rsi_14", "op": "<", "right": 70},
            ]
        },
        "sizing": _SIZING,
    }
    body.update(overrides)
    return body


def _hash(document: dict) -> str:
    return spec_hash(parse_spec(document))


def test_the_hash_is_a_sha256_of_the_rule():
    digest = _hash(_rule())
    assert len(digest) == 64 and int(digest, 16) >= 0


def test_the_hash_does_not_see_clause_order_or_a_clause_written_twice():
    reordered = _rule(
        entry={
            "long": [
                {"left": "rsi_14", "op": "<", "right": 70},
                {"left": "close", "op": ">", "right": "sma_20"},
                {"left": "rsi_14", "op": "<", "right": 70},
            ]
        }
    )
    assert _hash(reordered) == _hash(_rule())


def test_the_hash_does_not_see_a_parameter_s_name():
    named = _rule(
        entry={
            "long": [
                {"left": "close", "op": ">", "right": "sma_20"},
                {"left": "rsi_14", "op": "<", "right": {"param": "overbought"}},
            ]
        },
        params={"overbought": 70},
    )
    assert _hash(named) == _hash(_rule())


def test_the_hash_does_not_see_the_family_label():
    """Relabelling is free (plan §10.6), so it must not buy a second trial."""
    assert _hash(_rule(family="mean_reversion")) == _hash(_rule())


def test_the_hash_reads_a_feature_comparison_from_either_side():
    mirrored = _rule(
        entry={
            "long": [
                {"left": "sma_20", "op": "<", "right": "close"},
                {"left": "rsi_14", "op": "<", "right": 70},
            ]
        }
    )
    assert _hash(mirrored) == _hash(_rule())
    lagged = {"feature": "close", "offset": 1}
    forwards = _rule(entry={"long": [{"left": "close", "op": ">=", "right": lagged}]})
    backwards = _rule(entry={"long": [{"left": lagged, "op": "<=", "right": "close"}]})
    assert _hash(forwards) == _hash(backwards)


def test_the_hash_does_not_see_how_a_number_was_spelled():
    spelled = _rule(
        entry={
            "long": [
                {"left": "close", "op": ">", "right": "sma_20"},
                {"left": "rsi_14", "op": "<", "right": 70.0},
            ]
        }
    )
    assert _hash(spelled) == _hash(_rule())

    def ret_above(value):
        return _rule(entry={"long": [{"left": "ret_1", "op": ">", "right": value}]})

    assert _hash(ret_above(-0.0)) == _hash(ret_above(0))


def _long(*clauses) -> dict:
    return {"entry": {"long": list(clauses)}}


_SMA = {"left": "close", "op": ">", "right": "sma_20"}
_RSI = {"left": "rsi_14", "op": "<", "right": 70}


@pytest.mark.parametrize(
    "overrides",
    [
        _long(_SMA, {**_RSI, "right": 71}),
        _long({**_SMA, "op": ">="}, _RSI),
        _long({**_SMA, "right": {"feature": "sma_20", "offset": 1}}, _RSI),
        _long({**_SMA, "op": "<"}, _RSI),
        {"entry": {"short": [_SMA, _RSI]}},
        {"exit": {"max_bars": 5}},
        {"sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5}},
        {**_long(_SMA), "filters": [_RSI]},
    ],
    ids=[
        "threshold",
        "operator",
        "offset",
        "direction without swapping sides",
        "side",
        "max_bars",
        "sizing",
        "a clause moved to filters",
    ],
)
def test_the_hash_sees_everything_that_changes_what_is_traded(overrides):
    assert _hash(_rule(**overrides)) != _hash(_rule())
