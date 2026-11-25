"""The summary arm, run by the bench (tools plan §3.7, T6): searched last, asked with its own training window's figures, recorded and read back.

The block itself and the hypothesist that carries it are held in
``test_summaryarm.py`` on the hand-written history. Here the arm is one
arm of a world: the example world landed under a manifest that names it
and searched ONCE (``summarised``), with a scripted client in the model's
place — two replicates, the first answering twice (a rule, then something
refused) and the second meeting a provider that went away at its first
round, as the blind model arm's run in ``test_bench.py`` is scripted.

What is read off that one run: where the arm is searched and what it
leaves; the prompts its model was actually sent, against the block
recomputed from the arm's OWN store; and that what it spent is written,
read and tabled as the blind arm's is.
"""

from __future__ import annotations

import json
from dataclasses import fields, replace
from typing import get_args

import pytest

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.features import FeatureError
from contrib.autoresearch.hypothesis import build_system_prompt
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.vocabulary import feature_names
from contrib.discovery import bench, manifest as manifest_module
from contrib.discovery.bench import (
    ARMS_DIRNAME,
    Arm,
    BenchError,
    _train_view,
    calibration_table,
    experiment_id,
    metered_arm,
    read_manifest,
    read_searched,
    run_world,
    store_path,
    summary_arm,
)
from contrib.discovery.cli import main as cli_main
from contrib.discovery.landing import MANIFEST_FILENAME, world_dir
from contrib.discovery.manifest import (
    Arms,
    LlmArm,
    LlmSummaryArm,
    LlmToolsArm,
    Manifest,
    ModelArm,
    TrainPickArm,
)
from contrib.discovery.modeluse import MODEL_USE_FILENAME, MeteredModel, ModelUse
from contrib.discovery.scoring import report_table, score_world
from contrib.discovery.summaryarm import SUMMARY_VERSION, SummaryHypothesist, summary
from contrib.discovery.tooluse import TOOL_USE_FILENAME
from contrib.discovery.verdict import Ended

from .conftest import COIN, PLANTED, STEP_MS, arm_store, bench_manifest, land_run
from .test_bench import (
    _BLIND_LABEL,
    _BLIND_SPENT,
    _BLIND_TOKENS,
    _PRICES,
    LANDED,
    LLM_ARM,
    _copy_records,
    _listing,
    _records_of,
    _ReportingModel,
)
from .test_research_tools import _rewrite

# The blind arm's run in ``test_bench.py`` over again with the block in
# front of the model: the same model, two replicates, the same script. So
# what this arm is filed under, what it spent and what its row says of it
# are that run's own (``_BLIND_*``), and are not written out a second time.
BLIND = replace(LLM_ARM, replicates=2)
SUMMARY_ARM = LlmSummaryArm(
    provider=BLIND.provider,
    model=BLIND.model,
    temperature=BLIND.temperature,
    replicates=BLIND.replicates,
    world_subset=BLIND.world_subset,
    summary_version=SUMMARY_VERSION,
)
LABEL, SPENT, TOKENS = _BLIND_LABEL, _BLIND_SPENT, _BLIND_TOKENS
ASK = "Propose ONE new rule as a single JSON object, and nothing else."


def _summary_manifest(arm: LlmSummaryArm = SUMMARY_ARM) -> Manifest:
    """The bench's manifest (one Random seed, a two-answer budget) naming the summary arm."""
    base = bench_manifest()
    return replace(base, arms=replace(base.arms, llm_summary=arm))


def _scripted(client):
    """``ChatHypothesist.build`` with ``client`` in the model's place, and what it was asked to build."""
    built = []

    def build(provider, name, base_url=None, **kwargs):
        built.append((provider, name, kwargs))
        return bench.ChatHypothesist(model=client, label=f"{provider}/{name}")

    return build, built


@pytest.fixture(scope="module")
def summarised(landed_world, tmp_path_factory):
    """The example world searched once with the summary arm, and scored: the run, the records, the client, what was built."""
    artifact, _landing = landed_world
    run = land_run(tmp_path_factory.mktemp("summary"), artifact, _summary_manifest())
    client = _ReportingModel([json.dumps(PLANTED), "not a rule"])
    build, built = _scripted(client)
    lines: list[str] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(bench.ChatHypothesist, "build", build)
        verdicts, _oracle = run_world(run, on_line=lines.append)
    score_world(run)
    return run, verdicts, lines, client, built


