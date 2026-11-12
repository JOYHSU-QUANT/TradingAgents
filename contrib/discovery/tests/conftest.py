"""Two histories under one coin, the arms built over them, and a scripted hypothesist.

The ledger-level suites (isolation, verdict) run their arms over a
HAND-WRITTEN history: 400 midnight-anchored 4h bars closing a millisecond
before the next open, 200 days of daily bars before them, hourly funding
from forty days before — the shape the research package's measurement
refuses anything else of, and no more. It is built once per session and
copied per test, since every arm writes to its own ledger. It is kept
beside the generator on purpose: the vocabulary's daily reach makes a
generated world at least 1,100 bars of warm-up before its decision bars,
every search round rebuilds the feature frame over the whole store, and
the frame's indicator walk was measured at 1.3 ms a bar — so the arm
suites, which care about ledger state and not about the bars, would pay
several seconds a round for a world they never read.

The generated world is the example manifest's, built once per session
(``example_world``) and landed once (``landed_world``) for the suites
that are about the world itself: the generator, the landing and the
command — and for the bench, whose ``searched`` fixture lands it under
``bench_manifest`` (``land_run``) and searches it once per session.

The example manifest is a conditional world over the same coin, so the
isolation tests can hold the one thing the manifest and the prompts share
(the opaque id) against the things they must not (the kind, the rule).
"""

from __future__ import annotations

import hashlib
import random
import shutil
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path

import pytest

from contrib.autoresearch.constants import bar_span_ms
from contrib.autoresearch.costs import CostModel
from contrib.autoresearch.dsl import Op, Side, parse_spec
from contrib.autoresearch.ledger import Experiment, Ledger, Penalty
from contrib.autoresearch.ports import HypothesistError
from contrib.autoresearch.research import open_experiment
from contrib.autoresearch.store import ResearchStore
from contrib.autoresearch.upstream import Candle, FundingPoint, interval_to_ms
from contrib.discovery.bench import run_world
from contrib.discovery.landing import (
    ARTIFACT_FILENAME,
    MANIFEST_FILENAME,
    Landing,
    artifact_digest,
    land,
    verify,
    world_dir,
)
from contrib.discovery.manifest import (
    Arms,
    Bars,
    Budget,
    CandidateOrder,
    Clause,
    Difficulty,
    FundingModel,
    LlmArm,
    Manifest,
    Planted,
    PriceModel,
    RandomArm,
    Recovery,
    Scoring,
    SearchSpace,
    SeedSet,
    Shares,
    Template,
    World,
    WorldKind,
    opaque_world_id,
)
from contrib.discovery.worlds import GENERATOR_VERSION, WorldHistory, generate

MS_PER_HOUR = 60 * 60_000
STEP_MS = interval_to_ms("4h")
DAY_MS = 24 * MS_PER_HOUR
# A UTC midnight: on the 4h grid and the 1d grid both.
START_MS = 1_700_000_000_000 - 1_700_000_000_000 % DAY_MS
BARS = 400
# Two hundred days: the longest daily mean (``sma_1d_200``) has a value
# once nine tenths of its window is there, so the generated world's first
# measurable bar sits about 185 days in, and the example manifest's warm-up
# clears it with a fortnight to spare (``test_landing`` measures it).
WARMUP_DAYS = 200
LOOKBACK = 50  # the engine's floor: the indicator walk is what every frame here pays for

# The coin every arm in this suite files under: world 0 of the development set.
COIN = opaque_world_id(SeedSet.DEV, 0)
# The penalty the fixtures open arms and manifests at: plan §3.10's, pinned so
# autoresearch's default can move without moving these tests' worlds.
FIXTURE_PENALTY = Penalty(sharpe_base=1.0, k=0.25)

SIZING = {"mode": "fixed_margin_fraction", "fraction": 0.5}
BUY = {
    "family": "breakout",
    "entry": {"long": [{"left": "close", "op": ">", "right": 0}]},
    "sizing": SIZING,
}
BUY_12 = {**BUY, "exit": {"max_bars": 12}}
FLAT = {
    "family": "breakout",
    "entry": {"long": [{"left": "close", "op": ">", "right": 1e9}]},
    "sizing": SIZING,
}
SHORT = {
    "family": "breakout",
    "entry": {"short": [{"left": "close", "op": ">", "right": 0}]},
    "sizing": SIZING,
}
# The rule the example world plants: an exogenous-funding entry, a volatility
# gate under ``filters`` (plan §4.1), a ``max_bars`` exit. The thresholds
# were chosen on the example seed so the gate BINDS (``realized_vol_20``
# sits around 0.0099 at ``vol`` 0.01, so 0.0095 passes about half the bars)
# and the rule holds about a fifth of the decision bars — a plant an arm has
# to find, not one that buy-and-hold earns (measured 2026-11-04: 78 of 400).
PLANTED = {
    "family": "funding_filter",
    "entry": {"long": [{"left": "funding_zscore_14", "op": "<", "right": -1.5}]},
    "filters": [{"left": "realized_vol_20", "op": ">", "right": 0.0095}],
    "exit": {"max_bars": 6},
    "sizing": SIZING,
}


