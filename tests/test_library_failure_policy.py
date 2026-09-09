"""The untyped lane: what a vendor leaves unhandled is read as its library (#187, #219).

Nine getters used to hand-copy ``except VendorError: raise`` / ``except
OSError: raise`` / ``except Exception: return "Error retrieving ..."``. One
that forgot the guard rendered a throttle as a report (#85), seven logged
nothing, and the prose — read by the router as a successful answer — ended
the chain at the vendor that had just failed. PR #218 replaced the copies
with one ``with`` block; this suite pinned that block. What it did not fix
was WHICH getters wore it: the same routed tool aborted the run through
Alpha Vantage and reported a line of prose through yfinance, because only
the yfinance side had ever carried a broad handler to replace (#219).

The conversion is the router's now, so these tests drive the router: what it
reads as a vendor's library failing, what it does not, and where the
traceback goes. That every registered impl leaves its untyped failures
unhandled is the registry-derived suites' job (test_yfinance_rate_limit for
the yfinance leaves, test_alpha_vantage_hardening for Alpha Vantage), and
the endings themselves are test_vendor_routing's.
"""

import logging
from unittest import mock

import pytest

from tests.test_yfinance_rate_limit import _PROPAGATED_OSERRORS
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import (
    NoMarketDataError,
    UnsupportedIndicatorError,
    VendorError,
    VendorLibraryError,
    VendorNotConfiguredError,
    VendorRateLimitError,
    VendorUnavailableError,
    WiringGapError,
)

# One routed tool, one vendor, so the ending is the failure's alone. news_data
# is a core category: nothing here degrades to an optional category's sentinel.
_METHOD = "get_news"
_ARGS = ("AAPL", "2026-06-01", "2026-06-05")
_SUBJECT = "news for AAPL"


def _route(impl):
    set_config({"data_vendors": {"news_data": "yfinance"}})
    with mock.patch.dict(interface.VENDOR_METHODS, {_METHOD: {"yfinance": impl}}):
        return interface.route_to_vendor(_METHOD, *_ARGS)


def _raises(exc):
    def impl(*a, **k):
        raise exc

    return impl


class _VendorNamedError(VendorError):
    """A thin vendor-named subclass, the shape ``DeribitError`` and friends take.

    The taxonomy promises one of these needs no new ``except`` clause, so it
    reaches the untyped lane like a library bug — and must not be rewritten
    into one: its message is the vendor's own verdict.
    """


# What the untyped lane must not read as the vendor's library: every taxonomy
# type (its own router lane, or the base type's promise, decides its ending),
# the caller's indicator mistake, the OSError family both transport libraries
# raise through (#116) plus the OHLCV cache's own, and this project's own
# wiring (#111, #200).
_NOT_THE_LIBRARY = {
    "no_data": NoMarketDataError("AAPL", detail="no rows"),
    "rate_limit": VendorRateLimitError("429"),
    "unavailable": VendorUnavailableError("HTTP 503"),
    "not_configured": VendorNotConfiguredError("no key"),
    "vendor_named": _VendorNamedError("no BTC DVOL readings on or before 2026-06-01"),
    "caller": UnsupportedIndicatorError("bogus"),
    # The measured transport family, from the suite that documents why these
    # three pin it — not a second copy of the table.
    **_PROPAGATED_OSERRORS,
    "wiring": WiringGapError("news configuration: 'news_article_limit'"),
}


@pytest.mark.unit
@pytest.mark.parametrize("name", sorted(_NOT_THE_LIBRARY))
def test_what_the_untyped_lane_does_not_read_as_the_library(name):
    exc = _NOT_THE_LIBRARY[name]
    if isinstance(exc, NoMarketDataError):
        # Its own ending, and the one that is not a raise: the symbol is
        # unavailable, which is a verdict rather than a failure.
        assert _route(_raises(exc)).startswith("NO_DATA_AVAILABLE")
        return
    # The instance itself, not a wrapper: a core chain with nothing else to
    # say raises what it met, and the report line is reserved for a library.
    with pytest.raises(type(exc)) as info:
        _route(_raises(exc))
    assert info.value is exc


@pytest.mark.unit
def test_anything_else_is_the_library_named_from_the_call():
    # The subject is the routed tool's, read off this call's own arguments —
    # the vendor does not enter into it, which is what makes two vendors of
    # one tool name the same failure alike (#187).
    out = _route(_raises(KeyError("volume")))
    assert out == f"Error retrieving {_SUBJECT}: 'volume'"


