"""SQLite schema for the Phase 2 paper-trading store (phase2-data.md).

SQLite is the single source of truth (phase2-data §1). This module owns the DDL:
the eight export logical tables (§5–§12, one-to-one with the CSV exports) and the
seven internal runtime tables (§1.2, plus ``run_seed_positions`` — the persisted
replay genesis). ``db.apply_migrations`` runs these in version order and records
each in ``schema_migrations``. Phase 3 live execution extends the store in v6
(phase3-spec §16): exchange-facing columns on the Phase 2 tables plus the seven
§16.5 live internal tables.

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

SCHEMA_VERSION = 9

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

# --------------------------------------------------------------------------
# Phase 3 live internal tables (phase3-spec §16.5) — not exported to CSV.
# --------------------------------------------------------------------------

# cloid_logical <-> cloid_hex mapping (§8.2). The exchange only ever sees (and
# echoes back) cloid_hex, so reverse lookup through this table is the ONE way
# to decide an exchange order is bot-owned (§19.3) — a human-readable prefix
# does not survive the hash. Both columns are unique: one logical order maps to
# exactly one wire id, and a hex value resolving to two logical ids would make
# the bot-owned decision ambiguous.
_CLOID_REGISTRY = """
CREATE TABLE cloid_registry (
    cloid_hex     TEXT PRIMARY KEY,
    cloid_logical TEXT NOT NULL UNIQUE,
    run_id        TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    order_role    TEXT NOT NULL,
    created_at    TEXT NOT NULL
)
"""

# One row per exchange round-trip attempt (§8.3): written BEFORE the network
# call (status 'submitted'), patched with the outcome after. A crash between
# the two leaves a durable record that an order MAY exist on the exchange —
# exactly the state the §8.3 duplicate-retry protocol (query by cloid_hex
# before resending) exists to resolve. ``attempt_index`` counts retries of the
# same logical action; the UNIQUE triple makes a retry a new row, never a
# silent overwrite of the previous attempt's evidence.
_LIVE_ORDER_ATTEMPTS = """
CREATE TABLE live_order_attempts (
    attempt_id                TEXT PRIMARY KEY,
    run_id                    TEXT NOT NULL,
    order_id                  TEXT,
    action                    TEXT NOT NULL,
    cloid_logical             TEXT,
    cloid_hex                 TEXT,
    exchange_order_id         TEXT,
    symbol                    TEXT NOT NULL,
    side                      TEXT,
    qty                       TEXT,
    price                     TEXT,
    reduce_only               INTEGER,
    order_role                TEXT,
    attempt_index             INTEGER NOT NULL,
    status                    TEXT NOT NULL,
    exchange_status           TEXT,
    error_message             TEXT,
    raw_exchange_payload_path TEXT,
    requested_at              TEXT NOT NULL,
    acknowledged_at           TEXT,
    UNIQUE (cloid_hex, action, attempt_index)
)
"""

# §18.5 event log — every dead-man's-switch state change is auditable.
_KILL_SWITCH_EVENTS = """
CREATE TABLE kill_switch_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    timestamp     TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    detail        TEXT,
    error_message TEXT
)
"""

# §12.3 case log (consumed by PR 4's reconciliation module; the table lands
# with the rest of the §16.5 DDL so live rows written by PR 2/3 never race a
# later migration).
_EXCHANGE_RECONCILIATION_EVENTS = """
CREATE TABLE exchange_reconciliation_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    trigger        TEXT NOT NULL,
    case_type      TEXT NOT NULL,
    symbol         TEXT,
    local_value    TEXT,
    exchange_value TEXT,
    action_taken   TEXT,
    detail         TEXT
)
"""

# §17 protection lifecycle log (PR 5 writes it).
_PROTECTION_ORDER_EVENTS = """
CREATE TABLE protection_order_events (
    event_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     TEXT NOT NULL,
    timestamp  TEXT NOT NULL,
    event_type TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    order_id   TEXT,
    cloid_hex  TEXT,
    detail     TEXT
)
"""

# §15 fee/funding corrections: a backfilled or corrected amount is an explicit
# adjustment event, never a silent overwrite (PR 3 writes it).
_ACCOUNTING_ADJUSTMENT_EVENTS = """
CREATE TABLE accounting_adjustment_events (
    adjustment_id   TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    adjustment_type TEXT NOT NULL,
    target_table    TEXT,
    target_id       TEXT,
    field           TEXT,
    old_value       TEXT,
    new_value       TEXT,
    reason          TEXT,
    source          TEXT
)
"""

# §13.6 safe-mode history (current state lives on scheduler_state; PR 4 owns
# the state machine).
_SAFE_MODE_EVENTS = """
CREATE TABLE safe_mode_events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    timestamp      TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    safe_mode_type TEXT,
    reason         TEXT,
    released_by    TEXT,
    detail         TEXT
)
"""

# --------------------------------------------------------------------------
# Phase 3 live acceptance tables (phase3-spec §20) — not exported to CSV.
# --------------------------------------------------------------------------

# §20.2 testnet smoke-test results (PR 6). Append-only audit: one row per test
# execution, so a re-run after a fix leaves the earlier failure on record and
# the gate reads the LATEST non-dry-run row per ``test_key`` (a fresh
# result_id > any prior). ``dry_run`` rows are wiring checks that never satisfy
# the cycle-entry gate (§20.2 "all smoke tests must pass"); ``test_number`` /
# ``test_key`` name the §20.2 item so the acceptance validator can read the
# four §20.3 ``*_test_passed`` booleans by key without positional guessing.
_LIVE_SMOKE_TESTS = """
CREATE TABLE live_smoke_tests (
    result_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    test_number   INTEGER NOT NULL,
    test_key      TEXT NOT NULL,
    test_name     TEXT NOT NULL,
    status        TEXT NOT NULL,
    network       TEXT,
    dry_run       INTEGER NOT NULL DEFAULT 0,
    detail        TEXT,
    error_message TEXT,
    executed_at   TEXT NOT NULL
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
    # v3: operational state on scheduler_state. The single-instance lease
    # (lock_pid + lock_heartbeat_at — a second `paper` process on the same run
    # must refuse to start while the holder's heartbeat is fresh) and the CSV
    # export breadcrumbs (last_export_status/error/at — export failures are
    # warn-and-carry-on, so without a durable record a post-mortem can't tell
    # how long the CSV view had been stale). Internal columns, never exported.
    3: (
        "ALTER TABLE scheduler_state ADD COLUMN lock_pid INTEGER",
        "ALTER TABLE scheduler_state ADD COLUMN lock_heartbeat_at TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_export_status TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_export_error TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_export_at TEXT",
    ),
    # v4: replay-verification breadcrumbs, mirroring the last_export_* trio. A
    # replay mismatch mid-run halts NEW decision cycles but the process keeps
    # running (SL/TP protection stays live) — without a durable record, that
    # halt (and any transient replay failure that healed before the next
    # restart) would be invisible to a post-mortem once the process exits.
    # Internal columns, never exported.
    4: (
        "ALTER TABLE scheduler_state ADD COLUMN last_replay_status TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_replay_error TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_replay_at TEXT",
    ),
    # v5: config-drift breadcrumb, mirroring the last_export_*/last_replay_*
    # trios. Parameter drift on resume is warn-and-carry-on (the run continues
    # under changed risk/decision/paper_trading behaviour) — stderr is the only
    # other witness, and a post-mortem over the store alone must be able to
    # tell that (and when) a run stopped being parameter-homogeneous. Clean
    # resumes stamp "ok" so a reverted config doesn't leave a stale "drift" as
    # the store's last word. Internal columns, never exported.
    5: (
        "ALTER TABLE scheduler_state ADD COLUMN last_config_drift_status TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_config_drift_error TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN last_config_drift_at TEXT",
    ),
    # v6: Phase 3 live execution (phase3-spec §16). Exchange-facing columns on
    # the four Phase 2 tables (§16.1–16.4), the seven §16.5 internal tables,
    # and the safe-mode / loss-guard state on scheduler_state (§16.6). All new
    # columns are nullable so every existing paper row stays valid.
    # ``exchange_order_id`` / ``order_role`` / ``reduce_only`` (orders) and
    # ``exchange_fill_id`` / ``exchange_order_id`` (fills) already exist in v1
    # and are NOT re-added. ``orders.client_order_id`` is deprecated in place:
    # live orders write cloid_logical / cloid_hex instead (§16.1); the column
    # stays for old paper rows but gains no new writers.
    6: (
        # §16.1 orders additions. exchange_status carries the normalized
        # status family (the orders.status vocabulary) and exchange_raw_status
        # the verbatim exchange word — every live write path fills both.
        "ALTER TABLE orders ADD COLUMN cloid_logical TEXT",
        "ALTER TABLE orders ADD COLUMN cloid_hex TEXT",
        "ALTER TABLE orders ADD COLUMN exchange_status TEXT",
        "ALTER TABLE orders ADD COLUMN exchange_raw_status TEXT",
        "ALTER TABLE orders ADD COLUMN submitted_at TEXT",
        "ALTER TABLE orders ADD COLUMN acknowledged_at TEXT",
        "ALTER TABLE orders ADD COLUMN canceled_at TEXT",
        "ALTER TABLE orders ADD COLUMN cancel_reason TEXT",
        "ALTER TABLE orders ADD COLUMN is_bot_owned INTEGER",
        "ALTER TABLE orders ADD COLUMN raw_exchange_payload_path TEXT",
        # One orders row per cloid: cloid_registry's PK pins the pair itself;
        # this pins the row count. NULLs (every paper row) stay distinct, same
        # as idx_fills_exchange_fill_key below.
        "CREATE UNIQUE INDEX idx_orders_cloid_hex ON orders (cloid_hex)",
        # §16.2 fills additions. exchange_fill_key is the §14.2 dedupe key;
        # SQLite cannot ADD COLUMN with a UNIQUE constraint, so the guard is a
        # UNIQUE index (NULLs stay distinct — paper fills never carry the key).
        "ALTER TABLE fills ADD COLUMN exchange_fill_key TEXT",
        "ALTER TABLE fills ADD COLUMN cloid_logical TEXT",
        "ALTER TABLE fills ADD COLUMN cloid_hex TEXT",
        "ALTER TABLE fills ADD COLUMN liquidity_role TEXT",
        "ALTER TABLE fills ADD COLUMN exchange_fee TEXT",
        "ALTER TABLE fills ADD COLUMN exchange_closed_pnl TEXT",
        "ALTER TABLE fills ADD COLUMN exchange_fill_time TEXT",
        "ALTER TABLE fills ADD COLUMN raw_exchange_payload_path TEXT",
        "CREATE UNIQUE INDEX idx_fills_exchange_fill_key ON fills (exchange_fill_key)",
        # §16.3 account_snapshots additions.
        "ALTER TABLE account_snapshots ADD COLUMN exchange_account_value TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN exchange_withdrawable TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN exchange_margin_used TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN exchange_unrealized_pnl TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN exchange_raw_payload_path TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN reconciliation_status TEXT",
        "ALTER TABLE account_snapshots ADD COLUMN reconciliation_diff TEXT",
        # §16.4 position_snapshots additions (exchange_liquidation_price
        # already exists in v1 and is NOT re-added).
        "ALTER TABLE position_snapshots ADD COLUMN exchange_position_size TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN exchange_entry_price TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN exchange_unrealized_pnl TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN exchange_margin_used TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN exchange_raw_payload_path TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN reconciliation_status TEXT",
        "ALTER TABLE position_snapshots ADD COLUMN reconciliation_diff TEXT",
        # §16.5 internal tables.
        _CLOID_REGISTRY,
        _LIVE_ORDER_ATTEMPTS,
        _KILL_SWITCH_EVENTS,
        _EXCHANGE_RECONCILIATION_EVENTS,
        _PROTECTION_ORDER_EVENTS,
        _ACCOUNTING_ADJUSTMENT_EVENTS,
        _SAFE_MODE_EVENTS,
        # §16.6 scheduler_state additions (PR 4 state machine / PR 5 loss guards).
        "ALTER TABLE scheduler_state ADD COLUMN safe_mode_type TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN safe_mode_reason TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN safe_mode_entered_at TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN day_start_equity TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN day_start_date TEXT",
        "ALTER TABLE scheduler_state ADD COLUMN consecutive_loss_count INTEGER",
    ),
    # v7: the §10.4 consecutive-loss segment baseline (PR 5 loss guards). A
    # "loss" is one position segment (flat → flat) whose net realized PnL
    # (closedPnl − fees + funding, all already folded into wallet_balance) is
    # negative; the baseline is the wallet_balance at the PREVIOUS settlement,
    # so the current segment's PnL is the delta against it. Durable so the
    # count survives a restart mid-segment (the count column landed in v6).
    # Internal column, never exported.
    7: ("ALTER TABLE scheduler_state ADD COLUMN last_settlement_wallet_balance TEXT",),
    # v8: the §20.2 testnet smoke-test result log (PR 6). One append-only table;
    # the acceptance validator reads its latest-per-key non-dry-run rows for the
    # §20.3 gate and ``*_test_passed`` metrics. No existing table is touched.
    8: (_LIVE_SMOKE_TESTS,),
    # v9: the exchange-reported liquidation estimate, mirrored onto the run's
    # current_positions row by the live reconciler each pass (clearinghouse
    # ``liquidationPx``; cleared when the exchange reports flat). The live SL
    # band (§3.6/§17.2) and the ai_inputs ``estimated_liquidation_price``
    # column read it via the engine — before v9 those always saw NULL because
    # PositionState carried no such field. Nullable so every existing paper row
    # (and any live row before its first reconcile pass) stays valid.
    9: ("ALTER TABLE current_positions ADD COLUMN exchange_liquidation_price TEXT",),
}
