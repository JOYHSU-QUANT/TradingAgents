"""A sidecar: a JSON artifact written beside a run's input payload.

Two exist today — ``<payload>.usage.json`` (the cycle's completion
measurement, :mod:`..integration.completion_usage`) and
``<payload>.reports.json`` (what the engine's agents wrote on the way to the
decision, :mod:`..integration.decision_reports`). Both follow one contract,
and this module IS that contract, so a third sidecar cannot drift from it:

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
  keys happen to be present;
- atomic (:func:`.atomic_io.atomic_write_bytes`): a service restart landing
  mid-write — a deploy push while a cycle is finishing — must not leave a
  truncated file at the final path for a later reader to choke on;
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

from .atomic_io import atomic_write_bytes
from .digest import json_bytes

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
    payload_path: str | None,
    *,
    suffix: str,
    what: str,
    build: Callable[[], Mapping[str, Any]],
) -> None:
    """Write ``build()``'s record, stamped with the schema, at :func:`sidecar_path`. Never raises.

    ``what`` names the artifact in the log lines. A run with no input payload
    (``payload_path`` is ``None``: the one-shot and test harnesses) has
    nowhere to put a sidecar and writes nothing — ``build`` is not called. A
    value JSON cannot carry is stored as its ``str`` at that leaf, with a
    WARNING naming its type, so one odd value cannot sink the record or turn
    its container into a repr string, and the degradation is not silent.
    """
    if payload_path is None:
        return
    try:
        record = {"schema": SIDECAR_SCHEMA, **build()}
        atomic_write_bytes(
            sidecar_path(payload_path, suffix), json_bytes(record, default=_stringify_for(what))
        )
    except Exception:  # noqa: BLE001 — recording must never fail the cycle
        logger.exception("%s sidecar could not be written; the decision is unaffected", what)


def _stringify_for(what: str) -> Callable[[Any], str]:
    def default(value: Any) -> str:
        logger.warning(
            "%s sidecar: a %s value JSON cannot carry was stored as its str",
            what,
            type(value).__name__,
        )
        return str(value)

    return default
