"""``generate``: a manifest in, a run directory out, the refusals named on stderr."""

from __future__ import annotations

from pathlib import Path

import pytest

from contrib.discovery.cli import main
from contrib.discovery.landing import ARTIFACT_FILENAME, MANIFEST_FILENAME, world_dir
from contrib.discovery.manifest import WorldKind

from .conftest import example_manifest


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
    assert f"manifest: {target / MANIFEST_FILENAME}" in out

    # The same manifest again is refused; the null twin lands beside it.
    assert main(argv) == 1
    assert "already holds this manifest's world" in capsys.readouterr().err
    twin = _manifest_file(tmp_path, "twin.json", kind=WorldKind.NULL_V1)
    assert main(["generate", "--manifest", str(twin), "--root", str(root)]) == 0
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
    other = tmp_path / "other.json"
    other.write_text(
        example_manifest().dumps().replace('"manifest_version": 1', '"manifest_version": 2'),
        encoding="utf-8",
    )
    assert main(["generate", "--manifest", str(other), "--root", root]) == 1
    assert "manifest_version" in capsys.readouterr().err
    assert not Path(root).exists()  # nothing was written for any of them


def test_usage_errors_exit_2():
    with pytest.raises(SystemExit) as usage:
        main([])
    assert usage.value.code == 2
