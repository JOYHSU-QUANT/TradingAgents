"""The command line's ``verdict``: what it prints, what it records, and its exit codes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from contrib.uniswap_v3 import cli
from contrib.uniswap_v3.agent import graph as _graph
from contrib.uniswap_v3.agent.errors import AgentError, JudgeUnavailable
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.store.repository import open_store
from contrib.uniswap_v3.tests.fakes.node import DAY, DEFAULT_TICK, FIRST_DAY, put_day
from contrib.uniswap_v3.tests.fakes.verdicts import (
    ScriptedJudge,
    judged_config,
    record as _record,
)

_EXAMPLE = Path(__file__).resolve().parents[1] / "configs" / "uniswap_v3.example.yaml"
_DAY2 = FIRST_DAY + 2 * DAY
_NOW = _DAY2 + 600


@pytest.fixture
def config(tmp_path):
    """The example config, with its verdicts section switched on under the source ``judge-v1``."""
    return judged_config(tmp_path, "judge-v1")


@pytest.fixture
def db(tmp_path):
    """A store with the bars of three days, under the test's own directory."""
    path = tmp_path / "data" / "store.db"
    path.parent.mkdir()
    with open_store(path) as store:
        for day in range(3):
            put_day(store, day, eth_tick=DEFAULT_TICK + day * 100)
    return path


def _run(*argv: str, now: int = _NOW) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = cli.main(list(argv), out=lines.append, now=lambda: float(now))
    return code, lines


def _verdict(config: Path, db: Path, *extra: str, now: int = _NOW) -> tuple[int, list[str]]:
    return _run("verdict", "--config", str(config), "--db", str(db), *extra, now=now)


def _judging(monkeypatch, judge, homes: list[Path] | None = None):
    """Have the command's judge be ``judge``, keeping where it was told to put the engine's files."""

    def build(settings, home):
        if homes is not None:
            homes.append(home)
        return judge

    monkeypatch.setattr(_graph, "TradingAgentsJudge", build)


def test_a_fake_rating_is_recorded_for_every_token_and_status_counts_it(config, db, capsys):
    code, lines = _verdict(config, db, "--fake-rating", "Buy")
    assert code == cli.EXIT_OK
    assert lines == [
        "verdicts of judge-v1 at 2024-01-03T00:00:00Z (trade date 2024-01-03):",
        "WBTC (BTC-USD): Buy, model fake, 0 s, no words kept",
        "WETH (ETH-USD): Buy, model fake, 0 s, no words kept",
        "2 asked, 0 already stored",
    ]
    assert capsys.readouterr().err == ""
    code, lines = _run("status", "--config", str(config), "--db", str(db))
    assert code == cli.EXIT_OK
    assert lines[-1] == (
        "verdicts from judge-v1: 1 of the latest 3 bar(s) have one for every token "
        "(WBTC 1, WETH 1)"
    )


def test_run_again_the_command_asks_nothing(config, db):
    assert _verdict(config, db, "--fake-rating", "Buy")[0] == cli.EXIT_OK
    code, lines = _verdict(config, db, "--fake-rating", "Sell", now=_NOW + 3_600)
    assert code == cli.EXIT_OK
    assert lines[1:] == [
        "WBTC (BTC-USD): already stored: Buy, model fake, asked 2024-01-03T00:10:00Z",
        "WETH (ETH-USD): already stored: Buy, model fake, asked 2024-01-03T00:10:00Z",
        "0 asked, 2 already stored",
    ]


