"""Every routed getter that takes a date refuses an unusable one with the shared
sentinel — and WHICH getters must is derived from ``interface.VENDOR_METHODS``,
not hand-written (#140, item 8).

The three suites that used to pin the refusal (``test_unusable_date_parity``,
``test_optional_date_refusal``, ``test_yfinance_freshness``) each listed the
getters they drove by hand, so a getter registered after them with no gate
shipped green. PR #113 closed the same gap for the yfinance throttle taxonomy
with a call table whose membership must equal the registry
(``test_yfinance_rate_limit``); this is that lock for the date sentinel, and
since #230 the one refusal matrix — the siblings keep only what is not a sweep
over the table. It lives in the tests rather than in ``route_to_vendor`` as
#140 sketched because the gates cannot leave the getters (direct callers and
those suites reach them without the router, so a router copy would be a
second judgement to keep aligned), because the fundamentals lanes judge the
date AFTER the fetch by design (an absent symbol outranks it, #89) and a
pre-call gate would reorder that, and because a new tool needs a row wherever
the table lives — here a row costs no coupling, and the router keeps not
knowing where each getter's date sits.

The table itself, and the seams that drive it, are ``tests._date_refusal_table``.
"""

import inspect
import itertools

import pytest

from tests._date_refusal_table import (
    DATE_CALLS,
    GOOD,
    PERIOD,
    SUPPLIED_UNUSABLE,
    call,
    dated_params,
    no_network,
    not_refused,
    route,
    rows,
)
from tests.conftest import registry_pairs
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.utils import _DATE_ARGUMENT_TAGS, invalid_date_sentinel


@pytest.mark.unit
class TestTheTableIsTheRegistry:
    def test_every_registered_impl_has_a_row(self):
        assert set(DATE_CALLS) == registry_pairs(interface.VENDOR_METHODS)

    def test_the_served_period_is_within_the_usable_date(self):
        # If ``GOOD`` ever moves before the served period, the statement rows'
        # usable-date calls would filter the body to nothing and fail as a
        # getter regression rather than as this.
        assert PERIOD <= GOOD

    def test_the_lock_catches_an_unlisted_registry_entry(self, grown_registry):
        # Discrimination, per pair: see the fixture.
        assert set(DATE_CALLS) != registry_pairs(grown_registry)

    @pytest.mark.parametrize("key,row", rows(dated=True))
    def test_each_row_drives_the_registered_impl(self, key, row):
        # A row that drove some other function would pin nothing about the
        # impl the router actually calls.
        method, vendor = key
        assert row.impl is interface.VENDOR_METHODS[method][vendor]

    @pytest.mark.parametrize("key,row", rows(dated=False))
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
        for (method, _vendor), row in DATE_CALLS.items():
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
    @pytest.mark.parametrize("value", SUPPLIED_UNUSABLE)
    @pytest.mark.parametrize("key,row,param", dated_params())
    def test_a_supplied_unusable_date_is_the_whole_answer(self, monkeypatch, key, row, param, value):
        reached = no_network(monkeypatch)
        served = row.serve(monkeypatch) if row.judged_after_fetch else None
        # Whole-answer equality: nothing rides behind the refusal (for the
        # statement lanes, not the row the missing bound would have removed —
        # the sentence interpolates only the date), and the sentence names the
        # parameter that was unusable. Every vendor of one tool equals the
        # same string, so the vendors agree with each other by construction.
        assert call(row, **{param: value}) == invalid_date_sentinel(
            value, what=row.what, kind=row.kind, param=param
        )
        # The ordering the row claims, measured: no getter reached a raising
        # seam on any path, and an after-fetch one did reach its served one.
        assert not reached, key
        if row.judged_after_fetch:
            assert served, key

    @pytest.mark.parametrize("key,row", [p for p in rows(dated=True) if len(p.values[1].params) > 1])
    def test_with_every_date_unusable_the_first_judged_is_named(self, monkeypatch, key, row):
        # One sentence asks for one fix, and which one is the tool's to say,
        # not the vendor's: two vendors naming different parameters would be
        # the #89 divergence in a new coat. The params tuple records the
        # order; this is what holds the getter to it.
        no_network(monkeypatch)
        if row.judged_after_fetch:
            row.serve(monkeypatch)
        out = call(row, **dict.fromkeys(row.params, "abc"))
        assert out == invalid_date_sentinel("abc", what=row.what, kind=row.kind, param=row.params[0])

    @pytest.mark.parametrize("key,row,param", dated_params())
    def test_none_is_refused_unless_the_row_says_it_is_a_lane(self, monkeypatch, key, row, param):
        reached = no_network(monkeypatch)
        if row.omitted_ok:
            not_refused(monkeypatch, reached, row, **{param: None})
        else:
            assert call(row, **{param: None}) == invalid_date_sentinel(
                None, what=row.what, kind=row.kind, param=param
            )

    @pytest.mark.parametrize(
        "date",
        [
            GOOD,
            # strptime accepts "2026-6-5" and every getter goes on to normalise
            # it for its own comparisons; the refusal must not be stricter
            # than the parser behind it (#89 kept this too).
            pytest.param("2026-6-5", id="non_zero_padded"),
        ],
    )
    @pytest.mark.parametrize("key,row", rows(dated=True))
    def test_a_usable_date_is_not_refused(self, monkeypatch, key, row, date):
        # A gate that refused everything would pass the tests above.
        not_refused(monkeypatch, no_network(monkeypatch), row, **dict.fromkeys(row.params, date))


