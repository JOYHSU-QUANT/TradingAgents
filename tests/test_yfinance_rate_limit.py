"""Yahoo Finance rate limits must reach the router's rate-limit lane (#67).

``yf_retry`` is the one boundary every yfinance network call goes through; when
its retries are exhausted, the vendor-native ``YFRateLimitError`` (not a
taxonomy type) is mapped to ``VendorRateLimitError``. Every yfinance leaf then
re-raises taxonomy errors (``except VendorError: raise``) instead of degrading
them to prose the router would read as a successful answer — the failure #60
fixed on the Alpha Vantage indicator path, on the vendor that is the default
for every one of these categories.
"""

import json
import logging
from unittest import mock

import pytest
import requests
from curl_cffi.requests import exceptions as curl_exceptions
from yfinance.exceptions import YFRateLimitError

import tradingagents.dataflows.stockstats_utils as su
import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as ynews
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    UnsupportedIndicatorError,
    VendorError,
    VendorRateLimitError,
    VendorUnavailableError,
)
from tradingagents.dataflows.throttle import THROTTLE_LATCH_TTL_S


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


# --- the latch: one caller discovers a throttle, the rest are spared it (#86) ---
# (frozen_clock comes from conftest: it steps the clock the latch and the
# backoff ladder both read.)


def _exhaust_the_ladder():
    with pytest.raises(VendorRateLimitError):
        su.yf_retry(mock.Mock(side_effect=YFRateLimitError()))


@pytest.mark.unit
def test_the_first_caller_still_pays_the_full_ladder(monkeypatch):
    # The latch is not a replacement for the retries: the shipped defaults give
    # the four yfinance categories a single-vendor chain, so there is no
    # fallback vendor to hand a brief throttle to and the call that discovers
    # one still gives Yahoo every chance.
    sleeps = []
    monkeypatch.setattr(su.time, "sleep", sleeps.append)
    attempts = mock.Mock(side_effect=YFRateLimitError())

    with pytest.raises(VendorRateLimitError):
        su.yf_retry(attempts)

    assert attempts.call_count == 4  # the initial call plus all three retries
    assert sleeps == [2.0, 4.0, 8.0]


@pytest.mark.unit
def test_a_latched_throttle_skips_both_the_vendor_and_the_ladder(frozen_clock):
    _exhaust_the_ladder()

    spared = mock.Mock(side_effect=YFRateLimitError())
    with pytest.raises(VendorRateLimitError, match="without contacting the vendor"):
        su.yf_retry(spared)

    # The point of #86: no second ladder, and no request added to a host that
    # is already refusing this client.
    spared.assert_not_called()


@pytest.mark.unit
def test_the_latch_spares_a_different_tool_the_same_discovery(frozen_clock, monkeypatch):
    # The cost #86 measures is per TOOL, not per call site: within one cycle
    # several yfinance-first tools each used to re-discover the same 429. Arm
    # it through one boundary, then check a leaf in a different module.
    search = mock.Mock()
    monkeypatch.setattr(ynews.yf, "Search", search)
    _exhaust_the_ladder()

    with pytest.raises(VendorRateLimitError):
        ynews.get_global_news_yfinance("2026-06-01")

    search.assert_not_called()


@pytest.mark.unit
def test_the_latch_holds_for_its_window_and_lets_go_on_the_deadline(frozen_clock):
    _exhaust_the_ladder()

    frozen_clock["t"] += THROTTLE_LATCH_TTL_S - 1
    blocked = mock.Mock(return_value="ok")
    with pytest.raises(VendorRateLimitError):
        su.yf_retry(blocked)
    blocked.assert_not_called()

    # Exclusive bound, like the other windows in this codebase: at the deadline
    # itself the vendor is contacted again.
    frozen_clock["t"] += 1
    served = mock.Mock(return_value="ok")
    assert su.yf_retry(served) == "ok"
    served.assert_called_once()


@pytest.mark.unit
def test_being_served_clears_the_latch(frozen_clock):
    _exhaust_the_ladder()
    frozen_clock["t"] += THROTTLE_LATCH_TTL_S

    assert su.yf_retry(lambda: "ok") == "ok"

    # Not merely expired — dropped, so no stale deadline is left behind for a
    # later call to reason about. (remaining_s reads None either way once the
    # window has passed, so it is the recorded deadline itself that is pinned.)
    assert not su._YF_THROTTLE_LATCH.has_deadline(su._YF_LATCH_KEY)


def _arm_then(outcome):
    """A fetch during which a sibling thread arms the latch (#114).

    ``yf_retry`` raises before calling when the latch is live, so the only way
    a call can meet a live latch is to have one armed while it is in flight.
    Modelled inline rather than with a real thread: what is under test is what
    the answered-branch does with the deadline it finds, not the race.
    """

    def fetch():
        su._YF_THROTTLE_LATCH.arm(su._YF_LATCH_KEY)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return fetch


