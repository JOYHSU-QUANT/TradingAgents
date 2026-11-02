"""A run whose every answer was written by the real gate: the past papers' fixture.

``conftest.ROWS`` is a hand-written table built to exercise the scorecard's
arithmetic, and its answers are shapes, not outcomes the gate would reach
from those positions. The replay needs the stronger fixture: a store whose
answers the gate actually produced (``parse_target_decision`` ->
``evaluate`` -> ``write_ai_output``, the daemon's own path) from positions
and a genesis ``risk:`` / ``decision:`` block it records, with a payload
file per question whose digest is on its input row. Then "a replayed answer
meets the gate the recorded one met" is a claim a test can hold, by
replaying each question's own recorded text.

Ten questions one bar apart, so the research split cuts them 6 / 2 / 2
(train slots 0-5, validation 6-7, holdout 8-9), and each scenario is one
thing the gate can do: approve from flat, keep a maintain, hold a target
inside the deadband, refuse a resize below the resize bar, clamp a flip,
approve a close to flat, fail closed on prose and on a cut-off answer,
refuse a low-confidence open, and hold the target already held (the
deadband answers before the zero-delta check).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from contrib.core.digest import json_bytes, payload_digest
from contrib.hyperliquid_perp.domains.perp.risk_gate import (
    CurrentPositionState,
    RiskConfig,
    evaluate,
)
from contrib.hyperliquid_perp.domains.perp.target_decision import (
    DecisionConfig,
    parse_target_decision,
)
from contrib.hyperliquid_perp.persistence import repository as repo
from contrib.hyperliquid_perp.persistence.audit_rows import write_ai_output
from contrib.hyperliquid_perp.persistence.ids import decision_attempt_id
from contrib.hyperliquid_perp.persistence.schema import SCHEMA_VERSION
from contrib.replay.lower_graph import GraphCompletion
from contrib.replay.replay import Completion
from contrib.replay.upstream import Database, from_epoch_ms, payload_dir
from contrib.replay.variant import Variant

from .conftest import COIN, GATE_DECISION, GATE_RISK, at_ms, insert_attempt, run_config

RUN_ID = "paper-GATE"
# The context is built from the newest closed candle, which the daemon
# records as ``candle_end``; the cycle itself ran this much later, so the
# fixture keeps the two apart as the real rows do.
CANDLE_LAG_MS = 26 * 60_000
EQUITY = Decimal("1000")
LEVERAGE = Decimal("1")
MAX_MARGIN_PCT = 60
FORMAT = "Answer with one fenced JSON block."

# Run 6's gate: deadband 4, the base bar 0.3, the resize bar 0.62 (the
# blocks ``conftest.run_config(gate=True)`` records).
RISK = GATE_RISK
DECISION = GATE_DECISION


def gate_run_config() -> dict:
    """The scorecard fixture's genesis, a maker run, with the gate's two blocks added."""
    return {**run_config(style="maker"), "risk": RISK, "decision": DECISION}


def decision_text(mode: str, side: str | None, margin: int | None, confidence: float) -> str:
    """A target block the engine could have emitted, fenced the way it emits it."""
    block = {
        "decision_mode": mode,
        "target_side": side,
        "requested_target_margin_pct": margin,
        "confidence": confidence,
        "rationale": "The fixture says so.",
        "key_risks": ["It is a fixture."],
    }
    return f"Final decision:\n\n```json\n{json.dumps(block)}\n```\n"


@dataclass(frozen=True)
class Paper:
    slot: int
    mark: str  # the mark the question was decided at
    size: str  # the signed position size the question was asked from
    text: str  # what the model said
    truncated: bool = False


# fmt: off
PAPERS: tuple[Paper, ...] = (
    Paper(0, "100", "0", decision_text("set_target", "long", 30, 0.8)),         # approved from flat
    Paper(1, "102", "3", decision_text("maintain_current", None, None, 0.6)),   # a maintain
    Paper(2, "101", "3", decision_text("set_target", "long", 32, 0.9)),         # inside the deadband
    Paper(3, "104", "3", decision_text("set_target", "long", 50, 0.5)),         # resize below 0.62
    Paper(4, "103", "3", decision_text("set_target", "short", 80, 0.9)),        # a flip, clamped to 60
    Paper(5, "105", "-6", decision_text("set_target", "flat", 0, 0.7)),         # a close to flat
    Paper(6, "104", "0", "I would rather not say."),                            # prose: invalid_output
    Paper(7, "106", "0", decision_text("set_target", "long", 30, 0.2)),         # an open below 0.3
    Paper(8, "108", "0", 'Final decision:\n\n```json\n{"decision_mode": "set', True),  # cut off
    Paper(9, "107", "2", decision_text("set_target", "long", 20, 0.7)),         # the target held
)
# fmt: on

TRAIN = (0, 1, 2, 3, 4, 5)
VALIDATION = (6, 7)
HOLDOUT = (8, 9)


def input_id(slot: int) -> str:
    return f"gate-in-{slot:02d}"


# The position section every fixture payload ends with, as the renderer
# spells a flat book; the asking walk replaces it with the account's own.
POSITION_SECTION = "Position:\n  flat, no open position (account equity 1000 USDC)"


def question_line(slot: int) -> str:
    """The first line of a question's context: how a fake finds the question it was sent."""
    return f"question {slot:02d}: {COIN} at mark {PAPERS[slot].mark}"


