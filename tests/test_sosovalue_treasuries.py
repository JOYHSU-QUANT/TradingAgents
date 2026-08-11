"""SoSoValue BTC corporate-treasury vendor: numeric-string parsing, listing
and purchase-history parsing (live-captured fixtures, including the
holdings-only disclosure shape), the top-N selection cap, the all-failed
vendor-failure rule, rolling-snapshot caching with stale fallback and
read-side validation, lookahead-safe rendering with implied-change and
history-depth disclosures, asset classification (ETH is a proxy here), and
router integration.

All network access is mocked and the parsers run against fixtures captured
from the real API, so these run without a network connection or a key.
"""

import json
import os
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import quote

import pytest
import requests

from tradingagents.dataflows import interface, sosovalue_common, sosovalue_treasuries
from tradingagents.dataflows.config import set_config

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture_json(name: str) -> dict:
    with open(os.path.join(FIXTURE_DIR, name), encoding="utf-8") as f:
        return json.load(f)


# Live-captured API responses (see the module docstring's live-verified facts).
LIST_FIX = _fixture_json("sosovalue_treasuries_list.json")
MSTR_FIX = _fixture_json("sosovalue_treasuries_history_mstr.json")
MARA_FIX = _fixture_json("sosovalue_treasuries_history_mara.json")

LIST_TICKERS = [r["ticker"] for r in LIST_FIX["data"]]


