"""The tool-using hypothesist (tools plan §2, §3.4; T3): a model that may look things up before it answers.

``search()`` asks a hypothesist for one rule a round and is not changed to
know about tools (plan §1 #1): everything here happens INSIDE one
``propose()``. The model is shown the three training-window look-ups, calls
them through the look-ups' server (:mod:`.tools_server`, a process of its
own, over MCP), and what ``propose()`` returns is the text of its last
message — a rule, which the loop parses, measures and files as it does any
answer.

**Where the model comes from.** The research package's own
:class:`~contrib.autoresearch.hypothesis.ChatHypothesist`, as for the model
arm: its client is bound with the tools (``bind_tools``, which every chat
client of the repo has) and asked with the research package's two messages,
a paragraph appended to the first. This module imports no model library: a
reply is read by the four attributes a chat client's reply carries, and a
tool's result goes back as a message written as a plain ``dict``
(:func:`tool_message`) — ``contrib/tests`` holds that shape against the
library the clients are built on.

**What the model is told beyond the two prompts** is a :class:`Pack`: the
tools as the server lists them (name, description, input schema), the
paragraph appended to the system prompt (:func:`addendum`), and the handful
of sentences this module can say into a conversation
(:data:`SENTENCES`). The paragraph states facts and gives no advice
(decided 2026-11-19): what a tool can read, that the validation bars stay
hidden, the limits, that a line is to be written before each call.
:data:`PACK_VERSION` versions the whole of it (decided 2026-11-19) — the
server's wording included, though that has a version of its own — and the
suite pins a digest beside the number. Before a model is asked anything,
the server is checked to be the one this build is written against and to
list exactly this build's tools, so the pack a model reads is this
build's — the one the suite pins. Two things a model reads are not in the
pack: the sentence the research package's parser refuses a mistake in,
which is that package's, and how the pieces are laid out (the newlines
between them, how a call's arguments are spelled where a round is written
out as text, a failed call's ``status``), which the suite holds by exact
text.

**Limits are counted here, not by the server** (plan §2.3,
:class:`Limits`). Every call a model makes in a round counts until none is
left, a refused one included; the result of each call that counted ends
with a line saying what is left (decided 2026-11-19: what is measured is
research, not counting). A backtest the parser refused counts as a call and
not as a backtest (decided 2026-11-19): that limit is on training scores
seen, and a slip of grammar shows none. A call past the limit is not run
and is answered with a sentence saying so; a later MESSAGE that still calls
a tool ends the conversation, and the rule is asked for once more with no
tools — in a fresh conversation, the round's look-ups written out as text,
since a provider may refuse a history of tool calls that comes with no
tools, and how a client spells "call nothing" differs by provider. A
message with calls in it that cannot be sent back as the model wrote it —
half a surrogate pair anywhere in it, or a ``NaN`` or an infinity in a
call, which not every client can encode a request from — ends the conversation the same
way, and at once. (A message with no call is the answer, whatever is in
it.)

**A failed seam is not an answer** (plan §2.3). The model not answering, a
reply this module cannot read, the server gone, a look-up that timed out or
that the stored history cannot answer: each is a ``HypothesistError``, so
``search()`` stops without spending a round. A mistake of the MODEL's — a
feature that does not exist, a rule the parser refuses, a call that cannot
be sent at all — is not: it is answered in a result marked as an error,
shown to the model, and counts as a call; or, when the message it came in
cannot be sent back, the round is answered without tools. Either way the
search goes on.

**Nothing is kept between rounds** (plan §2.4): each ``propose()`` starts
from the two prompts it is handed. (The research package's prompt does
list an answer it refused, in the parser's sentence, which quotes it: a
model can spend a round to carry a few characters forward that way. That
channel is the loop's, and every arm has it.)

**The trace** (plan §2.5): one JSON object a line, in the order things
happened. The pack once, then per round every model call (its text between
calls, its token counts), every tool call (arguments, result, whether it
was refused, whether it met a limit) and the answer. Neither prompt is
written: the user prompt carries validation figures, and this module is to
write nothing of the validation window — what it writes of the history is
what the server answered, which read the training bars alone. A model's own
words are kept as it wrote them, and a model may repeat a figure its prompt
showed it. Nor is the text of a failure written, which is a provider's and
may quote what it was sent.

The MCP SDK and anyio are the optional install the server needs
(``pip install -e ".[discovery-tools]"``) and are imported inside the
functions that use them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import wait
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Final, TextIO

from .research_tools import TOOL_NAMES
from .tools_server import IMPORT_ROOT, SERVER_NAME, TOOLS_VERSION, ServerParams, tool_specs
from .upstream import ChatHypothesist, HypothesistError, VocabEnum

__all__ = [
    "BACKTESTS_SPENT",
    "BACKTEST_TOOL",
    "CALL_TIMEOUT_S",
    "CONNECT_TIMEOUT_S",
    "DEFAULT_LIMITS",
    "FORCED_ASK",
    "FORCED_HEADER",
    "FORCED_LOOKUP",
    "FORCED_NOTE",
    "FORCED_NOTE_CAP",
    "MAX_ARGUMENT_CHARS",
    "MAX_ARGUMENT_DEPTH",
    "MAX_NAME_CHARS",
    "NOTE_CAP",
    "NOT_SENDABLE",
    "PACK_VERSION",
    "REMAINING",
    "RESULT_CAP",
    "SENTENCES",
    "SPENT",
    "TRACE_KEYS",
    "TRACE_VERSION",
    "Limit",
    "Limits",
    "Pack",
    "Reply",
    "Seam",
    "ToolArmError",
    "ToolCall",
    "ToolHypothesist",
    "ToolServerError",
    "TraceKind",
    "addendum",
    "read_reply",
    "require_client_sdk",
    "tool_message",
]

logger = logging.getLogger(__name__)

# The version of everything a model of this arm reads beyond the research
# package's two prompts: the tools as listed, the paragraph appended to the
# system prompt, and the sentences below. One number for the whole of it
# (decided 2026-11-19), so two runs are comparable when it and the limits
# agree. Frozen after the first trial with a real model (plan §5); the
# suite pins a digest beside it, so an edit without a bump is red — an edit
# of the server's wording too, which moves ``TOOLS_VERSION`` as well.
PACK_VERSION: Final = 1

# The one tool with a limit of its own: a whole backtest is the costly
# look-up, and the one a model can climb a training score with.
BACKTEST_TOOL: Final = "backtest_train"
if BACKTEST_TOOL not in TOOL_NAMES:  # pragma: no cover - a rename upstream of this line
    raise RuntimeError(f"{BACKTEST_TOOL!r} is not one of the look-ups {list(TOOL_NAMES)}")

# How long the server has to answer the handshake, and one look-up. The
# first is long because the server computes every feature before it answers
# at all (about fourteen seconds on a 5,000-bar world by the server
# module's figure, twenty-three from start to connected through the
# ``tools`` command by the README's); a warm look-up takes milliseconds, so
# a minute is a server that has stopped.
CONNECT_TIMEOUT_S: Final = 180.0
CALL_TIMEOUT_S: Final = 60.0

# How often the wait for a look-up, or for the tools' listing, looks up
# from it. A wait on another thread's result is one an interrupt does not
# break on every platform, so it is taken in slices: a Ctrl-C is heard
# within one. (The handshake and the close are not waited this way.)
_POLL_S: Final = 0.5

# What of a call is sent to the server at all. A call's arguments, written
# as JSON, may be as long as the loop lets an answer be (its
# ``MAX_RESPONSE_CHARS``, which the suite holds this to): a rule longer
# than that could not be proposed, so there is nothing in looking it up. A
# rule nests half a dozen deep and a tool's name is a word. Past any of the
# three a call is answered as one that cannot be sent, so neither the wire,
# nor a trace line, nor the round written out as text is handed whatever a
# model writes. The length is measured on the spelling a trace line writes
# (ASCII, the rest escaped), where the loop counts the characters of an
# answer: one number and two measures, a few characters apart on anything
# a rule is made of (the arguments also name the rule's key). A model is
# answered differently past the bounds, so they are in the pack.
MAX_ARGUMENT_CHARS: Final = 8000
MAX_ARGUMENT_DEPTH: Final = 32
MAX_NAME_CHARS: Final = 64

# How much of a model's text, and of a tool's result, one trace line keeps.
# The length before the cut is written beside it. What a trace keeps is not
# what a model reads: neither is in the pack.
NOTE_CAP: Final = 2000
RESULT_CAP: Final = 4000


class TraceKind(VocabEnum, noun="trace line"):
    """The kinds of line a trace holds."""

    PACK = "pack"
    MODEL = "model"
    TOOL = "tool"
    ANSWER = "answer"
    FAILED = "failed"


class Limit(VocabEnum, noun="limit"):
    """Which limit kept a call from being run."""

    LOOKUPS = "lookups"
    BACKTESTS = "backtests"


class Seam(VocabEnum, noun="seam"):
    """Which side of the hypothesist failed: the model's, or the look-ups' server's."""

    MODEL = "model"
    SERVER = "server"


