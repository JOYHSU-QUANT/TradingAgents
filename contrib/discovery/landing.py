"""Landing a world in its own research store, checking it, and the run directory it lives in (plan §2, §3 step 2, PR 2).

A world artifact is a research store with one coin in it, filled THROUGH
the research package's own walk (``backfill_candles``,
``backfill_funding``) from the world's :class:`~contrib.discovery.worlds.SyntheticHistory`
port — the same code path, page by page, that fills the real store from
the venue (plan §1 #3). Not written row by row: the walk is what records
where a series reached and why it stopped, and an arm later copies the
artifact and opens it as any store, so the artifact has to be one the
walk made.

What :func:`verify` holds is plan PR 2's acceptance list, measured with
the research package's own instruments rather than restated: the stored
history is a clean grid (``require_clean_history``), every feature of the
vocabulary has a value by the time the decision bars begin
(``first_measurable_index`` is at or before the warm-up's end), the tail
has every feature too (``require_measurable_tail``), and the split cut
from the manifest's shares tiles exactly the decision bars. A world that
fails any of these is refused before an arm ever runs on it, with the
measured number in the sentence — a warm-up too short says what to raise
it to.

The split is cut HERE, from the manifest, not measured by
``plan_experiment``: that verb starts train at the first measurable bar,
which on a generously warmed-up world is inside the warm-up, and the
decision span would then be longer than the manifest says and differ by
world. The benchmark's windows are the manifest's ``count`` bars by its
shares (``Split.by_shares``, the rule ``Shares`` restates), the same for
every arm; :func:`experiment_for` binds them with the other conditions
into the experiment an arm's ledger creates.

**The run directory** (plan §2's layout) is keyed by the MANIFEST, not by
the world's id alone: ``<root>/<world id>-<digest of the manifest>/``.
The id is a function of the seed set and the seed and of nothing else
(plan §1 #7), so the null twin and the conditional world of one seed, or
one seed planted at each β band during calibration (plan §4.1), share an
id and would collide on a directory named by it; the digest tells them
apart, and a directory name never reaches a prompt — only the coin name
inside the store does. :func:`land_world` builds the directory in a
staging directory beside its final place and renames it there once the
landing is verified and the manifest written, so a half-landed world is
never visible at the final path, whatever ended the run.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .manifest import Manifest, Shares, World
from .records import dumps
from .upstream import (
    DAILY_INTERVAL,
    Experiment,
    FeatureFrame,
    ResearchStore,
    SeriesFetch,
    Split,
    StopReason,
    backfill_candles,
    backfill_funding,
    default_db_path,
    describe_stop,
    first_measurable_index,
    from_epoch_ms,
    interval_to_ms,
    load_bundle,
    require_clean_history,
    require_measurable_tail,
)
from .worlds import SyntheticHistory, WorldError, WorldHistory

__all__ = [
    "ARTIFACT_FILENAME",
    "MANIFEST_FILENAME",
    "Landing",
    "default_root",
    "experiment_for",
    "land",
    "land_world",
    "manifest_digest",
    "split_of",
    "verify",
    "world_dir",
]

logger = logging.getLogger(__name__)

# What a run directory holds (plan §2's layout). Named once: the landing
# writes them and the runner (plan PR 3) reads them.
ARTIFACT_FILENAME = "world.sqlite"
MANIFEST_FILENAME = "manifest.json"

# How many hex digits of the manifest's digest name the run directory. Eight
# is the id's own width; the digest is a label for people and directories,
# not a key anything is looked up by.
_DIGEST_DIGITS = 8


def default_root() -> Path:
    """``<repo root>/data/discovery`` — where runs land unless told otherwise.

    Beside the research package's own default store, derived from that
    package's one statement of where ``data/`` is rather than from a second
    copy of the path arithmetic; the repository's ignore rules keep the
    directory out of a diff.
    """
    return default_db_path().parent / "discovery"


def manifest_digest(manifest: Manifest) -> str:
    """The first hex digits of the SHA-256 of the manifest's canonical text, ``pool_size`` set aside.

    ``search_space.pool_size`` is the one field a later step fills in (the
    pool is enumerated by plan PR 3 and the count written back), so it is
    nulled before hashing: the run directory keeps its name when its
    ``manifest.json`` gains the count, and the filled manifest names the
    same directory it was filled in.
    """
    record = manifest.to_dict()
    search_space = dict(record["search_space"])  # type: ignore[call-overload]
    search_space["pool_size"] = None
    record["search_space"] = search_space
    return hashlib.sha256(dumps(record).encode("utf-8")).hexdigest()[:_DIGEST_DIGITS]


def world_dir(root: Path, manifest: Manifest) -> Path:
    """``<root>/<world id>-<manifest digest>``: one manifest, one run directory (the module docstring)."""
    return root / f"{manifest.world.id}-{manifest_digest(manifest)}"


def land(history: WorldHistory, store: ResearchStore) -> tuple[SeriesFetch, ...]:
    """Walk ``history`` into ``store`` through the research package's backfill: bars, daily, funding.

    Every window is cut at the world's own clock, as a real fetch cuts at
    the venue's. Each walk has to end where a complete walk ends — at the
    requested start for the two backward candle walks, at the requested
    end for the forward funding walk — and leave the store holding exactly
    the rows the history has, or the artifact is not the world, and it is
    refused by name rather than left for the scans to describe: a walk that
    lost a page can still end where it meant to.
    """
    market = SyntheticHistory(history)
    coin = history.world.id
    since = from_epoch_ms(history.world.origin_ms)
    end = market.get_exchange_time(coin)
    fetched = (
        backfill_candles(
            market, store, coin=coin, interval=history.world.bars.interval, since=since, end=end
        ),
        backfill_candles(market, store, coin=coin, interval=DAILY_INTERVAL, since=since, end=end),
        backfill_funding(market, store, coin=coin, since=since, end=end),
    )
    for result, complete, rows in zip(
        fetched,
        (StopReason.REACHED_SINCE, StopReason.REACHED_SINCE, StopReason.REACHED_END),
        (len(history.bars), len(history.daily), len(history.funding)),
        strict=True,
    ):
        if result.stopped is not complete:
            raise WorldError(
                f"{result.label}: the walk stopped because {describe_stop(result.stopped)}, "
                f"not because it {describe_stop(complete)}; the store does not hold the world"
            )
        if result.rows_after != rows:
            raise WorldError(
                f"{result.label}: the store holds {result.rows_after} row(s) after the walk and "
                f"the world has {rows}; the store does not hold the world"
            )
    return fetched


def split_of(world: World, shares: Shares) -> Split:
    """The manifest's windows: ``world``'s decision bars, cut by ``shares``, holdout last."""
    step = interval_to_ms(world.bars.interval)
    start_ms = world.origin_ms + world.bars.warmup_bars * step
    return Split.by_shares(
        world.bars.interval,
        start_ms=start_ms,
        end_ms=start_ms + world.bars.count * step,
        train_share=shares.train_share,
        validation_share=shares.validation_share,
    )


def experiment_for(manifest: Manifest, experiment_id: str) -> Experiment:
    """The experiment an arm runs under ``manifest``: its coin, costs, split, window and penalty.

    A value, not a stored row: the arm's ledger writes it
    (``Ledger.create_experiment``), which is where the coin's holdout and
    penalty pins are checked — every arm of one world has its own store, so
    every arm is the first experiment on its coin.
    """
    return Experiment(
        experiment_id=experiment_id,
        coin=manifest.world.id,
        costs=manifest.costs,
        split=split_of(manifest.world, manifest.split),
        indicator_lookback=manifest.indicator_lookback,
        penalty=manifest.penalty,
    )


@dataclass(frozen=True)
class Landing:
    """What :func:`verify` measured on a landed world: the counts, the measured start, the windows.

    The decision bars begin where ``split.train`` does; ``decision_start_index``
    is that bar's index (the manifest's warm-up), kept beside the measured
    ``first_measurable_index`` so the margin between them is one subtraction.
    """

    world_id: str
    bars: int
    daily_bars: int
    settlements: int
    first_measurable_index: int
    first_measurable_ms: int
    decision_start_index: int
    split: Split
    segment_bars: tuple[int, int, int]

    def describe(self) -> list[str]:
        """The landing as lines to print — what ``generate`` reports."""
        lines = [
            f"world {self.world_id}: {self.bars} {self.split.interval} bars "
            f"({self.decision_start_index} warm-up + {self.bars - self.decision_start_index} "
            f"decision), {self.daily_bars} {DAILY_INTERVAL} bars, {self.settlements} "
            f"settlements",
            f"every feature has a value from bar {self.first_measurable_index} "
            f"({from_epoch_ms(self.first_measurable_ms).isoformat()}); the decision bars "
            f"begin at bar {self.decision_start_index} "
            f"({from_epoch_ms(self.split.train.start_ms).isoformat()})",
        ]
        lines += [
            f"{line} ({count} bars)"
            for line, count in zip(self.split.describe(), self.segment_bars, strict=True)
        ]
        return lines


def verify(store: ResearchStore, manifest: Manifest) -> Landing:
    """Refuse a landed world an arm could not measure on as the manifest says; return what was measured.

    Reads the whole store for the coin — the one read of the holdout rows
    this package makes before the arms run, and it computes no figure for
    any window: the frame is built so the warm-up and the tail can be
    MEASURED, as ``plan_experiment`` builds it for the same reason.
    """
    world = manifest.world
    interval = world.bars.interval
    bundle = load_bundle(store, coin=world.id, interval=interval)
    expected = world.bars.warmup_bars + world.bars.count
    if len(bundle.bars) != expected or bundle.bars[0].open_time != world.origin_ms:
        raise WorldError(
            f"{world.id}: the store holds {len(bundle.bars)} {interval} bars from "
            f"{from_epoch_ms(bundle.bars[0].open_time).isoformat()}; the manifest describes "
            f"{expected} ({world.bars.warmup_bars} warm-up + {world.bars.count}) from "
            f"{from_epoch_ms(world.origin_ms).isoformat()} — this is not that world's artifact"
        )
    holes = require_clean_history(bundle, interval)
    if holes:
        # The research package counts a missing settlement rather than
        # refusing it, because a venue skips one now and then. This world's
        # grid has no skips, so a hole is a row the landing lost, and a
        # world with one is not the world that was generated.
        raise WorldError(
            f"{world.id}: the store's funding settlements have {holes} hole(s); the world "
            f"has none, so the store does not hold the world"
        )
    frame = FeatureFrame(bundle, indicator_lookback=manifest.indicator_lookback)
    first = first_measurable_index(frame)
    start = world.bars.warmup_bars
    if first > start:
        raise WorldError(
            f"{world.id}: every feature of the vocabulary first has a value at bar {first} "
            f"({from_epoch_ms(bundle.bars[first].open_time).isoformat()}), after the warm-up "
            f"ends at bar {start}; a trial would refuse some legal spec at the decision bars' "
            f"first bar — raise world.bars.warmup_bars to at least {first}"
        )
    require_measurable_tail(frame)
    # ``by_shares`` cuts a span of exactly ``count`` bars on the grid the
    # bars sit on, so the three windows hold every decision bar once.
    split = split_of(world, manifest.split)
    opens = [bar.open_time for bar in bundle.bars]
    train, validation, holdout = (
        stop - begin for begin, stop in (s.bar_range(opens) for s in split.ordered)
    )
    return Landing(
        world_id=world.id,
        bars=len(bundle.bars),
        daily_bars=len(bundle.daily),
        settlements=len(bundle.funding),
        first_measurable_index=first,
        first_measurable_ms=bundle.bars[first].open_time,
        decision_start_index=start,
        split=split,
        segment_bars=(train, validation, holdout),
    )


def land_world(root: Path, history: WorldHistory) -> tuple[Path, tuple[SeriesFetch, ...], Landing]:
    """Land ``history`` as its manifest's run directory under ``root``; return it and what was measured.

    Built in a staging directory beside the final one and renamed into
    place only after the landing is verified and the canonical manifest
    written, so nothing half-made is ever at the final path (the module
    docstring); a run that ends any other way takes its staging directory
    with it. A directory already at the final path is this manifest's
    world, landed before: refused, since an artifact is landed once and
    copied, and whatever was measured beside it was measured on that copy.
    """
    manifest = history.manifest
    target = world_dir(root, manifest)
    if target.exists():
        raise WorldError(
            f"{target} already holds this manifest's world; a world artifact is landed once "
            f"and copied — remove the directory by hand to generate it again"
        )
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=root, prefix=f".{target.name}.partial-"))
    try:
        if os.name != "nt":
            # ``mkdtemp`` makes a private (0700) directory and the rename
            # keeps the mode; a run directory is an ordinary one under the
            # umask. Inside the ``try``: a mode that cannot be set is a run
            # that did not happen, and its staging goes with it.
            mask = os.umask(0)
            os.umask(mask)
            staging.chmod(0o777 & ~mask)
        with ResearchStore(staging / ARTIFACT_FILENAME) as store:
            fetched = land(history, store)
            landing = verify(store, manifest)
        # The store's close suppresses its own errors, and a close that
        # failed can leave a journal beside the file; an arm copies the one
        # file, so a directory holding more than the artifact is not one.
        leftover = sorted(p.name for p in staging.iterdir() if p.name != ARTIFACT_FILENAME)
        if leftover:
            raise WorldError(
                f"{staging / ARTIFACT_FILENAME} did not close cleanly: {leftover} left beside it"
            )
        # Bytes, not text: the canonical manifest's newlines are its own,
        # not the platform's.
        (staging / MANIFEST_FILENAME).write_bytes(manifest.dumps().encode("utf-8"))
        staging.rename(target)
    except BaseException:
        try:
            shutil.rmtree(staging)
        except OSError:
            # The failure propagating is the one to report; the orphan is
            # named so it is not mistaken for a world next time.
            logger.warning("could not remove the staging directory %s", staging, exc_info=True)
        raise
    return target, fetched, landing
