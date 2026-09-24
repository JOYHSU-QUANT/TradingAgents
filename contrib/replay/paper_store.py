"""Reading one paper run out of its store as questions and answers.

The only module here that speaks SQL to the perp store, and it only reads:
``runs`` for the terms the run was traded under (the fill model, and for
the past papers the ``risk:`` / ``decision:`` blocks its gate ran under),
and ``decision_attempts`` joined to the ``ai_inputs`` row and the
``ai_outputs`` row each attempt names. The store is opened by the caller through the perp package's
own :class:`~contrib.hyperliquid_perp.persistence.db.Database` with
``migrate=False`` — a report-only command never upgrades a store a daemon
may own (the perp CLI's rule) — so every column read here exists: a store
behind the current schema is refused at open, not misread here.

A question is a DECISION ATTEMPT, not an ``ai_inputs`` row. One scheduled
cycle writes an input row per try (``#in1``, ``#in2``, ``#in3`` on the paper
store — measured 2026-09-23: 22 of run 3's 94 input rows and 4 of run 4's
55 were earlier tries), the attempt's ``input_id`` is moved to the latest
try, and only that one can carry the output. Read row by row, the earlier
tries would be unanswered questions within minutes of the answered one and
the pairing would refuse the run; read through the attempt, each
cycle is one question — its final input — answered by its output or not
(``api_failed``: the last try's input, no output). An attempt that failed
before any input was written has no question at all and is counted apart,
and so is one not yet terminal — a store copied while the daemon is
mid-cycle holds one, and it is not a decision yet. How many cycles were
retried, and how many extra tries they took, is counted too (decided
2026-09-23: reported beside the fail-closed rate, not folded into it).

Values cross the seam the way the store wrote them: prices and margins as
``TEXT`` decimals (decoded through ``Decimal``, then to the float the
scorecard's statistics are computed in), instants as ISO-8601 UTC (decoded
by the perp package's own ``parse_instant`` / ``epoch_ms``), enum text
through the vocabulary that wrote it, each refused by row and column.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Final, TypeVar

from .score import Answer, Question, ScoreError
from .upstream import (
    TERMINAL_ATTEMPT_STATUSES,
    CostModel,
    Database,
    DecisionConfig,
    DecisionMode,
    FillRole,
    MarketDataConfig,
    PaperTradingConfig,
    ResearchStore,
    RiskAction,
    RiskConfig,
    TargetSide,
    epoch_ms,
    get_run,
    interval_to_ms,
    parse_instant,
    sidecar_path,
)

__all__ = [
    "REPORTS_SUFFIX",
    "Decisions",
    "InputFacts",
    "RunFacts",
    "answer_from_row",
    "gate_config",
    "load_decisions",
    "load_research_closes",
    "run_facts",
]

# The sidecar the engine writes beside each input payload (PR 0,
# ``integration/decision_reports.py``). Spelled here as well because the
# writer keeps it as a call-site literal; ``tests/test_paper_store.py`` pins
# the two spellings to each other.
REPORTS_SUFFIX: Final = ".reports.json"

_DECISIONS_SQL: Final = """
SELECT a.decision_attempt_id, a.scheduled_at, a.status, a.attempt_count,
       a.input_id AS attempt_input_id, a.output_id AS attempt_output_id,
       i.input_id, i.symbol, i.timestamp, i.mark_price, i.account_equity,
       i.current_position_side, i.current_margin_pct, i.configured_leverage,
       i.max_target_margin_pct, i.autoresearch_bias, i.autoresearch_strategy_id,
       i.prompt_version, i.model, i.context_shape, i.input_payload_path,
       i.input_payload_hash, i.current_position_size,
       o.output_id, o.decision_mode, o.target_side, o.requested_target_margin_pct,
       o.approved_target_margin_pct, o.risk_action, o.risk_reason, o.confidence,
       o.order_created, o.no_order_reason
  FROM decision_attempts AS a
  LEFT JOIN ai_inputs AS i ON i.input_id = a.input_id
  LEFT JOIN ai_outputs AS o ON o.output_id = a.output_id
 WHERE a.run_id = ?
 ORDER BY a.scheduled_at, a.decision_attempt_id
