"""Translate raw Hyperliquid JSON into the clean domain schema.

**This is the only module allowed to know Hyperliquid's raw field names.**
Everything downstream works with the :mod:`...domains.perp.schema` dataclasses
and :class:`~decimal.Decimal`. Keep all ``"markPx"``-style string keys here.

The shapes handled:

- ``metaAndAssetCtxs`` -> ``[meta, assetCtxs]``; ``meta["universe"]`` is parallel
  to ``assetCtxs`` (same index per coin).
- ``candleSnapshot`` -> ``[{t,T,o,h,l,c,v}, ...]``.
- ``fundingHistory`` -> ``[{fundingRate, premium, time}, ...]``.
- ``clearinghouseState`` (a.k.a. ``user_state``) -> margin summary + positions.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from ...domains.perp.margin import MarginSchedule, MarginTier
from ...domains.perp.schema import (
    AccountSnapshot,
    Candle,
    FundingPoint,
    MarketSnapshot,
    PerpPosition,
)
from .errors import MalformedResponseError, UnknownCoinError

logger = logging.getLogger(__name__)

# Conservative ceiling on the fraction of bars/points a single response may drop
# before the whole series is treated as a systematic feed fault rather than a
# transient glitch. The per-element drop+warn handling below tolerates the
# occasional bad bar, but indicators (ATR/RSI/EMA) and the funding z-score are
# computed over the survivor set as if it were contiguous — a silently gappy
# series skews them with no abort. Callers can override per-call (e.g. from config).
_MAX_DROP_FRACTION = 0.10


def _dec(value: Any, *, field: str) -> Decimal:
    """Parse a required HL numeric string into Decimal, or fail loudly."""
    if value is None:
        raise MalformedResponseError(f"missing required field '{field}'")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MalformedResponseError(f"field '{field}' is not numeric: {value!r}") from exc
    # ``Decimal("NaN")``/``Decimal("Inf")`` parse without error but would poison every
    # downstream computation — a required field carrying one is malformed, not numeric.
    if not result.is_finite():
        raise MalformedResponseError(f"field '{field}' is not a finite number: {value!r}")
    return result


def _opt_dec(value: Any, *, field: str) -> Decimal | None:
    """Parse an optional HL numeric string; ``None``/blank -> ``None``.

    A *present* value that fails to parse (e.g. ``"N/A"`` in ``liquidationPx``) is
    a data-quality signal, not an omitted field — it is still treated as absent
    (returns ``None``) so downstream fallbacks apply, but logged at WARNING (naming
    ``field``) so the corruption is not swallowed silently.
    """
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        logger.warning(
            "optional field %s is present but unparseable as a number: %r — %s",
            field,
            value,
            exc,
        )
        return None
    # ``Decimal("NaN")``/``Decimal("Inf")`` parse cleanly but are a data-quality signal,
    # not a usable number — drop them (like an absent field) so the NaN never propagates,
    # but log so the corruption is not swallowed silently.
    if not result.is_finite():
        logger.warning("optional field %s is present but not finite: %r", field, value)
        return None
    return result


# --------------------------------------------------------------------------
# metaAndAssetCtxs -> MarketSnapshot
# --------------------------------------------------------------------------


def map_market_snapshot(meta_and_asset_ctxs: Any, coin: str) -> MarketSnapshot:
    """Pick ``coin`` out of the ``metaAndAssetCtxs`` response.

    ``meta["universe"]`` and ``assetCtxs`` are index-parallel: the asset context
    for a coin lives at the same index as the coin in the universe.
    """
    if not isinstance(meta_and_asset_ctxs, (list, tuple)) or len(meta_and_asset_ctxs) < 2:
        raise MalformedResponseError("metaAndAssetCtxs must be [meta, assetCtxs]")
    meta, asset_ctxs = meta_and_asset_ctxs[0], meta_and_asset_ctxs[1]

    universe = (meta or {}).get("universe")
    if not isinstance(universe, list) or not isinstance(asset_ctxs, list):
        raise MalformedResponseError("metaAndAssetCtxs missing universe / assetCtxs lists")

    for i, asset in enumerate(universe):
        # A non-dict universe entry would raise a bare AttributeError on
        # ``asset.get("name")`` that escapes the port boundary unwrapped — fail
        # loud as a domain error instead (see map_candles).
        if not isinstance(asset, dict):
            raise MalformedResponseError(f"universe[{i}] is {type(asset).__name__}, expected dict")

    index = next((i for i, asset in enumerate(universe) if asset.get("name") == coin), None)
    if index is None:
        known = ", ".join(a.get("name", "?") for a in universe[:10])
        raise UnknownCoinError(f"coin {coin!r} not in perp universe (e.g. {known})")
    if index >= len(asset_ctxs):
        raise MalformedResponseError(f"assetCtxs has no entry for {coin!r} at index {index}")

    ctx = asset_ctxs[index]
    if not isinstance(ctx, dict):
        # Mirror the universe / assetPositions guards: a non-dict asset context
        # would raise a bare AttributeError on ``ctx.get(...)`` — fail loud as a
        # domain error so it is distinguishable from a network failure.
        raise MalformedResponseError(
            f"assetCtxs[{index}] for {coin!r} is {type(ctx).__name__}, expected dict"
        )
    return MarketSnapshot(
        coin=coin,
        mark_price=_dec(ctx.get("markPx"), field="markPx"),
        oracle_price=_dec(ctx.get("oraclePx"), field="oraclePx"),
        prev_day_price=_dec(ctx.get("prevDayPx"), field="prevDayPx"),
        open_interest=_dec(ctx.get("openInterest"), field="openInterest"),
        day_ntl_volume=_dec(ctx.get("dayNtlVlm"), field="dayNtlVlm"),
        funding=_dec(ctx.get("funding"), field="funding"),
        mid_price=_opt_dec(ctx.get("midPx"), field="midPx"),
        premium=_opt_dec(ctx.get("premium"), field="premium"),
    )


# --------------------------------------------------------------------------
# meta -> MarginSchedule
# --------------------------------------------------------------------------


def _margin_tiers_from_table(table: Any) -> list[MarginTier] | None:
    """Build tiers from one ``marginTables`` entry's ``marginTiers`` list.

    Returns ``None`` when the table has no usable ``marginTiers`` (so the caller
    falls back to the single-tier ``maxLeverage`` form) and raises only on a
    present-but-corrupt tier, matching the fail-loud stance of the other mappers.
    """
    if not isinstance(table, dict):
        return None
    raw_tiers = table.get("marginTiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        return None
    tiers: list[MarginTier] = []
    for i, raw in enumerate(raw_tiers):
        if not isinstance(raw, dict):
            raise MalformedResponseError(f"marginTiers[{i}] is {type(raw).__name__}, expected dict")
        try:
            tiers.append(
                MarginTier(
                    lower_bound=_dec(raw.get("lowerBound"), field=f"marginTiers[{i}].lowerBound"),
                    max_leverage=_dec(
                        raw.get("maxLeverage"), field=f"marginTiers[{i}].maxLeverage"
                    ),
                )
            )
        except ValueError as exc:
            # A bad tier value (non-positive leverage, negative bound) is corrupt
            # exchange data — keep it in the mapper's MalformedResponseError contract.
            raise MalformedResponseError(f"marginTiers[{i}]: {exc}") from exc
    return tiers


def map_margin_schedule(meta_and_asset_ctxs: Any, coin: str) -> MarginSchedule:
    """Extract ``coin``'s maintenance-margin schedule from ``metaAndAssetCtxs``.

    Prefers the explicit tiered table: ``meta["marginTables"]`` (a list of
    ``[id, table]`` pairs) keyed by the universe entry's ``marginTableId``. When
    no tier table is present — the common case for the recorded fixtures and
    older ``meta`` responses — falls back to a single tier spanning all notional
    from the asset's ``maxLeverage`` (execution §6.6.1 forbids hardcoding it, so
    it is always read from the response, never assumed).
    """
    if not isinstance(meta_and_asset_ctxs, (list, tuple)) or not meta_and_asset_ctxs:
        raise MalformedResponseError("metaAndAssetCtxs must be [meta, assetCtxs]")
    meta = meta_and_asset_ctxs[0]
    universe = (meta or {}).get("universe")
    if not isinstance(universe, list):
        raise MalformedResponseError("meta missing universe list")
    entry = next((a for a in universe if isinstance(a, dict) and a.get("name") == coin), None)
    if entry is None:
        known = ", ".join(a.get("name", "?") for a in universe[:10] if isinstance(a, dict))
        raise UnknownCoinError(f"coin {coin!r} not in perp universe (e.g. {known})")

    # Explicit tier table, when the entry references one. A present-but-
    # unresolvable table id is a data anomaly, not a reason to silently fall back
    # to the single-tier maxLeverage form — that would apply the lowest rate to
    # all notional and under-state maintenance margin (non-conservative), so we
    # fail loud like every other corrupt-field path in this module. That includes
    # the id pointing at a missing/malformed ``marginTables`` container, not just
    # a missing list entry. The converse — tables present but this entry carries
    # no ``marginTableId`` — is the normal shape for an untiered asset in a mixed
    # universe, and falls through to the maxLeverage fallback below.
    raw_tables = (meta or {}).get("marginTables")
    table_id = entry.get("marginTableId")
    if table_id is not None:
        if not isinstance(raw_tables, list):
            raise MalformedResponseError(
                f"universe entry for {coin!r} carries marginTableId {table_id!r} "
                "but meta has no marginTables list to resolve it against"
            )
        matched = next(
            (
                pair[1]
                for pair in raw_tables
                if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[0] == table_id
            ),
            None,
        )
        if matched is None:
            raise MalformedResponseError(
                f"marginTableId {table_id!r} for {coin!r} not found in marginTables"
            )
        tiers = _margin_tiers_from_table(matched)
        if not tiers:
            raise MalformedResponseError(
                f"margin table {table_id!r} for {coin!r} has no usable marginTiers"
            )
        try:
            return MarginSchedule(coin=coin, tiers=tuple(tiers))
        except ValueError as exc:
            raise MalformedResponseError(f"margin table {table_id!r} for {coin!r}: {exc}") from exc

    # Fallback: no tier table for this asset — one tier covering all notional at
    # the asset's max leverage (read from meta, never hardcoded).
    try:
        return MarginSchedule(
            coin=coin,
            tiers=(
                MarginTier(
                    lower_bound=Decimal(0),
                    max_leverage=_dec(entry.get("maxLeverage"), field="maxLeverage"),
                ),
            ),
        )
    except (ValueError, MalformedResponseError) as exc:
        # ``_dec`` raises MalformedResponseError (not ValueError) on a missing /
        # non-numeric maxLeverage; catch it too so the coin-context prefix is
        # added on that path as well, matching the tiered-table branch above.
        raise MalformedResponseError(f"maxLeverage for {coin!r}: {exc}") from exc


def map_sz_decimals(meta_and_asset_ctxs: Any, coin: str) -> int:
    """Extract ``coin``'s ``szDecimals`` from ``metaAndAssetCtxs``.

    The paper engine derives its quantity step and price tick from this
    (execution §1.2 — read from the exchange meta, never hardcoded).
    """
    if not isinstance(meta_and_asset_ctxs, (list, tuple)) or not meta_and_asset_ctxs:
        raise MalformedResponseError("metaAndAssetCtxs must be [meta, assetCtxs]")
    universe = (meta_and_asset_ctxs[0] or {}).get("universe")
    if not isinstance(universe, list):
        raise MalformedResponseError("meta missing universe list")
    entry = next((a for a in universe if isinstance(a, dict) and a.get("name") == coin), None)
    if entry is None:
        known = ", ".join(a.get("name", "?") for a in universe[:10] if isinstance(a, dict))
        raise UnknownCoinError(f"coin {coin!r} not in perp universe (e.g. {known})")
    raw = entry.get("szDecimals")
    # bool is an int subclass; True would silently become szDecimals=1.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise MalformedResponseError(f"szDecimals for {coin!r} is not a non-negative int: {raw!r}")
    return raw


# --------------------------------------------------------------------------
# candleSnapshot -> [Candle]
# --------------------------------------------------------------------------


def map_candles(raw_candles: Any, *, max_drop_fraction: float = _MAX_DROP_FRACTION) -> list[Candle]:
    """Map a ``candleSnapshot`` list to ``Candle`` objects, oldest first.

    Drops individual malformed bars (transient glitch), but raises
    :class:`MalformedResponseError` if more than ``max_drop_fraction`` of the bars
    are bad — a gappy series silently skews the downstream indicators.
    """
    if raw_candles is None:
        # A null payload is an anomaly, not "no candles" — fail loud so the run
        # aborts instead of reasoning over silently-empty market data.
        raise MalformedResponseError("candleSnapshot is None — expected a list")
    if not isinstance(raw_candles, list):
        raise MalformedResponseError("candleSnapshot must be a list")
    for i, c in enumerate(raw_candles):
        # A non-dict element (e.g. None during a partial response) would raise a
        # bare AttributeError on ``c.get(...)`` that escapes the port boundary
        # unwrapped — fail loud as a domain error instead, naming the bad index.
        if not isinstance(c, dict):
            raise MalformedResponseError(
                f"candleSnapshot[{i}] is {type(c).__name__}, expected dict"
            )
    candles: list[Candle] = []
    for i, c in enumerate(raw_candles):
        try:
            candle = Candle(
                open_time=int(_dec(c.get("t"), field="t")),
                close_time=int(_dec(c.get("T"), field="T")),
                open=_dec(c.get("o"), field="o"),
                high=_dec(c.get("h"), field="h"),
                low=_dec(c.get("l"), field="l"),
                close=_dec(c.get("c"), field="c"),
                volume=_dec(c.get("v"), field="v"),
            )
        except (ValueError, MalformedResponseError) as exc:
            # A single bad bar — whether it violates Candle's invariant (bad OHLC
            # ordering, time, negative volume -> ValueError) or has a missing/
            # non-numeric field (-> MalformedResponseError) — is a transient feed
            # glitch, not a reason to abort the whole run. Drop it and warn; the
            # warm-up threshold downstream still aborts if too few bars survive.
            logger.warning("dropping malformed candleSnapshot[%d]: %s", i, exc)
            continue
        candles.append(candle)
    dropped = len(raw_candles) - len(candles)
    if dropped and not candles:
        # Every bar was malformed: that is a systematic feed fault, not a glitch.
        # Fail loud (mirroring map_funding_history) rather than return an empty list,
        # which downstream reads as a benign "0 candles available" (a thin-market /
        # new-listing signal) and masks the real cause — a broken feed/parser.
        raise MalformedResponseError(
            f"all {len(raw_candles)} candleSnapshot bars were malformed — feed appears broken"
        )
    if dropped and dropped / len(raw_candles) > max_drop_fraction:
        # Too many bars dropped to treat as a transient glitch: the survivor set has
        # silent gaps, so ATR/RSI/EMA would be computed over a non-contiguous series
        # without warning. Fail loud rather than return a misleadingly-short window
        # that still clears the warm-up count gate downstream.
        raise MalformedResponseError(
            f"dropped {dropped}/{len(raw_candles)} candleSnapshot bars "
            f"({round(dropped * 100 / len(raw_candles))}%) exceeds "
            f"{round(max_drop_fraction * 100)}% threshold — feed appears systematically malformed"
        )
    if dropped:
        # Per-bar warnings can scroll past; an aggregate line makes a *systematic*
        # fault visible. Dropping a large fraction is not a transient glitch — the
        # operator should see the ratio, since the warm-up guard only checks the
        # absolute survivor count, not how many bars were silently discarded.
        logger.warning(
            "dropped %d/%d candleSnapshot bars (%d%%) — feed may be systematically malformed",
            dropped,
            len(raw_candles),
            round(dropped * 100 / len(raw_candles)),
        )
    candles.sort(key=lambda c: c.open_time)
    return candles


# --------------------------------------------------------------------------
# fundingHistory -> [FundingPoint]
# --------------------------------------------------------------------------


def map_funding_history(
    raw: Any, *, max_drop_fraction: float = _MAX_DROP_FRACTION
) -> list[FundingPoint]:
    """Map a ``fundingHistory`` list to ``FundingPoint`` objects, oldest first.

    Drops individual malformed points (transient glitch), but raises
    :class:`MalformedResponseError` if more than ``max_drop_fraction`` of the points
    are bad — a truncated window silently skews the funding z-score.
    """
    if raw is None:
        # A null payload is an anomaly, not "no history" — fail loud (see map_candles).
        raise MalformedResponseError("fundingHistory is None — expected a list")
    if not isinstance(raw, list):
        raise MalformedResponseError("fundingHistory must be a list")
    for i, p in enumerate(raw):
        # See map_candles: a non-dict element must fail loud as a domain error,
        # not as a bare AttributeError escaping the port boundary.
        if not isinstance(p, dict):
            raise MalformedResponseError(
                f"fundingHistory[{i}] is {type(p).__name__}, expected dict"
            )
    points: list[FundingPoint] = []
    for i, p in enumerate(raw):
        try:
            point = FundingPoint(
                time=int(_dec(p.get("time"), field="time")),
                rate=_dec(p.get("fundingRate"), field="fundingRate"),
                premium=_opt_dec(p.get("premium"), field="premium"),
            )
        except (ValueError, MalformedResponseError) as exc:
            # Mirror map_candles: a single bad point (missing/non-numeric field) is a
            # transient glitch, not a reason to abort. Funding is non-critical — the
            # z-score degrades to None on too few samples — so drop and warn.
            logger.warning("dropping malformed fundingHistory[%d]: %s", i, exc)
            continue
        points.append(point)
    dropped = len(raw) - len(points)
    if dropped and not points:
        # Every point was malformed: that is a systematic feed fault, not a glitch.
        # Fail loud rather than silently return an empty history (which would mask the
        # corruption as a benign "no funding data" and degrade the z-score to None).
        raise MalformedResponseError(
            f"all {len(raw)} fundingHistory points were malformed — feed appears broken"
        )
    if dropped and dropped / len(raw) > max_drop_fraction:
        # Too many points dropped to treat as a glitch: a truncated window compresses
        # the funding z-score's stddev and can make an extreme current rate look
        # normal. Fail loud rather than silently degrade the funding view.
        raise MalformedResponseError(
            f"dropped {dropped}/{len(raw)} fundingHistory points "
            f"({round(dropped * 100 / len(raw))}%) exceeds "
            f"{round(max_drop_fraction * 100)}% threshold — feed appears systematically malformed"
        )
    if dropped:
        logger.warning(
            "dropped %d/%d fundingHistory points (%d%%) — feed may be systematically malformed",
            dropped,
            len(raw),
            round(dropped * 100 / len(raw)),
        )
    points.sort(key=lambda p: p.time)
    return points


# --------------------------------------------------------------------------
# clearinghouseState -> AccountSnapshot / PerpPosition
# --------------------------------------------------------------------------


def _map_position(asset_position: Any) -> PerpPosition | None:
    """Map one ``assetPositions[]`` entry; ``None`` if it carries no size."""
    pos_raw = (asset_position or {}).get("position")
    if pos_raw is None:
        return None  # no position object at all -> genuinely flat, nothing to read
    # A present-but-non-dict ``position`` (e.g. a JSON ``null`` swapped for 0, a
    # string) would otherwise be masked by a bare ``or {}`` and read as flat —
    # fail loud, matching the assetPositions-entry guard in map_account_snapshot.
    if not isinstance(pos_raw, dict):
        raise MalformedResponseError(
            f"assetPositions entry 'position' is {type(pos_raw).__name__}, expected dict"
        )
    pos = pos_raw
    if not pos:
        return None  # empty position object -> genuinely flat, nothing to read
    # A present position must carry a parseable ``szi``; parse it loud so a corrupt
    # or missing size raises instead of silently reading as flat (see ``coin`` below).
    szi = _dec(pos.get("szi"), field="szi")
    if szi == 0:
        return None
    coin = pos.get("coin")
    if not coin:
        # A sized position with no coin can't be matched by ``position_for`` and
        # would silently read as flat — fail loud, like every other required field.
        raise MalformedResponseError(f"position has size {szi} but no 'coin': {pos!r}")
    leverage_raw = pos.get("leverage")
    if leverage_raw is not None and not isinstance(leverage_raw, dict):
        # A truthy non-dict leverage (e.g. the bare string "cross") would slip past
        # ``or {}`` and make ``.get("value")`` raise a bare AttributeError that
        # escapes the port's domain-error contract — guard it like every other
        # optional sub-object here and treat it as absent.
        logger.warning(
            "position 'leverage' is %s, expected dict — treating as absent",
            type(leverage_raw).__name__,
        )
        leverage_raw = None
    leverage = leverage_raw or {}
    return PerpPosition(
        coin=coin,
        size=szi,  # signed: + long, - short
        entry_price=_dec(pos.get("entryPx"), field="entryPx"),
        unrealized_pnl=_dec(pos.get("unrealizedPnl"), field="unrealizedPnl"),
        leverage=_opt_dec(leverage.get("value"), field="leverage.value"),
        liquidation_price=_opt_dec(pos.get("liquidationPx"), field="liquidationPx"),
        margin_used=_opt_dec(pos.get("marginUsed"), field="marginUsed"),
        position_value=_opt_dec(pos.get("positionValue"), field="positionValue"),
    )


def map_account_snapshot(clearinghouse_state: Any) -> AccountSnapshot:
    """Map ``clearinghouseState`` to an :class:`AccountSnapshot`."""
    if clearinghouse_state is None:
        # A null payload is an anomaly, not a flat account — fail loud (see
        # map_candles) so the run aborts instead of trading on a silently-empty
        # account read.
        raise MalformedResponseError("clearinghouseState is None — expected a dict")
    if not isinstance(clearinghouse_state, dict):
        raise MalformedResponseError("clearinghouseState must be a dict")
    state = clearinghouse_state
    # ``marginSummary`` carries the required accountValue/totalMarginUsed. A bare
    # ``or {}`` here would mask its absence and surface as a misdirecting "missing
    # field 'accountValue'" — name the actually-missing structure instead.
    margin = state.get("marginSummary")
    if not isinstance(margin, dict):
        raise MalformedResponseError(
            f"clearinghouseState 'marginSummary' must be a dict, got {type(margin).__name__}"
        )
    # A bare ``or []`` would mask a present-but-falsy non-list (``null``, ``false``,
    # ``0``) as "no positions" and bypass the type guard below — treat a missing/
    # null field as flat, but fail loud on any other non-list shape.
    raw_positions = state.get("assetPositions")
    if raw_positions is None:
        raw_positions = []
    elif not isinstance(raw_positions, list):
        raise MalformedResponseError(
            f"clearinghouseState 'assetPositions' must be a list, got {type(raw_positions).__name__}"
        )
    for i, ap in enumerate(raw_positions):
        # ``None`` is tolerated (``_map_position`` reads it as flat), but a truthy
        # non-dict entry would raise a bare AttributeError that escapes as a generic
        # fatal — and, not being an ExchangeError, would slip past _load_position's
        # guard and undermine the "don't trade on unknown account state" abort.
        if ap is not None and not isinstance(ap, dict):
            raise MalformedResponseError(
                f"assetPositions[{i}] is {type(ap).__name__}, expected dict"
            )
    positions = tuple(p for p in (_map_position(ap) for ap in raw_positions) if p is not None)
    return AccountSnapshot(
        account_value=_dec(margin.get("accountValue"), field="accountValue"),
        withdrawable=_dec(state.get("withdrawable"), field="withdrawable"),
        total_margin_used=_dec(margin.get("totalMarginUsed"), field="totalMarginUsed"),
        positions=positions,
    )


def map_position(clearinghouse_state: Any, coin: str) -> PerpPosition | None:
    """Extract the open position for ``coin`` from ``clearinghouseState``."""
    return map_account_snapshot(clearinghouse_state).position_for(coin)
