"""One hand-built run, eleven questions long, that every suite here scores.

The fixture is written as a TABLE (:data:`ROWS`) so an expected number in a
test can be re-derived by hand from the row it names: marks are round
numbers, one cycle is missing (slot 4 — an ``api_failed`` round that wrote
no ``ai_inputs`` row, whose later mark only the research store holds), one
question has no answer (slot 11 — a round that wrote its input and then
failed), and the answers cover every reading the scorecard makes: an
approved target from flat, a maintain, a clamped flip, a fail-closed round
of each kind, a flat target, a rejection, an approved target inside the
deadband that created no order, and a levered flip.

The same table is also written into a real paper store through the perp
package's own repository functions, so the store reader is tested against
rows encoded the way the daemon encodes them, not against hand-written SQL.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from contrib.hyperliquid_perp.domains.perp.schema import Candle
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.replay.score import Answer, Question, slot_of
from contrib.replay.upstream import (
    Database,
    ResearchStore,
    TargetSide,
    from_epoch_ms,
    interval_to_ms,
)

STEP_MS = interval_to_ms("4h")
# A 4h-aligned anchor well inside the decodable range; slot 0 of the fixture
# closes one millisecond before it.
ANCHOR_MS = 1_800_000_000_000 - (1_800_000_000_000 % STEP_MS)
RUN_ID = "paper-FIX"
COIN = "BTC"


def at_ms(slot: int) -> int:
    """The ``candle_end`` of the fixture's slot ``slot``: the venue's close, 1 ms before the open."""
    return ANCHOR_MS + slot * STEP_MS - 1


@dataclass(frozen=True)
class Row:
    slot: int
    mark: str
    side: str
    margin: str
    leverage: str
    bias: str | None
    answer: dict | None


# Every answer is in the shape the gate writes (``risk_gate.RiskGateResult``):
# a rejection and a fail-closed round are ``maintain_current`` with no
# approved margin and no order, the rejection keeping the side, margin and
# confidence it refused; ``no_order_reason`` is set exactly when no order was.
# fmt: off
ROWS: tuple[Row, ...] = (
    Row(0, "100", "flat", "0", "1", "long",
        {"decision_mode": "set_target", "target_side": "long", "requested": "30", "approved": "30",
         "risk_action": "approved", "risk_reason": None, "confidence": "0.8",
         "order_created": True, "no_order_reason": None}),
    Row(1, "101", "long", "30", "1", "long",
        {"decision_mode": "maintain_current", "target_side": None, "requested": None, "approved": None,
         "risk_action": "approved", "risk_reason": None, "confidence": None,
         "order_created": False, "no_order_reason": "maintain_current"}),
    Row(2, "99", "long", "30", "1", "short",
        {"decision_mode": "set_target", "target_side": "short", "requested": "40", "approved": "20",
         "risk_action": "clamped", "risk_reason": "exceeds_max_target_margin_pct",
         "confidence": "0.7", "order_created": True, "no_order_reason": None}),
    Row(3, "102", "short", "20", "1", None,
        {"decision_mode": "maintain_current", "target_side": None, "requested": None, "approved": None,
         "risk_action": "invalid_fail_closed", "risk_reason": "invalid_output", "confidence": None,
         "order_created": False, "no_order_reason": "invalid_fail_closed"}),
    # slot 4: no row at all (api_failed before the input was written); the
    # research store holds the bar that closed there, at 104.
    Row(5, "103", "short", "20", "1", "flat",
        {"decision_mode": "set_target", "target_side": "flat", "requested": "0", "approved": "0",
         "risk_action": "approved", "risk_reason": None, "confidence": "0.5",
         "order_created": True, "no_order_reason": None}),
    # A low-confidence rejection: the gate records it as maintain_current and
    # keeps the long 30 it refused.
    Row(6, "105", "flat", "0", "1", "long",
        {"decision_mode": "maintain_current", "target_side": "long", "requested": "30",
         "approved": None, "risk_action": "rejected", "risk_reason": "low_confidence",
         "confidence": "0.2", "order_created": False, "no_order_reason": "rejected"}),
    Row(7, "104", "long", "28", "1", "long",
        {"decision_mode": "set_target", "target_side": "long", "requested": "30", "approved": "30",
         "risk_action": "approved", "risk_reason": None, "confidence": "0.9",
         "order_created": False, "no_order_reason": "within_deadband"}),
    Row(8, "106", "long", "28", "1", "short",
        {"decision_mode": "maintain_current", "target_side": None, "requested": None, "approved": None,
         "risk_action": "invalid_fail_closed", "risk_reason": "truncated_output", "confidence": None,
         "order_created": False, "no_order_reason": "invalid_fail_closed"}),
    Row(9, "108", "long", "28", "2", "short",
        {"decision_mode": "set_target", "target_side": "short", "requested": "50", "approved": "50",
         "risk_action": "approved", "risk_reason": None, "confidence": "0.95",
         "order_created": True, "no_order_reason": None}),
    Row(10, "107", "short", "50", "1", "short",
        {"decision_mode": "maintain_current", "target_side": None, "requested": None, "approved": None,
         "risk_action": "approved", "risk_reason": None, "confidence": None,
         "order_created": False, "no_order_reason": "maintain_current"}),
    Row(11, "109", "short", "50", "1", "short", None),
)
# fmt: on

# The bar the research store holds for the missing slot: opened at slot 3's
# close, closed at slot 4's.
RESEARCH_CANDLE = Candle(
    open_time=ANCHOR_MS + 3 * STEP_MS,
    close_time=at_ms(4),
    open=Decimal("102"),
    high=Decimal("105"),
    low=Decimal("101"),
    close=Decimal("104"),
    volume=Decimal("1"),
)
# Keyed by the ABSOLUTE slot, as ``load_research_closes`` keys it.
RESEARCH_CLOSES = {slot_of(at_ms(4), STEP_MS): 104.0}

MAX_MARGIN_PCT = "60"
PROMPT_VERSION = "phase2-target-v6"
MODEL = "test-model"
CONTEXT_SHAPE = "perp"

# The shapes the real store has that a row-per-question reader misreads
# (measured on the paper store 2026-09-23): a cycle retried after a failed
# API call writes an EARLIER input row in the same slot, never answered —
# here slot 2's first try, at a slightly different mark — and a cycle that
# failed before any input was written leaves an attempt with no input at
# all — here one at slot 12.
RETRY_INPUTS: dict[int, str] = {2: "98"}
ATTEMPTS_WITHOUT_INPUT = 1
_INPUTLESS_SLOT = 12
# A cycle still open when the store was copied: an input row, no output,
# status ``in_progress``. Not a decision yet, so not a question.
IN_PROGRESS_SLOT = 13
IN_PROGRESS_ROW = Row(IN_PROGRESS_SLOT, "110", "short", "50", "1", "short", None)

EQUITY = "1000"
STRATEGY_ID = "btc-4h-maker#9"


def input_id(slot: int) -> str:
    return f"in-{slot:02d}"


def retry_input_id(slot: int) -> str:
    return f"in-{slot:02d}-try1"


def attempt_id(slot: int) -> str:
    return decision_attempt_id(RUN_ID, from_epoch_ms(at_ms(slot)))


def payload_name(slot: int) -> str:
    return f"{COIN}-{slot:02d}.json"


def fixture_questions(*, reports_present: bool | None = None) -> list[Question]:
    return [
        Question(
            input_id=input_id(row.slot),
            at_ms=at_ms(row.slot),
            mark=float(row.mark),
            current_side=TargetSide(row.side),
            current_margin_pct=float(row.margin),
            leverage=float(row.leverage),
            max_margin_pct=float(MAX_MARGIN_PCT),
            research_bias=None if row.bias is None else TargetSide(row.bias),
            prompt_version=PROMPT_VERSION,
            model=MODEL,
            context_shape=CONTEXT_SHAPE,
            reports_present=reports_present,
            account_equity=float(EQUITY),
            strategy_id=None if row.bias is None else STRATEGY_ID,
            attempt_id=attempt_id(row.slot),
        )
        for row in ROWS
    ]


def fixture_answers() -> list[Answer]:
    return [
        Answer(
            input_id=input_id(row.slot),
            decision_mode=row.answer["decision_mode"],
            target_side=row.answer["target_side"],
            requested_margin_pct=_maybe(row.answer["requested"]),
            approved_margin_pct=_maybe(row.answer["approved"]),
            risk_action=row.answer["risk_action"],
            risk_reason=row.answer["risk_reason"],
            confidence=_maybe(row.answer["confidence"]),
            order_created=row.answer["order_created"],
            no_order_reason=row.answer["no_order_reason"],
        )
        for row in ROWS
        if row.answer is not None
    ]


def _maybe(text: str | None) -> float | None:
    return None if text is None else float(text)


def run_config(*, style: str = "taker", interval: str = "4h") -> dict:
    """The genesis ``config_json`` subset the paper daemon records, at the fixture's terms."""
    return {
        "coin": COIN,
        "market_data": {"candle_interval": interval},
        "paper_trading": {
            "execution": {
                "taker_fee_rate": "0.00045",
                "fill_model": {"style": style, "slippage_bps": "5", "maker_fee_rate": "0.00015"},
            }
        },
    }


