"""Alpha Vantage request hardening.

Regressions for #990 (no request timeout -> can hang) and #991 (invalid-key
responses mislabeled as rate limits and silently treated as transient), plus the
fundamentals look-ahead filter (curr_date normalization + undated-row handling).
"""
import json

import pytest

import tradingagents.dataflows.alpha_vantage_common as av
import tradingagents.dataflows.alpha_vantage_fundamentals as avf
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
    body = ('{"Information": "the parameter apikey is invalid or missing. '
            'Please claim your free API key on (https://www.alphavantage.co/support/#api-key)."}')
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageNotConfiguredError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    with pytest.raises(av.AlphaVantageRateLimitError):  # sanity: rate-limit path still distinct
        monkeypatch.setattr(av.requests, "get", _patched_get('{"Note": "API call frequency is 5 calls per minute."}'))
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
