"""Farside spot-ETF flow vendor: table parsing (negatives, thousands, blanks,
structure mismatch), rolling per-asset caching with stale fallback, lookahead-safe
windowing, report formatting, and router integration.

All network access is mocked and the parser runs against a trimmed local fixture,
so these run without a network connection.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

from tradingagents.dataflows import farside, interface
from tradingagents.dataflows.config import set_config

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

FARSIDE_LOGGER = "tradingagents.dataflows.farside"


def _fixture(name: str) -> str:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return f.read()


BTC_HTML = _fixture("farside_btc.html")
# Parsed once for the render tests (the fixture is the source of truth for the
# expected numbers below).
PARSED = farside._parse_flow_table(BTC_HTML, "BTC")
RECORDS = PARSED.records


def _snapshot(records, fetched_at="2026-07-09", stale=False, issuers_named=True):
    return farside._FlowSnapshot(records, fetched_at, stale, issuers_named)


def _at(stamp: str):
    """Aware-UTC datetime for patching farside._utc_now in the cache/TTL tests.

    Accepts a full ``YYYY-MM-DDTHH:MM:SSZ`` stamp or a bare ``YYYY-MM-DD`` (midnight).
    """
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in stamp else "%Y-%m-%d"
    return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Value parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseFlowValue:
    def test_parenthesised_negative(self):
        assert farside._parse_flow_value("(44.5)") == -44.5

    def test_thousands_separator(self):
        assert farside._parse_flow_value("1,119.9") == 1119.9

    def test_blank_and_dash_are_zero(self):
        assert farside._parse_flow_value("") == 0.0
        assert farside._parse_flow_value("-") == 0.0
        assert farside._parse_flow_value("—") == 0.0

    def test_plain_number(self):
        assert farside._parse_flow_value("209.4") == 209.4

    def test_garbage_raises(self):
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_value("abc")


# --------------------------------------------------------------------------- #
# Table parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseTable:
    def test_parses_all_data_rows_excluding_summary(self):
        # 06/07/08/09 Jul are data rows; the "Average" summary row is excluded.
        dates = [r["date"] for r in RECORDS]
        assert dates == ["2026-07-06", "2026-07-07", "2026-07-08", "2026-07-09"]

    def test_negatives_thousands_and_blanks(self):
        by_date = {r["date"]: r for r in RECORDS}
        # redFont parenthesised negative
        assert by_date["2026-07-06"]["issuers"]["GBTC"] == -44.5
        # blank BTC cell -> 0.0
        assert by_date["2026-07-07"]["issuers"]["BTC"] == 0.0
        # thousands separator
        assert by_date["2026-07-08"]["issuers"]["IBIT"] == 1119.9
        # Total column
        assert by_date["2026-07-08"]["total"] == 1214.9

    def test_issuer_names_from_header(self):
        assert set(RECORDS[0]["issuers"]) == {"IBIT", "FBTC", "GBTC", "BTC"}
        assert PARSED.issuers_named is True

    def test_dash_forms_parse_as_zero_end_to_end(self):
        # _BLANK_CELLS covers hyphen/en-dash/em-dash, but the fixture only uses an
        # empty cell. Exercise the dash forms through the real table parser so a
        # regression in _BLANK_CELLS is caught end-to-end, not only in the unit
        # test for _parse_flow_value.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>"
            "<tr><td>06 Jul 2026</td><td>-</td><td>–</td><td>—</td><td>0.0</td></tr>"
            "</table>"
        )
        parsed = farside._parse_flow_table(html, "BTC")
        assert parsed.records[0]["issuers"] == {"IBIT": 0.0, "FBTC": 0.0, "GBTC": 0.0}
        assert parsed.records[0]["total"] == 0.0

    def test_missing_table_raises(self):
        with pytest.raises(farside.FarsideError, match="No ETF flow table"):
            farside._parse_flow_table("<html><body><p>nope</p></body></html>", "BTC")

    def test_header_only_table_is_not_selected(self):
        # A table with no dated rows is never selected as the flow table, so this
        # reports "no table found" rather than reaching the (defensive) "no dated
        # flow rows" guard. Asserted by message so the test cannot silently start
        # passing via a different branch.
        html = '<table class="etf"><tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr></table>'
        with pytest.raises(farside.FarsideError, match="No ETF flow table"):
            farside._parse_flow_table(html, "BTC")

    def test_too_few_columns_raises(self):
        # Fewer than 3 columns leaves no room for date + issuer + Total.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>Total</th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError, match="column count"):
            farside._parse_flow_table(html, "BTC")

    def test_missing_issuer_header_degrades_loudly(self, caplog):
        # An unreadable header must NOT discard the flow signal (the figures are
        # still Total-cross-checked), but it must be loud: placeholder labels
        # otherwise read to the agent as real tickers.
        html = (
            '<table class="etf">'
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "</table>"
        )
        with caplog.at_level(logging.WARNING, logger=FARSIDE_LOGGER):
            parsed = farside._parse_flow_table(html, "BTC")
        assert parsed.issuers_named is False
        # Self-describing placeholders, not fake ``ETF{j}`` tickers.
        assert set(parsed.records[0]["issuers"]) == {"unnamed col 1", "unnamed col 2"}
        assert "issuer-ticker header" in caplog.text

    def test_partial_issuer_header_is_disclosed(self, caplog):
        # Header found but one ticker cell is blank: that column must fall back to
        # a placeholder AND issuers_named must go False so the report still warns —
        # otherwise a fabricated label reads to the agent as a real fund ticker.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th></th><th>GBTC</th><th>Total</th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td><td>6.0</td></tr>"
            "</table>"
        )
        with caplog.at_level(logging.WARNING, logger=FARSIDE_LOGGER):
            parsed = farside._parse_flow_table(html, "BTC")
        assert parsed.issuers_named is False
        assert set(parsed.records[0]["issuers"]) == {"IBIT", "unnamed col 2", "GBTC"}
        assert "columns [2]" in caplog.text  # names which column fell back

    def test_ragged_row_raises(self):
        # First data row sets 4 columns; the second has 3 -> structural mismatch.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "<tr><td>07 Jul 2026</td><td>1.0</td><td>2.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError, match="expected"):
            farside._parse_flow_table(html, "BTC")

    def test_unparseable_value_raises(self):
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>garbage</td><td>3.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_table(html, "BTC")

    def test_unparseable_month_raises(self):
        # Passes _DATE_RE ([A-Za-z]{3}) but is not a real month: the locale-safe
        # parser must raise rather than silently mis-parse.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th></th></tr>"
            "<tr><td>06 Xyz 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError):
            farside._parse_flow_table(html, "BTC")

    def test_duplicate_date_rows_raise(self):
        # Two rows for one date would double-count that day's flow in the
        # cumulative and inflate the streak, so it must fail loud like every other
        # structural anomaly rather than serve inflated figures.
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th>Total</th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError, match="multiple rows for 2026-07-06"):
            farside._parse_flow_table(html, "BTC")

    def test_total_not_sum_of_issuers_raises(self):
        # The last column must be the daily Total (sum of issuer columns). A
        # trailing column that is numeric but not the sum (e.g. a cumulative
        # column mistaken for Total) is a structure change and must raise, not be
        # served as a wrong "net flow".
        html = (
            '<table class="etf">'
            "<tr><th></th><th>IBIT</th><th>FBTC</th><th>Total</th></tr>"
            "<tr><td>06 Jul 2026</td><td>1.0</td><td>2.0</td><td>999.0</td></tr>"
            "</table>"
        )
        with pytest.raises(farside.FarsideError, match="Total"):
            farside._parse_flow_table(html, "BTC")


# --------------------------------------------------------------------------- #
# Rendering + lookahead
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRender:
    def _render(self, curr_date, look_back_days=30, asset="BTC", snapshot=None):
        with mock.patch.object(farside, "_load_flows", return_value=snapshot or _snapshot(RECORDS)):
            return farside.get_etf_flow_data(asset, curr_date, look_back_days)

    def test_lookahead_drops_future_rows(self):
        out = self._render("2026-07-08")
        assert "2026-07-09" not in out  # future row filtered
        assert "**Latest (2026-07-08):** +1214.9 net" in out

    def test_non_zero_padded_curr_date_does_not_leak_future_rows(self):
        # A non-zero-padded curr_date ("2026-7-8") must be normalized BEFORE the
        # lookahead filter: a raw lexical compare would admit 2026-07-09 because
        # '2026-07-09' <= '2026-7-8' is True, leaking a future row.
        out = self._render("2026-7-8")  # intended 2026-07-08
        assert "2026-07-09" not in out  # future row still filtered
        assert "**Latest (2026-07-08):** +1214.9 net" in out
        assert "Window ending 2026-07-08" in out  # header shows the canonical date

    def test_cumulative_streak_and_breakdown(self):
        out = self._render("2026-07-08")
        # 216.9 + 29.9 + 1214.9 = 1461.7
        assert "+1461.7" in out
        # "session", not "day": the streak skips zero rows, so a day count would
        # overstate how many consecutive calendar days actually had flow.
        assert "3-session inflow" in out
        assert "(2026-07-06 → 2026-07-08)" in out
        assert "not calendar days" in out
        # latest-day leaders ranked by |flow|
        assert "IBIT +1119.9" in out
        assert "| 2026-07-08 | +1214.9 |" in out

    def test_unsupported_asset_falls_back_to_btc_market_proxy(self):
        # A crypto asset with no spot ETF of its own is served BTC flows as a
        # market-wide proxy, flagged as such (not an asset-specific signal).
        out = self._render("2026-07-08", asset="SOL")
        assert "market-wide" in out
        assert "SOL" in out

    def test_proxy_marker_is_in_the_heading(self):
        # The caveat line alone is not enough: this report gets re-summarised by
        # downstream agents, and a heading identical to a real BTC report is what
        # survives that hop with the proxy framing stripped.
        out = self._render("2026-07-08", asset="SOL")
        heading = out.splitlines()[0]
        assert "market-wide proxy for 'SOL'" in heading
        # A genuine BTC report must not carry the marker.
        assert "market-wide proxy" not in self._render("2026-07-08").splitlines()[0]

    def test_zero_total_day_does_not_break_streak(self):
        # A 0.0-total day is transparent: it neither breaks the inflow streak nor
        # counts as a flow session.
        recs = [
            {"date": "2026-07-01", "issuers": {"IBIT": 5.0}, "total": 5.0},
            {"date": "2026-07-02", "issuers": {"IBIT": 0.0}, "total": 0.0},
            {"date": "2026-07-03", "issuers": {"IBIT": 7.0}, "total": 7.0},
        ]
        out = self._render("2026-07-03", snapshot=_snapshot(recs))
        # 07-01 and 07-03; the 0.0 day is skipped, and the span shows the streak
        # actually reaches back further than 2 calendar days.
        assert "2-session inflow" in out
        assert "(2026-07-01 → 2026-07-03)" in out
        assert "2 flow sessions" in out

    def test_short_window_trims_table(self):
        # look_back_days bounds the rendered table: a row older than the window is
        # dropped from the table (the streak still spans all available history).
        out = self._render("2026-07-09", look_back_days=2)  # window starts 2026-07-07
        assert "| 2026-07-06 |" not in out  # outside the 2-day window
        assert "| 2026-07-08 |" in out  # inside the window

    def test_empty_window(self):
        out = self._render("2020-01-01")
        assert "No ETF flow rows on or before 2020-01-01" in out
        assert "do not fabricate values" in out

    def test_eth_pair_is_native_not_proxy(self):
        # A real ETH symbol renders the ETH header/source (its own spot ETF),
        # not the BTC market-wide proxy — the second entry of ASSET_PATHS.
        out = self._render("2026-07-08", asset="ETH-USD")
        assert "## Spot ETF Flows — ETH" in out
        assert "farside.co.uk/eth/" in out
        assert "market-wide" not in out

    def test_no_separator_ethusd_resolves_to_eth(self):
        # A no-separator pair form (ETHUSD/ETHUSDT) must resolve to ETH via the
        # shared symbol normalizer, not fall through to the BTC proxy.
        out = self._render("2026-07-08", asset="ETHUSDT")
        assert "## Spot ETF Flows — ETH" in out
        assert "market-wide" not in out

    def test_lookalike_symbol_uses_btc_proxy(self):
        # A BTC/ETH look-alike (ETHW is not ETH) is not an ETF asset, so it gets
        # the BTC market-wide proxy, flagged as such — normalizer must not treat
        # it as ETH.
        out = self._render("2026-07-08", asset="ETHW")
        assert "market-wide" in out
        assert "ETHW" in out

    def test_table_truncates_to_max_rows(self):
        # A window longer than MAX_ROWS days is truncated to the most recent
        # MAX_ROWS rows, with a note stating how many days the window held.
        start = datetime(2026, 5, 1)
        n = farside.MAX_ROWS + 10  # 50 consecutive daily rows
        recs = [
            {
                "date": (start + timedelta(days=i)).strftime("%Y-%m-%d"),
                "issuers": {"IBIT": 1.0},
                "total": 1.0,
            }
            for i in range(n)
        ]
        curr_date = recs[-1]["date"]
        out = self._render(curr_date, look_back_days=n + 5, snapshot=_snapshot(recs))
        assert f"most recent {farside.MAX_ROWS} of {n} days" in out
        assert f"| {recs[0]['date']} |" not in out  # oldest row dropped
        assert f"| {recs[-1]['date']} |" in out  # newest row kept
        assert out.count("| +1.0 |") == farside.MAX_ROWS  # exactly MAX_ROWS table rows

    def test_freshest_row_predates_window_shows_latest_not_flat(self):
        # curr_date is past the freshest available row by more than look_back_days
        # (a weekend/holiday gap + a short window): the window is empty but
        # `latest` exists, so the report shows the latest row with a caveat rather
        # than a misleading "+0.0 / 0 flow sessions / empty table" flat rendering.
        out = self._render("2026-07-11", look_back_days=1)  # freshest row is 07-09
        assert "**Latest (2026-07-09):**" in out
        assert "no flow rows within the 1-day window" in out
        assert "| 2026-07-09 |" in out  # latest available row still tabled
        assert "flow sessions in the window" not in out  # not the flat rendering

    def test_empty_window_caveat_is_not_duplicated(self):
        # The cumulative line already names the empty window and the latest date;
        # the table note must not repeat the same sentence.
        out = self._render("2026-07-11", look_back_days=1)
        assert out.count("no flow rows within the 1-day window") == 1

    def test_all_zero_totals_report_no_sessions(self):
        # Every day is a 0.0-total day: no flow sessions at all, so there is no
        # streak to report.
        recs = [
            {"date": "2026-07-01", "issuers": {"IBIT": 0.0}, "total": 0.0},
            {"date": "2026-07-02", "issuers": {"IBIT": 0.0}, "total": 0.0},
        ]
        out = self._render("2026-07-02", snapshot=_snapshot(recs))
        assert "no reported flow sessions on record" in out
        assert "0 flow sessions" in out

    def test_unpopulated_latest_row_is_not_rendered_as_zero_flow(self):
        # Farside posts the day's row before filling it; every cell blank parses
        # to 0.0. Rendering that as "+0.0 net" next to a live inflow streak reads
        # as "demand stopped", and which version the analyst sees would depend
        # only on what time of day the cycle fired.
        recs = [
            {"date": "2026-07-01", "issuers": {"IBIT": 5.0}, "total": 5.0},
            {"date": "2026-07-02", "issuers": {"IBIT": 7.0}, "total": 7.0},
            {"date": "2026-07-03", "issuers": {"IBIT": 0.0}, "total": 0.0},
        ]
        out = self._render("2026-07-03", snapshot=_snapshot(recs))
        assert "**Latest (2026-07-03):** no flow reported" in out
        assert "+0.0 net" not in out
        # The table must match the Latest line: the unpopulated row is "not yet
        # posted", not a confident +0.0 in the row agents most often re-quote.
        assert "| 2026-07-03 | not yet posted |" in out
        assert "| 2026-07-03 | +0.0 |" not in out
        # The streak still reports the real flow history behind the blank row.
        assert "2-session inflow" in out

    def test_data_lag_is_flagged_even_when_the_fetch_succeeded(self):
        # farside.co.uk can serve a parseable page that simply has not been
        # updated. The stale-cache machinery never sees this, so without the lag
        # caveat the report presents week-old flows as current.
        out = self._render("2026-07-20")  # freshest fixture row is 2026-07-09
        assert "Data lag" in out
        assert "11 days before 2026-07-20" in out

    def test_stale_serve_does_not_also_claim_the_fetch_succeeded(self, monkeypatch):
        # MAX_STALE_DAYS (14) > MAX_DATA_LAG_DAYS (4), so any ordinary multi-day
        # outage serves a stale cache whose newest row is ALSO lag-flagged. The
        # two caveats must not contradict each other: the STALE line says the
        # live refresh failed, so the lag line must not assert it succeeded.
        # STALE age is fetch age (fetched_at vs now); lag is data age (newest
        # row vs curr_date) — two different clocks, so pin now.
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-20"))
        out = self._render(
            "2026-07-20", snapshot=_snapshot(RECORDS, fetched_at="2026-07-14", stale=True)
        )
        assert "STALE by 6 days" in out  # fetched 07-14, now 07-20
        assert "Data lag" in out  # newest row 07-09, curr_date 07-20
        assert "the fetch succeeded" not in out
        assert "the cached snapshot above is itself that old" in out

    def test_fresh_serve_attributes_lag_to_the_source(self):
        # The counterpart: with a successful fetch the lag really is the source
        # not publishing, and the report should say so.
        out = self._render("2026-07-20")
        assert "STALE" not in out
        assert "the fetch succeeded" in out

    def test_no_data_lag_caveat_within_tolerance(self):
        # A weekend/holiday publishing gap is normal and must not be flagged.
        out = self._render("2026-07-11")  # 2 days behind, under MAX_DATA_LAG_DAYS
        assert "Data lag" not in out

    def test_unnamed_issuers_are_disclosed(self):
        # When a label could not be read the figures are still served, but the
        # report must say the placeholder labels carry no meaning.
        recs = [{"date": "2026-07-01", "issuers": {"unnamed col 1": 5.0}, "total": 5.0}]
        out = self._render("2026-07-01", snapshot=_snapshot(recs, issuers_named=False))
        assert "Issuer names incomplete" in out

    def test_caveats_render_as_separate_paragraphs(self):
        # Consecutive italic caveats joined by a single newline collapse into one
        # rendered markdown paragraph, running distinct warnings together.
        recs = [{"date": "2026-07-01", "issuers": {"ETF1": 5.0}, "total": 5.0}]
        out = self._render("2026-07-01", asset="SOL", snapshot=_snapshot(recs, issuers_named=False))
        assert "_\n\n_" in out


# --------------------------------------------------------------------------- #
# Caching (one rolling file per asset, with stale fallback)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCache:
    def _use_tmp_cache(self, tmp_path):
        set_config({"data_cache_dir": str(tmp_path)})

    def _write_cache(self, tmp_path, **overrides):
        payload = {
            "asset": "BTC",
            "fetched_at": "2026-07-20",
            "issuers_named": True,
            "rows": RECORDS,
        }
        payload.update(overrides)
        path = tmp_path / "farside_btc.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_cache_is_one_rolling_file_per_asset(self, tmp_path, monkeypatch):
        # A file-per-day scheme accumulates snapshots that can never be served
        # (anything past MAX_STALE_DAYS is refused), so a later refetch must
        # overwrite the one file rather than pile up.
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_request_html", lambda asset: BTC_HTML)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T00:00:00Z"))
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # A day later, past the TTL: the second call refetches and overwrites.
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-24T00:00:00Z"))
        farside.get_etf_flow_data("BTC", "2026-07-09")
        assert [f for f in os.listdir(tmp_path) if f.startswith("farside_")] == ["farside_btc.json"]

    def test_non_dict_cache_is_ignored(self, tmp_path):
        # A tampered/corrupt cache whose top-level JSON is not an object must be
        # treated as a miss, not crash on .get().
        self._use_tmp_cache(tmp_path)
        path = tmp_path / "farside_btc.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert farside._read_cache(str(path)) is None

    def test_non_list_rows_cache_is_ignored(self, tmp_path):
        # "rows" present but not a list (truthy) must still be a miss, not served
        # and then blow up downstream on r["date"].
        self._use_tmp_cache(tmp_path)
        path = self._write_cache(tmp_path, rows="oops")
        assert farside._read_cache(str(path)) is None

    def test_list_of_non_dict_rows_cache_is_ignored(self, tmp_path):
        # "rows" is a non-empty list but its elements are not records: still a
        # miss, else the served payload crashes later on r["date"].
        self._use_tmp_cache(tmp_path)
        path = self._write_cache(tmp_path, rows=[1, 2, 3])
        assert farside._read_cache(str(path)) is None

    def test_row_missing_field_cache_is_ignored(self, tmp_path):
        # A row dict missing a required field ("total") is malformed: a miss, so
        # a future record-schema change reading an old cache degrades cleanly.
        self._use_tmp_cache(tmp_path)
        path = self._write_cache(tmp_path, rows=[{"date": "2026-07-01", "issuers": {}}])
        assert farside._read_cache(str(path)) is None

    def test_row_with_wrong_value_type_is_ignored(self, tmp_path):
        # Key presence is not enough: a non-numeric "total" would pass a
        # presence-only guard and then raise a raw TypeError inside sum().
        self._use_tmp_cache(tmp_path)
        path = self._write_cache(
            tmp_path, rows=[{"date": "2026-07-01", "issuers": {}, "total": "N/A"}]
        )
        assert farside._read_cache(str(path)) is None

    def test_cache_without_fetched_at_is_ignored(self, tmp_path):
        # fetched_at now drives both the within-TTL hit and the staleness cap, so a
        # payload without it must be refetched rather than read as "age unknown".
        self._use_tmp_cache(tmp_path)
        path = tmp_path / "farside_btc.json"
        path.write_text(
            json.dumps({"asset": "BTC", "issuers_named": True, "rows": RECORDS}),
            encoding="utf-8",
        )
        assert farside._read_cache(str(path)) is None

    def test_cache_without_issuers_named_is_ignored(self, tmp_path):
        self._use_tmp_cache(tmp_path)
        path = tmp_path / "farside_btc.json"
        path.write_text(
            json.dumps({"asset": "BTC", "fetched_at": "2026-07-20", "rows": RECORDS}),
            encoding="utf-8",
        )
        assert farside._read_cache(str(path)) is None

    def test_unparseable_fetched_at_degrades(self, tmp_path, monkeypatch):
        # A stale cache whose fetched_at is not a date has an unknown age; on a
        # fetch failure it is treated as beyond the staleness cap and degrades,
        # rather than being served with an "age unknown" caveat.
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23"))
        self._write_cache(tmp_path, fetched_at="not-a-date")
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        with pytest.raises(farside.FarsideError, match="cap"):
            farside.get_etf_flow_data("BTC", "2026-07-09")

    def test_malformed_timestamp_fetched_at_degrades(self, tmp_path, monkeypatch):
        # A fetched_at whose date PREFIX is valid but whose suffix is malformed
        # (e.g. missing the trailing Z) must be unknown-age to BOTH the cap and
        # the display: they share one parse (_cache_age_hours), so it degrades
        # rather than being served with an "STALE by an unknown age" caveat.
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23"))
        self._write_cache(tmp_path, fetched_at="2026-07-20T14:30:00")  # no trailing Z
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        with pytest.raises(farside.FarsideError, match="cap"):
            farside.get_etf_flow_data("BTC", "2026-07-09")

    def test_future_dated_fetched_at_degrades(self, tmp_path, monkeypatch):
        # A future-dated stamp (clock skew / a tampered file) has a negative age.
        # The TTL check already refuses to treat it as fresh; on a refetch failure
        # the cap must also refuse it (negative age -> unknown -> beyond cap),
        # never serving a self-contradictory "STALE by -N hours".
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-15T00:00:00Z"))
        self._write_cache(tmp_path, fetched_at="2026-07-20T00:00:00Z")  # 5 days ahead
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        with pytest.raises(farside.FarsideError, match="cap"):
            farside.get_etf_flow_data("BTC", "2026-07-09")

    def test_within_ttl_reuses_cache(self, tmp_path, monkeypatch):
        # A repeat call inside the CACHE_TTL_HOURS window is served from cache
        # without a second fetch (the fetch throttle).
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T00:00:00Z"))
        calls = {"n": 0}

        def _fetch(asset):
            calls["n"] += 1
            return BTC_HTML

        monkeypatch.setattr(farside, "_request_html", _fetch)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        farside.get_etf_flow_data("BTC", "2026-07-09")
        assert calls["n"] == 1  # second call served from the within-TTL cache

    def test_past_ttl_refetches(self, tmp_path, monkeypatch):
        # Once the cached snapshot is older than CACHE_TTL_HOURS, the next call
        # refetches — so the intraday publication is not pinned for a whole day.
        self._use_tmp_cache(tmp_path)
        calls = {"n": 0}

        def _fetch(asset):
            calls["n"] += 1
            return BTC_HTML

        monkeypatch.setattr(farside, "_request_html", _fetch)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T00:00:00Z"))
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # CACHE_TTL_HOURS + 1 later: past the TTL, so this call refetches.
        later = _at("2026-07-23T00:00:00Z") + timedelta(hours=farside.CACHE_TTL_HOURS + 1)
        monkeypatch.setattr(farside, "_utc_now", lambda: later)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        assert calls["n"] == 2

    def test_failure_without_cache_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23"))
        monkeypatch.setattr(
            farside,
            "_request_html",
            mock.Mock(side_effect=requests.RequestException("boom")),
        )
        with pytest.raises(farside.FarsideError):
            farside.get_etf_flow_data("BTC", "2026-07-09")
        assert not [f for f in os.listdir(tmp_path) if f.startswith("farside_")]

    def test_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        self._use_tmp_cache(tmp_path)
        # Day 1: successful fetch populates the cache.
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T00:00:00Z"))
        monkeypatch.setattr(farside, "_request_html", lambda asset: BTC_HTML)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # Day 2: fetch fails -> stale fallback to day 1's snapshot.
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-24T00:00:00Z"))
        monkeypatch.setattr(
            farside,
            "_request_html",
            mock.Mock(side_effect=requests.RequestException("boom")),
        )
        out = farside.get_etf_flow_data("BTC", "2026-07-09")
        assert "STALE by 1 day:" in out  # singular; 2026-07-24 minus fetched 2026-07-23
        assert "+1214.9" in out  # still shows the cached data

    def test_same_day_stale_serve_shows_hours_not_zero_days(self, tmp_path, monkeypatch):
        # The hourly TTL can serve a stale cache after a same-UTC-day refresh
        # failure; the caveat must show hours, not a misleading "STALE by 0 days".
        self._use_tmp_cache(tmp_path)
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T00:30:00Z"))
        monkeypatch.setattr(farside, "_request_html", lambda asset: BTC_HTML)
        farside.get_etf_flow_data("BTC", "2026-07-09")
        # 7.5h later, still the same UTC day: past the TTL, refetch fails.
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-23T08:00:00Z"))
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        out = farside.get_etf_flow_data("BTC", "2026-07-09")
        assert "STALE by 7.5 hours" in out
        assert "STALE by 0 days" not in out

    def test_stale_cache_at_cap_is_still_served(self, tmp_path, monkeypatch):
        # Boundary: exactly MAX_STALE_DAYS old is within the cap.
        self._use_tmp_cache(tmp_path)
        self._write_cache(tmp_path, fetched_at="2026-07-01")
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-15"))  # 14 days
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        out = farside.get_etf_flow_data("BTC", "2026-07-09")
        assert f"STALE by {farside.MAX_STALE_DAYS} days" in out

    def test_stale_cache_one_day_past_cap_degrades(self, tmp_path, monkeypatch):
        # Boundary: one day beyond the cap must refuse to serve.
        self._use_tmp_cache(tmp_path)
        self._write_cache(tmp_path, fetched_at="2026-07-01")
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-16"))  # 15 days
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        with pytest.raises(farside.FarsideError, match="cap"):
            farside.get_etf_flow_data("BTC", "2026-07-09")

    def test_structural_failure_on_stale_serve_is_logged_at_error(
        self, tmp_path, monkeypatch, caplog
    ):
        # A parse break means the scraper needs a code fix, not that the network
        # blipped; it must not hide among warnings for up to the stale cap.
        self._use_tmp_cache(tmp_path)
        self._write_cache(tmp_path, fetched_at="2026-07-20")
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-22"))
        monkeypatch.setattr(farside, "_request_html", lambda asset: "<html>nope</html>")
        with caplog.at_level(logging.DEBUG, logger=FARSIDE_LOGGER):
            farside.get_etf_flow_data("BTC", "2026-07-09")
        assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == FARSIDE_LOGGER)

    def test_network_failure_on_stale_serve_stays_a_warning(self, tmp_path, monkeypatch, caplog):
        # The counterpart: an ordinary outage must not be escalated to ERROR, or
        # the escalation above stops meaning anything.
        self._use_tmp_cache(tmp_path)
        self._write_cache(tmp_path, fetched_at="2026-07-20")
        monkeypatch.setattr(farside, "_utc_now", lambda: _at("2026-07-22"))
        monkeypatch.setattr(
            farside, "_request_html", mock.Mock(side_effect=requests.RequestException("boom"))
        )
        with caplog.at_level(logging.DEBUG, logger=FARSIDE_LOGGER):
            farside.get_etf_flow_data("BTC", "2026-07-09")
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records if r.name == FARSIDE_LOGGER
        )


# --------------------------------------------------------------------------- #
# Router integration
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouting:
    def test_category_routes_to_farside(self):
        assert interface.get_category_for_method("get_etf_flows") == "crypto_etf_flows"
        set_config({"data_vendors": {"crypto_etf_flows": "farside"}})
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"farside": lambda *a, **k: "ETF_OK"}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-08", 30)
        assert out == "ETF_OK"

    def test_optional_category_degrades_to_sentinel(self):
        # crypto_etf_flows is optional: a vendor failure degrades to a sentinel
        # instead of aborting the run.
        set_config({"data_vendors": {"crypto_etf_flows": "farside"}})

        def _boom(*a, **k):
            raise farside.FarsideError("Farside unavailable")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"farside": _boom}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-08", 30)
        assert "DATA_UNAVAILABLE" in out

    def test_none_vendor_disables_without_calling_out(self):
        # Keyless vendors have no "unset the API key" escape hatch, so "none" is
        # the only way to stop calling one without a redeploy. It must short out
        # before the vendor is invoked.
        set_config({"data_vendors": {"crypto_etf_flows": "none"}})
        called = {"n": 0}

        def _impl(*a, **k):
            called["n"] += 1
            return "should not happen"

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"farside": _impl}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-08", 30)
        assert "DATA_UNAVAILABLE" in out
        assert "disabled by configuration" in out
        assert called["n"] == 0

    def test_none_vendor_on_core_category_is_rejected(self):
        # Disabling a core data category would silently gut the analysis; it must
        # fail loudly instead of degrading.
        set_config({"data_vendors": {"core_stock_apis": "none"}})
        with pytest.raises(ValueError, match="cannot be disabled"):
            interface.route_to_vendor("get_stock_data", "AAPL", "2026-07-01", "2026-07-08")

    def test_is_category_disabled_reflects_config(self):
        set_config({"data_vendors": {"crypto_sentiment": "none"}})
        assert interface.is_category_disabled("crypto_sentiment") is True
        set_config({"data_vendors": {"crypto_sentiment": "alternative_me"}})
        assert interface.is_category_disabled("crypto_sentiment") is False
