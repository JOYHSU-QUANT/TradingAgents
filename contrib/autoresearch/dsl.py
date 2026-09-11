"""The strategy DSL: declarative JSON in, a validated :class:`StrategySpec` out.

A hypothesis is DATA here, never code (plan §3.5). There is no ``eval``, no
``exec``, no expression to compile — a spec is a family, some conditions
built out of the closed feature vocabulary, and a sizing rule, and anything
else is refused by name. That is what makes the search space something the
evaluator can be trusted over: a generator optimising against a scorer will
find whatever the scorer cannot see, so the language it writes in has to be
small enough that there is nothing to find.

The shape, with the optional parts marked::

    {
      "family": "breakout",
      "entry": {"long":  [{"left": "close", "op": ">", "right": "donchian_high_20"}]},
      "exit":  {"long":  [{"left": "rsi_14", "op": "<", "right": {"param": "cool_off"}}],
                "max_bars": 30},                                      # optional
      "filters": [{"left": "regime", "op": "==", "right": "trending"}],  # optional
      "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.25},
      "params": {"cool_off": 45}                                      # optional
    }

``tests/test_dsl.py`` parses that document, because an example a reader
copies has to be one this parser accepts — the first version of it declared a
``params`` entry no condition referenced, which is refused four bullets below
by the rule the same paragraph teaches.

Four refusals are worth naming here, because each one exists to stop a spec
that PARSES from being a hypothesis that means nothing:

- **A future offset.** Offsets count backwards, and ``-1`` is refused with a
  sentence saying the bar has not closed when the decision is made (plan
  §3.6a). The lag syntax exists — ``close`` against ``close[1]`` is a real
  rule — so what is refused is the direction, not the concept.
- **A comparison across units.** ``close > rsi_14`` is well-formed and
  meaningless: a five-digit price against a bounded oscillator is the
  constant ``true`` wearing the clothes of an idea. Both sides must measure
  the same sort of thing (:class:`~contrib.autoresearch.vocabulary.FeatureUnit`).
- **Equality on a computed number.** ``ema_20 == 30000`` is never true, and a
  rule that never fires is scored as a rule that was tried. Equality is for
  the regime label and nothing else.
- **A declaration nothing consumes.** A ``params`` entry no condition refers
  to, and the plan's ``features`` list, are both refused: the feature set IS
  the set of features the conditions name, so a second copy of it can only
  ever be a copy that disagrees.

There is deliberately NO stop-loss and no take-profit. An exit is a feature
comparison or ``max_bars``, and a spec inventing ``"stop_loss": 0.02`` is
refused with the unknown-key sentence. The reason is the evaluator's fill
model: a decision is taken at a bar's close and filled at the next bar's
open (plan §3.6), so a protective exit could only ever trigger at a close —
and an intrabar stop is the single place a backtest most easily cheats,
which is the thing this package exists not to do. The consequence is worth
stating because it is not neutral: the live paper run DOES place stop and
take-profit orders, so a research drawdown is measured under a risk regime
the trader does not run, and is a floor on what the live one would have been
rather than an estimate of it.

``family`` is honest metadata for two of its five values and a checked claim
for the other three — ``regime_filter`` must actually read the regime,
``funding_filter`` must read funding, ``vol_targeting`` must size by
volatility. ``breakout`` and ``mean_reversion`` are intent, and nothing here
measures intent. A later phase keying the plan's per-family trial penalty
(§3.10) off this field should read that sentence first: relabelling a spec is
free.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

from .upstream import MarketRegime, VocabEnum
from .vocabulary import (
    FeatureKind,
    FeatureRef,
    FeatureUnit,
    SeriesSource,
    SpecError,
    parse_feature_name,
    periods_for,
    spec_of,
)

__all__ = [
    "MAX_HOLD_BARS",
    "Condition",
    "Family",
    "Op",
    "Side",
    "Sizing",
    "SizingMode",
    "SpecError",
    "StrategySpec",
    "describe_spec",
    "load_spec",
    "parse_spec",
]


class Family(VocabEnum, noun="strategy family"):
    """The five families plan §3.5 opens with."""

    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    FUNDING_FILTER = "funding_filter"
    REGIME_FILTER = "regime_filter"
    VOL_TARGETING = "vol_targeting"


class Side(VocabEnum, noun="side"):
    LONG = "long"
    SHORT = "short"


class Op(VocabEnum, noun="comparison operator"):
    """Four orderings and two equalities, and they are not interchangeable.

    Which of the two groups a condition may use is decided by the left
    feature's unit: the regime is a label, so it is compared with ``==`` and
    ``!=``; everything else is a computed number, where exact equality is a
    rule that never fires.
    """

    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    EQ = "=="
    NE = "!="


class SizingMode(VocabEnum, noun="sizing mode"):
    FIXED_MARGIN_FRACTION = "fixed_margin_fraction"
    VOL_TARGET = "vol_target"


# Which fields each mode reads. One table, because the same fact was written
# in three places — the type's guard, and the parser's ``allowed`` and
# ``required`` key lists — and a third mode would have to find all three.
_SIZING_FIELDS: Final[dict[SizingMode, tuple[str, ...]]] = {
    SizingMode.FIXED_MARGIN_FRACTION: ("fraction",),
    SizingMode.VOL_TARGET: ("target_vol", "vol_lookback", "max_fraction"),
}


# The one field whose name in a document differs from its name on the type.
_SIZING_KEYS: Final[dict[str, str]] = {"vol_lookback": "lookback"}

_ORDERINGS: Final = (Op.GT, Op.GE, Op.LT, Op.LE)
_EQUALITIES: Final = (Op.EQ, Op.NE)

# What a threshold may be, per unit — the numeric half of the same rule that
# refuses ``close > rsi_14``. Only HARD bounds are listed: a price cannot be
# negative, a return cannot be below -100%, an oscillator is defined on
# 0..100. They are not plausibility hints, because a hint that refuses an
# unusual-but-real hypothesis costs more than the trial it saves — BTC has
# doubled inside thirty days, so ``ret_180 > 1`` has to stay writable.
#
# Which means this catches the IMPOSSIBLE threshold (``rsi_14 > 150``,
# ``ret_6 > -2``) and not the merely wrong one: ``rsi_14 < 0.3``, from a model
# that has met a 0..1 oscillator elsewhere, is inside 0..100 and passes here.
# That mistake is left to the failure summary the hypothesis loop shows the
# model, and it is worth saying so rather than letting this table look like a
# defence it is not.
_UNIT_BOUNDS: Final[dict[FeatureUnit, tuple[float, float]]] = {
    FeatureUnit.PRICE: (0.0, math.inf),  # a price is not negative
    FeatureUnit.PRICE_SPAN: (0.0, math.inf),  # nor is a distance between two
    FeatureUnit.RETURN: (-1.0, math.inf),  # a price cannot fall by more than all of it
    FeatureUnit.OSCILLATOR: (0.0, 100.0),  # the definition of the index itself
}

# The units with NO bound, listed rather than left to a lookup miss. Each one
# is genuinely unbounded, and a first cut of this table did not say so: a
# z-score was capped at ±10, which is a plausibility hint wearing a hard
# bound's sentence. Measured on ordinary hourly funding, one spike scores
# z = 245 — so that cap sat exactly where the ``funding_filter`` family's
# signal lives and refused it as "a threshold it can never cross". A funding
# rate has no definitional limit either; the venue's own cap is a venue
# policy, not arithmetic.
_UNBOUNDED_UNITS: Final = (FeatureUnit.RATE, FeatureUnit.RATE_SUM, FeatureUnit.SCORE)

# Total over the units a NUMBER can be compared against — the regime is the
# one exclusion, since it is compared with a label and never with a threshold.
# Asserted rather than defaulted, because a lookup miss returning ``None``
# would silently exempt the next unit added (and two were added already).
if set(_UNIT_BOUNDS) | set(_UNBOUNDED_UNITS) != set(FeatureUnit) - {FeatureUnit.REGIME}:
    # Raised rather than asserted — see the note in ``vocabulary``. This one
    # is the load-bearing member of the three: ``_check_threshold`` returns
    # early for a unit it cannot classify, so under ``python -O`` a newly
    # added unit would take any threshold at all, silently.
    raise RuntimeError("every non-regime unit is either bounded or deliberately not")

# The share of equity the live path will actually let a decision commit
# (``RiskConfig.max_target_margin_pct`` is 60). Used as the default cap on
# vol-targeted sizing, so a research strategy is not promoted at a size
# RiskGate would clamp; pinned against the live default in
# ``tests/test_pins.py`` rather than imported, because reaching for that
# module would put its import cost on every store command.
LIVE_MARGIN_CAP: Final = 0.6

# What a per-BAR volatility target can plausibly be. ``realized_vol_N`` is a
# per-bar deviation (roughly 0.005 to 0.05 on 4h BTC), and the predictable
# mistake is a model writing an ANNUALISED figure: ``target_vol: 0.60`` then
# pins the size to the cap at every single bar, which is a fixed-size strategy
# wearing a vol-targeting label and being scored as one.
_MIN_TARGET_VOL: Final = 0.001
_MAX_TARGET_VOL: Final = 0.1

# A params key: lower-case, starting with a letter, no surprises. Short
# because the name is shown back in reports beside the number it stands for.
_PARAM_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")

# The longest hold a spec may declare. Bounded so ``max_bars`` cannot become a
# second spelling of "never exit" that the evaluator would have to treat as
# one; 1000 bars is over five months of 4h candles.
MAX_HOLD_BARS: Final = 1000

_TOP_LEVEL: Final = ("family", "entry", "exit", "filters", "sizing", "params")


def _number_text(value: float) -> str:
    """A number as the read-back prints it — shortest form that reads back EXACTLY.

    ``%g`` gives six significant digits, and this rendering exists so an
    operator can check that what was parsed is what they meant. On
    funding-scale thresholds six digits is where a transcription slip hides:
    ``0.0000123456789`` printed as ``1.23457e-05`` looks like a confirmation
    of a number that is not the one the evaluator will use.
    """
    return repr(value)


@dataclass(frozen=True)
class Condition:
    """``left op right`` — the only kind of statement this language has.

    ``right`` is another feature, a number, or a regime label; ``right_param``
    carries the ``params`` key a number came from, so a report can say
    ``rsi_14 < oversold (30)`` rather than losing the name the author gave it.
    """

    left: FeatureRef
    op: Op
    right: FeatureRef | float | MarketRegime
    right_param: str | None = None

    def __post_init__(self) -> None:
        """Check the comparison HERE, not in the parser that happens to build one.

        The parser is one constructor; the evaluator mutating a spec and the
        ledger reloading one are others, and the property "a condition
        compares comparable things" has to hold for every caller or it is not
        a property of a condition at all. ``FeatureRef`` already works this
        way for periods and offsets — this is the same rule one level up.

        Messages carry no path, because a condition does not know where it
        sits in a document; :func:`_condition` prefixes the one it built.
        """
        if isinstance(self.right, int) and not isinstance(self.right, bool):
            # ``json.loads`` yields an ``int`` for ``30`` and a ``float`` for
            # ``30.0``, and a threshold that is one or the other by accident
            # renders two ways in the read-back and answers the guard below two
            # ways. Narrowed once, here, so everything downstream sees one type
            # — THROUGH the shared guard, because a bare ``float()`` on a
            # 400-digit integer is the overflow that guard exists to catch,
            # and doing the conversion by hand put it back in front of it.
            object.__setattr__(self, "right", _require_number(self.right, "a threshold"))
        if self.right_param is not None and not isinstance(self.right, float):
            raise SpecError(
                f"right_param {self.right_param!r} names the parameter a NUMBER came "
                f"from, and this condition compares against {self.right!r}"
            )
        _check_comparison(self.left, self.op, self.right, named=self.right_param)

    def __str__(self) -> str:
        if isinstance(self.right, FeatureRef):
            right = str(self.right)
        elif isinstance(self.right, MarketRegime):
            right = self.right.value
        elif self.right_param is None:
            right = _number_text(self.right)
        else:
            right = f"{self.right_param} ({_number_text(self.right)})"
        return f"{self.left} {self.op.value} {right}"

    @property
    def refs(self) -> tuple[FeatureRef, ...]:
        return (self.left, self.right) if isinstance(self.right, FeatureRef) else (self.left,)


@dataclass(frozen=True)
class Sizing:
    """How much of the account a firing rule asks for.

    Two modes, and each carries only its own fields — a ``fraction`` on a
    ``vol_target`` spec is refused rather than ignored, because a knob read by
    nothing is a knob whose author believes it is doing something.
    """

    mode: SizingMode
    fraction: float | None = None  # fixed_margin_fraction
    target_vol: float | None = None  # vol_target: the per-BAR return deviation aimed at
    vol_lookback: int | None = None  # vol_target: which realized_vol_N measures it
    max_fraction: float | None = None  # vol_target: the cap on what it may ask for

    def __post_init__(self) -> None:
        """One mode, its own fields, and nothing belonging to the other one.

        Without this the docstring above was a description of what
        :func:`_parse_sizing` happened to do: ``Sizing(mode=VOL_TARGET,
        fraction=0.25)`` was constructible, and so was a fixed-fraction sizing
        with no fraction at all — whose ``__str__`` raises and whose share of
        the account is ``None``. Worse, ``refs`` builds a ``FeatureRef`` out of
        ``vol_lookback``, so a half-built vol-target made a PROPERTY raise a
        parser error.
        """
        wanted = _SIZING_FIELDS[self.mode]
        for name in sorted({field for row in _SIZING_FIELDS.values() for field in row}):
            value = getattr(self, name)
            # Named by the key a DOCUMENT uses, since that is the audience for
            # most of these sentences: told it "needs 'vol_lookback'", a model
            # writes that key and is told back that no such key exists.
            spelled = _SIZING_KEYS.get(name, name)
            if name in wanted and value is None:
                raise SpecError(f"{self.mode.value} sizing needs {spelled!r}")
            if name not in wanted and value is not None:
                raise SpecError(
                    f"{self.mode.value} sizing does not read {spelled!r}, got {value!r} — a "
                    f"knob nothing reads looks like a knob that is working"
                )
        # Each number is NARROWED, not merely checked: ``_require_number``
        # returns the float it validated, and dropping that return left a
        # code-built ``Sizing(fraction=1)`` holding an ``int`` where a parsed
        # one holds ``1.0``. That is the drift ``Condition`` narrows int to
        # float to prevent, at the same seam these guards exist for — the
        # evaluator rewriting a spec, the ledger reloading one.
        if self.mode is SizingMode.FIXED_MARGIN_FRACTION:
            object.__setattr__(self, "fraction", _require_fraction(self.fraction, "fraction"))
            return
        object.__setattr__(
            self, "max_fraction", _require_fraction(self.max_fraction, "max_fraction")
        )
        object.__setattr__(self, "target_vol", _require_number(self.target_vol, "target_vol"))
        if not _MIN_TARGET_VOL <= self.target_vol <= _MAX_TARGET_VOL:
            raise SpecError(
                f"target_vol {self.target_vol:g} is outside {_MIN_TARGET_VOL}..{_MAX_TARGET_VOL}; "
                f"it is a PER-BAR return deviation, the same quantity realized_vol_N measures "
                f"(roughly 0.005 to 0.05 on 4h BTC), not an annualised figure"
            )
        # The lookback IS a feature reference, so it is validated by BUILDING
        # one and the vocabulary owns "is this a legal period", int-ness
        # included. A membership test read as equivalent and was not:
        # ``10.0 in (10, 20, 50)`` is true, so a float lookback passed here and
        # ``refs`` then raised from a property — the very failure this guard
        # was added to prevent.
        try:
            FeatureRef(FeatureKind.REALIZED_VOL, self.vol_lookback)
        except SpecError as exc:
            raise SpecError(
                f"vol_lookback: volatility is measured by realized_vol_N, so the lookback has "
                f"to be one of {list(periods_for(FeatureKind.REALIZED_VOL))} bars, got "
                f"{self.vol_lookback!r}"
            ) from exc

    @property
    def refs(self) -> tuple[FeatureRef, ...]:
        """The features sizing itself needs, so the frame computes them too."""
        if self.mode is SizingMode.VOL_TARGET:
            return (FeatureRef(FeatureKind.REALIZED_VOL, self.vol_lookback),)
        return ()

    def __str__(self) -> str:
        if self.mode is SizingMode.FIXED_MARGIN_FRACTION:
            return f"{self.mode.value}: {_number_text(self.fraction)} of equity as margin"
        return (
            f"{self.mode.value}: aim at a per-bar deviation of "
            f"{_number_text(self.target_vol)} measured by realized_vol_{self.vol_lookback}, "
            f"never above {_number_text(self.max_fraction)} of equity as margin"
        )


@dataclass(frozen=True)
class StrategySpec:
    """One parsed hypothesis. Every field here was checked; nothing was defaulted silently."""

    family: Family
    entry_long: tuple[Condition, ...]
    entry_short: tuple[Condition, ...]
    exit_long: tuple[Condition, ...]
    exit_short: tuple[Condition, ...]
    filters: tuple[Condition, ...]
    sizing: Sizing
    max_bars: int | None = None
    # The author's named knobs, in declaration order. A tuple rather than a
    # mapping for two reasons: a "frozen" spec holding a live dict is not
    # frozen, and a dict field makes ``hash(spec)`` raise on a type that
    # advertises hashability — which is the first thing a ledger deduplicating
    # identical hypotheses (plan PR A4) will reach for. ``dict(spec.params)``
    # is the mapping when one is wanted.
    params: tuple[tuple[str, float], ...] = ()

    def __post_init__(self) -> None:
        _check_structure(self)

    def entries(self, side: Side) -> tuple[Condition, ...]:
        return self.entry_long if side is Side.LONG else self.entry_short

    def exits(self, side: Side) -> tuple[Condition, ...]:
        return self.exit_long if side is Side.LONG else self.exit_short

    @property
    def conditions(self) -> tuple[Condition, ...]:
        return (
            self.filters + self.entry_long + self.entry_short + self.exit_long + self.exit_short
        )

    @property
    def features(self) -> tuple[FeatureRef, ...]:
        """Every feature this spec needs, derived — never declared.

        Derived is the whole point: the plan's spec shape carries a
        ``features`` list, and the parser refuses it, because a list beside
        the conditions is a second statement of the same fact. Under-declared,
        it would be a rule reading something the evaluator never computed;
        over-declared, a warm-up longer than the spec actually needs. Sizing
        contributes too — ``vol_target`` reads a ``realized_vol`` the
        conditions may never mention.
        """
        found = [ref for condition in self.conditions for ref in condition.refs]
        found += list(self.sizing.refs)
        order = {kind: index for index, kind in enumerate(FeatureKind)}
        unique = dict.fromkeys(found)
        return tuple(
            sorted(unique, key=lambda ref: (order[ref.kind], ref.period or 0, ref.offset))
        )


# -- parsing ---------------------------------------------------------------


def load_spec(text: str) -> StrategySpec:
    """Parse JSON text into a spec, naming a decode failure as a spec failure.

    The hypothesis loop (plan PR B1) gets text back from a model, and a model
    that emits a trailing comma has made the same class of mistake as one that
    invents a feature: the trial is spent either way, and the note it is shown
    next round should read the same. ``parse_constant`` is what closes JSON's
    non-standard ``NaN`` / ``Infinity`` literals, which would otherwise arrive
    as floats no threshold check could describe.
    """
    try:
        payload = json.loads(
            text, parse_constant=_refuse_json_constant, object_pairs_hook=_object_without_repeats
        )
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec is not valid JSON: {exc}") from exc
    except RecursionError as exc:
        # ``json`` recurses once per nesting level, and a few thousand of them
        # ended the round with a traceback rather than with a refusal the loop
        # can show the model. Nothing legal here is nested more than five deep.
        raise SpecError("spec is nested too deeply to read") from exc
    return parse_spec(payload)


def _object_without_repeats(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Refuse a repeated key instead of letting the last one win.

    ``json.loads`` resolves ``{"op": ">", "op": "<"}`` to the second before any
    check here can see the first, so a model that emits both gets the OPPOSITE
    rule scored, silently. That is the same argument :func:`_check_keys` makes
    about an unknown key — a key whose author believes it is doing something —
    one layer lower, where the loss happens.
    """
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise SpecError(
                f"the key {key!r} appears twice in the same object; JSON keeps only the "
                f"last, so the rule that would be scored is not the one written"
            )
        seen[key] = value
    return seen


