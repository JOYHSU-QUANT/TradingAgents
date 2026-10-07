"""The handoff document: what each leg should hold at a boundary, as one JSON file.

The ONLY thing this package hands the two venues (carry plan §3.1). The
perp leg's file-target provider (plan PR 2) and the spot leg's ``target``
command (plan PR 3) read it; neither imports this package, so the document
is the contract, and this module is its one writer and its one reader on
this side. The fields, every one required:

- ``version`` — :data:`HANDOFF_VERSION`, a whole number; a reader refuses
  any other. Fields may be ADDED under the same version, and a reader
  ignores keys it does not know; the version moves only when an existing
  field changes meaning, and then the readers deploy before the writer;
- ``coin`` — the perp coin (``ETH``); ``spot.token`` is its spot twin
  (:data:`SPOT_TOKENS`), carried explicitly so a reader need not know the map;
- ``as_of_ms`` — the boundary the targets are for, epoch milliseconds, a
  UTC day boundary. The staleness anchor: a reader acts on the document
  only while ``as_of_ms <= now < as_of_ms + one day``, and treats it as
  stale otherwise (plan §2 D5: both legs maintain, with a warning).
  ``as_of`` is the same instant spelled out for a human;
- ``written_at_ms`` / ``written_at`` — when the file was written.
  Informational: not the staleness anchor, because a coordinator that
  failed for a day leaves a file whose write time is fresh enough to pass
  and whose target is a day old;
- ``action`` and ``position`` — what the boundary did and the position
  after it; the position is also the coordinator's own memory: the next
  run reads the previous file to learn whether it is in (:func:`previous_handoff`);
- ``params`` — the rule the decision was made with (window, thresholds,
  hold, margin), so a rerun that changes them can say so and a later
  report can group by them;
- ``perp`` — ``side`` (``short`` while in, ``flat`` while out) and
  ``margin_pct`` (``params.margin_pct`` while in, 0 while out): the rule's
  INTENDED sizing, before the perp gate has spoken;
- ``spot`` — ``token`` and ``weight``, the share of the spot run's value
  to hold in the token, derived from ``margin_pct`` and the two equities
  (:func:`spot_weight`) and written out so a reader need not derive it. A
  reader checks only that it lies in [0, 1]; this module, as the writer,
  cross-checks it against the equities when it reads its own file back;
- ``signal`` — the reading the decision was made from, or ``null``;
- ``equity`` — the two equities the weight was sized from, each with the
  instant its store wrote the row (``perp_at_ms`` / ``spot_at_ms``), or
  ``null`` where no store was given.

Decimals travel as strings in plain notation so no digit is lost in a
float; the z-score and the thresholds are floats because that is what
they are.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Final

from .signal import Action, Params, Position, Reading, Side, SignalError, finite, whole
from .stores import Equity
from .upstream import atomic_write_text, from_epoch_ms

__all__ = [
    "HANDOFF_VERSION",
    "SPOT_TOKENS",
    "WEIGHT_PLACES",
    "Handoff",
    "HandoffError",
    "build",
    "iso_utc",
    "plain",
    "previous_handoff",
    "read_handoff",
    "spot_weight",
    "write_handoff",
]

HANDOFF_VERSION: Final = 1
# The perp coin and the spot token that hedges it; ETH first (plan §2 D3),
# BTC is the second run. The spot token must be one the spot package's
# constants name, under the same spelling.
SPOT_TOKENS: Final[Mapping[str, str]] = MappingProxyType({"BTC": "WBTC", "ETH": "WETH"})
# Weights are cut to four places, as the spot package's strategies cut theirs.
WEIGHT_PLACES: Final = Decimal("0.0001")


class HandoffError(ValueError):
    """A document that is not a handoff, or a handoff that contradicts itself."""


def iso_utc(ms: int) -> str:
    """An instant as the document spells it: ISO-8601 UTC to the second."""
    return from_epoch_ms(ms).isoformat(timespec="seconds")


def plain(value: Decimal) -> str:
    """A decimal in positional notation (``0.0000005``, never ``5E-7``)."""
    return format(value, "f")


def _decimal(value: object, what: str) -> Decimal:
    if not isinstance(value, str):
        raise HandoffError(f"{what} must be a decimal string, got {value!r}")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise HandoffError(f"{what} is not a decimal: {value!r}") from None
    if not parsed.is_finite():
        raise HandoffError(f"{what} must be finite, got {value!r}")
    return parsed


def _optional_decimal(value: object, what: str) -> Decimal | None:
    return None if value is None else _decimal(value, what)


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HandoffError(f"{what} must be an object, got {type(value).__name__}")
    return value


def _field(doc: Mapping[str, object], key: str, what: str) -> object:
    if key not in doc:
        raise HandoffError(f"{what} is missing {key!r}")
    return doc[key]


def _whole(value: object, what: str, *, low: int, high: int | None = None) -> int:
    return whole(value, what, low=low, high=high, error=HandoffError)


def spot_weight(
    margin_pct: int, equity_perp: Decimal | None, equity_spot: Decimal | None
) -> Decimal:
    """The spot share that matches the perp leg's notional, in [0, 1], four places.

    The perp leg at 1x holds ``margin_pct`` percent of its equity as
    notional; the spot leg holds the same notional, which is that amount
    over its own equity. Three cases, kept apart because they mean
    different things to the operator reading the handoff, and checked in
    this order:

    - a KNOWN equity at or below zero on either leg — the perp account is
      blown up (the schema allows it) or the spot run holds nothing: there
      is nothing to hedge or nothing to hedge with, the weight is 0, and
      the coordinator warns. A handoff still goes out: the day that
      account dies is exactly the day the other leg must hear about it.
      A dead leg is a dead leg whether or not the other one was read;
    - an equity UNKNOWN (``None``: no store was given) — the two legs are
      taken as equal capital, so the weight is ``margin_pct`` percent, and
      the handoff's ``equity`` says ``null`` where the assumption was made;
    - both known and positive — the ratio, capped at 1 because the spot
      run cannot hold more than it has (the cap shows through the weight
      itself, which the report compares to the perp notional).
    """
    fraction = Decimal(margin_pct) / Decimal(100)
    if (equity_perp is not None and equity_perp <= 0) or (
        equity_spot is not None and equity_spot <= 0
    ):
        weight = Decimal(0)
    elif equity_perp is None or equity_spot is None:
        weight = fraction
    else:
        weight = min(fraction * equity_perp / equity_spot, Decimal(1))
    return weight.quantize(WEIGHT_PLACES, rounding=ROUND_DOWN)


@dataclass(frozen=True)
class Handoff:
    """One handoff document, validated (module docstring).

    The invariants are enforced HERE, so what the writer builds and what the
    reader accepts are held to one set: the action matches the position's
    side and its entry instant (an entry happens AT the boundary, a hold
    was entered BEFORE it), an entry has the reading it was decided from,
    and a reading is from before the boundary.
    """

    coin: str
    as_of_ms: int
    written_at_ms: int
    action: Action
    position: Position
    params: Params
    reading: Reading | None
    equity_perp: Equity | None
    equity_spot: Equity | None

    def __post_init__(self) -> None:
        if self.coin not in SPOT_TOKENS:
            raise HandoffError(f"coin must be one of {sorted(SPOT_TOKENS)}, got {self.coin!r}")
        _whole(self.as_of_ms, "as_of_ms", low=1)
        _whole(self.written_at_ms, "written_at_ms", low=1)
        if self.action.side_after is not self.position.side:
            raise HandoffError(
                f"action {self.action.value!r} contradicts position {self.position.side.value!r}"
            )
        entered_at = self.position.entered_at_ms
        if self.action is Action.ENTER and entered_at != self.as_of_ms:
            raise HandoffError(
                f"an entry is at the boundary: entered_at_ms {entered_at} is not as_of_ms "
                f"{self.as_of_ms}"
            )
        if self.action is Action.HOLD and entered_at is not None and entered_at >= self.as_of_ms:
            raise HandoffError(
                f"a hold was entered before the boundary: entered_at_ms {entered_at} is not "
                f"before as_of_ms {self.as_of_ms}"
            )
        if self.action is Action.ENTER and self.reading is None:
            raise HandoffError("an entry carries the reading it was decided from")
        if self.reading is not None and self.reading.at_ms >= self.as_of_ms:
            raise HandoffError(
                f"the reading at {self.reading.at_ms} is not before as_of_ms {self.as_of_ms}"
            )

    @property
    def margin_pct(self) -> int:
        """The rule's margin while in, 0 while out."""
        return self.params.margin_pct if self.position.side is Side.IN else 0

    @property
    def perp_side(self) -> str:
        return "short" if self.position.side is Side.IN else "flat"

    @property
    def spot_token(self) -> str:
        return SPOT_TOKENS[self.coin]

    @property
    def spot_weight(self) -> Decimal:
        """Derived from the margin and the equities (:func:`spot_weight`); 0 while out."""
        return spot_weight(
            self.margin_pct,
            None if self.equity_perp is None else self.equity_perp.value,
            None if self.equity_spot is None else self.equity_spot.value,
        )

    def to_document(self) -> dict[str, object]:
        reading = self.reading
        signal: dict[str, object] | None = None
        if reading is not None:
            signal = {
                "read_at_ms": reading.at_ms,
                "read_at": iso_utc(reading.at_ms),
                "funding_hourly": plain(reading.current),
                "z": reading.z,
                "samples": reading.samples,
                "recent_mean_hourly": (
                    None if reading.recent_mean is None else plain(reading.recent_mean)
                ),
                "recent_samples": reading.recent_samples,
            }
        return {
            "version": HANDOFF_VERSION,
            "coin": self.coin,
            "as_of_ms": self.as_of_ms,
            "as_of": iso_utc(self.as_of_ms),
            "written_at_ms": self.written_at_ms,
            "written_at": iso_utc(self.written_at_ms),
            "action": self.action.value,
            "position": {
                "side": self.position.side.value,
                "entered_at_ms": self.position.entered_at_ms,
            },
            "params": {
                "window_days": self.params.window_days,
                "z_in": self.params.z_in,
                "z_out": self.params.z_out,
                "min_hold_days": self.params.min_hold_days,
                "margin_pct": self.params.margin_pct,
            },
            "perp": {"side": self.perp_side, "margin_pct": self.margin_pct},
            "spot": {"token": self.spot_token, "weight": plain(self.spot_weight)},
            "signal": signal,
            "equity": {
                "perp": None if self.equity_perp is None else plain(self.equity_perp.value),
                "perp_at_ms": None if self.equity_perp is None else self.equity_perp.at_ms,
                "perp_at": None if self.equity_perp is None else iso_utc(self.equity_perp.at_ms),
                "spot": None if self.equity_spot is None else plain(self.equity_spot.value),
                "spot_at_ms": None if self.equity_spot is None else self.equity_spot.at_ms,
                "spot_at": None if self.equity_spot is None else iso_utc(self.equity_spot.at_ms),
            },
        }

    @classmethod
    def from_document(cls, doc: object) -> Handoff:
        """The handoff ``doc`` spells, every field checked, the derived ones cross-checked."""
        top = _mapping(doc, "the handoff")
        version = _field(top, "version", "the handoff")
        if type(version) is not int or version != HANDOFF_VERSION:
            raise HandoffError(f"handoff version {version!r} is not {HANDOFF_VERSION}")
        coin = _field(top, "coin", "the handoff")
        if not isinstance(coin, str) or coin not in SPOT_TOKENS:
            raise HandoffError(f"coin must be one of {sorted(SPOT_TOKENS)}, got {coin!r}")
        try:
            action = Action(_field(top, "action", "the handoff"))
        except ValueError:
            raise HandoffError(f"unknown action {top['action']!r}") from None
        position_doc = _mapping(_field(top, "position", "the handoff"), "position")
        try:
            side = Side(_field(position_doc, "side", "position"))
        except ValueError:
            raise HandoffError(f"unknown side {position_doc['side']!r}") from None
        entered_at = _field(position_doc, "entered_at_ms", "position")
        try:
            position = Position(
                side,
                None if entered_at is None else _whole(entered_at, "position.entered_at_ms", low=1),
            )
        except ValueError as exc:
            raise HandoffError(str(exc)) from None
        params_doc = _mapping(_field(top, "params", "the handoff"), "params")
        try:
            params = Params(
                window_days=_field(params_doc, "window_days", "params"),  # type: ignore[arg-type]
                z_in=_field(params_doc, "z_in", "params"),  # type: ignore[arg-type]
                z_out=_field(params_doc, "z_out", "params"),  # type: ignore[arg-type]
                min_hold_days=_field(params_doc, "min_hold_days", "params"),  # type: ignore[arg-type]
                margin_pct=_field(params_doc, "margin_pct", "params"),  # type: ignore[arg-type]
            )
        except SignalError as exc:
            raise HandoffError(f"params: {exc}") from None
        perp = _mapping(_field(top, "perp", "the handoff"), "perp")
        spot = _mapping(_field(top, "spot", "the handoff"), "spot")
        signal_doc = _field(top, "signal", "the handoff")
        equity = _mapping(_field(top, "equity", "the handoff"), "equity")
        try:
            reading = None if signal_doc is None else _reading_from(_mapping(signal_doc, "signal"))
        except SignalError as exc:
            raise HandoffError(f"signal: {exc}") from None
        handoff = cls(
            coin=coin,
            as_of_ms=_whole(_field(top, "as_of_ms", "the handoff"), "as_of_ms", low=1),
            written_at_ms=_whole(
                _field(top, "written_at_ms", "the handoff"), "written_at_ms", low=1
            ),
            action=action,
            position=position,
            params=params,
            reading=reading,
            equity_perp=_equity_from(equity, "perp"),
            equity_spot=_equity_from(equity, "spot"),
        )
        if _field(perp, "side", "perp") != handoff.perp_side:
            raise HandoffError(f"perp.side {perp['side']!r} contradicts position {side.value!r}")
        margin = _whole(_field(perp, "margin_pct", "perp"), "perp.margin_pct", low=0, high=100)
        if margin != handoff.margin_pct:
            raise HandoffError(
                f"perp.margin_pct {margin} disagrees with params and position "
                f"(they give {handoff.margin_pct})"
            )
        if _field(spot, "token", "spot") != handoff.spot_token:
            raise HandoffError(f"spot.token {spot['token']!r} is not {coin}'s {handoff.spot_token}")
        weight = _decimal(_field(spot, "weight", "spot"), "spot.weight")
        if weight != handoff.spot_weight:
            if handoff.position.side is Side.OUT:
                raise HandoffError(f"spot.weight {weight} must be 0 while out")
            raise HandoffError(
                f"spot.weight {weight} disagrees with the equities it travels with "
                f"(they give {handoff.spot_weight})"
            )
        return handoff


