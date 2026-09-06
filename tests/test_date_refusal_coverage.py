"""Every routed getter that takes a date refuses an unusable one with the shared
sentinel — and WHICH getters must is derived from ``interface.VENDOR_METHODS``,
not hand-written (#140, item 8).

The three suites that pin the refusal (``test_unusable_date_parity``,
``test_optional_date_refusal``, ``test_yfinance_freshness``) each list the
getters they drive by hand, so a getter registered after them with no gate
ships green. PR #113 closed the same gap for the yfinance throttle taxonomy
with a call table whose membership must equal the registry
(``test_yfinance_rate_limit``); this is that lock for the date sentinel. It
lives in the tests rather than in ``route_to_vendor`` as #140 sketched because
the gates cannot leave the getters (direct callers and those suites reach them
without the router, so a router copy would be a second judgement to keep
aligned), because the fundamentals lanes judge the date AFTER the fetch by
design (an absent symbol outranks it, #89) and a pre-call gate would reorder
that, and because a new tool needs a row wherever the table lives — here a row
costs no coupling, and the router keeps not knowing where each getter's date
sits.

A row says what the sentinel is built from (the date parameters, the ``what``
and ``kind`` the vendors of one tool must share, whether ``None`` is the
omitted lane) and whether the date is judged before the vendor is asked —
which the seams measure either way. A date-less tool has to say so with an
explicit ``None`` row, and every row's date parameters are checked against the
impl's signature in both directions.
"""

import contextlib
import dataclasses
import inspect
import json
from collections.abc import Callable
from unittest import mock

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
from tests.test_optional_date_refusal import _no_network as _optional_no_network
from tests.test_unusable_date_parity import (
    _GOOD,
    _UNUSABLE,
    _no_network as _core_no_network,
    _VendorReached,
)
from tests.test_yfinance_freshness import _patch_av_request, _patch_ticker, _statement
from tradingagents.dataflows import interface
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.utils import _DATE_ARGUMENT_TAGS, DateKind, invalid_date_sentinel

_SUPPLIED_UNUSABLE = [v for v in _UNUSABLE if v is not None]
# The fiscal period the statement rows serve: on or before ``_GOOD``, so the
# usable-date call serves it (whole-answer equality on the refusal already
# rules out a row riding behind the sentence, so no future period is needed).
_PERIOD = "2026-03-31"
_AV_STATEMENT = json.dumps({"quarterlyReports": [{"fiscalDateEnding": _PERIOD}]})


@dataclasses.dataclass(frozen=True)
class _Row:
    """How one registered impl is driven, and what its refusal must say."""

    impl: Callable
    # Positional arguments from a ``{param: value}`` mapping of the date
    # arguments, so the row — not the test — knows where each date sits.
    args: Callable[[dict[str, object]], tuple]
    # The date parameters, first-judged first (a range names only the first
    # unusable one — pinned below by making them all unusable at once).
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

    @property
    def judged_after_fetch(self) -> bool:
        return self.serve is not None


def _point(impl, args, what, **kw):
    """The common row: one ``curr_date`` that bounds ``what`` to a point."""
    return _Row(impl, args, ("curr_date",), what, "point", **kw)


