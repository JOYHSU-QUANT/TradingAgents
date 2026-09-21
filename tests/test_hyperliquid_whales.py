"""Hyperliquid whale-positioning vendor: leaderboard ranking, the per-address
sweep (skips, budget, all-failed), the pure aggregation, the rolling cache with
its history-backed 24-hour change, report rendering, router integration and the
news-analyst wiring.

All network access is mocked and the parsers run against trimmed local fixtures
captured from the live endpoints on 2026-09-21, so these run offline.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from tradingagents.dataflows import hyperliquid_whales as hlw, interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorRateLimitError, VendorUnavailableError

from .conftest import fake_response

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

WHALES_LOGGER = "tradingagents.dataflows.hyperliquid_whales"

DATE = "2026-09-21"


def _fixture(name: str):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


LEADERBOARD = _fixture("hyperliquid_leaderboard.json")
STATE_LONG = _fixture("hyperliquid_state_long.json")
STATE_SHORT = _fixture("hyperliquid_state_short.json")
STATE_NO_BTC = _fixture("hyperliquid_state_no_btc.json")
STATE_FLAT = _fixture("hyperliquid_state_flat.json")

# The two fixture accounts that hold BTC, so the expectations below are read
# off the fixtures rather than retyped.
LONG_POSITION = STATE_LONG["assetPositions"][0]["position"]
SHORT_POSITION = STATE_SHORT["assetPositions"][0]["position"]
LONG_ADDR = "0x92ea19eceb7a8de0f50978a1583a5d8b018050e9"
SHORT_ADDR = "0x5b5d51203a0f9079f8aeb098a6523a13f298c060"

LONG_NOTIONAL = float(LONG_POSITION["positionValue"])
SHORT_NOTIONAL = float(SHORT_POSITION["positionValue"])


def _at(stamp: str) -> datetime:
    """Aware-UTC datetime for patching ``_utc_now`` in the cache/TTL tests."""
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in stamp else "%Y-%m-%d"
    return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)


def _freeze(monkeypatch, stamp: str):
    monkeypatch.setattr(hlw, "_utc_now", lambda: _at(stamp))


def _position(address="0x" + "1" * 40, coin="BTC", szi=1.0, notional=100.0, leverage=2.0):
    return hlw.WhalePosition(
        address=address,
        coin=coin,
        szi=szi,
        notional=notional,
        entry_px=50_000.0,
        leverage=leverage,
    )


def _record(address, coin="BTC", szi=1.0, notional=100.0, leverage=2.0, entry_px=50_000.0):
    return {
        "address": address,
        "coin": coin,
        "szi": szi,
        "notional": notional,
        "entry_px": entry_px,
        "leverage": leverage,
    }


def _totals(long_count, short_count, long_notional, short_notional):
    return {
        "long_count": long_count,
        "short_count": short_count,
        "long_notional": long_notional,
        "short_notional": short_notional,
    }


def _snapshot(
    fetched_at, positions=(), *, sampled=2, attempted=2, answered=2, malformed=0, stopped=""
):
    return {
        "fetched_at": fetched_at,
        "digest": "cohortdigest",
        "sampled": sampled,
        "attempted": attempted,
        "answered": answered,
        "malformed": malformed,
        "stopped": stopped,
        "positions": list(positions),
    }


def _history(fetched_at, coins, digest="cohortdigest", answered=2, sampled=None):
    return {
        "fetched_at": fetched_at,
        "digest": digest,
        "answered": answered,
        "sampled": answered if sampled is None else sampled,
        "coins": coins,
    }


def _payload(current, history=(), leaderboard_at="2026-09-21T00:00:00Z"):
    return {
        "schema": hlw.CACHE_SCHEMA,
        "leaderboard": {
            "fetched_at": leaderboard_at,
            "addresses": [{"address": LONG_ADDR, "account_value": 1.0}],
        },
        "current": current,
        "history": list(history),
    }


def _write_cache(tmp_path, payload):
    set_config({"data_cache_dir": str(tmp_path)})
    path = tmp_path / "hyperliquid_whales.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _serve(monkeypatch, states=None, leaderboard=LEADERBOARD):
    """Arm both network boundaries; returns the list of addresses requested."""
    asked = []
    states = states if states is not None else {LONG_ADDR: STATE_LONG, SHORT_ADDR: STATE_SHORT}

    def _state(address):
        asked.append(address)
        served = states.get(address, STATE_FLAT)
        if isinstance(served, Exception):
            raise served
        return served

    monkeypatch.setattr(hlw, "_request_leaderboard", lambda: leaderboard)
    monkeypatch.setattr(hlw, "_request_state", _state)
    monkeypatch.setattr(hlw, "_sleep", lambda s: None)
    return asked


# --------------------------------------------------------------------------- #
# Leaderboard parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseLeaderboard:
    def test_ranks_by_account_value_descending(self):
        rows = hlw._parse_leaderboard(LEADERBOARD)
        values = [r["account_value"] for r in rows]
        assert values == sorted(values, reverse=True)
        # The fixture is deliberately stored unsorted, as the live body is, so
        # this measures the ranking rather than the fixture's order.
        fixture_order = [float(r["accountValue"]) for r in LEADERBOARD["leaderboardRows"]]
        assert fixture_order != sorted(fixture_order, reverse=True)

    def test_keeps_only_the_top_n(self, monkeypatch):
        monkeypatch.setattr(hlw, "TOP_N", 3)
        rows = hlw._parse_leaderboard(LEADERBOARD)
        assert len(rows) == 3
        assert rows[0]["account_value"] == max(
            float(r["accountValue"]) for r in LEADERBOARD["leaderboardRows"]
        )

    def test_a_cohort_shorter_than_top_n_is_served_whole(self):
        # The fixture carries fewer rows than TOP_N, which is the shape every
        # cache test already runs through - but nothing pinned the count, so a
        # parser that silently dropped rows would not have shown up.
        assert len(hlw._parse_leaderboard(LEADERBOARD)) == len(LEADERBOARD["leaderboardRows"])
        assert len(LEADERBOARD["leaderboardRows"]) < hlw.TOP_N

    def test_addresses_are_lowercased(self):
        payload = {"leaderboardRows": [{"ethAddress": "0x" + "AB" * 20, "accountValue": "5"}]}
        assert hlw._parse_leaderboard(payload)[0]["address"] == "0x" + "ab" * 20

    def test_rows_with_an_unusable_address_are_skipped(self):
        # The endpoint is undocumented; a row whose address is not an EVM
        # address must be dropped rather than sent to the info endpoint or
        # rendered as if it were one.
        payload = {
            "leaderboardRows": [
                {"ethAddress": "not-an-address", "accountValue": "900"},
                {"ethAddress": "0x" + "1" * 40, "accountValue": "5"},
            ]
        }
        assert [r["address"] for r in hlw._parse_leaderboard(payload)] == ["0x" + "1" * 40]

    def test_rows_with_an_unusable_account_value_are_skipped(self):
        payload = {
            "leaderboardRows": [
                {"ethAddress": "0x" + "2" * 40, "accountValue": "n/a"},
                {"ethAddress": "0x" + "3" * 40, "accountValue": None},
                {"ethAddress": "0x" + "4" * 40, "accountValue": "NaN"},
                {"ethAddress": "0x" + "1" * 40, "accountValue": "5"},
            ]
        }
        assert [r["address"] for r in hlw._parse_leaderboard(payload)] == ["0x" + "1" * 40]

    def test_a_duplicated_address_occupies_one_slot(self):
        # Two rows for one account would otherwise take two of the N slots AND
        # have that account's positions counted twice in the aggregate.
        payload = {
            "leaderboardRows": [
                {"ethAddress": "0x" + "1" * 40, "accountValue": "5"},
                {"ethAddress": "0x" + "1" * 40, "accountValue": "50"},
                {"ethAddress": "0x" + "2" * 40, "accountValue": "9"},
            ]
        }
        rows = hlw._parse_leaderboard(payload)
        assert [(r["address"], r["account_value"]) for r in rows] == [
            ("0x" + "1" * 40, 50.0),
            ("0x" + "2" * 40, 9.0),
        ]

    def test_ties_break_on_the_address(self):
        # Deterministic sampling: two accounts of equal value must not let dict
        # ordering decide which one the cohort (and its digest) contains.
        payload = {
            "leaderboardRows": [
                {"ethAddress": "0x" + "b" * 40, "accountValue": "5"},
                {"ethAddress": "0x" + "a" * 40, "accountValue": "5"},
            ]
        }
        assert [r["address"] for r in hlw._parse_leaderboard(payload)] == [
            "0x" + "a" * 40,
            "0x" + "b" * 40,
        ]

    def test_missing_rows_list_raises(self):
        with pytest.raises(hlw.HyperliquidWhalesError):
            hlw._parse_leaderboard({"somethingElse": []})

    def test_no_usable_row_raises_rather_than_returning_empty(self):
        # An emptied leaderboard must not read as "no whales hold anything":
        # that is a contract break, and the category degrades to the sentinel.
        payload = {"leaderboardRows": [{"ethAddress": "nope", "accountValue": "1"}]}
        with pytest.raises(hlw.HyperliquidWhalesError):
            hlw._parse_leaderboard(payload)


# --------------------------------------------------------------------------- #
# Account-state parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseState:
    def test_reads_the_fixture_long_position(self):
        records, malformed = hlw._parse_state(LONG_ADDR, STATE_LONG)
        assert malformed == 0
        assert len(records) == 1
        assert records[0]["coin"] == "BTC"
        assert records[0]["szi"] > 0
        assert records[0]["notional"] == LONG_NOTIONAL
        assert records[0]["leverage"] == float(LONG_POSITION["leverage"]["value"])

    def test_reads_every_coin_not_just_one(self):
        # The history aggregate answers for whichever coin the NEXT call names,
        # so the sweep keeps all of an account's coins.
        records, _ = hlw._parse_state(SHORT_ADDR, STATE_SHORT)
        assert {r["coin"] for r in records} == {"BTC", "ETH", "SOL"}

    def test_an_account_with_no_position_in_the_coin_still_parses(self):
        records, malformed = hlw._parse_state(SHORT_ADDR, STATE_NO_BTC)
        assert malformed == 0
        assert hlw._positions_from_records(records, "BTC") == []

    def test_a_flat_account_yields_nothing_and_no_complaint(self):
        assert hlw._parse_state(LONG_ADDR, STATE_FLAT) == ([], 0)

    def test_a_zero_size_entry_is_not_a_position(self):
        state = {
            "assetPositions": [{"position": {"coin": "BTC", "szi": "0.0", "positionValue": "0.0"}}]
        }
        assert hlw._parse_state(LONG_ADDR, state) == ([], 0)

    def test_an_unreadable_entry_is_counted_not_silently_dropped(self):
        # An aggregate quietly short of a leg is worse than one that says a leg
        # was dropped, which is what the coverage line reports.
        state = {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "1", "positionValue": "oops"}},
                {"position": {"coin": "BTC", "szi": "1", "positionValue": "5"}},
            ]
        }
        records, malformed = hlw._parse_state(LONG_ADDR, state)
        assert (len(records), malformed) == (1, 1)

    @pytest.mark.parametrize(
        "entry",
        [
            {"position": "not-a-dict"},
            {"noPosition": {}},
            {"position": {"coin": "BTC|forged", "szi": "1", "positionValue": "5"}},
            {"position": {"coin": "BTC", "szi": "nope", "positionValue": "5"}},
            {"position": {"coin": "BTC", "szi": "1", "positionValue": "-5"}},
            {"position": {"coin": "BTC", "szi": True, "positionValue": "5"}},
        ],
        ids=[
            "position_not_dict",
            "no_position",
            "forged_coin",
            "bad_size",
            "negative_notional",
            "bool_size",
        ],
    )
    def test_unreadable_shapes_are_counted(self, entry):
        assert hlw._parse_state(LONG_ADDR, {"assetPositions": [entry]}) == ([], 1)

    def test_a_missing_positions_list_is_a_contract_break_not_an_empty_book(self):
        # "no assetPositions key" is the contract changing; reporting it as a
        # flat account would put a fabricated zero into the aggregate.
        assert hlw._parse_state(LONG_ADDR, {"marginSummary": {}}) == ([], 1)

    def test_optional_fields_may_be_missing_without_losing_the_position(self):
        state = {
            "assetPositions": [{"position": {"coin": "BTC", "szi": "1", "positionValue": "5"}}]
        }
        records, malformed = hlw._parse_state(LONG_ADDR, state)
        assert malformed == 0
        assert records[0]["entry_px"] is None and records[0]["leverage"] is None


# --------------------------------------------------------------------------- #
# Aggregation (pure)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestAggregate:
    def test_mixed_book_splits_by_sign_and_weighs_by_notional(self):
        # The venue reports positionValue unsigned, so a short's notional must
        # add to the short side at full size rather than subtract from the long.
        positions = [
            _position(address="0x" + "1" * 40, szi=2.0, notional=300.0, leverage=4.0),
            _position(address="0x" + "2" * 40, szi=-1.0, notional=100.0, leverage=2.0),
            _position(address="0x" + "3" * 40, szi=-1.0, notional=100.0, leverage=6.0),
        ]
        agg = hlw.aggregate_positions(positions, "BTC")
        assert (agg.long_count, agg.short_count, agg.holders) == (1, 2, 3)
        assert (agg.long_notional, agg.short_notional) == (300.0, 200.0)
        assert agg.ratio == 1.5
        # 300*4 + 100*2 + 100*6 = 2000 over 500 of notional.
        assert agg.avg_leverage == 4.0
        assert agg.levered_notional == 500.0

    def test_no_short_side_reports_words_not_infinity(self):
        agg = hlw.aggregate_positions([_position(szi=1.0, notional=10.0)], "BTC")
        assert agg.ratio is None
        assert hlw._fmt_ratio(agg.ratio) == "n/a (no short notional)"

    def test_empty_sample_is_zeros_and_no_leverage(self):
        agg = hlw.aggregate_positions([], "BTC")
        assert (agg.holders, agg.long_notional, agg.short_notional) == (0, 0.0, 0.0)
        assert agg.avg_leverage is None and agg.ratio is None and agg.top == ()

    def test_leverage_average_covers_only_the_notional_that_reported_one(self):
        positions = [
            _position(address="0x" + "1" * 40, notional=100.0, leverage=10.0),
            _position(address="0x" + "2" * 40, notional=900.0, leverage=None),
        ]
        agg = hlw.aggregate_positions(positions, "BTC")
        assert agg.avg_leverage == 10.0
        # The weight behind the average, not the whole book: without it a
        # one-position average would read as the book's leverage.
        assert agg.levered_notional == 100.0
        assert agg.total_notional == 1000.0

    def test_no_reported_leverage_is_none_rather_than_zero(self):
        agg = hlw.aggregate_positions([_position(leverage=None)], "BTC")
        assert agg.avg_leverage is None

    def test_top_positions_are_the_largest_by_notional(self, monkeypatch):
        monkeypatch.setattr(hlw, "TOP_POSITIONS", 2)
        positions = [
            _position(address="0x" + "1" * 40, notional=10.0),
            _position(address="0x" + "2" * 40, notional=300.0),
            _position(address="0x" + "3" * 40, notional=200.0),
        ]
        agg = hlw.aggregate_positions(positions, "BTC")
        assert [p.notional for p in agg.top] == [300.0, 200.0]

    def test_equal_notionals_break_on_the_address(self):
        positions = [
            _position(address="0x" + "b" * 40, notional=10.0),
            _position(address="0x" + "a" * 40, notional=10.0),
        ]
        assert [p.address for p in hlw.aggregate_positions(positions, "BTC").top] == [
            "0x" + "a" * 40,
            "0x" + "b" * 40,
        ]

    def test_only_the_requested_coin_is_aggregated(self):
        records = [_record(LONG_ADDR, coin="BTC"), _record(SHORT_ADDR, coin="ETH", szi=-1.0)]
        assert len(hlw._positions_from_records(records, "BTC")) == 1
        assert len(hlw._positions_from_records(records, "ETH")) == 1

    def test_the_coin_match_is_case_insensitive(self):
        # The docstring promises it: the coins this module is ASKED about are
        # upper-case bases, while the venue's own spelling is its business.
        records = [_record(LONG_ADDR, coin="btc"), _record(SHORT_ADDR, coin="Btc", szi=-1.0)]
        assert len(hlw._positions_from_records(records, "BTC")) == 2

    def test_coin_totals_are_keyed_case_insensitively(self):
        totals = hlw._coin_totals([_record(LONG_ADDR, coin="btc", notional=10.0)])
        assert totals["BTC"]["long_notional"] == 10.0

    def test_coin_totals_cover_every_coin_in_the_snapshot(self):
        records = [
            _record(LONG_ADDR, coin="BTC", szi=1.0, notional=10.0),
            _record(SHORT_ADDR, coin="BTC", szi=-1.0, notional=30.0),
            _record(SHORT_ADDR, coin="ETH", szi=-1.0, notional=50.0),
        ]
        totals = hlw._coin_totals(records)
        assert totals["BTC"] == _totals(1, 1, 10.0, 30.0)
        assert totals["ETH"]["short_notional"] == 50.0


# --------------------------------------------------------------------------- #
# The per-address sweep
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestSweep:
    def _addresses(self, n):
        return [{"address": f"0x{i:040x}", "account_value": float(n - i)} for i in range(n)]

    def test_one_failing_address_costs_that_account_only(self, monkeypatch, caplog):
        addresses = self._addresses(3)
        served = {addresses[1]["address"]: hlw.HyperliquidWhalesUnavailableError("down")}

        def _state(address):
            outcome = served.get(address)
            if isinstance(outcome, Exception):
                raise outcome
            return STATE_LONG

        monkeypatch.setattr(hlw, "_request_state", _state)
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            snapshot = hlw._fetch_positions(addresses)
        assert (snapshot["attempted"], snapshot["answered"], snapshot["sampled"]) == (3, 2, 3)
        assert len(snapshot["positions"]) == 2
        assert "skipping that account" in caplog.text

    def test_the_budget_stops_the_sweep_and_leaves_the_rest_unattempted(self, monkeypatch):
        # A network that hangs instead of failing fast would otherwise cost
        # TOP_N sequential timeouts inside one tool call.
        addresses = self._addresses(5)
        clock = iter([0.0, hlw.POSITION_FETCH_BUDGET_S + 1])
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
        monkeypatch.setattr(hlw, "_request_state", lambda a: STATE_LONG)
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        snapshot = hlw._fetch_positions(addresses)
        assert (snapshot["attempted"], snapshot["answered"]) == (1, 1)
        assert "4 were not attempted" in hlw._coverage_line(snapshot)

    def test_the_first_address_is_always_attempted(self, monkeypatch):
        # The budget check is skipped for index 0 on purpose: a budget already
        # spent must still buy one request, not an empty sweep reported as a
        # vendor failure. A zero budget is what discriminates — under a
        # positive one the deadline is computed from the same clock, so no
        # clock value alone can be "already past".
        ticks = iter([0.0, 1.0, 2.0])
        monkeypatch.setattr(hlw, "POSITION_FETCH_BUDGET_S", 0.0)
        monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
        monkeypatch.setattr(hlw, "_request_state", lambda a: STATE_LONG)
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        assert hlw._fetch_positions(self._addresses(2))["attempted"] == 1

    def test_the_throttle_spaces_the_requests(self, monkeypatch):
        slept = []
        monkeypatch.setattr(hlw, "_sleep", slept.append)
        monkeypatch.setattr(hlw, "_request_state", lambda a: STATE_FLAT)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        hlw._fetch_positions(self._addresses(3))
        # Between requests, not before the first: three addresses, two gaps.
        assert slept == [hlw.MIN_REQUEST_INTERVAL_S] * 2

    def test_a_purely_transport_wipeout_raises_the_outage_type(self, monkeypatch):
        # The router logs an outage without a traceback and counts the vendor
        # as down; reporting it as breakage would read as "we need a fix".
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._fetch_positions(self._addresses(2))

    def test_a_structural_failure_in_the_mix_stays_structural(self, monkeypatch):
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        errors = [
            hlw.HyperliquidWhalesUnavailableError("down"),
            hlw.HyperliquidWhalesError("contract change"),
        ]
        monkeypatch.setattr(hlw, "_request_state", mock.Mock(side_effect=errors))
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._fetch_positions(self._addresses(2))
        assert not isinstance(excinfo.value, VendorUnavailableError)

    def test_the_all_failed_message_counts_both_flavours(self, monkeypatch):
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._fetch_positions(self._addresses(2))
        assert "2 transport failures" in str(excinfo.value)
        assert "0 contract failures" in str(excinfo.value)

    def test_an_answering_but_flat_sweep_is_not_a_failure(self, monkeypatch):
        # Eleven of the twenty largest accounts held no perp position at all
        # when this was measured, so "everyone answered, nobody holds" is the
        # ordinary case, not an error.
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        monkeypatch.setattr(hlw, "_request_state", lambda a: STATE_FLAT)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        snapshot = hlw._fetch_positions(self._addresses(3))
        assert snapshot["answered"] == 3 and snapshot["positions"] == []

    def test_a_throttle_drains_the_sweep_instead_of_buying_it_again(self, monkeypatch, caplog):
        # The info endpoint's budget is per-IP, so a 429 is a fact about this
        # client, not about one address: the remaining requests would each
        # spend a call learning the same refusal.
        addresses = self._addresses(5)
        calls = []

        def _state(address):
            calls.append(address)
            if len(calls) == 2:
                raise hlw.HyperliquidWhalesRateLimitError("Hyperliquid info answered HTTP 429")
            return STATE_LONG

        monkeypatch.setattr(hlw, "_request_state", _state)
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            snapshot = hlw._fetch_positions(addresses)
        assert len(calls) == 2
        assert (snapshot["attempted"], snapshot["answered"]) == (2, 1)
        assert snapshot["stopped"] == "rate_limit"
        assert "rate limited at" in caplog.text

    def test_the_coverage_line_blames_the_throttle_not_the_budget(self):
        # The budget and a throttle leave an identical count of unattempted
        # accounts behind; naming the wrong cause sends the reader to look at
        # the wrong thing.
        rate_limited = hlw._coverage_line(
            _snapshot(
                "2026-09-21T00:00:00Z",
                sampled=20,
                attempted=2,
                answered=1,
                stopped="rate_limit",
            )
        )
        assert "rate limited this client" in rate_limited
        assert "fetch budget" not in rate_limited
        budget = hlw._coverage_line(
            _snapshot(
                "2026-09-21T00:00:00Z", sampled=20, attempted=2, answered=1, stopped="budget"
            )
        )
        assert "fetch budget was spent" in budget
        assert "rate limited" not in budget

    def test_the_coverage_line_discloses_unparsed_position_entries(self):
        # An aggregate quietly short of a leg is the thing ``malformed`` exists
        # to disclose; the sentence had no coverage from any direction.
        line = hlw._coverage_line(
            _snapshot("2026-09-21T00:00:00Z", sampled=3, attempted=3, answered=3, malformed=2)
        )
        assert "2 position entries could not be parsed and are excluded" in line
        clean = hlw._coverage_line(
            _snapshot("2026-09-21T00:00:00Z", sampled=3, attempted=3, answered=3)
        )
        assert "could not be parsed" not in clean

    def test_a_malformed_entry_reaches_the_snapshot_count(self, monkeypatch):
        # End to end, not just the sentence: a bad entry has to survive the
        # sweep as a count rather than being dropped on the floor.
        state = {
            "assetPositions": [
                {"position": {"coin": "BTC", "szi": "1", "positionValue": "oops"}},
                {"position": {"coin": "BTC", "szi": "1", "positionValue": "5"}},
            ]
        }
        monkeypatch.setattr(hlw, "_request_state", lambda a: state)
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        snapshot = hlw._fetch_positions(self._addresses(2))
        assert snapshot["malformed"] == 2
        assert "2 position entries could not be parsed" in hlw._coverage_line(snapshot)

    def test_a_throttle_that_drained_everything_raises_the_rate_limit_type(self, monkeypatch):
        # Not the structural type: the router stands the vendor off on this
        # one, where a bare module error would be logged as a traceback and
        # read as "the client needs a fix" for a routine refusal.
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesRateLimitError("HTTP 429")),
        )
        with pytest.raises(hlw.HyperliquidWhalesRateLimitError) as excinfo:
            hlw._fetch_positions(self._addresses(3))
        assert isinstance(excinfo.value, VendorRateLimitError)
        # Drained on the first refusal rather than spending the other two.
        assert "after 1 of 3 addresses" in str(excinfo.value)

    def test_a_complete_sweep_records_no_stop_reason(self, monkeypatch):
        monkeypatch.setattr(hlw, "_sleep", lambda s: None)
        monkeypatch.setattr(hlw, "_request_state", lambda a: STATE_LONG)
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        assert hlw._fetch_positions(self._addresses(3))["stopped"] == ""

    def test_the_digest_is_order_independent(self):
        a, b = "0x" + "1" * 40, "0x" + "2" * 40
        assert hlw._cohort_digest([a, b]) == hlw._cohort_digest([b, a])
        assert hlw._cohort_digest([a]) != hlw._cohort_digest([a, b])


# --------------------------------------------------------------------------- #
# The network boundaries
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestLeaderboardBoundary:
    def _response(self, chunks, status=200):
        response = mock.Mock(spec=["status_code", "iter_content", "raise_for_status", "close"])
        response.status_code = status
        response.iter_content.return_value = iter(chunks)
        response.raise_for_status.side_effect = (
            requests.HTTPError(f"HTTP {status}", response=response) if status >= 400 else None
        )
        return response

    def test_a_body_over_the_cap_is_refused_rather_than_read(self, monkeypatch):
        # The endpoint is undocumented, so its size is not a contract; reading
        # it unbounded is how a changed endpoint becomes an OOM.
        monkeypatch.setattr(hlw, "MAX_LEADERBOARD_BYTES", 10)
        response = self._response([b"x" * 6, b"y" * 6, b"z" * 6])
        monkeypatch.setattr(requests, "get", lambda *a, **k: response)
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._request_leaderboard()
        assert "cap" in str(excinfo.value)
        response.close.assert_called_once()

    def test_a_5xx_is_the_outage_type(self, monkeypatch):
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._response([b"{}"], status=503))
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._request_leaderboard()

    def test_an_unreachable_host_is_the_outage_type(self, monkeypatch):
        def _boom(*a, **k):
            raise requests.ConnectionError("no route")

        monkeypatch.setattr(requests, "get", _boom)
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._request_leaderboard()

    def test_a_non_json_body_is_the_outage_type(self, monkeypatch):
        # A 2xx body that does not decode is a CDN or WAF page: the vendor
        # answered without data, which is the family's outage verdict.
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._response([b"<html>"]))
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._request_leaderboard()

    def test_a_body_of_the_wrong_shape_stays_structural(self, monkeypatch):
        # It DECODED, so the vendor is up and its contract changed — which no
        # retry heals and the router must not log as a traceback-free outage.
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._response([b"[1,2]"]))
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._request_leaderboard()
        assert not isinstance(excinfo.value, VendorUnavailableError)

    def test_a_429_is_the_rate_limit_type_not_a_contract_break(self, monkeypatch):
        # The shared status helper types only a 5xx and hands the rest to
        # requests, whose HTTPError ``is_unreached`` excludes - so without an
        # explicit check a throttle is filed as this module's STRUCTURAL type
        # and gets the ending meant for a broken parser: an ERROR with a
        # traceback, and a router verdict of "the client needs a fix".
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._response([b"{}"], status=429))
        with pytest.raises(hlw.HyperliquidWhalesRateLimitError) as excinfo:
            hlw._request_leaderboard()
        # The router dispatches on the SHARED type: this is what arms the
        # per-vendor throttle latch instead of logging a traceback.
        assert isinstance(excinfo.value, VendorRateLimitError)
        assert isinstance(excinfo.value, hlw.HyperliquidWhalesError)
        assert not isinstance(excinfo.value, VendorUnavailableError)

    def test_the_rate_limit_message_carries_the_status_not_the_body(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._response([b"slow down"], status=429)
        )
        with pytest.raises(hlw.HyperliquidWhalesRateLimitError) as excinfo:
            hlw._request_leaderboard()
        assert "429" in str(excinfo.value)
        assert "slow down" not in str(excinfo.value)

    def test_a_5xx_never_escapes_as_the_bare_shared_type(self, monkeypatch):
        # ``raise_for_http_status`` raises a bare ``VendorUnavailableError``.
        # Uncaught it would walk past the cache lane's
        # ``except HyperliquidWhalesError`` and reach the router without the
        # stale-snapshot fallback ever being tried.
        monkeypatch.setattr(requests, "get", lambda *a, **k: self._response([b"{}"], status=503))
        with pytest.raises(hlw.HyperliquidWhalesError):
            hlw._request_leaderboard()

    def test_the_body_is_parsed_when_it_is_within_the_cap(self, monkeypatch):
        monkeypatch.setattr(
            requests, "get", lambda *a, **k: self._response([b'{"leaderboardRows"', b": []}"])
        )
        assert hlw._request_leaderboard() == {"leaderboardRows": []}

    def test_the_failure_message_never_quotes_the_requests_message(self, monkeypatch):
        # A requests message carries the request URL (#203), and this string is
        # LLM-visible through the router's sentinel.
        def _boom(*a, **k):
            raise requests.ConnectionError("connect to https://secret.example/leak failed")

        monkeypatch.setattr(requests, "get", _boom)
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._request_leaderboard()
        assert "secret.example" not in str(excinfo.value)


@pytest.mark.unit
class TestStateBoundary:
    def test_posts_the_documented_body(self, monkeypatch):
        captured = {}

        def _post(url, json=None, timeout=None):
            captured.update(url=url, body=json, timeout=timeout)
            return fake_response(json={"assetPositions": []})

        monkeypatch.setattr(requests, "post", _post)
        hlw._request_state(LONG_ADDR)
        assert captured["url"] == hlw.INFO_URL
        assert captured["body"] == {"type": "clearinghouseState", "user": LONG_ADDR}
        assert captured["timeout"] == hlw.POSITION_TIMEOUT

    def test_a_non_object_body_is_structural(self, monkeypatch):
        # Decoded but the wrong shape: the endpoint changed, which is not an
        # outage a retry heals.
        monkeypatch.setattr(
            requests, "post", lambda *a, **k: fake_response(json=["not", "an", "obj"])
        )
        with pytest.raises(hlw.HyperliquidWhalesError) as excinfo:
            hlw._request_state(LONG_ADDR)
        assert not isinstance(excinfo.value, VendorUnavailableError)

    def test_an_unreachable_host_is_the_outage_type(self, monkeypatch):
        def _boom(*a, **k):
            raise requests.ConnectionError("no route")

        monkeypatch.setattr(requests, "post", _boom)
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._request_state(LONG_ADDR)

    def test_a_429_is_the_rate_limit_type(self, monkeypatch):
        response = fake_response(429)
        monkeypatch.setattr(requests, "post", lambda *a, **k: response)
        with pytest.raises(hlw.HyperliquidWhalesRateLimitError) as excinfo:
            hlw._request_state(LONG_ADDR)
        assert isinstance(excinfo.value, VendorRateLimitError)
        # Judged before the body is read at all.
        response.json.assert_not_called()


# --------------------------------------------------------------------------- #
# Cache, TTLs and the history behind the 24-hour change
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCache:
    def test_a_call_within_the_ttl_asks_nothing(self, tmp_path, monkeypatch):
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T00:30:00Z")
        asked = _serve(monkeypatch)
        snapshot = hlw._load_snapshot()
        assert asked == []
        assert snapshot.stale is False
        assert snapshot.current["fetched_at"] == "2026-09-21T00:00:00Z"

    def test_a_call_past_the_ttl_refreshes(self, tmp_path, monkeypatch):
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T02:00:00Z")
        asked = _serve(monkeypatch)
        snapshot = hlw._load_snapshot()
        assert asked
        assert snapshot.current["fetched_at"] == "2026-09-21T02:00:00Z"

    def test_a_future_dated_snapshot_is_not_perpetually_fresh(self, tmp_path, monkeypatch):
        _write_cache(tmp_path, _payload(_snapshot("2026-09-22T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        asked = _serve(monkeypatch)
        hlw._load_snapshot()
        assert asked

    def test_the_leaderboard_carries_its_own_longer_ttl(self, tmp_path, monkeypatch):
        # Re-downloading tens of megabytes on every hourly refresh buys nothing:
        # the top-by-account-value cohort turns over slowly.
        _write_cache(
            tmp_path,
            _payload(
                _snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]),
                leaderboard_at="2026-09-21T00:00:00Z",
            ),
        )
        _freeze(monkeypatch, "2026-09-21T02:00:00Z")
        _serve(monkeypatch)
        downloaded = []

        def _download():
            downloaded.append(1)
            return LEADERBOARD

        monkeypatch.setattr(hlw, "_request_leaderboard", _download)
        snapshot = hlw._load_snapshot()
        assert downloaded == []
        assert snapshot.leaderboard["fetched_at"] == "2026-09-21T00:00:00Z"

    def test_a_leaderboard_failure_reuses_the_cached_cohort(self, tmp_path, monkeypatch, caplog):
        # An out-of-date cohort still reads real positions, and the report
        # dates it; no cohort at all reads nothing.
        _write_cache(
            tmp_path,
            _payload(
                _snapshot("2026-09-20T00:00:00Z", [_record(LONG_ADDR)]),
                leaderboard_at="2026-09-20T00:00:00Z",
            ),
        )
        _freeze(monkeypatch, "2026-09-21T12:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_leaderboard",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("stats down")),
        )
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            snapshot = hlw._load_snapshot()
        assert snapshot.stale is False  # the positions themselves were refreshed
        assert snapshot.leaderboard["fetched_at"] == "2026-09-20T00:00:00Z"
        assert "reusing the cohort" in caplog.text

    def test_a_cohort_past_its_stale_cap_is_not_reused(self, tmp_path, monkeypatch):
        _write_cache(
            tmp_path,
            _payload(
                _snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]),
                leaderboard_at="2026-09-01T00:00:00Z",
            ),
        )
        _freeze(monkeypatch, "2026-09-21T02:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_leaderboard",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("stats down")),
        )
        # Falls through to the snapshot lane, which serves the cached snapshot
        # stale rather than inventing a cohort.
        assert hlw._load_snapshot().stale is True

    def test_a_refresh_failure_serves_the_snapshot_stale(self, tmp_path, monkeypatch, caplog):
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T03:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            snapshot = hlw._load_snapshot()
        assert snapshot.stale is True
        assert "serving the snapshot from" in caplog.text

    def test_a_snapshot_past_the_stale_cap_degrades_instead(self, tmp_path, monkeypatch):
        # Positioning half a day old presented under a "live snapshot" heading
        # is a false claim, not a lagging one.
        _write_cache(tmp_path, _payload(_snapshot("2026-09-20T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError) as excinfo:
            hlw._load_snapshot()
        assert f"{hlw.MAX_STALE_HOURS}-hour cap" in str(excinfo.value)

    def test_a_structural_refresh_failure_logs_an_error_not_a_warning(
        self, tmp_path, monkeypatch, caplog
    ):
        # A contract break is a code fix, not a brownout, and must not hide
        # among network-blip warnings for the whole stale window.
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T03:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw, "_request_state", mock.Mock(side_effect=hlw.HyperliquidWhalesError("shape"))
        )
        with caplog.at_level(logging.DEBUG, logger=WHALES_LOGGER):
            hlw._load_snapshot()
        assert [r.levelname for r in caplog.records if "structurally" in r.message] == ["ERROR"]

    def test_a_throttled_refresh_is_a_warning_not_an_escalation(
        self, tmp_path, monkeypatch, caplog
    ):
        # A throttle is the vendor answering, not a bug: it must not be logged
        # as "the parser or the endpoint likely changed" with a traceback.
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T03:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesRateLimitError("HTTP 429")),
        )
        with caplog.at_level(logging.DEBUG, logger=WHALES_LOGGER):
            assert hlw._load_snapshot().stale is True
        assert not [r for r in caplog.records if r.levelname == "ERROR"]
        assert "serving the snapshot from" in caplog.text

    def test_the_rate_limit_type_survives_the_no_cache_raise(self, tmp_path, monkeypatch):
        # The router classifies by type, so the wrap that adds context must not
        # re-file a throttle as breakage on its way out.
        set_config({"data_cache_dir": str(tmp_path)})
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_leaderboard",
            mock.Mock(side_effect=hlw.HyperliquidWhalesRateLimitError("HTTP 429")),
        )
        with pytest.raises(hlw.HyperliquidWhalesRateLimitError):
            hlw._load_snapshot()

    def test_a_failed_refresh_is_never_written(self, tmp_path, monkeypatch):
        path = _write_cache(
            tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]))
        )
        before = path.read_text(encoding="utf-8")
        _freeze(monkeypatch, "2026-09-21T03:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        hlw._load_snapshot()
        assert path.read_text(encoding="utf-8") == before

    def test_the_outage_type_survives_the_no_cache_raise(self, tmp_path, monkeypatch):
        set_config({"data_cache_dir": str(tmp_path)})
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_leaderboard",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        with pytest.raises(hlw.HyperliquidWhalesUnavailableError):
            hlw._load_snapshot()

    def test_a_cache_write_failure_does_not_fail_the_call(self, tmp_path, monkeypatch, caplog):
        set_config({"data_cache_dir": str(tmp_path)})
        _freeze(monkeypatch, "2026-09-21T00:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(hlw, "_cache_path", lambda: str(tmp_path / "nope" / "x.json"))
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            assert hlw._load_snapshot().current["answered"]
        assert "Could not write" in caplog.text

    def test_the_refresh_appends_to_the_history(self, tmp_path, monkeypatch):
        _write_cache(
            tmp_path,
            _payload(
                _snapshot("2026-09-20T02:00:00Z", [_record(LONG_ADDR)]),
                history=[_history("2026-09-20T02:00:00Z", {"BTC": _totals(1, 0, 10.0, 0.0)})],
            ),
        )
        _freeze(monkeypatch, "2026-09-21T02:00:00Z")
        _serve(monkeypatch)
        snapshot = hlw._load_snapshot()
        assert [e["fetched_at"] for e in snapshot.history] == [
            "2026-09-20T02:00:00Z",
            "2026-09-21T02:00:00Z",
        ]
        assert snapshot.history[-1]["coins"]["BTC"]["long_count"] == 1

    def test_history_older_than_the_keep_window_is_pruned(self):
        entries = [
            _history("2026-09-19T00:00:00Z", {}),
            _history("2026-09-20T22:00:00Z", {}),
        ]
        kept = hlw._prune_history(entries, "2026-09-21T02:00:00Z")
        assert [e["fetched_at"] for e in kept] == ["2026-09-20T22:00:00Z"]

    def test_history_is_capped_by_count(self, monkeypatch):
        monkeypatch.setattr(hlw, "MAX_HISTORY_ENTRIES", 2)
        entries = [_history(f"2026-09-21T0{i}:00:00Z", {}) for i in range(4)]
        kept = hlw._prune_history(entries, "2026-09-21T05:00:00Z")
        assert [e["fetched_at"] for e in kept] == [
            "2026-09-21T02:00:00Z",
            "2026-09-21T03:00:00Z",
        ]

    def test_a_pruned_entry_can_never_have_reached_the_comparison_band(self):
        # The invariant the module asserts at import; stated here too so a
        # later tuning of either constant fails a test rather than silently
        # making the 24-hour change unreachable.
        assert hlw.HISTORY_KEEP_HOURS >= hlw.DELTA_MAX_HOURS


# --------------------------------------------------------------------------- #
# Cache validation (the lower trust tier)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCacheValidation:
    def _read(self, tmp_path, payload):
        path = _write_cache(tmp_path, payload)
        return hlw._read_cache(str(path))

    def test_a_valid_payload_round_trips(self, tmp_path):
        payload = _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]))
        assert self._read(tmp_path, payload) is not None

    def test_a_missing_file_is_an_ordinary_miss(self, tmp_path):
        set_config({"data_cache_dir": str(tmp_path)})
        assert hlw._read_cache(str(tmp_path / "absent.json")) is None

    def test_a_non_object_payload_is_rejected(self, tmp_path):
        set_config({"data_cache_dir": str(tmp_path)})
        path = tmp_path / "hyperliquid_whales.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert hlw._read_cache(str(path)) is None

    def test_an_older_schema_is_ignored_rather_than_migrated(self, tmp_path):
        payload = _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]))
        payload["schema"] = hlw.CACHE_SCHEMA - 1
        assert self._read(tmp_path, payload) is None

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda p: p.update(leaderboard={"fetched_at": "2026-09-21T00:00:00Z", "addresses": []}),
            lambda p: p["leaderboard"]["addresses"].append({"address": "nope", "account_value": 1}),
            lambda p: p["leaderboard"].update(fetched_at="yesterday"),
            lambda p: p["current"].update(fetched_at="2026-09-21"),
            lambda p: p["current"].update(digest=42),
            lambda p: p["current"].update(answered=99),
            lambda p: p["current"].update(attempted=99),
            lambda p: p["current"].update(malformed=-1),
            lambda p: p["current"].update(malformed=True),
            lambda p: p["current"].update(answered=0, attempted=0, sampled=0),
            lambda p: p["current"].update(stopped="budget_exceeded"),
            lambda p: p["current"].pop("stopped"),
            lambda p: p["current"].update(positions="oops"),
            lambda p: p["current"]["positions"].append({"address": "nope"}),
            lambda p: p["current"]["positions"].append(_record(LONG_ADDR, szi=0.0)),
            lambda p: p["current"]["positions"].append(_record(LONG_ADDR, notional=float("nan"))),
            lambda p: p["current"]["positions"].append(_record(LONG_ADDR, notional=-1.0)),
            lambda p: p["current"]["positions"].append(_record(LONG_ADDR, coin="BT|C")),
            lambda p: p["current"]["positions"].append(_record(LONG_ADDR, leverage="lots")),
            lambda p: p.update(history="oops"),
            lambda p: p["history"].append({"fetched_at": "2026-09-21T00:00:00Z"}),
            lambda p: p["history"].append(_history("nope", {})),
            lambda p: p["history"].append(
                _history("2026-09-21T00:00:00Z", {}, answered=3, sampled=2)
            ),
            lambda p: p["history"][0].pop("sampled"),
            lambda p: p["history"][0].update(answered=0),
            lambda p: p["history"].append(_history("2026-09-21T00:00:00Z", {"BTC": "oops"})),
            lambda p: p["history"].append(
                _history("2026-09-21T00:00:00Z", {"BTC": _totals(1, 0, -5.0, 0.0)})
            ),
            lambda p: p["history"].append(
                _history("2026-09-21T00:00:00Z", {"BTC": _totals(1.5, 0, 5.0, 0.0)})
            ),
        ],
        ids=[
            "empty_cohort",
            "bad_cohort_address",
            "bad_cohort_stamp",
            "bad_snapshot_stamp",
            "non_string_digest",
            "answered_over_attempted",
            "attempted_over_sampled",
            "negative_count",
            "bool_count",
            "empty_sweep",
            "unknown_stop_reason",
            "missing_stop_reason",
            "positions_not_a_list",
            "malformed_record",
            "zero_size_record",
            "nonfinite_notional",
            "negative_notional",
            "forged_coin",
            "unreadable_leverage",
            "history_not_a_list",
            "history_missing_digest",
            "history_bad_stamp",
            "history_answered_over_sampled",
            "history_missing_sampled",
            "history_zero_answered",
            "history_totals_not_a_dict",
            "history_negative_notional",
            "history_non_integer_count",
        ],
    )
    def test_an_untrustworthy_payload_is_rejected(self, tmp_path, mutate, caplog):
        payload = _payload(
            _snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)]),
            history=[_history("2026-09-20T00:00:00Z", {"BTC": _totals(1, 0, 10.0, 0.0)})],
        )
        mutate(payload)
        with caplog.at_level(logging.WARNING, logger=WHALES_LOGGER):
            assert self._read(tmp_path, payload) is None
        # A silently-disabled cache would show up as an unexplained permanent
        # miss and an hourly multi-megabyte download.
        assert "Ignoring Hyperliquid whale cache" in caplog.text


# --------------------------------------------------------------------------- #
# The 24-hour change
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestDelta:
    def _snapshot_with(self, history, positions=(), digest="cohortdigest"):
        current = _snapshot("2026-09-21T12:00:00Z", positions)
        current["digest"] = digest
        return hlw._Snapshot(current, list(history), {"fetched_at": "x", "addresses": []}, False)

    def _aggregate(self, snapshot, coin="BTC"):
        return hlw.aggregate_positions(
            hlw._positions_from_records(snapshot.current["positions"], coin), coin
        )

    def test_picks_the_entry_closest_to_twenty_four_hours(self):
        history = [
            _history("2026-09-20T06:00:00Z", {"BTC": _totals(1, 0, 1.0, 0.0)}),  # 30h
            _history("2026-09-20T11:00:00Z", {"BTC": _totals(1, 0, 2.0, 0.0)}),  # 25h
            _history("2026-09-20T16:00:00Z", {"BTC": _totals(1, 0, 3.0, 0.0)}),  # 20h
        ]
        snapshot = self._snapshot_with(history)
        entry, age = hlw._baseline_entry(snapshot.history, snapshot.current)
        assert entry["fetched_at"] == "2026-09-20T11:00:00Z"
        assert age == 25.0

    def test_entries_outside_the_band_are_ineligible(self):
        history = [
            _history("2026-09-21T02:00:00Z", {}),  # 10h — too recent
            _history("2026-09-20T00:00:00Z", {}),  # 36h — too old
        ]
        snapshot = self._snapshot_with(history)
        assert hlw._baseline_entry(snapshot.history, snapshot.current) == (None, None)

    def test_no_comparison_point_says_so_rather_than_reading_as_no_change(self):
        snapshot = self._snapshot_with([])
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "24h change:** n/a" in line
        assert "first run" in line

    def test_the_change_is_measured_between_the_snapshots_not_against_the_clock(
        self, monkeypatch
    ):
        # A stale serve must compare the snapshot it is SHOWING with one a day
        # before that, and the figure must not drift while the cache sits.
        history = [_history("2026-09-20T12:00:00Z", {"BTC": _totals(1, 1, 10.0, 40.0)})]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR, notional=30.0)])
        _freeze(monkeypatch, "2026-09-25T00:00:00Z")
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "vs 24.0 hours earlier" in line

    def test_the_change_reports_both_sides_and_the_ratio_move(self):
        history = [
            _history("2026-09-20T12:00:00Z", {"BTC": _totals(1, 1, 10_000_000.0, 40_000_000.0)})
        ]
        positions = [
            _record(LONG_ADDR, szi=1.0, notional=30_000_000.0),
            _record(SHORT_ADDR, szi=-1.0, notional=30_000_000.0),
        ]
        snapshot = self._snapshot_with(history, positions)
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "long +20.0m" in line and "short -10.0m" in line
        assert "long/short 0.25 -> 1.00" in line

    def test_a_position_closed_since_the_baseline_reads_as_an_exit(self):
        # Held a day ago, gone now. Left to the ordinary branch this borrowed
        # the ratio's "n/a (no short notional)" - which describes an all-long
        # book, the opposite of an exit - beside two figures that are the whole
        # of the old position with a minus sign.
        history = [
            _history("2026-09-20T12:00:00Z", {"BTC": _totals(1, 1, 10_000_000.0, 30_000_000.0)})
        ]
        snapshot = self._snapshot_with(history)
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "now hold no BTC position at all, down from US$40.0m" in line
        assert "long -10.0m, short -30.0m" in line
        assert "no short notional" not in line

    def test_a_cohort_change_is_disclosed(self):
        # Otherwise a cohort turnover at the leaderboard TTL reads as a
        # position change nobody made.
        history = [
            _history(
                "2026-09-20T12:00:00Z", {"BTC": _totals(1, 0, 10.0, 0.0)}, digest="otherdigest"
            )
        ]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR)])
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "the sampled accounts changed" in line

    def test_an_unchanged_cohort_carries_no_cohort_caveat(self):
        # The negative half. Without it a mutation that appends the caveat
        # UNCONDITIONALLY passes every other test in this file, and every
        # report would carry a sample-changed warning that is not true.
        history = [_history("2026-09-20T12:00:00Z", {"BTC": _totals(1, 0, 10.0, 0.0)})]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR)])
        assert "sampled accounts changed" not in hlw._delta_line(
            snapshot, "BTC", self._aggregate(snapshot)
        )

    def test_neither_end_holding_the_coin_is_not_an_opening(self):
        # The "was opened since" wording over two zero figures, printed under
        # the report's own "No BTC position" line, asserts an event that never
        # happened. A small-cap coin nobody in the sample ever holds hits this
        # on every single call.
        history = [_history("2026-09-20T12:00:00Z", {"ETH": _totals(1, 0, 10.0, 0.0)})]
        snapshot = self._snapshot_with(history)
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "was opened since" not in line
        assert "no BTC position in either this snapshot" in line

    def test_a_baseline_whose_own_sweep_was_short_is_disclosed(self):
        # The CURRENT snapshot's coverage is always printed; without this the
        # older one's is the single thing the report drops, and coverage
        # missing a day ago reads as a position change today.
        history = [
            _history(
                "2026-09-20T12:00:00Z",
                {"BTC": _totals(1, 0, 10.0, 0.0)},
                answered=7,
                sampled=20,
            )
        ]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR)])
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "only reached 7 of its 20 accounts" in line

    def test_a_fully_covered_baseline_carries_no_coverage_caveat(self):
        history = [_history("2026-09-20T12:00:00Z", {"BTC": _totals(1, 0, 10.0, 0.0)})]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR)])
        assert "only reached" not in hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))

    def test_a_coin_absent_from_the_baseline_is_named_as_newly_opened(self):
        history = [_history("2026-09-20T12:00:00Z", {"ETH": _totals(1, 0, 10.0, 0.0)})]
        snapshot = self._snapshot_with(history, [_record(LONG_ADDR, notional=5_000_000.0)])
        line = hlw._delta_line(snapshot, "BTC", self._aggregate(snapshot))
        assert "held no BTC position at all" in line
        # The tail matters: ``caveats`` is empty here, so a sentence ending on
        # the interpolation dangles ("...was opened since").
        assert line.endswith("was opened since then")


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestReport:
    def _report(self, tmp_path, monkeypatch, *, curr_date=DATE, history=(), asset="BTC"):
        _write_cache(
            tmp_path,
            _payload(
                _snapshot(
                    "2026-09-21T00:00:00Z",
                    [
                        _record(LONG_ADDR, szi=1.0, notional=30_000_000.0, leverage=3.0),
                        _record(SHORT_ADDR, szi=-1.0, notional=10_000_000.0, leverage=5.0),
                    ],
                    sampled=12,
                    attempted=12,
                    answered=11,
                ),
                history=history,
            ),
        )
        _freeze(monkeypatch, "2026-09-21T00:30:00Z")
        _serve(monkeypatch)
        return hlw.get_whale_positions_data(asset, curr_date)

    def test_renders_the_headline_figures(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch)
        assert "## Hyperliquid Whale Positioning — BTC" in out
        assert "2 of 11 that answered — 1 long, 1 short" in out
        assert "long US$30.0m vs short US$10.0m — long/short 3.00 (long share 75%)" in out
        # 30m at 3x and 10m at 5x over 40m of notional.
        assert "**Notional-weighted leverage:** 3.5x" in out

    def test_labels_the_snapshot_instant_and_the_cohort(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch)
        assert "live snapshot as of 2026-09-21T00:00:00Z" in out
        assert "cohort fetched 2026-09-21T00:00:00Z" in out

    def test_states_the_coverage_even_when_the_sweep_was_complete(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch)
        assert "11 of 12 sampled accounts answered; 1 could not be read" in out

    def test_the_largest_positions_are_listed(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch)
        assert "| 0x92ea…50e9 | LONG | 30.0 |" in out
        assert "| 0x5b5d…c060 | SHORT | 10.0 |" in out

    def test_the_reading_caveat_names_what_the_sample_is(self, tmp_path, monkeypatch):
        # The measured fact that makes this signal easy to misread: the biggest
        # accounts are largely market makers and vaults.
        out = self._report(tmp_path, monkeypatch)
        assert "not as crowd sentiment" in out
        assert "market makers" in out

    def test_a_coin_nobody_holds_is_not_an_absence_of_open_interest(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch, asset="LINK")
        assert "**No LINK position**" in out
        assert "not as an absence of open interest" in out
        # No figures at all for a coin nobody in the sample holds.
        assert "long/short" not in out

    def test_a_stale_serve_is_labelled(self, tmp_path, monkeypatch):
        _write_cache(tmp_path, _payload(_snapshot("2026-09-21T00:00:00Z", [_record(LONG_ADDR)])))
        _freeze(monkeypatch, "2026-09-21T03:00:00Z")
        _serve(monkeypatch)
        monkeypatch.setattr(
            hlw,
            "_request_state",
            mock.Mock(side_effect=hlw.HyperliquidWhalesUnavailableError("down")),
        )
        out = hlw.get_whale_positions_data("BTC", DATE)
        assert "_STALE by 3.0 hours" in out

    def test_a_backtest_date_carries_the_live_snapshot_disclosure(self, tmp_path, monkeypatch):
        # Live-only data rendered for a past date must say the figures are the
        # fetch's, not that date's.
        out = self._report(tmp_path, monkeypatch, curr_date="2026-01-05")
        assert "live values as of the fetch" in out

    def test_even_one_day_behind_carries_the_disclosure(self, tmp_path, monkeypatch):
        # The shared helper's default threshold is 2 days, which left a
        # curr_date one or two days back carrying the fetch-instant header and
        # no sentence at all - the band a backtest most often sits in. This
        # vendor passes max_behind_days=0 because it serves PRESENT state.
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        out = self._report(tmp_path, monkeypatch, curr_date=yesterday)
        assert "live values as of the fetch" in out

    def test_a_same_day_date_carries_no_disclosure(self, tmp_path, monkeypatch):
        out = self._report(
            tmp_path, monkeypatch, curr_date=datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )
        assert "live values as of the fetch" not in out

    def test_the_delta_line_is_always_present(self, tmp_path, monkeypatch):
        out = self._report(tmp_path, monkeypatch)
        assert "**24h change" in out

    def test_the_delta_renders_from_the_history(self, tmp_path, monkeypatch):
        history = [
            _history("2026-09-20T00:00:00Z", {"BTC": _totals(1, 1, 10_000_000.0, 10_000_000.0)})
        ]
        out = self._report(tmp_path, monkeypatch, history=history)
        assert "long +20.0m" in out and "short +0.0m" in out

    def test_an_unusable_date_is_refused_before_any_request(self, monkeypatch):
        asked = []
        monkeypatch.setattr(hlw, "_load_snapshot", lambda: asked.append(1))
        out = hlw.get_whale_positions_data("BTC", "2026/09/21")
        assert "INVALID_CURR_DATE" in out
        assert asked == []

    def test_an_unrecognized_symbol_gets_a_no_signal_note(self, monkeypatch):
        monkeypatch.setattr(
            hlw, "_load_snapshot", mock.Mock(side_effect=AssertionError("must not fetch"))
        )
        out = hlw.get_whale_positions_data("USDT", DATE)
        assert "no whale-positioning signal" in out
        assert "Do not substitute another coin's positioning." in out

    def test_a_forged_symbol_cannot_open_structure_in_the_report(self, monkeypatch):
        monkeypatch.setattr(hlw, "_load_snapshot", mock.Mock(side_effect=AssertionError("no")))
        out = hlw.get_whale_positions_data("FAKE\n## Forged heading", DATE)
        assert "\n## Forged heading" not in out

    def test_a_non_string_symbol_is_the_callers_bug(self, monkeypatch):
        monkeypatch.setattr(hlw, "_load_snapshot", mock.Mock(side_effect=AssertionError("no")))
        with pytest.raises(hlw.HyperliquidWhalesError):
            hlw.get_whale_positions_data(b"BTC", DATE)

    @pytest.mark.parametrize("spelling", ["BTC-USD", "BTCUSDT", "btc/usd"])
    def test_pair_forms_resolve_like_the_sibling_crypto_vendors(
        self, tmp_path, monkeypatch, spelling
    ):
        out = self._report(tmp_path, monkeypatch, asset=spelling)
        assert "## Hyperliquid Whale Positioning — BTC" in out


# --------------------------------------------------------------------------- #
# Router and analyst wiring
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouting:
    def test_the_category_is_registered_and_optional(self):
        assert interface.TOOLS_CATEGORIES["whale_positioning"]["tools"] == ["get_whale_positions"]
        assert "whale_positioning" in interface.OPTIONAL_CATEGORIES
        assert "hyperliquid_stats" in interface.VENDOR_LIST
        assert interface.VENDOR_METHODS["get_whale_positions"] == {
            "hyperliquid_stats": hlw.get_whale_positions_data
        }

    def test_it_ships_off_and_is_enabled_by_a_dated_flip(self):
        # Keyless, so merging it on would change a running deployment's analyst
        # input surface with no server-side action to date the change from —
        # the same reason options_data and the SoSoValue pair shipped off.
        from tradingagents.default_config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["data_vendors"]["whale_positioning"] == "none"

    def test_the_router_reaches_the_vendor_when_the_category_is_enabled(self):
        set_config({"data_vendors": {"whale_positioning": "hyperliquid_stats"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_whale_positions": {"hyperliquid_stats": lambda *a: "REPORT"}},
        ):
            assert interface.route_to_vendor("get_whale_positions", "BTC", DATE) == "REPORT"

    def test_a_vendor_failure_degrades_to_the_optional_sentinel(self):
        set_config({"data_vendors": {"whale_positioning": "hyperliquid_stats"}})

        def _fail(*args):
            raise hlw.HyperliquidWhalesUnavailableError("stats endpoint down")

        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_whale_positions": {"hyperliquid_stats": _fail}}
        ):
            out = interface.route_to_vendor("get_whale_positions", "BTC", DATE)
        assert "DATA_UNAVAILABLE" in out
        assert "stats endpoint down" in out

    def test_the_disabled_default_answers_without_opening_a_connection(self):
        def _fail(*args):
            raise AssertionError("the vendor must not be called while the category is off")

        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_whale_positions": {"hyperliquid_stats": _fail}}
        ):
            out = interface.route_to_vendor("get_whale_positions", "BTC", DATE)
        assert "DATA_UNAVAILABLE" in out


@pytest.mark.unit
class TestAnalystWiring:
    def _bound(self, asset_type):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        captured = {}

        def _capture(inp):
            captured["prompt"] = str(inp)
            return AIMessage(content="ok")

        class _LLM:
            def bind_tools(self, tools):
                captured["tools"] = list(tools)
                return RunnableLambda(_capture)

        create_news_analyst(_LLM())(
            {
                "trade_date": DATE,
                "asset_type": asset_type,
                "company_of_interest": "BTC-USD" if asset_type == "crypto" else "AAPL",
                "messages": [],
            }
        )
        return captured

    def test_crypto_binds_it_when_the_category_is_enabled(self):
        set_config({"data_vendors": {"whale_positioning": "hyperliquid_stats"}})
        captured = self._bound("crypto")
        assert "get_whale_positions" in {t.name for t in captured["tools"]}
        # Bound but unadvertised leaves the model a tool it has no reason to call.
        assert "get_whale_positions(asset, curr_date)" in captured["prompt"]

    def test_the_stock_path_is_untouched(self):
        set_config({"data_vendors": {"whale_positioning": "hyperliquid_stats"}})
        captured = self._bound("stock")
        assert "get_whale_positions" not in {t.name for t in captured["tools"]}
        assert "whale" not in captured["prompt"].lower()

    def test_the_disabled_default_binds_nothing(self):
        captured = self._bound("crypto")
        assert "get_whale_positions" not in {t.name for t in captured["tools"]}
