"""Alpha Vantage request hardening.

Regressions for #990 (no request timeout -> can hang) and #991 (invalid-key
responses mislabeled as rate limits and silently treated as transient), plus the
fundamentals look-ahead filter (curr_date normalization + undated-row handling).
"""

import json

import pytest

import tradingagents.dataflows.alpha_vantage_common as av
import tradingagents.dataflows.alpha_vantage_fundamentals as avf
import tradingagents.dataflows.alpha_vantage_indicator as avi
from tradingagents.dataflows.alpha_vantage_fundamentals import _filter_reports_by_date


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _patched_get(body, capture=None):
    def fake_get(url, params=None, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _FakeResponse(body)

    return fake_get


@pytest.mark.unit
def test_request_passes_timeout(monkeypatch):
    captured = {}
    monkeypatch.setattr(av.requests, "get", _patched_get("Date,Close\n2025-01-02,1.0", captured))
    av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    assert captured.get("timeout") == av.REQUEST_TIMEOUT  # #990


@pytest.mark.unit
def test_rate_limit_detected(monkeypatch):
    body = '{"Information": "Our standard API rate limit is 25 requests per day. ... your API key ..."}'
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageRateLimitError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_invalid_key_not_mislabeled_as_rate_limit(monkeypatch):
    # AV's invalid-key notice mentions "API key"; it must NOT be treated as a
    # (transient) rate limit, but surface as a real configuration error (#991).
    body = (
        '{"Information": "the parameter apikey is invalid or missing. '
        'Please claim your free API key on (https://www.alphavantage.co/support/#api-key)."}'
    )
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageNotConfiguredError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    with pytest.raises(av.AlphaVantageRateLimitError):  # sanity: rate-limit path still distinct
        monkeypatch.setattr(
            av.requests,
            "get",
            _patched_get('{"Note": "API call frequency is 5 calls per minute."}'),
        )
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_lookahead_filter_normalizes_non_zero_padded_curr_date():
    # A non-zero-padded curr_date ("2026-6-5") must be normalized before the
    # comparison: a raw lexical compare keeps future reports because
    # "2026-12-31" <= "2026-6-5" is True, defeating the look-ahead guard.
    result = {
        "quarterlyReports": [
            {"fiscalDateEnding": "2026-03-31"},  # before -> kept
            {"fiscalDateEnding": "2026-12-31"},  # after  -> must be dropped
        ]
    }
    out = _filter_reports_by_date(result, "2026-6-5")
    kept = [r["fiscalDateEnding"] for r in out["quarterlyReports"]]
    assert kept == ["2026-03-31"]


@pytest.mark.unit
def test_lookahead_filter_drops_undated_reports():
    # A report whose fiscalDateEnding is missing or unparseable cannot be proven
    # to be in the past, so it must be dropped, not silently kept.
    result = {
        "annualReports": [
            {"fiscalDateEnding": "2025-12-31"},  # valid, in the past -> kept
            {"fiscalDateEnding": ""},  # missing -> dropped
            {"foo": "bar"},  # no fiscalDateEnding key -> dropped
            {"fiscalDateEnding": "garbage"},  # unparseable -> dropped
        ]
    }
    out = _filter_reports_by_date(result, "2026-06-05")
    kept = [r.get("fiscalDateEnding") for r in out["annualReports"]]
    assert kept == ["2025-12-31"]


@pytest.mark.unit
def test_lookahead_filter_raises_on_unparseable_curr_date():
    # A present-but-unparseable curr_date must fail CLOSED (raise), not silently
    # return unfiltered reports: this backs a core fundamentals tool, so a broken
    # point-in-time bound leaking future data must be loud, matching farside /
    # fear_greed. ("2026-13-45" is a valid shape but an impossible date.)
    result = {
        "quarterlyReports": [
            {"fiscalDateEnding": "2025-01-01"},
            {"fiscalDateEnding": "2099-01-01"},
        ]
    }
    with pytest.raises(ValueError, match="look-ahead guard"):
        _filter_reports_by_date(result, "2026-13-45")


@pytest.mark.unit
def test_get_balance_sheet_filters_future_reports_through_the_real_path(monkeypatch):
    # Regression: the look-ahead filter must run on the REAL _make_api_request
    # return (a JSON *string*), not only when handed a dict. An earlier version
    # type-checked isinstance(result, dict) on this always-str value, so the guard
    # silently never fired in production while its dict-level unit tests passed.
    body = json.dumps(
        {
            "symbol": "AAPL",
            "quarterlyReports": [
                {"fiscalDateEnding": "2026-03-31", "totalAssets": "1"},  # past -> kept
                {"fiscalDateEnding": "2026-12-31", "totalAssets": "2"},  # future -> dropped
            ],
        }
    )
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    out = avf.get_balance_sheet("AAPL", curr_date="2026-06-05")
    parsed = json.loads(out)
    kept = [r["fiscalDateEnding"] for r in parsed["quarterlyReports"]]
    assert kept == ["2026-03-31"]


@pytest.mark.unit
def test_get_balance_sheet_without_curr_date_is_unfiltered(monkeypatch):
    # No point-in-time bound: the raw response text is returned untouched (not even
    # re-serialized), preserving the pre-filter behaviour for date-less callers.
    body = '{"symbol": "AAPL", "quarterlyReports": [{"fiscalDateEnding": "2099-01-01"}]}'
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    assert avf.get_balance_sheet("AAPL") == body


@pytest.mark.unit
def test_get_balance_sheet_returns_error_string_on_unparseable_curr_date(monkeypatch):
    # Fail-closed but graceful: fundamentals is a NON-optional category, so a
    # raised ValueError would escape route_to_vendor (raise first_error) and crash
    # the ToolNode-wrapped run. The getter instead returns a loud INVALID_CURR_DATE
    # sentinel (no data served, no future leak, LLM can retry), never raising.
    body = '{"symbol": "AAPL", "quarterlyReports": [{"fiscalDateEnding": "2099-01-01"}]}'
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    out = avf.get_balance_sheet("AAPL", curr_date="not-a-date")
    assert out.startswith("INVALID_CURR_DATE")
    assert "2099-01-01" not in out  # no future/unfiltered data leaked


@pytest.mark.unit
def test_every_supported_indicator_has_a_csv_column_mapping():
    # The blind "default to the second column" fallback is gone (#31): adding a
    # new indicator without a CSV column mapping must turn this red, not
    # silently render numbers from whatever column happens to be second.
    exempt = {"vwma"}  # returns early — Alpha Vantage has no VWMA endpoint
    unmapped = set(avi._SUPPORTED_INDICATORS) - exempt - set(avi._CSV_COLUMN_MAP)
    assert not unmapped, f"indicators lacking a CSV column mapping: {sorted(unmapped)}"


@pytest.mark.unit
def test_missing_column_mapping_fails_loud_instead_of_guessing(monkeypatch):
    # With the mapping removed, the old code would render the second CSV column
    # (here a bogus 999-valued one) as RSI values; now it must return an
    # explicit error string and no data lines.
    monkeypatch.delitem(avi._CSV_COLUMN_MAP, "rsi")
    csv_body = "time,bogus,RSI\n2026-01-02,999.0,55.0\n"
    monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: csv_body)
    out = avi.get_indicator("AAPL", "rsi", "2026-01-05", look_back_days=10)
    assert "no CSV column mapping" in out
    assert "999.0" not in out
    assert "55.0" not in out


