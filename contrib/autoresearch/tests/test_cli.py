"""What the two commands do with a store, a venue, and a mistyped argument."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from contrib.autoresearch import upstream
from contrib.autoresearch.cli import _parse_since, main
from contrib.autoresearch.store import DB_FILENAME, ResearchStore
from contrib.autoresearch.upstream import ExchangeError

from .conftest import bars, funding_points, market_at


@pytest.fixture
def seeded(tmp_path):
    """A store on disk holding a 4h series with one hole and a clean funding series."""
    path = tmp_path / DB_FILENAME
    with ResearchStore(path) as store:
        store.upsert_candles("BTC", "4h", bars(20, skip={9}))
        store.upsert_funding("BTC", funding_points(24))
    return path


def _serve(monkeypatch, market):
    """Make ``fetch`` reach the scripted venue instead of the SDK.

    Patched on ``upstream``, not on ``cli``: the command imports the builder
    inside itself precisely so the SDK stays unloaded, so the module it reads
    the name from at call time is the one that has to be patched. A test that
    patched ``cli`` would pass while the real lookup went elsewhere.
    """
    monkeypatch.setattr(upstream, "build_market_data", lambda **_kwargs: market)
    return market


def test_parse_since_reads_a_bare_date_as_midnight_utc():
    assert _parse_since("2023-01-01") == datetime(2023, 1, 1, tzinfo=timezone.utc)


def test_parse_since_keeps_an_explicit_offset():
    parsed = _parse_since("2023-01-01T06:00:00+02:00")
    assert parsed.utcoffset().total_seconds() == 7200


def test_parse_since_refuses_a_datetime_without_an_offset():
    """The one door left open if a naive datetime were read in the host zone."""
    with pytest.raises(ValueError) as caught:
        _parse_since("2023-01-01T00:00:00")
    assert "no UTC offset" in str(caught.value)


def test_parse_since_refuses_something_that_is_not_a_date_at_all():
    with pytest.raises(ValueError) as caught:
        _parse_since("last tuesday")
    assert "not a date or ISO-8601 instant" in str(caught.value)


def test_gaps_reports_the_holes_and_still_exits_zero(seeded, capsys):
    """A store with gaps is a successful scan, not a failed command."""
    assert main(["gaps", "--coin", "BTC", "--interval", "4h", "--db", str(seeded)]) == 0
    out = capsys.readouterr().out
    assert "BTC 4h candles" in out
    assert "1 gap(s)" in out
    assert "BTC funding" in out and "no gaps" in out


def test_gaps_touches_no_network(seeded, monkeypatch):
    """Reaching for the SDK builder at all would be a defect, so make it fatal."""

    def explode(**_kwargs):
        raise AssertionError("gaps must not build a venue reader")

    monkeypatch.setattr(upstream, "build_market_data", explode)
    assert main(["gaps", "--db", str(seeded)]) == 0


def test_a_store_that_is_not_ours_is_refused_with_exit_one(tmp_path, capsys):
    foreign = tmp_path / "paper_trading.db"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    assert main(["gaps", "--db", str(foreign)]) == 1
    assert "not an AutoResearch store" in capsys.readouterr().err


def test_fetch_lands_both_series_and_scans_them(tmp_path, monkeypatch, capsys):
    series = bars(30)
    points = funding_points(48)
    market = _serve(
        monkeypatch,
        market_at(series[-1].close_time, candles={("BTC", "4h"): series}, funding={"BTC": points}),
    )
    path = tmp_path / DB_FILENAME
    code = main(
        [
            "fetch",
            "--coin",
            "BTC",
            "--interval",
            "4h",
            "--since",
            "2023-01-01",
            "--db",
            str(path),
        ]
    )
    assert code == 0
    with ResearchStore(path) as store:
        assert store.count_candles("BTC", "4h") == 30
        assert store.count_funding("BTC") == 48
    out = capsys.readouterr().out
    assert "venue clock:" in out
    assert "30 new" in out
    assert "no gaps" in out
    assert market.candle_calls and market.funding_calls


def test_skip_funding_leaves_the_funding_endpoint_alone(tmp_path, monkeypatch):
    series = bars(10)
    market = _serve(
        monkeypatch,
        market_at(
            series[-1].close_time,
            candles={("BTC", "4h"): series},
            funding={"BTC": funding_points(10)},
        ),
    )
    path = tmp_path / DB_FILENAME
    assert (
        main(["fetch", "--since", "2023-01-01", "--db", str(path), "--skip-funding"]) == 0
    )
    assert market.funding_calls == []


def test_a_venue_failure_is_named_and_exits_one_without_leaving_a_store(
    tmp_path, monkeypatch, capsys
):
    class DownMarket:
        def get_exchange_time(self, coin):
            raise ExchangeError("l2Book request failed")

    _serve(monkeypatch, DownMarket())
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--since", "2023-01-01", "--db", str(path)]) == 1
    assert "l2Book request failed" in capsys.readouterr().err
    # The clock is read before the store is opened, so a mistyped --db does not
    # leave an empty store behind on a run that never fetched anything.
    assert not path.exists()


def test_a_since_the_venue_predates_still_lands_what_exists(tmp_path, monkeypatch, capsys):
    series = bars(12)
    market = _serve(
        monkeypatch,
        market_at(series[-1].close_time, candles={("BTC", "4h"): series}, funding={"BTC": []}),
    )
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--since", "2020-01-01", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "the venue served no older data" in out
    assert "BTC funding: no rows stored" in out
    assert market.clock is not None


def test_an_interval_outside_the_vocabulary_is_an_argparse_refusal(tmp_path):
    with pytest.raises(SystemExit) as caught:
        main(["gaps", "--interval", "3h", "--db", str(tmp_path / DB_FILENAME)])
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "spelling",
    ["2023-01-01T00:00:00", "2023-01-01t00:00:00", "2023-01-01 00:00:00"],
)
def test_every_spelling_of_a_naive_datetime_is_refused(spelling):
    """Not just the capital-T one.

    ``fromisoformat`` accepts a lowercase "t" and a space as separators too,
    so a refusal written as a list of separators to exclude has to be kept in
    step with the parser's list. This one is a positive match on the bare-date
    shape instead, and these are the spellings that used to get through it.
    """
    with pytest.raises(ValueError, match="no UTC offset"):
        _parse_since(spelling)


def test_a_malformed_since_exits_one_instead_of_a_traceback(tmp_path, capsys):
    """The CLI's ValueError lane, which only the store and venue lanes covered."""
    code = main(["fetch", "--since", "last tuesday", "--db", str(tmp_path / DB_FILENAME)])
    assert code == 1
    assert "not a date or ISO-8601 instant" in capsys.readouterr().err


