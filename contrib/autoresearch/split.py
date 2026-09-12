"""The fixed train / validation / holdout split, and the lock on the last of them.

Plan §3.8: the split is by calendar instant, written down once per experiment
(``split_json``), and the holdout is the NEWEST stretch. It is a value here
rather than three ``--from``/``--to`` pairs on a command line because the
split is part of what a metric MEANS — a validation Sharpe is a number over
one named window — and a window typed by hand for each run is a window that
drifts between runs.

A segment is a half-open interval ``[start_ms, end_ms)`` over a bar's
``open_time``: a bar belongs to the segment it OPENS in. Half-open so the
three tile the span with nothing shared and nothing dropped, and on
``open_time`` because that is the stamp the store keys a bar by and the one
the gap scan measures on.

The holdout lock lives at this seam. :meth:`Split.segments` hands back the
train and validation windows and NOT the holdout unless it is asked for by
name, and :meth:`Split.loadable_until` says how far a bundle may be read from
the store for an unpromoted trial — the store's own window bound then keeps
the holdout ROWS out of memory, not merely out of the report. "It did not ask
for them" is a property a test can check; "it computed them and did not print
them" is not.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .constants import STUDIED_INTERVALS
from .upstream import VocabEnum, from_epoch_ms, interval_to_ms, parse_interval

__all__ = [
    "DEFAULT_TRAIN_SHARE",
    "DEFAULT_VALIDATION_SHARE",
    "Segment",
    "SegmentName",
    "Split",
    "SplitError",
    "studied_interval",
]

# Plan §3.8's suggestion: 60 / 20 / 20, holdout newest.
DEFAULT_TRAIN_SHARE: Final = 0.6
DEFAULT_VALIDATION_SHARE: Final = 0.2


class SplitError(ValueError):
    """A split that does not describe three ordered, non-empty windows."""


def studied_interval(interval: str) -> str:
    """``interval`` in its canonical spelling, if it is one this package studies.

    The venue's own vocabulary is wider (it has ``1h``, ``15m``), and a
    window over one of those would be legal to the venue and meaningless to
    this package — see :data:`~contrib.autoresearch.constants.STUDIED_INTERVALS`.
    Checked here rather than left to the CLI's ``choices``, because a split
    is also built from a ledger record and by code.
    """
    try:
        key = parse_interval(interval).value
    except ValueError as exc:
        # The venue enum's own sentence, re-raised as this module's: a ledger
        # record with a malformed interval is a bad RECORD, and the loader
        # that reads one catches SplitError to say so.
        raise SplitError(f"interval: {exc}") from exc
    if key not in STUDIED_INTERVALS:
        raise SplitError(
            f"this package studies {list(STUDIED_INTERVALS)} candles, not {key!r}; a window "
            f"over another interval would be legal to the venue and meaningless here"
        )
    return key


# The studied intervals are venue spellings, checked once here — the module
# that imports both — so a stale literal in ``constants`` fails at import
# rather than as a refusal at the first split built on it.
if any(parse_interval(interval).value != interval for interval in STUDIED_INTERVALS):
    raise RuntimeError("STUDIED_INTERVALS must be spelled the way the venue spells them")


class SegmentName(VocabEnum, noun="split segment"):
    """The three windows, in the order they sit on the calendar."""

    TRAIN = "train"
    VALIDATION = "validation"
    HOLDOUT = "holdout"


@dataclass(frozen=True)
class Segment:
    """One measured window: the bars that OPEN at or after ``start_ms`` and before ``end_ms``."""

    name: SegmentName
    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "name", SegmentName(self.name))
        except ValueError as exc:
            raise SplitError(str(exc)) from exc
        for field in ("start_ms", "end_ms"):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise SplitError(
                    f"{self.name.value}.{field}: a segment edge is epoch ms, got {value!r}"
                )
        if self.end_ms <= self.start_ms:
            raise SplitError(
                f"{self.name.value}: ends ({from_epoch_ms(self.end_ms).isoformat()}) at or "
                f"before it starts ({from_epoch_ms(self.start_ms).isoformat()})"
            )

    def bar_range(self, open_times: Sequence[int]) -> tuple[int, int]:
        """``(first, stop)`` indices into an ascending ``open_times`` — the segment's bars."""
        return bisect_left(open_times, self.start_ms), bisect_left(open_times, self.end_ms)

    def __str__(self) -> str:
        return (
            f"{self.name.value}: {from_epoch_ms(self.start_ms):%Y-%m-%d %H:%M} .. "
            f"{from_epoch_ms(self.end_ms):%Y-%m-%d %H:%M}"
        )


