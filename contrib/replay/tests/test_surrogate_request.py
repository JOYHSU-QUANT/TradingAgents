"""Text that cannot be sent (issue #441): ``backtest --ask`` end to end, with only the transport faked.

An earlier node's reply can hold a lone surrogate, and the next node's
prompt quotes it. Building that request raised ``UnicodeEncodeError``
inside the provider's SDK before anything was sent. The walk read that as
a failure that might pass: it paused, ran the lower graph again from the
Trader (paying for it again), three times, and stopped with exit 1; a
resume did the same on the same question. The engine's clients now send
U+FFFD in its place (``tradingagents.llm_clients.base_client.sendable``).

Everything here is the command's own: ``cli.main``, the real
``LowerGraph`` on the real engine nodes, the engine's real chat client, the
provider's SDK and its HTTP library, the real stores. The one fake is the
transport under that library, which answers each request with a canned
completion and keeps the bytes it was sent; no socket is opened, and the
key is a dummy. Beside it the command's pause between tries is recorded
rather than waited for, and the client is handed the dummy key and a
callback that notes which node is asking.

The character reaches the reply in the two ways a provider can deliver it:
the response body carries the lone surrogate itself, or the body is ASCII
and its JSON spells it as an escape that the client decodes.

Every such character here is built with ``chr``: a source file cannot hold
one, and an escape for one is easy to get wrong without noticing.
"""

from __future__ import annotations

import copy
import importlib
import json
import logging
import re
import socket
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from contrib.replay import cli
from contrib.replay.replay_store import ReplayStore
from contrib.replay.tests import papers
from contrib.replay.tests.test_backtest_cli import GATE_MARKS, write_research

HIGH = chr(0xD83D)  # the first half of a UTF-16 pair, on its own
FFFD = chr(0xFFFD)
ESCAPED = chr(0x5C) + "ud83d"  # the six ASCII characters JSON writes for it
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_KEY = "sk-test-not-a-key"
_CLIENT_LOGGER = "tradingagents.llm_clients.base_client"

TARGET = 1  # the fixture question whose Trader writes the character
TARGET_ID = papers.input_id(TARGET)
# The lower graph's nodes, in the order one question runs them
# (``lower_graph.py``): the risk debate is one round of three.
_TRADER = "Trader"
_NEXT = "Aggressive Analyst"  # the first node that reads the Trader's plan
_MANAGER = "Portfolio Manager"
_NODES = [_TRADER, _NEXT, "Conservative Analyst", "Neutral Analyst", _MANAGER]
# Two of them: a replacement that stopped at the first would leave one in the request.
_ODD_PLAN = f"{_TRADER} says: momentum {HIGH} holds, then {HIGH} fades."
_SENT_PLAN = _ODD_PLAN.replace(HIGH, FFFD)


def _note(count: int) -> str:
    return (
        f"{count} lone surrogate(s) in a model request were replaced with U+FFFD before "
        "sending: none can be written as UTF-8 (text a graph keeps is counted by each request quoting it)"
    )


# -- the provider, faked at the transport ---------------------------------------------------------
#
# ``_http``, ``_response_bytes`` and ``_Wire`` are repeated in the two other
# packages' tests of this issue (``test_surrogate_request.py`` under
# ``contrib/hyperliquid_perp/tests/integration/`` and
# ``contrib/uniswap_v3/tests/``) on purpose: the packages do not import each
# other's tests, and uniswap_v3 imports no other contrib package at all. A
# change here belongs in all three.


def _http() -> Any:
    """The HTTP library the provider's SDK sends through: ``httpx``, or its successor.

    Read off the SDK's own default client, so the fake transport is of the
    library the installed SDK is built on, whichever generation that is.
    """
    import openai

    return importlib.import_module(openai.DefaultHttpxClient.__mro__[1].__module__.split(".")[0])


