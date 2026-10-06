"""The store's verdicts: written once, read by source and bar, and kept with the decisions that saw them."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from contrib.uniswap_v3.domain.ledger import Ledger
from contrib.uniswap_v3.domain.records import (
    Decision,
    Outcome,
    RunRecord,
    StepRecord,
    Valuation,
)
from contrib.uniswap_v3.domain.types import RunMode
from contrib.uniswap_v3.domain.verdicts import Rating
from contrib.uniswap_v3.store.repository import StoreError, open_store
from contrib.uniswap_v3.store.schema import SCHEMA_VERSION
from contrib.uniswap_v3.store.verdict_source import load_verdicts
from contrib.uniswap_v3.tests.fakes.engine import (
    DAY,
    FIRST_DAY,
    PRICES,
    config as _plain_config,
    ledger as _ledger,
)
from contrib.uniswap_v3.tests.fakes.node import store_at as _store_at
from contrib.uniswap_v3.tests.fakes.verdicts import SOURCE, config as _config, record, verdict

_DIGEST = "ab" * 32
_CONFIG = _config()


@pytest.fixture
def store(tmp_path):
    with open_store(tmp_path / "store.db") as opened:
        yield opened


def _day(day: int) -> int:
    return FIRST_DAY + day * DAY


def _run(run_id: str = "run-1") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        mode=RunMode.BACKTEST,
        chain_id=1,
        quote="USDC",
        strategy="fixed_weights",
        config='{"chain_id":1}',
        ledger=_ledger(),
        created_at=FIRST_DAY,
    )


def _held(time: int = FIRST_DAY, ledger: Ledger | None = None, **changes) -> StepRecord:
    ledger = ledger or _ledger()
    return StepRecord(
        decision=Decision(time=time, outcome=Outcome.HOLD, close_block=9, **changes),
        valuation=Valuation(
            time=time, ledger=ledger, prices=PRICES, total_value=ledger.balances["USDC"]
        ),
    )


# --- verdicts --------------------------------------------------------------


def test_a_verdict_comes_back_as_it_was_written(store):
    said = record()
    store.insert_verdict(said)
    assert store.verdict(SOURCE, "WETH", FIRST_DAY) == said
    assert store.verdict(SOURCE, "WETH", _day(1)) is None
    assert store.verdict(SOURCE, "WBTC", FIRST_DAY) is None
    assert store.verdict("another-judge", "WETH", FIRST_DAY) is None


def test_a_verdict_with_a_sidecar_keeps_where_its_words_are(store):
    kept = record("WBTC", 1, Rating.REVIEW, sidecar_path="verdicts/x.json", sidecar_digest=_DIGEST)
    store.insert_verdict(kept)
    assert store.verdict(SOURCE, "WBTC", _day(1)) == kept


def test_a_verdict_is_written_once_and_never_rewritten(store):
    store.insert_verdict(record())
    with pytest.raises(StoreError, match="never rewritten"):
        store.insert_verdict(record(rating=Rating.SELL))
    assert store.verdict(SOURCE, "WETH", FIRST_DAY).verdict.rating is Rating.BUY


def test_the_verdicts_at_a_bar_are_those_of_one_source_by_symbol(store):
    for said in (
        record("WETH", 0),
        record("WBTC", 0, Rating.HOLD),
        record("WETH", 1, Rating.SELL),
        record("WETH", 0, source="another-judge"),
    ):
        store.insert_verdict(said)
    assert store.verdicts_at(SOURCE, _day(0)) == [record("WBTC", 0, Rating.HOLD), record("WETH", 0)]
    assert store.verdicts_at(SOURCE, _day(1)) == [record("WETH", 1, Rating.SELL)]
    assert store.verdicts_at(SOURCE, _day(2)) == []
    assert store.verdicts_at("another-judge", _day(0)) == [
        record("WETH", 0, source="another-judge")
    ]


def test_the_schema_refuses_a_sidecar_path_without_its_digest(tmp_path):
    path = tmp_path / "store.db"
    open_store(path).close()
    connection = sqlite3.connect(path)
    for path_value, digest in (("x.json", None), (None, _DIGEST)):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO verdicts VALUES (?, 'WETH', ?, 'Buy', 'm', 'v', 0, ?, ?, ?)",
                (SOURCE, FIRST_DAY, _DIGEST, path_value, digest),
            )
    connection.close()


@pytest.mark.parametrize(
    ("statement", "match"),
    [
        ("UPDATE verdicts SET rating = 'Strong Buy'", "is not valid"),
        ("UPDATE verdicts SET text_digest = 'xyz'", "is not valid"),
        ("UPDATE verdicts SET sidecar_path = 'x.json', sidecar_digest = 'nope'", "is not valid"),
    ],
)
def test_a_stored_verdict_that_no_longer_reads_is_a_store_error(tmp_path, statement, match):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_verdict(record())
    connection = sqlite3.connect(path)
    connection.execute(statement)
    connection.commit()
    connection.close()
    with open_store(path) as store:
        with pytest.raises(StoreError, match=match):
            store.verdict(SOURCE, "WETH", FIRST_DAY)
        with pytest.raises(StoreError, match=match):
            store.verdicts_at(SOURCE, FIRST_DAY)


def test_a_store_from_before_verdicts_gains_the_table_and_keeps_its_rows(tmp_path):
    path = tmp_path / "store.db"
    connection = _store_at(path, 5)
    connection.execute(
        "INSERT INTO runs (run_id, mode, chain_id, quote, strategy, config, balances, gas_eth, "
        "created_at) VALUES ('run-1', 'backtest', 1, 'USDC', 'fixed_weights', '{}', "
        """'{"USDC": "10000", "WBTC": "0", "WETH": "0"}', '1', 0)"""
    )
    connection.execute(
        "INSERT INTO decisions (run_id, time, outcome, close_block) VALUES ('run-1', ?, 'hold', 9)",
        (FIRST_DAY,),
    )
    connection.close()
    with open_store(path) as store:
        # A decision stored before the column existed saw no verdicts.
        assert store.decision("run-1", FIRST_DAY) == Decision(
            time=FIRST_DAY, outcome=Outcome.HOLD, close_block=9
        )
        assert store.verdicts_at(SOURCE, FIRST_DAY) == []
        store.insert_verdict(record())
        assert store.verdict(SOURCE, "WETH", FIRST_DAY) == record()
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone() == (
        SCHEMA_VERSION,
    )
    connection.close()


# --- what a decision saw -----------------------------------------------------


def test_a_decision_keeps_the_digests_of_the_verdicts_it_saw(store):
    store.insert_run(_run())
    saw = {"WETH": verdict("WETH", 0).digest, "WBTC": verdict("WBTC", 0).digest}
    store.record("run-1", _held(verdicts=saw))
    store.record("run-1", _held(_day(1), verdicts={}))
    store.record("run-1", _held(_day(2)))
    decisions = store.decisions("run-1")
    assert [decision.verdicts for decision in decisions] == [saw, {}, None]
    assert store.decision("run-1", _day(1)).verdicts == {}
    connection = store._connection
    assert connection.execute(
        "SELECT verdict_digests FROM decisions WHERE run_id = 'run-1' ORDER BY time"
    ).fetchall() == [
        (f'{{"WBTC": "{saw["WBTC"]}", "WETH": "{saw["WETH"]}"}}',),
        ("{}",),
        (None,),
    ]


@pytest.mark.parametrize("text", ["[1]", "null", "not json", '{"WETH": "xyz"}'])
def test_stored_digests_that_no_longer_read_are_a_store_error(tmp_path, text):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_run(_run())
        store.record("run-1", _held(verdicts={"WETH": _DIGEST}))
    connection = sqlite3.connect(path)
    connection.execute("UPDATE decisions SET verdict_digests = ?", (text,))
    connection.commit()
    connection.close()
    with open_store(path) as store, pytest.raises(StoreError, match="is not valid"):
        store.decision("run-1", FIRST_DAY)


# --- what a config reads -----------------------------------------------------


def test_a_config_without_a_verdicts_section_reads_none(store):
    store.insert_verdict(record())
    assert load_verdicts(store, _plain_config(), FIRST_DAY) == {}


def test_a_config_reads_its_sources_verdicts_on_the_tokens_it_trades(store):
    for said in (
        record("WETH", 0),
        record("WBTC", 0, Rating.UNDERWEIGHT),
        # The quote token, a token the config does not trade, another source, another bar.
        record("USDC", 0),
        record("LINK", 0),
        record("WETH", 0, Rating.SELL, source="another-judge"),
        record("WETH", 1, Rating.SELL),
    ):
        store.insert_verdict(said)
    said = load_verdicts(store, _CONFIG, FIRST_DAY)
    assert said == {"WETH": verdict("WETH", 0), "WBTC": verdict("WBTC", 0, Rating.UNDERWEIGHT)}
    with pytest.raises(TypeError):
        said["LINK"] = verdict("LINK", 0)  # type: ignore[index]
    assert load_verdicts(store, _config("another-judge"), FIRST_DAY) == {
        "WETH": verdict("WETH", 0, Rating.SELL, source="another-judge")
    }
    # The source said nothing at this bar: a mapping, and an empty one.
    assert load_verdicts(store, _CONFIG, _day(2)) == {}


def test_a_stored_verdict_that_no_longer_reads_stops_the_load(tmp_path):
    path = tmp_path / "store.db"
    with open_store(path) as store:
        store.insert_verdict(record())
    connection = sqlite3.connect(path)
    connection.execute("UPDATE verdicts SET rating = 'Maybe'")
    connection.commit()
    connection.close()
    with open_store(path) as store, pytest.raises(StoreError, match="is not valid"):
        load_verdicts(store, _CONFIG, FIRST_DAY)


def test_the_fakes_config_reads_the_test_source_over_the_engine_fakes():
    assert _CONFIG.verdicts is not None and _CONFIG.verdicts.source == SOURCE
    assert replace(_CONFIG, verdicts=None) == _plain_config()
