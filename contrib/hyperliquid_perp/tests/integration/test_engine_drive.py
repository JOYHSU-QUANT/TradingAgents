"""``EngineRun.drive``: what it hands ``propagate`` (#392) and what it collects around it (#399)."""

from __future__ import annotations

import logging

from contrib.hyperliquid_perp.domains.perp.target_decision import DecisionConfig
from contrib.hyperliquid_perp.integration.completion_usage import CompletionUsageCollector
from contrib.hyperliquid_perp.integration.engine_drive import EngineRun


class _Graph:
    """Records the ``propagate`` call and answers a final state with no decision text."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def propagate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return ({"final_trade_decision": ""}, "hold")


def test_drive_hands_the_graph_yahoos_spelling_of_the_coin_not_the_bare_coin():
    # A bare ``BTC`` reaches yfinance as the Grayscale ETF; the graph gets
    # ``BTC-USD`` while the caller keeps naming the coin as the perp side does.
    graph = _Graph()
    run = EngineRun(graph, CompletionUsageCollector(), cap=None, analysts=["market"])
    parsed = run.drive(
        coin="BTC", trade_date="2026-11-03", decision_cfg=DecisionConfig(), payload_path=None
    )
    assert graph.calls == [(("BTC-USD", "2026-11-03"), {"asset_type": "crypto"})]
    # An empty decision text fails the contract as an invalid decision, not an error.
    assert not parsed.is_valid
    assert parsed.unavailable_inputs == ()


class _GraphWithoutTwoInputs(_Graph):
    """Answers like ``_Graph`` after the router has logged two optional categories as unavailable."""

    def propagate(self, *args, **kwargs):
        router = logging.getLogger("tradingagents.dataflows.interface")
        for category in ("news_data", "macro_data", "news_data"):
            router.warning(
                "Optional %s unavailable for %s: %s",
                category,
                "get_x",
                "down",
                extra={"unavailable_category": category},
            )
        router.warning("Vendor 'x' failed for get_y: boom")  # no category: not an input
        return super().propagate(*args, **kwargs)


def test_drive_records_the_optional_inputs_the_run_went_without():
    # The categories the router's sentinel lane named during the run ride the
    # parsed decision, sorted and once each (#399); a WARNING without the
    # category attribute is some other lane's and does not count.
    graph = _GraphWithoutTwoInputs()
    run = EngineRun(graph, CompletionUsageCollector(), cap=None, analysts=["market"])
    parsed = run.drive(
        coin="BTC", trade_date="2026-11-03", decision_cfg=DecisionConfig(), payload_path=None
    )
    assert parsed.unavailable_inputs == ("macro_data", "news_data")
    # The collector was detached after the run: nothing of this run's is
    # left on the router's logger to collect the next run's categories.
    router = logging.getLogger("tradingagents.dataflows.interface")
    assert not any(hasattr(h, "categories") for h in router.handlers)


class _GraphAnswering(_Graph):
    """Answers with the decision text it was built with."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def propagate(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return ({"final_trade_decision": self._text}, "hold")


def _decision_text(rationale: str) -> str:
    return (
        '{"decision_mode": "set_target", "target_side": "long", '
        '"requested_target_margin_pct": 5, "confidence": 0.8, '
        f'"rationale": "{rationale}", "key_risks": ["a risk"]}}'
    )


def _note(count: int) -> str:
    return (
        f"the engine's answer held {count} lone surrogate character(s) (half of an emoji's "
        "pair), which UTF-8 cannot write; each was replaced with U+FFFD"
    )


def _drive_warnings(caplog, text: str) -> tuple[object, list[tuple[str, str]]]:
    run = EngineRun(_GraphAnswering(text), CompletionUsageCollector(), cap=None, analysts=["market"])
    with caplog.at_level(logging.WARNING, logger="contrib.hyperliquid_perp.integration.engine_drive"):
        parsed = run.drive(
            coin="BTC", trade_date="2026-11-03", decision_cfg=DecisionConfig(), payload_path=None
        )
    said = [
        (record.levelname, record.getMessage())
        for record in caplog.records
        if record.name == "contrib.hyperliquid_perp.integration.engine_drive"
    ]
    return parsed, said


def test_drive_says_so_when_the_answer_held_half_of_an_emoji(caplog):
    # issue #427: the model wrote half of an emoji's pair as an escape. The
    # decision stands with U+FFFD in its place, and the run that collected the
    # answer is where the operator is told — once, with how many.
    text = _decision_text("momentum \\ud83d up")
    assert text.isascii()
    parsed, said = _drive_warnings(caplog, text)
    assert parsed.is_valid and parsed.replaced_surrogates == 1
    assert said == [("WARNING", _note(1))]


def test_drive_says_nothing_of_the_kind_about_a_clean_answer(caplog):
    parsed, said = _drive_warnings(caplog, _decision_text("momentum up"))
    assert parsed.is_valid and parsed.replaced_surrogates == 0
    assert said == []


def test_drive_says_so_about_an_answer_that_failed_closed_as_well(caplog):
    # No decision in it at all, and the character in the text itself: the
    # answer is stored nowhere, but the copy the lane logs for diagnosis now
    # shows U+FFFD, and this line is what says the model did not write it.
    parsed, said = _drive_warnings(caplog, "no decision here \ud83d")
    assert not parsed.is_valid and parsed.replaced_surrogates == 1
    assert said == [("WARNING", _note(1))]