# The trace's own version, written on its opening line: the kinds of line
# there are and the keys each carries. Bumped when either changes, so a
# reader of stored traces can tell them apart.
TRACE_VERSION: Final = 1
_STAMPED: Final = ("kind", "round", "seq")
TRACE_KEYS: Final[Mapping[TraceKind, tuple[str, ...]]] = MappingProxyType(
    {
        TraceKind.PACK: (
            "kind",
            "trace_version",
            "pack_version",
            "pack_digest",
            "server",
            "model",
            "tools_version",
            "limits",
            "addendum",
            "sentences",
            "forced_note_chars",
            "argument_bounds",
            "tools",
        ),
        TraceKind.MODEL: (
            *_STAMPED,
            "bound",
            "text",
            "text_chars",
            "tool_calls",
            "input_tokens",
            "output_tokens",
            "elapsed_ms",
        ),
        TraceKind.TOOL: (
            *_STAMPED,
            "tool",
            "arguments",
            "sent",
            "tool_error",
            "limit",
            "result",
            "result_chars",
            "elapsed_ms",
        ),
        TraceKind.ANSWER: (
            *_STAMPED,
            "forced",
            "lookups",
            "backtests",
            "tool_errors",
            "text",
            "text_chars",
        ),
        TraceKind.FAILED: (*_STAMPED, "side", "error"),
    }
)

# -- what this module says into a conversation ---------------------------------------------------

REMAINING: Final = "[left this round: {lookups} tool call(s), of which backtest_train: {backtests}]"
SPENT: Final = (
    "No tool call is left this round, so this one was not run. Answer now: ONE new rule as a "
    "single JSON object, and nothing else."
)
BACKTESTS_SPENT: Final = "No backtest_train call is left this round, so this one was not run."
# One sentence for every way a call cannot be sent (:func:`_read_call`), so
# it names both halves of a call: the fault may be in either.
NOT_SENDABLE: Final = (
    "This call was not run: its tool name or its arguments could not be sent as written."
)
FORCED_HEADER: Final = (
    "WHAT YOU LOOKED UP THIS ROUND on the training bars (no tool read the validation bars), in "
    "order:"
)
# How the round is written out under that header: a line the model wrote
# before a call, and a look-up with what it returned.
FORCED_NOTE: Final = "note: {text}"
FORCED_LOOKUP: Final = "{tool} {arguments}\n-> {result}"
# True of both ways a round's tool use ends early — the calls spent and
# another made, and a message that could not be sent back, which may be
# the round's first — so it says what is so from here on and not how many
# calls were made.
FORCED_ASK: Final = (
    "No tool is at hand for the rest of this round. Propose ONE new rule as a single JSON "
    "object, and nothing else."
)
# How much of a line the model wrote is shown back to it when it is asked
# without tools. A model reads this cut, so it is the pack's.
FORCED_NOTE_CAP: Final = 2000