def _refuse_json_constant(name: str) -> float:
    raise SpecError(
        f"spec contains the JSON extension {name!r}; thresholds must be ordinary finite numbers"
    )


def parse_spec(payload: object) -> StrategySpec:
    """Validate one decoded spec, or raise :class:`SpecError` naming the path and the fix."""
    body = _mapping(payload, "spec")
    if "features" in body:
        raise SpecError(
            "spec.features: the feature set is derived from the conditions, not declared — "
            "remove the key. A declared list can only agree or disagree with the rules, and "
            "the disagreement is silent: too few and a rule reads a feature nothing computed, "
            "too many and the spec warms up longer than it needs to."
        )
    _check_keys(body, "spec", allowed=_TOP_LEVEL, required=("family", "entry", "sizing"))
    family = _enum(Family, body["family"], "spec.family")
    params = _parse_params(body.get("params"), "spec.params")

    entry = _mapping(body["entry"], "spec.entry")
    _check_keys(entry, "spec.entry", allowed=tuple(side.value for side in Side))
    entries = {
        side: _conditions(entry.get(side.value), f"spec.entry.{side.value}", params)
        for side in Side
    }

    exit_block = _mapping(body.get("exit", {}), "spec.exit")
    _check_keys(exit_block, "spec.exit", allowed=(*(side.value for side in Side), "max_bars"))
    exits = {
        side: _conditions(exit_block.get(side.value), f"spec.exit.{side.value}", params)
        for side in Side
    }
    max_bars = (
        None
        if exit_block.get("max_bars") is None
        else _whole(exit_block["max_bars"], "spec.exit.max_bars", low=1, high=MAX_HOLD_BARS)
    )

    return StrategySpec(
        family=family,
        entry_long=entries[Side.LONG],
        entry_short=entries[Side.SHORT],
        exit_long=exits[Side.LONG],
        exit_short=exits[Side.SHORT],
        filters=_conditions(body.get("filters"), "spec.filters", params),
        sizing=_parse_sizing(body["sizing"], "spec.sizing"),
        max_bars=max_bars,
        params=tuple(params.items()),
    )


