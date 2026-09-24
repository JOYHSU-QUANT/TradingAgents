"""One engine run, from an assembled prompt to a parsed target decision.

Both entry points drive the unmodified engine through here: ``main.run_engine``
(the one-shot) and ``EngineDecisionProvider.request_decision`` (the paper and
live daemons). They differ only in what a run that produced nothing to parse
becomes — an ``error:`` line and exit 1 on the one-shot, a
``RetryableDecisionError`` on the daemons — so :meth:`EngineRun.drive` raises
the failure types below and each caller words them.

The engine-side imports stay inside the functions: ``main`` imports this
module at load time, and ``--context-only`` must not pull in the engine tree
(``completion_usage`` imports ``langchain_core``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation-only: the engine-side imports stay function-local
    from ..domains.perp.target_decision import DecisionConfig, ParsedDecision
    from .completion_usage import CompletionUsageCollector


class EngineRunFailed(Exception):
    """``propagate`` raised ``cause``.

    ``note`` is the cap fact to append to the caller's failure text: ``""``
    unless the decision completion hit its token cap before the run failed.
    """

    def __init__(self, cause: Exception, note: str) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.note = note


class EngineOutputError(Exception):
    """``propagate`` returned ``returned``, which the parse cannot read.

    Its ``str()`` followed by ``note`` (as on :class:`EngineRunFailed`) is
    the failure text the daemon records. :class:`NonDictFinalState` is the
    one case the one-shot words apart.
    """

    def __init__(self, returned: object, note: str) -> None:
        super().__init__(
            f"engine.propagate returned an unexpected shape ({type(returned).__name__})"
        )
        self.returned = returned
        self.note = note


class NonDictFinalState(EngineOutputError):
    """``propagate`` returned a sequence of two or more whose first element,
    the final state, is not a dict."""

    def __init__(self, returned: Sequence[object], note: str) -> None:
        super().__init__(returned, note)
        self.final_state = returned[0]


class EngineRun:
    """A graph wired to its completion-usage collector. Built by :func:`build_engine_run`."""

    def __init__(
        self,
        graph: Any,
        usage: CompletionUsageCollector,
        *,
        cap: int | None,
        analysts: list[str],
    ) -> None:
        self._graph = graph
        self._usage = usage
        self._cap = cap
        self._analysts = analysts

    def drive(
        self,
        *,
        coin: str,
        trade_date: str,
        decision_cfg: DecisionConfig,
        payload_path: str | None,
    ) -> ParsedDecision:
        """``propagate``, then parse ``final_trade_decision`` under ``decision_cfg``.

        ``payload_path`` is the cycle's input payload, or ``None`` when the run
        has none (the one-shot): the two sidecars go beside it, and without
        one neither is written. Raises :class:`EngineRunFailed`,
        :class:`NonDictFinalState` or :class:`EngineOutputError` when there is
        nothing to parse; a response that fails the target contract is not an
        error — it comes back as an invalid ``ParsedDecision``.
        """
        from tradingagents.node_names import PORTFOLIO_MANAGER_NODE

        from ..domains.perp.target_decision import FINAL_TRADE_DECISION_KEY, parse_target_decision
        from .completion_usage import (
            log_decision_truncation,
            log_unparsed_decision_truncation,
            report_usage,
        )
        from .decision_reports import write_decision_reports

        usage, cap = self._usage, self._cap

        def unparsed_note() -> str:
            return log_unparsed_decision_truncation(
                usage.last_call(PORTFOLIO_MANAGER_NODE), cap=cap
            )

        try:
            propagated = self._graph.propagate(coin, trade_date, asset_type="crypto")
        except Exception as exc:  # noqa: BLE001 — every engine-side failure is the caller's to word
            raise EngineRunFailed(exc, unparsed_note()) from exc
        finally:
            # Paid for whether or not a decision came back: an engine run that
            # raised after ten completions still spent them, so the usage line
            # and sidecar are written on both exits. Never raises.
            report_usage(
                usage,
                cap=cap,
                payload_path=payload_path,
                decision_node=PORTFOLIO_MANAGER_NODE,
            )
        if not isinstance(propagated, (tuple, list)) or len(propagated) < 2:
            raise EngineOutputError(propagated, unparsed_note())
        final_state = propagated[0]
        if not isinstance(final_state, dict):
            raise NonDictFinalState(propagated, unparsed_note())
        # What the agents wrote on the way to the decision, beside the input
        # payload (the replay plan's PR 0): before the parse, so a cycle whose
        # target JSON fails closed still keeps the reports that led there.
        # Never raises; the model saw no different text. Not reached on the
        # three failures above — they have no final_state to record.
        write_decision_reports(
            final_state,
            payload_path=payload_path,
            selected_analysts=self._analysts,
        )
        # The decision completion's own stop reason decides ONE verdict: a
        # missing target JSON under a bound cap is recorded as truncated_output,
        # not invalid_output (issue #182) — the operator audits the cap number,
        # not the prompt contract. Parse first: a block that survived the cut
        # met the contract and is accepted as it always was. The LAST decision
        # completion, because only its text reached final_trade_decision.
        decision_call = usage.last_call(PORTFOLIO_MANAGER_NODE)
        truncated = decision_call is not None and decision_call.truncated
        parsed = parse_target_decision(
            final_state.get(FINAL_TRADE_DECISION_KEY), decision_cfg, truncated=truncated
        )
        if truncated and decision_call is not None:
            log_decision_truncation(decision_call, parsed, cap=cap)
        return parsed


def build_engine_run(
    *,
    engine_config: dict[str, Any],
    analysts: list[str],
    context_text: str,
    format_text: str,
) -> EngineRun:
    """Build the graph with a fresh collector attached. Nothing is spent until ``drive``.

    One collector per run (issue #182): the live lane drives runs on a worker
    thread, and one cycle's completions must not mix with another's.
    """
    from .completion_usage import CompletionUsageCollector
    from .trading_graph import build_graph

    usage = CompletionUsageCollector()
    graph = build_graph(
        perp_context_text=context_text,
        config=engine_config,
        selected_analysts=analysts,
        output_format_text=format_text,
        callbacks=[usage],
    )
    return EngineRun(graph, usage, cap=engine_config.get("max_tokens"), analysts=analysts)
