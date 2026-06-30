"""Map the engine's 5-tier rating into a :class:`PerpTradeDecision`.

This is the deterministic seam (``docs/INTEGRATION.md`` part 1): the unmodified
engine returns a ``final_state`` whose ``final_trade_decision`` carries a rating
(Buy/Overweight/Hold/Underweight/Sell). The adapter diffs the rating's *desired
signed target exposure* ``T`` against the *current* exposure ``C`` (from the live
position) and picks an intent by **rebalancing to the bounded per-tier target** —
size up toward ``T``, trim down, or hold inside a deadband; never pyramid past the
tier target; never flip in one step (``no_direct_flip``).

Shorting is enabled and tier-symmetric (revised decision #11): the bearish tiers
mirror the bullish ones — ``Sell`` is a full short (``-20``) and ``Underweight`` a
mild one (``-10``) — both gated by ``allow_short``, which forces any negative
target to flat when shorting is off. Everything here is a pure function of the
inputs, so the rating->intent table is unit-tested with fixtures, no engine run
required.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any

from ..domains.perp.decision import (
    EntryZone,
    FundingView,
    Intent,
    MarketRegime,
    PerpTradeDecision,
    Urgency,
)
from ..domains.perp.schema import PerpMarketContext, PerpPosition

logger = logging.getLogger(__name__)

# Sentinel for "parse_rating found no recognized tier" — distinct from a real
# rating word so a parse failure can be detected rather than masked as a Hold.
_UNPARSED_RATING = "\x00unparsed"


class RatingSource(str, Enum):
    """How a rating was resolved, stamped into the audit log for post-mortems.

    A ``str`` enum so it compares equal to its value and serialises to that value
    in JSON — existing callers/records that used the plain strings keep working,
    but this is now the single source of truth for the valid source tags.
    """

    EXPLICIT = "explicit"  # a recognized rating tier was found
    PARSE_FALLBACK = "parse_fallback"  # non-empty output but no recognized rating -> Hold
    DEFAULT = "default"  # empty output -> Hold


# Defaults mirror configs/hyperliquid.example.yaml so the adapter is usable even
# if a partial config omits a key.
_DEFAULT_TARGETS = {"buy": 20.0, "overweight": 10.0, "underweight": -10.0, "sell": -20.0}
_DEFAULT_CONFIDENCE = {"full": 0.8, "partial": 0.6, "hold": 0.4}
_DEFAULT_DEADBAND = 2.0
_DEFAULT_ENTRY_BAND = 0.5

# |z| below this is "no meaningful funding tilt" -> neutral, regardless of side.
_FUNDING_NEUTRAL_BAND = 0.5

# Rating -> confidence tier.
_FULL = {"buy", "sell"}
_PARTIAL = {"overweight", "underweight"}

_FIELD_RE = re.compile(r"^\*\*(.+?)\*\*:\s*(.*)$")


def _merge_tiers(
    defaults: dict[str, float], override: dict[str, Any] | None, field_name: str
) -> dict[str, float]:
    """Merge caller tier overrides onto ``defaults``, rejecting unknown keys loudly.

    A typo (e.g. ``buuy``) would otherwise be silently ignored, leaving that tier
    on its default while the intended override is dropped — and an unknown rating
    maps to a 0% (flat) target.
    """
    override = override or {}
    unknown = set(override) - set(defaults)
    if unknown:
        raise ValueError(
            f"unknown tier(s) in adapter.{field_name}: {sorted(unknown)}; "
            f"valid tiers: {sorted(defaults)}"
        )
    # A non-numeric override (e.g. ``buy: "twenty"`` or ``full: null`` from YAML)
    # would otherwise slip through and only blow up later in arithmetic/comparison
    # with a confusing traceback — reject it loudly here, at the config seam.
    # ``bool`` is an ``int`` subclass but is never a valid tier value.
    bad = {
        k: v for k, v in override.items() if isinstance(v, bool) or not isinstance(v, (int, float))
    }
    if bad:
        raise ValueError(f"non-numeric value(s) in adapter.{field_name}: {bad}")
    return {**defaults, **override}


@dataclass(frozen=True)
class AdapterConfig:
    """Typed view of the YAML ``adapter`` block.

    Built once by :meth:`from_dict`, which keeps the existing runtime validation at
    the config seam — unknown tier keys and non-numeric tier values (via
    :func:`_merge_tiers`) plus out-of-range confidence all raise here rather than
    surfacing later as a corrupt order size / gate. This only replaces loose dict
    access inside :class:`DecisionAdapter` with named fields; the adapter's outward
    behaviour is unchanged.
    """

    target_size_pct: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_TARGETS))
    confidence: Mapping[str, float] = field(default_factory=lambda: dict(_DEFAULT_CONFIDENCE))
    allow_short: bool = True
    no_direct_flip: bool = True
    rebalance_deadband_pct: float = _DEFAULT_DEADBAND
    entry_band_pct: float = _DEFAULT_ENTRY_BAND

    def __post_init__(self) -> None:
        # Both are magnitudes (% of net value). A negative deadband makes rebalance()
        # treat every position as outside the band -> perpetual OPEN; a negative entry
        # band yields EntryZone(low > high), which only fails later at decision time.
        # Reject both here at the config seam, like the confidence-range check below.
        if self.rebalance_deadband_pct < 0:
            raise ValueError(
                f"rebalance_deadband_pct must be >= 0, got {self.rebalance_deadband_pct}"
            )
        if self.entry_band_pct < 0:
            raise ValueError(f"entry_band_pct must be >= 0, got {self.entry_band_pct}")
        # ``frozen=True`` blocks reassignment but not in-place mutation of a plain dict
        # (e.g. ``config.target_size_pct["buy"] = 0.0`` silently zeroing long sizing for
        # the rest of the run). Mirror PerpMarketContext.indicators: wrap both tier maps
        # in a read-only proxy over a private copy so the advertised immutability holds.
        object.__setattr__(self, "target_size_pct", MappingProxyType(dict(self.target_size_pct)))
        object.__setattr__(self, "confidence", MappingProxyType(dict(self.confidence)))

    @classmethod
    def from_dict(cls, cfg: dict | None) -> AdapterConfig:
        """Parse a raw ``adapter`` dict into a validated config.

        Tier overrides are merged onto the defaults (unknown keys / non-numeric
        values rejected by :func:`_merge_tiers`), and every optional scalar treats
        a present-but-null YAML value (``None``) like an absent key — falling back
        to the default rather than crashing ``float(None)`` or silently flipping a
        safety boolean via ``bool(None)``.
        """
        cfg = cfg or {}
        targets = _merge_tiers(_DEFAULT_TARGETS, cfg.get("target_size_pct"), "target_size_pct")
        confidence = _merge_tiers(_DEFAULT_CONFIDENCE, cfg.get("confidence"), "confidence")
        # confidence is a 0-1 probability the RiskGate gates on; reject an out-of-range
        # tier here at the seam rather than let it silently corrupt that gate later.
        out_of_range = {k: v for k, v in confidence.items() if not 0.0 <= v <= 1.0}
        if out_of_range:
            raise ValueError(f"confidence tier(s) out of range [0, 1]: {out_of_range}")
        # ``.get(key, default)`` only falls back when the key is *absent*; a key present
        # but blank in YAML parses to ``None``. For the booleans that makes ``bool(None)``
        # silently ``False`` — inverting the default and e.g. disabling shorts or enabling
        # direct flips unintentionally; for the numerics it makes ``float(None)`` raise.
        allow_short_raw = cfg.get("allow_short")
        no_flip_raw = cfg.get("no_direct_flip")
        # No default arg: a missing key and a present-but-null YAML value both come
        # back as None here, and the None-guarded ternary below supplies the default
        # for both — a default in .get() would be dead (only returned when absent).
        deadband_raw = cfg.get("rebalance_deadband_pct")
        entry_band_raw = cfg.get("entry_band_pct")
        return cls(
            target_size_pct=targets,
            confidence=confidence,
            allow_short=bool(allow_short_raw) if allow_short_raw is not None else True,
            no_direct_flip=bool(no_flip_raw) if no_flip_raw is not None else True,
            rebalance_deadband_pct=(
                float(deadband_raw) if deadband_raw is not None else _DEFAULT_DEADBAND
            ),
            entry_band_pct=(
                float(entry_band_raw) if entry_band_raw is not None else _DEFAULT_ENTRY_BAND
            ),
        )


# --------------------------------------------------------------------------
# Pure helpers (no engine, no I/O) — these carry the testable logic.
# --------------------------------------------------------------------------


def extract_fields(markdown: str) -> dict[str, str]:
    """Parse ``**Field**: value`` blocks from rendered agent markdown.

    A field's value runs until the next ``**Field**:`` marker, so multi-line
    summaries/theses are captured whole. Keys are lower-cased field names
    (e.g. ``"executive summary"``, ``"stop loss"``).
    """
    fields: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in markdown.splitlines():
        m = _FIELD_RE.match(line.strip())
        if m:
            if current is not None:
                fields[current] = "\n".join(buf).strip()
            current = m.group(1).strip().lower()
            buf = [m.group(2)]
        elif current is not None:
            buf.append(line)
    if current is not None:
        fields[current] = "\n".join(buf).strip()
    return fields


def _coerce_engine_text(value: object, *, field: str) -> str:
    """Coerce an engine-state field to a string, warning on a type mismatch.

    ``final_state[field]`` is contractually markdown text. A plain ``... or ""``
    would silently coerce a non-string (e.g. the engine stuffing a ``list``/``dict``
    under a schema drift or bug) to ``""`` — indistinguishable from a deliberate
    empty response and an undetectable Hold in the audit log. Warn so the structural
    mismatch is visible, then treat it as absent.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        logger.warning("%s is %s, expected str — treating as empty", field, type(value).__name__)
        return ""
    return value


