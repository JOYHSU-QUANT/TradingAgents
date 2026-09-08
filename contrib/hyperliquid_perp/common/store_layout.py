"""Where a run's files live beside its SQLite store — the on-disk layout.

The daemons (``paper``, ``live``, ``live-smoke``) write every raw payload
they keep as evidence — the AI input JSON each cycle, the clearinghouse
snapshots behind a reconciliation verdict, the refused ``orderStatus``
answers — under ONE directory derived from the store's own path:
``<db dir>/payloads/<run_id>/``. The offline ``export
--backfill-format-fingerprint`` pass reads those AI payloads back, and on a
store copied away from its host it looks for them in the same place beside
the copy before asking the operator for ``--payload-root``.

One function rather than a recipe repeated at each site: the writers and
the reader used to spell ``db_path.resolve().parent / "payloads" / run_id``
independently (four ``cli`` sites, the backfill hint and a test pin), so a
rename of the middle segment at one writer would have left the reader
looking in the old place with nothing red (issue #221). The layout is a fact
about the store and its files, owned by neither side, so it lives here at
the bottom of the import graph.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["PAYLOADS_DIRNAME", "payload_dir"]

# The one spelling of the middle segment. Module-level so the test that pins
# the layout can name it without repeating the literal.
PAYLOADS_DIRNAME = "payloads"


def payload_dir(db_path: str | Path, run_id: str) -> Path:
    """``<db dir>/payloads/<run_id>`` for the store at ``db_path``.

    Resolved through the store's ABSOLUTE parent so the paths the daemon
    records on ``ai_inputs.input_payload_path`` stay meaningful after a
    ``chdir`` (a relative ``--db paper_trading.db`` is the common case). The
    store need not exist yet — a fresh ``paper`` run derives its payload
    directory before the first write — and nothing is created here: each
    writer makes the directory on first use.
    """
    return Path(db_path).resolve().parent / PAYLOADS_DIRNAME / run_id