"""


@dataclass(frozen=True)
class RunFacts:
    """The terms a run was traded under, as its genesis record states them.

    ``costs`` is the run's own fill model (plan §3-6): the taker fee, and the
    maker fee and slippage of ``fill_model``, with ``fill_role`` following
    ``fill_model.style``. Its ``leverage`` is left at 1 on purpose — the
    scorecard scales each row's exposure by that row's own configured
    leverage, which the store records per decision. ``config_recorded`` is
    false for a genesis row with no ``config_json`` at all; ``missing_blocks``
    names the blocks a recorded genesis lacks (``market_data``,
    ``paper_trading``, ``coin`` — older genesis rows predate some of them,
    ``cli/_drift.py`` says which). A missing block means its terms are the
    parser's defaults, and the coin is read off the run's first input row;
    the report says so either way.
    """

    run_id: str
    mode: str
    coin: str
    interval: str
    step_ms: int
    costs: CostModel
    config_recorded: bool
    missing_blocks: tuple[str, ...]

    def describe_source(self) -> str:
        """Where the costs and the interval came from, for the report's first line."""
        if not self.config_recorded:
            return "defaults, no config_json"
        defaulted = [block for block in self.missing_blocks if block != "coin"]
        notes = []
        if defaulted:
            notes.append(f"{', '.join(defaulted)} absent from the genesis: defaults used")
        if "coin" in self.missing_blocks:
            notes.append("coin read from the run's first input row")
        if notes:
            return f"the run's recorded config, except {'; '.join(notes)}"
        return "the run's recorded config"


def _config(run_id: str, text: object) -> dict:
    if text is None:
        return {}
    try:
        parsed = json.loads(str(text))
    except ValueError as exc:
        raise ScoreError(f"run {run_id!r}: config_json is not JSON ({exc})") from exc
    if not isinstance(parsed, dict):
        raise ScoreError(f"run {run_id!r}: config_json is not an object")
    return parsed


def run_facts(db: Database, run_id: str) -> RunFacts | None:
    """The run's genesis terms, or ``None`` for a run the store does not hold."""
    row = get_run(db.conn, run_id)
    if row is None:
        return None
    config = _config(run_id, row["config_json"])
    try:
        execution = PaperTradingConfig.from_dict(config.get("paper_trading")).execution
        market_data = MarketDataConfig.from_dict(config.get("market_data"))
        costs = CostModel(
            taker_fee_rate=float(execution.taker_fee_rate),
            maker_fee_rate=float(execution.fill_model.maker_fee_rate),
            slippage_bps=float(execution.fill_model.slippage_bps),
            fill_role=FillRole(execution.fill_model.style),
            leverage=1.0,
        )
    except (TypeError, ValueError) as exc:
        raise ScoreError(f"run {run_id!r}: config_json does not parse ({exc})") from exc
    coin = config.get("coin")
    if not isinstance(coin, str) or not coin:
        first = db.conn.execute(
            "SELECT symbol FROM ai_inputs WHERE run_id = ? ORDER BY timestamp LIMIT 1", (run_id,)
        ).fetchone()
        coin = "?" if first is None else str(first["symbol"])
    recorded = row["config_json"] is not None
    # Without a genesis nothing is "missing" — everything is defaulted, and
    # ``describe_source`` says so in one sentence rather than three.
    missing = tuple(
        block for block in ("market_data", "paper_trading", "coin") if recorded and block not in config
    )
    return RunFacts(
        run_id=run_id,
        mode=str(row["mode"]),
        coin=coin,
        interval=market_data.candle_interval,
        step_ms=interval_to_ms(market_data.candle_interval),
        costs=costs,
        config_recorded=recorded,
        missing_blocks=missing,
    )


def gate_config(db: Database, run_id: str) -> tuple[RiskConfig, DecisionConfig]:
    """The ``risk:`` and ``decision:`` blocks the run's genesis recorded, parsed as the daemon parses them.

    A replayed answer passes through the run's own gate (plan PR 2): the
    thresholds, caps and deadband the recorded answers met. Both blocks have
    been in the genesis subset since the first run (``cli/_drift.py``), so a
    genesis without them, or a run with no genesis config at all, is refused
    by name. Gating at the parser's defaults instead would compare a
    variant against a gate the run never had.
    """
    row = get_run(db.conn, run_id)
    if row is None:
        raise ScoreError(f"run {run_id!r} not found")
    if row["config_json"] is None:
        raise ScoreError(
            f"run {run_id!r} recorded no genesis config, so the gate its answers met is unknown"
        )
    config = _config(run_id, row["config_json"])
    missing = [block for block in ("risk", "decision") if block not in config]
    if missing:
        raise ScoreError(
            f"run {run_id!r}: the genesis config lacks {', '.join(missing)}, so the gate its "
            "answers met is unknown"
        )
    try:
        return RiskConfig.from_dict(config["risk"]), DecisionConfig.from_dict(config["decision"])
    except (TypeError, ValueError) as exc:
        raise ScoreError(
            f"run {run_id!r}: the genesis risk/decision blocks do not parse ({exc})"
        ) from exc


_T = TypeVar("_T")