# A parsed price further than this fraction from the reference mark is treated as a
# bad parse. It catches the gross failure mode — an English-locale parser misreading a
# European-format "63.000,5" as 630005 (a ~1000x error) — while staying loose enough
# never to reject a legitimately wide perp stop (which sits well within 50% of mark).
_PRICE_SANITY_TOLERANCE = Decimal("0.5")


def _parse_price(
    text: str | None, reference_price: Decimal | None = None, field: str = "price"
) -> Decimal | None:
    """Parse a price like ``"$63,000.0"`` / ``"~63000"`` -> ``Decimal("63000.0")``.

    Strips thousands separators and the ``$``/``~`` prefixes the engine commonly
    emits; returns ``None`` on anything still unparseable (e.g. a range), which the
    caller treats as "no price" and falls back to the ATR heuristic.

    Input is assumed to be **English-locale** numeric format: ``,`` is a thousands
    separator and ``.`` is the decimal point. A European-style ``"63.000,5"`` is
    therefore misread (as ``630005``); we deliberately do **not** add locale
    heuristics — they are fragile and the engine prompts/outputs are English. Instead,
    when ``reference_price`` (the mark) is supplied, a parse deviating from it by more
    than :data:`_PRICE_SANITY_TOLERANCE` is treated as absent, so a gross mis-parse
    falls back to the ATR heuristic rather than handing Phase 2 a wildly wrong price.

    ``field`` labels the source (e.g. ``"entry price"`` / ``"stop loss"``) in the
    warnings so an operator reading the log can tell *which* price was dropped when
    both are parsed in the same round.
    """
    if not text:
        return None
    # Drop thousands separators (English-locale ``,``), currency/approx prefixes, and
    # whitespace; the ``.`` decimal point is preserved (see the locale note above).
    cleaned = re.sub(r"[,$~\s]", "", text)
    # The engine sometimes appends the quote-currency ticker (e.g. "63000 USDT");
    # strip a trailing USDT/USDC/USD so the numeric core still parses instead of being
    # dropped to the ATR fallback. USDT/USDC precede USD in the alternation so the
    # longer ticker matches first.
    cleaned = re.sub(r"(?:USDT|USDC|USD)$", "", cleaned, flags=re.IGNORECASE)
    # ...and a leading ticker (e.g. "USD63000", which whitespace removal produces from
    # "USD 63000"); the trailing strip above only matches a suffix, so without this a
    # quote-currency *prefix* would survive into ``Decimal()`` and be dropped to ATR.
    cleaned = re.sub(r"^(?:USDT|USDC|USD)", "", cleaned, flags=re.IGNORECASE)
    try:
        result = Decimal(cleaned)
    except (InvalidOperation, ValueError):
        # A present-but-unparseable price (e.g. a range) silently drops the trader's
        # entry/stop from the decision and the audit record — warn so the loss is
        # visible rather than swallowed.
        logger.warning("could not parse %s from engine output %r — treating as absent", field, text)
        return None
    if not result.is_finite():
        logger.warning(
            "parsed %s from engine output %r is not finite — treating as absent", field, text
        )
        return None
    if result <= 0:
        # A price is always strictly positive; a non-positive value (a sign-flip or a
        # bad parse) would otherwise flow into entry_zone / invalidation_price and
        # hand Phase 2 a negative or zero stop. Drop it rather than propagate it.
        logger.warning(
            "parsed %s from engine output %r is not positive — treating as absent", field, text
        )
        return None
    if reference_price is not None and reference_price > 0:
        deviation = abs(result - reference_price) / reference_price
        if deviation > _PRICE_SANITY_TOLERANCE:
            # Most likely a locale mis-parse (see docstring) or a sign/scale slip: a
            # price this far from the mark is not a usable stop/entry. Drop it to the
            # ATR fallback rather than propagate a wildly wrong level to Phase 2.
            logger.warning(
                "parsed %s %s from engine output %r deviates %.0f%% from mark %s "
                "(tolerance %.0f%%) — treating as a bad parse and falling back",
                field,
                result,
                text,
                float(deviation * 100),
                reference_price,
                float(_PRICE_SANITY_TOLERANCE * 100),
            )
            return None
    return result