def _at(stamp: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%SZ" if "T" in stamp else "%Y-%m-%d"
    return datetime.strptime(stamp, fmt).replace(tzinfo=timezone.utc)


def _prow(date: str, holding: float, acq=None, cost=None) -> dict:
    return {"date": date, "btc_holding": holding, "btc_acq": acq, "acq_cost": cost}


def _snapshot(
    companies=None,
    companies_total=None,
    companies_failed=(),
    companies_unusable=0,
    order_unverified=False,
    fetched_at="2026-08-11T00:00:00Z",
    stale=False,
):
    if companies is None:
        companies = {
            "MSTR": {
                "name": "Strategy",
                "rows": [
                    # An old row outside any test window keeps the
                    # history-depth caveat quiet in the baseline snapshot.
                    _prow("2025-02-01", 471107.0, 20356.0, 1990000000.0),
                    _prow("2026-07-05", 843775.0, -2225.0, -135200000.0),
                    _prow("2026-08-10", 840447.0, -1690.0, -108600000.0),
                ],
            },
            "MARA": {
                "name": "MARA Holdings",
                "rows": [
                    _prow("2025-09-30", 52850.0, 736.0),
                    _prow("2026-03-31", 35303.0),  # holdings-only disclosure
                ],
            },
        }
    return sosovalue_treasuries._TreasurySnapshot(
        companies=companies,
        companies_total=len(companies) if companies_total is None else companies_total,
        companies_failed=list(companies_failed),
        companies_unusable=companies_unusable,
        order_unverified=order_unverified,
        fetched_at=fetched_at,
        stale=stale,
    )


def _render(snapshot, asset="BTC", curr_date="2026-08-11", look_back_days=None):
    with mock.patch.object(sosovalue_treasuries, "_load_snapshot", return_value=snapshot):
        return sosovalue_treasuries.get_btc_treasury_data(asset, curr_date, look_back_days)


# --------------------------------------------------------------------------- #
# numeric-string parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseAmount:
    def test_digit_strings_and_bare_numbers_parse(self):
        assert sosovalue_treasuries._parse_amount("840447") == 840447.0
        assert sosovalue_treasuries._parse_amount("-1690") == -1690.0
        assert sosovalue_treasuries._parse_amount("0.5") == 0.5
        assert sosovalue_treasuries._parse_amount(5) == 5.0
        assert sosovalue_treasuries._parse_amount(-3.25) == -3.25

    def test_everything_else_is_none(self):
        assert sosovalue_treasuries._parse_amount(True) is None
        assert sosovalue_treasuries._parse_amount(float("nan")) is None
        assert sosovalue_treasuries._parse_amount("1,234") is None
        assert sosovalue_treasuries._parse_amount("abc") is None
        assert sosovalue_treasuries._parse_amount("") is None
        assert sosovalue_treasuries._parse_amount(None) is None

    def test_oversized_magnitudes_never_become_inf_or_raise(self):
        # A 320-digit string would float() to inf (poisoning sums, then
        # perma-rejected read-side); a bare int that large makes
        # math.isfinite raise OverflowError. Both must resolve to None/False.
        assert sosovalue_treasuries._parse_amount("9" * 320) is None
        assert sosovalue_treasuries._parse_amount(10**400) is None
        assert sosovalue_common._is_finite_number(10**400) is False


# --------------------------------------------------------------------------- #
# listing parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParseCompanyList:
    def test_live_fixture_preserves_holdings_order_and_intl_tickers(self):
        companies, unusable = sosovalue_treasuries._parse_company_list(LIST_FIX["data"])
        assert unusable == 0
        assert companies[0] == ("MSTR", "Strategy")
        tickers = [t for t, _ in companies]
        assert tickers == LIST_TICKERS  # order untouched: it IS the ranking
        assert "3350" in tickers and "0434.HK" in tickers

    def test_unusable_tickers_are_dropped_and_counted(self, caplog):
        data = [{"ticker": "..", "name": "evil"}, {"name": "no ticker"}] + LIST_FIX["data"]
        with caplog.at_level("WARNING"):
            companies, unusable = sosovalue_treasuries._parse_company_list(data)
        assert unusable == 2
        assert [t for t, _ in companies] == LIST_TICKERS

    def test_duplicate_ticker_keeps_the_first_entry(self):
        data = LIST_FIX["data"] + [{"ticker": "MSTR", "name": "Impostor"}]
        companies, _ = sosovalue_treasuries._parse_company_list(data)
        assert dict(companies)["MSTR"] == "Strategy"

    def test_unrenderable_names_fall_back_to_empty(self):
        data = [
            {"ticker": "AAA", "name": "x" * 61},
            {"ticker": "BBB", "name": "bad\x01name"},
            {"ticker": "CCC", "name": 42},
        ]
        companies, unusable = sosovalue_treasuries._parse_company_list(data)
        assert companies == [("AAA", ""), ("BBB", ""), ("CCC", "")]
        assert unusable == 0  # the entry survives; only the name is dropped


# --------------------------------------------------------------------------- #
# purchase-history parsing
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestParsePurchaseRows:
    def test_mstr_fixture_normalizes_negative_strings_ascending(self):
        rows = sosovalue_treasuries._parse_purchase_rows(MSTR_FIX["data"], "MSTR")
        assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)
        newest = rows[-1]
        assert newest["btc_acq"] == -1690.0  # "-1690" normalized to a float
        assert newest["acq_cost"] == -108600000.0

    def test_mara_fixture_maps_missing_fields_to_none(self):
        rows = sosovalue_treasuries._parse_purchase_rows(MARA_FIX["data"], "MARA")
        newest = rows[-1]  # the live holdings-only disclosure
        assert newest["date"] == "2026-03-31"
        assert newest["btc_holding"] == 35303.0
        assert newest["btc_acq"] is None
        assert newest["acq_cost"] is None

    def test_empty_history_raises(self):
        with pytest.raises(sosovalue_common.SoSoValueError, match="no treasury history"):
            sosovalue_treasuries._parse_purchase_rows([], "MSTR")

    def test_missing_btc_holding_raises(self):
        with pytest.raises(sosovalue_common.SoSoValueError, match="btc_holding"):
            sosovalue_treasuries._parse_purchase_rows([{"date": "2026-08-10", "ticker": "X"}], "X")

    def test_present_but_unreadable_optional_field_raises(self):
        # Absence is a legal disclosure shape; a filed figure this module
        # cannot read is a contract break.
        with pytest.raises(sosovalue_common.SoSoValueError, match="unreadable btc_acq"):
            sosovalue_treasuries._parse_purchase_rows(
                [{"date": "2026-08-10", "btc_holding": "10", "btc_acq": "1,690"}], "X"
            )

    def test_duplicate_dates_are_both_kept(self):
        rows = sosovalue_treasuries._parse_purchase_rows(
            [
                {"date": "2026-08-10", "btc_holding": "10", "btc_acq": "5"},
                {"date": "2026-08-10", "btc_holding": "15", "btc_acq": "5"},
            ],
            "X",
        )
        assert len(rows) == 2  # two same-day filings are two disclosures


