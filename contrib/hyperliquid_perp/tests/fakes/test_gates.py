"""Each gate builder passes the check it is named after and refuses the next wider one."""

from __future__ import annotations

import pytest

from .gates import exchange_action_gate, new_target_gate, order_gate, protective_order_gate


@pytest.mark.parametrize(
    ("build", "passes", "refuses"),
    [
        (exchange_action_gate, "check_exchange_action", "check_protective_order"),
        (protective_order_gate, "check_protective_order", "check_order"),
        (order_gate, "check_order", "check_new_target"),
    ],
)
def test_a_gate_builder_stops_at_the_check_it_is_named_after(build, passes, refuses):
    gate = build()
    assert getattr(gate, passes)("BTC") is None
    assert getattr(gate, refuses)("BTC") is not None


def test_the_new_target_gate_passes_the_widest_check():
    assert new_target_gate().check_new_target("BTC") is None
