"""Synthetic documents, a fake runner and a config for the exporter's tests.

The documents are shaped as the three packages' status dataclasses are
(``test_schema_pin.py`` reads the field names off their sources and holds
these builders to them), with values a quiet host would show at :data:`NOW`:
no rule fires on them unless a test changes one. Amounts are given as the
packages give them, exact decimal text, so ``test_mail.py`` can prove none
reaches a letter.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from contrib.dashboard.config import (
    CarryLine,
    ExporterConfig,
    HyperliquidLine,
    Settings,
    Thresholds,
    UniswapLine,
)
from contrib.dashboard.readers import CommandResult

NOW = datetime(2026, 10, 14, 5, 0, tzinfo=timezone.utc)
# The documents' instants: the perp store's spelling (``+00:00``), carry's, Uniswap's (``Z``).
HL_CREATED = "2026-10-01T08:26:34.123456+00:00"
UNI_CREATED = "2026-10-08T03:16:50Z"
WALLET = "0x" + "ab" * 20
AGENT_KEY = "0x" + "cd" * 32


def hl_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def carry_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def uni_instant(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def breadcrumb(status: str | None, at: datetime | None, error: str | None = None) -> dict[str, Any]:
    return {"status": status, "at": None if at is None else hl_instant(at), "error": error}


def kill_switch_event(moment: datetime, event_type: str) -> dict[str, Any]:
    return {
        "timestamp": hl_instant(moment),
        "event_type": event_type,
        "detail": f"armed for {WALLET}",
        "error_message": None,
    }


def safe_mode_event(moment: datetime, event_type: str, safe_mode_type: str) -> dict[str, Any]:
    return {
        "timestamp": hl_instant(moment),
        "event_type": event_type,
        "safe_mode_type": safe_mode_type,
        "reason": "reconcile mismatch",
        "released_by": None,
        "detail": None,
    }


def hyperliquid_document(**overrides: Any) -> dict[str, Any]:
    """A ``hyperliquid_perp`` ``status --json`` document of a quiet run at :data:`NOW`."""
    scheduler = {
        "last_decision_at": hl_instant(NOW - timedelta(hours=1)),
        "next_decision_at": hl_instant(NOW + timedelta(hours=3)),
        "in_progress_attempt_id": None,
        "lease_heartbeat_at": hl_instant(NOW - timedelta(minutes=1)),
        "updated_at": hl_instant(NOW - timedelta(minutes=1)),
        "export": breadcrumb("ok", NOW - timedelta(hours=1)),
        "replay": breadcrumb("ok", NOW - timedelta(hours=1)),
        "config_drift": breadcrumb(None, None),
    }
    verdict = {
        "symbol": "BTC",
        "decision_mode": "set_target",
        "target_side": "long",
        "requested_target_margin_pct": "30",
        "approved_target_margin_pct": "30",
        "risk_action": "approved",
        "risk_reason": None,
        "confidence": "0.62",
        "mark_price": "67123.5",
        "account_equity": "10234.56",
        "order_created": True,
        "no_order_reason": None,
        "decision_reason": f"trend up; wallet {WALLET} fine",
    }
    cycle = {
        "scheduled_at": hl_instant(NOW - timedelta(hours=1)),
        "timestamp": hl_instant(NOW - timedelta(hours=1)),
        "status": "completed",
        "error_type": None,
        "error_message": None,
        "attempt_count": 1,
        "next_decision_at": hl_instant(NOW + timedelta(hours=3)),
        "verdict": verdict,
    }
    snapshot = {
        "timestamp": hl_instant(NOW - timedelta(hours=1)),
        "account_equity": "10234.56",
        "available_balance": "7234.56",
        "unrealized_pnl": "34.56",
        "total_pnl": "234.56",
        "total_position_notional": "3000.00",
        "effective_leverage": "0.29",
        "margin_ratio": "0.05",
    }
    position = {
        "symbol": "BTC",
        "size": "0.0447",
        "entry_price": "67000.0",
        "realized_pnl": "200.00",
        "stop_loss_price": "64000.0",
        "take_profit_price": None,
        "exchange_liquidation_price": "40123.0",
        "updated_at": hl_instant(NOW - timedelta(hours=1)),
    }
    fill = {
        "timestamp": hl_instant(NOW - timedelta(hours=1)),
        "symbol": "BTC",
        "side": "buy",
        "fill_qty": "0.0447",
        "fill_price": "67000.0",
        "fee": "1.35",
        "realized_pnl_delta": "0",
        "liquidity_type": "maker",
        "fill_reason": "target",
    }
    document = {
        "schema_version": 1,
        "source": "hyperliquid_perp",
        "generated_at": hl_instant(NOW),
        "run_id": "paper-BTC-8",
        "mode": "paper",
        "created_at": HL_CREATED,
        "initial_balance_usdc": "10000",
        "store_schema_version": 13,
        "scheduler": scheduler,
        "safe_mode": None,
        "cycles": {
            "total": 78,
            "by_status": {"in_progress": 0, "completed": 70, "api_failed": 6, "invalid_output": 2},
            "recent": [cycle],
        },
        "models": [
            {
                "model": "anthropic/claude-sonnet-5.5",
                "prompt_version": "v6",
                "prompts": 80,
                "first_at": HL_CREATED,
                "last_at": hl_instant(NOW - timedelta(hours=1)),
            }
        ],
        "account": {
            "ledger": {
                "wallet_balance": "10200.00",
                "realized_pnl": "200.00",
                "total_fees": "12.34",
                "net_funding_pnl": "-3.21",
            },
            "snapshot": snapshot,
            "recent_snapshots": [snapshot],
        },
        "positions": [position],
        "recent_fills": [fill],
        "kill_switch_events": [
            kill_switch_event(NOW - timedelta(minutes=10), "kill_switch_refreshed"),
            kill_switch_event(NOW - timedelta(days=1), "kill_switch_armed"),
        ],
        "safe_mode_events": [],
    }
    document.update(overrides)
    return document


def clock(state: str, latest_boundary: datetime, undecided: int = 0) -> dict[str, Any]:
    if state == "ahead":
        last_decided = latest_boundary + timedelta(days=1)
    elif state == "up_to_date":
        last_decided = latest_boundary
    else:
        last_decided = latest_boundary - timedelta(days=undecided or 1)
    return {
        "state": state,
        "now": uni_instant(NOW),
        "last_decided": uni_instant(last_decided),
        "latest_boundary": uni_instant(latest_boundary),
        "undecided": undecided,
    }


def curve(start: str, end: str, total_return: str, max_drawdown: str) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "total_return": total_return,
        "max_drawdown": max_drawdown,
    }


def decision(bar: datetime, outcome: str = "held", **overrides: Any) -> dict[str, Any]:
    document = {
        "time": uni_instant(bar),
        "outcome": outcome,
        "decided_at": uni_instant(bar + timedelta(hours=1)),
        "delay_seconds": 3600,
        "reason_code": None,
        "reason": f"engine held; wallet {WALLET} unchanged",
        "verdicts": None,
        "targets_now": {"WETH": "0.5", "USDC": "0.5"},
    }
    document.update(overrides)
    return document


def uniswap_run(**overrides: Any) -> dict[str, Any]:
    latest = datetime(2026, 10, 14, tzinfo=timezone.utc)
    run = {
        "run_id": "paper-trend-1",
        "mode": "paper",
        "fills": "quoter",
        "strategy": "trend",
        "quote": "USDC",
        "created_at": UNI_CREATED,
        "fork_block": None,
        "opening": {"balances": {"USDC": "10000"}, "gas_eth": "1"},
        "decided": 6,
        "first_decided": uni_instant(datetime(2026, 10, 9, tzinfo=timezone.utc)),
        "last_decided": uni_instant(latest),
        "outcomes": {"traded": 2, "held": 3, "skipped": 1, "rejected": 0, "partial": 0},
        "reason_codes": {"skipped": {"no_target": 1}, "rejected": {}, "partial": {}},
        "unsettled": 0,
        "clock": clock("up_to_date", latest),
        "holdings": {"balances": {"USDC": "9876.54", "WETH": "0.0512"}, "gas_eth": "0.998"},
        "holdings_at": uni_instant(latest),
        "value": "10012.34",
        "metrics": {
            "bars": 6,
            "measured_from": uni_instant(datetime(2026, 10, 9, tzinfo=timezone.utc)),
            "measured_to": uni_instant(latest),
            "suspect_left_out": 0,
            "strategy": curve("10000", "10012.34", "0.0012", "0.0040"),
            "opening_held": curve("10000", "10000", "0", "0"),
            "all_quote": curve("10000", "10000", "0", "0"),
            "rebalances": 2,
            "swaps": 2,
            "traded_value": "3012.34",
            "turnover": "0.3",
            "costs": {
                "pool_fees": "1.50",
                "slippage": "0.25",
                "gas": "0.80",
                "gas_eth": "0.0002",
                "total": "2.55",
            },
        },
        "metrics_error": None,
        "verdicts_seen": None,
        "recent_decisions": [decision(latest)],
        "recent_valuations": [{"time": uni_instant(latest), "total_value": "10012.34"}],
    }
    run.update(overrides)
    return run


def pool_extent(pool: str = "WETH/USDC 0.05%") -> dict[str, Any]:
    return {
        "pool": pool,
        "readings": 120,
        "first": uni_instant(datetime(2026, 6, 1, tzinfo=timezone.utc)),
        "last": uni_instant(NOW),
    }


def bar_status(**overrides: Any) -> dict[str, Any]:
    document = {
        "time": uni_instant(datetime(2026, 10, 14, tzinfo=timezone.utc)),
        "complete": True,
        "close_block": 23456789,
        "prices": {"WETH": "2567.89"},
        "finality": "final",
        "suspect": False,
        "flags": [],
    }
    document.update(overrides)
    return document


def coverage() -> dict[str, Any]:
    return {"what": "bars", "source": "chain", "complete": 120, "of": 120, "per_token": {"WETH": 120}}


def uniswap_document(run: Mapping[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
    """A ``uniswap_v3`` ``status --json`` document with one run (the default one unless given)."""
    document = {
        "schema_version": 1,
        "source": "uniswap_v3",
        "generated_at": uni_instant(NOW),
        "store": {
            "chain_id": 1,
            "interval_seconds": 86400,
            "quote": "USDC",
            "pools": [pool_extent()],
            "latest_bars": [bar_status()],
            "coverage": [coverage()],
        },
        "run": uniswap_run() if run is None else run,
    }
    document.update(overrides)
    return document


def light(kind: str) -> dict[str, Any]:
    return {"kind": kind, "text": f"{kind} on the day"}


def carry_day(boundary: datetime, lights: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    return {
        "boundary": boundary.date().isoformat(),
        "intent_in": False,
        "perp_funding": "0.00",
        "perp_funding_pending": 0,
        "perp_fees": "0.00",
        "spot_swap_cost": "0.00",
        "spot_gas_eth": "0",
        "spot_gas_cost": "0.00",
        "costs": "0.00",
        "net": "0.00",
        "unpriced": False,
        "perp_size": "0",
        "perp_mark": "2567.89",
        "perp_as_of": None,
        "perp_equity": "10000.00",
        "spot_qty": "0",
        "spot_price": "2567.89",
        "spot_as_of": None,
        "spot_equity": "10000.00",
        "delta": "0",
        "delta_share": None,
        "basis": "0.0012",
        "cycles": {"applied": 6, "maintained": 0, "held_d5": 0, "failed": 0},
        "refused": 0,
        "spot_outcome": "held",
        "spot_target": "0",
        "held_by_d5": any(light["kind"] == "held by D5" for light in lights),
        "alerts": list(lights),
    }


def handoff(as_of: datetime, **overrides: Any) -> dict[str, Any]:
    document = {
        "path": "handoff-ETH.json",
        "as_of": carry_instant(as_of),
        "written_at": carry_instant(as_of - timedelta(minutes=10)),
        "action": "stay_out",
        "perp_side": "flat",
        "margin_pct": 0,
        "spot_token": "WETH",
        "spot_weight": "0",
        "read_at": None,
        "funding_hourly": None,
        "z": None,
    }
    document.update(overrides)
    return document


def week_row(days: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    year, week, _ = datetime.fromisoformat(days[0]["boundary"]).isocalendar()
    return {
        "year": year,
        "week": week,
        "days": len(days),
        "days_in": sum(1 for day in days if day["intent_in"]),
        "funding": "0.00",
        "perp_fees": "0.00",
        "spot_swap_cost": "0.00",
        "spot_gas_cost": "0.00",
        "costs": "0.00",
        "net": "0.00",
        "unpriced": 0,
        "funding_pending": 0,
        "alerts": sum(len(day["alerts"]) for day in days),
    }


def problem(path: str = "payloads/2026-10-13.json", reason: str = "not JSON") -> dict[str, Any]:
    return {"path": path, "reason": reason}


def carry_document(
    days: Sequence[Mapping[str, Any]] | None = None, **overrides: Any
) -> dict[str, Any]:
    """A ``carry`` ``report --json`` document: the last day is yesterday, the handoff today's."""
    today = datetime(2026, 10, 14, tzinfo=timezone.utc)
    if days is None:
        days = [carry_day(today - timedelta(days=n)) for n in (3, 2, 1)]
    document = {
        "schema_version": 1,
        "source": "carry",
        "generated_at": carry_instant(NOW),
        "coin": "ETH",
        "token": "WETH",
        "perp_run_id": "carry-ETH-1",
        "spot_run_id": "paper-carry-1",
        "since": days[0]["boundary"],
        "until": days[-1]["boundary"],
        "delta_alert_pct": 10,
        "d5_days": 3,
        "handoff": handoff(today),
        "handoff_error": None,
        "days": list(days),
        "problems": [],
        "weeks": [week_row(days)],
        "span": {
            "days": len(days),
            "days_in": sum(1 for day in days if day["intent_in"]),
            "funding": "0.00",
            "funding_pending": 0,
            "costs": "0.00",
            "unpriced": 0,
            "net": "0.00",
            "alerts": 0,
            "held_by_d5": 0,
        },
    }
    document.update(overrides)
    return document


