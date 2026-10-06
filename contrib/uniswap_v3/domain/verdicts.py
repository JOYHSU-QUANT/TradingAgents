"""A verdict: what an outside judge said of one token at one bar, kept as data.

A verdict is recorded when it is given and read back later by whoever builds
a :class:`~.types.MarketView`; a strategy reads the :class:`Rating` and
nothing else of it. It belongs to no run, as a bar does: one verdict per
(source, token, bar) serves every run that reads that source. It is never
rewritten: a judge whose answers cannot be reproduced is asked once per bar,
and a bar decided on a verdict keeps that verdict's digest with the decision
(:attr:`~.records.Decision.verdicts`), so a replay can be checked against
what was seen.

The ratings are the five tiers the TradingAgents graph answers in, most
bullish first, and ``REVIEW``, its signal for an answer that holds no
rating. ``REVIEW`` is a verdict too: it says the judge was asked and gave
nothing usable, which a strategy treats as it treats no verdict at all.

:class:`Verdict` is the part a strategy may see. :class:`VerdictRecord` is
the stored row: the verdict, which model and prompt gave it, when it was
asked for, and where the judge's words were kept (a sidecar file beside the
store, with its digest), when they were. A synthetic verdict, written for a
test or a backtest, has no sidecar.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from typing import Final

__all__ = [
    "RATINGS",
    "Rating",
    "Verdict",
    "VerdictRecord",
    "VerdictSettings",
    "require_count",
    "require_digest",
    "require_source",
    "require_text",
    "text_digest",
]


class Rating(str, Enum):
    """The five tiers, most bullish first, and the signal for an answer without one."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"
    # The judge answered, and no rating could be read in the answer.
    REVIEW = "REVIEW"

    @property
    def is_review(self) -> bool:
        """Whether this is the signal for an answer that held no rating."""
        return self is Rating.REVIEW


# The tiers in order; ``REVIEW`` is not one.
RATINGS: Final = (Rating.BUY, Rating.OVERWEIGHT, Rating.HOLD, Rating.UNDERWEIGHT, Rating.SELL)

# A source names where verdicts come from and under which contract: letters,
# digits, dots, dashes and underscores, as a config key or a file name takes.
_SOURCE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# A SHA-256 digest in hex.
_DIGEST: Final = re.compile(r"[0-9a-f]{64}")


def text_digest(text: str) -> str:
    """The SHA-256 digest of ``text``, UTF-8 encoded, as 64 hex digits.

    It is how a verdict names the words it was read from, and how a
    decision names the verdicts it saw.
    """
    if not isinstance(text, str):
        raise ValueError(f"a digest is taken of a string, got {text!r}")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_text(value: object, what: str) -> None:
    """Refuse a ``value`` that is not a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")


def require_count(value: object, what: str, *, at_least: int = 0) -> None:
    """Refuse a ``value`` that is not an integer of at least ``at_least``; a bool is not one."""
    if isinstance(value, bool) or not isinstance(value, int) or value < at_least:
        bound = {0: "a non-negative integer", 1: "a positive integer"}.get(
            at_least, f"an integer of at least {at_least}"
        )
        raise ValueError(f"{what} must be {bound}, got {value!r}")


def require_source(value: object, what: str = "source") -> None:
    """Refuse a source name that is not one (:class:`Verdict`, :class:`VerdictSettings`)."""
    if not isinstance(value, str) or not _SOURCE.fullmatch(value):
        raise ValueError(
            f"{what} must be letters, digits, dots, dashes and underscores, starting with a "
            f"letter or a digit, got {value!r}"
        )


def require_digest(value: object, what: str) -> None:
    """Refuse a ``value`` that is not a digest :func:`text_digest` could have made."""
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError(
            f"{what} must be a SHA-256 digest of 64 lowercase hex digits, got {value!r}"
        )


@dataclass(frozen=True)
class Verdict:
    """What ``source`` said of ``symbol`` at the bar whose boundary is ``time``.

    ``digest`` is of the words the rating was read from, which are not here:
    a strategy reads the rating alone.
    """

    source: str
    symbol: str
    time: int
    rating: Rating
    digest: str

    def __post_init__(self) -> None:
        require_source(self.source)
        require_text(self.symbol, "symbol")
        require_count(self.time, "time")
        if not isinstance(self.rating, Rating):
            raise ValueError(f"rating must be a Rating, got {self.rating!r}")
        require_digest(self.digest, "digest")


@dataclass(frozen=True)
class VerdictRecord:
    """A verdict as it is stored: with what gave it, when, and where its words were kept.

    ``model`` and ``prompt_version`` say which judge, under which contract,
    gave the verdict; a synthetic one names what made it up. ``asked_at`` is
    when the judge was asked, in epoch seconds. ``sidecar_path`` is where the
    judge's full answer was written, with ``sidecar_digest`` of that file;
    both are ``None`` for a verdict that kept no words.
    """

    verdict: Verdict
    model: str
    prompt_version: str
    asked_at: int
    sidecar_path: str | None = None
    sidecar_digest: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.verdict, Verdict):
            raise ValueError(f"verdict must be a Verdict, got {self.verdict!r}")
        require_text(self.model, "model")
        require_text(self.prompt_version, "prompt_version")
        require_count(self.asked_at, "asked_at")
        if (self.sidecar_path is None) != (self.sidecar_digest is None):
            raise ValueError(
                "a sidecar is named with its digest, or not at all: got path "
                f"{self.sidecar_path!r} and digest {self.sidecar_digest!r}"
            )
        if self.sidecar_path is not None:
            require_text(self.sidecar_path, "sidecar_path")
            require_digest(self.sidecar_digest, "sidecar_digest")


@dataclass(frozen=True)
class VerdictSettings:
    """Which verdicts a run reads: those of one ``source``.

    A config without the section reads none, and its view carries none.
    """

    source: str

    def __post_init__(self) -> None:
        require_source(self.source)
