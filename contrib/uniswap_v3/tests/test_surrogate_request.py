"""Text that cannot be sent (issue #441): ``verdict`` end to end, with only the transport faked.

An analyst's report can hold a lone surrogate, and every later node of the
judge's graph quotes that report in its prompt. Building the first such
request raised ``UnicodeEncodeError`` inside the provider's SDK before
anything was sent. The visit took that for a failure that asking again
would not help: exit 1, the analysts already paid for, and no verdict for
that token at that bar, on that visit and on every later one. The
engine's clients now send U+FFFD in its place
(``tradingagents.llm_clients.base_client.sendable``).

Everything here is the command's own: ``cli.main``, the real
``TradingAgentsJudge`` over the engine's real graph, the engine's real
chat client, the provider's SDK and its HTTP library, the real store and
sidecars. The fakes are the transport under that library, which answers
each request with a canned completion and keeps the bytes it was sent,
and the data vendors behind the engine's router, which answer a canned
line; no socket is opened, and the key is a dummy.

The character reaches the reply in the two ways a provider can deliver it:
the response body carries the lone surrogate itself, or the body is ASCII
and its JSON spells it as an escape that the client decodes.

Every such character here is built with ``chr``: a source file cannot hold
one, and an escape for one is easy to get wrong without noticing. The
engine and the libraries under it are imported inside the fixtures, as
elsewhere in this suite, so collecting this file does not wait on them.
"""

from __future__ import annotations

import copy
import importlib
import json
import logging
import re
import socket
from pathlib import Path
from typing import Any

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.node import DAY, DEFAULT_TICK, FIRST_DAY, put_day
from contrib.uniswap_v3.tests.fakes.verdicts import judged_config

HIGH = chr(0xD83D)  # the first half of a UTF-16 pair, on its own
FFFD = chr(0xFFFD)
ESCAPED = chr(0x5C) + "ud83d"  # the six ASCII characters JSON writes for it
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_KEY = "sk-test-not-a-key"
_CLIENT_LOGGER = "tradingagents.llm_clients.base_client"

_SOURCE = "judge-v1"
_DAY2 = FIRST_DAY + 2 * DAY
_NOW = _DAY2 + 600
# The two tokens of the example config, in the order the visit asks about
# them; the second is the one whose analyst writes the character.
_TICKERS = {"BTC-USD": "WBTC", "ETH-USD": "WETH"}
_ODD_TICKER = "ETH-USD"
# The judge's graph, in the order one question runs it under the default
# settings: three analysts (each answering without calling a tool), one
# round of the research debate, its manager, the trader, one round of the
# risk debate, the portfolio manager.
_ANALYST = "Market Analyst"
_MANAGER = "Portfolio Manager"
_NODES = [
    _ANALYST,
    "Sentiment Analyst",
    "News Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Aggressive Analyst",
    "Conservative Analyst",
    "Neutral Analyst",
    _MANAGER,
]
# Two of them: a replacement that stopped at the first would leave one in the request.
_ODD_REPORT = f"{_ANALYST} says: momentum {HIGH} holds, then {HIGH} fades."
_SENT_REPORT = _ODD_REPORT.replace(HIGH, FFFD)
# The one structured call the graph makes: the sentiment analyst binds its
# report's schema as a tool and is answered with a call of it.
_SENTIMENT_SCHEMA = "SentimentReport"


def _note(count: int) -> str:
    return (
        f"{count} lone surrogate(s) in a model request were replaced with U+FFFD before "
        "sending: none can be written as UTF-8 (text a graph keeps is counted by each request quoting it)"
    )


# -- the provider, faked at the transport ---------------------------------------------------------
#
# ``_http``, ``_response_bytes`` and ``_Wire`` are repeated in the two other
# packages' tests of this issue (``test_surrogate_request.py`` under
# ``contrib/replay/tests/`` and ``contrib/hyperliquid_perp/tests/integration/``)
# on purpose: this package imports no other package under ``contrib/``
# (``test_isolation.py``), and the other two do not import each other's
# tests. A change here belongs in all three.


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


# -- the fixture: the engine with nothing to reach but the wire -----------------------------------


