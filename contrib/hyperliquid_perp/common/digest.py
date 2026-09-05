"""The payload artifact digest — ``sha256:<hex>`` over the bytes on disk."""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["json_bytes", "payload_digest"]


def json_bytes(obj: Any) -> bytes:
    """The one spelling of "indented JSON as UTF-8 bytes" the artifacts are written in.

    The input payload (``cli._provider``), its ``.usage.json`` sidecar and the
    tests that fabricate payload files all write this shape; ``payload_digest``
    hashes exactly these bytes. One function so an ``indent``/``sort_keys``
    change for digest stability cannot land on one writer and not the others.
    """
    return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")


def payload_digest(raw: bytes) -> str:
    """``sha256:<hex>`` of ``raw`` — the ``ai_inputs.input_payload_hash`` grammar.

    Over BYTES — the ones written to the file and read back from it — so the
    daemon that records the digest (``cli._provider``) and the backfill that
    verifies it (``persistence.backfill``) hash the same thing on every
    platform. Hashing the JSON *string* and then writing it in text mode let
    the OS rewrite the newlines in between (``\\r\\n`` on Windows), so the row
    described bytes the file never held (issue #163 review). One spelling of
    the grammar, shared by the writer, the verifier and the tests that
    fabricate rows.
    """
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"