# --------------------------------------------------------------------------- #
# _fetch_all: selection cap and vendor-success rule
# --------------------------------------------------------------------------- #
def _request_impl(listing=None, history_by_ticker=None, history_error=None, error_tickers=()):
    calls = []

    def impl(path, params):
        calls.append(path)
        if path == "/btc-treasuries":
            return LIST_FIX["data"] if listing is None else listing
        for row in LIST_FIX["data"] if listing is None else listing:
            ticker = row.get("ticker")
            if ticker and path == f"/btc-treasuries/{quote(ticker, safe='')}/purchase-history":
                if ticker in error_tickers:
                    raise history_error
                if history_by_ticker and ticker in history_by_ticker:
                    return history_by_ticker[ticker]
                return MSTR_FIX["data"]
        raise AssertionError(f"unexpected path {path}")

    impl.calls = calls
    return impl


@pytest.mark.unit
class TestFetchAll:
    def test_full_success_fetches_every_listed_company_under_the_cap(self, monkeypatch):
        monkeypatch.setattr(sosovalue_treasuries, "_request", _request_impl())
        payload = sosovalue_treasuries._fetch_all()
        assert set(payload["companies"]) == set(LIST_TICKERS)  # 12 <= cap
        assert payload["companies_total"] == len(LIST_TICKERS)
        assert payload["companies_failed"] == []
        # Every company serves the same fixture, so equal holdings verify as
        # non-increasing and the ordering claim stands.
        assert payload["order_unverified"] is False

    def test_selection_is_capped_at_the_top_of_the_listing(self, monkeypatch):
        listing = [{"ticker": f"T{i:02d}", "name": f"Company {i}"} for i in range(20)]
        impl = _request_impl(listing=listing)
        monkeypatch.setattr(sosovalue_treasuries, "_request", impl)
        payload = sosovalue_treasuries._fetch_all()
        cap = sosovalue_treasuries.MAX_COMPANIES
        assert list(payload["companies"]) == [f"T{i:02d}" for i in range(cap)]
        assert payload["companies_total"] == 20
        assert len(impl.calls) == 1 + cap

    def test_one_failed_history_is_disclosed_not_fatal(self, monkeypatch):
        monkeypatch.setattr(
            sosovalue_treasuries,
            "_request",
            _request_impl(history_by_ticker={"MARA": []}),
        )
        payload = sosovalue_treasuries._fetch_all()
        assert payload["companies_failed"] == ["MARA"]
        assert "MARA" not in payload["companies"]

    def test_a_first_request_429_keeps_its_rate_limit_type(self, monkeypatch):
        # A quota trip that drains the whole sweep must stay a rate-limit
        # error: the router and the stale-fallback classify by type, and a
        # 429 must not masquerade as structural breakage.
        impl = _request_impl(
            history_error=sosovalue_common.SoSoValueRateLimitError("429"),
            error_tickers=set(LIST_TICKERS),
        )
        monkeypatch.setattr(sosovalue_treasuries, "_request", impl)
        with pytest.raises(
            sosovalue_common.SoSoValueRateLimitError, match="rate limited before any"
        ):
            sosovalue_treasuries._fetch_all()
        # The first 429 drains the sweep: no further quota-burning requests.
        assert len([c for c in impl.calls if c != "/btc-treasuries"]) == 1

    def test_a_rate_limit_mid_sweep_drains_the_rest(self, monkeypatch):
        impl = _request_impl(
            history_error=sosovalue_common.SoSoValueRateLimitError("429"),
            error_tickers={LIST_TICKERS[2]},
        )
        monkeypatch.setattr(sosovalue_treasuries, "_request", impl)
        payload = sosovalue_treasuries._fetch_all()
        assert set(payload["companies"]) == set(LIST_TICKERS[:2])
        assert set(payload["companies_failed"]) == set(LIST_TICKERS[2:])
        assert len([c for c in impl.calls if c != "/btc-treasuries"]) == 3

    def test_holdings_order_violation_is_flagged_not_asserted(self, monkeypatch):
        listing = [{"ticker": "AAA", "name": "Small"}, {"ticker": "BBB", "name": "Big"}]
        histories = {
            "AAA": [{"date": "2026-08-10", "btc_holding": "10", "btc_acq": "1"}],
            "BBB": [{"date": "2026-08-10", "btc_holding": "100", "btc_acq": "1"}],
        }
        monkeypatch.setattr(
            sosovalue_treasuries,
            "_request",
            _request_impl(listing=listing, history_by_ticker=histories),
        )
        payload = sosovalue_treasuries._fetch_all()
        assert payload["order_unverified"] is True

    def test_network_streak_trips_the_breaker_then_fails_the_vendor(self, monkeypatch):
        impl = _request_impl(
            history_error=requests.ConnectionError("down"),
            error_tickers=set(LIST_TICKERS),
        )
        monkeypatch.setattr(sosovalue_treasuries, "_request", impl)
        with pytest.raises(sosovalue_common.SoSoValueError, match="all 12 selected"):
            sosovalue_treasuries._fetch_all()
        history_calls = [c for c in impl.calls if c != "/btc-treasuries"]
        assert len(history_calls) == sosovalue_treasuries.MAX_CONSECUTIVE_NETWORK_FAILURES

    def test_rejected_key_mid_loop_propagates(self, monkeypatch):
        monkeypatch.setattr(
            sosovalue_treasuries,
            "_request",
            _request_impl(
                history_error=sosovalue_common.SoSoValueNotConfiguredError("401"),
                error_tickers={"MSTR"},
            ),
        )
        with pytest.raises(sosovalue_common.SoSoValueNotConfiguredError):
            sosovalue_treasuries._fetch_all()

    def test_unusable_only_listing_fails_the_vendor(self, monkeypatch):
        monkeypatch.setattr(
            sosovalue_treasuries,
            "_request",
            _request_impl(listing=[{"ticker": "..", "name": "evil"}]),
        )
        with pytest.raises(sosovalue_common.SoSoValueError, match="no usable companies"):
            sosovalue_treasuries._fetch_all()


