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


# --- the latch: one caller discovers a throttle, the rest are spared it (#86) ---


@pytest.fixture()
def frozen_clock(monkeypatch):
    """A settable monotonic clock, so latch windows are stepped, not slept."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(su.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(su.time, "sleep", lambda s: None)
    return clock


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

    frozen_clock["t"] += su._THROTTLE_LATCH_TTL_S - 1
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
    frozen_clock["t"] += su._THROTTLE_LATCH_TTL_S

    assert su.yf_retry(lambda: "ok") == "ok"

    # Not merely expired — dropped. A served request proves the throttle is
    # over, so no stale deadline is left behind to reason about.
    assert su._throttle_latch_remaining_s() is None


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
    assert su._throttle_latch_remaining_s() is None

    with pytest.raises(ValueError):
        su.yf_retry(mock.Mock(side_effect=ValueError("boom")))
    assert su._throttle_latch_remaining_s() is None


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


# --- the leaves: a taxonomy error propagates instead of degrading to prose ---


# The arguments each routed yfinance implementation needs, keyed by the method
# name it is registered under. Deriving the leaf list from VENDOR_METHODS (see
# the coverage check below) makes the registry the single source of truth for
# "which leaves must honour the taxonomy"; hand-enumerating them made a third
# list, and a newly registered yfinance impl with no matching row shipped green
# (#86). This table only supplies call arguments — it may not decide membership.
_YFINANCE_LEAF_ARGS = {
    "get_stock_data": ("AAPL", "2026-06-01", "2026-06-05"),
    "get_indicators": ("AAPL", "rsi", "2026-06-01", 5),
    "get_fundamentals": ("AAPL", "2026-06-01"),
    "get_balance_sheet": ("AAPL", "quarterly", "2026-06-01"),
    "get_cashflow": ("AAPL", "quarterly", "2026-06-01"),
    "get_income_statement": ("AAPL", "quarterly", "2026-06-01"),
    "get_news": ("AAPL", "2026-06-01", "2026-06-05"),
    "get_global_news": ("2026-06-01",),
    "get_insider_transactions": ("AAPL",),
}

# Every yfinance network call is made through one of these bindings: the
# statement properties through yf_fetch_statement, everything else through
# yf_retry, each bound into the leaf's own module namespace by its import. All
# of them are replaced at once so the check does not have to know which
# boundary a given leaf reaches for — and monkeypatch.setattr raises if a
# binding is ever renamed away, rather than quietly patching nothing.
_FETCH_SEAMS = (
    (su, "yf_retry"),
    (yfin, "yf_retry"),
    (yfin, "yf_fetch_statement"),
    (ynews, "yf_retry"),
)


def _registered_yfinance_methods(registry):
    return {method for method, vendors in registry.items() if "yfinance" in vendors}


def _check_args_table_covers(registry):
    assert set(_YFINANCE_LEAF_ARGS) == _registered_yfinance_methods(registry)


def _check_impl_propagates(monkeypatch, tmp_path, impl, args):
    for module, name in _FETCH_SEAMS:
        monkeypatch.setattr(module, name, _throttled)
    # An empty cache dir, so the OHLCV leaves actually reach a fetch seam
    # instead of being served a file some other test wrote.
    set_config({"data_cache_dir": str(tmp_path)})
    with pytest.raises(VendorRateLimitError):
        impl(*args)


@pytest.mark.unit
def test_every_registered_yfinance_impl_has_a_row_in_the_args_table():
    _check_args_table_covers(interface.VENDOR_METHODS)


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
        _check_args_table_covers(interface.VENDOR_METHODS)


@pytest.mark.unit
@pytest.mark.parametrize("method", sorted(_YFINANCE_LEAF_ARGS))
def test_every_registered_yfinance_impl_lets_the_rate_limit_propagate(
    monkeypatch, tmp_path, method
):
    _check_impl_propagates(
        monkeypatch,
        tmp_path,
        interface.VENDOR_METHODS[method]["yfinance"],
        _YFINANCE_LEAF_ARGS[method],
    )


@pytest.mark.unit
def test_the_propagation_check_catches_a_leaf_that_degrades_the_throttle(monkeypatch, tmp_path):
    # Discrimination: the shape #67 fixed — a leaf whose broad handler turns a
    # typed vendor failure into prose the router reads as a successful answer.
    def degrading_leaf(ticker):
        try:
            return yfin.yf_retry(lambda: "data")
        except Exception as e:  # noqa: BLE001 - deliberately the buggy shape
            return f"Error retrieving something for {ticker}: {e}"

    with pytest.raises(pytest.fail.Exception):
        _check_impl_propagates(monkeypatch, tmp_path, degrading_leaf, ("AAPL",))


@pytest.mark.unit
def test_stockstats_indicator_lets_the_rate_limit_propagate(monkeypatch):
    # This leaf reaches Yahoo through StockstatsUtils, which resolves
    # load_ohlcv in the stockstats_utils namespace — patch it there.
    monkeypatch.setattr(su, "load_ohlcv", _throttled)
    with pytest.raises(VendorRateLimitError):
        yfin.get_stockstats_indicator("AAPL", "rsi", "2026-06-01")


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