@pytest.mark.unit
def test_a_restored_failure_does_not_clear_a_live_latch():
    # yf_fetch_unhidden restores a swallowed failure to the caller's hidden
    # answer, and that value used to reach yf_retry looking exactly like data
    # — so the answered-branch dropped a deadline a sibling had just armed and
    # the next tool re-discovered the throttle at the price of a ladder
    # (#114). Not a verdict bug: the caller still gets the empty frame.
    out = su.yf_fetch_statement(_arm_then(ValueError("boom")))
    assert out.empty
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is not None


@pytest.mark.unit
def test_a_restored_404_does_not_clear_a_live_latch():
    # The other restore path: Yahoo's verdict on the symbol comes back as the
    # hidden answer too, and is likewise not this client's standing with Yahoo.
    assert su.yf_fetch_unhidden(_arm_then(_http_error(404)), hidden_answer=list) == []
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is not None


@pytest.mark.unit
def test_a_served_fetch_still_clears_a_live_latch():
    # The discrimination for the two above: the same in-flight arm, but the
    # fetch actually answers — that is the fresher evidence, and the deadline
    # goes.
    assert su.yf_fetch_unhidden(_arm_then("data"), hidden_answer=list) == "data"
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is None


@pytest.mark.unit
def test_yf_retry_keeps_no_opinion_about_return_values():
    # The signal is a type yf_retry defines, not a shape it recognises: a bare
    # callable that returns the very value the restore paths hand back — an
    # empty frame — is an answer, and clears the latch. The pd knowledge stays
    # in yf_fetch_unhidden.
    import pandas as pd

    out = su.yf_retry(_arm_then(pd.DataFrame()))
    assert out.empty
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is None


@pytest.mark.unit
def test_only_an_exhausted_throttle_arms_the_latch(frozen_clock):
    # A throttle the retries clear, and any failure outside the taxonomy, leave
    # the next tool call free to reach Yahoo — the un-throttled path is exactly
    # as it was before #86.
    outcomes = [YFRateLimitError(), "ok"]

    def flaky():
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    assert su.yf_retry(flaky) == "ok"
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is None

    with pytest.raises(ValueError):
        su.yf_retry(mock.Mock(side_effect=ValueError("boom")))
    assert su._YF_THROTTLE_LATCH.remaining_s(su._YF_LATCH_KEY) is None


# --- the statement boundary: throttles and transport failures un-hidden ---


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
def test_yf_fetch_statement_lets_a_transport_failure_out():
    # The other exception the un-hidden window must not re-swallow (#116):
    # restored to an empty frame, a reset read as "no data" downstream.
    from yfinance.config import YfConfig

    with pytest.raises(curl_exceptions.ConnectionError):
        su.yf_fetch_statement(
            mock.Mock(side_effect=curl_exceptions.ConnectionError("connection reset"))
        )
    assert YfConfig.debug.hide_exceptions is True


@pytest.mark.unit
def test_yf_fetch_statement_is_safe_under_parallel_tool_execution():
    # ToolNode runs one message's tool calls on a thread pool, and the
    # fundamentals analyst binds the three statement tools together. Without
    # the lock, an interleaved backup capture restores the global flag
    # mid-fetch (re-swallowing a throttle) or leaves it stuck False.
    import threading
    import time as _time

    from yfinance.config import YfConfig

    seen = []
    entered = threading.Event()

    def probe():
        entered.set()
        seen.append(YfConfig.debug.hide_exceptions)
        _time.sleep(0.05)
        raise ValueError("boom")

    # Deterministic sequencing, not a timing coin-flip: the second call is
    # started only once the first is provably inside its fetch window. With
    # the lock it blocks until the first restores; without it, it is
    # guaranteed to capture backup=False and restore that stale value last,
    # so the final-flag assertion fails on every run.
    first = threading.Thread(target=lambda: su.yf_fetch_statement(probe))
    second = threading.Thread(target=lambda: su.yf_fetch_statement(probe))
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    first.join()
    second.join()
    assert seen == [False, False]  # each fetch ran inside its own window
    assert YfConfig.debug.hide_exceptions is True  # and the default came back


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


