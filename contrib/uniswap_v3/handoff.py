"""The spot leg's reader of the carry handoff: the file ``contrib/carry`` writes, read for its spot block.

The handoff is the ONE thing the carry coordinator hands this package
(carry plan §1.2: nothing here imports ``contrib/carry``, and the carry
package imports nothing of this one; the file is the contract, spelled out
in ``contrib/carry/README.md``, 「交接檔」). This module reads it for the
``target`` command: :func:`read_handoff` takes the bytes and the JSON,
:func:`target_from_document` the spot block, and :func:`check_applicable`
says whether the boundary it is for is the one to act on now.

Only what this leg reads is checked: ``version``, ``coin``, ``as_of_ms``,
``spot.token`` and ``spot.weight``, with ``action`` taken along for the
log. Unknown keys are ignored (the version policy, plan R4: fields may be
added under the same version, and the version moves only when an existing
field changes meaning), and the ``perp`` block is the other leg's business.
The weight is checked to lie in ``[0, 1]`` and not sized again (plan R3).

**Staleness is the document's rule** (plan §2 D5, R1): the targets apply
while ``as_of_ms <= now < as_of_ms + one day``, with ``now`` the clock of
the visit, and never ``written_at``, which a coordinator that failed for a
day leaves fresh beside a target a day old. A document for a boundary
still ahead is the normal schedule (the coordinator writes at 23:50 for
midnight) and one a day or more old is a coordinator that did not run:
both are :class:`HandoffNotApplicable`, for the visit to try again later,
when the file may be in its window or replaced. A file that is not there
yet is the same. Anything else wrong with the file, from bytes that are not
JSON to a weight above 1, is :class:`HandoffError`: running the command
again on the same file will not help.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Final

from .domain.decimal_context import parse_decimal
from .domain.targets import DAY_SECONDS, require_weight
from .domain.times import utc_text
from .domain.verdicts import require_count, require_text

__all__ = [
    "HANDOFF_VERSION",
    "HANDOFF_WINDOW_SECONDS",
    "HandoffError",
    "HandoffNotApplicable",
    "HandoffTarget",
    "check_applicable",
    "read_handoff",
    "target_from_document",
]

# The handoff schema this reader speaks (``contrib/carry/handoff.py``,
# ``HANDOFF_VERSION``), the same number the perp leg's reader holds.
HANDOFF_VERSION: Final = 1
# The targets' life: from their boundary, a UTC day boundary, until the next
# one. Fixed by the document's contract, not a config knob (plan R1).
HANDOFF_WINDOW_SECONDS: Final = DAY_SECONDS


class HandoffError(ValueError):
    """A document this leg cannot take a target from; the same file read again gives the same."""


class HandoffNotApplicable(Exception):
    """No document to act on now: none yet, or one whose boundary is ahead of now or a day behind it."""


@dataclass(frozen=True)
class HandoffTarget:
    """The spot leg of one handoff, validated: what the document said, before the applicability check.

    The values are judged HERE, whatever built the target: ``as_of`` is the
    boundary the target is for, in epoch seconds, a UTC day boundary (the
    document carries milliseconds) that this host can spell as a date;
    ``token`` is spelled as the config spells it (the coordinator writes the
    same spelling; nothing is folded); ``action`` is the coordinator's word
    for what the boundary did, for the log alone, and deliberately not
    stored: the ``targets`` row holds what this leg was told to hold, and
    the coordinator's own state (enter, hold, exit, stay out) is its
    business, recorded in the perp leg's sidecar and in ``carry history``.
    """

    coin: str
    as_of: int
    token: str
    weight: Decimal
    action: str | None

    def __post_init__(self) -> None:
        try:
            require_text(self.coin, "coin")
            require_count(self.as_of, "as_of")
            require_text(self.token, "spot.token")
            require_weight(self.weight, "spot.weight")
        except ValueError as exc:
            raise HandoffError(str(exc)) from None
        try:
            spelled = utc_text(self.as_of)
        except (OverflowError, OSError, ValueError):
            # An integer no calendar reaches: every later message spells the
            # boundary, so it is refused here, once, as the document's fault.
            raise HandoffError(f"as_of {self.as_of} is not an instant this host can spell") from None
        if self.as_of % DAY_SECONDS:
            raise HandoffError(
                f"as_of {spelled} is not a UTC day boundary, and a handoff's targets are for one "
                f"day"
            )
        if self.action is not None and not isinstance(self.action, str):
            raise HandoffError(f"action must be a string or None, got {self.action!r}")


def read_handoff(path: Path) -> tuple[bytes, object]:
    """The handoff file at ``path`` as its bytes and the JSON they decode to.

    The bytes come back too, for :func:`~.domain.verdicts.bytes_digest`. A file that is not
    there is :class:`HandoffNotApplicable` (the coordinator has not written
    it yet); one that cannot be read, or is not UTF-8 JSON, is
    :class:`HandoffError`.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise HandoffNotApplicable(f"no handoff at {path} yet") from None
    except OSError as exc:
        raise HandoffError(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        # A UnicodeDecodeError and a JSONDecodeError are both ValueErrors.
        raise HandoffError(f"{path} is not UTF-8 JSON: {exc}") from None


def _mapping(value: object, what: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HandoffError(f"{what} must be an object, got {value!r}")
    return value


def target_from_document(doc: object) -> HandoffTarget:
    """The spot leg ``doc`` spells: the shape decoded here, the values judged by :class:`HandoffTarget`.

    Unknown keys are ignored.
    """
    top = _mapping(doc, "the handoff")
    version = top.get("version")
    # ``bool`` is an ``int`` to ``isinstance`` and never a version.
    if type(version) is not int or version != HANDOFF_VERSION:
        raise HandoffError(f"handoff version {version!r} is not {HANDOFF_VERSION}")
    spot = _mapping(top.get("spot"), "the handoff's spot block")
    weight = spot.get("weight")
    if not isinstance(weight, str):
        # Decimals travel as strings in the document, so no digit is lost in a float.
        raise HandoffError(f"spot.weight must be a decimal string, got {weight!r}")
    as_of_ms = top.get("as_of_ms")
    try:
        require_count(as_of_ms, "as_of_ms")
        parsed = parse_decimal(weight, "spot.weight")
    except ValueError as exc:
        raise HandoffError(str(exc)) from None
    if type(as_of_ms) is not int:  # what ``require_count`` just held, spelled for the type checker
        raise HandoffError(f"as_of_ms must be a non-negative integer, got {as_of_ms!r}")
    as_of, remainder = divmod(as_of_ms, 1_000)
    if remainder:
        raise HandoffError(f"as_of_ms {as_of_ms} is not a whole second, and a boundary is one")
    # The raw values go in as read: ``HandoffTarget.__post_init__`` judges every
    # one of them and turns a refusal into a ``HandoffError``.
    return HandoffTarget(
        coin=top.get("coin"),  # type: ignore[arg-type]
        as_of=as_of,
        token=spot.get("token"),  # type: ignore[arg-type]
        weight=parsed,
        action=top.get("action"),  # type: ignore[arg-type]
    )


def check_applicable(target: HandoffTarget, *, now: int) -> None:
    """Raise unless the clock ``now`` is inside ``target``'s day.

    :class:`HandoffNotApplicable` for a boundary still ahead (the next
    day's document, already written) and for one a day or more behind;
    :class:`HandoffError` for a boundary more than a day ahead, which no
    coordinator on its schedule writes: a clock that is off, or a
    hand-written ``--as-of``. The check is made before the store is looked
    at, on purpose: a stale handoff whose row was stored yesterday is still
    "try again later" today, so the visit script stops there and `paper` is
    not run on a day the coordinator missed.
    """
    if now < target.as_of - HANDOFF_WINDOW_SECONDS:
        raise HandoffError(
            f"the handoff is for {utc_text(target.as_of)}, more than a day after now, "
            f"{utc_text(now)}"
        )
    if now < target.as_of:
        raise HandoffNotApplicable(
            f"the handoff is for {utc_text(target.as_of)}, which has not come yet (now "
            f"{utc_text(now)})"
        )
    if now >= target.as_of + HANDOFF_WINDOW_SECONDS:
        raise HandoffNotApplicable(
            f"the handoff is for {utc_text(target.as_of)}, a day or more before now, "
            f"{utc_text(now)}; the coordinator has not written a newer one"
        )