def _equity_from(doc: Mapping[str, object], leg: str) -> Equity | None:
    value = _optional_decimal(_field(doc, leg, "equity"), f"equity.{leg}")
    at = _field(doc, f"{leg}_at_ms", "equity")
    if (value is None) != (at is None):
        raise HandoffError(f"equity.{leg} and equity.{leg}_at_ms are both set or both null")
    if value is None:
        return None
    return Equity(value, _whole(at, f"equity.{leg}_at_ms", low=1))


def _reading_from(doc: Mapping[str, object]) -> Reading:
    z = _field(doc, "z", "signal")
    return Reading(
        at_ms=_whole(_field(doc, "read_at_ms", "signal"), "signal.read_at_ms", low=1),
        current=_decimal(_field(doc, "funding_hourly", "signal"), "signal.funding_hourly"),
        z=None if z is None else finite(z, "signal.z", error=HandoffError),
        samples=_whole(_field(doc, "samples", "signal"), "signal.samples", low=0),
        recent_mean=_optional_decimal(
            _field(doc, "recent_mean_hourly", "signal"), "signal.recent_mean_hourly"
        ),
        recent_samples=_whole(
            _field(doc, "recent_samples", "signal"), "signal.recent_samples", low=0
        ),
    )


def build(
    *,
    coin: str,
    as_of_ms: int,
    written_at_ms: int,
    action: Action,
    position: Position,
    params: Params,
    reading: Reading | None,
    equity_perp: Equity | None,
    equity_spot: Equity | None,
) -> Handoff:
    """The handoff for ``position`` (the position AFTER ``action``) at ``as_of_ms``."""
    return Handoff(
        coin=coin,
        as_of_ms=as_of_ms,
        written_at_ms=written_at_ms,
        action=action,
        position=position,
        params=params,
        reading=reading,
        equity_perp=equity_perp,
        equity_spot=equity_spot,
    )


