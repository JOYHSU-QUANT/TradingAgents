"""``live.shutdown`` — the §18.2 shutdown decisions of ``live --run-id``, tested directly.

The CLI drives in ``tests/cli/test_cli.py`` pin the wording each decision
prints; this file pins the decisions themselves.
"""

from __future__ import annotations

import logging

import pytest

from contrib.hyperliquid_perp.live import shutdown as shutdown_mod
from contrib.hyperliquid_perp.live.shutdown import (
    ExitReason,
    ExitState,
    ShutdownFlags,
    ShutdownVerdict,
    classify_exit,
    classify_shutdown,
    read_exit_state,
    sweep_on_exit,
)
from contrib.hyperliquid_perp.live.venue_identity import EscalationHolder

_LIVE = ("a live position",)  # classify_shutdown reads only whether it is empty
_ARMED_LEFT = (
    "shutdown sweep left the kill switch armed — bot orders may still rest and "
    "the wallet-wide scheduleCancel will fire at the deadline"
)
_IDENTITY = (
    "venue identity fault latched — the exchange kept answering orderStatus "
    "about orders that are not ours; manual safe mode entered (see "
    "identity_fault_latched in protection_order_events and "
    "payloads/orderStatus-*.json)"
)


def _flags(
    *, positions=_LIVE, safe_mode_active=False, safe_mode_unknown=False, **overrides
) -> ShutdownFlags:
    """A clean exit over a live position; each test overrides what it needs."""
    fields = {
        "verdict_passed": True,
        "loop_raised": False,
        "loop_refused": False,
        "protection_only": False,
    }
    fields.update(overrides)
    return ShutdownFlags(
        **fields,
        exit_state=ExitState(
            positions=positions,
            safe_mode_active=safe_mode_active,
            safe_mode_unknown=safe_mode_unknown,
        ),
    )


# --- classify_shutdown ------------------------------------------------------


def test_a_clean_exit_cancels_the_sl_tp_even_over_a_live_position():
    assert classify_shutdown(_flags()) == ShutdownVerdict(
        unclean_note=None, keep_protective=False, kept_on_unknown_safe_mode=False
    )


@pytest.mark.parametrize(
    ("overrides", "note"),
    [
        ({"verdict_passed": False}, "the startup verdict did not pass"),
        (
            {"loop_raised": True, "loop_refused": True},
            "the engine could not be built (see the error above)",
        ),
        ({"loop_raised": True}, "the live loop raised instead of returning"),
        ({"protection_only": True}, "the loop ran in protection-only mode"),
        (
            {"safe_mode_active": True, "safe_mode_unknown": True},
            "the exit-time safe-mode state could NOT be read (unknown ≠ clean)",
        ),
        ({"safe_mode_active": True}, "safe mode is active at exit"),
    ],
)
def test_each_unclean_cause_keeps_the_sl_tp_over_a_live_position_and_names_itself(overrides, note):
    verdict = classify_shutdown(_flags(**overrides))
    assert verdict.unclean_note == note
    assert verdict.keep_protective is True


def test_the_first_cause_in_order_names_an_exit_with_several():
    everything = _flags(
        verdict_passed=False,
        loop_raised=True,
        loop_refused=True,
        protection_only=True,
        safe_mode_active=True,
        safe_mode_unknown=True,
    )
    assert classify_shutdown(everything).unclean_note == "the startup verdict did not pass"
    assert classify_shutdown(_flags(loop_raised=True, protection_only=True)).unclean_note == (
        "the live loop raised instead of returning"
    )


def test_loop_refused_names_a_raise_but_is_no_cause_of_its_own():
    assert classify_shutdown(_flags(loop_refused=True)).unclean_note is None


@pytest.mark.parametrize(
    ("positions", "keep"),
    [((), False), (None, True), (_LIVE, True)],
    ids=["flat", "unreadable", "live"],
)
def test_an_unclean_exit_keeps_the_sl_tp_unless_the_book_is_read_flat(positions, keep):
    verdict = classify_shutdown(_flags(verdict_passed=False, positions=positions))
    assert verdict.unclean_note == "the startup verdict did not pass"
    assert verdict.keep_protective is keep


