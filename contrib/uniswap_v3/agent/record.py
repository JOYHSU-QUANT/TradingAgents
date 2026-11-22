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

A judge's words can hold a character that cannot be written as UTF-8 (a
lone surrogate: issue #429). Neither the digest nor the file can take one,
so an answer is made storable first (:func:`storable_answer`), once, and
the digest, the file and the row are all of the same words.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..domain.text import replace_lone_surrogates
from ..domain.times import FILE_STAMP, utc_text
from ..domain.verdicts import Verdict, VerdictRecord, text_digest
from .graph import PROMPT_VERSION, Answer

logger = logging.getLogger(__name__)

__all__ = [
    "sidecar_path",
    "sidecar_record",
    "storable_answer",
    "verdict_record",
    "write_sidecar",
]


def storable_answer(answer: Answer) -> tuple[Answer, int]:
    """``answer`` with every lone surrogate in its words as U+FFFD, and how many there were.

    The decision, the line that says why news was missing, and every string
    in the reports that is reached through mappings (their keys too), lists
    and tuples. Done once, before
    anything is digested or written, so that the sidecar's ``decision``,
    its ``decision_digest`` and the row's digest are of the same words and
    a reader who digests the sidecar's decision gets the row's. An answer
    with none comes back as the same object, and what it writes is byte for
    byte what it was.

    An answer that held one used to end the visit in a traceback, at the
    digest or at the sidecar's dump: the judge had been paid, the token was
    not recorded, and the next visit asked again.
    """
    decision, count = replace_lone_surrogates(answer.decision)
    reason = answer.news_missing_reason
    if reason is not None:
        reason, found = replace_lone_surrogates(reason)
        count += found
    reports: Any = answer.reports
    if reports is not None:
        reports, found = _storable(reports)
        count += found
    if not count:
        return answer, 0
    return (
        replace(answer, decision=decision, reports=reports, news_missing_reason=reason),
        count,
    )


def _storable(value: object) -> tuple[Any, int]:
    """``value`` with the strings in it made storable — mappings (keys too), lists and tuples walked."""
    if isinstance(value, str):
        return replace_lone_surrogates(value)
    if isinstance(value, Mapping):
        made: dict[Any, Any] = {}
        count = 0
        for key, item in value.items():
            key, in_key = _storable(key)
            made[key], in_item = _storable(item)
            count += in_key + in_item
        return made, count
    if isinstance(value, list | tuple):
        items = [_storable(item) for item in value]
        return [item for item, _ in items], sum(found for _, found in items)
    return value, 0


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
        "schema": 2,
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
        "news_missing": answer.news_missing,
        "news_missing_reason": answer.news_missing_reason,
        "spot_context": context,
        "decision": answer.decision,
        "decision_digest": text_digest(answer.decision),
        "reports": None if answer.reports is None else dict(answer.reports),
    }


def write_sidecar(home: Path, relative: str, record: Mapping[str, Any]) -> str:
    """Write ``record`` as JSON at ``home / relative`` and return the digest of the file's bytes.

    A value JSON cannot carry is written as its ``str``: the reports are the
    engine's, and one odd value must not lose the record. Nor must a
    character UTF-8 cannot carry: a lone surrogate still in the text —
    in the ``str`` of such a value, or in a record whose answer was not
    passed through :func:`storable_answer` — is written as U+FFFD, and a
    WARNING says how many. A write that fails (:class:`OSError`) leaves no
    partial file behind.
    """
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False, default=str)
    text, replaced = replace_lone_surrogates(text)
    if replaced:
        # Not reached by an answer that was made storable first, unless a
        # value of it was written through ``default=str``.
        logger.warning(
            "%d lone surrogate(s) in the sidecar at %s cannot be written as UTF-8; each is "
            "written as U+FFFD",
            replaced,
            relative,
        )
    data = text.encode("utf-8")
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
        news_missing=answer.news_missing,
    )
