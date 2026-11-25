"""Text that cannot be sent (issue #441): one paper cycle end to end, with only the transport faked.

An analyst's report, a tool's result or a vendor's text can hold a lone
surrogate, and a later request of the engine's graph quotes it. Building
that request raised ``UnicodeEncodeError`` inside the provider's SDK
before anything was sent. The decision provider filed that as a failure
that might pass, so the scheduler ran the whole graph again from the
first analyst (paying for it again) on each of its three tries and closed
the cycle as ``api_failed``: no decision, no plan. The engine's clients
now send U+FFFD in its place
(``tradingagents.llm_clients.base_client.sendable``).

Everything here is the daemon's own: the real ``PaperScheduler`` and paper
engine over a real store, the provider the daemon builds
(``build_decision_provider``) with its real ``build_input`` and
``request_decision``, the engine's real graph and chat client, the
provider's SDK and its HTTP library. The fakes are the transport under
that library, which answers each request with a canned completion and
keeps the bytes it was sent; the exchange read behind ``build_input`` (a
fixture market context); the data vendors behind the engine's router,
which answer a canned line; the paper engine's marks; and the clock, so
no retry wait is waited. No socket is opened, and the key is a dummy.

The character reaches a reply in the two ways a provider can deliver it:
the response body carries the lone surrogate itself, or the body is ASCII
and its JSON spells it as an escape that the client decodes.

Every such character here is built with ``chr``: a source file cannot hold
one, and an escape for one is easy to get wrong without noticing.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import importlib
import json
import logging
import re
import socket
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from contrib.hyperliquid_perp import engine_bridge
from contrib.hyperliquid_perp.domains.perp.risk_gate import DecisionConfig, RiskConfig
from contrib.hyperliquid_perp.integration.decision_provider import build_decision_provider
from contrib.hyperliquid_perp.paper.config import PaperTradingConfig
from contrib.hyperliquid_perp.paper.engine import PaperExecutionEngine
from contrib.hyperliquid_perp.paper.scheduler import CycleEvent, PaperScheduler, PollResult
from contrib.hyperliquid_perp.persistence.db import Database
from contrib.hyperliquid_perp.runtime import accounting
from contrib.hyperliquid_perp.runtime.asset_spec import AssetSpec
from contrib.hyperliquid_perp.runtime.clock import ManualClock
from contrib.hyperliquid_perp.runtime.market_feed import ScriptedSnapshotProvider

from ..fakes.decisions import market_ctx
from ..fakes.market import margin_schedule, snap

HIGH = chr(0xD83D)  # the first half of a UTF-16 pair, on its own
FFFD = chr(0xFFFD)
ESCAPED = chr(0x5C) + "ud83d"  # the six ASCII characters JSON writes for it
_SURROGATE = re.compile(f"[{chr(0xD800)}-{chr(0xDFFF)}]")
_KEY = "sk-test-not-a-key"
_CLIENT_LOGGER = "tradingagents.llm_clients.base_client"

_T0 = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
# The engine block a daemon's config carries, spelled out so that nothing
# of it is left to the environment the suite runs in.
_CONFIG = {
    "engine": {
        "llm_provider": "openrouter",
        "deep_think_llm": "fixture/deep",
        "quick_think_llm": "fixture/quick",
        "selected_analysts": ["market", "social", "news"],
        "structured_output": False,
    }
}
# The engine's graph, in the order one cycle runs it with those analysts
# and one round of each debate: three analysts (each answering without
# calling a tool), the research debate, its manager, the trader, the risk
# debate, the portfolio manager.
_ANALYST = "Market Analyst"
_SENTIMENT = "Sentiment Analyst"
_MANAGER = "Portfolio Manager"
_NODES = [
    _ANALYST,
    _SENTIMENT,
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
_PRICE_TOOL = "get_stock_data"
_DECISION = {
    "decision_mode": "set_target",
    "target_side": "long",
    "requested_target_margin_pct": 10,
    "confidence": 0.8,
    "rationale": "the fixture says so",
    "key_risks": ["a risk"],
}


def _note(count: int) -> str:
    return (
        f"{count} lone surrogate(s) in a model request were replaced with U+FFFD before "
        "sending: none can be written as UTF-8 (text a graph keeps is counted by each request quoting it)"
    )


# -- the provider, faked at the transport ---------------------------------------------------------
#
# ``_http``, ``_response_bytes`` and ``_Wire`` are repeated in the two other
# packages' tests of this issue (``test_surrogate_request.py`` under
# ``contrib/replay/tests/`` and ``contrib/uniswap_v3/tests/``) on purpose:
# the packages do not import each other's tests, and uniswap_v3 imports no
# other contrib package at all. A change here belongs in all three.


def _http() -> Any:
    """The HTTP library the provider's SDK sends through: ``httpx``, or its successor.

    Read off the SDK's own default client, so the fake transport is of the
    library the installed SDK is built on, whichever generation that is.
    """
    import openai

    return importlib.import_module(openai.DefaultHttpxClient.__mro__[1].__module__.split(".")[0])


def _sdk_refuses_such_text() -> bool:
    """Whether the installed provider SDK sends nothing for text holding a lone surrogate.

    It sends nothing at every version this was written against (it raises
    while it builds the request), and that failure is what the control at
    the end of this file reproduces. CI does not run this suite: it runs
    where the trader is deployed, on whatever SDK is installed there, and a
    control failing there would stop a deploy over a failure that had gone
    away. Read off the transport and not off the exception's type: an SDK
    that fails another way before sending still has the failure.
    """
    import openai

    http = _http()
    reached: list[Any] = []

    def answer(request: Any) -> Any:
        reached.append(request)
        return http.Response(500)

    client = openai.OpenAI(
        api_key=_KEY,
        http_client=http.Client(transport=http.MockTransport(answer)),
        max_retries=0,
    )
    # Whatever it raises, or not: only whether a request went out is asked.
    with contextlib.suppress(Exception):
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": HIGH}])
    return not reached


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


# -- the fixture: a daemon with nothing to reach but the wire -------------------------------------


@pytest.fixture
def offline(monkeypatch, tmp_path) -> SimpleNamespace:
    """No connection leaves the machine and the key is a dummy; ``refused`` keeps what was refused.

    Every data source the engine's graph can reach answers a canned line,
    or what ``vendor_text`` holds for the method: the router stays the
    engine's own, and each vendor function in its table is replaced, as are
    the two feeds and the identity look-up the graph reads without the
    router. The exchange read behind ``build_input`` answers a fixture
    context. The engine writes its own files under the test's directory,
    its two debates are held to the one round the node list above assumes,
    and its dataflows config, which building a graph writes to, is put back
    after the test.
    """
    pytest.importorskip("langchain_openai")
    import tradingagents.agents.analysts.sentiment_analyst as sentiment
    import tradingagents.dataflows.config as engine_config
    import tradingagents.dataflows.interface as interface
    import tradingagents.default_config as engine_defaults
    import tradingagents.graph.trading_graph as trading_graph

    refused: list[str] = []
    vendor_text: dict[str, str] = {}
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
        return lambda *args, **kwargs: vendor_text.get(
            method, f"(stub {method}: nothing to report)"
        )

    def fixture_context(config, coin, **kwargs):
        # A healthy indicator set: ``build_input``'s guards refuse a context
        # without a usable ATR before any model is asked.
        indicators = {"rsi_14": 55.0, "ema_20": 50000.0, "ema_50": 49000.0, "atr_14": 1200.0}
        return dataclasses.replace(market_ctx(_T0), indicators=indicators), None

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
    for key, value in {
        "results_dir": str(tmp_path / "engine-logs"),
        "data_cache_dir": str(tmp_path / "engine-cache"),
        "max_debate_rounds": 1,
        "max_risk_discuss_rounds": 1,
        # Off, whatever the environment says: a retry that resumed from a
        # checkpoint would not ask the earlier nodes again, and the control
        # at the end of this file is about a cycle that does.
        "checkpoint_enabled": False,
    }.items():
        monkeypatch.setitem(engine_defaults.DEFAULT_CONFIG, key, value)
    for method, vendors in interface.VENDOR_METHODS.items():
        for name in vendors:
            monkeypatch.setitem(vendors, name, vendor(method))
    monkeypatch.setattr(sentiment, "fetch_reddit_posts", vendor("reddit"))
    monkeypatch.setattr(sentiment, "fetch_stocktwits_messages", vendor("stocktwits"))
    monkeypatch.setattr(trading_graph, "resolve_instrument_identity", lambda ticker: {})
    monkeypatch.setattr(engine_bridge, "_build_context", fixture_context)
    return SimpleNamespace(refused=refused, vendor_text=vendor_text)


def _tool_call(name: str, arguments: dict) -> dict:
    call = {"name": name, "arguments": json.dumps(arguments)}
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_test", "type": "function", "function": call}],
    }


def _provider(form: str, *, price_tool: bool = False) -> _Wire:
    """The provider: one plain line from every node, the manager's ending in a target block.

    With ``form`` ``raw`` or ``escape`` the market analyst's line holds the
    lone surrogate, delivered that way; ``clean`` is the same cycle with no
    such character in any reply. With ``price_tool`` the market analyst
    first asks for its price tool, and answers once it has the result.
    """

    def reply(node: str | None, sent: str) -> str | dict:
        asked = json.loads(sent)
        tools = [tool["function"]["name"] for tool in asked.get("tools") or []]
        if _SENTIMENT_SCHEMA in tools:
            report = {
                "overall_band": "Neutral",
                "overall_score": 5.0,
                "confidence": "low",
                "narrative": "Sentiment narrative from two sources.",
            }
            return _tool_call(_SENTIMENT_SCHEMA, report)
        if node == _ANALYST:
            answered = any(message["role"] == "tool" for message in asked["messages"])
            if price_tool and not answered:
                days = {"symbol": "BTC-USD", "start_date": "2026-06-29", "end_date": "2026-07-06"}
                return _tool_call(_PRICE_TOOL, days)
            if form != "clean":
                return _ODD_REPORT
        line = f"{node} says: nothing odd."
        if node == _MANAGER:
            return f"{line}\n\nFinal decision:\n\n```json\n{json.dumps(_DECISION)}\n```\n"
        return line

    return _Wire(reply, form=form)


@dataclasses.dataclass
class _Cycle:
    """What one cycle did: every poll's result, and what the store holds after it."""

    polls: list[PollResult]
    attempts: list[tuple[str, int, str | None]]  # (status, tries, error_type) per decision attempt
    rows: dict[str, int]

    @property
    def events(self) -> list[CycleEvent]:
        return [poll.event for poll in self.polls]

    @property
    def plan(self) -> tuple | None:
        """The registered plan's gate verdict, or ``None`` when the cycle registered none."""
        started = self.polls[-1].plan
        if started is None:
            return None
        gate = started.gate
        return (
            gate.risk_action.value,
            gate.order_created,
            gate.target_side.value,
            gate.approved_target_margin_pct,
        )


