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

The two ways the TOOLS' side can fail — a server that dies in a round, one
that never starts — each stop a run, and are run on fresh directories.
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

from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.store import ResearchStore
from contrib.discovery import bench, cli
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    INQUIRY_FILENAME,
    ORACLE_FILENAME,
    VERDICTS_FILENAME,
    Arm,
    ArmResult,
    BenchError,
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
from contrib.discovery.cli import main
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
    "round(s) made none; backtests 1; the model's mistakes 1 of 3 call(s); answered without "
    "tools 0/2 = 0.0%"
)
TOKENS_LINE = (
    "  tokens reported over 2 search(es), failed seams included: 480,000 in, 120,000 out, in 4 "
    "model call(s) that came back (a floor: a retried call counts once, a reply that was not "
    "read not at all)"
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
    for wrong in ((), SPENT[:1], (SPENT[0], replace(SPENT[1], seam_failed=False))):
        with pytest.raises(ScoreError, match="row.uses"):
            replace(tools, uses=wrong)
    with pytest.raises(ScoreError, match="row.uses: 2 search"):
        replace(grid, uses=SPENT)
    with pytest.raises(ScoreError, match="row.uses: expected SearchUses"):
        replace(tools, uses=("spent",))


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
        (lambda r: r.update(pack_version=2), "told pack version 2"),
        (_one_round_short, r"llm_tools-0's trace answered 1 round\(s\) and its ledger holds 2"),
        (
            lambda r: r["searches"][1].update(seam_failed=False),
            "llm_tools-1 ended seam_failed, and its trace ends on no failed seam",
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
        "another-pack",
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
    with pytest.raises(BenchError, match=sentence):
        read_searched(copy)
    with pytest.raises(BenchError, match=sentence):
        calibration_table([copy])


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
    with pytest.raises(BenchError, match=refusal):
        run_world(run)
    with pytest.raises(BenchError, match=refusal):
        calibration_table([run])
    return staging


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
    with pytest.raises(BenchError) as stopped:
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
    # Cleared by hand, the directory is as it was landed, and may be searched.
    shutil.rmtree(staging)
    (run / ARMS_DIRNAME).rmdir()
    assert _listing(run) == LANDED


def test_a_server_that_does_not_start_stops_the_run_before_the_model_is_asked(
    fresh_tools_run, monkeypatch
):
    run = fresh_tools_run
    real = bench.ServerParams.for_experiment
    monkeypatch.setattr(
        bench.ServerParams,
        "for_experiment",
        classmethod(lambda cls, db, experiment: real(db.with_name("missing.sqlite"), experiment)),
    )
    model = scripted(monkeypatch, [says(ANSWER)])
    with pytest.raises(BenchError) as stopped:
        run_world(run)
    said = str(stopped.value)
    assert "the look-ups' server failed in llm_tools-0" in said and "did not start" in said
    assert model.asked == []
    staging = _assert_stopped_for_inquiry(run)
    # No trace was begun; why the server did not come up is on its stderr, kept.
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    assert _listing(kept) == ["replicate-0.server.log", "replicate-0.sqlite"]
    assert "is not a file with a store in it" in (kept / "replicate-0.server.log").read_text(
        encoding="utf-8"
    )


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
    with pytest.raises(BenchError) as stopped:
        run_world(run)
    said = str(stopped.value)
    assert "what llm_tools-0 spent could not be read back from its trace" in said
    assert "the trace ends inside round 2" in said
    staging = _assert_stopped_for_inquiry(run)
    kept = staging / ARMS_DIRNAME / Arm.LLM_TOOLS.value
    # The whole of the search that could not be summarised: both rounds answered.
    lines = read_trace(kept / "replicate-0.trace.jsonl")
    assert [line["kind"] for line in lines].count("answer") == 2


def test_an_arm_that_cannot_be_opened_as_asked_is_a_refusal_and_leaves_the_directory_as_landed(
    fresh_tools_run, monkeypatch
):
    # Not the tools' side failing: nothing was started that could be looked into.
    def refuse(*args, **kwargs):
        raise ToolArmError("the server lists tools other than this build's at version 1")

    monkeypatch.setattr(bench.ToolHypothesist, "open", refuse)
    scripted(monkeypatch, [says(ANSWER)])
    with pytest.raises(BenchError, match="llm_tools-0: the server lists tools other than"):
        run_world(fresh_tools_run)
    assert _listing(fresh_tools_run) == LANDED


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
    # ``search`` refuses it with the directory left as landed ...
    run = land_run(tmp_path, artifact, other)
    with pytest.raises(BenchError, match=sentence):
        run_world(run)
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
