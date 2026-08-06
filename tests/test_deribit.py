"""Deribit options vendor: instrument parsing, Black-76 delta, strike-adjacent wing
interpolation, expiry selection, DVOL windowing and sample-size honesty, per-half
degradation, historical-date chain suppression, report rendering, router
integration, and market-analyst wiring.

All network access is mocked and the maths runs against trimmed local fixtures
(captured from the live public API), so these run without a network connection.
"""

import datetime as dt
import json
import logging
import os
from datetime import datetime, timezone
from unittest import mock

import pytest
import requests
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from tradingagents.agents.analysts.market_analyst import create_market_analyst
from tradingagents.dataflows import deribit, interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import VendorRateLimitError
from tradingagents.graph.trading_graph import TradingAgentsGraph

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str):
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# The captured chain holds three expiries: 5AUG26 (inside the 7-day floor),
# 28AUG26 (the ~30-day expiry these tests expect to win), and 25DEC26 (far).
CHAIN = _fixture("deribit_book_summary_btc.json")["result"]
DVOL = _fixture("deribit_dvol_btc.json")["result"]

# The instant the fixtures were captured. Every test pins the clock to it so the
# tenor arithmetic is deterministic.
NOW = datetime(2026, 8, 5, 6, 6, tzinfo=timezone.utc)
TODAY = "2026-08-05"

CHAIN_ENDPOINT = "get_book_summary_by_currency"
DVOL_ENDPOINT = "get_volatility_index_data"


@pytest.fixture
def options_enabled():
    """Turn the category on for the duration of a test.

    The shipped default is "none" (an opt-in cutover, so merging cannot silently
    change a running deployment's input surface), and `set_config` mutates a
    process-wide dict, so anything switching it on must switch it back.
    """
    set_config({"data_vendors": {"options_data": "deribit"}})
    yield
    set_config({"data_vendors": {"options_data": "none"}})


class _RequestRecorder:
    """A `_request` replacement that records params and dispatches on endpoint.

    Passing an Exception instance for either half makes that endpoint fail, which
    is how the per-half degradation tests knock one signal out.
    """

    def __init__(self, chain=CHAIN, dvol=DVOL):
        self._chain = chain
        self._dvol = dvol
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, endpoint, params):
        self.calls.append((endpoint, params))
        payload = {CHAIN_ENDPOINT: self._chain, DVOL_ENDPOINT: self._dvol}.get(endpoint)
        if payload is None and endpoint not in (CHAIN_ENDPOINT, DVOL_ENDPOINT):
            raise AssertionError(f"unexpected Deribit endpoint: {endpoint}")
        if isinstance(payload, Exception):
            raise payload
        return payload

    def params_for(self, endpoint: str) -> dict:
        for called, params in self.calls:
            if called == endpoint:
                return params
        raise AssertionError(f"{endpoint} was never requested")

    def endpoints(self) -> set[str]:
        return {endpoint for endpoint, _ in self.calls}


def _run_report(asset="BTC", curr_date=TODAY, chain=CHAIN, dvol=DVOL, now=NOW):
    """Render a report, returning ``(markdown, recorder)``."""
    recorder = _RequestRecorder(chain, dvol)
    with (
        mock.patch.object(deribit, "_request", side_effect=recorder),
        mock.patch.object(deribit, "_utc_now", return_value=now),
    ):
        return deribit.get_options_market_data(asset, curr_date), recorder


def _report(**kwargs):
    return _run_report(**kwargs)[0]


def _run_report_with_clocks(clocks, asset="BTC", curr_date=TODAY, chain=CHAIN, dvol=DVOL):
    """Render a report where ``_utc_now`` returns each of ``clocks`` in turn.

    ``get_options_market_data`` reads the clock TWICE on the chain-serving path —
    once up front and once immediately before the chain fetch — and the second
    instant is the one that dates the snapshot and decides the historical rule.
    ``_run_report``'s single ``return_value`` makes the two indistinguishable.
    """
    recorder = _RequestRecorder(chain, dvol)
    with (
        mock.patch.object(deribit, "_request", side_effect=recorder),
        mock.patch.object(deribit, "_utc_now", side_effect=list(clocks)),
    ):
        return deribit.get_options_market_data(asset, curr_date), recorder


def _candle(day: str, close: float):
    """A DVOL 1D candle stamped at midnight UTC of ``day`` (as Deribit stamps them)."""
    ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return [ts, close, close, close, close]


def _candle_at(when: datetime, close: float):
    """A DVOL candle stamped at an EXPLICIT UTC instant rather than at midnight.

    ``_candle``'s midnight stamp is exactly the instant at which a naive
    timestamp conversion is hardest to see going wrong, so the timezone test
    needs to place candles either side of UTC midnight instead.
    """
    return [int(when.timestamp() * 1000), close, close, close, close]


def _dvol_days(count: int, end: str = TODAY, start_value: float = 40.0):
    """``count`` consecutive daily candles ending at ``end``, each 0.1 above the last."""
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    return {
        "data": [
            _candle(
                (end_dt - dt.timedelta(days=count - 1 - i)).strftime("%Y-%m-%d"),
                start_value + i * 0.1,
            )
            for i in range(count)
        ]
    }


def _dvol_falling_days(count: int, end: str = TODAY, start_value: float = 100.0):
    """The mirror of ``_dvol_days``: each candle 0.1 BELOW the last.

    The newest reading is then the sample MINIMUM, which is the one-year-low case
    the percentile's floor-at-1st exists for. ``_dvol_days`` only ever climbs, so
    every ascending series puts the latest reading at the 100th percentile.
    """
    end_dt = datetime.strptime(end, "%Y-%m-%d")
    return {
        "data": [
            _candle(
                (end_dt - dt.timedelta(days=count - 1 - i)).strftime("%Y-%m-%d"),
                start_value - i * 0.1,
            )
            for i in range(count)
        ]
    }


def _ladder(
    name: str,
    days_out: float,
    call_max: float | None = None,
    put_min: float | None = None,
    iv: float = 30.0,
):
    """A full strike ladder for one synthetic expiry, optionally truncated per side.

    ``call_max`` stops the call side at that strike, which is how an expiry is made
    unable to reach down to 0.25 call delta while its put wing stays intact.
    ``put_min`` does the mirror image for the put side — without it every fallback
    test cripples the same side, and an acceptance test that checks only the call
    wing looks fully covered.
    """
    return [
        _synthetic_contract(name, days_out, strike=float(k), is_call=is_call, iv=iv)
        for k in range(58000, 73000, 1000)
        for is_call in (True, False)
        if not (is_call and call_max is not None and k > call_max)
        and not (not is_call and put_min is not None and k < put_min)
    ]


def _synthetic_contract(name: str, days_out: float, strike=64000.0, is_call=True, iv=30.0):
    return deribit.Contract(
        expiry=name,
        expiry_dt=NOW + dt.timedelta(days=days_out),
        strike=strike,
        is_call=is_call,
        mark_iv=iv,
        underlying=64000.0,
    )


# --------------------------------------------------------------------------- #
# Instrument-name parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseInstrumentName:
    def test_two_digit_day_call(self):
        token, expiry, strike, is_call = deribit.parse_instrument_name("BTC-28AUG26-100000-C")
        assert token == "28AUG26"
        assert expiry == datetime(2026, 8, 28, 8, tzinfo=timezone.utc)
        assert strike == 100000.0
        assert is_call is True

    def test_one_digit_day_put(self):
        token, expiry, strike, is_call = deribit.parse_instrument_name("ETH-5AUG26-1900-P")
        assert token == "5AUG26"
        assert expiry == datetime(2026, 8, 5, 8, tzinfo=timezone.utc)
        assert strike == 1900.0
        assert is_call is False

    def test_future_and_perpetual_names_are_skipped(self):
        # Not options: fewer than four dash-separated parts.
        assert deribit.parse_instrument_name("BTC-PERPETUAL") is None
        assert deribit.parse_instrument_name("BTC-28AUG26") is None

    def test_a_name_with_more_than_four_parts_is_skipped(self):
        # A combo leg, which the docstring promises to skip. A `< 4` shape test
        # would let this reach the unpack and raise ValueError out of parse_chain,
        # costing the whole chain half on every fetch.
        assert deribit.parse_instrument_name("BTC-28AUG26-100000-C-EXTRA") is None

    def test_unknown_option_type_is_skipped(self):
        assert deribit.parse_instrument_name("BTC-28AUG26-100000-X") is None

    def test_decimal_strike_form_is_skipped(self):
        # Deribit writes sub-1 strikes with a 'd' separator on currencies this
        # module does not serve; it must be skipped, not mis-parsed.
        assert deribit.parse_instrument_name("XRP-28AUG26-0d5-C") is None

    def test_non_positive_or_unparseable_strike_is_skipped(self):
        assert deribit.parse_instrument_name("BTC-28AUG26-0-C") is None
        assert deribit.parse_instrument_name("BTC-28AUG26-abc-C") is None

    def test_non_string_is_skipped(self):
        assert deribit.parse_instrument_name(None) is None
        assert deribit.parse_instrument_name(12345) is None

    def test_bad_month_and_impossible_date_are_skipped(self):
        assert deribit.parse_expiry("28XXX26") is None
        assert deribit.parse_expiry("31FEB26") is None
        assert deribit.parse_expiry("AUG26") is None

    def test_an_over_long_token_is_skipped_not_mis_dated(self):
        # The 6/7-character guard had no coverage in either direction: widened to
        # allow 8, "031AUG26" silently parses as 31 August rather than being
        # skipped, which is exactly the mis-dating the docstring rules out.
        assert deribit.parse_expiry("031AUG26") is None
        assert deribit.parse_expiry("5AUG26") is not None
        assert deribit.parse_expiry("28AUG26") is not None

    def test_expiry_settles_at_0800_utc(self):
        assert deribit.parse_expiry("5AUG26").hour == 8


# --------------------------------------------------------------------------- #
# Chain parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseChain:
    def test_fixture_parses_every_held_contract(self):
        # One fixture row (BTC-5AUG26-65000-P) carries open_interest 0.0 alongside
        # a confident mark_iv and a bid/ask spanning two orders of magnitude — the
        # archetype the open-interest filter exists for — so it is dropped.
        contracts = deribit.parse_chain(CHAIN, NOW)
        assert len(contracts) == len(CHAIN) - 1
        assert {c.expiry for c in contracts} == {"5AUG26", "28AUG26", "25DEC26"}
        assert (65000.0, "5AUG26", False) not in {
            (c.strike, c.expiry, c.is_call) for c in contracts
        }

    def test_a_contract_nobody_holds_is_dropped(self):
        # Zero open interest is where stale and modelled marks live, and such a
        # quote can be perfectly monotone with its neighbours — so it passes both
        # wing guards and prints the exact strikes a reader expects.
        held = {
            "instrument_name": "BTC-28AUG26-64000-C",
            "mark_iv": 30,
            "underlying_price": 64000,
            "open_interest": 12.5,
        }
        unheld = dict(held, instrument_name="BTC-28AUG26-65000-C", open_interest=0.0)
        missing = dict(held, instrument_name="BTC-28AUG26-66000-C")
        del missing["open_interest"]
        contracts = deribit.parse_chain([held, unheld, missing], NOW)
        assert [c.strike for c in contracts] == [64000.0]

    def test_already_expired_contracts_are_dropped(self):
        # 5AUG26 settles at 08:00 UTC, so an hour later it is gone while the
        # later expiries survive.
        after_expiry = datetime(2026, 8, 5, 9, tzinfo=timezone.utc)
        expiries = {c.expiry for c in deribit.parse_chain(CHAIN, after_expiry)}
        assert "5AUG26" not in expiries
        assert "28AUG26" in expiries

    def test_contract_expiring_exactly_now_is_dropped(self):
        # Settlement is not a tradeable quote; the boundary must exclude it.
        at_expiry = datetime(2026, 8, 5, 8, tzinfo=timezone.utc)
        assert "5AUG26" not in {c.expiry for c in deribit.parse_chain(CHAIN, at_expiry)}

    def test_unusable_rows_are_skipped_not_fatal(self):
        oi = {"open_interest": 10.0}
        rows = [
            {
                "instrument_name": "BTC-28AUG26-64000-C",
                "mark_iv": None,
                "underlying_price": 64000,
                **oi,
            },
            {
                "instrument_name": "BTC-28AUG26-65000-C",
                "mark_iv": 0,
                "underlying_price": 64000,
                **oi,
            },
            {"instrument_name": "BTC-28AUG26-66000-C", "mark_iv": 30, "underlying_price": 0, **oi},
            {
                "instrument_name": "BTC-28AUG26-67000-C",
                "mark_iv": True,
                "underlying_price": 64000,
                **oi,
            },
            {
                "instrument_name": "BTC-28AUG26-69000-C",
                "mark_iv": 30,
                "underlying_price": 64000,
                "open_interest": True,
            },
            {"instrument_name": "BTC-PERPETUAL", "mark_iv": 30, "underlying_price": 64000, **oi},
            "not a dict",
            {
                "instrument_name": "BTC-28AUG26-68000-C",
                "mark_iv": 30,
                "underlying_price": 64000,
                **oi,
            },
        ]
        contracts = deribit.parse_chain(rows, NOW)
        assert [c.strike for c in contracts] == [68000.0]

    def test_non_list_payload_is_fatal(self):
        # A response-shape change must fail loud, not silently produce an empty
        # chain that would read as "no options listed".
        with pytest.raises(deribit.DeribitError, match="expected a list"):
            deribit.parse_chain({"result": []}, NOW)


# --------------------------------------------------------------------------- #
# Black-76 delta
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestBlackScholesDelta:
    def test_atm_forward_call_matches_hand_calculation(self):
        # F = K = 100, sigma = 50%, T = 0.25 -> d1 = 0.5*0.25*0.25 / (0.5*0.5) = 0.125,
        # and N(0.125) = 0.549738.
        assert deribit.black_scholes_delta(100.0, 100.0, 50.0, 0.25, True) == pytest.approx(
            0.549738, abs=1e-6
        )

    def test_otm_call_matches_hand_calculation(self):
        # d1 = (ln(100/120) + 0.03125) / 0.25 = -0.604287, N(-0.604287) = 0.272827.
        assert deribit.black_scholes_delta(100.0, 120.0, 50.0, 0.25, True) == pytest.approx(
            0.272827, abs=1e-6
        )

    def test_put_call_parity_holds(self):
        call = deribit.black_scholes_delta(100.0, 110.0, 40.0, 0.1, True)
        put = deribit.black_scholes_delta(100.0, 110.0, 40.0, 0.1, False)
        assert call - put == pytest.approx(1.0, abs=1e-12)

    def test_call_delta_in_unit_interval_and_put_negative(self):
        call = deribit.black_scholes_delta(64000.0, 68000.0, 29.55, 0.063, True)
        put = deribit.black_scholes_delta(64000.0, 61000.0, 34.34, 0.063, False)
        assert 0.0 < call < 1.0
        assert -1.0 < put < 0.0

    @pytest.mark.parametrize(
        "args",
        [
            (0.0, 100.0, 50.0, 0.25),  # no forward
            (100.0, 0.0, 50.0, 0.25),  # no strike
            (100.0, 100.0, 0.0, 0.25),  # no vol
            (100.0, 100.0, 50.0, 0.0),  # expired
            (100.0, 100.0, 50.0, -0.25),  # negative tenor
            (float("nan"), 100.0, 50.0, 0.25),
            (float("inf"), 100.0, 50.0, 0.25),
        ],
    )
    def test_degenerate_inputs_return_none(self, args):
        assert deribit.black_scholes_delta(*args, True) is None