def _check_structure(spec: StrategySpec) -> None:
    """The rules that are about the spec as a WHOLE rather than about one value.

    Called from ``StrategySpec.__post_init__``, so a spec assembled in code —
    by the evaluator, by a ledger reloading one — meets the same rules the
    parser applies. Which parameters are USED is derived from the conditions
    rather than tracked during parsing, for the same reason the feature set is:
    a second record of one fact is a record that can disagree.
    """
    if not spec.entry_long and not spec.entry_short:
        raise SpecError(
            "spec.entry: a hypothesis has to be able to take a position — give at least one "
            "of 'long' or 'short' a condition"
        )
    for side in Side:
        if spec.exits(side) and not spec.entries(side):
            raise SpecError(
                f"spec.exit.{side.value}: there are no {side.value} entries, so these exit "
                f"conditions can never be reached — remove them, or add the entry they belong to"
            )
    declared: dict[str, float] = {}
    for name, value in spec.params:
        # Checked here because a spec built in code does not pass the parser,
        # and a parameter is shown back beside the number it stands for: a
        # param carrying a string while its condition carries the real
        # threshold is a report that disagrees with what was measured.
        if not isinstance(name, str) or not _PARAM_NAME.fullmatch(name):
            raise SpecError(f"spec.params: {name!r} is not a usable parameter name")
        if name in declared:
            # The duplicate-key refusal one layer up, made again here: a
            # mapping keeps the last and a report shows the last, so two
            # declarations of one knob are two specs wearing one name.
            raise SpecError(f"spec.params: {name!r} is declared twice")
        declared[name] = _require_number(value, f"spec.params.{name}")
    for condition in spec.conditions:
        name = condition.right_param
        if name is None:
            continue
        if name not in declared:
            raise SpecError(
                f"spec.params: no parameter named {name!r} is declared, but a condition "
                f"({condition}) says its threshold came from one"
            )
        if declared[name] != condition.right:
            # The knob and the number it stands for are shown together in every
            # report, so they cannot be two records of one fact. A later phase
            # perturbing a threshold by rewriting ``params`` would otherwise
            # score the UNCHANGED rule and file it under the new value — a
            # strategy never tried, recorded as tried.
            raise SpecError(
                f"spec.params: {name!r} is declared as {declared[name]!r} while the "
                f"condition using it compares against {condition.right!r}"
            )
    used = {condition.right_param for condition in spec.conditions} - {None}
    if spec.max_bars is not None:
        # Bounded here as well as at the parser, for the reason the bound
        # exists: ``max_bars`` must not become a second spelling of "never
        # exit", and a later phase mutating a hold length does not go through
        # the parser. Its TYPE is checked for the same reason the thresholds'
        # is — ``12.5`` bars is not a hold and ``True`` is not one either,
        # while a string left the comparison as a ``TypeError``, outside the
        # refusal lane entirely.
        if isinstance(spec.max_bars, bool) or not isinstance(spec.max_bars, int):
            raise SpecError(
                f"spec.exit.max_bars: a hold is a whole number of bars, got {spec.max_bars!r}"
            )
        if not 1 <= spec.max_bars <= MAX_HOLD_BARS:
            raise SpecError(
                f"spec.exit.max_bars: a hold is between 1 and {MAX_HOLD_BARS} bars, got "
                f"{spec.max_bars}"
            )
    unused = sorted(set(declared) - used)
    if unused:
        raise SpecError(
            f"spec.params: {unused} are declared and referred to by no condition. A knob "
            f"nothing reads looks like a knob that is working — reference it with "
            f'{{"param": "{unused[0]}"}} or drop it.'
        )
    _check_family(spec)


