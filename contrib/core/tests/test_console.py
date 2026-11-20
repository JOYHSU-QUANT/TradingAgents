"""Tests for the console that escapes what it cannot encode (issue #430).

The command lines that call it pin THAT they do, on their own output
(``contrib/autoresearch/tests/test_hypothesis.py``, ``contrib/discovery/tests/
test_cli.py``); this file pins what the call does to a stream.
"""

from __future__ import annotations

import io
import sys

import pytest

from contrib.core.console import escape_unencodable_output

_EMOJI = "\U0001f600"  # outside every legacy Windows code page
_HALF = "\ud83d"  # half of its UTF-16 pair: outside UTF-8 too


def _stream(encoding: str, errors: str = "strict") -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors, newline="")


def _written(stream: io.TextIOWrapper) -> bytes:
    stream.flush()
    return stream.buffer.getvalue()


@pytest.fixture
def console(monkeypatch):
    """Stand ``sys.stdout`` and ``sys.stderr`` on fresh strict streams of an encoding."""

    def install(encoding: str) -> tuple[io.TextIOWrapper, io.TextIOWrapper]:
        out, err = _stream(encoding), _stream(encoding)
        monkeypatch.setattr(sys, "stdout", out)
        monkeypatch.setattr(sys, "stderr", err)
        return out, err

    return install


def test_a_strict_legacy_console_raises_on_an_emoji_which_is_the_defect(console):
    out, _ = console("cp950")
    with pytest.raises(UnicodeEncodeError):
        print(f"refused: {_EMOJI}")
        out.flush()


def test_what_the_console_cannot_encode_is_written_as_its_escape(console):
    out, err = console("cp950")
    escape_unencodable_output()
    print(f"refused: {_EMOJI}")
    print(f"error: {_EMOJI}", file=sys.stderr)
    assert _written(out) == b"refused: \\U0001f600\n"
    assert _written(err) == b"error: \\U0001f600\n"


def test_what_the_console_can_encode_is_written_as_it_was(console):
    # The page's own characters are untouched, beside one that is escaped.
    out, _ = console("cp950")
    escape_unencodable_output()
    text = "\u898f\u5247 rule \u2014 ok"
    print(text + _EMOJI)
    assert _written(out) == text.encode("cp950") + b"\\U0001f600\n"


def test_a_utf8_console_is_changed_for_nothing_but_a_lone_surrogate(console):
    out, _ = console("utf-8")
    escape_unencodable_output()
    print(f"refused: {_EMOJI}")
    assert _written(out) == f"refused: {_EMOJI}\n".encode()
    print(f"half: {_HALF}")
    assert _written(out).endswith(b"half: \\ud83d\n")


def test_the_mode_is_set_and_nothing_else_about_the_stream_is(console):
    out, _ = console("cp950")
    escape_unencodable_output()
    assert out.errors == "backslashreplace"
    assert out.encoding == "cp950"


@pytest.mark.parametrize(
    "stand_in",
    [None, io.StringIO()],
    ids=["no-console", "a-capture-with-no-reconfigure"],
)
def test_a_stream_that_is_no_text_wrapper_is_left_alone(monkeypatch, stand_in):
    monkeypatch.setattr(sys, "stdout", stand_in)
    monkeypatch.setattr(sys, "stderr", stand_in)
    escape_unencodable_output()  # does not raise
    assert sys.stdout is stand_in and sys.stderr is stand_in


def test_a_stream_that_refuses_is_skipped_and_the_other_is_still_set(monkeypatch):
    # A closed stream cannot be reconfigured. The courtesy must not be what
    # stops the command — and must still reach the stream that can take it.
    closed = _stream("cp950")
    closed.close()
    live = _stream("cp950")
    monkeypatch.setattr(sys, "stdout", closed)
    monkeypatch.setattr(sys, "stderr", live)
    escape_unencodable_output()
    assert live.errors == "backslashreplace"


def test_calling_it_again_changes_nothing(console):
    out, _ = console("cp950")
    escape_unencodable_output()
    escape_unencodable_output()
    print(_EMOJI)
    assert _written(out) == b"\\U0001f600\n"