@dataclass(frozen=True)
class Split:
    """Three consecutive segments over one candle interval, holdout last.

    ``interval`` is carried because two things the evaluator does depend on
    it — the annualisation of a per-bar statistic and the grid a segment is
    checked for holes against — and neither can be recovered from the bars
    themselves without assuming the very regularity being checked.
    """

    interval: str
    train: Segment
    validation: Segment
    holdout: Segment

    def __post_init__(self) -> None:
        object.__setattr__(self, "interval", studied_interval(self.interval))
        for segment, expected in zip(self.ordered, SegmentName, strict=True):
            if segment.name is not expected:
                raise SplitError(f"the {expected.value} segment is named {segment.name.value!r}")
        for earlier, later in zip(self.ordered, self.ordered[1:], strict=False):
            if later.start_ms != earlier.end_ms:
                raise SplitError(
                    f"{later.name.value} must begin where {earlier.name.value} ends: "
                    f"{from_epoch_ms(earlier.end_ms).isoformat()} vs "
                    f"{from_epoch_ms(later.start_ms).isoformat()}. The three segments tile "
                    f"the span — a gap between them is history no window measures, an "
                    f"overlap is history two windows count."
                )

    @property
    def ordered(self) -> tuple[Segment, Segment, Segment]:
        return (self.train, self.validation, self.holdout)

    def segments(self, *, holdout: bool = False) -> tuple[Segment, ...]:
        """The windows a trial is measured on. The holdout only when asked for BY NAME.

        The default is the lock (plan §3.8): a trial that has not been
        promoted is measured on train and validation and nothing else, and the
        caller that measures the holdout has to say so at the call site, where
        a reviewer can see it.
        """
        return self.ordered if holdout else self.ordered[:2]

    def loadable_until(self, *, holdout: bool = False) -> int:
        """The newest ``open_time`` a bundle may hold for this measurement, inclusive.

        Handed to the store's ``until_ms`` (inclusive) so the rows past it are
        never read. The last bar a window needs is the one that OPENS just
        before its end: a position still open at that bar's close is flattened
        there rather than filled at the next open, precisely so a window never
        reads a bar belonging to the next one.
        """
        last = self.holdout if holdout else self.validation
        return last.end_ms - 1

    @classmethod
    def by_shares(
        cls,
        interval: str,
        *,
        start_ms: int,
        end_ms: int,
        train_share: float = DEFAULT_TRAIN_SHARE,
        validation_share: float = DEFAULT_VALIDATION_SHARE,
    ) -> Split:
        """Cut ``[start_ms, end_ms)`` into three by share of its SPAN, snapped to the grid.

        Snapped to whole intervals from ``start_ms`` so a boundary never falls
        inside a bar — a bar that opens before a boundary and closes after it
        would otherwise belong to one segment by this module's rule and be
        argued into the other by a reader thinking in close times.
        """
        key = studied_interval(interval)
        step = interval_to_ms(key)
        for name, share in (("train_share", train_share), ("validation_share", validation_share)):
            if isinstance(share, bool) or not isinstance(share, (int, float)) or not 0 < share < 1:
                raise SplitError(
                    f"{name} is a share of the span strictly between 0 and 1, got {share!r}"
                )
        if train_share + validation_share >= 1:
            raise SplitError(
                f"train_share + validation_share must leave room for a holdout, got "
                f"{train_share:g} + {validation_share:g}"
            )
        if end_ms <= start_ms:
            raise SplitError("the split's span ends at or before it starts")
        span = end_ms - start_ms
        first_cut = start_ms + step * int(span * train_share // step)
        second_cut = start_ms + step * int(span * (train_share + validation_share) // step)
        return cls(
            interval=key,
            train=Segment(SegmentName.TRAIN, start_ms, first_cut),
            validation=Segment(SegmentName.VALIDATION, first_cut, second_cut),
            holdout=Segment(SegmentName.HOLDOUT, second_cut, end_ms),
        )

    def to_dict(self) -> dict[str, object]:
        """The record an experiment ledger writes (plan §3.3 ``split_json``)."""
        return {
            "interval": self.interval,
            **{
                segment.name.value: {"start_ms": segment.start_ms, "end_ms": segment.end_ms}
                for segment in self.ordered
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Split:
        expected = {"interval", *(name.value for name in SegmentName)}
        if set(payload) != expected:
            raise SplitError(
                f"a split record has exactly the keys {sorted(expected)}, got {sorted(payload)}"
            )
        segments = {}
        for name in SegmentName:
            body = payload[name.value]
            if not isinstance(body, dict) or set(body) != {"start_ms", "end_ms"}:
                raise SplitError(f"{name.value}: expected {{start_ms, end_ms}}, got {body!r}")
            segments[name.value] = Segment(name, body["start_ms"], body["end_ms"])
        return cls(interval=str(payload["interval"]), **segments)

    def describe(self) -> list[str]:
        return [f"split ({self.interval}): {segment}" for segment in self.ordered]
