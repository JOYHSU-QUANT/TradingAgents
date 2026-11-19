"""The tool arm run by the bench (tools plan §4, T4 a–d): what a search leaves, what the tables say, what stops a run.

One real run, once per module (``tooled``): the example world under a
manifest that names the tool arm, a scripted chat client standing in for
the model, and the look-ups' server started for each replicate as the
stdio child the bench starts — so the store the search writes to and the
server reads is one file opened by two processes, which is the part no
in-memory server shows. The first replicate answers its two rounds; the
script is then dry, so the second ends where a provider that went away
would end it.

The run's records are read for (a) and (b), set beside the session's
search of the same world without the arm for (c), and (d) is the last
group: a manifest that names no tool arm, a version-3 one included, leaves
and reads what it always did.

The ways the TOOL ARM's side can fail each stop a run and keep what it
left, and are run on fresh directories: a server that dies in a round or
never starts, an arm that cannot be opened once its server is up, a trace
that cannot be written, or read back, or is not its search's, and searches
that do not make a record. The marking of what is kept — by name, by note,
under an interrupt — is tested on its own.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib.autoresearch import upstream as research_upstream
from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.store import ResearchStore
from contrib.discovery import bench, cli, scoring, toolarm
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    INQUIRY_FILENAME,
    ORACLE_FILENAME,
    VERDICTS_FILENAME,
    Arm,
    ArmResult,
    BenchError,
    KeptForInquiry,
    calibration_table,
    experiment_id,
    read_searched,
    require_artifact,
    run_world,
    server_log_of,
    store_path,
    tool_arm,
    trace_of,
)
from contrib.discovery.cli import EXIT_KEPT_FOR_INQUIRY, main
from contrib.discovery.landing import MANIFEST_FILENAME
from contrib.discovery.manifest import LlmToolsArm, Manifest
from contrib.discovery.scoring import SCORES_FILENAME, ScoreError, report_table, score_world
from contrib.discovery.toolarm import PACK_VERSION, Limits, Pack, ToolArmError, addendum
from contrib.discovery.tools_server import tool_specs
from contrib.discovery.tooluse import (
    TOOL_USE_FILENAME,
    Prices,
    RoundUse,
    SearchUse,
    ToolUse,
    ToolUseError,
    TracePack,
    read_trace,
)
from contrib.discovery.verdict import Ended

from .conftest import COIN, PLANTED, bench_manifest, land_run
from .test_bench import LANDED, _listing, _quick_oracle
from .test_toolarm import _FAILING, ANSWER, BACKTEST, FATAL, STATS, UNKNOWN, Scripted, says
from .test_tooluse import answer as trace_answer, model as trace_model

# Not the arm's defaults, so the limits a model is told are seen to be the manifest's.
LIMITS = Limits(lookups=4, backtests=1)
TOOLS = LlmToolsArm(
    provider="scripted",
    model="model",
    temperature=0.0,
    replicates=2,
    world_subset=(COIN,),
    lookups=LIMITS.lookups,
    backtests=LIMITS.backtests,
    pack_version=PACK_VERSION,
)
# What the arm's searches are filed under: the model, as the research package labels it.
LABEL = f"{TOOLS.provider}/{TOOLS.model}"
# What each call of the scripted model reports having cost.
TOKENS = (120_000, 30_000)


def tools_manifest(**changes) -> Manifest:
    """The bench's small manifest, naming the tool arm (``changes`` over :data:`TOOLS`)."""
    manifest = bench_manifest()
    return replace(manifest, arms=replace(manifest.arms, llm_tools=replace(TOOLS, **changes)))


def scripted(patch, script) -> Scripted:
    """The model every arm of the run is built with: a scripted client under the manifest's name."""
    model = Scripted(script)
    patch.setattr(
        bench.ChatHypothesist,
        "build",
        lambda provider, name, **kwargs: bench.ChatHypothesist(
            model=model, label=f"{provider}/{name}"
        ),
    )
    return model


# Replicate 0. Round one: two look-ups asked for in one message, a backtest
# among them, then a rule outside the pool. Round two: a feature that does
# not exist, then an answer the parser refuses. Replicate 1 finds the
# script dry at its first call.
SCRIPT = [
    says("What does the close look like, and what does holding earn?", STATS, BACKTEST, tokens=TOKENS),
    says(json.dumps(PLANTED), tokens=TOKENS),
    says("A feature that is not there.", UNKNOWN, tokens=TOKENS),
    says("not a rule", tokens=TOKENS),
]
SPENT = (
    SearchUse(
        replicate=0,
        rounds=(RoundUse(2, 1, 0, False), RoundUse(1, 0, 1, False)),
        seam_failed=False,
        model_calls=4,
        unmetered_calls=0,
        input_tokens=4 * TOKENS[0],
        output_tokens=4 * TOKENS[1],
    ),
    SearchUse(
        replicate=1,
        rounds=(),
        seam_failed=True,
        model_calls=0,
        unmetered_calls=0,
        input_tokens=0,
        output_tokens=0,
    ),
)
USE_LINE = (
    "  tool use over 2 round(s) of 1 finished search(es): calls a round median 1.5 (max 2), 0 "
    "round(s) asked for no tool; backtests 1; the model's mistakes 1 of 3 call(s); answered "
    "without tools 0/2 = 0.0%"
)
TOKENS_LINE = (
    f"  tokens reported by {LABEL} over 2 search(es), failed seams included: 480,000 in, 120,000 "
    f"out, in 4 model call(s) that came back (a floor: a retried call counts once, a reply that "
    f"was not read not at all)"
)
COST = (
    "; estimated cost US$3.24 on those counts, at US$3 in and US$15 out a million tokens with no "
    "cache discount"
)


@pytest.fixture(scope="module")
def tooled(landed_world, tmp_path_factory):
    """The example world searched once with the tool arm, and scored: the directory, the records, the model."""
    artifact, _landing = landed_world
    run = land_run(tmp_path_factory.mktemp("bench-tools"), artifact, tools_manifest())
    lines: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        model = scripted(patch, SCRIPT)
        verdicts, oracle = run_world(run, on_line=lines.append)
    score_world(run)
    return run, verdicts, oracle, model, lines


def _timeless(result: ArmResult) -> dict:
    """A result as its record, without the one thing two runs of one search differ in."""
    document = result.to_dict()
    document["snapshot"]["experiment"]["created_at"] = ""
    return json.loads(json.dumps(document))


def _copy(run: Path, tmp_path: Path) -> Path:
    """A searched directory's manifest and records under its own name, to be edited."""
    target = tmp_path / run.name
    target.mkdir()
    for name in (MANIFEST_FILENAME, VERDICTS_FILENAME, ORACLE_FILENAME, TOOL_USE_FILENAME):
        if (run / name).exists():
            (target / name).write_bytes((run / name).read_bytes())
    return target


# -- (a) what a search with the tool arm leaves ---------------------------------------------------


