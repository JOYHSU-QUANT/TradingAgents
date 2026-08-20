"""Config-drift detection against a run’s genesis record.

Shared by the three entry points that resume/target an existing run (paper
resume, live resume, live-smoke), so what counts as run IDENTITY (hard exit 1)
versus parameter drift (a warning) can never diverge between them.
"""

from __future__ import annotations

import json

# Every behaviour-defining block the resume drift check compares — the single
# source shared by the genesis record and the comparison loop, so a new key
# can't be recorded but silently never drift-checked (or vice versa).
_DRIFT_COMPARED_KEYS = ("risk", "decision", "paper_trading", "engine", "market_data", "indicators")


def _run_config_subset(config: dict, coin: str) -> dict:
    """The behaviour-defining blocks recorded at run genesis (never network/wallet).

    ``engine`` (model/analysts), ``market_data`` (candle window feeding the
    context), and ``indicators`` (signal set + warm-up gate) are here because
    each redefines every subsequent decision at least as much as a
    risk-parameter tweak does; ``paper_trading`` is stored whole for the audit
    record even though only its ``execution`` sub-block is compared on resume
    (``account`` is genesis-only).
    """
    subset: dict = {key: config.get(key) for key in _DRIFT_COMPARED_KEYS}
    subset["coin"] = coin
    return subset


# Genesis records written before these keys joined the subset lack them;
# absence there means "unknown", not "was empty" — skip the comparison rather
# than false-flag every pre-upgrade run whose config carries the block today.
_DRIFT_KEYS_ADDED_LATER = frozenset({"engine", "market_data", "indicators"})


def _resume_effective(key: str, block: object) -> object:
    """Project a stored/current config block onto what actually applies on resume.

    ``paper_trading.account`` (initial balance, seed positions) is consumed
    only at run genesis — a resume-time edit there changes nothing, so it must
    not trip the "behaviour changes from here on" warning. Everything else
    applies as-is.
    """
    if key == "paper_trading" and isinstance(block, dict):
        return block.get("execution")
    return block


def _norm_network(live_block: dict) -> object:
    """``live.network`` normalised the way LiveConfig reads it (case-insensitive)."""
    net = live_block.get("network")
    return net.strip().lower() if isinstance(net, str) else net


# The drift kinds that are run IDENTITY (a different instrument or a different
# exchange). Every entry point that resumes/targets an existing run refuses
# these with exit 1; any other kind is parameter drift and only warns. One
# shared datum so the three call sites (paper resume, live resume, live-smoke)
# can never diverge on what counts as hard.
_HARD_DRIFT_KINDS = frozenset({"coin", "network"})


def _config_drift_report(
    stored_json: str | None, config: dict, coin: str
) -> tuple[str, str] | None:
    """Compare today's config against the run's genesis record.

    Returns ``("coin", msg)`` for a coin mismatch or ``("network", msg)`` for a
    ``live.network`` mismatch (both hard errors — a different instrument or a
    different exchange is a different run, not a resumption), ``("params",
    msg)`` for risk/decision/paper_trading(execution)/engine/market_data/
    indicators / non-network ``live:`` drift (warning — behaviour changes
    mid-run but the operator may intend it; genesis-only
    ``paper_trading.account`` edits are inert on resume and don't warn, and a
    genesis record predating a key in ``_DRIFT_KEYS_ADDED_LATER`` skips that
    comparison rather than false-flagging), or ``None`` when nothing drifted or
    no record exists (a pre-drift-check store). A genesis record this process
    cannot parse also reports as ``("params", ...)``: the homogeneity check
    became impossible, which is breadcrumb-grade — never a startup abort (that
    would fire before the protection-only fork, leaving a live position
    unwatched). The ``live:`` checks fire only for records that stored the
    block (a live run's genesis — a paper run never stores it), so paper
    resumes reach neither the network hard-fail nor the live-block warning.
    """
    if not stored_json:
        return None
    try:
        stored = json.loads(stored_json)
    except ValueError as exc:
        return ("params", f"could not verify config drift (corrupt stored config_json: {exc})")
    if not isinstance(stored, dict):
        return (
            "params",
            "could not verify config drift (corrupt stored config_json: not an object)",
        )
    # Round-trip today's subset through JSON so both sides compare in the same
    # serialized shape (default=str stringifies any non-JSON scalar).
    current = json.loads(json.dumps(_run_config_subset(config, coin), default=str))
    if stored.get("coin") != current.get("coin"):
        return (
            "coin",
            f"run was created for coin {stored.get('coin')!r} but this resume "
            f"targets {coin!r} — refusing to continue a run on a different "
            "instrument (use a new --run-id).",
        )
    # live.network is run IDENTITY, like coin (decided 2026-07-17): a
    # testnet↔mainnet swap on resume would arm the wallet-wide kill switch and
    # reconcile the WRONG exchange against this ledger — every position/equity
    # leg mismatches and the operator sees "reconciliation mismatch" instead of
    # the true cause. The live create path stores the whole ``live:`` block; a
    # paper run never does, so ``stored_live`` is absent there and the check
    # is skipped. current_live is round-tripped through JSON to match the
    # stored block's serialized shape.
    stored_live = stored.get("live")
    raw_current_live = config.get("live")
    current_live = (
        json.loads(json.dumps(raw_current_live, default=str))
        if isinstance(raw_current_live, dict)
        else None
    )
    both_have_live_block = isinstance(stored_live, dict) and isinstance(current_live, dict)
    if both_have_live_block and _norm_network(stored_live) != _norm_network(current_live):
        return (
            "network",
            f"run was created against live.network {_norm_network(stored_live)!r} "
            f"but this resume targets {_norm_network(current_live)!r} — refusing to "
            "arm the kill switch and reconcile a different exchange (use a new "
            "--run-id).",
        )
    drifted = sorted(
        key
        for key in _DRIFT_COMPARED_KEYS
        if not (key in _DRIFT_KEYS_ADDED_LATER and key not in stored)
        and _resume_effective(key, stored.get(key)) != _resume_effective(key, current.get(key))
    )
    # Non-network ``live:`` drift is a warning (network already hard-failed
    # above): safety caps, kill-switch timings, allow_real_orders wiring etc.
    # redefine behaviour mid-run and the operator should be told, even though
    # they may intend it. Compared with network excluded so an equal-network
    # block that changed elsewhere still surfaces.
    if both_have_live_block:
        stored_live_rest = {k: v for k, v in stored_live.items() if k != "network"}
        current_live_rest = {k: v for k, v in current_live.items() if k != "network"}
        if stored_live_rest != current_live_rest:
            drifted = sorted([*drifted, "live"])
    if drifted:
        return (
            "params",
            f"config drift on resume: {', '.join(drifted)} differ from the values "
            "recorded at run creation — this run's behaviour changes from here on.",
        )
    return None
