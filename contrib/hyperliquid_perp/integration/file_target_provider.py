"""The file-target provider: the carry coordinator's handoff as the perp leg's target.

The second :class:`~..ports.DecisionProvider` (carry plan §2 D2, §3.3), chosen
by ``decision_source: {provider: file_target, target_path: ...}``. Its
``build_input`` is :class:`.decision_provider.MarketContextProvider`'s — the
same fetch, the same guards, the same payload and ``ai_inputs`` row — with
``model`` set to ``FILE_TARGET_MODEL``, so the audit trail says no model was
asked (and ``contrib.replay`` refuses the run by that word: there is no
question to re-ask). ``request_decision`` reads ONE JSON file — the handoff
``contrib/carry`` writes (its README, 「交接檔」) — and spends nothing.

**One contract, not two.** The perp leg the file names is rendered as the
decision text the engine would have emitted and put through
``parse_target_decision`` like any engine answer, so the margin grid, the
flat-means-zero rule and the fail-closed vocabulary are the ones every other
decision meets: an off-grid margin fails closed as ``margin_off_step_grid``,
exactly as an LLM's would (the plan's R3 asks the RUNBOOK to keep
``--margin-pct`` on the run's grid for this reason), and the risk gate
downstream sees nothing it has not seen.

**Staleness is the document's rule** (README 「陳舊政策」; plan §2 D5, R1):
the targets apply while ``as_of_ms <= now < as_of_ms + one day``, with
``now`` the CYCLE's clock (``decision_input.context.as_of``, the daemon's one
time base) and never ``written_at`` — a coordinator that failed for a day
leaves a file whose write time is fresh and whose target is a day old.
Outside the window, or with no file, a file that is not JSON, another
version, another coin, or a perp block that contradicts itself, the answer is
a VALID ``maintain_current`` that carries the reason, plus a WARNING: both
legs hold what they hold, because a flat perp beside a spot leg that is still
long is a naked position. Not an invalid parse — nothing here broke the
contract, and the validation counters must not read a day the coordinator
missed as a model that drifted from the schema.

**Every cycle in the window re-asserts the target.** The scheduler's cycles
roll (``paper/scheduler``: the last decision instant + 4h, never a UTC
boundary), so there is no "midnight cycle" to single out. Re-asserting is
idempotent through the deadband (same side, same margin: no order) and
self-healing: a cycle the gate or the venue refused is retried four hours
later, and a leg the stops closed is re-opened while the document still asks
for it — the hedge, not a view, is what the document expresses.

Only what this leg reads is checked (``version``, ``coin``, ``as_of_ms``,
``perp``); unknown keys are ignored (the version policy, R4), and the
``spot`` block is the other leg's business. Nothing here imports
``contrib.carry``: the file is the contract (plan §1.2), and the carry
package's own tests hold that no neighbour reaches into it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from ..common.constants import FILE_TARGET_MODEL
from ..common.enum_guard import check_enum
from ..common.instants import from_epoch_ms
from ..domains.perp.target_decision import ParsedDecision, parse_target_decision
from .decision_provider import MarketContextProvider

__all__ = [
    "FILE_TARGET_CONFIDENCE",
    "HANDOFF_VERSION",
    "HANDOFF_WINDOW",
    "PERP_SIDES",
    "FileTarget",
    "FileTargetDecisionProvider",
    "StaleTarget",
    "check_applicable",
    "decision_text",
    "maintain_text",
    "read_target",
    "target_from_document",
]

logger = logging.getLogger(__name__)

# The handoff schema this reader speaks (``contrib/carry/handoff.py``,
# ``HANDOFF_VERSION``). Fields may be ADDED under the same version and this
# reader ignores what it does not know; the version moves only when an
# existing field changes meaning, and then readers deploy before the writer.
HANDOFF_VERSION = 1
# The targets' life: from their boundary until the next one. Fixed by the
# document's contract, not a config knob — the plan's §3.3 first sketched a
# ``target_max_age_hours`` (26), and its R1 replaced that with this window
# anchored on ``as_of_ms``.
HANDOFF_WINDOW = timedelta(days=1)
# What the perp block may say: the carry leg is short while in and flat while
# out, never long (the spot leg cannot be short; plan §1.2 point 5).
PERP_SIDES = ("short", "flat")
# A rule has no doubt to report: the gate's confidence bars exist to filter a
# model's weak calls, and a target the coordinator decided passes them whole.
FILE_TARGET_CONFIDENCE = Decimal(1)


class StaleTarget(Exception):
    """A document this leg must not act on; the message is the reason, for the log and the rationale."""


@dataclass(frozen=True)
class FileTarget:
    """The perp leg of one handoff: what it said, before the applicability check."""

    coin: str
    as_of: datetime
    side: str
    margin_pct: int
    action: str | None


def read_target(path: Path) -> FileTarget:
    """The handoff at ``path`` as this leg reads it; :class:`StaleTarget` for anything unusable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise StaleTarget(f"no handoff at {path}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise StaleTarget(f"cannot read {path}: {getattr(exc, 'strerror', None) or exc}") from None
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StaleTarget(f"{path} is not JSON: {exc}") from None
    return target_from_document(doc)


def _whole(value: object, what: str) -> int:
    # ``bool`` is an ``int`` to ``isinstance`` and never a count or an instant.
    if type(value) is not int:
        raise StaleTarget(f"{what} must be a whole number, got {value!r}")
    return value


def target_from_document(doc: object) -> FileTarget:
    """The perp leg ``doc`` spells, checked field by field; unknown keys ignored."""
    if not isinstance(doc, Mapping):
        raise StaleTarget(f"the handoff is not a JSON object but {type(doc).__name__}")
    version = doc.get("version")
    if type(version) is not int or version != HANDOFF_VERSION:
        raise StaleTarget(f"handoff version {version!r} is not {HANDOFF_VERSION}")
    coin = doc.get("coin")
    if not isinstance(coin, str) or not coin:
        raise StaleTarget(f"handoff coin must be a symbol, got {coin!r}")
    as_of_ms = _whole(doc.get("as_of_ms"), "as_of_ms")
    try:
        as_of = from_epoch_ms(as_of_ms)
    except (OverflowError, ValueError) as exc:
        raise StaleTarget(f"as_of_ms {as_of_ms} is not an instant: {exc}") from None
    perp = doc.get("perp")
    if not isinstance(perp, Mapping):
        raise StaleTarget(f"handoff perp block must be an object, got {perp!r}")
    side = perp.get("side")
    try:
        check_enum(side, PERP_SIDES, name="perp.side")
    except ValueError as exc:
        raise StaleTarget(str(exc)) from None
    assert isinstance(side, str)  # check_enum admits only the tuple's strings
    margin = _whole(perp.get("margin_pct"), "perp.margin_pct")
    if not 0 <= margin <= 100:
        raise StaleTarget(f"perp.margin_pct must be within 0..100, got {margin}")
    # The writer's own invariant (``Handoff.margin_pct``): sized while short,
    # zero while flat. A document that breaks it was not written by the
    # coordinator, or was edited; neither is a target.
    if (side == "flat") != (margin == 0):
        raise StaleTarget(f"perp.side {side!r} contradicts perp.margin_pct {margin}")
    action = doc.get("action")
    return FileTarget(
        coin=coin,
        as_of=as_of,
        side=side,
        margin_pct=margin,
        action=action if isinstance(action, str) else None,
    )


def check_applicable(target: FileTarget, *, coin: str, now: datetime) -> None:
    """Raise :class:`StaleTarget` unless ``target`` is ``coin``'s and ``now`` is inside its day."""
    if target.coin != coin:
        raise StaleTarget(f"the handoff is {target.coin}'s and this run trades {coin}")
    if now < target.as_of:
        raise StaleTarget(
            f"the handoff is for {target.as_of.isoformat()}, which this cycle "
            f"at {now.isoformat()} has not reached"
        )
    if now >= target.as_of + HANDOFF_WINDOW:
        raise StaleTarget(
            f"the handoff is for {target.as_of.isoformat()}, a day or more before "
            f"this cycle at {now.isoformat()}"
        )


def _contract(
    *,
    decision_mode: str,
    target_side: str | None,
    margin_pct: int | None,
    confidence: Decimal | None,
    rationale: str,
    key_risks: list[str],
) -> str:
    """The decision contract as text, the shape ``parse_target_decision`` reads."""
    return json.dumps(
        {
            "decision_mode": decision_mode,
            "target_side": target_side,
            "requested_target_margin_pct": margin_pct,
            # A number, as the format block asks for; a ``str`` would be the
            # parser's ``confidence_quoted_number`` refusal.
            "confidence": None if confidence is None else float(confidence),
            "rationale": rationale,
            "key_risks": key_risks,
        }
    )


def decision_text(target: FileTarget, path: Path) -> str:
    """``set_target`` for the leg ``target`` names: short at its margin, or flat at 0."""
    what = f"{target.action} " if target.action else ""
    return _contract(
        decision_mode="set_target",
        target_side=target.side,
        margin_pct=target.margin_pct,
        confidence=FILE_TARGET_CONFIDENCE,
        rationale=(
            f"carry handoff {path.name}: {what}at {target.as_of.isoformat()}; "
            f"perp leg {target.side} at {target.margin_pct}% margin (carry plan D2, no model asked)"
        ),
        key_risks=[
            "the spot leg reads the same document on its own clock; the carry report "
            "reconciles the two notionals (carry plan D7)"
        ],
    )


def maintain_text(reason: str) -> str:
    """``maintain_current`` carrying ``reason``: both legs hold (carry plan D5)."""
    return _contract(
        decision_mode="maintain_current",
        target_side=None,
        margin_pct=None,
        confidence=None,
        rationale=f"carry handoff unusable: {reason}; both legs maintain (carry plan D5)",
        key_risks=[
            "a leg that acted on a fresher document than the other is a naked "
            "position until the next handoff"
        ],
    )


class FileTargetDecisionProvider(MarketContextProvider):
    """:class:`~..ports.DecisionProvider` that takes its target from the carry handoff file.

    ``build_input`` is the base's, recording ``FILE_TARGET_MODEL`` as the
    model; ``request_decision`` reads ``target_path`` and parses the leg it
    names through the decision contract (module docstring). It raises no
    :class:`RetryableDecisionError`: there is nothing to retry — a file that is
    missing now is missing in ten seconds, and the §3.1 ladder exists for
    venues and models — so every unusable document is a decided
    ``maintain_current`` with the reason on the row.
    """

    @property
    def _model(self) -> str:
        return FILE_TARGET_MODEL

    def __init__(
        self,
        config: dict,
        *,
        risk_cfg,
        decision_cfg,
        payload_dir: Path,
        on_blocking_read=None,
        position_source,
        target_path: Path,
    ) -> None:
        super().__init__(
            config,
            risk_cfg=risk_cfg,
            decision_cfg=decision_cfg,
            payload_dir=payload_dir,
            on_blocking_read=on_blocking_read,
            position_source=position_source,
        )
        self._target_path = target_path

    def request_decision(self, decision_input) -> ParsedDecision:
        path = self._target_path
        coin = decision_input.context.coin
        now = decision_input.context.as_of
        try:
            target = read_target(path)
            check_applicable(target, coin=coin, now=now)
        except StaleTarget as stale:
            # WARNING on every cycle it holds (six a day): the condition does
            # not heal by itself, and the operator reading the log must see it
            # each time, the way a stale market feed is said each cycle.
            logger.warning(
                "file target %s: %s — maintaining the current position on both legs "
                "(carry plan D5)",
                path,
                stale,
            )
            text = maintain_text(str(stale))
        else:
            logger.info(
                "file target %s: %s %d%% for %s at %s%s",
                path,
                target.side,
                target.margin_pct,
                coin,
                target.as_of.isoformat(),
                f" ({target.action})" if target.action else "",
            )
            text = decision_text(target, path)
        return parse_target_decision(text, self._decision)