# --- the host ----------------------------------------------------------------------


def unit_text(
    *,
    type_: str = "simple",
    load: str = "loaded",
    active: str = "active",
    sub: str = "running",
    result: str = "success",
    restarts: int = 0,
    start: str = "Mon 2026-10-12 05:21:10 UTC",
    exit_: str = "",
    status: int = 0,
) -> str:
    """What ``systemctl show --property=...`` prints for a unit."""
    return (
        f"Type={type_}\nLoadState={load}\nActiveState={active}\nSubState={sub}\n"
        f"Result={result}\nNRestarts={restarts}\nExecMainStartTimestamp={start}\n"
        f"ExecMainExitTimestamp={exit_}\nExecMainStatus={status}\n"
    )


ONESHOT_OK = unit_text(
    type_="oneshot",
    active="inactive",
    sub="dead",
    start="Tue 2026-10-14 00:10:02 UTC",
    exit_="Tue 2026-10-14 00:31:40 UTC",
)
NOT_FOUND = "Type=\nLoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=success\n"

UNITS_QUIET: dict[str, str] = {
    "hl-paper.service": unit_text(),
    "hl-paper-carry-eth.service": unit_text(),
    "uniswap-v3-paper.service": ONESHOT_OK,
    "carry-signal.service": ONESHOT_OK,
}