def test_the_tool_arm_searches_last_over_a_server_of_its_own_and_leaves_its_trace(tooled):
    run, verdicts, _oracle, model, _lines = tooled
    assert [(r.arm, r.replicate) for r in verdicts.results] == [
        (Arm.RANDOM, 0),
        (Arm.GRID, 0),
        (Arm.LLM_TOOLS, 0),
        (Arm.LLM_TOOLS, 1),
    ]
    assert _listing(run) == sorted(
        [*LANDED, ARMS_DIRNAME, VERDICTS_FILENAME, TOOL_USE_FILENAME, ORACLE_FILENAME, SCORES_FILENAME]
    )
    # Each replicate's store, its trace and its server's stderr, side by side.
    assert _listing(run / ARMS_DIRNAME / Arm.LLM_TOOLS.value) == [
        "replicate-0.server.log",
        "replicate-0.sqlite",
        "replicate-0.trace.jsonl",
        "replicate-1.server.log",
        "replicate-1.sqlite",
        "replicate-1.trace.jsonl",
    ]
    for replicate in (0, 1):
        store = store_path(run, Arm.LLM_TOOLS, replicate)
        assert store.is_file()
        # A server with nothing to report says nothing.
        assert server_log_of(store).read_text(encoding="utf-8") == ""
    # The landed artifact is still the one the manifest landed: every arm,
    # and every server, worked on a copy.
    require_artifact(run, read_searched(run).manifest)

    alive, dead = verdicts.results[2], verdicts.results[3]
    assert (alive.snapshot.ended, alive.snapshot.answers) == (Ended.BUDGET_SPENT, 2)
    # The rule outside the pool was measured and filed; "not a rule" spent a round.
    assert len(alive.snapshot.trials) == 1
    pool = {c.spec_hash for c in read_searched(run).manifest.search_space.candidates}
    assert alive.snapshot.trials[0].spec_hash not in pool
    assert (dead.snapshot.ended, dead.snapshot.answers) == (Ended.SEAM_FAILED, 0)
    # Filed under the model's own name, as the model arm files.
    with ResearchStore(store_path(run, Arm.LLM_TOOLS, 0)) as store:
        rows = store.conn.execute(
            "SELECT DISTINCT model FROM proposals WHERE experiment_id = ?",
            (experiment_id(Arm.LLM_TOOLS, 0),),
        ).fetchall()
    assert [row[0] for row in rows] == [LABEL]

    # The trace of the search that answered: the pack this build pins under
    # the manifest's limits, then each round to its answer.
    lines = read_trace(trace_of(store_path(run, Arm.LLM_TOOLS, 0)))
    assert [line["kind"] for line in lines] == [
        "pack",
        "model",
        "tool",
        "tool",
        "model",
        "answer",
        "model",
        "tool",
        "model",
        "answer",
    ]
    assert lines[0]["pack_digest"] == Pack.of(tool_specs(), LIMITS).digest()
    assert (lines[0]["model"], lines[0]["limits"]) == (LABEL, {"lookups": 4, "backtests": 1})
    # The server read the arm's own store while the search held it open,
    # and answered from the training bars: a count, and a score.
    stats, backtest = lines[2], lines[3]
    assert (stats["tool"], stats["sent"], stats["tool_error"]) == ("feature_stats", True, False)
    assert '"net_sharpe"' in backtest["result"]
    assert backtest["result"].endswith("[left this round: 2 tool call(s), of which backtest_train: 0]")
    # And of the one that did not: its pack, and where it ended.
    dead_trace = read_trace(trace_of(store_path(run, Arm.LLM_TOOLS, 1)))
    assert [(line["kind"], line.get("side")) for line in dead_trace] == [
        ("pack", None),
        ("failed", "model"),
    ]


def test_the_model_is_told_the_manifest_s_limits_and_nothing_of_the_world(tooled):
    _run, _verdicts, _oracle, model, _lines = tooled
    bound, messages = model.asked[0]
    (_, system), (_, user) = messages[0], messages[1]
    assert bound is True and system.endswith(addendum(LIMITS))
    assert "at most 4 tool call(s), of which at most 1 may be backtest_train" in system
    assert COIN in user
    said = "\n".join(str(message) for _bound, asked in model.asked for message in asked).lower()
    assert "conditional" not in said and "planted" not in said and "null" not in said


def test_what_the_searches_spent_is_a_record_of_its_own_beside_the_verdicts(tooled):
    run, verdicts, _oracle, _model, _lines = tooled
    expected = ToolUse(
        world_id=COIN,
        manifest_digest=verdicts.manifest_digest,
        pack=TracePack(
            model=LABEL,
            pack_version=PACK_VERSION,
            pack_digest=Pack.of(tool_specs(), LIMITS).digest(),
            limits=LIMITS,
        ),
        searches=SPENT,
    )
    assert (run / TOOL_USE_FILENAME).read_bytes() == expected.dumps().encode("utf-8")
    assert read_searched(run).tool_use == expected
    # The verdicts' record is the shape it was: no key of it knows the arm
    # looked anything up.
    record = json.loads((run / VERDICTS_FILENAME).read_text(encoding="utf-8"))
    assert sorted(record) == [
        "manifest_digest",
        "pool_size",
        "results",
        "verdicts_version",
        "world_id",
    ]
    assert sorted(record["results"][2]) == ["arm", "holdout", "replicate", "snapshot", "verdict"]


# -- (b) what the tables say -----------------------------------------------------------------------


def test_the_tables_give_the_tool_arm_a_row_and_the_report_says_what_it_spent(tooled):
    run, _verdicts, _oracle, _model, _lines = tooled
    calibration = {row.arm: row for row in calibration_table([run])}
    assert (calibration[Arm.LLM_TOOLS].searches, calibration[Arm.LLM_TOOLS].failed) == (1, 1)
    assert calibration[Arm.LLM_TOOLS].effort == ((2, 1),)
    rows = {row.arm: row for row in report_table([run])}
    assert list(rows) == [Arm.RANDOM, Arm.GRID, Arm.LLM_TOOLS]
    tools = rows[Arm.LLM_TOOLS]
    assert (tools.searches, tools.failed, tools.uses) == (1, 1, SPENT)
    described = tools.describe()
    assert described[0].startswith(f"conditional-v1 (v1) dev [{tools.conditions}] llm_tools: ")
    assert "1 seam failure(s) not counted" in described[0]
    assert described[-3:] == [
        "  answers median 2 (min 2), distinct rules median 1 (min 1)",
        USE_LINE,
        TOKENS_LINE,
    ]
    # A cost when prices are given, on the tool arm's row and on no other.
    prices = Prices(input=3, output=15)
    assert tools.describe(prices=prices) == [*described[:-1], TOKENS_LINE + COST]
    for arm in (Arm.RANDOM, Arm.GRID):
        assert rows[arm].uses == ()
        assert rows[arm].describe(prices=prices) == rows[arm].describe()
        assert not any("tool use" in line or "tokens" in line for line in rows[arm].describe())


