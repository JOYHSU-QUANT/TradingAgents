"""Tests for the standard streams a command line can print any text to (issue #430).

The command lines that call it pin THAT they do, on their own output
(``contrib/autoresearch/tests/test_hypothesis.py``, ``contrib/discovery/tests/
test_cli.py``); this file pins what the call does to a stream in each state.

The streams here are stand-ins: a ``TextIOWrapper`` over bytes, redirected by
default (``isatty()`` is false) or a terminal by subclass. ``cp950`` with
``surrogateescape`` is what a redirected ``sys.stdout`` was measured to be on
the Windows machine this project is developed on; ``strict`` and
``surrogatepass`` are tried beside it because an emoji raises through all
three.
"""

from __future__ import annotations

import io
import sys

import pytest

from contrib.core.console import accept_any_text_on_output

_EMOJI = "\U0001f600"  # outside every Windows ANSI code page
_HALF = "\ud83d"  # half of its UTF-16 pair: outside UTF-8 too


class _Terminal(io.TextIOWrapper):
    """A stream that says it is a terminal."""

    def isatty(self) -> bool:
        return True


def _stream(encoding: str, errors: str = "surrogateescape", *, terminal: bool = False):
    kind = _Terminal if terminal else io.TextIOWrapper
    return kind(io.BytesIO(), encoding=encoding, errors=errors, newline="")


def _written(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()


@pytest.fixture(autouse=True)
def _no_encoding_chosen(monkeypatch):
    # The shell these tests are run from may export it; each test that wants
    # it set says so.
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)


def _install(monkeypatch, out, err=None):
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err if err is not None else _stream("cp950"))
    return out


# -- the premise ---------------------------------------------------------------


@pytest.mark.parametrize("errors", ["strict", "surrogateescape", "surrogatepass"])
def test_the_premise_a_cp950_stream_raises_on_an_emoji_through_each_raising_handler(errors):
    # No product code: this is what the stand-in has to do for the tests
    # below to mean anything, and it is the defect.
    stream = _stream("cp950", errors)
    with pytest.raises(UnicodeEncodeError):
        stream.write(_EMOJI)


# -- a redirected stream ---------------------------------------------------------


@pytest.mark.parametrize("errors", ["strict", "surrogateescape", "surrogatepass"])
def test_a_redirected_stream_is_written_as_utf8_whatever_it_was(monkeypatch, errors):
    out = _install(monkeypatch, _stream("cp950", errors))
    accept_any_text_on_output()
    text = f"refused: {_EMOJI} \u898f\u5247 \u2014"
    print(text)
    assert _written(out) == (text + "\n").encode("utf-8")
    assert (out.encoding, out.errors) == ("utf-8", "backslashreplace")


def test_what_utf8_itself_cannot_write_is_written_as_its_escape(monkeypatch):
    out = _install(monkeypatch, _stream("cp950"))
    accept_any_text_on_output()
    print(f"half: {_HALF}")
    assert _written(out) == b"half: \\ud83d\n"


def test_both_streams_are_set_each_on_its_own_state(monkeypatch):
    out, err = _stream("cp950", "strict"), _stream("cp950", "backslashreplace")
    _install(monkeypatch, out, err)
    accept_any_text_on_output()
    print(_EMOJI)
    print(_EMOJI, file=sys.stderr)
    assert _written(out) == _written(err) == (_EMOJI + "\n").encode("utf-8")
    assert err.errors == "backslashreplace"


def test_a_handler_that_cannot_raise_is_kept_as_it_was_chosen(monkeypatch):
    # ``replace`` is the caller's choice (pytest's default capture of the
    # file descriptors uses it); the encoding still becomes UTF-8, because
    # the stream is redirected.
    out = _install(monkeypatch, _stream("cp950", "replace"))
    accept_any_text_on_output()
    assert (out.encoding, out.errors) == ("utf-8", "replace")


# -- a terminal, and an encoding the operator chose -------------------------------


def test_a_terminal_keeps_its_encoding_and_escapes_what_is_outside_it(monkeypatch):
    # A terminal displays its own encoding; writing another would garble it.
    out = _install(monkeypatch, _stream("cp950", "strict", terminal=True))
    accept_any_text_on_output()
    text = "\u898f\u5247 \u2014 ok"
    print(text + _EMOJI)
    assert _written(out) == text.encode("cp950") + b"\\U0001f600\n"
    assert (out.encoding, out.errors) == ("cp950", "backslashreplace")


