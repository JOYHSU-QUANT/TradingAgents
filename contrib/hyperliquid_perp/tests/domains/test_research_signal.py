"""The research radar's handoff document: its shape, and every way of refusing it.

Two halves. ``ResearchSignal`` is the CONTRACT — the field guards and the
encoding both packages share — and it is tested as a value, because a signal
built by hand in a fixture has to carry the same guarantees as one read off
disk. ``load_research_signal`` is the READER, and what is tested there is that
each way of not having a trustworthy document answers ``None`` with exactly
one named WARNING: never a half-filled section, and never an exception loose
in the cycle.

The counterpart lives in ``contrib/autoresearch/tests/test_signal.py``: a
document that package actually writes, read back by the function under test
here. Neither side restates the other's rules.
"""

from __future__ import annotations

import json
import logging

import pytest

from contrib.hyperliquid_perp.domains.perp.research_signal import (
    MAX_SIGNAL_AGE_INTERVALS,
    load_research_signal,
)
from contrib.hyperliquid_perp.domains.perp.schema import (
    MAX_RESEARCH_TEXT_CHARS,
    RESEARCH_SIGNAL_DOCUMENT_VERSION,
    ResearchBias,
    ResearchConfidence,
    ResearchDrawdown,
    ResearchSignal,
)

_MS_PER_4H = 4 * 60 * 60_000
_MS_PER_DAY = 24 * 60 * 60_000
# A 4h bar's close, a millisecond short of the next open, the way the venue
# stamps one.
_AS_OF_MS = 1_704_182_399_999


def _signal(**overrides) -> ResearchSignal:
    base = {
        "coin": "BTC",
        "interval": "4h",
        "as_of_ms": _AS_OF_MS,
        "strategy_id": "btc-4h#7",
        "bias": "long",
        "confidence": "medium",
        "drawdown": "moderate",
        "eval_window_days": 90,
        "holdout_window_days": 30,
        "notes": "held-back window, measured once: net return positive",
    }
    base.update(overrides)
    return ResearchSignal(**base)


def _document(**overrides) -> dict:
    document = _signal().to_document()
    document.update(overrides)
    return document


def _write(tmp_path, payload, name="signal.json") -> str:
    target = tmp_path / name
    target.write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return str(target)


def _load(path, caplog, **overrides):
    """Read ``path`` at the fixture's own bar, capturing what was logged."""
    arguments = {"coin": "BTC", "as_of_ms": _AS_OF_MS, "candle_interval_ms": _MS_PER_4H}
    arguments.update(overrides)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        return load_research_signal(path, **arguments)


# -- the contract ----------------------------------------------------------


def test_a_document_round_trips_to_an_equal_signal():
    signal = _signal()
    assert ResearchSignal.from_document(signal.to_document()) == signal
    assert signal.to_document()["document_version"] == RESEARCH_SIGNAL_DOCUMENT_VERSION


def test_the_vocabularies_are_coerced_to_their_members_not_left_as_text():
    signal = _signal()
    assert signal.bias is ResearchBias.LONG
    assert signal.confidence is ResearchConfidence.MEDIUM
    assert signal.drawdown is ResearchDrawdown.MODERATE


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bias", "flat", "unsupported research signal bias"),
        ("confidence", "high", "unsupported research signal confidence band"),
        ("drawdown", "deepish", "unsupported research signal drawdown band"),
        ("interval", "4H", "unsupported candle interval"),
        ("coin", "  ", "coin must be a non-empty string"),
        ("as_of_ms", 0, "must be > 0"),
        ("as_of_ms", "1704182399999", "must be an int of UTC epoch ms"),
        ("eval_window_days", 0, "must be >= 1 day"),
        ("holdout_window_days", 1.5, "must be an int of days"),
        ("strategy_id", "", "must not be blank"),
        ("notes", "  ", "must not be blank"),
        ("strategy_id", "a\nb", "must be a single line"),
        ("notes", "a\rb", "must be a single line"),
    ],
)
def test_every_field_is_refused_by_name(field, value, message):
    with pytest.raises(ValueError, match=message):
        _signal(**{field: value})


def test_a_rendered_field_is_bounded_rather_than_truncated():
    # Truncating would put a cut-off sentence in the prompt reading as a whole
    # one; the bound refuses instead, and the section is simply absent.
    assert _signal(notes="x" * MAX_RESEARCH_TEXT_CHARS).notes
    with pytest.raises(ValueError, match=f"at most {MAX_RESEARCH_TEXT_CHARS} characters"):
        _signal(notes="x" * (MAX_RESEARCH_TEXT_CHARS + 1))


def test_surrounding_whitespace_is_stripped_but_the_sentence_is_not_touched():
    assert _signal(notes="  two  words  ").notes == "two  words"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "is a JSON object"),
        ("text", "is a JSON object"),
        ({"document_version": 99}, "the document declares 99"),
    ],
)
def test_a_payload_that_is_not_this_version_of_the_document_is_refused(payload, message):
    with pytest.raises(ValueError, match=message):
        ResearchSignal.from_document(payload)


