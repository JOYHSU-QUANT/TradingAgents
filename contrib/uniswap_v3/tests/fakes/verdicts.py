"""Stand-ins for verdicts: synthetic verdicts on the test tokens, and a config that reads them."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from contrib.uniswap_v3.config import UniswapConfig
from contrib.uniswap_v3.domain.verdicts import (
    Rating,
    Verdict,
    VerdictRecord,
    VerdictSettings,
    text_digest,
)

from .engine import DAY, FIRST_DAY, config as _config

__all__ = ["SOURCE", "config", "record", "verdict"]

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