@pytest.mark.unit
def test_statement_transport_failure_survives_yfinance_internal_swallowing(monkeypatch):
    # Through the REAL yfinance property, like the throttle test above: the
    # fundamentals scraper swallows a transport failure into an empty frame
    # too, which _statement_report read as "no balance sheet data" and the
    # router's no-data sentinel then outranked the recorded failure — the
    # agent was told the symbol was not covered and the fallback vendor never
    # got its turn. The statement getters' own OSError clause cannot see this
    # layer; yf_fetch_statement's un-hidden window is what lets it out (#116).
    import yfinance.data as yfdata

    boom = mock.Mock(side_effect=curl_exceptions.ConnectionError("connection reset"))
    monkeypatch.setattr(yfdata.YfData, "get", boom)
    monkeypatch.setattr(yfdata.YfData, "cache_get", boom)
    with pytest.raises(curl_exceptions.ConnectionError):
        yfin.get_balance_sheet("AAPL", "quarterly", "2026-06-01")


def _yahoo_raises(monkeypatch, exc):
    """Make every request yfinance's scrapers issue raise ``exc``.

    Below the library's own swallow, so a test here exercises the un-hidden
    window rather than a patched seam above it.
    """
    import yfinance.data as yfdata

    boom = mock.Mock(side_effect=exc)
    for name in ("get", "post", "cache_get", "get_raw_json"):
        if hasattr(yfdata.YfData, name):
            monkeypatch.setattr(yfdata.YfData, name, boom)


def _http_error(status):
    """An ``HTTPError`` shaped the way curl_cffi's ``raise_for_status`` builds it."""
    import types

    return curl_exceptions.HTTPError(
        f"HTTP Error {status}", 0, types.SimpleNamespace(status_code=status)
    )