def test_the_summary_arm_is_searched_last_in_stores_of_its_own_and_recorded_with_the_others(
    summarised,
):
    run, verdicts, _lines, _client, built = summarised
    # One adapter for every replicate, built as the manifest says, retrying.
    assert built == [
        (SUMMARY_ARM.provider, SUMMARY_ARM.model, {"temperature": 0.0, "max_retries": 6})
    ]
    assert [(r.arm, r.replicate) for r in verdicts.results] == [
        (Arm.RANDOM, 0),
        (Arm.GRID, 0),
        (Arm.LLM_SUMMARY, 0),
        (Arm.LLM_SUMMARY, 1),
    ]
    assert sorted(p.name for p in (run / ARMS_DIRNAME).iterdir()) == [
        "grid",
        "llm_summary",
        "random",
    ]
    # Its stores and nothing beside them: the arm starts no server, keeps
    # no trace, and what it was told is recomputed from the store.
    assert sorted(p.name for p in (run / ARMS_DIRNAME / "llm_summary").iterdir()) == [
        "replicate-0.sqlite",
        "replicate-1.sqlite",
    ]
    assert not (run / TOOL_USE_FILENAME).exists()
    alive, failed = verdicts.results[2], verdicts.results[3]
    assert alive.experiment_id == "llm_summary-0"
    assert (alive.snapshot.ended, alive.snapshot.answers) == (Ended.BUDGET_SPENT, 2)
    assert len(alive.snapshot.trials) == 1  # the refused answer spent budget and filed nothing
    # A model answers in the whole language: its rule need not be the pool's.
    pool = {candidate.spec_hash for candidate in read_manifest(run).search_space.candidates}
    assert alive.snapshot.trials[0].spec_hash not in pool
    assert (failed.snapshot.ended, failed.snapshot.answers) == (Ended.SEAM_FAILED, 0)
    # The ledger files its answers under the model's own name.
    with ResearchStore(store_path(run, Arm.LLM_SUMMARY, 0)) as store:
        rows = store.conn.execute(
            "SELECT DISTINCT model FROM proposals WHERE experiment_id = ?",
            (experiment_id(Arm.LLM_SUMMARY, 0),),
        ).fetchall()
    assert [row[0] for row in rows] == [LABEL]
    searched = read_searched(run)
    assert searched is not None and searched.verdicts == verdicts
    assert searched.tool_use is None


def test_the_model_is_sent_the_figures_of_its_own_training_window_after_the_system_prompt(
    summarised,
):
    run, verdicts, _lines, client, _built = summarised
    experiment = verdicts.results[2].snapshot.experiment
    # The block over again, from each replicate's own store: what the model
    # was sent is this text, after the research package's system prompt.
    # (The two stores are copies of one artifact, so the two blocks are one
    # text: that each replicate read ITS store is held where two stores
    # that differ are opened, in the test of the order below.)
    blocks = [
        summary(
            _train_view(
                store_path(run, Arm.LLM_SUMMARY, k), verdicts.results[2 + k].snapshot.experiment
            )
        )
        for k in (0, 1)
    ]
    block = blocks[0]
    assert blocks[1] == block
    told = f"{build_system_prompt()}\n\n{block}"
    # Two rounds of the first replicate, and the one call the second's seam failed on.
    assert len(client.prompts) == 3
    for (system_role, system), (user_role, user) in client.prompts:
        assert (system_role, user_role) == ("system", "human")
        # The same block every round.
        assert system == told
        # The user prompt is the loop's, untouched: it ends on the instruction.
        assert COIN in user and user.endswith(ASK)
    first, second = (prompt[1][1] for prompt in client.prompts[:2])
    assert "RULES ALREADY SCORED" not in first and "RULES ALREADY SCORED" in second
    # Every line of figures is of the training window of the stored split.
    train = experiment.split.train
    figures = block.split("\n")[-len(feature_names()) :]
    assert {json.loads(line)["bars"] for line in figures} == {
        (train.end_ms - train.start_ms) // STEP_MS
    }
    # The model sees the opaque coin and nothing about what the world is.
    shown = f"{told}{second}".lower()
    assert "conditional" not in shown and "planted" not in shown and "holdout" not in shown