def _check_family(spec: StrategySpec) -> None:
    """The three family claims that are structural, and can therefore be checked.

    Deliberately three of five. ``breakout`` and ``mean_reversion`` describe
    what an author MEANT — the same conditions can be either — and a check
    invented for them would refuse honest specs while proving nothing. The
    module docstring says so where a reader of ``family`` will meet it.
    """
    kinds = {ref.kind for ref in spec.features}
    if spec.family is Family.REGIME_FILTER and FeatureKind.REGIME not in kinds:
        raise SpecError(
            "spec.family: 'regime_filter' names a rule that reads the regime, and no "
            "condition here refers to 'regime'"
        )
    if spec.family is Family.FUNDING_FILTER and not any(
        spec_of(kind).source is SeriesSource.FUNDING for kind in kinds
    ):
        raise SpecError(
            "spec.family: 'funding_filter' names a rule that reads funding, and no condition "
            "here refers to a funding feature"
        )
    if spec.family is Family.VOL_TARGETING and spec.sizing.mode is not SizingMode.VOL_TARGET:
        raise SpecError(
            "spec.family: 'vol_targeting' names a rule that sizes by volatility, but "
            f"spec.sizing.mode is {spec.sizing.mode.value!r}"
        )


def _parse_params(value: object, path: str) -> dict[str, float]:
    if value is None:
        return {}
    body = _mapping(value, path)
    out = {}
    for name, number in body.items():
        if not _PARAM_NAME.fullmatch(name):
            raise SpecError(
                f"{path}: {name!r} is not a usable parameter name (lower-case letters, "
                f"digits and underscores, starting with a letter)"
            )
        out[name] = _number(number, f"{path}.{name}")
    return out