_SWALLOWING_LEAVES = [
    pytest.param(lambda: yfin.get_fundamentals("AAPL", "2026-06-01"), id="fundamentals"),
    pytest.param(lambda: yfin.get_insider_transactions("AAPL"), id="insiders"),
    pytest.param(
        lambda: yfin.get_YFin_data_online("AAPL", "2026-06-01", "2026-06-05"), id="prices"
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("call", _SWALLOWING_LEAVES)
def test_a_transport_failure_survives_yfinance_internal_swallowing(monkeypatch, tmp_path, call):
    # The scrapers behind these calls catch their own failures under
    # hide_exceptions: quote answers None (its parser then trips), holders and
    # history an empty frame. Measured before #116: fundamentals came back as
    # "Error retrieving ... 'NoneType' is not iterable" prose, insiders as "No
    # insider transactions reported", prices as the no-data sentinel — each
    # read by the router as an answer. The leaves' own OSError clause sits
    # above that swallow and cannot see it; the un-hidden window lets it out.
    set_config({"data_cache_dir": str(tmp_path)})
    _yahoo_raises(monkeypatch, curl_exceptions.ConnectionError("connection reset"))
    with pytest.raises(curl_exceptions.ConnectionError):
        call()


@pytest.mark.unit
@pytest.mark.parametrize("call", _SWALLOWING_LEAVES)
def test_an_http_404_is_still_the_no_data_verdict(monkeypatch, tmp_path, call):
    # Yahoo's quoteSummary answers 404 for an unknown or delisted symbol, and
    # history reaches the same fetch through its timezone lookup. That is the
    # vendor's verdict on the symbol, not a failure of the wire: under the
    # swallow it became the empty answer, and the un-hidden window must keep
    # it there rather than surface an HTTPError that reads as a transport
    # failure — measured as a regression in review before this pin existed.
    from tradingagents.dataflows.errors import NoMarketDataError

    set_config({"data_cache_dir": str(tmp_path)})
    _yahoo_raises(monkeypatch, _http_error(404))
    try:
        out = call()
    except NoMarketDataError:
        return
    # The insider lane treats an empty stream as a normal answer rather than
    # no-data (many valid symbols have no filings), and a 404 still reads as
    # that empty stream — as it did under the swallow.
    assert out.startswith("No insider transactions reported"), out


@pytest.mark.unit
@pytest.mark.parametrize("call", _SWALLOWING_LEAVES[:2])
def test_an_http_5xx_surfaces_from_the_quote_scrapers(monkeypatch, tmp_path, call):
    # The quoteSummary fetch is the one that checks the status, so a 5xx there
    # is a genuine vendor failure the fallback chain should see. (history,
    # get_news and Search parse the body first, so a 5xx page reaches them as
    # a JSON error — the vendor-unavailable lane, pinned further down, #136.)
    set_config({"data_cache_dir": str(tmp_path)})
    _yahoo_raises(monkeypatch, _http_error(503))
    with pytest.raises(curl_exceptions.HTTPError):
        call()


# --- parsed before status: an outage page is not "no data" (#136) ---


def _yahoo_answers(monkeypatch, status, body):
    """Make every request yfinance's scrapers issue come back as one response.

    ``get``/``post``/``cache_get`` hand it back the way the library does — it
    checks no status below 429 there — and ``get_raw_json`` raises for a
    4xx/5xx the way its ``raise_for_status`` does before parsing the body.
    """
    import yfinance.data as yfdata

    class _Response:
        status_code = status
        text = body
        url = "https://query2.finance.yahoo.com/"

        def json(self):
            return json.loads(self.text)

        def raise_for_status(self):
            if status >= 400:
                raise _http_error(status)

    response = _Response()
    for name in ("get", "post", "cache_get"):
        monkeypatch.setattr(yfdata.YfData, name, mock.Mock(return_value=response))

    def raw_json(self, url, params=None, timeout=30):
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(yfdata.YfData, "get_raw_json", raw_json)
    return response


_OUTAGE_PAGES = {
    "5xx_html": (503, "<html><body><h1>503 Service Unavailable</h1></body></html>"),
    "right_back": (200, "<html><body>Will be right back</body></html>"),
}

_PARSE_FIRST_LEAVES = [
    _SWALLOWING_LEAVES[2],  # prices
    pytest.param(lambda: ynews.get_news_yfinance("AAPL", "2026-06-01", "2026-06-05"), id="news"),
    pytest.param(lambda: ynews.get_global_news_yfinance("2026-06-01"), id="global_news"),
]


@pytest.mark.unit
@pytest.mark.parametrize("call", _PARSE_FIRST_LEAVES)
@pytest.mark.parametrize("page", sorted(_OUTAGE_PAGES))
def test_an_outage_page_is_not_no_data_on_the_parse_first_paths(monkeypatch, tmp_path, call, page):
    # history, get_news and Search parse the body before they look at the
    # status, so a 5xx HTML page reaches them as JSONDecodeError — not an
    # OSError, so the un-hidden window restored it to the empty answer, which
    # the leaves read as "No news found" or the no-data sentinel (and that
    # sentinel outranks the recorded failure in the router, so the fallback
    # vendor was never tried). Yahoo's own "Will be right back" page is the
    # library's YFDataException, which the window let out raw and the leaves'
    # broad handler made prose of. Both are the vendor answering without data
    # (#136). Below the library's own swallow, so the real scrapers run.
    set_config({"data_cache_dir": str(tmp_path)})
    _yahoo_answers(monkeypatch, *_OUTAGE_PAGES[page])
    with pytest.raises(VendorUnavailableError):
        call()


@pytest.mark.unit
def test_search_restores_a_library_bug_to_its_own_empty_answer(monkeypatch):
    # The window's hidden answer for Search is the library's own news=[], so
    # anything the swallow would have hidden (a KeyError from a changed
    # response shape) still reads as the empty search it always did.
    monkeypatch.setattr(ynews.yf, "Search", mock.Mock(side_effect=KeyError("quotes")))
    assert ynews.get_global_news_yfinance("2026-06-01") == "No global news found for 2026-06-01"


@pytest.mark.unit
def test_an_outage_page_on_the_second_fundamentals_fetch_is_not_no_data(monkeypatch, tmp_path):
    # info makes two fetches: quoteSummary through get_raw_json, which checks
    # the status (pinned above), then the fundamentals-timeseries page, which
    # it json.loads without looking. A 5xx there used to be restored to the
    # stub dict and reach the router as "no fundamentals returned" — with the
    # quoteSummary payload already in hand (#136).
    import yfinance.data as yfdata

    set_config({"data_cache_dir": str(tmp_path)})
    _yahoo_answers(monkeypatch, *_OUTAGE_PAGES["5xx_html"])
    payload = {"quoteSummary": {"result": [{"symbol": "AAPL", "longName": "Apple Inc."}]}}
    monkeypatch.setattr(yfdata.YfData, "get_raw_json", mock.Mock(return_value=payload))
    with pytest.raises(VendorUnavailableError):
        yfin.get_fundamentals("AAPL", "2026-06-01")


@pytest.mark.unit
def test_an_outage_page_reaches_the_fallback_vendor(monkeypatch, tmp_path, caplog):
    # Through the real router: the outage takes its own lane — the chain goes
    # on, and the log line carries no traceback, which the router reserves
    # for a bug (#136).
    set_config(
        {"data_cache_dir": str(tmp_path), "data_vendors": {"news_data": "yfinance,alpha_vantage"}}
    )
    _yahoo_answers(monkeypatch, *_OUTAGE_PAGES["5xx_html"])
    with (
        mock.patch.dict(
            interface.VENDOR_METHODS,
            {
                "get_news": {
                    "yfinance": ynews.get_news_yfinance,
                    "alpha_vantage": mock.Mock(return_value="AV_NEWS"),
                }
            },
            clear=False,
        ),
        caplog.at_level(logging.WARNING, logger=interface.__name__),
    ):
        result = interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")
    assert result == "AV_NEWS"
    assert caplog.records and not any(r.exc_info for r in caplog.records)


@pytest.mark.unit
def test_an_outage_page_on_a_single_vendor_chain_fails_loud(monkeypatch, tmp_path):
    # news_data is a core category: with no other vendor to serve it, the
    # outage itself surfaces — not "No news found", and not prose.
    set_config({"data_cache_dir": str(tmp_path), "data_vendors": {"news_data": "yfinance"}})
    _yahoo_answers(monkeypatch, *_OUTAGE_PAGES["5xx_html"])
    with (
        mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_news": {"yfinance": ynews.get_news_yfinance}},
            clear=False,
        ),
        pytest.raises(VendorUnavailableError),
    ):
        interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")


@pytest.mark.unit
def test_yf_fetch_unhidden_sorts_failures_into_their_lanes():
    # Restored to the caller's hidden answer: the library's own expected
    # conditions (a delisted symbol's YFTzMissingError) and an HTTP 404. Let
    # out: a throttle (for yf_retry's mapping) and every other OSError.
    # Mapped to the vendor-unavailable type: the library's "Yahoo is down"
    # signal and a body that is not JSON — the vendor answering without data,
    # which is neither a transport failure nor "no data" (#136). The flag
    # comes back either way.
    from yfinance.config import YfConfig
    from yfinance.exceptions import YFDataException, YFTzMissingError

    def fetch(exc):
        return su.yf_fetch_unhidden(mock.Mock(side_effect=exc), hidden_answer=list)

    assert fetch(YFTzMissingError("AAPL")) == []
    assert fetch(_http_error(404)) == []
    with pytest.raises(VendorUnavailableError):
        fetch(YFDataException("*** YAHOO! FINANCE IS CURRENTLY DOWN! ***"))
    with pytest.raises(VendorUnavailableError):
        fetch(json.JSONDecodeError("Expecting value", "<html>503</html>", 0))
    with pytest.raises(curl_exceptions.HTTPError):
        fetch(_http_error(503))
    # 404 alone is the symbol verdict: a 403 is Yahoo refusing this client
    # (crumb or IP block) and must reach the fallback chain, not read as
    # "symbol not covered".
    with pytest.raises(curl_exceptions.HTTPError):
        fetch(_http_error(403))
    with pytest.raises(curl_exceptions.ConnectionError):
        fetch(curl_exceptions.ConnectionError("connection reset"))
    assert YfConfig.debug.hide_exceptions is True


@pytest.mark.unit
def test_a_partial_service_history_failure_takes_the_no_data_lane(monkeypatch, tmp_path):
    # Pins a deliberate choice (#137): yfinance 1.4.1's history swallows an
    # auto/back-adjust failure (and the unit-mixup repair's metadata probe) by
    # logging and serving the frame WITHOUT that repair; un-hidden, those
    # sites raise instead, and yf_fetch_unhidden restores the raise to the
    # empty frame — so rows the library would have served on the wrong price
    # basis become this vendor's no-data verdict and the chain moves on. No
    # data over possibly-wrong prices, the same judgement the integrity
    # cleaner makes about fabricated values (#38).
    import yfinance as yf
    from yfinance.config import YfConfig

    from tradingagents.dataflows.errors import NoMarketDataError

    set_config({"data_cache_dir": str(tmp_path)})

    def adjust_failure_site(self, *a, **k):
        # The raise this simulates exists only when the swallow is off — the
        # assert keeps this double honest about which library branch it plays.
        assert YfConfig.debug.hide_exceptions is False
        raise ValueError("auto_adjust failed with missing Adj Close")

    monkeypatch.setattr(yf.Ticker, "history", adjust_failure_site)
    with pytest.raises(NoMarketDataError):
        su.load_ohlcv("AAPL", "2026-06-01")


# --- the leaves: a taxonomy error propagates instead of degrading to prose ---


# How to drive each routed yfinance implementation, keyed by the method name it
# is registered under: the fetch boundary it reaches Yahoo through, and the
# arguments it takes.
#
# Membership is NOT this table's to decide — the coverage check below derives it
# from VENDOR_METHODS, so the registry is the single source of truth for "which
# leaves must honour the taxonomy". Hand-enumerating them made a third list, and
# a newly registered yfinance impl with no matching row shipped green (#86).
# get_stock_data is the proof: the hand-written lists this table replaced never
# checked it, so nothing in this file made it honour the taxonomy.
#
# The seam is per-leaf rather than "patch them all", because which boundary a
# leaf uses is itself an invariant worth pinning: every leaf goes through
# yf_fetch_unhidden (the statements by its yf_fetch_statement name), since
# plain yf_retry leaves yfinance free to swallow a 429, a transport failure or
# an outage page into an empty answer that reads as "no data" (#67, #116,
# #136). Naming the wrong seam leaves the real fetch running,
# so the row fails rather than passing for the wrong reason — and
# monkeypatch.setattr raises if a binding is renamed away instead of quietly
# patching nothing.
_YFINANCE_LEAF_CALLS = {
    "get_stock_data": ((yfin, "yf_fetch_unhidden"), ("AAPL", "2026-06-01", "2026-06-05")),
    "get_indicators": ((su, "yf_fetch_unhidden"), ("AAPL", "rsi", "2026-06-01", 5)),
    "get_fundamentals": ((yfin, "yf_fetch_unhidden"), ("AAPL", "2026-06-01")),
    "get_balance_sheet": ((yfin, "yf_fetch_statement"), ("AAPL", "quarterly", "2026-06-01")),
    "get_cashflow": ((yfin, "yf_fetch_statement"), ("AAPL", "quarterly", "2026-06-01")),
    "get_income_statement": ((yfin, "yf_fetch_statement"), ("AAPL", "quarterly", "2026-06-01")),
    # The news row pins SEAM IDENTITY only: base.get_news's post does not
    # swallow transport failures, so these propagation rows would stay green
    # without the window. What the window buys news is the outage mapping —
    # Yahoo's "Will be right back" page raising VendorUnavailableError instead
    # of being restored to the [] that reads as "No news found" — and THAT is
    # pinned by test_an_outage_page_is_not_no_data_on_the_parse_first_paths,
    # which drives the real scrapers and goes red if the window is removed
    # from the getter (#137).
    "get_news": ((ynews, "yf_fetch_unhidden"), ("AAPL", "2026-06-01", "2026-06-05")),
    "get_global_news": ((ynews, "yf_fetch_unhidden"), ("2026-06-01",)),
    "get_insider_transactions": ((yfin, "yf_fetch_unhidden"), ("AAPL",)),
}


def _registered_yfinance_methods(registry):
    return {method for method, vendors in registry.items() if "yfinance" in vendors}


def _check_call_table_covers(registry):
    assert set(_YFINANCE_LEAF_CALLS) == _registered_yfinance_methods(registry)


def _check_impl_propagates(
    monkeypatch, tmp_path, impl, seam, args, raiser=_throttled, expected=VendorRateLimitError
):
    monkeypatch.setattr(*seam, raiser)
    # An empty cache dir, so the OHLCV leaves actually reach the seam instead of
    # being served a file some other test wrote.
    set_config({"data_cache_dir": str(tmp_path)})
    with pytest.raises(expected):
        impl(*args)


@pytest.mark.unit
def test_every_registered_yfinance_impl_has_a_row_in_the_call_table():
    _check_call_table_covers(interface.VENDOR_METHODS)


@pytest.mark.unit
def test_the_coverage_check_catches_an_unlisted_registry_entry():
    # Discrimination: register a yfinance impl the table does not know about
    # and the coverage check must fail — that is the whole point of deriving
    # the leaf list from the registry.
    with (
        mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_unlisted_thing": {"yfinance": lambda: None}},
            clear=False,
        ),
        pytest.raises(AssertionError),
    ):
        _check_call_table_covers(interface.VENDOR_METHODS)


