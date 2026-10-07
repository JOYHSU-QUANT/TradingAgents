"""Stand-ins for verdicts: synthetic verdicts on the test tokens, a config that reads them, and a scripted judge."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from contrib.uniswap_v3.agent.graph import REPORT_KEYS, Answer
from contrib.uniswap_v3.config import UniswapConfig
from contrib.uniswap_v3.domain.verdicts import (
    Rating,
    Verdict,
    VerdictRecord,
    VerdictSettings,
    text_digest,
)

from .engine import DAY, FIRST_DAY, config as _config

__all__ = ["SOURCE", "ScriptedJudge", "config", "record", "verdict"]

# The source the tests' verdicts come from.
SOURCE = "test-judge"


def verdict(
    symbol: str = "WETH",
    day: int = 0,
    rating: Rating = Rating.BUY,
    *,
    source: str = SOURCE,
    words: str | None = None,
) -> Verdict:
    """``rating`` on ``symbol`` at the bar ``day`` days after :data:`FIRST_DAY`.

    The digest is of ``words``, or of a sentence naming the rating, so two
    verdicts of one rating on one token at one bar have one digest.
    """
    time = FIRST_DAY + day * DAY
    text = f"{symbol} at {time}: {rating.value}" if words is None else words
    return Verdict(source=source, symbol=symbol, time=time, rating=rating, digest=text_digest(text))


def record(
    symbol: str = "WETH",
    day: int = 0,
    rating: Rating = Rating.BUY,
    *,
    source: str = SOURCE,
    asked_after: int = 600,
    **changes: Any,
) -> VerdictRecord:
    """A stored synthetic verdict, asked ``asked_after`` seconds after its bar, without a sidecar."""
    said = verdict(symbol, day, rating, source=source)
    fields: dict[str, Any] = {
        "verdict": said,
        "model": "synthetic",
        "prompt_version": "test-1",
        "asked_at": said.time + asked_after,
    }
    return VerdictRecord(**{**fields, **changes})


def config(source: str = SOURCE, **execution: Any) -> UniswapConfig:
    """The engine fakes' config, reading the verdicts of ``source``."""
    return replace(_config(**execution), verdicts=VerdictSettings(source=source))


class ScriptedJudge:
    """Answers each ticker with what the script holds for it, keeping words like the real judge.

    An answer that is an exception is raised; a ticker the script lacks
    gets ``Hold``. Like the real judge, it reads data through the day it
    is asked on, so it is not point in time. ``asked`` keeps every question.
    """

    model = "scripted-model"
    settings: Mapping[str, object] = {"llm_provider": "scripted"}
    rehearsal = False
    point_in_time = False

    def __init__(self, script: Mapping[str, Rating | Exception] | None = None) -> None:
        self.script = dict(script or {})
        self.asked: list[tuple[str, str, str]] = []

    def ask(self, ticker: str, trade_date: str, context: str) -> Answer:
        self.asked.append((ticker, trade_date, context))
        rating = self.script.get(ticker, Rating.HOLD)
        if isinstance(rating, Exception):
            raise rating
        decision = (
            f"The scripted judge on {ticker} as of {trade_date} came to no rating."
            if rating is Rating.REVIEW
            else f"**Rating**: {rating.value}\n\nThe scripted judge on {ticker} as of {trade_date}."
        )
        reports: dict[str, object] = {"selected_analysts": ["market"]}
        reports.update(dict.fromkeys(REPORT_KEYS))
        reports["market_report"] = f"a market report on {ticker}"
        reports["final_trade_decision"] = decision
        return Answer(decision=decision, rating=rating, reports=reports, elapsed_seconds=1.5)