MEMINFO = "MemTotal:        2003876 kB\nMemFree:          150000 kB\nMemAvailable:    1300000 kB\n"


class FakeRunner:
    """A :data:`Runner` that answers from dictionaries and remembers every call.

    A line's command is answered by ``documents[<module>:<run id>]`` — a
    document (printed as JSON, exit 0) or a :class:`CommandResult`;
    ``systemctl show <unit>`` by ``units[<unit>]`` (``NOT_FOUND`` when the
    unit is not there); ``git rev-parse`` by ``commit``; ``journalctl`` by
    ``journal[<unit>]`` lines (``None``: journalctl refuses).
    """

    def __init__(
        self,
        documents: Mapping[str, Mapping[str, Any] | CommandResult],
        *,
        units: Mapping[str, str] = UNITS_QUIET,
        commit: str | None = "b17efda8",
        journal: Mapping[str, int | None] | None = None,
    ) -> None:
        self.documents = dict(documents)
        self.units = dict(units)
        self.commit = commit
        self.journal = dict(journal or {})
        self.calls: list[tuple[list[str], Path, float]] = []

    def __call__(self, argv: Sequence[str], cwd: Path, timeout: float) -> CommandResult:
        argv = list(argv)
        self.calls.append((argv, cwd, timeout))
        if argv[0] == "systemctl":
            return CommandResult(0, self.units.get(argv[2], NOT_FOUND), "")
        if argv[0] == "git":
            if self.commit is None:
                return CommandResult(128, "", "fatal: not a git repository")
            return CommandResult(0, self.commit + "\n", "")
        if argv[0] == "journalctl":
            lines = self.journal.get(argv[2])
            if lines is None:
                return CommandResult(1, "", "No journal files were opened")
            return CommandResult(0, "something failed\n" * lines, "")
        module = argv[2]
        flag = "--perp-run-id" if module == "contrib.carry" else "--run-id"
        run_id = argv[argv.index(flag) + 1]
        answer = self.documents[f"{module}:{run_id}"]
        if isinstance(answer, CommandResult):
            return answer
        return CommandResult(0, json.dumps(answer, indent=2) + "\n", "")

    def line_calls(self) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if argv[1:2] == ["-m"]]


