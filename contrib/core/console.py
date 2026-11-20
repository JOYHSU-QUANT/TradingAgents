"""A console that cannot stop a run: what it cannot encode is printed escaped.

``print`` raises ``UnicodeEncodeError`` when the text holds a character the
stream's encoding has no bytes for. On a UTF-8 terminal that is only ever a
lone surrogate; on a Windows console in a legacy code page (cp950, cp1252)
it is any character outside that page — an emoji is enough. A command that
prints what a model wrote, or what a provider answered, then dies in the
middle of a run on a character it was only reporting: the research loop
printed a refusal that quoted an emoji and ended a paid search after one
round of three (issue #430).

:func:`escape_unencodable_output` sets the two standard streams to
``backslashreplace``, so such a character is written as its escape
(``\\U0001f600``) and everything the encoding can carry is written as it
was. Nothing changes on a stream that can encode the text, which is every
character but a lone surrogate under UTF-8. ``sys.stderr`` is already in
this mode by default; it is set anyway, because a caller may have replaced
it.

A command line calls this once, first thing in its ``main``. It changes the
process's streams and is not undone: a command line is the process.
"""

from __future__ import annotations

import sys

__all__ = ["escape_unencodable_output"]


def escape_unencodable_output() -> None:
    """Make ``sys.stdout`` and ``sys.stderr`` escape what they cannot encode.

    A stream that is not a text wrapper is left alone: ``None`` (a windowed
    interpreter has no console), or a stand-in with no ``reconfigure`` such
    as an ``io.StringIO`` a caller captures into — that one encodes nothing
    and cannot fail this way. So is one that refuses to be reconfigured
    (closed, or detached from its buffer): this is a courtesy to the
    operator, and must not be the thing that stops the command.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="backslashreplace")
        except (ValueError, OSError):
            continue