# --------------------------------------------------------------------------- #
# caching, TTLs, stale fallback, read-side validation
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestCacheAndLoad:
    def _setup(self, tmp_path, monkeypatch, now="2026-08-11T12:00:00Z"):
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.setenv("SOSOVALUE_API_KEY", "test-key")
        monkeypatch.setattr(sosovalue_common, "_utc_now", lambda: _at(now))
        impl = _request_impl()
        monkeypatch.setattr(sosovalue_treasuries, "_request", impl)
        return impl

    def _write_cache(self, tmp_path, **overrides):
        payload = {
            "companies": {
                "MSTR": {
                    "name": "Strategy",
                    "rows": [_prow("2026-08-10", 840447.0, -1690.0, -108600000.0)],
                }
            },
            "companies_total": 57,
            "companies_failed": [],
            "companies_unusable": 0,
            "order_unverified": False,
            "fetched_at": "2026-08-11T00:00:00Z",
        }
        payload.update(overrides)
        (tmp_path / "sosovalue_treasuries.json").write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def test_within_the_day_ttl_reuses_cache(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch)  # cache is 12h old vs 24h TTL
        self._write_cache(tmp_path)
        snapshot = sosovalue_treasuries._load_snapshot()
        assert snapshot.stale is False
        assert impl.calls == []

    def test_incomplete_snapshot_uses_the_short_ttl(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-11T02:00:00Z")
        # 2h old: fresh under 24h, expired under the 1h incomplete TTL.
        self._write_cache(tmp_path, companies_failed=["MARA"])
        snapshot = sosovalue_treasuries._load_snapshot()
        assert impl.calls
        assert snapshot.companies_failed == []

    def test_past_ttl_refetches(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-12T01:00:00Z")  # 25h
        self._write_cache(tmp_path)
        sosovalue_treasuries._load_snapshot()
        assert "/btc-treasuries" in impl.calls

    def test_unset_key_raises_even_with_a_fresh_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path)
        monkeypatch.delenv("SOSOVALUE_API_KEY")
        with pytest.raises(sosovalue_common.SoSoValueNotConfiguredError):
            sosovalue_treasuries._load_snapshot()

    def test_failure_without_cache_raises_and_writes_nothing(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch)

        def broken(path, params):
            raise requests.ConnectionError("down")

        monkeypatch.setattr(sosovalue_treasuries, "_request", broken)
        with pytest.raises(sosovalue_common.SoSoValueError, match="no usable cache"):
            sosovalue_treasuries._load_snapshot()
        assert not (tmp_path / "sosovalue_treasuries.json").exists()

    def test_failure_falls_back_to_stale_cache(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-13T00:00:00Z")  # 2 days

        def broken(path, params):
            raise requests.ConnectionError("down")

        self._write_cache(tmp_path)
        monkeypatch.setattr(sosovalue_treasuries, "_request", broken)
        snapshot = sosovalue_treasuries._load_snapshot()
        assert snapshot.stale is True

    def test_stale_cache_past_cap_degrades(self, tmp_path, monkeypatch):
        self._setup(tmp_path, monkeypatch, now="2026-08-26T01:00:00Z")  # 15 days

        def broken(path, params):
            raise requests.ConnectionError("down")

        self._write_cache(tmp_path)
        monkeypatch.setattr(sosovalue_treasuries, "_request", broken)
        with pytest.raises(sosovalue_common.SoSoValueError, match="days stale"):
            sosovalue_treasuries._load_snapshot()

    def test_legitimate_degraded_payload_shapes_are_accepted(self, tmp_path, monkeypatch):
        # The validator must accept everything _fetch_all can write: same-day
        # double filings, holdings-only rows (the live MARA shape, both
        # optional fields None), and a non-empty failed bucket — served from
        # cache within even the short TTL, with zero requests. A tightening
        # that rejects any of these turns the TTL throttle silently off.
        impl = self._setup(tmp_path, monkeypatch, now="2026-08-11T00:30:00Z")
        self._write_cache(
            tmp_path,
            companies={
                "MSTR": {
                    "name": "Strategy",
                    "rows": [
                        _prow("2026-08-09", 100.0, 5.0, -300000.0),
                        _prow("2026-08-09", 105.0, 5.0),
                        _prow("2026-08-10", 90.0),
                    ],
                }
            },
            companies_failed=["MARA"],
        )
        snapshot = sosovalue_treasuries._load_snapshot()
        assert impl.calls == []
        assert snapshot.companies_failed == ["MARA"]
        assert len(snapshot.companies["MSTR"]["rows"]) == 3

    def test_a_row_missing_an_optional_key_rejects_the_cache(self, tmp_path, monkeypatch):
        # Key ABSENT is not the legal null: the renderer subscripts both
        # optional keys, so the validator must reject the file rather than
        # let a KeyError escape the vendor taxonomy.
        impl = self._setup(tmp_path, monkeypatch)
        self._write_cache(
            tmp_path,
            companies={
                "MSTR": {
                    "name": "Strategy",
                    "rows": [{"date": "2026-08-10", "btc_holding": 840447.0}],
                }
            },
        )
        sosovalue_treasuries._load_snapshot()
        assert impl.calls  # rejected, refetched

    def test_cache_write_failure_is_non_fatal(self, tmp_path, monkeypatch, caplog):
        self._setup(tmp_path, monkeypatch)
        with (
            mock.patch.object(sosovalue_treasuries.json, "dump", side_effect=OSError("disk full")),
            caplog.at_level("WARNING"),
        ):
            snapshot = sosovalue_treasuries._load_snapshot()
        assert snapshot.stale is False
        assert "Could not write SoSoValue treasuries cache" in caplog.text

    def test_unrenderable_cached_name_rejects_the_cache(self, tmp_path, monkeypatch):
        # The cache is a lower trust tier than the API: a name that the live
        # parser would have dropped must not reach the report via the file.
        impl = self._setup(tmp_path, monkeypatch)
        self._write_cache(
            tmp_path,
            companies={
                "MSTR": {
                    "name": "bad\x01name",
                    "rows": [_prow("2026-08-10", 840447.0, -1690.0, -108600000.0)],
                }
            },
        )
        sosovalue_treasuries._load_snapshot()
        assert impl.calls  # rejected, refetched

    def test_oversized_selection_rejects_the_cache(self, tmp_path, monkeypatch, caplog):
        impl = self._setup(tmp_path, monkeypatch)
        companies = {
            f"T{i:02d}": {"name": "", "rows": [_prow("2026-08-10", 1.0)]}
            for i in range(sosovalue_treasuries.MAX_COMPANIES + 1)
        }
        self._write_cache(tmp_path, companies=companies, companies_total=57)
        with caplog.at_level("WARNING"):
            sosovalue_treasuries._load_snapshot()
        assert impl.calls
        assert "exceeds MAX_COMPANIES" in caplog.text

    def test_failed_overlap_or_short_total_rejects_the_cache(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch)
        self._write_cache(tmp_path, companies_failed=["MSTR"])  # overlaps companies
        sosovalue_treasuries._load_snapshot()
        assert impl.calls
        impl.calls.clear()
        self._write_cache(tmp_path, companies_total=0)  # smaller than selection
        sosovalue_treasuries._load_snapshot()
        assert impl.calls

    def test_malformed_row_rejects_the_cache(self, tmp_path, monkeypatch):
        impl = self._setup(tmp_path, monkeypatch)
        self._write_cache(
            tmp_path,
            companies={
                "MSTR": {
                    "name": "Strategy",
                    "rows": [{"date": "2026-08-10", "btc_holding": "840447"}],
                }
            },
        )  # a string amount in the cache: only floats are written
        sosovalue_treasuries._load_snapshot()
        assert impl.calls


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRender:
    def test_holdings_section_sums_and_ranks_visible_companies(self):
        report = _render(_snapshot())
        assert "875,750 BTC across 2 tracked companies" in report
        assert "MSTR (Strategy) 840,447 BTC (as of 2026-08-10)" in report
        assert "largest holder MSTR = 96%" in report

    def test_activity_table_shows_disposals_with_implied_price(self):
        report = _render(_snapshot())
        assert "| 2026-08-10 | MSTR | -1,690 | -108.6 | 64,260 |" in report
        assert "-3,915 BTC (0 companies adding, 1 reducing, of 2 tracked)" in report

    def test_a_holdings_derived_delta_never_gets_an_implied_price(self):
        # acq_cost filed but btc_acq missing: the delta spans every
        # transaction since the prior disclosure, and one filing's cost
        # divided by it would be a price no transaction traded at.
        snapshot = _snapshot(
            companies={
                "X": {
                    "name": "",
                    "rows": [
                        _prow("2026-05-20", 100.0, 10.0),
                        _prow("2026-06-15", 80.0, None, -500000.0),
                    ],
                }
            }
        )
        report = _render(snapshot)
        assert (
            "| 2026-06-15 | X | -20 (from holdings change since 2026-05-20) | -0.5 | — |"
            in report
        )

    def test_a_sub_coin_disposal_keeps_its_sign(self):
        snapshot = _snapshot(
            companies={
                "X": {
                    "name": "",
                    # The buy sits outside the 90-day window, so the window's
                    # only event — and the company's net — is the disposal.
                    "rows": [
                        _prow("2026-01-20", 100.0, 10.0),
                        _prow("2026-06-15", 99.6, -0.4),
                    ],
                }
            }
        )
        report = _render(snapshot)
        # "-0.40", never "+0": the row must agree with the reducers tally
        # computed from the unrounded deltas.
        assert "| 2026-06-15 | X | -0.40 |" in report
        assert "1 reducing" in report

    def test_unverified_ordering_downgrades_the_coverage_claim(self):
        report = _render(_snapshot(order_unverified=True))
        assert "provider ordering unverified" in report
        assert "largest holders first" not in report
        assert "largest holders first" in _render(_snapshot())

    def test_holdings_only_disclosure_renders_the_implied_change(self):
        snapshot = _snapshot(
            companies={
                "X": {
                    "name": "",
                    "rows": [
                        _prow("2026-05-20", 100.0, 10.0),
                        _prow("2026-06-15", 80.0),  # holdings-only
                    ],
                }
            }
        )
        report = _render(snapshot)
        assert (
            "| 2026-06-15 | X | -20 (from holdings change since 2026-05-20) | — | — |"
            in report
        )

    def test_first_row_holdings_only_is_disclosed_not_guessed(self):
        snapshot = _snapshot(companies={"Y": {"name": "", "rows": [_prow("2026-06-01", 50.0)]}})
        report = _render(snapshot)
        assert "1 holdings-only disclosure in the window is not in the table" in report
        assert "| 2026-06-01 |" not in report

    def test_lookahead_drops_future_disclosures(self):
        report = _render(_snapshot(), curr_date="2026-07-31")
        assert "2026-08-10" not in report
        assert "843,775" in report  # MSTR as of 2026-07-05 instead

    def test_all_future_rows_yield_the_no_rows_message(self):
        snapshot = _snapshot(
            companies={"X": {"name": "", "rows": [_prow("2026-08-10", 10.0, 1.0)]}}
        )
        report = _render(snapshot, curr_date="2026-08-01")
        assert "No treasury disclosures on or before 2026-08-01" in report
        assert "do not fabricate" in report

    def test_shallow_history_inside_the_window_is_disclosed(self):
        snapshot = _snapshot(
            companies={
                "X": {
                    "name": "",
                    "rows": [
                        _prow("2026-07-01", 100.0, 100.0),
                        _prow("2026-08-01", 120.0, 20.0),
                    ],
                }
            }
        )
        report = _render(snapshot)  # window start 2026-05-13 < 2026-07-01
        assert "served history for X starts inside the window" in report

    def test_empty_window_names_the_latest_disclosure(self):
        snapshot = _snapshot(
            companies={"X": {"name": "", "rows": [_prow("2026-01-10", 100.0, 5.0)]}}
        )
        report = _render(snapshot, look_back_days=30)
        assert "no disclosed holdings changes in the window" in report
        assert "latest disclosure across tracked companies: 2026-01-10" in report

    def test_eth_and_sol_get_the_proxy_labelling(self):
        for asset in ("ETH", "SOL"):
            report = _render(_snapshot(), asset=asset)
            assert f"market-wide demand proxy for '{asset}'" in report
            assert "840,447" in report  # still the BTC data

    def test_pair_form_btc_is_native(self):
        report = _render(_snapshot(), asset="BTC-USD")
        assert "proxy" not in report.split("\n")[0]

    def test_stablecoin_and_unknown_get_the_no_signal_note(self):
        for asset in ("USDT", "NOTREAL"):
            with mock.patch.object(sosovalue_treasuries, "_load_snapshot") as loader:
                report = sosovalue_treasuries.get_btc_treasury_data(asset, "2026-08-11")
            loader.assert_not_called()
            assert "no corporate BTC-treasury signal" in report
            assert "Do not substitute" in report

    def test_incomplete_coverage_and_unusable_entries_are_disclosed(self):
        snapshot = _snapshot(
            companies_total=57, companies_failed=["HUT", "RIOT"], companies_unusable=1
        )
        report = _render(snapshot)
        assert "Coverage incomplete (2/4 selected companies)" in report
        assert "HUT, RIOT" in report
        assert "1 listing entry had no usable ticker" in report
        assert "top 4 of 57 listed companies" in report

    def test_stale_snapshot_is_disclosed(self):
        report = _render(_snapshot(fetched_at="2026-08-09T00:00:00Z", stale=True))
        assert "STALE by" in report

    def test_fixed_caveats_are_always_present(self):
        report = _render(_snapshot())
        assert "disclosure dates" in report
        assert "announcement-driven and lumpy" in report

    def test_activity_table_is_capped_with_a_note(self):
        rows = [
            _prow(f"2026-{m:02d}-{d:02d}", 1000.0 + d, 1.0) for m in (6, 7) for d in range(1, 27)
        ]
        snapshot = _snapshot(companies={"X": {"name": "", "rows": rows}})
        report = _render(snapshot, curr_date="2026-08-01", look_back_days=90)
        assert f"most recent {sosovalue_treasuries.MAX_ROWS} of" in report

    def test_non_padded_curr_date_is_normalized(self):
        assert _render(_snapshot(), curr_date="2026-8-11") == _render(
            _snapshot(), curr_date="2026-08-11"
        )


# --------------------------------------------------------------------------- #
# router integration
# --------------------------------------------------------------------------- #
@pytest.mark.unit
class TestRouterIntegration:
    def _with_vendor(self, vendor):
        set_config({"data_vendors": {"btc_treasuries": vendor}})

    def teardown_method(self):
        set_config({"data_vendors": {"btc_treasuries": "none"}})

    def test_routes_to_the_sosovalue_report(self):
        self._with_vendor("sosovalue")
        with mock.patch.object(sosovalue_treasuries, "_load_snapshot", return_value=_snapshot()):
            report = interface.route_to_vendor("get_btc_treasuries", "BTC", "2026-08-11", None)
        assert "BTC Corporate Treasuries" in report

    def test_vendor_failure_degrades_to_the_sentinel(self):
        self._with_vendor("sosovalue")
        with mock.patch.object(
            sosovalue_treasuries,
            "_load_snapshot",
            side_effect=sosovalue_common.SoSoValueError("down"),
        ):
            report = interface.route_to_vendor("get_btc_treasuries", "BTC", "2026-08-11", None)
        assert report.startswith("DATA_UNAVAILABLE")

    def test_unset_key_degrades_to_the_sentinel(self, monkeypatch, tmp_path):
        self._with_vendor("sosovalue")
        set_config({"data_cache_dir": str(tmp_path)})
        monkeypatch.delenv("SOSOVALUE_API_KEY", raising=False)
        report = interface.route_to_vendor("get_btc_treasuries", "BTC", "2026-08-11", None)
        assert report.startswith("DATA_UNAVAILABLE")

    def test_none_vendor_is_the_disabled_sentinel(self):
        self._with_vendor("none")
        report = interface.route_to_vendor("get_btc_treasuries", "BTC", "2026-08-11", None)
        assert "disabled by configuration" in report
