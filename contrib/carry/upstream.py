"""What this package borrows from its two neighbours — in one place, read-only.

The same funnel ``contrib.replay`` and ``contrib.autoresearch`` keep: every
module here imports from THIS module, ``BORROWED`` is the audit list, and
``tests/test_upstream.py`` parses the sources to hold both halves — that no
other module reaches out, and that no neighbour reaches back. A borrowed
name that vanishes upstream fails the pin test by name, not as an
ImportError deep in a command. ``__all__`` repeats the names as a literal on
purpose: ruff reads a literal ``__all__`` as a use of each import.

What is borrowed and why:

- from the perp package: ``FundingPoint`` (the settlement record the venue
  reader and the research store both speak), ``funding_zscore`` and its
  sample floor (so the number this package acts on is the number the perp
  prompt prints — one definition, not a copy), the instants (so a boundary
  is encoded by the same integer arithmetic the stores use, and a perp
  snapshot's timestamp is decoded by the decoder paired with its encoder), the
  atomic writer (a half-written handoff must never be readable), the SQLite
  URI spelling (``Path.as_uri`` is wrong for a UNC share and a relative
  path; the perp store opens through this one, so the carry read must too),
  and the venue error type (a fetch the venue refused is reported, not
  tracebacked); and, for the report, the file-target run's spellings
  (``FILE_TARGET_MODEL`` on ``ai_inputs.model`` says the run decided from the
  handoff and not from a model; the two rationale prefixes tell a cycle that
  maintained because the document was unusable from one that merely sat
  before the next boundary; the sidecar suffix names the file a cycle keeps
  its document in), the payload directory layout (the sidecars live beside
  the payloads, and that rule is the perp package's, not a copy), and the
  two vocabularies the report classifies cycles by (``AttemptStatus`` for
  how a cycle ended, ``RiskAction`` for what the gate did), so a member
  added upstream is not a word this package silently fails to know;
- from the research package: the research store and its funding walk
  (``backfill_funding`` pages the venue's capped endpoint forwards and
  loses nothing; ``StopReason`` says how it ended, and only a walk that
  reached its end is decided on; ``render_fetch`` is the one line the
  research package prints for the same walk), the venue reader's
  ``HistoryMarketData`` protocol (so the fetch seam is typed, not ``Any``),
  the day and the settlement cadence in milliseconds, and
  ``require_number`` — THE numeric guard that package keeps beside its
  error, so a threshold here is refused the way a threshold there is
  (bool, huge int, non-finite).

The venue reader is borrowed LAZILY: ``VENUE_BORROWED`` lists it and only
:func:`load_market` imports it, because it pulls in the Hyperliquid SDK and
the ``history`` command and every test with a fake venue must not pay for
that.
"""

from __future__ import annotations

from contrib.autoresearch.constants import FUNDING_INTERVAL_MS, MS_PER_DAY
from contrib.autoresearch.fetch import StopReason, backfill_funding, render_fetch
from contrib.autoresearch.ports import HistoryMarketData
from contrib.autoresearch.store import ResearchStore, StoreError
from contrib.autoresearch.vocabulary import SpecError, require_number
from contrib.core.atomic_io import atomic_write_text
from contrib.core.instants import epoch_ms, from_epoch_ms, parse_instant
from contrib.hyperliquid_perp.common.constants import (
    FILE_TARGET_MODEL,
    FILE_TARGET_PENDING_PREFIX,
    FILE_TARGET_UNUSABLE_PREFIX,
    HANDOFF_SIDECAR_SUFFIX,
)
from contrib.hyperliquid_perp.common.store_layout import payload_dir
from contrib.hyperliquid_perp.domains.perp.context_builder import (
    MIN_FUNDING_SAMPLES,
    funding_zscore,
)
from contrib.hyperliquid_perp.domains.perp.risk_gate import RiskAction
from contrib.hyperliquid_perp.domains.perp.schema import FundingPoint
from contrib.hyperliquid_perp.exchanges.hyperliquid.errors import ExchangeError
from contrib.hyperliquid_perp.persistence.repository import AttemptStatus
from contrib.hyperliquid_perp.persistence.store_identity import sqlite_file_uri

