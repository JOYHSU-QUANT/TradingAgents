"""The experiment ledger: what was measured, under what, and who may see the holdout.

Plan §3.3 and §3.10, and the decisions §10.6, §10.7 and §11 left to this PR.
The evaluator answers "what did this spec score on this window"; this module
answers the questions a SEARCH over specs raises, which the evaluator cannot:

**How many looks has this validation window had?** Every distinct rule
measured against a coin's history is one more comparison against the bars
before its holdout, so the bar a trial must clear to be promoted rises with
``ln(n)`` (plan §3.10). ``n`` counts every DISTINCT RULE tried on the COIN,
across every experiment on it — not the trials of one ``family``, and not the
trials of one experiment. Both are labels a hypothesis loop can change for
free: two of the five families are an author's intent (plan §10.6), and an
experiment is a name — on an unchanged store a second one cuts the same
windows, since the holdout is pinned per coin. A count either could reset is
not a count of looks (decided 2026-09-14). The same rule measured again under
other costs in another experiment is a second trial there, and still ONE
rule: it does not raise ``n``. And ``n`` is the count at PROMOTE time, not the
trial's own ordinal — the look-elsewhere problem is about how many rules were
tried, and an early trial promoted after five hundred others were measured
was chosen from five hundred and one.

**Is this the same rule again?** A spec whose :func:`~.dsl.spec_hash` is
already in the experiment is not a second trial: the evaluator is
deterministic, so it would be the same numbers, not another look. The ledger
refuses the duplicate row, and the caller reports the trial it duplicates.

**Who may see the holdout?** Nobody, until a trial passes the gate and an
operator promotes it — once (plan §3.8). The store enforces the shape:
holdout figures exist on a row if and only if it is promoted. And the holdout
has to be the SAME calendar window for every experiment on a coin, which is
the one lock the store cannot give by itself (plan §11): ``Split.by_shares``
cuts the span an experiment was created over, so as the store grows a new
experiment's validation would slide over the window an earlier one withheld.
The first experiment on a coin pins its holdout start, and every later one
must start its holdout at exactly that instant. Exactly — not "no earlier",
which the plan first wrote: an EARLIER start would put bars an earlier
experiment's trials were selected on inside the new holdout, and a LATER one
would put the old holdout inside the new validation. Either way the holdout
stops being history nothing was chosen on. It may grow at the far end.

**What does a promotion gate read?** ``ruined`` before any ratio, and the
Sharpe beside the return and the trade count — a Sharpe of 0 is not "did
nothing" (plan §11, decided 2026-09-14). See :func:`promotion_verdict`.

Rows are decoded back into the same frozen values the evaluator built —
``CostModel``, ``Split``, ``StrategySpec``, ``SegmentMetrics`` — through each
type's own strict ``from_dict``/parser, so a row that a later build can no
longer read is refused NAMING the row rather than rendered as something it
was not. This module imports nothing that computes a feature: ``report`` reads
the ledger alone and must not pay for pandas.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Final

from .costs import CostModel, require_amount
from .dsl import StrategySpec, parse_spec, spec_hash, spec_to_document
from .metrics import SegmentMetrics
from .split import Segment, Split
from .store import ResearchStore, _utcnow_iso, canonical_coin
from .upstream import VocabEnum, from_epoch_ms
from .vocabulary import SpecError, require_number

__all__ = [
    "PENALTY_K",
    "SHARPE_BASE",
    "Experiment",
    "Ledger",
    "LedgerError",
    "Penalty",
    "SearchTrial",
    "Trial",
    "TrialStatus",
    "Verdict",
    "promotion_verdict",
]

# Plan §3.10's starting values. Overridable per experiment, and recorded there
# when they are, because a threshold is part of what "promoted" meant.
SHARPE_BASE: Final = 1.0
PENALTY_K: Final = 0.25

# What an experiment may be called: it is typed on every command line that
# reads the experiment, and printed in every report.
_EXPERIMENT_ID: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class LedgerError(RuntimeError):
    """The ledger cannot do what was asked, and the sentence says why.

    A ``RuntimeError`` rather than a ``ValueError``: the refusals here are
    about the state of the store — an experiment that exists already, a trial
    promoted already, a row a later build cannot read — not about a malformed
    argument, and the CLI names both families on its exit-1 lane.
    """


class TrialStatus(VocabEnum, noun="trial status"):
    """Where a trial stands. ``measured`` -> ``promoted``, once, and never back."""

    MEASURED = "measured"
    PROMOTED = "promoted"


@dataclass(frozen=True)
class Penalty:
    """The promote threshold as a function of how many trials the experiment holds.

    ``threshold(n) = sharpe_base + k * ln(n)``: the first trial must reach the
    base, and every tenfold increase in trials adds ``k * ln 10`` (0.58 at the
    defaults). Deflated Sharpe is the principled version and is deferred
    (plan §8); this is the simple one plan §6.7 asks to see working.
    """

    sharpe_base: float = SHARPE_BASE
    k: float = PENALTY_K

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "sharpe_base", require_number(self.sharpe_base, "Penalty.sharpe_base")
            )
        except SpecError as exc:
            raise ValueError(str(exc)) from exc
        # A negative k would LOWER the bar as more rules are tried: the one
        # direction a multiple-comparison penalty cannot point.
        object.__setattr__(self, "k", require_amount(self.k, "Penalty.k"))

    def threshold(self, trials: int) -> float:
        if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
            raise ValueError(
                f"a threshold is for an experiment holding at least one trial, got {trials!r}"
            )
        return self.sharpe_base + self.k * math.log(trials)

    def describe(self, trials: int) -> str:
        return (
            f"promote threshold at {trials} rule(s) tried on this coin: validation net sharpe >= "
            f"{self.threshold(trials):.2f} ({self.sharpe_base:g} + {self.k:g} × ln {trials})"
        )

    def to_dict(self) -> dict[str, float]:
        """The record an experiment writes (``penalty_json``)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: object) -> Penalty:
        fields = set(cls.__dataclass_fields__)
        if not isinstance(payload, Mapping) or set(payload) != fields:
            shown = sorted(payload) if isinstance(payload, Mapping) else payload
            raise ValueError(
                f"a penalty record has exactly the keys {sorted(fields)}, got {shown!r}"
            )
        return cls(**payload)