# -- the hand-written history ------------------------------------------------------------
#
# A copy of the builder in ``contrib/autoresearch/tests/test_research.py``
# (``_prices``, ``_fill``), which is private to that module; the two drift
# knowingly. Kept beside the generator for the reason the module docstring
# gives.


def _prices(count: int, *, seed: int, start: float, drift: float) -> tuple[list, list]:
    rng = random.Random(seed)
    closes = [start]
    for _ in range(count - 1):
        closes.append(round(closes[-1] * (1 + drift + rng.uniform(-0.01, 0.01)), 4))
    return closes, [closes[0], *closes[:-1]]


def _candles(closes, opens, *, start_ms: int, step_ms: int) -> list[Candle]:
    made = []
    for index, (close, opened) in enumerate(zip(closes, opens, strict=True)):
        price, open_ = Decimal(str(close)), Decimal(str(opened))
        made.append(
            Candle(
                open_time=start_ms + index * step_ms,
                close_time=start_ms + index * step_ms + bar_span_ms(step_ms),
                open=open_,
                high=max(open_, price) + 10,
                low=min(open_, price) - 10,
                close=price,
                volume=Decimal("1.5"),
            )
        )
    return made


def fill_history(store: ResearchStore, coin: str = COIN) -> None:
    """The three series a measurement reads, on a rising walk, under ``coin``."""
    closes, opens = _prices(BARS, seed=1, start=30000.0, drift=0.002)
    store.upsert_candles(coin, "4h", _candles(closes, opens, start_ms=START_MS, step_ms=STEP_MS))
    days = WARMUP_DAYS + BARS // 6 + 1
    closes, opens = _prices(days, seed=2, start=20000.0, drift=0.001)
    store.upsert_candles(
        coin,
        "1d",
        _candles(closes, opens, start_ms=START_MS - WARMUP_DAYS * DAY_MS, step_ms=DAY_MS),
    )
    first = START_MS - 40 * DAY_MS
    store.upsert_funding(
        coin,
        [
            FundingPoint(
                time=first + (i + 1) * MS_PER_HOUR + 57,
                rate=Decimal(str(round(0.00001 * ((i % 9) - 3), 6))),
            )
            for i in range(40 * 24 + BARS * 4)
        ],
    )


@pytest.fixture(scope="session")
def history(tmp_path_factory) -> Path:
    """The world artifact every arm copies: built once, never written to again."""
    path = tmp_path_factory.mktemp("history") / "world.sqlite"
    with ResearchStore(path) as store:
        fill_history(store)
    return path


def arm_store(history: Path, path: Path) -> ResearchStore:
    """A fresh arm: its own copy of the world, opened."""
    shutil.copy(history, path)
    return ResearchStore(path)


def open_arm(
    ledger: Ledger, *, name: str = "arm", penalty: Penalty | None = None, coin: str = COIN
) -> Experiment:
    return open_experiment(
        ledger,
        experiment_id=name,
        coin=coin,
        interval="4h",
        costs=CostModel(),
        indicator_lookback=LOOKBACK,
        penalty=FIXTURE_PENALTY if penalty is None else penalty,
    )


@pytest.fixture
def arm(history, tmp_path):
    """One arm's ledger over a fresh copy of the world, no experiment opened yet."""
    with arm_store(history, tmp_path / "arm.sqlite") as store:
        yield Ledger(store)


# -- the generated world ----------------------------------------------------------------------


def small_manifest(
    kind: WorldKind = WorldKind.NULL_V1,
    *,
    seed: int = 0,
    seed_set: SeedSet = SeedSet.DEV,
    warmup: int = 0,
    count: int = 60,
    vol: float = 0.01,
    drift: float = 0.0,
    sigma: float = 1e-4,
    beta: float = 0.001,
) -> Manifest:
    """The example manifest over a small world: the generator's unit tests need no warm-up.

    A null one is instant (no frame is built for it); a conditional one
    needs enough bars for the planted rule's features to have a value.
    """
    base = example_manifest(kind=kind, seed_set=seed_set, world_seed=seed)
    world = replace(
        base.world,
        origin_ms=START_MS,
        bars=Bars(interval="4h", count=count, warmup_bars=warmup),
        price=PriceModel(vol=vol, drift=drift),
        funding=FundingModel(sigma=sigma),
        planted=None if base.world.planted is None else replace(base.world.planted, beta=beta),
    )
    return replace(base, world=world, indicator_lookback=LOOKBACK)


@pytest.fixture(scope="session")
def example_world() -> WorldHistory:
    """The example manifest's world — the conditional world of development seed 0 — built once."""
    return generate(example_manifest())


@pytest.fixture(scope="session")
def landed_world(example_world, tmp_path_factory) -> tuple[Path, Landing]:
    """That world landed in its own store through the backfill and verified, once; read-only after."""
    path = tmp_path_factory.mktemp("world") / "world.sqlite"
    with ResearchStore(path) as store:
        land(example_world, store)
        landing = verify(store, example_manifest())
    return path, landing


# -- the searched world (plan PR 3) ------------------------------------------------------------