def _parse_sizing(value: object, path: str) -> Sizing:
    """Shape and keys here; every RANGE and every cross-field rule on the type.

    The split is the same one the conditions use: what a JSON document may
    contain is this function's business, and what a sizing rule may MEAN is
    ``Sizing.__post_init__``'s, so a caller assembling one in code cannot get a
    sizing the parser would have refused.
    """
    body = _mapping(value, path)
    if "mode" not in body:
        raise SpecError(f"{path}: needs a 'mode' — one of {[mode.value for mode in SizingMode]}")
    mode = _enum(SizingMode, body["mode"], f"{path}.mode")
    if mode is SizingMode.FIXED_MARGIN_FRACTION:
        _check_keys(body, path, allowed=("mode", "fraction"), required=("mode", "fraction"))
        fields = {"fraction": _number(body["fraction"], f"{path}.fraction")}
    else:
        # ``lookback``, not ``vol_lookback``: the document's own spelling, which
        # is why these key lists are not simply the field names.
        _check_keys(
            body,
            path,
            allowed=("mode", "target_vol", "lookback", "max_fraction"),
            required=("mode", "target_vol", "lookback"),
        )
        fields = {
            "target_vol": _number(body["target_vol"], f"{path}.target_vol"),
            "vol_lookback": body["lookback"],
            # Defaulted rather than required, and defaulted to the live clamp
            # rather than to all of the account: a spec promoted at a size
            # RiskGate would cut is a spec measured at a size it will never run
            # at.
            "max_fraction": (
                LIVE_MARGIN_CAP
                if body.get("max_fraction") is None
                else _number(body["max_fraction"], f"{path}.max_fraction")
            ),
        }
    with _prefixed(path):
        return Sizing(mode=mode, **fields)