@pytest.mark.unit
def test_the_failure_carries_the_whole_message_and_its_cause():
    # The router caps the message on its way into the report line; the type
    # itself carries it whole, and the library's own exception rides along as
    # __cause__ for whoever reads the failure rather than the report.
    cause = RuntimeError("boom")
    failure = interface._as_library_failure("news_data", _METHOD, _ARGS, cause)
    assert isinstance(failure, VendorLibraryError)
    assert failure.what == _SUBJECT
    assert failure.detail == "boom"
    assert failure.__cause__ is cause


@pytest.mark.unit
def test_the_traceback_is_logged_by_the_router_with_the_whole_message(caplog):
    # The lane at the getter used to log this, under the getter's own module;
    # with the conversion at the router there is one log line, and it is the
    # uncapped copy of a message the report line caps (#187).
    cause = RuntimeError("boom")
    with caplog.at_level(logging.WARNING, logger="tradingagents.dataflows.interface"):
        _route(_raises(cause))
    [record] = [r for r in caplog.records if r.exc_info is not None]
    assert record.name == "tradingagents.dataflows.interface"
    assert record.exc_info[1] is cause
    # ERROR and named by subject: the degrade the leaf logs existed to make
    # visible (#187) must not read like the routine vendor fallbacks this
    # lane's other endings are.
    assert record.levelno == logging.ERROR
    assert _SUBJECT in record.getMessage()
    assert "boom" in record.getMessage()


@pytest.mark.unit
def test_the_lane_reserves_that_level_for_the_library(caplog):
    # The other half of the rule: an untyped failure this lane does NOT read
    # as the library keeps the WARNING every vendor's routine bad day has,
    # so ERROR stays the level an operator can grep for a degrade.
    with (
        caplog.at_level(logging.WARNING, logger="tradingagents.dataflows.interface"),
        pytest.raises(WiringGapError),
    ):
        _route(_raises(WiringGapError("news configuration: 'global_news_queries'")))
    [record] = [r for r in caplog.records if r.exc_info is not None]
    assert record.levelno == logging.WARNING


@pytest.mark.unit
@pytest.mark.parametrize("failure", [KeyError("volume"), VendorLibraryError("x", "y")])
def test_a_loud_category_raises_whoever_named_the_library_failure(failure):
    # ``LOUD_LIBRARY_CATEGORIES`` is about the ENDING, so it cannot depend on
    # who typed the failure: a boundary naming its own library bug gets the
    # same answer from the router as one that left it untyped (#219). OHLCV
    # is the analyst's primary input, and a report line where the prices
    # should be would leave the run reasoning from nothing.
    set_config({"data_vendors": {"core_stock_apis": "yfinance"}})
    with (
        mock.patch.dict(interface.VENDOR_METHODS, {"get_stock_data": {"yfinance": _raises(failure)}}),
        pytest.raises(type(failure)) as info,
    ):
        interface.route_to_vendor("get_stock_data", "AAPL", "2026-06-01", "2026-06-05")
    assert info.value is failure


@pytest.mark.unit
@pytest.mark.parametrize("failure", [KeyError("volume"), VendorLibraryError("x", "y")])
def test_a_loud_category_still_yields_to_a_sibling_that_reported_no_data(failure):
    # What "loud" promises is that the failure is never rendered as report
    # text — not that the call always raises. A sibling vendor that answered
    # with a clean no-data verdict still ends the chain in its sentinel,
    # which outranks the raise, and did so before the declaration existed.
    # Pinned because the declaration reads like the stronger promise and the
    # single-vendor test above cannot tell the two apart (#219).
    set_config({"data_vendors": {"core_stock_apis": "yfinance,alpha_vantage"}})
    with mock.patch.dict(
        interface.VENDOR_METHODS,
        {
            "get_stock_data": {
                "yfinance": _raises(failure),
                "alpha_vantage": _raises(NoMarketDataError("AAPL", "AAPL", "no rows")),
            }
        },
    ):
        out = interface.route_to_vendor("get_stock_data", "AAPL", "2026-06-01", "2026-06-05")
    assert out.startswith("NO_DATA_AVAILABLE")
    assert "Error retrieving" not in out


@pytest.mark.unit
def test_an_optional_category_degrading_over_a_wiring_gap_says_which_one():
    # The sentinel is what the report artifacts keep, and the text of a
    # WiringGapError is ours — the prologue's name and the key that was
    # missing — so it rides along flattened and capped rather than degrading
    # to the class name the untyped rule reserves for a vendor's message
    # (#171, #219).
    set_config({"data_vendors": {"crypto_etf_flows": "farside"}})
    gap = WiringGapError("cache configuration: 'data_cache_dir'\n" + "x" * 500)
    with mock.patch.dict(interface.VENDOR_METHODS, {"get_etf_flows": {"farside": _raises(gap)}}):
        out = interface.route_to_vendor("get_etf_flows", "BTC", "2026-06-01", 7)
    assert out.startswith("DATA_UNAVAILABLE")
    assert "cache configuration: 'data_cache_dir'" in out
    # Flattened and capped, which the value it echoes can make necessary: a
    # bad date reaches this text as the caller wrote it. Asserted with a
    # message that is long and multi-line, so dropping the sanitize would
    # fail here rather than pass on a short one.
    assert "\n" not in out
    assert len(out) < len(str(gap))