def test_the_report_command_prints_a_cost_when_it_is_given_both_prices(tooled, capsys, monkeypatch):
    run, *_ = tooled
    root = str(run.parent)
    assert main(["report", "--root", root]) == 0
    plain = capsys.readouterr().out
    assert USE_LINE in plain and TOKENS_LINE in plain and "estimated cost" not in plain
    assert main(["report", "--root", root, "--price-in", "3", "--price-out", "15"]) == 0
    priced = capsys.readouterr()
    assert TOKENS_LINE + COST in priced.out and priced.err == ""
    for one in (["--price-in", "3"], ["--price-out", "15"]):
        assert main(["report", "--root", root, *one]) == 1
        captured = capsys.readouterr()
        assert "--price-in and --price-out are given together" in captured.err
        assert captured.out == ""
    # A price that is no price is a usage error naming its flag, before any table.
    for bad in ("-3", "nan", "inf", "cheap"):
        with pytest.raises(SystemExit) as usage:
            main(["report", "--root", root, f"--price-in={bad}", "--price-out", "15"])
        assert usage.value.code == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert (
            f"argument --price-in: expected a number of US dollars at or above zero, got {bad!r}"
            in captured.err
        )
    # Nothing is a price: a model that costs nothing is priced at nothing.
    assert main(["report", "--root", root, "--price-in", "0", "--price-out", "0"]) == 0
    assert "estimated cost US$0.00 on those counts, at US$0 in" in capsys.readouterr().out
    # Prices that priced nothing are said to have, beside the table and not in it.
    plain_row = SimpleNamespace(uses=(), describe=lambda *, unseal, prices: ["a pool arm's row"])
    monkeypatch.setattr(cli, "report_table", lambda run_dirs: [plain_row])
    assert main(["report", "--root", root, "--price-in", "3", "--price-out", "15"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "a pool arm's row\n"
    assert "is a tool arm's, so --price-in and --price-out priced nothing" in captured.err


def test_the_search_command_names_the_tool_use_record_where_there_is_one(
    tooled, searched, capsys, monkeypatch
):
    monkeypatch.setattr(cli, "run_world", lambda run_dir, on_line: None)
    run, *_ = tooled
    assert main(["search", "--run", str(run)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        f"verdicts: {run / VERDICTS_FILENAME}",
        f"tool use: {run / TOOL_USE_FILENAME}",
        f"oracle: {run / ORACLE_FILENAME}",
    ]
    plain, *_ = searched
    assert main(["search", "--run", str(plain)]) == 0
    assert "tool use" not in capsys.readouterr().out


def test_a_row_is_held_to_one_use_for_each_search_of_the_tool_arm(tooled):
    run, *_ = tooled
    rows = {row.arm: row for row in report_table([run])}
    tools, grid = rows[Arm.LLM_TOOLS], rows[Arm.GRID]
    # One use too few; and two, of which two finished where the row has one finished search.
    for wrong in (SPENT[:1], (SPENT[0], replace(SPENT[0], replicate=1))):
        with pytest.raises(ScoreError, match="row.uses"):
            replace(tools, uses=wrong)
    with pytest.raises(ScoreError, match="row.uses: 2 search"):
        replace(grid, uses=SPENT, pack=tools.pack)
    with pytest.raises(ScoreError, match="row.uses: expected SearchUses"):
        replace(tools, uses=("spent",))
    # What the searches were told goes with them, and with nothing else.
    assert (tools.pack.model, tools.pack.limits, grid.pack) == (LABEL, LIMITS, None)
    for row, changes in ((tools, {"uses": ()}), (tools, {"pack": None}), (grid, {"uses": SPENT})):
        with pytest.raises(ScoreError, match="row.pack: what the searches were told"):
            replace(row, **changes)
    with pytest.raises(ScoreError, match="row.pack: expected a TracePack"):
        replace(tools, pack="the pack")


def test_one_row_is_one_pack_and_tallies_every_world_of_its_conditions(tooled, monkeypatch):
    run, *_ = tooled
    searched = read_searched(run)
    # Two worlds of one set of conditions (this one, met twice): the row
    # holds both worlds' searches.
    monkeypatch.setattr(
        scoring, "iter_searched", lambda run_dirs: iter([(run, searched), (run, searched)])
    )
    (tools,) = (row for row in report_table([run]) if row.arm is Arm.LLM_TOOLS)
    assert (tools.searches, tools.failed, tools.uses) == (2, 2, SPENT + SPENT)
    # The conditions name the model, the pack's version and the limits; the
    # pack's WORDS are named by its digest alone. A world told other words
    # under the same version is another build's run, and is not tallied in.
    reworded = replace(searched.tool_use.pack, pack_digest="cd" * 32)
    other = replace(searched, tool_use=replace(searched.tool_use, pack=reworded))
    monkeypatch.setattr(
        scoring, "iter_searched", lambda run_dirs: iter([(run, searched), (run, other)])
    )
    with pytest.raises(ScoreError, match="One row is one pack"):
        report_table([run])


# -- the record, held to the manifest and the verdicts beside it ------------------------------------


def _use_record(run: Path) -> dict:
    return json.loads((run / TOOL_USE_FILENAME).read_text(encoding="utf-8"))


def _rewritten(run: Path, tmp_path: Path, edit) -> Path:
    copy = _copy(run, tmp_path)
    record = _use_record(copy)
    edit(record)
    (copy / TOOL_USE_FILENAME).write_text(json.dumps(record), encoding="utf-8")
    return copy


def _one_round_short(record: dict) -> None:
    record["searches"][0]["rounds"].pop()


@pytest.mark.parametrize(
    ("edit", "sentence"),
    [
        (lambda r: r.update(world_id="SYN-00000000"), "is for SYN-00000000 under manifest"),
        (lambda r: r.update(manifest_digest="00000000"), "under manifest 00000000"),
        (lambda r: r["searches"].pop(), r"replicate\(s\) \[0\]; the manifest names 2"),
        (
            lambda r: r["searches"][1].update(replicate=2),
            r"replicate\(s\) \[0, 2\]; the manifest names 2",
        ),
        (
            lambda r: r["limits"].update(lookups=5),
            r"told pack version 1 under limits of 5 call\(s\) and 1 backtest\(s\); the manifest "
            r"sets 1, 4 and 1",
        ),
        (
            lambda r: r["limits"].update(backtests=2),
            r"under limits of 4 call\(s\) and 2 backtest\(s\); the manifest sets 1, 4 and 1",
        ),
        (lambda r: r.update(pack_version=2), "told pack version 2"),
        (
            lambda r: r.update(model="another/model"),
            f"are those of the model another/model; the manifest's tool arm names {LABEL}",
        ),
        (_one_round_short, r"llm_tools-0's trace answered 1 round\(s\) and its ledger holds 2"),
        (
            # Refused as a record before it is held to anything: a search
            # that answered no round did end on a failed seam.
            lambda r: r["searches"][1].update(seam_failed=False),
            r"searches\[1\]: search: a search that answered no round ended on a failed seam",
        ),
        (
            lambda r: r["searches"][0].update(seam_failed=True),
            "llm_tools-0 ended budget_spent, and its trace ends on a failed seam",
        ),
        (lambda r: r.update(tool_use_version=2), "tool_use.json: tool_use_version"),
    ],
    ids=[
        "another-world",
        "another-manifest",
        "a-replicate-missing",
        "a-replicate-misnumbered",
        "other-limits",
        "another-backtest-limit",
        "another-pack",
        "another-model",
        "a-round-missing",
        "a-failed-seam-unsaid",
        "a-failed-seam-invented",
        "another-version",
    ],
)
def test_a_tool_use_record_that_is_not_its_search_s_is_refused_by_every_reader(
    tooled, tmp_path, edit, sentence
):
    run, *_ = tooled
    copy = _rewritten(run, tmp_path, edit)
    readers = (
        read_searched,
        score_world,
        lambda r: calibration_table([r]),
        lambda r: report_table([r]),
    )
    for reader in readers:
        with pytest.raises(BenchError, match=sentence):
            reader(copy)


def test_a_search_s_use_is_held_to_how_its_verdict_says_it_ended(tooled):
    # Both ways round, on the two things a verdict says of a search: how
    # many answers its ledger holds, and whether it ended on a failed seam.
    _run, verdicts, *_ = tooled
    alive, dead = verdicts.results[2], verdicts.results[3]
    bench._hold_use_to_result("tool_use.json", SPENT[0], alive)
    bench._hold_use_to_result("tool_use.json", SPENT[1], dead)
    with pytest.raises(BenchError, match="llm_tools-0 ended budget_spent, and its trace ends on a"):
        bench._hold_use_to_result("tool_use.json", replace(SPENT[0], seam_failed=True), alive)
    with pytest.raises(BenchError, match=r"answered 0 round\(s\) and its ledger holds 2 answer"):
        bench._hold_use_to_result("tool_use.json", SPENT[1], alive)
    # A search that answered and then lost its model, read as one that finished.
    answered_then_failed = SimpleNamespace(
        experiment_id="llm_tools-1", snapshot=SimpleNamespace(answers=2, ended=Ended.SEAM_FAILED)
    )
    with pytest.raises(
        BenchError, match="llm_tools-1 ended seam_failed, and its trace ends on no failed seam"
    ):
        bench._hold_use_to_result("tool_use.json", SPENT[0], answered_then_failed)


def test_the_record_is_due_exactly_where_the_manifest_names_the_arm(tooled, searched, tmp_path):
    run, *_ = tooled
    missing = _copy(run, tmp_path)
    (missing / TOOL_USE_FILENAME).unlink()
    with pytest.raises(BenchError, match=f"ran a tool arm and holds no {TOOL_USE_FILENAME}"):
        read_searched(missing)
    # And one beside a search that had no such arm was not written by it.
    plain, *_ = searched
    (tmp_path / "plain").mkdir()
    stray = _copy(plain, tmp_path / "plain")
    (stray / TOOL_USE_FILENAME).write_bytes((run / TOOL_USE_FILENAME).read_bytes())
    with pytest.raises(BenchError, match="names no tool arm to have written it"):
        read_searched(stray)


def test_a_staging_kept_for_inquiry_refuses_its_directory_whatever_else_it_holds(
    tooled, tmp_path
):
    # Beside a whole set of records, too — which no search of this build
    # leaves, and which is not then read as a world searched and done.
    run, *_ = tooled
    copy = _copy(run, tmp_path)
    assert read_searched(copy) is not None
    (copy / ".search.kept-abc").mkdir()
    refusal = r"holds \['\.search\.kept-abc'\], kept on purpose by a search that stopped"
    for reader in (read_searched, run_world, lambda r: calibration_table([r])):
        with pytest.raises(BenchError, match=refusal):
            reader(copy)


def test_a_tool_use_record_left_alone_is_a_search_that_did_not_finish(landed_world, tooled, tmp_path):
    artifact, _landing = landed_world
    run, *_ = tooled
    fresh = land_run(tmp_path, artifact, tools_manifest())
    (fresh / TOOL_USE_FILENAME).write_bytes((run / TOOL_USE_FILENAME).read_bytes())
    with pytest.raises(BenchError, match=r"holds \['tool_use.json'\]: a search of this world"):
        read_searched(fresh)
    # The guard names it among what is to be removed, where it is there.
    with pytest.raises(
        BenchError,
        match=r"searched before.* remove arms/, verdicts.json, tool_use.json, oracle.json by hand",
    ):
        run_world(fresh)


# -- (c) the other arms are what they were ----------------------------------------------------------


def test_the_pool_arms_and_the_oracle_are_what_a_search_without_the_tool_arm_gives(tooled, searched):
    _run, verdicts, oracle, _model, _lines = tooled
    _plain, plain_verdicts, plain_oracle, *_ = searched
    # One world, one seed, one budget: the Random and Grid results of the
    # run with the arm are, key for key, those of the run without it, but
    # for the stamp an experiment is made with.
    assert [_timeless(r) for r in verdicts.results[:2]] == [
        _timeless(r) for r in plain_verdicts.results
    ]
    assert (oracle.planted, oracle.pool) == (plain_oracle.planted, plain_oracle.pool)
    # What differs is what should: the two are two manifests.
    assert verdicts.manifest_digest != plain_verdicts.manifest_digest


# -- the tools' side failing stops the run ----------------------------------------------------------


@pytest.fixture
def fresh_tools_run(landed_world, tmp_path, monkeypatch):
    """A landed directory whose manifest names the tool arm, with what is slow and beside the point stubbed.

    The landing check answers with the session's; the two pool arms end at
    their first round as a dead seam does, which costs no indicator walk
    and still leaves each its store — the tool arm, searched after them,
    is what these runs are about.
    """
    artifact, landing = landed_world
    monkeypatch.setattr(bench, "verify", lambda store, manifest: landing)

    def dead(self, system, user):
        raise HypothesistError("the seam failed")

    monkeypatch.setattr(bench.PoolHypothesist, "propose", dead)
    return land_run(tmp_path, artifact, tools_manifest(replicates=1))


def _kept(run: Path) -> Path:
    (name,) = bench._kept_staging(run)
    return run / name


def _assert_stopped_for_inquiry(run: Path) -> Path:
    """Nothing recorded, the claim and the staging kept, and the next search and the tables refused."""
    staging = _kept(run)
    # Marked by its name, which is what every later reader goes by: a real
    # stop that left it under a staging's ordinary name fell back to the note.
    assert staging.name.startswith(".search.kept-")
    assert _listing(run) == sorted([*LANDED, ARMS_DIRNAME, staging.name])
    assert _listing(run / ARMS_DIRNAME) == []
    # The arms that finished are kept with the one that did not.
    assert _listing(staging / ARMS_DIRNAME) == ["grid", "llm_tools", "random"]
    assert not any((staging / name).exists() for name in (VERDICTS_FILENAME, TOOL_USE_FILENAME))
    # A note in the staging says why it was kept, and the commands that
    # refuse the directory from here on point at it: "remove them by hand"
    # alone would have the evidence removed unread.
    note = (staging / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "looked into, not searched past" in note and str(staging) in note
    refusal = (
        rf"holds \['arms', '{staging.name}'\]: a search of this world is running or did not "
        rf"finish.* but \['{staging.name}'\] was kept on purpose by a search that stopped on its "
        rf"tools' side: read {INQUIRY_FILENAME} in it"
    )
    for reader in (run_world, read_searched, score_world, lambda r: calibration_table([r])):
        with pytest.raises(BenchError, match=refusal) as refused:
            reader(run)
        # The ordinary refusal: only the search that stopped is the one a batch stops on.
        assert not isinstance(refused.value, KeptForInquiry)
    with pytest.raises(BenchError, match=refusal):
        report_table([run])
    return staging


def _assert_cleared_only_with_the_evidence(run: Path, staging: Path) -> None:
    """The claim removed and the staging left is still a world that stopped, to every reader."""
    (run / ARMS_DIRNAME).rmdir()
    refusal = (
        rf"holds \['{staging.name}'\], kept on purpose by a search that stopped on its tools' "
        rf"side: read {INQUIRY_FILENAME} in it.* neither searched nor read while that staging "
        rf"is in it; once it has been looked into, remove it by hand"
    )
    readers = (
        run_world,
        read_searched,
        score_world,
        lambda r: calibration_table([r]),
        lambda r: report_table([r]),
    )
    for reader in readers:
        with pytest.raises(BenchError, match=refusal):
            reader(run)
    # Both gone, the directory is as it was landed: not searched, and may be.
    shutil.rmtree(staging)
    assert _listing(run) == LANDED
    assert read_searched(run) is None


def test_a_server_that_dies_in_a_round_stops_the_run_and_keeps_what_it_left(
    fresh_tools_run, monkeypatch
):
    run = fresh_tools_run
    dying = _FAILING.format(fate="os._exit(3)")
    monkeypatch.setattr(
        bench.ServerParams, "command", lambda self: [sys.executable, "-c", dying, *self.argv()]
    )
    # A round answered, then a look-up the server dies on.
    # (Answers the parser refuses: a refused answer is a round all the same,
    # and costs no measurement these runs are not about.)
    scripted(
        monkeypatch,
        [says("fine", STATS), says("not a rule"), says("fatal", FATAL), says("not a rule")],
    )
    with pytest.raises(KeptForInquiry) as stopped:
        run_world(run)
    said = str(stopped.value)
    assert "the look-ups' server failed in llm_tools-0" in said
    assert "did not answer 'feature_stats'" in said
    assert "looked into, not searched past" in said
    staging = _assert_stopped_for_inquiry(run)
    assert str(staging) in said
    # The trace says where: the round that was answered, then the call the
    # server died on and the side that failed.
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    assert _listing(kept) == [
        "replicate-0.server.log",
        "replicate-0.sqlite",
        "replicate-0.trace.jsonl",
    ]
    lines = read_trace(kept / "replicate-0.trace.jsonl")
    assert [line["kind"] for line in lines].count("answer") == 1
    assert (lines[-2]["arguments"], lines[-2]["result"]) == ({"feature": "volume"}, None)
    assert (lines[-1]["kind"], lines[-1]["side"]) == ("failed", "server")
    # The failure happened in the server's process and inside ``search()``,
    # which keeps a seam's sentence and no exception: the note is the sentence.
    assert (staging / INQUIRY_FILENAME).read_text(encoding="utf-8") == f"{said}\n"
    _assert_cleared_only_with_the_evidence(run, staging)


def test_a_server_that_does_not_start_stops_the_run_before_the_model_is_asked(
    fresh_tools_run, monkeypatch, capsys
):
    run = fresh_tools_run
    real = bench.ServerParams.for_experiment
    monkeypatch.setattr(
        bench.ServerParams,
        "for_experiment",
        classmethod(lambda cls, db, experiment: real(db.with_name("missing.sqlite"), experiment)),
    )
    model = scripted(monkeypatch, [says(ANSWER)])
    # Through the command: a code of its own, for a batch to stop on.
    assert main(["search", "--run", str(run)]) == EXIT_KEPT_FOR_INQUIRY == 3
    said = capsys.readouterr().err
    assert said.startswith(f"error: {run}: the look-ups' server failed in llm_tools-0")
    assert "did not start" in said and "looked into, not searched past" in said
    assert model.asked == []
    staging = _assert_stopped_for_inquiry(run)
    # No trace was begun; why the server did not come up is on its stderr, kept.
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    assert _listing(kept) == ["replicate-0.server.log", "replicate-0.sqlite"]
    assert "is not a file with a store in it" in (kept / "replicate-0.server.log").read_text(
        encoding="utf-8"
    )
    # The failure was an exception this process caught: its stack is under the sentence.
    note = (staging / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in note and "ToolServerError" in note
    # The directory searched again is the ordinary refusal, which a batch skips.
    assert main(["search", "--run", str(run)]) == 1
    assert "was kept on purpose by a search that stopped" in capsys.readouterr().err


def test_a_trace_that_cannot_be_read_back_stops_the_run_and_keeps_the_trace(
    fresh_tools_run, monkeypatch
):
    # The search finished and was paid for; what it spent cannot be read
    # off its trace. That is this side's defect, and the trace is the
    # evidence: kept as for a server that failed, not discarded with the run.
    run = fresh_tools_run

    def unreadable(lines, replicate):
        raise ToolUseError("the trace ends inside round 2")

    monkeypatch.setattr(bench, "trace_use", unreadable)
    scripted(monkeypatch, [says("not a rule"), says("not a rule")])
    with pytest.raises(KeptForInquiry) as stopped:
        run_world(run)
    said = str(stopped.value)
    assert "what llm_tools-0 spent could not be read back from its trace" in said
    assert "the trace ends inside round 2" in said
    staging = _assert_stopped_for_inquiry(run)
    # Raised from the reader's own refusal, whose stack is under the sentence in the note.
    assert isinstance(stopped.value.__cause__, ToolUseError)
    note = (staging / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in note and "ToolUseError" in note
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    # The whole of the search that could not be summarised: both rounds answered.
    lines = read_trace(kept / "replicate-0.trace.jsonl")
    assert [line["kind"] for line in lines].count("answer") == 2


def _a_round_the_ledger_does_not_hold(lines: list[dict]) -> list[dict]:
    return [*lines, trace_model(3), trace_answer(3)]


def _other_words(lines: list[dict]) -> list[dict]:
    return [{**lines[0], "pack_digest": "cd" * 32}, *lines[1:]]


@pytest.mark.parametrize(
    ("replicates", "forged", "sentence"),
    [
        (
            1,
            _a_round_the_ledger_does_not_hold,
            r"llm_tools-0's trace answered 3 round\(s\) and its ledger holds 2 answer\(s\)",
        ),
        (2, _other_words, "llm_tools-1 was told .* an earlier replicate .* one model under one pack"),
    ],
    ids=["a-trace-that-is-not-the-ledger-s-search", "a-replicate-told-other-words"],
)
def test_a_trace_that_is_not_its_search_s_stops_the_run_with_the_trace_kept(
    landed_world, fresh_tools_run, tmp_path, monkeypatch, replicates, forged, sentence
):
    # The reader itself is real here: the trace is read, and what is wrong
    # with it is found by the checks a finished search is held to.
    artifact, _landing = landed_world
    run = fresh_tools_run
    if replicates != 1:
        run = land_run(tmp_path, artifact, tools_manifest(replicates=replicates))
    scripted(monkeypatch, [says("not a rule")] * (2 * replicates))
    real = bench.read_trace
    read: list[Path] = []

    def forging(path):
        read.append(path)
        # The last replicate's trace is the one that is not what it should be.
        return forged(real(path)) if len(read) == replicates else real(path)

    monkeypatch.setattr(bench, "read_trace", forging)
    with pytest.raises(KeptForInquiry, match=sentence):
        run_world(run)
    staging = _assert_stopped_for_inquiry(run)
    assert [path.name for path in read] == [
        f"replicate-{k}.trace.jsonl" for k in range(replicates)
    ]
    # Every replicate's trace is kept, the finished ones' with the one that stopped the run.
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    assert all((kept / path.name).is_file() for path in read)


def test_limits_that_are_not_the_manifest_s_stop_the_run_at_the_first_search(
    fresh_tools_run, monkeypatch
):
    # A search told other limits than its manifest sets is not that
    # manifest's arm: found when its trace is read, the trace kept.
    scripted(monkeypatch, [says("not a rule"), says("not a rule")])
    real = bench.tool_arm
    monkeypatch.setattr(
        bench, "tool_arm", lambda tools: (real(tools)[0], Limits(lookups=5, backtests=2))
    )
    # Said where the search's trace is read — not later, when the record
    # of every search is built, which would say it of a whole arm paid for.
    with pytest.raises(
        KeptForInquiry,
        match=r"what llm_tools-0 spent could not be read back from its trace — .*told pack "
        r"version 1 under limits of 5 call\(s\) and 2 backtest\(s\); the manifest sets 1, 4 and 1",
    ):
        run_world(fresh_tools_run)
    _assert_stopped_for_inquiry(fresh_tools_run)


def test_a_pool_arm_s_os_error_is_not_the_tool_arm_s_to_keep_anything_for(
    landed_world, tmp_path, monkeypatch
):
    # Only the tool arm has files of its own to fail on. An ``OSError`` out
    # of another arm's search is what it always was: raised as itself, the
    # run discarded, the directory as landed.
    artifact, landing = landed_world
    monkeypatch.setattr(bench, "verify", lambda store, manifest: landing)

    def unreadable(self, system, user):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(bench.PoolHypothesist, "propose", unreadable)
    run = land_run(tmp_path, artifact, tools_manifest(replicates=1))
    scripted(monkeypatch, [])
    with pytest.raises(OSError, match="Input/output error") as raised:
        run_world(run)
    assert not isinstance(raised.value, BenchError)
    assert _listing(run) == LANDED


def test_an_arm_that_cannot_be_opened_once_its_server_is_up_stops_the_run_and_keeps_the_log(
    fresh_tools_run, monkeypatch
):
    # What can be refused without a server is, before any arm runs
    # (``tool_arm``). A refusal at the opening comes after the child was
    # started, and the child's log is where it says why: kept, with the rest.
    run = fresh_tools_run

    def not_this_build_s(info):
        raise ToolArmError("the server answered as 'another' version '9'")

    monkeypatch.setattr(toolarm, "_require_this_server", not_this_build_s)
    model = scripted(monkeypatch, [says(ANSWER)])
    with pytest.raises(KeptForInquiry) as stopped:
        run_world(run)
    said = str(stopped.value)
    assert "llm_tools-0 could not be opened — the server answered as 'another' version '9'" in said
    assert model.asked == []
    staging = _assert_stopped_for_inquiry(run)
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    # The real child was started and stopped; its log is there, and no trace was begun.
    assert _listing(kept) == ["replicate-0.server.log", "replicate-0.sqlite"]
    note = (staging / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert note.startswith(f"{said}\n") and "ToolArmError" in note
    # And the refusal itself is raised from what stopped the arm.
    assert isinstance(stopped.value.__cause__, ToolArmError)


def test_a_trace_that_cannot_be_written_mid_search_stops_the_run_and_keeps_what_was(
    fresh_tools_run, monkeypatch
):
    run = fresh_tools_run
    scripted(monkeypatch, [says("not a rule"), says("not a rule")])
    real = toolarm._Trace.write
    written: list[str] = []

    def full(self, kind, state=None, **payload):
        if len(written) == 3:
            raise OSError(28, "No space left on device")
        written.append(kind.value)
        return real(self, kind, state, **payload)

    monkeypatch.setattr(toolarm._Trace, "write", full)
    with pytest.raises(
        KeptForInquiry, match="llm_tools-0 could not open or write its trace or its server's log"
    ) as stopped:
        run_world(run)
    staging = _assert_stopped_for_inquiry(run)
    assert isinstance(stopped.value.__cause__, OSError)
    note = (staging / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in note and "No space left on device" in note
    # The pack and the first round, which was answered and paid for.
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value / "replicate-0.trace.jsonl"
    assert [json.loads(line)["kind"] for line in kept.read_text("utf-8").splitlines()] == written
    assert written == ["pack", "model", "answer"]


def test_a_kept_staging_is_marked_by_its_name_whether_or_not_its_note_can_be_written(
    tmp_path, caplog, monkeypatch
):
    # The disk that could not take a trace's next line may not take a note
    # either. The mark every later reader goes by is the staging's NAME,
    # which a rename gives it without writing a byte.
    run = tmp_path / "run"
    staging = run / ".search.partial-abc"
    staging.mkdir(parents=True)
    real = Path.write_text

    def full(self, *args, **kwargs):
        if self.name == INQUIRY_FILENAME:
            raise OSError(28, "No space left on device")
        return real(self, *args, **kwargs)

    with monkeypatch.context() as patch, caplog.at_level("WARNING", logger=bench.__name__):
        patch.setattr(Path, "write_text", full)
        refusal = bench._kept_for_inquiry(run, staging, "the server failed", None)
    kept = run / ".search.kept-abc"
    assert isinstance(refusal, KeptForInquiry)
    assert "the server failed" in str(refusal) and str(kept) in str(refusal)
    assert _listing(run) == [kept.name] and _listing(kept) == []
    assert f"could not write {INQUIRY_FILENAME}" in caplog.text
    # No note, and the directory is refused all the same, to a reader that
    # finds the claim beside it and to one that finds the claim removed.
    assert bench._kept_on_purpose(run) == [kept.name]
    assert "kept on purpose" in bench._unfinished(run, [ARMS_DIRNAME])
    assert f"holds ['{kept.name}'], kept on purpose" in bench._unread_evidence(run)


def test_a_staging_that_cannot_be_renamed_is_marked_by_its_note(tmp_path, caplog, monkeypatch):
    run = tmp_path / "run"
    staging = run / ".search.partial-abc"
    staging.mkdir(parents=True)

    def locked(self, target):
        raise PermissionError("locked by another process")

    with monkeypatch.context() as patch, caplog.at_level("WARNING", logger=bench.__name__):
        patch.setattr(Path, "rename", locked)
        refusal = bench._kept_for_inquiry(run, staging, "the server failed", None)
    # Where it was, under the name it had — which the refusal says — and
    # still read as kept on purpose, by the note.
    assert _listing(run) == [staging.name] and str(staging) in str(refusal)
    assert "could not rename the kept staging" in caplog.text
    assert (staging / INQUIRY_FILENAME).read_text(encoding="utf-8") == f"{refusal}\n"
    assert bench._kept_on_purpose(run) == [staging.name]


def test_a_note_holds_the_cause_s_own_stack_and_no_sentence_it_was_raised_while_handling(tmp_path):
    # A sentence no file takes as it is — half a surrogate pair — is written all the same.
    staging = tmp_path / ".search.partial-x"
    staging.mkdir()
    bench._kept_for_inquiry(tmp_path, staging, "said \ud83d", ValueError("the cause"))
    note = (tmp_path / ".search.kept-x" / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "said ?" in note and "ValueError: the cause" in note
    # The cause's own stack, and not what it was raised while handling:
    # that may be a provider's failure, whose sentence may quote what the
    # provider was sent and is written nowhere by this package.
    try:
        try:
            raise RuntimeError("the provider said: <what it was sent>")
        except RuntimeError:
            raise OSError(28, "No space left on device")  # noqa: B904 - the chain is the point
    except OSError as exc:
        cause = exc
    other = tmp_path / ".search.partial-y"
    other.mkdir()
    bench._kept_for_inquiry(tmp_path, other, "the trace could not be written", cause)
    note = (tmp_path / ".search.kept-y" / INQUIRY_FILENAME).read_text(encoding="utf-8")
    assert "No space left on device" in note and "Traceback (most recent call last)" in note
    assert "the provider said" not in note


def test_only_a_staging_marked_as_kept_is_said_to_be_kept_on_purpose(tmp_path):
    # A run killed outright leaves a staging too; it is not evidence
    # anyone chose to keep, and is not said to be.
    killed = tmp_path / ".search.partial-killed"
    killed.mkdir()
    assert bench._kept_staging(tmp_path) == [killed.name]
    assert bench._kept_on_purpose(tmp_path) == []
    assert bench._unread_evidence(tmp_path) is None
    assert "kept on purpose" not in bench._unfinished(tmp_path, [ARMS_DIRNAME])
    # Either mark is one: the note inside it, or its name.
    (killed / INQUIRY_FILENAME).write_text("why\n", encoding="utf-8")
    assert bench._kept_on_purpose(tmp_path) == [killed.name]
    (killed / INQUIRY_FILENAME).unlink()
    named = tmp_path / ".search.kept-named"
    named.mkdir()
    assert bench._kept_staging(tmp_path) == [named.name, killed.name]
    assert bench._kept_on_purpose(tmp_path) == [named.name]
    assert "kept on purpose" in bench._unfinished(tmp_path, [ARMS_DIRNAME])


@pytest.mark.parametrize(
    ("refusing", "refusal"),
    [("ToolUse", ToolUseError), ("_hold_use_to_manifest", BenchError)],
    ids=["the-record-itself", "the-record-held-to-the-manifest"],
)
def test_searches_that_do_not_make_a_record_stop_the_run_with_their_traces_kept(
    fresh_tools_run, monkeypatch, refusing, refusal
):
    # Each search was held as its trace was read; the record of them all is
    # built, and held once more, at the end, and a refusal at either is the
    # same defect found later — not a run to discard.
    def refuse(*args, **kwargs):
        raise refusal("searches: each replicate once, got [0, 0]")

    monkeypatch.setattr(bench, refusing, refuse)
    scripted(monkeypatch, [says("not a rule"), says("not a rule")])
    with pytest.raises(
        KeptForInquiry,
        match="what the tool arm's searches spent does not make a record — searches: each",
    ) as stopped:
        run_world(fresh_tools_run)
    assert isinstance(stopped.value.__cause__, refusal)
    staging = _assert_stopped_for_inquiry(fresh_tools_run)
    assert (staging / ARMS_DIRNAME / "llm_tools" / "replicate-0.trace.jsonl").is_file()


def test_an_interrupt_while_the_evidence_is_being_marked_still_keeps_it(
    fresh_tools_run, monkeypatch
):
    # The run is marked to be kept BEFORE anything is done to say so, so an
    # interrupt that lands while the staging is renamed or its note written
    # leaves the staging and the claim, and not a directory as landed.
    run = fresh_tools_run
    real = bench.ServerParams.for_experiment
    monkeypatch.setattr(
        bench.ServerParams,
        "for_experiment",
        classmethod(lambda cls, db, experiment: real(db.with_name("missing.sqlite"), experiment)),
    )
    scripted(monkeypatch, [says(ANSWER)])

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(bench, "_kept_for_inquiry", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_world(run)
    (kept,) = bench._kept_staging(run)
    assert _listing(run) == sorted([*LANDED, ARMS_DIRNAME, kept])
    assert _listing(run / kept / ARMS_DIRNAME) == ["grid", "llm_tools", "random"]
    with pytest.raises(BenchError, match="a search of this world is running or did not finish"):
        run_world(run)


def test_the_oracle_s_record_goes_in_last_whatever_else_a_search_writes(
    fresh_tools_run, monkeypatch, landed_world, searched
):
    # "The records are in" is read off ``oracle.json`` alone, so a run cut
    # short while its records move in must not have put that one in first.
    run = fresh_tools_run
    _quick_oracle(monkeypatch, landed_world, searched)
    scripted(monkeypatch, [says("not a rule"), says("not a rule")])
    real = os.replace
    moved: list[str] = []

    def cut(source, target):
        if Path(target).name == ORACLE_FILENAME:
            raise KeyboardInterrupt
        moved.append(Path(target).name)
        return real(source, target)

    monkeypatch.setattr(bench.os, "replace", cut)
    with pytest.raises(KeyboardInterrupt):
        run_world(run)
    monkeypatch.setattr(bench.os, "replace", real)
    assert moved == [VERDICTS_FILENAME, TOOL_USE_FILENAME]
    # Not a searched world: the staging is gone, the claim given back, and
    # what did get in is named by every reader as a search that did not finish.
    assert _listing(run) == sorted([*LANDED, VERDICTS_FILENAME, TOOL_USE_FILENAME])
    with pytest.raises(BenchError, match="a search of this world is running or did not finish"):
        read_searched(run)


# -- refused before anything is landed or run -------------------------------------------------------


def test_a_tool_arm_this_build_cannot_run_is_refused_before_a_world_is_landed_or_searched(
    landed_world, tmp_path, monkeypatch, capsys
):
    artifact, _landing = landed_world
    scripted(monkeypatch, [])
    model, limits = tool_arm(TOOLS)
    assert (model.label, limits) == (LABEL, LIMITS)

    # Another pack: the manifest would describe a run told other words.
    other = tools_manifest(pack_version=PACK_VERSION + 1)
    sentence = (
        f"arms.llm_tools.pack_version: this build tells a model about its tools at pack version "
        f"{PACK_VERSION}, the manifest asks for {PACK_VERSION + 1}"
    )
    with pytest.raises(BenchError, match=sentence):
        tool_arm(other.arms.llm_tools)
    # ``search`` refuses it with the directory left as landed, and before
    # any arm has searched: the pool arms' minutes are not spent on a world
    # whose model arm cannot run.
    def searched_anyway(*args, **kwargs):
        raise AssertionError("an arm searched before the tool arm was asked whether it can run")

    run = land_run(tmp_path, artifact, other)
    with monkeypatch.context() as patch:
        patch.setattr(bench, "search", searched_anyway)
        with pytest.raises(BenchError, match=sentence) as refused:
            run_world(run)
    assert not isinstance(refused.value, KeptForInquiry)
    assert _listing(run) == LANDED
    # ... and ``generate`` before anything is written for it.
    written = tmp_path / "other.json"
    written.write_bytes(other.dumps().encode("utf-8"))
    root = tmp_path / "never"
    assert main(["generate", "--manifest", str(written), "--root", str(root)]) == 1
    assert sentence in capsys.readouterr().err and not root.exists()


def test_a_missing_sdk_a_client_with_no_tools_and_a_model_that_cannot_be_built_are_named(
    monkeypatch,
):
    scripted(monkeypatch, [])

    def no_sdk():
        raise ToolArmError("the MCP SDK the tool arm is written against cannot be imported")

    with monkeypatch.context() as patch:
        patch.setattr(bench, "require_client_sdk", no_sdk)
        with pytest.raises(BenchError, match="arms.llm_tools: the MCP SDK the tool arm"):
            tool_arm(TOOLS)

    def built(client):
        return lambda *args, **kwargs: bench.ChatHypothesist(model=client, label=LABEL)

    monkeypatch.setattr(bench.ChatHypothesist, "build", built(SimpleNamespace(temperature=0.0)))
    with pytest.raises(BenchError, match=f"arms.llm_tools: {LABEL} takes no tools"):
        tool_arm(TOOLS)

    # A client that has the method and will not be bound — what the base
    # class every chat client inherits from does — is found by binding it
    # with this build's tools, here, and not after the pool arms have run.
    bound: list[list] = []

    def unbindable(tools):
        bound.append(tools)
        raise NotImplementedError("this model takes no tools")

    monkeypatch.setattr(
        bench.ChatHypothesist,
        "build",
        built(SimpleNamespace(temperature=0.0, bind_tools=unbindable)),
    )
    with pytest.raises(
        BenchError,
        match=f"arms.llm_tools: {LABEL} could not be given the tools: this model takes no tools",
    ):
        tool_arm(TOOLS)
    assert bound == [list(Pack.of(tool_specs(), LIMITS).tools)]
    monkeypatch.setattr(bench.ChatHypothesist, "build", built(SimpleNamespace(temperature=None)))
    with pytest.raises(BenchError, match="arms.llm_tools: the client for .* dropped temperature"):
        tool_arm(TOOLS)

    def refuse(*args, **kwargs):
        raise ValueError("API key for provider is not set")

    monkeypatch.setattr(bench.ChatHypothesist, "build", refuse)
    with pytest.raises(BenchError, match="arms.llm_tools: API key for provider is not set"):
        tool_arm(TOOLS)


def test_the_model_a_search_is_held_to_is_the_label_the_research_package_builds_one_under(
    monkeypatch,
):
    # A finished search's trace is held to the manifest's model by its
    # label, and the label's spelling is the research package's
    # (``ChatHypothesist.build``). Every other test of this module puts its
    # own ``build`` in that one's place, so this one runs the real one —
    # over a client that is no provider's — and holds the two spellings
    # together: were they to part, every paid search would stop at its
    # first replicate, and no other test would say so.
    monkeypatch.setattr(
        research_upstream,
        "build_chat_model",
        lambda provider, model, base_url=None, **kwargs: Scripted([]),
    )
    for provider, name in (("scripted", "model"), ("openrouter", "vendor/some-model:beta")):
        arm = replace(TOOLS, provider=provider, model=name)
        built, limits = tool_arm(arm)
        pack = TracePack(
            model=built.label, pack_version=PACK_VERSION, pack_digest="ab" * 32, limits=limits
        )
        bench._hold_pack_to_arm("replicate-0.trace.jsonl", pack, arm)
        with pytest.raises(BenchError, match="are those of the model another/model"):
            bench._hold_pack_to_arm("replicate-0.trace.jsonl", replace(pack, model="another/model"), arm)


# -- (d) a manifest that names no tool arm ----------------------------------------------------------


def test_a_search_without_the_tool_arm_leaves_no_tool_use_record(searched):
    run, *_ = searched
    assert not (run / TOOL_USE_FILENAME).exists()
    assert read_searched(run).tool_use is None
    assert not (run / ARMS_DIRNAME / Arm.LLM_TOOLS.value).exists()


def test_a_version_3_run_directory_is_named_searched_and_read_as_it_was(
    landed_world, searched, tmp_path, monkeypatch
):
    artifact, _landing = landed_world
    run = land_run(tmp_path, artifact, replace(bench_manifest(), manifest_version=3))
    # The directory and the row a version-3 build gave this manifest
    # (develop at f2039c6b, measured 2026-11-19): the worlds searched before
    # the tool arm are found under the names they have.
    assert run.name == f"{COIN}-01bb4a6e"
    on_disk = json.loads((run / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert on_disk["manifest_version"] == 3 and sorted(on_disk["arms"]) == ["llm", "random"]
    _quick_oracle(monkeypatch, landed_world, searched)
    verdicts, _oracle = run_world(run)
    assert verdicts.manifest_digest == "01bb4a6e"
    assert _listing(run) == sorted([*LANDED, ARMS_DIRNAME, VERDICTS_FILENAME, ORACLE_FILENAME])
    # Searched as the same manifest written today is, to the last figure.
    _plain, plain_verdicts, *_ = searched
    assert [_timeless(r) for r in verdicts.results] == [
        _timeless(r) for r in plain_verdicts.results
    ]
    read = read_searched(run)
    assert (read.manifest.manifest_version, read.tool_use) == (3, None)
    # The landed manifest was not rewritten as another version by any of it.
    assert json.loads((run / MANIFEST_FILENAME).read_text(encoding="utf-8")) == on_disk
    assert {row.conditions for row in calibration_table([run])} == {"710e2deb"}
