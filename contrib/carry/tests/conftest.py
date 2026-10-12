"""Synthetic funding series and the stores the commands read, shared by the suites.

A series is hourly, as the venue settles, and starts at a UTC midnight so
the day boundaries the rule reads at fall on whole days of it. The base
rate alternates a tenth up and down so a window has a variance (a flat
window has no z-score, by the perp package's definition), and a hump
multiplies a run of days by a factor so the z-score has something to see.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from contrib.carry.upstream import (
    FUNDING_INTERVAL_MS,
    MS_PER_DAY,
    FundingPoint,
    ResearchStore,
    epoch_ms,
)

MS_PER_HOUR = FUNDING_INTERVAL_MS
# 2026-01-01T00:00:00Z, a UTC midnight.
DAY0 = 1_767_225_600_000
BASE = Decimal("0.0000100")
COIN = "ETH"


def day(n: int) -> int:
    """The UTC midnight ``n`` days after :data:`DAY0`."""
    return DAY0 + n * MS_PER_DAY


def hourly(start_ms: int, rates: Sequence[Decimal]) -> list[FundingPoint]:
    return [
        FundingPoint(time=start_ms + i * MS_PER_HOUR, rate=rate) for i, rate in enumerate(rates)
    ]


def alternating(days: int, *, base: Decimal = BASE) -> list[Decimal]:
    """``days`` days of hourly rates, a tenth above and below ``base`` in turn."""
    return [base * (Decimal("1.1") if i % 2 == 0 else Decimal("0.9")) for i in range(days * 24)]


def hump_series(
    days: int = 90, *, hump: tuple[int, int] = (40, 55), factor: int = 5
) -> list[FundingPoint]:
    """An alternating series with the days ``hump[0] <= d < hump[1]`` multiplied by ``factor``."""
    rates = alternating(days)
    for i, rate in enumerate(rates):
        d = i // 24
        if hump[0] <= d < hump[1]:
            rates[i] = rate * factor
    return hourly(DAY0, rates)


def write_research_store(path: Path, points: Sequence[FundingPoint], *, coin: str = COIN) -> Path:
    with ResearchStore(path) as store:
        store.upsert_funding(coin, points)
    return path


class FakeMarket:
    """The venue's funding endpoint over a fixed series, with its record cap."""

    CAP = 500

    def __init__(self, points: Sequence[FundingPoint]) -> None:
        self.points = sorted(points, key=lambda p: p.time)
        self.calls: list[tuple[str, int, datetime]] = []

    def get_funding_history(
        self, coin: str, window_days: int, *, end: datetime
    ) -> list[FundingPoint]:
        self.calls.append((coin, window_days, end))
        end_ms = epoch_ms(end, what="fake end")
        start_ms = end_ms - window_days * MS_PER_DAY
        return [p for p in self.points if start_ms <= p.time <= end_ms][: self.CAP]