@pytest.mark.unit
class TestCsvDateFilterFailsClosed:
    """_filter_csv_by_date_range must never return the unfiltered body when
    it cannot filter — that silently served out-of-range/future rows (#33)."""

    _CSV = "timestamp,close\n2026-01-02,1.0\n2099-01-01,2.0\n"

    def test_happy_path_drops_out_of_range_rows(self):
        out = av._filter_csv_by_date_range(self._CSV, "2026-01-01", "2026-01-05", symbol="AAPL")
        assert "2026-01-02" in out
        assert "2099-01-01" not in out

    def test_bad_end_date_raises_instead_of_returning_unfiltered(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError):
            av._filter_csv_by_date_range(self._CSV, "2026-01-01", "not-a-date", symbol="AAPL")

    def test_unparseable_csv_raises_instead_of_returning_unfiltered(self):
        from tradingagents.dataflows.errors import NoMarketDataError

        with pytest.raises(NoMarketDataError):
            av._filter_csv_by_date_range(
                "timestamp,close\ngarbage,1.0\n", "2026-01-01", "2026-01-05", symbol="AAPL"
            )

    def test_blank_body_passes_through(self):
        assert av._filter_csv_by_date_range("", "2026-01-01", "2026-01-05", symbol="AAPL") == ""