@pytest.mark.unit
@pytest.mark.parametrize("method", sorted(_YFINANCE_LEAF_CALLS))
def test_every_registered_yfinance_impl_lets_the_rate_limit_propagate(
    monkeypatch, tmp_path, method
):
    seam, args = _YFINANCE_LEAF_CALLS[method]
    _check_impl_propagates(
        monkeypatch, tmp_path, interface.VENDOR_METHODS[method]["yfinance"], seam, args
    )


@pytest.mark.unit
def test_the_propagation_check_catches_a_leaf_that_degrades_the_throttle(monkeypatch, tmp_path):
    # Discrimination: the shape #67 fixed — a leaf whose broad handler turns a
    # typed vendor failure into prose the router reads as a successful answer.
    def degrading_leaf(ticker):
        try:
            return yfin.yf_fetch_unhidden(lambda: "data", hidden_answer=str)
        except Exception as e:  # noqa: BLE001 - deliberately the buggy shape
            return f"Error retrieving something for {ticker}: {e}"

    with pytest.raises(pytest.fail.Exception):
        _check_impl_propagates(
            monkeypatch, tmp_path, degrading_leaf, (yfin, "yf_fetch_unhidden"), ("AAPL",)
        )


@pytest.mark.unit
def test_bulk_rate_limit_escapes_the_windowed_getter(monkeypatch):
    # The typed error must escape the broad handler rather than degrade to
    # prose — pinned at the load_ohlcv seam, the fetch the bulk path actually
    # makes (the parametrized table pins the yf_fetch_unhidden seam).
    monkeypatch.setattr(yfin, "load_ohlcv", _throttled)
    with pytest.raises(VendorRateLimitError):
        yfin.get_stock_stats_indicators_window("AAPL", "rsi", "2026-06-01", 5)