@pytest.fixture
def offline(monkeypatch) -> list[str]:
    """No connection leaves the machine and the key is a dummy; what was refused is kept.

    Every data source the judge's graph can reach answers a canned line:
    the router stays the engine's own, and each vendor function in its
    table is replaced, as are the two feeds and the identity look-up the
    graph reads without the router. The engine's dataflows config, which
    building a graph writes to, is put back after the test.
    """
    pytest.importorskip("langchain_openai")
    import tradingagents.agents.analysts.sentiment_analyst as sentiment
    import tradingagents.dataflows.config as engine_config
    import tradingagents.dataflows.interface as interface
    import tradingagents.default_config as engine_defaults
    import tradingagents.graph.trading_graph as trading_graph

    # The judge takes its two round counts from the engine's defaults, which
    # the environment (or a .env found from the working directory) can set:
    # one round each here, whatever is set there, so the graph has the
    # eleven nodes ``_NODES`` lists.
    for key in ("max_debate_rounds", "max_risk_discuss_rounds"):
        monkeypatch.setitem(engine_defaults.DEFAULT_CONFIG, key, 1)

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

    def refused_curl(self, *args, **kwargs):
        # curl_cffi does not go through Python's sockets.
        refused.append(f"curl_cffi {args[:2]!r}")
        raise OSError("the test is offline: curl_cffi request refused")

    def vendor(method: str) -> Any:
        return lambda *args, **kwargs: f"(stub {method}: nothing to report)"

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_lookup)
    try:
        import curl_cffi.requests as curl_requests
    except ImportError:
        pass
    else:
        monkeypatch.setattr(curl_requests.Session, "request", refused_curl)
    monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)
    monkeypatch.setattr(engine_config, "_config", copy.deepcopy(engine_config._config))
    for method, vendors in interface.VENDOR_METHODS.items():
        for name in vendors:
            monkeypatch.setitem(vendors, name, vendor(method))
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", vendor("reddit"))
    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", vendor("stocktwits"))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda ticker: {})
    return refused


def _site(directory: Path) -> tuple[Path, Path]:
    """A config with its verdicts section switched on and a store with three days of bars."""
    directory.mkdir()
    config = judged_config(directory, _SOURCE)
    db = directory / "data" / "store.db"
    db.parent.mkdir()
    with open_store(db) as store:
        for day in range(3):
            put_day(store, day, eth_tick=DEFAULT_TICK + day * 100)
    return config, db


def _ticker(sent: str) -> str:
    """The token a request is about: the engine names it in backticks in every prompt."""
    (ticker,) = (ticker for ticker in _TICKERS if f"`{ticker}`" in sent)
    return ticker


def _sentiment_call() -> dict:
    arguments = {
        "overall_band": "Neutral",
        "overall_score": 5.0,
        "confidence": "low",
        "narrative": "Sentiment narrative from two sources.",
    }
    call = {"name": _SENTIMENT_SCHEMA, "arguments": json.dumps(arguments)}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_test", "type": "function", "function": call}],
    }


def _provider(form: str) -> _Wire:
    """The provider: one plain line from every node, the manager's ending in a rating.

    With ``form`` ``raw`` or ``escape`` the market analyst's line on the
    second token holds the lone surrogate, delivered that way; ``clean`` is
    the same visit with no such character in it.
    """

    def reply(node: str | None, sent: str) -> str | dict:
        tools = json.loads(sent).get("tools") or []
        if any(tool["function"]["name"] == _SENTIMENT_SCHEMA for tool in tools):
            return _sentiment_call()
        if node == _ANALYST and _ticker(sent) == _ODD_TICKER and form != "clean":
            return _ODD_REPORT
        line = f"{node} says: nothing odd."
        return f"{line}\n\nRating: Hold" if node == _MANAGER else line

    return _Wire(reply, form=form)


def _visit(site: tuple[Path, Path], monkeypatch, wire: _Wire) -> tuple[int, list[str]]:
    """Run ``verdict`` on ``site`` against ``wire``: its exit code and what it printed."""
    import tradingagents.graph.trading_graph as trading_graph

    # The judge and its graph are the command's own; the engine's client
    # factory, where the graph reads it, is the one seam taken.
    config, db = site
    lines: list[str] = []
    with monkeypatch.context() as scoped:
        scoped.setattr(
            trading_graph, "create_llm_client", wire.wired(trading_graph.create_llm_client)
        )
        code = cli.main(
            ["verdict", "--config", str(config), "--db", str(db)],
            out=lines.append,
            now=lambda: float(_NOW),
        )
    assert wire.keys <= {f"Bearer {_KEY}"}
    return code, lines


def _asked(wire: _Wire) -> list[tuple[str, str | None]]:
    """``(token, node)`` for every request that reached the transport, in order."""
    return [(_ticker(text), node) for node, text in wire.sent()]