# --------------------------------------------------------------------------- #
# Wing interpolation (strike-adjacent)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestInterpolateIvAtDelta:
    def test_interpolates_between_the_bracketing_quotes(self):
        # 0.25 is the midpoint of [0.40, 0.10], so the answer is the midpoint of
        # the two vols — a value neither quote carries, which is what separates
        # real interpolation from picking the nearest quote.
        quotes = [(100.0, 0.40, 50.0), (110.0, 0.10, 20.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0

    def test_reports_the_strikes_it_interpolated_between(self):
        quotes = [(100.0, 0.40, 50.0), (110.0, 0.10, 20.0)]
        wing = deribit.interpolate_iv_at_delta(quotes, 0.25)
        assert (wing.strike_low, wing.strike_high) == (100.0, 110.0)

    def test_uses_the_strike_adjacent_pair_not_the_delta_nearest_one(self):
        # The 105 quote's delta says 0.26 but it sits between two strikes whose
        # deltas straddle 0.25 much more tightly. Ordering by delta would pair 105
        # with 110 across a strike gap; ordering by strike keeps 105-110 adjacent.
        quotes = [(100.0, 0.40, 50.0), (105.0, 0.26, 40.0), (110.0, 0.10, 20.0)]
        wing = deribit.interpolate_iv_at_delta(quotes, 0.25)
        assert (wing.strike_low, wing.strike_high) == (105.0, 110.0)

    def test_unordered_input_is_sorted_by_strike(self):
        quotes = [(110.0, 0.10, 20.0), (100.0, 0.40, 50.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0

    def test_exact_hit_on_a_quote_returns_that_quote(self):
        quotes = [(100.0, 0.25, 33.0), (110.0, 0.10, 20.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 33.0

    def test_negative_deltas_interpolate_the_same_way(self):
        quotes = [(100.0, -0.10, 20.0), (110.0, -0.40, 50.0)]
        assert deribit.interpolate_iv_at_delta(quotes, -0.25).iv == 35.0

    def test_an_inverted_step_is_refused_not_interpolated(self):
        # Delta rising with strike is not a smile. The pair is *seen* as a bracket
        # (the span test does not assume a direction, so the guard gets to judge
        # it) and is then refused rather than interpolated.
        quotes = [(100.0, 0.10, 20.0), (110.0, 0.40, 50.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25) is None

    def test_an_inversion_bordering_the_bracket_also_refuses(self):
        # The corrupt quote need not be inside the bracket: the guard looks one
        # quote past each end, because a contract that mis-prices its neighbour's
        # side of the smile is the same signal.
        quotes = [(90.0, 0.60, 60.0), (95.0, 0.30, 40.0), (100.0, 0.40, 50.0), (110.0, 0.10, 20.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25) is None

    def test_an_inversion_far_from_the_bracket_does_not_veto_it(self):
        # ... but a rounding inversion between two deep-wing strikes says nothing
        # about a bracket further along, and must not cost the whole surface.
        quotes = [
            (70.0, 0.90, 80.0),
            (75.0, 0.95, 85.0),  # inverted, four strikes away from the bracket
            (90.0, 0.60, 60.0),
            (100.0, 0.40, 50.0),
            (110.0, 0.10, 20.0),
        ]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0

    def test_an_inversion_two_quotes_before_the_bracket_does_not_veto_it(self):
        # The guard's window is documented as "the bracket plus one quote on each
        # side", but nothing pinned either edge: widening it to `index - 2` left
        # the whole suite green, because the test above puts its inversion at the
        # very head of the list where a two-wide window still only half reaches
        # it. Here the inverted PAIR sits exactly one step beyond the left edge,
        # so a window that starts at `index - 2` swallows it and vetoes a bracket
        # a rounding artefact three strikes away says nothing about.
        quotes = [
            (80.0, 0.70, 70.0),
            (90.0, 0.80, 75.0),  # inverted, one quote past the window's left edge
            (100.0, 0.40, 50.0),
            (110.0, 0.10, 20.0),
            (120.0, 0.05, 10.0),
        ]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0

    def test_an_inversion_two_quotes_after_the_bracket_does_not_veto_it(self):
        # The right-edge mirror. The window ends at `index + 3`, one quote past
        # the bracket; at `index + 4` this deep-wing inversion costs a bracket
        # that has three clean quotes between it and the corruption.
        quotes = [
            (90.0, 0.60, 60.0),
            (100.0, 0.40, 50.0),
            (110.0, 0.10, 20.0),
            (120.0, 0.05, 10.0),
            (130.0, 0.09, 12.0),  # inverted, one quote past the window's right edge
        ]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0

    def test_an_inversion_one_quote_after_the_bracket_vetoes_it(self):
        # ... and the inclusive end of that same window, which is the half the
        # bordering-inversion test above does NOT cover: it corrupts the quote
        # BELOW the bracket, so narrowing the window to `index + 2` — blind to
        # everything past the bracket's own high strike — kept it green while a
        # quote mis-pricing the smile immediately above the wing went unjudged.
        quotes = [
            (90.0, 0.60, 60.0),
            (100.0, 0.40, 50.0),
            (110.0, 0.10, 20.0),
            (120.0, 0.15, 25.0),  # inverted, inside the window
        ]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25) is None

    def test_no_bracket_returns_none_rather_than_extrapolating(self):
        # Every quote sits inside 0.25; extrapolating would invent an unlisted vol.
        assert (
            deribit.interpolate_iv_at_delta([(100.0, 0.20, 20.0), (110.0, 0.05, 10.0)], 0.25)
            is None
        )
        assert (
            deribit.interpolate_iv_at_delta([(100.0, 0.90, 90.0), (110.0, 0.30, 40.0)], 0.25)
            is None
        )

    def test_single_quote_cannot_bracket(self):
        assert deribit.interpolate_iv_at_delta([(100.0, 0.25, 33.0)], 0.25) is None
        assert deribit.interpolate_iv_at_delta([], 0.25) is None

    def test_duplicate_delta_uses_the_midpoint_without_dividing_by_zero(self):
        quotes = [(100.0, 0.25, 30.0), (110.0, 0.25, 40.0)]
        assert deribit.interpolate_iv_at_delta(quotes, 0.25).iv == 35.0


# --------------------------------------------------------------------------- #
# Expiry selection
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRankExpiries:
    def test_picks_the_expiry_nearest_thirty_days(self):
        contracts = deribit.parse_chain(CHAIN, NOW)
        # 5AUG26 is ~0 days out, 28AUG26 ~23, 25DEC26 ~142. Only 28AUG26 is inside
        # the eligible band, and it is also the nearest to the target.
        assert deribit.rank_expiries(contracts, NOW)[0] == "28AUG26"

    def test_target_tenor_is_thirty_days_not_some_other_number(self):
        # The pin is the literal constant. The expiry set below is chosen to sit
        # INSIDE the eligible band on both sides of the target, so it discriminates
        # the target itself rather than the band edges.
        assert deribit.TARGET_DTE_DAYS == 30
        near = _synthetic_contract("NEAR", 17)
        mid = _synthetic_contract("MID", 31)
        far = _synthetic_contract("FAR", 44)
        assert deribit.rank_expiries([near, mid, far], NOW)[0] == "MID"

    def test_ranking_orders_every_qualifying_expiry_best_first(self):
        # compute_skew steps down this order when the nearest expiry cannot bracket
        # a wing, so the tail of the ranking is load-bearing, not just its head.
        near = _synthetic_contract("NEAR", 17)
        mid = _synthetic_contract("MID", 31)
        far = _synthetic_contract("FAR", 44)
        # 31d is 1 from target, 17d is 13 away, 44d is 14 away.
        assert deribit.rank_expiries([far, near, mid], NOW) == ["MID", "NEAR", "FAR"]
        assert deribit.rank_expiries([_synthetic_contract("TOO_NEAR", 3)], NOW) == []

    def test_the_eligible_band_is_bounded_on_both_sides(self):
        # The band constants themselves, and the derivation that keeps them in
        # agreement. Pinned literally: every other test in this class only
        # discriminates a RANGE of values, so none of them alone can stand in for
        # the constants.
        assert deribit.MAX_TENOR_DISTANCE_DAYS == 15
        assert deribit.MIN_ELIGIBLE_DTE_DAYS == 15
        assert deribit.MAX_ELIGIBLE_DTE_DAYS == 45
        assert (
            max(deribit.MIN_DTE_DAYS, deribit.TARGET_DTE_DAYS - deribit.MAX_TENOR_DISTANCE_DAYS)
            == deribit.MIN_ELIGIBLE_DTE_DAYS
        )
        assert deribit.MAX_ELIGIBLE_DTE_DAYS == (
            deribit.TARGET_DTE_DAYS + deribit.MAX_TENOR_DISTANCE_DAYS
        )

    def test_the_pin_noise_floor_still_binds_if_the_band_is_widened(self):
        # MIN_DTE_DAYS is currently subsumed (15 > 7), which is exactly how a floor
        # quietly stops existing. The max() is what keeps it real, so pin the
        # derivation at a width where the floor is the binding bound.
        assert max(deribit.MIN_DTE_DAYS, deribit.TARGET_DTE_DAYS - 25) == deribit.MIN_DTE_DAYS

    def test_an_expiry_beyond_the_ceiling_is_excluded_even_when_it_is_the_only_one(self):
        # The whole point of the ceiling: a thinned book must yield NO skew rather
        # than answer the ~30-day question with a 96-day risk reversal.
        assert deribit.rank_expiries([_synthetic_contract("FAR_OUT", 96)], NOW) == []

    def test_neither_bound_is_reachable_by_the_other(self):
        # Both edges, and the first ineligible value outside each. 45 in / 45.01
        # out proves the ceiling is not merely the floor restated.
        assert deribit.rank_expiries([_synthetic_contract("AT_FLOOR", 15)], NOW) == ["AT_FLOOR"]
        assert deribit.rank_expiries([_synthetic_contract("UNDER", 14.99)], NOW) == []
        assert deribit.rank_expiries([_synthetic_contract("AT_CEIL", 45)], NOW) == ["AT_CEIL"]
        assert deribit.rank_expiries([_synthetic_contract("OVER", 45.01)], NOW) == []

    def test_an_expiry_inside_the_floor_is_excluded(self):
        # With only the out-of-band expiries listed, nothing qualifies: 5AUG26 is
        # ~0 days out and 25DEC26 ~142, so the near one loses to the floor and the
        # far one to the ceiling. Before the ceiling existed this returned 25DEC26.
        contracts = [
            c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry in ("5AUG26", "25DEC26")
        ]
        assert deribit.rank_expiries(contracts, NOW) == []

    def test_returns_empty_when_nothing_is_eligible(self):
        contracts = [c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry == "5AUG26"]
        assert deribit.rank_expiries(contracts, NOW) == []

    def test_tie_goes_to_the_longer_dated_expiry(self):
        # 23 and 37 days are equidistant from the 30-day target; the pin-noise
        # rationale that motivates the floor also breaks the tie.
        near = _synthetic_contract("NEAR", 23)
        far = _synthetic_contract("FAR", 37)
        assert deribit.rank_expiries([near, far], NOW)[0] == "FAR"
        assert deribit.rank_expiries([far, near], NOW)[0] == "FAR"

    def test_the_tenor_key_is_not_floored_to_whole_days(self):
        # A "// 86400" ranking key collapses tenors that differ by under a day, so
        # the longer-dated tie-break then resolves by dict insertion order instead
        # of by tenor. Both of these floor to day 30, and LATE is genuinely nearer
        # the target, so it must win from either input order.
        early = _synthetic_contract("EARLY", 30.9)
        late = _synthetic_contract("LATE", 30.1)
        assert deribit.rank_expiries([early, late], NOW) == ["LATE", "EARLY"]
        assert deribit.rank_expiries([late, early], NOW) == ["LATE", "EARLY"]

    def test_select_expiry_is_gone(self):
        # Deleted as dead production API: compute_skew calls rank_expiries
        # directly, and the only callers were this test class. Its docstring had
        # already drifted into describing compute_skew's behaviour.
        assert not hasattr(deribit, "select_expiry")


# --------------------------------------------------------------------------- #
# Skew computation
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestComputeSkew:
    def test_fixture_surface_matches_hand_interpolation(self):
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)
        assert skew.expiry == "28AUG26"
        assert skew.days_to_expiry == pytest.approx(23.08, abs=0.05)
        assert skew.forward == pytest.approx(64456.93, abs=0.5)
        # Interpolated between the 64000 (0.551 delta, 31.58% IV) and 65000
        # (0.472, 30.88%) calls, so a value neither quote carries.
        assert skew.atm.iv == pytest.approx(31.12, abs=0.05)
        assert (skew.atm.strike_low, skew.atm.strike_high) == (64000.0, 65000.0)
        assert skew.call_25.iv == pytest.approx(29.56, abs=0.05)
        assert (skew.call_25.strike_low, skew.call_25.strike_high) == (67000.0, 68000.0)
        assert skew.put_25.iv == pytest.approx(34.30, abs=0.05)
        assert (skew.put_25.strike_low, skew.put_25.strike_high) == (61000.0, 62000.0)
        assert skew.n_calls == 8
        assert skew.n_puts == 8

    def test_rr25_is_negative_on_this_put_skewed_chain(self):
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)
        assert skew.rr25 == pytest.approx(-4.74, abs=0.05)

    @staticmethod
    def _with_broken_quote(instrument: str, mark_iv: float):
        rows = [dict(row) for row in CHAIN]
        for row in rows:
            if row["instrument_name"] == instrument:
                row["mark_iv"] = mark_iv
        return deribit.compute_skew(deribit.parse_chain(rows, NOW), NOW)

    @pytest.mark.parametrize(
        "instrument,mark_iv",
        [
            # Every value here was verified to produce a wrong wing before the
            # monotonicity guard existed. The first two hijack the CALL side; the
            # last three hijack the PUT side badly enough to flip RR25's sign, so
            # the reading line would have stated the opposite volatility regime.
            ("BTC-28AUG26-65000-C", 4.5),  # was call 6.11%, RR25 -28.19
            ("BTC-28AUG26-67000-C", 5.0),  # was call 18.67%, RR25 -15.63
            ("BTC-28AUG26-67000-C", 300.0),  # was call 31.46% on the right bracket
            ("BTC-28AUG26-61000-P", 0.5),  # was put 27.23%, RR25 +2.33 (sign flip)
            ("BTC-28AUG26-62000-P", 5.0),  # was put 19.79%, RR25 +9.77 (sign flip)
            ("BTC-28AUG26-61000-P", 300.0),  # was put 143.10%, RR25 -113.54
        ],
    )
    def test_a_corrupt_quote_never_produces_a_wing(self, instrument, mark_iv):
        # Strike adjacency alone does not save a wing from a bad quote sitting
        # INSIDE the bracket it legitimately borders — several of these printed
        # exactly the strikes a reader would expect, so there was no visible
        # symptom. The smile's delta must fall with strike; a corrupt quote breaks
        # that at its own strike, and the affected wing is then refused.
        skew = self._with_broken_quote(instrument, mark_iv)
        wing = skew.call_25 if instrument.endswith("-C") else skew.put_25
        assert wing is None
        assert skew.rr25 is None

    def test_a_corrupt_quote_does_not_take_down_the_far_side(self):
        # The guard is local: breaking the call side must not cost the put wing.
        skew = self._with_broken_quote("BTC-28AUG26-65000-C", 4.5)
        assert skew.put_25.iv == pytest.approx(34.30, abs=0.05)

    def test_a_clean_chain_still_yields_both_wings(self):
        # The guard must not be so strict that ordinary data trips it.
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)
        assert skew.call_25 is not None
        assert skew.put_25 is not None
        assert skew.atm is not None

    def test_a_refused_wing_says_why(self):
        rows = [dict(row) for row in CHAIN]
        for row in rows:
            if row["instrument_name"] == "BTC-28AUG26-61000-P":
                row["mark_iv"] = 0.5
        out = _report(chain=rows)
        assert "**25Δ put IV:** n/a (no two strike-adjacent quotes bracket this point, or " in out
        assert "not a monotone smile" in out
        # ... and the reading line must not state a risk reversal it does not have.
        assert "vol points above" not in out

    def test_unbracketed_wing_is_none_not_extrapolated(self):
        # Drop every call above the forward: the call side can no longer reach
        # down to 0.25 delta, so the call wing (and RR25) must be missing while
        # the put wing survives.
        contracts = [
            c
            for c in deribit.parse_chain(CHAIN, NOW)
            if c.expiry == "28AUG26" and not (c.is_call and c.strike > 64000)
        ]
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.call_25 is None
        assert skew.rr25 is None
        assert skew.put_25 is not None

    def test_atm_falls_back_to_the_put_curve(self):
        # Calls priced only far from the money; the puts still bracket -0.50, and
        # both curves cross 50 delta at the same strike (d1 = 0).
        contracts = [
            c
            for c in deribit.parse_chain(CHAIN, NOW)
            if c.expiry == "28AUG26" and (not c.is_call or c.strike >= 68000)
        ]
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.atm.iv == pytest.approx(31.12, abs=0.1)

    def test_no_eligible_expiry_raises(self):
        contracts = [c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry == "5AUG26"]
        with pytest.raises(deribit.DeribitError, match="falls between 15 and 45 days out"):
            deribit.compute_skew(contracts, NOW)

    def test_an_out_of_band_expiry_yields_no_skew_rather_than_a_wrong_tenor(self):
        # A thinned book whose only listed expiry is months out. Before the tenor
        # ceiling this returned a complete 96-day surface, which _skew_section then
        # rendered as "the listed expiry closest to 30 days".
        far = _ladder("FAR_OUT", 96)
        with pytest.raises(deribit.DeribitError, match="falls between 15 and 45 days out"):
            deribit.compute_skew(far, NOW)

    @pytest.mark.parametrize("bad_forward", [-1.0, 0.0])
    def test_unusable_forward_raises(self, bad_forward):
        # compute_skew is public, so a caller can hand it contracts this module
        # would never have built. Zero as well as negative: a `< 0` guard would
        # let a zero forward through and render "**Forward:** 0.00" with every
        # wing n/a, which reads as a thin chain rather than a broken one.
        contracts = [_synthetic_contract("FAR", 30)._replace(underlying=bad_forward)]
        with pytest.raises(deribit.DeribitError, match="no usable forward"):
            deribit.compute_skew(contracts, NOW)

    def test_a_wingless_nearest_expiry_falls_through_to_the_next(self):
        # NEARBY (25d) is nearest the 30-day target but its call side stops at the
        # forward, so it cannot bracket the 25Δ call. NEXT (40d) can, and a
        # labelled 40-day skew beats an all-n/a section in a risk debate.
        contracts = _ladder("NEARBY", 25, call_max=64000) + _ladder("NEXT", 40)
        assert deribit.rank_expiries(contracts, NOW) == ["NEARBY", "NEXT"]
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.expiry == "NEXT"
        assert skew.is_fallback is True
        assert skew.call_25 is not None and skew.put_25 is not None

    def test_a_missing_put_wing_also_triggers_the_fallback(self):
        # Mirror of the call-side case. Without it, deleting the `put_25 is not
        # None` half of the acceptance test leaves the whole suite green while
        # compute_skew silently ships a half-skew with RR25 = None.
        contracts = _ladder("NEARBY", 25, put_min=64000) + _ladder("NEXT", 40)
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.expiry == "NEXT"
        assert skew.is_fallback is True
        assert skew.rr25 is not None

    def test_the_fallback_is_one_step_and_never_walks_the_whole_ladder(self):
        # A risk reversal is not comparable across tenors, so the walk stops after
        # the next-nearest expiry. Here A (25d) and B (35d) both fail their call
        # wing and C (45d) is clean — C must NOT be reached, because reporting a
        # far-dated skew as if it were the ~30-day one is worse than reporting none.
        contracts = (
            _ladder("A", 25, call_max=64000) + _ladder("B", 35, call_max=64000) + _ladder("C", 45)
        )
        assert deribit._MAX_EXPIRY_CANDIDATES == 2
        assert deribit.rank_expiries(contracts, NOW) == ["B", "A", "C"]
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.expiry == "B"
        assert skew.call_25 is None
        assert skew.is_fallback is False

    def test_the_raised_error_names_the_expiry_that_would_have_been_used(self):
        # The first failure is kept, not the last: the caller must be told about
        # the expiry the report would have shown, not one it never reached.
        near = _synthetic_contract("NEARBY", 25)._replace(underlying=-1.0)
        # 42d, not 60d: both candidates must be inside the eligible tenor band, or
        # the ranking holds one entry and the "first failure is kept" rule is not
        # exercised at all.
        far = _synthetic_contract("FARAWAY", 42)._replace(underlying=-1.0)
        assert deribit.rank_expiries([near, far], NOW) == ["NEARBY", "FARAWAY"]
        with pytest.raises(deribit.DeribitError, match="NEARBY contracts carry no usable forward"):
            deribit.compute_skew([near, far], NOW)

    def test_the_nearest_expiry_wins_when_it_can_bracket_both_wings(self):
        # The fallback must not fire when there is nothing to fall back from.
        contracts = _ladder("NEARBY", 25) + _ladder("NEXT", 40)
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.expiry == "NEARBY"
        assert skew.is_fallback is False

    def test_no_candidate_bracketing_both_wings_keeps_the_nearest(self):
        # Falling back must not become "search until something answers": when no
        # expiry brackets both wings the report still shows the NEAREST one's
        # forward, ATM and whichever wing exists, rather than a distant expiry.
        contracts = _ladder("NEARBY", 25, call_max=64000) + _ladder("NEXT", 40, call_max=64000)
        skew = deribit.compute_skew(contracts, NOW)
        assert skew.expiry == "NEARBY"
        assert skew.is_fallback is False
        assert skew.call_25 is None
        assert skew.put_25 is not None

    def test_the_report_never_calls_a_fallback_expiry_the_nearest(self):
        contracts = _ladder("NEARBY", 25, call_max=64000) + _ladder("NEXT", 40)
        section = deribit._skew_section(
            deribit.compute_skew(contracts, NOW), "2026-08-05T06:06:00Z"
        )
        assert "**Expiry used:** NEXT — 40.0 days out" in section
        assert "could not be used, so this is the next eligible one" in section
        assert f"the eligible expiry closest to {deribit.TARGET_DTE_DAYS} days" not in section
        # The fallback branch must carry the SAME exclusions as the non-fallback
        # one. It used to name neither, so stepping to the second candidate
        # silently dropped the only line that states them.
        assert "only expiries 15-45 days out are eligible" in section
        assert "no open interest never enter the smile" in section

    def test_the_non_fallback_expiry_basis_names_its_exclusions(self):
        # The other half of the pair above. This wording was entirely unpinned:
        # reverting it to the older, narrower sentence shipped green.
        section = deribit._skew_section(
            deribit.compute_skew(_ladder("NEARBY", 25), NOW), "2026-08-05T06:06:00Z"
        )
        assert f"the eligible expiry closest to {deribit.TARGET_DTE_DAYS} days" in section
        assert "only expiries 15-45 days out are eligible" in section
        assert "no open interest never enter the smile" in section
        assert "could not be used" not in section

    def test_the_reading_line_prints_the_tenor_to_one_decimal(self):
        # Matches _skew_section's "N.N days out". At ":.0f" this used format's
        # banker's rounding — the very thing _ordinal avoids — so one report could
        # say "22.5 days out" and "(22-day)". The fixture expiry is ~23.0 days,
        # where nothing distinguishes the formats, so the tenor is synthetic.
        contracts = _ladder("NEARBY", 22.5)
        line = deribit._reading_line("BTC", deribit.compute_skew(contracts, NOW), None, 0)
        assert "NEARBY (22.5-day) chain" in line

    @pytest.mark.parametrize(
        "quotes",
        [
            # The monotonicity guard must REFUSE the point, not skip the bracket
            # and keep scanning: the later 110/120 pair also spans 0.25, so a
            # `continue` would supply a wing from strikes nowhere near the target
            # — precisely the silent wrong number the guard exists to prevent.
            pytest.param(
                [(100.0, 0.20, 20.0), (105.0, 0.35, 3.0), (110.0, 0.30, 40.0), (120.0, 0.05, 10.0)],
                id="corrupt-quote-inside-its-own-bracket",
            ),
            # The span test compares against the pair's own min/max rather than
            # assuming delta_b <= delta_a, so an inverted step cannot slip past
            # unjudged and let that same later pair answer instead.
            pytest.param(
                [
                    (100.0, 0.10, 20.0),
                    (105.0, 0.40, 55.0),
                    (110.0, 0.30, 40.0),
                    (120.0, 0.05, 10.0),
                ],
                id="inverted-step",
            ),
        ],
    )
    def test_a_broken_smile_never_leaks_a_wing_from_a_later_pair(self, quotes):
        # Values are the ones that actually produce a further-along bracket, not
        # chosen for convenience.
        assert deribit.interpolate_iv_at_delta(quotes, 0.25) is None

    def test_contracts_with_no_computable_delta_are_skipped(self):
        # Same reasoning: a zero IV cannot produce a delta, and must not abort the
        # whole surface or be counted as a usable quote.
        good = _synthetic_contract("FAR", 30, strike=64000.0)
        bad = _synthetic_contract("FAR", 30, strike=65000.0, iv=0.0)
        # parse_chain would reject `bad`, so build the Contract list directly.
        skew = deribit.compute_skew([good, bad._replace(mark_iv=0.0)], NOW)
        assert skew.n_calls == 1

    def test_forward_is_the_median_of_the_expiry_quotes(self):
        # Deribit stamps each row with the index at its own quote instant, so a
        # single outlying row must not move the forward.
        selected = [c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry == "28AUG26"]
        poisoned = [selected[0]._replace(underlying=1.0)] + selected[1:]
        assert deribit.compute_skew(poisoned, NOW).forward == pytest.approx(64456.93, abs=0.5)

    def test_the_forward_ignores_an_outlier_ABOVE_the_quotes_too(self):
        # The mirror of the case above, and the only direction that separates a
        # median from a max(): poisoning one row DOWNWARD leaves max() looking
        # perfectly correct on this sample, so `_median(...)` could be swapped for
        # `max(...)` with the whole suite green. A single stale row would then set
        # the forward, putting every listed strike far out of the money — both
        # wings collapse to n/a and "**Forward:** 1,000,000,000.00" is printed as
        # BTC's price.
        selected = [c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry == "28AUG26"]
        poisoned = [selected[0]._replace(underlying=1.0e9)] + selected[1:]
        assert deribit.compute_skew(poisoned, NOW).forward == pytest.approx(64456.93, abs=0.5)

    def test_median_of_an_even_sample_is_the_midpoint(self):
        assert deribit._median([1.0, 2.0, 3.0, 4.0]) == 2.5
        assert deribit._median([3.0, 1.0, 2.0]) == 2.0


# --------------------------------------------------------------------------- #
# DVOL fetch and windowing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestDvol:
    def test_request_window_and_resolution_are_pinned(self):
        recorder = _RequestRecorder()
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        assert series.latest_date == "2026-07-20"
        params = recorder.params_for(DVOL_ENDPOINT)
        # Daily candles: a sub-daily resolution would over-weight recent days in
        # the same calendar window purely by contributing more observations.
        assert params["resolution"] == "1D"
        assert params["currency"] == "BTC"
        end = datetime.fromtimestamp(params["end_timestamp"] / 1000, tz=timezone.utc)
        # To the second: an end of 00:00:00 would silently exclude curr_date's own
        # candle while still landing on the right calendar day.
        assert end.strftime("%Y-%m-%dT%H:%M:%S") == "2026-07-20T23:59:59"
        # Literal date: deriving it from the constants would let either the window
        # or the buffer shrink to nothing while this still passed, and the report
        # would go on claiming a "365-day percentile" over whatever arrived. The
        # span is driven by the LONGER (percentile) window: 365 + 10 = 375 days.
        start = datetime.fromtimestamp(params["start_timestamp"] / 1000, tz=timezone.utc)
        assert start == datetime(2025, 7, 10, tzinfo=timezone.utc)  # 375 days back

    def test_the_fetch_span_stays_inside_deribit_s_one_page_cap(self):
        # Deribit caps this endpoint at 1000 candles per response and pages the
        # OLDEST data out. At 1D resolution the fetch is one candle per day, so
        # this is the invariant that keeps the no-paging design honest: widening
        # the percentile window past the cap would silently truncate the history.
        #
        # Measured off the request actually sent, not recomputed from the same two
        # constants the request is built from — that form restates the source and
        # cannot see the request expression itself change.
        assert deribit._DVOL_MAX_CANDLES_PER_RESPONSE == 1000
        recorder = _RequestRecorder()
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        params = recorder.params_for(DVOL_ENDPOINT)
        span_days = (params["end_timestamp"] - params["start_timestamp"]) / 86_400_000
        assert span_days < deribit._DVOL_MAX_CANDLES_PER_RESPONSE
        # One candle per day at 1D, so the span in days IS the candle count.
        assert deribit.DVOL_RESOLUTION == "1D"

    def test_the_fetch_span_follows_whichever_statistics_window_is_longer(self, monkeypatch):
        # `max(DVOL_WINDOW_DAYS, DVOL_PERCENTILE_WINDOW_DAYS)`, not the percentile
        # window named directly. At today's values the percentile window IS the
        # longer one, so the pinned 375-day span above cannot tell the two forms
        # apart — and the day the range window is widened past it, the range would
        # be computed over a silently truncated sample and still labelled with its
        # full span. Drive the range window past the percentile one and the fetch
        # must follow it.
        monkeypatch.setattr(deribit, "DVOL_WINDOW_DAYS", 500)
        recorder = _RequestRecorder()
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        params = recorder.params_for(DVOL_ENDPOINT)
        start = datetime.fromtimestamp(params["start_timestamp"] / 1000, tz=timezone.utc)
        # 500 + the 10-day buffer = 510 days before 2026-07-20.
        assert start == datetime(2025, 2, 25, tzinfo=timezone.utc)

    def test_candle_timestamps_are_read_in_utc_not_the_runner_s_local_zone(self):
        # The `tz=timezone.utc` on the fromtimestamp call is invisible to every
        # other test in this file: `_candle` stamps midnight UTC, and on a UTC+8
        # runner a naive conversion lands at 08:00 the SAME day, so nothing moves.
        # On a runner west of UTC that identical candle slides to the PREVIOUS
        # day, and `day <= curr_date` then drops the newest reading — a report
        # that is a day stale on one CI box and current on another. These two
        # instants straddle UTC midnight, so any non-zero offset in either
        # direction collapses them onto a single calendar day.
        dvol = {
            "data": [
                _candle_at(datetime(2026, 8, 4, 23, 30, tzinfo=timezone.utc), 40.0),
                _candle_at(datetime(2026, 8, 5, 0, 30, tzinfo=timezone.utc), 41.0),
            ]
        }
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert series.dates == ["2026-08-04", "2026-08-05"]
        assert series.closes == [40.0, 41.0]

    def test_the_series_is_sorted_by_date_not_by_arrival_order(self):
        # `sorted(by_date)`, not `list(by_date)`. Every other DVOL fixture arrives
        # already ascending, so the dict's insertion order happens to be the right
        # answer and the sort could be deleted outright. Deribit is only
        # documented to page NEWEST-first; were a response ever to arrive
        # descending, `latest` would return the OLDEST reading in the history and
        # the report would date and age it as the current level.
        dvol = {
            "data": [
                _candle("2026-08-05", 42.0),
                _candle("2026-08-04", 41.0),
                _candle("2026-08-03", 40.0),
            ]
        }
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert series.dates == ["2026-08-03", "2026-08-04", "2026-08-05"]
        assert series.closes == [40.0, 41.0, 42.0]
        assert (series.latest, series.latest_date) == (42.0, "2026-08-05")

    def test_a_repeated_day_keeps_the_later_candle(self):
        # A resolution change, or a partial candle alongside the settled one for
        # the same day. Plain assignment keeps the LAST value seen; `setdefault`
        # would keep the first, freezing the report on a stale intraday print and
        # never picking up the settled close. Nothing else in the suite feeds a
        # duplicate day, so the two forms were indistinguishable.
        dvol = {"data": [_candle("2026-08-05", 40.0), _candle("2026-08-05", 55.0)]}
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert series.dates == ["2026-08-05"]
        assert series.closes == [55.0]

    def test_a_truncated_history_is_not_passed_over_in_silence(self, caplog):
        # Unreachable at the current span, but if Deribit ever lowers the cap the
        # percentile sample is short and the operator must be able to see why.
        dvol = {"data": [_candle("2026-07-20", 40.0)], "continuation": 1699574400000}
        recorder = _RequestRecorder(dvol=dvol)
        with (
            caplog.at_level(logging.WARNING),
            mock.patch.object(deribit, "_request", side_effect=recorder),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        assert "truncated" in caplog.text

    def test_candles_after_curr_date_are_dropped(self):
        # Belt-and-braces: even if Deribit ignored the requested range, a future
        # candle must never reach the report.
        dvol = {"data": [_candle("2026-07-19", 40.0), _candle("2026-07-21", 99.0)]}
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        assert series.dates == ["2026-07-19"]
        assert series.closes == [40.0]

    def test_close_is_the_fifth_field(self):
        dvol = {"data": [[_candle("2026-07-19", 0)[0], 10.0, 20.0, 5.0, 15.0]]}
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        assert series.closes == [15.0]

    def test_no_visible_candles_raises(self):
        dvol = {"data": [_candle("2026-07-21", 40.0)]}
        recorder = _RequestRecorder(dvol=dvol)
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError, match="No BTC DVOL readings on or before"),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 7, 20))

    @pytest.mark.parametrize(
        "bad",
        [
            [1782950400000, 42.9, 43.0, 40.2],  # too few fields
            [1782950400000, 42.9, 43.0, 40.2, 40.4, 1.0],  # too many fields
            [1782950400000, 42.9, 43.0, 40.2, "40.4"],  # non-numeric close
            [1782950400000, 42.9, 43.0, 40.2, float("nan")],  # NaN close
            {"t": 1782950400000, "close": 40.4},  # shape change
        ],
    )
    def test_malformed_candle_is_fatal(self, bad):
        # A candle-shape change would silently reinterpret which number is the
        # close, so it must fail loud rather than report a wrong vol level.
        recorder = _RequestRecorder(dvol={"data": [bad]})
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError, match="Malformed DVOL candle"),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))

    def test_missing_data_list_is_fatal(self):
        recorder = _RequestRecorder(dvol={})
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError, match="no 'data' list"),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))

    def test_mismatched_series_lengths_fail_loud(self):
        # dates and closes are built together; drifting apart would silently
        # mis-window the statistics, so the zip is strict.
        broken = deribit.DvolSeries(dates=["2026-08-04", "2026-08-05"], closes=[40.0])
        with pytest.raises(ValueError):
            deribit._dvol_section(broken, datetime(2026, 8, 5), TODAY)

    @pytest.mark.parametrize("bad_close", [0.0, -12.5])
    def test_a_non_positive_reading_is_dropped_not_published(self, bad_close):
        # parse_chain rejects a non-positive mark_iv or underlying for exactly this
        # glitch class. A 0.00 DVOL is not a low reading, it is a broken one, and
        # left in it renders as "0.00 ... 3rd percentile" — a maximally
        # vol-is-cheap read — with no caveat anywhere.
        dvol = {
            "data": [_candle("2026-08-04", 40.0), _candle("2026-08-05", bad_close)],
        }
        recorder = _RequestRecorder(dvol=dvol)
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            series = deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        # The bad candle is gone and the previous day stands as the latest, which
        # the report then dates and ages honestly.
        assert series.closes == [40.0]
        assert series.latest_date == "2026-08-04"

    def test_a_dropped_non_positive_reading_says_so_in_the_log(self, caplog):
        # Skipping is silent to the report — an older reading simply becomes the
        # latest, dated and aged honestly — so the log line is the ONLY place a
        # corrupt candle is visible at all. Deleting it left the suite green while
        # a feed quietly publishing zeroes became indistinguishable from one that
        # had merely stopped. Same pattern as the truncation warning above.
        dvol = {"data": [_candle("2026-08-04", 40.0), _candle("2026-08-05", 0.0)]}
        recorder = _RequestRecorder(dvol=dvol)
        with (
            caplog.at_level(logging.WARNING),
            mock.patch.object(deribit, "_request", side_effect=recorder),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert (
            "Skipping non-positive Deribit DVOL close 0.0 dated 2026-08-05 for BTC" in caplog.text
        )

    def test_every_reading_being_unusable_raises(self):
        # Skipping must not degrade into an empty series presented as a report, AND
        # the message must not say the feed published nothing: it published a full
        # history of corrupt candles, which is a different thing to go and look at.
        # The message renders verbatim into "**DVOL:** unavailable — ...".
        recorder = _RequestRecorder(dvol={"data": [_candle("2026-08-05", 0.0)]})
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(
                deribit.DeribitError, match=r"Every BTC DVOL reading .* was non-positive"
            ) as excinfo,
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert "1 skipped" in str(excinfo.value)
        assert "No BTC DVOL readings" not in str(excinfo.value)

    def test_a_non_positive_candle_after_curr_date_is_not_a_corrupt_feed(self):
        # The non-positive check runs BEFORE the `day <= curr_date` filter, and
        # Deribit may honour a wider range than asked — a state this function's
        # docstring says the row-level filter exists for. Counting an out-of-window
        # bad candle made "every reading was non-positive" fire on a window that
        # simply held nothing, which is the wrong-cause defect this round fixed at
        # three other sites.
        recorder = _RequestRecorder(dvol={"data": [_candle("2026-08-05", 0.0)]})
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError) as excinfo,
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 7, 20))
        assert "No BTC DVOL readings on or before 2026-07-20" in str(excinfo.value)
        assert "non-positive" not in str(excinfo.value)

    def test_a_non_positive_candle_before_curr_date_still_counts_as_corrupt(self):
        # The lower half of the same guard. The existing corrupt-feed test uses a
        # candle dated exactly ON curr_date, and the test above one AFTER it, so
        # `day <= curr_date` could be narrowed to `day == curr_date` and ship
        # green — which would report a fortnight of zeroed in-window readings as
        # "no readings published at all", the wrong cause this guard exists to
        # avoid.
        recorder = _RequestRecorder(
            dvol={"data": [_candle("2026-08-01", 0.0), _candle("2026-08-02", 0.0)]}
        )
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError) as excinfo,
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))
        assert "Every BTC DVOL reading on or before 2026-08-05 was non-positive" in str(
            excinfo.value
        )
        assert "2 skipped" in str(excinfo.value)


