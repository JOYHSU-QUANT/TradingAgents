"""Stand-ins for targets: synthetic targets on the test tokens, a config that reads them, and handoff documents."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from contrib.uniswap_v3.config import UniswapConfig
from contrib.uniswap_v3.domain.targets import Target, TargetRecord, TargetSettings
from contrib.uniswap_v3.domain.times import utc_text
from contrib.uniswap_v3.domain.verdicts import bytes_digest

from .engine import DAY, FIRST_DAY, config as _config

__all__ = [
    "SOURCE",
    "config",
    "handoff_document",
    "record",
    "target",
    "targeted_config",
    "write_handoff",
]

# The source the tests' targets come from.
SOURCE = "test-coordinator"
_EXAMPLE = Path(__file__).resolve().parents[2] / "configs" / "uniswap_v3.example.yaml"
_EXAMPLE_STRATEGY = (
    "strategy:\n"
    "  name: fixed_weights\n"
    "  params:\n"
    "    weights:\n"
    '      USDC: "0.5"\n'
    '      WETH: "0.3"\n'
    '      WBTC: "0.2"\n'
    '    band: "0.05"\n'
)


def targeted_config(directory: Path, source: str = SOURCE, *, wbtc: bool = False) -> Path:
    """The example config written under ``directory`` as a carry run's: WETH alone, ``external_weights``, targets of ``source``.

    With ``wbtc`` the example's WBTC token and pool are kept: a config that
    trades more than the handoff sizes, for the tests of what refuses it.
    Each edit is pinned: a change to the example that moves what is edited
    fails here rather than silently leaving the config as it was.
    """
    text = _EXAMPLE.read_text(encoding="utf-8")
    edits = [
        ("# targets:\n#   source: carry-eth-v1", f"targets:\n  source: {source}"),
        (_EXAMPLE_STRATEGY, 'strategy:\n  name: external_weights\n  params:\n    band: "0.05"\n'),
    ]
    if not wbtc:
        edits += [
            ("tokens: [USDC, WETH, WBTC]", "tokens: [USDC, WETH]"),
            ("  - WBTC/WETH-500\n", ""),
        ]
    for old, new in edits:
        assert old in text, f"the example no longer holds {old!r}"
        text = text.replace(old, new, 1)
    path = directory / "targeted.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def target(
    symbol: str = "WETH", day: int = 0, weight: str = "0.3", *, source: str = SOURCE
) -> Target:
    """``weight`` of ``symbol`` at the bar ``day`` days after :data:`FIRST_DAY`."""
    return Target(source=source, symbol=symbol, time=FIRST_DAY + day * DAY, weight=Decimal(weight))


def handoff_document(
    *,
    as_of: int = FIRST_DAY,
    token: str = "WETH",
    weight: str = "0.3",
    coin: str = "ETH",
    action: str | None = "enter",
    version: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    """A handoff as the carry coordinator writes one, for the bar ``as_of``; ``extra`` goes over the top.

    The fields this leg reads, the ones it is told to ignore in the same
    shapes the coordinator writes them (the human-readable instants, the
    ``perp`` block), and nothing else of the real document.
    """
    doc: dict[str, Any] = {
        "version": version,
        "coin": coin,
        "as_of_ms": as_of * 1_000,
        "as_of": utc_text(as_of),
        "written_at_ms": as_of * 1_000 - 600_000,
        "written_at": utc_text(as_of - 600),
        "action": action,
        "perp": {"side": "short", "margin_pct": 30},
        "spot": {"token": token, "weight": weight},
    }
    doc.update(extra)
    return doc


def write_handoff(path: Path, doc: object) -> str:
    """Write ``doc`` as JSON at ``path`` and return the digest the ``target`` command will store."""
    raw = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return bytes_digest(raw)


def record(
    symbol: str = "WETH",
    day: int = 0,
    weight: str = "0.3",
    *,
    source: str = SOURCE,
    written_after: int = 600,
    **changes: Any,
) -> TargetRecord:
    """A stored synthetic target, written ``written_after`` seconds after its bar, with a digest of nothing."""
    aimed = target(symbol, day, weight, source=source)
    fields: dict[str, Any] = {
        "target": aimed,
        "written_at": aimed.time + written_after,
        "digest": bytes_digest(b""),
    }
    return TargetRecord(**{**fields, **changes})


def config(source: str = SOURCE, **execution: Any) -> UniswapConfig:
    """The engine fakes' config, reading the targets of ``source``."""
    return replace(_config(**execution), targets=TargetSettings(source=source))