@dataclass(frozen=True)
class Experiment:
    """The fixed conditions every trial inside one experiment is measured under."""

    experiment_id: str
    coin: str
    costs: CostModel
    split: Split
    indicator_lookback: int
    penalty: Penalty = Penalty()
    notes: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.experiment_id, str) or not _EXPERIMENT_ID.fullmatch(
            self.experiment_id
        ):
            raise ValueError(
                f"an experiment is named with letters, digits, '_', '.' and '-' (up to 64, "
                f"starting with a letter or digit), got {self.experiment_id!r}"
            )
        object.__setattr__(self, "coin", canonical_coin(self.coin))
        for name, kind in (("costs", CostModel), ("split", Split), ("penalty", Penalty)):
            if not isinstance(getattr(self, name), kind):
                raise ValueError(
                    f"an experiment's {name} is a {kind.__name__}, got {getattr(self, name)!r}"
                )
        lookback = self.indicator_lookback
        if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 1:
            raise ValueError(f"indicator_lookback is a whole number of bars, got {lookback!r}")
        if not isinstance(self.notes, str):
            raise ValueError(f"notes are text, got {self.notes!r}")

    def describe(self) -> list[str]:
        lines = [f"experiment {self.experiment_id}: {self.coin} {self.split.interval}"]
        if self.created_at:
            lines[0] += f", created {self.created_at}"
        if self.notes:
            lines.append(f"notes: {self.notes}")
        lines.append(self.costs.describe())
        lines.append(f"indicator window: {self.indicator_lookback} bars")
        lines += self.split.describe()
        return lines


