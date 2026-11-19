"""What a search with tools spent (tools plan §3.5, T4): its trace read back, and the record a table reads.

The tool arm (:mod:`.toolarm`) writes a trace as it goes: every model
call, every look-up, every answer. A trace is long, it sits beside the
arm's store under ``arms/`` — which may be pruned once a world is scored —
and no table should have to parse one. So when a tool arm's search is
over, the bench reads its trace once (:func:`read_trace`,
:func:`trace_use`) and keeps what a table needs in a record of its own,
``tool_use.json``, beside ``verdicts.json``: per round how many calls were
made, how many of them were backtests that returned a score, how many were
the model's mistakes, and whether the round was answered without tools;
per search how many times the model was called and the tokens it reported.

``verdicts.json`` is not touched (plan §3.5): what an arm FOUND is read
there, as for every arm, and what a tool arm SPENT finding it is read
here. A run with no tool arm writes no such file.

The record is a function of the traces alone, so it can be rebuilt from
them; it carries the manifest's digest as the other records do, and the
pack the searches were told (its version, its digest, the two limits), so
a table that sets two runs side by side can say they read the same words.

**What is counted.** A round is one answer: the trace's ``answer`` line
carries the round's counts as the arm kept them. A search that ended
because the MODEL did not answer has its answered rounds and its tokens
recorded and is marked (``seam_failed``); the tables count it apart, as
they count that arm's verdict. A search that ended on the tools' side is
not a record at all — the bench stops the run there (decided 2026-11-19) —
so a trace that says so is refused here rather than summarised.

**Tokens are what the provider reported**, summed over the model calls of
a search that CAME BACK and were read, a failed search's included: they
were paid for. The sums are a floor. A call the client retried is in them
once, a reply the arm could not read is not in them at all (the trace has
no line for either), and a call whose reply was short of a count — the
input's, the output's or both, or a figure that is no count — is counted
as unmetered and adds what it did report. :func:`usage_lines` turns the sums
into an estimated cost when it is handed prices (:class:`Prices`); the
prices are the operator's, given where the table is asked for, because a
price is not a condition of a run and a table of them kept here would be
out of date by the next one. The estimate prices every input token alike:
a provider's discount for a cached prefix is not in it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any, Final

from .records import Reader, RecordError, dumps
from .toolarm import (
    BACKTEST_TOOL,
    TRACE_KEYS,
    TRACE_VERSION,
    Limit,
    Limits,
    Seam,
    ToolArmError,
    TraceKind,
)

__all__ = [
    "TOOL_USE_FILENAME",
    "TOOL_USE_VERSION",
    "Prices",
    "RoundUse",
    "SearchUse",
    "ToolUse",
    "ToolUseError",
    "TracePack",
    "read_trace",
    "trace_use",
    "usage_lines",
]

TOOL_USE_FILENAME: Final = "tool_use.json"

# Bumped when a key of ``tool_use.json`` is added, removed or re-read.
TOOL_USE_VERSION: Final = 1

_PACK_DIGEST: Final = re.compile(r"[0-9a-f]{64}")
_TOKENS_PER_PRICE: Final = 1_000_000


class ToolUseError(RecordError):
    """A trace or a tool-use record cannot be read or built, and the sentence says why."""


_read = Reader(ToolUseError, "tool use")


# -- the record ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class RoundUse:
    """One answered round: what the model spent before it answered.

    ``lookups`` is every call that counted, ``backtests`` those of them
    that returned a training score, ``tool_errors`` those of them that were
    the model's mistake (a refused argument, a call that could not be
    sent). ``forced`` says the answer was asked for with no tool at hand.
    """

    lookups: int
    backtests: int
    tool_errors: int
    forced: bool

    def __post_init__(self) -> None:
        _read.whole(self.lookups, "round.lookups", minimum=0)
        _read.whole(self.backtests, "round.backtests", minimum=0)
        _read.whole(self.tool_errors, "round.tool_errors", minimum=0)
        _read.flag(self.forced, "round.forced")
        # A call is at most one of the two: a backtest is counted when it
        # returned a score, a mistake when it was refused.
        if self.backtests + self.tool_errors > self.lookups:
            raise ToolUseError(
                f"round: {self.backtests} backtest(s) and {self.tool_errors} tool error(s) among "
                f"{self.lookups} call(s); each is one of the calls, and no call is both"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "lookups": self.lookups,
            "backtests": self.backtests,
            "tool_errors": self.tool_errors,
            "forced": self.forced,
        }

    @classmethod
    def from_dict(cls, payload: object) -> RoundUse:
        return cls(**_read.exact(payload, ("lookups", "backtests", "tool_errors", "forced"), "round"))


@dataclass(frozen=True)
class SearchUse:
    """One replicate's search: its answered rounds in order, and what its model calls cost.

    ``seam_failed`` says the search ended on a round the model did not
    answer; the rounds before it are here, and so are the tokens. Every
    answered round took at least one model call, and one that looked
    something up, or was answered without tools, a second: the answer came
    in a message of its own. ``unmetered_calls`` counts the calls that
    reported no count, or one of the two only; what such a call did report
    is in the sums.
    """

    replicate: int
    rounds: tuple[RoundUse, ...]
    seam_failed: bool
    model_calls: int
    unmetered_calls: int
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _read.whole(self.replicate, "search.replicate", minimum=0)
        rounds = tuple(_read.sequence(self.rounds, "search.rounds"))
        if any(not isinstance(round_, RoundUse) for round_ in rounds):
            raise ToolUseError("search.rounds: expected RoundUses")
        object.__setattr__(self, "rounds", rounds)
        _read.flag(self.seam_failed, "search.seam_failed")
        took = len(rounds) + sum(round_.lookups > 0 or round_.forced for round_ in rounds)
        _read.whole(self.model_calls, "search.model_calls", minimum=took)
        _read.whole(self.unmetered_calls, "search.unmetered_calls", minimum=0)
        if self.unmetered_calls > self.model_calls:
            raise ToolUseError(
                f"search.unmetered_calls: {self.unmetered_calls} of {self.model_calls} model "
                f"call(s)"
            )
        _read.whole(self.input_tokens, "search.input_tokens", minimum=0)
        _read.whole(self.output_tokens, "search.output_tokens", minimum=0)
        if not self.model_calls and (self.input_tokens or self.output_tokens):
            raise ToolUseError("search: tokens are counted, and no model call came back")

    def to_dict(self) -> dict[str, object]:
        return {
            "replicate": self.replicate,
            "seam_failed": self.seam_failed,
            "model_calls": self.model_calls,
            "unmetered_calls": self.unmetered_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "rounds": [round_.to_dict() for round_ in self.rounds],
        }

    @classmethod
    def from_dict(cls, payload: object) -> SearchUse:
        body = _read.exact(
            payload,
            (
                "replicate",
                "seam_failed",
                "model_calls",
                "unmetered_calls",
                "input_tokens",
                "output_tokens",
                "rounds",
            ),
            "search",
        )
        rounds = tuple(
            RoundUse.from_dict(item) for item in _read.sequence(body["rounds"], "search.rounds")
        )
        return cls(**{**body, "rounds": rounds})


@dataclass(frozen=True)
class TracePack:
    """What a trace's opening line says its search was told: which model, which pack, which limits.

    The limits are the arm's own :class:`~.toolarm.Limits`, so what a
    limit may be is said once, where the arm counts by it.
    """

    model: str
    pack_version: int
    pack_digest: str
    limits: Limits

    def __post_init__(self) -> None:
        _read.text(self.model, "pack.model")
        _read.whole(self.pack_version, "pack.pack_version", minimum=1)
        if not isinstance(self.pack_digest, str) or not _PACK_DIGEST.fullmatch(self.pack_digest):
            raise ToolUseError(
                f"pack.pack_digest: a SHA-256 is 64 lower-case hex digits, got {self.pack_digest!r}"
            )
        if not isinstance(self.limits, Limits):
            raise ToolUseError(f"pack.limits: expected the arm's Limits, got {self.limits!r}")

    @classmethod
    def from_document(cls, body: Mapping[str, Any]) -> TracePack:
        """The pack off a document that carries it: a trace's opening line, and the record.

        Both write it under ``model``, ``pack_version``, ``pack_digest``
        and ``limits``, among their other keys.
        """
        absent = [key for key in ("model", "pack_version", "pack_digest", "limits") if key not in body]
        if absent:
            raise ToolUseError(f"pack: no {', '.join(absent)} in it")
        limits = _read.exact(body["limits"], ("lookups", "backtests"), "pack.limits")
        try:
            held = Limits(**limits)
        except ToolArmError as exc:
            raise ToolUseError(f"pack.{exc}") from exc
        return cls(
            model=body["model"],
            pack_version=body["pack_version"],
            pack_digest=body["pack_digest"],
            limits=held,
        )


@dataclass(frozen=True)
class ToolUse:
    """``tool_use.json``: every tool-arm search of one world, under the one pack they were told.

    One pack for the record because one manifest names one tool arm: the
    replicates share the model, the wording and the limits, and the bench
    holds each trace to that before it builds this. No round spent more
    than the limits allow.
    """

    world_id: str
    manifest_digest: str
    pack: TracePack
    searches: tuple[SearchUse, ...]

    def __post_init__(self) -> None:
        _read.text(self.world_id, "world_id")
        _read.digest(self.manifest_digest, "manifest_digest")
        if not isinstance(self.pack, TracePack):
            raise ToolUseError("pack: expected a TracePack")
        searches = tuple(_read.sequence(self.searches, "searches"))
        if not searches or any(not isinstance(search, SearchUse) for search in searches):
            raise ToolUseError("searches: at least one SearchUse")
        replicates = [search.replicate for search in searches]
        if len(set(replicates)) != len(replicates):
            raise ToolUseError(f"searches: each replicate once, got {replicates}")
        limits = self.pack.limits
        for search in searches:
            for number, round_ in enumerate(search.rounds, start=1):
                if round_.lookups > limits.lookups or round_.backtests > limits.backtests:
                    raise ToolUseError(
                        f"replicate {search.replicate}, round {number}: {round_.lookups} call(s) "
                        f"and {round_.backtests} backtest(s) under limits of {limits.lookups} "
                        f"and {limits.backtests}"
                    )
        object.__setattr__(self, "searches", searches)

    def to_dict(self) -> dict[str, object]:
        return {
            "tool_use_version": TOOL_USE_VERSION,
            "world_id": self.world_id,
            "manifest_digest": self.manifest_digest,
            "model": self.pack.model,
            "pack_version": self.pack.pack_version,
            "pack_digest": self.pack.pack_digest,
            "limits": asdict(self.pack.limits),
            "searches": [search.to_dict() for search in self.searches],
        }

    @classmethod
    def from_dict(cls, payload: object) -> ToolUse:
        _read.version(payload, "tool_use_version", TOOL_USE_VERSION)
        body = _read.exact(
            payload,
            (
                "tool_use_version",
                "world_id",
                "manifest_digest",
                "model",
                "pack_version",
                "pack_digest",
                "limits",
                "searches",
            ),
            "tool use",
        )
        return cls(
            world_id=body["world_id"],
            manifest_digest=body["manifest_digest"],
            pack=TracePack.from_document(body),
            searches=tuple(
                SearchUse.from_dict(item) for item in _read.sequence(body["searches"], "searches")
            ),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> ToolUse:
        return cls.from_dict(_read.loads(text))


# -- a trace, read back -------------------------------------------------------------------------


def read_trace(path: Path) -> list[dict[str, Any]]:
    """The lines of the trace at ``path``, each held to the keys its kind carries.

    A trace this build wrote: the pack first and once, at this build's
    trace version, and every line one of the kinds :mod:`.toolarm` writes
    with exactly that kind's keys. Anything else is refused with the file
    and the line, not read around: the record built from it is what a
    table counts.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolUseError(f"cannot read the trace at {path}: {exc}") from exc
    lines: list[dict[str, Any]] = []
    for number, written in enumerate(text.splitlines(), start=1):
        where = f"{path}:{number}"
        try:
            line = json.loads(written)
        except json.JSONDecodeError as exc:
            raise ToolUseError(f"{where}: not a line of JSON ({exc})") from exc
        kind = _read.enum(TraceKind, _read.mapping(line, where).get("kind"), where)
        _read.exact(line, TRACE_KEYS[kind], where)
        if (kind is TraceKind.PACK) != (number == 1):
            raise ToolUseError(f"{where}: a trace opens with its pack, and holds one")
        if kind is not TraceKind.PACK:
            _read.whole(line["round"], f"{where}: round", minimum=1)
        lines.append(dict(line))
    if not lines:
        raise ToolUseError(f"{path}: an empty trace; a search writes its pack before it asks")
    # By type, as a record's version is read: ``True`` and ``1.0`` equal 1.
    version = lines[0]["trace_version"]
    if type(version) is not int or version != TRACE_VERSION:
        raise ToolUseError(
            f"{path}: this build reads trace version {TRACE_VERSION}, got {version!r}"
        )
    return lines


