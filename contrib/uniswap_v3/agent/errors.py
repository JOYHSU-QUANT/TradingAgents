"""What the agent layer raises, by what the caller should do about it."""

from __future__ import annotations

__all__ = ["AgentError", "BarNotStored", "JudgeUnavailable"]


class AgentError(Exception):
    """Anything the agent layer raises: the judge could not be asked, or what it said cannot be used.

    Raised as is, it says asking again unchanged will not help; the two
    subclasses are the cases where a later visit may do better.
    """


class JudgeUnavailable(AgentError):
    """The judge was asked and did not answer, for a reason that may pass: the gateway, the network, a quota, a data vendor down.

    Nothing of that answer is recorded; a later visit asks again.
    """


class BarNotStored(AgentError):
    """The store has no bar at the boundary to judge, so there is nothing to show the judge yet.

    A backfill or a paper visit reads it; a later visit asks.
    """
