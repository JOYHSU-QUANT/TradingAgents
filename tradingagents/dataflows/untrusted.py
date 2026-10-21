"""Flattening for text the vendor - or the model - did not author.

:func:`sanitize_untrusted` is the one flattening (the comment above it says
why); :func:`echo_argument` and :func:`quote_argument` are the two guards
for the MODEL's own argument quoted back at it, bare in running prose or
inside ``repr``'s own quotes (#140, #232).

Moved out of ``utils`` on 2026-10-21 (dataflows refactor plan 2026-10-16,
D4); ``utils`` re-exports every name here for its readers.
"""

from __future__ import annotations

import re

# Everything the vendor modules render — and every error they raise, since
# ``route_to_vendor`` hands an optional category's failure to the model as
# ``DATA_UNAVAILABLE: ... ({error})`` — is assembled into an LLM prompt, so a
# fragment that survives verbatim from outside the module can forge report
# structure rather than merely read oddly. The one definition of that
# flattening (it used to be copied into deribit and sosovalue_common, and
# those modules now import it under their old ``_sanitize`` name); the vendor
# modules keep the reasoning for WHICH fragments they flatten.
#
# ``sanitize_untrusted`` and the three names it reads (``MARKDOWN_CONTROL``,
# ``EMPHASIS_UNDERSCORE``, ``MAX_UNTRUSTED_CHARS``) are SUPPORTED API for
# vendor modules (#140): a new vendor module calls them directly rather than
# minting its own flattening or aliasing the cap under a private name — the
# alias is one more site a cap change has to find (deribit's was the last).
#
# Collapsing whitespace is the load-bearing half: the reports are block-level
# (sections joined by blank lines, headings and labels at the start of a line),
# so a fragment with no line breaks cannot open a block however it is
# punctuated. The character strip is the second line of defence, against what
# still reads as a marker mid-line: "*" and "`" for bold and code spans, "#"
# and "|" because a heading or a table row is worth denying even inert. "<"
# and ">" are deliberately NOT stripped: both are block-level only, and both
# carry real meaning in vendor diagnostics ("start_timestamp must be <
# end_timestamp"). Translated to a SPACE rather than deleted: deletion joins
# the fragments either side and can fuse two tokens into a third that reads as
# legitimate ("BTC|USD" → "BTCUSD").
MARKDOWN_CONTROL = str.maketrans(dict.fromkeys("#*`|", " "))

# "_" is handled separately because it also occurs inside ordinary words (a
# field name in a vendor diagnostic, an event or company name). Only underscores
# in EMPHASIS position — at a word boundary, where "_caveat._" lines sit — are
# removed; one between two alphanumerics stays.
EMPHASIS_UNDERSCORE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")

# Long enough for any real vendor error message or caller argument, short
# enough that a payload cannot bury the report's own sentences under its bulk.
MAX_UNTRUSTED_CHARS = 200


_WHITESPACE_RUN = re.compile(r"\s+")


def sanitize_untrusted(text: object, *, limit: int | None = None, keep_edges: bool = False) -> str:
    """Flatten a fragment the vendor did not author so it cannot forge structure.

    The strip runs FIRST and whitespace is collapsed after it, so neither the
    spaces the translation introduces nor the ones already in the fragment can
    survive as a run or rebuild a line break inside a table cell. ``limit``
    caps the result. Pass it where the fragment is ISOLATED (an echoed raw
    row or argument in a raised message — not a vendor's whole reason, which
    the router caps at its slot), and where a whole message is about
    to enter text the MODEL reads (a sentinel slot, a leaf's degrade line):
    there the vendor's share of the prompt has to be bounded, and what the
    cap drops is the operator's to read in a log (#171, #172). Not when
    flattening a whole message for its own raise or a log line, where the
    module's own diagnostic carries the meaning.

    ``keep_edges`` is for echoing a CALLER'S OWN value back to its author — a
    refused date, or an argument a report quotes back (see
    :func:`echo_argument`): markers become a space rather than vanishing, and
    nothing is trimmed off the ends, so ``"_2026-08-18"`` cannot come back as
    ``2026-08-18`` inside a sentence calling it invalid. A rendered vendor
    fragment wants the default — there the marker is noise and a boundary
    space would rebuild a table cell.
    """
    marker = " " if keep_edges else ""
    flat = EMPHASIS_UNDERSCORE.sub(marker, str(text).translate(MARKDOWN_CONTROL))
    flat = _WHITESPACE_RUN.sub(" ", flat) if keep_edges else " ".join(flat.split())
    if limit is not None and len(flat) > limit:
        flat = (flat[:limit] if keep_edges else flat[:limit].rstrip()) + "..."
    return flat