HL_KEY = "contrib.hyperliquid_perp:paper-BTC-8"
CARRY_PERP_KEY = "contrib.hyperliquid_perp:carry-ETH-1"
UNI_TREND_KEY = "contrib.uniswap_v3:paper-trend-1"
UNI_AI_KEY = "contrib.uniswap_v3:paper-ai-1"
UNI_CARRY_KEY = "contrib.uniswap_v3:paper-carry-1"
CARRY_KEY = "contrib.carry:carry-ETH-1"


def quiet_documents() -> dict[str, Any]:
    """Every line's document on a quiet host: no rule fires at :data:`NOW`."""
    return {
        HL_KEY: hyperliquid_document(),
        CARRY_PERP_KEY: hyperliquid_document(run_id="carry-ETH-1"),
        UNI_TREND_KEY: uniswap_document(),
        UNI_AI_KEY: uniswap_document(uniswap_run(run_id="paper-ai-1")),
        UNI_CARRY_KEY: uniswap_document(uniswap_run(run_id="paper-carry-1")),
        CARRY_KEY: carry_document(),
    }


def build_config(
    tmp_path: Path,
    *,
    thresholds: Thresholds | None = None,
    digest_utc: time = time(3, 30),
    marker: Path | None = None,
) -> ExporterConfig:
    """The six lines of the example config, on ``tmp_path``."""
    hl = tmp_path / "hl"
    uni = tmp_path / "uniswap-paper"
    python = Path("/usr/bin/python3")
    data = tmp_path / "data"
    return ExporterConfig(
        status_dir=tmp_path / "status",
        lines=(
            HyperliquidLine(
                "hl-paper",
                python,
                hl,
                "hl-paper.service",
                data / "paper_trading.db",
                "paper-BTC-8",
                segment_marker=marker,
            ),
            HyperliquidLine(
                "carry-perp",
                python,
                hl,
                "hl-paper-carry-eth.service",
                data / "carry" / "paper_trading.db",
                "carry-ETH-1",
            ),
            UniswapLine(
                "uniswap-trend",
                python,
                uni,
                "uniswap-v3-paper.service",
                uni / "paper-trend.local.yaml",
                data / "uniswap" / "paper.db",
                "paper-trend-1",
            ),
            UniswapLine(
                "uniswap-ai",
                python,
                uni,
                "uniswap-v3-paper.service",
                uni / "paper-ai.local.yaml",
                data / "uniswap" / "paper.db",
                "paper-ai-1",
            ),
            UniswapLine(
                "uniswap-carry",
                python,
                uni,
                "uniswap-v3-paper.service",
                uni / "paper-carry.local.yaml",
                data / "uniswap" / "paper.db",
                "paper-carry-1",
            ),
            CarryLine(
                "carry",
                python,
                hl,
                "carry-signal.service",
                "ETH",
                data / "carry" / "paper_trading.db",
                "carry-ETH-1",
                data / "uniswap" / "paper.db",
                "paper-carry-1",
                data / "carry" / "handoff-ETH.json",
            ),
        ),
        thresholds=thresholds or Thresholds(),
        digest_utc=digest_utc,
    )