@pytest.mark.parametrize(("positions", "kept"), [((), False), (_LIVE, True)], ids=["flat", "live"])
def test_kept_on_unknown_safe_mode_only_when_the_failed_read_actually_kept(positions, kept):
    verdict = classify_shutdown(
        _flags(safe_mode_active=True, safe_mode_unknown=True, positions=positions)
    )
    assert verdict.kept_on_unknown_safe_mode is kept


# --- classify_exit ----------------------------------------------------------


def _exit(**overrides) -> ExitReason:
    """A passing --loop run that stopped clean; each test overrides what it needs."""
    fields = {
        "verdict_passed": True,
        "sweep_unclean": False,
        "loop": True,
        "protection_only_settled": None,
        "safe_mode_latched": False,
        "kept_on_unknown_safe_mode": False,
    }
    fields.update(overrides)
    return classify_exit(**fields)


@pytest.mark.parametrize(
    ("overrides", "reason", "code"),
    [
        ({}, ExitReason.LOOP_CLEAN, 0),
        ({"loop": False}, ExitReason.ONE_SHOT_PASSED, 0),
        ({"verdict_passed": False}, ExitReason.VERDICT_FAILED, 4),
        ({"sweep_unclean": True}, ExitReason.SWEEP_UNCLEAN, 4),
        ({"protection_only_settled": True}, ExitReason.PROTECTION_ONLY_SETTLED, 1),
        ({"protection_only_settled": False}, ExitReason.PROTECTION_ONLY_STOPPED, 4),
        ({"safe_mode_latched": True}, ExitReason.LOOP_IN_SAFE_MODE, 4),
        ({"kept_on_unknown_safe_mode": True}, ExitReason.LOOP_KEPT_ON_UNKNOWN_SAFE_MODE, 4),
    ],
)
def test_each_exit_reason_and_its_code(overrides, reason, code):
    assert _exit(**overrides) is reason
    assert reason.code == code


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"verdict_passed": False, "sweep_unclean": True}, ExitReason.VERDICT_FAILED),
        ({"loop": False, "sweep_unclean": True}, ExitReason.SWEEP_UNCLEAN),
        (
            {"sweep_unclean": True, "protection_only_settled": True},
            ExitReason.SWEEP_UNCLEAN,
        ),
        (
            {"protection_only_settled": False, "safe_mode_latched": True},
            ExitReason.PROTECTION_ONLY_STOPPED,
        ),
        (
            {"safe_mode_latched": True, "kept_on_unknown_safe_mode": True},
            ExitReason.LOOP_IN_SAFE_MODE,
        ),
        # The one-shot's code follows its verdict and the sweep only, as it
        # did before this module existed; the --loop lane alone exits 4 here.
        (
            {"loop": False, "safe_mode_latched": True, "kept_on_unknown_safe_mode": True},
            ExitReason.ONE_SHOT_PASSED,
        ),
    ],
)
def test_the_first_exit_reason_in_order_wins(overrides, reason):
    assert _exit(**overrides) is reason


# --- read_exit_state / sweep_on_exit ----------------------------------------


class _SafeMode:
    def __init__(self, *, active=False, raises=False):
        self._active = active
        self._raises = raises

    @property
    def active(self):
        if self._raises:
            raise RuntimeError("database is locked")
        return self._active


class _Switch:
    def __init__(self, *, armed=True, stays_armed=False, raises=False):
        self.armed = armed
        self.stop_new_orders = False
        self._stays_armed = stays_armed
        self._raises = raises
        self.shutdowns: list[bool] = []

    def shutdown(self, *, keep_protective):
        self.shutdowns.append(keep_protective)
        if self._raises:
            raise RuntimeError("boom")
        if not self._stays_armed:
            self.armed = False


class _Session:
    def __init__(self, *, clearinghouse=None, safe_mode=None, kill_switch=None):
        self.events: list = []
        self._clearinghouse = clearinghouse
        self.safe_mode = safe_mode or _SafeMode()
        self.kill_switch = kill_switch or _Switch()
        self.identity = object()
        self.reconciler = self

    def reconcile_and_apply(self, trigger, **kwargs):
        self.events.append(("reconcile", trigger, kwargs))
        if self._clearinghouse is None:
            raise RuntimeError("reconcile down")

    def fetch_clearinghouse(self):
        self.events.append(("fetch",))
        if self._clearinghouse is None:
            raise RuntimeError("clearinghouse down")
        return self._clearinghouse


