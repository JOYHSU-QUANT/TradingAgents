"""``status``: what one run's store says about it right now, as a model, lines and JSON.

The dashboard plan's reading model. :func:`read_status` reads a run inside
the caller's read transaction into a :class:`RunStatus` — a frozen tree of
dataclasses, every field of which is a value the store holds about the run
(the classes below are the inventory; the SQL is the readers this module
calls on :mod:`.persistence.repository`).

The model carries only what the store knows. The host — whether the daemon's
unit is running, how old the export files are — is for an exporter that
adds host facts around this document, and nothing from the run's config is
copied out: the config names the wallet and the keys, and a status is for a
page that should never see them. Free text the store holds (an error
message, a breadcrumb's error, a safe-mode reason or detail, an event's
detail or error, a verdict's reasons, a fill's reason) can still quote one — an exchange refusal names
the wallet it refused — so every column read through :func:`_text_or_none`
has a ``0x``-prefixed 20-byte or 32-byte hex folded to its first six and
last four characters, and is cut at :data:`TEXT_LIMIT` characters. Ids,
instants and vocabulary words are read as they are.

Impossible states are refused, not defaulted: a run without its opening
ledger row, a safe-mode trio or an export / replay / config-drift breadcrumb
half written, a position whose size and entry price disagree, a decided
cycle without its verdict (or an undecided one with a verdict), an amount
that is not a finite number. Each raises ``ValueError``; the CLI turns it
into ``validate``'s "investigate the store" exit code. Vocabulary words (an attempt status, a gate action, an event
type) pass through as stored: a word this build does not know is shown, not
refused, so a page keeps working across a daemon upgrade.

:func:`status_json` renders the model field by field (``dataclasses.asdict``:
the field list IS the whitelist; a value not in a dataclass is not printed),
``Decimal`` as its exact text and instants as the ISO-8601 UTC text the store
keeps; the document is ASCII (``json.dumps`` escapes the rest), so a console
or a pipe on any code page carries it. The document opens with
``schema_version`` and ``source``: the version is scoped to this package's
documents (another package's status has its own), and a change to a field's
name, place or meaning bumps :data:`SCHEMA_VERSION`; ``tests/test_status.py``
pins the field paths so the bump cannot be forgotten. :func:`status_lines` is
the same model for an operator's eyes — one ``key: value`` line per fact as
``validate`` prints, and one line per recent cycle, snapshot, fill and event —
with free text folded to one printable ASCII line and cut shorter still.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .persistence import repository as repo
from .persistence.db import stored_schema_version
from .persistence.models import PositionState

__all__ = [
    "DEFAULT_RECENT",
    "SCHEMA_VERSION",
    "SOURCE",
    "TEXT_LIMIT",
    "Account",
    "AccountSnapshot",
    "Breadcrumb",
    "Cycle",
    "Cycles",
    "Fill",
    "KillSwitchEvent",
    "Ledger",
    "ModelBucket",
    "Position",
    "RunStatus",
    "SafeMode",
    "SafeModeEvent",
    "Scheduler",
    "Verdict",
    "read_status",
    "status_json",
    "status_lines",
]

# The document's version, scoped to SOURCE. Bump it when a field is added,
# removed, renamed, moved or re-typed; the field-path pin in
# tests/test_status.py changes with it.
SCHEMA_VERSION = 1
# What the document says produced it, so a reader handed the file alone
# knows which package's schema (and version) to read it with.
SOURCE = "hyperliquid_perp"
# How many recent cycles, snapshots, fills and events a status carries unless
# told otherwise.
DEFAULT_RECENT = 10
# The longest a free-text value runs in the model (and so in the document):
# nothing reads a verdict's reason whole, and the store keeps it whole.
TEXT_LIMIT = 1000
# The longest the same text runs on an operator line.
_LINE_TEXT_LIMIT = 160
# A 32-byte hex (an agent key) or a 20-byte hex (a wallet address) quoted in
# free text; the longer shape first so a key is not read as an address plus
# a tail. Masked to its first six and last four characters (plan §4).
_HEX_SECRET = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})")
# The attempt statuses the scheduler writes a verdict for, and the ones it
# cannot have: a cycle that ended ``completed`` or ``invalid_output`` wrote
# its ``ai_outputs`` row in the same step; one still ``in_progress`` or ended
# ``api_failed`` never reached the model's answer. Spelled from the
# vocabulary so a status added to it cannot land in neither set unseen;
# tests/test_status.py pins the split against ``TERMINAL_ATTEMPT_STATUSES``.
_DECIDED_STATUSES = frozenset(
    {repo.AttemptStatus.COMPLETED.value, repo.AttemptStatus.INVALID_OUTPUT.value}
)
_UNDECIDED_STATUSES = frozenset(status.value for status in repo.AttemptStatus) - _DECIDED_STATUSES


@dataclass(frozen=True)
class Breadcrumb:
    """A ``scheduler_state`` ``last_*_status`` / ``_error`` / ``_at`` triple.

    ``status`` is ``ok`` / ``failed`` for the export, ``ok`` / ``mismatch`` /
    ``failed`` for the replay and ``ok`` / ``drift`` for the config drift, or
    ``None`` before the first pass — and then ``at`` and ``error`` are
    ``None`` too: the daemon stamps the three together.
    """

    status: str | None
    at: str | None
    error: str | None


@dataclass(frozen=True)
class Scheduler:
    """The run's ``scheduler_state`` row: where the cycle clock stands and what last happened.

    ``in_progress_attempt_id`` is set exactly while one attempt is
    ``in_progress`` (the daemon writes the pointer and the attempt row in one
    transaction and clears it with the terminal status), so it names the
    cycle ``cycles.by_status["in_progress"]`` counts.
    """

    last_decision_at: str | None
    next_decision_at: str | None
    in_progress_attempt_id: str | None
    lease_heartbeat_at: str | None
    updated_at: str
    export: Breadcrumb
    replay: Breadcrumb
    config_drift: Breadcrumb


@dataclass(frozen=True)
class SafeMode:
    """The safe mode the run is latched in (``None`` on the status when it is in none).

    ``type`` is ``recoverable`` or ``manual``. The three columns are written
    together, so a latched run always has all three.
    """

    type: str
    reason: str
    entered_at: str


@dataclass(frozen=True)
class Verdict:
    """The ``ai_outputs`` row a cycle ended with: what the model asked and what the gate said.

    ``decision_mode`` is ``set_target`` or ``maintain_current``; ``target_side``
    is ``long`` / ``short`` / ``flat`` for a ``set_target`` and ``None`` otherwise;
    ``risk_action`` is the gate's word (``approved``, ``clamped``, ``rejected``,
    ``invalid_fail_closed`` …, as ``domains.perp.risk_gate.RiskAction`` spells it).
    """

    symbol: str
    decision_mode: str
    target_side: str | None
    requested_target_margin_pct: Decimal | None
    approved_target_margin_pct: Decimal | None
    risk_action: str
    risk_reason: str | None
    confidence: Decimal | None
    mark_price: Decimal | None
    account_equity: Decimal | None
    order_created: bool
    no_order_reason: str | None
    decision_reason: str | None
    # What the trend guardrail read for this verdict (``long`` / ``short`` /
    # ``flat`` / ``unknown``); ``None`` with the guardrail off, and on rows
    # written before schema v14.
    guardrail_side: str | None = None


@dataclass(frozen=True)
class Cycle:
    """One ``decision_attempts`` row, with its verdict when it reached one.

    ``status`` is ``in_progress``, ``completed``, ``api_failed`` or
    ``invalid_output``. ``verdict`` is present exactly for the two decided
    statuses; ``None`` means the cycle is still running or never got an
    answer, not that it failed.
    """

    scheduled_at: str
    timestamp: str
    status: str
    error_type: str | None
    error_message: str | None
    attempt_count: int
    next_decision_at: str | None
    verdict: Verdict | None

    def __post_init__(self) -> None:
        if self.status in _DECIDED_STATUSES and self.verdict is None:
            raise ValueError(
                f"the {self.status} cycle scheduled {self.scheduled_at} has no ai_outputs row"
            )
        if self.status in _UNDECIDED_STATUSES and self.verdict is not None:
            raise ValueError(
                f"the {self.status} cycle scheduled {self.scheduled_at} has an ai_outputs row"
            )


@dataclass(frozen=True)
class Cycles:
    """The run's attempts: how many in each status, and the latest few, newest first.

    ``by_status`` always carries the four statuses the scheduler writes
    (zero when the run has none), in the vocabulary's order, and then any
    word this build does not know.
    """

    total: int
    by_status: dict[str, int]
    recent: tuple[Cycle, ...]


@dataclass(frozen=True)
class ModelBucket:
    """One ``(model, prompt_version)`` bucket of the ``ai_inputs`` rows the run wrote.

    ``prompts`` counts the rows as :func:`repository.model_regimes` counts
    them (once per try, so the buckets need not sum to ``cycles.total``).
    """

    model: str | None
    prompt_version: str | None
    prompts: int
    first_at: str
    last_at: str


@dataclass(frozen=True)
class Ledger:
    """The ``current_account_state`` accumulators (``wallet_balance`` folds the other three in)."""

    wallet_balance: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    net_funding_pnl: Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    """An ``account_snapshots`` row: the account valued at that instant's mark."""

    timestamp: str
    account_equity: Decimal
    available_balance: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    total_position_notional: Decimal
    effective_leverage: Decimal | None
    margin_ratio: Decimal | None