def echo_argument(value: object) -> str:
    """The model's own argument, flattened and capped, for text it reads again.

    A tool that quotes one of its arguments back — the symbol it could not
    verify, the topic nothing matched, the alias it does not know, or simply
    the one it names in the report it returns — is putting a fragment the
    MODEL authored into the prompt, the same direction
    :func:`quote_argument` guards for a refused date. Same FLATTENING and
    cap, and ``keep_edges`` for the same reason: markers become a space
    rather than vanishing, so ``_foo`` cannot come back as ``foo`` inside a
    sentence calling it unusable.

    Not the whole of that guard, though: this returns a BARE string, where
    :func:`quote_argument` returns one already inside ``repr``'s own escaped
    quotes. So control characters reach the prompt as themselves here, and a
    caller that wraps this result in quotes of its own supplies them as
    literal text — a value carrying that same quote character then ends the
    span early. Only use this where the echo is named bare, as the snapshot
    report's heading line does; every quoted site wants
    :func:`quote_argument` (#232). The control characters that reach the
    prompt as themselves are the NON-whitespace ones: the flattening above
    collapses every whitespace run, a lone tab or line break included.

    A clean value comes through byte for byte apart from whitespace, which
    collapses to single spaces — a topic typed with two spaces renders with
    one.

    Not for a VENDOR's message — that one is not quoted back at its author and
    wants the default edges (see :func:`sanitize_untrusted`).
    """
    return sanitize_untrusted(value, limit=MAX_UNTRUSTED_CHARS, keep_edges=True)


def quote_argument(value: object) -> str:
    """A model argument echoed back INSIDE ITS OWN QUOTES, flattened and capped.

    Returns a STRING value already delimited — do not wrap the result in
    quotes of your own. That is the whole point: the delimiters come from
    ``repr``, which
    escapes any quote character in the value and picks the other quote style
    when it has to, so the value cannot end the quoted span early. A caller
    that supplies its own literal quotes around :func:`echo_argument` instead
    gets a span a value containing that quote character can close, and the
    prose after it reads to the model as the tool's own words rather than the
    caller's value (#232). Use this wherever the echo is quoted; use
    :func:`echo_argument` only where it is named bare in running prose.

    The value is the model's own text echoed back into a sentence it reads, so
    it gets the vendor flattening with ``keep_edges`` (see there). The cap is
    enforced on the ESCAPED form: the character cap runs first (bounding the
    re-cap loop's work), ``repr`` then escapes control and non-printable
    astral characters — 4-10x growth that used to carry a "capped" hostile
    value far past the promise (#140) — and the re-cap below drops whole
    characters until the quoted form fits, so an escape sequence stays whole
    or vanishes and the quotes stay balanced. A clean value such as ``'abc'``
    comes through byte for byte, quotes included; one containing whitespace
    does not, since :func:`echo_argument` collapses every run to a single
    space.

    A NON-string is ``repr``'d too, so it is delimited only where its own
    ``repr`` is: a number, ``None`` or a bool comes back bare. The tool
    schemas send JSON strings, so only a direct caller can reach that lane.

    One VENDOR value takes this guard, and takes it for the reason above rather
    than in spite of it: ``stocktwits``' symbol-mismatch answer names the
    requested spelling and the echoed one in a single sentence whose whole
    content is that they DIFFER. A vendor spelling flattened with the default
    edges comes back reading like the clean one there ("AAPL#" as "AAPL"), and
    the sentence becomes a self-contradiction (#233). Two spellings contrasted
    character for character both want the quoted form.
    """
    if isinstance(value, str):
        flat = echo_argument(value)
        quoted = repr(flat)
        if len(quoted) > MAX_UNTRUSTED_CHARS + 2:
            # Whole characters, not repr bytes, and repr is recomputed each
            # step because dropping a character can flip repr's quote choice.
            # O(cap^2) in the worst case, bounded by the character pre-cap
            # above — rework this loop before ever growing
            # MAX_UNTRUSTED_CHARS by orders of magnitude.
            while len(repr(flat)) > MAX_UNTRUSTED_CHARS + 2:
                flat = flat[:-1]
            requoted = repr(flat)
            quoted = requoted[:-1] + "..." + requoted[-1]
        return quoted
    try:
        raw = repr(value)
    except Exception:  # noqa: BLE001 — a refusal must not become an untyped raise
        # Only a direct caller can hand over an object whose repr raises; the
        # tool schemas send JSON values. Still, the refusal is what stands
        # between that caller and the router's "vendor down" lane.
        raw = f"<{type(value).__name__} value>"
    # A non-string can only come from a direct caller (tool schemas send JSON
    # strings), so this lane is best-effort by design: bound the work up front
    # (a buggy caller can hand over a million-element repr), keep the
    # truncation boundary in the shared helper, and re-close the OUTERMOST
    # delimiter when the echo was cut so it reads as a cut value rather than a
    # broken one (#140). No whole-escape promise here — that promise above is
    # for model input, which is always a string.
    closer = raw[-1] if raw and raw[-1] in "'\")]}" else ""
    sliced = len(raw) > MAX_UNTRUSTED_CHARS * 4
    raw = raw[: MAX_UNTRUSTED_CHARS * 4]
    flat = sanitize_untrusted(raw, keep_edges=True)
    if sliced or len(flat) > MAX_UNTRUSTED_CHARS:
        # ``sliced`` forces the cut MARKING even when whitespace collapse
        # brought the prefix under the cap — a cut without an ellipsis reads
        # as the whole value.
        capped = sanitize_untrusted(raw, limit=MAX_UNTRUSTED_CHARS, keep_edges=True)
        if not capped.endswith("..."):
            capped += "..."
        return capped + closer
    return flat