def _run_cycle(directory, monkeypatch, wire: _Wire) -> _Cycle:
    """One decision cycle of a fresh paper daemon in ``directory``, its graph asking ``wire``."""
    import tradingagents.graph.trading_graph as trading_graph

    directory.mkdir()
    db = Database(directory / "store.db")
    try:
        accounting.initialize_run(
            db, run_id="r", mode="paper", initial_balance_usdc=Decimal(1000), schema_version=1
        )
        clock = ManualClock(_T0)
        asset = AssetSpec(coin="BTC", sz_decimals=3, margin_schedule=margin_schedule())
        risk = RiskConfig(leverage=Decimal(5), max_target_margin_pct=60)
        decision = DecisionConfig()
        engine = PaperExecutionEngine(
            db=db,
            run_id="r",
            asset=asset,
            clock=clock,
            provider=ScriptedSnapshotProvider("BTC", [snap()] * 4),
            risk_config=risk,
            decision_config=decision,
            paper_config=PaperTradingConfig.from_dict(None),
        )
        # The graph is built per try by the provider the daemon builds; the
        # engine's client factory, where the graph reads it, is the one
        # seam taken on the model side.
        with monkeypatch.context() as scoped:
            scoped.setattr(
                trading_graph, "create_llm_client", wire.wired(trading_graph.create_llm_client)
            )
            scheduler = PaperScheduler(
                db=db,
                run_id="r",
                engine=engine,
                clock=clock,
                provider=build_decision_provider(
                    _CONFIG,
                    db=db,
                    run_id="r",
                    coin="BTC",
                    risk_cfg=risk,
                    decision_cfg=decision,
                    payload_dir=directory / "payloads",
                ),
                asset=asset,
                risk_config=risk,
                decision_config=decision,
            )
            polls: list[PollResult] = []
            while not (polls and polls[-1].event.is_cycle_terminal):
                # A retry is due at an instant the clock is set to, not waited for.
                if polls:
                    clock.set(polls[-1].retry_at)
                polls.append(scheduler.poll())
        attempts = [
            (row["status"], row["attempt_count"], row["error_type"])
            for row in db.conn.execute(
                "SELECT status, attempt_count, error_type FROM decision_attempts"
            )
        ]
        rows = {
            table: db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("ai_inputs", "ai_outputs", "execution_plans", "orders")
        }
    finally:
        db.close()
    assert wire.keys <= {f"Bearer {_KEY}"}
    return _Cycle(polls, attempts, rows)


