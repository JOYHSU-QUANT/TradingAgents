"""Recording a verdict: the sidecar's path and content, its atomic write, and the stored row."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from contrib.uniswap_v3.agent.graph import Answer
from contrib.uniswap_v3.agent.record import (
    sidecar_path,
    sidecar_record,
    verdict_record,
    write_sidecar,
)
from contrib.uniswap_v3.domain.verdicts import Rating, Verdict, text_digest

# 2024-01-03 00:00:00 UTC.
_AT = 1_704_240_000
_ASKED_AT = _AT + 600
_DIGEST = "ab" * 32
_DECISION = "**Rating**: Buy\n\nBecause."


def _answer(rating: Rating = Rating.BUY, reports=None) -> Answer:
    return Answer(decision=_DECISION, rating=rating, reports=reports, elapsed_seconds=12.3456)


def _record(answer: Answer, **changes):
    arguments = {
        "source": "judge-v1",
        "symbol": "WETH",
        "ticker": "ETH-USD",
        "time": _AT,
        "trade_date": "2024-01-03",
        "context": "the spot context",
        "model": "vendor/model",
        "settings": {"llm_provider": "vendor", "max_tokens": 8192},
        "asked_at": _ASKED_AT,
    }
    return sidecar_record(answer, **{**arguments, **changes})


def test_the_sidecar_path_names_the_source_the_token_and_the_bar():
    assert sidecar_path("judge-v1", "WETH", _AT) == "verdicts/judge-v1/WETH-20240103T000000Z.json"


def test_the_sidecar_record_keeps_what_was_asked_and_everything_that_came_back():
    record = _record(_answer(reports={"market_report": "a report", "news_report": None}))
    assert record == {
        "schema": 1,
        "source": "judge-v1",
        "symbol": "WETH",
        "ticker": "ETH-USD",
        "time": _AT,
        "trade_date": "2024-01-03",
        "model": "vendor/model",
        "judge": {"llm_provider": "vendor", "max_tokens": 8192},
        "prompt_version": "spot-context-v3",
        "asked_at": _ASKED_AT,
        "elapsed_seconds": 12.346,
        "rating": "Buy",
        "spot_context": "the spot context",
        "decision": _DECISION,
        "decision_digest": text_digest(_DECISION),
        "reports": {"market_report": "a report", "news_report": None},
    }
    assert _record(_answer())["reports"] is None


def test_write_sidecar_writes_the_json_whole_and_returns_the_digest_of_the_bytes(tmp_path):
    digest = write_sidecar(tmp_path, "verdicts/j/WETH-x.json", {"b": 1, "a": Decimal("1.5")})
    path = tmp_path / "verdicts" / "j" / "WETH-x.json"
    data = path.read_bytes()
    assert digest == hashlib.sha256(data).hexdigest()
    # Sorted keys, and a value JSON cannot carry written as its text.
    assert json.loads(data) == {"a": "1.5", "b": 1}
    assert data.startswith(b"{\n")
    # Nothing is left beside it: the partial file was renamed into place.
    assert sorted(path.parent.iterdir()) == [path]
    # Written again, the file is replaced whole and the digest follows the content.
    assert write_sidecar(tmp_path, "verdicts/j/WETH-x.json", {"b": 2}) != digest
    assert json.loads(path.read_bytes()) == {"b": 2}


def test_a_write_that_fails_leaves_no_partial_file(tmp_path, monkeypatch):
    import os

    def refuse(source, target):
        raise PermissionError("the target is held open")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        write_sidecar(tmp_path, "verdicts/j/WETH-x.json", {"b": 1})
    assert list((tmp_path / "verdicts" / "j").iterdir()) == []


def test_a_verdict_record_names_its_sidecar_or_none():
    kept = verdict_record(
        _answer(),
        source="judge-v1",
        symbol="WETH",
        time=_AT,
        model="vendor/model",
        asked_at=_ASKED_AT,
        sidecar_path="verdicts/judge-v1/WETH-20240103T000000Z.json",
        sidecar_digest=_DIGEST,
    )
    assert kept.verdict == Verdict(
        source="judge-v1", symbol="WETH", time=_AT, rating=Rating.BUY, digest=text_digest(_DECISION)
    )
    assert (kept.model, kept.prompt_version, kept.asked_at) == (
        "vendor/model",
        "spot-context-v3",
        _ASKED_AT,
    )
    assert (kept.sidecar_path, kept.sidecar_digest) == (
        "verdicts/judge-v1/WETH-20240103T000000Z.json",
        _DIGEST,
    )
    wordless = verdict_record(
        _answer(Rating.REVIEW), source="judge-v1", symbol="WETH", time=_AT, model="fake",
        asked_at=_ASKED_AT,
    )  # fmt: skip
    assert wordless.verdict.rating is Rating.REVIEW
    assert (wordless.sidecar_path, wordless.sidecar_digest) == (None, None)