# Every sentence above, by the name a trace's opening line lists it under.
# The pack's digest is taken over this table, so a sentence a model can be
# told has to be in it; the suite holds each constant above to be.
SENTENCES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "remaining": REMAINING,
        "spent": SPENT,
        "backtests_spent": BACKTESTS_SPENT,
        "not_sendable": NOT_SENDABLE,
        "forced_header": FORCED_HEADER,
        "forced_note": FORCED_NOTE,
        "forced_lookup": FORCED_LOOKUP,
        "forced_ask": FORCED_ASK,
    }
)

_ADDENDUM: Final = "\n".join(
    [
        "TOOLS you may use before you answer:",
        "The prompt above says a rule is scored on history you cannot see. With the tools you "
        "are given, part of that history can be looked at: the training bars. A tool reads the "
        "training bars and nothing after them. The validation bars stay hidden from you and "
        "from every tool, and the validation figures a scored rule is listed with are measured "
        "on them.",
        "- Using a tool is optional. A look-up is not a proposal: it is not scored and does not "
        "spend a round.",
        "- In one round, that is before one answer, you may make at most {lookups} tool "
        "call(s), of which at most {backtests} may be backtest_train call(s) that return a "
        "score. Every call counts toward the {lookups}, one refused for a mistake in its "
        "arguments included; a backtest_train call refused that way does not count toward the "
        "{backtests}. The result of each call that counts ends with a line saying how many "
        "are left.",
        "- When none is left, a further call is not run and returns a sentence saying so. If a "
        "later message of yours still calls a tool, you are asked for your rule with no tool at "
        "hand.",
        "- Nothing you look up is kept for the next round: each round starts from the prompt "
        "alone.",
        "- Before each tool call, write one line saying what you are about to check and why.",
        "- Your answer is still what the prompt asks for: ONE new rule as a single JSON object, "
        "and nothing else, in a message that calls no tool.",
    ]
)


class ToolArmError(ValueError):
    """The arm cannot be started as asked, and the sentence says why.

    A refusal to START: the SDK missing, limits that are not limits, a
    client that takes no tools, a trace or a log that exists, a server
    that is not the one this build is written against. Not a failed seam:
    nothing was asked of a model yet, and it is the caller's to put right.
    """


class ToolServerError(HypothesistError):
    """The look-ups' server did not answer: the seam failed on the tools' side.

    A ``HypothesistError`` (plan §2.3), so ``search()`` stops without
    spending a round; a class of its own so a caller can tell the server
    from the model. It carries no sentence of the research package's: the
    server keeps those on its stderr.
    """


@dataclass(frozen=True)
class Limits:
    """How many calls a model may make before one answer (plan §2.3).

    ``lookups`` bounds every call, of any tool; ``backtests`` bounds the
    ``backtest_train`` calls among them that return a score. The plan's
    starting point; settled after the first trial with a real model and not
    moved after.
    """

    lookups: int = 5
    backtests: int = 2

    def __post_init__(self) -> None:
        for name in ("lookups", "backtests"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ToolArmError(
                    f"limits.{name}: expected a whole number of calls, got {value!r}"
                )
        if self.lookups < 1:
            raise ToolArmError(
                f"limits.lookups: an arm that may make no call is the model arm, got {self.lookups}"
            )
        if not 0 <= self.backtests <= self.lookups:
            raise ToolArmError(
                f"limits.backtests: expected 0 to {self.lookups} (the calls there are), got "
                f"{self.backtests}"
            )


DEFAULT_LIMITS: Final = Limits()


def addendum(limits: Limits) -> str:
    """The paragraph appended to the research package's system prompt, under ``limits``.

    Facts, and no advice (decided 2026-11-19): a sentence on how to read a
    figure would be read by this arm alone, and a result could not then be
    told from the sentence. It does answer the one line of the system
    prompt the tools make untrue as written — "history you cannot see" —
    by saying which part can now be looked at and which cannot.
    """
    return _ADDENDUM.format(lookups=limits.lookups, backtests=limits.backtests)


def _remaining(limits: Limits, lookups: int, backtests: int) -> str:
    left = limits.lookups - lookups
    return REMAINING.format(lookups=left, backtests=min(limits.backtests - backtests, left))


_TOOL_KEYS: Final = frozenset({"type", "function"})
_FUNCTION_KEYS: Final = frozenset({"name", "description", "parameters"})


def _canonical(tools: object) -> str:
    return json.dumps(tools, sort_keys=True)


@dataclass(frozen=True)
class Pack:
    """Everything a model of this arm reads beyond the research package's two prompts.

    The tools are the look-ups as a chat client is bound with them: the
    function form every client of the repo takes, carrying the name, the
    description and the input schema the server listed. The output schema
    is not among them — a client sends a model none.

    They are HELD as text, their one canonical spelling, and handed out as
    a fresh copy each time (:attr:`tools`): a pack is written into a trace
    with its digest, and a pack whose tools could be changed afterwards
    would be a record that no longer says what was sent. Built by
    :meth:`of`; built by hand, it is held to the same things — the three
    look-ups, in their order, each in the function form.
    """

    tools_json: str
    limits: Limits

    def __post_init__(self) -> None:
        if not isinstance(self.limits, Limits):
            raise ToolArmError(f"pack.limits: expected Limits, got {type(self.limits).__name__}")
        try:
            tools = json.loads(self.tools_json)
        except (TypeError, ValueError) as exc:
            raise ToolArmError(f"pack.tools_json: not JSON ({exc})") from exc
        if not isinstance(tools, list):
            raise ToolArmError(
                f"pack.tools_json: expected a list of tools, got {type(tools).__name__}"
            )
        names = []
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if (
                not isinstance(tool, dict)
                or set(tool) != _TOOL_KEYS
                or tool["type"] != "function"
                or not isinstance(function, dict)
                or set(function) != _FUNCTION_KEYS
                or not isinstance(function["name"], str)
                or not isinstance(function["description"], str)
                or not isinstance(function["parameters"], dict)
            ):
                raise ToolArmError(
                    f"pack.tools_json: a tool is {{'type': 'function', 'function': "
                    f"{{{', '.join(sorted(_FUNCTION_KEYS))}}}}} with a name and a description "
                    f"in text and its parameters as an object, got {tool!r:.200}"
                )
            names.append(function["name"])
        if tuple(names) != TOOL_NAMES:
            raise ToolArmError(
                f"pack.tools_json: the tools are {names}, not the look-ups {list(TOOL_NAMES)}"
            )
        if self.tools_json != _canonical(tools):
            raise ToolArmError("pack.tools_json: not the canonical spelling of its tools")

    @classmethod
    def of(cls, listed: Iterable[Any], limits: Limits) -> Pack:
        """The pack over the tools as listed: by a server, or by ``tool_specs()``."""
        return cls(
            tools_json=_canonical(
                [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.input_schema,
                        },
                    }
                    for tool in listed
                ]
            ),
            limits=limits,
        )

    @property
    def tools(self) -> tuple[dict[str, Any], ...]:
        """The tools as a client is bound with them: a copy of its own for each caller."""
        return tuple(json.loads(self.tools_json))

    def document(self) -> dict[str, Any]:
        """The pack as the trace opens with it, and as its digest is taken."""
        return {
            "tools_version": TOOLS_VERSION,
            "limits": asdict(self.limits),
            "addendum": addendum(self.limits),
            "sentences": dict(SENTENCES),
            "forced_note_chars": FORCED_NOTE_CAP,
            "argument_bounds": {
                "chars": MAX_ARGUMENT_CHARS,
                "depth": MAX_ARGUMENT_DEPTH,
                "name_chars": MAX_NAME_CHARS,
            },
            "tools": list(self.tools),
        }

    def digest(self) -> str:
        written = json.dumps(self.document(), sort_keys=True)
        return hashlib.sha256(written.encode("utf-8")).hexdigest()