@dataclass(frozen=True)
class Account:
    """The ledger, the latest snapshot (``None`` before the first) and the latest few, newest first.

    ``recent_snapshots`` is the tail ``--recent`` asks for, ``snapshot`` its
    first row; the latest is carried even when the tail is asked to be
    empty, so a reader can tell "no snapshot yet" from "tail suppressed".
    """

    ledger: Ledger
    snapshot: AccountSnapshot | None
    recent_snapshots: tuple[AccountSnapshot, ...]

    def __post_init__(self) -> None:
        if self.recent_snapshots and self.recent_snapshots[0] != self.snapshot:
            raise ValueError("the latest account snapshot is not the first of the recent ones")


@dataclass(frozen=True)
class Position:
    """A ``current_positions`` row: signed size (``0`` is flat), entry, protection, mirrored liquidation.

    A flat position has no entry price and a held one has a positive one:
    the reader builds the accounting identity (``models.PositionState``)
    from the row first, so its invariant refuses a damaged row.
    """

    symbol: str
    size: Decimal
    entry_price: Decimal | None
    realized_pnl: Decimal
    stop_loss_price: Decimal | None
    take_profit_price: Decimal | None
    exchange_liquidation_price: Decimal | None
    updated_at: str


@dataclass(frozen=True)
class Fill:
    """A ``fills`` row, the columns paper and live share.

    ``side`` is ``buy`` or ``sell``; ``liquidity_type`` is ``maker``, ``taker``
    or ``simulated``.
    """

    timestamp: str
    symbol: str
    side: str
    fill_qty: Decimal
    fill_price: Decimal
    fee: Decimal
    realized_pnl_delta: Decimal
    liquidity_type: str
    fill_reason: str | None


