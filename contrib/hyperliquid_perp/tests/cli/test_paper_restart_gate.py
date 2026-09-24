"""``cli.paper.gate_restart`` — a paper restart's mode, decided directly.

The daemon drives in ``tests/cli/test_cli.py`` pin what each mode prints and
runs; this file pins the decision.
"""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.cli.paper import RestartGate, gate_restart


def _gate(*, live: bool | None, **facts) -> tuple[RestartGate, int]:
    """Run the gate; ``live=None`` means the book must not be read at all."""
    reads = []

    def _holds_live_work() -> bool:
        reads.append(1)
        if live is None:
            raise AssertionError("the gate read the book with no fault to decide")
        return live

    fields = {"replay_mismatch": False, "key_present": True, "engine_failed": False}
    fields.update(facts)
    return gate_restart(holds_live_work=_holds_live_work, **fields), len(reads)


@pytest.mark.parametrize(
    ("facts", "live", "expected"),
    [
        ({}, None, RestartGate(halt_reason=None, exits=False)),
        (
            {"replay_mismatch": True, "key_present": False, "engine_failed": True},
            None,
            RestartGate(halt_reason="replay", exits=False),
        ),
        ({"key_present": False}, True, RestartGate(halt_reason="missing-key", exits=False)),
        ({"key_present": False}, False, RestartGate(halt_reason="missing-key", exits=True)),
        (
            {"engine_failed": True},
            True,
            RestartGate(halt_reason="engine-config-error", exits=False),
        ),
        (
            {"engine_failed": True},
            False,
            RestartGate(halt_reason="engine-config-error", exits=True),
        ),
        (
            {"key_present": False, "engine_failed": True},
            False,
            RestartGate(halt_reason="missing-key", exits=True),
        ),
    ],
    ids=[
        "healthy-trades",
        "replay-halts-first",
        "keyless-live-halts",
        "keyless-flat-exits",
        "engine-live-halts",
        "engine-flat-exits",
        "missing-key-named-first",
    ],
)
def test_gate_restart(facts, live, expected):
    gate, reads = _gate(live=live, **facts)
    assert gate == expected
    assert reads == (0 if live is None else 1)