def context_text(slot: int) -> str:
    """The recorded perp context: the question line, then the position section last."""
    return f"{question_line(slot)}\n\n{POSITION_SECTION}"


def candle_end_ms(slot: int) -> int:
    """The as-of the question's context was built from: the candle end the row records."""
    return at_ms(slot) - CANDLE_LAG_MS


def payload_name(slot: int) -> str:
    return f"{COIN}-gate-{slot:02d}.json"


def payload_bytes(slot: int) -> bytes:
    return json_bytes(
        {
            "coin": COIN,
            "as_of": from_epoch_ms(at_ms(slot)).isoformat(),
            "prompt_version": "phase2-target-v6",
            "context_shape": "price|position",
            "format_fingerprint": "sha256:fixture",
            "context_text": context_text(slot),
            "format_instructions": FORMAT,
        }
    )


def position(paper: Paper) -> CurrentPositionState:
    """The gate's position input, derived as the daemon derives it from the books."""
    return CurrentPositionState.from_signed_size(
        Decimal(paper.size), mark=Decimal(paper.mark), equity=EQUITY, leverage=LEVERAGE
    )


def reports_record(slot: int) -> dict:
    """A ``.reports.json`` of the daemon's shape for one fixture question."""
    return {
        "schema": 1,
        "selected_analysts": ["market", "social", "news"],
        "market_report": f"market report {slot:02d}",
        "sentiment_report": f"sentiment report {slot:02d}",
        "news_report": f"news report {slot:02d}",
        "fundamentals_report": None,
        "investment_debate_state": {
            "bull_history": "bull",
            "bear_history": "bear",
            "history": "bull\nbear",
            "current_response": "bear",
            "judge_decision": f"judge {slot:02d}",
            "count": 2,
        },
        "investment_plan": f"plan {slot:02d}",
        "trader_investment_plan": "the recorded trader plan",
        "risk_debate_state": {
            "judge_decision": "the recorded judge decision",
            "history": "",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Judge",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 3,
        },
        "final_trade_decision": PAPERS[slot].text,
    }


