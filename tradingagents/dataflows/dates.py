"""The date argument a routed tool refuses, and the sentinel it answers with.

:func:`normalize_iso_date` is the one parse rule a model-supplied date is
judged by; :func:`date_refusal` and :func:`date_range_refusal` are the
judgement, :func:`refuse_date` the act (one operator log line, one sentinel)
and :func:`invalid_date_sentinel` the sentence - one sentence for every
vendor of every routed tool, since which vendor ``data_vendors`` selected is
not something the agent can see (#89, #111, #119). :func:`date_sentinel_note`
is the tool-description paragraph that describes the sentinel, kept beside it
so the two cannot drift (#140).

Moved out of ``utils`` on 2026-10-21 (dataflows refactor plan 2026-10-16,
D4); ``utils`` re-exports every name here for its readers.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal

from .errors import WiringGapError
from .failures import wiring_gap
from .untrusted import quote_argument

logger = logging.getLogger(__name__)


def normalize_iso_date(value) -> str | None:
    """Canonical ``YYYY-MM-DD`` for a date string, or None if it is not a date.

    ``strptime`` accepts non-zero-padded input ("2026-6-5"), which then compares
    WRONG lexically against zero-padded vendor date fields — ``"2024-12-31" <=
    "2026-6-5"`` is True — so the raw string must be normalised before any
    look-ahead filter rather than compared as text.

    That lexical comparison is also why this parser is strict where
    :func:`lag_notes.parse_day` is deliberately lenient: that one prefix-trims so an
    annotation can read datetimes and ISO time suffixes, and relaxing this one to
    match would drop the guarantee silently. They are not two spellings of one
    idea — do not unify them.
    """
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


# The closed set of date arguments a refusal can name, and the tag each one
# carries. Closed on purpose (the same reasoning as the disposition vocabulary,
# #84): the tags are read by the model, so a new one must be a decision made
# here, not minted by whatever a new call site happens to pass — an unknown
# argument name raises at the call rather than inventing a tag.
DATE_ARGUMENT_TAGS = {
    "curr_date": "INVALID_CURR_DATE",
    "start_date": "INVALID_START_DATE",
    "end_date": "INVALID_END_DATE",
}

# What a usable date would have done: bounded a single point in time (the
# analysis date), bounded one end of a window, or — for the live-only sources
# whose ``curr_date`` never bounds the data at all — gated the as-of
# DISCLOSURE the report carries (#89: the date decides whether the report may
# claim its live values are that date's). Stated by the caller, not inferred
# from the argument's name — a future point-in-time tool whose argument is
# called ``as_of_date`` must not silently be told it asked for a window, and a
# disclosure-only tool must not claim its data could have been bounded (#144:
# that claim invited the model to retry historical dates against a live
# snapshot).
DateKind = Literal["point", "window", "disclosure"]


# The omission clause both the sentinel tail and the tool-description note
# append for the disclosure-only tools — one literal, so a description cannot
# promise a remedy the sentinel stopped offering, or vice versa (#140 review).
_OMIT_CLAUSE = " or omit it"


def invalid_date_sentinel(
    value,
    *,
    what: str,
    kind: DateKind,
    param: str = "curr_date",
    _echo: str | None = None,
) -> str:
    """The sentinel served when a supplied date argument is not a usable date.

    Loud to the LLM (it can retry with a valid date), leaks no data, and is
    RETURNED, never raised, by every date-taking routed tool, core or optional:
    the router serves any returned string as the tool's answer, whereas a raise
    is judged by the category — a core one crashes the ToolNode-wrapped run
    (``raise first_error``), and an optional one is rendered as "this source is
    down, proceed without it" (#119), a verdict the model cannot fix by
    retrying with a better argument.

    Shared by both vendors serving each routed tool so the agent reads the same
    sentence either way (#89) — which vendor ``data_vendors`` selected is not
    something the agent can see, so an answer that differs by vendor is one it
    has no way to interpret. ``what`` names the data the date was meant to
    bound and ``kind`` says how (see :data:`DateKind`); both are required, so a
    caller that forgot either cannot emit a confident sentence about the wrong
    thing. ``param`` names the argument being refused (#111) — the OHLCV and
    ticker-news tools take ``start_date``/``end_date`` — and must be one of
    :data:`DATE_ARGUMENT_TAGS`.

    The retry tail offers omission exactly for ``kind="disclosure"`` (#144 —
    the model failing RIGHT NOW reads this sentence, not the tool
    description, so a legal exit stated only there kept it resubmitting
    reformatted dates). Derived from ``kind`` rather than carried as a second
    flag, so "these values are live" and "or omit it" cannot ship separately:
    a BOUNDED tool must never invite the omission that would switch its
    look-ahead filtering off, even where omission is technically legal (the
    statement getters' #73 lane — their ``date_refusal`` gate stays
    ``omitted_ok=True``, their sentence stays a bounding one). ``_echo`` lets
    :func:`refuse_date` pass the echo it already computed for its log line;
    anyone else leaves it to be computed here.

    The echoed value is flattened and capped (see :func:`quote_argument`):
    the optional-category getters used to carry that guard in their own
    vendor-error messages (#119 moved them onto this sentinel), and the core
    tools never had it.
    """
    # Both of this module's own tables below — the tag lookup here and the
    # ``kind`` branch under it — are keyed by literals the getters write, so
    # a miss is a typo or a parameter added without a row —
    # ours, and reachable only on a call the model already spoiled with a bad
    # date. Said by type, since the router cannot otherwise tell it from the
    # vendor's library failing and would hand the analyst the miss as its
    # report (#219).
    with wiring_gap("date-refusal argument tag"):
        tag = DATE_ARGUMENT_TAGS[param]
    retry = "retry with a valid yyyy-mm-dd date"
    if kind == "point":
        consequence = f"{what} cannot be bounded to a point in time"
    elif kind == "window":
        consequence = f"the {what} window cannot be resolved"
    elif kind == "disclosure":
        # Disclosure-only tools serve LIVE values whatever the date; the date
        # only gates the as-of disclosure. "Cannot be bounded" here claimed
        # the opposite and invited a historical-date retry (#144). The
        # template conjugates for a PLURAL ``what`` ("are") — both current
        # disclosure whats are plural; a singular one needs a decision here.
        consequence = f"the report cannot say whether the live {what} are as of that date"
        retry += _OMIT_CLAUSE
    else:
        # Mirror DATE_ARGUMENT_TAGS: an unknown kind fails at the call
        # rather than falling into whichever branch is last — which would
        # hand a typo the strongest wrong claim (#140 review).
        raise WiringGapError(f"unknown DateKind {kind!r}")
    echo = _echo if _echo is not None else quote_argument(value)
    return (
        f"{tag}: {param} {echo} is not a valid yyyy-mm-dd date, "
        f"so {consequence}. "
        f"No data returned; {retry}. Do not fabricate values."
    )


def date_refusal(
    value,
    *,
    what: str,
    kind: DateKind,
    param: str = "curr_date",
    omitted_ok: bool = False,
) -> str | None:
    """The sentinel refusing a SUPPLIED-but-unusable date, or None to proceed.

    ``None`` is refused by default: the routed tools declare their dates as
    required ``str`` arguments, so the model cannot send it (the tool schema
    rejects a null before the getter runs) and a ``None`` that arrives came
    from a direct caller — passed on, it reached a ``strptime`` as a TypeError
    outside every vendor lane. The fundamentals getters and the
    prediction-market getter are the stated exceptions: they pass
    ``omitted_ok=True`` because there ``None`` means the model omitted the
    argument, which keeps the date-less fallback lane (#73, #139).
    Any other value was supplied, so one that will not parse — the empty string
    included — is a request that cannot be answered, not a request for no bound.

    Both halves are one judgement, so it lives here rather than in either vendor:
    a later refinement (say, also refusing a future-dated curr_date) applied to a
    vendor-local copy would reach that vendor's getters and silently miss the
    other's, which is the drift this whole change exists to close. The three
    inputs that used to be answered differently per vendor are in the CHANGELOG.

    The half that ACTS on the judgement is :func:`refuse_date`, which this
    delegates to. Prefer this function: reach for that one only where the
    caller owns a different parse rule on purpose (#112), and read its
    docstring first — it does not judge.
    """
    if value is None and omitted_ok:
        return None
    if value is not None and normalize_iso_date(value) is not None:
        return None
    return refuse_date(value, what=what, kind=kind, param=param)


def refuse_date(
    value,
    *,
    what: str,
    kind: DateKind,
    param: str = "curr_date",
) -> str:
    """Refuse a date already judged unusable: log the one operator line, serve the sentinel.

    The refusal is RETURNED, so it never reaches the router's warning lane;
    without this line an operator's log shows nothing for a model that keeps
    sending a date no tool can use (#119). Info, not warning: the model is
    told to retry, and the echo is the flattened one the sentence carries —
    computed once here and handed to the sentinel (#140).

    Split out of :func:`date_refusal` — which judges the value first — so a
    tool that keeps its own parse rule (the verification snapshot's looser
    pandas gate is a decision, #112) can refuse with the same log line and
    the same sentence rather than a hand-written copy of either (#230).
    """
    echo = quote_argument(value)
    logger.info("Refusing unusable %s %s for %s", param, echo, what)
    return invalid_date_sentinel(value, what=what, kind=kind, param=param, _echo=echo)


def date_sentinel_note(*params: str, omitted_ok: bool = False, disclosure: bool = False) -> str:
    """The tool-description sentence for a tool that answers with the date sentinel.

    Appended to ``tool.description`` by
    ``agents.utils.tool_notes.notes_date_sentinel`` — one wording, owned here
    beside the sentinel it describes, so a wrapper cannot hand-write a variant
    or describe itself as returning only "a formatted report" while its getter
    answers a bad date with :func:`invalid_date_sentinel` (#140: eleven
    wrappers did exactly that, and the model reads the description when
    CHOOSING arguments). ``params`` are the tool's date arguments (a typo
    raises through :data:`DATE_ARGUMENT_TAGS` as a bare ``KeyError``: this
    runs while a tool description is built, never inside the router's call,
    so it has no ending to be told apart from a vendor's — which is why the
    sentinel's own lookup of that table wears ``WiringGapError`` and this
    one does not).

    Two knobs because they answer different questions: ``omitted_ok`` matches
    the getter's None-gate and shapes the trigger ("is supplied but ..."),
    while ``disclosure`` matches the getter's ``kind="disclosure"`` and adds
    the omission remedy. The statement tools legally accept omission (#73)
    yet must not ADVERTISE it — their date bounds the data, and inviting
    omission invites switching look-ahead filtering off (#140 review).
    """
    if not params:
        raise ValueError("date_sentinel_note needs at least one date parameter")
    if disclosure and not omitted_ok:
        raise ValueError("a disclosure-only date is omittable by construction")
    tags = " or ".join(DATE_ARGUMENT_TAGS[p] for p in params)
    names = " or ".join(params)
    supplied = "is supplied but " if omitted_ok else "is "
    remedy = "retry with a valid date" + (_OMIT_CLAUSE if disclosure else "")
    # Own paragraph ("\n\n"): appended to docstrings whose last line rarely
    # ends with a period, a same-line note read as a run-on sentence in 14 of
    # the 16 rendered descriptions.
    return (
        f"\n\nReturns an {tags} sentinel instead of data when {names} {supplied}"
        f"not a usable yyyy-mm-dd date ({remedy}; do not fabricate values)."
    )


def date_range_refusal(start_date, end_date, *, what: str) -> str | None:
    """:func:`date_refusal` over a required ``start_date``/``end_date`` pair.

    The OHLCV and ticker-news tools bound a window rather than a point, so they
    have two arguments to refuse and no date-less lane. Start is judged first
    and only the first unusable one is named: one sentence asks for one fix (a
    preference, not a measured claim about how the model retries).
    """
    refusal = date_refusal(start_date, what=what, kind="window", param="start_date")
    if refusal is not None:
        return refusal
    return date_refusal(end_date, what=what, kind="window", param="end_date")
