"""One pool's reading at a bar boundary, the checks on it, and the bar built from several.

A :class:`PoolBar` is what the store keeps: one pool's raw state at the end
of the last block before a boundary. It belongs to no run. A
:class:`~.types.Bar` is the view the engine works with, every token priced
in the quote token, and :func:`assemble_bar` builds one from the readings of
the configured pools at one boundary.

The data checks are not stored. :func:`pool_bar_flags` works them out from a
reading, the one before it and the :class:`BarSettings` in force, so a
reading filled in later, or a changed limit, changes the flags the next time
they are read. The one thing a reading does carry is its :class:`Finality`:
whether its close block has been checked against the final chain.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Final

from .decimal_context import DECIMAL_CONTEXT
from .prices import (
    MAX_SQRT_RATIO,
    MAX_TICK,
    MIN_SQRT_RATIO,
    MIN_TICK,
    price_from_sqrt_price_x96,
    price_from_tick,
)
from .types import Bar, Pool, Token

__all__ = [
    "MAX_TWAP_WINDOW_SECONDS",
    "BarFlag",
    "BarSettings",
    "Finality",
    "PoolBar",
    "SuspectCause",
    "assemble_bar",
    "pool_bar_flags",
    "suspect_causes",
]

_ADDRESS: Final = re.compile(r"0x[0-9a-fA-F]{40}")
_BLOCK_HASH: Final = re.compile(r"0x[0-9a-f]{64}")
_DAY: Final = 86_400
# ``observe`` takes its ages as uint32.
MAX_TWAP_WINDOW_SECONDS: Final = 2**32 - 1


class Finality(str, Enum):
    """Whether a reading's close block has been checked against the final chain.

    ``PENDING`` was read from a block that could still be replaced.
    ``FINAL`` is on the final chain. ``REORGED`` was checked once its block
    number was final and is not: the reading describes a block the chain
    dropped, or a block that turned out not to be the last before the
    boundary.
    """

    PENDING = "pending"
    FINAL = "final"
    REORGED = "reorged"


class BarFlag(str, Enum):
    """What a data check found on one pool's reading."""

    # The close price is further from the TWAP than the limit allows.
    TWAP_DEVIATION = "twap_deviation"
    # The reading's block is not on the final chain.
    REORGED = "reorged"
    # The reading before this one is not one interval earlier.
    GAP = "gap"
    # The close price moved more than the limit since the reading before.
    LARGE_MOVE = "large_move"