@dataclass(frozen=True)
class KillSwitchEvent:
    """A ``kill_switch_events`` row (``event_type`` as ``repository.KILL_SWITCH_EVENT_TYPES``)."""

    timestamp: str
    event_type: str
    detail: str | None
    error_message: str | None


@dataclass(frozen=True)
class SafeModeEvent:
    """A ``safe_mode_events`` row (``event_type`` as ``repository.SAFE_MODE_EVENT_TYPES``)."""

    timestamp: str
    event_type: str
    safe_mode_type: str | None
    reason: str | None
    released_by: str | None
    detail: str | None


@dataclass(frozen=True)
class RunStatus:
    """What the store says about one run; the document ``status --json`` prints.

    ``mode`` is ``paper`` or ``live``, as the run was created.
    """

    schema_version: int
    source: str
    generated_at: str
    run_id: str
    mode: str
    created_at: str
    initial_balance_usdc: Decimal
    store_schema_version: int
    scheduler: Scheduler | None
    safe_mode: SafeMode | None
    cycles: Cycles
    models: tuple[ModelBucket, ...]
    account: Account
    positions: tuple[Position, ...]
    recent_fills: tuple[Fill, ...]
    kill_switch_events: tuple[KillSwitchEvent, ...]
    safe_mode_events: tuple[SafeModeEvent, ...]