@pytest.mark.unit
@pytest.mark.parametrize(
    "passed_through",
    [
        VendorRateLimitError("slow down"),
        UnsupportedIndicatorError("no vendor computes 'mfi'"),
        WiringGapError("an inner prologue already named this"),
        *_PROPAGATED_OSERRORS.values(),
    ],
)
def test_the_prologue_guard_passes_a_verdict_and_a_transport_failure_through(passed_through):
    # ``wiring_gap`` says "this is ours", so it must not relabel anything the
    # router tells apart from an untyped failure: a taxonomy verdict, the
    # transports that suite documents, the caller's own indicator mistake, or
    # a gap an inner block already named. Nothing in today's blocks can raise
    # any of them — they read dicts and forget a cache — but the block is the
    # kind that grows a statement, and a rate limit relabelled as our wiring
    # would abort the run where the next vendor was owed its turn (#219).
    from tradingagents.dataflows.utils import wiring_gap

    with pytest.raises(type(passed_through)) as info, wiring_gap("some prologue"):
        raise passed_through
    assert info.value is passed_through


@pytest.mark.unit
@pytest.mark.parametrize("vendor", ["yfinance", "alpha_vantage"])
def test_both_global_news_vendors_name_an_unusable_window_alike(vendor, monkeypatch):
    # The property this whole change is about, at the one value both vendors
    # coerce: an unusable window names the same guard whichever serves, so
    # the operator is sent to the same place. It used to name the yfinance
    # side's configuration guard and the Alpha Vantage side's window guard,
    # which is the sibling divergence in miniature (#219).
    import tradingagents.dataflows.alpha_vantage_news as avn
    import tradingagents.dataflows.yfinance_news as ynews

    set_config(
        {
            "global_news_lookback_days": "ten",
            "global_news_article_limit": 5,
            "global_news_queries": ["macro"],
            "alpha_vantage_api_key": "k",
        }
    )
    monkeypatch.setattr(ynews.yf, "Search", lambda *a, **k: pytest.fail("no fetch may be made"))
    monkeypatch.setattr(avn, "_make_api_request", lambda *a, **k: pytest.fail("no fetch"))
    getter = {
        "yfinance": ynews.get_global_news_yfinance,
        "alpha_vantage": avn.get_global_news,
    }[vendor]
    with pytest.raises(WiringGapError, match="global news lookback window"):
        getter("2026-06-01")


@pytest.mark.unit
@pytest.mark.parametrize("order", ["gap first", "library first"])
def test_a_wiring_gap_outranks_a_sibling_library_failure_in_a_core_chain(order):
    # Whichever met first: our own breakage is the one someone can fix, and
    # handing the analyst the other vendor's parser bug as the answer would
    # leave the missing key unmentioned in everything but a WARNING (#219).
    gap = _raises(WiringGapError("news configuration: 'news_article_limit'"))
    library = _raises(RuntimeError("pandas exploded"))
    chain = {"yfinance": gap, "alpha_vantage": library}
    if order == "library first":
        chain = {"yfinance": library, "alpha_vantage": gap}
    set_config({"data_vendors": {"news_data": "yfinance,alpha_vantage"}})
    with (
        mock.patch.dict(interface.VENDOR_METHODS, {"get_news": chain}),
        pytest.raises(WiringGapError, match="news_article_limit"),
    ):
        interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")