def write_gate_store(
    path: Path,
    *,
    papers: tuple[Paper, ...] = PAPERS,
    grow: bool = False,
    run_id: str = RUN_ID,
    reports: bool = False,
) -> Path:
    """The fixture run, every answer produced by the real gate, every payload on disk.

    Payloads go where the daemon puts them (``payloads/<run-id>/`` beside
    the store), so the ``replay`` command finds them without a flag; the
    input rows name them by the daemon host's absolute path, as the real
    store does, and are matched by file name. ``grow`` adds ``papers`` to a
    store this function already wrote: the run trading on. ``run_id`` writes
    another run into the same store, its ids prefixed with the run id.
    ``reports`` writes a ``.reports.json`` sidecar beside every payload.
    """
    risk, decision = RiskConfig.from_dict(RISK), DecisionConfig.from_dict(DECISION)

    def ids(slot: int) -> tuple[str, str]:
        prefix = "" if run_id == RUN_ID else f"{run_id}-"
        return f"{prefix}{input_id(slot)}", f"{prefix}gate-out-{slot:02d}"

    root = payload_dir(path, run_id)
    root.mkdir(parents=True, exist_ok=True)
    with Database(path) as db, db.transaction() as conn:
        if not grow:
            repo.insert_run(
                conn,
                run_id=run_id,
                mode="paper",
                initial_balance_usdc=EQUITY,
                schema_version=SCHEMA_VERSION,
                config_json=json.dumps(gate_run_config()),
            )
        for paper in papers:
            stamp = from_epoch_ms(at_ms(paper.slot))
            raw = payload_bytes(paper.slot)
            (root / payload_name(paper.slot)).write_bytes(raw)
            if reports:
                (root / payload_name(paper.slot)).with_suffix(".reports.json").write_bytes(
                    json_bytes(reports_record(paper.slot))
                )
            state = position(paper)
            side = "flat" if state.side is None else state.side.value
            repo.insert_ai_input(
                conn,
                input_id=ids(paper.slot)[0],
                timestamp=stamp,
                mode="paper",
                run_id=run_id,
                symbol=COIN,
                candle_end=from_epoch_ms(candle_end_ms(paper.slot)),
                mark_price=Decimal(paper.mark),
                account_equity=EQUITY,
                current_position_side=side,
                current_position_size=Decimal(paper.size),
                current_margin_pct=state.margin_pct,
                configured_leverage=LEVERAGE,
                max_target_margin_pct=Decimal(MAX_MARGIN_PCT),
                input_payload_path=f"/home/trader/data/payloads/{run_id}/{payload_name(paper.slot)}",
                input_payload_hash=payload_digest(raw),
                prompt_version="phase2-target-v6",
                model="recorded-model",
                context_shape="perp",
            )
            parsed = parse_target_decision(paper.text, decision, truncated=paper.truncated)
            gate = evaluate(
                parsed, account_equity=EQUITY, current=state, risk=risk, decision_cfg=decision
            )
            output_id = ids(paper.slot)[1]
            write_ai_output(
                conn,
                now=stamp,
                output_id=output_id,
                input_id=ids(paper.slot)[0],
                decision_attempt_id=decision_attempt_id(run_id, stamp),
                mode="paper",
                run_id=run_id,
                symbol=COIN,
                gate=gate,
                parsed=parsed,
                mark_price=Decimal(paper.mark),
                account_equity=EQUITY,
            )
            insert_attempt(
                conn,
                paper.slot,
                input_id=ids(paper.slot)[0],
                output_id=output_id,
                status="completed" if parsed.is_valid else "invalid_output",
                run_id=run_id,
            )
    return path


