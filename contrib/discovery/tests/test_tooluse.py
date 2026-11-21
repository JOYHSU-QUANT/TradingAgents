"""What a search with tools spent: a trace read back, the record built from it, and the lines a table prints (tools plan T4).

The traces here are written by hand, a line at a time, in the shapes
:mod:`contrib.discovery.toolarm` writes — its own table of keys is what
each line is built from, so a key these tests set that the arm has since
removed or renamed is a red test here. (A key the arm ADDS is not: it is
filled in empty. What ties the reader to the arm's counting is the arm's
own suite, which reads a real trace back, and ``test_bench_tools.py``,
where a real search's is.)
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from contrib.discovery.toolarm import BACKTEST_TOOL, TRACE_KEYS, TRACE_VERSION, Limits, TraceKind
from contrib.discovery.tooluse import (
    TOOL_USE_VERSION,
    Prices,
    RoundUse,
    SearchUse,
    ToolUse,
    ToolUseError,
    TracePack,
    read_trace,
    reported_count,
    trace_use,
    usage_lines,
)

DIGEST = "ab" * 32
MODEL = "scripted/model"
PACK = TracePack(
    model=MODEL, pack_version=1, pack_digest=DIGEST, limits=Limits(lookups=5, backtests=2)
)


def _line(kind: TraceKind, **values) -> dict:
    """A trace line of ``kind`` with every key that kind carries, ``values`` over plain defaults."""
    defaults = {
        "trace_version": TRACE_VERSION,
        "pack_version": 1,
        "pack_digest": DIGEST,
        "model": MODEL,
        "limits": {"lookups": 5, "backtests": 2},
        "round": 1,
        "seq": 1,
        "bound": True,
        "input_tokens": 100,
        "output_tokens": 20,
        "lookups": 0,
        "backtests": 0,
        "tool_errors": 0,
        "forced": False,
        "tool": "feature_stats",
        "sent": True,
        "tool_error": False,
        "side": "model",
    }
    line = {key: defaults.get(key) for key in TRACE_KEYS[kind]}
    line["kind"] = kind.value
    unknown = set(values) - set(line)
    assert not unknown, f"{kind.value} lines carry no {sorted(unknown)}"
    return {**line, **values}


def pack_line(**values) -> dict:
    return _line(TraceKind.PACK, **values)


def model(round_: int, **values) -> dict:
    return _line(TraceKind.MODEL, round=round_, **values)


def tool(round_: int, **values) -> dict:
    """A look-up the server answered; ``tool_error`` for one it refused, ``limit`` for one not run."""
    return _line(TraceKind.TOOL, round=round_, **values)


def backtest(round_: int, **values) -> dict:
    return tool(round_, tool=BACKTEST_TOOL, **values)


def answer(round_: int, **values) -> dict:
    return _line(TraceKind.ANSWER, round=round_, **values)


def failed(round_: int, side: str = "model") -> dict:
    return _line(TraceKind.FAILED, round=round_, side=side, error="RuntimeError")


def written(tmp_path, lines) -> list[dict]:
    """``lines`` as a trace file, read back the way the bench reads one."""
    path = tmp_path / "replicate-0.trace.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return read_trace(path)


# Two answered rounds: one that looked two things up, a backtest among
# them, and one answered without tools after a mistake.
TWO_ROUNDS = [
    pack_line(),
    model(1),
    tool(1),
    backtest(1),
    model(1),
    answer(1, lookups=2, backtests=1),
    model(2),
    tool(2, tool_error=True),
    model(2, bound=False),
    answer(2, lookups=1, tool_errors=1, forced=True),
]
SPENT = SearchUse(
    replicate=0,
    rounds=(RoundUse(2, 1, 0, False), RoundUse(1, 0, 1, True)),
    seam_failed=False,
    model_calls=4,
    unmetered_calls=0,
    input_tokens=400,
    output_tokens=80,
)
FLOOR = " (a floor: a retried call counts once, a reply that was not read not at all)"


# -- a trace, read back ------------------------------------------------------------------------


def test_a_trace_gives_what_its_search_was_told_and_what_each_round_spent(tmp_path):
    pack, use = trace_use(written(tmp_path, TWO_ROUNDS), replicate=0)
    assert pack == PACK
    assert use == SPENT


def test_a_round_s_counts_are_what_its_own_lines_add_up_to(tmp_path):
    # Every way the arm ends a call, in one round at the limits: answered,
    # a backtest that scored, one the parser refused (a call and a mistake,
    # no backtest), one that could not be sent (the same), a backtest past
    # its limit (a call, and neither), and a call past the round's limit —
    # written down and counted as nothing.
    lines = [
        pack_line(),
        model(1),
        tool(1),
        backtest(1),
        backtest(1, tool_error=True),
        tool(1, sent=False, tool_error=True),
        backtest(1, sent=False, limit="backtests"),
        tool(1, sent=False, limit="lookups"),
        model(1),
        answer(1, lookups=5, backtests=1, tool_errors=2),
    ]
    _pack, use = trace_use(written(tmp_path, lines), replicate=0)
    assert use.rounds == (RoundUse(lookups=5, backtests=1, tool_errors=2, forced=False),)


def test_a_search_the_model_stopped_answering_keeps_its_rounds_and_its_tokens(tmp_path):
    # Round two's first call came back and its second did not: the call
    # that answered was paid for, and the round is no answer.
    lines = [*TWO_ROUNDS[:6], model(2), tool(2), failed(2)]
    _pack, use = trace_use(written(tmp_path, lines), replicate=3)
    assert use == SearchUse(
        replicate=3,
        rounds=(RoundUse(2, 1, 0, False),),
        seam_failed=True,
        model_calls=3,
        unmetered_calls=0,
        input_tokens=300,
        output_tokens=60,
    )
    # One that failed before any call came back spent nothing and is still a search.
    _pack, dead = trace_use(written(tmp_path, [pack_line(), failed(1)]), replicate=1)
    assert (dead.rounds, dead.seam_failed, dead.model_calls) == ((), True, 0)


@pytest.mark.parametrize(
    ("tokens", "sums"),
    [
        ({"input_tokens": None, "output_tokens": None}, (100, 20)),
        ({"input_tokens": 100, "output_tokens": None}, (200, 20)),
        ({"input_tokens": True, "output_tokens": 20}, (100, 40)),
        ({"input_tokens": -5, "output_tokens": 20}, (100, 40)),
        ({"input_tokens": 100.0, "output_tokens": "20"}, (100, 20)),
    ],
    ids=["neither", "one", "a-flag", "a-negative", "not-whole-numbers"],
)
def test_a_call_short_of_a_count_is_unmetered_and_what_it_did_report_is_kept(
    tmp_path, tokens, sums
):
    # No count, or nothing that is one — a negative among them, which would
    # otherwise have a finished search refused over a figure that is a floor.
    lines = [pack_line(), model(1, **tokens), model(1), answer(1)]
    _pack, use = trace_use(written(tmp_path, lines), replicate=0)
    assert (use.model_calls, use.unmetered_calls) == (2, 1)
    assert (use.input_tokens, use.output_tokens) == sums


@pytest.mark.parametrize(
    ("lines", "sentence"),
    [
        ([pack_line(), model(1), answer(1), model(1), answer(1)], "where round 2 is being written"),
        ([pack_line(), model(2), answer(2)], "where round 1 is being written"),
        ([pack_line(), model(1), tool(1)], "the trace ends inside round 1"),
        ([pack_line(), model(1)], "the trace ends inside round 1"),
        ([pack_line(), failed(1), model(2), answer(2)], "goes on after round 1"),
        (
            [pack_line(), model(1), tool(1), failed(1, side="server")],
            "ended on the look-ups' server",
        ),
        ([pack_line(), model(1), answer(1, lookups=-1)], "round.lookups"),
        ([pack_line(), model(1), answer(1, lookups=1, backtests=2)], "each is one of the calls"),
        ([pack_line(), model(1), answer(1, forced="no")], "round.forced"),
        # An answer's counts are the arm's own word; the round's lines are held to bear them out.
        ([pack_line(), answer(1)], "its 0 model line"),
        ([pack_line(), model(1), answer(1, lookups=1)], r"says 1 call\(s\).* give 0, 0, 0"),
        (
            [pack_line(), model(1), tool(1), model(1), answer(1, lookups=1, backtests=1)],
            r"says 1 call\(s\), 1 backtest\(s\).* give 1, 0, 0",
        ),
        (
            [pack_line(), model(1), tool(1), model(1), answer(1, lookups=1, tool_errors=1)],
            r"1 mistake\(s\).* give 1, 0, 0",
        ),
        ([pack_line(), model(1), answer(1, forced=True)], "forced True.* and False"),
        (
            [pack_line(), model(1), model(1, bound=False), answer(1)],
            "forced False.* and True",
        ),
        (
            [pack_line(), model(1), *[tool(1)] * 6, model(1), answer(1, lookups=6)],
            r"round 1: 6 call\(s\) and 0 backtest\(s\) under limits of 5 and 2",
        ),
        (
            [pack_line(), model(1), *[backtest(1)] * 3, model(1), answer(1, lookups=3, backtests=3)],
            r"round 1: 3 call\(s\) and 3 backtest\(s\) under limits of 5 and 2",
        ),
        ([pack_line(limits={"lookups": 5}), model(1), answer(1)], "pack.limits: expected exactly"),
        ([pack_line(pack_digest="abc"), model(1), answer(1)], "pack.pack_digest"),
        ([pack_line(pack_digest="ab" * 33), model(1), answer(1)], "pack.pack_digest"),
        ([pack_line(model=""), model(1), answer(1)], "pack.model"),
    ],
    ids=[
        "a-round-twice",
        "a-round-skipped",
        "a-round-left-open-on-a-tool",
        "a-round-left-open-on-a-model",
        "lines-after-the-end",
        "the-tools-side",
        "a-negative-count",
        "more-backtests-than-calls",
        "a-flag-that-is-not-one",
        "an-answer-nothing-asked-for",
        "a-call-no-line-shows",
        "a-backtest-no-line-shows",
        "a-mistake-no-line-shows",
        "forced-with-the-tools-still-bound",
        "unbound-and-not-said-to-be-forced",
        "more-calls-than-the-limit",
        "more-backtests-than-the-limit",
        "limits-short-a-key",
        "a-digest-that-is-not-one",
        "a-digest-too-long",
        "no-model",
    ],
)
def test_a_trace_a_search_does_not_write_is_refused(tmp_path, lines, sentence):
    with pytest.raises(ToolUseError, match=sentence):
        trace_use(written(tmp_path, lines), replicate=0)


def test_lines_that_are_no_trace_are_refused_by_name_and_not_by_an_index():
    for lines in ([], [model(1)], [{}]):
        with pytest.raises(ToolUseError, match="a trace opens with its pack"):
            trace_use(lines, replicate=0)
    with pytest.raises(ToolUseError, match="pack: no pack_version, pack_digest, limits in it"):
        TracePack.from_document({"model": MODEL})
    with pytest.raises(ToolUseError, match="pack: no model, pack_version, pack_digest, limits"):
        TracePack.from_document({})


def test_a_count_no_search_could_have_spent_is_no_count(tmp_path):
    # A figure past a million million tokens is a provider's mistake, and one
    # large enough stops a table (a cost is a float) or a record (JSON does
    # not write an integer of thousands of digits).
    assert reported_count(10**12) == 10**12
    assert reported_count(10**12 + 1) is None
    assert reported_count(10**400) is None
    assert reported_count(10**5000) is None
    # And a trace is read by it: such a call is unmetered, and adds what it
    # did report.
    lines = [pack_line(), model(1, input_tokens=10**13, output_tokens=7), model(1), answer(1)]
    _pack, use = trace_use(written(tmp_path, lines), replicate=0)
    assert (use.model_calls, use.unmetered_calls) == (2, 1)
    assert (use.input_tokens, use.output_tokens) == (100, 27)


def test_nothing_is_a_count_a_provider_may_report(tmp_path):
    # Zero is a count: a call that reported it is metered, and adds nothing.
    lines = [pack_line(), model(1, input_tokens=0, output_tokens=0), model(1), answer(1)]
    _pack, use = trace_use(written(tmp_path, lines), replicate=0)
    assert (use.model_calls, use.unmetered_calls) == (2, 0)
    assert (use.input_tokens, use.output_tokens) == (100, 20)


def test_a_trace_file_is_held_line_by_line_to_the_shapes_the_arm_writes(tmp_path):
    path = tmp_path / "trace.jsonl"

    def refused(text: str, sentence: str) -> None:
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ToolUseError, match=sentence):
            read_trace(path)

    def one(line: dict) -> str:
        return json.dumps(line) + "\n"

    good = "".join(one(line) for line in TWO_ROUNDS)
    path.write_text(good, encoding="utf-8")
    assert read_trace(path) == TWO_ROUNDS
    refused("", "an empty trace")
    refused(good + "{not json\n", r"trace\.jsonl:11: not a line of JSON")
    refused(good + "[1]\n", r"trace\.jsonl:11: expected an object")
    refused(good + one({"kind": "thought"}), r"trace\.jsonl:11: .*thought")
    short = {key: value for key, value in model(3).items() if key != "elapsed_ms"}
    refused(good + one(short), r"trace\.jsonl:11: expected exactly the keys")
    refused(good + one({**model(3), "note": 1}), "expected exactly the keys")
    refused(good + one(pack_line()), "opens with its pack, and holds one")
    refused(good.split("\n", 1)[1], r"trace\.jsonl:1: a trace opens with its pack")
    # A round is a whole number from one, and a version this build's by
    # type: ``True`` and ``1.0`` both equal 1.
    for round_ in (True, 0, 1.0, "1"):
        refused(good + one(model(round_)), r"trace\.jsonl:11: round")
    for version in (TRACE_VERSION + 1, True, float(TRACE_VERSION)):
        refused(
            one(pack_line(trace_version=version)),
            f"this build reads trace version {TRACE_VERSION}, got",
        )
    with pytest.raises(ToolUseError, match="cannot read the trace at"):
        read_trace(tmp_path / "missing.jsonl")
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(ToolUseError, match="cannot read the trace at"):
        read_trace(path)


# -- the record ---------------------------------------------------------------------------------

USE = ToolUse(
    world_id="SYN-EB3BC030",
    manifest_digest="0123abcd",
    pack=PACK,
    searches=(SPENT, replace(SPENT, replicate=1, rounds=(), seam_failed=True)),
)


def test_the_record_comes_back_equal_from_its_text_and_says_what_was_told():
    assert ToolUse.loads(USE.dumps()) == USE
    record = json.loads(USE.dumps())
    assert record["tool_use_version"] == TOOL_USE_VERSION
    assert (record["model"], record["pack_version"], record["pack_digest"]) == (MODEL, 1, DIGEST)
    assert record["limits"] == {"lookups": 5, "backtests": 2}
    assert record["searches"][0]["rounds"] == [
        {"lookups": 2, "backtests": 1, "tool_errors": 0, "forced": False},
        {"lookups": 1, "backtests": 0, "tool_errors": 1, "forced": True},
    ]
    assert (record["searches"][1]["seam_failed"], record["searches"][1]["rounds"]) == (True, [])


def _record(**changes) -> dict:
    record = json.loads(USE.dumps())
    for dotted, value in changes.items():
        *parents, last = dotted.split(".")
        body = record
        for key in parents:
            body = body[int(key)] if key.isdigit() else body[key]
        body[last] = value
    return record


@pytest.mark.parametrize(
    ("changes", "sentence"),
    [
        ({"tool_use_version": 2}, "this build reads version 1, got 2"),
        ({"extra": 1}, "tool use: expected exactly the keys"),
        ({"world_id": ""}, "world_id"),
        ({"manifest_digest": "XYZ"}, "manifest_digest"),
        ({"pack_digest": "ab"}, "pack.pack_digest"),
        ({"pack_version": 0}, "pack.pack_version"),
        ({"limits.lookups": 0}, "pack.limits.lookups"),
        ({"limits.backtests": 6}, "pack.limits.backtests: expected 0 to 5"),
        ({"limits.extra": 1}, "pack.limits: expected exactly the keys"),
        ({"searches": []}, "searches: at least one SearchUse"),
        ({"searches.1.replicate": 0}, "each replicate once"),
        ({"searches.0.rounds.0.lookups": 6}, "replicate 0, round 1: 6 call"),
        # A refusal inside a search or a round says which: thirty rounds a search, several searches.
        ({"searches.0.rounds.0.backtests": 3}, r"searches\[0\]: rounds\[0\]: round: 3 backtest"),
        ({"searches.1.model_calls": -1}, r"searches\[1\]: search.model_calls"),
        ({"searches.0.rounds.1.tool_errors": 2}, "0 backtest.s. and 2 tool error.s. among 1 call"),
        # Each within the calls, and too many together: no call is both.
        ({"searches.0.rounds.0.tool_errors": 2}, "1 backtest.s. and 2 tool error.s. among 2 call"),
        ({"searches.0.rounds.0.extra": 1}, "round: expected exactly the keys"),
        ({"searches.0.model_calls": 1}, "search.model_calls"),
        # A round that looked something up, or was answered without tools,
        # took a second call: two rounds like that are four at the least.
        ({"searches.0.model_calls": 3}, "search.model_calls: expected a whole number of at least 4"),
        ({"searches.0.unmetered_calls": 5}, "5 of 4 model call"),
        ({"searches.0.input_tokens": -1}, "search.input_tokens"),
        ({"searches.0.seam_failed": 0}, "search.seam_failed"),
        ({"searches.0.extra": 1}, "search: expected exactly the keys"),
    ],
)
def test_a_record_that_is_not_one_a_search_wrote_is_refused(changes, sentence):
    with pytest.raises(ToolUseError, match=sentence):
        ToolUse.from_dict(_record(**changes))


def test_a_search_that_no_call_came_back_from_counts_no_tokens():
    dead = replace(
        SPENT, rounds=(), seam_failed=True, model_calls=0, input_tokens=0, output_tokens=0
    )
    for counted in ({"input_tokens": 5}, {"output_tokens": 5}):
        with pytest.raises(ToolUseError, match="tokens are counted, and no model call came back"):
            replace(dead, **counted)
    # A search that answered no round ended on a failed seam: a budget is a
    # round at least, so one said to have finished with none is no search.
    with pytest.raises(ToolUseError, match="answered no round ended on a failed seam"):
        replace(dead, seam_failed=False)
    # A round answered without tools took a second call, whatever it had
    # counted: the answer came in a message of its own.
    forced = RoundUse(0, 0, 0, True)
    assert replace(SPENT, rounds=(forced,), model_calls=2).model_calls == 2
    with pytest.raises(ToolUseError, match="search.model_calls: expected a whole number of at least 2"):
        replace(SPENT, rounds=(forced,), model_calls=1)
    # Calls that came back short of a count may still have reported the other.
    half = replace(SPENT, unmetered_calls=4)
    assert (half.input_tokens, half.unmetered_calls) == (400, 4)


def test_a_pack_s_limits_are_the_arm_s_own():
    # One statement of what a limit may be: the arm's, read back through it.
    assert PACK.limits == Limits(lookups=5, backtests=2)
    with pytest.raises(ToolUseError, match="pack.limits: expected the arm's Limits"):
        replace(PACK, limits=(5, 2))


def test_a_round_may_spend_the_limits_and_no_more():
    full = replace(SPENT, rounds=(RoundUse(5, 2, 3, True),))
    assert ToolUse(USE.world_id, USE.manifest_digest, PACK, (full,)).searches == (full,)
    for over in (RoundUse(6, 2, 0, False), RoundUse(5, 3, 0, False)):
        with pytest.raises(ToolUseError, match="under limits of 5 and 2"):
            ToolUse(USE.world_id, USE.manifest_digest, PACK, (replace(SPENT, rounds=(over,)),))


# -- what a table says of it ---------------------------------------------------------------------


def test_a_row_s_lines_count_finished_rounds_and_every_search_s_tokens():
    dead = replace(
        SPENT,
        replicate=1,
        rounds=(RoundUse(4, 2, 0, False),),
        seam_failed=True,
        model_calls=3,
        unmetered_calls=1,
        input_tokens=1_000_000,
        output_tokens=200_000,
    )
    use, tokens = usage_lines([SPENT, dead])
    # The failed search's round is not among the rounds: its verdict is not
    # in the row's rate either. Its tokens are in the sum: they were paid for.
    assert use == (
        "  tool use over 2 round(s) of 1 finished search(es): calls a round median 1.5 (max 2), "
        "0 round(s) asked for no tool; backtests 1; the model's mistakes 1 of 3 call(s); "
        "answered without tools 1/2 = 50.0%"
    )
    assert tokens == (
        "  tokens reported over 2 search(es), failed seams included: 1,000,400 in, 200,080 out, "
        f"in 7 model call(s) that came back, 1 of them short of a count{FLOOR}"
    )
    _use, priced = usage_lines([SPENT, dead], Prices(input=3, output=15))
    assert priced == (
        f"{tokens}; estimated cost US$6.00 on those counts, at US$3 in and US$15 out a million "
        f"tokens with no cache discount"
    )
    # Whose tokens they are, where the row knows: one pair of prices is one model's.
    _use, named = usage_lines([SPENT, dead], model=MODEL)
    assert named.startswith(f"  tokens reported by {MODEL} over 2 search(es), failed seams")


def test_a_row_with_no_tool_arm_prints_nothing_and_one_with_no_finished_search_says_so():
    assert usage_lines([]) == []
    assert usage_lines([], Prices(1, 1)) == []
    dead = replace(SPENT, rounds=(), seam_failed=True, model_calls=0, input_tokens=0, output_tokens=0)
    assert usage_lines([dead]) == [
        "  tool use: no finished search answered a round",
        "  tokens reported over 1 search(es), failed seams included: 0 in, 0 out, in 0 model "
        f"call(s) that came back{FLOOR}",
    ]


def test_a_round_that_asked_for_no_tool_is_not_one_that_was_answered_without_them():
    # A round that looked nothing up and answered asked for no tool. One
    # answered without tools DID ask — for a call that could not be sent
    # back, say, and so counted none — and is not among those.
    idle = replace(SPENT, rounds=(RoundUse(0, 0, 0, False), RoundUse(3, 0, 0, False)))
    assert "1 round(s) asked for no tool" in usage_lines([idle])[0]
    forced = replace(SPENT, rounds=(RoundUse(0, 0, 0, True), RoundUse(3, 0, 0, False)))
    line = usage_lines([forced])[0]
    assert "0 round(s) asked for no tool" in line and "answered without tools 1/2" in line


def test_no_cost_is_printed_where_no_call_reported_its_counts():
    blind = replace(SPENT, unmetered_calls=4, input_tokens=0, output_tokens=0)
    line = usage_lines([blind], Prices(input=3, output=15))[1]
    assert line.endswith(f"{FLOOR}; no cost is estimated: no call reported both its counts")
    assert "US$" not in line
    # Nor where no call came back at all, which is said as that.
    dead = replace(blind, rounds=(), seam_failed=True, model_calls=0, unmetered_calls=0)
    assert usage_lines([dead], Prices(3, 15))[1].endswith("; no cost is estimated: no call came back")
    # One call that did is enough for a figure, said to be on those counts.
    some = replace(blind, unmetered_calls=3, input_tokens=100, output_tokens=20)
    assert "estimated cost US$0.00 on those counts" in usage_lines([some], Prices(3, 15))[1]


def test_prices_are_amounts_and_a_cost_is_tokens_at_a_price_a_million():
    assert Prices(input=3, output=15).cost(SPENT) == pytest.approx((400 * 3 + 80 * 15) / 1e6)
    assert Prices(input=0, output=0).cost(SPENT) == 0
    for bad in ({"input": -1, "output": 1}, {"input": 1, "output": float("nan")}):
        with pytest.raises(ToolUseError, match="prices"):
            Prices(**bad)
