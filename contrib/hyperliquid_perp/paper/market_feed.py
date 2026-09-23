"""Compatibility path: the snapshot providers moved to :mod:`..runtime.market_feed` (refactor plan v2, T1).

The :class:`~..ports.SnapshotProvider` protocol sits in ``ports`` beside the
package's other seams; the result types and the two providers sit in
``runtime``. Every in-package importer already uses those paths — this
module exists for one PR, until the accounting split (plan PR 3) deletes it.
"""

from __future__ import annotations

from ..ports import SnapshotProvider
from ..runtime.market_feed import (
    PortSnapshotProvider,
    PriceSnapshot,
    ScriptedSnapshotProvider,
    SnapshotOutcome,
    SnapshotResult,
)

__all__ = [
    "PortSnapshotProvider",
    "PriceSnapshot",
    "ScriptedSnapshotProvider",
    "SnapshotOutcome",
    "SnapshotProvider",
    "SnapshotResult",
]