@dataclass(frozen=True)
class Trial:
    """One distinct rule measured inside an experiment, as the ledger holds it."""

    trial_id: int
    experiment_id: str
    family: str
    spec: StrategySpec
    spec_hash: str
    train: SegmentMetrics
    validation: SegmentMetrics
    holdout: SegmentMetrics | None
    status: TrialStatus
    created_at: str
    promoted_at: str | None

    def __post_init__(self) -> None:
        # The table's two CHECKs, held by the value too: a Trial built by hand
        # (a test, a preview) must not be able to say "measured" with holdout
        # figures on it, and a reader may branch on either half.
        if not isinstance(self.status, TrialStatus):
            raise ValueError(f"a trial's status is a TrialStatus, got {self.status!r}")
        promoted = self.status is TrialStatus.PROMOTED
        if promoted != (self.holdout is not None) or promoted != (self.promoted_at is not None):
            raise ValueError(
                f"a trial carries holdout figures and a promotion time if and only if it is "
                f"promoted; got status {self.status.value} with holdout "
                f"{'present' if self.holdout is not None else 'absent'} and promoted_at "
                f"{self.promoted_at!r}"
            )
        # ``family`` is a column for SQL, and a copy of the spec's: a row whose
        # two disagree would be counted in ``report`` under a label its rule
        # does not carry.
        if self.family != self.spec.family.value:
            raise ValueError(
                f"a trial's family column says {self.family!r} and its spec says "
                f"{self.spec.family.value!r}"
            )

    @property
    def segments(self) -> tuple[SegmentMetrics, ...]:
        measured = (self.train, self.validation)
        return measured if self.holdout is None else (*measured, self.holdout)

    def for_search(self) -> SearchTrial:
        """What a search may be shown of this trial: train and validation, never the holdout."""
        return SearchTrial(
            trial_id=self.trial_id,
            experiment_id=self.experiment_id,
            family=self.family,
            spec=self.spec,
            spec_hash=self.spec_hash,
            train=self.train,
            validation=self.validation,
            created_at=self.created_at,
        )


@dataclass(frozen=True)
class SearchTrial:
    """A trial as a hypothesis search may see it (plan §3.11: the loop never sees the holdout).

    Not a :class:`Trial` with ``holdout=None``: that value would say "not
    promoted" about a trial that was, and a reader could not tell a withheld
    figure from an absent one. This type has no holdout to leak — the
    failure summary a search builds from the ledger is built from these
    (:meth:`Ledger.search_trials`, and :func:`~.research.measure` answers a
    duplicate with one). Whether the trial was promoted is still readable
    from the gate's blockers; plan B1 decides whether that too is withheld.
    """

    trial_id: int
    experiment_id: str
    family: str
    spec: StrategySpec
    spec_hash: str
    train: SegmentMetrics
    validation: SegmentMetrics
    created_at: str

    @property
    def segments(self) -> tuple[SegmentMetrics, ...]:
        return (self.train, self.validation)


@dataclass(frozen=True)
class Verdict:
    """Whether a trial may be promoted, the threshold it was held to, and what stopped it."""

    trials: int
    threshold: float
    blockers: tuple[str, ...]

    @property
    def eligible(self) -> bool:
        return not self.blockers


def promotion_verdict(experiment: Experiment, trial: Trial, trials: int) -> Verdict:
    """The promote gate, read off the ledger's figures for the VALIDATION window.

    Every blocker is listed, not only the first:

    1. **Not already promoted** — a trial sees its holdout once (plan §3.8).
    2. **Not ruined** — ``ruined`` is read before any ratio; a ruined run's
       Sharpe is diluted by the flat bars booked after the ruin and can read
       as anything.
    3. **At least one trade** — a threshold lowered to zero or below would
       otherwise promote a rule that never traded, on a Sharpe of 0.
    4. **A positive net return** — the Sharpe is read beside the return: a
       series that lost the same amount at every bar has no deviation and
       reads 0 as well.
    5. **Net Sharpe at or above** :meth:`Penalty.threshold` of the CURRENT
       number of distinct rules tried on the coin (:meth:`Ledger.rules_tried`).

    Only annualised figures would be comparable across windows:
    ``total_return`` is a whole-window total, and validation is a third the
    length of train under the default split (plan §11). So there is no
    train-to-validation decay gate built on totals here; ``report`` prints the
    two Sharpes side by side instead.
    """
    threshold = experiment.penalty.threshold(trials)
    validation = trial.validation
    blockers = []
    if trial.status is TrialStatus.PROMOTED:
        blockers.append(f"trial #{trial.trial_id} has already been promoted")
    if validation.ruined:
        blockers.append("its validation run was ruined, and no ratio of a ruined run is read")
    if validation.trades == 0:
        blockers.append("it made no trades in validation")
    if validation.net.total_return <= 0:
        blockers.append(
            f"its validation net return is {validation.net.total_return:+.2%}, not positive"
        )
    if validation.net.sharpe < threshold:
        blockers.append(
            f"its validation net sharpe {validation.net.sharpe:.2f} is below {threshold:.2f} "
            f"({experiment.penalty.sharpe_base:g} + {experiment.penalty.k:g} × ln {trials})"
        )
    return Verdict(trials=trials, threshold=threshold, blockers=tuple(blockers))