@pytest.mark.unit
class TestPercentile:
    def test_percentile_counts_the_sample_at_or_below(self):
        assert deribit._percentile_of(3.0, [1.0, 3.0, 5.0]) == pytest.approx(66.67, abs=0.01)
        assert deribit._percentile_of(1.0, [1.0, 3.0, 5.0]) == pytest.approx(33.33, abs=0.01)
        assert deribit._percentile_of(5.0, [1.0, 3.0, 5.0]) == 100.0

    def test_ties_count_as_at_or_below(self):
        assert deribit._percentile_of(2.0, [2.0, 2.0, 9.0]) == pytest.approx(66.67, abs=0.01)

    @pytest.mark.parametrize(
        "value,expected",
        [
            (1.0, "1st"),
            (2.0, "2nd"),
            (3.0, "3rd"),
            (4.0, "4th"),
            (7.4, "7th"),
            (11.0, "11th"),
            (12.0, "12th"),
            (13.0, "13th"),  # the teens are all "th"
            (21.0, "21st"),
            (22.0, "22nd"),
            (23.0, "23rd"),
            (100.0, "100th"),
        ],
    )
    def test_percentile_is_rendered_as_an_english_ordinal(self, value, expected):
        assert deribit._ordinal(value) == expected

    @pytest.mark.parametrize(
        "value,expected", [(12.5, "13th"), (37.5, "38th"), (62.5, "63rd"), (87.5, "88th")]
    )
    def test_an_exact_half_always_rounds_up(self, value, expected):
        # round() is banker's rounding: it sent an exact .5 to the nearest EVEN
        # integer, so 62.5 rendered "62nd" while 37.5 rendered "38th" — the same
        # half-percent landing differently depending on the neighbouring digit.
        # Reachable whenever the sample size is a multiple of 8 (n=16 and n=24 both
        # yield exactly {12.5, 37.5, 62.5, 87.5}); at n=20 every 100*k/20 is an
        # integer, so no exact half arises there at all.
        assert deribit._ordinal(value) == expected


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #
def _response(status=200, payload=None, text_body=None):
    response = mock.Mock()
    response.status_code = status
    if text_body is not None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload
    response.raise_for_status.side_effect = (
        requests.HTTPError(f"HTTP {status}") if status >= 400 else None
    )
    return response


