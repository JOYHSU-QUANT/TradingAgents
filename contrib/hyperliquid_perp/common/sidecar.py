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
- atomic (:func:`.atomic_io.atomic_write_bytes`): a service restart landing
  mid-write — a deploy push while a cycle is finishing — must not leave a
  truncated file at the final path for a later reader to choke on;
- never raises: a recording failure must not cost the decision the engine
  already paid for. It is logged with its traceback and the cycle goes on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .atomic_io import atomic_write_bytes
from .digest import json_bytes

logger = logging.getLogger(__name__)

__all__ = ["sidecar_path", "write_sidecar"]


def sidecar_path(payload_path: str | Path, suffix: str) -> Path:
    """``<payload dir>/<payload stem><suffix>``; ``suffix`` carries its dot (``".usage.json"``)."""
    return Path(payload_path).with_suffix(suffix)


def write_sidecar(payload_path: str | None, *, suffix: str, record: Any, what: str) -> None:
    """Write ``record`` as indented JSON at :func:`sidecar_path`. Never raises.

    ``what`` names the artifact in the failure log line. A run with no input
    payload (``payload_path`` is ``None``: the one-shot and test harnesses)
    has nowhere to put a sidecar and writes nothing. A value JSON cannot
    carry is stored as its ``str`` at that leaf (``default=str``), so one odd
    value cannot sink the record or turn its container into a repr string.
    """
    if payload_path is None:
        return
    try:
        atomic_write_bytes(sidecar_path(payload_path, suffix), json_bytes(record, default=str))
    except Exception:  # noqa: BLE001 — recording must never fail the cycle
        logger.exception("%s sidecar could not be written; the decision is unaffected", what)
