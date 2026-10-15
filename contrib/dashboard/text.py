"""Free text from a document or a command, as this package shows it.

The packages' documents are whitelists, but a free-text field — a decision's
reason, a breadcrumb's error, a command's stderr — can quote a wallet address
or an agent key (the packages mask their own, and this package masks again
whatever it shows: a reader's error line, a page's text). A ``0x``-prefixed
20-byte or 32-byte hex is folded to its first six and last four characters;
the page and the mail never print one whole.
"""

from __future__ import annotations

import re

__all__ = ["cut", "mask_hex"]

_HEX_SECRET = re.compile(r"0x(?:[0-9a-fA-F]{64}|[0-9a-fA-F]{40})")


def mask_hex(text: str) -> str:
    """``text`` with every 20- or 32-byte ``0x`` hex folded to ``0x1234...abcd``."""
    return _HEX_SECRET.sub(lambda found: f"{found.group(0)[:6]}...{found.group(0)[-4:]}", text)


def cut(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, the last three an ellipsis when it was longer."""
    return text if len(text) <= limit else text[: limit - 3] + "..."