# --- reading ----------------------------------------------------------------


def _dec(value: Any) -> Decimal:
    """A stored amount, exact: the store keeps Decimals as their text.

    ``NaN`` and the infinities parse as Decimals, but no writer produces
    them, and a reader that compared one would never trip an alert.
    """
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError(f"a stored amount is not a finite number: {value!r}")
    return amount


def _dec_or_none(value: Any) -> Decimal | None:
    return None if value is None else _dec(value)


def _masked(text: str) -> str:
    """``text`` with every quoted key or address folded to ``0x1234...abcd``."""
    return _HEX_SECRET.sub(lambda found: f"{found.group()[:6]}...{found.group()[-4:]}", text)


def _text_or_none(value: Any) -> str | None:
    """A free-text column, or ``None``: masked, and cut at :data:`TEXT_LIMIT`.

    The one path for text that a person or a model wrote (the module
    docstring lists the columns); ids, instants and vocabulary words are
    read with ``str`` and never come through here.
    """
    if value is None:
        return None
    text = _masked(str(value))
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 3] + "..."


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _breadcrumb(row: sqlite3.Row, what: str) -> Breadcrumb:
    status = _str_or_none(row[f"last_{what}_status"])
    at = _str_or_none(row[f"last_{what}_at"])
    error = _text_or_none(row[f"last_{what}_error"])
    if (status is None) != (at is None) or (status is None and error is not None):
        raise ValueError(
            f"the {what} breadcrumb of run {row['run_id']!r} is half written "
            f"(status {status!r}, at {at!r}, error {error!r})"
        )
    return Breadcrumb(status=status, at=at, error=error)


def _scheduler(row: sqlite3.Row | None) -> Scheduler | None:
    if row is None:
        return None
    return Scheduler(
        last_decision_at=_str_or_none(row["last_decision_at"]),
        next_decision_at=_str_or_none(row["next_decision_at"]),
        in_progress_attempt_id=_str_or_none(row["current_attempt_id"]),
        lease_heartbeat_at=_str_or_none(row["lock_heartbeat_at"]),
        updated_at=str(row["updated_at"]),
        export=_breadcrumb(row, "export"),
        replay=_breadcrumb(row, "replay"),
        config_drift=_breadcrumb(row, "config_drift"),
    )


def _safe_mode(row: sqlite3.Row | None) -> SafeMode | None:
    if row is None:
        return None
    mode_type = _str_or_none(row["safe_mode_type"])
    reason = _text_or_none(row["safe_mode_reason"])
    entered_at = _str_or_none(row["safe_mode_entered_at"])
    present = [value for value in (mode_type, reason, entered_at) if value is not None]
    if not present:
        return None
    if len(present) != 3:
        raise ValueError(
            f"the scheduler row of run {row['run_id']!r} has a safe mode half written "
            f"(type {mode_type!r}, reason {reason!r}, entered {entered_at!r})"
        )
    return SafeMode(type=str(mode_type), reason=str(reason), entered_at=str(entered_at))