__all__ = [
    "BORROWED",
    "FILE_TARGET_MODEL",
    "FILE_TARGET_PENDING_PREFIX",
    "FILE_TARGET_UNUSABLE_PREFIX",
    "FUNDING_INTERVAL_MS",
    "HANDOFF_SIDECAR_SUFFIX",
    "MIN_FUNDING_SAMPLES",
    "MS_PER_DAY",
    "UPSTREAM_PACKAGES",
    "VENUE_BORROWED",
    "AttemptStatus",
    "ExchangeError",
    "FundingPoint",
    "HistoryMarketData",
    "ResearchStore",
    "RiskAction",
    "SpecError",
    "StopReason",
    "StoreError",
    "atomic_write_text",
    "backfill_funding",
    "epoch_ms",
    "from_epoch_ms",
    "funding_zscore",
    "load_market",
    "parse_instant",
    "payload_dir",
    "render_fetch",
    "require_number",
    "sqlite_file_uri",
]

# The packages this one may name at all. The reverse edge (any package under
# ``contrib/`` naming this one) is held shut by ``tests/test_upstream.py``.
UPSTREAM_PACKAGES: tuple[str, ...] = (
    "contrib.core",
    "contrib.hyperliquid_perp",
    "contrib.autoresearch",
)

# Every borrow as ``(dotted module, attribute)`` — the audit list, and the
# sequence the pin test walks. ``tests/test_upstream.py`` holds it equal to
# the imports above and to ``__all__``.
BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.autoresearch.constants", "FUNDING_INTERVAL_MS"),
    ("contrib.autoresearch.constants", "MS_PER_DAY"),
    ("contrib.autoresearch.fetch", "StopReason"),
    ("contrib.autoresearch.fetch", "backfill_funding"),
    ("contrib.autoresearch.fetch", "render_fetch"),
    ("contrib.autoresearch.ports", "HistoryMarketData"),
    ("contrib.autoresearch.store", "ResearchStore"),
    ("contrib.autoresearch.store", "StoreError"),
    ("contrib.autoresearch.vocabulary", "SpecError"),
    ("contrib.autoresearch.vocabulary", "require_number"),
    ("contrib.core.atomic_io", "atomic_write_text"),
    ("contrib.core.instants", "epoch_ms"),
    ("contrib.core.instants", "from_epoch_ms"),
    ("contrib.core.instants", "parse_instant"),
    ("contrib.hyperliquid_perp.common.constants", "FILE_TARGET_MODEL"),
    ("contrib.hyperliquid_perp.common.constants", "FILE_TARGET_PENDING_PREFIX"),
    ("contrib.hyperliquid_perp.common.constants", "FILE_TARGET_UNUSABLE_PREFIX"),
    ("contrib.hyperliquid_perp.common.constants", "HANDOFF_SIDECAR_SUFFIX"),
    ("contrib.hyperliquid_perp.common.store_layout", "payload_dir"),
    ("contrib.hyperliquid_perp.domains.perp.context_builder", "MIN_FUNDING_SAMPLES"),
    ("contrib.hyperliquid_perp.domains.perp.context_builder", "funding_zscore"),
    ("contrib.hyperliquid_perp.domains.perp.risk_gate", "RiskAction"),
    ("contrib.hyperliquid_perp.domains.perp.schema", "FundingPoint"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.errors", "ExchangeError"),
    ("contrib.hyperliquid_perp.persistence.repository", "AttemptStatus"),
    ("contrib.hyperliquid_perp.persistence.store_identity", "sqlite_file_uri"),
)

# The venue half, borrowed lazily (module docstring): imported only by
# :func:`load_market`. The pin test imports every entry, so a name that
# vanishes upstream still fails by name.
VENUE_BORROWED: tuple[tuple[str, str], ...] = (
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.market_data", "HyperliquidMarketData"),
    ("contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client", "HyperliquidClient"),
)


def load_market() -> HistoryMarketData:
    """The mainnet venue reader, built on first use.

    Mainnet only, for the reason ``contrib.autoresearch.upstream`` gives:
    the research store keys a settlement by coin and time alone, so a
    testnet series would silently blend into the mainnet one.
    """
    from contrib.hyperliquid_perp.exchanges.hyperliquid.market_data import HyperliquidMarketData
    from contrib.hyperliquid_perp.exchanges.hyperliquid.sdk_client import HyperliquidClient

    return HyperliquidMarketData(HyperliquidClient(network="mainnet"))
