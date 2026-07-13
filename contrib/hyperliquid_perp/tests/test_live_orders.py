"""Tests for the §8.3 idempotent submit protocol (fake exchange, no network).

The fake transport mirrors :class:`HyperliquidSignedClient`'s method contract
(post-PR-2-review: the §4.1 gate is checked by the submitter BEFORE any
evidence is written, and re-checked by the real client's bound gate at the
wire), so what is under test is the submitter's protocol: evidence before
network, outcome patches, duplicate resolution through orderStatus, and the
never-blind-resend rules.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
    ExchangeError,
    ExchangeRequestError,
    MalformedResponseError,
)
from contrib.hyperliquid_perp.exchanges.hyperliquid.signed_client import OrderAck
from contrib.hyperliquid_perp.live.config import ExecutionMode
from contrib.hyperliquid_perp.live.order_gate import LiveOrderGateRejected, RealOrderGate
from contrib.hyperliquid_perp.live.orders import (
    LiveOrderSubmitter,
    SubmitOutcome,
    _local_status_for_exchange_status,
)
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.cloid import cloid_hex as derive_cloid_hex
from contrib.hyperliquid_perp.persistence.db import Database

_NOW = datetime(2026, 7, 12, 8, 0, tzinfo=timezone.utc)
_LOGICAL = "hta_r_BTC_out1_plan1_open_000_entry"
_HEX = derive_cloid_hex(_LOGICAL)


class _FakeClient:
    """Scripted transport: each place/query call pops the next canned result."""

    def __init__(self):
        self.place_results: list = []
        self.status_results: list = []
        self.place_calls: list[dict] = []
        self.status_calls: list[str] = []

    def place_ioc_limit(self, *, coin, is_buy, size, limit_price, cloid_hex, reduce_only):
        self.place_calls.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "limit_price": limit_price,
                "cloid_hex": cloid_hex,
                "reduce_only": reduce_only,
            }
        )
        result = self.place_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def query_order_by_cloid(self, cloid_hex):
        self.status_calls.append(cloid_hex)
        result = self.status_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _open_gate() -> RealOrderGate:
    return RealOrderGate(
        allow_real_orders=True,
        mode=ExecutionMode.TESTNET_LIVE,
        allowed_symbols=("BTC",),
        agent_authorized=True,
        startup_reconciliation_passed=True,
        kill_switch_active=True,
        state_reconciled=True,
        risk_gate_approved=True,
    )


_RESTING_ACK = OrderAck(
    status="resting", exchange_order_id="111", raw={"status": "ok", "kind": "resting"}
)
_FILLED_ACK = OrderAck(
    status="filled",
    exchange_order_id="222",
    filled_size=Decimal("0.01"),
    average_price=Decimal("100"),
    raw={"status": "ok", "kind": "filled"},
)
_REJECT_ACK = OrderAck(status="error", error="Insufficient margin", raw={"kind": "reject"})
_DUPLICATE_ACK = OrderAck(status="error", error="Duplicate cloid", raw={"kind": "dup"})

_KNOWN_STATUS = {"status": "order", "order": {"order": {"oid": 333}, "status": "filled"}}
_REJECTED_STATUS = {"status": "order", "order": {"order": {"oid": 334}, "status": "rejected"}}
_SCHEDULED_CANCEL_STATUS = {
    "status": "order",
    "order": {"order": {"oid": 335}, "status": "scheduledCancel"},
}
_UNKNOWN_STATUS = {"status": "unknownOid"}


@pytest.fixture
def env(tmp_path: Path):
    db = Database(":memory:")
    client = _FakeClient()
    gate = _open_gate()
    submitter = LiveOrderSubmitter(
        client=client,
        gate=gate,
        db=db,
        run_id="r",
        payload_dir=tmp_path / "raw",
        clock=ManualClock(_NOW),
    )
    yield db, client, gate, submitter
    db.close()


def _submit(submitter, **overrides):
    fields = {
        "order_id": "o1",
        "coin": "BTC",
        "side": "buy",
        "size": Decimal("0.01"),
        "limit_price": Decimal("100"),
        "cloid_logical": _LOGICAL,
        "order_role": "entry",
        "output_id": "out1",
    }
    fields.update(overrides)
    return submitter.submit_ioc_limit(**fields)


def test_accepted_order_writes_evidence_then_backfills_ack(env):
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    assert outcome.exchange_order_id == "111"
    # Registry, order row and attempt all exist with the ack back-fill (§16.1).
    assert repo.get_cloid_by_hex(db.conn, _HEX)["cloid_logical"] == _LOGICAL
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "open"
    assert order["exchange_order_id"] == "111"
    # §16.1 vocabulary: exchange_status is the normalized family (the
    # orders.status words), exchange_raw_status the verbatim wire word.
    assert order["exchange_status"] == "open"
    assert order["exchange_raw_status"] == "resting"
    assert order["submitted_at"] is not None
    assert order["acknowledged_at"] is not None
    assert order["is_bot_owned"] == 1
    assert order["output_id"] == "out1"
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["acknowledged"]
    # The attempt row keeps the verbatim ack word — raw evidence trail.
    assert attempts[0]["exchange_status"] == "resting"
    # The raw exchange payload landed on disk and its path on the rows.
    raw_path = order["raw_exchange_payload_path"]
    assert raw_path is not None
    assert json.loads(Path(raw_path).read_text(encoding="utf-8"))["kind"] == "resting"
    # The wire call carried the derived cloid_hex, never the logical id (§8.3 rule 7).
    assert client.place_calls[0]["cloid_hex"] == _HEX


def test_ioc_filled_ack_maps_to_filled_status(env):
    db, client, _, submitter = env
    client.place_results = [_FILLED_ACK]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    assert repo.get_order(db.conn, "o1")["status"] == "filled"


def test_exchange_rejection_is_recorded_not_raised(env):
    db, client, _, submitter = env
    client.place_results = [_REJECT_ACK]
    # Before any REJECTED verdict, orderStatus (not the rejection wording)
    # gets the last word on rejected-vs-exists — here it confirms absence.
    client.status_results = [_UNKNOWN_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "rejected"
    assert outcome.error == "Insufficient margin"  # the ONE home for the reason
    assert client.status_calls == [_HEX]
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "rejected"
    assert order["status_reason"] == "Insufficient margin"
    # Orders row: normalized family + verbatim wire word (§16.1).
    assert order["exchange_status"] == "rejected"
    assert order["exchange_raw_status"] == "error"
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["rejected"]
    # The attempt keeps the verbatim ack word.
    assert attempts[0]["exchange_status"] == "error"


def test_rejection_ack_with_known_cloid_recovers_instead_of_rejecting(env):
    # The duplicate markers are a fast-path, not the authority: a duplicate
    # rejection whose wording they fail to match must not become REJECTED —
    # that verdict licenses the caller to mint a NEW logical order, a double
    # order if the original is live. orderStatus knowing the cloid wins.
    db, client, _, submitter = env
    client.place_results = [_REJECT_ACK]
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    assert outcome.exchange_order_id == "333"
    assert len(client.place_calls) == 1  # nothing was resent
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "filled"
    assert order["exchange_order_id"] == "333"


def test_gate_rejection_blocks_before_any_evidence_is_written(env):
    db, client, gate, submitter = env
    gate.risk_gate_approved = False
    client.place_results = [_RESTING_ACK]
    with pytest.raises(LiveOrderGateRejected):
        _submit(submitter)
    # §4.1: a gate-blocked order is order_created=false and NOTHING else — no
    # phantom 'submitted' rows, no registry entry, no wire traffic.
    assert client.place_calls == []
    assert repo.get_cloid_by_hex(db.conn, _HEX) is None
    assert repo.get_order(db.conn, "o1") is None
    assert repo.iter_live_order_attempts(db.conn, "r") == []


def test_duplicate_resolves_through_order_status_and_backfills(env):
    db, client, _, submitter = env
    client.place_results = [_DUPLICATE_ACK]
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    assert outcome.exchange_order_id == "333"
    assert outcome.attempt_id is not None  # resolved from this call's attempt
    # Rule 4: SQLite now agrees with the exchange; nothing was resent.
    order = repo.get_order(db.conn, "o1")
    assert order["exchange_order_id"] == "333"
    assert order["status"] == "filled"
    assert client.status_calls == [_HEX]
    assert len(client.place_calls) == 1  # the original send only
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["duplicate"]
    # The exchange answered this round-trip: stamped like the rejected patch,
    # so acknowledged_at NULL stays exactly "no answer observed".
    assert attempts[0]["exchange_status"] == "error"
    assert attempts[0]["acknowledged_at"] is not None


def test_duplicate_with_unknown_status_refuses_to_resend(env):
    db, client, _, submitter = env
    client.place_results = [_DUPLICATE_ACK, _RESTING_ACK]
    client.status_results = [_UNKNOWN_STATUS]
    with pytest.raises(ExchangeError, match="refusing to resend"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # rule 5: no blind second send


def test_transport_failure_marks_attempt_failed_and_reraises(env):
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["failed"]
    assert attempts[0]["error_message"] == "timeout"


def test_retry_after_unknown_outcome_queries_before_resending(env):
    db, client, _, submitter = env
    # First send: transport failure (outcome unknown — the order may exist).
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    # Retry: the exchange turns out to know the cloid → recover, don't resend.
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    assert outcome.attempt_id is None  # resolved pre-send: no new round-trip
    assert len(client.place_calls) == 1  # the failed send only
    assert client.status_calls == [_HEX]


def test_precheck_query_failure_propagates_and_never_resends(env):
    # §8.3 rule 5 permits a resend ONLY when the cloid is CONFIRMED absent.
    # If the pre-check's orderStatus query itself fails (timeout, transport),
    # that confirmation never happened: the error must propagate loud and no
    # second place call may reach the wire — a regression that swallowed the
    # query failure and read it as "absent" would double-send real money.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [ExchangeRequestError("orderStatus down")]
    with pytest.raises(ExchangeRequestError, match="orderStatus down"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # the failed first send only
    assert client.status_calls == [_HEX]


def test_duplicate_ack_query_failure_propagates_without_resend(env):
    # Same guarantee on the post-duplicate-ack path: duplicate says the cloid
    # exists somewhere, so a failed resolution query must fail loud, never
    # fall through to a resend or a fabricated outcome.
    db, client, _, submitter = env
    client.place_results = [_DUPLICATE_ACK]
    client.status_results = [ExchangeRequestError("orderStatus down")]
    with pytest.raises(ExchangeRequestError, match="orderStatus down"):
        _submit(submitter)
    assert len(client.place_calls) == 1
    # The duplicate verdict itself was settled on the trail before the query.
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["duplicate"]


def test_retry_after_acknowledged_send_never_blind_resends(env):
    # The exchange's duplicate rejection only guards OPEN orders: a filled
    # cloid would be accepted again as a brand-new order. So even a prior
    # 'acknowledged' attempt must route through orderStatus, not the wire.
    db, client, _, submitter = env
    client.place_results = [_FILLED_ACK]
    assert _submit(submitter).outcome == "acknowledged"
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)  # e.g. engine replay after a crash
    assert outcome.outcome == "recovered_existing"
    assert len(client.place_calls) == 1  # no second send
    assert client.status_calls == [_HEX]


def test_recovered_rejected_status_reports_rejected_not_recovered(env):
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [_REJECTED_STATUS]
    outcome = _submit(submitter)
    # The earlier send is CONFIRMED unsuccessful — the caller must not read
    # this as a live/filled order.
    assert outcome.outcome == "rejected"
    # Both rejected paths populate the same error field (never ack-only).
    assert outcome.error is not None and "rejected" in outcome.error
    assert repo.get_order(db.conn, "o1")["status"] == "rejected"
    assert len(client.place_calls) == 1  # the cloid is known: still no resend


def test_recovered_terminal_family_status_never_reads_as_open(env):
    # 'scheduledCancel' (the dead man's switch's own product) is not in the
    # exact-name map; the family classifier must land it on 'canceled'.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [_SCHEDULED_CANCEL_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    assert repo.get_order(db.conn, "o1")["status"] == "canceled"


def test_acknowledged_cloid_missing_from_order_status_fails_loud(env):
    # The durable trail says the exchange ACKNOWLEDGED this cloid; orderStatus
    # answering unknownOid contradicts that evidence (retention expiry, Info
    # inconsistency), and a resend would be accepted as a brand-new order —
    # same fail-loud posture as the duplicate/unknownOid contradiction.
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]
    assert _submit(submitter).outcome == "acknowledged"
    client.status_results = [_UNKNOWN_STATUS]
    with pytest.raises(ExchangeError, match="was acknowledged"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # never resent


def test_partial_ioc_fill_maps_to_partially_filled(env):
    # A 'filled' ack whose totalSz is below the requested size must not read
    # as fully filled — the local status says partially_filled while the
    # exchange columns keep the wire verdict verbatim.
    db, client, _, submitter = env
    client.place_results = [
        OrderAck(
            status="filled",
            exchange_order_id="444",
            filled_size=Decimal("0.004"),  # < the requested 0.01
            average_price=Decimal("100"),
            raw={"kind": "partial"},
        )
    ]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    order = repo.get_order(db.conn, "o1")
    assert order["status"] == "partially_filled"
    assert order["exchange_status"] == "filled"
    assert order["exchange_raw_status"] == "filled"


def test_order_id_reuse_under_a_different_cloid_fails_loud(env):
    # §8.3 leans on order_id↔cloid coherence: reusing an order_id under a new
    # cloid pair would send the wire order under the NEW cloid while the local
    # row still carries the old one. The intent transaction refuses.
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]
    assert _submit(submitter).outcome == "acknowledged"
    with pytest.raises(ValueError, match="cloid_hex"):
        _submit(submitter, cloid_logical="hta_r_BTC_out1_plan1_open_001_entry")
    assert len(client.place_calls) == 1  # raised before any wire traffic


def test_post_ack_persistence_failure_rolls_back_and_recovers_on_retry(env, monkeypatch):
    # The exchange accepted but the outcome transaction failed: the whole
    # transaction rolls back, so the attempt stays 'submitted' — its defined
    # terminal state for an unobserved outcome — and the retry resolves
    # through orderStatus without resending.
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(repo, "update_order", _boom)
    with pytest.raises(sqlite3.OperationalError):
        _submit(submitter)
    monkeypatch.undo()
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["submitted"]  # rolled back
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    assert len(client.place_calls) == 1  # no resend
    assert client.status_calls == [_HEX]


def test_raw_payload_write_failure_does_not_fail_the_submit(env, tmp_path):
    # The order is already live when the evidence file is written: an OSError
    # there must degrade to a warning + NULL path, never an exception.
    db, client, _, submitter = env
    (tmp_path / "raw").write_text("not a directory", encoding="utf-8")
    client.place_results = [_RESTING_ACK]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    assert repo.get_order(db.conn, "o1")["raw_exchange_payload_path"] is None


def test_retry_after_unknown_outcome_resends_when_exchange_confirms_absent(env):
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    # Rule 5: orderStatus confirms the cloid never landed → resend, SAME cloid.
    client.status_results = [_UNKNOWN_STATUS]
    client.place_results = [_RESTING_ACK]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    assert len(client.place_calls) == 2
    assert client.place_calls[0]["cloid_hex"] == client.place_calls[1]["cloid_hex"] == _HEX
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["attempt_index"] for a in attempts] == [0, 1]
    assert [a["status"] for a in attempts] == ["failed", "acknowledged"]


def test_exchange_status_family_classifier():
    # "reject" outranks "cancel": iocCancelRejected — the vocabulary's one
    # both-words status — is a placement rejection (nothing ever rested), and
    # reading it as canceled would report a never-placed order as recovered.
    assert _local_status_for_exchange_status("iocCancelRejected") == "rejected"
    assert _local_status_for_exchange_status("tickRejected") == "rejected"
    assert _local_status_for_exchange_status("minTradeNtlRejected") == "rejected"
    assert _local_status_for_exchange_status("scheduledCancel") == "canceled"
    assert _local_status_for_exchange_status("liquidatedCanceled") == "canceled"
    assert _local_status_for_exchange_status("resting") == "open"
    assert _local_status_for_exchange_status("filled") == "filled"


def test_recovered_ioc_cancel_rejected_reports_rejected(env):
    # Flow-level pin for the classifier fix: an IOC that could not match is a
    # rejection, never RECOVERED_EXISTING with a canceled row.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [
        {"status": "order", "order": {"order": {"oid": 336}, "status": "iocCancelRejected"}}
    ]
    outcome = _submit(submitter)
    assert outcome.outcome == "rejected"
    assert outcome.error is not None and "iocCancelRejected" in outcome.error
    assert repo.get_order(db.conn, "o1")["status"] == "rejected"
    assert len(client.place_calls) == 1  # the cloid is known: no resend


def test_malformed_order_status_payload_fails_loud_never_reads_as_absent(env):
    # Rule 5 permits a resend only on a CONFIRMED-absent cloid; a payload the
    # parser does not recognise must raise, not silently authorize a resend.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [{"weird": "shape"}]
    client.place_results = [_RESTING_ACK]
    with pytest.raises(MalformedResponseError, match="not recognised"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # no resend happened


def test_evidence_is_durable_before_the_wire_and_failure_is_patched(env):
    db, client, _, submitter = env

    class _Boom(Exception):
        pass

    def _check_then_boom(**kwargs):
        # At the moment of the network call, the intent evidence must already
        # be durable (§8.3: a process crash here must leave the marker behind).
        assert repo.get_cloid_by_hex(db.conn, _HEX) is not None
        attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
        assert [a["status"] for a in attempts] == ["submitted"]
        raise _Boom()

    client.place_ioc_limit = _check_then_boom
    with pytest.raises(_Boom):
        _submit(submitter)
    # A Python-level failure is patched to 'failed' (outcome unknown — the
    # request may or may not have left the process); a hard crash would leave
    # 'submitted'. Both are unknown-outcome markers for the retry pre-check.
    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["failed"]


def test_submit_outcome_enforces_its_evidence_contract():
    # A verdict whose evidence fields disagree with it cannot be constructed.
    with pytest.raises(ValueError, match="acknowledged"):
        SubmitOutcome(outcome="acknowledged", order_id="o1", cloid_logical=_LOGICAL, cloid_hex=_HEX)
    # A rejection must carry its reason.
    with pytest.raises(ValueError, match="rejected"):
        SubmitOutcome(
            outcome="rejected",
            order_id="o1",
            cloid_logical=_LOGICAL,
            cloid_hex=_HEX,
            attempt_id="a1",
            ack=_REJECT_ACK,
        )
    # The cloid pair must be a real derivation.
    with pytest.raises(ValueError, match="derivation"):
        SubmitOutcome(
            outcome="rejected",
            order_id="o1",
            cloid_logical=_LOGICAL,
            cloid_hex="0x" + "00" * 16,
            error="x",
            ack=_REJECT_ACK,
        )
    # A typo'd verdict is not silently a fourth state.
    with pytest.raises(ValueError):
        SubmitOutcome(outcome="acknowleged", order_id="o1", cloid_logical=_LOGICAL, cloid_hex=_HEX)
