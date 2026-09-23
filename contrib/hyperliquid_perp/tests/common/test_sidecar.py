"""The sidecar contract: beside the payload, atomic, never raises."""

from __future__ import annotations

import json
import logging

from contrib.hyperliquid_perp.common import atomic_io
from contrib.hyperliquid_perp.common.sidecar import sidecar_path, write_sidecar

_LOGGER = "contrib.hyperliquid_perp.common.sidecar"
_STEM = "BTC-20260315T000000_000000Z"


def test_sidecar_path_keeps_the_directory_and_stem_and_takes_the_suffix(tmp_path):
    payload = tmp_path / f"{_STEM}.json"

    assert sidecar_path(str(payload), ".usage.json") == tmp_path / f"{_STEM}.usage.json"
    assert sidecar_path(payload, ".reports.json") == tmp_path / f"{_STEM}.reports.json"


def test_write_puts_the_record_beside_the_payload_and_leaves_the_payload_alone(tmp_path):
    payload = tmp_path / f"{_STEM}.json"
    payload.write_bytes(b"{}")

    write_sidecar(str(payload), suffix=".x.json", record={"a": 1, "b": ["two"]}, what="x")

    assert json.loads((tmp_path / f"{_STEM}.x.json").read_text(encoding="utf-8")) == {
        "a": 1,
        "b": ["two"],
    }
    assert payload.read_bytes() == b"{}"  # the hash-locked payload is untouched


def test_a_value_json_cannot_carry_is_stored_as_its_str_at_the_leaf(tmp_path):
    class _Message:
        def __str__(self):
            return "message text"

    payload = tmp_path / f"{_STEM}.json"
    write_sidecar(str(payload), suffix=".x.json", record={"inner": {"m": _Message()}}, what="x")

    # The leaf became a string; its container is still a JSON object, not a repr.
    record = json.loads((tmp_path / f"{_STEM}.x.json").read_text(encoding="utf-8"))
    assert record == {"inner": {"m": "message text"}}


def test_no_payload_path_writes_nothing(tmp_path):
    write_sidecar(None, suffix=".x.json", record={"a": 1}, what="x")

    assert list(tmp_path.iterdir()) == []


def test_a_write_failure_is_logged_with_its_traceback_and_never_raises(tmp_path, caplog):
    missing_dir = tmp_path / "no-such-dir" / f"{_STEM}.json"

    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        write_sidecar(str(missing_dir), suffix=".x.json", record={}, what="x")  # must not raise

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
    write_sidecar(str(payload), suffix=".x.json", record={"n": 1}, what="x")
    sidecar = tmp_path / f"{_STEM}.x.json"
    first = sidecar.read_bytes()

    def _refuse(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(atomic_io.os, "replace", _refuse)
    with caplog.at_level(logging.ERROR, logger=_LOGGER):
        write_sidecar(str(payload), suffix=".x.json", record={"n": 2}, what="x")

    assert sidecar.read_bytes() == first
    assert sorted(p.name for p in tmp_path.iterdir()) == [payload.name, sidecar.name]
    assert [r.levelno for r in caplog.records if r.name == _LOGGER] == [logging.ERROR]
