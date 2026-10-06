"""What the agent layer raises, by what the caller should do about it."""

from __future__ import annotations

__all__ = ["AgentError", "BarNotStored", "JudgeUnavailable"]


class AgentError(Exception):
    """The judge cannot be asked, or what it said cannot be used, and asking again unchanged will not help."""


class JudgeUnavailable(AgentError):
    """The judge was asked and did not answer: the model, its gateway or the data it fetches failed.

    Nothing of that answer is recorded; a later visit asks again.
    """


class BarNotStored(AgentError):
    """The store has no bar at the boundary to judge, so there is nothing to show the judge yet.

    A backfill or a paper visit reads it; a later visit asks.
    """