def test_at_picks_the_bar_and_must_be_a_boundary_that_has_passed(config, db, capsys):
    code, lines = _verdict(config, db, "--at", "2024-01-02", "--fake-rating", "Hold")
    assert code == cli.EXIT_OK
    assert lines[0] == "verdicts of judge-v1 at 2024-01-02T00:00:00Z (trade date 2024-01-02):"
    with open_store(db, create=False) as store:
        assert store.verdict("judge-v1", "WETH", FIRST_DAY + DAY).verdict.rating is Rating.HOLD
    code, _ = _verdict(config, db, "--at", "2024-01-02T01:00:00", "--fake-rating", "Hold")
    assert code == cli.EXIT_FAILED
    assert "failed: --at 2024-01-02T01:00:00Z is not a bar boundary" in capsys.readouterr().err
    code, _ = _verdict(config, db, "--at", "2024-01-04", "--fake-rating", "Hold")
    assert code == cli.EXIT_FAILED
    assert (
        "failed: --at 2024-01-04T00:00:00Z has not passed; the latest boundary that has is "
        "2024-01-03T00:00:00Z"
    ) in capsys.readouterr().err


def test_a_judge_is_asked_about_the_latest_bar_alone(config, db, monkeypatch, capsys):
    judge = ScriptedJudge()
    _judging(monkeypatch, judge)
    code, lines = _verdict(config, db, "--at", "2024-01-02")
    assert code == cli.EXIT_FAILED and len(lines) == 1 and judge.asked == []
    assert (
        "failed: the bar at 2024-01-02 (2024-01-02T00:00:00Z) is not the latest whose boundary "
        "has passed (2024-01-03T00:00:00Z); the judge is asked about the latest bar alone"
    ) in capsys.readouterr().err
    # Naming the latest boundary is the default spelled out.
    _judging(monkeypatch, ScriptedJudge())
    assert _verdict(config, db, "--at", "2024-01-03")[0] == cli.EXIT_OK


