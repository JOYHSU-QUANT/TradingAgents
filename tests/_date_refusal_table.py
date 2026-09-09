"""The per-tool date-refusal contract, in one table, for every suite that pins it.

Not a test module (the leading underscore keeps pytest from collecting it):
the table and the vendor seams that drive it live here so that the suites
which read them — the coverage lock (``test_date_refusal_coverage``), the
sibling regressions (``test_unusable_date_parity``,
``test_optional_date_refusal``, ``test_yfinance_freshness``) and the tool
descriptions (``test_tool_wrappers``) — import one definition instead of each
hand-copying the getters, their ``what``/``kind`` and the shape of their
arguments (#230). Before this the coverage lock imported its seams FROM the
siblings, so a sibling could not read the table back without an import
cycle.

A row says what the sentinel is built from (the date parameters, the ``what``
and ``kind`` the vendors of one tool must share, whether ``None`` is the
omitted lane) and whether the date is judged before the vendor is asked —
which the seams measure either way. A date-less tool has to say so with an
explicit ``None`` row. Which rows must exist is the registry's business
(``interface.VENDOR_METHODS``), and the coverage lock holds the table to it.
"""

import contextlib
import dataclasses
import json
from collections.abc import Callable

import pandas as pd
import pytest

import tradingagents.dataflows.alpha_vantage_fundamentals as avf
import tradingagents.dataflows.alpha_vantage_indicator as avi
import tradingagents.dataflows.alpha_vantage_news as avn
import tradingagents.dataflows.alpha_vantage_stock as avs
import tradingagents.dataflows.deribit as deribit
import tradingagents.dataflows.farside as farside
import tradingagents.dataflows.fear_greed as fear_greed
import tradingagents.dataflows.fred as fred
import tradingagents.dataflows.polymarket as polymarket
import tradingagents.dataflows.sosovalue as sosovalue
import tradingagents.dataflows.sosovalue_macro as sosovalue_macro
import tradingagents.dataflows.sosovalue_treasuries as sosovalue_treasuries
import tradingagents.dataflows.y_finance as yfin
import tradingagents.dataflows.yfinance_news as yfnews
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.utils import DateKind

# A date every getter accepts, so the parameters a test is NOT refusing on
# are usable ones.
GOOD = "2026-06-05"
# The three values the vendors used to answer differently (#89; the CHANGELOG
# entry records what each one did), which every getter refuses. ``None`` is
# not a supplied value: each row says whether it is refused or is the omitted
# lane (#73, #139).
SUPPLIED_UNUSABLE = ("", "abc", "2026/08/18")
# The fiscal period the statement rows serve: on or before ``GOOD``, so the
# usable-date call serves it (whole-answer equality on the refusal already
# rules out a row riding behind the sentence, so no future period is needed).
PERIOD = "2026-03-31"


class VendorReached(Exception):
    """Raised by every raising network seam: reaching it is the failure."""


class FakeTicker:
    def __init__(self, **attrs):
        for k, v in attrs.items():
            setattr(self, k, v)


def patch_ticker(monkeypatch, **attrs):
    """A yfinance ``Ticker`` with the given attributes behind transparent fetch
    boundaries. Returns the list each fetch is appended to — at the boundaries
    (``yf_fetch_unhidden`` and ``yf_fetch_statement``, both made transparent),
    not at ``Ticker`` construction, which is lazy and asks the vendor nothing —
    for a caller that has to know the vendor WAS asked."""
    reached = []

    def _fetch(fn, **kw):
        reached.append(fn)
        return fn()

    monkeypatch.setattr(yfin.yf, "Ticker", lambda symbol: FakeTicker(**attrs))
    monkeypatch.setattr(yfin, "yf_fetch_unhidden", _fetch)
    monkeypatch.setattr(yfin, "yf_fetch_statement", _fetch)
    return reached


def statement(*cols, tz=None):
    """One-row yfinance statement frame with the given fiscal-period end
    columns — zoned when ``tz`` is given, as some yfinance builds label them."""
    return pd.DataFrame({pd.Timestamp(c, tz=tz): [100.0] for c in cols}, index=["Total Assets"])