def decimal_texts(value: Any) -> set[str]:
    """Every string in ``value`` that spells a number with a decimal point, and every ``0x`` one."""
    found: set[str] = set()
    if isinstance(value, str):
        stripped = value.lstrip("-")
        if (stripped.replace(".", "", 1).isdigit() and "." in stripped) or value.startswith("0x"):
            found.add(value)
    elif isinstance(value, Mapping):
        for item in value.values():
            found |= decimal_texts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            found |= decimal_texts(item)
    return found


# --- one tick's reads and host facts, for the rule, letter and exporter tests ------------


def meminfo_file(tmp_path: Path, text: str = MEMINFO) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(text, encoding="ascii")
    return path


def host_reader_for(tmp_path: Path, *, meminfo_text: str = MEMINFO, hostname: str = "tokyo"):
    """A ``read_host`` bound to ``tmp_path``: its disk, a meminfo file, a fixed host name."""
    from functools import partial

    from contrib.dashboard.host import read_host

    return partial(
        read_host,
        disk_path=tmp_path,
        meminfo=meminfo_file(tmp_path, meminfo_text),
        hostname=lambda: hostname,
    )


def read_lines_and_host(
    tmp_path: Path,
    documents: Mapping[str, Any],
    *,
    now: datetime = NOW,
    units: Mapping[str, str] = UNITS_QUIET,
    config: ExporterConfig | None = None,
    runner: Any = None,
    meminfo_text: str = MEMINFO,
    hostname: str = "tokyo",
):
    """``(config, reads, host)`` as a tick reads them, from ``documents`` (or ``runner``)."""
    from contrib.dashboard.readers import read_line

    config = config or build_config(tmp_path)
    runner = runner or FakeRunner(documents, units=units)
    reads = tuple(read_line(line, runner=runner, timeout=60) for line in config.lines)
    host = host_reader_for(tmp_path, meminfo_text=meminfo_text, hostname=hostname)(
        config, now=now, runner=runner
    )
    return config, reads, host