def write_paper_store(
    path: Path,
    *,
    payload_root: Path,
    config: dict | None = None,
    rows: Sequence[Row] = ROWS,
    run_id: str = RUN_ID,
    mode: str = "paper",
) -> Path:
    """A real store holding the fixture run, written through the repository functions.

    Every row becomes one decision attempt: ``completed`` with its output,
    or ``api_failed`` holding the input alone. The retry inputs and the
    input-less attempt are written whatever ``rows`` holds, so a subset
    still has the shapes the reader must skip.
    """
    with Database(path) as db, db.transaction() as conn:
        repo.insert_run(
            conn,
            run_id=run_id,
            mode=mode,
            initial_balance_usdc=Decimal("1000"),
            schema_version=SCHEMA_VERSION,
            config_json=None if config is None else json.dumps(config),
        )
        for row in rows:
            stamp = from_epoch_ms(at_ms(row.slot))
            tries = 1
            if row.slot in RETRY_INPUTS:
                tries = 2
                _insert_input(
                    conn, row, retry_input_id(row.slot), RETRY_INPUTS[row.slot], payload_root,
                    run_id=run_id, mode=mode,
                )
            _insert_input(
                conn, row, input_id(row.slot), row.mark, payload_root, run_id=run_id, mode=mode
            )
            output_id = None if row.answer is None else f"out-{row.slot:02d}"
            if row.answer is not None:
                answer = row.answer
                repo.insert_ai_output(
                    conn,
                    output_id=output_id,
                    timestamp=stamp,
                    mode=mode,
                    run_id=run_id,
                    input_id=input_id(row.slot),
                    symbol=COIN,
                    decision_mode=answer["decision_mode"],
                    target_side=answer["target_side"],
                    requested_target_margin_pct=_decimal(answer["requested"]),
                    approved_target_margin_pct=_decimal(answer["approved"]),
                    risk_action=answer["risk_action"],
                    risk_reason=answer["risk_reason"],
                    confidence=_decimal(answer["confidence"]),
                    order_created=answer["order_created"],
                    no_order_reason=answer["no_order_reason"],
                )
            insert_attempt(
                conn,
                row.slot,
                input_id=input_id(row.slot),
                output_id=output_id,
                tries=tries,
                status="completed" if row.answer is not None else "api_failed",
                run_id=run_id,
                mode=mode,
            )
        insert_attempt(
            conn, _INPUTLESS_SLOT, input_id=None, tries=3, status="api_failed",
            run_id=run_id, mode=mode,
        )
        _insert_input(
            conn, IN_PROGRESS_ROW, input_id(IN_PROGRESS_SLOT), IN_PROGRESS_ROW.mark, payload_root,
            run_id=run_id, mode=mode,
        )
        insert_attempt(
            conn, IN_PROGRESS_SLOT, input_id=input_id(IN_PROGRESS_SLOT), status="in_progress",
            run_id=run_id, mode=mode,
        )
    return path


