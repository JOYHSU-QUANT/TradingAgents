"""Compatibility path: the clocks moved to :mod:`..runtime.clock` (refactor plan v2, T1).

The :class:`~..ports.Clock` protocol sits in ``ports`` beside the package's
other seams; the two implementations sit in ``runtime``. Every in-package
importer already uses those paths — this module exists for one PR, until
the accounting split (plan PR 3) deletes it.
"""

from __future__ import annotations

from ..ports import Clock
from ..runtime.clock import ManualClock, WallClock

__all__ = ["Clock", "ManualClock", "WallClock"]
