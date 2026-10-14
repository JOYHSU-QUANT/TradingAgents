"""Writing under the status directory: atomic, and readable by the dashboard group only.

Every file this package writes goes through :func:`write_private`: the
shared floor's atomic write (a reader never sees half a document) and then
mode ``0640`` — owner read-write, group read, nothing for others — so the
``dashboard`` account the plan gives the page reads them through the
directory's group and nothing else on the host does (plan §4). The unit
file sets ``UMask=0027`` as well, so the temporary file the atomic write
goes through is never wider than that either.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from contrib.core.atomic_io import atomic_write_text

__all__ = ["dump_json", "write_private"]

_FILE_MODE = 0o640


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, mode 0640."""
    atomic_write_text(path, lambda fh: fh.write(text))
    os.chmod(path, _FILE_MODE)


def _default(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"a status document carries no {type(value).__name__}")


def dump_json(document: object) -> str:
    """``document`` (dataclasses welcome) as an ASCII JSON text ending in a newline."""
    return json.dumps(document, indent=2, default=_default) + "\n"
