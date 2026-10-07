"""Synthetic funding series and the stores the commands read, shared by the suites.

A series is hourly, as the venue settles, and starts at a UTC midnight so
the day boundaries the rule reads at fall on whole days of it. The base
rate alternates a tenth up and down so a window has a variance (a flat
window has no z-score, by the perp package's definition), and a hump
multiplies a run of days by a factor so the z-score has something to see.
"""

from __future__ import annotations

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