def _response_bytes(payload: dict, form: str) -> bytes:
    """``payload`` as a provider's response body: the surrogate itself (``raw``) or its escape."""
    if form == "raw":
        return json.dumps(payload, ensure_ascii=False).encode("utf-8", "surrogatepass")
    return json.dumps(payload, ensure_ascii=True).encode("ascii")


class _Wire:
    """Answers every chat-completion request with ``reply``'s words, and keeps what it was sent.

    ``reply(node, sent)`` gets the graph node that is asking and the request
    body as text, and returns the reply's text or a whole ``message`` (for a
    tool call). ``requests`` holds ``(node, body bytes)`` for every request
    that reached the transport, in order: nothing that failed before it was
    sent is in it. The engine is imported where it is used, so collecting
    this file costs nothing.
    """

    def __init__(self, reply: Any, *, form: str) -> None:
        self.reply = reply
        self.form = form
        self.node: str | None = None
        self.requests: list[tuple[str | None, bytes]] = []
        self.keys: set[str | None] = set()

    def _answer(self, request: Any) -> Any:
        sent = request.content
        self.requests.append((self.node, sent))
        self.keys.add(request.headers.get("authorization"))
        said = self.reply(self.node, sent.decode("utf-8"))
        message = said if isinstance(said, dict) else {"role": "assistant", "content": said}
        payload = {
            "id": f"gen-test-{len(self.requests)}",
            "object": "chat.completion",
            "created": 1,
            "model": json.loads(sent).get("model", "test"),
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                    "message": message,
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
        }
        return _http().Response(
            200,
            content=_response_bytes(payload, self.form),
            headers={"content-type": "application/json"},
        )

    def _watch(self) -> Any:
        """A callback that notes which graph node is asking, read off langgraph's metadata."""
        from langchain_core.callbacks import BaseCallbackHandler

        wire = self

        class Watch(BaseCallbackHandler):
            def on_chat_model_start(self, serialized, messages, *, metadata=None, **kwargs):
                wire.node = (metadata or {}).get("langgraph_node")

        return Watch()

    def wired(self, create_llm_client: Any) -> Any:
        """The engine's client factory, every client it builds talking to this wire with a dummy key."""
        http = _http()

        def create(*args: Any, **kwargs: Any) -> Any:
            kwargs["http_client"] = http.Client(transport=http.MockTransport(self._answer))
            kwargs["api_key"] = _KEY
            kwargs["callbacks"] = [*(kwargs.get("callbacks") or []), self._watch()]
            return create_llm_client(*args, **kwargs)

        return create

    def sent(self) -> list[tuple[str | None, str]]:
        """``(node, what it sent)`` per request, the body read back as one text.

        Each is checked on the way: the bytes are UTF-8 (a lone surrogate
        is not), and no string of the JSON holds one either (an escape in
        the body would decode into one).
        """
        found = []
        for node, body in self.requests:
            text = json.dumps(json.loads(body.decode("utf-8")), ensure_ascii=False)
            assert not _SURROGATE.search(text)
            found.append((node, text))
        return found


def test_the_characters_and_the_two_bodies_are_what_they_are_meant_to_be():
    # No product code: a test about a lone surrogate that held something
    # else, or reached the client some other way, would pass for the wrong reason.
    assert len(HIGH) == 1 and 0xD800 <= ord(HIGH) <= 0xDFFF and _SURROGATE.fullmatch(HIGH)
    with pytest.raises(UnicodeEncodeError):
        HIGH.encode("utf-8")
    assert ESCAPED.isascii() and len(ESCAPED) == 6 and json.loads(f'"{ESCAPED}"') == HIGH
    assert FFFD.encode("utf-8") == b"\xef\xbf\xbd" and not _SURROGATE.search(FFFD)
    raw, escaped = (_response_bytes({"content": HIGH}, form) for form in ("raw", "escape"))
    assert raw == b'{"content": "\xed\xa0\xbd"}'
    assert escaped == b'{"content": "' + ESCAPED.encode("ascii") + b'"}'
    # Either body is the same reply once a client has read it.
    assert json.loads(raw) == json.loads(escaped) == {"content": HIGH}


