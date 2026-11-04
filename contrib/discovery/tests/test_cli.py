"""``generate``: a manifest in, a run directory out, the refusals named on stderr."""

from __future__ import annotations

from pathlib import Path

import pytest

from contrib.autoresearch.evaluator import EvaluationError
from contrib.autoresearch.features import FeatureError
from contrib.autoresearch.split import SplitError
from contrib.autoresearch.store import StoreError
from contrib.discovery import cli, landing
from contrib.discovery.cli import main
from contrib.discovery.landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, world_dir
from contrib.discovery.manifest import WorldKind
from contrib.discovery.worlds import WorldError

from .conftest import example_manifest, small_manifest


def _manifest_file(tmp_path: Path, name: str = "world.json", **changes) -> Path:
    path = tmp_path / name
    path.write_bytes(example_manifest(**changes).dumps().encode("utf-8"))
    return path


def test_generate_lands_the_world_and_its_null_twin_beside_it_and_refuses_a_repeat(
    tmp_path, capsys
):
    manifest = example_manifest()
    root = tmp_path / "bench"
    argv = ["generate", "--manifest", str(_manifest_file(tmp_path)), "--root", str(root)]
    assert main(argv) == 0
    target = world_dir(root, manifest)
    assert (target / MANIFEST_FILENAME).read_bytes() == manifest.dumps().encode("utf-8")
    assert (target / ARTIFACT_FILENAME).is_file()
    out = capsys.readouterr().out
    assert f"store: {target / ARTIFACT_FILENAME}" in out
    assert "4h candles: 2 request(s)" in out
    assert "every feature has a value from bar" in out
    assert "split (4h): holdout:" in out
    assert "planted rule: holds a position on 78 of 400 decision bars (19.5%)" in out
    assert f"manifest: {target / MANIFEST_FILENAME}" in out

    # The same manifest again is refused; the null twin lands beside it,
    # and says nothing about a plant.
    assert main(argv) == 1
    assert "already holds this manifest's world" in capsys.readouterr().err
    twin = _manifest_file(tmp_path, "twin.json", kind=WorldKind.NULL_V1)
    assert main(["generate", "--manifest", str(twin), "--root", str(root)]) == 0
    assert "planted rule" not in capsys.readouterr().out
    assert sorted(p.name for p in root.iterdir()) == sorted(
        [target.name, world_dir(root, example_manifest(kind=WorldKind.NULL_V1)).name]
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
        example_manifest().dumps().replace('"manifest_version": 1', '"manifest_version": 2'),
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
    path.write_bytes(small_manifest(count=30).dumps().encode("utf-8"))
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


def test_usage_errors_exit_2():
    with pytest.raises(SystemExit) as usage:
        main([])
    assert usage.value.code == 2
