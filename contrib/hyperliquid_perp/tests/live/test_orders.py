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
    local_status_for_exchange_status,
    parse_order_status,
)
from contrib.hyperliquid_perp.paper.clock import ManualClock
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.cloid import (
    cloid_hex as derive_cloid_hex,
    cloid_logical,
)
from contrib.hyperliquid_perp.persistence.db import Database

from ..conftest import echo_order_status_cloid

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

    def place_ioc_limit(
        self, *, coin, is_buy, size, limit_price, cloid_hex, reduce_only, protective=False
    ):
        self.place_calls.append(
            {
                "coin": coin,
                "is_buy": is_buy,
                "size": size,
                "limit_price": limit_price,
                "cloid_hex": cloid_hex,
                "reduce_only": reduce_only,
                "protective": protective,
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
        return echo_order_status_cloid(result, cloid_hex)


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


def test_risk_adding_order_still_blocked_in_safe_mode(env):
    # A safe mode dropped state_reconciled; a risk-adding (entry) order is still
    # gate-rejected before any evidence — the protective exemption is de-risking only.
    db, client, gate, submitter = env
    gate.state_reconciled = False
    with pytest.raises(LiveOrderGateRejected):
        _submit(submitter, order_role="entry")
    assert client.place_calls == []  # rejected before any wire call


def test_emergency_close_routes_through_the_protective_gate(env):
    # §17.2: an emergency_close IOC rides the protective gate (sendable while a safe
    # mode has dropped state_reconciled / raised manual_safe_mode) and carries
    # protective=True down to the wire backstop.
    db, client, gate, submitter = env
    gate.state_reconciled = False
    gate.manual_safe_mode = True
    ec_logical = cloid_logical(
        prefix="hta",
        run_id="r",
        symbol="BTC",
        output_id="na",
        plan_id="ec1",
        leg="na",
        slice_index=0,
        order_role="emergency_close",
    )
    client.place_results = [_RESTING_ACK]
    outcome = submitter.submit_ioc_limit(
        order_id="ec1",
        coin="BTC",
        side="sell",
        size=Decimal("0.01"),
        limit_price=Decimal("100"),
        cloid_logical=ec_logical,
        order_role="emergency_close",
        reduce_only=True,
    )
    assert outcome.outcome == "acknowledged"
    assert client.place_calls[0]["protective"] is True


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
    # A WIRE-scoped condition: the submitter asks check_order, which no longer
    # carries the decision-scoped trio (risk_gate_approved / active_slice_plan /
    # unresolved_protection_failure — those gate check_new_target, per §9.3).
    gate.kill_switch_active = False
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


def test_a_resent_order_does_not_keep_its_old_rejection_reason(env):
    # A rejected attempt is NOT exchange-known (only acknowledged/duplicate are),
    # so rule 5 permits a resend of the same cloid once orderStatus confirms the
    # cloid is absent — and that resend patches the SAME orders row. The accepted
    # patch must therefore CLEAR status_reason, not leave it untouched: a filled
    # order carrying the previous attempt's rejection text would misread as a
    # rejected fill to anyone (PR 4's reconciliation included) reading the row.
    db, client, _, submitter = env
    client.place_results = [_REJECT_ACK]
    client.status_results = [_UNKNOWN_STATUS]
    assert _submit(submitter).outcome == "rejected"
    row = repo.get_order(db.conn, "o1")
    assert row["status"] == "rejected" and row["status_reason"] is not None

    # Same cloid, resent (rule 5: orderStatus says the exchange never took it).
    client.place_results = [_RESTING_ACK]
    client.status_results = [_UNKNOWN_STATUS]
    assert _submit(submitter).outcome == "acknowledged"
    row = repo.get_order(db.conn, "o1")
    assert row["status"] == "open"
    assert row["status_reason"] is None  # the stale rejection text is gone


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
    with pytest.raises(ExchangeError, match="reached the exchange"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # never resent


def test_a_recovered_order_is_never_resent_when_order_status_later_forgets_it(env):
    # §8.3 rule 10, the arm the ATTEMPT rows cannot see. A successful recovery
    # writes its proof (the exchange's own oid) to the ORDERS row and
    # deliberately does not back-patch the attempt row — and on the pre-check
    # path it recovers with no attempt row at all. So the durable evidence that
    # "the exchange took this cloid" can live only on orders.exchange_order_id:
    #
    #   1. send times out -> attempt 'failed' (the order actually rests)
    #   2. retry: orderStatus finds it RESTING (oid 111) -> recovered_existing,
    #      orders row back-filled; the attempt row still says 'failed'
    #   3. retry again: orderStatus now answers unknownOid (Info lag/retention).
    #      Reading only the attempts (['failed']) would call that "confirmed
    #      absent" and RESEND — a second live order for one logical order.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)

    client.status_results = [
        {"status": "order", "order": {"order": {"oid": 111}, "status": "resting"}}
    ]
    assert _submit(submitter).outcome == "recovered_existing"
    assert repo.get_order(db.conn, "o1")["exchange_order_id"] == "111"
    # Recovery leaves the attempt row untouched — that is the trap.
    assert [a["status"] for a in repo.iter_live_order_attempts(db.conn, "r")] == ["failed"]

    client.status_results = [_UNKNOWN_STATUS]
    with pytest.raises(ExchangeError, match="reached the exchange"):
        _submit(submitter)
    assert len(client.place_calls) == 1  # NOT 2 — the recovered oid forbade it


def test_duplicate_cloid_missing_from_order_status_never_resends(env):
    # §8.3 rule 10, the other half of the evidence. A 'duplicate' attempt row is
    # proof at least as strong as an ack — the EXCHANGE ITSELF said the cloid
    # exists. Reading only 'acknowledged' here let this sequence resend:
    #
    #   1. send times out -> attempt 'failed' (the order actually rests)
    #   2. retry: unknownOid -> resend -> exchange answers "duplicate" ->
    #      attempt 'duplicate', recovery still unknownOid -> correctly refuses
    #   3. retry again (rule 1 mandates the SAME cloid): the guard saw only
    #      ['failed','duplicate'], found no 'acknowledged', read unknownOid as
    #      "confirmed absent" -> RESENT. If the original had filled, that is a
    #      double position.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)

    # Two unknownOid answers: the pre-check asks (a prior attempt exists), and
    # the duplicate ack asks again before it may refuse.
    client.place_results = [_DUPLICATE_ACK]
    client.status_results = [_UNKNOWN_STATUS, _UNKNOWN_STATUS]
    with pytest.raises(ExchangeError, match="refusing to resend"):
        _submit(submitter)
    assert [r["status"] for r in repo.iter_live_order_attempts(db.conn, "r")] == [
        "failed",
        "duplicate",
    ]

    # The third call is the one that used to resend.
    client.status_results = [_UNKNOWN_STATUS]
    with pytest.raises(ExchangeError, match="reached the exchange"):
        _submit(submitter)
    assert len(client.place_calls) == 2  # NOT 3 — the duplicate row forbade it


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
    # The EXACT table is the ONLY authority — including for iocCancelRejected,
    # the vocabulary's one both-words status ("rejected because the IOC could
    # not match"): a placement rejection, nothing ever rested, and reading it as
    # canceled would report a never-placed order as recovered. No substring
    # heuristic decides any of these.
    assert local_status_for_exchange_status("iocCancelRejected") == "rejected"
    assert local_status_for_exchange_status("tickRejected") == "rejected"
    assert local_status_for_exchange_status("minTradeNtlRejected") == "rejected"
    assert local_status_for_exchange_status("scheduledCancel") == "canceled"
    assert local_status_for_exchange_status("liquidatedCanceled") == "canceled"
    assert local_status_for_exchange_status("resting") == "open"
    assert local_status_for_exchange_status("filled") == "filled"


def test_an_unknown_exchange_status_is_never_guessed_at():
    # The table carries Hyperliquid's complete documented vocabulary, so anything
    # outside it is a word the exchange gained afterwards — and EVERY guess about
    # such a word is unsafe in some direction, so we make none:
    #
    #   * guessing "rejected" licenses the caller to mint a NEW logical order
    #     (§8.3 rule 9), so a reject-ish word on a still-RESTING order = double
    #     position — the same bug class as the rule-10 resend guard;
    #   * guessing a TERMINAL status abandons a possibly-live order (a word like
    #     cancelRequested = cancel in flight, order still resting, would be booked
    #     canceled and a later fill would land against a canceled order).
    #
    # "open" is the one conservative reading: the order keeps being watched and
    # PR 4's reconciliation settles it against the exchange. It must also not
    # raise — that would break §8.3 recovery for every order carrying the word.
    assert local_status_for_exchange_status("someBrandNewStatusWord") == "open"
    # ...reject-ish: must NOT become "rejected".
    assert local_status_for_exchange_status("someNewThingRejected") == "open"
    assert local_status_for_exchange_status("someNewCancelRejected") == "open"
    # ...terminal-ish: must NOT become "canceled" / "filled".
    assert local_status_for_exchange_status("cancelRequested") == "open"
    assert local_status_for_exchange_status("pendingCancel") == "open"
    assert local_status_for_exchange_status("partiallyFilledSomehow") == "open"


def test_recovery_of_an_unrecognised_status_lands_open_and_does_not_resend(env):
    # The flow-level half: an unknown status word must still recover, not blow
    # up the retry path and not resend a cloid the exchange demonstrably knows.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [
        {"status": "order", "order": {"order": {"oid": 77}, "status": "someBrandNewStatusWord"}}
    ]
    outcome = _submit(submitter)
    assert outcome.outcome == "recovered_existing"
    row = repo.get_order(db.conn, "o1")
    assert row["status"] == "open"
    assert row["exchange_raw_status"] == "someBrandNewStatusWord"  # verbatim, §16.1
    assert len(client.place_calls) == 1  # never resent


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


# ---- review round 7: the rule-5 resend must not leave a terminal orders row ----


def test_resend_after_rejection_restamps_the_order_row_to_submitted(env):
    # THE DOUBLE-POSITION BUG. Attempt 0 is rejected by the exchange and
    # orderStatus confirms the cloid is absent, so the orders row is settled
    # 'rejected'. Rule 5 then permits a resend of the SAME cloid. If the intent
    # transaction leaves the row at 'rejected' — a TERMINAL status, not in
    # LIVE_ORDER_STATUSES — a crash inside the send window leaves a live order
    # that PR 4's reconciliation skips as settled, and an engine rebuilding
    # intent from the DB reads "rejected" and is licensed (§8.3 rule 9) to mint a
    # NEW logical order for a position it may already hold.
    db, client, _, submitter = env
    client.place_results = [_REJECT_ACK]
    client.status_results = [_UNKNOWN_STATUS]
    assert _submit(submitter).outcome == "rejected"
    row = repo.get_order(db.conn, "o1")
    assert row["status"] == "rejected" and row["status_reason"] == "Insufficient margin"

    # The resend: margin freed up, same cloid_logical (rule 6). Freeze the wire
    # call to inspect the row exactly as a crash mid-send would leave it.
    client.status_results = [_UNKNOWN_STATUS]

    seen: dict[str, sqlite3.Row] = {}

    def _capture(**kwargs):
        seen["row"] = repo.get_order(db.conn, "o1")
        return _RESTING_ACK

    client.place_ioc_limit = _capture  # type: ignore[method-assign]
    outcome = _submit(submitter)

    mid_send = seen["row"]
    assert mid_send["status"] == "submitted", (
        "the durable row during the resend's network window must be non-terminal"
    )
    assert mid_send["status"] in repo.LIVE_ORDER_STATUSES
    # The stale rejection reason is gone — it described the PREVIOUS send.
    assert mid_send["status_reason"] is None
    assert outcome.outcome == "acknowledged"


def test_a_failed_payload_write_never_erases_an_earlier_recorded_path(env, monkeypatch):
    # update_order's _UNSET convention: an omitted keyword leaves the column
    # alone, an explicit None CLEARS it. _write_raw_payload returns None when the
    # disk write fails — passing that through would delete the pointer to the
    # ORIGINAL ack's payload, for an order the recovery just confirmed is live.
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]
    _submit(submitter)
    original = repo.get_order(db.conn, "o1")["raw_exchange_payload_path"]
    assert original is not None

    # Now a same-cloid retry recovers through orderStatus, but the disk is full.
    from contrib.hyperliquid_perp.live import payloads

    monkeypatch.setattr(payloads.Path, "write_text", _boom)
    client.status_results = [_KNOWN_STATUS]
    outcome = _submit(submitter)

    assert outcome.outcome == "recovered_existing"
    assert repo.get_order(db.conn, "o1")["raw_exchange_payload_path"] == original, (
        "a failed payload write must omit the column, not clear it"
    )


def _boom(*_args, **_kwargs):
    raise OSError("disk full")


def test_a_busy_db_does_not_replace_the_exchange_error_that_caused_it(env, monkeypatch):
    # The exchange failure is the diagnosis. If the transaction that records it
    # raises (sqlite BUSY at the worst moment), THAT error must not propagate in
    # its place — the caller would read "database is locked" where the true story
    # was a timeout on a possibly-live order.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("read timed out")]

    real_transaction = db.transaction
    calls = {"n": 0}

    def _transaction_then_fail():
        calls["n"] += 1
        if calls["n"] == 1:  # the intent transaction must still work
            return real_transaction()
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "transaction", _transaction_then_fail)

    with pytest.raises(ExchangeRequestError, match="read timed out"):
        _submit(submitter)


