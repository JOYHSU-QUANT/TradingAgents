"""Alpha Vantage request hardening.

Regressions for #990 (no request timeout -> can hang) and #991 (invalid-key
responses mislabeled as rate limits and silently treated as transient), plus the
fundamentals look-ahead filter (curr_date normalization + undated-row handling).
"""

import json
import logging

import pytest
import requests

import tradingagents.dataflows.alpha_vantage_common as av
import tradingagents.dataflows.alpha_vantage_fundamentals as avf
import tradingagents.dataflows.alpha_vantage_indicator as avi
import tradingagents.dataflows.alpha_vantage_news as avn
from tradingagents.dataflows.alpha_vantage_fundamentals import _filter_reports_by_date
from tradingagents.dataflows.errors import NoMarketDataError


class _FakeResponse:
    def __init__(self, text, status_code=200, headers=None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error")


def _patched_get(body, capture=None, status_code=200, headers=None):
    def fake_get(url, params=None, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _FakeResponse(body, status_code, headers)

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
def test_premium_endpoint_is_an_entitlement_verdict_not_a_throttle(monkeypatch):
    # A free key asking a premium-only endpoint gets a "premium endpoint"
    # notice: an entitlement the key lacks, not a throttle it will outlive.
    # Classified as a rate limit it would now keep every free Alpha Vantage
    # endpoint unasked for the router's latch window (#114). The daily-quota
    # notice also mentions the premium plans and must stay a rate limit.
    premium = (
        '{"Information": "Thank you for using Alpha Vantage! This is a premium endpoint. '
        'You may subscribe to any of the premium plans to instantly unlock all premium endpoints"}'
    )
    monkeypatch.setattr(av.requests, "get", _patched_get(premium))
    with pytest.raises(av.AlphaVantageNotConfiguredError, match="premium endpoint"):
        av._make_api_request("TIME_SERIES_DAILY_ADJUSTED", {"symbol": "AAPL"})

    quota = (
        '{"Information": "our standard API rate limit is 25 requests per day. Please subscribe '
        'to any of the premium plans to instantly remove all daily rate limits."}'
    )
    monkeypatch.setattr(av.requests, "get", _patched_get(quota))
    with pytest.raises(av.AlphaVantageRateLimitError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})

    # Any other premium-flavoured refusal still raises rather than coming back
    # to the caller as data — the bare substring was the catch-all before.
    other = '{"Information": "The entitlement parameter requires a premium membership."}'
    monkeypatch.setattr(av.requests, "get", _patched_get(other))
    with pytest.raises(av.AlphaVantageNotConfiguredError, match="premium-only"):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_http_429_is_classified_as_a_rate_limit(monkeypatch):
    # #72: a status-code throttle used to become a bare requests.HTTPError via
    # raise_for_status() — outside the taxonomy, so it fell into each caller's
    # broad except and never reached the router's rate-limit lane. The body
    # notice checks read only HTTP 200 answers, so this is the only place an
    # HTTP 429 can be classified.
    monkeypatch.setattr(av.requests, "get", _patched_get("", status_code=429))
    with pytest.raises(av.AlphaVantageRateLimitError) as exc:
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    assert "HTTP 429" in str(exc.value)


@pytest.mark.unit
def test_http_429_reports_retry_after_when_present(monkeypatch):
    monkeypatch.setattr(
        av.requests, "get", _patched_get("", status_code=429, headers={"Retry-After": "42"})
    )
    with pytest.raises(av.AlphaVantageRateLimitError) as exc:
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    assert "Retry-After: 42" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 500])
def test_other_http_errors_keep_their_requests_behaviour(monkeypatch, status):
    # #72 deliberately narrows to 429: any other 4xx/5xx keeps raising the
    # requests.HTTPError callers already handle.
    monkeypatch.setattr(av.requests, "get", _patched_get("", status_code=status))
    with pytest.raises(requests.HTTPError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_error_message_envelope_raises_no_market_data(monkeypatch):
    # #68: an "Error Message" body used to return to callers as text that read
    # like a successful answer, so the router never fell back. It now raises
    # the no-data type at the request boundary, with the vendor's own wording
    # riding along so a parameter mistake stays distinguishable in the sentinel.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"Error Message": "Invalid API call. Please retry."})
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(NoMarketDataError) as exc:
        av._make_api_request("OVERVIEW", {"symbol": "AAPL"})
    msg = str(exc.value)
    assert "AAPL" in msg
    assert "Invalid API call" in msg


@pytest.mark.unit
def test_error_message_envelope_names_the_tickers_param_when_no_symbol(monkeypatch):
    # The news endpoints address instruments through "tickers"; the raise must
    # still be attributed to a real subject rather than a blank.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"Error Message": "Invalid API call."})
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(NoMarketDataError) as exc:
        av._make_api_request("NEWS_SENTIMENT", {"tickers": "AAPL"})
    assert "AAPL" in str(exc.value)


@pytest.mark.unit
def test_error_message_envelope_uses_the_callers_subject_when_named(monkeypatch):
    # A request addressed by neither symbol nor tickers (global news uses
    # topics) would fall back to the function name — prose the router's
    # sentinel then presents as a tradable symbol. The caller names the
    # subject instead.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"Error Message": "Invalid API call."})
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(NoMarketDataError) as exc:
        av._make_api_request(
            "NEWS_SENTIMENT", {"topics": "economy_macro"}, subject="global market news"
        )
    assert "global market news" in str(exc.value)


@pytest.mark.unit
def test_global_news_rejection_is_attributed_to_its_named_subject(monkeypatch):
    # Through the REAL get_global_news call site: it must pass its subject to
    # the boundary. The direct _make_api_request test above proves the kwarg
    # works, not that the caller wires it — dropping the argument would fall
    # back to "NEWS_SENTIMENT" with every other test still green.
    import tradingagents.dataflows.alpha_vantage_news as avn
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"Error Message": "Invalid API call."})
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(NoMarketDataError) as exc:
        avn.get_global_news("2026-06-01")
    assert "global market news" in str(exc.value)
    assert "NEWS_SENTIMENT'" not in str(exc.value).split(":", 1)[0]