def _dumps(payload: object) -> str:
    # ``allow_nan=False``: the evaluator guarantees finite figures, and a NaN
    # that got past it must fail HERE rather than become text JSON cannot read.
    return json.dumps(payload, sort_keys=True, allow_nan=False)


class Ledger:
    """The two ledger tables of one open :class:`~.store.ResearchStore`."""

    def __init__(self, store: ResearchStore) -> None:
        self.store = store

    # -- experiments -------------------------------------------------------

    def create_experiment(self, experiment: Experiment) -> Experiment:
        """Write ``experiment`` once, stamped; refuse a name or a holdout start that clashes.

        The holdout pin is checked inside the write transaction, so two
        experiments created at once cannot both be "the first" on a coin. The
        NAME is checked before it: re-running a creation after the store grew
        would otherwise be told about a moved holdout, when what happened is
        that the experiment already exists.
        """
        stamped = replace(experiment, created_at=_utcnow_iso())
        with self.store.transaction() as conn:
            taken = conn.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?", (stamped.experiment_id,)
            ).fetchone()
            if taken is not None:
                raise LedgerError(_name_taken(stamped.experiment_id))
            pin = self._holdout_pin(conn, stamped.coin)
            start = stamped.split.holdout.start_ms
            if pin is not None and start != pin:
                raise LedgerError(
                    f"{stamped.coin}'s holdout begins at {_instant(pin)}, pinned by the first "
                    f"experiment on it, and this split's begins at {_instant(start)}. Every "
                    f"experiment on a coin withholds the same window: an earlier start would put "
                    f"bars trials were chosen on inside the holdout, a later one would put the "
                    f"old holdout inside validation."
                )
            penalty = self._penalty_pin(conn, stamped.coin)
            if penalty is not None and stamped.penalty != penalty:
                raise LedgerError(_penalty_moved(stamped.coin, penalty, stamped.penalty))
            try:
                conn.execute(
                    "INSERT INTO experiments (experiment_id, coin, created_at, cost_params_json,"
                    " split_json, indicator_lookback, penalty_json, notes)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        stamped.experiment_id,
                        stamped.coin,
                        stamped.created_at,
                        _dumps(stamped.costs.to_dict()),
                        _dumps(stamped.split.to_dict()),
                        stamped.indicator_lookback,
                        _dumps(stamped.penalty.to_dict()),
                        stamped.notes,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError(_name_taken(stamped.experiment_id)) from exc
        return stamped

    def holdout_pin(self, coin: str) -> int | None:
        """The holdout start the first experiment on ``coin`` fixed, or ``None`` if none exists."""
        return self._holdout_pin(self.store.conn, canonical_coin(coin))

    def _holdout_pin(self, conn: sqlite3.Connection, coin: str) -> int | None:
        # ``rowid`` order, not ``created_at``: insertion order is the fact, and
        # two stamps in the same microsecond would tie.
        row = conn.execute(
            "SELECT experiment_id, split_json FROM experiments WHERE coin = ?"
            " ORDER BY rowid LIMIT 1",
            (coin,),
        ).fetchone()
        if row is None:
            return None
        try:
            return Split.from_dict(json.loads(row["split_json"])).holdout.start_ms
        except ValueError as exc:
            raise LedgerError(
                f"experiment {row['experiment_id']}'s split record cannot be read, and it is "
                f"the one that pins {coin}'s holdout: {exc}"
            ) from exc

    def penalty_pin(self, coin: str) -> Penalty | None:
        """The promote penalty the first experiment on ``coin`` fixed, or ``None`` if none exists.

        Pinned for the same reason ``n`` is counted per coin (decided
        2026-09-14): a later experiment with ``k = 0`` or a lower base would
        lower the bar every rule on the coin has to clear, one flag away from
        the reset a per-coin count exists to close.
        """
        return self._penalty_pin(self.store.conn, canonical_coin(coin))

    def _penalty_pin(self, conn: sqlite3.Connection, coin: str) -> Penalty | None:
        row = conn.execute(
            "SELECT experiment_id, penalty_json FROM experiments WHERE coin = ?"
            " ORDER BY rowid LIMIT 1",
            (coin,),
        ).fetchone()
        if row is None:
            return None
        try:
            return Penalty.from_dict(json.loads(row["penalty_json"]))
        except ValueError as exc:
            raise LedgerError(
                f"experiment {row['experiment_id']}'s penalty record cannot be read, and it is "
                f"the one that pins {coin}'s promote threshold: {exc}"
            ) from exc

    def experiment(self, experiment_id: str) -> Experiment:
        row = self.store.conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(
                f"this store has no experiment named {experiment_id!r} — `report` with no "
                f"--experiment lists the ones it has"
            )
        return self._experiment(row)

    def require_stored(self, experiment: Experiment) -> None:
        """Refuse an ``Experiment`` value that is not the row this store holds under its name.

        A trial is filed against the conditions it was measured under, and
        :func:`_require_window` can only compare figures with the VALUE it is
        handed. A value built or planned (``research.plan_experiment`` writes
        nothing) under a stored name, with another split or other costs, would
        file a trial the stored experiment never had the windows for — in an
        append-only ledger, raising the coin's rule count for good. Read the
        experiment back with :meth:`experiment` instead.
        """
        self._require_stored(self.store.conn, experiment)

    def _require_stored(self, conn: sqlite3.Connection, experiment: Experiment) -> None:
        row = conn.execute(
            "SELECT * FROM experiments WHERE experiment_id = ?", (experiment.experiment_id,)
        ).fetchone()
        if row is None:
            raise LedgerError(
                f"this store has no experiment named {experiment.experiment_id!r} to file the "
                f"trial under — an experiment held in memory is not one the store holds"
            )
        stored = self._experiment(row)
        if replace(experiment, created_at=stored.created_at) != stored:
            raise LedgerError(
                f"experiment {experiment.experiment_id} is stored with other conditions than the "
                f"ones offered — read it back from the ledger rather than building or planning one"
            )

    def experiments(self) -> list[Experiment]:
        rows = self.store.conn.execute("SELECT * FROM experiments ORDER BY rowid").fetchall()
        return [self._experiment(row) for row in rows]

    # -- trials ------------------------------------------------------------

    def record_trial(
        self,
        experiment: Experiment,
        spec: StrategySpec,
        train: SegmentMetrics,
        validation: SegmentMetrics,
    ) -> Trial:
        """Write one measured trial; refuse figures for other windows, and a rule measured already."""
        _require_window(experiment, train, experiment.split.train)
        _require_window(experiment, validation, experiment.split.validation)
        digest = spec_hash(spec)
        with self.store.transaction() as conn:
            self._require_stored(conn, experiment)
            try:
                cursor = conn.execute(
                    "INSERT INTO trials (experiment_id, family, spec_json, spec_hash,"
                    " train_metrics_json, validation_metrics_json, holdout_metrics_json, status,"
                    " created_at, promoted_at) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)",
                    (
                        experiment.experiment_id,
                        spec.family.value,
                        _dumps(spec_to_document(spec)),
                        digest,
                        _dumps(train.to_dict()),
                        _dumps(validation.to_dict()),
                        TrialStatus.MEASURED.value,
                        _utcnow_iso(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # Read on this connection, still inside the failed transaction:
                # a lock taken elsewhere cannot make the duplicate's number
                # disagree with the refusal it is quoted in.
                existing = conn.execute(
                    "SELECT trial_id FROM trials WHERE experiment_id = ? AND spec_hash = ?",
                    (experiment.experiment_id, digest),
                ).fetchone()
                if existing is None:
                    # Not the UNIQUE constraint, so not a duplicate: say what
                    # sqlite said rather than guess which constraint it was.
                    raise LedgerError(
                        f"the trial could not be filed in {experiment.experiment_id}: {exc}"
                    ) from exc
                raise LedgerError(
                    f"this rule was already measured in {experiment.experiment_id} as trial "
                    f"#{existing[0]}; measuring it again is the same numbers, not another trial"
                ) from exc
        return self.trial(experiment.experiment_id, int(cursor.lastrowid))

    def trial(self, experiment_id: str, trial_id: int) -> Trial:
        row = self.store.conn.execute(
            "SELECT * FROM trials WHERE experiment_id = ? AND trial_id = ?",
            (experiment_id, trial_id),
        ).fetchone()
        if row is None:
            raise LedgerError(f"experiment {experiment_id} has no trial #{trial_id}")
        return self._trial(row)

    def trial_by_hash(self, experiment_id: str, digest: str) -> Trial | None:
        row = self.store.conn.execute(
            "SELECT * FROM trials WHERE experiment_id = ? AND spec_hash = ?",
            (experiment_id, digest),
        ).fetchone()
        return None if row is None else self._trial(row)

    def trials(self, experiment_id: str) -> list[Trial]:
        rows = self.store.conn.execute(
            "SELECT * FROM trials WHERE experiment_id = ? ORDER BY trial_id", (experiment_id,)
        ).fetchall()
        return [self._trial(row) for row in rows]

    def search_trials(self, experiment_id: str) -> list[SearchTrial]:
        """Every trial of the experiment as a search may see it — no holdout figure on any."""
        return [trial.for_search() for trial in self.trials(experiment_id)]

    def rules_tried(self, coin: str) -> int:
        """Distinct rules measured on ``coin`` in any experiment, promoted or not — plan §3.10's ``n``.

        Counted by ``spec_hash`` across every experiment on the coin, all of
        which withhold the same holdout (see the module docstring for why not
        per experiment).
        """
        return self.store.conn.execute(
            "SELECT COUNT(DISTINCT trials.spec_hash) FROM trials"
            " JOIN experiments ON experiments.experiment_id = trials.experiment_id"
            " WHERE experiments.coin = ?",
            (canonical_coin(coin),),
        ).fetchone()[0]

    def holdout_looks(self, coin: str) -> int:
        """How many times ``coin``'s holdout has been measured: every promotion, in any experiment.

        Not a limit — a count, printed where a promotion happens and where the
        ledger is read. Each promotion is one more look at the one window the
        pin keeps unchosen-on, and after enough of them it is not that window.
        """
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM trials"
            " JOIN experiments ON experiments.experiment_id = trials.experiment_id"
            " WHERE experiments.coin = ? AND trials.status = ?",
            (canonical_coin(coin), TrialStatus.PROMOTED.value),
        ).fetchone()[0]

    def verdict(self, experiment: Experiment, trial: Trial) -> Verdict:
        """:func:`promotion_verdict` at the coin's CURRENT rule count — the one gate call."""
        return promotion_verdict(experiment, trial, self.rules_tried(experiment.coin))

    def trial_counts(self, experiment_id: str) -> tuple[int, int]:
        """``(trials, promoted)``, counted in SQL for a listing that shows no figures."""
        row = self.store.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(status = ?), 0) FROM trials WHERE experiment_id = ?",
            (TrialStatus.PROMOTED.value, experiment_id),
        ).fetchone()
        return row[0], row[1]

    def promote(self, experiment: Experiment, trial: Trial, holdout: SegmentMetrics) -> Trial:
        """Mark ``trial`` promoted with its holdout figures. Once: a second call is refused.

        The gate is the caller's to have applied (:func:`promotion_verdict`).
        What this enforces is the part a caller cannot be trusted with: that
        the figures are for THIS experiment's holdout window, and that the row
        leaves ``measured`` exactly once — decided by the UPDATE's own
        ``WHERE``, not by a read taken before it.
        """
        _require_window(experiment, holdout, experiment.split.holdout)
        with self.store.transaction() as conn:
            self._require_stored(conn, experiment)
            cursor = conn.execute(
                "UPDATE trials SET status = ?, holdout_metrics_json = ?, promoted_at = ?"
                " WHERE experiment_id = ? AND trial_id = ? AND status = ?",
                (
                    TrialStatus.PROMOTED.value,
                    _dumps(holdout.to_dict()),
                    _utcnow_iso(),
                    experiment.experiment_id,
                    trial.trial_id,
                    TrialStatus.MEASURED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise LedgerError(
                    f"trial #{trial.trial_id} of {experiment.experiment_id} is not a measured "
                    f"trial awaiting promotion — a trial sees its holdout once"
                )
        return self.trial(experiment.experiment_id, trial.trial_id)

    # -- decoding rows -----------------------------------------------------

    def _experiment(self, row: sqlite3.Row) -> Experiment:
        name = row["experiment_id"]
        try:
            return Experiment(
                experiment_id=name,
                coin=row["coin"],
                costs=CostModel.from_dict(json.loads(row["cost_params_json"])),
                split=Split.from_dict(json.loads(row["split_json"])),
                indicator_lookback=row["indicator_lookback"],
                penalty=Penalty.from_dict(json.loads(row["penalty_json"])),
                notes=row["notes"],
                created_at=row["created_at"],
            )
        except ValueError as exc:
            raise LedgerError(
                f"experiment {name}'s record cannot be read by this build: {exc}"
            ) from exc

    def _trial(self, row: sqlite3.Row) -> Trial:
        label = f"trial #{row['trial_id']} of {row['experiment_id']}"
        try:
            holdout_text = row["holdout_metrics_json"]
            return Trial(
                trial_id=row["trial_id"],
                experiment_id=row["experiment_id"],
                family=row["family"],
                spec=parse_spec(json.loads(row["spec_json"])),
                spec_hash=row["spec_hash"],
                train=SegmentMetrics.from_dict(json.loads(row["train_metrics_json"])),
                validation=SegmentMetrics.from_dict(json.loads(row["validation_metrics_json"])),
                holdout=None
                if holdout_text is None
                else SegmentMetrics.from_dict(json.loads(holdout_text)),
                status=TrialStatus(row["status"]),
                created_at=row["created_at"],
                promoted_at=row["promoted_at"],
            )
        except ValueError as exc:
            # A spec this build's vocabulary no longer accepts lands here too
            # (``SpecError`` is a ``ValueError``): the row is kept, and named.
            raise LedgerError(f"{label} cannot be read by this build: {exc}") from exc


def _require_window(experiment: Experiment, metrics: SegmentMetrics, segment: Segment) -> None:
    if metrics.segment != segment:
        raise LedgerError(
            f"figures for {metrics.segment} were offered as {experiment.experiment_id}'s "
            f"{segment}; a trial's figures are for the experiment's own windows"
        )


def _name_taken(experiment_id: str) -> str:
    return (
        f"this store already has an experiment named {experiment_id!r}; an experiment's "
        f"conditions are written once — name the new one differently"
    )


def _penalty_moved(coin: str, pinned: Penalty, offered: Penalty) -> str:
    return (
        f"{coin}'s promote threshold is sharpe_base {pinned.sharpe_base:g}, k {pinned.k:g}, pinned "
        f"by the first experiment on it, and this experiment asks for sharpe_base "
        f"{offered.sharpe_base:g}, k {offered.k:g}. The rules tried on a coin are counted across "
        f"its experiments, so every experiment on it holds them to the same bar."
    )


def _instant(ms: int) -> str:
    return from_epoch_ms(ms).isoformat()