def _conditions(
    value: object, path: str, params: Mapping[str, float]
) -> tuple[Condition, ...]:
    if value is None:
        return ()
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, Sequence):
        raise SpecError(f"{path}: expected a list of conditions, got {type(value).__name__}")
    return tuple(
        _condition(item, f"{path}[{index}]", params) for index, item in enumerate(value)
    )


def _condition(value: object, path: str, params: Mapping[str, float]) -> Condition:
    body = _mapping(value, path)
    parts = ("left", "op", "right")
    _check_keys(body, path, allowed=parts, required=parts)
    left = _ref(body["left"], f"{path}.left")
    op = _enum(Op, body["op"], f"{path}.op")
    right, param = _right(body["right"], f"{path}.right", left, params)
    # The condition checks itself, so every constructor meets the rule — and
    # therefore cannot know where in the document it sits.
    with _prefixed(path):
        return Condition(left=left, op=op, right=right, right_param=param)


def _check_comparison(
    left: FeatureRef,
    op: Op,
    right: FeatureRef | float | MarketRegime,
    *,
    named: str | None = None,
) -> None:
    """Is this comparison one that could mean something? Sentences carry no path."""
    if left.unit is FeatureUnit.REGIME:
        if op not in _EQUALITIES:
            raise SpecError(
                f"the regime is a label, so it is compared with "
                f"{[member.value for member in _EQUALITIES]}, not {op.value!r}"
            )
        if not isinstance(right, MarketRegime):
            raise SpecError(f"the regime is compared with a regime, got {right!r}")
        return
    if isinstance(right, MarketRegime):
        raise SpecError(f"{left} is a number and {right.value!r} is a regime label")
    if op in _EQUALITIES:
        raise SpecError(
            f"{op.value!r} on a computed number is a rule that never fires — "
            f"{left} is a float. Use one of {[member.value for member in _ORDERINGS]}, or "
            f"compare the regime, which is the one label in this vocabulary."
        )
    if isinstance(right, FeatureRef):
        if right == left:
            # Same kind, same period, same offset: ``close > close`` never
            # fires and ``close >= close`` always does, and both satisfy every
            # other guard — same unit, an ordering operator, no threshold to
            # bound. Worse than a wasted trial when the family check is
            # watching: ``funding_rate >= funding_rate`` certifies a
            # ``funding_filter`` whose rule does not depend on funding at all.
            raise SpecError(
                f"{left} is compared with itself, which is a rule that fires at every bar "
                f"or at none. Compare it with another feature, with an offset of itself "
                f"(an object carrying {left.name!r} and an offset), or with a number."
            )
        if right.unit is not left.unit:
            raise SpecError(
                f"{left} is measured in {left.unit.value} and {right} in {right.unit.value}; "
                f"comparing them is well-formed and meaningless. Compare features of the "
                f"same unit, or compare against a number."
            )
        return
    _check_threshold(left, right, named=named)


