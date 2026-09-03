"""Write each trade decision to a durable JSON audit record.

One file per decision under ``<results_dir>/perp_decisions/``: the Phase 2
structured-target record (:func:`build_target_log_record` /
:func:`log_target_decision`, ``TARGET_SCHEMA_VERSION``). Beyond the prompt
hash / models / timestamp header it persists the raw engine response, the
parse verdict (``is_valid`` / ``invalid_reason``), the full RiskGate outcome,
and the decision-time sizing inputs (``mark_price`` / ``account_equity``) —
the fields a post-mortem of the structured-target contract needs.

Records from the retired Phase 1 rating pipeline (``schema_version: 2``) may
still exist on disk; old JSON needs no writer to stay readable, so that write
path was deleted rather than maintained alongside the live format.

``build_target_log_record`` is pure (timestamp injected) so it is unit-tested
without touching the filesystem; :func:`write_decision_log` does the I/O.
"""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.atomic_io import atomic_write_text
from ..common.instants import epoch_ms

if TYPE_CHECKING:
    # Type-only imports (annotations are strings under ``from __future__``) so the
    # audit layer documents the value shapes without a runtime dependency on the
    # domain decision modules.
    from decimal import Decimal

    from ..domains.perp.risk_gate import RiskGateResult
    from ..domains.perp.target_decision import ParsedDecision

logger = logging.getLogger(__name__)

# Phase 2 structured-target records carry their own version so a reader can
# tell them apart from retired Phase 1 records (schema_version 2) still on
# disk without sniffing fields.
TARGET_SCHEMA_VERSION = 3


def prompt_hash(prompt: str) -> str:
    """``sha256:<hex>`` of the prompt text the caller hands in.

    ``main.py`` passes the full adapter-injected text — the rendered market
    context *and* the output-format contract (which embeds the live margin
    grid and effective ceiling) — so records produced under a different grid
    or ceiling never share a hash. Since prompt v5 the gate thresholds are
    NOT in that text, so a ``decision:`` threshold edit alone leaves the hash
    unchanged (segment those by run-id, per the RUNBOOK). The engine's own
    base instrument context is engine-internal and not captured.
    """
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _record_header(
    *,
    schema_version: int,
    coin: str,
    prompt: str,
    models: dict[str, str],
    timestamp: datetime,
) -> dict[str, Any]:
    """The fields every audit record format shares (pure — no clock, no I/O).

    A naive timestamp is refused here, once for both formats, by ``epoch_ms``
    naming the record. The defect that refusal exists for: read through a
    float ``.timestamp()``, a naive datetime is interpreted in the host's
    local zone, so ``timestamp_ms`` would be silently off by the UTC offset
    (e.g. 8h on a UTC+8 box) while the ISO ``timestamp`` string looks fine — a
    corrupt audit record that no test on a UTC machine would catch.
    """
    return {
        "schema_version": schema_version,
        "coin": coin,
        "timestamp": timestamp.isoformat(),
        "timestamp_ms": epoch_ms(timestamp, what="audit record timestamp"),
        "prompt_hash": prompt_hash(prompt),
        "models": models,
    }


def _filename(coin: str, timestamp: datetime) -> str:
    """Sortable, filesystem-safe name: ``BTC_20260627_174500_000.json``.

    The millisecond suffix keeps two decisions for the same coin in the same
    second from silently overwriting each other.
    """
    stamp = timestamp.strftime("%Y%m%d_%H%M%S") + f"_{timestamp.microsecond // 1000:03d}"
    safe_coin = "".join(c for c in coin if c.isalnum()) or "UNKNOWN"
    return f"{safe_coin}_{stamp}.json"


def _parse_timestamp(ts_raw: str | None) -> datetime:
    """Parse the record's ISO timestamp for the filename stamp; robust to junk.

    ``datetime.fromisoformat`` only learned to accept a trailing ``Z`` in 3.11, and
    the ``timestamp`` field may carry a value built elsewhere — so normalise a ``Z``
    suffix and fall back to now(UTC) on anything unparseable. This affects **only**
    the filename stamp; the record's own ``timestamp`` field is written verbatim, so
    a degraded parse never corrupts the audit content, just its filename.
    """
    if not ts_raw:
        return datetime.now(timezone.utc)
    normalized = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
    try:
        return datetime.fromisoformat(normalized)
    except (ValueError, TypeError):
        logger.warning(
            "could not parse record timestamp %r — using now() for the filename stamp", ts_raw
        )
        return datetime.now(timezone.utc)


