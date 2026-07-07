"""SQLite schema for the Phase 2 paper-trading store (phase2-data.md).

SQLite is the single source of truth (phase2-data §1). This module owns the DDL:
the eight export logical tables (§5–§12, one-to-one with the CSV exports) and the
seven internal runtime tables (§1.2, plus ``run_seed_positions`` — the persisted
replay genesis). ``db.apply_migrations`` runs these in version order and records
each in ``schema_migrations``.

Storage conventions (this module's rule, so no money value ever passes through
a REAL float):

- money / price / quantity / rate values are stored as **TEXT** — the string form
  of a :class:`~decimal.Decimal`, so no precision is lost to a REAL float;
- timestamps are stored as **TEXT** ISO-8601 UTC strings (lexically sortable);
- booleans are stored as INTEGER ``0`` / ``1``.

The dedup / exactly-once UNIQUE constraints are the heart of the restart-safety
guarantee: ``fills.slice_id`` (one fill per TWAP slice), ``funding_events`` on
``(run_id, symbol, funding_timestamp)`` (funding once per settlement), and the
deterministic primary keys on ``decision_attempts`` / ``funding_events``. SQLite
treats NULLs as distinct in a UNIQUE column, so a ``paper_market`` / SL / TP fill
(no ``slice_id``) is never blocked by the slice constraint.
"""

from __future__ import annotations

__all__ = ["MIGRATIONS", "SCHEMA_MIGRATIONS_DDL", "SCHEMA_VERSION"]

SCHEMA_VERSION = 2

# --------------------------------------------------------------------------
# Export logical tables (phase2-data §5–§12) — one-to-one with CSV exports.
# --------------------------------------------------------------------------

_AI_INPUTS = """
CREATE TABLE ai_inputs (
    input_id                    TEXT PRIMARY KEY,
    timestamp                   TEXT NOT NULL,
    mode                        TEXT NOT NULL,
    run_id                      TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    candle_start                TEXT,
    candle_end                  TEXT,
    mark_price                  TEXT,
    mid_price                   TEXT,
    funding_rate                TEXT,
    wallet_balance              TEXT,
    account_equity              TEXT,
    available_balance           TEXT,
    realized_pnl                TEXT,
    unrealized_pnl              TEXT,
    total_fees                  TEXT,
    net_funding_pnl             TEXT,
    effective_leverage          TEXT,
    margin_ratio                TEXT,
    current_position_side       TEXT,
    current_position_size       TEXT,
    entry_price                 TEXT,
    position_notional           TEXT,
    current_margin_pct          TEXT,
    configured_leverage         TEXT,
    estimated_liquidation_price TEXT,
    stop_loss_price             TEXT,
    take_profit_price           TEXT,
    active_twap                 INTEGER,
    remaining_twap_qty          TEXT,
    last_fill_time              TEXT,
    max_target_margin_pct       TEXT,
    input_payload_path          TEXT,
    input_payload_hash          TEXT,
    prompt_version              TEXT,
    model                       TEXT
)
"""

_DECISION_ATTEMPTS = """
CREATE TABLE decision_attempts (
    decision_attempt_id TEXT PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    mode                TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    scheduled_at        TEXT NOT NULL,
    input_id            TEXT,
    output_id           TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    first_attempt_at    TEXT,
    last_attempt_at     TEXT,
    status              TEXT NOT NULL,
    error_type          TEXT,
    error_message       TEXT,
    next_decision_at    TEXT,
    UNIQUE (run_id, scheduled_at)
)
"""

_AI_OUTPUTS = """
CREATE TABLE ai_outputs (
    output_id                   TEXT PRIMARY KEY,
    timestamp                   TEXT NOT NULL,
    mode                        TEXT NOT NULL,
    run_id                      TEXT NOT NULL,
    input_id                    TEXT,
    decision_attempt_id         TEXT,
    symbol                      TEXT NOT NULL,
    decision_mode               TEXT NOT NULL,
    target_side                 TEXT,
    requested_target_margin_pct TEXT,
    approved_target_margin_pct  TEXT,
    risk_action                 TEXT NOT NULL,
    risk_reason                 TEXT,
    target_margin               TEXT,
    configured_leverage         TEXT,
    target_notional             TEXT,
    target_signed_notional      TEXT,
    current_signed_notional     TEXT,
    delta_notional              TEXT,
    mark_price                  TEXT,
    account_equity              TEXT,
    confidence                  TEXT,
    decision_reason             TEXT,
    key_risks                   TEXT,
    order_created               INTEGER NOT NULL,
    no_order_reason             TEXT
)
"""