@pytest.mark.unit
@pytest.mark.parametrize(
    "sibling, expected",
    [
        (NoMarketDataError("AAPL", "AAPL", "no rows"), "NO_DATA_AVAILABLE"),
        (UnsupportedIndicatorError("no vendor computes 'zzz'"), None),
        (VendorUnavailableError("yahoo answered an outage page"), None),
    ],
)
def test_a_wiring_gap_outranks_nothing_but_the_report_line(sibling, expected):
    # The other half of the rule above. A gap is raised ahead of a library
    # failure's report line and ahead of nothing else: a sibling's clean
    # no-data verdict still ends the chain, the caller's own indicator
    # mistake still surfaces first (#137 — a missing key in its place points
    # at the wrong remedy), and among the vendors' own failures the first met
    # still wins. Pinned because the guard that buys the rule above is one
    # condition away from taking these too.
    # The sibling first, so what is asserted is that the gap does not jump
    # ahead of it. Met first the gap would win the vendors' own slot on the
    # ordinary first-met rule, which is a different fact.
    set_config({"data_vendors": {"news_data": "yfinance,alpha_vantage"}})
    chain = {
        "yfinance": _raises(sibling),
        "alpha_vantage": _raises(WiringGapError("news configuration: 'news_article_limit'")),
    }
    with mock.patch.dict(interface.VENDOR_METHODS, {"get_news": chain}):
        if expected is None:
            with pytest.raises(type(sibling)) as info:
                interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")
            assert info.value is sibling
        else:
            out = interface.route_to_vendor("get_news", "AAPL", "2026-06-01", "2026-06-05")
            assert out.startswith(expected)


@pytest.mark.unit
def test_the_cache_directory_readers_name_the_gap_rather_than_the_vendor(monkeypatch):
    # The readers of ``data_cache_dir`` the entry names. Farside has no copy
    # of its own any more — it imports SoSoValue's — so the identity is what
    # keeps the two from drifting apart again, and there is one guard to
    # exercise rather than two.
    import tradingagents.dataflows.farside as farside
    import tradingagents.dataflows.sosovalue_common as soso
    import tradingagents.dataflows.yfinance_common as yfc

    assert farside._cache_dir is soso._cache_dir

    monkeypatch.setattr(soso, "get_config", dict)
    with pytest.raises(WiringGapError, match="cache configuration"):
        soso._cache_dir()

    monkeypatch.setattr(yfc, "get_config", dict)
    with pytest.raises(WiringGapError, match="OHLCV cache configuration"):
        yfc.load_ohlcv("AAPL", "2026-06-01")

    # The other half of what the guard covers, and the reason ``makedirs``
    # is inside it: a key set to something that is not a path answers with a
    # TypeError, which outside the guard would read as the vendor's library.
    monkeypatch.setattr(soso, "get_config", lambda: {"data_cache_dir": 12345})
    with pytest.raises(WiringGapError, match="cache configuration"):
        soso._cache_dir()


@pytest.mark.unit
@pytest.mark.parametrize(
    "config_value, guard",
    [
        ({"news_article_limit": "ten"}, "news configuration"),
        ({"global_news_queries": "macro"}, "global news configuration"),
    ],
)
def test_a_configured_value_of_the_wrong_shape_names_its_own_guard(config_value, guard, monkeypatch):
    # Sent on as read, the first reaches Yahoo as the article count and comes
    # back as "No news found" — a coverage claim over a call that never asked
    # properly (#136) — and the second runs one search per character. Both
    # are our configuration, so both end at the guard that names it.
    import tradingagents.dataflows.yfinance_news as ynews

    monkeypatch.setattr(ynews.yf, "Ticker", lambda *a, **k: pytest.fail("no fetch may be made"))
    monkeypatch.setattr(ynews.yf, "Search", lambda *a, **k: pytest.fail("no fetch may be made"))
    set_config(config_value)
    # Anchored: "news configuration" is a substring of "global news
    # configuration", so an unanchored match would pass with the two guards
    # collapsed into one name and send an operator to the wrong key.
    with pytest.raises(WiringGapError) as info:
        if "news_article_limit" in config_value:
            ynews.get_news_yfinance("AAPL", "2026-06-01", "2026-06-05")
        else:
            ynews.get_global_news_yfinance("2026-06-01")
    assert str(info.value).startswith(f"{guard}: ")


@pytest.mark.unit
@pytest.mark.parametrize("configured", [0, -5])
def test_a_configured_article_count_below_one_asks_for_one(configured, monkeypatch):
    # The floor the Alpha Vantage sibling has always had. Sent on as read, a
    # zero reaches Yahoo as the article count and comes back as "No news
    # found" — a coverage claim over a call that never asked for an article
    # (#136). Measured at the count Yahoo is handed, since the value is not
    # visible anywhere else.
    import tradingagents.dataflows.yfinance_news as ynews

    asked: dict = {}

    class _Ticker:
        def __init__(self, *a, **k):
            pass

        def get_news(self, count):
            asked["count"] = count
            return []

    monkeypatch.setattr(ynews.yf, "Ticker", _Ticker)
    set_config({"news_article_limit": configured})
    ynews.get_news_yfinance("AAPL", "2026-06-01", "2026-06-05")
    assert asked["count"] == 1


@pytest.mark.unit
def test_the_success_path_is_untouched():
    assert _route(lambda *a, **k: "the report") == "the report"
