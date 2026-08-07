"""Every ``@tool`` wrapper forwards the arguments it was handed, in order.

These wrappers are one-line adapters between the LLM's tool call and
``route_to_vendor``. Their bodies were invisible to the entire test suite: the
existing wiring tests assert tool NAMES and ToolNode membership
(``{t.name for t in llm.bound_tools}``, ``tools_by_name``), which cannot see a
wrong argument order, a dropped argument, or a body replaced wholesale.

A mutation sweep proved the exposure on the crypto tools — swapping
``get_options_market``'s two arguments passed all 1115 tests — and a same-concept
scan found the identical hole in eleven of the repo's fifteen wrappers. The
failures are silent rather than loud, which is why nothing surfaced them:

* For an OPTIONAL category (macro_data, prediction_markets, options_data,
  crypto_*), a swap makes the vendor raise, and ``route_to_vendor`` converts that
  into the ``DATA_UNAVAILABLE`` sentinel. The vendor is 100% broken on every
  cycle and the only trace is a ``logger.warning``.
* For the date-range tools, swapping ``start_date`` with ``end_date`` parses
  fine and returns an empty frame, which flows into the ordinary "no data" path
  rather than an error.
* For the three financial-statement tools, *dropping* ``curr_date`` from the
  forward turns off future-report filtering entirely — a look-ahead-bias leak
  with no symptom at all.
* ``get_indicators`` catches ``ValueError`` and pastes the message into the
  report text, so even a loud failure degrades to prose inside the market report.

Type annotations do not protect any of this: the arguments are overwhelmingly
same-typed, so a swap is type-correct.

Each test patches the wrapper module's own ``route_to_vendor`` with a recorder
and asserts the exact forwarded tuple. Defaults are asserted as they arrive at
the router, because the router — not the wrapper — owns what a missing window
means; a wrapper substituting its own number would silently override the
deployment's configuration.
"""

from unittest import mock

import pytest

from tradingagents.agents.utils import (
    core_stock_tools,
    crypto_data_tools,
    fundamental_data_tools,
    macro_data_tools,
    market_data_validation_tools,
    news_data_tools,
    prediction_markets_tools,
    technical_indicators_tools,
)

DATE = "2026-08-05"

# Every ``@tool`` in the repo, as (module, attribute name). The attribute name is
# also the name the tool advertises to the LLM and the key ``route_to_vendor``
# dispatches on, so the two must not drift.
ALL_WRAPPERS = [
    (core_stock_tools, "get_stock_data"),
    (crypto_data_tools, "get_etf_flows"),
    (crypto_data_tools, "get_fear_greed"),
    (crypto_data_tools, "get_options_market"),
    (fundamental_data_tools, "get_balance_sheet"),
    (fundamental_data_tools, "get_cashflow"),
    (fundamental_data_tools, "get_fundamentals"),
    (fundamental_data_tools, "get_income_statement"),
    (macro_data_tools, "get_macro_indicators"),
    (market_data_validation_tools, "get_verified_market_snapshot"),
    (news_data_tools, "get_global_news"),
    (news_data_tools, "get_insider_transactions"),
    (news_data_tools, "get_news"),
    (prediction_markets_tools, "get_prediction_markets"),
    (technical_indicators_tools, "get_indicators"),
]


def _recorder():
    """Return ``(calls, fake)``; ``fake`` records positional args and returns a marker."""
    calls = []

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        return "ROUTED"

    return calls, fake


def _forwarded(module, tool, payload, attr="route_to_vendor"):
    """Invoke ``tool`` with ``payload`` and return what the patched router saw."""
    calls, fake = _recorder()
    with mock.patch.object(module, attr, fake):
        result = tool.invoke(payload)
    assert result == "ROUTED", "the wrapper must return the router's result verbatim"
    assert len(calls) == 1, f"expected exactly one router call, got {calls}"
    args, kwargs = calls[0]
    assert kwargs == {}, "these wrappers forward positionally; a keyword here changes the contract"
    return args


