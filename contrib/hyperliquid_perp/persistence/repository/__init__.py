"""Typed insert / update / query helpers over the Phase 2 SQLite schema.

Every write runs inside a caller-supplied transaction (``with db.transaction()
as conn: ...``). The repository never opens its own transaction, so the
accounting layer can post a fill *and* update ``current_positions`` /
``current_account_state`` in one atomic unit (phase2-data §1.2: the materialized
current-state tables must update in the same transaction as the event that
changed them).

Values cross this seam as native Python types (``Decimal``, ``datetime``,
``bool``) and are encoded to the schema's storage form here — Decimals to TEXT
(no REAL float), datetimes to ISO-8601 UTC, bools to ``0`` / ``1`` — so callers
never hand-serialize. Reads decode the mutable ``current_*`` rows back into the
:mod:`..models` dataclasses; append-only event rows are returned as ``sqlite3.Row``
for the reporting / replay consumers.
"""

# Some re-exported names are deliberately NOT in __all__ (kept identical to the
# pre-split module): they are still load-bearing for external consumers, which
# reach them as package attributes (repo.LIVE_ORDER_STATUSES, repo.LIVE_PLAN_STATUSES,
# repo.TERMINAL_ATTEMPT_STATUSES, repo.iter_open_live_orders, repo.iter_other_run_leases,
# repo.stamp_reconciliation_action_if_unset) or by direct import (UNSET / Unset in
# live.payloads). Do not prune them as unused.
from ._base import UNSET, Unset
from ._vocab import (
    ACCOUNTING_ADJUSTMENT_TYPES,
    ERROR_TYPES,
    EXCHANGE_KNOWN_ATTEMPT_STATUSES,
    KILL_SWITCH_EVENT_TYPES,
    LIVE_LIQUIDITY_ROLES,
    LIVE_ORDER_STATUSES,
    LIVE_PLAN_STATUSES,
    LIVE_SMOKE_TEST_STATUSES,
    MACHINE_DISPOSITIONS,
    PROTECTION_ORDER_EVENT_TYPES,
    PROVISIONAL_DISPOSITIONS,
    RECONCILIATION_CASE_TYPES,
    RECONCILIATION_TRIGGERS,
    RESTING_ORDER_STATUSES,
    ROLE_TO_ORDER_TYPE,
    SAFE_MODE_EVENT_TYPES,
    SAFE_MODE_TYPES,
    TERMINAL_ATTEMPT_STATUSES,
)
from .account import (
    get_current_account_state,
    require_current_account_state,
    upsert_current_account_state,
)
from .cloid_registry import get_cloid_by_hex, get_cloid_by_logical, insert_cloid_mapping
from .decisions import (
    PromptRegime,
    UnstampedInput,
    ai_inputs_without_format_fingerprint,
    find_in_progress_attempt,
    get_decision_attempt,
    insert_account_snapshot,
    insert_ai_input,
    insert_ai_output,
    insert_decision_attempt,
    insert_position_snapshot,
    prompt_regime_counts,
    stamp_ai_input_format_fingerprint,
    store_pending_response,
    update_decision_attempt,
)
from .events import (
    get_accounting_adjustment_event,
    insert_accounting_adjustment_event,
    insert_kill_switch_event,
    insert_protection_order_event,
    iter_accounting_adjustment_events,
    iter_kill_switch_events,
    iter_protection_order_events,
)
from .fills import (
    get_fill,
    get_fill_by_exchange_key,
    insert_fill,
    insert_live_fill,
    iter_fills,
    iter_live_fills,
    last_fill_time,
    last_live_fill_time,
    newest_live_fill_order_key,
    posted_exchange_fee,
    require_live_fill_basis,
)
from .funding import (
    get_funding_event,
    insert_funding_event,
    iter_funding_events,
    set_funding_status,
)
from .live_attempts import (
    get_live_order_attempt,
    has_exchange_known_cloid,
    has_place_attempt,
    insert_live_order_attempt,
    iter_live_order_attempts,
    next_live_attempt_index,
    update_live_order_attempt,
)
from .orders import (
    active_protection_order,
    count_orders_by_role,
    get_order,
    get_order_by_cloid_hex,
    get_order_by_exchange_order_id,
    insert_order,
    iter_open_live_orders,
    iter_orders,
    max_engine_seq,
    update_order,
)
from .plans import (
    get_execution_plan,
    insert_execution_plan,
    iter_execution_plans,
    update_execution_plan,
)
from .positions import (
    get_all_current_positions,
    get_current_position,
    get_position_protection,
    set_position_liquidation_price,
    set_position_protection,
    upsert_current_position,
)
from .reconciliation import (
    get_exchange_reconciliation_case,
    has_exchange_reconciliation_case,
    insert_exchange_reconciliation_event,
    iter_exchange_reconciliation_events,
    iter_unresolved_fill_sightings,
    set_reconciliation_action,
    stamp_reconciliation_action_if_unset,
)
from .runs import get_run, get_run_seed_positions, insert_run, insert_run_seed_position
from .safe_mode import has_safe_mode_reason_event, insert_safe_mode_event, iter_safe_mode_events
from .scheduler import get_scheduler_state, iter_other_run_leases, upsert_scheduler_state
from .smoke import insert_smoke_test_result, iter_smoke_test_results, latest_smoke_test_results

