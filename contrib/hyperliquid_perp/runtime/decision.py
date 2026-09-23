"""What one AI call sees, and how a provider says the call should be retried.

The two halves of the :class:`~..ports.DecisionProvider` seam that are data
rather than interface: the provider builds a :class:`DecisionInput` before
the paid call (so the ``ai_inputs`` row can be recorded between the two
phases — phase2-spec §5.1), and raises :class:`RetryableDecisionError` for a
failure the §3.1 ladder should retry. Both drivers — the paper scheduler and
the live decision worker — consume them, and ``persistence.audit_rows``
writes the ``ai_inputs`` row from the input, so neither lane owns them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..common.constants import ERROR_TYPES
from ..common.enum_guard import check_enum
from ..domains.perp.schema import PerpMarketContext
from .position_facts import BookFacts

__all__ = ["DecisionInput", "RetryableDecisionError"]


class RetryableDecisionError(Exception):
    """A market-data / AI API failure worth retrying (spec §3.1).

    ``error_type`` is a member of the §6.2 vocabulary,
    ``common.constants.ERROR_TYPES`` (the member-by-member rationale lives
    there); ``check_enum`` enforces it HERE, at construction — the same
    posture as ``ContextRefusal`` — so a producer's typo fails on the raise
    instead of when the daemon tries to record its failure at the repository
    write boundary (which checks the same set; issue #122). Anything the
    provider does not classify is a bug, not a retry: it fails the cycle
    closed with no class at all (see ``PaperScheduler._fail_untyped``).
    """

    def __init__(self, error_type: str, message: str) -> None:
        check_enum(error_type, ERROR_TYPES, name="RetryableDecisionError.error_type")
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.message = message


@dataclass(frozen=True)
class DecisionInput:
    """Everything one AI call sees, built by the provider before the call.

    ``context`` is the market side of the ``ai_inputs`` row; the account side
    rides along as ``books`` (the one read the position section was priced
    from), and the driver reads it itself only for an input that carries
    none. The payload
    path/hash point at the full JSON the provider persisted (phase2-data §5:
    SQLite keeps the summary + path + hash, never the whole prompt).
    """

    context: PerpMarketContext
    candle_start: datetime | None = None
    candle_end: datetime | None = None
    input_payload_path: str | None = None
    input_payload_hash: str | None = None
    prompt_version: str | None = None
    # The prompt's section structure (prompt_context.context_shape), the
    # second segmentation key beside prompt_version (issue #97).
    context_shape: str | None = None
    # The third: a content digest of the format block
    # (target_decision.format_fingerprint) — the half of the prompt the other
    # two keys do not cover, whose numbers move on a config edit (issue #129).
    format_fingerprint: str | None = None
    model: str | None = None
    # The books the position section was priced from — ledger, position, the
    # newest fill's stamp — so the ``ai_inputs`` row is written from the SAME
    # read rather than a second one (issue #134). ``None``: the provider
    # carries no books (a test double, a replay harness) and the driver reads
    # them itself.
    books: BookFacts | None = None

    def __post_init__(self) -> None:
        # Path and hash are two halves of one artifact (phase2-data §5: the
        # store keeps path + hash together); a provider supplying one without
        # the other would persist an audit row that can't be verified. The
        # candle window is the same kind of pair: one boundary without the
        # other is a malformed §5 audit row, not a narrower one.
        if (self.input_payload_path is None) != (self.input_payload_hash is None):
            raise ValueError(
                "DecisionInput.input_payload_path and input_payload_hash must be "
                "provided together (or both omitted)"
            )
        if (self.candle_start is None) != (self.candle_end is None):
            raise ValueError(
                "DecisionInput.candle_start and candle_end must be provided "
                "together (or both omitted)"
            )
        # The three segmentation keys are one set too: a row stamped with a
        # version but no shape (or no fingerprint) would be indistinguishable
        # from pre-v10 / pre-v11 history, which the review reads as "unknown".
        keys = (self.prompt_version, self.context_shape, self.format_fingerprint)
        if any(k is None for k in keys) and not all(k is None for k in keys):
            raise ValueError(
                "DecisionInput.prompt_version, context_shape and format_fingerprint "
                "must be provided together (or all omitted)"
            )
        # An inverted window (start after end) is a malformed §5 row the same way
        # a half-present pair is; the spec pair is one candle's [start, end].
        if (
            self.candle_start is not None
            and self.candle_end is not None
            and self.candle_start > self.candle_end
        ):
            raise ValueError(
                "DecisionInput.candle_start must not be after candle_end "
                f"({self.candle_start} > {self.candle_end})"
            )
