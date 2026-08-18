from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

if TYPE_CHECKING:
    import pandas as pd

logger = logging.getLogger(__name__)

SavePathType = Annotated[str, "File path to save data. If None, data is not saved."]

# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), and plus
# (forex/CFD symbols like XAUUSD+). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(f"ticker contains characters not allowed in a filesystem path: {value!r}")
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value


# How far the analysis date may sit behind the wall clock before a live-only
# vendor's report discloses that its values are today's, not that date's. One
# shared bound (the default of live_snapshot_note) so every live-only vendor
# agrees on what counts as "a backtest" within the same run (#30).
MAX_LIVE_SNAPSHOT_BEHIND_DAYS = 2


def _plural_days(n: int) -> str:
    return "day" if n == 1 else "days"


def _parse_day(value, label: str) -> datetime | None:
    """Parse a date-ish value's ``yyyy-mm-dd`` prefix, or ``None``.

    Accepts strings, datetimes, and pandas Timestamps (anything whose ``str``
    starts with the date). An unparseable non-empty value is logged: the
    freshness annotations degrade to silence on bad input, and without a log
    line a vendor-side date-format change would turn every future disclosure
    off invisibly.
    """
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        if value:
            logger.warning("freshness note: unparseable %s %r; note suppressed", label, value)
        return None


def data_lag_note(
    latest_date,
    curr_date,
    max_lag_days: int,
    source_phrase: str,
) -> str:
    """Return a one-line staleness disclosure when the newest data row lags curr_date.

    Opt-in freshness annotation (#30): a vendor that renders a "latest" value
    compares that row's date against the date being analysed and discloses the
    gap instead of letting an old value read as current. Annotation, not a
    raise (PR #16 route): these vendors have no fresher fallback and a lag
    usually means the upstream has not published yet, so the honest output is
    the stale value plus this disclosure.

    Both dates accept ``yyyy-mm-dd`` strings (extra ISO time suffixes are
    ignored) or date-like objects. Returns ``""`` when the data is fresh
    enough or when either date is unparseable — an annotation helper must
    degrade, never raise.
    """
    latest_dt = _parse_day(latest_date, "latest_date")
    curr_dt = _parse_day(curr_date, "curr_date")
    if latest_dt is None or curr_dt is None:
        return ""
    lag_days = (curr_dt - latest_dt).days
    if lag_days <= max_lag_days:
        return ""
    return (
        f"_Data lag: the newest {source_phrase} is {latest_dt.strftime('%Y-%m-%d')}, "
        f"{lag_days} {_plural_days(lag_days)} before {str(curr_date)[:10]}; "
        f"treat the latest value as stale._"
    )


def live_snapshot_note(
    curr_date,
    source_phrase: str,
    max_behind_days: int = MAX_LIVE_SNAPSHOT_BEHIND_DAYS,
    today: str | None = None,
) -> str:
    """Return a disclosure when live-only data is rendered for a past analysis date.

    Some vendors (prediction markets, current-state fundamentals, live message
    streams) can only serve *today's* values — there is no historical snapshot
    to fetch. When the date being analysed sits meaningfully behind the wall
    clock (a backtest), the report must say the numbers are live as of the
    fetch, not as of ``curr_date``, or the agent will read today's state as
    history (#30).

    Returns ``""`` when curr_date is close enough to today or unparseable —
    same degrade-never-raise contract as :func:`data_lag_note`.
    """
    today_str = today or date.today().strftime("%Y-%m-%d")
    curr_dt = _parse_day(curr_date, "curr_date")
    today_dt = _parse_day(today_str, "today")
    if curr_dt is None or today_dt is None:
        return ""
    behind_days = (today_dt - curr_dt).days
    if behind_days <= max_behind_days:
        return ""
    return (
        f"_Note: {source_phrase} live values as of the fetch ({today_str}), not a "
        f"snapshot as of {str(curr_date)[:10]} ({behind_days} {_plural_days(behind_days)} "
        f"earlier); do not read them as that date's state._"
    )


def save_output(data: pd.DataFrame, tag: str, save_path: SavePathType = None) -> None:
    if save_path:
        data.to_csv(save_path, encoding="utf-8")
        print(f"{tag} saved to {save_path}")


def get_current_date():
    return date.today().strftime("%Y-%m-%d")


def decorate_all_methods(decorator):
    def class_decorator(cls):
        for attr_name, attr_value in cls.__dict__.items():
            if callable(attr_value):
                setattr(cls, attr_name, decorator(attr_value))
        return cls

    return class_decorator


def get_next_weekday(date):

    if not isinstance(date, datetime):
        date = datetime.strptime(date, "%Y-%m-%d")

    if date.weekday() >= 5:
        days_to_add = 7 - date.weekday()
        next_weekday = date + timedelta(days=days_to_add)
        return next_weekday
    else:
        return date