def test_read_exit_state_reads_position_and_safe_mode_and_reconciles_only_on_loop(
    clearinghouse_state,
):
    one_shot = _Session(clearinghouse=clearinghouse_state, safe_mode=_SafeMode(active=True))
    state = read_exit_state(one_shot, reconcile_first=False)
    assert one_shot.events == [("fetch",)]
    assert [p.coin for p in state.positions] == ["BTC"]
    assert (state.safe_mode_active, state.safe_mode_unknown) == (True, False)

    looped = _Session(clearinghouse=clearinghouse_state)
    read_exit_state(looped, reconcile_first=True)
    assert looped.events == [
        (
            "reconcile",
            "shutdown",
            {"safe_mode": looped.safe_mode, "ws_restored": True, "kill_switch_active": True},
        ),
        ("fetch",),
    ]


def test_read_exit_state_survives_every_failed_read_and_fails_toward_keeping(caplog):
    session = _Session(clearinghouse=None, safe_mode=_SafeMode(raises=True))
    with caplog.at_level(logging.ERROR, logger=shutdown_mod.__name__):
        state = read_exit_state(session, reconcile_first=True)
    assert state == ExitState(positions=None, safe_mode_active=True, safe_mode_unknown=True)
    assert [r.getMessage() for r in caplog.records] == [
        "§12.2 pre-shutdown reconciliation failed (sweep proceeds)",
        "shutdown position re-read failed",
        "shutdown safe-mode read failed",
    ]


def test_sweep_on_exit_does_nothing_over_an_unarmed_switch(monkeypatch):
    escalations: list = []
    monkeypatch.setattr(
        shutdown_mod, "escalate_identity_fault", lambda *a, **k: escalations.append(k)
    )
    switch = _Switch(armed=False)
    assert sweep_on_exit(_Session(kill_switch=switch), keep_protective=True) is None
    assert switch.shutdowns == []
    assert escalations == []


@pytest.mark.parametrize(
    ("switch_kwargs", "problem"),
    [
        ({}, None),
        ({"stays_armed": True}, _ARMED_LEFT),
        ({"raises": True, "stays_armed": True}, "shutdown sweep raised: boom"),
    ],
    ids=["clean", "left-armed", "raised"],
)
def test_sweep_on_exit_reports_what_the_sweep_left_behind(monkeypatch, switch_kwargs, problem):
    holders: list = []
    monkeypatch.setattr(
        shutdown_mod,
        "escalate_identity_fault",
        lambda identity, safe_mode, *, holder: holders.append(holder) or False,
    )
    switch = _Switch(**switch_kwargs)
    assert sweep_on_exit(_Session(kill_switch=switch), keep_protective=True) == problem
    assert switch.shutdowns == [True]
    assert holders == [EscalationHolder.SHUTDOWN]


def test_a_latched_identity_fault_leads_the_problem_and_keeps_the_sweeps(monkeypatch):
    monkeypatch.setattr(shutdown_mod, "escalate_identity_fault", lambda *a, **k: True)
    clean = _Session(kill_switch=_Switch())
    assert sweep_on_exit(clean, keep_protective=False) == _IDENTITY
    armed = _Session(kill_switch=_Switch(stays_armed=True))
    assert sweep_on_exit(armed, keep_protective=False) == f"{_IDENTITY}; also: {_ARMED_LEFT}"


def test_a_failed_escalation_write_keeps_the_sweeps_problem(monkeypatch, caplog):
    def _busy(*a, **k):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(shutdown_mod, "escalate_identity_fault", _busy)
    session = _Session(kill_switch=_Switch(stays_armed=True))
    with caplog.at_level(logging.ERROR, logger=shutdown_mod.__name__):
        assert sweep_on_exit(session, keep_protective=False) == _ARMED_LEFT
    assert caplog.records[-1].getMessage() == (
        "could not persist the venue-identity escalation at shutdown"
    )
