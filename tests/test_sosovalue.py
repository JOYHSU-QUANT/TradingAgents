"""SoSoValue spot-ETF flow vendor: request/error taxonomy (incl. API-key
sanitization, redaction, and server-text bounding), response parsing
(live-captured fixtures), rolling
per-asset caching with stale fallback, revision and out-of-window restatement
diffing, partial-failure semantics (aggregate decides vendor success),
lookahead-safe windowing, report formatting, and router integration.

All network access is mocked and the parsers run against fixtures captured from
the real API, so these run without a network connection or a key.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
import requests

import tradingagents.default_config as default_config
from tradingagents.dataflows import farside, interface, sosovalue
from tradingagents.dataflows.config import set_config

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

SOSOVALUE_LOGGER = "tradingagents.dataflows.sosovalue"


def _fixture_json(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# Live-captured API responses (see the module docstring's live-verified facts).
SUMMARY_FIX = _fixture_json("sosovalue_summary_btc.json")
ETFS_FIX = _fixture_json("sosovalue_etfs_btc.json")
IBIT_FIX = _fixture_json("sosovalue_history_ibit.json")
BAD_KEY_FIX = _fixture_json("sosovalue_error_bad_key.json")
OVER_WINDOW_FIX = _fixture_json("sosovalue_error_over_window.json")
BAD_TICKER_FIX = _fixture_json("sosovalue_error_bad_ticker.json")
MISSING_PARAM_FIX = _fixture_json("sosovalue_error_missing_param.json")


def _usd(millions: float) -> float:
    return millions * 1_000_000.0


def _srow(date: str, total_m: float, cum_m: float = 50_000.0) -> dict:
    return {"date": date, "total_net_inflow": _usd(total_m), "cum_net_inflow": _usd(cum_m)}


def _frow(date: str, flow_m: float | None) -> dict:
    return {"date": date, "net_inflow": None if flow_m is None else _usd(flow_m)}


SUMMARY_RECS = [
    _srow("2026-07-06", 216.9),
    _srow("2026-07-07", 29.9),
    _srow("2026-07-08", 1214.9, cum_m=51_244.8),
    _srow("2026-07-09", -100.0, cum_m=51_144.8),
]

FUNDS = {
    "IBIT": {
        "name": "iShares Bitcoin Trust",
        "rows": [
            _frow("2026-07-06", 150.0),
            _frow("2026-07-07", 20.0),
            _frow("2026-07-08", 1119.9),
            _frow("2026-07-09", -60.0),
        ],
    },
    "FBTC": {
        "name": "Fidelity Wise Origin Bitcoin Fund",
        # 2026-07-09 is the pending-day shape the live API serves: the date
        # exists but net_inflow is null (issuer has not filed yet).
        "rows": [_frow("2026-07-08", 95.0), _frow("2026-07-09", None)],
    },
}


def _snapshot(
    summary=None,
    funds=None,
    funds_total=None,
    funds_failed=(),
    funds_unusable=0,
    list_fetched=True,
    revisions=None,
    cum_restated=None,
    fetched_at="2026-07-09T00:00:00Z",
    stale=False,
):
    funds = FUNDS if funds is None else funds
    return sosovalue._FlowSnapshot(
        summary_rows=SUMMARY_RECS if summary is None else summary,
        funds=funds,
        funds_total=len(funds) if funds_total is None else funds_total,
        funds_failed=list(funds_failed),
        funds_unusable=funds_unusable,
        list_fetched=list_fetched,
        revisions=revisions or {},
        cum_restated=cum_restated,
        fetched_at=fetched_at,
        stale=stale,
    )


def _at(stamp: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in stamp else "%Y-%m-%d"
    return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)


class _FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("not json")
        return self._body


# --------------------------------------------------------------------------- #
# _request: auth, rate limit, and the two live-verified error body shapes
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRequest:
    def _get(self, response, path="/etfs/summary-history", params=None):
        with (
            mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": "test-key"}),
            mock.patch.object(sosovalue.requests, "get", return_value=response) as getter,
        ):
            result = sosovalue._request(path, params or {"symbol": "BTC"})
        return result, getter

    def test_success_returns_data_and_sends_key_header(self):
        body = {"code": 0, "message": "success", "data": [1, 2], "details": None}
        result, getter = self._get(_FakeResponse(200, body))
        assert result == [1, 2]
        kwargs = getter.call_args.kwargs
        assert kwargs["headers"] == {"x-soso-api-key": "test-key"}
        assert kwargs["timeout"] == sosovalue.REQUEST_TIMEOUT

    def test_unset_key_raises_before_any_request(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(sosovalue.requests, "get") as getter,
            pytest.raises(sosovalue.SoSoValueNotConfiguredError, match="SOSOVALUE_API_KEY"),
        ):
            sosovalue._request("/etfs", {"symbol": "BTC"})
        getter.assert_not_called()

    def test_401_bad_key_raises_not_configured(self):
        # Fixture: the live 401 body. A rejected key is config breakage like an
        # unset one — the router must fall through, never stale-serve it.
        with pytest.raises(sosovalue.SoSoValueNotConfiguredError, match="SOSOVALUE_API_KEY"):
            self._get(_FakeResponse(401, BAD_KEY_FIX))

    def test_429_raises_rate_limit(self):
        with pytest.raises(sosovalue.SoSoValueRateLimitError):
            self._get(_FakeResponse(429, {"code": 429, "message": "slow down"}))

    def test_over_window_403_is_a_vendor_error(self):
        # Fixture: the live HTTP 403 / code 400301 "date range exceeds 30 day
        # limit" body. The client never sends date params, so hitting this is a
        # contract break, not something to retry.
        with pytest.raises(sosovalue.SoSoValueError, match="400301"):
            self._get(_FakeResponse(403, OVER_WINDOW_FIX))

    def test_gateway_error_shape_uses_msg_field(self):
        # Fixture: the live missing-param body carries "msg", not "message".
        with pytest.raises(sosovalue.SoSoValueError, match=r"参数\[symbol\]"):
            self._get(_FakeResponse(400, MISSING_PARAM_FIX))

    def test_bad_ticker_400_is_a_vendor_error(self):
        with pytest.raises(sosovalue.SoSoValueError, match="NOTREAL"):
            self._get(_FakeResponse(400, BAD_TICKER_FIX))

    def test_nonzero_envelope_code_on_http_200_is_an_error(self):
        with pytest.raises(sosovalue.SoSoValueError, match="code 500"):
            self._get(_FakeResponse(200, {"code": 500, "message": "oops"}))

    def test_non_json_body_is_an_error(self):
        with pytest.raises(sosovalue.SoSoValueError, match="no-json"):
            self._get(_FakeResponse(200, None))

    def test_missing_data_list_is_an_error(self):
        with pytest.raises(sosovalue.SoSoValueError, match="'data' list"):
            self._get(_FakeResponse(200, {"code": 0, "message": "success", "data": None}))

    def test_not_configured_is_a_value_error(self):
        # Routing relies on this subclassing for "vendor unavailable" handling.
        assert issubclass(sosovalue.SoSoValueNotConfiguredError, ValueError)

    def test_key_message_names_the_dashboard(self):
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            pytest.raises(sosovalue.SoSoValueNotConfiguredError, match="developer dashboard"),
        ):
            sosovalue.get_api_key()

    def test_request_sends_the_stripped_key(self):
        # A key deployed through a Windows env file gains a trailing CRLF;
        # unstripped it never reaches the network — requests raises a
        # pre-network InvalidHeader whose message embeds the key verbatim.
        body = {"code": 0, "message": "success", "data": [], "details": None}
        with (
            mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": " test-key\r\n"}),
            mock.patch.object(
                sosovalue.requests, "get", return_value=_FakeResponse(200, body)
            ) as getter,
        ):
            sosovalue._request("/etfs", {"symbol": "BTC"})
        assert getter.call_args.kwargs["headers"] == {"x-soso-api-key": "test-key"}

    def test_error_body_echoing_the_key_is_redacted(self):
        # An error body is server-controlled text that lands in the raised
        # message (and from there in logs and the router's LLM-visible
        # DATA_UNAVAILABLE string); a 401 is where a server is most likely to
        # echo the submitted credential back.
        body = {"code": 400103, "message": "invalid key: test-key"}
        with pytest.raises(sosovalue.SoSoValueNotConfiguredError) as exc_info:
            self._get(_FakeResponse(401, body))
        assert "test-key" not in str(exc_info.value)
        assert "[redacted]" in str(exc_info.value)

    def test_redaction_happens_before_the_300_char_truncation(self):
        # A key straddling the truncation boundary must not survive as a
        # partial fragment: redact first, then cut. str(body)'s prefix
        # "{'code': 1, 'detail': '" is 23 chars, so a 270-char filler places
        # the echoed key across the 300-char mark (chars 293-301) — cutting
        # first would leave its head ("test-ke") visible.
        filler = "a" * 270
        body = {"code": 1, "detail": filler + "test-key"}
        with pytest.raises(sosovalue.SoSoValueError) as exc_info:
            self._get(_FakeResponse(500, body))
        assert "test-k" not in str(exc_info.value)

    def test_structured_message_is_truncated_after_redaction(self):
        # The structured `message`/`msg` branch is exactly as server-controlled
        # as the raw-body fallback. Two properties, each with its own probe:
        # the bound (a 400-char filler must be cut at the 300 cap — an
        # unbounded mutant keeps all 400 a's) ...
        body = {"code": 400000, "message": "a" * 400 + "test-key"}
        with pytest.raises(sosovalue.SoSoValueError) as exc_info:
            self._get(_FakeResponse(500, body))
        msg = str(exc_info.value)
        assert "test-k" not in msg
        assert "a" * 301 not in msg
        # ... and the order: a 294-char filler places the key across the
        # 300-char cap (chars 295-302), so a cut-first implementation leaks
        # its head ("test-k").
        straddle = {"code": 400000, "message": "a" * 294 + "test-key"}
        with pytest.raises(sosovalue.SoSoValueError) as exc_info:
            self._get(_FakeResponse(500, straddle))
        assert "test-k" not in str(exc_info.value)

    def test_oversized_envelope_code_is_bounded(self):
        # `code` is only known to be != 0 — a hostile body can carry any JSON
        # value there, and it is interpolated separately from _error_message.
        body = {"code": "c" * 500, "message": "oops"}
        with pytest.raises(sosovalue.SoSoValueError) as exc_info:
            self._get(_FakeResponse(200, body))
        assert "c" * 41 not in str(exc_info.value)

    def test_key_echoed_in_the_code_field_is_redacted(self):
        # The code interpolation is a separate path from _error_message and
        # must scrub the credential the same way.
        body = {"code": "bad test-key", "message": "oops"}
        with pytest.raises(sosovalue.SoSoValueError) as exc_info:
            self._get(_FakeResponse(200, body))
        assert "test-key" not in str(exc_info.value)
        assert "[redacted]" in str(exc_info.value)


@pytest.mark.unit
class TestGetApiKey:
    def test_surrounding_whitespace_is_stripped(self):
        with mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": " test-key\r\n"}):
            assert sosovalue.get_api_key() == "test-key"

    def test_whitespace_only_key_raises_as_unset(self):
        with (
            mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": " \r\n"}),
            pytest.raises(sosovalue.SoSoValueNotConfiguredError, match="not set"),
        ):
            sosovalue.get_api_key()

    @pytest.mark.parametrize("bad", ["se cret", "se\tcret", "sécret", "se\x7fcret", "se\ncret"])
    def test_unheaderable_key_raises_without_echoing_it(self, bad):
        # The rejection message must describe the problem without the key:
        # it propagates into logs and the LLM-visible report text.
        with (
            mock.patch.dict(os.environ, {"SOSOVALUE_API_KEY": bad}),
            pytest.raises(sosovalue.SoSoValueNotConfiguredError) as exc_info,
        ):
            sosovalue.get_api_key()
        assert bad.strip() not in str(exc_info.value)
        assert "cannot travel in an HTTP header" in str(exc_info.value)


# --------------------------------------------------------------------------- #
# Parsers, against the live-captured fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseSummary:
    def test_fixture_rows_parse_ascending_with_only_needed_fields(self):
        rows = sosovalue._parse_summary_rows(SUMMARY_FIX["data"], "BTC")
        assert len(rows) == 20
        assert rows[0]["date"] == "2026-07-06"
        assert rows[-1]["date"] == "2026-07-31"
        assert rows[-1]["total_net_inflow"] == -265372778.16
        assert rows[-1]["cum_net_inflow"] == 51324507916.308
        # Normalized shape: exactly the fields the report uses.
        assert set(rows[0]) == {"date", "total_net_inflow", "cum_net_inflow"}

    def test_empty_data_raises(self):
        with pytest.raises(sosovalue.SoSoValueError, match="no summary rows"):
            sosovalue._parse_summary_rows([], "BTC")

    def test_missing_field_raises(self):
        bad = [{"date": "2026-07-31", "total_net_inflow": 1.0}]  # no cum_net_inflow
        with pytest.raises(sosovalue.SoSoValueError, match="Malformed"):
            sosovalue._parse_summary_rows(bad, "BTC")

    def test_non_finite_value_raises(self):
        bad = [{"date": "2026-07-31", "total_net_inflow": float("nan"), "cum_net_inflow": 1.0}]
        with pytest.raises(sosovalue.SoSoValueError, match="Malformed"):
            sosovalue._parse_summary_rows(bad, "BTC")

    def test_boolean_value_raises(self):
        # bool is an int subclass; a JSON true must not pass as a flow figure.
        bad = [{"date": "2026-07-31", "total_net_inflow": True, "cum_net_inflow": 1.0}]
        with pytest.raises(sosovalue.SoSoValueError, match="Malformed"):
            sosovalue._parse_summary_rows(bad, "BTC")

    def test_non_canonical_date_raises(self):
        bad = [{"date": "2026-7-31", "total_net_inflow": 1.0, "cum_net_inflow": 1.0}]
        with pytest.raises(sosovalue.SoSoValueError, match="Malformed"):
            sosovalue._parse_summary_rows(bad, "BTC")

    def test_duplicate_date_raises(self):
        bad = [
            {"date": "2026-07-31", "total_net_inflow": 1.0, "cum_net_inflow": 1.0},
            {"date": "2026-07-31", "total_net_inflow": 2.0, "cum_net_inflow": 2.0},
        ]
        with pytest.raises(sosovalue.SoSoValueError, match="double-count"):
            sosovalue._parse_summary_rows(bad, "BTC")


@pytest.mark.unit
class TestParseEtfList:
    def test_fixture_lists_thirteen_funds(self):
        funds, unusable = sosovalue._parse_etf_list(ETFS_FIX["data"], "BTC")
        assert len(funds) == 13
        assert unusable == 0
        assert funds[0] == ("IBIT", "iShares Bitcoin Trust")

    def test_unusable_ticker_is_skipped_with_warning_and_counted(self, caplog):
        data = [
            {"ticker": "IBIT", "name": "iShares"},
            {"ticker": "../evil", "name": "path traversal"},
            {"name": "no ticker at all"},
            "not even a dict",
        ]
        with caplog.at_level(logging.WARNING, logger=SOSOVALUE_LOGGER):
            funds, unusable = sosovalue._parse_etf_list(data, "BTC")
        assert funds == [("IBIT", "iShares")]
        assert unusable == 3
        assert caplog.text.count("no usable ticker") == 3

    def test_ticker_shape_boundaries(self):
        # Dots and dashes are legal inside a ticker (share-class suffixes),
        # but the first character must be alphanumeric so a dot-led token can
        # never form a path-traversal-shaped URL segment (quote() leaves "."
        # unescaped regardless of safe=).
        data = [
            {"ticker": "BRK.B", "name": "dot inside"},
            {"ticker": "ABC-D", "name": "dash inside"},
            {"ticker": "..", "name": "dot-led"},
            {"ticker": ".A", "name": "dot-led 2"},
            {"ticker": "ABCDEFGHIJK", "name": "11 chars"},
            # "$" would match before this trailing newline (\Z does not): the
            # raw newline would ride the ticker verbatim into report lines.
            {"ticker": "IBIT\n", "name": "trailing newline"},
        ]
        funds, unusable = sosovalue._parse_etf_list(data, "BTC")
        assert [t for t, _ in funds] == ["BRK.B", "ABC-D"]
        assert unusable == 4

    def test_duplicate_ticker_keeps_first_and_is_not_counted_unusable(self):
        data = [
            {"ticker": "IBIT", "name": "first"},
            {"ticker": "IBIT", "name": "second"},
        ]
        assert sosovalue._parse_etf_list(data, "BTC") == ([("IBIT", "first")], 0)

    def test_non_string_name_becomes_empty(self):
        data = [{"ticker": "IBIT", "name": None}]
        assert sosovalue._parse_etf_list(data, "BTC") == ([("IBIT", "")], 0)

    def test_oversized_name_is_truncated_at_the_boundary(self):
        # The name is stored in the cache verbatim (never rendered), so it is
        # the one server string that could bloat the cache file unbounded.
        data = [{"ticker": "IBIT", "name": "x" * 500}]
        funds, unusable = sosovalue._parse_etf_list(data, "BTC")
        assert funds == [("IBIT", "x" * sosovalue.MAX_FUND_NAME_CHARS)]
        assert unusable == 0


@pytest.mark.unit
class TestParseFundRows:
    def test_fixture_parses_with_null_pending_day(self):
        rows = sosovalue._parse_fund_rows(IBIT_FIX["data"], "IBIT")
        assert len(rows) == 21
        assert rows[0]["date"] == "2026-07-06"
        # The live pending-day shape: the newest row exists with a null flow
        # (trading data is same-day, flow reports lag) and must stay None.
        assert rows[-1]["date"] == "2026-08-03"
        assert rows[-1]["net_inflow"] is None
        assert rows[-2] == {"date": "2026-07-31", "net_inflow": -122656640.0}

    def test_empty_history_raises(self):
        # Mirrors the summary parser's empty-data guard: a 200-with-[] glitch
        # treated as a valid-but-empty history would silently wipe the fund's
        # cached rows (no funds_failed entry, no caveat, no log) and disable
        # the reconciliation check's every-fund-filed gate.
        with pytest.raises(sosovalue.SoSoValueError, match="no history rows"):
            sosovalue._parse_fund_rows([], "IBIT")

    def test_malformed_row_raises(self):
        with pytest.raises(sosovalue.SoSoValueError, match="Malformed"):
            sosovalue._parse_fund_rows([{"net_inflow": 1.0}], "IBIT")

    def test_non_finite_flow_raises(self):
        bad = [{"date": "2026-07-31", "net_inflow": float("inf")}]
        with pytest.raises(sosovalue.SoSoValueError, match="Non-finite"):
            sosovalue._parse_fund_rows(bad, "IBIT")

    def test_oversized_non_finite_value_is_bounded(self):
        # The rejected value is by construction not a number — a hostile body
        # can put any JSON blob there, and it must not flow unbounded into the
        # raised message (and from there into logs and the router sentinel).
        bad = [{"date": "2026-07-31", "net_inflow": "x" * 500}]
        with pytest.raises(sosovalue.SoSoValueError, match="Non-finite") as exc_info:
            sosovalue._parse_fund_rows(bad, "IBIT")
        assert "x" * 201 not in str(exc_info.value)

    def test_duplicate_date_last_wins(self):
        rows = sosovalue._parse_fund_rows(
            [
                {"date": "2026-07-31", "net_inflow": 1.0},
                {"date": "2026-07-31", "net_inflow": 2.0},
            ],
            "IBIT",
        )
        assert rows == [{"date": "2026-07-31", "net_inflow": 2.0}]


@pytest.mark.unit
class TestDiffCumRestatement:
    def test_no_prior_snapshot_or_disjoint_windows_yield_no_marker(self):
        # Nothing to diff against (first fetch), or windows so far apart no
        # date overlaps (a cache resurrected after a long outage): either way
        # there is no anchor day to compare, so nothing is disclosed.
        rows = [_srow("2026-07-31", 1.0, cum_m=2.0)]
        assert sosovalue._diff_cum_restatement(None, rows) is None
        cached = {"summary_rows": [_srow("2026-06-01", 1.0, cum_m=9.0)]}
        assert sosovalue._diff_cum_restatement(cached, rows) is None

    def test_offsetting_changes_that_leave_the_cum_unmoved_yield_no_marker(self):
        # The residual is material (the day's flow moved +$40M with no cum
        # change, so pre-window history moved -$40M), but the two rendered
        # cums agree — the since-launch figure the report shows never moved,
        # so there is nothing visible to disclose.
        cached = {"summary_rows": [_srow("2026-07-01", 10.0, cum_m=100.0)]}
        rows = [_srow("2026-07-01", 50.0, cum_m=100.0)]
        assert sosovalue._diff_cum_restatement(cached, rows) is None


@pytest.mark.unit
class TestClassifyAsset:
    def test_mirrors_farside_classification(self):
        # The two vendors must agree on the support surface, or switching the
        # chain would change which assets get flows at all.
        for symbol in ("BTC", "ETH-USD", "ETHUSDT", "SOL", "XRP", "USDT", "WOOF", "ETHW"):
            assert sosovalue._classify_asset(symbol) == farside._classify_asset(symbol)

    def test_native_proxy_and_no_signal(self):
        assert sosovalue._classify_asset("BTC") == ("BTC", False)
        assert sosovalue._classify_asset("ETH-USD") == ("ETH", False)
        assert sosovalue._classify_asset("SOL") == ("BTC", True)
        assert sosovalue._classify_asset("USDT") == (None, False)
        assert sosovalue._classify_asset("WOOF") == (None, False)


# --------------------------------------------------------------------------- #
# Rendering + lookahead (snapshot injected; no cache, no network)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRender:
    # look_back_days=3 keeps the synthetic 4-day history clear of the
    # window-clamp caveat (which is exercised explicitly below).
    def _render(self, curr_date, look_back_days=3, asset="BTC", snapshot=None):
        with mock.patch.object(sosovalue, "_load_snapshot", return_value=snapshot or _snapshot()):
            return sosovalue.get_etf_flow_data(asset, curr_date, look_back_days)

    def test_lookahead_drops_future_rows(self):
        out = self._render("2026-07-08")
        assert "2026-07-09" not in out
        assert "**Latest (2026-07-08):** +1214.9 net" in out

    def test_non_zero_padded_curr_date_does_not_leak_future_rows(self):
        out = self._render("2026-7-8")  # intended 2026-07-08
        assert "2026-07-09" not in out
        assert "**Latest (2026-07-08):** +1214.9 net" in out
        assert "Window ending 2026-07-08" in out

    def test_cumulative_since_launch_and_table_are_in_usd_millions(self):
        out = self._render("2026-07-08")
        # 216.9 + 29.9 + 1214.9 (stored in raw USD, rendered in US$m)
        assert "**3d cumulative net flow:** +1461.7 (3 flow sessions in the window)" in out
        assert "**Since-launch cumulative:** +51244.8" in out
        assert "as of 2026-07-08" in out
        assert "| 2026-07-08 | +1214.9 |" in out

    def test_streak_reaching_window_edge_says_at_least(self):
        # Every visible session is same-signed, so the true streak may extend
        # past the API's ~30-day window: the report must say ">=N", not "N".
        out = self._render("2026-07-08")
        assert "**Streak:** ≥3-session inflow (2026-07-06 → 2026-07-08)" in out
        assert "may extend further back" in out

    def test_streak_with_visible_break_is_exact(self):
        out = self._render("2026-07-09")
        assert "**Streak:** 1-session outflow (2026-07-09 → 2026-07-09)" in out
        assert "≥" not in out
        assert "not calendar days" in out

    def test_zero_total_day_is_transparent_to_the_streak(self):
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0), _srow("2026-07-03", 7.0)]
        out = self._render("2026-07-03", snapshot=_snapshot(summary=recs, funds={}, funds_total=0))
        assert "≥2-session inflow (2026-07-01 → 2026-07-03)" in out
        assert "(2 flow sessions in the window)" in out

    def test_genuine_zero_net_day_renders_zero(self):
        # Aggregate 0.0 with funds reporting offsetting flows is a real
        # zero-NET day, not a placeholder: render the honest +0.0.
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 5.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", -5.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest (2026-07-02):** +0.0 net" in out
        assert "| 2026-07-02 | +0.0 |" in out
        assert "1 of 2 reporting funds saw inflows" in out

    def test_placeholder_zero_day_is_not_rendered_as_zero_flow(self):
        # Aggregate 0.0 with no fund reporting anything is the live-verified
        # pre-filing placeholder; a confident "+0.0" would read as "demand
        # stopped".
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-01", 5.0)]}}
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest (2026-07-02):** no flow reported yet" in out
        assert "| 2026-07-02 | not yet posted |" in out
        assert "+0.0 net" not in out
        assert "no fund has filed a flow report for 2026-07-02 yet" in out

    def test_null_fund_row_is_excluded_from_leaders_and_breadth(self):
        # FBTC's 2026-07-09 row is the null pending shape: it must not appear
        # as a $0 leader nor count as a reporting fund.
        out = self._render("2026-07-09")
        assert "**Latest-day leaders:** IBIT -60.0" in out
        leaders_line = out.split("**Latest-day leaders:**")[1].splitlines()[0]
        assert "FBTC" not in leaders_line
        assert "0 of 1 reporting funds saw inflows" in out

    def test_leaders_and_breadth_shapes(self):
        out = self._render("2026-07-08")
        assert "**Latest-day leaders:** IBIT +1119.9, FBTC +95.0" in out
        assert (
            "**Breadth (2026-07-08):** 2 of 2 reporting funds saw inflows; "
            "top-3 funds = 100% of gross flow, largest single fund IBIT = 92%"
        ) in out

    def test_breadth_counts_flat_filings_in_the_denominator(self):
        # An explicit $0 filing is a report (user decision): the denominator
        # must not shrink to the movers, or a broadly-flat day reads as thin
        # coverage. Concentration stays over the movers' gross.
        recs = [_srow("2026-07-08", 3.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-08", 5.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-08", 0.0)]},
            "CCC": {"name": "C", "rows": [_frow("2026-07-08", -2.0)]},
        }
        out = self._render("2026-07-08", snapshot=_snapshot(summary=recs, funds=funds))
        assert "1 of 3 reporting funds saw inflows (1 flat)" in out
        # Movers' gross = 7.0: the flat filing adds nothing to concentration.
        assert "largest single fund AAA = 71%" in out

    def test_flat_filing_day_is_a_genuine_zero_not_unposted(self):
        # A fund that filed an explicit $0 IS a report: the day renders as a
        # real zero-flow day, not the pre-filing placeholder.
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-01", 5.0), _frow("2026-07-02", 0.0)]}}
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest (2026-07-02):** +0.0 net" in out
        assert "| 2026-07-02 | +0.0 |" in out
        assert "not yet posted" not in out
        # The leaders/breadth block must agree with the Latest line: the flat
        # filing is a completed report, not a still-pending one.
        assert (
            "**Latest-day leaders:** none — all 1 reporting fund filed flat "
            "($0 at the 0.1 US$m granularity shown) for 2026-07-02" in out
        )
        assert "0 of 1 reporting funds saw inflows (1 flat)" in out

    def test_all_flat_day_renders_breadth_without_pending_wording(self):
        # Every fund filed an explicit $0: the breadth line keeps its settled
        # denominator (flat filings count) and nothing may claim the day is
        # still pending — the Latest line renders it as a confident +0.0.
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 0.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", 0.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest (2026-07-02):** +0.0 net" in out
        assert (
            "**Latest-day leaders:** none — all 2 reporting funds filed flat "
            "($0 at the 0.1 US$m granularity shown) for 2026-07-02" in out
        )
        assert "**Breadth (2026-07-02):** 0 of 2 reporting funds saw inflows (2 flat)" in out
        assert "yet" not in out

    def test_material_aggregate_with_flat_filings_owns_the_asynchrony(self):
        # Mid-filing state: the aggregate already shows +250.0 while the only
        # filings are an explicit $0 and a null. The Latest line, streak, and
        # table all report the material figure, so the breadth line must not
        # declare "a confirmed flat day" — the endpoints disagree and the
        # wording has to own that instead of contradicting the header figure.
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 250.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 0.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", None)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest (2026-07-02):** +250.0 net" in out
        assert "confirmed flat day" not in out
        assert "issuer filings for the day are likely still posting" in out
        assert (
            "**Latest-day leaders:** none — all 1 reporting fund filed flat "
            "($0 at the 0.1 US$m granularity shown) for 2026-07-02" in out
        )
        # The flat-aggregate verdict is untouched: same filings, flat day.
        flat_recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        flat_out = self._render("2026-07-02", snapshot=_snapshot(summary=flat_recs, funds=funds))
        assert "a confirmed flat day, not an unposted one" in flat_out
        assert "still posting" not in flat_out

    def test_leaders_truncation_and_top3_slice_are_pinned(self):
        # Six directional funds with distinct magnitudes: the leaders line
        # shows exactly TOP_ISSUERS of them in descending |flow| order, and
        # the top-3 concentration is computed over the top-3 by magnitude —
        # not over however many the leaders line happens to display.
        recs = [_srow("2026-07-02", 46.0)]  # equals the fund sum
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 40.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", -30.0)]},
            "CCC": {"name": "C", "rows": [_frow("2026-07-02", 20.0)]},
            "DDD": {"name": "D", "rows": [_frow("2026-07-02", 10.0)]},
            "EEE": {"name": "E", "rows": [_frow("2026-07-02", 5.0)]},
            "FFF": {"name": "F", "rows": [_frow("2026-07-02", 1.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        # Exactly five leaders, FFF truncated (the trailing newline pins the cut).
        assert (
            "**Latest-day leaders:** AAA +40.0, BBB -30.0, CCC +20.0, DDD +10.0, EEE +5.0\n" in out
        )
        # gross 106; top-3 (AAA, BBB, CCC) = 90 -> 85%; AAA alone -> 38%.
        assert "5 of 6 reporting funds saw inflows" in out
        assert "top-3 funds = 85% of gross flow" in out
        assert "largest single fund AAA = 38%" in out

    def test_reconciliation_gap_is_disclosed(self):
        # Every listed fund filed, yet the aggregate carries +85.0 US$m the
        # breakdown cannot account for: the /etfs listing is likely missing a
        # fund — the one gap funds_failed/funds_unusable cannot disclose.
        recs = [_srow("2026-07-02", 100.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 10.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", 5.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "_Reconciliation gap:" in out
        assert "sum to +15.0 US$m but the aggregate reports +100.0" in out
        assert "(+85.0 unaccounted)" in out
        assert "may be missing a fund" in out

    def test_reconciled_flat_filings_blame_the_listing_gap_not_still_posting(self):
        # Every listed fund filed an explicit $0 while the aggregate reports
        # +250.0: the reconciliation gate is open, and its premise (every
        # fund HAS filed) contradicts "filings still posting" — the breadth
        # verdict must point at the disclosed listing gap instead of running
        # both explanations in the same report.
        recs = [_srow("2026-07-02", 250.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 0.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", 0.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "_Reconciliation gap:" in out
        assert "reconciliation gap flagged above" in out
        assert "likely still posting" not in out

    def test_no_reconciliation_caveat_within_absolute_tolerance(self):
        recs = [_srow("2026-07-02", 16.5)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 10.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", 5.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "Reconciliation" not in out

    def test_no_reconciliation_caveat_within_relative_tolerance(self):
        # Offsetting big flows: gross 990 -> the 2% slice is 19.8, so a 15.0
        # gap on a huge day is endpoint noise, not a missing fund.
        recs = [_srow("2026-07-02", 25.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 500.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", -490.0)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "Reconciliation" not in out

    def test_no_reconciliation_check_when_breakdown_is_incomplete(self):
        # A failed fund history already explains any gap (and is disclosed);
        # reconciling against a knowingly-short universe would false-alarm.
        recs = [_srow("2026-07-02", 100.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-02", 10.0)]}}
        out = self._render(
            "2026-07-02",
            snapshot=_snapshot(summary=recs, funds=funds, funds_total=2, funds_failed=["XXX"]),
        )
        assert "Reconciliation" not in out

    def test_no_reconciliation_check_when_a_fund_is_still_pending(self):
        # A null filing legitimately shorts the fund sum; only a day where
        # every listed fund filed can be reconciled.
        recs = [_srow("2026-07-02", 100.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 10.0)]},
            "BBB": {"name": "B", "rows": [_frow("2026-07-02", None)]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "Reconciliation" not in out

    def test_no_reconciliation_check_when_listing_has_unusable_entries(self):
        # Unusable listing entries are excluded from funds_total, so the
        # every-fund-filed count check alone would NOT block this snapshot:
        # the funds_unusable guard is load-bearing. The dropped entry's flow
        # is unknown and already disclosed, so a gap against it would
        # false-alarm.
        recs = [_srow("2026-07-02", 100.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-02", 10.0)]}}
        out = self._render(
            "2026-07-02",
            snapshot=_snapshot(summary=recs, funds=funds, funds_unusable=1),
        )
        assert "Reconciliation" not in out

    def test_sub_tick_negative_flow_never_renders_negative_zero(self):
        # A −$40k day rounds to -0.0 at US$m precision; _usd_m normalizes it
        # so the report shows "+0.0", never a confusing "-0.0".
        recs = [
            _srow("2026-07-01", 5.0),
            {
                "date": "2026-07-02",
                "total_net_inflow": -40_000.0,
                "cum_net_inflow": _usd(50_000.0),
            },
        ]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-02", -0.04)]}}
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "-0.0" not in out
        assert "**Latest (2026-07-02):** +0.0 net" in out
        # Classification agrees with the rendered +0.0 (materiality at the
        # report's own granularity): the -$40k day is not a flow session, not
        # a streak member, and AAA is not a leader — the report must not call
        # a figure it displays as zero a "flow event" two lines later.
        assert "(1 flow sessions in the window)" in out
        assert "≥1-session inflow (2026-07-01 → 2026-07-01)" in out
        assert "AAA -0.0" not in out and "AAA +0.0" not in out
        assert "0 of 1 reporting funds saw inflows (1 flat)" in out

    def test_materiality_boundary_sits_at_the_render_tick(self):
        # $50k rounds to 0.1 (a session); $49k rounds to 0.0 (transparent,
        # like a flat day) — the classification threshold IS the render tick.
        recs = [
            {"date": "2026-07-01", "total_net_inflow": 49_000.0, "cum_net_inflow": _usd(50_000)},
            {"date": "2026-07-02", "total_net_inflow": 50_000.0, "cum_net_inflow": _usd(50_000)},
        ]
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds={}))
        assert "(1 flow sessions in the window)" in out
        assert "≥1-session inflow (2026-07-02 → 2026-07-02)" in out
        assert "**Latest (2026-07-02):** +0.1 net" in out

    def test_sub_tick_fund_is_flat_not_a_leader(self):
        # A +$30k filing renders +0.0, so it must count as flat in breadth
        # and stay off the leaders line, not read as a second inflow fund.
        recs = [_srow("2026-07-02", 2.0)]
        funds = {
            "AAA": {"name": "A", "rows": [_frow("2026-07-02", 2.0)]},
            "BBB": {"name": "B", "rows": [{"date": "2026-07-02", "net_inflow": 30_000.0}]},
        }
        out = self._render("2026-07-02", snapshot=_snapshot(summary=recs, funds=funds))
        assert "**Latest-day leaders:** AAA +2.0\n" in out
        assert "BBB" not in out
        assert "1 of 2 reporting funds saw inflows (1 flat)" in out
        assert "largest single fund AAA = 100%" in out

    def test_restatement_caveat_renders_for_a_visible_marker(self):
        out = self._render(
            "2026-07-09",
            snapshot=_snapshot(
                cum_restated={"date": "2026-07-06", "old": 49_800.0, "new": 50_000.0}
            ),
        )
        assert (
            "_Restatement: the cumulative total as of 2026-07-06 moved "
            "+49800.0 → +50000.0 US$m since the previous snapshot" in out
        )
        assert "restated or backfilled history older than the ~30-day window" in out

    def test_restatement_for_not_yet_visible_day_is_not_leaked(self):
        # Same lookahead rule as the revision caveat: a marker dated after
        # curr_date must neither leak the future date nor flag a shift the
        # report's own since-launch figure does not include.
        out = self._render(
            "2026-07-07",
            snapshot=_snapshot(
                cum_restated={"date": "2026-07-08", "old": 51_000.0, "new": 51_244.8}
            ),
        )
        assert "Restatement" not in out
        assert "2026-07-08" not in out

    def test_stale_serve_time_stamps_the_restatement_caveat(self, monkeypatch):
        # Same anchoring as the revision caveat: on a stale serve the
        # disclosure is as old as the snapshot and must say so.
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-07-10T00:00:00Z"))
        out = self._render(
            "2026-07-09",
            snapshot=_snapshot(
                cum_restated={"date": "2026-07-06", "old": 49_800.0, "new": 50_000.0},
                stale=True,
            ),
        )
        restatement = next(line for line in out.split("\n\n") if "Restatement" in line)
        assert "not from this call" in restatement

    def test_window_includes_row_exactly_on_window_start(self):
        # The window filter is inclusive at its lower bound; an off-by-one
        # would silently drop the oldest day from the cumulative and table.
        recs = [
            _srow("2026-07-05", 10.0),
            _srow("2026-07-06", 20.0),
            _srow("2026-07-07", 30.0),
            _srow("2026-07-08", 40.0),
        ]
        out = self._render(
            "2026-07-08",
            look_back_days=3,
            snapshot=_snapshot(summary=recs, funds={}, funds_total=0),
        )
        assert "| 2026-07-05 | +10.0 |" in out
        assert "**3d cumulative net flow:** +100.0 (4 flow sessions in the window)" in out

    def test_recognized_risk_asset_falls_back_to_btc_market_proxy(self):
        out = self._render("2026-07-08", asset="SOL")
        heading = out.splitlines()[0]
        assert "market-wide proxy for 'SOL'" in heading
        assert "not an 'SOL'-specific signal" in out

    def test_native_report_has_no_proxy_marker(self):
        assert "market-wide" not in self._render("2026-07-08")

    def test_stablecoin_and_unrecognized_return_no_signal(self):
        for sym in ("USDT", "USDC", "WOOF", "ETHW"):
            out = self._render("2026-07-08", asset=sym)
            assert f"no spot-ETF flow signal for '{sym}'" in out
            assert "## Spot ETF Flows" not in out

    def test_eth_reports_natively(self):
        recs = [_srow("2026-07-08", 9.0, cum_m=11_210.2)]
        out = self._render(
            "2026-07-08",
            asset="ETH-USD",
            snapshot=_snapshot(summary=recs, funds={}, funds_total=0),
        )
        assert "## Spot ETF Flows — ETH (SoSoValue, net US$m)" in out
        assert "market-wide" not in out
        assert "all ETH US spot ETFs since inception" in out

    def test_stale_caveat_shows_age_from_the_shared_clock(self, monkeypatch):
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-07-08T12:00:00Z"))
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(fetched_at="2026-07-08T00:00:00Z", stale=True),
        )
        assert "_STALE by 12.0 hours: live refresh failed" in out
        assert "fetched 2026-07-08T00:00:00Z" in out

    def test_breakdown_unavailable_caveat_omits_leaders(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds={}, funds_total=0, list_fetched=False),
        )
        assert "Issuer breakdown unavailable" in out
        assert "could not be fetched" in out
        assert "aggregate figures" in out
        assert "**Latest-day leaders:**" not in out
        assert "**Breadth" not in out
        # The aggregate signal itself is intact.
        assert "**Latest (2026-07-08):** +1214.9 net" in out

    def test_empty_fund_list_is_not_reported_as_a_fetch_failure(self):
        # /etfs answered but yielded nothing usable: blaming "could not be
        # fetched" would point operators at the wrong failure.
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds={}, funds_total=0, funds_unusable=2),
        )
        assert (
            "the fund list returned no usable funds (2 listing entries had no usable ticker)" in out
        )
        assert "could not be fetched" not in out

    def test_unusable_listing_entries_are_disclosed(self):
        # Entries dropped by the ticker filter must shrink the disclosed
        # universe visibly, not silently vanish from the denominator.
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds_total=2, funds_unusable=1),
        )
        assert "Issuer breakdown incomplete (2/2 funds)" in out
        assert "1 listing entry with no usable ticker was skipped" in out

    def test_breakdown_incomplete_caveat_names_the_missing_funds(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds_total=3, funds_failed=["ARKB"]),
        )
        assert "Issuer breakdown incomplete (2/3 funds)" in out
        assert "histories for ARKB could not be fetched" in out
        assert "cover only the 2 fetched funds" in out
        # Partial data still renders leaders from what was fetched.
        assert "**Latest-day leaders:** IBIT +1119.9, FBTC +95.0" in out

    def test_all_failed_breakdown_caveat_says_omitted_not_covered(self):
        # With zero fetched funds the body renders no leaders/breadth lines at
        # all, so the incomplete caveat must say they are omitted — claiming
        # they "cover" any funds would describe lines that do not exist.
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds={}, funds_total=3, funds_failed=["ARKB", "FBTC", "IBIT"]),
        )
        assert "Issuer breakdown incomplete (0/3 funds)" in out
        assert "histories for ARKB, FBTC, IBIT could not be fetched" in out
        assert "the per-fund leaders and breadth lines are omitted" in out
        assert "cover only" not in out
        assert "**Latest-day leaders:**" not in out
        assert "**Breadth" not in out

    def test_unfiled_latest_day_with_failed_histories_scopes_the_leaders_claim(self):
        # AAA was fetched but has not filed for the latest day; BBB's history
        # failed outright. "No fund has filed" would claim knowledge of BBB's
        # filings, which the failed history never delivered.
        recs = [_srow("2026-07-01", 5.0), _srow("2026-07-02", 0.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-01", 5.0)]}}
        out = self._render(
            "2026-07-02",
            snapshot=_snapshot(summary=recs, funds=funds, funds_total=2, funds_failed=["BBB"]),
        )
        assert "no fetched fund has filed a flow report for 2026-07-02 yet" in out
        assert "cover only the 1 fetched fund" in out

    def test_incomplete_caveat_sorts_the_failed_tickers(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds_total=4, funds_failed=["ZZZ", "ARKB"]),
        )
        assert "histories for ARKB, ZZZ could not be fetched" in out

    def test_no_rows_report_omits_the_breakdown_caveats(self):
        # With no visible rows the body is the no-rows message; a breakdown
        # caveat about "the leaders and breadth lines" or "the aggregate
        # figures" would describe sections that do not exist (the same guard
        # standard the window-clamp caveat follows).
        out = self._render(
            "2026-06-20",
            snapshot=_snapshot(funds_total=4, funds_failed=["ZZZ", "ARKB"]),
        )
        assert "No ETF flow rows on or before 2026-06-20" in out
        assert "Issuer breakdown" not in out
        out2 = self._render(
            "2026-06-20",
            snapshot=_snapshot(funds={}, funds_total=0, list_fetched=False),
        )
        assert "Issuer breakdown" not in out2

    def test_revision_caveat_for_latest_visible_day(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(revisions={"2026-07-08": [1200.0, 1214.9]}),
        )
        assert (
            "_Revision: 1 day's figure has changed since the previous snapshot "
            "(2026-07-08: +1200.0 → +1214.9, US$m)" in out
        )
        assert "not new flow events" in out

    def test_stale_serve_time_stamps_the_revision_caveat(self, monkeypatch):
        # A stale serve restates the cached revisions for up to the 14-day
        # cap; the caveat must anchor the disclosure to the snapshot's own
        # refresh, or an outage reads as recurring catch-up activity.
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-07-10T00:00:00Z"))
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(
                revisions={"2026-07-08": [1200.0, 1214.9]},
                fetched_at="2026-07-09T00:00:00Z",
                stale=True,
            ),
        )
        assert "_Revision: 1 day's figure has changed" in out
        assert "not from this call" in out

    def test_fresh_serve_revision_caveat_has_no_stale_qualifier(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(revisions={"2026-07-08": [1200.0, 1214.9]}),
        )
        assert "not from this call" not in out

    def test_revision_for_older_visible_day_is_flagged(self):
        # The catch-up main case (user decision): yesterday's provisional
        # figure firmed up after a newer day already became the latest — a
        # latest-day-only check would silently skip exactly this.
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(revisions={"2026-07-07": [25.0, 29.9]}),
        )
        assert "2026-07-07: +25.0 → +29.9" in out

    def test_revision_for_not_yet_visible_day_is_not_leaked(self):
        # A revision on a row beyond curr_date must not leak through the
        # lookahead guard.
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(revisions={"2026-07-09": [-90.0, -100.0]}),
        )
        assert "Revision:" not in out
        assert "2026-07-09" not in out

    def test_multi_day_revisions_merge_into_one_caveat(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(
                revisions={"2026-07-07": [25.0, 29.9], "2026-07-08": [1200.0, 1214.9]}
            ),
        )
        assert (
            "_Revision: 2 days' figures have changed since the previous snapshot "
            "(2026-07-07: +25.0 → +29.9; 2026-07-08: +1200.0 → +1214.9, US$m)" in out
        )
        assert out.count("_Revision:") == 1

    def test_retraction_to_placeholder_is_not_called_catch_up(self):
        # A day the server pulled back to the exact-zero placeholder (no fund
        # filing left) renders as "not yet posted" in the table below; the
        # revision caveat must describe a withdrawal, not "reporting catch-up"
        # that contradicts the cell two paragraphs down.
        recs = [_srow("2026-07-07", 29.9), _srow("2026-07-08", 0.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-07", 20.0)]}}
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(summary=recs, funds=funds, revisions={"2026-07-08": [25.0, 0.0]}),
        )
        assert "2026-07-08: +25.0 → retracted (now unposted)" in out
        assert "The prior figure was withdrawn" in out
        assert "reporting catch-up" not in out
        assert "| 2026-07-08 | not yet posted |" in out

    def test_sub_tick_revision_to_zero_is_not_a_retraction(self):
        # A day revised down to a sub-tick flow rounds to +0.0 at render
        # granularity but is a real (tiny) figure, not the unposted
        # placeholder: the plain arrow and the catch-up verdict stay.
        recs = [_srow("2026-07-07", 29.9), _srow("2026-07-08", 0.04)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-07", 20.0)]}}
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(summary=recs, funds=funds, revisions={"2026-07-08": [25.0, 0.0]}),
        )
        assert "2026-07-08: +25.0 → +0.0" in out
        assert "retracted" not in out
        assert "reporting catch-up" in out

    def test_mixed_revision_and_retraction_keeps_catch_up_for_the_rest(self):
        # One real revision plus one retraction: the retracted entry is
        # labeled by its own arrow, and the shared verdict stays the decided
        # catch-up wording for the entry that genuinely firmed up.
        recs = [_srow("2026-07-07", 29.9), _srow("2026-07-08", 0.0)]
        funds = {"AAA": {"name": "A", "rows": [_frow("2026-07-07", 20.0)]}}
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(
                summary=recs,
                funds=funds,
                revisions={"2026-07-07": [25.0, 29.9], "2026-07-08": [25.0, 0.0]},
            ),
        )
        assert "2026-07-07: +25.0 → +29.9" in out
        assert "2026-07-08: +25.0 → retracted (now unposted)" in out
        assert "reporting catch-up" in out
        assert "withdrawn" not in out

    def test_window_clamp_caveat_when_history_cannot_fill_the_window(self):
        out = self._render("2026-07-08", look_back_days=30)
        assert "_Window clamped: the requested 30-day window would start 2026-06-08" in out
        assert "oldest available row: 2026-07-06" in out

    def test_no_clamp_caveat_for_trading_calendar_artifacts(self):
        # The oldest trading day sits a couple of calendar days inside any
        # window (weekends); that is not a clamp and must not add noise.
        out = self._render("2026-07-08", look_back_days=4)
        assert "Window clamped" not in out

    def test_no_clamp_caveat_when_no_rows_are_visible(self):
        out = self._render("2020-01-01", look_back_days=3650)
        assert "No ETF flow rows on or before 2020-01-01" in out
        assert "do not fabricate values" in out
        assert "Window clamped" not in out

    def test_data_lag_is_flagged_even_when_the_fetch_succeeded(self):
        out = self._render("2026-07-19", look_back_days=15)
        assert "Data lag" in out
        assert "10 days before 2026-07-19" in out
        # Claims visibility as of curr_date, not the feed's frontier: in a
        # backtest the lookahead filter may be hiding genuinely newer rows.
        assert "the fetch succeeded — no newer filing is visible as of 2026-07-19" in out

    def test_stale_serve_does_not_also_claim_the_fetch_succeeded(self, monkeypatch):
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-07-19T00:00:00Z"))
        out = self._render(
            "2026-07-19",
            look_back_days=15,
            snapshot=_snapshot(fetched_at="2026-07-13T00:00:00Z", stale=True),
        )
        assert "STALE by 6 days" in out
        assert "Data lag" in out
        assert "the fetch succeeded" not in out
        # No age claim: snapshot age (STALE line) and row lag are independent
        # quantities — a feed stall served from an hours-stale snapshot must
        # not have the two lines contradict each other.
        assert "the stale snapshot above may itself be missing newer filings" in out

    def test_no_data_lag_caveat_within_tolerance(self):
        out = self._render("2026-07-11", look_back_days=15)
        assert "Data lag" not in out

    def test_empty_window_shows_latest_not_flat(self):
        out = self._render("2026-07-11", look_back_days=1)
        assert "**Latest (2026-07-09):**" in out
        assert "no flow rows within the 1-day window" in out
        assert "_(table shows the latest available row)_" in out
        assert "| 2026-07-09 |" in out
        assert "flow sessions in the window" not in out

    def test_zero_look_back_clamps_to_default_window(self):
        zero = self._render("2026-07-08", look_back_days=0)
        default = self._render("2026-07-08", look_back_days=sosovalue.DEFAULT_LOOKBACK_DAYS)
        assert zero == default
        assert "**0d cumulative net flow:**" not in zero

    def test_table_truncates_to_max_rows(self):
        start = datetime(2026, 5, 1)
        n = sosovalue.MAX_ROWS + 10
        recs = [_srow((start + timedelta(days=i)).strftime("%Y-%m-%d"), 1.0) for i in range(n)]
        curr_date = recs[-1]["date"]
        out = self._render(
            curr_date,
            look_back_days=n + 2,  # inside the clamp tolerance: no extra caveat
            snapshot=_snapshot(summary=recs, funds={}, funds_total=0),
        )
        assert f"most recent {sosovalue.MAX_ROWS} of {n} days" in out
        assert f"| {recs[0]['date']} |" not in out
        assert f"| {recs[-1]['date']} |" in out
        assert out.count("| +1.0 |") == sosovalue.MAX_ROWS

    def test_caveats_render_as_separate_paragraphs(self):
        out = self._render(
            "2026-07-08",
            snapshot=_snapshot(funds={}, funds_total=0, stale=True),
        )
        assert "_\n\n_" in out


# --------------------------------------------------------------------------- #
# Cache + load: TTL, stale fallback, revision diff, partial failures
# --------------------------------------------------------------------------- #
SUMMARY_API = [
    # Descending like the live API; extra fields prove the parser tolerates them.
    {
        "date": "2026-07-31",
        "total_net_inflow": -265372778.16,
        "total_value_traded": 2.67e9,
        "total_net_assets": 7.6e10,
        "cum_net_inflow": 51324507916.308,
    },
    {
        "date": "2026-07-30",
        "total_net_inflow": 233129286.21,
        "total_value_traded": 1.7e9,
        "total_net_assets": 7.9e10,
        "cum_net_inflow": 51589880694.47,
    },
]
LISTING_API = [
    {"ticker": "IBIT", "name": "iShares Bitcoin Trust", "exchange": "NASDAQ"},
    {"ticker": "FBTC", "name": "Fidelity Wise Origin Bitcoin Fund", "exchange": "CBOE"},
]
HISTORIES_API = {
    "IBIT": [
        {"date": "2026-07-31", "ticker": "IBIT", "net_inflow": -122656640.0, "volume": "1"},
        {"date": "2026-07-30", "ticker": "IBIT", "net_inflow": 183385000.0, "volume": "2"},
    ],
    "FBTC": [
        {"date": "2026-07-31", "ticker": "FBTC", "net_inflow": -50000000.0, "volume": "3"},
    ],
}

_HISTORY_PATH_RE = re.compile(r"^/etfs/([^/]+)/history$")


def _stub_requests(
    monkeypatch, summary=None, listing=None, histories=None, fail=(), errors=None, calls=None
):
    """Replace sosovalue._request with an in-memory dispatcher.

    ``fail`` is a set of paths that raise a network error, ``errors`` maps a
    path to a specific exception instance to raise; ``calls`` collects every
    requested path for throttle assertions.
    """
    summary = SUMMARY_API if summary is None else summary
    listing = LISTING_API if listing is None else listing
    histories = HISTORIES_API if histories is None else histories
    errors = errors or {}

    def _impl(path, params):
        if calls is not None:
            calls.append(path)
        if path in errors:
            raise errors[path]
        if path in fail:
            raise requests.RequestException("boom")
        if path == "/etfs/summary-history":
            return summary
        if path == "/etfs":
            return listing
        match = _HISTORY_PATH_RE.match(path)
        if match and match.group(1) in histories:
            return histories[match.group(1)]
        raise AssertionError(f"unexpected SoSoValue path {path}")

    monkeypatch.setattr(sosovalue, "_request", _impl)


@pytest.mark.unit
class TestCacheAndLoad:
    def _setup(self, tmp_path, monkeypatch, now="2026-08-01T00:00:00Z"):
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.setenv("SOSOVALUE_API_KEY", "test-key")
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at(now))

    def _advance(self, monkeypatch, hours=0, minutes=0):
        """Pin the module clock to the _setup base time plus an offset."""
        later = _at("2026-08-01T00:00:00Z") + timedelta(hours=hours, minutes=minutes)
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: later)

    def _write_cache(self, tmp_path, **overrides):
        payload = {
            "asset": "BTC",
            "fetched_at": "2026-07-31T00:00:00Z",
            "summary_rows": [
                {
                    "date": "2026-07-30",
                    "total_net_inflow": _usd(233.1),
                    "cum_net_inflow": _usd(51_589.9),
                },
                {
                    "date": "2026-07-31",
                    "total_net_inflow": _usd(-265.4),
                    "cum_net_inflow": _usd(51_324.5),
                },
            ],
            "funds": {
                "IBIT": {
                    "name": "iShares Bitcoin Trust",
                    "rows": [_frow("2026-07-31", -122.7)],
                }
            },
            "funds_total": 1,
            "funds_failed": [],
            "funds_unusable": 0,
            "list_fetched": True,
            "revisions": {},
            "cum_restated": None,
        }
        payload.update(overrides)
        path = tmp_path / "sosovalue_btc.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_within_ttl_reuses_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, calls=calls)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 1

    def test_cache_file_is_one_rolling_json_per_asset(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        files = [f for f in os.listdir(tmp_path) if f.startswith("sosovalue_")]
        assert files == ["sosovalue_btc.json"]
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["fetched_at"] == "2026-08-01T00:00:00Z"
        assert [r["date"] for r in payload["summary_rows"]] == ["2026-07-30", "2026-07-31"]
        assert set(payload["funds"]) == {"IBIT", "FBTC"}
        assert payload["funds_total"] == 2
        assert payload["funds_failed"] == []
        assert payload["funds_unusable"] == 0
        assert payload["list_fetched"] is True
        assert payload["revisions"] == {}

    def test_past_ttl_refetches(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, calls=calls)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 2

    def test_unset_key_raises_even_with_a_fresh_cache(self, tmp_path, monkeypatch):
        # The emergency-disable switch: removing the key must stop the vendor
        # on the very next call, not after the cache TTL runs out.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        monkeypatch.delenv("SOSOVALUE_API_KEY")
        with pytest.raises(sosovalue.SoSoValueNotConfiguredError):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_failure_without_cache_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        with pytest.raises(sosovalue.SoSoValueError, match="no usable cache"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert not [f for f in os.listdir(tmp_path) if f.startswith("sosovalue_")]

    def test_rate_limited_refresh_with_no_cache_keeps_the_rate_limit_type(
        self, tmp_path, monkeypatch
    ):
        # The wrap that adds "no usable cache" context must not erase the
        # rate-limit type: the router logs VendorRateLimitError as a routine
        # quiet fall-through, not as unexpected vendor breakage with a
        # traceback that then wins the surfaced first-error slot.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(
            monkeypatch,
            errors={"/etfs/summary-history": sosovalue.SoSoValueRateLimitError("429 slow down")},
        )
        with pytest.raises(sosovalue.SoSoValueRateLimitError, match="no usable cache"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_rate_limited_refresh_past_stale_cap_keeps_the_rate_limit_type(
        self, tmp_path, monkeypatch
    ):
        self._setup(tmp_path, monkeypatch, now="2026-08-15T00:00:00Z")
        self._write_cache(tmp_path)
        _stub_requests(
            monkeypatch,
            errors={"/etfs/summary-history": sosovalue.SoSoValueRateLimitError("429 slow down")},
        )
        with pytest.raises(sosovalue.SoSoValueRateLimitError, match="cap"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-08-02T00:00:00Z"))
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        with caplog.at_level(logging.DEBUG, logger=SOSOVALUE_LOGGER):
            out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "STALE by 1 day:" in out
        assert "-265.4" in out  # cached figures still served
        # An ordinary outage stays a warning, not an escalated ERROR.
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records if r.name == SOSOVALUE_LOGGER
        )

    def test_structural_failure_on_stale_serve_is_logged_at_error(
        self, tmp_path, monkeypatch, caplog
    ):
        # A contract/parse break needs a code fix and must not hide among
        # network-blip warnings for up to the stale cap.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-08-02T00:00:00Z"))
        _stub_requests(monkeypatch, summary=[{"date": "garbage"}])
        with caplog.at_level(logging.DEBUG, logger=SOSOVALUE_LOGGER):
            out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "STALE" in out
        assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == SOSOVALUE_LOGGER)

    def test_stale_cache_at_cap_is_still_served(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-14T00:00:00Z")  # exactly 14 days
        self._write_cache(tmp_path)
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert f"STALE by {sosovalue.MAX_STALE_DAYS} days" in out

    def test_stale_cache_past_cap_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-15T00:00:00Z")  # 15 days
        self._write_cache(tmp_path)
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        with pytest.raises(sosovalue.SoSoValueError, match="cap"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_future_dated_fetched_at_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-07-25T00:00:00Z")
        self._write_cache(tmp_path, fetched_at="2026-07-31T00:00:00Z")  # future stamp
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        with pytest.raises(sosovalue.SoSoValueError, match="cap"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_unparseable_fetched_at_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path, fetched_at="2026-07-31T00:00:00")  # no trailing Z
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})
        with pytest.raises(sosovalue.SoSoValueError, match="cap"):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_fund_history_failure_is_partial_not_fatal(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, fail={"/etfs/IBIT/history"})
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown incomplete (1/2 funds)" in out
        assert "histories for IBIT could not be fetched" in out
        assert "cover only the 1 fetched fund" in out
        # The fetched fund still renders; the aggregate is untouched.
        assert "**Latest (2026-07-31):** -265.4 net" in out
        assert "FBTC -50.0" in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["funds_failed"] == ["IBIT"]

    def _run_fund_breaker(self, tmp_path, monkeypatch, tickers, fail=(), errors=None):
        """One refresh over a synthetic listing; -> (out, history_calls, payload)."""
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(
            monkeypatch,
            listing=[{"ticker": t, "name": t} for t in tickers],
            fail=set(fail),
            errors=errors,
            calls=calls,
        )
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        history_calls = [c for c in calls if c.endswith("/history")]
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        return out, history_calls, payload

    def test_consecutive_network_failures_trip_the_fund_breaker(self, tmp_path, monkeypatch):
        # Five listed funds, every history failing at the transport level: the
        # breaker trips on the third consecutive failure and the remaining two
        # histories are never requested — they join funds_failed so disclosure
        # and the short TTL behave like any other incomplete breakdown, and
        # the call stops burning a full timeout per dead fund.
        tickers = ("AAA", "BBB", "CCC", "DDD", "EEE")
        out, history_calls, payload = self._run_fund_breaker(
            tmp_path, monkeypatch, tickers, fail={f"/etfs/{t}/history" for t in tickers}
        )
        assert history_calls == [f"/etfs/{t}/history" for t in ("AAA", "BBB", "CCC")]
        assert "Issuer breakdown incomplete (0/5 funds)" in out
        assert "the per-fund leaders and breadth lines are omitted" in out
        assert "**Latest-day leaders:**" not in out
        assert payload["funds_failed"] == ["AAA", "BBB", "CCC", "DDD", "EEE"]
        assert payload["funds_total"] == 5
        # The skipped funds keep the bookkeeping invariant: the payload the
        # breaker wrote passes its own reader.
        assert sosovalue._read_cache(str(tmp_path / "sosovalue_btc.json"), "BTC") is not None

    def test_a_completed_answer_resets_the_breaker(self, tmp_path, monkeypatch):
        # AAA transport-fails, BBB 429s (the server answered -> counter
        # resets), CCC/DDD/EEE transport-fail: the trip lands on EEE, the
        # third *consecutive* transport failure, and only FFF is skipped. An
        # API-level failure can neither trip nor feed the breaker.
        _, history_calls, payload = self._run_fund_breaker(
            tmp_path,
            monkeypatch,
            ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
            fail={f"/etfs/{t}/history" for t in ("AAA", "CCC", "DDD", "EEE")},
            errors={"/etfs/BBB/history": sosovalue.SoSoValueRateLimitError("throttled")},
        )
        assert history_calls == [f"/etfs/{t}/history" for t in ("AAA", "BBB", "CCC", "DDD", "EEE")]
        assert payload["funds_failed"] == ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]

    def test_a_pure_429_burst_still_tries_every_fund(self, tmp_path, monkeypatch):
        # The decided mid-burst-429 semantics survive the breaker: rate limits
        # are API-level answers, so every history still gets its own try.
        tickers = ("AAA", "BBB", "CCC", "DDD", "EEE")
        _, history_calls, payload = self._run_fund_breaker(
            tmp_path,
            monkeypatch,
            tickers,
            errors={
                f"/etfs/{t}/history": sosovalue.SoSoValueRateLimitError("throttled")
                for t in tickers
            },
        )
        assert history_calls == [f"/etfs/{t}/history" for t in tickers]
        assert payload["funds_failed"] == ["AAA", "BBB", "CCC", "DDD", "EEE"]

    def test_fund_list_failure_omits_breakdown_not_the_vendor(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, fail={"/etfs"})
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown unavailable" in out
        assert "**Latest (2026-07-31):** -265.4 net" in out
        assert "**Latest-day leaders:**" not in out

    def test_refresh_diffs_revisions_against_the_prior_snapshot(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        # Past the TTL, the latest day's figure has firmed up server-side.
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        revised = [dict(SUMMARY_API[0], total_net_inflow=-300000000.0), SUMMARY_API[1]]
        _stub_requests(monkeypatch, summary=revised)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "_Revision: 1 day's figure has changed" in out
        assert "(2026-07-31: -265.4 → -300.0, US$m)" in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["revisions"] == {"2026-07-31": [-265.4, -300.0]}

    def test_unchanged_refresh_flags_no_revision(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        _stub_requests(monkeypatch)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Revision:" not in out

    def test_revision_diff_ignores_sub_tenth_million_noise(self, tmp_path, monkeypatch):
        # The diff works at the report's 0.1 US$m granularity: a change the
        # rendered figure cannot show is float noise, not a revision.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        noisy = [dict(SUMMARY_API[0], total_net_inflow=-265372779.0), SUMMARY_API[1]]
        _stub_requests(monkeypatch, summary=noisy)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Revision:" not in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["revisions"] == {}

    def test_out_of_window_restatement_is_marked_and_disclosed(self, tmp_path, monkeypatch):
        # The earliest overlapping day's cum moved while its own daily flow
        # did not: history older than the servable window was restated. The
        # daily revision diff cannot see it, so the dedicated marker must —
        # or the Since-launch figure drifts silently between calls.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        restated = [
            dict(SUMMARY_API[0], cum_net_inflow=SUMMARY_API[0]["cum_net_inflow"] + 200e6),
            dict(SUMMARY_API[1], cum_net_inflow=SUMMARY_API[1]["cum_net_inflow"] + 200e6),
        ]
        _stub_requests(monkeypatch, summary=restated)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert (
            "_Restatement: the cumulative total as of 2026-07-30 moved "
            "+51589.9 → +51789.9 US$m since the previous snapshot" in out
        )
        assert "Revision:" not in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["cum_restated"] == {"date": "2026-07-30", "old": 51589.9, "new": 51789.9}

    def test_cum_shift_explained_by_a_daily_revision_is_not_marked(self, tmp_path, monkeypatch):
        # The earliest overlapping day's own (disclosed) revision fully
        # explains the cum shift — the residual is zero, so flagging it as an
        # out-of-window restatement too would double-report one event.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        revised = [
            SUMMARY_API[0],
            dict(
                SUMMARY_API[1],
                total_net_inflow=SUMMARY_API[1]["total_net_inflow"] + 100e6,
                cum_net_inflow=SUMMARY_API[1]["cum_net_inflow"] + 100e6,
            ),
        ]
        _stub_requests(monkeypatch, summary=revised)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Revision:" in out
        assert "Restatement" not in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["cum_restated"] is None

    def test_revision_masking_an_out_of_window_restatement_is_still_marked(
        self, tmp_path, monkeypatch
    ):
        # An issuer refiling straddles the window edge: the earliest
        # overlapping day's flow is revised (+$100M, disclosed) AND its cum
        # moves +$140M — the $40M residual can only come from pre-window
        # history. A date-membership test would let the in-window revision
        # silence the restatement permanently; the residual test must not.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        refiled = [
            dict(SUMMARY_API[0], cum_net_inflow=SUMMARY_API[0]["cum_net_inflow"] + 140e6),
            dict(
                SUMMARY_API[1],
                total_net_inflow=SUMMARY_API[1]["total_net_inflow"] + 100e6,
                cum_net_inflow=SUMMARY_API[1]["cum_net_inflow"] + 140e6,
            ),
        ]
        _stub_requests(monkeypatch, summary=refiled)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "(2026-07-30: +233.1 → +333.1, US$m)" in out
        assert (
            "_Restatement: the cumulative total as of 2026-07-30 moved "
            "+51589.9 → +51729.9 US$m" in out
        )
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["cum_restated"] == {"date": "2026-07-30", "old": 51589.9, "new": 51729.9}
        assert "2026-07-30" in payload["revisions"]
        # A TTL-fresh serve replays both disclosures from cache: the validator
        # must accept the marker-plus-revision pair it just wrote.
        out2 = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Restatement" in out2 and "Revision:" in out2

    def test_ttl_boundary_age_exactly_at_ttl_refetches(self, tmp_path, monkeypatch):
        # The freshness comparison is strict: a snapshot aged exactly
        # CACHE_TTL_HOURS is due for refresh, not served for one more cycle.
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, calls=calls)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 2

    def test_incomplete_snapshot_retries_on_the_short_ttl(self, tmp_path, monkeypatch):
        # A partial breakdown must not sit for the full window (user
        # decision): past INCOMPLETE_CACHE_TTL_HOURS the next call refetches
        # and heals, while calls inside that window still hit the cache.
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, fail={"/etfs/IBIT/history"}, calls=calls)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown incomplete" in out
        self._advance(monkeypatch, minutes=30)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 1  # still cached
        self._advance(monkeypatch, hours=sosovalue.INCOMPLETE_CACHE_TTL_HOURS, minutes=1)
        _stub_requests(monkeypatch, calls=calls)  # endpoint recovered
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 2
        assert "Issuer breakdown incomplete" not in out

    def test_aggregate_401_during_refresh_is_not_stale_served(self, tmp_path, monkeypatch):
        # A rejected key is config breakage: it must reach the router even
        # when a stale-servable cache exists (the emergency-disable flip).
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.CACHE_TTL_HOURS + 1)
        _stub_requests(
            monkeypatch,
            errors={"/etfs/summary-history": sosovalue.SoSoValueNotConfiguredError("key rejected")},
        )
        with pytest.raises(sosovalue.SoSoValueNotConfiguredError):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")

    def test_fund_401_mid_batch_propagates_not_absorbed(self, tmp_path, monkeypatch):
        # A 401 on request #2..N must not be swallowed into funds_failed: the
        # payload would have been fetched with a since-revoked key and cached
        # as fresh, defeating the documented next-call cutoff.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(
            monkeypatch,
            errors={"/etfs/IBIT/history": sosovalue.SoSoValueNotConfiguredError("key rejected")},
        )
        with pytest.raises(sosovalue.SoSoValueNotConfiguredError):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert not [f for f in os.listdir(tmp_path) if f.startswith("sosovalue_")]

    def test_empty_listing_from_fetch_renders_the_empty_caveat(self, tmp_path, monkeypatch):
        # /etfs answering an empty list is not a fetch failure: the honest
        # "returned no usable funds" branch must fire end-to-end, not the
        # "could not be fetched" one.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, listing=[])
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "the fund list returned no usable funds" in out
        assert "could not be fetched" not in out

    def test_list_failure_snapshot_also_retries_on_the_short_ttl(self, tmp_path, monkeypatch):
        # The short-TTL rule covers BOTH incomplete shapes: failed fund
        # histories and a failed fund list.
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, fail={"/etfs"}, calls=calls)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        self._advance(monkeypatch, hours=sosovalue.INCOMPLETE_CACHE_TTL_HOURS, minutes=1)
        _stub_requests(monkeypatch, calls=calls)  # listing recovered
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 2
        assert "Issuer breakdown" not in out

    def test_unusable_only_snapshot_keeps_the_long_ttl(self, tmp_path, monkeypatch):
        # Unusable listing entries cannot be healed by a re-fetch, so they do
        # NOT shorten the TTL: a 2h-old snapshot whose only blemish is dropped
        # listing entries is still served from cache, not re-fetched.
        self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path, fetched_at="2026-07-31T22:00:00Z", funds_unusable=2)
        calls = []
        _stub_requests(monkeypatch, calls=calls)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls == []  # served from cache; no refresh attempted
        assert "STALE" not in out

    def test_empty_fund_history_counts_as_failed_not_success(self, tmp_path, monkeypatch):
        # A 200-with-[] fund history is a failure with full disclosure — not
        # a "successful" empty fund that silently vanishes from the breakdown
        # while funds_total still counts it as fetched.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, histories={"IBIT": [], "FBTC": HISTORIES_API["FBTC"]})
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown incomplete (1/2 funds)" in out
        assert "histories for IBIT could not be fetched" in out
        payload = json.loads((tmp_path / "sosovalue_btc.json").read_text(encoding="utf-8"))
        assert payload["funds_failed"] == ["IBIT"]
        assert "IBIT" not in payload["funds"]

    def test_empty_listing_snapshot_retries_on_the_short_ttl(self, tmp_path, monkeypatch):
        # An empty listing is disclosed rather than treated as a fetch
        # failure (settled), but it wholesale-overwrites the previous
        # snapshot's funds — so it must re-try on the short TTL like the
        # other incomplete shapes, not blank leaders/breadth for 6 hours.
        # Contrast with the unusable-only long-TTL rule above: a re-fetch
        # can heal an empty listing, not an unusable entry.
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, listing=[], calls=calls)
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "the fund list returned no usable funds" in out
        self._advance(monkeypatch, minutes=30)
        sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 1  # still cached
        self._advance(monkeypatch, hours=sosovalue.INCOMPLETE_CACHE_TTL_HOURS, minutes=1)
        _stub_requests(monkeypatch, calls=calls)  # listing recovered
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 2
        assert "Issuer breakdown" not in out

    def test_structural_list_failure_is_logged_at_error(self, tmp_path, monkeypatch, caplog):
        # The fund-list twin of the per-fund escalation: a contract break on
        # /etfs must not hide among transient warnings either.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(
            monkeypatch,
            errors={"/etfs": sosovalue.SoSoValueError("contract break")},
        )
        with caplog.at_level(logging.DEBUG, logger=SOSOVALUE_LOGGER):
            out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown unavailable" in out
        assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == SOSOVALUE_LOGGER)

    def test_eth_uses_its_own_rolling_cache_file(self, tmp_path, monkeypatch):
        # The cache pipeline is asset-parametrized; ETH must round-trip its
        # own file, not share (or miss) BTC's.
        self._setup(tmp_path, monkeypatch)
        calls = []
        _stub_requests(monkeypatch, calls=calls)
        out = sosovalue.get_etf_flow_data("ETH", "2026-07-31")
        assert "## Spot ETF Flows — ETH (SoSoValue, net US$m)" in out
        assert (tmp_path / "sosovalue_eth.json").exists()
        sosovalue.get_etf_flow_data("ETH", "2026-07-31")
        assert calls.count("/etfs/summary-history") == 1  # within-TTL hit

    def test_cache_hit_serves_the_stored_revisions(self, tmp_path, monkeypatch):
        # A within-TTL hit must carry the stored revisions forward, or the
        # caveat would only ever survive the single call after a refresh.
        # The stored pair's "new" side matches the summary row (-265.4), as
        # _diff_revisions always writes it.
        self._setup(tmp_path, monkeypatch)
        self._write_cache(
            tmp_path,
            fetched_at="2026-08-01T00:00:00Z",  # fresh relative to pinned now
            revisions={"2026-07-31": [-300.0, -265.4]},
        )
        _stub_requests(monkeypatch, fail={"/etfs/summary-history"})  # must not be reached
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "2026-07-31: -300.0 → -265.4" in out

    def test_cache_write_failure_does_not_fail_the_call(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch)
        with (
            mock.patch.object(sosovalue.json, "dump", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING, logger=SOSOVALUE_LOGGER),
        ):
            out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "**Latest (2026-07-31):** -265.4 net" in out
        assert "Could not write SoSoValue cache" in caplog.text

    def test_structural_fund_failure_is_logged_at_error(self, tmp_path, monkeypatch, caplog):
        # A malformed fund history is a contract break needing a client fix:
        # ERROR with traceback, not a transient-blip warning — while the
        # vendor itself still serves.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(
            monkeypatch,
            histories={"IBIT": [{"date": "garbage"}], "FBTC": HISTORIES_API["FBTC"]},
        )
        with caplog.at_level(logging.DEBUG, logger=SOSOVALUE_LOGGER):
            out = sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert "Issuer breakdown incomplete" in out
        assert any(r.levelno >= logging.ERROR for r in caplog.records if r.name == SOSOVALUE_LOGGER)

    def test_transient_fund_failure_stays_a_warning(self, tmp_path, monkeypatch, caplog):
        # The counterpart: an ordinary blip must not be escalated, or the
        # structural escalation stops meaning anything.
        self._setup(tmp_path, monkeypatch)
        _stub_requests(monkeypatch, fail={"/etfs/IBIT/history"})
        with caplog.at_level(logging.DEBUG, logger=SOSOVALUE_LOGGER):
            sosovalue.get_etf_flow_data("BTC", "2026-07-31")
        assert not any(
            r.levelno >= logging.ERROR for r in caplog.records if r.name == SOSOVALUE_LOGGER
        )


@pytest.mark.unit
class TestReadCache:
    def _write(self, tmp_path, payload):
        path = tmp_path / "sosovalue_btc.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def _valid_payload(self):
        return {
            "asset": "BTC",
            "fetched_at": "2026-07-31T00:00:00Z",
            "summary_rows": [
                {"date": "2026-07-31", "total_net_inflow": 1.0, "cum_net_inflow": 2.0}
            ],
            "funds": {"IBIT": {"name": "iShares", "rows": [_frow("2026-07-31", 1.0)]}},
            "funds_total": 1,
            "funds_failed": [],
            "funds_unusable": 0,
            "list_fetched": True,
            # The pair's "new" side equals the summary row's total at report
            # granularity (_usd_m(1.0 USD) == 0.0), as _diff_revisions writes.
            "revisions": {"2026-07-31": [1.0, 0.0]},
            "cum_restated": None,
        }

    def test_valid_payload_round_trips(self, tmp_path):
        payload = self._valid_payload()
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") == payload

    def test_name_at_the_hygiene_bound_is_accepted(self, tmp_path):
        # The writer truncates to exactly MAX_FUND_NAME_CHARS, so the reader
        # must accept that length — an off-by-one here would evict every
        # freshly-written cache whose name hit the bound.
        payload = self._valid_payload()
        payload["funds"]["IBIT"]["name"] = "x" * sosovalue.MAX_FUND_NAME_CHARS
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") == payload

    def test_valid_cum_restated_marker_round_trips(self, tmp_path):
        # A well-formed marker: date in the summary, "new" equal to that
        # row's cum at report granularity (_usd_m(2.0 USD) == 0.0), old
        # different.
        payload = self._valid_payload()
        payload["revisions"] = {}
        payload["cum_restated"] = {"date": "2026-07-31", "old": 1.0, "new": 0.0}
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") == payload

    def test_marker_coexisting_with_a_revision_on_its_date_round_trips(self, tmp_path):
        # Legal state: the day's flow was revised (disclosed) AND pre-window
        # history was restated — the marker fires on the unexplained
        # residual, so the validator must not treat the pair as exclusive.
        payload = self._valid_payload()  # revisions already carry 2026-07-31
        payload["cum_restated"] = {"date": "2026-07-31", "old": 1.0, "new": 0.0}
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") == payload

    def test_missing_cum_restated_is_a_miss(self, tmp_path):
        payload = self._valid_payload()
        del payload["cum_restated"]
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") is None

    def test_missing_file_is_a_silent_miss(self, tmp_path):
        assert sosovalue._read_cache(str(tmp_path / "sosovalue_btc.json"), "BTC") is None

    def test_rejections_are_logged_with_a_reason(self, tmp_path, caplog):
        path = tmp_path / "sosovalue_btc.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger=SOSOVALUE_LOGGER):
            assert sosovalue._read_cache(str(path), "BTC") is None
        assert "Ignoring SoSoValue cache" in caplog.text

    @pytest.mark.parametrize(
        "corrupt",
        [
            {"summary_rows": []},
            {"summary_rows": "oops"},
            {
                "summary_rows": [
                    {"date": "2026-07-31", "total_net_inflow": "N/A", "cum_net_inflow": 1.0}
                ]
            },
            {
                "summary_rows": [
                    {"date": "2026-7-31", "total_net_inflow": 1.0, "cum_net_inflow": 1.0}
                ]
            },
            {
                "summary_rows": [
                    {
                        "date": "2026-07-31",
                        "total_net_inflow": float("nan"),
                        "cum_net_inflow": 1.0,
                    }
                ]
            },
            {"funds": "oops"},
            {
                "funds": {"IBIT": {"rows": [{"date": "2026-07-31", "net_inflow": 1.0}]}}
            },  # name missing
            {
                "funds": {
                    "IBIT": {
                        "name": "x" * (sosovalue.MAX_FUND_NAME_CHARS + 1),
                        "rows": [{"date": "2026-07-31", "net_inflow": 1.0}],
                    }
                }
            },  # name over the cache-hygiene bound the writer enforces
            {
                "funds": {
                    "IBIT": {"name": "x", "rows": [{"date": "2026-07-31", "net_inflow": "N/A"}]}
                }
            },
            {"funds_total": True},
            {"funds_total": -1},
            {"funds_failed": "IBIT"},
            {"funds_failed": [1]},
            {"funds_unusable": True},
            {"funds_unusable": -1},
            {"funds_unusable": "0"},
            {"list_fetched": "yes"},
            {"list_fetched": 1},
            {"revisions": {"2026-07-31": [1.0]}},
            {"revisions": {"not-a-date": [1.0, 2.0]}},
            {"fetched_at": ""},
            # Cross-field invariants _fetch_all always writes: each field
            # below is individually well-shaped, so only a relationship
            # check can reject the payload.
            {"asset": "ETH"},  # copied/renamed cache file for another asset
            {"funds_total": 3},  # != fetched (1) + failed (0)
            {"funds_total": 2, "funds_failed": ["IBIT"]},  # ticker in both buckets
            {"funds_total": 3, "funds_failed": ["FBTC", "FBTC"]},  # repeated failure
            {"funds_total": 2, "funds_failed": ["BAD TICKER"]},  # implausible ticker
            {"funds": {"ib!t": {"name": "x", "rows": []}}},  # implausible ticker key
            {"list_fetched": False},  # breakdown populated despite no listing
            {
                "summary_rows": [
                    {"date": "2026-07-31", "total_net_inflow": 1.0, "cum_net_inflow": 2.0},
                    {"date": "2026-07-30", "total_net_inflow": 1.0, "cum_net_inflow": 2.0},
                ]
            },  # descending order
            {
                "summary_rows": [
                    {"date": "2026-07-31", "total_net_inflow": 1.0, "cum_net_inflow": 2.0},
                    {"date": "2026-07-31", "total_net_inflow": 1.0, "cum_net_inflow": 2.0},
                ]
            },  # duplicated date
            {"revisions": {"2026-07-31": [1.0, float("nan")]}},  # non-finite second element
            {"revisions": {"2026-07-30": [1.0, 0.0]}},  # date absent from summary_rows
            {"revisions": {"2026-07-31": [5.0, 2.0]}},  # "new" disagrees with the row
            {"revisions": {"2026-07-31": [0.0, 0.0]}},  # no-op pair
            {"cum_restated": "oops"},  # not a dict or null
            {"cum_restated": {"date": "2026-07-31", "old": 1.0}},  # "new" missing
            {"cum_restated": {"date": "not-a-date", "old": 1.0, "new": 0.0}},
            {"cum_restated": {"date": "2026-07-31", "old": float("nan"), "new": 0.0}},
            # Cross-field: date absent from the summary; no-op pair; "new"
            # disagreeing with the row's cum at report granularity.
            {"cum_restated": {"date": "2026-07-30", "old": 1.0, "new": 0.0}},
            {"cum_restated": {"date": "2026-07-31", "old": 0.0, "new": 0.0}},
            {"cum_restated": {"date": "2026-07-31", "old": 1.0, "new": 5.0}},
            {"funds": {"IBIT": {"name": "iShares", "rows": []}}},  # empty history rows
        ],
    )
    def test_malformed_field_is_a_miss(self, tmp_path, corrupt):
        payload = self._valid_payload()
        payload.update(corrupt)
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") is None

    def test_missing_fetched_at_is_a_miss(self, tmp_path):
        payload = self._valid_payload()
        del payload["fetched_at"]
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") is None

    def test_missing_asset_is_a_miss(self, tmp_path):
        payload = self._valid_payload()
        del payload["asset"]
        assert sosovalue._read_cache(self._write(tmp_path, payload), "BTC") is None


# --------------------------------------------------------------------------- #
# End-to-end against the full live-captured fixtures
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestEndToEndFixture:
    def test_full_render_from_live_fixtures(self, tmp_path, monkeypatch):
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.setenv("SOSOVALUE_API_KEY", "test-key")
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-08-04T02:00:00Z"))

        def _impl(path, params):
            if path == "/etfs/summary-history":
                return SUMMARY_FIX["data"]
            if path == "/etfs":
                return ETFS_FIX["data"]
            if _HISTORY_PATH_RE.match(path):
                # Every fund serves the captured IBIT history; per-fund
                # variation is covered by the synthetic tests above.
                return IBIT_FIX["data"]
            raise AssertionError(f"unexpected SoSoValue path {path}")

        monkeypatch.setattr(sosovalue, "_request", _impl)
        out = sosovalue.get_etf_flow_data("BTC", "2026-08-04")

        assert out.splitlines()[0] == "## Spot ETF Flows — BTC (SoSoValue, net US$m)"
        assert "**Latest (2026-07-31):** -265.4 net" in out
        assert "**Since-launch cumulative:** +51324.5" in out
        assert "(20 flow sessions in the window)" in out
        assert "**Streak:** 1-session outflow (2026-07-31 → 2026-07-31)" in out
        assert "| 2026-07-06 | +265.7 |" in out
        assert "IBIT -122.7" in out
        assert "**Breadth (2026-07-31):** 0 of 13" in out
        # Clean fetch, complete breakdown, fresh data: none of the
        # fetch-health caveats fire. The reconciliation caveat DOES: serving
        # the IBIT history for all 13 funds makes the fund sum 13 × -122.7 ≈
        # -1594.5 against the aggregate's -265.4, which is exactly the
        # listing-gap condition the check exists to disclose.
        for absent in ("STALE", "Issuer breakdown", "Window clamped", "Data lag", "Revision:"):
            assert absent not in out
        assert "Reconciliation gap: the 13 fund filings for 2026-07-31 sum to -1594.5" in out
        # The pending 2026-08-03 null row must not surface anywhere: the
        # aggregate stops at 07-31 and null fund rows are not $0 flows.
        assert "2026-08-03" not in out

    def test_lookahead_against_fixtures(self, tmp_path, monkeypatch):
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.setenv("SOSOVALUE_API_KEY", "test-key")
        monkeypatch.setattr(sosovalue, "_utc_now", lambda: _at("2026-08-04T02:00:00Z"))
        _stub_requests(
            monkeypatch,
            summary=SUMMARY_FIX["data"],
            listing=ETFS_FIX["data"],
            histories={e["ticker"]: IBIT_FIX["data"] for e in ETFS_FIX["data"]},
        )
        out = sosovalue.get_etf_flow_data("BTC", "2026-07-15", 5)
        assert "**Latest (2026-07-15):**" in out
        assert "2026-07-31" not in out
        assert "2026-07-16" not in out


# --------------------------------------------------------------------------- #
# Router integration: the sosovalue,farside chain
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouting:
    def test_default_chain_is_sosovalue_then_farside(self):
        assert (
            default_config.DEFAULT_CONFIG["data_vendors"]["crypto_etf_flows"] == "sosovalue,farside"
        )
        assert list(interface.VENDOR_METHODS["get_etf_flows"]) == ["sosovalue", "farside"]
        assert "sosovalue" in interface.VENDOR_LIST

    def test_primary_serves_without_touching_the_fallback(self):
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue,farside"}})
        called = {"farside": 0}

        def _farside(*a, **k):
            called["farside"] += 1
            return "FARSIDE"

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"sosovalue": lambda *a, **k: "SOSO_OK", "farside": _farside}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert out == "SOSO_OK"
        assert called["farside"] == 0

    def test_unconfigured_key_falls_through_to_farside(self):
        # The chain semantics behind the key escape hatch: unset key -> the
        # router logs "not configured" and serves Farside instead.
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue,farside"}})

        def _unconfigured(*a, **k):
            raise sosovalue.SoSoValueNotConfiguredError("SOSOVALUE_API_KEY not set")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_etf_flows": {
                    "sosovalue": _unconfigured,
                    "farside": lambda *a, **k: "FARSIDE_OK",
                }
            },
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert out == "FARSIDE_OK"

    def test_whole_chain_failing_degrades_to_the_sentinel(self):
        # sosovalue down + farside Cloudflare-blocked = today's worst case with
        # no key: the optional category degrades, the run continues.
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue,farside"}})

        def _soso_boom(*a, **k):
            raise sosovalue.SoSoValueError("SoSoValue unavailable")

        def _farside_boom(*a, **k):
            raise farside.FarsideError("Cloudflare challenge (403)")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"sosovalue": _soso_boom, "farside": _farside_boom}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert "DATA_UNAVAILABLE" in out
        assert "do not fabricate values" in out

    def test_rate_limited_primary_falls_through(self):
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue,farside"}})

        def _throttled(*a, **k):
            raise sosovalue.SoSoValueRateLimitError("429")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_etf_flows": {
                    "sosovalue": _throttled,
                    "farside": lambda *a, **k: "FARSIDE_OK",
                }
            },
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert out == "FARSIDE_OK"

    def test_rate_limit_only_chain_degrades_to_the_sentinel(self):
        # A chain whose only failure is an uncached 429 (here a single-vendor
        # chain with farside disabled) must degrade like any other optional-
        # category failure, not abort with the bare no-vendor RuntimeError.
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue"}})

        def _throttled(*a, **k):
            raise sosovalue.SoSoValueRateLimitError("429: too many requests")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_etf_flows": {
                    "sosovalue": _throttled,
                    "farside": lambda *a, **k: pytest.fail("farside is not in the chain"),
                }
            },
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert "DATA_UNAVAILABLE" in out
        assert "429" in out

    def test_a_real_error_outranks_the_rate_limit_in_the_sentinel(self):
        # The rate-limit is recorded only as a fallback: with a real error in
        # the chain (farside's), the sentinel surfaces that one.
        set_config({"data_vendors": {"crypto_etf_flows": "sosovalue,farside"}})

        def _throttled(*a, **k):
            raise sosovalue.SoSoValueRateLimitError("429: too many requests")

        def _farside_boom(*a, **k):
            raise farside.FarsideError("Cloudflare challenge (403)")

        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_etf_flows": {"sosovalue": _throttled, "farside": _farside_boom}},
            clear=False,
        ):
            out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-07-31", 30)
        assert "DATA_UNAVAILABLE" in out
        assert "Cloudflare" in out
        assert "429" not in out
