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
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from contrib.core.atomic_io import atomic_write_text

__all__ = ["dump_json", "ensure_private_dir", "read_json_object", "write_private"]

_FILE_MODE = 0o640
# A directory under the status directory: group can enter and read, setgid
# so what is written there keeps the dashboard group (as the RUNBOOK makes
# the status directory itself, 2750).
_DIR_MODE = 0o2750


def write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, mode 0640."""
    atomic_write_text(path, lambda fh: fh.write(text))
    os.chmod(path, _FILE_MODE)


def ensure_private_dir(path: Path) -> None:
    """Make ``path`` (one directory under the status directory) if it is not there, mode 2750."""
    path.mkdir(exist_ok=True)
    os.chmod(path, _DIR_MODE)


def _default(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError(f"a status document carries no {type(value).__name__}")


def dump_json(document: object, *, compact: bool = False) -> str:
    """``document`` (dataclasses welcome) as an ASCII JSON text ending in a newline.

    Indented for an operator's eyes unless ``compact`` (a history file is
    thousands of points nobody reads by hand).
    """
    if compact:
        return json.dumps(document, separators=(",", ":"), default=_default) + "\n"
    return json.dumps(document, indent=2, default=_default) + "\n"


def read_json_object(path: Path) -> Mapping[str, Any] | None:
    """The JSON object at ``path``; ``None`` when there is no file; ``ValueError`` saying why otherwise.

    The reason never quotes the path or the text (``not readable
    (PermissionError)``, ``not UTF-8``, ``not JSON``, ``not a JSON object``):
    the page shows it, and a document could quote anything.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"not readable ({type(exc).__name__})") from exc
    except ValueError as exc:  # UnicodeDecodeError is one; so is a null byte in the name
        raise ValueError("not UTF-8") from exc
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValueError("not JSON") from exc
    if not isinstance(document, Mapping):
        raise ValueError("not a JSON object")
    return document