@pytest.mark.unit
def test_an_untyped_bulk_failure_renders_one_line_and_fetches_once(monkeypatch):
    # #137: after the taxonomy (#67) and transport (#116) re-raises, nothing
    # that reaches the windowed getter's broad handler is transient — the
    # per-day fallback loop it used to run performed the identical fetch and
    # calculation once per day of the window and rendered a column of blanks
    # under a successful-looking header. Now: one fetch, one line of prose.
    fetch = mock.Mock(side_effect=KeyError("volume"))
    monkeypatch.setattr(yfin, "load_ohlcv", fetch)
    out = yfin.get_stock_stats_indicators_window("AAPL", "rsi", "2026-06-01", 30)
    assert out.startswith("Error retrieving rsi values for AAPL")
    assert "\n" not in out  # one line, not a 30-day column of blanks
    assert fetch.call_count == 1  # a deterministic failure is not re-run per day


@pytest.mark.unit
def test_untyped_failures_still_degrade_to_prose(monkeypatch):
    # The broad handler keeps its job for anything outside the taxonomy AND
    # outside the transport family below: a vendor-library bug must not abort
    # a run that another data point could still serve.
    monkeypatch.setattr(yfin, "yf_fetch_unhidden", mock.Mock(side_effect=RuntimeError("boom")))
    out = yfin.get_fundamentals("AAPL", "2026-06-01")
    assert out.startswith("Error retrieving fundamentals")