@pytest.mark.unit
class TestRequest:
    def test_returns_the_result_payload(self):
        with mock.patch.object(
            deribit.requests, "get", return_value=_response(payload={"result": {"data": []}})
        ):
            assert deribit._request(DVOL_ENDPOINT, {}) == {"data": []}

    def test_what_actually_goes_on_the_wire_is_pinned(self):
        # Every other test in this file patches deribit._request itself, so the URL,
        # the params and the timeout were built but never observed. Without this,
        # dropping `timeout=` (an unbounded hang on every call), dropping `params=`,
        # or pointing DERIBIT_BASE at testnet all pass the whole suite.
        with mock.patch.object(
            deribit.requests, "get", return_value=_response(payload={"result": 1})
        ) as get:
            deribit._request(DVOL_ENDPOINT, {"currency": "BTC"})
        args, kwargs = get.call_args
        assert args == ("https://www.deribit.com/api/v2/public/get_volatility_index_data",)
        assert kwargs["params"] == {"currency": "BTC"}
        # A missing timeout is the one failure mode with no upper bound: the
        # analyst graph would block forever rather than degrade the category.
        assert kwargs["timeout"] == 30
        assert deribit.REQUEST_TIMEOUT == 30

    def test_the_retry_pause_is_pinned(self):
        # The module docstring turns these into a latency-envelope claim
        # (2 * (30 + 2 + 30) ~= 124s); pinning the attempt count alone left two of
        # the three terms free to float.
        assert deribit._RETRY_DELAY_SECONDS == 2
        assert deribit._RETRY_ATTEMPTS == 2

    def test_the_chain_request_asks_for_options(self):
        # `kind` was never asserted: dropping it, or asking for "future", returns
        # rows whose names parse to nothing, so the skew half dies permanently
        # while presenting as an ordinary outage.
        recorder = _RequestRecorder()
        with mock.patch.object(deribit, "_request", side_effect=recorder):
            deribit._fetch_chain("BTC", NOW)
        assert recorder.params_for(CHAIN_ENDPOINT) == {"currency": "BTC", "kind": "option"}

    @pytest.mark.parametrize("status", [500, 503])
    def test_a_5xx_without_a_reason_is_retried(self, status):
        # 500 exactly is the commonest 5xx and was outside the tested set, so a
        # `> 500` boundary would silently drop its retry.
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(status=status, payload={})
            ) as get,
            mock.patch.object(deribit.time, "sleep") as sleep,
            pytest.raises(deribit.DeribitError, match="did not return a usable response after"),
        ):
            deribit._request(DVOL_ENDPOINT, {})
        assert get.call_count == 2
        assert sleep.call_count == 1

    def test_a_bare_400_is_diagnosed_as_a_rejection(self):
        # 400 exactly sits on the 4xx boundary. A `> 400` test would fall through
        # to the result-field check and misreport a rejection as a shape change.
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(status=400, payload={})
            ),
            pytest.raises(deribit.DeribitError, match="HTTP 400"),
        ):
            deribit._request(DVOL_ENDPOINT, {})

    def test_jsonrpc_error_is_reported_with_its_reason_and_not_retried(self):
        payload = {
            "error": {
                "code": -32602,
                "message": "Invalid params",
                "data": {"reason": "value required", "param": "start_timestamp"},
            }
        }
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(status=400, payload=payload)
            ) as get,
            mock.patch.object(deribit.time, "sleep") as sleep,
            pytest.raises(deribit.DeribitError, match="start_timestamp"),
        ):
            deribit._request(DVOL_ENDPOINT, {})
        assert get.call_count == 1  # deterministic failure: no wasted retry
        sleep.assert_not_called()

    def test_a_5xx_that_explains_itself_is_believed_rather_than_retried(self):
        # The ORDER of the two checks. The docstring's rule is "anything Deribit
        # explains — a JSON-RPC error object AT ANY STATUS — is deterministic and
        # raises immediately", and the only way to see that is a response that is
        # both 5xx and carries the object: hoisting the `>= 500` test above the
        # JSON-RPC test passes every other case in this class, then throws away
        # Deribit's own reason, burns the retry and the two-second pause, and
        # reports the vendor as not answering when it answered precisely.
        payload = {"error": {"code": 10028, "message": "too_many_requests_for_currency"}}
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(status=503, payload=payload)
            ) as get,
            mock.patch.object(deribit.time, "sleep") as sleep,
            pytest.raises(deribit.DeribitError, match="too_many_requests_for_currency"),
        ):
            deribit._request(DVOL_ENDPOINT, {})
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_the_retry_announces_itself_before_sleeping(self, caplog):
        # The one operator-facing trace that a request was repeated. Deleted, a
        # vendor flapping on every other call looks perfectly healthy in the log
        # (the second attempt succeeds and nothing is raised), and the latency
        # this module's docstring budgets for has no evidence behind it.
        with (
            caplog.at_level(logging.WARNING),
            mock.patch.object(
                deribit.requests,
                "get",
                side_effect=[_response(status=503, payload={}), _response(payload={"result": 42})],
            ),
            mock.patch.object(deribit.time, "sleep"),
        ):
            assert deribit._request(DVOL_ENDPOINT, {}) == 42
        assert (
            "Deribit get_volatility_index_data request failed (HTTP 503); retrying in 2s"
            in caplog.text
        )

    def test_bare_4xx_is_not_retried(self):
        # A WAF block or renamed endpoint has no JSON-RPC error object. Leaving it
        # to raise_for_status() would raise requests.HTTPError — a RequestException
        # — which the retry handler catches, so the request would be repeated to no
        # purpose and then misreported as "unreachable".
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(status=403, payload={})
            ) as get,
            mock.patch.object(deribit.time, "sleep") as sleep,
            pytest.raises(deribit.DeribitError, match="HTTP 403"),
        ):
            deribit._request(CHAIN_ENDPOINT, {})
        assert get.call_count == 1
        sleep.assert_not_called()

    def test_rate_limit_raises_the_shared_taxonomy_error(self):
        with (
            mock.patch.object(deribit.requests, "get", return_value=_response(status=429)),
            pytest.raises(VendorRateLimitError),
        ):
            deribit._request(CHAIN_ENDPOINT, {})

    def test_network_error_is_retried_then_wrapped(self):
        with (
            mock.patch.object(
                deribit.requests, "get", side_effect=requests.ConnectionError("boom")
            ) as get,
            mock.patch.object(deribit.time, "sleep") as sleep,
            pytest.raises(deribit.DeribitError, match="did not return a usable response after"),
        ):
            deribit._request(DVOL_ENDPOINT, {})
        # Literal: the module docstring turns this count into a latency-envelope
        # claim (2 * (30 + 2 + 30) ~= 124s), so it must not float with the constant.
        assert deribit._RETRY_ATTEMPTS == 2
        assert get.call_count == 2
        sleep.assert_called_once()

    def test_server_error_is_retried_then_succeeds(self):
        good = _response(payload={"result": 42})
        with (
            mock.patch.object(
                deribit.requests, "get", side_effect=[_response(status=503, payload={}), good]
            ),
            mock.patch.object(deribit.time, "sleep"),
        ):
            assert deribit._request(DVOL_ENDPOINT, {}) == 42

    def test_undecodable_body_is_retried(self):
        with (
            mock.patch.object(
                deribit.requests, "get", return_value=_response(text_body="<html>WAF</html>")
            ) as get,
            mock.patch.object(deribit.time, "sleep"),
            pytest.raises(deribit.DeribitError),
        ):
            deribit._request(DVOL_ENDPOINT, {})
        # Literal: the module docstring turns this count into a latency-envelope
        # claim (2 * (30 + 2 + 30) ~= 124s), so it must not float with the constant.
        assert deribit._RETRY_ATTEMPTS == 2
        assert get.call_count == 2

    def test_missing_result_field_is_fatal(self):
        with (
            mock.patch.object(deribit.requests, "get", return_value=_response(payload={"ok": 1})),
            pytest.raises(deribit.DeribitError, match="no 'result' field"),
        ):
            deribit._request(DVOL_ENDPOINT, {})