def test_what_the_summary_arm_spent_is_recorded_read_and_tabled_as_the_blind_arm_s_is(summarised):
    run, verdicts, lines, _client, _built = summarised
    written = ModelUse.loads((run / MODEL_USE_FILENAME).read_text(encoding="utf-8"))
    assert written == ModelUse(
        world_id=verdicts.world_id,
        manifest_digest=verdicts.manifest_digest,
        model=LABEL,
        searches=SPENT,
    )
    assert read_searched(run).model_use == written
    assert not any(line.startswith("model use:") for line in lines)
    assert metered_arm(read_manifest(run)) == (Arm.LLM_SUMMARY, SUMMARY_ARM)
    # A row of its own on both tables, after the rows of its manifest's pool arms.
    rows = calibration_table([run])
    assert [row.arm for row in rows] == [Arm.RANDOM, Arm.GRID, Arm.LLM_SUMMARY]
    mine = rows[-1]
    assert (mine.searches, mine.failed, mine.effort) == (1, 1, ((2, 1),))
    assert " llm_summary: " in mine.describe()
    report = {row.arm: row for row in report_table([run])}
    assert list(report) == [Arm.RANDOM, Arm.GRID, Arm.LLM_SUMMARY]
    told = report[Arm.LLM_SUMMARY]
    assert (told.model_uses, told.model, told.unrecorded) == (SPENT, LABEL, 0)
    assert (told.uses, told.pack, told.scored_a_round) == ((), None, None)
    assert told.describe()[-2:] == [
        "  answers median 2 (min 2), distinct rules median 1 (min 1)",
        TOKENS,
    ]
    assert told.describe(prices=_PRICES)[-1] == (
        TOKENS + "; estimated cost US$0.01 on those counts, at US$3 in and US$15 out a "
        "million tokens with no cache discount"
    )
    # The pool arms asked no model, and their rows say nothing of tokens.
    for arm in (Arm.RANDOM, Arm.GRID):
        assert (report[arm].model_uses, report[arm].model, report[arm].unrecorded) == ((), None, 0)
    # A search is spent once, and this record is among what goes before another.
    with pytest.raises(
        BenchError, match=r"remove arms/, verdicts\.json, model_use\.json, oracle\.json by hand"
    ):
        run_world(run)


def test_the_record_is_held_to_the_summary_arm_s_own_searches_and_may_be_absent(
    summarised, tmp_path
):
    run, verdicts, *_ = summarised

    def edited(name, change):
        copy = _records_of(run, tmp_path, name)
        path = copy / MODEL_USE_FILENAME
        record = json.loads(path.read_text(encoding="utf-8"))
        change(record)
        path.write_text(json.dumps(record), encoding="utf-8")
        return copy

    # Held to the results of THIS arm, by name: the record is the summary
    # arm's, and its searches are the ones the verdicts file under it.
    for name, change, sentence in (
        (
            "calls",
            lambda r: r["searches"][0].update(model_calls=3),
            r"llm_summary-0 is said to have had 3 model call\(s\) come back and its ledger "
            r"holds 2 answer\(s\)",
        ),
        (
            "seam",
            lambda r: r["searches"][0].update(seam_failed=True),
            "llm_summary-0 ended budget_spent, and its record says it ended on a failed seam",
        ),
        (
            "model",
            lambda r: r.update(model="openai/gpt"),
            "are those of the model openai/gpt; the manifest's model arm names",
        ),
        (
            "replicates",
            lambda r: r.update(searches=r["searches"][:1]),
            r"holds the model arm's replicate\(s\) \[0\]; the manifest names 2, numbered from 0",
        ),
    ):
        with pytest.raises(BenchError, match=sentence):
            read_searched(edited(name, change))
    # Absent, it is a search whose counts were not recorded: read, and said on the row.
    old = _records_of(run, tmp_path, "old")
    (old / MODEL_USE_FILENAME).unlink()
    read = read_searched(old)
    assert read.model_use is None and read.verdicts == verdicts
    (row,) = (row for row in report_table([old]) if row.arm is Arm.LLM_SUMMARY)
    assert (row.model_uses, row.model, row.unrecorded) == ((), None, 2)
    assert row.describe()[-1].startswith("  tokens not recorded for 2 search(es) of this row")
    # The record is of an arm counted around its client, and is held to
    # no other arm's searches, whatever is passed beside it.
    use = read_searched(run).model_use
    bench._hold_model_use("model_use.json", use, Arm.LLM_SUMMARY, SUMMARY_ARM, verdicts)
    for arm in (Arm.LLM_TOOLS, Arm.RANDOM):
        with pytest.raises(BenchError, match=rf"counted around its client, and {arm.value} is not"):
            bench._hold_model_use("model_use.json", use, arm, SUMMARY_ARM, verdicts)
    # ... nor to the other counted arm's: these verdicts file no blind-arm
    # search, and that is said, where a look-up by replicate would only raise.
    with pytest.raises(
        BenchError,
        match=r"verdicts\.json beside it files no llm search for replicate\(s\) \[0, 1\]",
    ):
        bench._hold_model_use("model_use.json", use, Arm.LLM, BLIND, verdicts)