@pytest.mark.unit
def test_rate_limit_notice_outranks_an_error_message_rider(monkeypatch):
    # Order pin: a body carrying both a throttle notice and an "Error Message"
    # keeps the more actionable rate-limit verdict.
    body = json.dumps(
        {"Note": "API call frequency is 5 calls per minute.", "Error Message": "Invalid API call."}
    )
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(av.AlphaVantageRateLimitError):
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})


@pytest.mark.unit
def test_blank_error_message_still_raises(monkeypatch):
    # Keyed on presence, not truthiness: a rejection with blank wording is
    # still a rejection, not data.
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setattr(av.requests, "get", _patched_get(json.dumps({"Error Message": ""})))
    with pytest.raises(NoMarketDataError):
        av._make_api_request("OVERVIEW", {"symbol": "AAPL"})


@pytest.mark.unit
@pytest.mark.parametrize("body", ["null", "[]", '"throttled"'])
def test_non_object_json_bodies_are_classified_as_no_data(monkeypatch, body):
    # No AV data path serves a non-object JSON body, and the classification
    # reads keys. Classifying keeps the router's fallback (the pre-#68
    # behavior here was an AttributeError into the router's broad handler)
    # without serving 'null' to the agent as a report, and without crashing.
    from tradingagents.dataflows.errors import NoMarketDataError

    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    with pytest.raises(NoMarketDataError) as exc:
        av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"})
    assert "non-object JSON" in str(exc.value)


@pytest.mark.unit
def test_non_string_notice_does_not_crash_classification(monkeypatch):
    # No such shape has been observed; the pin is that an unclassifiable
    # notice degrades to a served body rather than an AttributeError.
    body = json.dumps({"Information": {"code": 429}})
    monkeypatch.setattr(av.requests, "get", _patched_get(body))
    assert av._make_api_request("TIME_SERIES_DAILY", {"symbol": "AAPL"}) == body


@pytest.mark.unit
def test_envelope_key_list_has_one_definition():
    # #68: the AV problem-key vocabulary must not fork between the request
    # boundary and the fundamentals disclosure guard.
    assert avf._AV_ENVELOPE_KEYS is av._AV_ENVELOPE_KEYS


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
    # No point-in-time bound means no look-ahead filtering — deliberately (#73):
    # with no bound there is nothing to filter against, and dropping rows anyway
    # would fake a protection this call cannot have. The freshness disclosure is
    # handled separately by the wall-clock fallback (next test); this fixture's
    # period postdates today, so no note is due and the raw response text is
    # returned untouched (not even re-serialized).
    body = '{"symbol": "AAPL", "quarterlyReports": [{"fiscalDateEnding": "2099-01-01"}]}'
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    assert avf.get_balance_sheet("AAPL") == body


@pytest.mark.unit
def test_statement_without_curr_date_still_discloses_staleness(monkeypatch, caplog):
    # The model omitting curr_date used to switch off BOTH protections silently
    # (#73). Filtering stays off (see above), but the lag note survives on a
    # wall-clock reference — the insider path's design — and the degraded mode
    # is logged. The body keeps both report lists: unfiltered means unfiltered.
    body = json.dumps(
        {
            "symbol": "AAPL",
            "quarterlyReports": [{"fiscalDateEnding": "2020-03-31"}],
            "annualReports": [{"fiscalDateEnding": "2019-12-31"}],
        }
    )
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    with caplog.at_level(logging.WARNING, logger=avf.__name__):
        out = avf.get_balance_sheet("AAPL")
    parsed = json.loads(out)
    assert "Data lag" in parsed[avf._FRESHNESS_NOTE_KEY]
    assert "2020-03-31" in parsed[avf._FRESHNESS_NOTE_KEY]
    assert "annualReports" in parsed  # unfiltered: both lists survive
    assert any("without curr_date" in r.message for r in caplog.records)


@pytest.mark.unit
@pytest.mark.parametrize("key", ["Error Message", "Information", "Note"])
def test_statement_envelope_without_curr_date_is_not_dressed(monkeypatch, key):
    # Ordering pin: the envelope exit outranks the date-less fallback. An
    # envelope that slips past the request boundary (#68) with no curr_date
    # must be served as the failure body it is — not judged against the wall
    # clock and dressed in a freshness disclosure.
    body = json.dumps({key: "Invalid API call."})
    monkeypatch.setattr(avf, "_make_api_request", lambda function_name, params: body)
    assert avf.get_balance_sheet("AAPL") == body


@pytest.mark.unit
def test_statement_without_curr_date_tolerates_malformed_rows(monkeypatch):
    # The date-less fallback judges RAW vendor rows — no look-ahead filter has
    # vetted them as mappings. A scalar row must degrade to no note, not crash
    # the getter into the router's broad handler.
    body = json.dumps({"symbol": "AAPL", "quarterlyReports": ["2020-03-31", {"x": 1}]})
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
@pytest.mark.parametrize(
    "registry", ["_CSV_COLUMN_MAP", "_INDICATOR_REQUESTS", "_INDICATOR_DESCRIPTIONS"]
)
def test_the_supported_indicators_and_each_wiring_registry_cover_the_same_set(registry):
    # Three wiring invariants in one shape (#31, #106, #117). An indicator
    # supported but absent from a registry used to answer an "Error: ..." string
    # the router reads as a report — the CSV-column half would previously have
    # rendered whatever column happened to be second as RSI values, and the
    # description half a "No description available." placeholder. Set EQUALITY,
    # not subtraction: an entry added to a registry alone is drift too, and the
    # subtraction form this replaced could not see it.
    assert set(getattr(avi, registry)) | avi._NO_ENDPOINT_INDICATORS == set(
        avi._SUPPORTED_INDICATORS
    )


@pytest.mark.unit
def test_an_unsupported_indicator_is_the_caller_mistake_type(monkeypatch):
    # The wrapper renders exactly this type as report text (#117); a plain
    # ValueError would now reach the ToolNode as a failure instead. Raised
    # before any request.
    from tradingagents.dataflows.errors import UnsupportedIndicatorError

    monkeypatch.setattr(
        avi, "_make_api_request", lambda *a, **k: pytest.fail("no request may be made")
    )
    with pytest.raises(UnsupportedIndicatorError, match="not supported"):
        avi.get_indicator("AAPL", "bogus", "2026-06-01", 30)


