"""A sidecar: a JSON artifact written beside a run's input payload.

Three exist today — ``<payload>.usage.json`` (the cycle's completion
measurement, :mod:`..integration.completion_usage`), ``<payload>.reports.json``
(what the engine's agents wrote on the way to the decision,
:mod:`..integration.decision_reports`) and ``<payload>.handoff.json`` (the
carry handoff a file-target cycle acted on,
:mod:`..integration.file_target_provider`). All follow one contract, and this
module IS that contract, so a fourth sidecar cannot drift from it:

- same directory, same stem, its own suffix (:func:`sidecar_path`) — one
  spelling, so a rename at a writer cannot leave a reader looking in the old
  place (the ``store_layout`` lesson, issue #221);
- NOT the payload: the payload's bytes are hashed into
  ``ai_inputs.input_payload_hash``, so nothing is ever appended to that file;
- no row points at a sidecar and no verdict reads one (``validate`` /
  ``export`` / the fingerprint backfill), so deleting one loses a measurement
  or a record, never a verdict;
- every record carries ``"schema": SIDECAR_SCHEMA`` so a later reader can
  tell the format apart from a successor's instead of guessing from which
  keys happen to be present. A ``.usage.json`` with NO ``schema`` key is one
  written before the stamp shipped (every paper-BTC run up to and including
  6, the run live when this landed): same shape as schema 1 minus the stamp;
- atomic (:func:`contrib.core.atomic_io.atomic_write_bytes`): a service
  restart landing mid-write — a deploy push while a cycle is finishing —
  must not leave a truncated file at the final path for a later reader to
  choke on;
- never raises: a recording failure must not cost the decision the engine
  already paid for. It is logged with its traceback and the cycle goes on —
  and the record is BUILT inside that protection too (``build`` is called
  here), so a builder that trips over its input is a logged failure, not a
  lost decision.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from contrib.core.atomic_io import atomic_write_bytes
from contrib.core.digest import json_text
from contrib.core.surrogates import replace_lone_surrogates

logger = logging.getLogger(__name__)

__all__ = ["SIDECAR_SCHEMA", "sidecar_path", "write_sidecar"]

#: The format version stamped into every sidecar as its ``"schema"`` key. Bump
#: it when a sidecar's key set or a value's shape changes in a way a reader of
#: the old files would misread.
SIDECAR_SCHEMA = 1


def sidecar_path(payload_path: str | Path, suffix: str) -> Path:
    """``<payload dir>/<payload stem><suffix>``; ``suffix`` carries its dot (``".usage.json"``)."""
    return Path(payload_path).with_suffix(suffix)


def write_sidecar(
    payload_path: str | Path | None,
    *,
    suffix: str,
    what: str,
    build: Callable[[], Mapping[str, Any]],
) -> None:
    """Write ``build()``'s record, stamped with the schema, at :func:`sidecar_path`. Never raises.

    ``what`` names the artifact in the log lines. A run with no input payload
    (``payload_path`` is ``None``: the one-shot and test harnesses) has
    nowhere to put a sidecar and writes nothing — ``build`` is not called. The
    stamp is this module's: a builder that returns its own ``"schema"`` key is
    refused (logged, nothing written) rather than allowed to lie about the
    format. A VALUE JSON cannot carry is stored as its ``str`` at that leaf,
    with one WARNING per type per write, so one odd value cannot sink the
    record or turn its container into a repr string, and the degradation is
    not silent. A CHARACTER UTF-8 cannot carry — a lone surrogate in what an
    agent wrote: it reached the reports record already when the final
    decision held one, and every report can bring one since a later node
    can be sent such text (issue #441) — is written as U+FFFD, one for one,
    with a WARNING naming the file and saying how many: the same thing the
    store does with the decision's own text (issue #427), and for the same
    reason. Counted where it stands in the written text, so a key and a
    value each count; a record holding none is written byte for byte as
    before. (The handoff sidecar records the digest of the coordinator's file
    beside what was parsed from it: after a replacement the two no longer
    describe the same bytes, and that WARNING is what says so.) A dict KEY
    JSON cannot carry is the one exception:
    ``json.dumps`` never consults ``default`` for keys, so that record is lost
    whole — logged as a failed write, no file. Both builders' own keys are
    ``str``; the reports record's nested debate dicts are upstream's.
    """
    if payload_path is None:
        return
    try:
        built = build()
        if "schema" in built:
            raise ValueError(f"{what} sidecar builder returned its own 'schema' key")
        record = {"schema": SIDECAR_SCHEMA, **built}
        path = sidecar_path(payload_path, suffix)
        text, replaced = replace_lone_surrogates(json_text(record, default=_stringify_for(what)))
        if replaced:
            logger.warning(
                "%s sidecar: %d lone surrogate(s) in %s cannot be written as UTF-8; each is "
                "written as U+FFFD",
                what,
                replaced,
                path.name,
            )
        atomic_write_bytes(path, text.encode("utf-8"))
    except Exception:  # noqa: BLE001 — recording must never fail the cycle
        logger.exception("%s sidecar could not be written; the decision is unaffected", what)


def _stringify_for(what: str) -> Callable[[Any], str]:
    """``json.dumps``'s ``default`` for one write: ``str(value)``, warned once per type."""
    warned: set[str] = set()

    def default(value: Any) -> str:
        kind = type(value).__name__
        if kind not in warned:
            warned.add(kind)
            logger.warning(
                "%s sidecar: a %s value JSON cannot carry was stored as its str", what, kind
            )
        return str(value)

    return default
