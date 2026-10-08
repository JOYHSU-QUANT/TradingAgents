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
``now`` the CYCLE's clock — the ``as_of`` the driver handed ``build_input``,
which the base stashes as ``_cycle_at``. NOT ``decision_input.context.as_of``:
that is the last closed candle's close, up to four hours behind the cycle,
which would read every cycle in the first interval after midnight as "before
the boundary" and act on a dead document for one interval after the next.
And never ``written_at`` — a coordinator that failed for a day leaves a file
whose write time is fresh and whose target is a day old.

Outside the window, or with no file, a file that is not JSON, another
version, another coin, or a perp block that contradicts itself, the answer is
a VALID ``maintain_current`` that carries the reason, plus a WARNING: both
legs hold what they hold, because a flat perp beside a spot leg that is still
long is a naked position. Not an invalid parse — nothing here broke the
contract, and the validation counters must not read a day the coordinator
missed as a model that drifted from the schema. One case is not a fault: a
document whose boundary lies AHEAD of the cycle. The coordinator writes the
next day's targets at 23:50 (plan §3.1), so a cycle in the minutes before
midnight sees tomorrow's document; it maintains too, but says ``pending`` at
INFO, so neither the log nor the report (D5's "N days before a human steps
in") counts the normal schedule as an outage. The two spellings are
``common.constants.FILE_TARGET_UNUSABLE_PREFIX`` / ``FILE_TARGET_PENDING_PREFIX``.

**Every cycle in the window re-asserts the target.** The scheduler's cycles
roll (``paper/scheduler``: the last decision instant + 4h, never a UTC
boundary), so there is no "midnight cycle" to single out. Re-asserting is
idempotent through the deadband (same side, same margin: no order) and
self-healing: a cycle the gate or the venue refused is retried four hours
later, and a leg the stops closed is re-opened while the document still asks
for it — the hedge, not a view, is what the document expresses.

**The document a decision was built from is kept.** The coordinator
overwrites one path every day, so a cycle whose document applied writes it
beside its payload as ``<payload>.handoff.json`` (the sidecar contract,
``common.sidecar``): the parsed document re-serialized, plus the sha256 of
the bytes it was read from. The carry report (plan PR 4, D7) reconciles each
day's legs against the handoff of that day, not against whatever the path
holds when it runs. Whether the cycle then ACTED is the ``ai_outputs`` row's
business — an off-grid margin fails closed with its sidecar written — so the
sidecar's presence says "read and applicable", not "traded".

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
from typing import Final

from ..common.constants import (
    FILE_TARGET_MODEL,
    FILE_TARGET_PENDING_PREFIX,
    FILE_TARGET_UNUSABLE_PREFIX,
)
from ..common.digest import payload_digest
from ..common.enum_guard import check_enum
from ..common.instants import from_epoch_ms
from ..common.sidecar import write_sidecar
from ..domains.perp.target_decision import ParsedDecision, TargetSide, parse_target_decision
from ..engine_bridge import EngineConfigError
from .decision_provider import MarketContextProvider

__all__ = [
    "FILE_TARGET_CONFIDENCE",
    "HANDOFF_SIDECAR_SUFFIX",
    "HANDOFF_VERSION",
    "HANDOFF_WINDOW",
    "PERP_SIDES",
    "FileTarget",
    "FileTargetConfigError",
    "FileTargetDecisionProvider",
    "PendingTarget",
    "StaleTarget",
    "check_applicable",
    "decision_text",
    "maintain_text",
    "read_document",
    "target_from_document",
]

logger = logging.getLogger(__name__)

# The handoff schema this reader speaks (``contrib/carry/handoff.py``,
# ``HANDOFF_VERSION``). Fields may be ADDED under the same version and this
# reader ignores what it does not know; the version moves only when an
# existing field changes meaning, and then readers deploy before the writer.
HANDOFF_VERSION: Final = 1
# The targets' life: from their boundary until the next one. Fixed by the
# document's contract, not a config knob — the plan's §3.3 first sketched a
# ``target_max_age_hours`` (26), and its R1 replaced that with this window
# anchored on ``as_of_ms``.
HANDOFF_WINDOW: Final = timedelta(days=1)
# What the perp block may say: the carry leg is short while in and flat while
# out, never long (the spot leg cannot be short; plan §1.2 point 5). Spelled
# from the decision contract's own vocabulary, so the two cannot drift.
PERP_SIDES: Final = (TargetSide.SHORT.value, TargetSide.FLAT.value)
# A rule has no doubt to report: the gate's confidence bars exist to filter a
# model's weak calls, and a target the coordinator decided passes them whole.
FILE_TARGET_CONFIDENCE: Final = Decimal(1)
# The sidecar a cycle that acted writes beside its payload (module docstring).
HANDOFF_SIDECAR_SUFFIX: Final = ".handoff.json"


class StaleTarget(Exception):
    """A document this leg must not act on; the message is the reason, for the log and the rationale."""


class PendingTarget(StaleTarget):
    """The document's boundary lies ahead of the cycle: tomorrow's targets, already written.

    Maintained like a stale one — the targets are not yet in force — but the
    normal schedule, not a fault (module docstring).
    """


class FileTargetConfigError(EngineConfigError):
    """``decision_source.target_path`` cannot ever be written to as configured.

    An :class:`EngineConfigError` so the two daemons treat it as the
    operator-fixable startup fault it is: a fresh run exits 1 before its row
    is written, a restart over live work falls back to protection-only rather
    than leave the position unwatched (``cli/paper.py``, ``cli/live_loop.py``).
    """


@dataclass(frozen=True)
class FileTarget:
    """The perp leg of one handoff, validated: what the document said, before the applicability check.

    The invariants live HERE, not in the decoder, so a target built any other
    way is held to the same rules: the side is one of :data:`PERP_SIDES`
    (never long — a long perp is not a carry hedge), the margin is a whole
    percent sized while short and zero while flat (the writer's own
    invariant, ``Handoff.margin_pct``), the coin is a symbol, and ``as_of`` is
    an aware instant with a day after it, so :attr:`expires_at` cannot
    overflow later inside ``request_decision`` where only :class:`StaleTarget`
    is caught.
    """

    coin: str
    as_of: datetime
    side: str
    margin_pct: int
    action: str | None

    def __post_init__(self) -> None:
        if not self.coin:
            raise StaleTarget("handoff coin must be a symbol, got ''")
        if self.as_of.tzinfo is None:
            raise StaleTarget(f"as_of {self.as_of.isoformat()} must be timezone-aware")
        try:
            _ = self.expires_at  # the addition IS the check
        except OverflowError:
            raise StaleTarget(f"as_of {self.as_of.isoformat()} has no day after it") from None
        try:
            check_enum(self.side, PERP_SIDES, name="perp.side")
        except ValueError as exc:
            raise StaleTarget(str(exc)) from None
        if type(self.margin_pct) is not int:
            raise StaleTarget(f"perp.margin_pct must be a whole number, got {self.margin_pct!r}")
        if not 0 <= self.margin_pct <= 100:
            raise StaleTarget(f"perp.margin_pct must be within 0..100, got {self.margin_pct}")
        if (self.side == TargetSide.FLAT.value) != (self.margin_pct == 0):
            raise StaleTarget(
                f"perp.side {self.side!r} contradicts perp.margin_pct {self.margin_pct}"
            )

    @property
    def expires_at(self) -> datetime:
        """The first instant the targets no longer apply: the next boundary."""
        return self.as_of + HANDOFF_WINDOW


def read_document(path: Path) -> tuple[bytes, object]:
    """The handoff file at ``path`` as its bytes and the JSON they decode to; :class:`StaleTarget` otherwise.

    The bytes come back too so the sidecar can record their digest — the one
    fact about the file as written that survives the coordinator's daily
    overwrite.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise StaleTarget(f"no handoff at {path}") from None
    except OSError as exc:
        raise StaleTarget(f"cannot read {path}: {exc.strerror or exc}") from None
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StaleTarget(f"{path} is not UTF-8 JSON: {exc}") from None


def _whole(value: object, what: str) -> int:
    # ``bool`` is an ``int`` to ``isinstance`` and never a count or an instant.
    if type(value) is not int:
        raise StaleTarget(f"{what} must be a whole number, got {value!r}")
    return value


def target_from_document(doc: object) -> FileTarget:
    """The perp leg ``doc`` spells: the types decoded here, the values judged by :class:`FileTarget`. Unknown keys ignored."""
    if not isinstance(doc, Mapping):
        raise StaleTarget(f"the handoff is not a JSON object but {type(doc).__name__}")
    version = doc.get("version")
    if type(version) is not int or version != HANDOFF_VERSION:
        raise StaleTarget(f"handoff version {version!r} is not {HANDOFF_VERSION}")
    coin = doc.get("coin")
    if not isinstance(coin, str):
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
    if not isinstance(side, str):
        raise StaleTarget(f"perp.side must be one of {list(PERP_SIDES)}, got {side!r}")
    action = doc.get("action")
    return FileTarget(
        coin=coin,
        as_of=as_of,
        side=side,
        margin_pct=_whole(perp.get("margin_pct"), "perp.margin_pct"),
        action=action if isinstance(action, str) else None,
    )


def check_applicable(target: FileTarget, *, coin: str, now: datetime) -> None:
    """Raise unless ``target`` is ``coin``'s and the cycle clock ``now`` is inside its day.

    :class:`PendingTarget` for a boundary still ahead (the next day's
    document, already written); :class:`StaleTarget` for every other reason.
    """
    if target.coin != coin:
        raise StaleTarget(f"the handoff is {target.coin}'s and this run trades {coin}")
    if now < target.as_of - HANDOFF_WINDOW:
        # Further ahead than the coordinator's next write could be: a clock
        # that is off, or a hand-written --as-of. A fault, not the schedule.
        raise StaleTarget(
            f"the handoff is for {target.as_of.isoformat()}, more than a day after "
            f"this cycle at {now.isoformat()}"
        )
    if now < target.as_of:
        raise PendingTarget(
            f"the handoff is for {target.as_of.isoformat()}, which this cycle "
            f"at {now.isoformat()} has not reached"
        )
    if now >= target.expires_at:
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


def maintain_text(prefix: str, reason: str) -> str:
    """``maintain_current`` carrying ``reason`` under ``prefix``: both legs hold (carry plan D5).

    ``prefix`` is one of the two ``common.constants`` spellings — the report
    reads the row by it.
    """
    return _contract(
        decision_mode="maintain_current",
        target_side=None,
        margin_pct=None,
        confidence=None,
        rationale=f"{prefix}{reason}; both legs maintain (carry plan D5)",
        key_risks=[
            "a leg that acted on a fresher document than the other is a naked "
            "position until the next handoff"
        ],
    )


class FileTargetDecisionProvider(MarketContextProvider):
    """:class:`~..ports.DecisionProvider` that takes its target from the carry handoff file.

    ``build_input`` is the base's, recording ``FILE_TARGET_MODEL`` as the
    model and stashing the cycle clock; ``request_decision`` reads
    ``target_path`` and parses the leg it names through the decision contract
    (module docstring). It raises no :class:`RetryableDecisionError`: there is
    nothing to retry — a file that is missing now is missing in ten seconds,
    and the §3.1 ladder exists for venues and models — so every unusable
    document is a decided ``maintain_current`` with the reason on the row.

    Construction refuses a ``target_path`` whose directory does not exist
    (:class:`FileTargetConfigError`): the coordinator could never write
    there, and the symptom — six WARNINGs a day, a flat leg — is exactly a
    coordinator that never ran. The FILE may be absent: switching the provider
    on before the coordinator's first run is the normal order, said once as a
    WARNING here and then once per cycle.
    """

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
        if not target_path.parent.is_dir():
            raise FileTargetConfigError(
                f"decision_source.target_path {target_path}: its directory "
                f"{target_path.parent} does not exist, so the carry coordinator could never "
                "write the handoff there — fix the path (or create the directory) and restart"
            )
        if not target_path.exists():
            logger.warning(
                "file target %s does not exist yet: every cycle maintains the current position "
                "until the carry coordinator writes it",
                target_path,
            )
        self._target_path = target_path

    @property
    def _model(self) -> str:
        return FILE_TARGET_MODEL

    def request_decision(self, decision_input) -> ParsedDecision:
        path = self._target_path
        coin = decision_input.context.coin
        now = self.cycle_at
        try:
            raw, doc = read_document(path)
            target = target_from_document(doc)
            check_applicable(target, coin=coin, now=now)
        except PendingTarget as pending:
            logger.info("file target %s: %s — maintaining until the boundary", path, pending)
            text = maintain_text(FILE_TARGET_PENDING_PREFIX, str(pending))
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
            text = maintain_text(FILE_TARGET_UNUSABLE_PREFIX, str(stale))
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
            write_sidecar(
                decision_input.input_payload_path,
                suffix=HANDOFF_SIDECAR_SUFFIX,
                what="carry handoff",
                build=lambda: {
                    "path": str(path),
                    "digest": payload_digest(raw),
                    "cycle_at": now.isoformat(),
                    "handoff": doc,
                },
            )
            text = decision_text(target, path)
        return parse_target_decision(text, self._decision)