def rating_to_target(
    rating: str, current_pct: float, targets: dict[str, float], allow_short: bool
) -> float:
    """Signed target exposure ``T`` (% of net value) for a rating tier.

    ``Hold`` keeps the current exposure (``T = C``) — except it never maintains a
    short while shorting is disabled (the position may be read in from the live
    wallet or opened outside this adapter), in which case it de-risks to flat. The
    bearish tiers (``Sell``, ``Underweight``) carry negative targets and open/keep
    a short only when ``allow_short``; otherwise they de-risk to flat (``T = 0``).
    """
    key = rating.lower()
    if key == "hold":
        if current_pct < 0 and not allow_short:
            return 0.0
        return current_pct
    target = targets.get(key)
    if target is None:
        # An unrecognized rating de-risks to flat — but that silently CLOSEs any open
        # position, so surface it rather than letting a typo'd/novel tier exit a trade
        # unnoticed. (main.py guards engine output with a _UNPARSED sentinel; direct
        # callers do not, so warn here at the seam.)
        logger.warning("unrecognized rating %r — defaulting target exposure to flat (0%%)", rating)
        return 0.0
    if target < 0 and not allow_short:
        return 0.0
    return target


def current_exposure_pct(
    position: PerpPosition | None, account_value: Decimal, mark_price: Decimal
) -> float:
    """Current signed exposure as a % of account net value (+long / -short).

    Uses the position's notional value when the exchange reports it, else
    ``|size| * mark_price``. Returns ``0.0`` for a flat or unknown account.
    """
    if position is None or account_value <= 0:
        return 0.0
    notional = position.position_value
    if notional is None:
        # The exchange-reported notional is missing, so approximate it from the live
        # mark — but warn: for an aged position the mark can drift materially from the
        # true (entry-based) notional the exchange would report, so the exposure % here
        # is an estimate, not an authoritative reading.
        notional = abs(position.size) * mark_price
        logger.warning(
            "position_value missing — approximating notional from |size| * mark_price "
            "(%s * %s); exposure %% is an estimate and may drift on aged positions",
            abs(position.size),
            mark_price,
        )
    exposure = float(abs(notional) / account_value * 100)
    return exposure if position.is_long else -exposure


