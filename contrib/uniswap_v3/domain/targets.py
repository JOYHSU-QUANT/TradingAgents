"""A target: the share of the portfolio an outside coordinator said to hold in one token at one bar.

A target is what the carry coordinator (``contrib/carry``) hands this leg:
not a view on the token but a hedge ratio, the share of the spot run's
value that matches the perp leg's notional at a UTC day boundary. It is
recorded by the ``target`` command from the coordinator's handoff file and
read back later by whoever builds a :class:`~.types.MarketView`; a strategy
reads the :class:`Target` and nothing else of it. It belongs to no run, as
a bar and a verdict do: one target per (source, token, bar) serves every
run that reads that source. It is never rewritten: the handoff is
overwritten every day, so the row is the one record of what this leg was
told at that bar.

:class:`Target` is the part a strategy may see. :class:`TargetRecord` is
the stored row: the target, when it was written, and the digest of the
handoff document's bytes it was read from, which is how the row is matched
to what the perp leg read of the same document.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from .verdicts import require_count, require_digest, require_source, require_text

__all__ = ["DAY_SECONDS", "Target", "TargetRecord", "TargetSettings", "require_weight"]

# A target is for a UTC day boundary, and stands for that day: the coordinator
# decides once a day (carry plan D4), so a run that reads targets takes daily
# bars, and a handoff's boundary is a multiple of this.
DAY_SECONDS: Final = 86_400


def require_weight(value: object, what: str) -> None:
    """Refuse a ``value`` that is not a finite ``Decimal`` in ``[0, 1]``; a negative zero is refused too."""
    if not isinstance(value, Decimal) or not value.is_finite() or value.is_signed() or value > 1:
        raise ValueError(f"{what} must be a Decimal in [0, 1], got {value!r}")


@dataclass(frozen=True)
class Target:
    """What ``source`` said to hold of ``symbol`` at the bar whose boundary is ``time``.

    ``weight`` is the share of portfolio value, in ``[0, 1]``, as the
    coordinator wrote it: this leg checks the range and does not size it
    again (carry plan R3).
    """

    source: str
    symbol: str
    time: int
    weight: Decimal

    def __post_init__(self) -> None:
        require_source(self.source)
        require_text(self.symbol, "symbol")
        require_count(self.time, "time")
        require_weight(self.weight, "weight")


@dataclass(frozen=True)
class TargetRecord:
    """A target as it is stored: with when it was written, and the digest of the document it came from.

    ``written_at`` is when the ``target`` command wrote the row, in epoch
    seconds. ``digest`` is the SHA-256 of the handoff file's bytes, as 64
    hex digits (:func:`~.verdicts.bytes_digest`), the one fact about the
    file as written that survives the coordinator's daily overwrite.
    """

    target: Target
    written_at: int
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.target, Target):
            raise ValueError(f"target must be a Target, got {self.target!r}")
        require_count(self.written_at, "written_at")
        require_digest(self.digest, "digest")


@dataclass(frozen=True)
class TargetSettings:
    """Which targets a run reads: those of one ``source``.

    A config without the section reads none, and its view carries none.
    """

    source: str

    def __post_init__(self) -> None:
        require_source(self.source)
