"""Tests for the decision audit log (pure record build + filesystem write)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.audit.decision_log import (
    _VALID_RATING_SOURCES,
    _filename,
    build_log_record,
    log_decision,
    prompt_hash,
    write_decision_log,
)
from contrib.hyperliquid_perp.domains.perp.decision import (
    EntryZone,
    FundingView,
    Intent,
    MarketRegime,
    PerpTradeDecision,
    Urgency,
)

_TS = datetime(2026, 6, 27, 17, 45, 0, tzinfo=timezone.utc)
_MODELS = {
    "provider": "openrouter",
    "deep": "anthropic/claude-sonnet-4-6",
    "quick": "deepseek/deepseek-chat",
}


def _decision() -> PerpTradeDecision:
    return PerpTradeDecision(
        intent=Intent.OPEN_LONG,
        confidence=0.8,
        target_size_pct=20.0,
        entry_zone=EntryZone(low=Decimal("63000.0"), high=Decimal("63400.0")),
        invalidation_price=Decimal("61800.0"),
        urgency=Urgency.LOW,
        rationale="Breakout confirmed.",
        key_risks=("Resistance overhead",),
        market_regime=MarketRegime.TRENDING,
        funding_view=FundingView.NEUTRAL,
    )


def test_prompt_hash_is_deterministic_and_prefixed():
    h1 = prompt_hash("context A")
    h2 = prompt_hash("context A")
    h3 = prompt_hash("context B")
    assert h1 == h2
    assert h1.startswith("sha256:")
    assert h1 != h3


def test_build_log_record_shape():
    record = build_log_record(
        coin="BTC",
        decision=_decision(),
        prompt="rendered perp context",
        models=_MODELS,
        rating="Buy",
        timestamp=_TS,
        rating_source="default",
    )
    assert record["schema_version"] == 2
    assert record["coin"] == "BTC"
    assert record["timestamp"] == "2026-06-27T17:45:00+00:00"
    assert record["timestamp_ms"] == int(_TS.timestamp() * 1000)
    assert record["prompt_hash"] == prompt_hash("rendered perp context")
    assert record["models"] == _MODELS
    assert record["rating"] == "Buy"
    assert record["rating_source"] == "default"
    assert record["decision"]["intent"] == "open_long"


def test_build_log_record_rejects_naive_timestamp():
    # A naive datetime would make timestamp_ms silently off by the host's UTC offset
    # while the ISO string still looks valid — reject it rather than corrupt the audit.
    with pytest.raises(ValueError, match="timezone-aware"):
        build_log_record(
            coin="BTC",
            decision=_decision(),
            prompt="ctx",
            models=_MODELS,
            rating="Buy",
            timestamp=datetime(2026, 6, 27, 17, 45, 0),  # naive, no tzinfo
        )


@pytest.mark.parametrize("bad_source", ["EXPLICIT", "fallback", "", "Hold"])
def test_build_log_record_rejects_unknown_rating_source(bad_source):
    # rating_source is the one record field not derived from a validated domain object;
    # a wrong-case/unknown tag would write a silently corrupt audit field — reject it.
    with pytest.raises(ValueError, match="unknown rating_source"):
        build_log_record(
            coin="BTC",
            decision=_decision(),
            prompt="ctx",
            models=_MODELS,
            rating="Buy",
            timestamp=_TS,
            rating_source=bad_source,
        )


def test_valid_rating_sources_stays_in_sync_with_enum():
    # ``_VALID_RATING_SOURCES`` is a hardcoded copy of ``RatingSource``'s values, kept
    # in the audit layer so it takes no *runtime* dependency on the integration layer
    # (RatingSource is only TYPE_CHECKING-imported there). This test is the safety net:
    # if a future RatingSource member is added without updating the set, build_log_record
    # would raise on that valid value at runtime — catch the drift here in CI instead.
    from contrib.hyperliquid_perp.integration.decision_adapter import RatingSource

    assert {member.value for member in RatingSource} == _VALID_RATING_SOURCES


def test_build_log_record_accepts_rating_source_enum_and_serializes_value():
    # A RatingSource enum is accepted and flattened to its string value in the record.
    from contrib.hyperliquid_perp.integration.decision_adapter import RatingSource

    record = build_log_record(
        coin="BTC",
        decision=_decision(),
        prompt="ctx",
        models=_MODELS,
        rating="Buy",
        timestamp=_TS,
        rating_source=RatingSource.PARSE_FALLBACK,
    )
    assert record["rating_source"] == "parse_fallback"
    json.dumps(record)  # no enum leak that would break json.dump


def test_write_decision_log_roundtrip(tmp_path):
    record = build_log_record(
        coin="BTC",
        decision=_decision(),
        prompt="ctx",
        models=_MODELS,
        rating="Buy",
        timestamp=_TS,
    )
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
    record = build_log_record(
        coin="BTC", decision=_decision(), prompt="ctx", models=_MODELS, rating="Buy", timestamp=_TS
    )
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
    record = build_log_record(
        coin="BTC", decision=_decision(), prompt="ctx", models=_MODELS, rating="Buy", timestamp=_TS
    )
    record["timestamp"] = "2026-06-27T17:45:00Z"
    path = write_decision_log(record, tmp_path)
    assert path.name == "BTC_20260627_174500_000.json"


def test_write_decision_log_falls_back_on_unparseable_timestamp(tmp_path, caplog):
    # An unparseable timestamp must not crash the audit write: it falls back to now()
    # for the filename stamp and warns, while the record content is written verbatim.
    record = build_log_record(
        coin="BTC", decision=_decision(), prompt="ctx", models=_MODELS, rating="Buy", timestamp=_TS
    )
    record["timestamp"] = "not-a-timestamp"
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
    record = build_log_record(
        coin="BTC", decision=_decision(), prompt="ctx", models=_MODELS, rating="Buy", timestamp=_TS
    )
    record["unserializable"] = object()
    with pytest.raises(TypeError):
        write_decision_log(record, tmp_path)
    directory = tmp_path / "perp_decisions"
    assert not list(directory.glob("*.tmp"))  # temp file cleaned up
    assert not list(directory.glob("*.json"))  # no partial record left in place


def test_write_decision_log_cleanup_unlink_failure_preserves_original_error(tmp_path, monkeypatch):
    # Double fault: the write fails AND cleanup's unlink itself fails (e.g. a Windows
    # file lock on the temp file). The secondary OSError must not replace the original
    # write error — the caller needs to see *why the write failed*, not why cleanup did.
    record = build_log_record(
        coin="BTC", decision=_decision(), prompt="ctx", models=_MODELS, rating="Buy", timestamp=_TS
    )
    record["unserializable"] = object()

    def _locked_unlink(self, *args, **kwargs):
        raise OSError("temp file is locked")

    monkeypatch.setattr("pathlib.Path.unlink", _locked_unlink)
    with pytest.raises(TypeError):  # the original serialization error, not OSError
        write_decision_log(record, tmp_path)


def test_log_decision_builds_and_writes(tmp_path):
    record, path = log_decision(
        coin="BTC",
        decision=_decision(),
        prompt="ctx",
        models=_MODELS,
        rating="Buy",
        results_dir=tmp_path,
        timestamp=_TS,
    )
    assert path.exists()
    assert record["decision"]["target_size_pct"] == 20.0