# The foreign cloid a misrouted venue answer carries. Shared with the identity
# -echo tests further down this file (they read it off the SAME constant), so
# "the stranger" means one value everywhere in here rather than two literals
# that a later edit could drift apart.
_OTHER_CLOID = "0x" + "cd" * 16
_STRANGER_ACK_BODY = {
    "status": "ok",
    "response": {
        "type": "order",
        "data": {"statuses": [{"resting": {"oid": 4242, "cloid": _OTHER_CLOID}}]},
    },
}


def test_a_misrouted_ack_leaves_its_payload_on_disk_and_on_the_attempt_row(env):
    """The other half of issue #47's evidence gap.

    ``signed_client`` attaches the refused round-trip to the exception; THIS is
    the layer that owns ``payload_dir``, so it has to persist it. The
    accepted-ack write further down ``submit_ioc_limit`` is unreachable here —
    the raise happens inside the ``place_ioc_limit`` call above it — so without
    this the stranger's oid and the rest of the body were simply gone, and the
    durable trail held only the two cloids ``str(exc)`` names.
    """
    db, client, _, submitter = env
    refusal = MalformedResponseError(
        f"Hyperliquid resting order status for cloid {_HEX} answered with cloid "
        f"{_OTHER_CLOID!r} — refusing to book another order's ack"
    )
    refusal.attach_payload(_STRANGER_ACK_BODY)
    client.place_results = [refusal]

    with pytest.raises(MalformedResponseError, match="refusing to book"):
        _submit(submitter)

    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["failed"]  # outcome unknown, unchanged
    raw_path = attempts[0]["raw_exchange_payload_path"]
    assert raw_path is not None, "the refused ack's payload path must be on the row"
    written = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    assert written == _STRANGER_ACK_BODY
    # The one fact str(exc) could never carry: WHOSE order the venue answered
    # with. That is what makes keeping the body worth anything.
    assert written["response"]["data"]["statuses"][0]["resting"]["oid"] == 4242