def _verdict(row: sqlite3.Row) -> Verdict | None:
    if row["decision_mode"] is None:
        return None
    return Verdict(
        symbol=str(row["symbol"]),
        decision_mode=str(row["decision_mode"]),
        target_side=_str_or_none(row["target_side"]),
        requested_target_margin_pct=_dec_or_none(row["requested_target_margin_pct"]),
        approved_target_margin_pct=_dec_or_none(row["approved_target_margin_pct"]),
        risk_action=str(row["risk_action"]),
        risk_reason=_text_or_none(row["risk_reason"]),
        confidence=_dec_or_none(row["confidence"]),
        mark_price=_dec_or_none(row["mark_price"]),
        account_equity=_dec_or_none(row["account_equity"]),
        order_created=bool(row["order_created"]),
        no_order_reason=_text_or_none(row["no_order_reason"]),
        decision_reason=_text_or_none(row["decision_reason"]),
        # ``sqlite3.Row`` has no ``in``; a store older than v14 has no column.
        guardrail_side=(
            _str_or_none(row["guardrail_side"]) if "guardrail_side" in row.keys() else None  # noqa: SIM118
        ),
    )


def _cycle(row: sqlite3.Row) -> Cycle:
    return Cycle(
        scheduled_at=str(row["scheduled_at"]),
        timestamp=str(row["timestamp"]),
        status=str(row["status"]),
        error_type=_str_or_none(row["error_type"]),
        error_message=_text_or_none(row["error_message"]),
        attempt_count=int(row["attempt_count"]),
        next_decision_at=_str_or_none(row["next_decision_at"]),
        verdict=_verdict(row),
    )


def _by_status(counts: Mapping[str, int]) -> dict[str, int]:
    """The four statuses in vocabulary order, zero-filled, then any unknown word."""
    return {**{status.value: 0 for status in repo.AttemptStatus}, **counts}


def _model_bucket(row: sqlite3.Row) -> ModelBucket:
    return ModelBucket(
        model=_str_or_none(row["model"]),
        prompt_version=_str_or_none(row["prompt_version"]),
        prompts=int(row["prompts"]),
        first_at=str(row["first_at"]),
        last_at=str(row["last_at"]),
    )


def _snapshot(row: sqlite3.Row) -> AccountSnapshot:
    return AccountSnapshot(
        timestamp=str(row["timestamp"]),
        account_equity=_dec(row["account_equity"]),
        available_balance=_dec(row["available_balance"]),
        unrealized_pnl=_dec(row["unrealized_pnl"]),
        total_pnl=_dec(row["total_pnl"]),
        total_position_notional=_dec(row["total_position_notional"]),
        effective_leverage=_dec_or_none(row["effective_leverage"]),
        margin_ratio=_dec_or_none(row["margin_ratio"]),
    )


def _position(row: sqlite3.Row) -> Position:
    identity = PositionState(
        coin=str(row["symbol"]),
        size=_dec(row["size"]),
        entry_price=_dec_or_none(row["entry_price"]),
        realized_pnl=_dec(row["realized_pnl"]),
    )
    return Position(
        symbol=identity.coin,
        size=identity.size,
        entry_price=identity.entry_price,
        realized_pnl=identity.realized_pnl,
        stop_loss_price=_dec_or_none(row["stop_loss_price"]),
        take_profit_price=_dec_or_none(row["take_profit_price"]),
        exchange_liquidation_price=_dec_or_none(row["exchange_liquidation_price"]),
        updated_at=str(row["updated_at"]),
    )


