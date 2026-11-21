"""Standard streams a command line can print any text to.

``print`` raises ``UnicodeEncodeError`` when the text holds a character the
stream's encoding has no bytes for and the stream's error handler is one that
raises (``sys.stdout``'s is; ``sys.stderr``'s, ``backslashreplace``, is not).
Where that bites is narrower than it looks, and was measured on the Windows
machine this project is developed on (Python 3.12, ANSI code page 950):

- On a CONSOLE, ``sys.stdout`` is UTF-8 whatever the code page — the
  interpreter writes through the console API — and no character raises but
  a lone surrogate, which UTF-8 cannot write either.
- With output REDIRECTED — a pipe, ``> run.log``, a tool that captures the
  command — ``sys.stdout`` is the ANSI code page, and a character outside it
  raises. An emoji is enough.

A command that prints what a model wrote, or what a provider answered, then
dies in the middle of a run on a character it was only reporting. Reproduced
with a scripted model: the research loop printed a refusal that quoted an
emoji, and a search of three rounds exited 1 after the first (issue #430).

:func:`accept_any_text_on_output` makes the two standard streams unable to
fail that way, in two steps a stream takes or not on its own state:

1. **A redirected stream is written as UTF-8.** What a reader of those bytes
   gets is the text itself — a model's emoji, the prompt a ``--dry-run`` shows
   exactly as the model receives it — in the encoding Python writes already
   under a UTF-8 locale, as on CI. Not done to a terminal, which already has
   the encoding it displays; and not done when ``PYTHONIOENCODING`` is set,
   since then the operator has said how they want the streams encoded.
2. **A handler that can raise becomes ``backslashreplace``,** so what is
   still outside the encoding (under UTF-8, only a lone surrogate) is
   written as its escape. A handler that already cannot raise — ``replace``,
   or the ``backslashreplace`` ``sys.stderr`` has by default — is left as it
   was chosen.

Two costs, both accepted. The first: a reader that decodes a pipe as a
legacy code page no longer reads non-ASCII from these commands, where a
reader that expects UTF-8 used not to. Two such readers were met on that
Windows machine. ``subprocess.run(..., text=True)`` decodes with the locale's
encoding: a test that runs autoresearch's ``report`` in a child could no
longer decode the dash in one of its lines until the test said
``encoding="utf-8"``. And Windows PowerShell 5.1 decodes what a command pipes
or redirects (``|``, ``>``, ``$x = ...``) with ``[Console]::OutputEncoding``,
the OEM code page unless changed: a dash came out as ``??`` and a Chinese
character as another one, until that was set to UTF-8 or
``PYTHONIOENCODING=cp950`` was set for the command (which then writes cp950
again, and escapes what that cannot carry). So ``> run.log`` from PowerShell
holds the text itself only with one of those two; from Git Bash, where the
command writes the file itself, it does without either.

The second: where the handler was ``surrogateescape`` — ``sys.stdout`` on
Windows, console or redirected, and on POSIX under the C locale — a byte
that could not be decoded (on POSIX, in a file name say) used to be written
back as that byte and is now written as an escape.

A command line calls this once, first thing in its ``main``: text a stream
still holds when it is called is flushed in the old encoding. It changes the
process's streams and is not undone: a command line is the process. One
state is knowingly left as it is: under ``-E`` or ``-I`` the interpreter
ignores ``PYTHONIOENCODING`` and this function still reads it, so with the
variable set a redirected stream keeps its encoding and gets only the
handler.
"""

from __future__ import annotations

import os
import sys

__all__ = ["accept_any_text_on_output"]

# The error handlers that raise on some text. ``surrogateescape`` and
# ``surrogatepass`` are here because they only ever rescue surrogates: an
# emoji on a cp950 stream raises through either, exactly as through ``strict``.
_CAN_RAISE = frozenset({"strict", "surrogateescape", "surrogatepass"})


def accept_any_text_on_output() -> None:
    """Make ``sys.stdout`` and ``sys.stderr`` able to print any text; see the module docstring.

    A stream that is not a text wrapper is left alone: ``None`` (a windowed
    interpreter has no console), or a stand-in with no ``reconfigure`` such
    as an ``io.StringIO`` a caller captures into — that one encodes nothing
    and cannot fail this way. So is one that will not be asked or changed
    (closed, detached from its buffer, a pipe whose reader has gone): this is
    a courtesy to the operator, and must not be what stops the command.
    """
    encoding_was_chosen = bool(os.environ.get("PYTHONIOENCODING"))
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            was = stream.errors
            errors = "backslashreplace" if was in _CAN_RAISE else was
            if not encoding_was_chosen and not stream.isatty():
                # Both, always: given an encoding alone, ``reconfigure`` puts
                # the handler back to ``strict`` whatever it was.
                reconfigure(encoding="utf-8", errors=errors)
            elif errors != was:
                reconfigure(errors=errors)
        except Exception:  # noqa: BLE001 - a courtesy must not stop the command
            continue
