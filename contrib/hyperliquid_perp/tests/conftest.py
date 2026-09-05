"""Shared pytest helpers for the Hyperliquid perp tests."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from contrib.hyperliquid_perp.common.instants import parse_instant
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id as derive_attempt_id

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_dotenv(monkeypatch):
    """Keep the whole suite immune to a developer's real repo-root ``.env``.

    Both CLI entry points call ``load_dotenv_files()`` as their first act; under
    pytest that would inject every variable of a real ``.env`` into the process,
    and ``load_dotenv``'s ``os.environ`` writes happen outside monkeypatch's
    snapshot, so they outlive the test that triggered them. Neutralize the
    entry-point bindings suite-wide; the loader itself stays testable through
    ``config.load_dotenv_files`` (untouched), and the entry-point ordering tests
    opt back in by re-binding the real function.
    """
    from contrib.hyperliquid_perp import cli, main

    monkeypatch.setattr(cli, "load_dotenv_files", lambda: None)
    monkeypatch.setattr(main, "load_dotenv_files", lambda: None)


def _load(name: str):
    with (_FIXTURES / name).open(encoding="utf-8") as fh:
        return json.load(fh)


def doc_text(name: str) -> str:
    """Read one of the package's docs, for the tests that pin a doc to a constant.

    The plumbing lives here because the pins themselves do not: each sits beside
    the constant it guards, in whichever module reaches that subsystem. Without
    a shared reader every pin re-derives a ``parents`` index that differs with
    its own directory depth, and re-explains it — the shape of duplication those
    pins exist to close. Anchored on conftest the depth is fixed, and stated
    once.

    Never cwd-relative: a cwd-relative path breaks the moment pytest is invoked
    from anywhere but the repo root (2026-08-01 round-17 probe).
    """
    return (Path(__file__).parent.parent / "docs" / name).read_text(encoding="utf-8")


def config_text() -> str:
    """Read ``configs/hyperliquid.example.yaml``, for the pins on its comments.

    The example config is what an operator copies into ``hyperliquid.local.yaml``
    — closer to real behaviour than any doc — and its comments restate the
    loader's hard limits as bare literals. Same plumbing rationale as
    :func:`doc_text`; the pins sit beside the constants they guard (issue #102).
    """
    from contrib.hyperliquid_perp.config import _EXAMPLE

    return _EXAMPLE.read_text(encoding="utf-8")


def package_sources(pkg) -> list[Path]:
    """Every ``.py`` under the regular package ``pkg``, subpackages included, in path order.

    The ONE enumeration behind the tests that read a package's source rather
    than import it (the ``common/`` bottom-of-graph check, the machine-
    disposition vocabulary scan). Each used to spell its own walk, so whether
    a scan reached into subpackages, and in what order it reported, was
    decided per test and could drift apart silently (issue #151). Regular
    packages only: ``__path__[0]`` is the one directory a package with an
    ``__init__.py`` has.
    """
    return sorted(Path(pkg.__path__[0]).rglob("*.py"))


@contextmanager
def migrations_up_to(version: int):
    """Inside the block, ``persistence.db`` knows only the migrations up to ``version``.

    The one staging behind every "an OLDER build made this store" test: a
    store built inside the block is a GENUINELY old one, made by the
    migrations that existed then (deleting a bookkeeping row from a current
    store is not the same thing — its DDL has already run, so re-applying it
    collides). Scoped, so the real ``MIGRATIONS`` is back the moment the
    block ends — the caller reopens the same store under the current build
    in the same test. Each behind-store test used to spell this dance itself
    (issue #147 review). ``MonkeyPatch.context()`` is self-contained, so no
    fixture needs threading through.
    """
    import contrib.hyperliquid_perp.persistence.db as db_module
    from contrib.hyperliquid_perp.persistence.schema import MIGRATIONS

    older = {v: MIGRATIONS[v] for v in sorted(MIGRATIONS) if v <= version}
    with pytest.MonkeyPatch.context() as scoped:
        scoped.setattr(db_module, "MIGRATIONS", older)
        yield


def build_store_at(path, version: int, populate=None) -> None:
    """Build ``path`` at exactly schema ``version`` and close it.

    ``populate`` gets the open ``Database`` to seed rows with, under that old
    schema. See :func:`migrations_up_to` for what "at version N" means.
    """
    from contrib.hyperliquid_perp.persistence.db import Database

    with migrations_up_to(version), Database(path) as db:
        if populate is not None:
            populate(db)


@pytest.fixture
def meta_and_asset_ctxs():
    return _load("meta_and_asset_ctxs.json")


@pytest.fixture
def candle_snapshot():
    return _load("candle_snapshot.json")


@pytest.fixture
def funding_history():
    return _load("funding_history.json")


@pytest.fixture
def clearinghouse_state():
    return _load("clearinghouse_state.json")


def record_reconciliation_sweep_wiring(monkeypatch):
    """Record what a recovery site builds its §18.2 sweep components with.

    Both production sites — ``_live_startup_recovery`` (the daemon) and
    ``_smoke_startup_recovery`` — build a ``KillSwitchManager``, a
    ``FillBackfiller`` and a ``LiveReconciler`` over one refresh closure, and
    the kwargs that arm the refresh are optional on both components. The pins
    live in two modules because only one command reaches each site; the
    plumbing is identical, so it lives here together with the shared
    assertions. What stays with the tests is what each site does NOT share: the
    recovery count (the daemon's one against the smoke suite's four — a
    pre-flight plus restart tests 15–17) and the daemon's own no-REST guard.

    Returns a namespace of ``switches`` / ``refreshes`` / ``backfillers`` /
    ``reconcilers``, in construction order.
    """
    from contrib.hyperliquid_perp.live import (
        fill_backfill as fb_mod,
        kill_switch as ks_mod,
        reconcile as rec_mod,
    )

    record = SimpleNamespace(switches=[], refreshes=[], backfillers=[], reconcilers=[])
    real_switch = ks_mod.KillSwitchManager
    real_refresh = ks_mod.refresh_across_blocking_work

    class _RecordingSwitch(real_switch):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs):
            # Record AFTER the real constructor accepts them. Recording
            # first made every pin blind to the failure mode they exist for: a
            # call site that drifts from the signature (an extra or renamed
            # kwarg) dies on EVERY start-up, but the recorder had already banked
            # its evidence — the instance here, the kwargs in ``_record_kwargs``
            # — so the drive's own suppression swallowed the TypeError and the
            # assertions still passed (2026-08-19 mutation probe).
            super().__init__(**kwargs)
            record.switches.append(self)

    def _recording_refresh(switch, *, what):
        # Delegate rather than replace. The seam is narrower than it looks —
        # ``live/engine.py`` and ``live/protection.py`` bind this name at MODULE
        # level and never see the patch, and ``live/smoke.py`` does not use it at
        # all — so what actually flows through here is cli.py's function-local
        # callers, which in the smoke drive means the recovery's OWN in-sweep
        # refreshes (the daemon drive dies at the arm, before any sweep runs).
        # Swallowing those would change what the drive proves.
        real_refresh(switch, what=what)
        # Recorded after the delegate, for the reason ``_RecordingSwitch`` above
        # argues at length. (The helper never raises, so nothing is lost.)
        record.refreshes.append((switch, what))

    def _record_kwargs(module, name, sink):
        real = getattr(module, name)

        class _Recording(real):  # type: ignore[misc, valid-type]
            def __init__(self, **kwargs):
                super().__init__(**kwargs)  # order matters — see _RecordingSwitch
                sink.append(kwargs)

        monkeypatch.setattr(module, name, _Recording)

    # cli.py imports all of these inside the command functions, so the seam is
    # the SOURCE module — the closures the CLI builds pick the patched ones up.
    monkeypatch.setattr(ks_mod, "KillSwitchManager", _RecordingSwitch)
    monkeypatch.setattr(ks_mod, "refresh_across_blocking_work", _recording_refresh)
    _record_kwargs(fb_mod, "FillBackfiller", record.backfillers)
    _record_kwargs(rec_mod, "LiveReconciler", record.reconcilers)
    return record


def _assert_sweep_refreshes(record, switch, kwargs, *, label):
    """The recorded hook must route THAT switch to the §18.2 refresh helper.

    Routing, not a completed refresh, and for a different reason on each side.
    The daemon pin's switch never armed — its drive dies in ``arm()`` — so a
    refresh IS due and ``refresh()`` refuses outright. The smoke pin's four are
    armed and live, and what keeps the post-drive call off the wire is only that
    ``refresh_due()`` is false so soon after the suite's own refresh; that is
    wall-clock, not structure. Either way the helper swallows the outcome (a
    refresh miss must never abort the work it protects), so identity is the fact
    left to assert — and it is what ``lambda: None`` and a hook closed over the
    WRONG switch both fail.

    ``what=`` is deliberately not asserted: it feeds one ``logger.warning`` and
    nothing else, so pinning it would turn a pure wording change into a red
    suite.
    """
    hook = kwargs.get("refresh_kill_switch")
    assert hook is not None, f"the {label} sweep refreshes nothing (§18.2)"
    record.refreshes.clear()
    hook()
    assert [s for s, _what in record.refreshes] == [switch], (label, record.refreshes)


def assert_paired_sweep_refreshes(record, *, owner):
    """Every recovery the drive ran armed BOTH of its sweep components.

    Paired per recovery rather than checked once: a hook that is right on the
    first construction and dropped on a later rebuild has nowhere to hide.
    """
    counts = (len(record.switches), len(record.backfillers), len(record.reconcilers))
    assert len(set(counts)) == 1, counts
    for switch, backfiller, reconciler in zip(
        record.switches, record.backfillers, record.reconcilers, strict=True
    ):
        _assert_sweep_refreshes(record, switch, backfiller, label=f"{owner}'s backfiller")
        _assert_sweep_refreshes(record, switch, reconciler, label=f"{owner}'s reconciler")


def assert_payload_dir(reconciler_kwargs, db_path, *, run_id):
    """The reconciler was given somewhere to write its raw payload evidence.

    Segment by segment rather than one path comparison — the same claim either
    way, but a failure names which part of ``<db parent>/payloads/<run id>``
    moved. Every segment IS checked: dropping the middle one left a rename of
    the production literal invisible to both payload-dir pins (2026-08-19
    mutation probe). The recipe is hand-copied at four cli.py sites and these
    pins drive two of them; the other two are nobody's here.
    """
    payload_dir = reconciler_kwargs.get("payload_dir")
    assert payload_dir is not None, "the reconciler writes no clearinghouse evidence"
    assert payload_dir.name == run_id, payload_dir
    assert payload_dir.parent.name == "payloads", payload_dir
    assert payload_dir.parent.parent == db_path.resolve().parent, payload_dir


def synthetic_bar(**over):
    """A minimal well-formed candleSnapshot bar: BTC/1m, t/T in the past."""
    base = {
        "t": 1000,
        "T": 1999,
        "s": "BTC",
        "i": "1m",
        "o": "1",
        "h": "2",
        "l": "1",
        "c": "1",
        "v": "1",
    }
    base.update(over)
    return base


# A cloid no test registers as its own: the identity a MISROUTING venue answers
# with (see misrouted_order_status), distinct from every "ours" constant the
# consumers' tests mint.
_STRANGER_CLOID = "0x" + "ee" * 16


def misrouted_order_status(oid: str = "4242") -> dict:
    """An orderStatus answer about SOMEONE ELSE's order — the venue misrouting.

    Carries its own ``cloid``, so :func:`echo_order_status_cloid` leaves it
    alone and ``parse_order_status`` raises rather than handing back a
    stranger's status. The one shape every §13.5 venue-identity test scripts
    (issue #132: it used to live once per test module).
    """
    return {
        "status": "order",
        "order": {"order": {"oid": oid, "cloid": _STRANGER_CLOID}, "status": "open"},
    }


def identity_latch_rows(db, *, run_id: str) -> list:
    """The ``identity_fault_latched`` rows a run wrote (the §13.5 latch's audit trail).

    ``run_id`` is required: a default would let a test on some other run
    assert ``== []`` against rows it never looked at.
    """
    return [
        e
        for e in repo.iter_protection_order_events(db.conn, run_id)
        if e["event_type"] == "identity_fault_latched"
    ]


def echo_order_status_cloid(payload, cloid_hex):
    """Echo the queried cloid into a canned orderStatus payload, like the venue.

    ``parse_order_status`` requires the inner order to echo the cloid it was
    queried by (identity check, 2026-08-17). The real Info endpoint always
    echoes it; the fakes' canned payloads predate the check and are often
    served against cloids the test never sees (a manager-derived hex), so each
    fake routes its answer through here to model the well-behaved venue. A
    payload that ALREADY carries a cloid is returned untouched — that is how a
    test scripts a mismatch. Copies rather than mutates: several canned
    payloads are shared module constants.
    """
    if (
        isinstance(payload, dict)
        and payload.get("status") == "order"
        and isinstance(payload.get("order"), dict)
        and isinstance(payload["order"].get("order"), dict)
        and "cloid" not in payload["order"]["order"]
    ):
        inner = {**payload["order"]["order"], "cloid": cloid_hex}
        return {**payload, "order": {**payload["order"], "order": inner}}
    return payload


def insert_decision_attempts(db, outcomes, *, run_id="r", start: datetime, mode="paper"):
    """Insert one decision_attempts row per outcome, on the 4h grid.

    Each outcome is a bare status string, or a ``(status, error_type)`` pair
    when the test needs the §6.2 class the row carries — the acceptance
    validators key on it (``common.no_decision.trailing_failure_streaks``), so
    tests about those verdicts have to be able to set it.
    """
    with db.transaction() as conn:
        for i, outcome in enumerate(outcomes):
            status, error_type = outcome if isinstance(outcome, tuple) else (outcome, None)
            scheduled = start + timedelta(hours=4 * i)
            repo.insert_decision_attempt(
                conn,
                decision_attempt_id=derive_attempt_id(run_id, scheduled),
                timestamp=scheduled,
                mode=mode,
                run_id=run_id,
                scheduled_at=scheduled,
                attempt_count=1,
                status=status,
                error_type=error_type,
            )


def arm_lock_fault(monkeypatch, target, name, *, shots=1):
    """Make ``target.name`` raise "database is locked" for its first ``shots`` hits.

    Returns the mutable state so a test can assert how many faults fired.
    Shared by the paper and live suites: both lanes' §3.1 response store is
    one repository writer (``repo.store_pending_response``, issue #181), so
    patching the NAME is what scopes the fault. Deliberately no predicate on
    the arguments: the paper-local ancestor needed one only because it
    patched the shared ``update_decision_attempt`` and had to tell the store
    apart from every other attempt write in the poll — splitting the writer
    is the fix, and a filter here would invite that pattern back.
    """
    real = getattr(target, name)
    state = {"fired": 0}

    def flaky(*args, **kwargs):
        if state["fired"] < shots:
            state["fired"] += 1
            raise sqlite3.OperationalError("database is locked")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, flaky)
    return state


def poison_stored_parse(monkeypatch, module):
    """Make ``module.parse_target_decision`` RAISE, counting the calls.

    The parse is fail-closed by contract (malformed content returns an
    invalid decision), so a raise stands for a parser bug or a corrupted
    store — the deterministic kind that recurs on every restart. Both lanes'
    resume guards (paper ``_resume_pending``, live ``resume_startup``) are
    pinned against this same poison.
    """
    calls = {"n": 0}

    def boom(raw, cfg):
        calls["n"] += 1
        raise ValueError("corrupt stored response")

    monkeypatch.setattr(module, "parse_target_decision", boom)
    return calls


def write_payload(path: Path, body) -> tuple[str, str]:
    """Write a payload file the way the daemon does; ``(path, digest)`` for its row.

    Bytes, through the daemon's own ``payload_digest``: a test that hashed
    with its own formula, or wrote in text mode, would pin a reader against
    rows the daemon never writes.
    """
    from contrib.hyperliquid_perp.common.digest import json_bytes, payload_digest

    raw = body if isinstance(body, bytes) else json_bytes(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return str(path), payload_digest(raw)


def fake_completion_message(
    *, finish_reason, output_tokens, model="fake-1", content="…", input_tokens=10
):
    """An ``AIMessage`` shaped like the one ``on_llm_end`` sees after langchain_core's
    metadata merge: stop reason and model in ``response_metadata``, counts in
    ``usage_metadata``. The test-side half of the provider-slot contract
    ``tradingagents.llm_clients.completion_metadata`` documents — one builder so
    the collector tests and the provider tests cannot drift onto different fakes.
    """
    from langchain_core.messages import AIMessage

    return AIMessage(
        content=content,
        response_metadata={"finish_reason": finish_reason, "model_name": model},
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
    )


def stamp_prompt_regimes(db, regimes, *, run_id="r", mode="paper", symbol="BTC"):
    """Give the run's attempts (in scheduled order) an ``ai_inputs`` row each.

    ``regimes`` holds one ``(prompt_version, context_shape,
    format_fingerprint)`` per attempt — or, for a test about the payload
    artifact, ``(…, input_payload_path, input_payload_hash)`` (see
    :func:`write_payload`) — or ``None`` for a cycle that failed before
    ``build_input`` and so never wrote an input row. ``input_id`` is stamped
    straight onto the attempt row: ``update_decision_attempt`` refuses
    terminal rows by design, and these are seeded terminal.
    """
    rows = db.conn.execute(
        "SELECT decision_attempt_id, scheduled_at FROM decision_attempts"
        " WHERE run_id = ? ORDER BY scheduled_at",
        (run_id,),
    ).fetchall()
    assert len(rows) == len(regimes), "one regime (or None) per attempt"
    with db.transaction() as conn:
        for row, regime in zip(rows, regimes, strict=True):
            if regime is None:
                continue
            version, shape, fingerprint, *artifact = regime
            path, digest = artifact if artifact else (None, None)
            input_id = f"{row['decision_attempt_id']}#in1"
            repo.insert_ai_input(
                conn,
                input_id=input_id,
                timestamp=parse_instant(row["scheduled_at"]),
                mode=mode,
                run_id=run_id,
                symbol=symbol,
                prompt_version=version,
                context_shape=shape,
                format_fingerprint=fingerprint,
                input_payload_path=path,
                input_payload_hash=digest,
            )
            conn.execute(
                "UPDATE decision_attempts SET input_id = ? WHERE decision_attempt_id = ?",
                (input_id, row["decision_attempt_id"]),
            )