def test_an_unknown_or_missing_key_is_refused_rather_than_ignored():
    # Strict for the same reason the config loader is strict about YAML keys:
    # a producer that added a field without bumping the version is not the
    # version it claims to be.
    with pytest.raises(ValueError, match=r"unknown \['extra'\]"):
        ResearchSignal.from_document(_document(extra=1))
    short = _document()
    del short["notes"]
    with pytest.raises(ValueError, match=r"missing \['notes'\]"):
        ResearchSignal.from_document(short)


# -- the reader ------------------------------------------------------------


def test_a_document_at_this_runs_own_bar_is_read(tmp_path, caplog):
    assert _load(_write(tmp_path, _document()), caplog) == _signal()
    assert caplog.records == []


def test_a_missing_file_omits_the_section_with_one_warning(tmp_path, caplog):
    # The ordinary case on the day the switch is turned on and the producer
    # has not run yet: a legal config, no document, one WARNING naming where
    # it looked, and a prompt without the section.
    assert _load(str(tmp_path / "absent.json"), caplog) is None
    assert len(caplog.records) == 1
    assert "could not be read" in caplog.text
    assert "absent.json" in caplog.text


def test_a_directory_where_the_document_should_be_is_the_same_refusal(tmp_path, caplog):
    (tmp_path / "signal.json").mkdir()
    assert _load(str(tmp_path / "signal.json"), caplog) is None
    assert len(caplog.records) == 1
    assert "could not be read" in caplog.text


def test_a_file_that_is_not_utf8_is_refused_rather_than_raised(tmp_path, caplog):
    # ``read_text`` raises this one from the decoder, and ``UnicodeDecodeError``
    # is not an ``OSError`` — unhandled it would fail the cycle instead of the
    # section.
    target = tmp_path / "signal.json"
    target.write_bytes(b"\xff\xfe\x00nonsense")
    assert _load(str(target), caplog) is None
    assert len(caplog.records) == 1
    assert "not UTF-8" in caplog.text


def test_a_half_written_document_is_refused(tmp_path, caplog):
    assert _load(_write(tmp_path, '{"document_version": 1, "coin":'), caplog) is None
    assert len(caplog.records) == 1
    assert "not valid JSON" in caplog.text


def test_a_document_the_contract_refuses_is_logged_and_dropped(tmp_path, caplog):
    assert _load(_write(tmp_path, _document(bias="sideways")), caplog) is None
    assert len(caplog.records) == 1
    assert "unsupported research signal bias" in caplog.text


def test_another_coins_document_is_not_read_as_this_ones(tmp_path, caplog):
    # One host can run the radar over several coins; pointing a BTC daemon at
    # the ETH document must not print ETH's rule under a BTC prompt.
    assert _load(_write(tmp_path, _document(coin="ETH")), caplog) is None
    assert len(caplog.records) == 1
    assert "is for ETH and this run trades BTC" in caplog.text


def test_the_stale_bound_is_the_documents_own_interval(tmp_path, caplog):
    path = _write(tmp_path, _document())
    bound = MAX_SIGNAL_AGE_INTERVALS * _MS_PER_4H
    assert _load(path, caplog, as_of_ms=_AS_OF_MS + bound) is not None
    assert caplog.records == []
    assert _load(path, caplog, as_of_ms=_AS_OF_MS + bound + 1) is None
    assert len(caplog.records) == 1
    assert f"past the {MAX_SIGNAL_AGE_INTERVALS} x 4h bound" in caplog.text


def test_a_daily_document_gets_a_daily_bound(tmp_path, caplog):
    # The stale bound follows the DOCUMENT's cadence, not this run's: "too
    # old" is a statement about how often the producer runs.
    path = _write(tmp_path, _document(interval="1d"))
    bound = MAX_SIGNAL_AGE_INTERVALS * _MS_PER_DAY
    assert _load(path, caplog, as_of_ms=_AS_OF_MS + bound) is not None
    assert _load(path, caplog, as_of_ms=_AS_OF_MS + bound + 1) is None
    assert f"past the {MAX_SIGNAL_AGE_INTERVALS} x 1d bound" in caplog.text


def test_a_document_from_ahead_of_this_runs_own_bar_is_refused(tmp_path, caplog):
    # No closed bar can be newer than "now", and "now" is less than one of
    # THIS run's intervals past its own newest close — otherwise another of
    # its candles would have closed and that one would be the newest. So the
    # future bound is this run's interval, whatever the document's cadence is.
    path = _write(tmp_path, _document())
    assert _load(path, caplog, as_of_ms=_AS_OF_MS - _MS_PER_4H + 1) is not None
    assert caplog.records == []
    assert _load(path, caplog, as_of_ms=_AS_OF_MS - _MS_PER_4H) is None
    assert len(caplog.records) == 1
    assert "AFTER this context's own bar" in caplog.text


def test_a_tilde_in_the_path_is_expanded_and_the_warning_names_where_it_looked(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    _write(tmp_path, _document())
    assert _load("~/signal.json", caplog) == _signal()
    assert _load("~/absent.json", caplog) is None
    assert str(tmp_path) in caplog.text
    assert "~" not in caplog.text