@pytest.mark.unit
class TestWrapperInventory:
    def test_every_wrapper_advertises_its_own_function_name(self):
        # The @tool decorator takes an optional name override, so a tool can
        # advertise something other than its function name while the Python
        # attribute keeps working. Only get_options_market's name is pinned
        # elsewhere (by the ToolNode and bound-tools assertions); the other
        # fourteen could be renamed silently, and the LLM calls tools by the
        # advertised name.
        for module, attr in ALL_WRAPPERS:
            assert getattr(module, attr).name == attr, (
                f"{module.__name__}.{attr} advertises a different name to the LLM"
            )

    def test_the_inventory_lists_every_tool_in_the_repo(self):
        # Guards this file's stated job: to be the ONE place the whole @tool
        # surface is pinned, so a wrapper added later is visibly missing here
        # rather than quietly uncovered.
        import pathlib
        import re

        utils = pathlib.Path(__file__).resolve().parents[1] / "tradingagents" / "agents" / "utils"
        found = set()
        for path in utils.glob("*.py"):
            source = path.read_text(encoding="utf-8")
            found.update(re.findall(r"^@tool\s*\ndef (\w+)", source, re.MULTILINE))
        assert found == {attr for _, attr in ALL_WRAPPERS}


@pytest.mark.unit
class TestCoreAndNewsWrappers:
    def test_get_stock_data_forwards_symbol_start_then_end(self):
        # start/end swapped parses fine and yields an empty frame, which lands in
        # the clean "no data" path rather than raising — prices silently vanish.
        assert _forwarded(
            core_stock_tools,
            core_stock_tools.get_stock_data,
            {"symbol": "AAPL", "start_date": "2026-07-01", "end_date": DATE},
        ) == ("get_stock_data", "AAPL", "2026-07-01", DATE)

    def test_get_news_forwards_ticker_start_then_end(self):
        assert _forwarded(
            news_data_tools,
            news_data_tools.get_news,
            {"ticker": "AAPL", "start_date": "2026-07-01", "end_date": DATE},
        ) == ("get_news", "AAPL", "2026-07-01", DATE)

    def test_get_global_news_forwards_lookback_before_limit(self):
        # The worst swap in the repo: look_back_days and limit are adjacent, both
        # ``int | None``, both optional. Swapped, "50 articles over 7 days"
        # becomes "7 articles over 50 days" — no exception, no sentinel, no log
        # line, just wrong data in the news report.
        assert _forwarded(
            news_data_tools,
            news_data_tools.get_global_news,
            {"curr_date": DATE, "look_back_days": 7, "limit": 50},
        ) == ("get_global_news", DATE, 7, 50)

    def test_get_global_news_leaves_both_defaults_to_the_router(self):
        # Omitted, both must arrive as None: DEFAULT_CONFIG owns the real values
        # (global_news_lookback_days / _limit), so a wrapper substituting its own
        # number would silently override the deployment's configuration.
        assert _forwarded(
            news_data_tools, news_data_tools.get_global_news, {"curr_date": DATE}
        ) == ("get_global_news", DATE, None, None)

    def test_get_insider_transactions_forwards_its_only_argument(self):
        # One parameter, so no swap is possible — but the body is still never
        # otherwise executed, so ``return ""`` or a misspelt method name ships
        # green.
        assert _forwarded(
            news_data_tools,
            news_data_tools.get_insider_transactions,
            {"ticker": "AAPL"},
        ) == ("get_insider_transactions", "AAPL")


@pytest.mark.unit
class TestFundamentalWrappers:
    """``curr_date`` is the look-ahead guard, and it rides on the third argument."""

    TOOLS = [
        ("get_balance_sheet", "get_balance_sheet"),
        ("get_cashflow", "get_cashflow"),
        ("get_income_statement", "get_income_statement"),
    ]

    @pytest.mark.parametrize("attr,method", TOOLS)
    def test_statement_tools_forward_ticker_freq_then_curr_date(self, attr, method):
        # Dropping curr_date from the forward disables future-report filtering
        # entirely — a look-ahead-bias leak that produces no error, no sentinel
        # and no log line. Swapping freq with curr_date is nearly as quiet: the
        # vendor returns an error STRING, which lands in the LLM's context as
        # text rather than raising.
        assert _forwarded(
            fundamental_data_tools,
            getattr(fundamental_data_tools, attr),
            {"ticker": "AAPL", "freq": "annual", "curr_date": DATE},
        ) == (method, "AAPL", "annual", DATE)

    @pytest.mark.parametrize("attr,method", TOOLS)
    def test_statement_tools_pass_their_declared_defaults_through(self, attr, method):
        # freq defaults to "quarterly" and curr_date to None. Both are declared on
        # the wrapper, so both are its contract with the router.
        assert _forwarded(
            fundamental_data_tools, getattr(fundamental_data_tools, attr), {"ticker": "AAPL"}
        ) == (method, "AAPL", "quarterly", None)

    def test_get_fundamentals_forwards_ticker_then_curr_date(self):
        assert _forwarded(
            fundamental_data_tools,
            fundamental_data_tools.get_fundamentals,
            {"ticker": "AAPL", "curr_date": DATE},
        ) == ("get_fundamentals", "AAPL", DATE)


