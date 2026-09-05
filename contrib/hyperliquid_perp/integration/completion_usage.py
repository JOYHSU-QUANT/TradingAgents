"""Collect per-call completion metadata from one engine run (issue #182).

``TradingAgentsGraph.propagate`` returns only rendered text; the provider's
stop reason and token usage never leave the graph. This callback handler is
the seam that carries them out: attached to both LLM clients through the
graph's ``callbacks`` constructor argument, it records one
:class:`CompletionCall` per LLM completion — which graph node made it (from
langgraph's ``langgraph_node`` run metadata), how many output tokens it used,
and whether the provider said the completion stopped at its token cap.

Measurement must never cost a decision: langchain swallows handler
exceptions (``raise_error`` is False, so a failing hook is logged by the
callback manager and the run continues), the two places that do real work —
``on_llm_end``'s metadata read and ``report_usage`` — catch and log their
own, and the collector holds no reference to the engine. One instance per
``request_decision`` — the live lane runs that on a worker thread and the
cycle's calls must not mix with another cycle's. That per-request instance is
the whole isolation story: langchain's sync callback manager invokes handlers
on the calling thread and the base graph runs its agents in sequence, so no
lock guards the lists below.

Calls made outside any graph node (the base engine's ``process_signal``
after the graph, its pending-reflection pass before it) arrive with no
``langgraph_node`` and are recorded with ``node=None``: they are paid for
and count toward the cycle's total, but no cap policy is attached to them.

Only completions that RETURNED are recorded. A call that ends in an
exception (``on_llm_error`` — e.g. the openai SDK's ``LengthFinishReasonError``
when a structured-output parse hits the cap) reported no usage the provider
vouches for, so it adds nothing to the totals; the record is what the
provider said, never a reconstruction.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult

from tradingagents.llm_clients.completion_metadata import completion_metadata_of, is_truncated

from ..common.digest import json_bytes
from ..domains.perp.target_decision import TRUNCATED_OUTPUT

if TYPE_CHECKING:
    from ..domains.perp.target_decision import ParsedDecision

logger = logging.getLogger(__name__)

__all__ = [
    "CompletionCall",
    "CompletionUsageCollector",
    "log_decision_truncation",
    "log_unparsed_decision_truncation",
    "report_usage",
]


@dataclass(frozen=True)
class CompletionCall:
    """One LLM completion as the provider reported it."""

    node: str | None  # langgraph node name; None for a call outside the graph
    model: str | None
    input_tokens: int | None
    output_tokens: int | None  # includes reasoning tokens when the provider reports them
    reasoning_tokens: int | None
    stop_reason: str | None

    @property
    def truncated(self) -> bool:
        # Derived, never stored: the same rule ``CompletionMetadata`` applies,
        # so a call cannot carry a stop reason and a verdict that disagree.
        return is_truncated(self.stop_reason)

    def to_record(self) -> dict[str, Any]:
        return {**asdict(self), "truncated": self.truncated}


class CompletionUsageCollector(BaseCallbackHandler):
    """Record every LLM completion of one engine run with its node attribution."""

    def __init__(self) -> None:
        super().__init__()
        self._node_by_run: dict[UUID, str | None] = {}
        self._calls: list[CompletionCall] = []

    # -- langchain callback surface -------------------------------------------------

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self._remember_node(run_id, metadata)

    def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # A string-prompt LLM starts with the same run metadata as a chat model.
        self._remember_node(run_id, metadata)

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        node = self._forget_node(run_id)
        try:
            meta = completion_metadata_of(response)
        except Exception:  # noqa: BLE001 — a metadata read must never fail the run
            logger.exception("completion metadata could not be read; call not recorded")
            return
        call = CompletionCall(
            node=node,
            model=meta.model,
            input_tokens=meta.input_tokens,
            output_tokens=meta.output_tokens,
            reasoning_tokens=meta.reasoning_tokens,
            stop_reason=meta.stop_reason,
        )
        self._calls.append(call)

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        self._forget_node(run_id)

    # -- what the provider reads after propagate() -----------------------------------

    @property
    def calls(self) -> tuple[CompletionCall, ...]:
        return tuple(self._calls)

    def truncated_calls(self) -> tuple[CompletionCall, ...]:
        """Every call whose provider stop reason says the cap bound."""
        return tuple(c for c in self.calls if c.truncated)

    def last_call(self, node: str) -> CompletionCall | None:
        """The LAST completion ``node`` made, or ``None`` if it made none.

        The last, not "any": with ``engine.structured_output: true`` the
        Portfolio Manager makes up to two completions (the structured attempt,
        then the free-text fallback), and only the last one's text reaches
        ``final_trade_decision``. A verdict about that text must be read off
        the completion that produced it.
        """
        for call in reversed(self.calls):
            if call.node == node:
                return call
        return None

    def total_output_tokens(self) -> int:
        """Sum of the reported output tokens (calls that reported none add 0)."""
        return sum(c.output_tokens or 0 for c in self.calls)

    def to_record(self, *, cap: int | None) -> dict[str, Any]:
        """The cycle-level usage record the provider persists beside the payload.

        ``truncated_nodes`` repeats what ``calls`` already says, on purpose: it
        is the field an operator greps a directory of sidecars for, without
        reassembling the per-call list.
        """
        return {
            "cap": cap,
            "call_count": len(self.calls),
            "total_output_tokens": self.total_output_tokens(),
            "truncated_nodes": [c.node for c in self.truncated_calls()],
            "calls": [c.to_record() for c in self.calls],
        }

    # -- internals -------------------------------------------------------------------

    def _remember_node(self, run_id: UUID, metadata: dict[str, Any] | None) -> None:
        node = (metadata or {}).get("langgraph_node")
        self._node_by_run[run_id] = node if isinstance(node, str) else None

    def _forget_node(self, run_id: UUID) -> str | None:
        return self._node_by_run.pop(run_id, None)


# -- what both lanes (daemon provider, one-shot main) do with a collector -------------


def report_usage(
    usage: CompletionUsageCollector,
    *,
    cap: int | None,
    payload_path: str | None,
    decision_node: str,
) -> None:
    """One INFO line per engine run, a WARNING per truncated non-decision call,
    and — when the run has an input payload — a ``<payload>.usage.json``
    sidecar: the cycle's completion budget made measurable (issue #182; "8192
    is enough" was a guess nothing measured).

    The sidecar sits beside the input payload but is NOT the payload: that
    file's bytes are hashed into ``ai_inputs.input_payload_hash`` and verified
    by the fingerprint backfill, so usage cannot be appended to it after the
    fact. No row points at the sidecar; it is a durable measurement artifact,
    not an audit-trail contract. Never raises — a measurement failure must not
    cost the decision the engine already paid for.
    """
    try:
        truncated = usage.truncated_calls()
        logger.info(
            "completion usage: %d call(s), %d output tokens total, cap %s; truncated: %s",
            len(usage.calls),
            usage.total_output_tokens(),
            cap,
            ", ".join(_call_label(c) for c in truncated) or "none",
        )
        for call in truncated:
            if call.node == decision_node:
                continue  # the decision lane logs its own verdict after parsing
            # A cut analyst/debate report degrades the prompt downstream but
            # breaks no contract — the cycle continues, and says so, instead of
            # the silent degradation the issue names.
            logger.warning(
                "completion truncated in %s (model %s): %s output tokens against a cap "
                "of %s — the report was cut and the cycle continues on it (issue #182)",
                _call_label(call),
                call.model,
                call.output_tokens,
                cap,
            )
        if payload_path is not None:
            Path(payload_path).with_suffix(".usage.json").write_bytes(
                json_bytes(usage.to_record(cap=cap))
            )
    except Exception:  # noqa: BLE001 — measurement must never fail the cycle
        logger.exception("completion usage could not be reported; the decision is unaffected")


def log_decision_truncation(call: CompletionCall, parsed: ParsedDecision, *, cap: int | None) -> None:
    """Say what a cut DECISION completion did to the verdict (issue #182).

    Three outcomes, three lines: the block did not survive (``truncated_output``
    — the cap is the fix, ERROR); the block survived and parsed (accepted,
    WARNING); the block survived whole but failed a field rule (the verdict
    stands and the cap is NOT its cause, WARNING) — the third exists so the
    ERROR never names the cap as the fix for a verdict the parse just decided
    the cap did not produce.
    """
    if parsed.invalid_reason == TRUNCATED_OUTPUT:
        logger.error(
            "the decision completion was truncated: %s output tokens against a cap of %s "
            "(model %s); the target JSON did not survive the cut, so this cycle fails closed "
            "as %s — the fix is engine.max_completion_tokens, a config number, not the prompt "
            "contract (issue #182)",
            call.output_tokens,
            cap,
            call.model,
            parsed.invalid_reason,
        )
    elif parsed.is_valid:
        logger.warning(
            "the decision completion was truncated (%s output tokens against a cap of %s, "
            "model %s) but the target JSON survived the cut; accepted (issue #182)",
            call.output_tokens,
            cap,
            call.model,
        )
    else:
        logger.warning(
            "the decision completion was truncated (%s output tokens against a cap of %s, "
            "model %s) but the target JSON block was whole; the verdict %s is the block's "
            "own and the cap is not its cause (issue #182)",
            call.output_tokens,
            cap,
            call.model,
            parsed.invalid_reason,
        )


def log_unparsed_decision_truncation(call: CompletionCall | None, *, cap: int | None) -> str:
    """The cap bound on the decision call, and the run then failed before parsing.

    ``report_usage`` leaves the decision node to the post-parse verdict; when
    the run raises after that completion (the engine's trailing
    ``process_signal`` call timing out, say) or returns an unusable shape, no
    parse happens and the only trace would be a word in the INFO list. Say it
    outright — the cap bound whatever the run did next — and hand the caller a
    note to append to the failure it is about to raise, so the ``api_failed``
    row's ``error_message`` (the RUNBOOK's free-text discriminator for that
    status) carries the fact too, not only the log. Returns ``""`` and logs
    nothing when ``call`` is ``None`` (the node never completed) or was not
    truncated.
    """
    if call is None or not call.truncated:
        return ""
    logger.error(
        "the decision completion was truncated (%s output tokens against a cap of %s, "
        "model %s) and the engine run then failed before the answer could be parsed; "
        "the cap bound regardless — see engine.max_completion_tokens (issue #182)",
        call.output_tokens,
        cap,
        call.model,
    )
    return f" (decision completion truncated: {call.output_tokens} output tokens against cap {cap})"


def _call_label(call: CompletionCall) -> str:
    return call.node if call.node is not None else "(outside the graph)"