def insert_attempt(
    conn,
    slot: int,
    *,
    input_id: str | None,
    output_id: str | None = None,
    tries: int = 1,
    status: str,
    run_id: str = RUN_ID,
    mode: str = "paper",
) -> None:
    """One ``decision_attempts`` row for the fixture's slot, the way the scheduler keys it."""
    stamp = from_epoch_ms(at_ms(slot))
    repo.insert_decision_attempt(
        conn,
        decision_attempt_id=decision_attempt_id(run_id, stamp),
        timestamp=stamp,
        mode=mode,
        run_id=run_id,
        scheduled_at=stamp,
        input_id=input_id,
        output_id=output_id,
        attempt_count=tries,
        status=status,
    )


def _insert_input(
    conn, row: Row, identifier: str, mark: str, payload_root: Path, *, run_id: str, mode: str
) -> None:
    stamp = from_epoch_ms(at_ms(row.slot))
    repo.insert_ai_input(
        conn,
        input_id=identifier,
        timestamp=stamp,
        mode=mode,
        run_id=run_id,
        symbol=COIN,
        candle_end=stamp,
        mark_price=Decimal(mark),
        account_equity=Decimal(EQUITY),
        current_position_side=row.side,
        current_position_size=Decimal("0"),
        current_margin_pct=Decimal(row.margin),
        configured_leverage=Decimal(row.leverage),
        max_target_margin_pct=Decimal(MAX_MARGIN_PCT),
        input_payload_path=str(payload_root / payload_name(row.slot)),
        prompt_version=PROMPT_VERSION,
        model=MODEL,
        context_shape=CONTEXT_SHAPE,
        autoresearch_bias=row.bias,
        autoresearch_strategy_id=None if row.bias is None else STRATEGY_ID,
    )


def _decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def write_research_store(path: Path) -> Path:
    with ResearchStore(path) as store:
        store.upsert_candles(COIN, "4h", [RESEARCH_CANDLE])
    return path