def _open_intent(target_pct: float) -> Intent:
    """The open-side intent matching a signed target's direction."""
    return Intent.OPEN_LONG if target_pct > 0 else Intent.OPEN_SHORT


def rebalance(
    current_pct: float, target_pct: float, deadband: float, no_direct_flip: bool
) -> tuple[Intent, float | None]:
    """Rebalance ``current -> target`` into an (intent, target_magnitude).

    Implements the decision table in ``docs/INTEGRATION.md``: open/add toward the
    target, reduce, hold inside the deadband, and close-first on an opposite-sign
    target when ``no_direct_flip``. ``target_magnitude`` is the decision's
    ``target_size_pct`` (``None`` for a plain hold, ``0.0`` for a close).

    ``target_magnitude`` is always **unsigned** — the side is carried by the
    ``intent`` plus the existing position's sign, not by this number. For
    ``REDUCE`` on a short it is the absolute size the short should shrink to
    (e.g. ``10.0`` means "trim the short to 10% magnitude"), not ``-10.0``.
    """
    c, t = current_pct, target_pct

    if c == 0:
        if t == 0:
            return Intent.HOLD, None
        return _open_intent(t), abs(t)

    if t == 0:
        return Intent.CLOSE, 0.0

    same_sign = (c > 0) == (t > 0)
    if not same_sign:
        if no_direct_flip:
            return Intent.CLOSE, 0.0  # close to flat now; re-enter next round
        return _open_intent(t), abs(t)

    if abs(t) > abs(c) + deadband:
        return _open_intent(t), abs(t)
    if abs(t) < abs(c) - deadband:
        return Intent.REDUCE, abs(t)
    return Intent.HOLD, None


