"""Tests for the decision audit log (pure record build + filesystem write)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.audit.decision_log import (
    _filename,
    build_target_log_record,
    log_target_decision,
    prompt_hash,
    write_decision_log,
)

_TS = datetime(2026, 6, 27, 17, 45, 0, tzinfo=timezone.utc)
_MODELS = {
    "provider": "openrouter",
    "deep": "anthropic/claude-sonnet-4-6",
    "quick": "deepseek/deepseek-chat",
}


def _record(**overrides):
    """A minimal record for write-path tests.

    ``write_decision_log`` treats the record as an opaque dict — only ``coin``
    and ``timestamp`` feed the filename — so these tests don't need the full
    builder output.
    """
    record = {
        "schema_version": 3,
        "coin": "BTC",
        "timestamp": _TS.isoformat(),
        "timestamp_ms": int(_TS.timestamp() * 1000),
        "prompt_hash": prompt_hash("ctx"),
        "models": _MODELS,
    }
    record.update(overrides)
    return record


def test_prompt_hash_is_deterministic_and_prefixed():
    h1 = prompt_hash("context A")
    h2 = prompt_hash("context A")
    h3 = prompt_hash("context B")
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert h1 != h3


def test_write_decision_log_roundtrip(tmp_path):
    record = _record()
    path = write_decision_log(record, tmp_path)

    assert path.parent.name == "perp_decisions"
    assert path.name == "BTC_20260627_174500_000.json"
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded == record


def test_write_decision_log_same_timestamp_does_not_overwrite(tmp_path, caplog):
    # Two decisions for the same coin at the same (millisecond) timestamp must not
    # collide-and-overwrite: the second write lands on a counter-suffixed name so the
    # first audit record survives. A silent overwrite would destroy an audit record.
    record = _record()
    first = write_decision_log(record, tmp_path, timestamp=_TS)
    with caplog.at_level("WARNING"):
        second = write_decision_log(record, tmp_path, timestamp=_TS)

    assert first.name == "BTC_20260627_174500_000.json"
    assert second.name == "BTC_20260627_174500_000_1.json"
    assert first != second
    assert first.exists() and second.exists()  # the first record was not destroyed
    assert "already exists" in caplog.text


def test_filename_sanitizes_coin_and_falls_back_to_unknown():
    # A coin with path separators must not produce a nested/invalid path, and an
    # empty/all-symbol coin falls back to UNKNOWN rather than an empty name.
    assert _filename("BTC/USDT", _TS) == "BTCUSDT_20260627_174500_000.json"
    assert _filename("", _TS) == "UNKNOWN_20260627_174500_000.json"
    assert _filename("---", _TS) == "UNKNOWN_20260627_174500_000.json"


def test_write_decision_log_handles_z_suffix_timestamp(tmp_path):
    # A record whose timestamp carries a trailing 'Z' (built outside this module)
    # must still name the file correctly — fromisoformat rejected 'Z' before 3.11.
    record = _record(timestamp="2026-06-27T17:45:00Z")
    path = write_decision_log(record, tmp_path)
    assert path.name == "BTC_20260627_174500_000.json"


def test_write_decision_log_falls_back_on_unparseable_timestamp(tmp_path, caplog):
    # An unparseable timestamp must not crash the audit write: it falls back to now()
    # for the filename stamp and warns, while the record content is written verbatim.
    record = _record(timestamp="not-a-timestamp")
    with caplog.at_level("WARNING"):
        path = write_decision_log(record, tmp_path)
    assert path.exists()
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["timestamp"] == "not-a-timestamp"  # record content untouched
    assert any("could not parse" in r.message for r in caplog.records)


def test_write_decision_log_cleans_tmp_on_serialization_failure(tmp_path):
    # A non-serializable value mid-write must propagate and leave no partial .tmp
    # file behind, so the audit directory never holds a truncated/corrupt record.
    record = _record(unserializable=object())
    with pytest.raises(TypeError):
        write_decision_log(record, tmp_path)
    directory = tmp_path / "perp_decisions"
    assert not list(directory.glob("*.tmp"))  # temp file cleaned up
    assert not list(directory.glob("*.json"))  # no partial record left in place


def test_write_decision_log_cleanup_unlink_failure_preserves_original_error(tmp_path, monkeypatch):
    # Double fault: the write fails AND cleanup's unlink itself fails (e.g. a Windows
    # file lock on the temp file). The secondary OSError must not replace the original
    # write error — the caller needs to see *why the write failed*, not why cleanup did.
    record = _record(unserializable=object())

    def _locked_unlink(self, *args, **kwargs):
        raise OSError("temp file is locked")

    monkeypatch.setattr("pathlib.Path.unlink", _locked_unlink)
    with pytest.raises(TypeError):  # the original serialization error, not OSError
        write_decision_log(record, tmp_path)


# --------------------------------------------------------------------------
# Phase 2 — structured target records
# --------------------------------------------------------------------------


def _target_fixtures():
    from contrib.hyperliquid_perp.domains.perp.risk_gate import (
        CurrentPositionState,
        RiskConfig,
        evaluate,
    )
    from contrib.hyperliquid_perp.domains.perp.target_decision import (
        DecisionConfig,
        parse_target_decision,
    )

    raw = (
        'Final answer:\n```json\n{"decision_mode": "set_target", "target_side": "long", '
        '"requested_target_margin_pct": 61, "confidence": 0.7, '
        '"rationale": "Trend up.", "key_risks": ["Funding"]}\n```'
    )
    parsed = parse_target_decision(raw, DecisionConfig())
    result = evaluate(
        parsed,
        account_equity=Decimal("1000"),
        current=CurrentPositionState.flat(),
        risk=RiskConfig(),
        decision_cfg=DecisionConfig(),
    )
    return raw, parsed, result


def test_log_target_decision_roundtrip_preserves_raw_response(tmp_path):
    raw, parsed, result = _target_fixtures()
    record, path = log_target_decision(
        coin="BTC",
        parsed=parsed,
        risk_result=result,
        prompt="ctx",
        models=_MODELS,
        results_dir=tmp_path,
        timestamp=_TS,
    )
    with path.open(encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded == record
    assert loaded["schema_version"] == 3
    assert loaded["raw_response"] == raw  # verbatim, never reconstructed
    assert loaded["parse"] == {"is_valid": True, "invalid_reason": None}
    assert loaded["decision"]["requested_target_margin_pct"] == 61
    assert loaded["risk"]["risk_action"] == "clamped"
    assert loaded["risk"]["approved_target_margin_pct"] == 60
    assert loaded["prompt_hash"] == prompt_hash("ctx")


def test_build_target_log_record_rejects_naive_timestamp():
    _raw, parsed, result = _target_fixtures()
    with pytest.raises(ValueError, match="timezone-aware"):
        build_target_log_record(
            coin="BTC",
            parsed=parsed,
            risk_result=result,
            prompt="ctx",
            models=_MODELS,
            timestamp=datetime(2026, 6, 27, 17, 45, 0),  # naive, no tzinfo
        )