# --- the status directory as the page reads it ---------------------------------------

# The page's default: a document older than three ticks is stale.
STALE = timedelta(minutes=6)
# The six lines of build_config, in the exporter's order.
LINE_NAMES = ("hl-paper", "carry-perp", "uniswap-trend", "uniswap-ai", "uniswap-carry", "carry")


def status_dir(
    tmp_path: Path,
    documents: Mapping[str, Any] | None = None,
    *,
    units: Mapping[str, str] = UNITS_QUIET,
    now: datetime = NOW,
) -> Path:
    """One exporter tick (no mail, no ping) over ``documents`` (the quiet host's unless given)."""
    from contrib.dashboard.exporter import run_once

    config = build_config(tmp_path)
    runner = FakeRunner(quiet_documents() if documents is None else documents, units=units)
    run_once(
        config,
        Settings(mail=None, healthchecks_url=None),
        now=now,
        runner=runner,
        host_reader=host_reader_for(tmp_path),
    )
    return config.status_dir


def need_web() -> None:
    """Skip without the page's requirements (the trader's venv has none); fail where CI requires them.

    ``DASHBOARD_WEB_TESTS=required`` in the environment turns the skip into
    an import error, so a dropped install step fails the job.
    """
    if os.environ.get("DASHBOARD_WEB_TESTS") == "required":
        import fastapi  # noqa: F401
        import httpx  # noqa: F401

        return
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