def confidence_for(rating: str, conf: dict[str, float]) -> float:
    """Rating tier strength -> confidence (Buy/Sell high, OW/UW medium, Hold low)."""
    key = rating.lower()
    if key in _FULL:
        return conf.get("full", _DEFAULT_CONFIDENCE["full"])
    if key in _PARTIAL:
        return conf.get("partial", _DEFAULT_CONFIDENCE["partial"])
    if key != "hold":
        # ``rating_to_target`` warns on an unrecognized tier; mirror it here so a
        # novel rating reaching this seam (via a direct ``build_decision`` call) is
        # not silently scored at the hold tier.
        logger.warning("unrecognized rating %r in confidence_for — defaulting to hold tier", rating)
    return conf.get("hold", _DEFAULT_CONFIDENCE["hold"])


def funding_view_for(zscore: float | None, bias_sign: int) -> FundingView:
    """Deterministic funding view relative to the position's direction.

    Longs pay positive funding, so a high positive z-score is a ``headwind`` for a
    long and ``favorable`` for a short; the sign flips for shorts. ``|z|`` inside
    :data:`_FUNDING_NEUTRAL_BAND` (or no bias / no data) is ``neutral``.
    """
    if zscore is None or bias_sign == 0 or abs(zscore) < _FUNDING_NEUTRAL_BAND:
        return FundingView.NEUTRAL
    # The neutral gate above is strict (|z| < band -> neutral), so anything past
    # it has |z| >= band; use the matching non-strict bounds here so the exact
    # boundary (|z| == band) resolves to a verdict instead of falling through.
    effective = zscore * bias_sign  # >0 means we are on the paying side
    if effective >= _FUNDING_NEUTRAL_BAND:
        return FundingView.HEADWIND
    return FundingView.FAVORABLE