def test_a_wire_failure_with_no_payload_records_no_evidence_path(env):
    """Narrowness: only a refusal that CARRIES a body writes one.

    A timeout has no payload to keep, and inventing an evidence file for it —
    or clearing the column with an explicit NULL — would make the pointer
    meaningless on exactly the rows that do have one.
    """
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("read timed out")]

    with pytest.raises(ExchangeRequestError, match="read timed out"):
        _submit(submitter)

    attempts = repo.iter_live_order_attempts(db.conn, "r", cloid_hex=_HEX)
    assert [a["status"] for a in attempts] == ["failed"]
    assert attempts[0]["raw_exchange_payload_path"] is None
    assert list(submitter._payload_dir.glob("*.json")) == []


def test_the_two_contradiction_cases_escape_a_transport_retry_lane(env):
    # Both mean "an order MAY be live under this cloid; a human must look".
    # ExchangeError is the BASE of ExchangeRequestError, so raising it bare let
    # the obvious `except ExchangeRequestError: retry` idiom swallow a
    # double-position risk. They are now a distinct, named lane.
    from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import (
        OrderIdempotencyContradiction,
    )

    db, client, _, submitter = env
    client.place_results = [_DUPLICATE_ACK]
    client.status_results = [_UNKNOWN_STATUS]
    with pytest.raises(OrderIdempotencyContradiction):
        _submit(submitter)
    # Still an ExchangeError (callers may catch the base deliberately) but NOT a
    # request error, so a transport-retry handler cannot catch it.
    assert issubclass(OrderIdempotencyContradiction, ExchangeError)
    assert not issubclass(OrderIdempotencyContradiction, ExchangeRequestError)


