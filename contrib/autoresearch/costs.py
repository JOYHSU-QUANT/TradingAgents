"""What a fill and an hour of holding cost — the parameters every trial is measured under.

Plan §3.7: a research number is only comparable with another research number
if both were computed under the SAME cost assumptions, and only comparable
with the paper trader if those assumptions are the trader's own. So the cost
model is one frozen value, written into every experiment's record (plan §3.3,
``cost_params_json``) and printed at the head of every report, rather than a
set of defaults read from wherever the evaluator happened to be run.

The defaults are the live paper run's: a taker fee of 0.045%, five basis
points of adverse slippage on every fill, and the configured leverage of 1.
They are written as numbers here rather than imported, for the reason
``dsl.LIVE_MARGIN_CAP`` gives — reaching into the paper package for three
constants would put its import cost on every store command — and pinned
against those config defaults in ``tests/test_pins.py`` so the two cannot
drift without a red test.

The maker lane exists because the roadmap changes the live order type at
paper run 5 (plan §2). Switching ``fill_role`` changes the fee rate and
NOTHING else on purpose: a resting order that fills has no adverse slippage
by construction, but it also may not fill at all, and a research model that
zeroed slippage would be pricing in the fill and ignoring the miss. Slippage
under a maker assumption is the operator's number to set, stated beside the
fee it goes with, and every promoted strategy is re-measured under it before
the bridge opens (plan §2, run 5 connection 1).

Leverage is here because sizing is written in MARGIN — ``fixed_margin_fraction``
is a share of equity committed as margin, the same quantity the live decision
schema carries as ``requested_target_margin_pct`` — and the notional a margin
buys is ``margin × leverage``. Under the live default of 1 the two are the
same number, which is exactly the kind of coincidence a parameter exists to
stop being assumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final

from .upstream import VocabEnum
from .vocabulary import SpecError, require_number

__all__ = [
    "LIVE_LEVERAGE",
    "LIVE_SLIPPAGE_BPS",
    "LIVE_TAKER_FEE_RATE",
    "VENUE_BASE_MAKER_FEE_RATE",
    "CostModel",
    "FillRole",
]

# The paper run's own execution assumptions (``PaperExecutionConfig``,
# ``FillModelConfig`` and ``RiskConfig`` defaults), pinned in
# ``tests/test_pins.py``.
LIVE_TAKER_FEE_RATE: Final = 0.00045
LIVE_SLIPPAGE_BPS: Final = 5.0
LIVE_LEVERAGE: Final = 1.0

# The venue's published base-tier maker rate. Not a live number — the paper
# run does not place maker orders yet — so it is not pinned against anything
# in the perp package; it is the starting point for the run-5 re-measurement.
VENUE_BASE_MAKER_FEE_RATE: Final = 0.00015

_BPS: Final = 10_000.0


def _cost(value: object, name: str, *, positive: bool = False) -> float:
    """A cost field: a finite number that is not negative (and not zero, if ``positive``).

    Through the vocabulary's one numeric guard, then re-raised as a plain
    ``ValueError`` naming the field: ``SpecError`` means "the hypothesis said
    something the language does not contain", and a bad fee is not that.
    """
    try:
        number = require_number(value, f"CostModel.{name}")
    except SpecError as exc:
        raise ValueError(str(exc)) from exc
    if number < 0 or (positive and number == 0):
        bound = "> 0" if positive else ">= 0"
        raise ValueError(f"CostModel.{name} must be a number {bound}, got {value!r}")
    return number


class FillRole(VocabEnum, noun="fill role"):
    """Which side of the book a fill is assumed to take, and therefore which fee."""

    TAKER = "taker"
    MAKER = "maker"


@dataclass(frozen=True)
class CostModel:
    """One set of execution assumptions. Frozen, because a trial is measured under it.

    Every figure is a plain float rather than a ``Decimal``: the evaluator's
    arithmetic is float throughout (its inputs are feature values, which are
    floats), and a research metric is a statistic, not a ledger entry that
    has to replay bit for bit.
    """

    taker_fee_rate: float = LIVE_TAKER_FEE_RATE
    maker_fee_rate: float = VENUE_BASE_MAKER_FEE_RATE
    slippage_bps: float = LIVE_SLIPPAGE_BPS
    fill_role: FillRole = FillRole.TAKER
    leverage: float = LIVE_LEVERAGE

    def __post_init__(self) -> None:
        object.__setattr__(self, "fill_role", FillRole(self.fill_role))
        for name in ("taker_fee_rate", "maker_fee_rate", "slippage_bps"):
            object.__setattr__(self, name, _cost(getattr(self, name), name))
        object.__setattr__(self, "leverage", _cost(self.leverage, "leverage", positive=True))

    @property
    def fee_rate(self) -> float:
        """The fee a fill pays under ``fill_role``, as a fraction of its notional."""
        return self.taker_fee_rate if self.fill_role is FillRole.TAKER else self.maker_fee_rate

    @property
    def slippage_rate(self) -> float:
        """Slippage as a fraction of notional — the same quantity the fee is."""
        return self.slippage_bps / _BPS

    def fill_cost(self, notional: float) -> tuple[float, float]:
        """``(fee, slippage)`` paid on one fill of ``notional``, both non-negative.

        Charged on the MID notional. The live fill model prices the fill at
        mid ± slippage and books the fee on that price; here the price move
        is kept gross (mid to mid) so the two costs can be reported apart
        from it, and the difference between fee-on-mid and fee-on-slipped-mid
        is the fee rate times the slippage rate — five parts in a hundred
        million of notional under the defaults.
        """
        notional = abs(notional)
        return notional * self.fee_rate, notional * self.slippage_rate

    def describe(self) -> str:
        """One line, for the head of a report — every number the metrics depend on."""
        return (
            f"costs: {self.fill_role.value} fills at {self.fee_rate:g} fee + "
            f"{self.slippage_bps:g} bps slippage, funding settled hourly, "
            f"leverage {self.leverage:g}"
        )

    def to_dict(self) -> dict[str, float | str]:
        """The record an experiment ledger writes (plan §3.3 ``cost_params_json``).

        ``asdict`` rather than a hand-kept key list, so a field added later
        (the maker-side slippage the module docstring anticipates) reaches the
        ledger the day it is added; :meth:`from_dict` is driven by the same
        field table, and the two only stay symmetric if neither is a copy.
        """
        return {**asdict(self), "fill_role": self.fill_role.value}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CostModel:
        """The inverse of :meth:`to_dict`: exactly the model's fields, all of them.

        A missing key is refused, not defaulted. A record that lost its
        ``fill_role`` column would otherwise read back as the live taker
        model, and a trial measured under it would be filed as measured
        under the defaults.
        """
        fields = set(cls.__dataclass_fields__)
        if set(payload) != fields:
            raise ValueError(
                f"a cost record has exactly the keys {sorted(fields)}, got {sorted(payload)}"
            )
        return cls(**payload)  # type: ignore[arg-type]
