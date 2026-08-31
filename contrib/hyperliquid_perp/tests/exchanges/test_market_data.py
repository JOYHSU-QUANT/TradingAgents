"""Tests for market_data helpers that need no network (meta, candle window)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from contrib.hyperliquid_perp.domains.perp.margin import MarginSchedule, MarginTier
from contrib.hyperliquid_perp.domains.perp.schema import interval_to_ms
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import MalformedResponseError
from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import HyperliquidMarketData
from contrib.hyperliquid_perp.ports import ExchangeMarketData

from ..conftest import synthetic_bar as _settled_bar

# A window end past ``synthetic_bar``'s ``T`` (1999 ms), for the identity tests
# that only care that the one bar survives the cut.
_AFTER_THE_SETTLED_BAR = datetime.fromtimestamp(2, tz=timezone.utc)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _ms(moment):
    return (moment - _EPOCH) // timedelta(milliseconds=1)


class _MetaOnlyInfo:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def meta_and_asset_ctxs(self):
        self.calls += 1
        return self._payload


class _FakeClient:
    def __init__(self, payload):
        self.info = _MetaOnlyInfo(payload)


def test_get_asset_meta_pair_from_one_meta_request(meta_and_asset_ctxs):
    # AssetSpec's two exchange-derived inputs come from the SAME meta response —
    # one request, so the (szDecimals, schedule) pair can never be internally
    # inconsistent.
    client = _FakeClient(meta_and_asset_ctxs)
    market = HyperliquidMarketData(client)
    sz_decimals, schedule = market.get_asset_meta("BTC")
    assert client.info.calls == 1
    assert sz_decimals == 5  # BTC's szDecimals, not ETH's 4
    # No tier table in the fixture -> single tier from BTC's maxLeverage.
    assert schedule == MarginSchedule(coin="BTC", tiers=(MarginTier(Decimal(0), Decimal(50)),))


def test_market_data_satisfies_exchange_market_data_port():
    # Method-presence check only (``runtime_checkable``): a rename/removal on
    # either side of the port fails here rather than in a paper run.
    assert issubclass(HyperliquidMarketData, ExchangeMarketData)


# ---------------------------------------------------------------------------
# identity echo wiring (2026-08-17): the client must ASK for the check
# ---------------------------------------------------------------------------
#
# map_candles / map_funding_history only verify identity when the caller passes
# the expected values (None skips, for identity-agnostic fixtures). These tests
# pin that the production reader actually passes them — the check being optional
# at the mapper makes THIS wiring the load-bearing part.


class _CandleInfo:
    def __init__(self, bars=None, funding=None):
        self._bars = bars or []
        self._funding = funding or []

    def candles_snapshot(self, coin, interval, start, end):
        return self._bars

    def funding_history(self, coin, start, end):
        return self._funding


def test_get_candles_rejects_a_misrouted_response():
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar(s="ETH")])
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_candles("BTC", "1m", 1, end=_AFTER_THE_SETTLED_BAR)


def test_get_candles_accepts_the_matching_identity():
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar()])
    candles = HyperliquidMarketData(client).get_candles("BTC", "1m", 1, end=_AFTER_THE_SETTLED_BAR)
    assert len(candles) == 1


def test_get_funding_history_rejects_a_misrouted_response():
    client = _FakeClient(None)
    client.info = _CandleInfo(
        funding=[{"coin": "ETH", "time": 1000, "fundingRate": "0.0001", "premium": "0"}]
    )
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_funding_history("BTC", 7, end=_AFTER_THE_SETTLED_BAR)


def test_get_funding_history_accepts_the_matching_identity():
    # The positive companion the reject test cannot stand in for: its ``match=``
    # names the ECHOED value, so it keeps raising even when the call site passes
    # a mangled expectation. Mutating expected_coin to ``coin + "-PERP"`` --
    # which would blow up every real funding read -- left 76 tests green without
    # this one (2026-08-17 exit-sweep mutation probe).
    client = _FakeClient(None)
    client.info = _CandleInfo(
        funding=[{"coin": "BTC", "time": 1000, "fundingRate": "0.0001", "premium": "0"}]
    )
    assert (
        len(HyperliquidMarketData(client).get_funding_history("BTC", 7, end=_AFTER_THE_SETTLED_BAR))
        == 1
    )


class _BookInfo:
    def __init__(self, book):
        self._book = book
        self.asked = []

    def l2_snapshot(self, coin):
        self.asked.append(coin)
        return self._book


def test_get_exchange_time_reads_the_l2book_stamp_for_the_coin():
    # Issue #51: the exchange clock comes off the public l2Book snapshot —
    # requested for the coin (so the answer carries an identity to check) and
    # mapped to an aware UTC datetime at ms precision.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "BTC", "time": 1787369175468, "levels": [[], []]})
    when = HyperliquidMarketData(client).get_exchange_time("BTC")
    assert client.info.asked == ["BTC"]
    assert when.tzinfo is not None
    assert int(when.timestamp() * 1000) == 1787369175468


def test_get_exchange_time_rejects_a_misrouted_response():
    # Same identity-echo discipline as the other two reads, and the same
    # mutation-probed wiring: the mapper only checks when the reader asks.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "ETH", "time": 1787369175468, "levels": [[], []]})
    with pytest.raises(MalformedResponseError, match="carries coin 'ETH'"):
        HyperliquidMarketData(client).get_exchange_time("BTC")


def test_get_exchange_time_fails_closed_without_a_stamp():
    # Fail-closed (decided 2026-08-22): the stamp is the guard's only clock, so
    # a book without one is a malformed answer, not a skipped check.
    client = _FakeClient(None)
    client.info = _BookInfo({"coin": "BTC", "levels": [[], []]})
    with pytest.raises(MalformedResponseError, match="'time' is unusable"):
        HyperliquidMarketData(client).get_exchange_time("BTC")


def test_get_candles_rejects_a_response_for_the_wrong_interval():
    # The interval half of the wiring, pinned separately: the coin test's bar
    # carries the RIGHT interval, so its raise proves nothing about ``i``.
    # Dropping expected_interval from the call site left 76 tests green
    # (2026-08-17 exit-sweep mutation probe), and a 4h series read as 1m
    # rescales every indicator -- this is not the cheap half.
    client = _FakeClient(None)
    client.info = _CandleInfo(bars=[_settled_bar(i="4h")])
    with pytest.raises(MalformedResponseError, match="carries interval '4h'"):
        HyperliquidMarketData(client).get_candles("BTC", "1m", 1, end=_AFTER_THE_SETTLED_BAR)


# ---------------------------------------------------------------------------
# the window is cut at the clock the caller hands in — the exchange's (issue #124)
# ---------------------------------------------------------------------------


class _VenueLikeInfo:
    """A ``candleSnapshot`` / ``fundingHistory`` that answers like the venue.

    Returns every bar that OVERLAPS the requested ``[start, end]`` — so the
    still-forming bar (``t <= end < T``) is in the response, as it is on the
    real endpoint — and records what it was asked, so a test can pin the
    window the reader cut rather than infer it from the survivors.
    """

    def __init__(self, bars=(), funding=()):
        self._bars = list(bars)
        self._funding = list(funding)
        self.candle_windows = []
        self.funding_windows = []

    def candles_snapshot(self, coin, interval, start, end):
        self.candle_windows.append((start, end))
        return [b for b in self._bars if b["t"] <= end and b["T"] >= start]

    def funding_history(self, coin, start, end):
        self.funding_windows.append((start, end))
        return [p for p in self._funding if start <= p["time"] <= end]


def _bars_closing_every(interval, *, last_close, count):
    """``count`` consecutive ``interval`` bars, the newest closing at ``last_close``."""
    width = interval_to_ms(interval)
    last_close_ms = _ms(last_close)
    return [
        _settled_bar(
            i=interval,
            t=last_close_ms - (count - k) * width,
            T=last_close_ms - (count - k - 1) * width - 1,
        )
        for k in range(count)
    ]


_BOUNDARY = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)  # a 4h bar closes here


def test_get_candles_keeps_only_bars_the_exchanges_clock_has_passed():
    # Issue #124's acceptance fixture: 4h bars, the exchange's clock 3h30 into
    # the forming bar (08:00-12:00), and this HOST 90 minutes ahead of the
    # exchange — past the forming bar's close. The window is cut at the clock
    # the caller hands in (the exchange's), and the reader consults no other
    # (the signature pin below is what keeps it that way), so the host's
    # clock plays no part: the venue is asked for a window ending at the
    # exchange's reading, the forming bar it returns is dropped, and the
    # newest bar handed back is the one that closed at 08:00 — the previous
    # CLOSED bar, which is what the context's ``as_of`` becomes. Before #124
    # the host-cut window kept the forming bar (close 12:00 <= host 13:00)
    # and the guard refused the cycle a step later.
    exchange_now = _BOUNDARY + timedelta(hours=3, minutes=30)
    info = _VenueLikeInfo(
        bars=_bars_closing_every("4h", last_close=_BOUNDARY + timedelta(hours=4), count=6)
    )
    client = _FakeClient(None)
    client.info = info
    candles = HyperliquidMarketData(client).get_candles("BTC", "4h", 3, end=exchange_now)
    assert info.candle_windows == [
        (_ms(exchange_now) - 4 * interval_to_ms("4h"), _ms(exchange_now))
    ]
    assert [c.close_time for c in candles] == [
        _ms(_BOUNDARY - timedelta(hours=8)) - 1,
        _ms(_BOUNDARY - timedelta(hours=4)) - 1,
        _ms(_BOUNDARY) - 1,
    ]
    assert all(c.close_time <= _ms(exchange_now) for c in candles)


def test_get_candles_cut_is_inclusive_at_the_bars_close():
    # ``close_time <= end``: a bar whose close IS the exchange's reading is
    # closed (the 2026-08-26 measurement: it reads closed the moment the
    # clock passes its close), one millisecond earlier it is not. The
    # millisecond matters because ``end`` is the exchange's own stamp handed
    # back in, so the comparison is exact, not approximate.
    bars = _bars_closing_every("1m", last_close=_BOUNDARY, count=2)
    newest_close_ms = bars[-1]["T"]
    at_close = _EPOCH + timedelta(milliseconds=newest_close_ms)
    client = _FakeClient(None)
    client.info = _VenueLikeInfo(bars=bars)
    kept = HyperliquidMarketData(client).get_candles("BTC", "1m", 2, end=at_close)
    assert [c.close_time for c in kept] == [bars[0]["T"], newest_close_ms]
    client.info = _VenueLikeInfo(bars=bars)
    kept = HyperliquidMarketData(client).get_candles(
        "BTC", "1m", 2, end=at_close - timedelta(milliseconds=1)
    )
    assert [c.close_time for c in kept] == [bars[0]["T"]]


def test_get_funding_history_window_ends_at_the_clock_handed_in():
    # Same discipline for the funding window: a host behind the exchange used
    # to end this window early and silently lose the newest settlements from
    # the z-score sample. The venue is asked for exactly
    # ``[end - window_days, end]`` at the exchange's clock, host clock aside.
    exchange_now = _BOUNDARY + timedelta(minutes=5)
    info = _VenueLikeInfo(
        funding=[
            {"coin": "BTC", "time": _ms(_BOUNDARY), "fundingRate": "0.0001", "premium": "0"},
            {
                "coin": "BTC",
                "time": _ms(_BOUNDARY - timedelta(hours=1)),
                "fundingRate": "0.0002",
                "premium": "0",
            },
        ]
    )
    client = _FakeClient(None)
    client.info = info
    points = HyperliquidMarketData(client).get_funding_history("BTC", 7, end=exchange_now)
    assert info.funding_windows == [(_ms(exchange_now) - 7 * 24 * 60 * 60_000, _ms(exchange_now))]
    assert [p.time for p in points] == [_ms(_BOUNDARY - timedelta(hours=1)), _ms(_BOUNDARY)]


def test_windowed_reads_refuse_a_naive_end():
    # A naive ``end`` is a caller that reached for ``datetime.now()`` without
    # a zone — refused by name before any request goes out, on both reads.
    client = _FakeClient(None)
    client.info = _VenueLikeInfo()
    reader = HyperliquidMarketData(client)
    naive = datetime(2026, 8, 31, 8, 0)
    with pytest.raises(ValueError, match="window end must be timezone-aware"):
        reader.get_candles("BTC", "4h", 3, end=naive)
    with pytest.raises(ValueError, match="window end must be timezone-aware"):
        reader.get_funding_history("BTC", 7, end=naive)
    assert client.info.candle_windows == [] and client.info.funding_windows == []


def test_windowed_reads_take_no_default_clock():
    # The parameter is REQUIRED, not defaulted to the host clock: a default
    # would be a silent road back to the window the host's clock cut. Pinned
    # on the concrete reader and on the port, so neither can grow one alone.
    import inspect

    for cls in (HyperliquidMarketData, ExchangeMarketData):
        for name in ("get_candles", "get_funding_history"):
            end = inspect.signature(getattr(cls, name)).parameters["end"]
            assert end.kind is inspect.Parameter.KEYWORD_ONLY, (cls, name)
            assert end.default is inspect.Parameter.empty, (cls, name)