def _claim_unique_path(directory: Path, coin: str, timestamp: datetime) -> Path:
    """Atomically claim a not-yet-existing path for this coin/timestamp.

    The millisecond-stamped name from :func:`_filename` normally suffices, but two
    decisions for the same coin in the same millisecond (a retry loop, a batch run on a
    low-resolution clock, or a second *process*) would collide. A plain
    ``exists()``-then-write is a TOCTOU race: between the check and the atomic rename in
    :func:`write_decision_log` a concurrent writer can create the same path, and the
    rename then silently overwrites it, destroying an audit record. Instead, claim each
    candidate with ``O_CREAT | O_EXCL`` (an empty placeholder) so only one writer can ever
    own a given name; on collision try ``_1``/``_2``/... Warn when a counter suffix was
    needed so the (unexpected) collision stays visible.
    """
    base = _filename(coin, timestamp)
    stem, suffix = Path(base).stem, Path(base).suffix
    names = itertools.chain([base], (f"{stem}_{n}{suffix}" for n in itertools.count(1)))
    for i, name in enumerate(names):
        candidate = directory / name
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        if i:
            logger.warning(
                "audit filename %s already exists — claimed %s instead to avoid "
                "overwriting an earlier decision record",
                base,
                name,
            )
        return candidate
    raise AssertionError("unreachable: itertools.count is infinite")  # pragma: no cover


def write_decision_log(
    record: dict[str, Any],
    results_dir: str | Path,
    *,
    coin: str | None = None,
    timestamp: datetime | None = None,
) -> Path:
    """Write ``record`` as pretty JSON under ``<results_dir>/perp_decisions/``.

    ``coin``/``timestamp`` default to the values inside ``record`` so callers can
    just pass the record. Returns the path written.
    """
    coin = coin or record.get("coin", "UNKNOWN")
    if timestamp is None:
        timestamp = _parse_timestamp(record.get("timestamp"))

    directory = Path(results_dir) / "perp_decisions"
    directory.mkdir(parents=True, exist_ok=True)
    # Atomically claim the final path (an empty placeholder) so a concurrent writer can
    # never overwrite this record — see _claim_unique_path.
    path = _claim_unique_path(directory, coin, timestamp)
    # atomic_write_text owns the tmp-then-replace dance (and tmp cleanup); the
    # site-specific delta is the empty placeholder claimed above — on failure it
    # must go too, so the audit directory never holds a zero-length record.
    try:
        atomic_write_text(path, lambda fh: json.dump(record, fh, indent=2, ensure_ascii=False))
    except BaseException:
        # Best-effort cleanup: a secondary unlink failure (e.g. a Windows file
        # lock, or a read-only filesystem) must not replace the original write
        # error — that is the one the caller needs to see.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        raise
    return path


# --------------------------------------------------------------------------
# Phase 2 — structured target contract records
# --------------------------------------------------------------------------


def build_target_log_record(
    *,
    coin: str,
    parsed: ParsedDecision,
    risk_result: RiskGateResult,
    mark_price: Decimal,
    account_equity: Decimal,
    prompt: str,
    models: dict[str, str],
    timestamp: datetime,
) -> dict[str, Any]:
    """Assemble the Phase 2 audit record (pure — no clock, no I/O).

    Preserves the engine's **raw response verbatim** (DESIGN Part 2: an invalid
    output is recorded, never reconstructed) next to the parse verdict and the
    RiskGate outcome, so a post-mortem can replay exactly what the model said
    and what the deterministic layer did with it.

    ``mark_price`` / ``account_equity`` are the decision-time sizing inputs the
    gate worked from, captured so every row — including REJECTED /
    maintain_current, whose ``target_*`` fields are all ``None`` — stays
    reproducible as a coin quantity after the fact (execution §6.2).
    """
    record = _record_header(
        schema_version=TARGET_SCHEMA_VERSION,
        coin=coin,
        prompt=prompt,
        models=models,
        timestamp=timestamp,
    )
    record.update(
        {
            "raw_response": parsed.raw_response,
            "parse": {
                "is_valid": parsed.is_valid,
                "invalid_reason": parsed.invalid_reason,
            },
            "decision": parsed.decision.to_dict(),
            "risk": risk_result.to_dict(),
            # Decimals become strings, matching RiskGateResult.to_dict.
            "mark_price": str(mark_price),
            "account_equity": str(account_equity),
        }
    )
    return record


def log_target_decision(
    *,
    coin: str,
    parsed: ParsedDecision,
    risk_result: RiskGateResult,
    mark_price: Decimal,
    account_equity: Decimal,
    prompt: str,
    models: dict[str, str],
    results_dir: str | Path,
    timestamp: datetime | None = None,
) -> tuple[dict[str, Any], Path]:
    """Build the Phase 2 record and write it; returns ``(record, path)``.

    Convenience wrapper for :func:`build_target_log_record` +
    :func:`write_decision_log` used by ``main.py``. ``timestamp`` defaults to
    now (UTC). Files land in the same ``perp_decisions/`` directory as Phase 1
    records; ``schema_version`` distinguishes the two formats.
    """
    timestamp = timestamp or datetime.now(timezone.utc)
    record = build_target_log_record(
        coin=coin,
        parsed=parsed,
        risk_result=risk_result,
        mark_price=mark_price,
        account_equity=account_equity,
        prompt=prompt,
        models=models,
        timestamp=timestamp,
    )
    path = write_decision_log(record, results_dir, coin=coin, timestamp=timestamp)
    return record, path
