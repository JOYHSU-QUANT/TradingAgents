"""The :class:`PerpTradeDecision` — the Phase 1 rating adapter's output contract.

A ``PerpTradeDecision`` describes **intent and direction**, never a concrete
order (see ``docs/DESIGN.md`` part 2). This is Phase 1 legacy: the rating adapter
that mapped the engine's 5-tier rating (together with the live
:class:`PerpMarketContext` and :class:`PerpPosition`) into this schema was retired
in the Phase 2 contract migration. The type is retained only so the audit log can
still read older Phase 1 records; the live Phase 2 path uses
:class:`~.target_decision.TargetDecision` and the RiskGate instead.

Kept as plain dataclasses + enums (no pydantic) to match the rest of the perp
domain. :meth:`PerpTradeDecision.to_dict` produces a JSON-ready dict for the
audit log, with every enum flattened to its string value.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from .schema import MarketRegime  # re-exported: the decision carries the context's regime

# Explicit public surface. ``MarketRegime`` is re-exported from ``schema`` and
# listed here so it is a deliberate public name of this module (the decision
# carries the regime), not an implicit leak of an import.
__all__ = [
    "Intent",
    "Urgency",
    "FundingView",
    "EntryZone",
    "PerpTradeDecision",
    "MarketRegime",
]


class Intent(str, Enum):
    """The core decision — one of five, no fuzzy answers."""

    HOLD = "hold"
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    REDUCE = "reduce"
    CLOSE = "close"


class Urgency(str, Enum):
    """Lets the OrderPlanner choose limit vs market (Phase 2+)."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FundingView(str, Enum):
    """Makes the funding judgement explicit rather than leaving raw numbers."""

    FAVORABLE = "favorable"
    NEUTRAL = "neutral"
    HEADWIND = "headwind"


@dataclass(frozen=True)
class EntryZone:
    """Suggested entry range; ``null`` decision-side means "around market"."""

    low: Decimal
    high: Decimal

    def __post_init__(self) -> None:
        # The type advertises an ordered range; an inverted zone (low > high) is a
        # structurally invalid entry band that downstream order placement would read
        # backwards. Reject it at construction rather than serialize it silently.
        # Prices are strictly positive; a non-positive ``low`` (and hence an inverted
        # negative-price band) is a corrupt parse that order placement would read as a
        # real entry. ``_parse_price`` already rejects these upstream — enforce it on the
        # type too. ``high > 0`` then follows from ``low > 0`` and ``low <= high``.
        if self.low <= 0:
            raise ValueError(f"EntryZone.low must be > 0, got {self.low}")
        if self.low > self.high:
            raise ValueError(f"EntryZone.low ({self.low}) must be <= high ({self.high})")

    def to_dict(self) -> dict[str, str]:
        # Serialize as strings to preserve full Decimal precision end-to-end: a
        # sub-cent-priced coin loses significant digits through float(), so the audit
        # log / Phase 2 read exchange-native precision rather than a lossy float.
        return {"low": str(self.low), "high": str(self.high)}


@dataclass(frozen=True)
class PerpTradeDecision:
    """Intent + direction produced by the decision adapter.

    ``target_size_pct`` is an **unsigned** magnitude *target* as a percent of
    account net value (0 = flat, ``None`` = "not applicable", e.g. a plain
    ``hold``); the direction is carried by ``intent`` (``OPEN_LONG`` vs
    ``OPEN_SHORT``), never by this number's sign. The real size is computed
    downstream by RiskGate + OrderPlanner. ``entry_zone`` and
    ``invalidation_price`` are references, not hard commands.
    """

    intent: Intent
    confidence: float
    target_size_pct: float | None
    entry_zone: EntryZone | None
    invalidation_price: Decimal | None
    urgency: Urgency
    rationale: str
    key_risks: tuple[str, ...]
    market_regime: MarketRegime
    funding_view: FundingView

    def __post_init__(self) -> None:
        # ``confidence`` is a 0-1 probability (DESIGN part 2); RiskGate gates orders
        # on it (e.g. < 0.6 -> hold). A misconfigured tier value outside [0, 1] would
        # silently corrupt that gate, so reject it at construction.
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")
        # ``target_size_pct`` is an *unsigned* magnitude (direction lives in ``intent``).
        # A negative value is a sign error that would mis-size the order downstream;
        # the adapter always produces ``abs(...)``, so reject anything else here.
        if self.target_size_pct is not None and self.target_size_pct < 0:
            raise ValueError(
                f"target_size_pct must be >= 0 (unsigned magnitude), got {self.target_size_pct}"
            )
        # Cross-field invariant: an OPEN intent must carry a positive size — an
        # ``OPEN_LONG``/``OPEN_SHORT`` with target_size_pct of 0 or None is a degenerate
        # "open nothing" that Phase 2 would have to special-case as a silent no-op. The
        # adapter never emits one (``rebalance`` only opens with ``abs(t) > 0``), so make
        # the contract explicit here rather than leave a constructable nonsense decision.
        if self.intent in (Intent.OPEN_LONG, Intent.OPEN_SHORT) and (
            self.target_size_pct is None or self.target_size_pct == 0
        ):
            raise ValueError(
                f"intent={self.intent.value} requires target_size_pct > 0, "
                f"got {self.target_size_pct}"
            )
        # ``frozen=True`` blocks reassignment but not in-place mutation of a list
        # value, so a caller passing a list would hold a mutable object behind a
        # ``tuple`` annotation. Coerce to honor the immutability contract.
        object.__setattr__(self, "key_risks", tuple(self.key_risks))

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict — enums flattened to their string values."""
        return {
            "intent": self.intent.value,
            "confidence": self.confidence,
            "target_size_pct": self.target_size_pct,
            "entry_zone": self.entry_zone.to_dict() if self.entry_zone else None,
            "invalidation_price": (
                str(self.invalidation_price) if self.invalidation_price is not None else None
            ),
            "urgency": self.urgency.value,
            "rationale": self.rationale,
            "key_risks": list(self.key_risks),
            "market_regime": self.market_regime.value,
            "funding_view": self.funding_view.value,
        }
