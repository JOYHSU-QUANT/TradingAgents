"""Tests for the two-layer cloid derivation (phase3-spec §8.2)."""

from __future__ import annotations

import hashlib

import pytest

from contrib.hyperliquid_perp.persistence.cloid import (
    LIVE_ORDER_ROLES,
    assert_cloid_provenance,
    cloid_hex,
    cloid_logical,
)

_KW = {
    "prefix": "hta",
    "run_id": "live_20260708",
    "symbol": "BTC",
    "output_id": "out123",
    "plan_id": "plan456",
    "leg": "open",
    "slice_index": 0,
    "order_role": "entry",
}


def test_logical_matches_the_spec_example_format():
    assert cloid_logical(**_KW) == "hta_live_20260708_BTC_out123_plan456_open_000_entry"


def test_slice_index_is_zero_padded_and_sorts_in_slice_order():
    ids = [cloid_logical(**{**_KW, "slice_index": i}) for i in (0, 2, 10, 119)]
    assert ids == sorted(ids)
    assert "_010_" in ids[2]


def test_provenance_accepts_the_fields_the_id_was_built_from():
    logical = cloid_logical(**_KW)
    assert_cloid_provenance(logical, run_id=_KW["run_id"], symbol="BTC", order_role="entry")


@pytest.mark.parametrize("role", sorted(LIVE_ORDER_ROLES))
def test_provenance_accepts_every_role_round_trip(role):
    logical = cloid_logical(**{**_KW, "order_role": role})
    assert_cloid_provenance(logical, run_id=_KW["run_id"], symbol="BTC", order_role=role)


def test_provenance_rejects_a_role_the_id_was_not_built_for():
    logical = cloid_logical(**{**_KW, "order_role": "entry"})
    with pytest.raises(ValueError, match="was not built for order_role"):
        assert_cloid_provenance(logical, run_id=_KW["run_id"], symbol="BTC", order_role="close")


def test_provenance_is_not_fooled_by_a_role_that_suffixes_another():
    # 'close' is a suffix of 'emergency_close': a bare endswith test would pass
    # this mismatched pair straight into the audit trail. The role is anchored
    # to the zero-padded slice index that always precedes it.
    logical = cloid_logical(**{**_KW, "order_role": "emergency_close"})
    with pytest.raises(ValueError, match="was not built for order_role"):
        assert_cloid_provenance(logical, run_id=_KW["run_id"], symbol="BTC", order_role="close")


def test_provenance_rejects_a_foreign_run_or_symbol():
    logical = cloid_logical(**_KW)
    with pytest.raises(ValueError, match="does not carry run_id"):
        assert_cloid_provenance(logical, run_id="other_run", symbol="BTC", order_role="entry")
    with pytest.raises(ValueError, match="does not carry symbol"):
        assert_cloid_provenance(logical, run_id=_KW["run_id"], symbol="ETH", order_role="entry")


def test_hex_is_deterministic_sha256_prefix():
    logical = cloid_logical(**_KW)
    expected = "0x" + hashlib.sha256(logical.encode("utf-8")).digest()[:16].hex()
    assert cloid_hex(logical) == expected
    # Same input, same output — the property the §8.3 retry protocol rests on.
    assert cloid_hex(logical) == cloid_hex(logical)


def test_hex_is_128_bits_wire_format():
    value = cloid_hex(cloid_logical(**_KW))
    assert value.startswith("0x")
    assert len(value) == 34  # 0x + 32 hex chars = 16 bytes
    int(value, 16)  # parses as hex


def test_different_logical_ids_map_to_different_hex():
    a = cloid_hex(cloid_logical(**_KW))
    b = cloid_hex(cloid_logical(**{**_KW, "slice_index": 1}))
    assert a != b


def test_live_role_vocabulary_is_the_section_8_1_list():
    assert {
        "entry",
        "rebalance",
        "close",
        "stop_loss",
        "take_profit",
        "emergency_close",
        "cleanup_cancel",
    } == LIVE_ORDER_ROLES


def test_unknown_order_role_is_rejected():
    with pytest.raises(ValueError, match="order_role"):
        cloid_logical(**{**_KW, "order_role": "yolo"})


def test_negative_slice_index_is_rejected():
    with pytest.raises(ValueError, match="slice_index"):
        cloid_logical(**{**_KW, "slice_index": -1})


@pytest.mark.parametrize("field", ["prefix", "run_id", "symbol", "output_id", "plan_id", "leg"])
def test_empty_or_whitespace_segments_are_rejected(field):
    with pytest.raises(ValueError, match="segment"):
        cloid_logical(**{**_KW, field: ""})
    with pytest.raises(ValueError, match="segment"):
        cloid_logical(**{**_KW, field: "a b"})


def test_hex_rejects_empty_logical():
    with pytest.raises(ValueError, match="non-empty"):
        cloid_hex("")