@pytest.mark.unit
def test_global_news_clamps_untrusted_sizes(monkeypatch):
    # Oversized limit / lookback must be clamped before parameterizing the
    # external request (#33).
    import tradingagents.dataflows.alpha_vantage_news as avn

    captured = {}

    def fake_request(function_name, params):
        captured.update(params)
        return "{}"

    monkeypatch.setattr(avn, "_make_api_request", fake_request)
    avn.get_global_news("2026-06-05", look_back_days=99999, limit=99999)
    assert captured["limit"] == str(avn.MAX_NEWS_LIMIT)
    assert captured["time_from"] == "20250605T0000"  # exactly 365 days back


@pytest.mark.unit
def test_global_news_none_optionals_resolve_to_defaults_before_clamp(monkeypatch):
    # The tool wrapper forwards omitted optionals as explicit None through the
    # router; they must resolve to the documented defaults instead of crashing
    # in int(None) (review round 1).
    import tradingagents.dataflows.alpha_vantage_news as avn

    captured = {}

    def fake_request(function_name, params):
        captured.update(params)
        return "{}"

    monkeypatch.setattr(avn, "_make_api_request", fake_request)
    avn.get_global_news("2026-06-05", look_back_days=None, limit=None)
    assert captured["limit"] == "50"
    assert captured["time_from"] == "20260529T0000"  # default 7-day lookback


# ---------------------------------------------------------------------------
# Freshness annotations (#30)


@pytest.mark.unit
def test_indicator_lag_note_when_series_stalls(monkeypatch):
    # Newest surviving row 2026-05-02 vs curr_date 2026-06-01 (> 7 days).
    csv = "time,RSI\n2026-05-01,55.0\n2026-05-02,56.0"
    monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: csv)
    out = avi.get_indicator("AAPL", "rsi", "2026-06-01", 60)
    assert "Data lag" in out
    assert "2026-05-02" in out


@pytest.mark.unit
def test_indicator_no_note_on_fresh_series(monkeypatch):
    csv = "time,RSI\n2026-05-29,55.0\n2026-05-30,56.0"
    monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: csv)
    out = avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)
    assert "Data lag" not in out


@pytest.mark.unit
def test_indicator_bound_is_interval_aware(monkeypatch):
    # A monthly bar ~31 days back is on-cadence, not stale; the same gap on
    # the daily interval must note. Flat 7-day bound would false-alarm every
    # monthly call.
    csv = "time,RSI\n2026-04-01,55.0\n2026-05-01,56.0"
    monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: csv)
    monthly = avi.get_indicator("AAPL", "rsi", "2026-06-01", 180, interval="monthly")
    assert "Data lag" not in monthly
    daily = avi.get_indicator("AAPL", "rsi", "2026-06-01", 180, interval="daily")
    assert "Data lag" in daily


@pytest.mark.unit
def test_indicator_unknown_interval_never_notes(monkeypatch):
    # Same must-not-false-alarm rule as fred's frequency map: an unmapped
    # cadence gets no note even when the newest row is far back.
    csv = "time,RSI\n2026-01-01,55.0"
    monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: csv)
    out = avi.get_indicator("AAPL", "rsi", "2026-06-01", 365, interval="60min")
    assert "Data lag" not in out