def _is_int(value: object, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _is_fraction(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _require_interval(value: object) -> None:
    if not _is_int(value, minimum=1) or _DAY % value:  # type: ignore[operator]
        raise ValueError(
            f"interval_seconds must be a positive integer that divides a day "
            f"({_DAY} seconds), got {value!r}"
        )


def _require_window(value: object) -> None:
    if not _is_int(value, minimum=1) or value > MAX_TWAP_WINDOW_SECONDS:  # type: ignore[operator]
        raise ValueError(
            f"twap_window_seconds must be an integer from 1 to {MAX_TWAP_WINDOW_SECONDS}, "
            f"got {value!r}"
        )


@dataclass(frozen=True)
class BarSettings:
    """How long a bar is, and the limits its data checks hold a reading to.

    Boundaries are the multiples of ``interval_seconds`` since the epoch, so
    a one-day bar closes at 00:00 UTC. The interval must divide a day: a
    longer or an uneven one would put its boundaries on no fixed hour (a
    week counted from the epoch starts on a Thursday). ``max_twap_deviation``
    and ``max_move`` are relative: ``Decimal("0.05")`` is 5%.
    """

    interval_seconds: int = 86_400
    twap_window_seconds: int = 1_800
    max_twap_deviation: Decimal = Decimal("0.02")
    max_move: Decimal = Decimal("0.5")

    def __post_init__(self) -> None:
        _require_interval(self.interval_seconds)
        _require_window(self.twap_window_seconds)
        for name in ("max_twap_deviation", "max_move"):
            if not _is_fraction(getattr(self, name)):
                raise ValueError(
                    f"{name} must be a finite, positive Decimal, got {getattr(self, name)!r}"
                )


@dataclass(frozen=True)
class PoolBar:
    """One pool's state at the end of the last block before a bar boundary.

    ``time`` is the boundary in epoch seconds and ``close_block`` the last
    block before it; ``close_block_hash`` and ``close_block_time`` are that
    block's, as read. ``sqrt_price_x96`` and ``tick`` are the pool's
    ``slot0`` at the end of the block, ``twap_tick`` its mean tick over the
    ``twap_window_seconds`` ending there, and ``base_fee_wei`` the block's
    base fee. ``pool`` is the pool's address.
    """

    chain_id: int
    pool: str
    interval_seconds: int
    time: int
    close_block: int
    close_block_hash: str
    close_block_time: int
    sqrt_price_x96: int
    tick: int
    twap_tick: int
    twap_window_seconds: int
    base_fee_wei: int
    finality: Finality = Finality.PENDING

    def __post_init__(self) -> None:
        for name in ("chain_id", "close_block", "close_block_time", "base_fee_wei"):
            if not _is_int(getattr(self, name)):
                raise ValueError(
                    f"{name} must be a non-negative integer, got {getattr(self, name)!r}"
                )
        if not isinstance(self.pool, str) or not _ADDRESS.fullmatch(self.pool):
            raise ValueError(f"pool must be 0x followed by 40 hex digits, got {self.pool!r}")
        _require_interval(self.interval_seconds)
        if not _is_int(self.time, minimum=1) or self.time % self.interval_seconds:
            raise ValueError(
                f"time must be a positive multiple of the {self.interval_seconds}-second "
                f"interval, got {self.time!r}"
            )
        if self.close_block_time >= self.time:
            raise ValueError(
                f"the close block is the last before the boundary {self.time}, and block "
                f"{self.close_block} is at {self.close_block_time}"
            )
        if not isinstance(self.close_block_hash, str) or not _BLOCK_HASH.fullmatch(
            self.close_block_hash
        ):
            raise ValueError(
                f"close_block_hash must be 0x followed by 64 lowercase hex digits, "
                f"got {self.close_block_hash!r}"
            )
        if (
            not _is_int(self.sqrt_price_x96)
            or not MIN_SQRT_RATIO <= self.sqrt_price_x96 < MAX_SQRT_RATIO
        ):
            raise ValueError(
                f"sqrt_price_x96 must be an integer in [{MIN_SQRT_RATIO}, {MAX_SQRT_RATIO}), "
                f"got {self.sqrt_price_x96!r}"
            )
        for name in ("tick", "twap_tick"):
            value = getattr(self, name)
            if not _is_int(value, minimum=MIN_TICK) or value > MAX_TICK:
                raise ValueError(
                    f"{name} must be an integer in [{MIN_TICK}, {MAX_TICK}], got {value!r}"
                )
        _require_window(self.twap_window_seconds)
        if not isinstance(self.finality, Finality):
            raise ValueError(f"finality must be a Finality, got {self.finality!r}")


def _require_reading_of(pool: Pool, reading: PoolBar) -> None:
    if reading.pool != pool.address:
        raise ValueError(f"the reading is of pool {reading.pool}, not of {pool.address}")


def _relative_gap(value: Decimal, reference: Decimal) -> Decimal:
    """How far ``value`` is from ``reference``, as a fraction of ``reference``."""
    return DECIMAL_CONTEXT.subtract(DECIMAL_CONTEXT.divide(value, reference), Decimal(1)).copy_abs()


def pool_bar_flags(
    pool: Pool, reading: PoolBar, previous: PoolBar | None, settings: BarSettings
) -> frozenset[BarFlag]:
    """What the data checks find on ``reading``.

    ``previous`` is the same pool's latest reading before this one, at the
    same interval, or ``None`` when there is none: the first reading of a
    pool has no gap and no move to check. A move is measured only across one
    interval; after a gap it is the gap that is flagged. Prices are compared
    in the pool's own order, ``token1`` per ``token0``.

    A reading whose TWAP was taken over another window than the settings'
    is refused: the limit was set for the settings' window.
    """
    _require_reading_of(pool, reading)
    if reading.twap_window_seconds != settings.twap_window_seconds:
        raise ValueError(
            f"the reading of pool {reading.pool} at {reading.time} has a TWAP over "
            f"{reading.twap_window_seconds} seconds, and the settings ask for "
            f"{settings.twap_window_seconds}"
        )
    close = price_from_sqrt_price_x96(pool, reading.sqrt_price_x96, base=pool.token0)
    twap = price_from_tick(pool, reading.twap_tick, base=pool.token0)
    flags: set[BarFlag] = set()
    if _relative_gap(close, twap) > settings.max_twap_deviation:
        flags.add(BarFlag.TWAP_DEVIATION)
    if reading.finality is Finality.REORGED:
        flags.add(BarFlag.REORGED)
    if previous is not None:
        _require_reading_of(pool, previous)
        if (
            previous.chain_id != reading.chain_id
            or previous.interval_seconds != reading.interval_seconds
            or previous.time >= reading.time
        ):
            raise ValueError(
                f"the reading at {previous.time} does not come before the one at "
                f"{reading.time} in the same series"
            )
        if previous.time != reading.time - reading.interval_seconds:
            flags.add(BarFlag.GAP)
        else:
            before = price_from_sqrt_price_x96(pool, previous.sqrt_price_x96, base=pool.token0)
            if _relative_gap(close, before) > settings.max_move:
                flags.add(BarFlag.LARGE_MOVE)
    return frozenset(flags)


def _quote_prices(
    quote: Token, pools: Sequence[Pool], readings: Sequence[PoolBar]
) -> dict[str, Decimal]:
    """The quote-token price of every token the pools reach from ``quote``.

    A token is priced through the first pool, in the order given, that
    joins it to a token already priced; a pool whose two tokens are both
    priced by then is not used.
    """
    priced = {quote.symbol: Decimal(1)}
    waiting = list(zip(pools, readings, strict=True))
    while waiting:
        reachable = next(
            (
                index
                for index, (pool, _) in enumerate(waiting)
                if pool.token0.symbol in priced or pool.token1.symbol in priced
            ),
            None,
        )
        if reachable is None:
            stranded = sorted(
                {token.symbol for pool, _ in waiting for token in (pool.token0, pool.token1)}
            )
            raise ValueError(f"no pool joins {stranded} to the quote token {quote.symbol}")
        pool, reading = waiting.pop(reachable)
        has0, has1 = pool.token0.symbol in priced, pool.token1.symbol in priced
        if has0 != has1:
            base, known = (pool.token1, pool.token0) if has0 else (pool.token0, pool.token1)
            in_known = price_from_sqrt_price_x96(pool, reading.sqrt_price_x96, base=base)
            priced[base.symbol] = DECIMAL_CONTEXT.multiply(in_known, priced[known.symbol])
    del priced[quote.symbol]
    return priced


class SuspectCause(str, Enum):
    """Why a bar is suspect, the gravest first."""

    # A reading's close block turned out not to be on the final chain.
    REORGED = "reorged"
    # The pools' readings do not agree on the close block.
    CLOSE_BLOCK_MISMATCH = "close_block_mismatch"
    # A pool's close price is further from its TWAP than the limit allows.
    TWAP_DEVIATION = "twap_deviation"


def suspect_causes(
    pools: Sequence[Pool], readings: Sequence[PoolBar], flags: Sequence[frozenset[BarFlag]]
) -> tuple[tuple[SuspectCause, Pool | None], ...]:
    """Every cause the bar of ``readings`` is suspect for, the gravest first; none when it is not.

    A reading that is ``REORGED``, by its flag or its finality, or whose
    close is too far from its TWAP makes its bar suspect, and so do readings
    that do not agree on the close block. A gap and a large move do not:
    they are reported and left to whoever reads the report, since a market
    can move that far and a bar after a gap is still a true reading.

    A cause comes with the pool whose reading it was found on, and the
    disagreement on the close block, which is no one pool's, with ``None``.
    The arguments are :func:`assemble_bar`'s, which holds a bar suspect
    exactly when this finds a cause.
    """
    both = list(zip(pools, readings, flags, strict=True))
    blocks = {(reading.close_block, reading.close_block_hash) for reading in readings}
    return (
        *(
            (SuspectCause.REORGED, pool)
            for pool, reading, found in both
            if BarFlag.REORGED in found or reading.finality is Finality.REORGED
        ),
        *([(SuspectCause.CLOSE_BLOCK_MISMATCH, None)] if len(blocks) > 1 else []),
        *(
            (SuspectCause.TWAP_DEVIATION, pool)
            for pool, _, found in both
            if BarFlag.TWAP_DEVIATION in found
        ),
    )


def assemble_bar(
    quote: Token,
    pools: Sequence[Pool],
    readings: Sequence[PoolBar],
    *,
    flags: Sequence[frozenset[BarFlag]],
) -> Bar:
    """The bar at one boundary, from each pool's reading there.

    ``readings`` and ``flags`` are in the order of ``pools``, one each, all
    at the same boundary of the same series; the flags are what
    :func:`pool_bar_flags` found on each reading. The bar is suspect when
    :func:`suspect_causes` finds a cause. When the readings do not agree on
    the close block, at most one of them describes the block the boundary
    closed on, and the bar carries the highest of the blocks.
    """
    if not pools or len(pools) != len(readings) or len(flags) != len(readings):
        raise ValueError(
            f"a bar needs one reading and one set of flags per pool: {len(pools)} pool(s), "
            f"{len(readings)} reading(s) and {len(flags)} set(s) of flags"
        )
    for pool, reading in zip(pools, readings, strict=True):
        _require_reading_of(pool, reading)
    series = {(reading.chain_id, reading.interval_seconds, reading.time) for reading in readings}
    if len(series) != 1:
        raise ValueError(f"the readings are not of one boundary of one series: {sorted(series)}")
    closing = max(readings, key=lambda reading: reading.close_block)
    return Bar(
        time=closing.time,
        close_block=closing.close_block,
        prices=_quote_prices(quote, pools, readings),
        base_fee_wei=closing.base_fee_wei,
        suspect=bool(suspect_causes(pools, readings, flags)),
    )