def av_statement(*periods):
    """The Alpha Vantage statement body for the same fiscal periods, so a
    yfinance frame and an AV body "of one period" are a pair by construction."""
    return json.dumps({"quarterlyReports": [{"fiscalDateEnding": p} for p in periods]})


def patch_av_request(monkeypatch, body, reached=None):
    """Serve ``body`` as the Alpha Vantage fundamentals response, and hand back
    the module. ``reached``, when given, has each requested function name
    appended to it, for a caller that has to know the vendor WAS asked."""

    def _request(function_name, params):
        if reached is not None:
            reached.append(function_name)
        return body

    monkeypatch.setattr(avf, "_make_api_request", _request)
    return avf


@dataclasses.dataclass(frozen=True)
class Row:
    """How one registered impl is driven, and what its refusal must say."""

    impl: Callable
    # Positional arguments from a ``{param: value}`` mapping of the date
    # arguments, so the row — not the test — knows where each date sits.
    args: Callable[[dict[str, object]], tuple]
    # The date parameters, first-judged first (a range names only the first
    # unusable one — pinned by making them all unusable at once).
    params: tuple[str, ...]
    what: str
    kind: DateKind
    # ``None`` takes a date-less lane rather than being refused (#73, #139).
    omitted_ok: bool = False
    # ``None``: the date is judged before any vendor seam is reached, and the
    # raising seams prove it. Otherwise the getter judges the date AFTER the
    # fetch (the fundamentals lanes, #89) and this arms a seam that serves a
    # body and returns the list it records each fetch on — which the tests
    # then require to be non-empty, so the ordering the row claims is
    # measured rather than labelled.
    serve: Callable[[pytest.MonkeyPatch], list] | None = None

    def __post_init__(self):
        # A date-less tool is a ``None`` entry, never an empty row: an empty
        # ``params`` would sweep nothing and still read as a covered row. And
        # the omitted lane is a claim about one parameter; a window has two.
        if not self.params:
            raise ValueError("a row names at least one date parameter; a date-less tool is None")
        if self.omitted_ok and self.kind == "window":
            raise ValueError("omitted_ok is a point/disclosure lane; a window has none")

    @property
    def judged_after_fetch(self) -> bool:
        return self.serve is not None


def _point(impl, args, what, **kw):
    """The common row: one ``curr_date`` that bounds ``what`` to a point."""
    return Row(impl, args, ("curr_date",), what, "point", **kw)


def _window(impl, what):
    return Row(
        impl,
        lambda d: ("AAPL", d["start_date"], d["end_date"]),
        ("start_date", "end_date"),
        what,
        "window",
    )


def _statement_row(impl, serve):
    return _point(
        impl, lambda d: ("AAPL", "quarterly", d["curr_date"]), "fundamentals", omitted_ok=True, serve=serve
    )


def _serve_av(body):
    def serve(monkeypatch):
        reached = []
        patch_av_request(monkeypatch, body, reached)
        return reached

    return serve


# Shared by the three AV statement rows: each call allocates its own list.
_AV_SERVE = _serve_av(av_statement(PERIOD))


