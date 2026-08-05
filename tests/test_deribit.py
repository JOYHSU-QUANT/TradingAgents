"""Deribit options vendor: instrument parsing, Black-76 delta, strike-adjacent wing
interpolation, expiry selection, DVOL windowing and sample-size honesty, per-half
degradation, historical-date chain suppression, report rendering, router
integration, and market-analyst wiring.

All network access is mocked and the maths runs against trimmed local fixtures
(captured from the live public API), so these run without a network connection.
"""

import datetime as dt
import json
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

    def test_expiry_settles_at_0800_utc(self):
        assert deribit.parse_expiry("5AUG26").hour == 8


# --------------------------------------------------------------------------- #
# Chain parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseChain:
    def test_fixture_parses_every_contract(self):
        contracts = deribit.parse_chain(CHAIN, NOW)
        assert len(contracts) == len(CHAIN)
        assert {c.expiry for c in contracts} == {"5AUG26", "28AUG26", "25DEC26"}

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
        rows = [
            {"instrument_name": "BTC-28AUG26-64000-C", "mark_iv": None, "underlying_price": 64000},
            {"instrument_name": "BTC-28AUG26-65000-C", "mark_iv": 0, "underlying_price": 64000},
            {"instrument_name": "BTC-28AUG26-66000-C", "mark_iv": 30, "underlying_price": 0},
            {"instrument_name": "BTC-28AUG26-67000-C", "mark_iv": True, "underlying_price": 64000},
            {"instrument_name": "BTC-PERPETUAL", "mark_iv": 30, "underlying_price": 64000},
            "not a dict",
            {"instrument_name": "BTC-28AUG26-68000-C", "mark_iv": 30, "underlying_price": 64000},
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
        # Only a 30-day target picks the 31d expiry out of this set: a 12d target
        # would take NEAR and a 55d target would take FAR.
        near = _synthetic_contract("NEAR", 12)
        mid = _synthetic_contract("MID", 31)
        far = _synthetic_contract("FAR", 55)
        assert deribit.select_expiry([near, mid, far], NOW) == "MID"

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

    def test_unusable_forward_raises(self):
        # compute_skew is public, so a caller can hand it contracts this module
        # would never have built.
        contracts = [_synthetic_contract("FAR", 30)._replace(underlying=-1.0)]
        with pytest.raises(deribit.DeribitError, match="no usable forward"):
            deribit.compute_skew(contracts, NOW)

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
        # would go on claiming a "30-day range" over whatever arrived.
        start = datetime.fromtimestamp(params["start_timestamp"] / 1000, tz=timezone.utc)
        assert start == datetime(2026, 6, 10, tzinfo=timezone.utc)  # 40 days back

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
            deribit._dvol_section(broken, datetime(2026, 8, 5), TODAY, True)


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
        out = _report()
        assert "**DVOL (30-day implied vol index), latest:** 34.43 on 2026-08-05" in out
        assert (
            "min 34.04 / max 39.53 — the latest reading sits at the 7th percentile of "
            "the 30 daily readings in that window" in out
        )

    def test_window_holds_exactly_dvol_window_days_of_closes(self):
        # An inclusive lower bound would put 31 closes in a window called "30d".
        assert deribit.DVOL_WINDOW_DAYS == 30
        out = _report(dvol=_dvol_days(60))
        assert "of the 30 daily readings in that window" in out

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
            "latest DVOL reading sits at the 7th percentile of the 30-day window ending on "
            "the analysis date." in out
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
        line = deribit._reading_line("BTC", skew, 55.0)
        assert "25Δ calls are priced 2.00 vol points above 25Δ puts (RR25 +2.00" in line

    def test_reading_line_handles_a_flat_skew(self):
        skew = deribit.compute_skew(deribit.parse_chain(CHAIN, NOW), NOW)._replace(
            call_25=deribit.WingQuote(34.0, 67000.0, 68000.0),
            put_25=deribit.WingQuote(34.0, 61000.0, 62000.0),
        )
        line = deribit._reading_line("BTC", skew, None)
        assert "both 25Δ wings carry the same implied vol (RR25 +0.00)" in line
        assert "percentile" not in line

    def test_reading_line_is_empty_with_nothing_to_state(self):
        assert deribit._reading_line("BTC", None, None) == ""

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
        # The latest close is itself in the sample, so a percentile over 5 closes
        # could not read below 20% — a number that would describe the sample size,
        # not the vol regime.
        out = _report(dvol=_dvol_days(5))
        assert "too few for a percentile" in out
        assert "percentile of the" not in out
        # ... and the reading line drops the DVOL clause rather than stating it.
        assert "DVOL close sits at" not in out

    def test_a_series_entirely_outside_the_window_reports_no_range(self):
        # A stalled or backfilled feed: without this the range collapses onto the
        # single fallback observation and its percentile is always exactly 100,
        # which used to render as "DVOL is near the top of its 30-day range".
        out = _report(dvol={"data": [_candle("2026-05-01", 58.0)]})
        assert "not computed — no DVOL reading falls inside the 30 days ending 2026-08-05" in out
        assert "**DVOL (30-day implied vol index), latest:** 58.00 on 2026-05-01" in out
        assert "percentile" not in out
        assert "Data lag" in out

    def test_minimum_sample_is_ten_closes(self):
        # Literal, not derived from the constant: a test whose input is computed
        # from the value it means to pin can never see that value move.
        assert deribit._MIN_PERCENTILE_SAMPLE == 10
        assert "too few for a percentile" in _report(dvol=_dvol_days(9))
        assert "th percentile of the 10 daily readings" in _report(dvol=_dvol_days(10))
        assert "th percentile of the 15 daily readings" in _report(dvol=_dvol_days(15))

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

    def test_today_serves_the_chain(self):
        out, recorder = _run_report()
        assert recorder.endpoints() == {DVOL_ENDPOINT, CHAIN_ENDPOINT}
        assert "Historical date:" not in out

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