@pytest.mark.unit
def test_an_unsupported_indicator_is_the_caller_mistake_type(monkeypatch):
    # The wrapper renders exactly this type as report text (#117); a plain
    # ValueError would now reach the ToolNode as a failure instead. Raised
    # before any fetch.
    monkeypatch.setattr(yfin, "load_ohlcv", lambda *a, **k: pytest.fail("no fetch may be made"))
    with pytest.raises(UnsupportedIndicatorError, match="not supported"):
        yfin.get_stock_stats_indicators_window("AAPL", "bogus", "2026-06-01", 5)


# --- the leaves, again: an OSError propagates instead of degrading to prose ---

# yfinance 1.4.1 fetches through curl_cffi, whose request exceptions escape the
# ``info``/``insider_transactions``/news calls as raised (measured: a session
# raising curl_cffi's ConnectionError surfaces from data.py's crumb fetch
# unwrapped). That family and ``requests.RequestException`` both subclass
# OSError, and nothing in yfinance's own YFException family does — which is
# what lets every leaf re-raise one type (#116). Both libraries are driven so
# the clause is pinned to the family, not to whichever one yfinance links
# today; the third row is the OHLCV cache's own OSError, which the clause
# covers on purpose (a cache the process cannot read or write is not a
# report either) and which only the OHLCV leaves can actually raise.
_PROPAGATED_OSERRORS = {
    "curl_cffi": curl_exceptions.ConnectionError("connection reset"),
    "requests": requests.Timeout("read timed out"),
    "cache": PermissionError("cache dir is read-only"),
}


@pytest.mark.unit
@pytest.mark.parametrize("family", sorted(_PROPAGATED_OSERRORS))
@pytest.mark.parametrize("method", sorted(_YFINANCE_LEAF_CALLS))
def test_every_registered_yfinance_impl_lets_an_oserror_propagate(
    monkeypatch, tmp_path, method, family
):
    # #116: a reset or a timeout used to reach each leaf's broad except and
    # come back as "Error retrieving ..." prose route_to_vendor reads as a
    # successful report, so the chain stopped at the vendor that had just
    # failed and the agent analysed the error sentence. Same registry-derived
    # table as the throttle check, so a newly registered leaf must honour both.
    # The statement rows raise at the yf_fetch_statement seam, which pins the
    # getters' clause; the swallow one layer below it has its own test
    # (test_statement_transport_failure_survives_yfinance_internal_swallowing).
    exc = _PROPAGATED_OSERRORS[family]
    seam, args = _YFINANCE_LEAF_CALLS[method]
    _check_impl_propagates(
        monkeypatch,
        tmp_path,
        interface.VENDOR_METHODS[method]["yfinance"],
        seam,
        args,
        raiser=mock.Mock(side_effect=exc),
        expected=type(exc),
    )