def test_skip_funding_also_leaves_funding_out_of_the_scan(tmp_path, monkeypatch, capsys):
    """Asserting the OUTPUT, not only that the endpoint went untouched.

    A run that skipped the fetch but still scanned would print a funding line
    reading "no rows stored", which is the shape of a failed backfill rather
    than of a pass that was deliberately not run.
    """
    series = bars(10)
    _serve(
        monkeypatch,
        market_at(
            series[-1].close_time,
            candles={("BTC", "4h"): series},
            funding={"BTC": funding_points(10)},
        ),
    )
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--since", "2023-01-01", "--db", str(path), "--skip-funding"]) == 0
    out = capsys.readouterr().out
    assert "BTC 4h candles" in out
    assert "BTC funding" not in out


def test_there_is_no_network_switch_to_blend_two_venues_into_one_series(tmp_path):
    """A row is filed under (coin, interval, open_time) and names no venue.

    So a testnet bar and a mainnet bar for the same instant ARE the same row -
    one overwrites the other, and nothing afterwards can tell. A switch whose
    only reachable effect is that blend is worse than no switch.
    """
    with pytest.raises(SystemExit) as caught:
        main(["fetch", "--since", "2023-01-01", "--network", "testnet",
              "--db", str(tmp_path / DB_FILENAME)])
    assert caught.value.code == 2


@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "1h"])
def test_an_interval_too_short_for_the_venue_depth_limit_is_refused(tmp_path, interval):
    """Plan section 1 names 4h and 1d; the depth limit makes that a correctness rule.

    At the venue's ~5000-bar limit, 1h reaches about 208 days and 15m about
    52 - series that scan as having no holes and are far too short for the
    train/validation/holdout split to mean anything.
    """
    with pytest.raises(SystemExit) as caught:
        main(["gaps", "--interval", interval, "--db", str(tmp_path / DB_FILENAME)])
    assert caught.value.code == 2


