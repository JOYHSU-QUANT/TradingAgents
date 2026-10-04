"""Recorded JSON-RPC responses, and the fixed points they were recorded at.

``mainnet.json`` is written by ``record.py`` from a real archive node; the
tests that replay it never reach a node.
"""

from __future__ import annotations

from pathlib import Path

CASSETTE = Path(__file__).with_name("mainnet.json")
# Every pool read and quote in the cassette is at this block.
BLOCK = 18_000_000
# 2023-10-01 00:00:00 UTC, the time the cassette's block search looks for.
BAR_TIME = 1_696_118_400