@pytest.mark.unit
def test_the_transport_check_catches_a_leaf_that_degrades_it(monkeypatch, tmp_path):
    # Discrimination: the exact pre-#116 shape — taxonomy re-raised, everything
    # else pasted into prose — must fail the transport check.
    def degrading_leaf(ticker):
        try:
            return yfin.yf_fetch_unhidden(lambda: "data", hidden_answer=str)
        except VendorError:
            raise
        except Exception as e:  # noqa: BLE001 - deliberately the buggy shape
            return f"Error retrieving something for {ticker}: {e}"

    with pytest.raises(pytest.fail.Exception):
        _check_impl_propagates(
            monkeypatch,
            tmp_path,
            degrading_leaf,
            (yfin, "yf_fetch_unhidden"),
            ("AAPL",),
            raiser=mock.Mock(side_effect=_PROPAGATED_OSERRORS["curl_cffi"]),
            expected=curl_exceptions.ConnectionError,
        )


@pytest.mark.unit
def test_bulk_transport_failure_escapes_the_windowed_getter(monkeypatch):
    # A transport failure must escape the broad handler on this leaf too —
    # its old per-day fallback re-ran the failed fetch once per day of the
    # window and rendered a column of blanks under a successful-looking
    # header (#116); the loop is gone (#137), and the raise is the pin.
    monkeypatch.setattr(
        yfin, "load_ohlcv", mock.Mock(side_effect=_PROPAGATED_OSERRORS["curl_cffi"])
    )
    with pytest.raises(curl_exceptions.ConnectionError):
        yfin.get_stock_stats_indicators_window("AAPL", "rsi", "2026-06-01", 5)


# --- end to end: through the real boundary into the router's lanes ---
# (config isolation comes from the autouse _isolate_config fixture in conftest)


def _failing_yahoo(monkeypatch, tmp_path, exc=None):
    """Point the real OHLCV path at an empty cache and a Yahoo that raises ``exc``.

    Patched at Ticker.history — the call load_ohlcv actually makes, and one
    that genuinely re-raises YFRateLimitError (yf.download swallows it into an
    empty frame, which is why load_ohlcv does not use it, #67). The default
    is an always-429 Yahoo; a transport type drives the #116 lane.
    """
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    monkeypatch.setattr(su.yf.Ticker, "history", mock.Mock(side_effect=exc or YFRateLimitError()))
    set_config({"data_cache_dir": str(tmp_path)})


@pytest.mark.unit
def test_yfinance_rate_limit_reaches_the_fallback_vendor(monkeypatch, tmp_path):
    # #67 end-to-end through the REAL windowed indicator getter and the real
    # yf_retry boundary: an exhausted 429 used to come back as prose the router
    # read as a successful answer, so the configured fallback vendor never got
    # its turn.
    _failing_yahoo(monkeypatch, tmp_path)
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
    _failing_yahoo(monkeypatch, tmp_path)
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


@pytest.mark.unit
def test_yfinance_transport_failure_reaches_the_fallback_vendor(monkeypatch, tmp_path):
    # #116 end-to-end: a reset used to come back as prose the router read as a
    # successful answer, so the configured fallback vendor never got its turn.
    _failing_yahoo(monkeypatch, tmp_path, curl_exceptions.ConnectionError("connection reset"))
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
def test_yfinance_transport_failure_on_a_single_vendor_chain_fails_loud(monkeypatch, tmp_path):
    # technical_indicators is a core category: with no other vendor to serve
    # it, the transport failure itself surfaces (the router's ``raise
    # first_error``), not prose and not a bare RuntimeError.
    _failing_yahoo(monkeypatch, tmp_path, curl_exceptions.ConnectionError("connection reset"))
    set_config({"data_vendors": {"technical_indicators": "yfinance"}})
    with (
        mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_indicators": {"yfinance": yfin.get_stock_stats_indicators_window}},
            clear=False,
        ),
        pytest.raises(curl_exceptions.ConnectionError),
    ):
        interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-06-01", 5)


@pytest.mark.unit
def test_an_unsupported_indicator_reaches_the_wrapper_as_report_text(monkeypatch, tmp_path, caplog):
    # Through the real router and the real yfinance getter: the wrapper's
    # narrow except only helps if the type survives route_to_vendor. Pinned
    # end-to-end so a router that later wraps generic failures cannot silently
    # turn every typo into an aborted run (#117). And no traceback in the log:
    # a typo is not the bug exc_info is reserved for.
    from tradingagents.agents.utils import technical_indicators_tools as tools

    set_config(
        {"data_vendors": {"technical_indicators": "yfinance"}, "data_cache_dir": str(tmp_path)}
    )
    monkeypatch.setattr(
        yfin, "load_ohlcv", mock.Mock(side_effect=AssertionError("no fetch may be made"))
    )
    with caplog.at_level(logging.WARNING, logger=interface.__name__):
        out = tools.get_indicators.invoke(
            {"symbol": "AAPL", "indicator": "bogus", "curr_date": "2026-06-01"}
        )
    assert "Indicator bogus is not supported" in out
    assert caplog.records and not any(r.exc_info for r in caplog.records)