# Keyed by the registry's own (method, vendor) pair. ``None`` declares a tool
# that takes no date at all — and is held to that by its signature in the
# coverage lock. ``what`` is the noun the sentence names — article-free, since
# the window template supplies "the" itself; the vendors of one tool say the
# same one because the model cannot see which of them answered (#89).
DATE_CALLS: dict[tuple[str, str], Row | None] = {
    ("get_stock_data", "alpha_vantage"): _window(avs.get_stock, "stock price data"),
    ("get_stock_data", "yfinance"): _window(yfin.get_YFin_data_online, "stock price data"),
    ("get_indicators", "alpha_vantage"): _point(
        avi.get_indicator, lambda d: ("AAPL", "rsi", d["curr_date"], 30), "indicator values"
    ),
    ("get_indicators", "yfinance"): _point(
        yfin.get_stock_stats_indicators_window,
        lambda d: ("AAPL", "rsi", d["curr_date"], 30),
        "indicator values",
    ),
    ("get_fundamentals", "alpha_vantage"): Row(
        avf.get_fundamentals,
        lambda d: ("AAPL", d["curr_date"]),
        ("curr_date",),
        "fundamentals",
        "disclosure",
        omitted_ok=True,
        serve=_serve_av(json.dumps({"Symbol": "AAPL"})),
    ),
    ("get_fundamentals", "yfinance"): Row(
        yfin.get_fundamentals,
        lambda d: ("AAPL", d["curr_date"]),
        ("curr_date",),
        "fundamentals",
        "disclosure",
        omitted_ok=True,
        serve=lambda mp: patch_ticker(mp, info={"longName": "Apple Inc.", "marketCap": 1_000_000}),
    ),
    ("get_balance_sheet", "alpha_vantage"): _statement_row(avf.get_balance_sheet, _AV_SERVE),
    ("get_balance_sheet", "yfinance"): _statement_row(
        yfin.get_balance_sheet,
        lambda mp: patch_ticker(mp, quarterly_balance_sheet=statement(PERIOD)),
    ),
    ("get_cashflow", "alpha_vantage"): _statement_row(avf.get_cashflow, _AV_SERVE),
    ("get_cashflow", "yfinance"): _statement_row(
        yfin.get_cashflow, lambda mp: patch_ticker(mp, quarterly_cashflow=statement(PERIOD))
    ),
    ("get_income_statement", "alpha_vantage"): _statement_row(avf.get_income_statement, _AV_SERVE),
    ("get_income_statement", "yfinance"): _statement_row(
        yfin.get_income_statement,
        lambda mp: patch_ticker(mp, quarterly_income_stmt=statement(PERIOD)),
    ),
    ("get_news", "alpha_vantage"): _window(avn.get_news, "news"),
    ("get_news", "yfinance"): _window(yfnews.get_news_yfinance, "news"),
    ("get_global_news", "yfinance"): _point(
        yfnews.get_global_news_yfinance, lambda d: (d["curr_date"], 7), "global news"
    ),
    ("get_global_news", "alpha_vantage"): _point(
        avn.get_global_news, lambda d: (d["curr_date"], 7), "global news"
    ),
    ("get_insider_transactions", "alpha_vantage"): None,
    ("get_insider_transactions", "yfinance"): None,
    ("get_macro_indicators", "fred"): _point(
        fred.get_macro_data, lambda d: ("cpi", d["curr_date"], 90), "macro data"
    ),
    ("get_prediction_markets", "polymarket"): Row(
        polymarket.get_prediction_markets,
        lambda d: ("Fed", None, d["curr_date"]),
        ("curr_date",),
        "prediction-market probabilities",
        "disclosure",
        omitted_ok=True,
    ),
    ("get_etf_flows", "sosovalue"): _point(
        sosovalue.get_etf_flow_data, lambda d: ("BTC", d["curr_date"], 30), "ETF flows"
    ),
    ("get_etf_flows", "farside"): _point(
        farside.get_etf_flow_data, lambda d: ("BTC", d["curr_date"], 30), "ETF flows"
    ),
    ("get_fear_greed", "alternative_me"): _point(
        fear_greed.get_fear_greed_data, lambda d: (d["curr_date"], 30), "Fear & Greed readings"
    ),
    ("get_options_market", "deribit"): _point(
        deribit.get_options_market_data, lambda d: ("BTC", d["curr_date"]), "options market data"
    ),
    ("get_economic_calendar", "sosovalue"): _point(
        sosovalue_macro.get_economic_calendar_data,
        lambda d: (d["curr_date"], 30),
        "economic calendar data",
    ),
    ("get_btc_treasuries", "sosovalue"): _point(
        sosovalue_treasuries.get_btc_treasury_data,
        lambda d: ("BTC", d["curr_date"], 90),
        "BTC treasury holdings",
    ),
}


def rows(*, dated: bool, where: Callable[[Row], bool] | None = None):
    """The table as pytest params: every entry, only the dated rows, or only
    the dated rows ``where`` accepts."""

    def keep(row):
        if row is None:
            return not dated and where is None
        return where is None or where(row)

    return [
        pytest.param(key, row, id=f"{key[0]}/{key[1]}")
        for key, row in DATE_CALLS.items()
        if keep(row)
    ]


