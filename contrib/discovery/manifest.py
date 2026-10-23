"""The manifest: one world and one benchmark run over it, as a versioned, frozen record (plan §5).

Everything a world's results depend on is written here once — how the
world was generated, how its history is split, what a trial costs, the
promote penalty, the budget, the candidate pool the Random and Grid arms
draw from, how each arm is configured, and how recovery is scored — so a
number in a report can be traced to the parameters that produced it, and
two worlds can be told apart by their manifests alone.

Two rules of the design are held by TYPES here rather than by discipline:

**The world's kind never reaches a model.** A world's ``id`` is opaque
(:func:`opaque_world_id`): a digest of the seed set and the seed, spelled
so the research store files it unchanged, and derived from NOTHING that
says whether the world is pure noise or has a rule planted in it. It is a
property of :class:`World`, computed from the two fields it is a function
of, so no world can carry another's id. The id is what becomes the
experiment's coin — the one word of the manifest a prompt ever carries
(plan §1 #7) — and ``kind`` and ``planted`` stay in this file.
``tests/test_isolation.py`` reads the prompts to hold that.

**A planted rule is a rule of the language.** ``planted.spec`` is a parsed
:class:`~contrib.autoresearch.dsl.StrategySpec`, read through the research
package's own parser, so a world cannot plant something the loop could not
write (plan §0.3) — and it carries an exit on every side it enters, because
the evaluator closes a position only through an exit rule, ``max_bars`` or
a reversal (plan §1 #11): a rule planted without one would hold forever
and measure nothing about timing.

The frozen values the research package already has — ``CostModel``,
``Penalty`` — are serialised through their own records, not re-described.
Every record here is read the way theirs are (:mod:`.records`): exactly
the keys written, every value checked in ``__post_init__``, a manifest
from another version refused by name rather than read as something it is
not.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from .records import Reader, dumps
from .upstream import (
    DEFAULT_TRAIN_SHARE,
    DEFAULT_VALIDATION_SHARE,
    MAX_HOLD_BARS,
    MIN_INDICATOR_LOOKBACK,
    MS_PER_DAY,
    CostModel,
    Op,
    Penalty,
    Side,
    SpecError,
    StrategySpec,
    VocabEnum,
    parse_feature_name,
    parse_spec,
    spec_to_document,
    studied_interval,
)

__all__ = [
    "MANIFEST_VERSION",
    "Arms",
    "Bars",
    "Budget",
    "Clause",
    "Difficulty",
    "FundingModel",
    "LlmArm",
    "Manifest",
    "ManifestError",
    "Planted",
    "PriceModel",
    "RandomArm",
    "Recovery",
    "Scoring",
    "SearchSpace",
    "SeedSet",
    "Shares",
    "Template",
    "World",
    "WorldKind",
    "opaque_world_id",
]

# Bumped when a key is added, removed or re-read: a manifest of another
# version is refused by name, never read with a default filled in.
MANIFEST_VERSION: Final = 1


class ManifestError(ValueError):
    """The manifest says something this benchmark cannot run, and the sentence says what.

    A ``ValueError`` for the same reason the research package's refusals
    are: a value outside its domain, on the lane a CLI already names.
    """


_read = Reader(ManifestError, "manifest")


class WorldKind(VocabEnum, noun="world kind"):
    """What was generated. Never a model-visible word (see the module docstring)."""

    NULL_V1 = "null-v1"
    CONDITIONAL_V1 = "conditional-v1"


class Difficulty(VocabEnum, noun="planted difficulty"):
    """How much room the oracle has over the promote threshold (plan §4.1), by band."""

    TIGHT = "tight"
    EASY = "easy"
    TRIVIAL = "trivial"


class SeedSet(VocabEnum, noun="seed set"):
    """Development seeds tune the benchmark; test seeds are run once, untuned (plan §4.2)."""

    DEV = "dev"
    TEST = "test"


# -- the opaque id ------------------------------------------------------------

# ``SYN-`` and eight upper-case hex digits. Upper-case because the research
# store files a coin under its ``canonical_coin`` — upper-cased — and the id
# has to come back from the store spelled as the manifest spells it
# (``tests/test_manifest.py`` holds the two spellings equal). Hex cannot
# spell a word, which is the point: nothing in the id can say "null" or
# "conditional" (plan §1 #7).
_ID_DIGITS: Final = 8
_WORLD_ID: Final = re.compile(rf"SYN-[0-9A-F]{{{_ID_DIGITS}}}")
_ID_NAMESPACE: Final = "contrib.discovery world"


def opaque_world_id(seed_set: SeedSet, world_seed: int) -> str:
    """The id a world is known by everywhere, a model's prompt included.

    A digest of the seed set and the seed, and of nothing else: the kind of
    world is not an input, so no reading of the id — not even one that
    brute-forces the small seed space — can recover it. The seed set IS an
    input, because development seed 0 and test seed 0 are two worlds and
    must not be filed under one coin.
    """
    seed = _read.whole(world_seed, "world_seed", minimum=0)
    text = f"{_ID_NAMESPACE} {_read.enum(SeedSet, seed_set, 'seed_set').value} {seed}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"SYN-{digest[:_ID_DIGITS].upper()}"


def _world_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not _WORLD_ID.fullmatch(value):
        raise ManifestError(
            f"{path}: an opaque id is 'SYN-' and {_ID_DIGITS} upper-case hex digits "
            f"(opaque_world_id writes one), got {value!r}"
        )
    return value


# -- the world ---------------------------------------------------------------------


@dataclass(frozen=True)
class Bars:
    """The decision bars generated: their interval, how many, and the warm-up before them.

    ``warmup_bars`` is history generated BEFORE the first bar the split may
    use, sized to the longest reach any feature of the vocabulary has
    (plan §1 #14) — not to the indicator window, which is only the 4h
    indicators' share of it.
    """

    interval: str
    count: int
    warmup_bars: int

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "interval", studied_interval(self.interval))
        except ValueError as exc:
            raise ManifestError(f"world.bars.interval: {exc}") from exc
        _read.whole(self.count, "world.bars.count", minimum=1)
        _read.whole(self.warmup_bars, "world.bars.warmup_bars", minimum=0)

    def to_dict(self) -> dict[str, object]:
        return {"interval": self.interval, "count": self.count, "warmup_bars": self.warmup_bars}

    @classmethod
    def from_dict(cls, payload: object) -> Bars:
        return cls(**_read.exact(payload, ("interval", "count", "warmup_bars"), "world.bars"))


@dataclass(frozen=True)
class PriceModel:
    """The log-price walk: ``vol`` per bar (not annualised) and ``drift`` per bar (plan §4.1)."""

    vol: float
    drift: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "vol", _read.amount(self.vol, "world.price.vol", positive=True))
        object.__setattr__(self, "drift", _read.number(self.drift, "world.price.drift"))

    def to_dict(self) -> dict[str, float]:
        return {"vol": self.vol, "drift": self.drift}

    @classmethod
    def from_dict(cls, payload: object) -> PriceModel:
        return cls(**_read.exact(payload, ("vol", "drift"), "world.price"))


@dataclass(frozen=True)
class FundingModel:
    """Exogenous funding: i.i.d. zero-mean with ``sigma`` > 0 (plan §1 #12).

    Strictly positive, because a constant-zero funding series has no
    variance for ``funding_zscore`` to standardise by, and the feature then
    has no value on any bar.
    """

    sigma: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "sigma", _read.amount(self.sigma, "world.funding.sigma", positive=True)
        )

    def to_dict(self) -> dict[str, float]:
        return {"sigma": self.sigma}

    @classmethod
    def from_dict(cls, payload: object) -> FundingModel:
        return cls(**_read.exact(payload, ("sigma",), "world.funding"))


@dataclass(frozen=True)
class Planted:
    """The rule a conditional world carries, and the effect it adds when it fires.

    ``beta`` is the per-bar drift added, IN FAVOUR OF THE PLANTED SIDE'S
    POSITION, to the bars a position opened on the rule would hold (plan
    §4.1): a short-side plant adds a negative price drift. Strictly
    positive, because a conditional world with nothing to find is a null
    world filed under the wrong kind — the cross-field rule ``World``
    holds, one level down. Its band (``difficulty``) is decided on
    development seeds and recorded so a report can group worlds by it.
    """

    spec: StrategySpec
    beta: float
    difficulty: Difficulty

    def __post_init__(self) -> None:
        if not isinstance(self.spec, StrategySpec):
            raise ManifestError(
                f"world.planted.spec: a planted rule is a parsed StrategySpec, got {self.spec!r}"
            )
        # Per side: a two-sided rule with an exit on one side only would hold
        # the other side until a reversal, which on a world where the first
        # side never fires is forever.
        unclosed = [
            side.value
            for side in Side
            if self.spec.entries(side) and not self.spec.exits(side)
        ]
        if self.spec.max_bars is None and unclosed:
            raise ManifestError(
                f"world.planted.spec: a planted rule needs an exit on every side it enters "
                f"({', '.join(unclosed)} has none) — an exit rule or max_bars. The evaluator "
                f"closes a position only through those (or a reversal), so a rule without one "
                f"holds forever and measures nothing about timing"
            )
        object.__setattr__(
            self, "beta", _read.amount(self.beta, "world.planted.beta", positive=True)
        )
        object.__setattr__(
            self,
            "difficulty",
            _read.enum(Difficulty, self.difficulty, "world.planted.difficulty"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "spec": spec_to_document(self.spec),
            "beta": self.beta,
            "difficulty": self.difficulty.value,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Planted:
        body = _read.exact(payload, ("spec", "beta", "difficulty"), "world.planted")
        try:
            spec = parse_spec(body["spec"])
        except SpecError as exc:
            raise ManifestError(f"world.planted.spec: {exc}") from exc
        return cls(spec=spec, beta=body["beta"], difficulty=body["difficulty"])


@dataclass(frozen=True)
class World:
    """One generated history: what it is, which seed made it, and every generator parameter.

    ``origin_ms`` is the instant the first warm-up bar opens, and it sits
    on a UTC midnight: a day edge is on both the 4h grid and the 1d grid,
    so the daily backdrop aggregated from the same path lines up with the
    decision bars, and the split's first holdout cut (snapped to a
    midnight) is on the grid too.
    """

    kind: WorldKind
    seed_set: SeedSet
    world_seed: int
    generator_version: str
    origin_ms: int
    initial_price: float
    bars: Bars
    price: PriceModel
    funding: FundingModel
    planted: Planted | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _read.enum(WorldKind, self.kind, "world.kind"))
        object.__setattr__(self, "seed_set", _read.enum(SeedSet, self.seed_set, "world.seed_set"))
        _read.whole(self.world_seed, "world.world_seed", minimum=0)
        _read.text(self.generator_version, "world.generator_version")
        _read.whole(self.origin_ms, "world.origin_ms", minimum=0)
        if self.origin_ms % MS_PER_DAY:
            raise ManifestError(
                f"world.origin_ms: {self.origin_ms} is not a UTC midnight; the world grid starts "
                f"on a day edge so the 4h bars, the 1d bars and the holdout cut share it"
            )
        object.__setattr__(
            self,
            "initial_price",
            _read.amount(self.initial_price, "world.initial_price", positive=True),
        )
        for name, kind in (("bars", Bars), ("price", PriceModel), ("funding", FundingModel)):
            if not isinstance(getattr(self, name), kind):
                raise ManifestError(
                    f"world.{name}: expected a {kind.__name__}, got {getattr(self, name)!r}"
                )
        if self.planted is not None and not isinstance(self.planted, Planted):
            raise ManifestError(
                f"world.planted: expected a Planted or None, got {self.planted!r}"
            )
        # The one cross-field rule: what the world IS and what it CARRIES
        # agree. A null world with a rule in its manifest, or a conditional
        # one without, would be a report row filed under the wrong kind.
        conditional = self.kind is WorldKind.CONDITIONAL_V1
        if conditional != (self.planted is not None):
            raise ManifestError(
                f"world.planted: a {self.kind.value} world "
                + ("carries a planted rule" if conditional else "plants nothing")
                + f", got {'one' if self.planted is not None else 'none'}"
            )

    @property
    def id(self) -> str:
        """The opaque id: a function of the seed set and the seed, so no world can carry another's."""
        return opaque_world_id(self.seed_set, self.world_seed)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "seed_set": self.seed_set.value,
            "world_seed": self.world_seed,
            "generator_version": self.generator_version,
            "origin_ms": self.origin_ms,
            "initial_price": self.initial_price,
            "bars": self.bars.to_dict(),
            "price": self.price.to_dict(),
            "funding": self.funding.to_dict(),
            "planted": None if self.planted is None else self.planted.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> World:
        body = _read.exact(
            payload,
            (
                "id",
                "kind",
                "seed_set",
                "world_seed",
                "generator_version",
                "origin_ms",
                "initial_price",
                "bars",
                "price",
                "funding",
                "planted",
            ),
            "world",
        )
        world = cls(
            kind=body["kind"],
            seed_set=body["seed_set"],
            world_seed=body["world_seed"],
            generator_version=body["generator_version"],
            origin_ms=body["origin_ms"],
            initial_price=body["initial_price"],
            bars=Bars.from_dict(body["bars"]),
            price=PriceModel.from_dict(body["price"]),
            funding=FundingModel.from_dict(body["funding"]),
            planted=None if body["planted"] is None else Planted.from_dict(body["planted"]),
        )
        # Written for the reader of the file; checked against the two fields
        # it is derived from, so a hand-written manifest (the development
        # seeds will be) naming another world's id is refused rather than
        # filed under that world's coin.
        if body["id"] != world.id:
            raise ManifestError(
                f"world.id: {body['id']!r} is not the id of {world.seed_set.value} seed "
                f"{world.world_seed} ({world.id})"
            )
        return world


# -- the run ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Shares:
    """How the measurable span is cut: the research package's own shares, holdout taking the rest.

    The same two rules ``Split.by_shares`` applies, stated here so a
    manifest is refused where it is written; the split itself is cut at
    run time, when the span is known.
    """

    train_share: float = DEFAULT_TRAIN_SHARE
    validation_share: float = DEFAULT_VALIDATION_SHARE

    def __post_init__(self) -> None:
        for name in ("train_share", "validation_share"):
            share = _read.number(getattr(self, name), f"split.{name}")
            if not 0 < share < 1:
                raise ManifestError(
                    f"split.{name}: a share of the span is strictly between 0 and 1, got {share!r}"
                )
            object.__setattr__(self, name, share)
        if self.train_share + self.validation_share >= 1:
            raise ManifestError(
                f"split: train_share + validation_share must leave room for a holdout, got "
                f"{self.train_share:g} + {self.validation_share:g}"
            )

    def to_dict(self) -> dict[str, float]:
        return {"train_share": self.train_share, "validation_share": self.validation_share}

    @classmethod
    def from_dict(cls, payload: object) -> Shares:
        return cls(**_read.exact(payload, ("train_share", "validation_share"), "split"))


@dataclass(frozen=True)
class Budget:
    """Answers per arm, and how far over threshold(``max_trials``) the oracle must sit (plan §4.4)."""

    max_trials: int
    oracle_margin: float

    def __post_init__(self) -> None:
        _read.whole(self.max_trials, "budget.max_trials", minimum=1)
        object.__setattr__(
            self, "oracle_margin", _read.amount(self.oracle_margin, "budget.oracle_margin")
        )

    def to_dict(self) -> dict[str, object]:
        return {"max_trials": self.max_trials, "oracle_margin": self.oracle_margin}

    @classmethod
    def from_dict(cls, payload: object) -> Budget:
        return cls(**_read.exact(payload, ("max_trials", "oracle_margin"), "budget"))


@dataclass(frozen=True)
class Clause:
    """A feature compared with a threshold the grid supplies: ``feature op <grid value>``."""

    feature: str
    op: Op

    def __post_init__(self) -> None:
        try:
            parse_feature_name(self.feature, path="search_space.templates[].feature")
        except SpecError as exc:
            raise ManifestError(str(exc)) from exc
        object.__setattr__(self, "op", _read.enum(Op, self.op, "search_space.templates[].op"))

    def to_dict(self) -> list[str]:
        return [self.feature, self.op.value]

    @classmethod
    def from_dict(cls, payload: object, path: str) -> Clause:
        pair = _read.sequence(payload, path)
        if len(pair) != 2:
            raise ManifestError(f"{path}: a clause is [feature, op], got {payload!r}")
        return cls(feature=pair[0], op=pair[1])


@dataclass(frozen=True)
class Template:
    """One structure of the candidate pool: a side, an entry clause, a gate clause, a hold.

    The gate goes under ``filters`` when the spec is written (plan §1 #10),
    the one canonical place, so two candidates that trade the same are not
    two hashes. ``max_bars`` is the exit — every candidate has one, as a
    planted rule must — and is held to the language's bound here, where
    the manifest path can be named, before the parser holds it again.
    """

    side: Side
    entry: Clause
    gate: Clause
    max_bars: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "side", _read.enum(Side, self.side, "search_space.templates[].side")
        )
        for name in ("entry", "gate"):
            if not isinstance(getattr(self, name), Clause):
                raise ManifestError(
                    f"search_space.templates[].{name}: expected a Clause, "
                    f"got {getattr(self, name)!r}"
                )
        bars = _read.whole(self.max_bars, "search_space.templates[].exit.max_bars", minimum=1)
        if bars > MAX_HOLD_BARS:
            raise ManifestError(
                f"search_space.templates[].exit.max_bars: {bars} is over the language's "
                f"{MAX_HOLD_BARS}-bar hold"
            )

    @property
    def features(self) -> tuple[str, str]:
        return (self.entry.feature, self.gate.feature)

    def to_dict(self) -> dict[str, object]:
        return {
            "side": self.side.value,
            "entry": self.entry.to_dict(),
            "gate": self.gate.to_dict(),
            "exit": {"max_bars": self.max_bars},
        }

    @classmethod
    def from_dict(cls, payload: object, path: str) -> Template:
        body = _read.exact(payload, ("side", "entry", "gate", "exit"), path)
        exit_ = _read.exact(body["exit"], ("max_bars",), f"{path}.exit")
        return cls(
            side=body["side"],
            entry=Clause.from_dict(body["entry"], f"{path}.entry"),
            gate=Clause.from_dict(body["gate"], f"{path}.gate"),
            max_bars=exit_["max_bars"],
        )