# --------------------------------------------------------------------------- #
# Asset classification
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestClassifyAsset:
    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("BTC", ("BTC", False)),
            ("BTC-USD", ("BTC", False)),
            ("BTCUSDT", ("BTC", False)),
            ("BTC/USD", ("BTC", False)),
            ("eth", ("ETH", False)),
            ("ETH-USDC", ("ETH", False)),
            ("SOL", ("BTC", True)),
            ("XRP-USD", ("BTC", True)),
            ("USDT", (None, False)),
            ("USDC", (None, False)),
            ("ETHW", (None, False)),
            ("AAPL", (None, False)),
            ("", (None, False)),
        ],
    )
    def test_classification(self, symbol, expected):
        assert deribit._classify_asset(symbol) == expected


# --------------------------------------------------------------------------- #
# The clock source
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestUtcClock:
    def test_the_single_clock_source_is_timezone_aware_utc(self):
        # Every other test in this file PATCHES `_utc_now`, so its two-line body
        # never runs: dropping the `timezone.utc` argument ships a naive local
        # datetime with the suite fully green. Downstream that instant is compared
        # against tz-aware expiry datetimes (`expiry_dt <= now` in parse_chain),
        # which raises TypeError and costs the entire chain half on a real run,
        # and `strftime("%Y-%m-%d")` on it would date the report by the server's
        # local calendar rather than by UTC.
        now = deribit._utc_now()
        assert now.tzinfo is not None
        assert now.utcoffset() == dt.timedelta(0)
        # It must remain usable in arithmetic against this module's own aware
        # datetimes — the operation that actually breaks under a naive clock.
        assert isinstance(now - deribit.parse_expiry("28AUG26"), dt.timedelta)


# --------------------------------------------------------------------------- #
# Report rendering
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestReport:
    def test_reports_dvol_level_range_and_percentile(self):
        # Range and percentile are computed over DIFFERENT windows and printed on
        # separate lines, each naming its own span and its own sample count. The
        # fixture holds 35 readings, so the 365-day percentile sample is all of
        # them while the 30-day range sees only the last 30.
        out = _report()
        assert "**DVOL (30-day implied vol index), latest:** 34.43% annualized on 2026-08-05" in out
        assert "**30d range:** min 34.04% / max 39.53% over 30 daily readings" in out
        assert (
            "**365d percentile:** the latest reading sits at the 6th percentile of the "
            "35 daily readings in the 365 days ending 2026-08-05" in out
        )

    def test_each_window_holds_exactly_its_own_span_of_readings(self):
        # An inclusive lower bound would put N+1 readings in a window called "Nd".
        assert deribit.DVOL_WINDOW_DAYS == 30
        assert deribit.DVOL_PERCENTILE_WINDOW_DAYS == 365
        # 400 ascending readings ending today: the range sees the last 30 and the
        # percentile the last 365, so neither window can quietly borrow the other's
        # span. Values are the observed render, not derived from the constants.
        out = _report(dvol=_dvol_days(400))
        assert "**30d range:** min 77.00% / max 79.90% over 30 daily readings" in out
        assert "percentile of the 365 daily readings in the 365 days ending" in out

    def test_reports_the_surface_with_its_bracketing_strikes(self):
        out = _report()
        assert "**Expiry used:** 28AUG26" in out
        assert "**ATM IV (50Δ):** 31.12% (between the 64,000 and 65,000 strikes)" in out
        assert "**25Δ call IV:** 29.56% (between the 67,000 and 68,000 strikes)" in out
        assert "**25Δ put IV:** 34.30% (between the 61,000 and 62,000 strikes)" in out
        assert "-4.74 vol points" in out

    def test_quote_counts_describe_what_was_counted(self):
        out = _report()
        assert "8 call quotes and 8 put quotes on this expiry yielded a usable delta" in out

    def test_a_one_quote_side_is_counted_in_the_singular(self):
        # `_quotes`'s singular branch had no render behind it: forcing the helper
        # permanently plural shipped green, and "1 call quotes" is garbled exactly
        # on the sparse chain the count exists to expose. Two contracts on a
        # single ~30-day expiry is the smallest chain that reaches this line.
        out = _report(
            chain=[
                {
                    "instrument_name": "BTC-4SEP26-60000-C",
                    "mark_iv": 30,
                    "underlying_price": 64000,
                    "open_interest": 5,
                },
                {
                    "instrument_name": "BTC-4SEP26-68000-P",
                    "mark_iv": 30,
                    "underlying_price": 64000,
                    "open_interest": 5,
                },
            ]
        )
        assert "1 call quote and 1 put quote on this expiry yielded a usable delta" in out
        assert "1 call quotes" not in out
        assert "1 put quotes" not in out

    def test_the_forward_keeps_its_thousands_separator(self):
        # Never asserted anywhere: the forward is a five-figure price and the
        # report's other five-figure numbers (the bracketing strikes) are all
        # grouped, so an ungrouped "64456.93" beside "between the 64,000 and
        # 65,000 strikes" reads as a different quantity at a glance.
        assert "**Forward:** 64,456.93" in _report()

    def test_a_call_skewed_rr25_keeps_its_plus_sign_in_the_section(self):
        # The fixture chain is put-skewed, so every rendered RR25 in this suite is
        # negative and the sign is supplied by the minus that is part of the
        # number. Only a POSITIVE risk reversal can see the explicit "+", and
        # without it "**RR25 ...:** 2.00 vol points" is unsigned in a report whose
        # whole subject is which wing is dearer. The reading line's own "+" is
        # covered separately; this pins the section's.
        skewed = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(36.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.0, 61000.0, 62000.0),
        )
        section = deribit._skew_section(skewed, "2026-08-05T06:06:00Z")
        assert "**RR25 (25Δ call IV − 25Δ put IV):** +2.00 vol points" in section

    def test_an_rr25_that_rounds_away_never_renders_a_negative_zero(self):
        # An RR25 in (-0.005, 0): a minus sign asserting the put wing is richer,
        # bolted to a magnitude saying the wings are level. The existing flat-skew
        # test uses EXACTLY equal wings, where the raw format gives "+0.00" of its
        # own accord, so the clamp itself was unpinned. Both render sites, because
        # the reading line and the section format the number independently.
        flat = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(34.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.001, 61000.0, 62000.0),
        )
        assert flat.rr25 < 0  # genuinely negative, and genuinely below the epsilon
        assert abs(flat.rr25) < deribit._RR_ZERO_EPSILON
        section = deribit._skew_section(flat, "2026-08-05T06:06:00Z")
        assert "**RR25 (25Δ call IV − 25Δ put IV):** +0.00 vol points" in section
        line = deribit._reading_line("BTC", flat, None, 0)
        assert "both 25Δ wings carry the same implied vol (RR25 +0.00)" in line
        assert "-0.00" not in section
        assert "-0.00" not in line

    def test_the_flat_smile_epsilon_is_half_the_last_printed_decimal(self):
        # The test above asserts `abs(rr) < _RR_ZERO_EPSILON`, which moves WITH the
        # constant and so cannot pin it: raising it to 0.05 left all 252 tests
        # green. The value is derived, not chosen — half of the last decimal
        # `:+.2f` prints — so pin the derivation and the literal, then prove the
        # band between the two is still reported as a real skew.
        assert deribit._RR_ZERO_EPSILON == 0.005
        assert deribit._RR_ZERO_EPSILON == 0.01 / 2
        skewed = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(34.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.03, 61000.0, 62000.0),
        )
        # -0.03 resolves perfectly well at two decimals; a wider epsilon would
        # swallow a genuine put skew and call the smile flat, which is the -0.00
        # defect pointing the other way.
        section = deribit._skew_section(skewed, "2026-08-05T06:06:00Z")
        assert "**RR25 (25Δ call IV − 25Δ put IV):** -0.03 vol points" in section
        line = deribit._reading_line("BTC", skewed, None, 0)
        assert "25Δ puts are priced 0.03 vol points above 25Δ calls (RR25 -0.03" in line
        assert "carry the same implied vol" not in line

    def test_a_one_year_low_renders_as_the_1st_percentile_never_the_0th(self):
        # A percentile here counts the readings at or below the latest one, and
        # the latest reading is itself in the sample, so its true minimum is
        # 100/n. Widening the window to 365 days made that minimum 0.27, which
        # half-up rounding renders "0th" — a figure contradicting the definition
        # printed on the same line, and reachable any day BTC's DVOL prints a
        # one-year low. `_ordinal`'s unit tests all sit well above the floor.
        out = _report(dvol=_dvol_falling_days(365))
        assert (
            "**365d percentile:** the latest reading sits at the 1st percentile of the "
            "365 daily readings in the 365 days ending 2026-08-05" in out
        )
        assert "0th" not in out

    def test_chain_is_labelled_a_live_snapshot(self):
        assert "**Chain snapshot:** taken 2026-08-05T06:06:00Z" in _report()

    def test_reading_line_states_the_numbers_without_characterising_them(self):
        out = _report()
        # The tenor is stated, not just the expiry token: RR25 is not comparable
        # across tenors and the fallback can change which expiry this is, so a
        # reader who assumes ~30 days would otherwise be reading a different
        # quantity than the one printed.
        assert (
            "_Reading:_ In the live BTC 28AUG26 (23.1-day) chain, 25Δ puts are priced 4.74 "
            "vol points above 25Δ calls (RR25 -4.74, defined as 25Δ call IV minus 25Δ put IV); "
            "and BTC's latest DVOL reading (as of 2026-08-05) sits at the 6th percentile of "
            "the 35 daily readings in the 365-day window ending on the analysis date." in out
        )
        # No value-laden characterisation: this sentence is re-read verbatim by the
        # downstream research and risk agents.
        for banned in ("strongly", "modestly", "paying up", "near the top", "near the bottom"):
            assert banned not in out
        # The acronym must not be case-mangled by the sentence assembly.
        assert "dVOL" not in out

    def test_reading_line_names_the_proxy_currency(self):
        # The heading's proxy framing is the only other place that says whose
        # surface this is, and it does not survive a downstream summary.
        out = _report(asset="SOL")
        assert "BTC's latest DVOL reading (as of 2026-08-05) sits at" in out

    def test_reading_line_reports_a_call_skewed_chain_symmetrically(self):
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(36.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.0, 61000.0, 62000.0),
        )
        line = deribit._reading_line("BTC", skew, 55.0, 365)
        assert "25Δ calls are priced 2.00 vol points above 25Δ puts (RR25 +2.00" in line
        # A percentile clause must always carry the sample it was computed over.
        assert "55th percentile of the 365 daily readings" in line

    def test_reading_line_handles_a_flat_skew(self):
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(34.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.0, 61000.0, 62000.0),
        )
        line = deribit._reading_line("BTC", skew, None, 0)
        assert "both 25Δ wings carry the same implied vol (RR25 +0.00)" in line
        assert "percentile" not in line

    def test_reading_line_is_empty_with_nothing_to_state(self):
        assert deribit._reading_line("BTC", None, None, 0) == ""

    def test_todays_candle_is_labelled_in_progress(self):
        assert (
            "that day's candle was still open when it was read, so this is the level so far"
            in _report()
        )
        # Never "today's": on an ahead-of-clock run this line is dated by the DVOL
        # half's clock and can sit three lines below a caveat saying the clock has
        # since reached the NEXT day.
        assert "today's candle" not in _report()

    def test_a_past_date_has_no_in_progress_label(self):
        assert "still open" not in _report(curr_date="2026-07-20")

    def test_proxy_asset_is_labelled_in_the_heading(self):
        out = _report(asset="SOL")
        assert out.startswith("## Options Volatility — BTC (market-wide proxy for 'SOL', Deribit)")
        assert "not a signal specific to 'SOL'" in out

    def test_unsupported_asset_gets_a_no_signal_note(self):
        out, recorder = _run_report(asset="USDT")
        assert "This vendor reads Deribit options for BTC and ETH only" in out
        assert "'USDT' is not a recognized crypto risk asset" in out
        assert "Do not substitute BTC or ETH implied volatility" in out
        # The sentence must not claim anything about what Deribit itself lists:
        # nothing at runtime checks SUPPORTED_CURRENCIES against the exchange.
        assert "Deribit lists no options" not in out
        assert "no listed options market" not in out
        # What a *recognized* risk asset would have got is the DVOL level, not the
        # volatility surface — the skew never transfers. Pinned literally: the
        # structural assertions below pass under either wording.
        assert "BTC's DVOL level serves as a market-wide proxy" in out
        assert "surface" not in out
        # No DVOL figure of any kind is served for this symbol: no section, no
        # reading, no percentile of its own.
        assert "**DVOL" not in out
        assert "percentile" not in out
        assert "_Reading:_" not in out
        assert recorder.calls == []  # no request is opened for a symbol with no signal

    def test_eth_is_requested_and_labelled_as_eth(self):
        # Without this the currency argument could be dropped on the floor and the
        # report would serve BTC's surface under an ETH heading.
        out, recorder = _run_report(asset="ETH-USD")
        assert out.startswith("## Options Volatility — ETH (Deribit)")
        assert recorder.params_for(DVOL_ENDPOINT)["currency"] == "ETH"
        assert recorder.params_for(CHAIN_ENDPOINT)["currency"] == "ETH"

    def test_proxied_asset_requests_btc(self):
        _, recorder = _run_report(asset="SOL")
        assert recorder.params_for(DVOL_ENDPOINT)["currency"] == "BTC"

    def test_non_zero_padded_curr_date_is_normalised_before_filtering(self):
        # "2026-7-9" would compare wrong lexically against ISO candle dates and
        # silently admit later readings.
        out = _report(curr_date="2026-7-9")
        assert "window ending 2026-07-09" in out
        assert "on 2026-07-09" in out

    def test_invalid_curr_date_raises_the_vendor_error_not_a_bare_valueerror(self):
        # curr_date is an LLM-supplied tool argument. A bare ValueError lands in
        # route_to_vendor's generic lane and is logged with a traceback as "Vendor
        # 'deribit' failed" — a caller's typo reported as a vendor outage.
        with pytest.raises(deribit.DeribitError, match="is not a yyyy-mm-dd date") as excinfo:
            _report(curr_date="not-a-date")
        assert type(excinfo.value) is deribit.DeribitError
        assert "not-a-date" in str(excinfo.value)

    def test_missing_wing_renders_as_not_available(self):
        chain = [
            row
            for row in CHAIN
            if not (
                row["instrument_name"].split("-")[1] == "28AUG26"
                and float(row["instrument_name"].split("-")[2]) > 64000
                and row["instrument_name"].endswith("-C")
            )
        ]
        out = _report(chain=chain)
        assert "**25Δ call IV:** n/a (no two strike-adjacent quotes bracket this point" in out
        assert "**RR25 (25Δ call IV − 25Δ put IV):** n/a" in out
        assert "no wing vol is extrapolated" in out

    def test_a_mixed_case_chain_still_groups_into_one_expiry(self):
        # The expiry token returned by parse_instrument_name is BOTH the grouping
        # key (`c.expiry == expiry`) and the printed expiry, so dropping its
        # `.strip().upper()` splits one expiry into two buckets: each holds half
        # the ladder, neither brackets both wings, and the report shows a
        # thinned-out surface for an expiry that was fully quoted. The same edit
        # in parse_expiry drops a padded token on the 6/7-character length check,
        # and in parse_instrument_name drops a lowercase "c"/"p" option type — so
        # one chain carrying all four shapes covers all three sites.
        mixed = []
        for i, row in enumerate(CHAIN):
            row = dict(row)
            parts = row["instrument_name"].split("-")
            if len(parts) == 4 and parts[1] == "28AUG26":
                if i % 4 == 0:
                    parts[1] = parts[1].lower()
                elif i % 4 == 1:
                    parts[1] = f" {parts[1]} "
                elif i % 4 == 2:
                    parts[3] = parts[3].lower()
                else:
                    parts[3] = f" {parts[3]} "
                row["instrument_name"] = "-".join(parts)
            mixed.append(row)
        # All four shapes are actually present, or this proves nothing.
        names = [r["instrument_name"] for r in mixed]
        assert "BTC-28aug26-60000-C" in names
        assert "BTC- 28AUG26 -60000-P" in names
        assert "BTC-28AUG26-61000-c" in names
        assert "BTC-28AUG26-61000- P " in names
        out = _report(chain=mixed)
        assert "**Expiry used:** 28AUG26 — 23.1 days out" in out
        assert "8 call quotes and 8 put quotes on this expiry yielded a usable delta" in out
        # The full surface survives, exactly as on the untouched fixture.
        assert "**25Δ call IV:** 29.56% (between the 67,000 and 68,000 strikes)" in out
        assert "**25Δ put IV:** 34.30% (between the 61,000 and 62,000 strikes)" in out
        assert "28aug26" not in out