__all__ = [
    "ACCOUNTING_ADJUSTMENT_TYPES",
    "EXCHANGE_KNOWN_ATTEMPT_STATUSES",
    "KILL_SWITCH_EVENT_TYPES",
    "LIVE_LIQUIDITY_ROLES",
    "LIVE_SMOKE_TEST_STATUSES",
    "MACHINE_DISPOSITIONS",
    "PROTECTION_ORDER_EVENT_TYPES",
    "PROVISIONAL_DISPOSITIONS",
    "PromptRegime",
    "RECONCILIATION_CASE_TYPES",
    "RECONCILIATION_TRIGGERS",
    "RESTING_ORDER_STATUSES",
    "ROLE_TO_ORDER_TYPE",
    "SAFE_MODE_EVENT_TYPES",
    "SAFE_MODE_TYPES",
    "UnstampedInput",
    "active_protection_order",
    "ai_inputs_without_format_fingerprint",
    "count_orders_by_role",
    "find_in_progress_attempt",
    "get_accounting_adjustment_event",
    "get_all_current_positions",
    "get_cloid_by_hex",
    "get_cloid_by_logical",
    "get_current_account_state",
    "get_current_position",
    "get_decision_attempt",
    "get_exchange_reconciliation_case",
    "get_execution_plan",
    "get_fill",
    "get_fill_by_exchange_key",
    "get_funding_event",
    "get_live_order_attempt",
    "get_order",
    "get_order_by_cloid_hex",
    "get_order_by_exchange_order_id",
    "get_position_protection",
    "get_run",
    "get_run_seed_positions",
    "get_scheduler_state",
    "has_exchange_known_cloid",
    "has_safe_mode_reason_event",
    "has_exchange_reconciliation_case",
    "has_place_attempt",
    "insert_account_snapshot",
    "insert_accounting_adjustment_event",
    "insert_ai_input",
    "insert_ai_output",
    "insert_cloid_mapping",
    "insert_decision_attempt",
    "insert_exchange_reconciliation_event",
    "insert_execution_plan",
    "insert_fill",
    "insert_funding_event",
    "insert_kill_switch_event",
    "insert_live_fill",
    "insert_live_order_attempt",
    "insert_order",
    "insert_position_snapshot",
    "insert_protection_order_event",
    "insert_run",
    "insert_run_seed_position",
    "insert_safe_mode_event",
    "insert_smoke_test_result",
    "iter_accounting_adjustment_events",
    "iter_exchange_reconciliation_events",
    "iter_execution_plans",
    "iter_fills",
    "iter_funding_events",
    "iter_kill_switch_events",
    "iter_live_fills",
    "iter_live_order_attempts",
    "iter_orders",
    "iter_protection_order_events",
    "iter_safe_mode_events",
    "iter_smoke_test_results",
    "iter_unresolved_fill_sightings",
    "last_fill_time",
    "last_live_fill_time",
    "latest_smoke_test_results",
    "max_engine_seq",
    "newest_live_fill_order_key",
    "next_live_attempt_index",
    "posted_exchange_fee",
    "prompt_regime_counts",
    "require_current_account_state",
    "require_live_fill_basis",
    "set_funding_status",
    "set_reconciliation_action",
    "set_position_liquidation_price",
    "set_position_protection",
    "stamp_ai_input_format_fingerprint",
    "store_pending_response",
    "update_decision_attempt",
    "update_execution_plan",
    "update_live_order_attempt",
    "update_order",
    "upsert_current_account_state",
    "upsert_current_position",
    "upsert_scheduler_state",
]
