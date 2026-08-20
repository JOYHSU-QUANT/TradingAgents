"""Atomic text-file writes, shared by CSV export and the audit decision log.

One definition of the tmp -> replace dance (phase2-data §1.1): write the whole
payload to a sibling ``<name>.tmp``, then atomically rename over the
destination, so a crash or a serialization error mid-write can never leave a
truncated or half-written file at the final path. On failure the tmp file is
removed (best-effort) and the original exception re-raised; the destination —
if one already existed — is untouched either way, because ``os.replace`` is
all-or-nothing.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import IO

__all__ = ["atomic_write_text"]


def atomic_write_text(
    path: Path, write_body: Callable[[IO[str]], object], *, newline: str | None = None
) -> None:
    """Write ``write_body(fh)``'s output to ``path`` atomically (UTF-8 text).

    ``newline`` is passed straight through to :meth:`Path.open`; each caller
    owns the choice, because it is a property of the payload writer (e.g. the
    csv module must own its own line endings) — the helper picking one for
    everybody would silently flip newline bytes for some caller.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as fh:
            write_body(fh)
        os.replace(tmp, path)
    except BaseException:
        # Never leave a stray .tmp behind; a secondary unlink failure (e.g. a
        # Windows file lock) must not replace the original write error — that
        # is the one the caller needs to see.
        with suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
