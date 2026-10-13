"""Text as a line prints it: one printable ASCII line, and counts by name."""

from __future__ import annotations

from collections.abc import Mapping

__all__ = ["counts", "one_ascii_line"]


def one_ascii_line(text: object) -> str:
    """``text`` as one printable ASCII line.

    A line that quotes what a node said need not be ASCII, which a console
    may be unable to print; a line break in it would read as a second line,
    and a control character could do anything to a terminal.
    """
    line = " ".join(str(text).split())
    printable = "".join(char if char.isprintable() else "?" for char in line)
    return printable.encode("ascii", "backslashreplace").decode()


def counts(found: Mapping[str, int]) -> str:
    """``name count`` for every entry, by name, comma-separated."""
    return ", ".join(f"{name} {count}" for name, count in sorted(found.items()))