def _stored(site: tuple[Path, Path]) -> tuple[list[tuple[str, str]], int]:
    """The ``(symbol, rating)`` of every verdict stored at the bar, and how many declines are."""
    with open_store(site[1], create=False) as store:
        records = store.verdicts_at(_SOURCE, _DAY2)
        declines = store.judge_declines_at(_SOURCE, _DAY2)
    return [(r.verdict.symbol, r.verdict.rating.value) for r in records], len(declines)


def _client_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _CLIENT_LOGGER and record.levelno == logging.WARNING
    ]


# -- the visit goes on ----------------------------------------------------------------------------


@pytest.mark.parametrize("form", ["raw", "escape"])
def test_an_analysts_report_holding_one_is_sent_on_as_u_fffd_and_the_token_is_rated(
    form, tmp_path, monkeypatch, offline, caplog
):
    site, twin = _site(tmp_path / "odd"), _site(tmp_path / "twin")
    odd, clean = _provider(form), _provider("clean")
    with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
        # The same visit with no such character in it: sent as it is, nothing said.
        assert _visit(twin, monkeypatch, clean)[0] == cli.EXIT_OK
        assert _client_warnings(caplog) == []
        assert not any(FFFD in text for _node, text in clean.sent())
        code, lines = _visit(site, monkeypatch, odd)
    assert code == cli.EXIT_OK and lines[-1] == "2 asked, 0 already stored"
    # Not one request more than the clean visit made: each token asked of
    # the graph's eleven nodes once.
    assert _asked(odd) == _asked(clean) == [(t, node) for t in _TICKERS for node in _NODES]
    # Every node that quotes the analyst's report comes after the analysts,
    # and its request, as the transport received it, holds U+FFFD where the
    # character stood (``sent`` has checked that none holds the character itself).
    sent = odd.sent()
    readers = [(_ticker(text), node) for node, text in sent if _SENT_REPORT in text]
    assert readers and all(
        ticker == _ODD_TICKER and _NODES.index(node) >= 3 for ticker, node in readers
    )
    # One warning per request it was replaced in, each with how many.
    replaced = [text.count(FFFD) for _node, text in sent if FFFD in text]
    assert replaced and _client_warnings(caplog) == [_note(count) for count in replaced]
    # Both tokens are rated at the bar, as on the clean visit, and nothing declined.
    assert _stored(site) == _stored(twin) == ([("WBTC", "Hold"), ("WETH", "Hold")], 0)
    # The report is in the sidecar with U+FFFD in it, and it was the write
    # that put it there (issue #429): the graph's own state was not changed,
    # so the answer arrived holding the characters, and the writer said so.
    sidecar = site[1].parent / "verdicts" / _SOURCE / "WETH-20240103T000000Z.json"
    kept = json.loads(sidecar.read_bytes().decode("utf-8"))
    assert kept["reports"]["market_report"] == _SENT_REPORT
    (said,) = [
        record.getMessage()
        for record in caplog.records
        if record.name == "contrib.uniswap_v3.agent.verdicts" and record.levelno == logging.WARNING
    ]
    assert said.startswith(
        f"{_ODD_REPORT.count(HIGH)} lone surrogate(s) in the judge's words on WETH at "
    )
    assert offline == []


# -- the control: what the same visit did before --------------------------------------------------


def test_without_the_replacement_the_same_visit_fails_and_the_token_is_left_unrated(
    tmp_path, monkeypatch, offline, capsys
):
    # Why the fix exists, and a tripwire: with the replacement taken out of
    # the client, the visit above is the failure issue #441 measured. Should
    # the SDK ever send such text instead of raising, this is the test that
    # says so.
    import tradingagents.llm_clients.openai_client as openai_client

    monkeypatch.setattr(openai_client, "sendable", lambda input: input)
    site, wire = _site(tmp_path / "odd"), _provider("escape")
    code, _lines = _visit(site, monkeypatch, wire)
    assert code == cli.EXIT_FAILED
    # The three analysts were asked, and paid; the first node that quotes
    # the report never got a request out.
    assert _asked(wire) == [("BTC-USD", node) for node in _NODES] + [
        (_ODD_TICKER, node) for node in _NODES[:3]
    ]
    # The token asked about first is rated; this one is not.
    assert _stored(site)[0] == [("WBTC", "Hold")]
    assert "the judge failed on ETH-USD for good (UnicodeEncodeError" in capsys.readouterr().err
    assert offline == []