def _check_threshold(left: FeatureRef, number: float, *, named: str | None = None) -> None:
    """A number compared against ``left``, checked against that feature's own scale.

    Called from :func:`_check_comparison`, which means from
    ``Condition.__post_init__`` — the same seam the cross-unit rule passes,
    rather than from the JSON path alone. A threshold is exactly the field a
    later phase perturbs when it mutates a spec, and ``rsi_14 > 150`` built in
    code is the same rule that never fires as one that was written down.

    The bounds are hard ones (see :data:`_UNIT_BOUNDS`), so what this refuses
    is a threshold that could never be met. It is a narrower guard than the
    cross-unit one: it catches the impossible threshold, not every threshold
    on the wrong scale — and three units have no bound at all, which is said
    where they are listed rather than left to a lookup that finds nothing.
    """
    _require_number(number, "a threshold")
    if left.unit not in _UNIT_BOUNDS:
        return
    low, high = _UNIT_BOUNDS[left.unit]
    if low <= number <= high:
        return
    if high == math.inf:
        limit = f"below {low:g}"
    elif low == -math.inf:
        limit = f"above {high:g}"
    else:
        limit = f"outside {low:g}..{high:g}"
    source = f"{named!r} ({number:g})" if named else f"{number:g}"
    raise SpecError(
        f"{left} is measured in {left.unit.value}, which cannot be {limit}, so {source} is "
        f"a threshold it can never cross. Thresholds are on the feature's own scale — "
        f"`python -m contrib.autoresearch vocab` states each one."
    )


def _right(
    value: object, path: str, left: FeatureRef, params: Mapping[str, float]
) -> tuple[FeatureRef | float | MarketRegime, str | None]:
    """The right-hand side: a regime label, a feature, a number, or a named param."""
    if left.unit is FeatureUnit.REGIME:
        if not isinstance(value, str):
            raise SpecError(
                f"{path}: the regime is compared with one of "
                f"{[member.value for member in MarketRegime]}, got {value!r}"
            )
        return _enum(MarketRegime, value, path), None
    if isinstance(value, Mapping) and "param" in value:
        _check_keys(value, path, allowed=("param",), required=("param",))
        name = value["param"]
        if not isinstance(name, str) or name not in params:
            raise SpecError(
                f"{path}: no parameter named {name!r} is declared in spec.params "
                f"({sorted(params)})"
            )
        return params[name], name
    if isinstance(value, (str, Mapping)):
        return _ref(value, path), None
    return _number(value, path), None


