"""Alpha Vantage request hardening.

Regressions for #990 (no request timeout -> can hang) and #991 (invalid-key
responses mislabeled as rate limits and silently treated as transient), plus the
fundamentals look-ahead filter (curr_date normalization + undated-row handling).
"""
import pytest

import tradingagents.dataflows.alpha_vantage_common as av
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
def test_lookahead_filter_leaves_data_untouched_on_unparseable_curr_date():
    # An unparseable curr_date cannot bound anything; the filter leaves the data
    # untouched (matching the no-curr_date path) rather than dropping everything.
    result = {"quarterlyReports": [{"fiscalDateEnding": "2026-03-31"}]}
    out = _filter_reports_by_date(result, "not-a-date")
    assert out["quarterlyReports"] == [{"fiscalDateEnding": "2026-03-31"}]
