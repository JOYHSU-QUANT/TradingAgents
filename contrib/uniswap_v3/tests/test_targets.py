"""The target types: what a coordinator's target is, what a stored one carries, and what is refused."""

from __future__ import annotations

from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.targets import (
    Target,
    TargetRecord,
    TargetSettings,
    require_weight,
)
from contrib.uniswap_v3.tests.fakes.targets import SOURCE, record, target

_DIGEST = "ab" * 32


def test_a_target_names_its_source_token_bar_and_weight():
    aimed = target("WETH", 1, "0.3")
    assert (aimed.source, aimed.symbol, aimed.weight) == (SOURCE, "WETH", Decimal("0.3"))
    # The two ends of the range are targets too.
    assert target(weight="0").weight == 0
    assert target(weight="1").weight == 1


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"source": "two words"}, "source must be letters"),
        ({"symbol": ""}, "symbol must be a non-empty string"),
        ({"time": -1}, "time must be a non-negative integer"),
        ({"time": True}, "time must be a non-negative integer"),
        ({"weight": Decimal("1.0001")}, r"weight must be a Decimal in \[0, 1\]"),
        ({"weight": Decimal("-0")}, r"weight must be a Decimal in \[0, 1\]"),
        ({"weight": Decimal("NaN")}, r"weight must be a Decimal in \[0, 1\]"),
        ({"weight": 0.3}, r"weight must be a Decimal in \[0, 1\]"),
        ({"weight": "0.3"}, r"weight must be a Decimal in \[0, 1\]"),
    ],
)
def test_a_target_that_is_not_one_is_refused(fields, match):
    by_hand = {"source": SOURCE, "symbol": "WETH", "time": 100, "weight": Decimal("0.3")}
    with pytest.raises(ValueError, match=match):
        Target(**{**by_hand, **fields})


def test_require_weight_is_the_one_rule():
    for value in (Decimal(0), Decimal("0.5"), Decimal(1)):
        require_weight(value, "w")
    with pytest.raises(ValueError, match=r"w must be a Decimal in \[0, 1\], got Decimal\('2'\)"):
        require_weight(Decimal(2), "w")


def test_a_record_carries_when_it_was_written_and_the_documents_digest():
    stored = record("WETH", 0, "0.3", digest=_DIGEST)
    assert stored.target == target("WETH", 0, "0.3")
    assert stored.written_at == target().time + 600
    assert stored.digest == _DIGEST


@pytest.mark.parametrize(
    ("fields", "match"),
    [
        ({"target": "WETH"}, "target must be a Target"),
        ({"written_at": -1}, "written_at must be a non-negative integer"),
        ({"digest": "xyz"}, "digest must be a SHA-256 digest"),
        ({"digest": _DIGEST.upper()}, "digest must be a SHA-256 digest"),
    ],
)
def test_a_record_that_is_not_one_is_refused(fields, match):
    by_hand = {"target": target(), "written_at": 100, "digest": _DIGEST}
    with pytest.raises(ValueError, match=match):
        TargetRecord(**{**by_hand, **fields})


def test_settings_name_one_source():
    assert TargetSettings(source="carry-eth-v1").source == "carry-eth-v1"
    with pytest.raises(ValueError, match="source must be letters"):
        TargetSettings(source="")