# Independently transcribed from the elif ladder the dispatch table replaced
# (`origin/hyperliquid-adapter`), with the caller's time_period written out as
# the 9 the test below passes. Deriving these from `_INDICATOR_REQUESTS` would
# make that table its own witness: a transcription slip such as
# `close_50_sma -> ("SMA", "20")` renders a 20-period average under a
# `## CLOSE_50_SMA` header, and a test reading the same entry stays green
# (measured — that mutation passed the whole suite before this table existed).
_LADDER_REQUESTS = {
    "close_50_sma": ("SMA", {"time_period": "50", "series_type": "close"}),
    "close_200_sma": ("SMA", {"time_period": "200", "series_type": "close"}),
    "close_10_ema": ("EMA", {"time_period": "10", "series_type": "close"}),
    "macd": ("MACD", {"series_type": "close"}),
    "macds": ("MACD", {"series_type": "close"}),
    "macdh": ("MACD", {"series_type": "close"}),
    "rsi": ("RSI", {"time_period": "9", "series_type": "close"}),
    "boll": ("BBANDS", {"time_period": "20", "series_type": "close"}),
    "boll_ub": ("BBANDS", {"time_period": "20", "series_type": "close"}),
    "boll_lb": ("BBANDS", {"time_period": "20", "series_type": "close"}),
    "atr": ("ATR", {"time_period": "9"}),  # the one entry that sends no series_type
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "indicator", sorted(set(avi._SUPPORTED_INDICATORS) - avi._NO_ENDPOINT_INDICATORS)
)
def test_each_indicator_issues_the_request_the_elif_ladder_used_to(monkeypatch, indicator):
    # Exactly one call, and the whole params dict compared — so a parameter
    # dropped, added or renamed by the refactor is caught too. Parametrized over
    # the supported set rather than over _LADDER_REQUESTS: a new indicator
    # reaches this as a KeyError, which is the drift lock in the other
    # direction. It does NOT pin that an entry names the right Alpha Vantage
    # endpoint — nothing in this repo can check that — only that today's request
    # is the one the ladder made.
    calls = []

    def _capture(function_name, params):
        calls.append((function_name, params))
        return "time,X\n"  # header-only: the request is what this pins

    monkeypatch.setattr(avi, "_make_api_request", _capture)
    with pytest.raises(NoMarketDataError):
        avi.get_indicator("AAPL", indicator, "2026-06-01", 30, time_period=9)

    expected_function, expected_params = _LADDER_REQUESTS[indicator]
    assert calls == [
        (
            expected_function,
            {"symbol": "AAPL", "interval": "daily", "datatype": "csv", **expected_params},
        )
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    "registry,expected",
    [
        ("_CSV_COLUMN_MAP", "no CSV column mapping"),
        ("_INDICATOR_REQUESTS", "no Alpha Vantage request"),
        ("_INDICATOR_DESCRIPTIONS", "no description"),
    ],
)
def test_a_wiring_gap_raises_before_any_request(monkeypatch, registry, expected):
    # Any of these gaps used to come back as an "Error: ..." string the router
    # accepts as a report — the missing-column one would previously have
    # rendered whatever column happened to be second as RSI values (#31), and
    # the missing-description one a "No description available." placeholder
    # from a function-local dict nothing tested (#117). All are our own wiring
    # bugs, not vendor conditions, so they raise before a request is made
    # rather than after paying for one.
    monkeypatch.delitem(getattr(avi, registry), "rsi")
    monkeypatch.setattr(
        avi, "_make_api_request", lambda *a, **k: pytest.fail("no request may be made")
    )
    with pytest.raises(ValueError, match=expected):
        avi.get_indicator("AAPL", "rsi", "2026-01-05", look_back_days=10)


@pytest.mark.unit
class TestIndicatorStructuralFailuresAreNotReports:
    """Alpha Vantage answers this getter carries no usable indicator rows (#106).

    Each of these used to ``return`` prose that ``route_to_vendor`` reads as a
    successful answer — the chain stopped at the vendor that had just failed to
    answer and the agent analysed the sentence as an indicator report. They now
    raise ``NoMarketDataError``, the same lane the vendor's own daily-bars
    getter takes for a header-only CSV (#30).
    """

    def _indicator(self, monkeypatch, body):
        monkeypatch.setattr(avi, "_make_api_request", lambda *a, **k: body)
        return avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)

    @pytest.mark.parametrize(
        "body",
        ["", "   ", "time,RSI", "time,RSI\n"],
        ids=["blank", "whitespace", "header-only", "header-and-newline"],
    )
    def test_a_body_with_no_rows_is_no_data(self, monkeypatch, body):
        with pytest.raises(NoMarketDataError) as exc:
            self._indicator(monkeypatch, body)
        assert "carried no data beyond its header" in str(exc.value)

    def test_a_missing_time_column_names_the_columns_it_did_get(self, monkeypatch):
        with pytest.raises(NoMarketDataError) as exc:
            self._indicator(monkeypatch, "date,RSI\n2026-05-30,55.0\n")
        assert "no 'time' column" in str(exc.value)
        assert "'date'" in str(exc.value)  # what arrived, so the drift is diagnosable

    def test_a_missing_value_column_names_the_one_it_wanted(self, monkeypatch):
        with pytest.raises(NoMarketDataError) as exc:
            self._indicator(monkeypatch, "time,Relative Strength\n2026-05-30,55.0\n")
        assert "no 'RSI' column" in str(exc.value)

    def test_rows_only_outside_the_window_are_no_data_not_an_empty_report(self, monkeypatch):
        # The most concealed of the four: the range filter kept nothing, and the
        # sentence went inside a well-formed "## RSI values from ... to ..."
        # header with no error wording anywhere in it.
        with pytest.raises(NoMarketDataError) as exc:
            self._indicator(monkeypatch, "time,RSI\n2020-01-02,55.0\n")
        assert "no rsi rows between 2026-05-02 and 2026-06-01" in str(exc.value)

    def test_an_indicator_this_vendor_has_no_endpoint_for_is_no_data(self, monkeypatch):
        # VWMA used to answer "## VWMA ... is not directly available from Alpha
        # Vantage API" — prose, so route_to_vendor recorded a successful report
        # and the chain stopped, even though the yfinance vendor serving the
        # same routed tool computes vwma from OHLCV. Same class as the four
        # above, and the one this getter can answer without asking anyone.
        monkeypatch.setattr(
            avi, "_make_api_request", lambda *a, **k: pytest.fail("no request may be made")
        )
        with pytest.raises(NoMarketDataError) as exc:
            avi.get_indicator("AAPL", "vwma", "2026-06-01", 30)
        assert "no VWMA endpoint" in str(exc.value)

    def test_rows_inside_the_window_still_render(self, monkeypatch):
        # The other side of the same boundary: one in-window row is a report, so
        # the raise above cannot have been widened into every answer.
        out = self._indicator(monkeypatch, "time,RSI\n2020-01-02,11.0\n2026-05-30,55.0\n")
        assert "## RSI values from 2026-05-02 to 2026-06-01" in out
        assert "2026-05-30: 55.0" in out
        assert "11.0" not in out  # the out-of-window row is still filtered out


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

    def fake_request(function_name, params, subject=None):
        captured.update(params)
        return "{}"

    monkeypatch.setattr(avn, "_make_api_request", fake_request)
    avn.get_global_news("2026-06-05", look_back_days=99999, limit=99999)
    assert captured["limit"] == str(avn.MAX_NEWS_LIMIT)
    assert captured["time_from"] == "20250605T0000"  # exactly 365 days back