def _fill(row: sqlite3.Row) -> Fill:
    return Fill(
        timestamp=str(row["timestamp"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        fill_qty=_dec(row["fill_qty"]),
        fill_price=_dec(row["fill_price"]),
        fee=_dec(row["fee"]),
        realized_pnl_delta=_dec(row["realized_pnl_delta"]),
        liquidity_type=str(row["liquidity_type"]),
        fill_reason=_text_or_none(row["fill_reason"]),
    )


def _kill_switch_event(row: sqlite3.Row) -> KillSwitchEvent:
    return KillSwitchEvent(
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        detail=_text_or_none(row["detail"]),
        error_message=_text_or_none(row["error_message"]),
    )


def _safe_mode_event(row: sqlite3.Row) -> SafeModeEvent:
    return SafeModeEvent(
        timestamp=str(row["timestamp"]),
        event_type=str(row["event_type"]),
        safe_mode_type=_str_or_none(row["safe_mode_type"]),
        reason=_text_or_none(row["reason"]),
        released_by=_str_or_none(row["released_by"]),
        detail=_text_or_none(row["detail"]),
    )


def _generated_at(now: Callable[[], datetime]) -> str:
    """The read instant as the store spells instants: aware, in UTC."""
    moment = now()
    if moment.tzinfo is None:
        raise ValueError("the status clock must give an aware datetime")
    return moment.astimezone(timezone.utc).isoformat()


def read_status(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    now: Callable[[], datetime],
    recent: int = DEFAULT_RECENT,
) -> RunStatus:
    """The run's status, read inside the caller's read transaction (one snapshot).

    ``recent`` caps the cycles, snapshots, fills and events carried. Raises
    ``ValueError`` for a run the store does not hold (the CLI refuses that by
    name before calling, in the wording every read-style subcommand shares)
    and for the impossible states the module docstring lists.
    """
    run = repo.get_run(conn, run_id)
    if run is None:
        raise ValueError(f"run {run_id!r} does not exist")
    scheduler_row = repo.get_scheduler_state(conn, run_id)
    by_status = _by_status(repo.attempt_status_counts(conn, run_id))
    ledger = repo.require_current_account_state(conn, run_id)
    # One read for the tail and the latest: the tail's first row is the
    # latest, read even when the tail itself is asked to be empty.
    snapshots = tuple(
        _snapshot(row) for row in repo.recent_account_snapshots(conn, run_id, limit=max(recent, 1))
    )
    return RunStatus(
        schema_version=SCHEMA_VERSION,
        source=SOURCE,
        generated_at=_generated_at(now),
        run_id=run_id,
        mode=str(run["mode"]),
        created_at=str(run["created_at"]),
        initial_balance_usdc=_dec(run["initial_balance_usdc"]),
        store_schema_version=stored_schema_version(conn),
        scheduler=_scheduler(scheduler_row),
        safe_mode=_safe_mode(scheduler_row),
        cycles=Cycles(
            total=sum(by_status.values()),
            by_status=by_status,
            recent=tuple(
                _cycle(row) for row in repo.recent_decision_cycles(conn, run_id, limit=recent)
            ),
        ),
        models=tuple(_model_bucket(row) for row in repo.model_regimes(conn, run_id)),
        account=Account(
            ledger=Ledger(
                wallet_balance=_dec(ledger.wallet_balance),
                realized_pnl=_dec(ledger.realized_pnl),
                total_fees=_dec(ledger.total_fees),
                net_funding_pnl=_dec(ledger.net_funding_pnl),
            ),
            snapshot=snapshots[0] if snapshots else None,
            recent_snapshots=snapshots[:recent],
        ),
        positions=tuple(_position(row) for row in repo.current_position_rows(conn, run_id)),
        recent_fills=tuple(_fill(row) for row in repo.recent_fills(conn, run_id, limit=recent)),
        kill_switch_events=tuple(
            _kill_switch_event(row)
            for row in repo.recent_kill_switch_events(conn, run_id, limit=recent)
        ),
        safe_mode_events=tuple(
            _safe_mode_event(row)
            for row in repo.recent_safe_mode_events(conn, run_id, limit=recent)
        ),
    )


# --- rendering ----------------------------------------------------------------


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"a status carries no {type(value).__name__} (got {value!r})")


def status_json(status: RunStatus) -> str:
    """The status as an ASCII JSON document, field by field, ``Decimal`` as exact text."""
    return json.dumps(asdict(status), default=_json_default, indent=2)


def _fmt(value: Decimal | None) -> str:
    return "n/a" if value is None else str(value)


def _one_line(text: str | None, limit: int = _LINE_TEXT_LIMIT) -> str:
    """Free text as one printable ASCII line of at most ``limit`` characters.

    A line break would read as a second line, a control character could do
    anything to a terminal, and a character outside the console's code page
    would stop the command on Windows; the last is spelled as its escape, as
    the Uniswap package's status spells it.
    """
    if text is None:
        return ""
    line = " ".join(text.split())
    printable = "".join(char if char.isprintable() else "?" for char in line)
    ascii_line = printable.encode("ascii", "backslashreplace").decode()
    return ascii_line if len(ascii_line) <= limit else ascii_line[: limit - 3] + "..."


def _breadcrumb_line(crumb: Breadcrumb) -> str:
    if crumb.status is None:
        return "n/a"
    line = f"{crumb.status} at {crumb.at or 'n/a'}"
    return f"{line} ({_one_line(crumb.error)})" if crumb.error else line


def _counts(by_status: Mapping[str, int]) -> str:
    return ", ".join(f"{status} {count}" for status, count in by_status.items()) or "none"


def _position_line(position: Position) -> str:
    if position.size == 0:
        core = f"{position.symbol} flat"
    else:
        side = "long" if position.size > 0 else "short"
        core = f"{position.symbol} {side} {abs(position.size)} entry {position.entry_price}"
    return (
        f"{core} realized {position.realized_pnl} sl {_fmt(position.stop_loss_price)} "
        f"tp {_fmt(position.take_profit_price)} liq {_fmt(position.exchange_liquidation_price)} "
        f"(updated {position.updated_at})"
    )


def _cycle_line(cycle: Cycle) -> str:
    kind = cycle.status + (f" (try {cycle.attempt_count})" if cycle.attempt_count > 1 else "")
    verdict = cycle.verdict
    if verdict is None:
        return _event_line(cycle.scheduled_at, kind, cycle.error_type, cycle.error_message)
    head = f"  {cycle.scheduled_at}  {kind}"
    order = "order" if verdict.order_created else f"no order ({_one_line(verdict.no_order_reason)})"
    gate = verdict.risk_action
    if verdict.risk_reason:
        gate += f" ({_one_line(verdict.risk_reason)})"
    line = (
        f"{head}  {verdict.symbol} {verdict.target_side or verdict.decision_mode}  "
        f"requested {_fmt(verdict.requested_target_margin_pct)}% "
        f"approved {_fmt(verdict.approved_target_margin_pct)}%  "
        f"conf {_fmt(verdict.confidence)}  {gate}  {order}"
    )
    return f"{line}  | {_one_line(verdict.decision_reason)}" if verdict.decision_reason else line


def _snapshot_text(snapshot: AccountSnapshot) -> str:
    return (
        f"equity {snapshot.account_equity} available {snapshot.available_balance} "
        f"unrealized {snapshot.unrealized_pnl} total_pnl {snapshot.total_pnl} "
        f"notional {snapshot.total_position_notional} "
        f"leverage {_fmt(snapshot.effective_leverage)} margin_ratio {_fmt(snapshot.margin_ratio)}"
    )


def _fill_line(fill: Fill) -> str:
    line = (
        f"  {fill.timestamp}  {fill.symbol} {fill.side} {fill.fill_qty} @ {fill.fill_price}  "
        f"fee {fill.fee}  realized {fill.realized_pnl_delta}  {fill.liquidity_type}"
    )
    return f"{line}  ({_one_line(fill.fill_reason)})" if fill.fill_reason else line


def _event_line(timestamp: str, kind: str, *parts: str | None) -> str:
    tail = "  ".join(_one_line(part) for part in parts if part)
    return f"  {timestamp}  {kind}  {tail}".rstrip()


def _listing(title: str, lines: list[str], *, of: int | None = None) -> list[str]:
    if not lines:
        return [f"{title}: none"]
    count = f"{len(lines)} of {of}" if of is not None and of != len(lines) else str(len(lines))
    return [f"{title} ({count}, newest first):", *lines]


def status_lines(status: RunStatus) -> list[str]:
    """The status for an operator: ``key: value`` lines, then the recent cycles, snapshots, fills and events."""
    lines = [
        f"run_id: {status.run_id}",
        f"mode: {status.mode}",
        f"created_at: {status.created_at}",
        f"initial_balance_usdc: {status.initial_balance_usdc}",
        f"store_schema_version: {status.store_schema_version}",
        f"generated_at: {status.generated_at}",
    ]
    scheduler = status.scheduler
    if scheduler is None:
        lines.append("scheduler: none (no cycle scheduled yet)")
    else:
        lines += [
            f"last_decision_at: {scheduler.last_decision_at or 'n/a'}",
            f"next_decision_at: {scheduler.next_decision_at or 'n/a'}",
            f"in_progress_attempt: {scheduler.in_progress_attempt_id or 'none'}",
            f"lease_heartbeat_at: {scheduler.lease_heartbeat_at or 'none'}",
            f"scheduler_updated_at: {scheduler.updated_at}",
            f"export: {_breadcrumb_line(scheduler.export)}",
            f"replay: {_breadcrumb_line(scheduler.replay)}",
            f"config_drift: {_breadcrumb_line(scheduler.config_drift)}",
        ]
    safe_mode = status.safe_mode
    lines.append(
        "safe_mode: none"
        if safe_mode is None
        else f"safe_mode: {safe_mode.type} since {safe_mode.entered_at} "
        f"({_one_line(safe_mode.reason)})"
    )
    lines.append(f"cycles: {status.cycles.total} ({_counts(status.cycles.by_status)})")
    if status.models:
        lines += [
            f"model: {bucket.model or 'n/a'} prompt_version={bucket.prompt_version or 'n/a'} "
            f"prompts={bucket.prompts} first={bucket.first_at} last={bucket.last_at}"
            for bucket in status.models
        ]
    else:
        lines.append("model: none (no prompt built yet)")
    ledger, snapshot = status.account.ledger, status.account.snapshot
    lines.append(
        f"ledger: wallet {ledger.wallet_balance} realized {ledger.realized_pnl} "
        f"fees {ledger.total_fees} funding {ledger.net_funding_pnl}"
    )
    lines.append(
        "snapshot: none"
        if snapshot is None
        else f"snapshot: at {snapshot.timestamp} {_snapshot_text(snapshot)}"
    )
    lines += [f"position: {_position_line(p)}" for p in status.positions] or ["position: none"]
    lines += _listing(
        "recent cycles", [_cycle_line(c) for c in status.cycles.recent], of=status.cycles.total
    )
    lines += _listing(
        "recent snapshots",
        [f"  {s.timestamp}  {_snapshot_text(s)}" for s in status.account.recent_snapshots],
    )
    lines += _listing("recent fills", [_fill_line(f) for f in status.recent_fills])
    lines += _listing(
        "kill_switch_events",
        [
            _event_line(e.timestamp, e.event_type, e.detail, e.error_message)
            for e in status.kill_switch_events
        ],
    )
    lines += _listing(
        "safe_mode_events",
        [
            _event_line(
                e.timestamp, e.event_type, e.safe_mode_type, e.reason, e.released_by, e.detail
            )
            for e in status.safe_mode_events
        ],
    )
    return lines