def write_perp_store(path: Path, rows: Sequence[tuple[str, str, str]]) -> Path:
    """A perp store with only the table the coordinator reads: ``(timestamp, run_id, equity)``."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "CREATE TABLE account_snapshots ("
            "snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
            "mode TEXT NOT NULL DEFAULT 'paper', run_id TEXT NOT NULL, "
            "account_equity TEXT NOT NULL)"
        )
        conn.executemany(
            "INSERT INTO account_snapshots (timestamp, run_id, account_equity) VALUES (?, ?, ?)",
            rows,
        )
    conn.close()
    return path


def write_spot_store(
    path: Path, rows: Sequence[tuple[str, int, str]], *, quote: str = "USDC"
) -> Path:
    """A spot store with the tables the coordinator reads: ``runs`` (its quote) and the valuations."""
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, quote TEXT NOT NULL)")
        conn.executemany(
            "INSERT OR IGNORE INTO runs (run_id, quote) VALUES (?, ?)",
            [(run_id, quote) for run_id, _, _ in rows],
        )
        conn.execute(
            "CREATE TABLE valuations (run_id TEXT NOT NULL, time INTEGER NOT NULL, "
            "total_value TEXT NOT NULL, PRIMARY KEY (run_id, time))"
        )
        conn.executemany(
            "INSERT INTO valuations (run_id, time, total_value) VALUES (?, ?, ?)", rows
        )
    conn.close()
    return path


# --- the books the report reads ------------------------------------------------
#
# Only the columns the report's queries name, in the shapes the two packages
# write them (ISO instants on the perp side, epoch seconds and JSON objects of
# decimal text on the spot side).

_PERP_BOOKS = (
    "CREATE TABLE decision_attempts (decision_attempt_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
    "scheduled_at TEXT NOT NULL, input_id TEXT, output_id TEXT, status TEXT NOT NULL)",
    "CREATE TABLE ai_inputs (input_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, model TEXT)",
    "CREATE TABLE ai_outputs (output_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
    "decision_reason TEXT, risk_action TEXT NOT NULL)",
    "CREATE TABLE funding_events (funding_event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, "
    "symbol TEXT NOT NULL, funding_timestamp TEXT NOT NULL, funding_pnl TEXT, "
    "status TEXT NOT NULL)",
    "CREATE TABLE fills (fill_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, symbol TEXT NOT NULL, "
    "timestamp TEXT NOT NULL, fee TEXT NOT NULL)",
    "CREATE TABLE account_snapshots (snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "timestamp TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'paper', run_id TEXT NOT NULL, "
    "account_equity TEXT NOT NULL)",
    "CREATE TABLE position_snapshots (position_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "timestamp TEXT NOT NULL, run_id TEXT NOT NULL, symbol TEXT NOT NULL, "
    "position_size TEXT NOT NULL, mark_price TEXT NOT NULL)",
)
_SPOT_BOOKS = (
    "CREATE TABLE runs (run_id TEXT PRIMARY KEY, quote TEXT NOT NULL, config TEXT NOT NULL)",
    "CREATE TABLE decisions (run_id TEXT NOT NULL, time INTEGER NOT NULL, outcome TEXT NOT NULL, "
    "PRIMARY KEY (run_id, time))",
    "CREATE TABLE valuations (run_id TEXT NOT NULL, time INTEGER NOT NULL, balances TEXT NOT NULL, "
    "prices TEXT NOT NULL, total_value TEXT NOT NULL, PRIMARY KEY (run_id, time))",
    "CREATE TABLE fills (run_id TEXT NOT NULL, time INTEGER NOT NULL, leg INTEGER NOT NULL, "
    "token_in TEXT NOT NULL, token_out TEXT NOT NULL, amount_in TEXT NOT NULL, "
    "amount_out TEXT NOT NULL, gas_cost_eth TEXT NOT NULL, PRIMARY KEY (run_id, time, leg))",
    "CREATE TABLE targets (source TEXT NOT NULL, symbol TEXT NOT NULL, time INTEGER NOT NULL, "
    "weight TEXT NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (source, time, symbol))",
)

# The cycle kinds a test states, as (attempt status, ai_inputs.model, rationale).
_CYCLE_ROWS = {
    "applied": ("completed", "file-target", "carry handoff h.json: enter at X; perp leg short"),
    "pending": ("completed", "file-target", "carry handoff pending: the document is for X"),
    "unusable": ("completed", "file-target", "carry handoff unusable: no such file"),
    "failed": ("api_failed", "file-target", None),
    "engine": ("completed", "anthropic/claude-sonnet-5.5", "the model's reasoning"),
    "in_progress": ("in_progress", None, None),
}


def write_perp_books(
    path: Path,
    run_id: str,
    *,
    coin: str = COIN,
    cycles: Sequence[tuple[str, ...]] = (),
    funding: Sequence[tuple[str, str]] = (),
    fills: Sequence[tuple[str, str]] = (),
    snapshots: Sequence[tuple[str, str, str | None, str | None]] = (),
) -> Path:
    """A perp store with the books the report reads.

    ``cycles`` are ``(scheduled_at, kind[, risk_action])`` with the kinds of
    ``_CYCLE_ROWS``; ``funding`` ``(funding_timestamp, pnl)`` posted events
    (a pending one is always added, which the report must not count);
    ``fills`` ``(timestamp, fee)``; ``snapshots`` ``(timestamp, equity,
    size, mark)``, a position row written only when ``size`` is given.
    """
    conn = sqlite3.connect(path)
    with conn:
        for ddl in _PERP_BOOKS:
            conn.execute(ddl)
        for index, (at, kind, *rest) in enumerate(cycles):
            risk_action = rest[0] if rest else "approved"
            status, model, reason = _CYCLE_ROWS[kind]
            input_id = output_id = None
            if model is not None:
                input_id = f"in{index}"
                conn.execute(
                    "INSERT INTO ai_inputs (input_id, run_id, model) VALUES (?, ?, ?)",
                    (input_id, run_id, model),
                )
            if reason is not None:
                output_id = f"out{index}"
                conn.execute(
                    "INSERT INTO ai_outputs (output_id, run_id, decision_reason, risk_action) "
                    "VALUES (?, ?, ?, ?)",
                    (output_id, run_id, reason, risk_action),
                )
            conn.execute(
                "INSERT INTO decision_attempts (decision_attempt_id, run_id, scheduled_at, "
                "input_id, output_id, status) VALUES (?, ?, ?, ?, ?, ?)",
                (f"attempt{index}", run_id, at, input_id, output_id, status),
            )
        conn.executemany(
            "INSERT INTO funding_events (funding_event_id, run_id, symbol, funding_timestamp, "
            "funding_pnl, status) VALUES (?, ?, ?, ?, ?, 'posted')",
            [(f"f{i}", run_id, coin, at, pnl) for i, (at, pnl) in enumerate(funding)],
        )
        conn.execute(
            "INSERT INTO funding_events (funding_event_id, run_id, symbol, funding_timestamp, "
            "funding_pnl, status) VALUES ('pending-one', ?, ?, '2026-02-10T23:00:00+00:00', "
            "'999', 'pending')",
            (run_id, coin),
        )
        conn.executemany(
            "INSERT INTO fills (fill_id, run_id, symbol, timestamp, fee) VALUES (?, ?, ?, ?, ?)",
            [(f"fill{i}", run_id, coin, at, fee) for i, (at, fee) in enumerate(fills)],
        )
        for at, equity, size, mark in snapshots:
            conn.execute(
                "INSERT INTO account_snapshots (timestamp, run_id, account_equity) VALUES (?, ?, ?)",
                (at, run_id, equity),
            )
            if size is not None and mark is not None:
                conn.execute(
                    "INSERT INTO position_snapshots (timestamp, run_id, symbol, position_size, "
                    "mark_price) VALUES (?, ?, ?, ?, ?)",
                    (at, run_id, coin, size, mark),
                )
    conn.close()
    return path


def write_spot_books(
    path: Path,
    run_id: str,
    *,
    source: str | None = "carry-eth-v1",
    tokens: Sequence[str] = ("USDC", "WETH"),
    quote: str = "USDC",
    decisions: Sequence[tuple[int, str]] = (),
    valuations: Sequence[tuple[int, dict[str, str], dict[str, str], str]] = (),
    fills: Sequence[tuple[int, int, str, str, str, str, str]] = (),
    targets: Sequence[tuple[int, str, str]] = (),
) -> Path:
    """A spot store with the books the report reads.

    ``decisions`` are ``(bar seconds, outcome)``; ``valuations`` ``(bar
    seconds, balances, prices, total)``; ``fills`` ``(bar seconds, leg,
    token_in, token_out, amount_in, amount_out, gas_eth)``; ``targets``
    ``(bar seconds, weight, digest)`` under ``source`` for the WETH symbol.
    A ``source`` of ``None`` stores a config with no targets section.
    """
    config: dict[str, object] = {"quote_token": quote, "tokens": list(tokens)}
    if source is not None:
        config["targets"] = {"source": source}
    conn = sqlite3.connect(path)
    with conn:
        for ddl in _SPOT_BOOKS:
            conn.execute(ddl)
        conn.execute(
            "INSERT INTO runs (run_id, quote, config) VALUES (?, ?, ?)",
            (run_id, quote, json.dumps(config, sort_keys=True)),
        )
        conn.executemany(
            "INSERT INTO decisions (run_id, time, outcome) VALUES (?, ?, ?)",
            [(run_id, at, outcome) for at, outcome in decisions],
        )
        conn.executemany(
            "INSERT INTO valuations (run_id, time, balances, prices, total_value) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (run_id, at, json.dumps(balances), json.dumps(prices), total)
                for at, balances, prices, total in valuations
            ],
        )
        conn.executemany(
            "INSERT INTO fills (run_id, time, leg, token_in, token_out, amount_in, amount_out, "
            "gas_cost_eth) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(run_id, *fill) for fill in fills],
        )
        conn.executemany(
            "INSERT INTO targets (source, symbol, time, weight, digest) VALUES (?, 'WETH', ?, ?, ?)",
            [(source or "carry-eth-v1", at, weight, digest) for at, weight, digest in targets],
        )
    conn.close()
    return path


def handoff_document(
    *, as_of_ms: int, side: str = "short", margin_pct: int = 30, written_at_ms: int | None = None
) -> dict[str, object]:
    """A valid handoff document: an entry at ``as_of_ms`` when ``side`` is short, a stay-out otherwise."""
    from contrib.carry.handoff import build
    from contrib.carry.signal import OUT, Action, Params, Position, Reading, Side

    reading = Reading(
        at_ms=as_of_ms - MS_PER_HOUR,
        current=Decimal("0.00002"),
        z=2.0,
        samples=720,
        recent_mean=Decimal("0.00002"),
        recent_samples=24,
    )
    if side == "short":
        action, position = Action.ENTER, Position(Side.IN, as_of_ms)
    else:
        action, position = Action.STAY_OUT, OUT
    return build(
        coin=COIN,
        as_of_ms=as_of_ms,
        written_at_ms=as_of_ms - 10 * 60 * 1000 if written_at_ms is None else written_at_ms,
        action=action,
        position=position,
        params=Params(margin_pct=margin_pct),
        reading=reading,
        equity_perp=None,
        equity_spot=None,
    ).to_document()


def write_sidecar(
    payloads: Path, stem: str, document: dict[str, object], *, cycle_at: str, digest: str
) -> Path:
    """A ``<stem>.handoff.json`` as the perp file-target provider writes it; ``digest`` is the bare hex."""
    payloads.mkdir(parents=True, exist_ok=True)
    path = payloads / f"{stem}.handoff.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "path": "/srv/carry/handoff-ETH.json",
                "digest": f"sha256:{digest}",
                "cycle_at": cycle_at,
                "handoff": document,
            }
        ),
        encoding="utf-8",
    )
    return path