def _counted(value: object) -> int | None:
    """A token count as a provider reported one, or ``None``: no count, or nothing that is one.

    A negative number is no count either. A provider that sent one would
    otherwise have a finished search refused over a figure that is a floor
    at best.
    """
    return value if type(value) is int and value >= 0 else None


@dataclass
class _RoundLines:
    """What the lines of the round being read add up to, to hold its answer's counts to."""

    asked: int = 0
    counted: int = 0
    scored: int = 0
    mistakes: int = 0
    bound: bool = True

    def read(self, kind: TraceKind, line: Mapping[str, Any]) -> None:
        if kind is TraceKind.MODEL:
            self.asked += 1
            self.bound = line["bound"] is not False
        elif kind is TraceKind.TOOL:
            # As the arm counts (``ToolHypothesist._run``): every call but
            # one made when none was left; a mistake when it was marked
            # one; a backtest when the server took it and returned a score.
            self.counted += line["limit"] != Limit.LOOKUPS.value
            self.mistakes += line["tool_error"] is True
            self.scored += (
                line["tool"] == BACKTEST_TOOL
                and line["sent"] is True
                and line["tool_error"] is False
                and line["limit"] is None
            )

    def hold(self, number: int, answered: RoundUse) -> None:
        """Refuse an answer line whose counts are not what the round's own lines add up to."""
        written = (answered.lookups, answered.backtests, answered.tool_errors, answered.forced)
        read = (self.counted, self.scored, self.mistakes, not self.bound)
        if not self.asked or written != read:
            raise ToolUseError(
                f"round {number}: its answer says {written[0]} call(s), {written[1]} backtest(s), "
                f"{written[2]} mistake(s), forced {written[3]}; its {self.asked} model line(s) "
                f"and its tool lines give {read[0]}, {read[1]}, {read[2]} and {read[3]}"
            )