def _asked(wire: _Wire) -> list[str | None]:
    """The node behind every request that reached the transport, in order."""
    return [node for node, _text in wire.sent()]


def _client_warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _CLIENT_LOGGER and record.levelno == logging.WARNING
    ]


# -- the cycle goes on ----------------------------------------------------------------------------

_APPROVED = ("approved", True, "long", 10)


def _assert_decided_on_the_first_try(cycle: _Cycle, clean: _Cycle) -> None:
    """The cycle completed on its first try and registered the plan the clean cycle registered."""
    assert cycle.events == [CycleEvent.COMPLETED]
    assert cycle.attempts == clean.attempts == [("completed", 1, None)]
    assert cycle.plan == clean.plan == _APPROVED
    assert cycle.rows == clean.rows
    assert cycle.rows == {"ai_inputs": 1, "ai_outputs": 1, "execution_plans": 1, "orders": 1}


@pytest.mark.parametrize("form", ["raw", "escape"])
def test_an_analysts_report_holding_one_is_sent_on_as_u_fffd_and_the_cycle_decides(
    form, tmp_path, monkeypatch, offline, caplog
):
    odd, clean = _provider(form), _provider("clean")
    with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
        # The same cycle with no such character in it: sent as it is, nothing said.
        plain = _run_cycle(tmp_path / "twin", monkeypatch, clean)
        assert _client_warnings(caplog) == []
        assert not any(FFFD in text for _node, text in clean.sent())
        cycle = _run_cycle(tmp_path / "odd", monkeypatch, odd)
    # Decided on the first try: no retry was scheduled and nothing failed.
    _assert_decided_on_the_first_try(cycle, plain)
    # What the agents wrote is recorded beside the payload, as for the clean
    # cycle. Written as it stands in the graph's state, the report would
    # sink the file (as a final decision holding such a character already
    # did); the write replaces, and says how many. The file is UTF-8, read
    # strictly here.
    (recorded,) = (tmp_path / "odd" / "payloads").glob("*.reports.json")
    assert len(list((tmp_path / "twin" / "payloads").glob("*.reports.json"))) == 1
    kept = recorded.read_bytes().decode("utf-8")
    assert json.loads(kept)["market_report"] == _SENT_REPORT
    assert [
        record.getMessage()
        for record in caplog.records
        if record.name == "contrib.hyperliquid_perp.common.sidecar"
        and record.levelno == logging.WARNING
    ] == [
        f"decision reports sidecar: {_ODD_REPORT.count(HIGH)} lone surrogate(s) in "
        f"{recorded.name} "
        "cannot be written as UTF-8; each is written as U+FFFD"
    ]
    # Not one request more than the clean cycle made: the graph's eleven nodes, once each.
    assert _asked(odd) == _asked(clean) == _NODES
    # Every node that quotes the analyst's report comes after the analysts,
    # and its request, as the transport received it, holds U+FFFD where the
    # character stood (``sent`` has checked that none holds the character itself).
    sent = odd.sent()
    readers = [node for node, text in sent if _SENT_REPORT in text]
    assert readers and all(_NODES.index(node) >= 3 for node in readers)
    # One warning per request it was replaced in, each with how many.
    replaced = [text.count(FFFD) for _node, text in sent if FFFD in text]
    assert replaced and _client_warnings(caplog) == [_note(count) for count in replaced]
    assert offline.refused == []