@pytest.mark.unit
def test_global_news_none_optionals_resolve_to_defaults_before_clamp(monkeypatch):
    # The tool wrapper forwards omitted optionals as explicit None through the
    # router; they must resolve to the configured defaults instead of crashing
    # in int(None) (review round 1).
    import tradingagents.dataflows.alpha_vantage_news as avn

    captured = {}

    def fake_request(function_name, params, subject=None):
        captured.update(params)
        return "{}"

    monkeypatch.setattr(avn, "_make_api_request", fake_request)
    avn.get_global_news("2026-06-05", look_back_days=None, limit=None)
    assert captured["limit"] == "10"  # global_news_article_limit
    assert captured["time_from"] == "20260529T0000"  # global_news_lookback_days = 7


@pytest.mark.unit
class TestNewsVolumeComesFromConfigNotTheVendor:
    """How much news a routed tool ASKS FOR must not depend on the vendor (#107).

    Both getters used to carry literals — 50 articles and a 7-day window on
    global news, and no limit at all on ticker news, which left the endpoint's
    own default of 50 against the yfinance sibling's 20. Reading the same config
    keys the sibling reads is also what makes the tool wrapper's documented
    "defaults come from DEFAULT_CONFIG" true of both vendors rather than one.
    """

    def _captured_params(self, monkeypatch, call):
        captured = {}

        def fake_request(function_name, params, subject=None):
            captured.update(params)
            return "{}"

        monkeypatch.setattr(avn, "_make_api_request", fake_request)
        call()
        return captured

    def test_ticker_news_sizes_itself_from_news_article_limit(self, monkeypatch):
        from tradingagents.dataflows.config import get_config

        captured = self._captured_params(
            monkeypatch, lambda: avn.get_news("AAPL", "2026-06-01", "2026-06-05")
        )
        assert captured["limit"] == str(get_config()["news_article_limit"])

    @pytest.mark.parametrize(
        "key,override,param,expected",
        [
            ("news_article_limit", 3, "limit", "3"),
            ("global_news_article_limit", 4, "limit", "4"),
            ("global_news_lookback_days", 2, "time_from", "20260603T0000"),
        ],
    )
    def test_a_non_default_config_value_reaches_the_request(
        self, monkeypatch, key, override, param, expected
    ):
        # Discriminating against a literal that merely happens to equal the
        # shipped default: the assertion only passes if the value is read.
        from tradingagents.dataflows.config import get_config, set_config

        original = get_config()[key]
        set_config({key: override})
        try:
            call = (
                (lambda: avn.get_news("AAPL", "2026-06-01", "2026-06-05"))
                if key == "news_article_limit"
                else (lambda: avn.get_global_news("2026-06-05"))
            )
            captured = self._captured_params(monkeypatch, call)
        finally:
            set_config({key: original})
        assert captured[param] == expected

    def test_a_misconfigured_ticker_news_limit_is_still_clamped(self, monkeypatch):
        # The config is trusted no further than an LLM argument (#33): it is
        # operator-editable and now sizes an external request, so it takes the
        # same ceiling the global getter already applied to its own.
        from tradingagents.dataflows.config import get_config, set_config

        original = get_config()["news_article_limit"]
        set_config({"news_article_limit": 99999})
        try:
            captured = self._captured_params(
                monkeypatch, lambda: avn.get_news("AAPL", "2026-06-01", "2026-06-05")
            )
        finally:
            set_config({"news_article_limit": original})
        assert captured["limit"] == str(avn.MAX_NEWS_LIMIT)

    def test_an_explicit_argument_still_outranks_the_config(self, monkeypatch):
        # The config supplies the DEFAULT; a caller (or the LLM through the tool
        # wrapper) that names a value still gets it.
        captured = self._captured_params(
            monkeypatch, lambda: avn.get_global_news("2026-06-05", look_back_days=3, limit=7)
        )
        assert captured["limit"] == "7"
        assert captured["time_from"] == "20260602T0000"


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


@pytest.mark.unit
@pytest.mark.parametrize("status", [404, 500, 503])
def test_indicator_http_failure_propagates_instead_of_reading_as_success(monkeypatch, status):
    # #87: #72 classified only HTTP 429, so every other status reached this
    # getter's broad except and came back as "Error retrieving rsi data: 503
    # Server Error" — a string route_to_vendor reads as a successful answer, so
    # the chain stopped at the vendor that had just failed and the agent
    # analysed the error prose as an indicator report. Driven through the real
    # request boundary rather than a patched _make_api_request: the swallowing
    # happened to an exception that boundary raises, so the test has to make it
    # raise for real.
    monkeypatch.setattr(av, "get_api_key", lambda: "k")
    monkeypatch.setattr(av.requests, "get", _patched_get("", status_code=status))
    with pytest.raises(requests.HTTPError):
        avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)


