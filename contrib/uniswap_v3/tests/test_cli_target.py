"""The command line's ``target``: what it prints, what it records, and its exit codes."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.store.repository import Store, StoreError, open_store
from contrib.uniswap_v3.tests.fakes.node import DAY, FIRST_DAY, put_day
from contrib.uniswap_v3.tests.fakes.targets import (
    handoff_document,
    targeted_config,
    write_handoff,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_DAY2 = FIRST_DAY + 2 * DAY
# Ten past midnight, the scheduled visit.
_NOW = _DAY2 + 600


@pytest.fixture
def config(tmp_path):
    """The example config as a carry run's: WETH alone, external_weights, targets of ``carry-eth-v1``."""
    return targeted_config(tmp_path, "carry-eth-v1")


@pytest.fixture
def db(tmp_path):
    """A store with the bars of three days, under the test's own directory."""
    path = tmp_path / "data" / "store.db"
    path.parent.mkdir()
    with open_store(path) as store:
        for day in range(3):
            put_day(store, day)
    return path


@pytest.fixture
def handoff(tmp_path):
    """The coordinator's file for the third day, written ten minutes before it: WETH at 0.3."""
    path = tmp_path / "handoff-eth.json"
    write_handoff(path, handoff_document(as_of=_DAY2))
    return path


def _run(*argv: str, now: int = _NOW) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(list(argv), out=lines.append, now=lambda: float(now))
    return code, lines


def _target(config: Path, db: Path, handoff: Path, *extra: str, now: int = _NOW):
    return _run(
        "target",
        "--config",
        str(config),
        "--db",
        str(db),
        "--handoff",
        str(handoff),
        *extra,
        now=now,
    )


def test_the_handoffs_weight_is_written_at_its_boundary_and_status_counts_it(
    config, db, handoff, capsys
):
    code, lines = _target(config, db, handoff)
    assert code == cli.EXIT_OK
    assert lines == [
        "target of carry-eth-v1 at 2024-01-03T00:00:00Z from handoff-eth.json (ETH, enter):",
        "WETH: 0.3 written",
    ]
    assert capsys.readouterr().err == ""
    with open_store(db, create=False) as store:
        stored = store.target("carry-eth-v1", "WETH", _DAY2)
    assert stored.target.weight == Decimal("0.3")
    assert stored.written_at == _NOW
    assert stored.digest == write_handoff(handoff, handoff_document(as_of=_DAY2))
    code, lines = _run("status", "--config", str(config), "--db", str(db))
    assert code == cli.EXIT_OK
    assert lines[-1] == (
        "targets from carry-eth-v1: 1 of the latest 3 bar(s) have one for every token (WETH 1)"
    )
    assert not any(line.startswith("verdicts from") for line in lines)


def test_run_again_the_command_writes_nothing(config, db, handoff, capsys):
    assert _target(config, db, handoff)[0] == cli.EXIT_OK
    code, lines = _target(config, db, handoff, now=_NOW + 3_600)
    assert code == cli.EXIT_OK
    assert lines[1] == "WETH: already stored: 0.3, written 2024-01-03T00:10:00Z"
    assert capsys.readouterr().err == ""