def _grid_value(value: Any, path: str) -> float | str:
    """A number, or a label (for a feature compared with one, the regime)."""
    return value if isinstance(value, str) and value else _read.number(value, path)


@dataclass(frozen=True)
class SearchSpace:
    """The finite candidate pool Random and Grid draw from (plan §4.3): structures × thresholds.

    ``threshold_grid`` names a grid for every feature a template compares,
    and for no other — a grid nothing reads is a knob whose author believes
    it is doing something. Which features take a label and which a number
    is the parser's call, made when the pool is written out (plan PR 3).
    ``pool_size`` is filled in then, after ``spec_hash`` deduplication, and
    is ``None`` until it is: a pool that was enumerated and came out empty
    is a different fact from one not yet enumerated.

    The grid is held as a tuple of ``(feature, values)`` pairs, sorted by
    feature — immutable, as the rest of a frozen record is, and hashable —
    and accepted as a mapping at construction; :meth:`grid` reads it by
    name.
    """

    templates: tuple[Template, ...]
    threshold_grid: tuple[tuple[str, tuple[float | str, ...]], ...]
    candidate_order: str
    pool_size: int | None = None

    def __post_init__(self) -> None:
        templates = tuple(_read.sequence(self.templates, "search_space.templates"))
        if not templates or any(not isinstance(t, Template) for t in templates):
            raise ManifestError(
                f"search_space.templates: at least one Template, got {self.templates!r}"
            )
        object.__setattr__(self, "templates", templates)
        given = self.threshold_grid
        pairs = (
            list(given.items())
            if isinstance(given, Mapping)
            else [
                tuple(_read.sequence(pair, f"search_space.threshold_grid[{index}]"))
                for index, pair in enumerate(_read.sequence(given, "search_space.threshold_grid"))
            ]
        )
        grid: dict[str, tuple[float | str, ...]] = {}
        for pair in pairs:
            if len(pair) != 2 or not isinstance(pair[0], str):
                raise ManifestError(
                    f"search_space.threshold_grid: a (feature, values) pair, got {pair!r}"
                )
            feature, values = pair
            path = f"search_space.threshold_grid[{feature!r}]"
            try:
                parse_feature_name(feature, path=path)
            except SpecError as exc:
                raise ManifestError(str(exc)) from exc
            if feature in grid:
                raise ManifestError(f"{path}: listed twice")
            grid[feature] = _read.distinct(values, path, _grid_value)
        compared = {feature for template in templates for feature in template.features}
        if set(grid) != compared:
            raise ManifestError(
                f"search_space.threshold_grid: a grid for every feature the templates compare "
                f"and no other — templates compare {sorted(compared)}, grids are for "
                f"{sorted(grid)}"
            )
        object.__setattr__(self, "threshold_grid", tuple(sorted(grid.items())))
        _read.text(self.candidate_order, "search_space.candidate_order")
        if self.pool_size is not None:
            _read.whole(self.pool_size, "search_space.pool_size", minimum=1)

    def grid(self, feature: str) -> tuple[float | str, ...]:
        """The thresholds for ``feature``; the templates name only features that have one."""
        return dict(self.threshold_grid)[feature]

    def to_dict(self) -> dict[str, object]:
        return {
            "templates": [template.to_dict() for template in self.templates],
            "threshold_grid": {feature: list(values) for feature, values in self.threshold_grid},
            "candidate_order": self.candidate_order,
            "pool_size": self.pool_size,
        }

    @classmethod
    def from_dict(cls, payload: object) -> SearchSpace:
        body = _read.exact(
            payload,
            ("templates", "threshold_grid", "candidate_order", "pool_size"),
            "search_space",
        )
        return cls(
            templates=tuple(
                Template.from_dict(item, f"search_space.templates[{index}]")
                for index, item in enumerate(
                    _read.sequence(body["templates"], "search_space.templates")
                )
            ),
            threshold_grid=body["threshold_grid"],
            candidate_order=body["candidate_order"],
            pool_size=body["pool_size"],
        )


