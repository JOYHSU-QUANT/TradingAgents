"""The sidecar contract: beside the payload, schema-stamped, atomic, never raises."""

from __future__ import annotations

import json
import logging

from contrib.hyperliquid_perp.common import atomic_io, sidecar
from contrib.hyperliquid_perp.common.sidecar import SIDECAR_SCHEMA, sidecar_path, write_sidecar

_LOGGER = "contrib.hyperliquid_perp.common.sidecar"
_STEM = "BTC-20260315T000000_000000Z"


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_sidecar_path_keeps_the_directory_and_stem_and_takes_the_suffix(tmp_path):
    payload = tmp_path / f"{_STEM}.json"

    assert sidecar_path(str(payload), ".usage.json") == tmp_path / f"{_STEM}.usage.json"
    assert sidecar_path(payload, ".reports.json") == tmp_path / f"{_STEM}.reports.json"


def test_write_puts_the_stamped_record_beside_the_payload_and_leaves_the_payload_alone(tmp_path):
    payload = tmp_path / f"{_STEM}.json"
    payload.write_bytes(b"{}")

    write_sidecar(str(payload), suffix=".x.json", what="x", build=lambda: {"a": 1, "b": ["two"]})

    assert _read(tmp_path / f"{_STEM}.x.json") == {"schema": SIDECAR_SCHEMA, "a": 1, "b": ["two"]}
    assert SIDECAR_SCHEMA == 1  # the format every file on disk so far carries
    assert payload.read_bytes() == b"{}"  # the hash-locked payload is untouched


def test_a_value_json_cannot_carry_is_stored_as_its_str_at_the_leaf_and_warned(tmp_path, caplog):
    class _Message:
        def __str__(self):
            return "message text"

    payload = tmp_path / f"{_STEM}.json"
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        write_sidecar(
            str(payload), suffix=".x.json", what="x", build=lambda: {"in": {"m": _Message()}}
        )

    # The leaf became a string; its container is still a JSON object, not a repr.
    assert _read(tmp_path / f"{_STEM}.x.json")["in"] == {"m": "message text"}
    (warning,) = [r for r in caplog.records if r.name == _LOGGER]
    assert warning.levelno == logging.WARNING
    assert (
        warning.getMessage()
        == "x sidecar: a _Message value JSON cannot carry was stored as its str"
    )


def test_no_payload_path_writes_nothing_and_does_not_build(monkeypatch):
    writes: list = []
    monkeypatch.setattr(sidecar, "atomic_write_bytes", lambda path, data: writes.append(path))

    def _build():
        raise AssertionError("build must not run without a payload path")

    write_sidecar(None, suffix=".x.json", what="x", build=_build)

    assert writes == []


def test_a_builder_that_raises_is_the_same_logged_failure_and_writes_nothing(tmp_path, caplog):
    payload = tmp_path / f"{_STEM}.json"

    def _build():
        raise AttributeError("'list' object has no attribute 'get'")

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        write_sidecar(str(payload), suffix=".x.json", what="x", build=_build)  # must not raise

    (error,) = [r for r in caplog.records if r.name == _LOGGER]
    assert error.getMessage() == "x sidecar could not be written; the decision is unaffected"
    assert error.exc_info is not None
    assert not (tmp_path / f"{_STEM}.x.json").exists()


def test_a_write_failure_is_logged_with_its_traceback_and_never_raises(tmp_path, caplog):
    missing_dir = tmp_path / "no-such-dir" / f"{_STEM}.json"

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        write_sidecar(str(missing_dir), suffix=".x.json", what="x", build=dict)  # must not raise

    (error,) = [r for r in caplog.records if r.name == _LOGGER]
    assert error.levelno == logging.ERROR
    assert error.getMessage() == "x sidecar could not be written; the decision is unaffected"
    assert error.exc_info is not None
    assert not (tmp_path / "no-such-dir").exists()  # no directory is conjured for it


def test_a_failure_mid_write_leaves_the_earlier_record_whole_and_no_tmp(
    tmp_path, monkeypatch, caplog
):
    # Atomic: the record goes to a sibling .tmp and is renamed over the final
    # path, so a failure between the two leaves the previous file untouched —
    # never a half-written one a reader would choke on — and no .tmp behind.
    payload = tmp_path / f"{_STEM}.json"
    payload.write_bytes(b"{}")
    write_sidecar(str(payload), suffix=".x.json", what="x", build=lambda: {"n": 1})
    sidecar_file = tmp_path / f"{_STEM}.x.json"
    first = sidecar_file.read_bytes()

    def _refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", _refuse)
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        write_sidecar(str(payload), suffix=".x.json", what="x", build=lambda: {"n": 2})

    assert sidecar_file.read_bytes() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == [payload.name, sidecar_file.name]
    assert [r.levelno for r in caplog.records if r.name == _LOGGER] == [logging.ERROR]