@pytest.mark.unit
@pytest.mark.parametrize(
    "exc_type", [requests.ConnectionError, requests.Timeout], ids=["reset", "timeout"]
)
def test_indicator_transport_failure_propagates_instead_of_reading_as_success(
    monkeypatch, exc_type
):
    # The same lane one layer down: a reset or a timeout never reaches an HTTP
    # status at all and was swallowed identically. Every other Alpha Vantage
    # getter carries no broad except, so these already reach the router from
    # fundamentals/news/stock — this getter was the outlier (#87).
    def _boom(*a, **k):
        raise exc_type("connection died")

    monkeypatch.setattr(avi, "_make_api_request", _boom)
    with pytest.raises(exc_type):
        avi.get_indicator("AAPL", "rsi", "2026-06-01", 30)


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
    # than MAX_OHLCV_STALE_DAYS. The yfinance path raises on this exact gap
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
def test_stock_staleness_bound_is_the_single_shared_definition():
    # The two market-data paths must reject the same gap. Since #70 they both
    # import the one definition from utils (stdlib-only, so yfinance/stockstats
    # are not dragged into the pure-requests vendor module); this pins the
    # value and that the three bounds agree — a re-grown local copy shows up
    # here once it drifts.
    import tradingagents.dataflows.alpha_vantage_stock as avs
    import tradingagents.dataflows.stockstats_utils as ssu
    from tradingagents.dataflows import utils

    assert utils.MAX_OHLCV_STALE_DAYS == 10
    assert avs.MAX_OHLCV_STALE_DAYS == ssu.MAX_OHLCV_STALE_DAYS == utils.MAX_OHLCV_STALE_DAYS


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
@pytest.mark.parametrize("curr_date", ["2020-01-01", None])
def test_overview_empty_payload_raises_no_market_data(monkeypatch, curr_date):
    # AV answers an unknown symbol with "{}". Serving that as a successful
    # (optionally note-dressed) fundamentals report is what let a routed call
    # differ by vendor — the yfinance path raises here, which is what opens the
    # router's no-data lane.
    from tradingagents.dataflows.errors import NoMarketDataError

    _patch_av_request(monkeypatch, "{}")
    with pytest.raises(NoMarketDataError):
        avf.get_fundamentals("AAPL", curr_date)


@pytest.mark.unit
def test_overview_unparseable_curr_date_returns_the_shared_sentinel(monkeypatch):
    # The statement tools already answer INVALID_CURR_DATE here. Staying silent
    # on this path would serve today's ratios with no disclosure, because
    # live_snapshot_note degrades to "" on a date it cannot parse.
    _patch_av_request(monkeypatch, _OVERVIEW)
    out = avf.get_fundamentals("AAPL", "not-a-date")
    assert out.startswith("INVALID_CURR_DATE")
    assert "MarketCapitalization" not in out


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


@pytest.mark.unit
def test_a_vendor_supplied_note_key_cannot_shadow_the_real_disclosure(monkeypatch):
    # `{key: note, **payload}` lets the body win on a collision: the computed
    # disclosure disappears with no log line, and whatever the vendor put under
    # that key is read by the agent as a system-issued freshness statement.
    body = json.dumps(
        {
            "_freshness_note": "vendor supplied text",
            "quarterlyReports": [{"fiscalDateEnding": "2020-03-31"}],
        }
    )
    _patch_av_request(monkeypatch, body)
    note = _note_of(avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18"))
    assert "Data lag" in note
    assert "vendor supplied text" not in note


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        # Fresh data: our own note is empty, so nothing overwrites the vendor's
        # key. Each parameter reaches a different stripper — the first through
        # the served-cadence rebuild, the second through the passthrough exit
        # (which re-serializes only because there is a key to remove).
        lambda: avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18"),
        lambda: avf.get_balance_sheet("AAPL", "quarterly", None),
    ],
    ids=["rebuilt-body", "passthrough-body"],
)
def test_a_vendor_note_key_is_dropped_even_when_we_add_no_note(monkeypatch, call):
    body = json.dumps(
        {
            "_freshness_note": "vendor supplied text",
            "quarterlyReports": [{"fiscalDateEnding": "2026-06-30"}],
        }
    )
    _patch_av_request(monkeypatch, body)
    assert "vendor supplied text" not in call()


