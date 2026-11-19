"""What a search with tools spent: a trace read back, the record built from it, and the lines a table prints (tools plan T4).

The traces here are written by hand, a line at a time, in the shapes
:mod:`contrib.discovery.toolarm` writes — its own table of keys is what
each line is built from, so a key added there without a thought for the
reader is a red test here. A trace written by a real search is read in
``test_bench_tools.py``.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from contrib.discovery.toolarm import TRACE_KEYS, TRACE_VERSION, Limits, TraceKind
from contrib.discovery.tooluse import (
    TOOL_USE_VERSION,
    Prices,
    RoundUse,
    SearchUse,
    ToolUse,
    ToolUseError,
    TracePack,
    read_trace,
    trace_use,
    usage_lines,
)

DIGEST = "ab" * 32
PACK = TracePack(
    model="scripted/model", pack_version=1, pack_digest=DIGEST, limits=Limits(lookups=5, backtests=2)
)


def _line(kind: TraceKind, **values) -> dict:
    """A trace line of ``kind`` with every key that kind carries, ``values`` over plain defaults."""
    defaults = {
        "trace_version": TRACE_VERSION,
        "pack_version": 1,
        "pack_digest": DIGEST,
        "model": "scripted/model",
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
    return _line(TraceKind.TOOL, round=round_, **values)


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
    tool(1),
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


# -- a trace, read back ------------------------------------------------------------------------


def test_a_trace_gives_what_its_search_was_told_and_what_each_round_spent(tmp_path):
    pack, use = trace_use(written(tmp_path, TWO_ROUNDS), replicate=0)
    assert pack == PACK
    assert use == SPENT


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
    "tokens",
    [
        {"input_tokens": None, "output_tokens": None},
        {"input_tokens": 100, "output_tokens": None},
        {"input_tokens": True, "output_tokens": 20},
    ],
    ids=["neither", "one", "a-flag"],
)
def test_a_call_that_reported_no_counts_is_unmetered_and_adds_nothing(tmp_path, tokens):
    lines = [pack_line(), model(1, **tokens), model(1), answer(1)]
    _pack, use = trace_use(written(tmp_path, lines), replicate=0)
    assert (use.model_calls, use.unmetered_calls) == (2, 1)
    assert (use.input_tokens, use.output_tokens) == (100, 20)


@pytest.mark.parametrize(
    ("lines", "sentence"),
    [
        ([pack_line(), model(1), answer(1), model(1), answer(1)], "where round 2 is being written"),
        ([pack_line(), model(2), answer(2)], "where round 1 is being written"),
        ([pack_line(), model(1), tool(1)], "the trace ends inside round 1"),
        ([pack_line(), failed(1), model(2), answer(2)], "goes on after round 1"),
        (
            [pack_line(), model(1), tool(1), failed(1, side="server")],
            "ended on the look-ups' server",
        ),
        ([pack_line(), model(1), answer(1, lookups=-1)], "round.lookups"),
        ([pack_line(), model(1), answer(1, lookups=1, backtests=2)], "each is one of the calls"),
        ([pack_line(), model(1), answer(1, forced="no")], "round.forced"),
        ([pack_line(limits={"lookups": 5}), model(1), answer(1)], "pack.limits: expected exactly"),
        ([pack_line(pack_digest="abc"), model(1), answer(1)], "pack.pack_digest"),
        ([pack_line(model=""), model(1), answer(1)], "pack.model"),
    ],
    ids=[
        "a-round-twice",
        "a-round-skipped",
        "a-round-left-open",
        "lines-after-the-end",
        "the-tools-side",
        "a-negative-count",
        "more-backtests-than-calls",
        "a-flag-that-is-not-one",
        "limits-short-a-key",
        "a-digest-that-is-not-one",
        "no-model",
    ],
)
def test_a_trace_a_search_does_not_write_is_refused(tmp_path, lines, sentence):
    with pytest.raises(ToolUseError, match=sentence):
        trace_use(written(tmp_path, lines), replicate=0)


def test_a_trace_file_is_held_line_by_line_to_the_shapes_the_arm_writes(tmp_path):
    path = tmp_path / "trace.jsonl"

    def refused(text: str, sentence: str) -> None:
        path.write_text(text, encoding="utf-8")
        with pytest.raises(ToolUseError, match=sentence):
            read_trace(path)

    good = "".join(json.dumps(line) + "\n" for line in TWO_ROUNDS)
    path.write_text(good, encoding="utf-8")
    assert read_trace(path) == TWO_ROUNDS
    refused("", "an empty trace")
    refused(good + "{not json\n", r"trace\.jsonl:11: not a line of JSON")
    refused(good + "[1]\n", r"trace\.jsonl:11: expected an object")
    refused(good + json.dumps({"kind": "thought"}) + "\n", r"trace\.jsonl:11: .*thought")
    short = {key: value for key, value in model(3).items() if key != "elapsed_ms"}
    refused(good + json.dumps(short) + "\n", r"trace\.jsonl:11: expected exactly the keys")
    refused(good + json.dumps({**model(3), "note": 1}) + "\n", "expected exactly the keys")
    refused(good + json.dumps(pack_line()) + "\n", "opens with its pack, and holds one")
    refused(good.split("\n", 1)[1], r"trace\.jsonl:1: a trace opens with its pack")
    later = json.dumps(pack_line(trace_version=TRACE_VERSION + 1)) + "\n"
    refused(later, f"this build reads trace version {TRACE_VERSION}, got {TRACE_VERSION + 1}")
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
    assert (record["model"], record["pack_version"], record["pack_digest"]) == (
        "scripted/model",
        1,
        DIGEST,
    )
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
        ({"searches.0.rounds.0.backtests": 3}, "each is one of the calls"),
        ({"searches.0.rounds.1.tool_errors": 2}, "0 backtest.s. and 2 tool error.s. among 1 call"),
        ({"searches.0.rounds.0.extra": 1}, "round: expected exactly the keys"),
        ({"searches.0.model_calls": 1}, "search.model_calls"),
        ({"searches.0.unmetered_calls": 5}, "5 of 4 model call"),
        ({"searches.0.unmetered_calls": 4}, "no model call reported any"),
        ({"searches.0.input_tokens": -1}, "search.input_tokens"),
        ({"searches.0.seam_failed": 0}, "search.seam_failed"),
    ],
)
def test_a_record_that_is_not_one_a_search_wrote_is_refused(changes, sentence):
    with pytest.raises(ToolUseError, match=sentence):
        ToolUse.from_dict(_record(**changes))


def test_a_pack_s_limits_are_the_arm_s_own():
    # One statement of what a limit may be: the arm's, read back through it.
    assert PACK.limits == Limits(lookups=5, backtests=2)
    with pytest.raises(ToolUseError, match="pack.limits: expected the arm's Limits"):
        replace(PACK, limits=(5, 2))


def test_a_round_may_spend_the_limits_and_no_more():
    full = replace(SPENT, rounds=(RoundUse(5, 2, 5, True),))
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
        "0 round(s) made none; backtests 1; the model's mistakes 1 of 3 call(s); answered "
        "without tools 1/2 = 50.0%"
    )
    assert tokens == (
        "  tokens reported over 2 search(es), failed seams included: 1,000,400 in, 200,080 out, "
        "in 7 model call(s) that came back, 1 of them with no counts (a floor: a retried call "
        "counts once, a reply that was not read not at all)"
    )
    _use, priced = usage_lines([SPENT, dead], Prices(input=3, output=15))
    assert priced == (
        f"{tokens}; estimated cost US$6.00 on those counts, at US$3 in and US$15 out a million "
        f"tokens with no cache discount"
    )


def test_a_row_with_no_tool_arm_prints_nothing_and_one_with_no_finished_search_says_so():
    assert usage_lines([]) == []
    assert usage_lines([], Prices(1, 1)) == []
    dead = replace(SPENT, rounds=(), seam_failed=True, model_calls=0, input_tokens=0, output_tokens=0)
    assert usage_lines([dead]) == [
        "  tool use: no finished search answered a round",
        "  tokens reported over 1 search(es), failed seams included: 0 in, 0 out, in 0 model "
        "call(s) that came back (a floor: a retried call counts once, a reply that was not read "
        "not at all)",
    ]
    # A round that looked nothing up is counted as one that made no call.
    idle = replace(SPENT, rounds=(RoundUse(0, 0, 0, False), RoundUse(3, 0, 0, False)))
    assert "1 round(s) made none" in usage_lines([idle])[0]


def test_prices_are_amounts_and_a_cost_is_tokens_at_a_price_a_million():
    assert Prices(input=3, output=15).cost(SPENT) == pytest.approx((400 * 3 + 80 * 15) / 1e6)
    assert Prices(input=0, output=0).cost(SPENT) == 0
    for bad in ({"input": -1, "output": 1}, {"input": 1, "output": float("nan")}):
        with pytest.raises(ToolUseError, match="prices"):
            Prices(**bad)