def trace_use(lines: Sequence[Mapping[str, Any]], replicate: int) -> tuple[TracePack, SearchUse]:
    """What the search behind ``lines`` (:func:`read_trace`'s) was told, and what it spent.

    The rounds are held to the order a search writes them in: numbered
    from one, each ended by its answer, or by the failure the search ended
    on. A round left open, lines after the end, or a failure on the tools'
    side — which the bench does not record (the module docstring) — is
    refused. An answer's counts are the arm's own, written as it went;
    they are held here to what the round's model and tool lines add up to
    and to the pack's limits, so the record is one the trace bears out
    line by line.
    """
    if not lines or lines[0].get("kind") != TraceKind.PACK.value:
        raise ToolUseError("a trace opens with its pack; these lines do not")
    pack = TracePack.from_document(lines[0])
    rounds: list[RoundUse] = []
    seam_failed = False
    # The kind of the last line read: a round is open while it is a model's
    # or a tool's, and ended by an answer or a failure.
    kind = TraceKind.PACK
    within = _RoundLines()
    model_calls = unmetered = tokens_in = tokens_out = 0
    for line in lines[1:]:
        kind = TraceKind(line["kind"])
        number = len(rounds) + 1
        if seam_failed:
            raise ToolUseError(
                f"the trace goes on after round {number}, where the search ended on a failed seam"
            )
        if line["round"] != number:
            raise ToolUseError(
                f"a {kind.value!r} line of round {line['round']!r} where round {number} is being "
                f"written; a search's rounds are written in order, each to its end"
            )
        within.read(kind, line)
        if kind is TraceKind.MODEL:
            model_calls += 1
            reported_in = _counted(line["input_tokens"])
            reported_out = _counted(line["output_tokens"])
            # A call short of either count is unmetered; what it did
            # report is kept, the sums being a floor either way.
            unmetered += reported_in is None or reported_out is None
            tokens_in += reported_in or 0
            tokens_out += reported_out or 0
        elif kind is TraceKind.ANSWER:
            answered = RoundUse(
                lookups=line["lookups"],
                backtests=line["backtests"],
                tool_errors=line["tool_errors"],
                forced=line["forced"],
            )
            within.hold(number, answered)
            if (
                answered.lookups > pack.limits.lookups
                or answered.backtests > pack.limits.backtests
            ):
                raise ToolUseError(
                    f"round {number}: {answered.lookups} call(s) and {answered.backtests} "
                    f"backtest(s) under limits of {pack.limits.lookups} and "
                    f"{pack.limits.backtests}"
                )
            rounds.append(answered)
            within = _RoundLines()
        elif kind is TraceKind.FAILED:
            if line["side"] != Seam.MODEL.value:
                raise ToolUseError(
                    f"round {number} ended on the look-ups' server ({line['error']}): a search "
                    f"that ended on the tools' side is not a result, and is not summarised"
                )
            seam_failed = True
    if kind in (TraceKind.MODEL, TraceKind.TOOL):
        raise ToolUseError(
            f"the trace ends inside round {len(rounds) + 1}: a round ends with its answer, or "
            f"with the failure the search ended on"
        )
    return pack, SearchUse(
        replicate=replicate,
        rounds=tuple(rounds),
        seam_failed=seam_failed,
        model_calls=model_calls,
        unmetered_calls=unmetered,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )


# -- what a table says of it ----------------------------------------------------------------------


@dataclass(frozen=True)
class Prices:
    """What a model's tokens cost, in US dollars per million: the operator's figures (the module docstring)."""

    input: float
    output: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "input", _read.amount(self.input, "prices.input"))
        object.__setattr__(self, "output", _read.amount(self.output, "prices.output"))

    def cost(self, search: SearchUse) -> float:
        return (
            search.input_tokens * self.input + search.output_tokens * self.output
        ) / _TOKENS_PER_PRICE


def usage_lines(
    searches: Sequence[SearchUse], prices: Prices | None = None, *, model: str | None = None
) -> list[str]:
    """What a row's tool-arm searches spent, as the lines a table prints under the row.

    The first line is over the rounds of the searches that finished, the
    ones the row's rate counts: how many calls a round made, how many
    rounds asked for no tool, how many calls were the model's mistakes,
    how many rounds were answered without tools — kept in the row's result
    and printed beside it (decided 2026-11-19). A round answered without
    tools asked for one, so it is not among those that asked for none,
    whatever it had counted by then. The second line is over every search,
    failed seams included, because those were paid for too, and names the
    ``model`` whose tokens they are when it is given; the cost is there
    when ``prices`` are and some call reported both its counts — one pair
    of prices is one model's, and a root may hold rows of several.
    """
    if not searches:
        return []
    finished = [search for search in searches if not search.seam_failed]
    rounds = [round_ for search in finished for round_ in search.rounds]
    if rounds:
        calls = [round_.lookups for round_ in rounds]
        forced = sum(round_.forced for round_ in rounds)
        idle = sum(not round_.lookups and not round_.forced for round_ in rounds)
        use = (
            f"  tool use over {len(rounds)} round(s) of {len(finished)} finished search(es): "
            f"calls a round median {median(calls):g} (max {max(calls)}), {idle} round(s) asked "
            f"for no tool; backtests {sum(round_.backtests for round_ in rounds)}; the model's "
            f"mistakes {sum(round_.tool_errors for round_ in rounds)} of {sum(calls)} call(s); "
            f"answered without tools {forced}/{len(rounds)} = {forced / len(rounds):.1%}"
        )
    else:
        use = "  tool use: no finished search answered a round"
    model_calls = sum(search.model_calls for search in searches)
    unmetered = sum(search.unmetered_calls for search in searches)
    spent = (
        f"  tokens reported{'' if model is None else f' by {model}'} over {len(searches)} "
        f"search(es), failed seams included: "
        f"{sum(search.input_tokens for search in searches):,} in, "
        f"{sum(search.output_tokens for search in searches):,} out, in {model_calls} model "
        f"call(s) that came back"
    )
    if unmetered:
        spent += f", {unmetered} of them short of a count"
    # Said on every row: what a retry cost, and a reply that could not be
    # read, are in no line of a trace.
    spent += " (a floor: a retried call counts once, a reply that was not read not at all)"
    if prices is not None and unmetered < model_calls:
        spent += (
            f"; estimated cost US${sum(prices.cost(search) for search in searches):,.2f} on "
            f"those counts, at US${prices.input:g} in and US${prices.output:g} out a million "
            f"tokens with no cache discount"
        )
    elif prices is not None:
        # No call reported both counts: a figure here would read as a cost.
        spent += "; no cost is estimated: no call reported both its counts"
    return [use, spent]
