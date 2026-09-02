"""Attach the shared date-sentinel sentence to a ``@tool``'s description.

The description is what the model reads when CHOOSING arguments, and the
getters behind the date-taking tools answer an unusable date with
``dataflows.utils.invalid_date_sentinel`` rather than data. #140 found eleven
wrappers describing only "a formatted report": the tools this repo had just
taught to refuse were exactly the ones whose descriptions never mentioned a
refusal. The sentence itself is owned by ``dataflows.utils`` beside the
sentinel it describes; this decorator exists so a wrapper cannot hand-write a
drifting variant — or forget one — and a test iterates every date-taking tool
asserting the tag is present.

Apply ABOVE ``@tool`` (so it decorates the tool object, not the function)::

    @notes_date_sentinel("curr_date")
    @tool
    def get_fear_greed(...): ...
"""

from tradingagents.dataflows.utils import date_sentinel_note


def notes_date_sentinel(*params: str, omitted_ok: bool = False, disclosure: bool = False):
    """Append the standard date-sentinel sentence to the decorated tool's description.

    The knobs are ``date_sentinel_note``'s — see there for why the omission
    REMEDY (``disclosure``) is separate from the omission GATE (``omitted_ok``).
    """
    note = date_sentinel_note(*params, omitted_ok=omitted_ok, disclosure=disclosure)

    def attach(tool_obj):
        tool_obj.description = tool_obj.description.rstrip() + note
        return tool_obj

    return attach
