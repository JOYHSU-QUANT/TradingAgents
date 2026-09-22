"""CFTC COT positioning (``dataflows.cftc_cot``) and its wiring.

The fixture is eight real rows of the Traders in Financial Futures series
(report dates 2026-07-28 to 2026-09-15), trimmed to the columns the module
reads. Every report test freezes the module clock and points the cache at a
temporary directory, so the only thing that varies between tests is the
analysis date and what the fake CFTC answers.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
import requests
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts import news_analyst as news_analyst_module
from tradingagents.agents.analysts.news_analyst import create_news_analyst
from tradingagents.agents.utils import crypto_data_tools
from tradingagents.dataflows import cftc_cot, interface, sosovalue_common
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorRateLimitError, VendorUnavailableError
from tradingagents.default_config import DEFAULT_CONFIG

FIXTURE = Path(__file__).parent / "fixtures" / "cftc_cot_btc.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))

# A Tuesday, one week after the newest fixture report: that report (as of
# 09-15) was published about 09-19, and the 09-22 report is not in the series.
NOW = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
TODAY = "2026-09-22"


class _Cftc:
    """Stands in for ``requests.get``: one canned answer, every call recorded."""

    def __init__(self, status=200, body=None, text=None, raises=None):
        self.status, self.body, self.text, self.raises = status, body, text, raises
        self.calls: list[dict] = []

    def __call__(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        response = mock.Mock()
        response.status_code = self.status
        response.text = self.text if self.text is not None else json.dumps(self.body)
        response.content = response.text.encode("utf-8")
        response.headers = {"Content-Type": "application/json"}
        response.json = lambda: json.loads(response.text)

        def raise_for_status():
            if self.status >= 400:
                error = requests.HTTPError(f"HTTP {self.status}")
                error.response = response
                raise error

        response.raise_for_status = raise_for_status
        return response


def _freeze(monkeypatch, at: datetime) -> None:
    # The family clock: the snapshot skeleton stamps and ages the cache on it,
    # and the module reads its own time through it, so one patch moves both.
    monkeypatch.setattr(sosovalue_common, "_utc_now", lambda: at)


@pytest.fixture
def clock(monkeypatch):
    _freeze(monkeypatch, NOW)


@pytest.fixture
def cache(tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    return tmp_path


def _serve(monkeypatch, body=ROWS, **kw) -> _Cftc:
    fake = _Cftc(body=body, **kw)
    monkeypatch.setattr(cftc_cot.requests, "get", fake)
    return fake


def _report(monkeypatch, curr_date=TODAY, body=ROWS, asset="BTC") -> str:
    _serve(monkeypatch, body)
    return cftc_cot.get_futures_positioning(asset, curr_date)


@pytest.mark.unit
class TestParse:
    def test_the_fixture_parses_newest_first(self):
        rows = cftc_cot._parse_rows(ROWS)
        assert [r["report_date"] for r in rows][:3] == ["2026-09-15", "2026-09-08", "2026-09-01"]
        assert rows[0]["oi"] == 20773
        assert rows[0]["cats"]["Leveraged funds"] == [5545, 11899, 1841]
        assert rows[0]["cats"]["Non-reportable"] == [1021, 832, None]

    def test_a_row_missing_a_position_column_is_dropped_and_counted(self, caplog):
        rows = [dict(ROWS[0]), dict(ROWS[1])]
        del rows[0]["asset_mgr_positions_short"]
        with caplog.at_level(logging.WARNING):
            parsed = cftc_cot._parse_rows(rows)
        assert [r["report_date"] for r in parsed] == ["2026-09-08"]
        assert "dropped 1 malformed row(s) of 2" in caplog.text

    def test_a_repeated_report_date_keeps_the_first(self):
        twice = [dict(ROWS[0]), dict(ROWS[0], open_interest_all="1")]
        assert cftc_cot._parse_rows(twice)[0]["oi"] == 20773

    def test_out_of_order_rows_are_sorted(self):
        assert cftc_cot._parse_rows(list(reversed(ROWS)))[0]["report_date"] == "2026-09-15"

    def test_nothing_readable_is_a_vendor_error(self):
        with pytest.raises(cftc_cot.CftcError, match="none was a readable report"):
            cftc_cot._parse_rows([{"report_date_as_yyyy_mm_dd": "2026-09-15T00:00:00.000"}, 7])

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("20773", 20773),
            ("20773.0", 20773),
            (0, 0),
            ("-1", None),
            ("1.5", None),
            ("nan", None),
            ("abc", None),
            (None, None),
            (True, None),
            ("1e13", None),
        ],
    )
    def test_a_number_cell_is_a_non_negative_int_or_nothing(self, value, expected):
        assert cftc_cot._int(value) == expected

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2026-09-15T00:00:00.000", "2026-09-15"),
            ("2026-09-15", "2026-09-15"),
            ("2026-13-15T00:00:00.000", None),
            ("2026-9-5", None),
            (20260915, None),
        ],
    )
    def test_a_floating_timestamp_yields_its_day(self, value, expected):
        assert cftc_cot._iso_day(value) == expected


@pytest.mark.unit
class TestRequest:
    def test_the_query_names_the_contract_and_asks_newest_first(self, monkeypatch):
        fake = _serve(monkeypatch)
        cftc_cot._request()
        [call] = fake.calls
        assert call["url"] == cftc_cot.DATASET_URL
        assert call["params"]["$where"] == "cftc_contract_market_code='133741'"
        assert call["params"]["$order"] == "report_date_as_yyyy_mm_dd DESC"
        assert call["params"]["$limit"] == "2000"
        assert call["timeout"] == cftc_cot.REQUEST_TIMEOUT

    def test_a_404_says_the_dataset_moved(self, monkeypatch):
        _serve(monkeypatch, status=404, body={"code": "dataset.missing"})
        with pytest.raises(cftc_cot.CftcError, match="dataset id has probably moved") as info:
            cftc_cot._request()
        assert not isinstance(info.value, VendorUnavailableError)

    def test_a_429_is_the_rate_limit_type(self, monkeypatch):
        _serve(monkeypatch, status=429, body={})
        with pytest.raises(cftc_cot.CftcRateLimitError) as info:
            cftc_cot._request()
        assert isinstance(info.value, VendorRateLimitError)

    def test_a_5xx_is_the_outage_type_with_the_status_only(self, monkeypatch):
        _serve(monkeypatch, status=503, text="<html>gateway</html>")
        with pytest.raises(cftc_cot.CftcUnavailableError, match="HTTP 503") as info:
            cftc_cot._request()
        assert "gateway" not in str(info.value)

    def test_an_unreachable_host_is_the_outage_type(self, monkeypatch):
        _serve(monkeypatch, raises=requests.ConnectionError("dns"))
        with pytest.raises(cftc_cot.CftcUnavailableError):
            cftc_cot._request()

    def test_a_400_is_the_module_type(self, monkeypatch):
        _serve(monkeypatch, status=400, body={"message": "no such column"})
        with pytest.raises(cftc_cot.CftcError) as info:
            cftc_cot._request()
        assert not isinstance(info.value, VendorUnavailableError)

    def test_a_body_that_is_not_json_is_an_outage(self, monkeypatch):
        _serve(monkeypatch, text="<html>maintenance</html>")
        with pytest.raises(cftc_cot.CftcUnavailableError):
            cftc_cot._request()

    def test_a_json_object_is_the_wrong_shape(self, monkeypatch):
        _serve(monkeypatch, body={"rows": []})
        with pytest.raises(cftc_cot.CftcError, match="expected an array"):
            cftc_cot._request()

    def test_a_runaway_answer_is_refused(self, monkeypatch):
        _serve(monkeypatch, body=[ROWS[0]] * cftc_cot.MAX_ROWS)
        with pytest.raises(cftc_cot.CftcError, match="refusing to read"):
            cftc_cot._request()


@pytest.mark.unit
class TestCache:
    def test_a_fetch_writes_the_cache_and_the_next_call_reads_it(self, monkeypatch, clock, cache):
        fake = _serve(monkeypatch)
        cftc_cot.get_futures_positioning("BTC", TODAY)
        path = cache / "cftc_cot_btc.json"
        assert path.exists()
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema"] == cftc_cot.CACHE_SCHEMA
        assert payload["fetched_at"] == "2026-09-22T12:00:00Z"
        assert cftc_cot._read_cache(str(path)) == payload
        cftc_cot.get_futures_positioning("BTC", TODAY)
        assert len(fake.calls) == 1

    def test_a_failed_fetch_serves_the_cache_stale_with_the_caveat(self, monkeypatch, clock, cache):
        _serve(monkeypatch)
        cftc_cot.get_futures_positioning("BTC", TODAY)
        _freeze(monkeypatch, datetime(2026, 9, 24, 12, tzinfo=timezone.utc))
        _serve(monkeypatch, status=503, text="down")
        out = cftc_cot.get_futures_positioning("BTC", "2026-09-24")
        assert "STALE" in out and "2.0 days" in out
        assert "| Dealer |" in out

    def test_a_failed_fetch_past_the_stale_cap_raises(self, monkeypatch, clock, cache):
        _serve(monkeypatch)
        cftc_cot.get_futures_positioning("BTC", TODAY)
        _freeze(monkeypatch, datetime(2026, 10, 14, 12, tzinfo=timezone.utc))
        _serve(monkeypatch, status=503, text="down")
        with pytest.raises(cftc_cot.CftcUnavailableError, match="stale"):
            cftc_cot.get_futures_positioning("BTC", "2026-10-14")

    def test_a_failed_fetch_is_never_written(self, monkeypatch, clock, cache):
        _serve(monkeypatch, status=503, text="down")
        with pytest.raises(cftc_cot.CftcUnavailableError):
            cftc_cot.get_futures_positioning("BTC", TODAY)
        assert not (cache / "cftc_cot_btc.json").exists()

    @pytest.mark.parametrize(
        "mutate, reason",
        [
            (lambda p: p.__setitem__("schema", 0), "schema 0 is not 1"),
            (lambda p: p.pop("fetched_at"), "'fetched_at' is missing"),
            (lambda p: p.__setitem__("rows", []), "'rows' is missing, empty"),
            (lambda p: p["rows"][0].__setitem__("oi", -5), "malformed report"),
            (lambda p: p["rows"][0]["cats"].pop("Dealer"), "malformed report"),
            (
                lambda p: p["rows"][0]["cats"].__setitem__("Non-reportable", [1, 1, 1]),
                "malformed report",
            ),
            (lambda p: p["rows"].reverse(), "not unique and newest first"),
            (lambda p: p["rows"].append(dict(p["rows"][0])), "not unique and newest first"),
        ],
    )
    def test_a_bad_cache_is_rejected_with_its_reason(
        self, monkeypatch, clock, cache, caplog, mutate, reason
    ):
        _serve(monkeypatch)
        cftc_cot.get_futures_positioning("BTC", TODAY)
        path = cache / "cftc_cot_btc.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        mutate(payload)
        path.write_text(json.dumps(payload), encoding="utf-8")
        with caplog.at_level(logging.WARNING):
            assert cftc_cot._read_cache(str(path)) is None
        assert f"Ignoring CFTC COT cache {path}: " in caplog.text and reason in caplog.text

    def test_the_cache_directory_and_the_clock_are_the_familys(self, monkeypatch):
        assert cftc_cot._cache_dir is sosovalue_common._cache_dir
        _freeze(monkeypatch, NOW)
        assert cftc_cot._utc_now() == NOW


@pytest.mark.unit
class TestAsOf:
    """The publication date, not the report date, decides what a day can see."""

    @pytest.mark.parametrize(
        "curr_date, report",
        [
            ("2026-09-16", "2026-09-08"),  # Wednesday: the 09-15 report exists but is unpublished
            ("2026-09-17", "2026-09-08"),  # Thursday
            ("2026-09-18", "2026-09-08"),  # Friday: published in the evening, so not yet
            ("2026-09-19", "2026-09-15"),  # Saturday: the derived publication date
            ("2026-09-22", "2026-09-15"),  # the following Tuesday
        ],
    )
    def test_a_mid_week_date_sees_the_previous_weeks_report(
        self, monkeypatch, clock, cache, curr_date, report
    ):
        out = _report(monkeypatch, curr_date)
        assert f"- Report as of {report} (Tuesday close)" in out
        assert f"| analysis date {curr_date} |" in out

    def test_the_publication_date_is_printed_and_called_derived(self, monkeypatch, clock, cache):
        out = _report(monkeypatch)
        assert "published about 2026-09-19 (derived: report date + 4 days)" in out

    def test_a_date_before_the_series_is_withheld(self, monkeypatch, clock, cache):
        out = _report(monkeypatch, "2026-07-31")  # the 07-28 report publishes 08-01
        assert "- Withheld for 2026-07-31" in out
        assert "No report in the series had been published by 2026-07-31" in out
        assert "the oldest report is as of 2026-07-28" in out
        assert "|" not in out.split("\n", 2)[2]  # no table

    def test_the_first_publishable_report_has_no_change_columns(self, monkeypatch, clock, cache):
        out = _report(monkeypatch, "2026-08-01")
        assert "- Report as of 2026-07-28" in out
        assert "**Open interest:** 20,019 contracts\n" in out
        assert "| n/a | n/a |" in out
        assert "_No earlier published report in the series, so the change columns are n/a._" in out
        assert "_Fewer than 5 published reports in the series, so the 4-week column is n/a._" in out

    def test_a_stale_series_is_withheld_past_the_bound(self, monkeypatch, clock, cache):
        # The newest report publishes 09-19: 21 days later is served, 22 is not.
        served = _report(monkeypatch, "2026-10-10")
        assert "- Report as of 2026-09-15" in served and "_Data lag" in served
        withheld = _report(monkeypatch, "2026-10-11")
        assert "- Withheld for 2026-10-11" in withheld
        assert "22 days earlier; more than 21 days" in withheld
        assert "%" not in withheld

    def test_the_lag_line_appears_past_two_weeks(self, monkeypatch, clock, cache):
        assert "_Data lag" not in _report(monkeypatch, "2026-10-03")  # 14 days
        assert "_Data lag: the newest published COT report is 2026-09-19" in _report(
            monkeypatch, "2026-10-04"
        )


@pytest.mark.unit
class TestReport:
    def test_the_table_and_the_reading_state_the_fixture(self, monkeypatch, clock, cache):
        out = _report(monkeypatch)
        assert out.startswith("## CFTC Commitments of Traders — CME Bitcoin futures (BTC)\n")
        assert "5 BTC standard contract only, code 133741; the Micro contract" in out
        assert "**Open interest:** 20,773 contracts (-310 on the week)" in out
        assert "| Dealer | 6,587 | 3,168 | 620 | +3,419 | 16.5% | +476 | +448 |" in out
        assert "| Asset manager | 4,528 | 1,768 | 486 | +2,760 | 13.3% | -983 | +28 |" in out
        assert (
            "| Leveraged funds | 5,545 | 11,899 | 1,841 | -6,354 | -30.6% | +1,538 | +1,085 |"
        ) in out
        assert "| Non-reportable | 1,021 | 832 | — | +189 | 0.9% | -348 | +477 |" in out
        reading = out.split("_Reading:")[1]
        assert "as of 2026-09-15, 20,773 contracts of open interest" in reading
        assert "dealer net +3,419 (16.5% of OI, +476 on the week)" in reading
        assert "leveraged funds net -6,354 (-30.6% of OI, +1,538 on the week)" in reading
        assert "never a single week's move as a standalone directional signal" in reading
        # The two non-headline categories stay out of the closing line.
        assert "other reportables" not in reading and "non-reportable" not in reading

    def test_the_four_week_change_is_by_report_count(self, monkeypatch, clock, cache):
        # 09-15 against 08-18 (four reports back): dealer net 3419 - 2971.
        out = _report(monkeypatch)
        assert "| Dealer | 6,587 | 3,168 | 620 | +3,419 | 16.5% | +476 | +448 |" in out

    def test_the_method_line_explains_the_carry_short(self, monkeypatch, clock, cache):
        method = _report(monkeypatch).split("_Method:")[1].split("_Reading:")[0]
        assert "a large leveraged-fund short is not by itself a bearish view" in method
        assert "can be later than shown around a US holiday" in method

    def test_a_zero_open_interest_report_does_not_divide(self, monkeypatch, clock, cache):
        rows = [dict(ROWS[0], open_interest_all="0")] + ROWS[1:]
        assert "| n/a | +476 |" in _report(monkeypatch, body=rows)

    def test_an_unpadded_date_is_rendered_canonically(self, monkeypatch, clock, cache):
        assert "| analysis date 2026-09-22 |" in _report(monkeypatch, "2026-9-22")

    def test_the_symbol_decided_on_is_the_one_rendered(self, monkeypatch, clock, cache):
        # Markdown around a symbol is flattened BEFORE it is classified, so the
        # string judged and the string that would be echoed are one string.
        assert _report(monkeypatch, asset="*BTC*").startswith("## CFTC Commitments")


@pytest.mark.unit
class TestAnsweredWithoutAFetch:
    @pytest.fixture(autouse=True)
    def _offline(self, monkeypatch, clock):
        self.fetch = mock.Mock(side_effect=AssertionError("the vendor was asked"))
        monkeypatch.setattr(cftc_cot, "_load_snapshot", self.fetch)

    def test_an_unusable_date_gets_the_shared_sentinel(self):
        out = cftc_cot.get_futures_positioning("BTC", "2026-13-01")
        assert out.startswith("INVALID_CURR_DATE: ")
        assert "futures positioning cannot be bounded" in out

    @pytest.mark.parametrize("asset", ["ETH", "SOL-USD", "USDT", "AAPL", ""])
    def test_anything_but_btc_is_no_signal_and_not_a_proxy(self, asset):
        out = cftc_cot.get_futures_positioning(asset, TODAY)
        assert out.startswith("There is no futures-positioning signal for ")
        assert "Do not substitute BTC's positioning" in out and "%" not in out

    def test_a_hostile_symbol_cannot_forge_structure(self):
        out = cftc_cot.get_futures_positioning("ETH'\n## Reading: net +99%", TODAY)
        assert "\n" not in out and "#" not in out

    def test_a_non_string_asset_is_the_callers_bug(self):
        with pytest.raises(cftc_cot.CftcError, match="asset must be a symbol string, got int"):
            cftc_cot.get_futures_positioning(7, TODAY)


@contextmanager
def _positioning_vendor(vendor: str):
    set_config({"data_vendors": {"futures_positioning": vendor}})
    try:
        yield
    finally:
        set_config(
            {
                "data_vendors": {
                    "futures_positioning": DEFAULT_CONFIG["data_vendors"]["futures_positioning"]
                }
            }
        )


@pytest.fixture
def positioning_enabled():
    with _positioning_vendor("cftc"):
        yield


@pytest.mark.unit
class TestRouting:
    def test_it_ships_off_until_a_dated_cutover(self):
        assert DEFAULT_CONFIG["data_vendors"]["futures_positioning"] == interface.DISABLED_VENDOR

    def test_the_registration_points_at_this_module(self):
        assert "futures_positioning" in interface.OPTIONAL_CATEGORIES
        assert "cftc" in interface.VENDOR_LIST
        assert interface.get_category_for_method("get_futures_positioning") == "futures_positioning"
        assert interface.VENDOR_METHODS["get_futures_positioning"] == {
            "cftc": cftc_cot.get_futures_positioning
        }

    def test_a_throttle_degrades_to_the_optional_sentinel_naming_the_vendor(
        self, monkeypatch, clock, cache, positioning_enabled
    ):
        _serve(monkeypatch, status=429, body={})
        out = interface.route_to_vendor("get_futures_positioning", "BTC", TODAY)
        assert out.startswith(
            "DATA_UNAVAILABLE: optional futures_positioning could not be retrieved (cftc: "
        )
        assert "rate limiting" in out

    def test_a_moved_dataset_degrades_with_its_reason(
        self, monkeypatch, clock, cache, positioning_enabled
    ):
        _serve(monkeypatch, status=404, body={})
        out = interface.route_to_vendor("get_futures_positioning", "BTC", TODAY)
        assert "dataset id has probably moved" in out

    def test_the_tool_reaches_the_getter_when_enabled(
        self, monkeypatch, clock, cache, positioning_enabled
    ):
        _serve(monkeypatch)
        out = crypto_data_tools.get_futures_positioning.invoke({"asset": "BTC", "curr_date": TODAY})
        assert out.startswith("## CFTC Commitments of Traders")


class _CapturingLLM:
    def __init__(self):
        self.bound_tools = None
        self.prompt_value = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)

        def _run(prompt_value):
            self.prompt_value = prompt_value
            return AIMessage(content="ok")

        return RunnableLambda(_run)


def _run_analyst(asset_type="crypto", ticker="BTC-USD") -> _CapturingLLM:
    llm = _CapturingLLM()
    create_news_analyst(llm)(
        {
            "trade_date": TODAY,
            "asset_type": asset_type,
            "company_of_interest": ticker,
            "messages": [],
        }
    )
    return llm


def _prompt(llm) -> str:
    return llm.prompt_value.to_messages()[0].content


def _bound(llm) -> set[str]:
    return {tool.name for tool in llm.bound_tools}


@pytest.mark.unit
class TestNewsAnalystWiring:
    def test_the_shipped_default_does_not_bind_it(self):
        llm = _run_analyst()
        assert "get_futures_positioning" not in _bound(llm)
        assert "get_futures_positioning" not in _prompt(llm)

    def test_crypto_binds_and_advertises_it_when_enabled(self, positioning_enabled):
        llm = _run_analyst()
        assert "get_futures_positioning" in _bound(llm)
        assert "get_futures_positioning(asset, curr_date) for the CFTC Commitments" in _prompt(llm)

    def test_stock_never_binds_it(self, positioning_enabled):
        llm = _run_analyst("stock", "AAPL")
        assert "get_futures_positioning" not in _bound(llm)
        assert "futures_positioning" not in _prompt(llm)

    def test_the_stock_prompt_is_the_same_whether_the_category_is_on_or_off(self):
        off = _prompt(_run_analyst("stock", "AAPL"))
        with _positioning_vendor("cftc"):
            assert _prompt(_run_analyst("stock", "AAPL")) == off

    def test_the_table_row_is_validated_at_import(self):
        [row] = [
            r
            for r in news_analyst_module.OPTIONAL_NEWS_TOOLS
            if r.category == "futures_positioning"
        ]
        assert row.scope == "crypto"
        assert row.tool is crypto_data_tools.get_futures_positioning

    def test_the_tool_node_can_execute_what_the_analyst_binds(self):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        node = TradingAgentsGraph._create_tool_nodes(mock.Mock())["news"]
        assert "get_futures_positioning" in node.tools_by_name


@pytest.mark.unit
class TestProseFollowsTheConstants:
    def test_the_tool_description(self):
        text = " ".join(crypto_data_tools.get_futures_positioning.description.split())
        assert f"report date plus {cftc_cot.PUBLICATION_LAG_DAYS} days" in text
        assert f"more than {cftc_cot.MAX_STALENESS_DAYS} days old" in text
        assert "5-BTC standard contract" in text and cftc_cot.CONTRACT_UNITS == "5 BTC"
        assert "one week and four weeks" in text and cftc_cot.TREND_WEEKS == 4

    def test_the_analyst_hint(self):
        [row] = [
            r
            for r in news_analyst_module.OPTIONAL_NEWS_TOOLS
            if r.category == "futures_positioning"
        ]
        assert "four-week changes" in row.hint and cftc_cot.TREND_WEEKS == 4
        assert "cash-and-carry" in row.hint
        assert "BTC only" in row.hint

    def test_the_report_and_the_constants_agree(self, monkeypatch, clock, cache):
        out = _report(monkeypatch)
        assert f"(derived: report date + {cftc_cot.PUBLICATION_LAG_DAYS} days)" in out
        assert f"| {cftc_cot.TREND_WEEKS}-week Δ net |" in out
        assert f"code {cftc_cot.CONTRACT_CODE};" in out

    def test_the_bounds_are_ordered(self):
        assert cftc_cot.MAX_DATA_LAG_DAYS < cftc_cot.MAX_STALENESS_DAYS
        assert cftc_cot.MAX_STALE_DAYS * 24 > cftc_cot.CACHE_TTL_HOURS
        assert 3 < cftc_cot.PUBLICATION_LAG_DAYS <= 7  # after Friday, before the next report
        assert os.path.basename(cftc_cot.DATASET_URL).endswith(".json")
