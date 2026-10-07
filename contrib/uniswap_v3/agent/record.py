"""Recording a verdict: the sidecar beside the store, and the row the store takes.

A verdict's words are kept in a sidecar, a JSON file under the store's
directory at ``verdicts/<source>/<symbol>-<bar time>.json``: the decision
text, the reports the graph wrote on the way, the context it was handed,
which model under which settings and contract, and how long it took. The
row names the sidecar by that relative path, so the store and its
sidecars move together, and by the digest of the file's bytes, so a
sidecar that is edited or replaced no longer matches its row. The sidecar
is written whole, under another name and then renamed, so a stop
mid-write leaves no half file at the path a row names, and a write that
fails leaves nothing behind. A judge that keeps no words
(:class:`~.graph.FakeJudge`) gets a row without a sidecar.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..domain.times import FILE_STAMP, utc_text
from ..domain.verdicts import Verdict, VerdictRecord, text_digest
from .graph import PROMPT_VERSION, Answer

__all__ = ["sidecar_path", "sidecar_record", "verdict_record", "write_sidecar"]


def sidecar_path(source: str, symbol: str, time: int) -> str:
    """Where the words of ``source``'s verdict on ``symbol`` at ``time`` go, relative to the store's directory."""
    return f"verdicts/{source}/{symbol}-{utc_text(time, FILE_STAMP)}.json"


def sidecar_record(
    answer: Answer,
    *,
    source: str,
    symbol: str,
    ticker: str,
    time: int,
    trade_date: str,
    context: str,
    model: str,
    settings: Mapping[str, object],
    asked_at: int,
) -> dict[str, Any]:
    """The sidecar's content: what was asked, of whom and how it was set up, and everything that came back."""
    return {
        "schema": 1,
        "source": source,
        "symbol": symbol,
        "ticker": ticker,
        "time": time,
        "trade_date": trade_date,
        "model": model,
        "judge": dict(settings),
        "prompt_version": PROMPT_VERSION,
        "asked_at": asked_at,
        "elapsed_seconds": round(answer.elapsed_seconds, 3),
        "rating": answer.rating.value,
        "spot_context": context,
        "decision": answer.decision,
        "decision_digest": text_digest(answer.decision),
        "reports": None if answer.reports is None else dict(answer.reports),
    }


def write_sidecar(home: Path, relative: str, record: Mapping[str, Any]) -> str:
    """Write ``record`` as JSON at ``home / relative`` and return the digest of the file's bytes.

    A value JSON cannot carry is written as its ``str``: the reports are the
    engine's, and one odd value must not lose the record. A write that
    fails (:class:`OSError`) leaves no partial file behind.
    """
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, default=str).encode(
        "utf-8"
    )
    partial = path.with_name(f"{path.name}.{os.getpid()}.part")
    try:
        with open(partial, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(partial, path)
    except OSError:
        with contextlib.suppress(OSError):
            os.remove(partial)
        raise
    return hashlib.sha256(data).hexdigest()


def verdict_record(
    answer: Answer,
    *,
    source: str,
    symbol: str,
    time: int,
    model: str,
    asked_at: int,
    sidecar_path: str | None = None,
    sidecar_digest: str | None = None,
) -> VerdictRecord:
    """The row for ``answer``, naming its sidecar by relative path and file digest when it has one."""
    return VerdictRecord(
        verdict=Verdict(
            source=source,
            symbol=symbol,
            time=time,
            rating=answer.rating,
            digest=text_digest(answer.decision),
        ),
        model=model,
        prompt_version=PROMPT_VERSION,
        asked_at=asked_at,
        sidecar_path=sidecar_path,
        sidecar_digest=sidecar_digest,
    )
