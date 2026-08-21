"""Yahoo Finance rate limits must reach the router's rate-limit lane (#67).

``yf_retry`` is the one boundary every yfinance network call goes through; when
its retries are exhausted, the vendor-native ``YFRateLimitError`` (not a
taxonomy type) is mapped to ``VendorRateLimitError``. Every yfinance leaf then
re-raises taxonomy errors (``except VendorError: raise``) instead of degrading
them to prose the router would read as a successful answer — the failure #60
fixed on the Alpha Vantage indicator path, on the vendor that is the default
for every one of these categories.
"""

from unittest import mock

import pytest
from yfinance.exceptions import YFRateLimitError

import tradingagents.dataflows.stockstats_utils as su
import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as ynews
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorRateLimitError


def _throttled(*a, **k):
    raise VendorRateLimitError("Yahoo Finance rate limited the request")


# --- the boundary: yf_retry maps exhaustion into the taxonomy ---


@pytest.mark.unit
def test_yf_retry_exhaustion_raises_the_taxonomy_type(monkeypatch):
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_throttled():
        calls["n"] += 1
        raise YFRateLimitError()

    with pytest.raises(VendorRateLimitError) as exc:
        su.yf_retry(always_throttled, max_retries=2)
    # The vendor's own error stays attached as the cause, so a traceback still
    # shows what Yahoo actually raised.
    assert isinstance(exc.value.__cause__, YFRateLimitError)
    assert calls["n"] == 3  # the initial call plus both retries ran first


@pytest.mark.unit
def test_yf_retry_recovers_when_a_retry_succeeds(monkeypatch):
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    outcomes = [YFRateLimitError(), "ok"]

    def flaky():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    assert su.yf_retry(flaky, max_retries=2) == "ok"


@pytest.mark.unit
def test_yf_retry_lets_other_errors_out_immediately(monkeypatch):
    sleeps = []
    monkeypatch.setattr(su.time, "sleep", sleeps.append)

    def broken():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        su.yf_retry(broken)
    assert sleeps == []  # no retry burned on a non-throttle failure


# --- the statement boundary: throttles un-hidden, everything else unchanged ---


@pytest.mark.unit
def test_yf_fetch_statement_maps_a_throttle(monkeypatch):
    from yfinance.config import YfConfig

    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    with pytest.raises(VendorRateLimitError):
        su.yf_fetch_statement(mock.Mock(side_effect=YFRateLimitError()))
    assert YfConfig.debug.hide_exceptions is True  # backup/restore held


@pytest.mark.unit
def test_yf_fetch_statement_restores_other_errors_to_an_empty_frame():
    from yfinance.config import YfConfig

    out = su.yf_fetch_statement(mock.Mock(side_effect=ValueError("boom")))
    assert out.empty  # the library's swallowed-empty answer, preserved
    assert YfConfig.debug.hide_exceptions is True


@pytest.mark.unit
def test_statement_throttle_survives_yfinance_internal_swallowing(monkeypatch):
    # Through the REAL yfinance property: the fundamentals scraper swallows
    # YFRateLimitError into an empty frame under the default hidden-exception
    # mode (verified on yfinance 1.4.1), which would read as "no data". The
    # un-hidden window in yf_fetch_statement is what lets the throttle out —
    # a test that patches our own yf_retry cannot see this layer.
    import yfinance.data as yfdata

    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    monkeypatch.setattr(yfdata.YfData, "get", mock.Mock(side_effect=YFRateLimitError()))
    monkeypatch.setattr(yfdata.YfData, "cache_get", mock.Mock(side_effect=YFRateLimitError()))
    with pytest.raises(VendorRateLimitError):
        yfin.get_balance_sheet("AAPL", "quarterly", "2026-06-01")


# --- the leaves: a taxonomy error propagates instead of degrading to prose ---


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attr", "call"),
    [
        # attr is the boundary each leaf actually fetches through: the
        # statement getters go through yf_fetch_statement, the rest through
        # yf_retry — patching the wrong one leaves the real fetch running.
        ("yf_retry", lambda: yfin.get_fundamentals("AAPL", "2026-06-01")),
        ("yf_fetch_statement", lambda: yfin.get_balance_sheet("AAPL", "quarterly", "2026-06-01")),
        ("yf_fetch_statement", lambda: yfin.get_cashflow("AAPL", "quarterly", "2026-06-01")),
        (
            "yf_fetch_statement",
            lambda: yfin.get_income_statement("AAPL", "quarterly", "2026-06-01"),
        ),
        ("yf_retry", lambda: yfin.get_insider_transactions("AAPL")),
    ],
    ids=[
        "fundamentals",
        "balance-sheet",
        "cashflow",
        "income-statement",
        "insider-transactions",
    ],
)
def test_y_finance_leaves_let_the_rate_limit_propagate(monkeypatch, attr, call):
    monkeypatch.setattr(yfin, attr, _throttled)
    with pytest.raises(VendorRateLimitError):
        call()