def _bias_sign(intent: Intent, position: PerpPosition | None, target_pct: float) -> int:
    """Directional sign of the resulting/affected position for the funding view."""
    if intent == Intent.OPEN_LONG:
        return 1
    if intent == Intent.OPEN_SHORT:
        return -1
    if position is not None:
        return 1 if position.is_long else -1
    if target_pct > 0:
        return 1
    if target_pct < 0:
        return -1
    return 0


def _urgency_for(regime: MarketRegime) -> Urgency:
    """Phase-1 urgency: a volatile regime is urgent, otherwise low.

    The entry-distance refinement noted in INTEGRATION is deferred; this matches
    every worked example in DESIGN (volatile -> high, else low).
    """
    return Urgency.HIGH if regime == MarketRegime.VOLATILE else Urgency.LOW


def _invalidation_price(
    intent: Intent, stop_loss: Decimal | None, ctx: PerpMarketContext
) -> Decimal | None:
    """Stop-loss from the trader when present, else a deterministic ATR level.

    For a freshly opened position with no trader stop we fall back to
    ``mark -/+ 2*ATR`` (below for longs, above for shorts); otherwise ``None``.
    """
    if stop_loss is not None:
        return stop_loss
    atr = ctx.indicators.get("atr_14")
    if atr is None:
        return None
    mark = ctx.mark_price
    if intent == Intent.OPEN_LONG:
        level = mark - 2 * Decimal(str(atr))
        if level <= 0:
            # ATR >= mark/2 (a very low-priced or extremely volatile coin) drives the
            # long stop to zero or below — a nonsensical price. Drop it rather than
            # serialize a negative stop into the audit log and hand it to Phase 2.
            logger.warning(
                "ATR-derived long stop for %s is non-positive (mark=%s, atr=%s) — no stop",
                ctx.coin,
                mark,
                atr,
            )
            return None
        return level
    if intent == Intent.OPEN_SHORT:
        level = mark + 2 * Decimal(str(atr))
        if level <= mark:
            # atr == 0 (perfectly flat candles) puts the short stop exactly at the
            # mark — an immediate stop-out. Mirror the long-side guard and drop it
            # rather than serialize a degenerate stop into the audit log / Phase 2.
            logger.warning(
                "ATR-derived short stop for %s is at or below mark (mark=%s, atr=%s) — no stop",
                ctx.coin,
                mark,
                atr,
            )
            return None
        return level
    return None


def _key_risks(
    ctx: PerpMarketContext,
    position: PerpPosition | None,
    funding_view: FundingView,
    intent: Intent,
) -> tuple[str, ...]:
    """Deterministic 1-3 risks from the live context signals.

    Thesis/risk-debate mining (per INTEGRATION) is a later refinement; Phase 1
    derives concrete, testable risks from funding / regime / position state so the
    field is never empty for the downstream Risk Manager.
    """
    risks: list[str] = []
    if funding_view == FundingView.HEADWIND:
        z = ctx.funding_zscore_30d
        risks.append(
            f"Funding is a headwind (30d z-score {z:+.2f}) — carry cost rising on this side."
            if z is not None
            else "Funding is a headwind — carry cost rising on this side."
        )
    if ctx.market_regime == MarketRegime.VOLATILE:
        risks.append("Volatile regime — wide ATR raises slippage and whipsaw risk.")
    # An underwater position is only a forward risk if we are keeping/adding to it;
    # a CLOSE/REDUCE decision is the response to that loss, not exposed to it.
    if (
        position is not None
        and position.unrealized_pnl < 0
        and intent not in (Intent.CLOSE, Intent.REDUCE)
    ):
        risks.append(
            f"Open {'long' if position.is_long else 'short'} is underwater "
            f"(uPnL {position.unrealized_pnl})."
        )
    if not risks:
        risks.append(
            f"Standard market risk; {ctx.market_regime.value} regime with no acute funding tilt."
        )
    return tuple(risks[:3])


