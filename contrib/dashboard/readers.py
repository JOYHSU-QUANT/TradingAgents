"""Reading one line's document by running its package's command line.

The exporter does not import the packages it reports on: it runs each
line's command (``status --json`` or ``report --json``) from the line's own
checkout with the line's own python, and takes the JSON document it prints.
A read either gives the document or says why not, in one word or sentence
the alert rules carry as the ``read_failed`` condition: the command timed
out, exited non-zero (``exit 5`` is the perp package's "investigate the
store"), printed no JSON object, or printed one of another source or
schema version than this build reads (:data:`KNOWN_SCHEMA_VERSIONS` — a
package that bumps its ``SCHEMA_VERSION`` bumps it here in the same PR,
``tests/test_schema_pin.py`` holds the two together).

The first line of what the command wrote to stderr is kept for the
``exporter.json`` document, masked for a ``0x``-prefixed 20- or 32-byte hex
and cut at :data:`STDERR_LIMIT` characters, as the packages' own status
documents mask their free text. It is never put in a mail.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CarryLine, Line

__all__ = [
    "KNOWN_SCHEMA_VERSIONS",
    "STDERR_LIMIT",
    "CommandResult",
    "LineRead",
    "Runner",
    "masked_line",
    "read_line",
    "run_command",
    "run_label",
]

# The document version of each source this build reads; see the module docstring.
KNOWN_SCHEMA_VERSIONS: Mapping[str, int] = {"hyperliquid_perp": 1, "uniswap_v3": 1, "carry": 1}
STDERR_LIMIT = 200
_HEX_SECRET = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})")


@dataclass(frozen=True)
class CommandResult:
    """What a command left behind; ``exit_code`` ``None`` when it was stopped for taking too long."""

    exit_code: int | None
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], Path, float], CommandResult]


def _text(raw: object) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw if isinstance(raw, str) else ""


def run_command(argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
    """Run ``argv`` from ``cwd`` and capture it; the :data:`Runner` the exporter uses outside tests."""
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(None, _text(exc.stdout), _text(exc.stderr))
    except OSError as exc:
        # The python is not there, or the checkout: the command never ran.
        return CommandResult(127, "", f"{type(exc).__name__}: {exc.strerror or exc}")
    return CommandResult(done.returncode, done.stdout, done.stderr)


def masked_line(text: str) -> str | None:
    """The first non-empty line of ``text``, hex secrets masked, cut at :data:`STDERR_LIMIT`; ``None`` if none."""
    for raw in text.splitlines():
        line = raw.strip()
        if line:
            masked = _HEX_SECRET.sub(lambda m: f"{m.group(0)[:6]}...{m.group(0)[-4:]}", line)
            return masked if len(masked) <= STDERR_LIMIT else masked[: STDERR_LIMIT - 3] + "..."
    return None


def run_label(line: Line) -> str:
    """The run(s) a line reads, for a label: a carry sleeve names both legs."""
    if isinstance(line, CarryLine):
        return f"{line.perp_run_id}+{line.spot_run_id}"
    return line.run_id


@dataclass(frozen=True)
class LineRead:
    """One line's read: the document, or why there is none.

    ``raw`` is the document as the command printed it, written to the
    status directory verbatim.
    """

    line: Line
    exit_code: int | None
    failure: str | None
    stderr_line: str | None
    document: Mapping[str, Any] | None
    raw: str | None
    duration_ms: int

    def __post_init__(self) -> None:
        if (self.failure is None) != (self.document is not None):
            raise ValueError("a read has its document, or the reason it has none")
        if (self.document is None) != (self.raw is None):
            raise ValueError("the raw text goes with the document")

    @property
    def ok(self) -> bool:
        return self.failure is None

    @property
    def name(self) -> str:
        return self.line.name

    @property
    def kind(self) -> str:
        return self.line.kind

    @property
    def unit(self) -> str:
        return self.line.unit

    @property
    def run_label(self) -> str:
        return run_label(self.line)


def _document(
    line: Line, result: CommandResult, timeout: float
) -> tuple[Mapping[str, Any] | None, str | None]:
    """``(document, None)`` or ``(None, failure)``."""
    if result.exit_code is None:
        return None, f"timed out after {timeout:g} s"
    if result.exit_code != 0:
        return None, f"exit {result.exit_code}"
    try:
        parsed = json.loads(result.stdout)
    except ValueError:
        return None, "no JSON document on stdout"
    if not isinstance(parsed, dict):
        return None, "no JSON document on stdout"
    source = parsed.get("source")
    version = parsed.get("schema_version")
    if source != line.source or version != KNOWN_SCHEMA_VERSIONS[line.source]:
        return None, (
            f"document is {source!r} schema {version!r}; this exporter reads "
            f"{line.source!r} schema {KNOWN_SCHEMA_VERSIONS[line.source]}"
        )
    return parsed, None


def read_line(line: Line, *, runner: Runner, timeout: float) -> LineRead:
    """Run ``line``'s command and read what it printed."""
    started = time.monotonic()
    result = runner(line.argv(), line.cwd, timeout)
    duration_ms = int((time.monotonic() - started) * 1000)
    document, failure = _document(line, result, timeout)
    return LineRead(
        line=line,
        exit_code=result.exit_code,
        failure=failure,
        stderr_line=masked_line(result.stderr),
        document=document,
        raw=result.stdout.strip() + "\n" if document is not None else None,
        duration_ms=duration_ms,
    )
