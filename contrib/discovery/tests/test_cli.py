"""The four commands: ``generate`` lands a run directory, ``oracle``/``search``/``calibrate`` print what the bench returns; every refusal family exits with its code and sentence on stderr."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.features import FeatureError
from contrib.autoresearch.ledger import LedgerError
from contrib.autoresearch.split import SplitError
from contrib.autoresearch.store import StoreError
from contrib.discovery import cli, landing
from contrib.discovery.bench import ORACLE_FILENAME, VERDICTS_FILENAME, BenchError, read_manifest
from contrib.discovery.cli import main
from contrib.discovery.landing import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    artifact_digest,
    conditions_digest,
    world_dir,
)
from contrib.discovery.manifest import ManifestError, WorldKind
from contrib.discovery.oracle import OracleError
from contrib.discovery.scoring import SCORES_FILENAME, ScoreError
from contrib.discovery.verdict import VerdictError
from contrib.discovery.worlds import WorldError

from .conftest import example_manifest, small_manifest


def _landable(manifest):
    """The example manifest without its model arm: these tests build no model."""
    return replace(manifest, arms=replace(manifest.arms, llm=None))


def _manifest_file(tmp_path: Path, name: str = "world.json", **changes) -> Path:
    path = tmp_path / name
    path.write_bytes(_landable(example_manifest(**changes)).dumps().encode("utf-8"))
    return path


def test_generate_lands_the_world_and_its_null_twin_beside_it_and_refuses_a_repeat(
    tmp_path, capsys, example_world
):
    manifest = _landable(example_manifest())
    root = tmp_path / "bench"
    argv = ["generate", "--manifest", str(_manifest_file(tmp_path)), "--root", str(root)]
    assert main(argv) == 0
    target = world_dir(root, manifest)
    # Landed with the pool's size and the artifact's digest filled in: the
    # two fields the directory's digest sets aside, so it is still the
    # unfilled manifest's.
    space = manifest.search_space
    filled = replace(
        manifest,
        search_space=replace(space, pool_size=16),
        artifact_sha256=artifact_digest(target / ARTIFACT_FILENAME),
    )
    assert (target / MANIFEST_FILENAME).read_bytes() == filled.dumps().encode("utf-8")
    assert (target / ARTIFACT_FILENAME).is_file()
    out = capsys.readouterr().out
    assert f"store: {target / ARTIFACT_FILENAME}" in out
    assert "pool: 16 candidate(s) (16 enumerated)" in out
    assert "4h candles: 2 request(s)" in out
    assert "every feature has a value from bar" in out
    assert "split (4h): holdout:" in out
    held = example_world.planted_exposure  # pinned once, in test_worlds
    assert (
        f"planted rule: holds a position on {held} of 400 decision bars ({held / 400:.1%})" in out
    )
    assert f"manifest: {target / MANIFEST_FILENAME}" in out

    # The same manifest again is refused; the null twin lands beside it,
    # and says nothing about a plant.
    assert main(argv) == 1
    assert "already holds this manifest's world" in capsys.readouterr().err
    twin = _manifest_file(tmp_path, "twin.json", kind=WorldKind.NULL_V1)
    assert main(["generate", "--manifest", str(twin), "--root", str(root)]) == 0
    assert "planted rule" not in capsys.readouterr().out
    assert sorted(p.name for p in root.iterdir()) == sorted(
        [target.name, world_dir(root, _landable(example_manifest(kind=WorldKind.NULL_V1))).name]
    )


def test_a_manifest_that_cannot_be_read_is_a_named_refusal(tmp_path, capsys):
    root = str(tmp_path / "bench")
    assert main(["generate", "--manifest", str(tmp_path / "missing.json"), "--root", root]) == 1
    assert "cannot read the manifest" in capsys.readouterr().err
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    assert main(["generate", "--manifest", str(broken), "--root", root]) == 1
    assert "not valid JSON" in capsys.readouterr().err
    binary = tmp_path / "binary.json"
    binary.write_bytes(b"\xff\xfe{")
    assert main(["generate", "--manifest", str(binary), "--root", root]) == 1
    assert "cannot read the manifest" in capsys.readouterr().err
    other = tmp_path / "other.json"
    other.write_text(
        example_manifest().dumps().replace('"manifest_version": 3', '"manifest_version": 4'),
        encoding="utf-8",
    )
    assert main(["generate", "--manifest", str(other), "--root", root]) == 1
    assert "manifest_version" in capsys.readouterr().err
    assert not Path(root).exists()  # nothing was written for any of them


def test_a_landing_the_checks_refuse_exits_1_and_leaves_nothing_behind(
    tmp_path, capsys, monkeypatch
):
    def refuse(store, m):
        raise WorldError("the world is not fit to measure on")

    monkeypatch.setattr(landing, "verify", refuse)
    root = tmp_path / "bench"
    path = tmp_path / "small.json"
    path.write_bytes(_landable(small_manifest(count=30)).dumps().encode("utf-8"))
    assert main(["generate", "--manifest", str(path), "--root", str(root)]) == 1
    assert "not fit to measure on" in capsys.readouterr().err
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (KeyboardInterrupt(), 130),
        (EvaluationError("the history is not a grid"), 1),
        (FeatureError("a feature has no value anywhere"), 1),
        (StoreError("cannot open the store"), 1),
        (SplitError("the span is too short to cut"), 1),
    ],
    ids=["interrupt", "evaluation", "feature", "store", "split"],
)
def test_the_named_families_exit_with_their_code_and_sentence(
    tmp_path, capsys, monkeypatch, raised, code
):
    def fail(manifest):
        raise raised

    monkeypatch.setattr(cli, "generate", fail)
    argv = ["generate", "--manifest", str(_manifest_file(tmp_path)), "--root", str(tmp_path)]
    assert main(argv) == code
    err = capsys.readouterr().err
    assert ("interrupted" if code == 130 else f"error: {raised}") in err


def test_a_defect_is_not_dressed_as_a_refusal(tmp_path, monkeypatch):
    def defect(manifest):
        raise ValueError("a bare ValueError is a bug, not a refusal")

    monkeypatch.setattr(cli, "generate", defect)
    argv = ["generate", "--manifest", str(_manifest_file(tmp_path)), "--root", str(tmp_path)]
    with pytest.raises(ValueError, match="a bug"):
        main(argv)


def test_a_root_that_cannot_be_written_is_a_named_refusal(tmp_path, capsys):
    # A file where the bench root should be: the run directory cannot be
    # made, and the OSError is the operator's, named rather than traced.
    blocker = tmp_path / "bench"
    blocker.write_text("not a directory", encoding="utf-8")
    path = tmp_path / "small.json"
    path.write_bytes(_landable(small_manifest(count=30)).dumps().encode("utf-8"))
    assert main(["generate", "--manifest", str(path), "--root", str(blocker)]) == 1
    assert "error:" in capsys.readouterr().err
    assert blocker.read_text(encoding="utf-8") == "not a directory"


def test_usage_errors_exit_2_and_help_survives_a_percent_in_the_default_root(monkeypatch):
    with pytest.raises(SystemExit) as usage:
        main([])
    assert usage.value.code == 2
    # argparse %-formats help text; a repository under ``100%done`` must not break it.
    monkeypatch.setattr(cli, "default_root", lambda: Path("C:/work/100%done/data/discovery"))
    with pytest.raises(SystemExit) as shown:
        main(["generate", "--help"])
    assert shown.value.code == 0
    with pytest.raises(SystemExit) as shown:
        main(["calibrate", "--help"])
    assert shown.value.code == 0


# -- oracle / search / calibrate (PR 3) ------------------------------------------------------


def test_search_runs_the_world_reporting_as_it_goes_and_names_the_records(
    tmp_path, capsys, monkeypatch
):
    seen = {}

    def fake_run(run_dir, *, on_line):
        seen["run"] = run_dir
        on_line("pool: 16 candidate(s) (16 enumerated)")
        on_line("grid-0: ended budget_spent")
        return None, None

    monkeypatch.setattr(cli, "run_world", fake_run)
    assert main(["search", "--run", str(tmp_path / "run")]) == 0
    out = capsys.readouterr().out.splitlines()
    assert seen["run"] == tmp_path / "run"
    assert out == [
        "pool: 16 candidate(s) (16 enumerated)",
        "grid-0: ended budget_spent",
        f"verdicts: {tmp_path / 'run' / VERDICTS_FILENAME}",
        f"oracle: {tmp_path / 'run' / ORACLE_FILENAME}",
    ]


def test_oracle_prints_the_preview_s_lines(tmp_path, capsys, monkeypatch):
    class Figures:
        def describe(self):
            return ["planted rule: train net sharpe 1.0000, validation 2.0000"]

    monkeypatch.setattr(cli, "preview_oracle", lambda run_dir: Figures())
    assert main(["oracle", "--run", str(tmp_path)]) == 0
    assert capsys.readouterr().out == "planted rule: train net sharpe 1.0000, validation 2.0000\n"


def test_calibrate_tallies_the_root_and_says_when_nothing_is_searched(searched, tmp_path, capsys):
    run, *_ = searched
    assert main(["calibrate", "--root", str(run.parent)]) == 0
    out = capsys.readouterr().out.splitlines()
    conditions = conditions_digest(read_manifest(run))
    assert [line.split(":")[0] for line in out] == [
        f"conditional-v1 (v1) dev [{conditions}] random",
        f"conditional-v1 (v1) dev [{conditions}] grid",
    ]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["calibrate", "--root", str(empty)]) == 0
    assert capsys.readouterr().out == f"no searched world under {empty}\n"
    assert main(["calibrate", "--root", str(tmp_path / "missing")]) == 1
    assert "is not a directory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (KeyboardInterrupt(), 130),
        (BenchError("this world was searched before"), 1),
        (VerdictError("the record cannot be read"), 1),
        (OracleError("the record cannot be read"), 1),
        (ManifestError("cannot read the landed manifest"), 1),
    ],
    ids=["interrupt", "bench", "verdict", "oracle", "manifest"],
)
def test_search_s_named_families_exit_with_their_code_and_sentence(
    tmp_path, capsys, monkeypatch, raised, code
):
    def fail(run_dir, *, on_line):
        raise raised

    monkeypatch.setattr(cli, "run_world", fail)
    assert main(["search", "--run", str(tmp_path)]) == code
    err = capsys.readouterr().err
    assert ("interrupted" if code == 130 else f"error: {raised}") in err


def test_generate_refuses_a_manifest_whose_pool_cannot_be_written_before_landing_anything(
    tmp_path, capsys, monkeypatch
):
    # A label on a numeric feature's grid (only a categorical feature's grid
    # takes labels): refused where the
    # manifest is read for the run, before the minutes of backfill.
    def never(manifest):
        raise AssertionError("the world must not be built")

    monkeypatch.setattr(cli, "generate", never)
    text = example_manifest().dumps().replace("0.004", '"high"', 1)
    path = tmp_path / "mixed.json"
    path.write_text(text, encoding="utf-8")
    root = tmp_path / "bench"
    assert main(["generate", "--manifest", str(path), "--root", str(root)]) == 1
    assert "realized_vol_20 is a number, so its grid is numbers" in capsys.readouterr().err
    assert not root.exists()


def test_generate_refuses_a_model_it_cannot_build_before_landing_anything(
    tmp_path, capsys, monkeypatch
):
    def refuse(llm):
        raise BenchError("arms.llm: the key is not set")

    monkeypatch.setattr(cli, "model_hypothesist", refuse)
    path = tmp_path / "model.json"
    path.write_bytes(example_manifest().dumps().encode("utf-8"))
    root = tmp_path / "bench"
    assert main(["generate", "--manifest", str(path), "--root", str(root)]) == 1
    assert "arms.llm: the key is not set" in capsys.readouterr().err
    assert not root.exists()


def test_a_ledger_refusal_inside_a_search_is_not_dressed_as_one(tmp_path, monkeypatch):
    # The ledger's own refusals are ``RuntimeError``s this CLI does not
    # list: inside a search they are a defect of this package's sequencing.
    def defect(run_dir, *, on_line):
        raise LedgerError("trial #1 cannot be promoted")

    monkeypatch.setattr(cli, "run_world", defect)
    with pytest.raises(LedgerError):
        main(["search", "--run", str(tmp_path)])


# -- score / report (PR 4) ------------------------------------------------------------------------


def test_score_prints_what_the_scorer_reports_and_names_the_record(tmp_path, capsys, monkeypatch):
    def fake(run_dir, *, on_line):
        on_line("grid-0: trial #3: recovered")

    monkeypatch.setattr(cli, "score_world", fake)
    assert main(["score", "--run", str(tmp_path)]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "grid-0: trial #3: recovered",
        f"scores: {tmp_path / SCORES_FILENAME}",
    ]


def test_report_prints_the_rows_and_says_when_nothing_is_searched(tmp_path, capsys, monkeypatch):
    class Row:
        unsealed = []

        def describe(self, *, unseal):
            self.unsealed.append(unseal)
            return ["headline", "  diagnostics"]

    (tmp_path / "world").mkdir()
    (tmp_path / "a-file").write_text("not a run", encoding="utf-8")
    seen = {}

    def fake_table(run_dirs):
        seen["dirs"] = run_dirs
        return [Row()]

    monkeypatch.setattr(cli, "report_table", fake_table)
    assert main(["report", "--root", str(tmp_path)]) == 0
    assert seen["dirs"] == [tmp_path / "world"]
    assert capsys.readouterr().out.splitlines() == ["headline", "  diagnostics"]
    assert main(["report", "--root", str(tmp_path), "--unseal"]) == 0
    capsys.readouterr()
    assert Row.unsealed == [False, True]
    monkeypatch.setattr(cli, "report_table", lambda run_dirs: [])
    assert main(["report", "--root", str(tmp_path)]) == 0
    assert capsys.readouterr().out == f"no searched world under {tmp_path}\n"
    assert main(["report", "--root", str(tmp_path / "missing")]) == 1
    assert "is not a directory" in capsys.readouterr().err


def test_generate_refuses_a_scorer_this_build_does_not_have_before_landing_anything(
    tmp_path, capsys
):
    # A world is searched once; one this build cannot score would spend it.
    path = tmp_path / "other-scorer.json"
    text = _landable(example_manifest()).dumps()
    assert text.count('"version": "1"') == 1
    path.write_text(text.replace('"version": "1"', '"version": "2"'), encoding="utf-8")
    root = tmp_path / "bench"
    assert main(["generate", "--manifest", str(path), "--root", str(root)]) == 1
    assert "this build scores by version 1" in capsys.readouterr().err
    assert not root.exists()


def test_a_score_refusal_exits_1_with_its_sentence(tmp_path, capsys, monkeypatch):
    def refuse(run_dir, *, on_line):
        raise ScoreError("this world was not searched")

    monkeypatch.setattr(cli, "score_world", refuse)
    assert main(["score", "--run", str(tmp_path)]) == 1
    assert "error: this world was not searched" in capsys.readouterr().err
