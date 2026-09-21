"""Shared look-ahead-safe date-window filtering for dated content.

News, StockTwits, and Reddit all pull recent items that must be trimmed to the
analysis window so a historical/backtest run never sees content published after
its as-of date. Centralizing the rule keeps every source consistent (#1126,
#1220): every timestamp is normalized to UTC, the upper bound is exclusive at
midnight after ``end`` (so an item stamped exactly then can't leak), and an
undated item is kept only when the window reaches the present (a live run), since
in a backtest we can't prove it isn't future.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .utils import echo_argument, get_current_date, normalize_iso_date


def to_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware; a naive value is assumed to be UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def in_window(pub_dt: datetime | None, start_dt: datetime, end_dt: datetime) -> bool:
    """Whether an item belongs in the half-open window ``[start, end + 1 day)``.

    ``pub_dt`` None means undated: kept only when the window reaches the present.
    """
    end = to_utc(end_dt)
    if pub_dt is not None:
        return to_utc(start_dt) <= to_utc(pub_dt) < end + timedelta(days=1)
    return end >= datetime.now(timezone.utc) - timedelta(days=1)


def is_past_analysis_date(curr_date: str | None, today: str) -> bool:
    """Whether ``curr_date`` names a date BEFORE ``today``: the backtest lane.

    THE FAMILY POLICY for a live-only figure — one a vendor can only serve as
    of the present, because its endpoint takes no date: an options chain, an
    open-position snapshot, a company profile. On a past analysis date such a
    figure is future information, and it is WITHHELD rather than served with a
    warning beside it. A warning is not a guard: whether it holds depends on
    the model choosing to obey it, nothing in the run records whether it did,
    and a downstream summary can drop the sentence while keeping the number.
    Each vendor writes its own notice — what is missing and why differs — but
    none of them decides the rule.

    The test is "earlier than the clock", not "different from it". Callers
    derive ``curr_date`` from a local clock (``cli/main.py`` does), so east of
    UTC a run routinely sits a few hours AHEAD of the UTC date; the live figure
    is then no later than the analysis date, which is not lookahead at all, and
    refusing it would withhold these vendors' main signal for the first hours
    of every local day. A date far enough ahead to be implausible is a
    different question, and one each vendor answers for itself.

    ``today`` is passed in rather than read here so the vendor keeps ONE clock:
    a boundary that took ``now`` for its own windowing and then let this read a
    second one could straddle midnight and disagree with itself. A ``curr_date``
    that is not a usable date is not this guard's to answer — the getter's own
    date refusal names it — so it reads as "not past" and the call proceeds.
    """
    if not curr_date:
        return False
    # Judged as a DATE, not as a string: compared raw, a non-zero-padded
    # "2026-6-5" sorts after "2026-09-15", and a June backtest would be served
    # today's figures.
    as_of = normalize_iso_date(curr_date)
    return as_of is not None and as_of < today


def withhold_live_profile(curr_date: str | None, label: str) -> str | None:
    """Notice to serve instead of a live-only company profile, or None to serve it.

    Vendor "company overview" endpoints (yfinance ``Ticker.info``, Alpha Vantage
    ``OVERVIEW``) carry no historical vintage — not even name, sector and
    industry, which move when a company renames or is reclassified — so serving
    one into a run dated in the past leaks post-decision information (#1300).
    Every fundamentals vendor withholds on this rule, so switching between them
    cannot reintroduce the leak.

    The rule itself is :func:`is_past_analysis_date` above, shared with the
    other live-only vendors; what is specific here is the notice, which names
    the fields that move and where point-in-time ones can be had instead.
    """
    today = get_current_date()
    if not is_past_analysis_date(curr_date, today):
        return None
    # Non-None because the guard above answers True only for a usable date.
    curr_date = normalize_iso_date(curr_date)
    return (
        # The label is the caller's own argument coming back into text the
        # model reads, so it takes the argument guard like every other
        # rendered argument (#233): a line break in it must not open a
        # heading of the vendor's.
        f"# Company Fundamentals for {echo_argument(label)}\n"
        f"# Point-in-time as of: {curr_date}\n\n"
        f"Profile fundamentals are withheld for this date. This vendor serves "
        f"only present-day values ({today}) with no historical vintage: market "
        f"cap, valuation multiples, the 52-week range and TTM income move with "
        f"today's quote, and even the name, sector and industry reflect today "
        f"rather than {curr_date} (companies rename and get reclassified). "
        f"Serving them would put post-decision information into a {curr_date} "
        f"analysis. Point-in-time fundamentals for {curr_date} are available "
        f"from the balance sheet, income statement, and cash flow tools."
    )
