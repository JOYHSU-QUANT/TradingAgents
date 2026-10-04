"""The engine's bars, built from the store's per-pool readings.

:func:`load_bar` reads the configured pools' readings at one boundary, runs
the data checks on each against the reading before it, and assembles the
:class:`~..domain.types.Bar`. A boundary that lacks the reading of any
configured pool has no bar.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..config import UniswapConfig
from ..constants import pool_key
from ..domain.bars import (
    BarFlag,
    Finality,
    PoolBar,
    SuspectCause,
    assemble_bar,
    pool_bar_flags,
    suspect_causes,
)
from ..domain.records import BarSeen, SkipCode, Suspicion
from ..domain.types import Bar, Pool
from .repository import Store, StoreError

__all__ = ["StoreBarSource", "StoredBar", "load_bar"]

# The furthest from final first.
_FINALITY_ORDER = (Finality.REORGED, Finality.PENDING, Finality.FINAL)


@dataclass(frozen=True)
class StoredBar:
    """A bar, with what it was built from: a reading and its flags per configured pool, in order.

    ``suspicion`` says why the bar is suspect, and is ``None`` for a bar that is not.
    """

    bar: Bar
    readings: tuple[PoolBar, ...]
    flags: tuple[frozenset[BarFlag], ...]
    suspicion: Suspicion | None = None

    @property
    def finality(self) -> Finality:
        """The bar's finality: that of its reading furthest from final."""
        return min(
            (reading.finality for reading in self.readings), key=_FINALITY_ORDER.index
        )

    @property
    def seen(self) -> BarSeen:
        """What a decision on this bar keeps of it: the close block, its hash, and the finality.

        The hash is that of the reading the bar takes its close block from,
        the highest when the readings do not agree.
        """
        closing = max(self.readings, key=lambda reading: reading.close_block)
        return BarSeen(
            close_block=closing.close_block,
            close_block_hash=closing.close_block_hash,
            finality=self.finality,
        )


def _suspicion(
    pools: Sequence[Pool], readings: Sequence[PoolBar], flags: Sequence[frozenset[BarFlag]]
) -> Suspicion | None:
    """Why the bar of ``readings`` is suspect, in words, or ``None`` when it is not.

    The code is that of the gravest cause, and the reason names every one.
    """
    causes = suspect_causes(pools, readings, flags)
    if not causes:
        return None
    closes = ", ".join(str(block) for block in sorted({reading.close_block for reading in readings}))
    words = {
        SuspectCause.REORGED: "its close block is not on the final chain",
        SuspectCause.TWAP_DEVIATION: "its close price is further from its TWAP than the limit",
    }
    return Suspicion(
        code=SkipCode(causes[0][0].value),
        reason="; ".join(
            f"the pools' readings do not agree on the close block ({closes})"
            if pool is None
            else f"{pool_key(pool)}: {words[cause]}"
            for cause, pool in causes
        ),
    )


def load_bar(store: Store, config: UniswapConfig, time: int) -> StoredBar | None:
    """The bar at the boundary ``time``, or ``None`` when a configured pool has no reading there.

    A reading the data checks refuse, such as one whose TWAP was taken over
    another window than the config's, raises :class:`~.repository.StoreError`.
    """
    interval = config.bars.interval_seconds
    readings: list[PoolBar] = []
    flags: list[frozenset[BarFlag]] = []
    for pool in config.pools:
        reading = store.bar(config.chain_id, pool.address, interval, time)
        if reading is None:
            return None
        readings.append(reading)
        previous = store.previous_bar(config.chain_id, pool.address, interval, time)
        try:
            flags.append(pool_bar_flags(pool, reading, previous, config.bars))
        except ValueError as exc:
            raise StoreError(
                f"the stored reading of {pool_key(pool)} at {time} cannot be checked ({exc})"
            ) from exc
    try:
        bar = assemble_bar(config.quote, config.pools, readings, flags=flags)
    except ValueError as exc:
        raise StoreError(f"the stored readings at {time} do not make a bar ({exc})") from exc
    return StoredBar(
        bar=bar,
        readings=tuple(readings),
        flags=tuple(flags),
        suspicion=_suspicion(config.pools, readings, flags),
    )


class StoreBarSource:
    """A :class:`~..ports.BarSource` over stored readings; it never reads a chain."""

    def __init__(self, store: Store, config: UniswapConfig) -> None:
        self._store = store
        self._config = config

    def bar_at(self, time: int) -> Bar | None:
        """The bar whose boundary is ``time``, or ``None`` when the store does not hold all of it."""
        stored = load_bar(self._store, self._config, time)
        return None if stored is None else stored.bar