@pytest.mark.unit
def test_overview_vendor_note_key_is_dropped_when_no_disclosure_is_due(monkeypatch):
    # curr_date is today, so live_snapshot_note is empty and no note of ours is
    # written — this pins the exit that serves the body with nothing attached.
    from datetime import date

    body = json.dumps({"_freshness_note": "vendor supplied text", "Symbol": "AAPL"})
    _patch_av_request(monkeypatch, body)
    out = avf.get_fundamentals("AAPL", date.today().strftime("%Y-%m-%d"))
    assert avf._FRESHNESS_NOTE_KEY not in json.loads(out)
    assert json.loads(out)["Symbol"] == "AAPL"


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
    assert phrase in note
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
def test_statement_serves_only_the_requested_cadence(monkeypatch):
    # Alpha Vantage returns BOTH lists on every call. Only the requested one is
    # served: the freshness note judges that list, so shipping the other would
    # hand the agent a 2.6-year-old annual balance sheet with no disclosure
    # attached to it (the yfinance path fetches one frame and has no such gap).
    body = _statement_body(quarterly=["2026-06-30"], annual=["2023-12-31"])
    _patch_av_request(monkeypatch, body)

    quarterly = json.loads(avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18"))
    assert [r["fiscalDateEnding"] for r in quarterly["quarterlyReports"]] == ["2026-06-30"]
    assert "annualReports" not in quarterly
    assert quarterly["symbol"] == "AAPL"  # the rebuild keeps what the body says it describes
    assert avf._FRESHNESS_NOTE_KEY not in quarterly  # a 49-day-old quarter is on cadence

    annual = json.loads(avf.get_balance_sheet("AAPL", "annual", "2026-08-18"))
    assert [r["fiscalDateEnding"] for r in annual["annualReports"]] == ["2023-12-31"]
    assert "quarterlyReports" not in annual
    assert "2023-12-31" in annual[avf._FRESHNESS_NOTE_KEY]


@pytest.mark.unit
def test_statement_with_nothing_left_after_filtering_raises_no_market_data(monkeypatch):
    # Undated rows are dropped by the look-ahead filter. An empty list rendered
    # as a successful report is the dishonest outcome: yfinance raises on an
    # empty frame, and only a raise opens the router's no-data lane.
    from tradingagents.dataflows.errors import NoMarketDataError

    _patch_av_request(monkeypatch, _statement_body(quarterly=["not-a-date"]))
    with pytest.raises(NoMarketDataError) as exc:
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    # "rows arrived but none were usable" must not read as "this symbol has no
    # filings": a vendor schema rename would otherwise report every ticker as
    # uncovered, and the router's sentinel says exactly that.
    assert "all 1 quarterly balance sheet reports carried no usable fiscalDateEnding" in str(
        exc.value
    )


@pytest.mark.unit
def test_statement_with_no_reports_at_all_says_so_distinctly(monkeypatch):
    from tradingagents.dataflows.errors import NoMarketDataError

    _patch_av_request(monkeypatch, _statement_body(annual=["2025-12-31"]))
    with pytest.raises(NoMarketDataError) as exc:
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    assert "no quarterly balance sheet reports on or before" in str(exc.value)


@pytest.mark.unit
@pytest.mark.parametrize(
    "getter",
    [
        lambda: avf.get_balance_sheet("AAPL", "quarterly", ""),
        lambda: avf.get_fundamentals("AAPL", ""),
    ],
    ids=["statement", "overview"],
)
def test_an_empty_curr_date_is_treated_as_supplied_and_unusable(monkeypatch, getter):
    # `if not curr_date` lumps "no bound requested" together with "a bound was
    # supplied and is unusable", so an empty string used to take the passthrough
    # lane: unfiltered reports, no disclosure, no sentinel — silently the most
    # permissive answer of the three.
    body = json.dumps({"symbol": "AAPL", "quarterlyReports": [{"fiscalDateEnding": "2099-03-31"}]})
    _patch_av_request(monkeypatch, body)
    out = getter()
    assert out.startswith("INVALID_CURR_DATE")
    assert "2099-03-31" not in out


@pytest.mark.unit
def test_a_vendor_note_key_does_not_make_an_envelope_look_like_data(monkeypatch):
    # The envelope test asks whether anything BUT a notice is present. A
    # vendor-supplied `_freshness_note` is an extra key, so counting it as
    # content dresses a rate-limit notice in our own live-snapshot disclosure.
    body = json.dumps({"Information": "rate limit reached", "_freshness_note": "vendor text"})
    _patch_av_request(monkeypatch, body)
    out = avf.get_fundamentals("AAPL", "2020-01-01")
    assert "live values" not in out
    assert "vendor text" not in out
    assert json.loads(out) == {"Information": "rate limit reached"}


@pytest.mark.unit
def test_statement_reports_that_only_postdate_curr_date_are_not_reported_as_a_fault(
    monkeypatch, caplog
):
    # A backtest older than the vendor's coverage window drops every row for an
    # entirely correct reason. Calling that "undatable" — or logging a warning
    # for it — would page an operator on every ticker of every early backtest.
    from tradingagents.dataflows.errors import NoMarketDataError

    _patch_av_request(monkeypatch, _statement_body(quarterly=["2019-03-31", "2019-06-30"]))
    with (
        caplog.at_level(logging.WARNING, logger=avf.__name__),
        pytest.raises(NoMarketDataError) as exc,
    ):
        avf.get_balance_sheet("AAPL", "quarterly", "2015-01-01")
    assert "none on or before 2015-01-01" in str(exc.value)
    assert "usable fiscalDateEnding" not in str(exc.value)
    assert caplog.records == []


@pytest.mark.unit
def test_statement_undatable_rows_are_reported_and_logged_as_a_fault(monkeypatch, caplog):
    # A schema rename nulls every fiscalDateEnding. Reported as "no coverage"
    # this reads as an uncovered symbol on every ticker at once, so it must name
    # the real cause and leave a log line — the sentinel names only the symbol.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps(
        {"symbol": "AAPL", "quarterlyReports": [{"fiscal_date_ending": "2026-06-30"}]}
    )
    _patch_av_request(monkeypatch, body)
    with (
        caplog.at_level(logging.WARNING, logger=avf.__name__),
        pytest.raises(NoMarketDataError) as exc,
    ):
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    assert "all 1 quarterly balance sheet reports carried no usable fiscalDateEnding" in str(
        exc.value
    )
    assert any("undatable" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
@pytest.mark.parametrize(
    "reports",
    [
        [],  # nothing arrived at all
        [{"fiscalDateEnding": "not-a-date"}],  # rows arrived but none were usable
    ],
    ids=["no-reports", "undatable-reports"],
)
def test_statement_no_data_reason_carries_a_vendor_notice_when_one_rode_along(monkeypatch, reports):
    # A notice beside real keys escapes the whole-body envelope test and lands
    # here; without this the vendor's stated reason is destroyed by the rebuild.
    # Both no-data branches interpolate it, so both are pinned.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps(
        {
            "symbol": "AAPL",
            "Information": "our standard API rate limit is 25/day",
            "quarterlyReports": reports,
        }
    )
    _patch_av_request(monkeypatch, body)
    with pytest.raises(NoMarketDataError) as exc:
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    assert "Information" in str(exc.value)


@pytest.mark.unit
def test_statement_with_only_the_other_cadence_raises_instead_of_serving_it(monkeypatch):
    # An annual-only filer under the tool's default quarterly freq: the old
    # shape returned the annual reports with the note silent, so a seven-year-old
    # balance sheet reached the agent undisclosed.
    from tradingagents.dataflows.errors import NoMarketDataError

    _patch_av_request(monkeypatch, _statement_body(annual=["2019-12-31"]))
    with pytest.raises(NoMarketDataError):
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")