def test_every_outcome_carries_the_exchange_verbatim_status_word(env):
    # The only non-guessing basis PR 4/5 have for telling a permanent rejection
    # (tick / min-notional — retry fails forever) from a transient one.
    db, client, _, submitter = env
    client.place_results = [_RESTING_ACK]
    assert _submit(submitter).exchange_raw_status == "resting"

    client.status_results = [
        {"status": "order", "order": {"order": {"oid": 9}, "status": "minTradeNtlRejected"}}
    ]
    outcome = _submit(submitter, order_id="o1")
    assert outcome.outcome == "rejected"
    assert outcome.exchange_raw_status == "minTradeNtlRejected"


# -- R4 loop: pre-wire store failures are typed, never "outcome unknown" ------


def test_pre_wire_store_failure_raises_presubmit_and_leaves_no_evidence(env, monkeypatch):
    """A transient sqlite failure BEFORE the network call means nothing was sent
    and nothing recorded — it must surface as LiveOrderPreSubmitError (the caller
    holds its cursor and retries the same cloid) rather than the generic
    "attempted, outcome unknown" shape that advances past a never-sent order."""
    from contrib.hyperliquid_perp.live.orders import LiveOrderPreSubmitError

    db, client, _, submitter = env

    def _locked(conn, *, cloid_hex):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "has_place_attempt", _locked)
    with pytest.raises(LiveOrderPreSubmitError):
        _submit(submitter)
    monkeypatch.undo()
    assert client.place_calls == []  # never reached the wire
    assert repo.get_order(db.conn, "o1") is None  # no evidence row survives
    # The store healed: the SAME logical order goes through under the same cloid.
    client.place_results = [_RESTING_ACK]
    outcome = _submit(submitter)
    assert outcome.outcome == "acknowledged"
    assert len(client.place_calls) == 1


