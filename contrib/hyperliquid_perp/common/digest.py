"""The payload artifact digest — ``sha256:<hex>`` over the bytes on disk."""

from __future__ import annotations

import hashlib

__all__ = ["payload_digest"]


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