@pytest.mark.unit
def test_indicator_rate_limit_propagates_instead_of_reading_as_success(monkeypatch):
    # #60: a 429 used to be caught by the broad handler and returned as
    # "Error retrieving rsi data: ...", which route_to_vendor reads as a
    # successful string — the rate-limit lane and every fallback vendor were
    # unreachable once Alpha Vantage's daily quota was spent.
    def _rate_limited(*a, **k):
        raise av.AlphaVantageRateLimitError("Alpha Vantage rate limit exceeded: 25/day")

    monkeypatch.setattr(avi, "_make_api_request", _rate_limited)
    with pytest.raises(av.AlphaVantageRateLimitError):
        avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)


@pytest.mark.unit
def test_indicator_not_configured_still_propagates(monkeypatch):
    # The pre-existing re-raise must survive being widened to the base type.
    def _unconfigured(*a, **k):
        raise av.AlphaVantageNotConfiguredError("ALPHA_VANTAGE_API_KEY is not set")

    monkeypatch.setattr(avi, "_make_api_request", _unconfigured)
    with pytest.raises(av.AlphaVantageNotConfiguredError):
        avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)


@pytest.mark.unit
def test_indicator_untyped_failure_still_degrades_to_an_error_string(monkeypatch):
    # Only the vendor-error taxonomy propagates; an unexpected failure keeps the
    # old degrade-to-string behavior so one broken indicator can't abort a run.
    def _boom(*a, **k):
        raise RuntimeError("socket exploded")

    monkeypatch.setattr(avi, "_make_api_request", _boom)
    out = avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)
    assert out.startswith("Error retrieving rsi data")


_DAILY_CSV = (
    "timestamp,open,high,low,close,adjusted_close,volume\n"
    "2026-05-01,10,11,9,10.5,10.5,1000\n"
    "2026-05-02,10.5,12,10,11.0,11.0,1200"
)


@pytest.mark.unit
def test_stock_all_rows_filtered_out_raises_no_market_data(monkeypatch):
    # A header-only CSV read as a successful fetch is the dishonest legacy
    # behavior; the router needs a typed error to fall back / emit the
    # sentinel (#30, mirroring the yfinance empty-frame raise).
    import tradingagents.dataflows.alpha_vantage_stock as avs
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setattr(avs, "_make_api_request", lambda *a, **k: _DAILY_CSV)
    with pytest.raises(NoMarketDataError):
        avs.get_stock("AAPL", "2026-07-01", "2026-07-31")


@pytest.mark.unit
def test_stock_blank_body_raises_no_market_data(monkeypatch):
    # An empty body passes through the range filter untouched; it must surface
    # as no data, not render as an empty success.
    import tradingagents.dataflows.alpha_vantage_stock as avs
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setattr(avs, "_make_api_request", lambda *a, **k: "")
    with pytest.raises(NoMarketDataError):
        avs.get_stock("AAPL", "2026-07-01", "2026-07-31")


@pytest.mark.unit
def test_stock_stale_rows_raise_like_the_yfinance_path(monkeypatch):
    # Rows exist in range but the newest (2026-05-02) trails end_date by more
    # than MAX_STOCK_LAG_DAYS. The yfinance path raises on this exact gap
    # (_assert_ohlcv_not_stale); an annotated success here would let a stalled
    # Alpha Vantage feed short-circuit the vendor chain, so this path must
    # raise too.
    import tradingagents.dataflows.alpha_vantage_stock as avs
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setattr(avs, "_make_api_request", lambda *a, **k: _DAILY_CSV)
    with pytest.raises(NoMarketDataError) as exc:
        avs.get_stock("AAPL", "2026-04-25", "2026-05-30")
    assert "stale" in str(exc.value)
    assert "2026-05-02" in str(exc.value)