def resolve_rating(decision_md: str) -> tuple[str, RatingSource]:
    """Parse the engine's final decision markdown into ``(rating, rating_source)``.

    The single rating-parse seam shared by :meth:`DecisionAdapter.decide` and
    ``main.py`` — the ``_UNPARSED_RATING`` sentinel is defined once here so the two
    callers can never drift. ``rating_source`` (a :class:`RatingSource`) records how
    the rating was resolved for the audit log.
    """
    try:
        from tradingagents.agents.utils.rating import parse_rating
    except ImportError as exc:
        # Deferred so --context-only stays import-light, but if the engine package is
        # missing/moved this is the single parse seam every decision flows through —
        # surface a clear cause instead of a generic top-level "unexpected error".
        raise RuntimeError(
            "tradingagents.agents.utils.rating.parse_rating is not importable — "
            "is the tradingagents package installed?"
        ) from exc

    parsed = parse_rating(decision_md, default=_UNPARSED_RATING)
    if parsed != _UNPARSED_RATING:
        return parsed, RatingSource.EXPLICIT
    if decision_md.strip():
        return "Hold", RatingSource.PARSE_FALLBACK
    return "Hold", RatingSource.DEFAULT


# --------------------------------------------------------------------------
# Adapter
# --------------------------------------------------------------------------