class Echo:
    """A fake model that says, for each question, what the paper trader's model said.

    It finds the question by the context line it was sent, counts its calls
    and keeps what it was sent. ``fail`` makes that many calls raise first;
    ``text_for`` says something else for the slots it names.
    """

    def __init__(self, *, fail: int = 0, text_for: dict[int, str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.built: list[str] = []  # the variants a client was built for, by name
        self.fail = fail
        self.text_for = text_for or {}

    def __call__(self, system: str, human: str) -> Completion:
        self.calls.append((system, human))
        paper = fixture_paper(self, human)
        return Completion(
            text=self.text_for.get(paper.slot, paper.text),
            truncated=paper.truncated,
            model="echo-1",
            input_tokens=100,
            output_tokens=10,
        )


def fixture_paper(fake, haystack: str) -> Paper:
    """The fixture question ``haystack`` names (by its question line), after ``fake``'s failures are spent."""
    if fake.fail > 0:
        fake.fail -= 1
        raise ConnectionError("the provider hung up")
    for paper in PAPERS:
        if question_line(paper.slot) in haystack:
            return paper
    raise AssertionError(f"no fixture question in {haystack!r}")


class EchoGraph:
    """A fake lower graph: says what paper's model said for the question named in the context.

    Built the way the command builds the real one (from the variant), it
    keeps every ask it was sent. ``fail`` makes that many asks raise first;
    ``text_for`` says something else for the slots it names.
    """

    def __init__(
        self, variant: Variant, *, fail: int = 0, text_for: dict[int, str] | None = None
    ) -> None:
        self.variant = variant
        self.asked: list[dict] = []
        self.fail = fail
        self.text_for = text_for or {}
        # What the real graph keeps of its failed runs; the fake's failures cost 40 / 4.
        self.lost_input_tokens = 0
        self.lost_output_tokens = 0

    def ask(self, *, coin, as_of, context_text, format_instructions, reports) -> GraphCompletion:
        self.asked.append(
            {
                "coin": coin,
                "as_of": as_of,
                "context_text": context_text,
                "format_instructions": format_instructions,
                "reports": reports,
            }
        )
        try:
            paper = fixture_paper(self, context_text)
        except ConnectionError:
            self.lost_input_tokens += 40
            self.lost_output_tokens += 4
            raise
        return GraphCompletion(
            text=self.text_for.get(paper.slot, paper.text),
            truncated=paper.truncated,
            model="echo-graph",
            input_tokens=100,
            output_tokens=10,
            graph_input_tokens=500,
            graph_output_tokens=50,
            completions=5,
        )


# -- variants and the command line, shared by the suites -------------------------------

SYSTEM = "You are the portfolio manager. Decide."


def variant(**overrides) -> Variant:
    """A variant in memory: the minimal one, with ``overrides`` applied."""
    fields = {"name": "v", "provider": "openrouter", "model": "a/b", "system_prompt": "Decide."}
    fields.update(overrides)
    return Variant(**fields)


def graph_variant(**overrides) -> Variant:
    """A graph variant in memory: no prompt, the same model throughout, with ``overrides`` applied."""
    return variant(**{"name": "g", "system_prompt": "", "mode": "graph", **overrides})


# The day before the fixture's first question (2027-01-15): a cutoff that
# leaves no question out, so a variant is scorable without it saying so.
BEFORE_ANY_QUESTION = "2027-01-14"


def write_variant(
    directory: Path, name: str, *, cutoff: str | None = BEFORE_ANY_QUESTION
) -> Path:
    """A variant file named ``name`` (model ``fixture/<name>``) and its prompt, in ``directory``."""
    (directory / "system.md").write_text(SYSTEM, encoding="utf-8")
    lines = [
        f"name: {name}",
        "model:",
        "  provider: openrouter",
        f"  id: fixture/{name}",
        "system_prompt_path: system.md",
    ]
    if cutoff is not None:
        lines.append(f"model_cutoff: {cutoff}")
    path = directory / f"{name}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_graph_variant(
    directory: Path,
    name: str,
    *,
    cutoff: str | None = BEFORE_ANY_QUESTION,
    guardrail: str = "recorded",
    quick: bool = True,
    prompt: bool = False,
) -> Path:
    """A graph variant file named ``name`` in ``directory``: deep model ``fixture/<name>``."""
    lines = [
        f"name: {name}",
        "mode: graph",
        "model:",
        "  provider: openrouter",
        f"  id: fixture/{name}",
    ]
    if quick:
        lines += ["quick_model:", "  provider: openrouter", f"  id: fixture/{name}-quick"]
    if prompt:
        (directory / "system.md").write_text(SYSTEM, encoding="utf-8")
        lines.append("system_prompt_path: system.md")
    lines.append(f"guardrail: {guardrail}")
    if cutoff is not None:
        lines.append(f"model_cutoff: {cutoff}")
    path = directory / f"{name}.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def replay_argv(store: Path, variant_file: Path, *extra: str) -> list[str]:
    """``replay`` on the fixture run, into ``replay.sqlite`` beside the store."""
    return [
        "replay",
        "--db",
        str(store),
        "--run-id",
        RUN_ID,
        "--variant",
        str(variant_file),
        "--replay-db",
        str(store.parent / "replay.sqlite"),
        *extra,
    ]