def write_handoff(path: Path, handoff: Handoff) -> None:
    """Write ``handoff`` to ``path`` atomically: a reader sees the old file or the new one.

    LF line endings on every host, so the bytes of a document do not depend
    on where the coordinator ran.
    """
    text = json.dumps(handoff.to_document(), indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, lambda fh: fh.write(text), newline="\n")


def read_handoff(path: Path) -> Handoff:
    """The handoff at ``path``, validated; :class:`HandoffError` for anything else."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HandoffError(f"{path}: cannot read: {exc.strerror or exc}") from None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise HandoffError(f"{path}: not JSON: {exc}") from None
    try:
        return Handoff.from_document(doc)
    except HandoffError as exc:
        raise HandoffError(f"{path}: {exc}") from None


def previous_handoff(path: Path, *, coin: str, as_of_ms: int) -> Handoff | None:
    """The coordinator's memory: the handoff last written at ``path``, or ``None`` without one.

    The memory is trusted only when it is ``coin``'s and no later than the
    boundary ``as_of_ms`` being decided; anything else is a
    :class:`HandoffError`, never ``None``, because reading a file as "no
    position" when there is one would enter a second time on top of a leg
    that is already in, and nothing downstream could tell — the same
    reason an unreadable file is an error. A file for a LATER boundary
    means the venues may already have acted on it; a file for the SAME
    boundary is the rerun the caller handles.
    """
    if not path.exists():
        return None
    last = read_handoff(path)
    if last.coin != coin:
        raise HandoffError(
            f"{path} is {last.coin}'s handoff, not {coin}'s; give each coin its own file"
        )
    if last.as_of_ms > as_of_ms:
        raise HandoffError(
            f"{path} is for {iso_utc(last.as_of_ms)}, later than {iso_utc(as_of_ms)}; "
            f"the venues may have acted on it"
        )
    return last