@pytest.mark.unit
class TestOptionalCategoryWrappers:
    """A swap here degrades to the DATA_UNAVAILABLE sentinel instead of raising."""

    def test_get_macro_indicators_forwards_indicator_curr_date_then_lookback(self):
        # macro_data is optional, so an indicator/curr_date swap makes FRED reject
        # the request and route_to_vendor converts that into the sentinel: a
        # totally dead vendor behind a green suite.
        assert _forwarded(
            macro_data_tools,
            macro_data_tools.get_macro_indicators,
            {"indicator": "cpi", "curr_date": DATE, "look_back_days": 365},
        ) == ("get_macro_indicators", "cpi", DATE, 365)

    def test_get_macro_indicators_leaves_the_window_default_to_the_router(self):
        assert _forwarded(
            macro_data_tools,
            macro_data_tools.get_macro_indicators,
            {"indicator": "cpi", "curr_date": DATE},
        ) == ("get_macro_indicators", "cpi", DATE, None)

    def test_get_prediction_markets_forwards_topic_then_limit(self):
        assert _forwarded(
            prediction_markets_tools,
            prediction_markets_tools.get_prediction_markets,
            {"topic": "fed rate", "limit": 3},
        ) == ("get_prediction_markets", "fed rate", 3)

    def test_get_prediction_markets_leaves_the_limit_default_to_the_router(self):
        assert _forwarded(
            prediction_markets_tools,
            prediction_markets_tools.get_prediction_markets,
            {"topic": "fed rate"},
        ) == ("get_prediction_markets", "fed rate", None)


@pytest.mark.unit
class TestIndicatorsWrapper:
    """The one wrapper with real logic, none of which ran in any test."""

    def test_a_comma_list_is_split_normalised_and_forwarded_one_call_per_indicator(self):
        # Pins the split, the .strip().lower() normalisation, the empty-token
        # skip, the argument order and the look_back_days default in one shot.
        calls, fake = _recorder()
        with mock.patch.object(technical_indicators_tools, "route_to_vendor", fake):
            out = technical_indicators_tools.get_indicators.invoke(
                {"symbol": "AAPL", "indicator": "rsi, MACD ,", "curr_date": DATE}
            )
        assert [args for args, _ in calls] == [
            ("get_indicators", "AAPL", "rsi", DATE, 30),
            ("get_indicators", "AAPL", "macd", DATE, 30),
        ]
        # Results are joined, not silently reduced to the last one.
        assert out == "ROUTED\n\nROUTED"

    def test_an_explicit_lookback_is_not_replaced_by_the_default(self):
        calls, fake = _recorder()
        with mock.patch.object(technical_indicators_tools, "route_to_vendor", fake):
            technical_indicators_tools.get_indicators.invoke(
                {"symbol": "AAPL", "indicator": "rsi", "curr_date": DATE, "look_back_days": 5}
            )
        assert [args for args, _ in calls] == [("get_indicators", "AAPL", "rsi", DATE, 5)]

    def test_a_rejected_indicator_becomes_report_text_rather_than_an_exception(self):
        # technical_indicators is NOT an optional category, so the router raises
        # loudly — but this wrapper catches ValueError and pastes the message into
        # the report. That is deliberate (one bad LLM-supplied indicator should
        # not cost the whole call), and it is also why a wrong argument order here
        # degrades to prose instead of failing: worth pinning so the behaviour is
        # a decision rather than an accident.
        def raiser(_method, _symbol, ind, *_rest):
            if ind == "bogus":
                raise ValueError(f"Unsupported indicator: {ind}")
            return "ROUTED"

        with mock.patch.object(technical_indicators_tools, "route_to_vendor", raiser):
            out = technical_indicators_tools.get_indicators.invoke(
                {"symbol": "AAPL", "indicator": "rsi,bogus", "curr_date": DATE}
            )
        assert out == "ROUTED\n\nUnsupported indicator: bogus"

    def test_only_a_valueerror_becomes_report_text(self):
        # The OTHER edge of that except clause, and the one that was unpinned:
        # deleting the try/except is caught by the test above, but WIDENING it to
        # `except Exception` shipped green. Widened, a VendorRateLimitError, a
        # KeyError, or a TypeError from a mis-forwarded argument would be pasted
        # into the market report as prose instead of failing — the silent
        # degradation this file exists to prevent, and it would also mask any
        # future argument-order regression in this same wrapper.
        def boom(*_args, **_kwargs):
            raise KeyError("unknown indicator")

        with (
            mock.patch.object(technical_indicators_tools, "route_to_vendor", boom),
            pytest.raises(KeyError),
        ):
            technical_indicators_tools.get_indicators.invoke(
                {"symbol": "AAPL", "indicator": "rsi", "curr_date": DATE}
            )