# -- the fixture: one directory the command runs in, offline --------------------------------------


def _work(directory: Path) -> Path:
    directory.mkdir()
    papers.write_gate_store(directory / "paper_trading.db", reports=True)
    papers.write_graph_variant(directory, "g")
    write_research(directory / "autoresearch.sqlite", GATE_MARKS, slots=12)
    with ReplayStore(directory / "replay.sqlite", create=True):
        pass
    return directory


@pytest.fixture
def offline(monkeypatch) -> list[str]:
    """No connection leaves the machine and the key is a dummy; what was refused is kept.

    The engine's dataflows config, which building the graph writes to, is
    put back after the test.
    """
    import tradingagents.dataflows.config as engine_config

    refused: list[str] = []
    connect, lookup = socket.socket.connect, socket.getaddrinfo
    local = ("127.0.0.1", "::1", "localhost", None)

    def guarded_connect(self, address):
        # A path (a Unix socket) is this machine's own; so is loopback.
        if not isinstance(address, tuple) or address[0] in local:
            return connect(self, address)
        refused.append(f"connect {address!r}")
        raise OSError(f"the test is offline: connect to {address!r} refused")

    def guarded_lookup(host, *args, **kwargs):
        if host in local:
            return lookup(host, *args, **kwargs)
        refused.append(f"getaddrinfo {host!r}")
        raise socket.gaierror(f"the test is offline: lookup of {host!r} refused")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_lookup)
    monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)
    monkeypatch.setattr(engine_config, "_config", copy.deepcopy(engine_config._config))
    return refused


def _argv(work: Path) -> list[str]:
    return [
        "backtest",
        "--db",
        str(work / "paper_trading.db"),
        "--run-id",
        papers.RUN_ID,
        "--research-db",
        str(work / "autoresearch.sqlite"),
        "--replay-db",
        str(work / "replay.sqlite"),
        "--ask",
        str(work / "g.yaml"),
        "--repeats",
        "1",
    ]


def _slot(sent: str) -> int:
    (slot,) = (paper.slot for paper in papers.PAPERS if papers.question_line(paper.slot) in sent)
    return slot


def _provider(form: str) -> _Wire:
    """The provider: the paper's own decision from the manager, one plain line from every other node.

    With ``form`` ``raw`` or ``escape`` the Trader's line on the target
    question holds the lone surrogate, delivered that way; ``clean`` is the
    same run with no such character in it.
    """

    def reply(node: str | None, sent: str) -> str:
        slot = _slot(sent)
        if node == _MANAGER:
            return papers.PAPERS[slot].text
        if node == _TRADER and slot == TARGET and form != "clean":
            return _ODD_PLAN
        return f"{node} says: nothing odd."

    return _Wire(reply, form=form)


def _ask(work: Path, monkeypatch, wire: _Wire) -> tuple[int, list[float]]:
    """Run the command in ``work`` against ``wire``: its exit code and the pauses it asked for."""
    # Imported before the factory is replaced, not for a name of its own: this
    # module binds ``create_llm_client`` to itself when it is first imported,
    # and the command below imports the engine. First imported inside the
    # ``with``, it would keep this test's factory after the ``with`` ended,
    # and every graph built later in the process would ask this test's wire.
    import tradingagents.graph.trading_graph  # noqa: F401
    import tradingagents.llm_clients as llm_clients

    # ``cli._build_graph`` stays the real ``LowerGraph``, which loads the
    # engine itself; the engine's client factory is the one seam taken.
    pauses: list[float] = []
    with monkeypatch.context() as scoped:
        scoped.setattr(llm_clients, "create_llm_client", wire.wired(llm_clients.create_llm_client))
        scoped.setattr(cli, "_sleep", pauses.append)
        code = cli.main(_argv(work))
    assert wire.keys <= {f"Bearer {_KEY}"}
    return code, pauses