def test_a_bar_whose_boundary_passed_longer_ago_than_the_window_is_left_unrated(
    config, db, capsys, monkeypatch
):
    judge = ScriptedJudge()
    _judging(monkeypatch, judge)
    # The default window is four hours; the boundary passed four hours and a minute ago.
    late = _DAY2 + 4 * 3_600 + 60
    code, lines = _verdict(config, db, now=late)
    assert code == cli.EXIT_OK and judge.asked == []
    assert lines == [
        "verdicts of judge-v1 at 2024-01-03T00:00:00Z (trade date 2024-01-03):",
        "0 asked, 0 already stored, 2 past the judge's window",
    ]
    assert (
        "warning: the boundary 2024-01-03T00:00:00Z passed 14460 s ago, more than "
        "agent.ask_within_seconds (14400 s); the judge would see that long past the fill, so it "
        "is not asked, and WBTC, WETH left unrated"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.verdicts_at("judge-v1", _DAY2) == []
    # What the store holds stands, late or not: one token stored, the other left.
    with open_store(db) as store:
        store.insert_verdict(_record("WETH", 2, Rating.HOLD, source="judge-v1"))
    code, lines = _verdict(config, db, now=late)
    assert code == cli.EXIT_OK and judge.asked == []
    assert lines[1:] == [
        "WETH (ETH-USD): already stored: Hold, model synthetic, asked 2024-01-03T00:10:00Z",
        "0 asked, 1 already stored, 1 past the judge's window",
    ]
    assert "and WBTC left unrated" in capsys.readouterr().err
    # Both stored: nothing is late any more, and nothing is warned of.
    with open_store(db) as store:
        store.insert_verdict(_record("WBTC", 2, Rating.SELL, source="judge-v1"))
    code, lines = _verdict(config, db, now=late)
    assert code == cli.EXIT_OK and lines[-1] == "0 asked, 2 already stored"
    assert "left unrated" not in capsys.readouterr().err


def test_a_fake_rating_is_not_bound_by_the_window(config, db):
    # A rehearsal, not a judge reading the day.
    late = _DAY2 + 4 * 3_600 + 60
    assert _verdict(config, db, "--fake-rating", "Buy", now=late)[0] == cli.EXIT_OK
    with open_store(db, create=False) as store:
        assert len(store.verdicts_at("judge-v1", _DAY2)) == 2


def test_a_bar_the_store_lacks_is_try_again_later(config, db, capsys):
    code, lines = _verdict(config, db, "--fake-rating", "Buy", now=FIRST_DAY + 3 * DAY + 600)
    assert code == cli.EXIT_RETRY
    assert lines == ["verdicts of judge-v1 at 2024-01-04T00:00:00Z (trade date 2024-01-04):"]
    assert "try again later: the store has no bar at 2024-01-04" in capsys.readouterr().err


def test_a_config_that_reads_no_verdicts_fails(db, capsys):
    code, lines = _verdict(_EXAMPLE, db, "--fake-rating", "Buy")
    assert code == cli.EXIT_FAILED and lines == []
    assert "failed: verdict writes under the source" in capsys.readouterr().err


def test_a_store_that_does_not_exist_fails(config, tmp_path, capsys):
    code, _ = _verdict(config, tmp_path / "none.db", "--fake-rating", "Buy")
    assert code == cli.EXIT_FAILED
    assert capsys.readouterr().err.startswith("failed: ")


def test_a_fake_rating_on_a_store_with_real_verdicts_fails(config, db, capsys):
    with open_store(db, create=False) as store:
        store.insert_verdict(_record("WETH", 0, Rating.HOLD, source="judge-v1"))
    code, lines = _verdict(config, db, "--fake-rating", "Buy")
    assert code == cli.EXIT_FAILED
    assert len(lines) == 1
    assert "failed: the store holds verdicts of 'judge-v1' given by ['synthetic']" in (
        capsys.readouterr().err
    )


def test_the_judge_is_built_beside_the_store_and_its_words_are_kept(
    config, db, monkeypatch, capsys
):
    homes: list[Path] = []
    judge = ScriptedJudge({"BTC-USD": Rating.OVERWEIGHT, "ETH-USD": Rating.UNDERWEIGHT})
    _judging(monkeypatch, judge, homes)
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_OK
    assert homes == [db.resolve().parent / "tradingagents"]
    assert lines == [
        "verdicts of judge-v1 at 2024-01-03T00:00:00Z (trade date 2024-01-03):",
        "WBTC (BTC-USD): Overweight, model scripted-model, 2 s, words in "
        "verdicts/judge-v1/WBTC-20240103T000000Z.json",
        "WETH (ETH-USD): Underweight, model scripted-model, 2 s, words in "
        "verdicts/judge-v1/WETH-20240103T000000Z.json",
        "2 asked, 0 already stored",
    ]
    sidecar = db.parent / "verdicts" / "judge-v1" / "WETH-20240103T000000Z.json"
    assert json.loads(sidecar.read_text(encoding="utf-8"))["rating"] == "Underweight"
    assert capsys.readouterr().err == ""
    assert [date for _, date, _ in judge.asked] == ["2024-01-03", "2024-01-03"]


def test_a_judge_that_does_not_answer_is_try_again_later_and_keeps_what_it_said(
    config, db, monkeypatch, capsys
):
    _judging(monkeypatch, ScriptedJudge({"ETH-USD": JudgeUnavailable("the gateway is down")}))
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_RETRY
    assert lines[1] == (
        "WBTC (BTC-USD): Hold, model scripted-model, 2 s, words in "
        "verdicts/judge-v1/WBTC-20240103T000000Z.json"
    )
    assert len(lines) == 2
    assert (
        "try again later: the gateway is down; 1 verdict(s) at 2024-01-03 stay recorded, and a "
        "later visit asks about the rest"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert store.verdict("judge-v1", "WBTC", _DAY2) is not None
        assert store.verdict("judge-v1", "WETH", _DAY2) is None


def test_a_judge_that_cannot_be_built_fails(config, db, monkeypatch, capsys):
    # The real judge refuses on the first question, after the header line is printed.
    judge = ScriptedJudge({"BTC-USD": AgentError("the judge cannot be built (ValueError: no key)")})
    _judging(monkeypatch, judge)
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_FAILED and len(lines) == 1
    assert (
        "failed: the judge cannot be built (ValueError: no key); 0 verdict(s) at 2024-01-03 "
        "stay recorded" in capsys.readouterr().err
    )


def test_a_review_is_recorded_and_warned_of(config, db, monkeypatch, capsys):
    _judging(monkeypatch, ScriptedJudge({"BTC-USD": Rating.REVIEW, "ETH-USD": Rating.HOLD}))
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_OK
    assert lines[1].startswith("WBTC (BTC-USD): REVIEW, model scripted-model")
    assert (
        "warning: the judge gave no rating on WBTC (REVIEW); the verdict is kept as such"
        in capsys.readouterr().err
    )


def test_the_engine_is_not_imported_for_a_fake_rating(config, db, monkeypatch):
    # A module set to None in sys.modules raises ImportError on import: the engine and
    # every module of it another test may have cached are made unimportable.
    for name in [name for name in sys.modules if name.split(".")[0] == "tradingagents"]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, "tradingagents", None)
    assert _verdict(config, db, "--fake-rating", "Buy")[0] == cli.EXIT_OK


def test_a_review_is_warned_of_even_when_a_later_token_fails(config, db, monkeypatch, capsys):
    judge = ScriptedJudge({"BTC-USD": Rating.REVIEW, "ETH-USD": JudgeUnavailable("down")})
    _judging(monkeypatch, judge)
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_RETRY
    err = capsys.readouterr().err
    assert "warning: the judge gave no rating on WBTC (REVIEW)" in err
    assert "try again later: down; 1 verdict(s) at 2024-01-03 stay recorded" in err


def test_a_judge_that_fails_for_good_on_a_later_token_keeps_the_earlier_and_fails(
    config, db, monkeypatch, capsys
):
    _judging(monkeypatch, ScriptedJudge({"ETH-USD": AgentError("the judge failed for good")}))
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_FAILED and len(lines) == 2
    assert (
        "failed: the judge failed for good; 1 verdict(s) at 2024-01-03 stay recorded"
        in capsys.readouterr().err
    )


def test_a_sidecar_that_cannot_be_written_fails_without_a_traceback(
    config, db, monkeypatch, capsys
):
    _judging(monkeypatch, ScriptedJudge({"ETH-USD": Rating.SELL}))
    replace = os.replace

    def refuse(source, target):
        # The second token's sidecar fails; the first was written and recorded.
        if "WETH" in str(target):
            raise PermissionError("the target is held open")
        replace(source, target)

    monkeypatch.setattr(os, "replace", refuse)
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_FAILED and len(lines) == 2
    assert (
        "failed: the sidecar for WETH at 2024-01-03 could not be written at "
        "verdicts/judge-v1/WETH-20240103T000000Z.json (PermissionError: the target is held "
        "open); the judge's answer (Sell) is not recorded, 1 verdict(s) at 2024-01-03 stay "
        "recorded, and a later visit asks again"
    ) in capsys.readouterr().err
    with open_store(db, create=False) as store:
        assert [r.verdict.symbol for r in store.verdicts_at("judge-v1", _DAY2)] == ["WBTC"]
    assert not list((db.parent / "verdicts" / "judge-v1").glob("WETH*"))


def test_an_answer_another_visit_overtook_is_warned_of(config, db, monkeypatch, capsys):
    class Overtaken(ScriptedJudge):
        def ask(self, ticker, trade_date, context):
            symbol = "WBTC" if ticker == "BTC-USD" else "WETH"
            with open_store(db, create=False) as store:
                store.insert_verdict(_record(symbol, 2, source="judge-v1"))
            return super().ask(ticker, trade_date, context)

    _judging(monkeypatch, Overtaken())
    code, lines = _verdict(config, db)
    assert code == cli.EXIT_OK and lines[-1] == "0 asked, 2 already stored"
    assert lines[1].startswith("WBTC (BTC-USD): already stored: Buy, model synthetic")
    err = capsys.readouterr().err
    assert "warning: another visit recorded WBTC while this one's judge was thinking" in err
    assert "warning: another visit recorded WETH" in err
