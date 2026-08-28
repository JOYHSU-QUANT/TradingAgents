"""``common.config_coercion``: the scalar converters' refusal edges.

The dataclass-level tests (``test_risk_gate`` / ``test_target_decision`` /
``test_config``) pin the bool and unknown-key refusals through their own
``from_dict``; this file pins the converter contract directly, so a new
consumer cannot rely on an edge no test names.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from contrib.hyperliquid_perp.common.config_coercion import decimal_from_yaml


@pytest.mark.parametrize("text", [".nan", ".inf", "-.inf"])
def test_decimal_from_yaml_refuses_a_non_finite_yaml_scalar(text):
    # Issue #128: YAML 1.1 parses these to floats, ``Decimal(str(...))`` accepts
    # them, and the dataclasses' range checks then raise decimal.InvalidOperation
    # — an ArithmeticError no config-error handler catches. Refuse at the
    # coercion point, as a ValueError naming the value.
    value = yaml.safe_load(text)
    assert isinstance(value, float)
    with pytest.raises(ValueError, match="finite") as exc_info:
        decimal_from_yaml(value)
    assert repr(value) in str(exc_info.value)


def test_decimal_from_yaml_still_accepts_finite_scalars_exactly():
    # Negative control for the finiteness gate: the str round-trip that keeps
    # float digits intact is unchanged.
    assert decimal_from_yaml(0.1) == Decimal("0.1")
    assert decimal_from_yaml("2.5") == Decimal("2.5")
    assert decimal_from_yaml(3) == Decimal(3)


@pytest.mark.parametrize("text", ["leverage: .nan", "leverage: .inf", "leverage: -.inf"])
def test_a_non_finite_risk_value_is_a_named_config_error_not_a_traceback(text, capsys):
    # The operator-visible shape of #128: the risk/decision loader's named
    # exit-1 lane only catches ValueError, so before the fix a `.nan` leverage
    # escaped it as decimal.InvalidOperation from RiskConfig.__post_init__.
    # Now the refusal names the key.
    from contrib.hyperliquid_perp.engine_bridge import _load_risk_decision

    assert _load_risk_decision({"risk": yaml.safe_load(text)}) is None
    err = capsys.readouterr().err
    assert "config key 'leverage'" in err
    assert "finite" in err
