"""The handoff reader: what it takes from the coordinator's file, what it ignores, and what it refuses."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from contrib.uniswap_v3.domain.verdicts import bytes_digest
from contrib.uniswap_v3.handoff import (
    HANDOFF_VERSION,
    HANDOFF_WINDOW_SECONDS,
    HandoffError,
    HandoffNotApplicable,
    HandoffTarget,
    check_applicable,
    read_handoff,
    target_from_document,
)
from contrib.uniswap_v3.tests.fakes.engine import DAY, FIRST_DAY
from contrib.uniswap_v3.tests.fakes.targets import handoff_document, write_handoff


def test_the_version_and_the_window_are_the_documents_contract():
    assert HANDOFF_VERSION == 1
    assert HANDOFF_WINDOW_SECONDS == DAY


# --- the file --------------------------------------------------------------------


def test_the_file_comes_back_as_its_bytes_and_its_json(tmp_path):
    path = tmp_path / "handoff-eth.json"
    digest = write_handoff(path, handoff_document())
    raw, doc = read_handoff(path)
    assert json.loads(raw) == doc == handoff_document()
    assert bytes_digest(raw) == digest
    assert len(digest) == 64


def test_a_file_that_is_not_there_yet_is_not_applicable(tmp_path):
    with pytest.raises(HandoffNotApplicable, match="no handoff at .*handoff-eth.json yet"):
        read_handoff(tmp_path / "handoff-eth.json")


def test_a_directory_in_place_of_the_file_is_an_error(tmp_path):
    with pytest.raises(HandoffError, match="cannot read"):
        read_handoff(tmp_path)


@pytest.mark.parametrize("raw", [b"{not json", b"\xff\xfe", b""])
def test_a_file_that_is_not_utf8_json_is_an_error(tmp_path, raw):
    path = tmp_path / "handoff-eth.json"
    path.write_bytes(raw)
    with pytest.raises(HandoffError, match="is not UTF-8 JSON"):
        read_handoff(path)


# --- the document ----------------------------------------------------------------


def test_the_spot_block_is_read_and_the_rest_ignored():
    doc = handoff_document(as_of=FIRST_DAY + DAY, weight="0.4500", action="hold", later="field")
    doc["perp"] = "whatever the other leg reads"
    doc["spot"]["extra"] = 1
    found = target_from_document(doc)
    assert found == HandoffTarget(
        coin="ETH", as_of=FIRST_DAY + DAY, token="WETH", weight=Decimal("0.4500"), action="hold"
    )


def test_an_action_may_be_null_and_is_otherwise_a_string():
    assert target_from_document(handoff_document(action=None)).action is None
    with pytest.raises(HandoffError, match="action must be a string or None, got 3"):
        target_from_document(handoff_document(action=3))


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"version": 2}, "handoff version 2 is not 1"),
        ({"version": "1"}, "handoff version '1' is not 1"),
        ({"version": True}, "handoff version True is not 1"),
        ({"coin": None}, "coin must be a non-empty string"),
        ({"coin": ""}, "coin must be a non-empty string"),
        ({"as_of_ms": "1704067200000"}, "as_of_ms must be a non-negative integer"),
        ({"as_of_ms": 1704067200000.0}, "as_of_ms must be a non-negative integer"),
        ({"as_of_ms": True}, "as_of_ms must be a non-negative integer"),
        ({"as_of_ms": 1704067200500}, "is not a whole second"),
        ({"as_of_ms": -1000}, "as_of_ms must be a non-negative integer"),
        # A whole number no calendar reaches, on a day boundary and off one.
        ({"as_of_ms": 10**25 * 86_400_000}, "is not an instant this host can spell"),
        ({"as_of_ms": 10**25 * 86_400_000 + 1_000}, "is not an instant this host can spell"),
        (
            {"as_of_ms": (FIRST_DAY + 3_600) * 1_000},
            "as_of 2024-01-01T01:00:00Z is not a UTC day boundary",
        ),
        ({"spot": None}, "spot block must be an object"),
        ({"spot": {"weight": "0.3"}}, "spot.token must be a non-empty string"),
        ({"spot": {"token": " ", "weight": "0.3"}}, "spot.token must be a non-empty string"),
        ({"spot": {"token": "WETH"}}, "spot.weight must be a decimal string"),
        ({"spot": {"token": "WETH", "weight": 0.3}}, "spot.weight must be a decimal string"),
        ({"spot": {"token": "WETH", "weight": "abc"}}, "spot.weight must be a quoted decimal"),
        ({"spot": {"token": "WETH", "weight": "NaN"}}, "spot.weight must be a quoted decimal"),
        ({"spot": {"token": "WETH", "weight": " 0.3"}}, "spot.weight must be a quoted decimal"),
        (
            {"spot": {"token": "WETH", "weight": "1.5"}},
            r"spot.weight must be a Decimal in \[0, 1\]",
        ),
        ({"spot": {"token": "WETH", "weight": "-0"}}, r"spot.weight must be a Decimal in \[0, 1\]"),
    ],
)
def test_a_document_this_leg_cannot_read_is_an_error(changes, match):
    doc = handoff_document()
    doc.update(changes)
    with pytest.raises(HandoffError, match=match):
        target_from_document(doc)


@pytest.mark.parametrize("doc", [None, [], "handoff", 1])
def test_a_document_that_is_not_an_object_is_an_error(doc):
    with pytest.raises(HandoffError, match="the handoff must be an object"):
        target_from_document(doc)


def test_a_target_built_by_hand_is_checked_too():
    with pytest.raises(HandoffError, match="as_of must be a non-negative integer"):
        HandoffTarget(coin="ETH", as_of=True, token="WETH", weight=Decimal("0.3"), action=None)
    with pytest.raises(HandoffError, match="is not a UTC day boundary"):
        HandoffTarget(coin="ETH", as_of=3_600, token="WETH", weight=Decimal("0.3"), action=None)
    with pytest.raises(HandoffError, match="action must be a string or None"):
        HandoffTarget(coin="ETH", as_of=0, token="WETH", weight=Decimal("0.3"), action=1)


# --- the window ------------------------------------------------------------------

_TARGET = target_from_document(handoff_document(as_of=FIRST_DAY + DAY))


def test_the_target_applies_from_its_boundary_until_the_next():
    check_applicable(_TARGET, now=FIRST_DAY + DAY)
    check_applicable(_TARGET, now=FIRST_DAY + 2 * DAY - 1)


def test_a_boundary_ahead_is_not_applicable_yet_and_a_day_ahead_is_an_error():
    # The coordinator writes at 23:50 for midnight: ten minutes early is the schedule.
    with pytest.raises(HandoffNotApplicable, match="which has not come yet"):
        check_applicable(_TARGET, now=FIRST_DAY + DAY - 600)
    # The day before is still "not yet"; a second further is nothing the schedule writes.
    with pytest.raises(HandoffNotApplicable, match="which has not come yet"):
        check_applicable(_TARGET, now=FIRST_DAY)
    with pytest.raises(HandoffError, match="more than a day after now"):
        check_applicable(_TARGET, now=FIRST_DAY - 1)


def test_a_boundary_a_day_or_more_behind_is_not_applicable():
    with pytest.raises(HandoffNotApplicable, match="a day or more before now"):
        check_applicable(_TARGET, now=FIRST_DAY + 2 * DAY)
    with pytest.raises(HandoffNotApplicable, match="has not written a newer one"):
        check_applicable(_TARGET, now=FIRST_DAY + 9 * DAY)
