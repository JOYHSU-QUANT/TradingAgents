"""The trend guardrail's handoff document: its shape, and every way of refusing it.

Mirrors ``test_research_signal``: the contract (``GuardrailSignal``) is tested
as a value, and the reader (``load_guardrail_signal``) for answering ``None``
with exactly one named WARNING on every untrustworthy document (and a
document saying ``unknown`` with that side and the same one WARNING) — this one
reaches a gate, so a refusal must never flatten the book and never let a new
position through (the engine turns ``None`` into ``UNKNOWN``).
"""

from __future__ import annotations

import json
import logging

import pytest

from contrib.hyperliquid_perp.domains.perp.guardrail_signal import load_guardrail_signal
from contrib.hyperliquid_perp.domains.perp.research_signal import MAX_SIGNAL_AGE_INTERVALS
from contrib.hyperliquid_perp.domains.perp.schema import (
    GUARDRAIL_INTERVAL,
    GUARDRAIL_SIGNAL_DOCUMENT_VERSION,
    GuardrailSide,
    GuardrailSignal,
)

_MS_PER_4H = 4 * 60 * 60_000
_AS_OF_MS = 1_704_182_399_999  # a 4h close, one ms short of the next open


def _signal(**overrides) -> GuardrailSignal:
    base = {
        "coin": "BTC",
        "interval": "4h",
        "as_of_ms": _AS_OF_MS,
        "rule_id": "btc-20d-breakout@ca50e67a",
        "side": "long",
    }
    return GuardrailSignal(**{**base, **overrides})


def _document(**overrides) -> dict:
    return {**_signal().to_document(), **overrides}


def _write(tmp_path, payload, name="guardrail.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload, "utf-8")
    return str(path)


def _load(path, caplog, *, coin="BTC", now_ms=_AS_OF_MS + 1):
    with caplog.at_level(logging.WARNING):
        return load_guardrail_signal(path, coin=coin, now_ms=now_ms)


def _the_one_warning(caplog) -> str:
    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1, [r.getMessage() for r in records]
    return records[0].getMessage()


# -- the contract -----------------------------------------------------------------


def test_a_document_round_trips_to_an_equal_signal():
    signal = _signal()
    assert GuardrailSignal.from_document(signal.to_document()) == signal
    assert signal.to_document()["document_version"] == GUARDRAIL_SIGNAL_DOCUMENT_VERSION


@pytest.mark.parametrize("side", ["long", "short", "flat", "unknown"])
def test_every_side_is_a_member_and_is_coerced(side):
    assert _signal(side=side).side is GuardrailSide(side)


def test_the_coin_is_normalised_the_way_the_research_signal_normalises_its_own():
    assert _signal(coin=" btc ").coin == "BTC"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("side", "neutral", "guardrail side"),
        ("coin", "  ", "GuardrailSignal.coin must be a non-empty string"),
        ("interval", "4H", "interval"),
        ("as_of_ms", "2024-01-02", "GuardrailSignal.as_of_ms"),
        ("rule_id", " ", "GuardrailSignal.rule_id must not be blank"),
        ("rule_id", "a b", "single token without whitespace"),
    ],
)
def test_every_field_is_refused_by_name(field, value, message):
    with pytest.raises(ValueError, match=message):
        _signal(**{field: value})


@pytest.mark.parametrize("version", [0, 2, True, "1", None])
def test_a_payload_that_is_not_this_version_of_the_document_is_refused(version):
    with pytest.raises(ValueError, match="this build reads guardrail signal document version 1"):
        GuardrailSignal.from_document(_document(document_version=version))


def test_an_unknown_or_missing_key_is_refused_rather_than_ignored():
    with pytest.raises(ValueError, match=r"unknown \['notes'\]"):
        GuardrailSignal.from_document(_document(notes="x"))
    missing = _document()
    del missing["side"]
    with pytest.raises(ValueError, match=r"missing \['side'\]"):
        GuardrailSignal.from_document(missing)
    with pytest.raises(ValueError, match="is a JSON object, got list"):
        GuardrailSignal.from_document([])


# -- the reader ---------------------------------------------------------------------


def test_a_fresh_document_is_read(tmp_path, caplog):
    path = _write(tmp_path, _document())
    assert _load(path, caplog) == _signal()
    assert not caplog.records


def test_a_missing_file_costs_one_warning_and_names_what_it_costs(tmp_path, caplog):
    assert _load(str(tmp_path / "nowhere.json"), caplog) is None
    message = _the_one_warning(caplog)
    assert message.startswith("trend guardrail document ")
    assert "could not be read, so the gate opens nothing new and keeps what is held" in message


def test_a_half_written_document_is_refused(tmp_path, caplog):
    path = _write(tmp_path, '{"document_version": 1, "coin": "BT')
    assert _load(path, caplog) is None
    assert "could not be decoded as JSON" in _the_one_warning(caplog)


def test_a_document_the_contract_refuses_is_logged_and_dropped(tmp_path, caplog):
    path = _write(tmp_path, _document(side="neutral"))
    assert _load(path, caplog) is None
    message = _the_one_warning(caplog)
    assert message.startswith("trend guardrail document at ")
    assert "was refused" in message and "guardrail side" in message


def test_another_coins_document_is_not_read_as_this_ones(tmp_path, caplog):
    path = _write(tmp_path, _document(coin="ETH"))
    assert _load(path, caplog) is None
    assert "is for 'ETH' and this run trades 'BTC'" in _the_one_warning(caplog)


def test_the_stale_bound_is_two_of_the_documents_own_bars(tmp_path, caplog):
    path = _write(tmp_path, _document())
    bound = MAX_SIGNAL_AGE_INTERVALS * _MS_PER_4H
    assert _load(path, caplog, now_ms=_AS_OF_MS + bound) == _signal()
    assert not caplog.records
    assert _load(path, caplog, now_ms=_AS_OF_MS + bound + 1) is None
    message = _the_one_warning(caplog)
    assert "past the 2 x 4h bound" in message
    assert "run the research package's `fetch` and then its `guardrail-signal` command" in message


def test_a_document_saying_unknown_is_read_and_said_out_loud(tmp_path, caplog):
    path = _write(tmp_path, _document(side="unknown"))
    assert _load(path, caplog) == _signal(side="unknown")
    message = _the_one_warning(caplog)
    assert "says the rule btc-20d-breakout@ca50e67a has no side at its newest bar" in message
    assert message.endswith("so the gate opens nothing new and keeps what is held")


def test_a_document_from_ahead_of_this_clock_is_refused(tmp_path, caplog):
    path = _write(tmp_path, _document())
    assert _load(path, caplog, now_ms=_AS_OF_MS - 1) is None
    assert "AFTER this gate's own clock" in _the_one_warning(caplog)


def test_the_interval_is_the_rules_and_no_other_is_read(tmp_path, caplog):
    # A supported interval that is not the rule's would stretch the freshness
    # bound (2 x 1d = 48h) on the one document that can close a position.
    assert GUARDRAIL_INTERVAL == "4h"
    with pytest.raises(ValueError, match="interval must be '4h'"):
        _signal(interval="1d")
    path = _write(tmp_path, _document(interval="1d"))
    assert _load(path, caplog) is None
    assert "interval must be '4h'" in _the_one_warning(caplog)