def _asked(wire: _Wire) -> list[tuple[int, str | None]]:
    """``(question, node)`` for every request that reached the transport, in order."""
    return [(_slot(text), node) for node, text in wire.sent()]


def _rows(work: Path, table: str) -> list[dict]:
    """``table`` of the replay store in ``work``, without the instants a row was written at."""
    conn = sqlite3.connect(work / "replay.sqlite")
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY input_id")]
    finally:
        conn.close()
    return [{k: v for k, v in row.items() if k not in ("asked_at", "failed_at")} for row in rows]


def _client_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _CLIENT_LOGGER and record.levelno == logging.WARNING
    ]


# -- the walk goes on -----------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["raw", "escape"])
def test_a_traders_reply_holding_one_is_sent_on_as_u_fffd_and_the_walk_goes_on(
    form, tmp_path, monkeypatch, offline, caplog
):
    pytest.importorskip("langchain_openai")
    work, twin = _work(tmp_path / "odd"), _work(tmp_path / "twin")
    odd, clean = _provider(form), _provider("clean")
    with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
        # The same walk with no such character in it: sent as it is, nothing said.
        assert _ask(twin, monkeypatch, clean) == (0, [])
        assert _client_warnings(caplog) == []
        assert not any(FFFD in text for _node, text in clean.sent())
        code, pauses = _ask(work, monkeypatch, odd)
    # The command finished, and it never paused: nothing was tried again.
    assert (code, pauses) == (0, [])
    # Not one request more than the clean walk made: the eight questions
    # before the holdout lock, each asked of the five nodes once.
    assert _asked(odd) == _asked(clean) == [(slot, node) for slot in range(8) for node in _NODES]
    # The next node's request, as the transport received it, quotes the
    # Trader's plan with U+FFFD where the character stood (``sent`` has
    # checked that no request holds the character itself).
    sent = odd.sent()
    (quoted,) = (text for node, text in sent if node == _NEXT and _SENT_PLAN in text)
    assert papers.question_line(TARGET) in quoted
    # One warning per request it was replaced in, each with how many.
    replaced = [text.count(FFFD) for _node, text in sent if FFFD in text]
    assert replaced and _client_warnings(caplog) == [_note(count) for count in replaced]
    # The answer is stored, and every table holds what the clean walk's
    # holds: no failure, and no tokens of a run that had to be made twice.
    assert TARGET_ID in [row["input_id"] for row in _rows(work, "answers")]
    assert _rows(work, "failures") == []
    for table in ("answers", "graph_books"):
        assert _rows(work, table) and _rows(work, table) == _rows(twin, table)
    assert offline == []


# -- the control: what the same run did before ----------------------------------------------------


def test_without_the_replacement_the_same_walk_pays_three_times_and_stops(
    tmp_path, monkeypatch, offline, capsys
):
    # Why the fix exists, and a tripwire: with the replacement taken out of
    # the client, the run above is the failure issue #441 measured. Should
    # the SDK ever send such text instead of raising, this is the test that
    # says so.
    pytest.importorskip("langchain_openai")
    import tradingagents.llm_clients.openai_client as openai_client

    monkeypatch.setattr(openai_client, "sendable", lambda input: input)
    work, wire = _work(tmp_path / "odd"), _provider("escape")
    code, pauses = _ask(work, monkeypatch, wire)
    assert code == 1
    # Tried three times, a pause before each of the two retries ...
    assert pauses == [5.0, 20.0]
    # ... each try running the Trader again, and paying for it; the node
    # that reads its plan never got a request out.
    asked = _asked(wire)
    assert asked == [(0, node) for node in _NODES] + [(TARGET, _TRADER)] * 3
    # The question before it is stored; this one and every later one are not.
    assert [row["input_id"] for row in _rows(work, "answers")] == [papers.input_id(0)]
    assert "UnicodeEncodeError" in capsys.readouterr().err
    assert offline == []
