"""Text as a line prints it, and as a file or a store can take it.

One printable ASCII line, counts by name, and words made storable: a
Python ``str`` can hold a lone surrogate (a code point from U+D800 to
U+DFFF, half of a UTF-16 pair with no partner), which has no UTF-8
encoding — it cannot be hashed over its bytes, written to a UTF-8 file or
bound into SQLite. A judge's words reach that state when a provider's JSON
carried half an escaped pair and the client decoded it (issue #429).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

__all__ = ["REPLACEMENT_CHARACTER", "counts", "one_ascii_line", "replace_lone_surrogates"]

REPLACEMENT_CHARACTER: Final = "\N{REPLACEMENT CHARACTER}"

# Built with ``chr`` so this source holds no surrogate, escaped or not.
_LONE_SURROGATE: Final = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")


def replace_lone_surrogates(text: str) -> tuple[str, int]:
    """``text`` with each lone surrogate as U+FFFD, and how many there were.

    One for one: writing an escape out instead would turn one character
    into six, which could not be told from a judge that wrote those six.
    A well-formed pair is a single code point in a ``str``, so every
    surrogate met here stands alone and no pairing is attempted: two halves
    that happen to be adjacent become two U+FFFD. Text without one comes
    back as the same object.

    The shared floor has this function too (``contrib/core/surrogates.py``);
    this package imports no other contrib package
    (``tests/test_isolation.py``), so it keeps its own, with its own tests
    (decided 2026-11-22).
    """
    replaced, count = _LONE_SURROGATE.subn(REPLACEMENT_CHARACTER, text)
    return (replaced, count) if count else (text, 0)


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