_ORDERS = """
CREATE TABLE orders (
    order_id          TEXT PRIMARY KEY,
    timestamp         TEXT NOT NULL,
    mode              TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    output_id         TEXT,
    exchange_order_id TEXT,
    client_order_id   TEXT,
    parent_order_id   TEXT,
    flip_plan_id      TEXT,
    flip_leg          TEXT,
    symbol            TEXT NOT NULL,
    order_role        TEXT NOT NULL,
    side              TEXT NOT NULL,
    type              TEXT NOT NULL,
    price             TEXT,
    trigger_price     TEXT,
    qty               TEXT NOT NULL,
    filled_qty        TEXT NOT NULL DEFAULT '0',
    remaining_qty     TEXT,
    status            TEXT NOT NULL,
    status_reason     TEXT,
    reduce_only       INTEGER NOT NULL DEFAULT 0,
    active_from       TEXT,
    updated_at        TEXT NOT NULL
)
"""

_FILLS = """
CREATE TABLE fills (
    fill_id            TEXT PRIMARY KEY,
    timestamp          TEXT NOT NULL,
    mode               TEXT NOT NULL,
    run_id             TEXT NOT NULL,
    order_id           TEXT NOT NULL,
    slice_id           TEXT UNIQUE,
    plan_id            TEXT,
    flip_leg           TEXT,
    slice_index        INTEGER,
    exchange_fill_id   TEXT,
    exchange_order_id  TEXT,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    fill_qty           TEXT NOT NULL,
    fill_price         TEXT NOT NULL,
    fill_notional      TEXT NOT NULL,
    fee                TEXT NOT NULL,
    fee_rate           TEXT NOT NULL,
    realized_pnl_delta TEXT NOT NULL,
    liquidity_type     TEXT NOT NULL,
    fill_reason        TEXT
)
"""

_FUNDING_EVENTS = """
CREATE TABLE funding_events (
    funding_event_id         TEXT PRIMARY KEY,
    recorded_at              TEXT NOT NULL,
    updated_at               TEXT NOT NULL,
    funding_timestamp        TEXT NOT NULL,
    mode                     TEXT NOT NULL,
    run_id                   TEXT NOT NULL,
    symbol                   TEXT NOT NULL,
    position_size            TEXT NOT NULL,
    mark_price               TEXT,
    signed_position_notional TEXT,
    funding_rate             TEXT,
    funding_pnl              TEXT,
    status                   TEXT NOT NULL,
    source                   TEXT,
    UNIQUE (run_id, symbol, funding_timestamp)
)
"""

_ACCOUNT_SNAPSHOTS = """
CREATE TABLE account_snapshots (
    snapshot_id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                TEXT NOT NULL,
    mode                     TEXT NOT NULL,
    run_id                   TEXT NOT NULL,
    wallet_balance           TEXT NOT NULL,
    account_equity           TEXT NOT NULL,
    available_balance        TEXT NOT NULL,
    realized_pnl             TEXT NOT NULL,
    unrealized_pnl           TEXT NOT NULL,
    total_pnl                TEXT NOT NULL,
    total_fees               TEXT NOT NULL,
    net_funding_pnl          TEXT NOT NULL,
    total_position_notional  TEXT NOT NULL,
    -- NULL when account_equity <= 0: leverage is undefined on a blown-up
    -- account, and 0 would read as "no exposure" — the opposite of the truth.
    effective_leverage       TEXT,
    used_initial_margin      TEXT NOT NULL,
    total_maintenance_margin TEXT NOT NULL,
    margin_ratio             TEXT
)
"""

_POSITION_SNAPSHOTS = """
CREATE TABLE position_snapshots (
    position_snapshot_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                   TEXT NOT NULL,
    mode                        TEXT NOT NULL,
    run_id                      TEXT NOT NULL,
    symbol                      TEXT NOT NULL,
    position_size               TEXT NOT NULL,
    side                        TEXT NOT NULL,
    entry_price                 TEXT,
    mark_price                  TEXT NOT NULL,
    position_notional           TEXT NOT NULL,
    exposure_pct                TEXT,
    unrealized_pnl              TEXT NOT NULL,
    realized_pnl                TEXT NOT NULL,
    maintenance_margin          TEXT NOT NULL,
    estimated_liquidation_price TEXT,
    exchange_liquidation_price  TEXT,
    margin_tier_id              TEXT,
    maintenance_margin_rate     TEXT,
    maintenance_deduction       TEXT,
    liquidation_model_version   TEXT,
    stop_loss_price             TEXT,
    take_profit_price           TEXT
)
"""