def _cell(row: sqlite3.Row, column: str, convert: Callable[[str], _T]) -> _T | None:
    """One column of ``row`` through ``convert``; ``None`` stays ``None``."""
    value = row[column]
    if value is None:
        return None
    try:
        return convert(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ScoreError(f"{row['input_id']}: {column}: {exc}") from exc


def _need(row: sqlite3.Row, column: str, convert: Callable[[str], _T]) -> _T:
    """:func:`_cell`, refusing a NULL by name — the row cannot be scored without it."""
    value = _cell(row, column, convert)
    if value is None:
        raise ScoreError(f"{row['input_id']}: {column} is NULL; the row cannot be scored")
    return value


def _float(text: str) -> float:
    return float(Decimal(text))


def _question(row: sqlite3.Row, reports_root: Path | None) -> Question:
    # The decision instant — when the mark was read — not ``candle_end``,
    # the closed bar's stamp: the scheduler rolls, so the two drift apart by
    # up to a bar (``score`` module docstring).
    stamp = _need(row, "timestamp", str)
    try:
        at_ms = epoch_ms(parse_instant(stamp), what=f"{row['input_id']}: timestamp")
    except ValueError as exc:
        raise ScoreError(str(exc)) from exc
    reports: bool | None = None
    if reports_root is not None:
        name = _payload_name(row["input_payload_path"])
        reports = name is not None and (
            sidecar_path(reports_root / name, REPORTS_SUFFIX).is_file()
        )
    # The gate imputes no margin for a flat book, and none for a sized
    # position when the account has no equity to impute it from; the first
    # is a zero, the second is a row the scorecard refuses by name.
    side = _need(row, "current_position_side", TargetSide)
    if side is TargetSide.FLAT:
        margin = _cell(row, "current_margin_pct", _float) or 0.0
    else:
        margin = _need(row, "current_margin_pct", _float)
    return Question(
        input_id=str(row["input_id"]),
        at_ms=at_ms,
        mark=_need(row, "mark_price", _float),
        current_side=side,
        current_margin_pct=margin,
        leverage=_need(row, "configured_leverage", _float),
        max_margin_pct=_need(row, "max_target_margin_pct", _float),
        research_bias=_cell(row, "autoresearch_bias", TargetSide),
        prompt_version=row["prompt_version"],
        model=row["model"],
        context_shape=row["context_shape"],
        reports_present=reports,
        account_equity=_cell(row, "account_equity", _float),
        strategy_id=row["autoresearch_strategy_id"],
        attempt_id=str(row["decision_attempt_id"]),
    )


def answer_from_row(row: sqlite3.Row) -> Answer:
    """An answer row in the ``ai_outputs`` column names, as the scorecard's record.

    Also the replay store's decoder: its ``answers`` rows keep these column
    names, so a replayed answer is decoded, and refused by name, exactly as
    a recorded one is.
    """
    return Answer(
        input_id=str(row["input_id"]),
        decision_mode=_need(row, "decision_mode", DecisionMode),
        target_side=_cell(row, "target_side", TargetSide),
        requested_margin_pct=_cell(row, "requested_target_margin_pct", _float),
        approved_margin_pct=_cell(row, "approved_target_margin_pct", _float),
        risk_action=_need(row, "risk_action", RiskAction),
        risk_reason=row["risk_reason"],
        confidence=_cell(row, "confidence", _float),
        order_created=bool(_need(row, "order_created", int)),
        no_order_reason=row["no_order_reason"],
    )


@dataclass(frozen=True)
class InputFacts:
    """A question's input row as the gate and the payload lookup need it: exact, not floats.

    The past-papers command (plan PR 2) re-runs the gate on a replayed
    answer, and the gate takes ``Decimal`` account state; the scorecard's
    floats would move a target sitting on the deadband's edge. Decoded, not
    checked: whether a row can be put back through the gate is the replay's
    question to refuse by name (``replay.position_state``). ``payload_name`` is
    the file NAME of ``input_payload_path`` (the store holds the daemon
    host's absolute path), and ``payload_hash`` the digest recorded beside it.
    """

    input_id: str
    payload_name: str | None
    payload_hash: str | None
    mark: Decimal
    account_equity: Decimal | None
    side: TargetSide
    size: Decimal | None
    margin_pct: Decimal | None
    leverage: Decimal


def _payload_name(recorded: object) -> str | None:
    """The file NAME of a recorded payload path, whichever host's separators it uses.

    The store keeps the daemon host's absolute path; a copy is read on
    another host, where ``Path`` would not split the other platform's
    separators. ``PureWindowsPath`` splits on both the slash and the
    backslash (the rule ``persistence/backfill`` uses for the same column).
    """
    return None if recorded is None else PureWindowsPath(str(recorded)).name


def _input_facts(row: sqlite3.Row) -> InputFacts:
    return InputFacts(
        input_id=str(row["input_id"]),
        payload_name=_payload_name(row["input_payload_path"]),
        payload_hash=row["input_payload_hash"],
        mark=_need(row, "mark_price", Decimal),
        account_equity=_cell(row, "account_equity", Decimal),
        side=_need(row, "current_position_side", TargetSide),
        size=_cell(row, "current_position_size", Decimal),
        margin_pct=_cell(row, "current_margin_pct", Decimal),
        leverage=_need(row, "configured_leverage", Decimal),
    )


@dataclass(frozen=True)
class Decisions:
    """One run's decision attempts as the scorecard's records.

    ``without_input`` counts the attempts that failed before any input row
    was written (a context refusal, a market-data failure): cycles the run
    scheduled but never turned into a question, reported so a short run and
    a run that kept failing to build its prompt read differently.
    ``in_progress`` counts the attempts not yet terminal when the store was
    read; ``retried`` the QUESTIONS that took more than one try, and
    ``extra_tries`` how many tries beyond the first they took in all (an
    attempt that never wrote an input is counted under ``without_input``
    only, whatever its try count). ``inputs`` holds each question's
    :class:`InputFacts`, keyed by ``input_id``.
    """

    questions: list[Question]
    answers: list[Answer]
    without_input: int
    in_progress: int
    retried: int
    extra_tries: int
    inputs: Mapping[str, InputFacts]

    def describe(self) -> list[str]:
        """The counts that are not questions, one line each, only when non-zero."""
        lines = []
        if self.without_input:
            lines.append(
                "cycles that failed before an input row was written (not questions): "
                f"{self.without_input}"
            )
        if self.in_progress:
            lines.append(
                f"cycles still in progress when the store was read (left out): {self.in_progress}"
            )
        if self.retried:
            lines.append(
                f"attempts retried: {self.retried} ({self.extra_tries} extra tries); "
                "the fail-closed rate below counts final answers only"
            )
        return lines


def load_decisions(
    db: Database, run_id: str, *, reports_root: Path | None = None
) -> Decisions:
    """Every finished decision attempt of the run: its final input as the question, its output as the answer.

    An attempt with an input and no output is a question nobody answered
    (``api_failed`` after the last try) and comes back as a question only;
    one not yet terminal is counted and left out. With ``reports_root``,
    each question also says whether a ``.reports.json`` sits beside its
    payload (looked up by file NAME under that root, so a store copied away
    from its host still counts them).
    """
    questions: list[Question] = []
    answers: list[Answer] = []
    inputs: dict[str, InputFacts] = {}
    without_input = in_progress = retried = extra_tries = 0
    for row in db.conn.execute(_DECISIONS_SQL, (run_id,)):
        if row["status"] not in TERMINAL_ATTEMPT_STATUSES:
            in_progress += 1
            continue
        if row["attempt_input_id"] is None:
            without_input += 1
            continue
        if row["input_id"] is None:
            raise ScoreError(
                f"{row['decision_attempt_id']}: names input {row['attempt_input_id']!r}, which "
                "ai_inputs does not hold"
            )
        if row["attempt_output_id"] is not None and row["output_id"] is None:
            raise ScoreError(
                f"{row['decision_attempt_id']}: names output {row['attempt_output_id']!r}, "
                "which ai_outputs does not hold"
            )
        # The engine writes a terminal status and its output row in one
        # transaction, so a finished attempt without an output — or an
        # api_failed one with — is a store the scorecard must not guess at.
        if (row["status"] == "api_failed") != (row["attempt_output_id"] is None):
            raise ScoreError(
                f"{row['decision_attempt_id']}: status {row['status']!r} with "
                f"output {row['attempt_output_id']!r}; a finished attempt carries an output and "
                "an api_failed one none"
            )
        tries = int(row["attempt_count"] or 1)
        if tries > 1:
            retried += 1
            extra_tries += tries - 1
        questions.append(_question(row, reports_root))
        inputs[str(row["input_id"])] = _input_facts(row)
        if row["output_id"] is not None:
            answers.append(answer_from_row(row))
    return Decisions(questions, answers, without_input, in_progress, retried, extra_tries, inputs)


def load_research_closes(
    store: ResearchStore,
    *,
    coin: str,
    interval: str,
    since_ms: int | None = None,
    until_ms: int | None = None,
) -> Mapping[int, float]:
    """The series' closes keyed by ``close_time``, for the bars opening in ``[since, until]``.

    Read through the store's own :meth:`iter_candles`, which re-checks each
    bar's invariants — a corrupt research row fails there, by field, not
    here as a nonsense return. The window is the caller's lock: a bar that
    opens past ``until_ms`` is never decoded, let alone paired.
    """
    return {
        c.close_time: float(c.close)
        for c in store.iter_candles(coin, interval, since_ms=since_ms, until_ms=until_ms)
    }