@pytest.mark.parametrize("interval", ["4h", "1d"])
def test_the_two_intervals_the_plan_names_are_accepted(tmp_path, interval):
    assert main(["gaps", "--interval", interval, "--db", str(tmp_path / DB_FILENAME)]) == 0


def test_the_scan_output_says_where_the_backfill_reached(tmp_path, monkeypatch, capsys):
    """The scan says "no gaps"; the reach says whether that covers the ask."""
    series = bars(12)
    _serve(
        monkeypatch,
        market_at(series[-1].close_time, candles={("BTC", "4h"): series}, funding={"BTC": []}),
    )
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--since", "2020-01-01", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "no gaps" in out
    assert "reach: stopped because the venue served no older data" in out
    assert "venue clock" in out


def test_a_store_nothing_was_ever_fetched_into_says_so(tmp_path, capsys):
    """Never fetched and fetched-but-empty are different, and read differently."""
    assert main(["gaps", "--db", str(tmp_path / DB_FILENAME)]) == 0
    out = capsys.readouterr().out
    assert "no rows stored" in out
    assert "reach: no fetch has recorded one in this store" in out


def test_a_lower_case_coin_reaches_the_venue_in_its_own_spelling(tmp_path, monkeypatch):
    """The store canonicalising is not enough - the REQUEST has to be right too.

    The venue looks its coin up in a map keyed by the exact ticker, so
    `--coin btc` arrived as a bare KeyError wearing an exchange-failure
    message: a shift key reported as the venue being broken. Caught on a live
    run, not by the store-level test, which never went near the wire.
    """
    series = bars(8)
    market = _serve(
        monkeypatch,
        market_at(series[-1].close_time, candles={("BTC", "4h"): series}, funding={"BTC": []}),
    )
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--coin", " btc ", "--since", "2023-01-01", "--db", str(path)]) == 0
    assert [coin for coin, _i, _l, _e in market.candle_calls] == ["BTC"] * len(
        market.candle_calls
    )
    with ResearchStore(path) as store:
        assert store.count_candles("BTC", "4h") == 8


def test_a_lower_case_coin_reads_the_same_series_back(tmp_path, capsys):
    """And `gaps --coin btc` must not answer "no rows stored" about a filled store."""
    path = tmp_path / DB_FILENAME
    with ResearchStore(path) as store:
        store.upsert_candles("BTC", "4h", bars(6))
    assert main(["gaps", "--coin", "btc", "--db", str(path)]) == 0
    assert "BTC 4h candles: 6 rows" in capsys.readouterr().out


def test_a_stop_reason_this_build_does_not_know_does_not_break_the_scan(tmp_path, capsys):
    """`gaps` is the command reached for when a store looks wrong.

    So a store written by a build with an ending this one lacks must still be
    inspectable. Raising there would fail the diagnostic outright, on exactly
    the store someone most needs to read.
    """
    path = tmp_path / DB_FILENAME
    with ResearchStore(path) as store:
        store.upsert_candles("BTC", "4h", bars(4))
        store.record_series_state(
            coin="BTC",
            series="4h",
            venue_clock_ms=1_700_000_000_000,
            since_ms=1_699_000_000_000,
            earliest_ms=1_699_000_000_000,
            latest_ms=1_700_000_000_000,
            rows=4,
            stopped="SOMETHING_A_LATER_BUILD_ADDED",
        )
    assert main(["gaps", "--db", str(path)]) == 0
    assert "reach: stopped because SOMETHING_A_LATER_BUILD_ADDED" in capsys.readouterr().out


def test_a_fetch_whose_breadcrumb_write_failed_does_not_claim_it_never_ran(
    tmp_path, monkeypatch, capsys
):
    """Two lines that contradicted each other, one of them false.

    A first-ever fetch whose ``series_state`` write fails is contained by
    design - the rows are landed and durable - but the scan below it then
    printed "never fetched into this store" directly under a summary saying
    thirty rows had just been written.
    """
    series = bars(30)
    _serve(
        monkeypatch,
        market_at(series[-1].close_time, candles={("BTC", "4h"): series}, funding={"BTC": []}),
    )

    def explode(**_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ResearchStore, "record_series_state", explode)
    path = tmp_path / DB_FILENAME
    assert main(["fetch", "--since", "2023-01-01", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "30 row(s) written" in out
    assert "never fetched" not in out
    assert "reach: no fetch has recorded one in this store" in out