def test_a_terminal_that_can_already_print_anything_is_not_touched(monkeypatch):
    out = _install(monkeypatch, _stream("utf-8", "replace", terminal=True))
    accept_any_text_on_output()
    assert (out.encoding, out.errors) == ("utf-8", "replace")


def test_each_stream_is_judged_on_whether_it_is_itself_a_terminal(monkeypatch):
    # ``2> err.log`` typed at a console: stdout is the terminal, stderr is not.
    out = _stream("cp950", "strict", terminal=True)
    err = _stream("cp950", "backslashreplace")
    _install(monkeypatch, out, err)
    accept_any_text_on_output()
    assert (out.encoding, out.errors) == ("cp950", "backslashreplace")
    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace")


def test_an_empty_variable_is_no_choice_as_it_is_none_to_the_interpreter(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "")
    out = _install(monkeypatch, _stream("cp950"))
    accept_any_text_on_output()
    assert (out.encoding, out.errors) == ("utf-8", "backslashreplace")


def test_an_encoding_the_operator_chose_is_kept_and_only_made_unable_to_raise(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp950")
    out = _install(monkeypatch, _stream("cp950", "strict"))
    accept_any_text_on_output()
    print(f"refused: {_EMOJI}")
    assert _written(out) == b"refused: \\U0001f600\n"
    assert out.encoding == "cp950"


def test_a_handler_the_operator_chose_is_kept_too(monkeypatch):
    monkeypatch.setenv("PYTHONIOENCODING", "cp950:replace")
    out = _install(monkeypatch, _stream("cp950", "replace"))
    accept_any_text_on_output()
    print(_EMOJI)
    assert _written(out) == b"?\n"
    assert (out.encoding, out.errors) == ("cp950", "replace")


# -- streams it leaves alone, and must not be stopped by ---------------------------


@pytest.mark.parametrize(
    "stand_in",
    [None, io.StringIO()],
    ids=["no-console", "a-capture-with-no-reconfigure"],
)
def test_a_stream_that_is_no_text_wrapper_is_passed_over_and_the_other_still_set(
    monkeypatch, stand_in
):
    err = _stream("cp950", "strict")
    monkeypatch.setattr(sys, "stdout", stand_in)
    monkeypatch.setattr(sys, "stderr", err)
    accept_any_text_on_output()  # does not raise
    assert sys.stdout is stand_in
    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace")


class _Refuses(io.TextIOWrapper):
    """A stream whose ``reconfigure`` fails the way the given exception says."""

    def __init__(self, error: Exception) -> None:
        super().__init__(io.BytesIO(), encoding="cp950", errors="strict")
        self._error = error

    def reconfigure(self, **_changes) -> None:
        raise self._error


@pytest.mark.parametrize(
    "error",
    [ValueError("closed"), BrokenPipeError("the reader has gone"), TypeError("no such keyword")],
    ids=["closed", "broken-pipe", "odd-stand-in"],
)
def test_a_stream_that_refuses_is_passed_over_and_the_other_still_set(monkeypatch, error):
    # A courtesy must not be what stops the command, whatever the refusal.
    err = _stream("cp950", "strict")
    monkeypatch.setattr(sys, "stdout", _Refuses(error))
    monkeypatch.setattr(sys, "stderr", err)
    accept_any_text_on_output()
    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace")


def test_a_closed_stream_does_not_stop_it(monkeypatch):
    closed = _stream("cp950")
    closed.close()
    err = _stream("cp950", "strict")
    monkeypatch.setattr(sys, "stdout", closed)
    monkeypatch.setattr(sys, "stderr", err)
    accept_any_text_on_output()
    assert (err.encoding, err.errors) == ("utf-8", "backslashreplace")


def test_calling_it_again_changes_nothing(monkeypatch):
    out = _install(monkeypatch, _stream("cp950"))
    accept_any_text_on_output()
    accept_any_text_on_output()
    print(_EMOJI)
    assert _written(out) == (_EMOJI + "\n").encode("utf-8")