@pytest.mark.unit
class TestDvolSampleHonesty:
    def test_a_thin_window_reports_no_percentile(self):
        # The latest reading is itself in the sample, so a percentile over 5 of
        # them could not read below 20% — a number that would describe the sample
        # size, not the vol regime. The RANGE is still given: 5 readings are a
        # perfectly honest 30-day high/low, it is the percentile that is the claim.
        out = _report(dvol=_dvol_days(5))
        assert (
            "**365d percentile:** not computed — the 365 days ending 2026-08-05 hold only "
            "5 daily readings, and the latest reading is itself in that sample, so a "
            "percentile over it could not read below 20%" in out
        )
        assert "**30d range:** min 40.00% / max 40.40% over 5 daily readings" in out
        assert "sits at the" not in out
        # ... and the reading line drops the DVOL clause rather than stating it.
        assert "latest DVOL reading" not in out

    def test_a_series_entirely_outside_the_window_reports_no_range(self):
        # A stalled or backfilled feed: without this the range collapses onto the
        # single fallback observation and its percentile is always exactly 100,
        # which used to render as "DVOL is near the top of its 30-day range".
        out = _report(dvol={"data": [_candle("2026-05-01", 58.0)]})
        assert "not computed — no DVOL reading falls inside the 30 days ending 2026-08-05" in out
        assert "**DVOL (30-day implied vol index), latest:** 58.00% annualized on 2026-05-01" in out
        # The reading is inside the 365-day percentile window even though it is
        # outside the 30-day range window — but one reading is far too few.
        assert "**365d percentile:** not computed" in out
        assert "sits at the" not in out
        assert "Data lag" in out

    def test_two_readings_are_enough_for_a_range(self):
        # The other side of the threshold. Only the one-reading case was covered,
        # so both `_MIN_RANGE_SAMPLE = 3` and `<` -> `<=` shipped green — the round
        # pinned MAX_TENOR_DISTANCE_DAYS and MAX_FUTURE_DAYS literally and skipped
        # this constant. Two readings are a real high and a real low.
        assert deribit._MIN_RANGE_SAMPLE == 2
        out = _report(dvol={"data": [_candle("2026-08-04", 61.0), _candle("2026-08-05", 63.0)]})
        assert "**30d range:** min 61.00% / max 63.00% over 2 daily readings" in out

    def test_a_series_entirely_outside_the_percentile_window_says_so_without_dividing_by_zero(
        self,
    ):
        # The percentile's EMPTY-window branch, which nothing reached: every other
        # sparse test leaves at least one reading inside the 365 days, so the
        # non-empty branch always answered — and that branch computes
        # `100 / len(pct_window)`, a ZeroDivisionError the moment it does not.
        # Reachable because the fetch buffer runs 10 days beyond the window, so a
        # feed that stopped just over a year ago still returns a latest reading
        # with nothing inside either statistics window.
        out = _report(dvol={"data": [_candle("2025-07-31", 58.0)]})
        assert (
            "**365d percentile:** not computed — no DVOL reading falls inside the 365 days "
            "ending 2026-08-05" in out
        )
        # The "latest reading is itself in that sample" rationale belongs to the
        # other branch: appended here it would claim an empty window contains it.
        assert "the latest reading is itself in that sample" not in out
        assert "could not read below" not in out
        # The level still renders, which is the whole point of the fetch buffer.
        assert "**DVOL (30-day implied vol index), latest:** 58.00% annualized on 2025-07-31" in out

    def test_the_percentile_gate_counts_its_own_window_not_the_range_window(self):
        # The whole point of the 365-day widening: a feed that published daily for
        # 200 days and then stopped two months ago has ZERO readings in the 30-day
        # range window but a full regime's worth in the percentile window. Pointing
        # the min-sample gate at the range window would throw that away — and every
        # other sample test uses series short enough that the two windows are the
        # same size, so nothing else can see the difference.
        end = (datetime.strptime(TODAY, "%Y-%m-%d") - dt.timedelta(days=60)).strftime("%Y-%m-%d")
        out = _report(dvol=_dvol_days(200, end=end))
        assert "**30d range:** not computed" in out
        assert (
            "**365d percentile:** the latest reading sits at the 100th percentile of the "
            "200 daily readings in the 365 days ending 2026-08-05" in out
        )

    def test_minimum_sample_is_ten_readings(self):
        # Literal, not derived from the constant: a test whose input is computed
        # from the value it means to pin can never see that value move.
        assert deribit._MIN_PERCENTILE_SAMPLE == 10
        assert "**365d percentile:** not computed" in _report(dvol=_dvol_days(9))
        assert "percentile of the 10 daily readings" in _report(dvol=_dvol_days(10))
        assert "percentile of the 15 daily readings" in _report(dvol=_dvol_days(15))

    @pytest.mark.parametrize("lag_days,expected", [(2, False), (3, True)])
    def test_data_lag_caveat_fires_just_past_two_days(self, lag_days, expected):
        # Literal day counts, for the same reason as above.
        assert deribit.MAX_DATA_LAG_DAYS == 2
        end = (datetime.strptime(TODAY, "%Y-%m-%d") - dt.timedelta(days=lag_days)).strftime(
            "%Y-%m-%d"
        )
        out = _report(dvol=_dvol_days(15, end=end))
        assert ("Data lag" in out) is expected
        if expected:
            assert f"{lag_days} days before {TODAY}" in out

    def test_a_stale_reading_carries_its_age_into_the_reading_line(self):
        # The reading line is the sentence that survives a downstream summary, so
        # a percentile quoted there without an age reads as current however old
        # the underlying level actually is.
        end = (datetime.strptime(TODAY, "%Y-%m-%d") - dt.timedelta(days=10)).strftime("%Y-%m-%d")
        out = _report(dvol=_dvol_days(15, end=end))
        assert f"latest DVOL reading (as of {end}, 10 days old) sits at the" in out

    def test_a_fresh_reading_is_dated_but_carries_no_age_qualifier(self):
        # The as-of date is unconditional: the reading line is the one clause a
        # downstream summary quotes on its own, and a reading one or two days old
        # used to be quoted with no date at all. Only the "N days old" half and the
        # lag note stay gated on MAX_DATA_LAG_DAYS.
        out = _report()
        assert "Data lag" not in out
        assert "latest DVOL reading (as of 2026-08-05) sits at the" in out
        assert "days old" not in out


