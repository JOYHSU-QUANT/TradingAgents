"""Text a model wrote, made safe to store: lone surrogates become U+FFFD.

A Python ``str`` can hold a code point from U+D800 to U+DFFF on its own — half
of a UTF-16 pair with no partner. UTF-8 has no encoding for one, so the text
cannot be bound into SQLite, written to a UTF-8 file, or hashed over its
bytes: each raises ``UnicodeEncodeError`` at the write, long after the text
was accepted.

Model output reaches that state two ways, and a caller has to cover both:

- the text itself holds one, because the provider's JSON carried half an
  escaped pair and the client decoded it;
- the text is plain ASCII, and a later ``json.loads`` of it turns the six
  characters of an escape (a backslash, ``u``, ``d83d``) into one. The raw
  reply stores fine; the field parsed out of it does not (issue #427).

:func:`replace_lone_surrogates` swaps each for U+FFFD, the replacement
character, one for one: writing the escape out instead would turn one
character into six and could not be told from a model that wrote those six.
It returns how many it replaced, because U+FFFD in the stored text does not
say whether the model wrote it or this function did.

Every surrogate code point in a ``str`` is a lone one — a well-formed pair is
a single code point above U+FFFF, never two — so there is nothing to pair up
before replacing.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["REPLACEMENT_CHARACTER", "replace_lone_surrogates"]

REPLACEMENT_CHARACTER: Final = "\ufffd"

_LONE_SURROGATE: Final = re.compile("[\ud800-\udfff]")


def replace_lone_surrogates(text: str) -> tuple[str, int]:
    """Return ``text`` with each lone surrogate as U+FFFD, and how many there were.

    Text without one comes back as the same object, so a caller may store the
    result unconditionally and a clean answer is byte-for-byte what it was.
    """
    replaced, count = _LONE_SURROGATE.subn(REPLACEMENT_CHARACTER, text)
    return (replaced, count) if count else (text, 0)
