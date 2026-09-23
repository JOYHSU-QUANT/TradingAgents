"""The paths refactor plan v2 T1 vacated still hand out the SAME objects.

Plan §2 rule 4: a move PR leaves the old path re-exporting, and the next PR
deletes it. Pinned by identity, not by importability — a second definition
under the old name would import fine and split the type (an ``isinstance``
against ``paper.scheduler.RetryableDecisionError`` catching nothing raised
through ``runtime.decision``). Both tests go when the shims go.
"""

from __future__ import annotations

import importlib

_PACKAGE = "contrib.hyperliquid_perp"

# (old module, name, module that defines it now)
_MOVED = (
    ("paper.clock", "Clock", "ports"),
    ("paper.clock", "ManualClock", "runtime.clock"),
    ("paper.clock", "WallClock", "runtime.clock"),
    ("paper.run_lock", "LOCK_STALE_SECONDS", "runtime.run_lock"),
    ("paper.run_lock", "RunLockError", "runtime.run_lock"),
    ("paper.run_lock", "acquire_run_lock", "runtime.run_lock"),
    ("paper.run_lock", "heartbeat_run_lock", "runtime.run_lock"),
    ("paper.run_lock", "lease_age_label", "runtime.run_lock"),
    ("paper.run_lock", "peek_run_lock", "runtime.run_lock"),
    ("paper.run_lock", "release_run_lock", "runtime.run_lock"),
    ("paper.position_facts", "BookFacts", "runtime.position_facts"),
    ("paper.position_facts", "BookPosition", "runtime.position_facts"),
    ("paper.position_facts", "BookSource", "runtime.position_facts"),
    ("paper.position_facts", "read_books", "runtime.position_facts"),
    ("paper.market_feed", "PortSnapshotProvider", "runtime.market_feed"),
    ("paper.market_feed", "PriceSnapshot", "runtime.market_feed"),
    ("paper.market_feed", "ScriptedSnapshotProvider", "runtime.market_feed"),
    ("paper.market_feed", "SnapshotOutcome", "runtime.market_feed"),
    ("paper.market_feed", "SnapshotProvider", "ports"),
    ("paper.market_feed", "SnapshotResult", "runtime.market_feed"),
    ("paper.engine", "AssetSpec", "runtime.asset_spec"),
    ("paper.engine", "FundingSource", "ports"),
    ("paper.scheduler", "DecisionInput", "runtime.decision"),
    ("paper.scheduler", "DecisionProvider", "ports"),
    ("paper.scheduler", "RetryableDecisionError", "runtime.decision"),
    ("paper.twap", "qty_step_from_sz_decimals", "runtime.asset_spec"),
    ("paper.liquidation", "price_tick_from_sz_decimals", "runtime.asset_spec"),
)


def test_every_vacated_path_reexports_the_object_its_new_home_defines():
    split = [
        f"{old}.{name} is not {new}.{name}"
        for old, name, new in _MOVED
        if getattr(importlib.import_module(f"{_PACKAGE}.{old}"), name)
        is not getattr(importlib.import_module(f"{_PACKAGE}.{new}"), name)
    ]
    assert not split, split


def test_the_four_shim_modules_export_exactly_the_names_they_used_to():
    # ``__all__`` of each shim is the old module's surface; a name dropped
    # here is a silent break for any ``from ..paper.clock import *``-style
    # reader, and one added is a second home.
    for old in ("paper.clock", "paper.run_lock", "paper.position_facts", "paper.market_feed"):
        module = importlib.import_module(f"{_PACKAGE}.{old}")
        expected = sorted(name for shim, name, _ in _MOVED if shim == old)
        assert sorted(module.__all__) == expected, old