@pytest.mark.unit
class TestHistoricalDate:
    def test_chain_is_not_requested_at_all(self):
        # Quoting today's chain on a past date is future information; a prose
        # warning is not an auditable guard, so the half is simply withheld.
        out, recorder = _run_report(curr_date="2026-07-20")
        assert recorder.endpoints() == {DVOL_ENDPOINT}
        assert "not served for a historical analysis date" in out
        assert "Historical date:" in out
        assert "Do not substitute today's skew for 2026-07-20" in out

    def test_dvol_half_is_still_served(self):
        out = _report(curr_date="2026-07-20")
        assert "**DVOL (30-day implied vol index), latest:** 36.26% annualized on 2026-07-20" in out
        assert "The DVOL history below IS filtered to 2026-07-20" in out

    def test_a_backtest_date_is_not_told_its_own_data_is_stale(self):
        # Lateness is measured from min(curr_date, today). Measuring it from the
        # CLOCK instead would give every backtest run a fabricated staleness
        # caveat — the fixture ends on the analysis date, so nothing is late.
        out = _report(curr_date="2026-07-20")
        assert "Data lag" not in out
        assert "days old) sits at the" not in out

    def test_the_ahead_of_clock_note_stays_off_the_ordinary_same_day_path(self):
        # `curr_date > today`, not `>=`: on the default path this note would
        # contradict itself ("2026-08-05 is ahead of the UTC clock (2026-08-05)").
        out = _report()
        assert "is ahead of the UTC clock" not in out

    def test_today_serves_the_chain(self):
        out, recorder = _run_report()
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}
        assert "Historical date:" not in out

    def test_a_proxied_asset_gets_the_dvol_level_but_never_the_skew(self):
        # A market-wide vol LEVEL is a defensible stand-in; a 25Δ risk reversal is
        # a statement about demand for downside in BTC specifically and does not
        # transfer to SOL. Withheld rather than caveated, because the caveat has to
        # survive every downstream summarisation hop and the number does not.
        out, recorder = _run_report(asset="SOL")
        assert recorder.endpoints() == {DVOL_ENDPOINT}
        assert "**Options chain (ATM IV / 25Δ skew):** not served for 'SOL'" in out
        assert "The 25Δ skew is NOT shown" in out
        assert "**DVOL (30-day implied vol index), latest:** 34.43" in out
        # No skew figure of any kind may reach the report or the reading line.
        for banned in ("RR25", "vol points", "**Expiry used:**", "**ATM IV"):
            assert banned not in out

    def test_a_past_date_on_a_proxied_asset_reports_the_historical_reason(self):
        # Both withholding reasons hold at once. The precedence is a decision — the
        # lookahead guard is the stronger claim and names the date the caller asked
        # for — and it must be the SAME decision at all three sites that explain it.
        out, recorder = _run_report(asset="SOL", curr_date="2026-07-20")
        assert recorder.endpoints() == {DVOL_ENDPOINT}
        assert "Historical date:" in out
        assert "not served for a historical analysis date" in out
        assert "not served for 'SOL'" not in out
        # ... and the raise, when DVOL also dies, names the same reason.
        with pytest.raises(deribit.DeribitError, match="historical date 2026-07-20"):
            _report(asset="SOL", curr_date="2026-07-20", dvol=deribit.DeribitError("down"))

    def test_a_supported_currency_still_gets_both_halves(self):
        # The proxy rule must key on the proxy flag, not on "not BTC".
        _, recorder = _run_report(asset="ETH")
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}

    def test_a_proxied_asset_losing_dvol_names_the_proxy_reason(self):
        # Both halves absent must not be reported as "the chain request failed":
        # for a proxied asset the chain was never attempted.
        with pytest.raises(deribit.DeribitError, match="no Deribit chain of its own"):
            _report(asset="SOL", dvol=requests.ConnectionError("down"))

    def test_a_date_ahead_of_the_utc_clock_still_serves_the_chain(self):
        # cli/main.py derives the analysis date from a LOCAL clock, so east of UTC
        # every run in the first hours of the day carries a curr_date one day
        # ahead. Today's chain is then OLDER than the analysis date — not
        # lookahead — and withholding it would cost this vendor's main signal for
        # those hours every day.
        out, recorder = _run_report(curr_date="2026-08-06")
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}
        assert "**25Δ call IV:** 29.56%" in out
        assert "Historical date:" not in out
        assert "was ahead of the UTC clock (2026-08-05)" in out
        assert "which is BEFORE the analysis date rather than after it" in out

    def test_the_ahead_of_clock_note_never_points_at_a_half_that_is_absent(self):
        # Reachable on any Taipei-clock morning: curr_date runs ahead of UTC and
        # the chain request happens to fail. The note is an italic caveat — exactly
        # the line a downstream summary keeps when it drops the body — so a
        # "the chain figures below are the live book as of ..." claim above a
        # "the chain request failed" body hands the next agent a reading that was
        # never fetched.
        out = _report(curr_date="2026-08-06", chain=deribit.DeribitError("chain down"))
        assert "was ahead of the UTC clock (2026-08-05)" in out
        assert "the live book as of" not in out
        assert "The DVOL windows end at the analysis date, which had not arrived" in out

    def test_the_ahead_of_clock_note_drops_the_dvol_sentence_when_dvol_is_absent(self):
        out = _report(curr_date="2026-08-06", dvol=deribit.DeribitError("dvol down"))
        assert "was ahead of the UTC clock (2026-08-05)" in out
        assert "the live book as of 2026-08-05" in out
        assert "DVOL windows end at a date" not in out

    def test_the_ahead_of_clock_note_carries_both_when_both_halves_are_there(self):
        out = _report(curr_date="2026-08-06")
        assert "the live book as of 2026-08-05" in out
        assert "The DVOL windows end at the analysis date, which had not arrived" in out
        # One closing italic marker, not two, and no doubled full stop.
        assert "spans._" in out
        assert ".._" not in out

    def test_a_date_ahead_of_the_clock_does_not_invent_a_data_lag(self):
        # lag must be measured from the earlier of the analysis date and the
        # clock: an index that printed today is not "27 days late" merely because
        # someone asked about next month.
        out = _report(curr_date="2026-09-01")
        assert "Data lag" not in out
        assert "has not printed since" not in out

    def test_the_open_candle_label_follows_the_clock_not_the_analysis_date(self):
        # The candle dated today is open because the day is not over, which has
        # nothing to do with which date is being analysed.
        assert "still open when it was read, so this is the level so far" in _report(curr_date="2026-08-06")

    def test_dvol_failure_on_a_historical_date_raises(self):
        # Nothing left to report: the chain is withheld by design, not by failure.
        with pytest.raises(deribit.DeribitError, match="not served for the historical date"):
            _report(curr_date="2026-07-20", dvol=deribit.DeribitError("dvol down"))

    @pytest.mark.parametrize("curr_date,days_ahead", [("2026-08-07", 2), ("2026-09-01", 27)])
    def test_a_date_further_ahead_than_the_tolerance_withholds_the_chain(
        self, curr_date, days_ahead
    ):
        # One day covers every timezone on earth, so past that the gap is not an
        # offset but a bad LLM-supplied argument, and "the live book, weeks before
        # the analysis date" is not a reading of that date under any reading. Both
        # ends of the range: two days is the first withheld value, and a month out
        # must not fall through to some other branch.
        out, recorder = _run_report(curr_date=curr_date)
        assert recorder.endpoints() == {DVOL_ENDPOINT}
        assert (
            f"_Analysis date {curr_date} is {days_ahead} days ahead of the UTC clock "
            f"(2026-08-05), further than any timezone offset explains." in out
        )
        assert (
            f"**Options chain (ATM IV / 25Δ skew):** not served for an analysis date "
            f"{days_ahead} days ahead of the UTC clock — see the note above. Do not "
            f"substitute today's skew for {curr_date}." in out
        )
        # The ordinary ahead-of-clock note must not ALSO fire: it promises a live
        # book below that was never fetched.
        assert "The chain figures below are the live book as of" not in out
        assert "Historical date:" not in out
        # The DVOL half is genuinely date-filtered and is still served.
        assert "**DVOL (30-day implied vol index), latest:** 34.43" in out

    def test_the_future_tolerance_is_the_constant_not_a_hardcoded_day(self, monkeypatch):
        # A literal `> 1` in the comparison behaves identically at today's value,
        # so the two forms are indistinguishable from behaviour alone — and the
        # constant carries the whole rationale for the number. Move it and the
        # boundary must move with it.
        assert deribit.MAX_FUTURE_DAYS == 1
        # At the shipped value, one day ahead is served (a Taipei-clock morning)
        # and two days ahead is not.
        assert _run_report(curr_date="2026-08-06")[1].endpoints() == {
            DVOL_ENDPOINT,
            CHAIN_ENDPOINT,
        }
        assert _run_report(curr_date="2026-08-07")[1].endpoints() == {DVOL_ENDPOINT}
        monkeypatch.setattr(deribit, "MAX_FUTURE_DAYS", 3)
        out, recorder = _run_report(curr_date="2026-08-07")
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}
        assert "further than any timezone offset explains" not in out
        assert "was ahead of the UTC clock (2026-08-05)" in out

    def test_dvol_failure_on_a_far_future_date_names_that_reason(self):
        # The fourth branch of the both-halves-failed raise. Without it a
        # far-future date falls through to "Deribit returned neither DVOL nor an
        # options chain", which reports a chain outage that never happened — the
        # chain was withheld by policy and never requested.
        with pytest.raises(deribit.DeribitError) as excinfo:
            _report(curr_date="2026-08-07", dvol=deribit.DeribitError("dvol down"))
        assert str(excinfo.value) == (
            "Deribit DVOL is unavailable for BTC (dvol down), and the options chain is not "
            "served for 2026-08-07, which is 2 days ahead of the UTC clock (2026-08-05)"
        )

    def test_the_chain_is_dated_by_the_clock_it_was_actually_fetched_on(self):
        # DVOL is fetched first, and its worst case is two timeouts plus the retry
        # pause (~62s), so the instant taken at the top of the function is not
        # when the book was read. The clock is re-taken immediately before the
        # chain fetch, and that second instant must drive BOTH the printed
        # snapshot time AND the tenor arithmetic — a single patched return value
        # makes the two indistinguishable, which is why this uses a side_effect
        # list. Fourteen hours apart so the tenor moves a visible tenth of a day.
        out, _ = _run_report_with_clocks([NOW, datetime(2026, 8, 5, 20, 6, tzinfo=timezone.utc)])
        assert "**Chain snapshot:** taken 2026-08-05T20:06:00Z" in out
        assert "06:06:00Z" not in out
        # parse_chain / compute_skew run against the same second instant.
        assert "**Expiry used:** 28AUG26 — 22.5 days out" in out
        assert "28AUG26 (22.5-day) chain" in out
        assert "23.1 days out" not in out

    def test_a_chain_fetch_that_crosses_utc_midnight_is_withheld_as_historical(self):
        # The reason the clock is re-taken rather than reused: across a UTC
        # midnight the opening instant is not even the same DAY as the fetch, so
        # curr_date = D would be served the D+1 book. That is precisely the
        # lookahead the withholding rule exists to prevent, and it is invisible to
        # any test that pins the clock to one value.
        out, recorder = _run_report_with_clocks(
            [
                datetime(2026, 8, 5, 23, 59, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 6, 0, 0, 30, tzinfo=timezone.utc),
            ]
        )
        assert recorder.endpoints() == {DVOL_ENDPOINT}
        assert "**Options chain (ATM IV / 25Δ skew):** not served for a historical analysis" in out
        assert "Historical date:" in out
        assert "Do not substitute today's skew for 2026-08-05" in out
        # The DVOL half is unaffected and still dated by the FIRST instant's day.
        assert "**DVOL (30-day implied vol index), latest:** 34.43% annualized on 2026-08-05" in out

    def test_an_ahead_of_clock_run_crossing_midnight_dates_the_chain_by_its_own_clock(self):
        # The midnight case one day further on, which is the routine east-of-UTC
        # one: curr_date is a day ahead, so the chain is SERVED, and the clock
        # crosses into curr_date while DVOL is being fetched. Re-clocking the fetch
        # without re-clocking the sentence that describes it left the italic
        # caveat — the line the module says a downstream summary keeps — asserting
        # a book "as of 2026-08-05, BEFORE the analysis date" three lines above a
        # snapshot stamped 2026-08-06, inside it.
        out, recorder = _run_report_with_clocks(
            [
                datetime(2026, 8, 5, 23, 59, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 6, 0, 0, 30, tzinfo=timezone.utc),
            ],
            curr_date="2026-08-06",
        )
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}
        assert "**Chain snapshot:** taken 2026-08-06T00:00:30Z" in out
        assert "the live book as of 2026-08-06" in out
        # The OPENING sentence keeps the clock the run STARTED on. Clocking it by
        # chain_date instead shipped green and rendered "Analysis date 2026-08-06
        # was ahead of the UTC clock (2026-08-06)" — a date declared ahead of
        # itself, in the caveat line. This is the only state where the two clocks
        # differ, so it is the only test that can pin the choice.
        assert (
            "_Analysis date 2026-08-06 was ahead of the UTC clock (2026-08-05) when this "
            "report was built." in out
        )
        # ... and the DVOL sentence must not contradict the chain sentence above it
        # by claiming the analysis date has still not arrived.
        assert "which had not arrived when they were read" in out
        assert "end at a date that has not arrived" not in out
        assert "the UTC clock reached the analysis date while this report was being built" in out
        # The three claims the stale clock used to make.
        assert "the live book as of 2026-08-05" not in out
        assert "which is BEFORE the analysis date" not in out