# ---------------------------------------------------------------------------
# identity echo (2026-08-17): orderStatus must answer for the queried cloid
# ---------------------------------------------------------------------------


_OTHER_CLOID = "0x" + "cd" * 16


def test_order_status_cloid_mismatch_raises_never_reads():
    # An answer carrying another order's cloid is a misrouted response; reading
    # its oid/status would bind a stranger's order to this cloid and feed the
    # verdict into a §8.3 resend decision.
    payload = {
        "status": "order",
        "order": {"order": {"oid": 1, "cloid": _OTHER_CLOID}, "status": "filled"},
    }
    with pytest.raises(MalformedResponseError, match="answered with cloid"):
        parse_order_status(payload, expected_cloid_hex=_HEX)


def test_order_status_missing_cloid_raises():
    # Bot orders always carry a cloid (§8.3 rule 7) and the venue echoes it, so
    # an inner order WITHOUT one is format drift — the strict side of the
    # 2026-08-17 decision — and must not quietly disarm the identity check.
    payload = {"status": "order", "order": {"order": {"oid": 1}, "status": "filled"}}
    with pytest.raises(MalformedResponseError, match="answered with cloid None"):
        parse_order_status(payload, expected_cloid_hex=_HEX)


def test_order_status_cloid_match_is_case_insensitive():
    # The hex digits are the identity, not their case (checksummed vs lowercase).
    payload = {
        "status": "order",
        "order": {"order": {"oid": 7, "cloid": _HEX.upper()}, "status": "resting"},
    }
    reading = parse_order_status(payload, expected_cloid_hex=_HEX)
    # By NAME, not position: both members are strings, so a parser that swapped
    # them would still equal ``("7", "resting")``-shaped expectations written
    # the other way round (issue #132).
    assert reading is not None
    assert reading.exchange_order_id == "7"
    assert reading.status == "resting"


def test_order_status_unknownoid_needs_no_cloid_echo():
    # The documented miss shape has no order to echo anything from.
    assert parse_order_status({"status": "unknownOid"}, expected_cloid_hex=_HEX) is None


def test_recovery_refuses_a_wrong_cloid_order_status_answer(env):
    # Caller-level: the §8.3 recovery read propagates the identity failure loud
    # (the same lane as any malformed payload) instead of back-filling SQLite
    # with another order's oid and reporting recovered_existing.
    db, client, _, submitter = env
    client.place_results = [ExchangeRequestError("timeout")]
    with pytest.raises(ExchangeRequestError):
        _submit(submitter)
    client.status_results = [
        {
            "status": "order",
            "order": {"order": {"oid": 111, "cloid": _OTHER_CLOID}, "status": "resting"},
        }
    ]
    with pytest.raises(MalformedResponseError, match="answered with cloid"):
        _submit(submitter)
    # The pre-wire evidence row exists (written before the first send) and is
    # UNTOUCHED by the misrouted answer: still the pre-wire status, no
    # stranger's oid back-filled. Asserting the concrete word, not merely
    # "not open" — an absence-shaped assertion would also pass if a future
    # change wrote some other wrong-but-not-open status.
    order = repo.get_order(db.conn, "o1")
    assert order["exchange_order_id"] is None
    assert order["status"] == "submitted"