# -- a source that is not a model's reply ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "text", "reader", "price_tool"),
    [
        # A tool's result: the market analyst asks for prices, and its next
        # request carries what the vendor answered.
        (_PRICE_TOOL, f"Date,Close\n2026-07-05,50000 {HIGH}\n", _ANALYST, True),
        # Text fetched before any model is asked: the sentiment analyst's
        # prompt quotes the news it was handed.
        ("get_news", f"Headline: momentum {HIGH} holds\n", _SENTIMENT, False),
    ],
    ids=["a-tools-result", "prefetched-news"],
)
def test_vendor_text_holding_one_is_sent_on_as_u_fffd_and_the_cycle_decides(
    method, text, reader, price_tool, tmp_path, monkeypatch, offline, caplog
):
    odd, clean = (
        _provider("clean", price_tool=price_tool),
        _provider("clean", price_tool=price_tool),
    )
    with caplog.at_level(logging.WARNING, logger=_CLIENT_LOGGER):
        plain = _run_cycle(tmp_path / "twin", monkeypatch, clean)
        assert _client_warnings(caplog) == []
        offline.vendor_text[method] = text
        cycle = _run_cycle(tmp_path / "odd", monkeypatch, odd)
    _assert_decided_on_the_first_try(cycle, plain)
    # Not one request more than the clean cycle made: the eleven nodes once
    # each, and the analyst's second round when it called its tool.
    assert _asked(odd) == _asked(clean)
    assert len(_asked(odd)) == len(_NODES) + (1 if price_tool else 0)
    # The request that quotes the vendor's text (its last line, here) is
    # the reader's, and holds U+FFFD where the character stood.
    sent = odd.sent()
    quoted = text.splitlines()[-1].replace(HIGH, FFFD)
    assert [node for node, said in sent if quoted in said] == [reader]
    replaced = [said.count(FFFD) for _node, said in sent if FFFD in said]
    assert replaced and _client_warnings(caplog) == [_note(count) for count in replaced]
    assert offline.refused == []