class DecisionAdapter:
    """Turn an engine ``final_state`` into a :class:`PerpTradeDecision`.

    Holds the live perp context, the current position, account net value, and the
    ``adapter`` config block. ``to_perp_decision`` is the only public method; the
    heavy lifting is in the module-level pure functions above.
    """

    def __init__(
        self,
        ctx: PerpMarketContext,
        position: PerpPosition | None,
        account_value: Decimal,
        adapter_config: dict | None = None,
    ) -> None:
        self.ctx = ctx
        self.position = position
        self.account_value = account_value
        # Parse the raw YAML ``adapter`` dict into a typed, validated config once;
        # the rest of the adapter reads named fields off ``self.config`` instead of
        # re-deriving them from a loose dict (validation lives in AdapterConfig).
        self.config = AdapterConfig.from_dict(adapter_config)

    def decide(self, final_state: dict) -> tuple[PerpTradeDecision, str, RatingSource]:
        """Map ``final_state`` to ``(decision, rating, rating_source)`` in one parse.

        The single seam ``main.py`` uses: it returns the rating + ``rating_source``
        alongside the decision so the audit log is stamped from the *same* parse,
        rather than ``main`` re-parsing the markdown with its own sentinel (which
        could drift from this one). See :func:`resolve_rating` for the source values.

        Engine output with no recognized rating tier defaults to ``Hold`` — but that
        is most likely a malformed response, not a deliberate hold, and a silent Hold
        can freeze a position that should move. Warn here so a direct caller cannot
        act on a degraded decision unaware.
        """
        # ``final_state`` comes from the external TradingAgents engine. A schema drift
        # or agent crash that hands back ``None``/a non-dict would otherwise raise a
        # bare ``AttributeError`` on ``.get`` below with no domain context; fail loud at
        # the seam instead (mirroring _coerce_engine_text's fail-loud handling).
        if not isinstance(final_state, dict):
            raise ValueError(f"final_state must be a dict, got {type(final_state).__name__}")
        decision_md = _coerce_engine_text(
            final_state.get("final_trade_decision"), field="final_trade_decision"
        )
        trader_md = _coerce_engine_text(
            final_state.get("trader_investment_plan"), field="trader_investment_plan"
        )
        if not trader_md.strip():
            # A blank trader plan means a crashed/empty upstream trader agent: entry
            # price and stop loss are lost, so entry_zone is dropped and the
            # invalidation price silently falls back to the ATR heuristic (or None).
            # Warn so a degraded open isn't passed downstream as if fully specified.
            logger.warning(
                "no trader_investment_plan in final_state — entry price and stop loss "
                "absent; entry_zone will be null and invalidation_price ATR-derived"
            )

        rating, rating_source = resolve_rating(decision_md)
        if rating_source != RatingSource.EXPLICIT:
            logger.warning(
                "no recognized rating in final_trade_decision — defaulting to Hold; "
                "treat this decision as degraded, not a deliberate hold"
            )
        decision_fields = extract_fields(decision_md)
        trader_fields = extract_fields(trader_md)
        decision = self.build_decision(rating, decision_fields, trader_fields)
        return decision, rating, rating_source

    def to_perp_decision(self, final_state: dict) -> PerpTradeDecision:
        """Return only the mapped decision; see :meth:`decide` for the rating seam."""
        decision, _rating, _rating_source = self.decide(final_state)
        return decision

    def build_decision(
        self,
        rating: str,
        decision_fields: dict[str, str],
        trader_fields: dict[str, str],
    ) -> PerpTradeDecision:
        """Pure assembly from a rating + already-parsed markdown fields.

        Split out from :meth:`to_perp_decision` so tests can exercise the full
        rating->decision mapping with synthetic fields and no engine import.
        """
        current = current_exposure_pct(self.position, self.account_value, self.ctx.mark_price)
        target = rating_to_target(
            rating, current, self.config.target_size_pct, self.config.allow_short
        )
        intent, target_magnitude = rebalance(
            current, target, self.config.rebalance_deadband_pct, self.config.no_direct_flip
        )

        entry_price = _parse_price(
            trader_fields.get("entry price"), self.ctx.mark_price, field="entry price"
        )
        stop_loss = _parse_price(
            trader_fields.get("stop loss"), self.ctx.mark_price, field="stop loss"
        )
        urgency = _urgency_for(self.ctx.market_regime)

        entry_zone = self._entry_zone(intent, entry_price, urgency)
        invalidation = _invalidation_price(intent, stop_loss, self.ctx)

        bias = _bias_sign(intent, self.position, target)
        funding_view = funding_view_for(self.ctx.funding_zscore_30d, bias)

        rationale = (
            decision_fields.get("executive summary")
            or decision_fields.get("investment thesis")
            or "No rationale provided by the engine."
        )
        if rationale == "No rationale provided by the engine.":
            # Neither field carried content: the audit record's rationale is a
            # placeholder, not engine output. Warn so a post-mortem reader can tell
            # the forensic field was synthesised rather than reasoned.
            logger.warning(
                "no 'executive summary' or 'investment thesis' in engine output — "
                "rationale defaulting to placeholder"
            )

        return PerpTradeDecision(
            intent=intent,
            confidence=confidence_for(rating, self.config.confidence),
            target_size_pct=target_magnitude,
            entry_zone=entry_zone,
            invalidation_price=invalidation,
            urgency=urgency,
            rationale=rationale,
            key_risks=_key_risks(self.ctx, self.position, funding_view, intent),
            market_regime=self.ctx.market_regime,
            funding_view=funding_view,
        )

    def _entry_zone(
        self, intent: Intent, entry_price: Decimal | None, urgency: Urgency
    ) -> EntryZone | None:
        """Entry band around the trader's entry price — opens only, non-urgent.

        ``null`` when closing/reducing/holding, when urgency is high (take market),
        or when the trader gave no entry price.
        """
        if intent not in (Intent.OPEN_LONG, Intent.OPEN_SHORT):
            return None
        if urgency == Urgency.HIGH or entry_price is None:
            return None
        band = entry_price * Decimal(str(self.config.entry_band_pct)) / 100
        low = entry_price - band
        if low <= 0:
            # entry_band_pct is only validated >= 0 (no upper bound), so a band >= 100%
            # of the entry price drives the lower bound to zero/negative — a degenerate
            # zone. Fall back to a market entry (no band), mirroring the ATR-stop guard
            # in _invalidation_price, rather than let the EntryZone.low > 0 invariant
            # raise uncaught out of build_decision (which would exit 2 with no audit log).
            logger.warning(
                "entry band (%s%% of %s) drives the zone low to %s <= 0 for %s — "
                "no entry zone (market entry)",
                self.config.entry_band_pct,
                entry_price,
                low,
                self.ctx.coin,
            )
            return None
        return EntryZone(low=low, high=entry_price + band)