@pytest.mark.unit
class TestNonRouterWrappers:
    def test_verified_snapshot_forwards_symbol_curr_date_then_lookback(self):
        # This one calls build_verified_market_snapshot directly rather than the
        # router. An existing test invokes it, but never with look_back_days — so
        # dropping the third argument (silently resetting the window to the
        # builder's own default) shipped green.
        assert _forwarded(
            market_data_validation_tools,
            market_data_validation_tools.get_verified_market_snapshot,
            {"symbol": "AAPL", "curr_date": DATE, "look_back_days": 5},
            attr="build_verified_market_snapshot",
        ) == ("AAPL", DATE, 5)

    def test_verified_snapshot_passes_its_declared_default_through(self):
        assert _forwarded(
            market_data_validation_tools,
            market_data_validation_tools.get_verified_market_snapshot,
            {"symbol": "AAPL", "curr_date": DATE},
            attr="build_verified_market_snapshot",
        ) == ("AAPL", DATE, 30)


@pytest.mark.unit
class TestCryptoWrappersAreCoveredToo:
    """The three this scan started from, asserted here as well.

    The Deribit suite covers them in their own context; this is the inventory
    check that the ``@tool`` surface is pinned in ONE place, so a wrapper added
    later is visibly missing from this file rather than quietly uncovered.
    """

    def test_every_crypto_tool_forwards_its_arguments_in_order(self):
        assert _forwarded(
            crypto_data_tools,
            crypto_data_tools.get_options_market,
            {"asset": "BTC", "curr_date": DATE},
        ) == ("get_options_market", "BTC", DATE)
        assert _forwarded(
            crypto_data_tools,
            crypto_data_tools.get_etf_flows,
            {"asset": "BTC", "curr_date": DATE, "look_back_days": 30},
        ) == ("get_etf_flows", "BTC", DATE, 30)
        assert _forwarded(
            crypto_data_tools,
            crypto_data_tools.get_fear_greed,
            {"curr_date": DATE, "look_back_days": 30},
        ) == ("get_fear_greed", DATE, 30)

    def test_the_crypto_lookback_defaults_reach_the_router_as_none(self):
        # Passing 30 explicitly above cannot see the declared default: the
        # assertion holds identically whether it is None or 30. Every sibling
        # optional default in this file IS pinned by an omitted-argument payload,
        # so leaving these two out was an inconsistency, not a choice — and a
        # wrapper hardcoding 30 here would silently override the deployment's
        # configured ETF-flow and Fear & Greed windows.
        assert _forwarded(
            crypto_data_tools,
            crypto_data_tools.get_etf_flows,
            {"asset": "BTC", "curr_date": DATE},
        ) == ("get_etf_flows", "BTC", DATE, None)
        assert _forwarded(
            crypto_data_tools,
            crypto_data_tools.get_fear_greed,
            {"curr_date": DATE},
        ) == ("get_fear_greed", DATE, None)