@pytest.mark.unit
def test_stock_fresh_rows_pass_through_unchanged(monkeypatch):
    import tradingagents.dataflows.alpha_vantage_stock as avs

    monkeypatch.setattr(avs, "_make_api_request", lambda *a, **k: _DAILY_CSV)
    out = avs.get_stock("AAPL", "2026-04-25", "2026-05-05")
    assert "2026-05-01" in out and "2026-05-02" in out
    assert "Data lag" not in out


@pytest.mark.unit
def test_stock_staleness_bound_matches_yfinance_bound():
    # The two market-data paths must reject the same gap. Pinned by test
    # instead of an import because stockstats_utils drags yfinance/stockstats
    # into what is otherwise a pure-requests vendor module.
    import tradingagents.dataflows.alpha_vantage_stock as avs
    import tradingagents.dataflows.stockstats_utils as ssu

    assert avs.MAX_STOCK_LAG_DAYS == ssu.MAX_OHLCV_STALE_DAYS


# ---------------------------------------------------------------------------
# Fundamentals freshness disclosures (#58): the same routed tool must be as
# honest through Alpha Vantage as it is through yfinance.

_OVERVIEW = json.dumps({"Symbol": "AAPL", "MarketCapitalization": "3000000000"})


def _patch_av_request(monkeypatch, body):
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)


def _note_of(out: str) -> str:
    """The freshness note carried by a rendered Alpha Vantage payload, or ""."""
    return json.loads(out).get(avf._FRESHNESS_NOTE_KEY, "")


@pytest.mark.unit
def test_overview_backtest_date_discloses_live_values(monkeypatch):
    # AV OVERVIEW is a current-state snapshot with no historical form, exactly
    # like yfinance `info`: rendered for a past analysis date it must say the
    # numbers are live as of the fetch, or the agent reads today's market cap
    # as that date's.
    _patch_av_request(monkeypatch, _OVERVIEW)
    out = avf.get_fundamentals("AAPL", "2020-01-01")
    assert "live values" in _note_of(out)
    assert json.loads(out)["MarketCapitalization"] == "3000000000"  # data still rendered


@pytest.mark.unit
def test_overview_current_date_carries_no_note(monkeypatch):
    from datetime import date

    _patch_av_request(monkeypatch, _OVERVIEW)
    out = avf.get_fundamentals("AAPL", date.today().strftime("%Y-%m-%d"))
    assert out == _OVERVIEW  # untouched, not re-serialized


@pytest.mark.unit
def test_overview_without_curr_date_is_untouched(monkeypatch):
    _patch_av_request(monkeypatch, _OVERVIEW)
    assert avf.get_fundamentals("AAPL") == _OVERVIEW


@pytest.mark.unit
def test_overview_non_json_body_is_untouched(monkeypatch):
    # An error/notice page has nowhere to put a key; annotating must degrade to
    # returning the body rather than raising inside a disclosure helper.
    body = "Thank you for using Alpha Vantage!"
    _patch_av_request(monkeypatch, body)
    assert avf.get_fundamentals("AAPL", "2020-01-01") == body


@pytest.mark.unit
def test_overview_empty_payload_is_not_dressed_with_a_note(monkeypatch):
    # AV answers an unknown symbol with "{}"; a lone disclosure key would imply
    # data that is not there.
    _patch_av_request(monkeypatch, "{}")
    assert avf.get_fundamentals("AAPL", "2020-01-01") == "{}"


@pytest.mark.unit
@pytest.mark.parametrize("key", ["Error Message", "Information", "Note"])
def test_overview_error_envelope_is_not_dressed_with_a_note(monkeypatch, key):
    # A rejected call answers a JSON envelope, not fundamentals. It parses as a
    # non-empty dict, so the "{}" guard alone would let the disclosure through —
    # and "these fundamentals are live values as of the fetch" beside an error
    # message asserts a fetch that never returned anything.
    body = json.dumps({key: "Invalid API call."})
    _patch_av_request(monkeypatch, body)
    assert avf.get_fundamentals("AAPL", "2020-01-01") == body