def _assert_routed_refusal(monkeypatch, method, row):
    """Through the router as configured, the row's first date unusable is the
    whole answer."""
    first = row.params[0]
    assert route(monkeypatch, method, row, **{first: "abc"}) == invalid_date_sentinel(
        "abc", what=row.what, kind=row.kind, param=first
    ), method


def _vendors_of(method):
    return [vendor for m, vendor in DATE_CALLS if m == method]


_DATED_METHODS = sorted({m for (m, _v), row in DATE_CALLS.items() if row})


@pytest.mark.unit
class TestEveryRowRefusesThroughTheRouter:
    """A refusal is returned, not raised, so ``route_to_vendor`` serves it as
    the tool's answer whatever the category's lane: for a core category a
    raise leaving the getter used to be ``raise first_error`` — a crash of the
    ToolNode-wrapped run — and for an optional one it is rendered as
    ``DATA_UNAVAILABLE: optional <category> could not be retrieved``, the
    "this source is down, proceed without it" verdict (#119)."""

    @pytest.mark.parametrize("key,row", rows(dated=True))
    def test_each_registered_vendor_serves_the_refusal(self, monkeypatch, key, row):
        # The vendor is selected through ``tool_vendors``, so the pair under
        # test is the one that answers rather than whichever the default
        # chain tries first — the Alpha Vantage vendors of the core tools,
        # and farside, are reached only this way.
        method, vendor = key
        set_config({"tool_vendors": {method: vendor}})
        _assert_routed_refusal(monkeypatch, method, row)

    @pytest.mark.parametrize(
        "method,chain",
        [
            pytest.param(m, chain, id=f"{m}/{','.join(chain)}")
            for m in _DATED_METHODS
            if len(_vendors_of(m)) > 1
            for chain in itertools.permutations(_vendors_of(m))
        ],
    )
    def test_a_chain_ends_at_the_first_vendors_refusal_whichever_is_first(
        self, monkeypatch, method, chain
    ):
        # A multi-vendor chain: the router must serve the first vendor's
        # refusal rather than move on to the next, and — since the model
        # cannot see which vendor answered (#89) — the sentence must not
        # depend on which one is configured first.
        set_config({"tool_vendors": {method: ",".join(chain)}})
        _assert_routed_refusal(monkeypatch, method, DATE_CALLS[(method, chain[0])])

    def test_the_shipped_default_chain_starts_at_a_registered_vendor(self):
        # With the configuration as shipped: no dated category is off, and
        # the vendor each chain tries first has a row — so the per-vendor
        # pins above cover the vendor that actually answers by default. A
        # default flipped to "none" would fail here while they stayed green.
        first_by_default = {
            (m, interface.get_vendor(interface.get_category_for_method(m), m).split(",")[0])
            for m in _DATED_METHODS
        }
        assert first_by_default <= set(DATE_CALLS)