# -- the control: what the same cycle did before --------------------------------------------------


def test_without_the_replacement_the_same_cycle_pays_three_times_and_decides_nothing(
    tmp_path, monkeypatch, offline
):
    # Why the fix exists: with the replacement taken out of the client, the
    # cycle above is the failure issue #441 measured. Where the installed
    # SDK sends such text itself there is nothing to reproduce, and this is
    # skipped and says so: the controls CI runs (the root suite's, replay's
    # and uniswap_v3's) are the ones that fail then.
    import tradingagents.llm_clients.openai_client as openai_client

    if not _sdk_refuses_such_text():
        pytest.skip("the installed SDK sends a lone surrogate itself: no failure to reproduce")
    monkeypatch.setattr(openai_client, "sendable", lambda input: input)
    wire = _provider("escape")
    cycle = _run_cycle(tmp_path / "odd", monkeypatch, wire)
    # The whole ladder, and no decision at the end of it.
    assert cycle.events == [
        CycleEvent.RETRY_SCHEDULED,
        CycleEvent.RETRY_SCHEDULED,
        CycleEvent.API_FAILED,
    ]
    # Three tries and no answer. How the failure is classed is not asserted:
    # that is read off the exception's name and words, which are the SDK's.
    assert [(status, tries) for status, tries, _kind in cycle.attempts] == [("api_failed", 3)]
    assert cycle.plan is None
    assert cycle.rows == {"ai_inputs": 3, "ai_outputs": 0, "execution_plans": 0, "orders": 0}
    # Each try ran the three analysts again, and paid for them; the first
    # node that quotes the report never got a request out.
    assert _asked(wire) == _NODES[:3] * 3
    assert offline.refused == []