def test_a_handoff_rerun_with_another_weight_is_warned_of_and_the_store_stands(
    config, db, handoff, capsys
):
    assert _target(config, db, handoff)[0] == cli.EXIT_OK
    write_handoff(handoff, handoff_document(as_of=_DAY2, weight="0.35", action="hold"))
    code, lines = _target(config, db, handoff, now=_NOW + 3_600)
    assert code == cli.EXIT_OK
    assert lines == [
        "target of carry-eth-v1 at 2024-01-03T00:00:00Z from handoff-eth.json (ETH, hold):",
        "WETH: already stored: 0.3, written 2024-01-03T00:10:00Z",
    ]
    assert (
        "warning: the handoff says 0.35 for WETH at 2024-01-03T00:00:00Z, and the store holds "
        "0.3; a target is never rewritten, and the run decides on what the store holds"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.target("carry-eth-v1", "WETH", _DAY2).target.weight == Decimal("0.3")


def test_a_store_that_refuses_the_write_for_another_reason_fails(
    config, db, handoff, monkeypatch, capsys
):
    # The store's refusal is read back as "already stored" only when the row is there.
    def refuse(self, record):
        raise StoreError("the store failed while writing a target (disk full)")

    monkeypatch.setattr(Store, "insert_target", refuse)
    code, lines = _target(config, db, handoff)
    assert code == cli.EXIT_FAILED and len(lines) == 1
    assert "failed: the store failed while writing a target (disk full)" in (
        capsys.readouterr().err
    )


def test_a_handoff_without_an_action_is_headed_by_its_coin_alone(config, db, tmp_path):
    path = tmp_path / "handoff-eth.json"
    write_handoff(path, handoff_document(as_of=_DAY2, action=None))
    code, lines = _target(config, db, path)
    assert code == cli.EXIT_OK
    assert lines[0] == "target of carry-eth-v1 at 2024-01-03T00:00:00Z from handoff-eth.json (ETH):"


def test_the_bar_need_not_be_in_the_store(config, tmp_path, handoff):
    db = tmp_path / "empty.db"
    open_store(db).close()
    assert _target(config, db, handoff)[0] == cli.EXIT_OK
    with open_store(db, create=False) as store:
        assert store.target("carry-eth-v1", "WETH", _DAY2) is not None


def test_a_handoff_not_there_yet_is_try_again_later(config, db, tmp_path, capsys):
    code, lines = _target(config, db, tmp_path / "handoff-eth.json")
    assert code == cli.EXIT_RETRY and lines == []
    assert "try again later: no handoff at " in capsys.readouterr().err


def test_a_boundary_not_reached_or_a_day_behind_is_try_again_later(config, db, handoff, capsys):
    # The visit before midnight sees tomorrow's document.
    code, lines = _target(config, db, handoff, now=_DAY2 - 600)
    assert code == cli.EXIT_RETRY and lines == []
    assert (
        "try again later: the handoff is for 2024-01-03T00:00:00Z, which has not come yet "
        "(now 2024-01-02T23:50:00Z)"
    ) in capsys.readouterr().err
    # The coordinator did not run today: yesterday's file is still there.
    code, lines = _target(config, db, handoff, now=_DAY2 + DAY + 600)
    assert code == cli.EXIT_RETRY and lines == []
    assert (
        "try again later: the handoff is for 2024-01-03T00:00:00Z, a day or more before now, "
        "2024-01-04T00:10:00Z; the coordinator has not written a newer one"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.targets_at("carry-eth-v1", _DAY2) == []


def test_a_boundary_more_than_a_day_ahead_fails(config, db, handoff, capsys):
    code, _ = _target(config, db, handoff, now=_DAY2 - DAY - 1)
    assert code == cli.EXIT_FAILED
    assert "failed: the handoff is for 2024-01-03T00:00:00Z, more than a day after now" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"version": 2}, "failed: handoff version 2 is not 1"),
        (
            {"spot": {"token": "WBTC", "weight": "0.3"}},
            "failed: the config trades ['WETH'] and the handoff sizes 'WBTC' alone; a run that "
            "reads targets trades the handoff's token and no other",
        ),
        ({"spot": {"token": "WETH", "weight": "1.5"}}, "spot.weight must be a Decimal in [0, 1]"),
        (
            {"as_of_ms": (_DAY2 + 3_600) * 1_000},
            "failed: as_of 2024-01-03T01:00:00Z is not a UTC day boundary",
        ),
    ],
)
def test_a_handoff_this_leg_cannot_act_on_fails(config, db, tmp_path, capsys, changes, match):
    path = tmp_path / "handoff-eth.json"
    write_handoff(path, handoff_document(as_of=_DAY2, **changes))
    code, lines = _target(config, db, path, now=_DAY2 + 7_200)
    assert code == cli.EXIT_FAILED and lines == []
    assert match in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.targets_at("carry-eth-v1", _DAY2) == []


def test_a_file_that_is_not_json_fails(config, db, tmp_path, capsys):
    path = tmp_path / "handoff-eth.json"
    path.write_text("{", encoding="utf-8")
    code, lines = _target(config, db, path)
    assert code == cli.EXIT_FAILED and lines == []
    err = capsys.readouterr().err
    assert err.startswith("failed: ") and "is not UTF-8 JSON" in err


def test_unknown_keys_in_the_handoff_are_ignored(config, db, tmp_path):
    path = tmp_path / "handoff-eth.json"
    doc = handoff_document(as_of=_DAY2, later="field")
    doc["spot"]["note"] = "added under the same version"
    write_handoff(path, doc)
    assert _target(config, db, path)[0] == cli.EXIT_OK


def test_a_config_that_reads_no_targets_fails(db, handoff, capsys):
    code, lines = _target(_EXAMPLE, db, handoff)
    assert code == cli.EXIT_FAILED and lines == []
    assert "failed: target writes under the source" in capsys.readouterr().err


def test_a_store_that_does_not_exist_fails_before_anything_is_printed(
    config, tmp_path, handoff, capsys
):
    code, lines = _target(config, tmp_path / "none.db", handoff)
    assert code == cli.EXIT_FAILED and lines == []
    assert capsys.readouterr().err.startswith("failed: there is no store at")


def test_a_config_that_trades_more_than_the_handoffs_token_is_refused_before_the_write(
    db, handoff, tmp_path, capsys
):
    # The strategy would refuse every bar with a target on WETH and none on WBTC, and
    # the row could never be taken back: the write is where this is caught.
    wide = targeted_config(tmp_path, "carry-eth-v1", wbtc=True)
    code, lines = _target(wide, db, handoff)
    assert code == cli.EXIT_FAILED and lines == []
    assert (
        "failed: the config trades ['WBTC', 'WETH'] and the handoff sizes 'WETH' alone; a run "
        "that reads targets trades the handoff's token and no other"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.targets_at("carry-eth-v1", _DAY2) == []


def test_a_config_whose_bars_are_not_a_day_long_is_refused(config, db, handoff, capsys):
    # The config's own rule (a stored target lands on one bar; with hourly bars the
    # strategy would hold, and warn, at 23 bars a day), met here through the option.
    code, lines = _target(config, db, handoff, "--interval-seconds", "3600", now=_DAY2 + 7_200)
    assert code == cli.EXIT_FAILED and lines == []
    assert (
        "failed: a config that reads targets takes daily bars: bars.interval_seconds is 3600, "
        "and a target is for one day (86400 seconds)"
    ) in capsys.readouterr().err
