"""Subcommand CLI for the Hyperliquid perp module.

The subcommands (plus full Phase 1/2 backward compatibility):

- ``python -m contrib.hyperliquid_perp paper --coin BTC`` — the long-running
  paper run: restart reconciliation (execution §1.2), then the 30-second
  monitor/scheduler loop (rolling 4h decisions, TWAP execution, SL/TP,
  funding), with CSV export after every completed cycle and on shutdown.
- ``python -m contrib.hyperliquid_perp export --run-id <id> --output-dir <dir>``
  — manual full-dataset CSV export (phase2-data §1.1).
- ``python -m contrib.hyperliquid_perp validate --run-id <id>`` — the acceptance
  report and verdict. A PAPER run gets the phase2-spec §5 report; a LIVE run gets
  the phase3-spec §20.3 / §21.4 report (the run's stored mode selects which).
- ``python -m contrib.hyperliquid_perp live --config <yaml>`` — the Phase 3
  startup skeleton (phase3-spec PR 1): load the ``live:`` config gates, verify
  the agent-wallet authorization, print the effective notional caps, and exit
  without entering any trading loop (that is ``live --run-id --loop``, PR 5).
  Places no orders.
- ``python -m contrib.hyperliquid_perp live-smoke --run-id <id> --config <yaml>``
  — the phase3-spec §20.2 testnet smoke checklist (PR 6): drive each signed
  exchange action against testnet, record the verdicts, and report the §20.2
  cycle-entry gate. ``--gate-status`` reads the stored gate (no network);
  ``--dry-run`` validates config + wiring and places no orders.
- ``python -m contrib.hyperliquid_perp safe-mode --run-id <id> --status`` —
  live-run safe-mode inspection and the manual lane (``--release``,
  ``--stamp-case``) for §12.3/§13.5 operator actions.

Empty argv and flag-style invocations (first argument starting with ``-``) —
including the Phase 1 ``--context-only`` smoke run and the single-shot engine
run — are delegated verbatim to :mod:`.main`, whose behaviour is unchanged.
A bare first argument that is not a known subcommand is an error (exit 1):
the legacy CLI takes no positionals, so it can only be a subcommand typo.

Exit codes: ``0`` success (for ``validate``: Phase-3 ready), ``1`` named
operator/config/environment errors — including a protection-only ``paper`` run
that self-terminates after its position closes (final export written; the
books never re-verified, the API key was never supplied, or the engine
failed to import — stderr says which), ``2`` unexpected error, ``4``
not-yet-at-the-gate outcomes (``validate``: short of the 30-cycle gate or a
red/missing smoke test — curable by a ``live-smoke`` re-run;
``live-smoke``: the §20.2 gate is not satisfied, incl. a pre-flight abort;
``live --loop``: the §20.2 smoke gate is not open on this run;
``live`` without ``--loop``: recovery ran but judged unclean; ``safe-mode
--status``: a safe mode is latched, recoverable OR manual — 4 means "latched",
not "human action required"), ``5`` (``validate`` only) the
run has integrity failures — orphans, snapshot or replay mismatches, or a
store so corrupt the checks themselves cannot run ("the store is broken;
investigate before trusting results"), ``130`` interrupted before a graceful
lane could take over (Ctrl-C in ``export``/``validate``/``live``/``live-smoke``
or during ``paper`` startup/reconciliation — SIGTERM likewise once ``paper``,
``live`` or ``live-smoke`` has installed its handler; once the ``paper`` loop
runs, both signals take the shutdown-export lane instead of ``130``). For
``live-smoke`` the handler is installed with the run lease, so an interrupt
after that point still runs the suite's cleanup (sweep resting probes, close the
staged long, disarm the switch — in that order, because flattening first makes
the exchange auto-cancel the reduce-only probes) rather than stranding it.
Legacy delegated invocations keep
:mod:`.main`'s own exit contract.
"""

from __future__ import annotations

import logging
import sys

from ..config import load_dotenv_files
from . import _provider, paper_export
from ._common import (
    _existing_run_row,
    _open_existing_db,
    _raise_keyboard_interrupt,
    _require_api_key,
    _require_live_run_mode,
)
from ._drift import (
    _HARD_DRIFT_KINDS,
    _config_drift_report,
    _norm_network,
    _run_config_subset,
)
from ._provider import (
    PROMPT_VERSION,
    _classify_engine_error,
    _EngineDecisionProvider,
    _HistoryFundingSource,
)
from .live import _cmd_live, _live_startup_recovery
from .live_loop import (
    _LIVE_TICK_SECONDS,
    _contain_as_recoverable_safe_mode,
    _day_baseline_from_exchange,
    _live_heartbeat,
    _run_live_loop,
    _still_owns_run,
)
from .live_shared import (
    _RECOVERY_MAX_TICK_GAP_SECONDS,
    _conflicting_run_lease,
    _print_smoke_gate,
    _run_genesis_network,
    _smoke_gate_buckets,
    _timing_preflight,
)
from .offline import _cmd_export, _cmd_validate, _validate_live
from .paper import _cmd_paper, _paper_loop
from .paper_export import (
    _UNVERIFIED_MARKER,
    _mark_export_verification,
    _post_cycle_export,
    _retry_pending_funding,
    _stamp_breadcrumb,
)
from .safe_mode import _cmd_safe_mode, _stamp_reconciliation_case
from .smoke import (
    _SMOKE_MIN_KILL_SWITCH_DEADLINE,
    _build_real_smoke_session,
    _build_smoke_session,
    _cmd_live_smoke,
    _smoke_startup_recovery,
)

logger = logging.getLogger(__name__)

_SUBCOMMANDS = ("paper", "export", "validate", "live", "live-smoke", "safe-mode")


def main(argv: list[str] | None = None) -> int:
    # Before anything reads os.environ: the OPENROUTER_API_KEY startup checks
    # (fresh `paper` runs, healthy keyless-restart triage) run long before the
    # lazily-imported engine package would load the .env files itself.
    load_dotenv_files()
    argv = list(sys.argv[1:]) if argv is None else list(argv)
    if not argv or argv[0].startswith("-"):
        # Phase 1/2 compatibility path: identical flags, identical behaviour.
        # Legacy accepts no positionals, so flag-shaped/empty argv is lossless.
        from ..main import main as legacy_main

        return legacy_main(argv)
    if argv[0] not in _SUBCOMMANDS:
        # A bare unknown word is almost certainly a subcommand typo; delegating
        # it would surface the legacy parser's usage (which never mentions the
        # subcommands) under exit code 2 ("unexpected error").
        print(
            f"error: unknown subcommand {argv[0]!r} (expected one of: "
            f"{', '.join(_SUBCOMMANDS)}; legacy flag invocations like "
            "--context-only pass through unchanged).",
            file=sys.stderr,
        )
        return 1
    command, rest = argv[0], argv[1:]
    try:
        if command == "export":
            return _cmd_export(rest)
        if command == "validate":
            return _cmd_validate(rest)
        if command == "live":
            return _cmd_live(rest)
        if command == "live-smoke":
            return _cmd_live_smoke(rest)
        if command == "safe-mode":
            return _cmd_safe_mode(rest)
        return _cmd_paper(rest)
    except KeyboardInterrupt:
        print("interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 — last-resort handler, mirrors main.main
        logger.exception("unexpected error in %s", command)
        print(f"fatal: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