def _window(impl, what):
    return _Row(
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
        _patch_av_request(monkeypatch, body, reached)
        return reached

    return serve


# Keyed by the registry's own (method, vendor) pair. ``None`` declares a tool
# that takes no date at all — and is held to that by its signature below.
_DATE_CALLS: dict[tuple[str, str], _Row | None] = {
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
    ("get_fundamentals", "alpha_vantage"): _Row(
        avf.get_fundamentals,
        lambda d: ("AAPL", d["curr_date"]),
        ("curr_date",),
        "fundamentals",
        "disclosure",
        omitted_ok=True,
        serve=_serve_av(json.dumps({"Symbol": "AAPL"})),
    ),
    ("get_fundamentals", "yfinance"): _Row(
        yfin.get_fundamentals,
        lambda d: ("AAPL", d["curr_date"]),
        ("curr_date",),
        "fundamentals",
        "disclosure",
        omitted_ok=True,
        serve=lambda mp: _patch_ticker(mp, info={"longName": "Apple Inc.", "marketCap": 1_000_000}),
    ),
    ("get_balance_sheet", "alpha_vantage"): _statement_row(
        avf.get_balance_sheet, _serve_av(_AV_STATEMENT)
    ),
    ("get_balance_sheet", "yfinance"): _statement_row(
        yfin.get_balance_sheet,
        lambda mp: _patch_ticker(mp, quarterly_balance_sheet=_statement(_PERIOD)),
    ),
    ("get_cashflow", "alpha_vantage"): _statement_row(avf.get_cashflow, _serve_av(_AV_STATEMENT)),
    ("get_cashflow", "yfinance"): _statement_row(
        yfin.get_cashflow, lambda mp: _patch_ticker(mp, quarterly_cashflow=_statement(_PERIOD))
    ),
    ("get_income_statement", "alpha_vantage"): _statement_row(
        avf.get_income_statement, _serve_av(_AV_STATEMENT)
    ),
    ("get_income_statement", "yfinance"): _statement_row(
        yfin.get_income_statement,
        lambda mp: _patch_ticker(mp, quarterly_income_stmt=_statement(_PERIOD)),
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
    ("get_prediction_markets", "polymarket"): _Row(
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


def _registered(registry):
    return {(method, vendor) for method, vendors in registry.items() for vendor in vendors}


def _rows(*, dated: bool):
    """The table as pytest params, all rows or only the ones that take a date."""
    return [
        pytest.param(key, row, id=f"{key[0]}/{key[1]}")
        for key, row in _DATE_CALLS.items()
        if row is not None or not dated
    ]


def _dated_params():
    return [
        pytest.param(key, row, param, id=f"{key[0]}/{key[1]}/{param}")
        for key, row in _DATE_CALLS.items()
        if row
        for param in row.params
    ]


def _no_network(monkeypatch):
    """Every vendor seam the two direct-call suites patch, armed to raise into
    one list — plus the Alpha Vantage fundamentals request, which neither
    suite arms because no getter of theirs reaches it (its own suite serves
    it a body): without it a fundamentals row whose ``serve`` went missing
    would make a real request from here. An after-fetch row's ``serve``
    re-arms its own seam afterwards, so the list stays what a getter must
    NOT reach on any path."""
    reached = []
    _core_no_network(monkeypatch, reached)
    _optional_no_network(monkeypatch, reached)

    def _reached(*a, **k):
        reached.append(a)
        raise _VendorReached("the vendor was asked before the date was judged")

    monkeypatch.setattr(avf, "_make_api_request", _reached)
    return reached


def _call(row, **dates):
    """``row.impl`` over its dates, with the other date arguments usable."""
    filled = dict.fromkeys(row.params, _GOOD) | dates
    return row.impl(*row.args(filled))


def _not_refused(monkeypatch, reached, row, **dates):
    """A call with these dates gets past the gate — the lane's own answer is
    the getter's business (and pinned by its suite); here it only has to not
    be the refusal, and the vendor has to have been asked: for an after-fetch
    row through its served seam, for a before-fetch row through a raising
    one, which also proves the seam list covers this getter's path to its
    vendor."""
    if row.judged_after_fetch:
        served = row.serve(monkeypatch)
        out = _call(row, **dates)
        assert served, row.impl
        assert "INVALID_" not in out
        return
    # Only what a vendor lane reports counts as "asked, then failed": a
    # mis-wired row's TypeError must surface as itself. Deribit's per-half
    # helper swallows the seam's raise and reports its own DeribitError, a
    # VendorError; the yfinance leaves re-raise it as VendorLibraryError.
    out = None
    with contextlib.suppress(_VendorReached, VendorError):
        out = _call(row, **dates)
    assert reached, row.impl
    assert not (isinstance(out, str) and out.startswith("INVALID_")), out


@pytest.mark.unit
class TestTheTableIsTheRegistry:
    def test_every_registered_impl_has_a_row(self):
        assert set(_DATE_CALLS) == _registered(interface.VENDOR_METHODS)

    def test_the_served_period_is_within_the_usable_date(self):
        # ``_GOOD`` is the parity suite's; if it ever moves before the served
        # period, the statement rows' usable-date calls would filter the body
        # to nothing and fail as a getter regression rather than as this.
        assert _PERIOD <= _GOOD

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param({"get_unlisted_thing": {"yfinance": lambda: None}}, id="new_method"),
            pytest.param(
                {"get_fear_greed": {"alternative_me": lambda: None, "other": lambda: None}},
                id="new_vendor_of_a_listed_method",
            ),
        ],
    )
    def test_the_lock_catches_an_unlisted_registry_entry(self, extra):
        # Discrimination: whichever way the registry grows, a pair without a
        # row must fail here — that is the whole point of deriving the list.
        with mock.patch.dict(interface.VENDOR_METHODS, extra, clear=False):
            assert set(_DATE_CALLS) != _registered(interface.VENDOR_METHODS)

    @pytest.mark.parametrize("key,row", _rows(dated=True))
    def test_each_row_drives_the_registered_impl(self, key, row):
        # A row that drove some other function would pin nothing about the
        # impl the router actually calls.
        method, vendor = key
        assert row.impl is interface.VENDOR_METHODS[method][vendor]

    @pytest.mark.parametrize("key,row", _rows(dated=False))
    def test_each_row_drives_exactly_the_date_arguments_its_impl_takes(self, key, row):
        # Both directions: a row cannot name a date the impl does not take,
        # and — the direction that matters for coverage — an impl cannot take
        # a date the row never drives, else a getter could grow an ungated
        # date argument and ship green. "A date" is read off the name as a
        # token (``date``, ``*_date``, ``date_*`` — not a substring, which
        # ``validate`` or ``updated`` would trip), not off the closed tag
        # set, so an ``as_of_date`` nobody tagged fails here and forces the
        # tag decision ``_DATE_ARGUMENT_TAGS`` reserves; ``look_back_days``
        # is the only date-ish parameter today and is an int. A ``None`` row
        # is the claim that the impl takes no date at all, held to the same
        # check.
        method, vendor = key
        taken = inspect.signature(interface.VENDOR_METHODS[method][vendor]).parameters
        claimed = set(row.params) if row else set()
        assert claimed <= set(_DATE_ARGUMENT_TAGS), key
        assert {p for p in taken if "date" in p.lower().split("_")} == claimed, key

    def test_the_vendors_of_one_tool_share_the_sentence(self):
        # The agent cannot see which vendor answered (#89), so the parts of
        # the sentence that are the tool's — not the vendor's — must agree
        # across every vendor registered for it, and so must WHEN the date is
        # judged (a symbol the vendor lacks outranks the date on both
        # fundamentals vendors, or on neither).
        by_method: dict[str, set] = {}
        for (method, _vendor), row in _DATE_CALLS.items():
            shape = (
                None
                if row is None
                else (row.params, row.what, row.kind, row.omitted_ok, row.judged_after_fetch)
            )
            by_method.setdefault(method, set()).add(shape)
        disagreeing = {m: shapes for m, shapes in by_method.items() if len(shapes) > 1}
        assert not disagreeing


@pytest.mark.unit
class TestEveryRowRefuses:
    @pytest.mark.parametrize("value", _SUPPLIED_UNUSABLE)
    @pytest.mark.parametrize("key,row,param", _dated_params())
    def test_a_supplied_unusable_date_is_the_whole_answer(self, monkeypatch, key, row, param, value):
        reached = _no_network(monkeypatch)
        served = row.serve(monkeypatch) if row.judged_after_fetch else None
        # Whole-answer equality: nothing rides behind the refusal, and the
        # sentence names the parameter that was unusable.
        assert _call(row, **{param: value}) == invalid_date_sentinel(
            value, what=row.what, kind=row.kind, param=param
        )
        # The ordering the row claims, measured: no getter reached a raising
        # seam on any path, and an after-fetch one did reach its served one.
        assert not reached, key
        if row.judged_after_fetch:
            assert served, key

    @pytest.mark.parametrize("key,row", [p for p in _rows(dated=True) if len(p.values[1].params) > 1])
    def test_with_every_date_unusable_the_first_judged_is_named(self, monkeypatch, key, row):
        # One sentence asks for one fix, and which one is the tool's to say,
        # not the vendor's: two vendors naming different parameters would be
        # the #89 divergence in a new coat. The params tuple records the
        # order; this is what holds the getter to it.
        _no_network(monkeypatch)
        if row.judged_after_fetch:
            row.serve(monkeypatch)
        out = _call(row, **dict.fromkeys(row.params, "abc"))
        assert out == invalid_date_sentinel("abc", what=row.what, kind=row.kind, param=row.params[0])

    @pytest.mark.parametrize("key,row,param", _dated_params())
    def test_none_is_refused_unless_the_row_says_it_is_a_lane(self, monkeypatch, key, row, param):
        reached = _no_network(monkeypatch)
        if row.omitted_ok:
            _not_refused(monkeypatch, reached, row, **{param: None})
        else:
            assert _call(row, **{param: None}) == invalid_date_sentinel(
                None, what=row.what, kind=row.kind, param=param
            )

    @pytest.mark.parametrize("key,row", _rows(dated=True))
    def test_a_usable_date_is_not_refused(self, monkeypatch, key, row):
        # A gate that refused everything would pass the tests above.
        _not_refused(monkeypatch, _no_network(monkeypatch), row)