def bench_manifest(**changes) -> Manifest:
    """The example manifest under a two-answer budget, one Random seed, no model arm.

    The world block is the example's, so the landed store is this
    manifest's world too; only the run's parameters are smaller, since
    every answer an arm spends is an indicator walk over the whole store.
    """
    base = example_manifest(**changes)
    return replace(
        base,
        budget=Budget(max_trials=2, oracle_margin=0.25),
        arms=replace(base.arms, random=RandomArm(search_seeds=(0,)), llm=None),
    )


def land_run(root: Path, artifact: Path, manifest: Manifest) -> Path:
    """A run directory as ``generate`` leaves it: the artifact copied in, the manifest beside it.

    As ``generate`` writes it, with ``pool_size`` and the artifact's digest
    filled in; the directory is named by the manifest's digest, which sets
    those two fields aside.
    """
    space = manifest.search_space
    filled = replace(
        manifest,
        search_space=replace(space, pool_size=len(space.candidates)),
        artifact_sha256=artifact_digest(artifact),
    )
    run = world_dir(root, filled)
    run.mkdir(parents=True)
    shutil.copyfile(artifact, run / ARTIFACT_FILENAME)
    (run / MANIFEST_FILENAME).write_bytes(filled.dumps().encode("utf-8"))
    return run


@pytest.fixture(scope="session")
def searched(landed_world, tmp_path_factory):
    """The example world searched once: the run directory, what came back, what was printed, the artifact's digest before."""
    artifact, _landing = landed_world
    run = land_run(tmp_path_factory.mktemp("bench"), artifact, bench_manifest())
    before = hashlib.sha256((run / ARTIFACT_FILENAME).read_bytes()).hexdigest()
    lines: list[str] = []
    verdicts, oracle = run_world(run, on_line=lines.append)
    return run, verdicts, oracle, lines, before


# -- the hypothesist ----------------------------------------------------------------------


@dataclass
class ScriptedHypothesist:
    """Answers a script, then ends the run one of two ways.

    When the script runs out it raises the seam error — as a candidate
    pool that was walked to its end does (plan §4.3's exhausted protocol)
    if ``runs_out``, setting its own ``exhausted`` flag the way the Random
    and Grid arms will; as a plain seam failure otherwise. ``prompts``
    keeps what it was shown, for the tests that read the prompt.
    """

    answers: list[str]
    runs_out: bool = False
    exhausted: bool = False
    prompts: list[tuple[str, str]] = field(default_factory=list)

    def propose(self, system: str, user: str) -> str:
        self.prompts.append((system, user))
        if self.answers:
            return self.answers.pop(0)
        if self.runs_out:
            self.exhausted = True
            raise HypothesistError("the candidate pool is walked to its end")
        raise HypothesistError("the seam failed")


class InterruptingHypothesist:
    """An operator's interrupt arriving inside the seam."""

    exhausted = False

    def propose(self, system: str, user: str) -> str:
        raise KeyboardInterrupt


# -- the manifest --------------------------------------------------------------------------


def example_manifest(
    *,
    kind: WorldKind = WorldKind.CONDITIONAL_V1,
    seed_set: SeedSet = SeedSet.DEV,
    world_seed: int = 0,
    drift: float = 0.0,
) -> Manifest:
    """A complete manifest for the suite's world, conditional unless asked otherwise."""
    planted = (
        Planted(spec=parse_spec(PLANTED), beta=0.001, difficulty=Difficulty.EASY)
        if kind.plants
        else None
    )
    world = World(
        kind=kind,
        seed_set=seed_set,
        world_seed=world_seed,
        generator_version=GENERATOR_VERSION,
        origin_ms=START_MS - WARMUP_DAYS * DAY_MS,
        initial_price=30000.0,
        bars=Bars(interval="4h", count=BARS, warmup_bars=WARMUP_DAYS * 6),
        price=PriceModel(vol=0.01, drift=drift),
        funding=FundingModel(sigma=0.0001),
        planted=planted,
    )
    gate = Clause("realized_vol_20", Op.GT)
    search_space = SearchSpace(
        templates=(
            Template(Side.LONG, Clause("funding_zscore_14", Op.LT), gate, 12),
            Template(Side.SHORT, Clause("funding_zscore_14", Op.GT), gate, 12),
        ),
        threshold_grid={
            "funding_zscore_14": (-2.0, -1.0, 1.0, 2.0),
            "realized_vol_20": (0.002, 0.004),
        },
        candidate_order=CandidateOrder.TEMPLATES_THEN_GRID,
    )
    return Manifest(
        world=world,
        split=Shares(),
        indicator_lookback=200,
        costs=CostModel(),
        penalty=FIXTURE_PENALTY,  # the README's example manifest shows it
        budget=Budget(max_trials=10, oracle_margin=0.25),
        search_space=search_space,
        arms=Arms(
            random=RandomArm(search_seeds=(0, 1)),
            llm=LlmArm(
                provider="openai",
                model="gpt",
                temperature=0.0,
                replicates=1,
                world_subset=(world.id,),
            ),
        ),
        scoring=Scoring(version="1", recovery=Recovery()),
        null_definition_version="v1",
    )