@pytest.mark.unit
def test_statement_malformed_report_rows_are_dropped_not_crashed(monkeypatch):
    # A scalar row makes r.get() raise AttributeError, which no caller catches:
    # the router logs it and falls back, leaving this vendor quietly broken.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"symbol": "AAPL", "quarterlyReports": ["2025-01-31", {"x": 1}]})
    _patch_av_request(monkeypatch, body)
    with pytest.raises(NoMarketDataError):
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")


@pytest.mark.unit
def test_statement_wrong_shaped_report_list_is_named_as_a_schema_break(monkeypatch):
    # A list replaced by an object is a vendor schema change; reporting it as
    # "no data" would make an incident look like an uncovered symbol.
    from tradingagents.dataflows.errors import NoMarketDataError

    body = json.dumps({"symbol": "AAPL", "quarterlyReports": {"fiscalDateEnding": "2026-06-30"}})
    _patch_av_request(monkeypatch, body)
    with pytest.raises(NoMarketDataError) as exc:
        avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18")
    assert "not a list of reports" in str(exc.value)


@pytest.mark.unit
def test_statement_error_envelope_is_served_unchanged(monkeypatch):
    # The request boundary now raises on an "Error Message" body (#68), so in
    # production one never reaches this module. This pins the defence in depth:
    # should a body slip past the boundary (a future refactor, a new caller),
    # this path must still not mistake it for a payload to filter, narrow, or
    # annotate.
    body = json.dumps({"Error Message": "Invalid API call."})
    _patch_av_request(monkeypatch, body)
    assert avf.get_balance_sheet("AAPL", "quarterly", "2026-08-18") == body


# ---------------------------------------------------------------------------
# Insider transactions freshness (#69): the same routed tool must flag a
# long-dead filing stream through Alpha Vantage as it does through yfinance.
# The reference date is the wall clock — no curr_date reaches an insider call.


def _insider_body(*dates):
    return json.dumps({"data": [{"transaction_date": d, "ticker": "AAPL"} for d in dates]})


def _patch_insider_request(monkeypatch, body):
    monkeypatch.setattr(avn, "_make_api_request", lambda function_name, params: body)


def _insider_note_of(out: str) -> str:
    return json.loads(out).get(avf._FRESHNESS_NOTE_KEY, "")


@pytest.mark.unit
def test_insider_dead_filing_stream_carries_note(monkeypatch):
    _patch_insider_request(monkeypatch, _insider_body("2020-01-01"))
    note = _insider_note_of(avn.get_insider_transactions("AAPL"))
    assert "Data lag" in note
    assert "insider filing" in note
    assert "2020-01-01" in note


@pytest.mark.unit
def test_insider_recent_filing_is_untouched(monkeypatch):
    from datetime import datetime

    body = _insider_body(datetime.now().strftime("%Y-%m-%d"))
    _patch_insider_request(monkeypatch, body)
    assert avn.get_insider_transactions("AAPL") == body  # not even re-serialized


@pytest.mark.unit
def test_insider_note_reflects_newest_filing(monkeypatch):
    # Order must not matter: the vendor serves newest-first today, but the note
    # judges the maximum, not the first row.
    _patch_insider_request(monkeypatch, _insider_body("2019-06-01", "2020-01-01"))
    note = _insider_note_of(avn.get_insider_transactions("AAPL"))
    assert "2020-01-01" in note


@pytest.mark.unit
@pytest.mark.parametrize("key", ["Error Message", "Information", "Note"])
def test_insider_error_envelope_is_not_dressed_with_a_note(monkeypatch, key):
    # An envelope that slips past the request boundary (#68) is a failure body:
    # attaching a freshness note would assert that filings were fetched.
    body = json.dumps({key: "Invalid API call."})
    _patch_insider_request(monkeypatch, body)
    assert avn.get_insider_transactions("AAPL") == body


@pytest.mark.unit
def test_insider_empty_list_beside_a_notice_is_not_flattened_to_prose(monkeypatch):
    # An empty list riding next to an unclassified Information/Note may be the
    # notice's side effect, not an affirmed "no filings" — the prose exit would
    # discard the vendor's own explanation, so the body passes through.
    body = json.dumps({"Information": "unclassified advisory", "data": []})
    _patch_insider_request(monkeypatch, body)
    out = avn.get_insider_transactions("AAPL")
    assert out == body
    assert "No insider transactions" not in out


@pytest.mark.unit
def test_insider_non_json_body_is_untouched(monkeypatch):
    body = "Thank you for using Alpha Vantage!"
    _patch_insider_request(monkeypatch, body)
    assert avn.get_insider_transactions("AAPL") == body


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"data": "not-a-list"}),
        json.dumps({"data": [{"ticker": "AAPL"}, "scalar-row"]}),
    ],
    ids=["wrong-shaped-data", "rows-without-dates"],
)
def test_insider_body_without_parseable_dates_degrades_to_no_note(monkeypatch, body):
    _patch_insider_request(monkeypatch, body)
    out = avn.get_insider_transactions("AAPL")
    assert out == body


@pytest.mark.unit
def test_insider_vendor_note_key_is_dropped_even_without_our_note(monkeypatch):
    # Family guard: a vendor-written _freshness_note must not reach the agent
    # looking like a system-issued freshness statement, on any served path.
    from datetime import datetime

    body = json.dumps(
        {
            "_freshness_note": "vendor supplied text",
            "data": [{"transaction_date": datetime.now().strftime("%Y-%m-%d")}],
        }
    )
    _patch_insider_request(monkeypatch, body)
    out = avn.get_insider_transactions("AAPL")
    assert "vendor supplied text" not in out
    assert avf._FRESHNESS_NOTE_KEY not in json.loads(out)


