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


def _candle(day: str, close: float):
    """A DVOL 1D candle stamped at midnight UTC of ``day`` (as Deribit stamps them)."""
    ts = int(datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    return [ts, close, close, close, close]


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


def _ladder(name: str, days_out: float, call_max: float | None = None, iv: float = 30.0):
    """A full strike ladder for one synthetic expiry, optionally truncated on calls.

    ``call_max`` stops the call side at that strike, which is how an expiry is made
    unable to reach down to 0.25 call delta while its put wing stays intact.
    """
    return [
        _synthetic_contract(name, days_out, strike=float(k), is_call=is_call, iv=iv)
        for k in range(58000, 73000, 1000)
        for is_call in (True, False)
        if not (is_call and call_max is not None and k > call_max)
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
class TestSelectExpiry:
    def test_picks_the_expiry_nearest_thirty_days(self):
        contracts = deribit.parse_chain(CHAIN, NOW)
        # 5AUG26 is ~0 days out, 28AUG26 ~23, 25DEC26 ~142.
        assert deribit.select_expiry(contracts, NOW) == "28AUG26"

    def test_target_tenor_is_thirty_days_not_some_other_number(self):
        # The pin is the literal constant. The expiry set below only discriminates
        # a ~21-day band of targets (every value from 22 to 42 picks MID), so it
        # cannot stand in for the constant on its own — it was claimed to, and did
        # not.
        assert deribit.TARGET_DTE_DAYS == 30
        near = _synthetic_contract("NEAR", 12)
        mid = _synthetic_contract("MID", 31)
        far = _synthetic_contract("FAR", 55)
        assert deribit.select_expiry([near, mid, far], NOW) == "MID"

    def test_ranking_orders_every_qualifying_expiry_best_first(self):
        # compute_skew walks this order when the nearest expiry cannot bracket a
        # wing, so the tail of the ranking is load-bearing, not just its head.
        near = _synthetic_contract("NEAR", 12)
        mid = _synthetic_contract("MID", 31)
        far = _synthetic_contract("FAR", 55)
        # 31d is 1 from target, 12d is 18 away, 55d is 25 away.
        assert deribit.rank_expiries([far, near, mid], NOW) == ["MID", "NEAR", "FAR"]
        assert deribit.rank_expiries([_synthetic_contract("TOO_NEAR", 3)], NOW) == []

    def test_expiry_inside_the_floor_is_excluded(self):
        # With only the sub-7-day expiry and the far one listed, the near expiry
        # must lose even though its tenor error is the smaller of the two.
        contracts = [
            c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry in ("5AUG26", "25DEC26")
        ]
        assert deribit.select_expiry(contracts, NOW) == "25DEC26"

    def test_returns_none_when_nothing_clears_the_floor(self):
        contracts = [c for c in deribit.parse_chain(CHAIN, NOW) if c.expiry == "5AUG26"]
        assert deribit.select_expiry(contracts, NOW) is None

    def test_exactly_at_the_seven_day_floor_is_eligible(self):
        assert deribit.MIN_DTE_DAYS == 7
        assert deribit.select_expiry([_synthetic_contract("AT_FLOOR", 7)], NOW) == "AT_FLOOR"

    def test_just_inside_the_floor_is_not_eligible(self):
        assert deribit.select_expiry([_synthetic_contract("JUST_INSIDE", 6.99)], NOW) is None

    def test_tie_goes_to_the_longer_dated_expiry(self):
        # 23 and 37 days are equidistant from the 30-day target; the pin-noise
        # rationale that motivates the floor also breaks the tie.
        near = _synthetic_contract("NEAR", 23)
        far = _synthetic_contract("FAR", 37)
        assert deribit.select_expiry([near, far], NOW) == "FAR"
        assert deribit.select_expiry([far, near], NOW) == "FAR"


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
        with pytest.raises(deribit.DeribitError, match="at least 7 days out"):
            deribit.compute_skew(contracts, NOW)

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
        assert "could not bracket both 25Δ wings, so this is the next qualifying expiry" in section
        assert f"the listed expiry closest to {deribit.TARGET_DTE_DAYS} days" not in section

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
        assert deribit._DVOL_MAX_CANDLES_PER_RESPONSE == 1000
        span_days = deribit.DVOL_PERCENTILE_WINDOW_DAYS + deribit._DVOL_FETCH_BUFFER_DAYS
        assert span_days < deribit._DVOL_MAX_CANDLES_PER_RESPONSE

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
            pytest.raises(deribit.DeribitError, match="No DVOL readings"),
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
            deribit._dvol_section(broken, datetime(2026, 8, 5), TODAY, TODAY)

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

    def test_every_reading_being_unusable_raises(self):
        # Skipping must not degrade into an empty series presented as a report.
        recorder = _RequestRecorder(dvol={"data": [_candle("2026-08-05", 0.0)]})
        with (
            mock.patch.object(deribit, "_request", side_effect=recorder),
            pytest.raises(deribit.DeribitError, match="No DVOL readings"),
        ):
            deribit._fetch_dvol("BTC", datetime(2026, 8, 5))


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
        # Reachable whenever the sample size is 16, 20 or 24.
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
            pytest.raises(deribit.DeribitError, match="unreachable after"),
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
            pytest.raises(deribit.DeribitError, match="unreachable after"),
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
        assert "**DVOL (30-day implied vol index), latest:** 34.43 on 2026-08-05" in out
        assert "**30d range:** min 34.04 / max 39.53 over 30 daily readings" in out
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
        assert "**30d range:** min 77.00 / max 79.90 over 30 daily readings" in out
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
        assert "8 call / 8 put quotes on this expiry yielded a usable delta" in out

    def test_chain_is_labelled_a_live_snapshot(self):
        assert "**Chain snapshot:** taken 2026-08-05T06:06:00Z" in _report()

    def test_reading_line_states_the_numbers_without_characterising_them(self):
        out = _report()
        assert (
            "_Reading:_ In the live BTC 28AUG26 chain, 25Δ puts are priced 4.74 vol points "
            "above 25Δ calls (RR25 -4.74, defined as 25Δ call IV minus 25Δ put IV); and BTC's "
            "latest DVOL reading sits at the 6th percentile of the 35 daily readings in the "
            "365-day window ending on the analysis date." in out
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
        assert "BTC's latest DVOL reading sits at" in out

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
        assert "today's candle is still open, so this is the level so far" in _report()

    def test_a_past_date_has_no_in_progress_label(self):
        assert "still open" not in _report(curr_date="2026-07-20")

    def test_proxy_asset_is_labelled_in_the_heading(self):
        out = _report(asset="SOL")
        assert out.startswith("## Options Volatility — BTC (market-wide proxy for 'SOL', Deribit)")
        assert "not an 'SOL'-specific signal" in out

    def test_unsupported_asset_gets_a_no_signal_note(self):
        out, recorder = _run_report(asset="USDT")
        assert "no listed options market for 'USDT'" in out
        assert "Do not substitute BTC or ETH implied volatility" in out
        assert "DVOL" not in out
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

    def test_invalid_curr_date_raises(self):
        with pytest.raises(ValueError):
            _report(curr_date="not-a-date")

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
            "5 daily readings, so a percentile over them could not read below 20%" in out
        )
        assert "**30d range:** min 40.00 / max 40.40 over 5 daily readings" in out
        assert "sits at the" not in out
        # ... and the reading line drops the DVOL clause rather than stating it.
        assert "latest DVOL reading" not in out

    def test_a_series_entirely_outside_the_window_reports_no_range(self):
        # A stalled or backfilled feed: without this the range collapses onto the
        # single fallback observation and its percentile is always exactly 100,
        # which used to render as "DVOL is near the top of its 30-day range".
        out = _report(dvol={"data": [_candle("2026-05-01", 58.0)]})
        assert "not computed — no DVOL reading falls inside the 30 days ending 2026-08-05" in out
        assert "**DVOL (30-day implied vol index), latest:** 58.00 on 2026-05-01" in out
        # The reading is inside the 365-day percentile window even though it is
        # outside the 30-day range window — but one reading is far too few.
        assert "**365d percentile:** not computed" in out
        assert "sits at the" not in out
        assert "Data lag" in out

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

    def test_a_fresh_reading_carries_no_age_qualifier(self):
        out = _report()
        assert "Data lag" not in out
        assert "latest DVOL reading sits at the" in out


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
        assert "**DVOL (30-day implied vol index), latest:** 36.26 on 2026-07-20" in out
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
        assert "is ahead of the UTC clock (2026-08-05)" in out
        assert "which is BEFORE the analysis date rather than after it" in out

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
        assert "still open, so this is the level so far" in _report(curr_date="2026-08-06")

    def test_dvol_failure_on_a_historical_date_raises(self):
        # Nothing left to report: the chain is withheld by design, not by failure.
        with pytest.raises(deribit.DeribitError, match="not served for the historical date"):
            _report(curr_date="2026-07-20", dvol=deribit.DeribitError("dvol down"))


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
        assert "**DVOL:** unavailable — No DVOL readings on or before 2026-08-05" in out
        assert "request failed" not in out

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
        assert "0 call / 8 put quotes" in section
        assert (
            "**25Δ call IV:** n/a (no quote on this side of the expiry yielded a usable delta)"
            in section
        )
        assert "**ATM IV (50Δ):** 31.12%" in section
        assert "**25Δ put IV:** 34.30%" in section
        # The bracket/monotonicity wording must not be reused for an empty side.
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

    def test_disabled_category_leaves_no_dangling_prompt_text(self):
        assert "get_options_market" not in str(_run_analyst("crypto").prompt_value)

    def test_market_toolnode_can_execute_the_options_tool(self):
        # _create_tool_nodes does not use self -> call unbound (avoids building LLMs).
        nodes = TradingAgentsGraph._create_tool_nodes(None)
        assert "get_options_market" in set(nodes["market"].tools_by_name), (
            "the options tool is bound to the market analyst for crypto assets but not "
            "registered in the market ToolNode, so the model's call fails."
        )