# -- a reply, read without the library that made it ----------------------------------------------


@dataclass(frozen=True)
class ToolCall:
    """One call a model asked for. ``arguments`` is ``None`` when the call cannot be sent as asked.

    ``name`` is the tool's name as it can be written down — what was
    asked for, unless that was no text a wire carries or was longer than
    :data:`MAX_NAME_CHARS`, and then the call has no arguments to send.
    """

    id: str
    name: str
    arguments: dict[str, Any] | None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("a tool call is answered by its id, and this one has none")
        if not isinstance(self.name, str) or len(self.name) > MAX_NAME_CHARS:
            raise ValueError(
                f"a tool call's name is text of at most {MAX_NAME_CHARS} characters, got "
                f"{self.name!r:.80}"
            )
        if self.arguments is not None and not isinstance(self.arguments, dict):
            raise ValueError(
                f"a tool call's arguments are an object or nothing, got "
                f"{type(self.arguments).__name__}"
            )


@dataclass(frozen=True)
class Reply:
    """A chat client's reply as this arm reads it: its text, the calls it asks for, what it cost.

    ``replayable`` says the reply can be put back into the conversation:
    everything in it — its text, each call's id, name and arguments as
    they came — is something every client can encode a request from. A
    reply holding a ``NaN`` is not, for a client that refuses to write
    one; a reply with half a surrogate pair in it is not: sent back, the
    provider's
    client fails to encode the request (measured 2026-11-19), and a
    mistake of the model's would end the search as a model that did not
    answer.
    """

    message: object
    text: str
    calls: tuple[ToolCall, ...]
    input_tokens: int | None
    output_tokens: int | None
    replayable: bool = True