def _require_number(value: object, what: str) -> float:
    """A finite number, refused as a :class:`SpecError` rather than a ``TypeError``.

    THE one numeric guard, reached from both directions: the parser passes the
    document path as ``what``, a type guard passes a noun. It was two guards
    for one round, and they immediately disagreed — the parser's caught the
    huge-integer ``OverflowError`` while the type seam's let it out, which is
    the same escape, at the layer written to close it.

    Three refusals, and none of them is a technicality. ``True`` is not a
    number, because ``isinstance(True, int)`` is true and a threshold of
    ``True`` would read as ``1.0`` — a plausible bound on anything scaled near
    unity. An integer too large for a float is not one either: JSON puts no
    limit on an integer literal, and ``float()`` raises an ``ArithmeticError``,
    which is outside the ``ValueError`` lane every refusal here travels in. And
    a non-finite float is not a number a rule can be written against.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpecError(f"{what}: expected a number, got {value!r}")
    try:
        number = float(value)
    except OverflowError as exc:
        raise SpecError(f"{what}: {value!r} is too large to be a number") from exc
    if not math.isfinite(number):
        raise SpecError(f"{what}: expected a finite number, got {value!r}")
    return number


def _require_fraction(value: object, name: str) -> float:
    """A share of the account: above 0, at most 1. Pathless, for the type guards.

    Returns the narrowed float, like :func:`_require_number` does, so a caller
    can store what was validated rather than the thing it was handed.
    """
    number = _require_number(value, name)
    if not 0 < number <= 1:
        raise SpecError(f"{name} is a fraction of the account, above 0 and at most 1, got {value!r}")
    return number


def _ref(value: object, path: str) -> FeatureRef:
    """A feature reference: a bare name, or a name with an offset in bars.

    Every rule about an offset — whole number, not negative, within the bound
    — belongs to ``FeatureRef`` (plan §3.6a), so this function checks none of
    them and only says WHERE the refused one sits. Re-checking the int-ness
    here put the path on that one sentence and left the other two, the two an
    LLM actually meets, with no way to locate them in the document.
    """
    if not isinstance(value, Mapping):
        with _prefixed(path):
            return FeatureRef(*parse_feature_name(value, path=path))
    _check_keys(value, path, allowed=("feature", "offset"), required=("feature",))
    kind, period = parse_feature_name(value["feature"], path=f"{path}.feature")
    with _prefixed(path):
        return FeatureRef(kind, period, value.get("offset", 0))


# -- small shared checks ---------------------------------------------------


@contextmanager
def _prefixed(path: str) -> Iterator[None]:
    """Say WHERE a pathless refusal happened, once, for every site that needs it.

    The types own their own invariants and therefore cannot know where in a
    document they were built from; the parser knows nothing else. Before this
    existed the three call sites each re-raised in their own way — one of them
    deciding whether to prefix by testing the message for the prefix it was
    about to add.
    """
    try:
        yield
    except SpecError as exc:
        message = str(exc)
        raise SpecError(message if message.startswith(f"{path}:") else f"{path}: {exc}") from exc


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SpecError(f"{path}: expected an object, got {type(value).__name__}")
    bad = [key for key in value if not isinstance(key, str)]
    if bad:
        raise SpecError(f"{path}: keys must be strings, got {bad!r}")
    return value


def _check_keys(
    body: Mapping[str, object],
    path: str,
    *,
    allowed: Sequence[str],
    required: Sequence[str] = (),
) -> None:
    """Unknown keys and missing keys, both named, both refused.

    Unknown keys are refused rather than ignored because of who writes these:
    a model that invents ``"stop_loss": 0.02`` has written a spec whose author
    believes it stops out, and silently dropping the key would score that
    belief as a strategy that was tried.
    """
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise SpecError(f"{path}: unknown key(s) {unknown}; this level accepts {list(allowed)}")
    missing = [key for key in required if key not in body]
    if missing:
        raise SpecError(f"{path}: missing required key(s) {missing}")


def _enum(vocabulary: type[VocabEnum], value: object, path: str):
    if not isinstance(value, str):
        raise SpecError(
            f"{path}: expected one of {[member.value for member in vocabulary]}, got {value!r}"
        )
    # ``VocabEnum`` already names the vocabulary and the choices; the path is
    # the one thing it cannot know.
    with _prefixed(path):
        try:
            return vocabulary(value)
        except ValueError as exc:
            raise SpecError(str(exc)) from exc


def _number(value: object, path: str) -> float:
    """A finite number at ``path``. The path IS the noun the shared guard names."""
    return _require_number(value, path)


def _whole(value: object, path: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{path}: expected a whole number, got {value!r}")
    if not low <= value <= high:
        raise SpecError(f"{path}: expected a whole number between {low} and {high}, got {value}")
    return value


# -- rendering -------------------------------------------------------------


def describe_spec(spec: StrategySpec) -> list[str]:
    """The spec read back as lines — what the ``validate-spec`` command prints.

    Rendered from the PARSED object rather than echoing the input, so what an
    operator sees is what the evaluator will act on: a default that was filled
    in shows up, and so does a condition whose meaning is not what its author
    expected.
    """
    lines = [f"family: {spec.family.value}"]
    for side in Side:
        if spec.entries(side):
            lines.append(f"enter {side.value} when: {_joined(spec.entries(side))}")
    for side in Side:
        if spec.exits(side):
            lines.append(f"exit {side.value} when: {_joined(spec.exits(side))}")
    if spec.max_bars is not None:
        lines.append(f"exit after {spec.max_bars} bars held")
    if spec.filters:
        lines.append(f"only while: {_joined(spec.filters)}")
    lines.append(f"sizing: {spec.sizing}")
    lines.append(f"features used: {', '.join(str(ref) for ref in spec.features)}")
    return lines


def _joined(conditions: Sequence[Condition]) -> str:
    """Conditions are ANDed; the rendering says so rather than leaving it implied."""
    return " AND ".join(str(condition) for condition in conditions)
