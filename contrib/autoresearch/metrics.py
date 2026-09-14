"""What one window said, as a record — the shape a ledger row stores and a report prints.

A :class:`~contrib.autoresearch.evaluator.SegmentResult` carries everything a
measurement produced, its bar returns and its trades included. A ledger row
should not: a thousand trials of bar returns is hundreds of megabytes, and the
trades are recomputable from the spec and the store. What a later reader needs
— ``report``, the promote gate, the hypothesis loop's failure summary — is the
figures, so this module is the one place they are written down, read back and
rendered.

ONE renderer, used by both sides. ``evaluate`` prints a result it has just
computed and ``report`` prints the same result read back from the ledger a
week later; if those were two pieces of formatting code, a reader comparing
the two outputs would be comparing formatting. So
:meth:`SegmentResult.describe` and :func:`describe_result` delegate here.

Deliberately light on imports. ``report`` reads the ledger and nothing else,
and this module must not pull in the feature stack (pandas, stockstats) that
COMPUTING a result needs — ``tests/test_upstream.py`` runs ``report`` in a
subprocess to hold that.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from typing import TYPE_CHECKING

from .costs import CostModel
from .dsl import StrategySpec, describe_spec
from .split import Segment, SegmentName, Split
from .vocabulary import SpecError, require_number

if TYPE_CHECKING:  # pragma: no cover - annotations only; importing it would load pandas
    from .evaluator import SegmentResult

__all__ = [
    "RegimeBucket",
    "SegmentMetrics",
    "Tally",
    "describe_measurement",
]


@dataclass(frozen=True)
class Tally:
    """The four statistics computed once GROSS and once NET (plan §3.9).

    Both are built from per-bar returns on the SAME denominator — the equity
    actually deployed, which is the net path — so the gross figures are "the
    price move, on the capital the strategy really had", compounded. A
    strategy that has lost half its equity to costs and then makes a bar's
    move of one hundredth of its starting stake is booked as a 2% gross bar,
    not 1%: that is the return the position earned, and it is why
    ``gross.total_return`` is not the plain sum of the trades' ``gross_pnl``.
    """

    total_return: float
    sharpe: float
    max_drawdown: float
    hit_rate: float

    def describe(self, label: str) -> str:
        return (
            f"{label}: return {self.total_return:+.2%}, sharpe {self.sharpe:.2f}, "
            f"max drawdown {self.max_drawdown:.2%}, hit rate {self.hit_rate:.0%}"
        )


@dataclass(frozen=True)
class RegimeBucket:
    """Net bar returns summed over the bars carrying one regime label (plan §3.9)."""

    label: str
    bars: int
    net_return: float


@dataclass(frozen=True)
class SegmentMetrics:
    """The figures of one :class:`SegmentResult`, without the bar series and the trades.

    ``trades`` is the COUNT. Everything else is the result's own field under
    its own name, so a reader who knows one knows the other; ``ruined`` keeps
    the reading the result documents — rank or filter on it before any ratio.
    """

    segment: Segment
    bars: int
    bars_per_year: float
    gross: Tally
    net: Tally
    trades: int
    exposure: float
    turnover: float
    fees_paid: float
    slippage_paid: float
    funding_paid: float
    bars_unevaluable: int
    bars_conflicting: int
    funding_settlements_missing: int
    regime_buckets: tuple[RegimeBucket, ...]
    ruined: bool

    @classmethod
    def from_result(cls, result: SegmentResult) -> SegmentMetrics:
        return cls(
            segment=result.segment,
            bars=result.bars,
            bars_per_year=result.bars_per_year,
            gross=result.gross,
            net=result.net,
            trades=len(result.trades),
            exposure=result.exposure,
            turnover=result.turnover,
            fees_paid=result.fees_paid,
            slippage_paid=result.slippage_paid,
            funding_paid=result.funding_paid,
            bars_unevaluable=result.bars_unevaluable,
            bars_conflicting=result.bars_conflicting,
            funding_settlements_missing=result.funding_settlements_missing,
            regime_buckets=result.regime_buckets,
            ruined=result.ruined,
        )

    def describe(self) -> list[str]:
        lines = [
            f"{self.segment}: {self.bars} bars, {self.trades} trades, "
            f"exposure {self.exposure:.0%}, turnover {self.turnover:.2f}x"
            + (" — RUINED" if self.ruined else ""),
            "  " + self.gross.describe("gross"),
            "  " + self.net.describe("net  "),
            f"  costs: fees {self.fees_paid:.4f}, slippage {self.slippage_paid:.4f}, "
            f"funding {self.funding_paid:+.4f} (positive = paid)",
        ]
        if self.regime_buckets:
            lines.append(
                "  by regime (net bar returns summed): "
                + ", ".join(
                    f"{bucket.label} {bucket.net_return:+.2%} over {bucket.bars} bars"
                    for bucket in self.regime_buckets
                )
            )
        notes = []
        if self.bars_unevaluable:
            notes.append(f"{self.bars_unevaluable} bars where a consulted rule had no value")
        if self.bars_conflicting:
            notes.append(f"{self.bars_conflicting} bars where long and short both fired")
        if self.funding_settlements_missing:
            notes.append(f"{self.funding_settlements_missing} funding settlements missing")
        if notes:
            lines.append("  note: " + "; ".join(notes))
        return lines

    # -- the ledger record -------------------------------------------------

    def to_dict(self) -> dict[str, object]:
        """The record a trial row writes (plan §3.3 ``*_metrics_json``)."""
        body: dict[str, object] = {field.name: getattr(self, field.name) for field in fields(self)}
        body["segment"] = {
            "name": self.segment.name.value,
            "start_ms": self.segment.start_ms,
            "end_ms": self.segment.end_ms,
        }
        body["gross"] = asdict(self.gross)
        body["net"] = asdict(self.net)
        body["regime_buckets"] = [asdict(bucket) for bucket in self.regime_buckets]
        return body

    @classmethod
    def from_dict(cls, payload: object) -> SegmentMetrics:
        """The inverse of :meth:`to_dict`: exactly its keys at every level, every value checked.

        A missing key is refused rather than defaulted, as the cost record's
        is — a metrics record that lost ``ruined`` would otherwise read back
        as a run that was not ruined, and a promote gate reading it would pass
        the one trial it exists to stop. ``ValueError`` names the field; the
        ledger names the trial.
        """
        body = _exact(payload, {field.name for field in fields(cls)}, "metrics")
        segment = _exact(body["segment"], {"name", "start_ms", "end_ms"}, "metrics.segment")
        try:
            name = SegmentName(segment["name"])
        except ValueError as exc:
            raise ValueError(f"metrics.segment.name: {exc}") from exc
        buckets = body["regime_buckets"]
        if not isinstance(buckets, list):
            raise ValueError(f"metrics.regime_buckets: expected a list, got {buckets!r}")
        return cls(
            segment=Segment(name, segment["start_ms"], segment["end_ms"]),
            bars=_count(body["bars"], "metrics.bars"),
            bars_per_year=_number(body["bars_per_year"], "metrics.bars_per_year"),
            gross=_tally(body["gross"], "metrics.gross"),
            net=_tally(body["net"], "metrics.net"),
            trades=_count(body["trades"], "metrics.trades"),
            exposure=_number(body["exposure"], "metrics.exposure"),
            turnover=_number(body["turnover"], "metrics.turnover"),
            fees_paid=_number(body["fees_paid"], "metrics.fees_paid"),
            slippage_paid=_number(body["slippage_paid"], "metrics.slippage_paid"),
            funding_paid=_number(body["funding_paid"], "metrics.funding_paid"),
            bars_unevaluable=_count(body["bars_unevaluable"], "metrics.bars_unevaluable"),
            bars_conflicting=_count(body["bars_conflicting"], "metrics.bars_conflicting"),
            funding_settlements_missing=_count(
                body["funding_settlements_missing"], "metrics.funding_settlements_missing"
            ),
            regime_buckets=tuple(
                _bucket(bucket, f"metrics.regime_buckets[{index}]")
                for index, bucket in enumerate(buckets)
            ),
            ruined=_flag(body["ruined"], "metrics.ruined"),
        )


def describe_measurement(
    spec: StrategySpec,
    costs: CostModel,
    split: Split,
    indicator_lookback: int,
    segments: Sequence[SegmentMetrics],
    *,
    withheld_because: str = "not promoted",
) -> list[str]:
    """A measured spec as lines to print: what was scored, under what, and what it scored.

    Every parameter a number depends on is printed above the numbers — the
    cost model, the indicator window, the annualisation, the split — because a
    Sharpe with those left implicit is a number nobody can reproduce. A window
    of the split that was not measured is named as withheld, and why:
    ``withheld_because`` is "not promoted" unless the caller withholds figures
    that exist (a search view of a promoted trial must not say it was not).
    """
    lines = list(describe_spec(spec))
    lines.append(costs.describe())
    if segments:
        lines.append(
            f"indicator window: {indicator_lookback} bars; sharpe annualised by "
            f"sqrt({segments[0].bars_per_year:.0f} bars/year)"
        )
    lines += split.describe()
    for segment in segments:
        lines += segment.describe()
    measured = {segment.segment.name for segment in segments}
    for withheld in split.ordered:
        if withheld.name not in measured:
            lines.append(f"{withheld.name.value}: withheld ({withheld_because})")
    return lines


# -- reading a record back ---------------------------------------------------


def _exact(payload: object, keys: set[str], path: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping) or set(payload) != keys:
        shown = sorted(payload) if isinstance(payload, Mapping) else payload
        raise ValueError(f"{path}: expected exactly the keys {sorted(keys)}, got {shown!r}")
    return payload


def _number(value: object, path: str) -> float:
    try:
        return require_number(value, path)
    except SpecError as exc:
        raise ValueError(str(exc)) from exc


def _count(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{path}: expected a count (an int >= 0), got {value!r}")
    return value


def _flag(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{path}: expected true or false, got {value!r}")
    return value


def _tally(payload: object, path: str) -> Tally:
    body = _exact(payload, {field.name for field in fields(Tally)}, path)
    return Tally(**{name: _number(body[name], f"{path}.{name}") for name in body})


def _bucket(payload: object, path: str) -> RegimeBucket:
    body = _exact(payload, {field.name for field in fields(RegimeBucket)}, path)
    if not isinstance(body["label"], str):
        raise ValueError(f"{path}.label: expected a string, got {body['label']!r}")
    return RegimeBucket(
        label=body["label"],
        bars=_count(body["bars"], f"{path}.bars"),
        net_return=_number(body["net_return"], f"{path}.net_return"),
    )