@pytest.mark.unit
def test_insider_vendor_note_key_cannot_shadow_the_real_disclosure(monkeypatch):
    body = json.dumps(
        {
            "_freshness_note": "vendor supplied text",
            "data": [{"transaction_date": "2020-01-01"}],
        }
    )
    _patch_insider_request(monkeypatch, body)
    note = _insider_note_of(avn.get_insider_transactions("AAPL"))
    assert "Data lag" in note
    assert "vendor supplied text" not in note


# --- news feeds: empty-window voice and the vendor note key (#90) ----------
#
# The third getter in the same module already took both rules (#88); these two
# served their bodies raw, so an empty feed arrived as empty JSON and a
# vendor-written _freshness_note arrived looking system-issued.

_NEWS_ARGS = ("AAPL", "2026-06-01", "2026-06-05")


def _patch_news_request(monkeypatch, body):
    monkeypatch.setattr(avn, "_make_api_request", lambda *a, **k: body)


@pytest.mark.unit
def test_news_empty_feed_answers_in_the_shared_voice(monkeypatch):
    _patch_news_request(monkeypatch, json.dumps({"items": "0", "feed": []}))
    assert avn.get_news(*_NEWS_ARGS) == "No news found for AAPL between 2026-06-01 and 2026-06-05"


@pytest.mark.unit
def test_global_news_empty_feed_answers_in_the_shared_voice(monkeypatch):
    _patch_news_request(monkeypatch, json.dumps({"items": "0", "feed": []}))
    out = avn.get_global_news("2026-06-08", look_back_days=7)
    assert out == "No global news found between 2026-06-01 and 2026-06-08"


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"feed": []}),
        json.dumps({"items": "3", "feed": []}),
    ],
    ids=["no-count", "count-disagrees"],
)
def test_news_empty_feed_verdict_reads_the_feed_alone(monkeypatch, body):
    # The companion "items" count could not be measured against a real empty
    # answer from this vendor (no key reaches the live endpoint from the test
    # environment), so the verdict deliberately does not consult it: a body
    # without the count, or with one that contradicts the feed, answers the
    # same. Whatever the vendor's real spelling is, only `feed` decides.
    _patch_news_request(monkeypatch, body)
    assert avn.get_news(*_NEWS_ARGS).startswith("No news found for AAPL")


@pytest.mark.unit
def test_news_empty_feed_beside_a_notice_is_not_flattened_to_prose(monkeypatch):
    # Same rule as the insider path: an empty feed next to an unclassified
    # Information/Note may be the notice's side effect, and the prose exit
    # would discard the vendor's own explanation.
    body = json.dumps({"Information": "unclassified advisory", "feed": []})
    _patch_news_request(monkeypatch, body)
    out = avn.get_news(*_NEWS_ARGS)
    assert out == body
    assert "No news found" not in out


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"items": "1", "feed": [{"title": "Fed cuts"}]}),
        json.dumps({"feed": "not-a-list"}),
        json.dumps({"items": "0"}),
        json.dumps({"Error Message": "Invalid API call."}),
        "Thank you for using Alpha Vantage!",
    ],
    ids=["articles", "wrong-shaped-feed", "no-feed-key", "error-envelope", "non-json"],
)
def test_news_body_without_an_affirmed_empty_feed_is_served_untouched(monkeypatch, body):
    _patch_news_request(monkeypatch, body)
    assert avn.get_news(*_NEWS_ARGS) == body  # not even re-serialized


@pytest.mark.unit
def test_news_passthrough_preserves_the_vendors_own_bytes(monkeypatch):
    # Stronger than the equality above, whose JSON fixtures are all built with
    # json.dumps' own defaults — so re-serializing a parsed copy would come out
    # byte-identical and pass vacuously (only its non-JSON case would notice).
    # A real Alpha Vantage answer is spelled
    # by the vendor (its own whitespace, its own key order), and the served
    # path promises those bytes back untouched, so the fixture here is
    # deliberately NOT what json.dumps would produce.
    body = '{\n    "feed": [\n        {"title": "Fed cuts"}\n    ],\n    "items": "1"\n}'
    _patch_news_request(monkeypatch, body)
    assert avn.get_news(*_NEWS_ARGS) == body


@pytest.mark.unit
def test_news_empty_feed_beside_a_vendor_note_answers_prose_without_it(monkeypatch):
    # A vendor-written note riding on an empty feed must not reach the agent by
    # either route: the prose exit answers, and the note goes nowhere. Unlike
    # the envelope keys, a freshness key is not evidence that the emptiness was
    # a notice's side effect, so it does not hold back the prose.
    _patch_news_request(
        monkeypatch, json.dumps({"_freshness_note": "vendor supplied text", "feed": []})
    )
    out = avn.get_news(*_NEWS_ARGS)
    assert out == "No news found for AAPL between 2026-06-01 and 2026-06-05"


@pytest.mark.unit
def test_global_news_empty_feed_names_the_window_resolved_from_an_omitted_lookback(monkeypatch):
    # The tool wrapper forwards an omitted optional as an explicit None — the
    # reason the parameter accepts one at all — so the sentence has to name the
    # window that resolution produced, not the raw argument.
    _patch_news_request(monkeypatch, json.dumps({"feed": []}))
    out = avn.get_global_news("2026-06-08", look_back_days=None)
    assert out == "No global news found between 2026-06-01 and 2026-06-08"


@pytest.mark.unit
@pytest.mark.parametrize(
    "getter,args",
    [("get_news", _NEWS_ARGS), ("get_global_news", ("2026-06-08",))],
    ids=["ticker-news", "global-news"],
)
def test_news_vendor_note_key_is_dropped_on_the_served_path(monkeypatch, getter, args):
    # Family guard (#88's rule, now on these two paths): neither getter attaches
    # a disclosure of its own, so a vendor-written note would stand unopposed
    # and read as a system-issued freshness statement.
    body = json.dumps({"_freshness_note": "vendor supplied text", "feed": [{"title": "Fed cuts"}]})
    _patch_news_request(monkeypatch, body)
    out = getattr(avn, getter)(*args)
    assert "vendor supplied text" not in out
    parsed = json.loads(out)
    assert avf._FRESHNESS_NOTE_KEY not in parsed
    assert parsed["feed"] == [{"title": "Fed cuts"}]  # the articles still arrive