@pytest.mark.unit
def test_stockstats_indicator_lets_the_rate_limit_propagate(monkeypatch):
    # This leaf reaches Yahoo through StockstatsUtils, which resolves
    # load_ohlcv in the stockstats_utils namespace — patch it there.
    monkeypatch.setattr(su, "load_ohlcv", _throttled)
    with pytest.raises(VendorRateLimitError):
        yfin.get_stockstats_indicator("AAPL", "rsi", "2026-06-01")


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        lambda: ynews.get_news_yfinance("AAPL", "2026-06-01", "2026-06-05"),
        lambda: ynews.get_global_news_yfinance("2026-06-01"),
    ],
    ids=["ticker-news", "global-news"],
)
def test_yfinance_news_leaves_let_the_rate_limit_propagate(monkeypatch, call):
    monkeypatch.setattr(ynews, "yf_retry", _throttled)
    with pytest.raises(VendorRateLimitError):
        call()


@pytest.mark.unit
def test_bulk_rate_limit_skips_the_per_day_fallback_loop(monkeypatch):
    # The windowed getter's broad handler falls back to a per-day loop; on a
    # rate limit that loop would re-run the same throttled fetch once per day
    # of the window and then render prose. The typed error must escape before
    # the loop starts.
    monkeypatch.setattr(yfin, "load_ohlcv", _throttled)
    fallback = mock.Mock(return_value="N/A")
    monkeypatch.setattr(yfin, "get_stockstats_indicator", fallback)
    with pytest.raises(VendorRateLimitError):
        yfin.get_stock_stats_indicators_window("AAPL", "rsi", "2026-06-01", 5)
    fallback.assert_not_called()


@pytest.mark.unit
def test_untyped_failures_still_degrade_to_prose(monkeypatch):
    # The broad handler keeps its job for anything outside the taxonomy: a
    # vendor-library bug must not abort a run that another data point could
    # still serve.
    monkeypatch.setattr(yfin, "yf_retry", mock.Mock(side_effect=RuntimeError("boom")))
    out = yfin.get_fundamentals("AAPL", "2026-06-01")
    assert out.startswith("Error retrieving fundamentals")


# --- end to end: through the real boundary into the router's lanes ---
# (config isolation comes from the autouse _isolate_config fixture in conftest)


def _rate_limited_yahoo(monkeypatch, tmp_path):
    """Point the real OHLCV path at an empty cache and an always-429 Yahoo.

    Patched at Ticker.history — the call load_ohlcv actually makes, and one
    that genuinely re-raises YFRateLimitError (yf.download swallows it into an
    empty frame, which is why load_ohlcv does not use it, #67).
    """
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    monkeypatch.setattr(su.yf.Ticker, "history", mock.Mock(side_effect=YFRateLimitError()))
    set_config({"data_cache_dir": str(tmp_path)})


@pytest.mark.unit
def test_yfinance_rate_limit_reaches_the_fallback_vendor(monkeypatch, tmp_path):
    # #67 end-to-end through the REAL windowed indicator getter and the real
    # yf_retry boundary: an exhausted 429 used to come back as prose the router
    # read as a successful answer, so the configured fallback vendor never got
    # its turn.
    _rate_limited_yahoo(monkeypatch, tmp_path)
    set_config({"data_vendors": {"technical_indicators": "yfinance,alpha_vantage"}})
    with mock.patch.dict(
        interface.VENDOR_METHODS,
        {
            "get_indicators": {
                "yfinance": yfin.get_stock_stats_indicators_window,
                "alpha_vantage": mock.Mock(return_value="AV_INDICATORS"),
            }
        },
        clear=False,
    ):
        result = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 5)
    assert result == "AV_INDICATORS"


@pytest.mark.unit
def test_yfinance_rate_limit_on_a_single_vendor_chain_fails_loud(monkeypatch, tmp_path):
    # technical_indicators is a core category: a chain exhausted by nothing but
    # rate limits surfaces the throttle instead of prose or a bare RuntimeError.
    _rate_limited_yahoo(monkeypatch, tmp_path)
    set_config({"data_vendors": {"technical_indicators": "yfinance"}})
    with (
        mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_indicators": {"yfinance": yfin.get_stock_stats_indicators_window}},
            clear=False,
        ),
        pytest.raises(VendorRateLimitError),
    ):
        interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 5)