# --------------------------------------------------------------------------
# Internal runtime tables (phase2-data §1.2) — not exported to CSV.
# --------------------------------------------------------------------------

_RUNS = """
CREATE TABLE runs (
    run_id               TEXT PRIMARY KEY,
    mode                 TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    initial_balance_usdc TEXT NOT NULL,
    config_json          TEXT,
    schema_version       INTEGER NOT NULL
)
"""

_SCHEDULER_STATE = """
CREATE TABLE scheduler_state (
    run_id             TEXT PRIMARY KEY,
    last_decision_at   TEXT,
    next_decision_at   TEXT,
    last_input_id      TEXT,
    last_output_id     TEXT,
    current_attempt_id TEXT,
    updated_at         TEXT NOT NULL
)
"""

_EXECUTION_PLANS = """
CREATE TABLE execution_plans (
    plan_id               TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL,
    output_id             TEXT,
    flip_plan_id          TEXT,
    flip_leg              TEXT,
    symbol                TEXT NOT NULL,
    status                TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    deadline_at           TEXT,
    planned_slices        INTEGER,
    total_qty             TEXT,
    remaining_qty         TEXT,
    residual_qty          TEXT,
    rounding_residual_qty TEXT,
    status_reason         TEXT,
    updated_at            TEXT NOT NULL
)
"""

_CURRENT_POSITIONS = """
CREATE TABLE current_positions (
    run_id            TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    size              TEXT NOT NULL,
    entry_price       TEXT,
    realized_pnl      TEXT NOT NULL DEFAULT '0',
    stop_loss_price   TEXT,
    take_profit_price TEXT,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (run_id, symbol)
)
"""

_CURRENT_ACCOUNT_STATE = """
CREATE TABLE current_account_state (
    run_id          TEXT PRIMARY KEY,
    wallet_balance  TEXT NOT NULL,
    realized_pnl    TEXT NOT NULL DEFAULT '0',
    total_fees      TEXT NOT NULL DEFAULT '0',
    net_funding_pnl TEXT NOT NULL DEFAULT '0',
    updated_at      TEXT NOT NULL
)
"""

# The run's seed positions as applied at creation (phase2-data §1.2 genesis).
# Replay reads its genesis from here + runs.initial_balance_usdc rather than
# trusting the caller's current config: a YAML edited after run creation must
# not shift the replay baseline (it would misreport — or mask — a mismatch).
_RUN_SEED_POSITIONS = """
CREATE TABLE run_seed_positions (
    run_id       TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    size         TEXT NOT NULL,
    entry_price  TEXT,
    realized_pnl TEXT NOT NULL DEFAULT '0',
    PRIMARY KEY (run_id, symbol)
)
"""

# schema_migrations is created by ``db.apply_migrations`` before any migration
# runs (it is the bookkeeping table that records which migrations ran), so it is
# intentionally separate from the versioned statement list below.
SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""

# version -> ordered DDL statements applied in one transaction for that version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        _AI_INPUTS,
        _DECISION_ATTEMPTS,
        _AI_OUTPUTS,
        _ORDERS,
        _FILLS,
        _FUNDING_EVENTS,
        _ACCOUNT_SNAPSHOTS,
        _POSITION_SNAPSHOTS,
        _RUNS,
        _SCHEDULER_STATE,
        _EXECUTION_PLANS,
        _CURRENT_POSITIONS,
        _CURRENT_ACCOUNT_STATE,
        _RUN_SEED_POSITIONS,
    ),
    # v2: the scheduler persists a successful AI response on its attempt row the
    # moment it arrives, so a restart during the market-data-blocked gate phase
    # resumes gating from the stored text instead of re-asking the AI (spec §3.1:
    # a restart must never produce a duplicate AI decision). Internal column —
    # cleared when the attempt terminalizes, never exported to CSV.
    2: ("ALTER TABLE decision_attempts ADD COLUMN pending_raw_response TEXT",),
}