@pytest.mark.unit
def test_overview_notice_beside_real_fields_is_still_disclosed(monkeypatch):
    # The envelope guard keys on "nothing BUT notice keys": a payload that also
    # carries fundamentals must keep its disclosure.
    body = json.dumps({"Note": "delayed", "Symbol": "AAPL", "MarketCapitalization": "1"})
    _patch_av_request(monkeypatch, body)
    assert "live values" in _note_of(avf.get_fundamentals("AAPL", "2020-01-01"))


def _statement_body(*, quarterly=(), annual=()):
    return json.dumps(
        {
            "symbol": "AAPL",
            "quarterlyReports": [{"fiscalDateEnding": d, "totalAssets": "1"} for d in quarterly],
            "annualReports": [{"fiscalDateEnding": d, "totalAssets": "1"} for d in annual],
        }
    )


_STATEMENT_GETTERS = [
    ("get_balance_sheet", "balance sheet period"),
    ("get_cashflow", "cash flow period"),
    ("get_income_statement", "income statement period"),
]


@pytest.mark.unit
@pytest.mark.parametrize("getter,phrase", _STATEMENT_GETTERS)
def test_statement_stale_quarterly_carries_note(monkeypatch, getter, phrase):
    # Newest surviving period 2025-01-31 vs analysis date 2026-08-18 (> 180d).
    _patch_av_request(monkeypatch, _statement_body(quarterly=["2025-01-31"]))
    note = _note_of(getattr(avf, getter)("AAPL", "quarterly", "2026-08-18"))
    assert f"quarterly {phrase}" in note
    assert "2025-01-31" in note


@pytest.mark.unit
def test_statement_normal_cadence_has_no_note(monkeypatch):
    # 49 days behind is a freshly filed quarter, not a stall.
    _patch_av_request(monkeypatch, _statement_body(quarterly=["2026-06-30"]))
    assert _note_of(avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")) == ""


@pytest.mark.unit
def test_statement_annual_bound_tolerates_a_year_old_filing(monkeypatch):
    # An annual statement is ~a year old by definition; the quarterly bound
    # would flag every annual call.
    _patch_av_request(monkeypatch, _statement_body(annual=["2025-09-27"]))
    assert _note_of(avf.get_balance_sheet("AAPL", "annual", "2026-08-18")) == ""


@pytest.mark.unit
def test_statement_note_reflects_newest_surviving_period(monkeypatch):
    # The look-ahead filter drops the future report first; the note must
    # describe the newest period the agent actually sees, not the raw newest.
    _patch_av_request(monkeypatch, _statement_body(quarterly=["2025-01-31", "2027-01-31"]))
    note = _note_of(avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18"))
    assert "2025-01-31" in note
    assert "2027-01-31" not in note


@pytest.mark.unit
def test_statement_note_follows_the_requested_freq(monkeypatch):
    # Alpha Vantage returns BOTH lists on every call, so the note has to pick
    # the one the caller's freq selects — reading the wrong list here would
    # flag a healthy quarterly filer (or stay silent on a dead annual one).
    body = _statement_body(quarterly=["2026-06-30"], annual=["2023-12-31"])
    _patch_av_request(monkeypatch, body)
    assert _note_of(avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")) == ""
    annual_note = _note_of(avf.get_balance_sheet("AAPL", "annual", "2026-08-18"))
    assert "2023-12-31" in annual_note
    # Both lists ride along in the payload, so the note must name the cadence it
    # judged — unqualified, it reads as a verdict on the 49-day-old quarterly
    # balance sheet sitting in the same JSON.
    assert "annual balance sheet period" in annual_note


@pytest.mark.unit
def test_statement_unparseable_periods_degrade_to_no_note(monkeypatch):
    # Undated rows are dropped by the look-ahead filter; with nothing datable
    # left the annotation stays silent instead of guessing.
    _patch_av_request(monkeypatch, _statement_body(quarterly=["not-a-date"]))
    out = avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    assert _note_of(out) == ""
    assert json.loads(out)["quarterlyReports"] == []