@dataclass(frozen=True)
class RandomArm:
    """One search seed per replicate: the order the pool is drawn in, apart from the world seed."""

    search_seeds: tuple[int, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "search_seeds",
            _read.distinct(
                self.search_seeds,
                "arms.random.search_seeds",
                lambda seed, path: _read.whole(seed, path, minimum=0),
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {"search_seeds": list(self.search_seeds)}

    @classmethod
    def from_dict(cls, payload: object) -> RandomArm:
        return cls(**_read.exact(payload, ("search_seeds",), "arms.random"))


@dataclass(frozen=True)
class LlmArm:
    """The model arm, and the subset of worlds the three arms are compared on (plan §8 #5).

    ``world_subset`` is fixed BEFORE a test seed set is used: Random and
    Grid run every world, the model runs these, and the side-by-side table
    is built from these alone.
    """

    provider: str
    model: str
    temperature: float
    replicates: int
    world_subset: tuple[str, ...]

    def __post_init__(self) -> None:
        _read.text(self.provider, "arms.llm.provider")
        _read.text(self.model, "arms.llm.model")
        object.__setattr__(
            self, "temperature", _read.amount(self.temperature, "arms.llm.temperature")
        )
        _read.whole(self.replicates, "arms.llm.replicates", minimum=1)
        object.__setattr__(
            self,
            "world_subset",
            _read.distinct(self.world_subset, "arms.llm.world_subset", _world_id),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "replicates": self.replicates,
            "world_subset": list(self.world_subset),
        }

    @classmethod
    def from_dict(cls, payload: object) -> LlmArm:
        return cls(
            **_read.exact(
                payload,
                ("provider", "model", "temperature", "replicates", "world_subset"),
                "arms.llm",
            )
        )


@dataclass(frozen=True)
class Arms:
    """The arms of this run. Grid has no record: it walks ``search_space`` in ``candidate_order``.

    ``llm`` is optional because the null calibration runs Random and Grid
    first, without a model (plan §4.3); a manifest with no model arm is a
    complete run of those two.
    """

    random: RandomArm
    llm: LlmArm | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.random, RandomArm):
            raise ManifestError(f"arms.random: expected a RandomArm, got {self.random!r}")
        if self.llm is not None and not isinstance(self.llm, LlmArm):
            raise ManifestError(f"arms.llm: expected an LlmArm or None, got {self.llm!r}")

    def to_dict(self) -> dict[str, object]:
        return {
            "random": self.random.to_dict(),
            "llm": None if self.llm is None else self.llm.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: object) -> Arms:
        body = _read.exact(payload, ("random", "llm"), "arms")
        return cls(
            random=RandomArm.from_dict(body["random"]),
            llm=None if body["llm"] is None else LlmArm.from_dict(body["llm"]),
        )


@dataclass(frozen=True)
class Recovery:
    """When a selected rule counts as the planted one found (plan §4.6).

    Behaviour agreement at or above ``theta_behavior`` over the bars at
    least one of the two holds a position, and — when required — a
    positive holdout net Sharpe. Structural distance is reported, never
    judged on.
    """

    theta_behavior: float = 0.8
    require_positive_holdout: bool = True

    def __post_init__(self) -> None:
        theta = _read.number(self.theta_behavior, "scoring.recovery.theta_behavior")
        if not 0 <= theta <= 1:
            raise ManifestError(
                f"scoring.recovery.theta_behavior: an agreement rate between 0 and 1, "
                f"got {theta!r}"
            )
        object.__setattr__(self, "theta_behavior", theta)
        _read.flag(self.require_positive_holdout, "scoring.recovery.require_positive_holdout")

    def to_dict(self) -> dict[str, object]:
        return {
            "theta_behavior": self.theta_behavior,
            "require_positive_holdout": self.require_positive_holdout,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Recovery:
        return cls(
            **_read.exact(
                payload, ("theta_behavior", "require_positive_holdout"), "scoring.recovery"
            )
        )


@dataclass(frozen=True)
class Scoring:
    """The scorer's version and its recovery rule, frozen before a test seed set is used."""

    version: str
    recovery: Recovery

    def __post_init__(self) -> None:
        _read.text(self.version, "scoring.version")
        if not isinstance(self.recovery, Recovery):
            raise ManifestError(f"scoring.recovery: expected a Recovery, got {self.recovery!r}")

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "recovery": self.recovery.to_dict()}

    @classmethod
    def from_dict(cls, payload: object) -> Scoring:
        body = _read.exact(payload, ("version", "recovery"), "scoring")
        return cls(version=body["version"], recovery=Recovery.from_dict(body["recovery"]))


@dataclass(frozen=True)
class Manifest:
    """One world, one run: every parameter of plan §5, versioned."""

    world: World
    split: Shares
    indicator_lookback: int
    costs: CostModel
    penalty: Penalty
    budget: Budget
    search_space: SearchSpace
    arms: Arms
    scoring: Scoring
    null_definition_version: str

    def __post_init__(self) -> None:
        for name, kind in (
            ("world", World),
            ("split", Shares),
            ("costs", CostModel),
            ("penalty", Penalty),
            ("budget", Budget),
            ("search_space", SearchSpace),
            ("arms", Arms),
            ("scoring", Scoring),
        ):
            if not isinstance(getattr(self, name), kind):
                raise ManifestError(
                    f"{name}: expected a {kind.__name__}, got {getattr(self, name)!r}"
                )
        # The engine's own floor, not 1: a smaller window is refused by the
        # frame at the first measurement, which is the wrong place to learn it.
        _read.whole(self.indicator_lookback, "indicator_lookback", minimum=MIN_INDICATOR_LOOKBACK)
        _read.text(self.null_definition_version, "null_definition_version")

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest_version": MANIFEST_VERSION,
            "world": self.world.to_dict(),
            "split": self.split.to_dict(),
            "indicator_lookback": self.indicator_lookback,
            "costs": self.costs.to_dict(),
            "penalty": self.penalty.to_dict(),
            "budget": self.budget.to_dict(),
            "search_space": self.search_space.to_dict(),
            "arms": self.arms.to_dict(),
            "scoring": self.scoring.to_dict(),
            "null_definition_version": self.null_definition_version,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Manifest:
        # The version first, before the key set: a later version's extra key
        # would otherwise be refused as an unknown key, and the sentence that
        # names the real reason — another build wrote this — never reached.
        version = _read.mapping(payload, "manifest").get("manifest_version")
        if isinstance(version, bool) or version != MANIFEST_VERSION:
            raise ManifestError(
                f"manifest_version: this build reads version {MANIFEST_VERSION}, got {version!r}"
            )
        body = _read.exact(
            payload,
            (
                "manifest_version",
                "world",
                "split",
                "indicator_lookback",
                "costs",
                "penalty",
                "budget",
                "search_space",
                "arms",
                "scoring",
                "null_definition_version",
            ),
            "manifest",
        )
        try:
            costs = CostModel.from_dict(dict(_read.mapping(body["costs"], "costs")))
            penalty = Penalty.from_dict(body["penalty"])
        except ValueError as exc:
            raise ManifestError(str(exc)) from exc
        return cls(
            world=World.from_dict(body["world"]),
            split=Shares.from_dict(body["split"]),
            indicator_lookback=body["indicator_lookback"],
            costs=costs,
            penalty=penalty,
            budget=Budget.from_dict(body["budget"]),
            search_space=SearchSpace.from_dict(body["search_space"]),
            arms=Arms.from_dict(body["arms"]),
            scoring=Scoring.from_dict(body["scoring"]),
            null_definition_version=body["null_definition_version"],
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> Manifest:
        return cls.from_dict(_read.loads(text))