def read_reply(message: object, label: str) -> Reply:
    """``message`` as a :class:`Reply`, or a failed seam if it is not a reply this arm can read.

    Read by attribute, so no model library is imported: ``content`` (text —
    the repo's clients flatten typed blocks before they hand a reply over),
    ``tool_calls`` (each a mapping with ``name``, ``args`` and ``id``),
    ``invalid_tool_calls`` (calls whose arguments the client could not
    parse) and ``usage_metadata``. A call with no id cannot be answered, so
    it is the seam's failure and not the model's mistake.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        raise HypothesistError(f"{label} answered with {type(content).__name__}, not text")
    parsed = _listed(message, "tool_calls", label)
    unparsed = _listed(message, "invalid_tool_calls", label)
    calls = [
        *(_read_call(item, label, parsed=True) for item in parsed),
        *(_read_call(item, label, parsed=False) for item in unparsed),
    ]
    usage = getattr(message, "usage_metadata", None)
    return Reply(
        message=message,
        text=content,
        calls=tuple(calls),
        input_tokens=_tokens(usage, "input_tokens"),
        output_tokens=_tokens(usage, "output_tokens"),
        replayable=_encodes(content)
        and all(
            _encodes([item.get("id"), item.get("name"), item.get("args")])
            for item in (*parsed, *unparsed)
        ),
    )


def _encodes(value: object) -> bool:
    """Whether ``value`` is something every client can encode a request from.

    Text all the way down, and no ``NaN`` or infinity: one client writes
    those out and another refuses to, so a reply holding one is not sent
    back to either.
    """
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False, default=str).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _listed(message: object, name: str, label: str) -> list[Any]:
    value = getattr(message, name, None)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise HypothesistError(
            f"{label} answered with {name} as {type(value).__name__}, not a list"
        )
    return list(value)


def _read_call(item: object, label: str, *, parsed: bool) -> ToolCall:
    """One call as asked for. What cannot be sent as asked is kept, with no arguments to send.

    That is the MODEL's mistake, so it is no failed seam: arguments the
    client could not parse, arguments that are no JSON object or that JSON
    cannot carry (a ``NaN``, half a surrogate pair), ones nested or long
    past any call a model means, and a tool's name that is no text a wire
    carries or is longer than any tool's. Sent on, half a surrogate pair
    fails inside the MCP client and the connection closes, so the search
    would end as a server that went away; a ``NaN`` goes over the wire as
    ``null``, so the server would refuse a value the model did not write
    (both measured on mcp 2.3, 2026-11-19). The bounds on depth and length
    are there so that neither the wire nor a trace line is handed whatever
    a model writes. (Not sending a call is half of it, and for two of
    these not the half a model meets: a reply with a ``NaN`` or with
    half a surrogate pair in it cannot be sent back to the model either,
    which is :attr:`Reply.replayable`'s to say.)
    """
    if not isinstance(item, Mapping):
        raise HypothesistError(f"{label} asked for a tool call as {type(item).__name__}")
    call_id, name = item.get("id"), item.get("name")
    if not isinstance(call_id, str) or not call_id:
        raise HypothesistError(
            f"{label} asked for a tool call with no id, which cannot be answered"
        )
    if not isinstance(name, str):
        if parsed:
            raise HypothesistError(f"{label} asked for a tool call with no tool named")
        name = ""
    shown = name.encode("utf-8", "replace").decode("utf-8")[:MAX_NAME_CHARS]
    arguments = item.get("args") if parsed else None
    if shown != name or not isinstance(arguments, dict) or not _sendable(arguments):
        arguments = None
    return ToolCall(id=call_id, name=shown, arguments=arguments)


def _deeper_than(value: object, depth: int) -> bool:
    if depth < 0:
        return True
    if isinstance(value, dict):
        return any(_deeper_than(item, depth - 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_deeper_than(item, depth - 1) for item in value)
    return False


def _sendable(arguments: object) -> bool:
    """Whether ``arguments`` can go over the wire, and into a trace line, as JSON.

    They can when JSON says exactly what they are — written out and read
    back they are the same value, which a ``NaN``, a key that is a number
    and a tuple among the values are not — when that text can be encoded,
    and when it is
    within the bounds. The length is the length of the spelling a trace
    line and a round written out as text use, so the bound is on what is
    written and not on how many characters a model typed.
    """
    if _deeper_than(arguments, MAX_ARGUMENT_DEPTH):
        return False
    try:
        written = json.dumps(arguments, allow_nan=False)
        json.dumps(arguments, ensure_ascii=False).encode("utf-8")
        same = json.loads(written) == arguments
    except (TypeError, ValueError):
        return False
    return same and len(written) <= MAX_ARGUMENT_CHARS


def _tokens(usage: object, key: str) -> int | None:
    value = usage.get(key) if isinstance(usage, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def tool_message(call_id: str, text: str, *, error: bool) -> dict[str, str]:
    """A tool's result as the next message of the conversation, written as a plain ``dict``.

    The role-keyed form a chat client reads as its own tool message, so
    this module builds one without the library; ``status`` is what a
    provider that marks a failed call is told.
    """
    return {
        "role": "tool",
        "content": text,
        "tool_call_id": call_id,
        "status": "error" if error else "success",
    }


# -- the server, asked from this thread ----------------------------------------------------------


def require_client_sdk() -> None:
    """Refuse without the MCP SDK and anyio, before anything is opened or started."""
    try:
        from anyio.from_thread import start_blocking_portal  # noqa: F401
        from mcp import Client, StdioServerParameters  # noqa: F401
        from mcp.client.stdio import stdio_client  # noqa: F401
    except ImportError as exc:
        raise ToolArmError(
            f"the MCP SDK the tool arm is written against cannot be imported ({exc}); if it is "
            f"not installed, it is this package's optional install: pip install -e "
            f'".[discovery-tools]"'
        ) from exc


def _leaves(exc: BaseException) -> list[BaseException]:
    grouped = getattr(exc, "exceptions", None)
    if not isinstance(grouped, (list, tuple)) or not grouped:
        return [exc]
    return [leaf for inner in grouped for leaf in _leaves(inner)]


def _described(exc: BaseException) -> str:
    """What went wrong on the server's side, the client's task groups unwrapped.

    The SDK raises a closed connection or a timeout inside an exception
    group; the leaves are what a person can read, each once and each cut
    to a line's length. Nothing of the research package's is in them: the
    server answers a failure of its own with a fixed sentence.
    """
    return "; ".join(dict.fromkeys(f"{type(leaf).__name__}: {leaf}"[:200] for leaf in _leaves(exc)))


def _waited(portal: Any, function: Callable[..., Any], *arguments: object) -> Any:
    """``function(*arguments)`` on the portal's loop, waited for in slices.

    A wait on another thread's result is one an interrupt does not break
    on every platform (measured on Windows: a Ctrl-C during a look-up was
    heard only when the look-up's own timeout ran out), so the wait looks
    up every :data:`_POLL_S`. Leaving for any reason takes the task along.

    The slices are waited with ``wait``, which says nothing when one ends:
    the task's result — a timeout of its own among them — is read once,
    when the task is done, and cannot be mistaken for a slice running out.
    """
    future = portal.start_task_soon(function, *arguments)
    try:
        while not future.done():
            wait([future], timeout=_POLL_S)
        return future.result()
    except BaseException:
        future.cancel()
        raise


def _transport(server: object, log: TextIO | None) -> Any:
    """What the client connects through: a child started on ``server``, or ``server`` itself.

    A :class:`~.tools_server.ServerParams` is the way a benchmark starts
    one: its own command, run where ``contrib`` imports from, its stderr
    written to ``log``. Anything else is handed to the client as it is — a
    server object for a connection in memory, which is how the suite
    drives this module without a process.
    """
    if not isinstance(server, ServerParams):
        return server
    if log is None:  # pragma: no cover - ``open`` refuses a child with no log
        raise ToolArmError("a child's stderr has to be kept: name a server_log")
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = server.command()
    child = StdioServerParameters(command=command[0], args=command[1:], cwd=str(IMPORT_ROOT))
    return stdio_client(child, errlog=log)


@contextmanager
def _connected(portal: Any, transport: Any, timeout: float) -> Iterator[Any]:
    """An open client on the portal's loop. A server that does not start is a failed seam.

    ``timeout`` bounds the handshake's ``initialize``, and through it the
    start: the server warms before it answers. Before that request the SDK
    probes for its newer handshake under a bound of its own (ten seconds on
    mcp 2.3, which ``timeout`` does not move) and falls back when the probe
    is not answered — so a server that warms for longer than the probe, as
    one over a few thousand bars does, is connected to by ``initialize``,
    and a silent one is given up on that much after ``timeout``. Closing is
    quiet — by then the search is over, and a server that died is already
    what the search ended on.
    """
    from mcp import Client

    manager = portal.wrap_async_context_manager(Client(transport, read_timeout_seconds=timeout))
    try:
        client = manager.__enter__()
    except Exception as exc:
        raise ToolServerError(f"the look-ups' server did not start: {_described(exc)}") from exc
    try:
        yield client
    finally:
        try:
            manager.__exit__(None, None, None)
        except Exception as exc:  # noqa: BLE001 - whatever a dead connection raises on the way out
            logger.warning("closing the look-ups' server: %s", _described(exc))


class _Session:
    """The open connection, asked from the thread the search runs on."""

    def __init__(self, portal: Any, client: Any, call_timeout: float) -> None:
        self._portal = portal
        self._client = client
        self._call_timeout = call_timeout

    def listing(self) -> list[Any]:
        try:
            return list(_waited(self._portal, self._client.list_tools).tools)
        except Exception as exc:
            raise ToolServerError(
                f"the look-ups' server did not list its tools: {_described(exc)}"
            ) from exc

    def call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """One look-up: the text a model reads, and whether the server refused the call.

        The server answers a call it took with one block of text and says
        whether it refused: anything else it does — a protocol error, a
        closed connection, a timeout, an answer in another shape — is the
        seam's failure.
        """
        try:
            result = _waited(
                self._portal, self._client.call_tool, name, arguments, self._call_timeout
            )
        except Exception as exc:
            raise ToolServerError(
                f"the look-ups' server did not answer {name!r}: {_described(exc)}"
            ) from exc
        texts = [block.text for block in result.content if getattr(block, "type", None) == "text"]
        if len(texts) != 1 or not texts[0].strip() or not isinstance(result.is_error, bool):
            raise ToolServerError(
                f"the look-ups' server answered {name!r} in a shape this arm does not read: "
                f"{len(texts)} block(s) of text, is_error {result.is_error!r}"
            )
        return texts[0], result.is_error


# -- the trace -----------------------------------------------------------------------------------


@dataclass
class _Round:
    """One ``propose()`` as it goes: what has been spent, said and written down."""

    number: int
    seq: int = 0
    lookups: int = 0
    backtests: int = 0
    tool_errors: int = 0
    # Set once the model has been told no call is left: its next call ends
    # the conversation.
    told_spent: bool = False
    # The round as text, for a model that has to be asked without tools.
    looked: list[str] = field(default_factory=list)


class _Trace:
    """One JSON object a line, flushed as written: a run that dies leaves what it did.

    A line of a round is numbered here, as it is written, with the round
    and its place in it: the lines a round wrote run 1, 2, 3, whatever
    failed between them. Each kind carries exactly its keys of
    :data:`TRACE_KEYS`; a line that does not is a defect of this module.
    """

    def __init__(self, file: IO[str]) -> None:
        self._file = file

    def write(self, kind: TraceKind, state: _Round | None = None, **payload: object) -> None:
        line: dict[str, object] = {"kind": kind.value}
        if state is not None:
            line.update(round=state.number, seq=state.seq + 1)
        line.update(payload)
        if set(line) != set(TRACE_KEYS[kind]):
            raise RuntimeError(
                f"a {kind.value!r} trace line carries {sorted(line)}, not "
                f"{sorted(TRACE_KEYS[kind])}"
            )
        self._file.write(json.dumps(line, allow_nan=False) + "\n")
        self._file.flush()
        if state is not None:
            state.seq += 1


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


# -- the hypothesist -----------------------------------------------------------------------------


class ToolHypothesist:
    """A :class:`~contrib.autoresearch.ports.Hypothesist` that may look things up before it answers.

    Made by :meth:`open`, which holds the server, the trace and the loop
    the client runs on for as long as its ``with`` block lasts — one arm's
    search. ``label`` is the model's, as the research package labels it.
    """

    def __init__(
        self, *, chat: ChatHypothesist, bound: Any, session: _Session, trace: _Trace, pack: Pack
    ) -> None:
        self._label = chat.label
        self._pack = pack
        self._chat = chat
        self._bound = bound
        self._session = session
        self._trace = trace
        self._rounds = 0
        self._tools_failed: str | None = None
        self._closed = False

    @property
    def label(self) -> str:
        """The model's label, as the research package labels it."""
        return self._label

    @property
    def pack(self) -> Pack:
        """What this search's model is told, as the trace's opening line has it."""
        return self._pack

    @classmethod
    @contextmanager
    def open(
        cls,
        chat: ChatHypothesist,
        server: object,
        *,
        trace: Path,
        limits: Limits = DEFAULT_LIMITS,
        server_log: Path | None = None,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        call_timeout: float = CALL_TIMEOUT_S,
    ) -> Iterator[ToolHypothesist]:
        """The hypothesist over ``chat`` and the look-ups' server, for one search.

        ``server`` is a :class:`~.tools_server.ServerParams` — the server
        is then started as a child on them, its stderr written to
        ``server_log``, which has to be named: it is the one place a child
        says why — or a server object, connected to in memory. ``trace``
        is the file this search's trace is written to. Neither file may
        exist: each is one search's.

        A :class:`ToolArmError` is a refusal to start; a
        :class:`ToolServerError` is a server that did not come up. Either
        way no model was asked anything.
        """
        require_client_sdk()
        if not isinstance(limits, Limits):
            raise ToolArmError(f"limits: expected Limits, got {type(limits).__name__}")
        if isinstance(server, (str, bytes, os.PathLike)):
            raise ToolArmError(
                f"server: expected the parameters a server is started on (ServerParams) or a "
                f"server object, got {type(server).__name__}"
            )
        if isinstance(server, ServerParams) == (server_log is None):
            raise ToolArmError(
                "server_log: a child's stderr is the one place it says why a look-up failed, "
                "so a server started as a child has to be given one"
                if server_log is None
                else "server_log is a child's stderr, and this server is no child"
            )
        bind = getattr(chat.model, "bind_tools", None)
        if not callable(bind):
            raise ToolArmError(f"{chat.label} takes no tools: its client has no bind_tools")
        trace, server_log = Path(trace), None if server_log is None else Path(server_log)
        _require_fresh(trace, server_log)
        from anyio.from_thread import start_blocking_portal

        with ExitStack() as stack:
            log = (
                None
                if server_log is None
                else stack.enter_context(server_log.open("x", encoding="utf-8"))
            )
            portal = stack.enter_context(start_blocking_portal())
            client = stack.enter_context(
                _connected(portal, _transport(server, log), connect_timeout)
            )
            _require_this_server(client.server_info)
            session = _Session(portal, client, call_timeout)
            pack = Pack.of(session.listing(), limits)
            if pack != Pack.of(tool_specs(), limits):
                # The same version and other words: the digest the suite
                # pins is this build's, and a model would be reading another.
                raise ToolArmError(
                    f"the server lists tools other than this build's at version "
                    f"{TOOLS_VERSION}: what a model would read is not the pinned pack"
                )
            try:
                bound = bind(list(pack.tools))
            except Exception as exc:
                raise ToolArmError(f"{chat.label} could not be given the tools: {exc}") from exc
            # Opened last, so a server that did not come up leaves no empty
            # trace behind; "x", so one made since the check is not written over.
            written = _Trace(stack.enter_context(trace.open("x", encoding="utf-8", newline="\n")))
            written.write(
                TraceKind.PACK,
                trace_version=TRACE_VERSION,
                pack_version=PACK_VERSION,
                pack_digest=pack.digest(),
                server=SERVER_NAME,
                model=chat.label,
                **pack.document(),
            )
            hypothesist = cls(chat=chat, bound=bound, session=session, trace=written, pack=pack)
            try:
                yield hypothesist
            finally:
                hypothesist._closed = True

    def propose(self, system: str, user: str) -> str:
        """One answer, after whatever the model looked up for it (the module docstring)."""
        if self._closed:
            raise RuntimeError(
                "this hypothesist's server and trace were closed with its `with` block"
            )
        if self._tools_failed is not None:
            # A failed tools seam ends this arm, whichever way it failed — a
            # server that died, or one alive and unable to answer: the search
            # is over (plan §2.3), and a second round would cost a model call
            # to be told the same.
            raise ToolServerError(self._tools_failed)
        self._rounds += 1
        state = _Round(number=self._rounds)
        try:
            return self._propose(system, user, state)
        except HypothesistError as exc:
            side = Seam.SERVER if isinstance(exc, ToolServerError) else Seam.MODEL
            if side is Seam.SERVER:
                self._tools_failed = str(exc)
            cause = exc.__cause__
            self._trace.write(
                TraceKind.FAILED,
                state,
                side=side.value,
                # A class and no sentence: a provider's may quote the
                # request, and the report already says how the search ended.
                # The class is the failure's own, read out of the group the
                # SDK raises it in; with nothing underneath — a reply this
                # arm could not read — it is the seam's.
                error=type(exc if cause is None else _leaves(cause)[0]).__name__,
            )
            raise

    def _propose(self, system: str, user: str, state: _Round) -> str:
        messages: list[object] = [
            ("system", f"{system}\n\n{addendum(self._pack.limits)}"),
            ("human", user),
        ]
        while True:
            reply = self._ask(messages, state, bound=True)
            if not reply.calls:
                return self._answer(reply, state, forced=False)
            if state.told_spent or not reply.replayable:
                return self._forced(system, user, state)
            messages.append(reply.message)
            note = reply.text.strip()
            if note:
                state.looked.append(FORCED_NOTE.format(text=note[:FORCED_NOTE_CAP]))
            for call in reply.calls:
                messages.append(self._run(call, state))

    def _ask(self, messages: list[object], state: _Round, *, bound: bool) -> Reply:
        """One call of the model, with the tools bound or on the client as it was built."""
        client: Any = self._bound if bound else self._chat.model
        started = time.monotonic()
        try:
            raw = client.invoke(list(messages))
        except Exception as exc:
            # The whole family, as the research package's own adapter
            # catches it and for its reason: the provider SDKs share no base
            # class, and each of these means no answer came back. An
            # interrupt is not an ``Exception`` and passes.
            raise HypothesistError(f"{self._label} did not answer: {exc}") from exc
        reply = read_reply(raw, self._label)
        self._trace.write(
            TraceKind.MODEL,
            state,
            bound=bound,
            text=reply.text[:NOTE_CAP],
            text_chars=len(reply.text),
            tool_calls=len(reply.calls),
            input_tokens=reply.input_tokens,
            output_tokens=reply.output_tokens,
            elapsed_ms=_elapsed_ms(started),
        )
        return reply

    def _run(self, call: ToolCall, state: _Round) -> dict[str, str]:
        """One call of the model's, answered: run if the round allows it, written down either way.

        Three things keep a call from being run, asked in this order: no
        call is left in the round (which is not counted — the count is
        full), the call cannot be sent, no backtest is left. The last
        two count as calls.
        """
        limits = self._pack.limits
        started = time.monotonic()
        if state.lookups >= limits.lookups:
            state.told_spent = True
            return self._ran(call, state, started, SPENT, limit=Limit.LOOKUPS)
        state.lookups += 1
        ended: dict[str, Any]
        if call.arguments is None:
            body, ended = NOT_SENDABLE, {"tool_error": True}
        elif call.name == BACKTEST_TOOL and state.backtests >= limits.backtests:
            body, ended = BACKTESTS_SPENT, {"limit": Limit.BACKTESTS}
        else:
            try:
                body, refused = self._session.call(call.name, call.arguments)
            except ToolServerError:
                # The call the search ended on is written down too, with no
                # result: which look-up it was is otherwise only on the
                # server's stderr.
                self._ran(call, state, started, None, sent=True)
                raise
            # A backtest counts as one when it returned a score: a rule the
            # parser refused showed the model nothing to climb.
            state.backtests += call.name == BACKTEST_TOOL and not refused
            state.looked.append(
                FORCED_LOOKUP.format(
                    tool=call.name, arguments=json.dumps(call.arguments), result=body
                )
            )
            ended = {"sent": True, "tool_error": refused}
        text = f"{body}\n{_remaining(limits, state.lookups, state.backtests)}"
        return self._ran(call, state, started, text, **ended)

    def _ran(
        self,
        call: ToolCall,
        state: _Round,
        started: float,
        result: str | None,
        *,
        sent: bool = False,
        tool_error: bool = False,
        limit: Limit | None = None,
    ) -> dict[str, str]:
        """Write one call down as it ended, and hand back the message that answers it.

        ``sent`` says the call was handed to the server, not that the
        server took it: with no ``result``, the seam failed on the way.
        """
        state.tool_errors += tool_error
        self._trace.write(
            TraceKind.TOOL,
            state,
            tool=call.name,
            arguments=call.arguments,
            sent=sent,
            tool_error=tool_error,
            limit=None if limit is None else limit.value,
            result=None if result is None else result[:RESULT_CAP],
            result_chars=0 if result is None else len(result),
            elapsed_ms=_elapsed_ms(started),
        )
        return tool_message(call.id, result or "", error=tool_error or limit is not None)

    def _forced(self, system: str, user: str, state: _Round) -> str:
        """The rule asked for once more with no tools, the round's look-ups written out as text.

        A fresh conversation on the client as the research package built
        it, under its own system prompt: there is no tool to describe. What
        is written out is each call the server answered, with its result in
        full, and each line the model wrote in a message that made calls
        (cut to :data:`FORCED_NOTE_CAP`); a call the arm did not run is not
        listed, and the message that ended the conversation is left out
        whole. A reply that asks for a tool all the same came from a client
        that still has them, and is no answer.
        """
        asked = [user, ""]
        if state.looked:
            asked += [FORCED_HEADER, *state.looked, ""]
        asked.append(FORCED_ASK)
        reply = self._ask([("system", system), ("human", "\n".join(asked))], state, bound=False)
        if reply.calls:
            raise HypothesistError(
                f"{self._label} asked for a tool when it was given none: its client is not the "
                f"one the research package built"
            )
        return self._answer(reply, state, forced=True)

    def _answer(self, reply: Reply, state: _Round, *, forced: bool) -> str:
        self._trace.write(
            TraceKind.ANSWER,
            state,
            forced=forced,
            lookups=state.lookups,
            backtests=state.backtests,
            tool_errors=state.tool_errors,
            text=reply.text[:NOTE_CAP],
            text_chars=len(reply.text),
        )
        return reply.text


def _require_fresh(trace: Path, server_log: Path | None) -> None:
    """Refuse a trace or a log that cannot be begun, before a server is started for it.

    Each is one search's: a file that exists is not written over — the log
    of an earlier search is the one place its server said why — and the two
    are two files. A directory that is not there is refused here too, and
    not after the server has warmed for nothing.
    """
    named = (
        [("trace", trace)] if server_log is None else [("trace", trace), ("server_log", server_log)]
    )
    for what, path in named:
        if path.exists():
            raise ToolArmError(
                f"{what}: {path} exists: it is one search's, and is not written over"
            )
        if not path.parent.is_dir():
            raise ToolArmError(f"{what}: {path.parent} is not a directory to write {path.name} in")
    if server_log is not None and trace.resolve() == server_log.resolve():
        raise ToolArmError(f"trace and server_log name one file, {trace}")


def _require_this_server(info: object) -> None:
    """Refuse a server that is not the look-ups' server at the version this build pins."""
    answered = (getattr(info, "name", None), getattr(info, "version", None))
    if answered != (SERVER_NAME, str(TOOLS_VERSION)):
        raise ToolArmError(
            f"the server answered as {answered[0]!r} version {answered[1]!r}; this arm's wording "
            f"is pinned against {SERVER_NAME!r} version {TOOLS_VERSION}"
        )