def dated_params():
    """One param per (row, date parameter), for the sweeps that refuse on each."""
    return [
        pytest.param(key, row, param, id=f"{key[0]}/{key[1]}/{param}")
        for key, row in DATE_CALLS.items()
        if row
        for param in row.params
    ]


def no_network(monkeypatch):
    """Every seam a getter in the table could reach its vendor through, armed
    to raise into the one list this returns.

    What becomes of the seam's error on the way out is the router's business,
    not the caller's (#187, #219), so "was the vendor asked?" is read from the
    list, never from the outcome. An after-fetch row's ``serve`` re-arms its
    own seam afterwards, so the list stays what a getter must NOT reach on any
    path — the Alpha Vantage fundamentals request is armed here for that
    reason: no before-fetch row reaches it, and without it a fundamentals row
    whose ``serve`` went missing would make a real request.
    """
    reached = []

    def _reached(*a, **k):
        reached.append(a)
        raise VendorReached("the vendor was asked before the date was judged")

    # The core categories' vendors (news, OHLCV, indicators, fundamentals).
    monkeypatch.setattr(yfnews.yf, "Ticker", _reached)
    monkeypatch.setattr(yfnews.yf, "Search", _reached)
    monkeypatch.setattr(yfin.yf, "Ticker", _reached)
    monkeypatch.setattr(yfin, "_get_stock_stats_bulk", _reached)
    monkeypatch.setattr(avn, "_make_api_request", _reached)
    monkeypatch.setattr(avs, "_make_api_request", _reached)
    monkeypatch.setattr(avi, "_make_api_request", _reached)
    monkeypatch.setattr(avf, "_make_api_request", _reached)
    # The fetch boundary wraps the yfinance calls; make it transparent so the
    # seam above is what fires.
    monkeypatch.setattr(yfnews, "yf_fetch_unhidden", lambda fn, **kw: fn())
    monkeypatch.setattr(yfin, "yf_fetch_unhidden", lambda fn, **kw: fn())
    # The first network-touching seam behind each optional-category getter.
    monkeypatch.setattr(fear_greed, "_request", _reached)
    monkeypatch.setattr(farside, "_load_flows", _reached)
    monkeypatch.setattr(sosovalue, "_load_snapshot", _reached)
    monkeypatch.setattr(sosovalue_macro, "_load_snapshot", _reached)
    monkeypatch.setattr(sosovalue_treasuries, "_load_snapshot", _reached)
    monkeypatch.setattr(deribit, "_request", _reached)
    monkeypatch.setattr(fred, "_request", _reached)
    monkeypatch.setattr(polymarket, "_request", _reached)
    return reached


def args_for(row, **dates):
    """The row's positional arguments over these dates, every other date usable."""
    return row.args(dict.fromkeys(row.params, GOOD) | dates)


def call(row, **dates):
    """``row.impl`` over its dates, with the other date arguments usable."""
    return row.impl(*args_for(row, **dates))


def not_refused(monkeypatch, reached, row, **dates):
    """A call with these dates gets past the gate — the lane's own answer is
    the getter's business (and pinned by its suite); here it only has to not
    be the refusal, and the vendor has to have been asked: for an after-fetch
    row through its served seam, for a before-fetch row through a raising
    one, which also proves the seam list covers this getter's path to its
    vendor."""
    if row.judged_after_fetch:
        served = row.serve(monkeypatch)
        out = call(row, **dates)
        assert served, row.impl
        assert "INVALID_" not in out
        return
    # Only what a vendor lane reports counts as "asked, then failed": a
    # mis-wired row's TypeError must surface as itself. Deribit's per-half
    # helper swallows the seam's raise and reports its own DeribitError, a
    # VendorError; every other getter lets the seam's raise out as itself,
    # for the router to classify (#219).
    out = None
    with contextlib.suppress(VendorReached, VendorError):
        out = call(row, **dates)
    assert reached, row.impl
    assert not (isinstance(out, str) and out.startswith("INVALID_")), out
