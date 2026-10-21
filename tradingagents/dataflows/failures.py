"""What a failed vendor call was, and the words it gets.

The library-neutral readers of an untyped failure - :func:`http_status`,
:func:`is_unreached`, :func:`is_vendor_outage` - behind the router's verdict
and the boundaries that classify their own transport failures;
:func:`generic_failure_words` and :func:`failure_account`, the one vocabulary
for the slots the model reads (#171, #172); :func:`raise_for_http_status` and
the two JSON decoders, the status decisions a vendor's request boundary makes
in a fixed order (#142); and :func:`wiring_gap`, which says by type that a
failure is this project's own and not the vendor's (#219).

Moved out of ``utils`` on 2026-10-21 (dataflows refactor plan 2026-10-16,
D4); ``utils`` re-exports the names it imports from here for its legacy
readers.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Iterator

import requests

from .errors import (
    UnsupportedIndicatorError,
    VendorError,
    VendorLibraryError,
    VendorUnavailableError,
    WiringGapError,
)
from .untrusted import MAX_UNTRUSTED_CHARS, sanitize_untrusted


def http_status(exc: BaseException) -> int | None:
    """The HTTP status an ``HTTPError`` carries, or ``None`` for any other error.

    Both transport libraries attach the response: curl_cffi's
    ``raise_for_status`` builds ``HTTPError(msg, 0, response)`` and requests'
    sets ``.response`` the same way (measured, curl_cffi 0.15.0). The one
    library-neutral way to read a status off an exception — the two
    ``HTTPError`` classes share no base below ``OSError``. ``None`` too for
    a status of 0: curl_cffi attaches a ``Response`` to its transport
    failures as well (a ``Timeout``, a ``ConnectionError``), with
    ``status_code == 0`` because nothing answered (measured), and a caller
    asking "did the vendor answer a status?" must hear no.
    """
    return getattr(getattr(exc, "response", None), "status_code", None) or None


def is_unreached(e: BaseException) -> bool:
    """Whether an untyped failure means the vendor was never reached at all.

    A transport exception with no status to read: every ``requests`` and
    curl_cffi failure is an ``OSError`` (curl_cffi attaches a status-0
    response to its transport failures, which ``http_status`` reads as
    none). The ``ValueError``-flavoured requests exceptions (MissingSchema,
    InvalidURL, a JSONDecodeError) are a bug or an answer, and a
    ``requests.HTTPError`` is an answer even with its response missing.
    ``TooManyRedirects`` and ``RetryError`` read as unreached too, although
    the vendor did answer (#172): a redirect loop or a spent retry is an
    endpoint that has moved or is blocked, closer to down than to an
    answer about this request, and no third class is drawn for them — so
    every caller below, the router's verdict included, reads them as
    down (#217). ``RetryError`` carries no
    response and reads so on its own; ``TooManyRedirects`` carries the last
    3xx it followed (measured, requests 2.34), so it is named here — read
    off its status it would be an answer, and a redirect loop would land
    on a boundary's structural lane (#217). The one definition behind
    ``is_vendor_outage``'s unreached branch, ``generic_failure_words``'
    "could not be reached", and the boundaries that classify their own
    transport failures (Farside, Fear & Greed, SoSoValue's ``_request``).
    """
    if isinstance(e, requests.TooManyRedirects):
        return True
    return (
        isinstance(e, OSError)
        and not isinstance(e, (ValueError, requests.HTTPError))
        and http_status(e) is None
    )


def is_vendor_outage(e: BaseException) -> bool:
    """Whether an untyped failure means the vendor was DOWN, not answering about the request.

    The one predicate behind the router's no-data verdict — a fallback's
    "no data" is unconfirmed when a vendor in the chain was down (#142).
    Judged by the status the exception carries, not by its library: an
    unreached vendor (``is_unreached``, which names the one exception that
    carries a status yet reads as unreached, a redirect loop's) is down,
    and an answered status can
    say as much — a 5xx, or a 401/403 refusing this client (what the
    yfinance window lets out raw for exactly that reason). Any other
    status is the vendor answering about this request (the 404 a boundary
    leaves alone), as is a ``requests.HTTPError`` with no status to read.
    A typed ``VendorError`` is never judged here — its type already says
    what it is — so it reads as ``False``. The boundaries that classify
    their own transport failures use ``is_unreached`` rather than this:
    they type a 5xx themselves through ``raise_for_http_status``, and a
    4xx they left alone — Farside's Cloudflare 403 — is by their decision
    the vendor answering, not an outage (#170).
    """
    if is_unreached(e):
        return True
    if not isinstance(e, OSError) or isinstance(e, ValueError):
        return False
    status = http_status(e)
    return status is not None and (status >= 500 or status in (401, 403))


def generic_failure_words(e: BaseException) -> str:
    """The words for an untyped vendor failure, by status and class — never its text.

    One vocabulary for every slot the model reads — the router's no-data
    outage clause and optional parenthesis, and the typed messages the
    boundaries that own their transport handling build when a request
    fails below their own taxonomy — so one 503 reads "answered HTTP 503"
    everywhere rather than one site saying ``HTTPError: HTTP 503``. The
    wire first (``is_unreached``, which also names the one exception that
    carries a status yet reads as unreached, a redirect loop's), then the
    status read off the exception the library-neutral way
    (``http_status``): a 401/403 is the vendor refusing this client, any
    other status is its answer; anything else — a library bug — is its
    class name. Never the message: a ``requests`` message quotes the
    request URL, API key included (#171). Which of these count as an
    OUTAGE is ``is_vendor_outage``'s decision, not this function's.
    """
    if is_unreached(e):
        return f"could not be reached: {type(e).__name__}"
    status = http_status(e)
    if status is not None:
        refused = ", refusing this client" if status in (401, 403) else ""
        return f"answered HTTP {status}{refused}"
    return type(e).__name__


def failure_account(e: BaseException, *, limit: int | None = MAX_UNTRUSTED_CHARS) -> str:
    """The words a failed vendor call contributes to a sentinel the model reads.

    Written into two slots of ``route_to_vendor`` — the optional category's
    ``DATA_UNAVAILABLE`` parenthesis (where the router substitutes one fixed
    phrase for a ``VendorNotConfiguredError``, whose message is the
    operator's remedy: ``interface._optional_failure_words``) and the
    no-data sentinel's unconfirmed clause — and by the boundaries that wrap
    a lower failure into a typed message of their own (#203), where a
    not-configured cause keeps its text for the log. A typed vendor error's message was
    authored at the boundary, so it rides along — flattened and capped,
    because not every boundary caps what it quotes (yfinance quotes the
    library's exception, decoded error body included, #172); a remedy a
    boundary appends after the vendor's text is the operator's, in the
    log. So would the caller's own indicator mistake, whose message is the
    remedy, should an optional category ever compute one. Anything else is
    untyped and contributes ``generic_failure_words`` — never its text: a
    ``requests`` message quotes the request URL, API key included (#171).
    The caller's warning log has the full message either way. ``limit`` is
    the cap on a typed message: the router's slots take the default; a
    boundary wrapping a typed cause into a message the router will cap
    again passes ``None``, so the cause's tail — a sweep verdict's
    ``(...; last: ...)`` tally — survives into the boundary's own log line.
    """
    if isinstance(e, VendorLibraryError):
        # Two parts, capped apart: the subject carries the caller's symbol,
        # the detail the library's message, and capped together a long
        # pandas message would swallow the subject (#187). One rule for both
        # slots that render this type: the core chain's report line and the
        # optional category's sentinel.
        return f"{sanitize_untrusted(e.what, limit=limit)}: {sanitize_untrusted(e.detail, limit=limit)}"
    if isinstance(e, (VendorError, UnsupportedIndicatorError, WiringGapError)):
        # A typed error raised with no message would render as "()".
        # ``WiringGapError`` for the same reason as the two above: its text is
        # ours — the prologue's name and the key or table that was missing —
        # and an optional category that degrades over one should say which,
        # not "WiringGapError". Flattened and capped like the rest, since the
        # value it echoes can be the caller's (#219).
        return sanitize_untrusted(e, limit=limit) or type(e).__name__
    return generic_failure_words(e)


@contextlib.contextmanager
def wiring_gap(what: str) -> Iterator[None]:
    """Run a block whose failure is this project's own and not the vendor's.

    The config a deployment did not set, the library attribute a version
    bump moved, a correspondence we keep across two of our own functions
    (Deribit's DVOL dates and readings): usually a getter's prologue, the
    statements it runs before asking the vendor anything, but the test is
    whose breakage it is rather than where it sits. Nine getters used to
    say this by placement — their
    fetch ran inside a ``with library_failure_lane(...)`` and the prologue
    above it, so a yfinance that drops ``cache_get.cache_clear`` failed the
    call rather than freezing global news behind a report (#111, #200),
    while everything under the block left as ``VendorLibraryError``. The
    conversion is the router's now, for every vendor of every routed tool
    rather than the nine getters that happened to carry a broad handler
    (#219) — and the router cannot see where in a getter a failure came
    from. So the block says it by type: this one is never read as the
    vendor's library and never becomes a line of report text — in a core
    category it is raised ahead of one, since our breakage is the half
    somebody can fix (``WiringGapError`` says which endings remain).

    ``what`` names the block in the raise (``global news configuration``),
    since the getter's own subject describes vendor work this failure is not
    about.

    Everything the router tells apart from an untyped failure passes through
    untouched, as it did through the lane this replaces: a taxonomy verdict,
    a transport failure, the caller's own indicator mistake, and a
    ``WiringGapError`` a nested block already named. Nothing inside today's
    blocks can raise any of them — they read dicts, coerce a configured
    number, pair two lists and forget a cache — but this is the kind of
    block that grows a statement, and relabelling a rate
    limit as our wiring would abort the run where the next vendor was owed
    its turn, while relabelling a bad indicator name would cost the whole
    call what should cost one indicator (#117).
    """
    try:
        yield
    except (VendorError, UnsupportedIndicatorError, WiringGapError, OSError):
        raise
    except Exception as e:
        raise WiringGapError(f"{what}: {e}") from e


def raise_for_http_status(response, vendor: str, *, rate_limit: Callable | None = None) -> None:
    """Raise ``VendorUnavailableError`` for a 5xx; otherwise ``response.raise_for_status()``.

    ``rate_limit``, when given, is the boundary's own 429 policy: a callable
    taking the response and RAISING its vendor's rate-limit type. It is
    consulted first, which is the whole point of passing it here rather than
    writing the check above the call - this function types only a 5xx, and
    ``is_unreached`` excludes ``requests.HTTPError``, so a 429 left to the
    ``raise_for_status()`` below reaches a boundary's generic lane and is filed
    as that module's own breakage: an ERROR with a traceback saying the parser
    changed, and a router verdict of "the client needs a fix" for a routine
    throttle no code change heals (#278 shipped exactly that).

    A boundary passing NOTHING keeps today's behaviour exactly, and that is
    deliberate rather than an oversight: several vendors absorb a throttle in
    their own retry or stale-cache lane, and a typed raise from here would walk
    straight past it - the same shape as the bug above, in the other direction.
    Which boundaries decline, and why, is declared and locked in
    ``tests/test_vendor_routing.py`` rather than left to be rediscovered.

    The one status decision at a vendor's request boundary — the 5xx check
    and the library's own raise in one call, in a fixed order, so a boundary
    has nothing of its own to sequence (a bare ``raise_for_status()`` ahead
    of the 5xx check would silently restore what this exists for).
    A 5xx is the vendor being down, and ``route_to_vendor`` reacts to that
    by behaviour — the chain goes on, logged without the traceback its
    generic lane reserves for a bug, and a fallback's "no data" is then
    reported as unconfirmed rather than as a verdict on the symbol — only
    when the type says so. Left to ``raise_for_status()`` the same event is
    a ``requests.HTTPError``, which that generic lane logs as a bug and the
    no-data verdict then hides (#142). Every other status keeps the vendor's
    own handling: a 4xx is the vendor's answer about this client or this
    request (a 401/403 refusal, a 404), which is the vendor's boundary to
    judge, not this helper's, so it leaves as the ``requests.HTTPError`` it
    always did; a status the vendor classifies itself (FRED's 400 with the
    reason in its body, Alpha Vantage's 429) is checked before this is
    called. (The router reads the status off an exception that carries one
    and counts a 401/403 — the vendor refusing this client — as an outage
    for its verdict; see ``route_to_vendor``.)

    The message carries the status code only, never the body: a 5xx body is
    the vendor's error page, and this message travels into a sentinel the
    model reads.
    """
    status = response.status_code
    if status == 429 and rate_limit is not None:
        rate_limit(response)
        # A policy that returns instead of raising would leave the throttle to
        # fall through to ``raise_for_status()`` below - the exact
        # misclassification the parameter exists to prevent, and silently.
        raise WiringGapError(
            f"the {vendor} rate-limit policy returned instead of raising on HTTP 429"
        )
    if status >= 500:
        raise VendorUnavailableError(f"{vendor} answered HTTP {status} without data")
    response.raise_for_status()


def json_body_or_outage(response, vendor: str):
    """Decode ``response`` as JSON, or raise ``VendorUnavailableError`` when it is not.

    For a vendor whose every data answer is JSON: a body that does not decode
    is an error page served with a 2xx (a CDN or WAF interstitial), so the
    vendor answered without data — the same reaction as a 5xx above, where a
    bare ``.json()`` raised a ``ValueError`` into the router's generic lane.
    Not for a vendor that serves data in another shape (Alpha Vantage's CSV,
    Farside's HTML), where a non-JSON body is the data. A body that fails
    below the decoder — ``requests``' ``ContentDecodingError`` on a broken
    gzip stream — is a transport failure by the library's own taxonomy and
    is left to the caller's transport handling.

    :func:`json_bytes_or_outage` is the twin for a boundary that has already
    read the body itself; both raise through :func:`_not_json_outage`, so the
    sentence cannot drift between them.
    """
    try:
        return response.json()
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        raise _not_json_outage(vendor, response.status_code) from e


def json_bytes_or_outage(body: bytes | bytearray, vendor: str, status: int):
    """The streamed twin of :func:`json_body_or_outage`, for a body read by hand.

    A vendor whose body is too large to hand to ``response.json()`` unbounded
    (the Hyperliquid leaderboard is tens of megabytes) reads it in chunks under
    a cap and decodes the bytes itself — but the EVENT is the same one, so the
    verdict and the sentence must be too. A ``bytearray`` is accepted as well as
    ``bytes``: that is what a chunked read accumulates into, and ``json.loads``
    takes it directly, so the caller need not copy the whole buffer to hand it
    over. Split out rather than copied: the
    "answered HTTP N with a body that is not JSON" wording is authored in this
    module alone, and a boundary that hand-copies it drifts on its own (#172
    found the copies, #217-4 locked it).
    """
    try:
        return json.loads(body)
    except ValueError as e:  # json.JSONDecodeError is a ValueError
        raise _not_json_outage(vendor, status) from e


def _not_json_outage(vendor: str, status: int) -> VendorUnavailableError:
    """The one authoring of the not-JSON outage, shared by the two decoders above."""
    return VendorUnavailableError(
        f"{vendor} answered HTTP {status} with a body that is not JSON"
    )
