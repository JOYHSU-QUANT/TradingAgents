"""Epoch seconds as UTC text, in the few shapes the package writes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Final

__all__ = ["DATE_ONLY", "FILE_STAMP", "ISO_SECONDS", "utc_text"]

#: ``2024-01-01T00:00:00Z``, as the command line prints a time.
ISO_SECONDS: Final = "%Y-%m-%dT%H:%M:%SZ"
#: ``20240101T000000Z``, as a file name carries one.
FILE_STAMP: Final = "%Y%m%dT%H%M%SZ"
#: ``2024-01-01``, the date alone.
DATE_ONLY: Final = "%Y-%m-%d"


def utc_text(time: int, pattern: str = ISO_SECONDS) -> str:
    """``time``, epoch seconds, as UTC text in ``pattern``."""
    return datetime.fromtimestamp(time, tz=timezone.utc).strftime(pattern)