@pytest.mark.unit
class TestPartialDegradation:
    def test_chain_survives_a_dvol_outage(self):
        out = _report(dvol=deribit.DeribitError("dvol down"))
        assert "**DVOL:** unavailable" in out
        assert "Do not fabricate a DVOL level" in out
        # The half that worked is still fully rendered.
        assert "**25Δ call IV:** 29.56%" in out

    def test_dvol_survives_a_chain_outage(self):
        out = _report(chain=deribit.DeribitError("chain down"))
        assert "**Options chain (ATM IV / 25Δ skew):** unavailable" in out
        assert "do not fabricate skew" in out
        assert "**DVOL (30-day implied vol index), latest:** 34.43" in out

    def test_an_empty_dvol_response_is_not_called_a_failed_request(self):
        # _fetch_dvol also raises when the request SUCCEEDED and carried no reading
        # on or before curr_date. Calling that "the volatility-index request
        # failed" sends an operator to the network when the network was fine.
        out = _report(dvol={"data": [_candle("2026-08-06", 40.0)]})
        assert "**DVOL:** unavailable — No BTC DVOL readings on or before 2026-08-05" in out
        assert "request failed" not in out

    def test_an_unusable_chain_response_is_not_called_a_failed_request_either(self):
        # The mirror of the DVOL case above, and untested: four of the five
        # reachable causes come from a SUCCESSFUL 200. Here every row fails the
        # field checks — the network was fine — so naming it "the chain request
        # failed" sends whoever is on call hunting an outage that will never pass,
        # in the same sentence that quotes the module saying otherwise.
        out = _report(chain=[{"instrument_name": "BTC-28AUG26-64000-C"}])
        assert (
            "**Options chain (ATM IV / 25Δ skew):** unavailable — Deribit returned no usable "
            "BTC option contracts" in out
        )
        assert "request failed" not in out
        assert "The DVOL figures above are unaffected; do not fabricate skew." in out

    def test_the_source_bullet_advertises_a_dvol_window_only_when_dvol_arrived(self):
        # Unconditional, this clause advertised "DVOL window ending 2026-08-05"
        # two lines above "**DVOL:** unavailable" — the header bullet is exactly
        # what a downstream summary keeps when it drops the body, so it would hand
        # the next agent a window that was never populated.
        failed = _report(dvol=deribit.DeribitError("dvol down"))
        assert "- Source: deribit.com public API\n" in failed
        assert "DVOL window ending" not in failed
        assert "**DVOL:** unavailable" in failed
        # ... and it is still there on the ordinary path.
        assert "- Source: deribit.com public API | DVOL window ending 2026-08-05" in _report()

    def test_a_failed_half_logs_a_traceback_not_just_its_message(self, caplog):
        # `exc_info=True` is the difference between an ordinary vendor outage and
        # a bug in this module: _try_fetch deliberately catches bare Exception, so
        # without the traceback a TypeError in the parsing code is logged as
        # "Deribit options chain unavailable" and is indistinguishable from
        # Deribit being down. Nothing asserted it, so it could be dropped.
        with caplog.at_level(logging.WARNING):
            _report(chain=RuntimeError("something nobody predicted"))
        records = [
            r for r in caplog.records if "options chain unavailable for BTC" in r.getMessage()
        ]
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[0] is RuntimeError

    def test_a_mixed_outage_is_not_reported_as_a_throttle(self):
        # `all`, not `any`: one 429 alongside a genuinely broken endpoint must not
        # put a broken vendor into the router's rate-limit lane.
        with pytest.raises(deribit.DeribitError):
            _report(
                dvol=VendorRateLimitError("throttled"),
                chain=deribit.DeribitError("chain down"),
            )

    def test_a_throttle_on_every_request_made_keeps_the_rate_limit_lane(self):
        with pytest.raises(VendorRateLimitError):
            _report(
                dvol=VendorRateLimitError("throttled"),
                chain=VendorRateLimitError("throttled"),
            )

    def test_an_empty_side_says_so_rather_than_blaming_the_bracket(self):
        # "no two strike-adjacent quotes bracket this point" names two causes that
        # did not happen when the side produced no usable quote at all.
        contracts = [c for c in deribit.parse_chain(CHAIN, NOW) if not c.is_call]
        section = deribit._skew_section(
            deribit.compute_skew(contracts, NOW), "2026-08-05T06:06:00Z"
        )
        assert "0 call quotes and 8 put quotes" in section
        assert (
            "**25Δ call IV:** n/a (no quote on this side of the expiry yielded a usable delta)"
            in section
        )
        assert "**ATM IV (50Δ):** 31.12%" in section
        assert "**25Δ put IV:** 34.30%" in section
        # The bracket/monotonicity wording must not be reused for an empty side.
        assert "no two strike-adjacent quotes bracket" not in section

    def test_the_atm_reason_reads_both_curves_not_their_sum(self):
        # ATM is interpolated on the call curve at +0.5 and then the put curve at
        # -0.5, so one call plus one put is two quotes and still no pair. Summing
        # the sides would clear a >= 2 threshold and print the bracket/monotonicity
        # wording — the false cause this reason exists to avoid.
        one_each = [
            _synthetic_contract("SOLO", 30, strike=60000.0, is_call=True),
            _synthetic_contract("SOLO", 30, strike=68000.0, is_call=False),
        ]
        skew = deribit.compute_skew(one_each, NOW)
        assert (skew.n_calls, skew.n_puts) == (1, 1)
        assert skew.atm is None
        assert deribit._atm_line(skew) == (
            "n/a (neither the call nor the put curve has two usable quotes, so there is no "
            "pair to bracket the 50Δ point with)"
        )

    def test_a_populated_curve_still_gets_the_bracket_reason_for_atm(self):
        # Four puts and no calls: a pair existed, so the honest reason really is
        # that nothing brackets 50Δ, not that quotes were missing.
        puts_only = [
            c for c in deribit.parse_chain(CHAIN, NOW) if not c.is_call and c.strike >= 65000
        ]
        skew = deribit.compute_skew(puts_only, NOW)
        assert (skew.n_calls, skew.atm) == (0, None)
        assert "no two strike-adjacent quotes bracket" in deribit._atm_line(skew)

    def test_a_populated_call_curve_gets_the_bracket_reason_too(self):
        # Mirror of the above. ATM is tried on the CALL curve FIRST, so calls-only
        # is the commoner shape — yet only the puts-only side was covered, which
        # left `max(n_calls, n_puts)` replaceable by `n_puts` with the suite green.
        calls_only = [
            _synthetic_contract("SOLO", 30, strike=float(k), is_call=True) for k in (58000, 59000)
        ]
        skew = deribit.compute_skew(calls_only, NOW)
        assert (skew.n_calls, skew.n_puts, skew.atm) == (2, 0, None)
        assert "no two strike-adjacent quotes bracket" in deribit._atm_line(skew)

    def test_exactly_two_quotes_on_one_curve_counts_as_a_pair(self):
        # The n == 2 boundary the predicate turns on. Raised to >= 3, this same
        # input is told its quotes were missing when two were present.
        strikes = [
            _synthetic_contract("SOLO", 30, strike=float(k), is_call=True) for k in (58000, 59000)
        ]
        assert "no two strike-adjacent quotes bracket" in deribit._atm_line(
            deribit.compute_skew(strikes, NOW)
        )
        assert "neither the call nor the put curve" in deribit._atm_line(
            deribit.compute_skew(strikes[:1], NOW)
        )

    def test_the_rendered_section_uses_the_atm_reason(self):
        # The tests above call _atm_line directly, so reverting _skew_section to
        # the old _wing_line(atm, n_calls + n_puts) call left every one of them
        # green while the wrong wording shipped. Assert through the RENDER.
        one_each = [
            _synthetic_contract("SOLO", 30, strike=60000.0, is_call=True),
            _synthetic_contract("SOLO", 30, strike=68000.0, is_call=False),
        ]
        section = deribit._skew_section(deribit.compute_skew(one_each, NOW), "2026-08-05T06:06:00Z")
        assert "**ATM IV (50Δ):** n/a (neither the call nor the put curve has two usable" in section
        assert "no two strike-adjacent quotes bracket" not in section

    def test_a_single_quote_side_says_a_pair_is_what_is_missing(self):
        # One quote cannot bracket anything, so "no two strike-adjacent quotes
        # bracket this point" is technically true but points the reader at the
        # smile's shape when the real answer is that the side has one contract.
        assert deribit._wing_line(None, 1) == (
            "n/a (only one usable quote on this side, so no pair can bracket this point)"
        )
        assert deribit._wing_line(None, 0) == (
            "n/a (no quote on this side of the expiry yielded a usable delta)"
        )
        # With a real pair present, the bracket/monotonicity wording is correct.
        assert "strike-adjacent" in deribit._wing_line(None, 2)

    @pytest.mark.parametrize(
        "count,expected",
        [(0, "0 daily readings"), (1, "1 daily reading"), (2, "2 daily readings")],
    )
    def test_a_lone_reading_is_singular(self, count, expected):
        # The singular is the whole reason this helper exists, and it is reachable
        # at two render sites (the 30d range line and the too-few-for-a-percentile
        # line) — but nothing called it with 0 or 1, so the conditional could be
        # deleted outright with the suite green.
        assert deribit._readings(count) == expected

    def test_a_one_reading_feed_renders_singular_end_to_end(self):
        # A single reading buys no range at all: min and max would both be the
        # level already printed above, which reads as a month of pinned vol.
        out = _report(dvol={"data": [_candle("2026-08-05", 60.5)]})
        assert (
            "**30d range:** not computed — the 30 days ending 2026-08-05 hold only 1 daily "
            "reading, so a min and a max would both be the level above" in out
        )
        assert "min 60.50" not in out
        assert "hold only 1 daily reading, and the latest reading is itself in that sample" in out
        assert "1 daily readings" not in out

    def test_an_all_rows_rejected_chain_names_a_shape_change(self):
        # The only operator-facing diagnostic in this area: every row failing the
        # field checks means a renamed field, not a quiet market. Untested, the
        # message reverts to a bare "no usable contracts" and sends whoever is on
        # call to wait out an outage that will not end.
        out = _report(chain=[{"instrument_name": "BTC-28AUG26-64000-C"}])
        assert "every row failed the instrument-name, mark-IV, underlying or open-interest" in out
        assert "response-shape change rather than an empty market" in out

    def test_report_sections_are_blank_line_separated(self):
        # Consecutive italic caveats joined by a single newline render as one
        # run-on paragraph in the analyst's markdown.
        out = _report(asset="SOL")
        assert "\n\n" in out
        assert "._\n\n" in out

    def test_an_unexpected_exception_still_leaves_the_other_half(self):
        # The promise is "either half survives", not "either of two exception
        # types survives" — an unforeseen error must not discard a good half.
        out = _report(chain=RuntimeError("something nobody predicted"))
        assert "**Options chain (ATM IV / 25Δ skew):** unavailable" in out
        assert "**DVOL (30-day implied vol index), latest:** 34.43" in out

    def test_rate_limited_half_degrades_rather_than_escaping(self):
        out = _report(dvol=VendorRateLimitError("429"))
        assert "**DVOL:** unavailable" in out
        assert "**25Δ call IV:** 29.56%" in out

    def test_both_halves_down_raises_for_the_router(self):
        with pytest.raises(deribit.DeribitError, match="neither DVOL nor an options chain"):
            _report(
                chain=deribit.DeribitError("chain down"), dvol=deribit.DeribitError("dvol down")
            )

    def test_both_halves_throttled_keeps_the_rate_limit_lane(self):
        # Collapsing a throttle into a DeribitError would make it indistinguishable
        # from a broken vendor to any future multi-vendor chain.
        with pytest.raises(VendorRateLimitError, match="rate-limited every request"):
            _report(chain=VendorRateLimitError("429"), dvol=VendorRateLimitError("429"))

    def test_a_throttle_on_a_historical_date_keeps_the_rate_limit_lane(self):
        # The chain is never attempted there, so DVOL's 429 is the only verdict —
        # and it must still reach the router as a throttle, not as a broken vendor.
        with pytest.raises(VendorRateLimitError, match="rate-limited every request"):
            _report(curr_date="2026-07-20", dvol=VendorRateLimitError("429"))

    def test_empty_chain_is_treated_as_a_chain_outage(self):
        # An unsupported currency answers 200 with an empty list; that is missing
        # data, not a usable surface.
        out = _report(chain=[])
        assert "**Options chain (ATM IV / 25Δ skew):** unavailable" in out
        assert "no usable BTC option contracts" in out


# --------------------------------------------------------------------------- #
# Router integration
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouting:
    def test_the_vendor_ships_disabled(self):
        # The entire justification for shipping this keyless vendor off is that
        # merging must not change the running paper deployment's analyst input
        # surface — there is no server-side action to date such a change from.
        # Nothing pinned it: the options_enabled fixture FORCES the value and
        # restores a hardcoded "none", so it cannot see DEFAULT_CONFIG move.
        # Flipping this to "deribit" would otherwise ship green.
        from tradingagents.default_config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["data_vendors"]["options_data"] == "none"
        assert interface.is_category_disabled("options_data", "get_options_market") is True

    def test_category_routes_to_deribit(self, options_enabled):
        assert interface.get_category_for_method("get_options_market") == "options_data"
        with mock.patch.dict(
            interface.VENDOR_METHODS,
            {"get_options_market": {"deribit": lambda *a, **k: "OPTIONS_OK"}},
            clear=False,
        ):
            assert interface.route_to_vendor("get_options_market", "BTC", TODAY) == "OPTIONS_OK"

    def test_optional_category_degrades_to_sentinel(self, options_enabled):
        def _boom(*a, **k):
            raise deribit.DeribitError("Deribit unavailable")

        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_options_market": {"deribit": _boom}}, clear=False
        ):
            out = interface.route_to_vendor("get_options_market", "BTC", TODAY)
        assert "DATA_UNAVAILABLE" in out
        assert "options_data" in out

    def test_rate_limited_vendor_degrades_to_sentinel(self, options_enabled):
        def _throttled(*a, **k):
            raise VendorRateLimitError("Deribit rate-limited")

        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_options_market": {"deribit": _throttled}}, clear=False
        ):
            out = interface.route_to_vendor("get_options_market", "BTC", TODAY)
        assert "DATA_UNAVAILABLE" in out

    def test_ships_disabled_so_a_merge_cannot_change_a_running_deployment(self):
        called = {"n": 0}

        def _impl(*a, **k):
            called["n"] += 1
            return "should not happen"

        with mock.patch.dict(
            interface.VENDOR_METHODS, {"get_options_market": {"deribit": _impl}}, clear=False
        ):
            out = interface.route_to_vendor("get_options_market", "BTC", TODAY)
        assert "disabled by configuration" in out
        assert called["n"] == 0

    def test_vendor_is_registered(self):
        assert "deribit" in interface.VENDOR_LIST
        assert "options_data" in interface.OPTIONAL_CATEGORIES
        assert interface.VENDOR_METHODS["get_options_market"]["deribit"] is (
            deribit.get_options_market_data
        )


# --------------------------------------------------------------------------- #
# Market-analyst wiring
# --------------------------------------------------------------------------- #
class _CapturingLLM:
    """Records the tools bound to it and the prompt it was invoked with."""

    def __init__(self):
        self.bound_tools = None
        self.prompt_value = None

    def bind_tools(self, tools):
        self.bound_tools = list(tools)

        def _run(prompt_value):
            self.prompt_value = prompt_value
            return AIMessage(content="ok")

        return RunnableLambda(_run)


def _run_analyst(asset_type, ticker="BTC-USD"):
    llm = _CapturingLLM()
    create_market_analyst(llm)(
        {
            "trade_date": TODAY,
            "asset_type": asset_type,
            "company_of_interest": ticker,
            "messages": [],
        }
    )
    return llm


@pytest.mark.unit
class TestMarketAnalystWiring:
    def test_crypto_binds_the_options_tool(self, options_enabled):
        bound = {t.name for t in _run_analyst("crypto").bound_tools}
        assert "get_options_market" in bound
        # the existing market tools are untouched
        assert bound >= {"get_stock_data", "get_indicators", "get_verified_market_snapshot"}

    def test_stock_does_not_bind_the_options_tool(self, options_enabled):
        bound = {t.name for t in _run_analyst("stock", "AAPL").bound_tools}
        assert "get_options_market" not in bound
        assert bound >= {"get_stock_data", "get_indicators", "get_verified_market_snapshot"}

    def test_missing_asset_type_defaults_to_stock(self, options_enabled):
        llm = _CapturingLLM()
        create_market_analyst(llm)(
            {"trade_date": TODAY, "company_of_interest": "AAPL", "messages": []}
        )
        assert "get_options_market" not in {t.name for t in llm.bound_tools}

    def test_disabled_category_is_not_bound(self):
        # Binding a tool whose category is switched off would spend a tool call
        # only to receive the disabled sentinel. This is also the shipped default.
        bound = {t.name for t in _run_analyst("crypto").bound_tools}
        assert "get_options_market" not in bound

    def test_prompt_advertises_the_tool_only_when_it_is_bound(self, options_enabled):
        crypto_prompt = str(_run_analyst("crypto").prompt_value)
        stock_prompt = str(_run_analyst("stock", "AAPL").prompt_value)
        assert "get_options_market" in crypto_prompt
        assert "risk reversal" in crypto_prompt
        assert "get_options_market" not in stock_prompt

    def test_the_prompt_exempts_the_forward_from_the_verified_snapshot_rule(self, options_enabled):
        # The base prompt makes the verified snapshot the source of truth for any
        # PRICE-LEVEL claim and orders the model to flag conflicts. **Forward:** is
        # a price level that is supposed to differ from spot — different venue,
        # different index, plus basis — so without an exemption every enabled
        # crypto run either raises a spurious data-integrity flag or drops the
        # forward. Nothing pinned this: the only crypto-prompt assertions were the
        # tool name and "risk reversal", and the latter also appears elsewhere in
        # the same string.
        crypto_prompt = str(_run_analyst("crypto").prompt_value)
        assert "source of truth for any exact OHLCV, price-level" in crypto_prompt
        assert "not spot" in crypto_prompt
        assert "do not reconcile the two and do not flag them as a discrepancy" in crypto_prompt

    def test_disabled_category_leaves_no_dangling_prompt_text(self):
        assert "get_options_market" not in str(_run_analyst("crypto").prompt_value)

    def test_market_toolnode_can_execute_the_options_tool(self):
        # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
        nodes = TradingAgentsGraph._create_tool_nodes(None)
        assert "get_options_market" in set(nodes["market"].tools_by_name), (
            "the options tool is bound to the market analyst for crypto assets but not "
            "registered in the market ToolNode, so the model's call fails."
        )