def test_records_and_a_manifest_that_disagree_about_the_summary_arm_are_refused(
    summarised, tmp_path
):
    run, _verdicts, *_ = summarised
    manifest = read_manifest(run)
    # The same model named as the blind arm: the records are another arm's.
    for name, arms in (
        ("blind", replace(manifest.arms, llm_summary=None, llm=BLIND)),
        ("none", replace(manifest.arms, llm_summary=None)),
        ("fewer", replace(manifest.arms, llm_summary=replace(SUMMARY_ARM, replicates=1))),
    ):
        other = replace(manifest, arms=arms)
        bare = world_dir(tmp_path / name, other)
        bare.mkdir(parents=True)
        (bare / MANIFEST_FILENAME).write_bytes(other.dumps().encode("utf-8"))
        _copy_records(run, bare)
        with pytest.raises(
            BenchError, match=r"holds the arms .*llm_summary-1.* the manifest beside it names"
        ):
            read_searched(bare)


def test_a_summary_version_that_is_not_this_build_s_is_refused_before_anything_is_landed_or_searched(
    monkeypatch, tmp_path, capsys, landed_world
):
    client = _ReportingModel([])
    build, _built = _scripted(client)
    monkeypatch.setattr(bench.ChatHypothesist, "build", build)
    assert summary_arm(SUMMARY_ARM).label == LABEL
    # Another version is an EARLIER one as much as a later one: a build
    # whose wording has moved on does not run a manifest written for the
    # wording before.
    with monkeypatch.context() as later:
        later.setattr(bench, "SUMMARY_VERSION", SUMMARY_VERSION + 1)
        with pytest.raises(
            BenchError,
            match=rf"at version {SUMMARY_VERSION + 1}, the manifest asks for {SUMMARY_VERSION}",
        ):
            summary_arm(SUMMARY_ARM)

    # Another version: the manifest would describe a run told other words.
    other = _summary_manifest(replace(SUMMARY_ARM, summary_version=SUMMARY_VERSION + 1))
    sentence = (
        f"arms.llm_summary.summary_version: this build hands a model its summary at version "
        f"{SUMMARY_VERSION}, the manifest asks for {SUMMARY_VERSION + 1}"
    )
    with pytest.raises(BenchError, match=sentence):
        summary_arm(other.arms.llm_summary)
    # Refused where the arms of a search are listed, before any is run ...
    with pytest.raises(BenchError, match=sentence):
        bench._arms(other)
    # ... and by ``generate`` before anything is written for it.
    written = tmp_path / "other.json"
    written.write_bytes(other.dumps().encode("utf-8"))
    root = tmp_path / "never"
    assert cli_main(["generate", "--manifest", str(written), "--root", str(root)]) == 1
    assert sentence in capsys.readouterr().err and not root.exists()
    # A world landed under it all the same (by another build, say) is
    # refused by ``search`` itself before any arm runs, and is left as
    # landed: nothing searched, the claim given back, to be searched by a
    # build that does speak that version.
    artifact, _landing = landed_world
    run = land_run(tmp_path / "landed", artifact, other)
    with pytest.raises(BenchError, match=sentence):
        run_world(run)
    assert _listing(run) == LANDED
    assert client.prompts == []

    # A model that cannot be built is named under this arm's path.
    def refuse(provider, name, base_url=None, **kwargs):
        raise RuntimeError("no key is set")

    monkeypatch.setattr(bench.ChatHypothesist, "build", refuse)
    with pytest.raises(BenchError, match="arms.llm_summary: no key is set"):
        summary_arm(SUMMARY_ARM)

    # The path is the arm's kind, read off what it IS: a subclass of an arm
    # is that arm, and something that is no model arm is refused by name,
    # where a look-up by exact type would have raised a bare ``KeyError``.
    class Renamed(LlmSummaryArm):
        pass

    renamed = Renamed(**{name: getattr(SUMMARY_ARM, name) for name in SUMMARY_ARM.to_dict()})
    with pytest.raises(BenchError, match="arms.llm_summary: no key is set"):
        bench.model_hypothesist(renamed)
    with pytest.raises(BenchError, match="one of a manifest's model arms, got TrainPickArm"):
        bench.model_hypothesist(TrainPickArm(search_seeds=(0,), backtests=1))


def test_the_model_arms_a_manifest_can_name_are_the_ones_the_bench_runs_as_model_arms():
    # Three lists say which arms are model arms: the keys a manifest may
    # name one under, the arms this runner does not walk the pool with, and
    # the records a model is built for. One going its own way would leave
    # an arm outside the one-model-arm rule, or a key no arm answers to.
    assert list(manifest_module._MODEL_ARMS) == [arm.value for arm in Arm if not arm.walks_pool]
    assert dict(bench._MODEL_ARM_PATHS) == {
        kind: f"arms.{key}"
        for key, kind in zip(
            manifest_module._MODEL_ARMS, (LlmArm, LlmSummaryArm, LlmToolsArm), strict=True
        )
    }
    assert set(get_args(ModelArm)) == {kind for kind, _path in bench._MODEL_ARM_PATHS}
    assert set(manifest_module._MODEL_ARMS) < {field.name for field in fields(Arms)}


def test_the_summary_arm_runs_after_every_arm_that_asks_no_provider_each_replicate_counted_apart(
    monkeypatch, opened_arm, history, tmp_path
):
    client = _ReportingModel([])
    build, _built = _scripted(client)
    monkeypatch.setattr(bench.ChatHypothesist, "build", build)
    base = _summary_manifest()
    manifest = replace(
        base, arms=replace(base.arms, train_pick=TrainPickArm(search_seeds=(4,), backtests=2))
    )
    arms = bench._arms(manifest)
    assert [(arm, replicate) for arm, replicate, _open in arms] == [
        (Arm.RANDOM, 0),
        (Arm.GRID, 0),
        (Arm.TRAIN_PICK, 4),
        (Arm.LLM_SUMMARY, 0),
        (Arm.LLM_SUMMARY, 1),
    ]
    # Opened for real, over two hand-written stores that DIFFER inside the
    # training window (in a run they are copies of one artifact, and a
    # block kept from the first replicate would pass for the second's):
    # each replicate is handed the block of the store it is opened on, and
    # a counter of its own around the one client.
    path, experiment = opened_arm
    other = tmp_path / "other.sqlite"
    last_close = _train_view(path, experiment).frame.bundle.bars[-1].close_time
    with arm_store(history, other) as store:
        assert _rewrite(store, since_ms=last_close, series=("4h",)) > 0
    stores = (path, other)
    blocks = [summary(_train_view(store, experiment)) for store in stores]
    assert blocks[0] != blocks[1]
    summary_arms = [open_ for arm, _replicate, open_ in arms if arm is Arm.LLM_SUMMARY]
    opened = []
    for open_, store in zip(summary_arms, stores, strict=True):
        with open_(store, experiment) as hypothesist:
            opened.append(hypothesist)
    first, second = opened
    for hypothesist, block in zip(opened, blocks, strict=True):
        assert isinstance(hypothesist, SummaryHypothesist)
        assert (hypothesist.block, hypothesist.label) == (block, LABEL)
        assert isinstance(hypothesist.model, MeteredModel)
    assert first.model is not second.model
    # Opening an arm asks its model nothing.
    assert client.prompts == []
    # The two arms whose spending is counted around their client (the
    # order a table lists the arms in is held in ``test_bench_pick.py``) ...
    assert [arm for arm in Arm if arm.metered] == [Arm.LLM, Arm.LLM_SUMMARY]
    # ... and a manifest's model arm is one of them or it is not: the
    # tool arm's spending is read off its trace, and a manifest with no
    # model arm has nobody's to count.
    assert metered_arm(base) == (Arm.LLM_SUMMARY, SUMMARY_ARM)
    assert metered_arm(replace(base, arms=replace(base.arms, llm_summary=None, llm=BLIND))) == (
        Arm.LLM,
        BLIND,
    )
    assert metered_arm(bench_manifest()) is None
    tools = LlmToolsArm(
        **BLIND.to_dict() | {"world_subset": BLIND.world_subset},
        lookups=5,
        backtests=2,
        pack_version=1,
    )
    with_tools = replace(base, arms=replace(base.arms, llm_summary=None, llm_tools=tools))
    assert with_tools.arms.model_arm == ("llm_tools", tools) and metered_arm(with_tools) is None


@pytest.mark.parametrize("error", [EvaluationError, FeatureError])
def test_a_store_that_cannot_answer_a_feature_is_a_named_refusal_before_the_model_is_asked(
    monkeypatch, opened_arm, error
):
    path, experiment = opened_arm
    client = _ReportingModel(["never asked for"])
    asked = bench.ChatHypothesist(model=client, label=LABEL)

    def unanswerable(view):
        raise error("rsi_14 has no value at the first training bar")

    monkeypatch.setattr("contrib.discovery.summaryarm.figures", unanswerable)
    with pytest.raises(
        BenchError,
        match=rf"{experiment.experiment_id}: the training window cannot be summarised . rsi_14 "
        rf"has no value",
    ):
        bench._summarised(asked, path, experiment)
    assert client.prompts == []
