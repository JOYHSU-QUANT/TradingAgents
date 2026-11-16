"""The training-window look-ups offered over MCP: a stdio server around :mod:`.research_tools` (tools plan §2.2, §3.3; T2).

A tool-using arm (plan T3) does not call the look-ups: it asks a server
that runs in a process of its own, over MCP on that process's stdin and
stdout. This module is that server, what it advertises, and the one way it
is started.

**The line, as a process (plan §2.2).** The server is started with five
things and nothing else (:class:`ServerParams`): the path of a research
store, a coin, the experiment's whole split, its costs and its indicator
window. It reads the store once, no further than the last training bar
(:meth:`~.research_tools.TrainView.load`), closes it, and answers from
memory. It is handed no manifest and imports none of the modules that read
one, so the world's kind and whatever is planted in it are not in the
process; and the validation and holdout bars are not in it either. The
process that runs the search does hold a frame read to the validation
window's end, which is why the look-ups are served from another one.

**What is advertised** (:func:`tool_specs`). The three look-ups under the
names :mod:`.research_tools` gives them, each with a description, an input
schema and an output schema. Whatever a schema can say is read from the
research package's own tables: the feature names are the vocabulary's
(``feature_names``), the operators, sides and regime labels are the
language's enums, the bounds are the parser's. A rule is advertised as an
object and no more (decided 2026-11-16): a JSON Schema of the whole
language would be a second grammar kept by hand beside the parser's.

**What is enforced.** Not the schemas. The SDK's low-level ``Server`` is
used because it advertises a schema and applies none (decided 2026-11-16,
against the plan's first wording): the arguments reach
:func:`~.research_tools.call` as they were sent, so a mistake is refused in
the parser's own sentence, and a key the tool does not read is refused
rather than dropped. The high-level server would have answered in its
validator's words and dropped the key.

**Three ways a call ends.** An answer: the look-up's ``dict``, as text and
as structured content. A refusal (:class:`~.research_tools.ToolRefusal`):
a tool result marked as an error, carrying the sentence, which the model is
meant to read. And a history that cannot answer (``EvaluationError``,
``FeatureError``): a protocol error with a fixed sentence
(:data:`STORE_CANNOT_ANSWER`). The research package's own sentence names
calendar instants, which nothing a model is shown carries, so it goes to
this process's stderr and not over the wire; the caller is to treat a
protocol error as a failed seam, not as a tool result. Any other exception
is a defect on this side: it is logged with its traceback and answered with
a second fixed sentence (:data:`LOOKUP_FAILED`). It is not left to the SDK,
whose answer to an unexpected exception depends on the handshake the client
opened with — under the older one it is the exception's own text.

**Started warm** (:func:`warm`). Every feature of the vocabulary whose
series the store holds is computed before the first request is read, so the
first look-up costs what the hundredth does and a caller's timing of a call
is the call's. The price is paid before ``initialize`` is answered: about
fourteen seconds on a 5,000-bar world, twelve of them the indicator walk.

**Stopped by an interrupt, at once.** The SDK reads stdin on a worker
thread, and a blocked read cannot be interrupted, so a serving process left
to ``KeyboardInterrupt`` does not stop until its stdin closes. Run as a
process, the module handles the interrupt itself and leaves
(:func:`_leave_on_interrupt`); it has nothing to flush, since it writes
nothing.

**No ``$ref`` in a schema.** A condition's schema is written out whole
(:func:`_feature_schema`): a provider's tool-calling layer that does not
resolve a reference would show its model a condition with no shape, and
nothing here could tell.

The SDK is an optional install (``pip install -e ".[discovery-tools]"``)
and is imported inside the functions that need it: this module imports
without it, so the package's other commands do not depend on it.

No look-up writes. The store is opened as every reader of it opens it,
though, and that migrates one an older build wrote: so the server is to be
pointed at an arm's own copy, which is what the benchmark has, and never
at a landed artifact, whose bytes its run directory is held to — the
``tools`` command serves a throw-away copy for that reason.
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple, TypeVar

from .research_tools import (
    MAX_HORIZON,
    MIN_HORIZON,
    QUANTILES,
    SIGNIFICANT_DIGITS,
    TOOL_NAMES,
    ToolRefusal,
    TrainView,
    ViewError,
    call,
    percentile_key,
)
from .upstream import (
    MAX_OFFSET_BARS,
    CostModel,
    EvaluationError,
    Experiment,
    FeatureError,
    FeatureRef,
    MarketRegime,
    Op,
    ResearchStore,
    SeriesSource,
    Side,
    Split,
    StoreError,
    feature_names,
    parse_feature_name,
)

if TYPE_CHECKING:
    from mcp.server import Server

__all__ = [
    "IMPORT_ROOT",
    "LOOKUP_FAILED",
    "SERVER_NAME",
    "STORE_CANNOT_ANSWER",
    "TOOLS_VERSION",
    "LaunchError",
    "ServerParams",
    "ToolSpec",
    "build_server",
    "load_view",
    "main",
    "require_sdk",
    "serve",
    "tool_specs",
    "warm",
]

logger = logging.getLogger(__name__)
_R = TypeVar("_R")

SERVER_NAME: Final = "discovery-research-tools"

# The wording this server advertises: the tools' names, descriptions and
# schemas. Bumped whenever any of that changes, and reported as the server's
# version. It covers what the SERVER says and no more: the text a tool-using
# arm adds to its prompt, and its call limits, are that arm's to version
# (plan T3). The wording is frozen after the first trial with a real model
# (plan §5); the suite pins a digest of it beside this number, so an edit
# without a bump is red.
TOOLS_VERSION: Final = 1

# The whole of what a caller is told when the history cannot answer a
# look-up. Fixed, and naming no instant: the research package's sentence
# goes to stderr.
STORE_CANNOT_ANSWER: Final = "the store cannot answer this look-up"

# The whole of what a caller is told when a look-up fails in a way this
# module did not foresee — a defect on this side. Fixed for the reason the
# sentence above is: an exception's own text may name an instant.
LOOKUP_FAILED: Final = "the look-up failed"

# Where ``contrib`` is importable from: the directory a child serving these
# tools is started in, since ``python -m`` reads the package off its cwd.
IMPORT_ROOT: Final = Path(__file__).resolve().parents[2]

_MODULE: Final = "contrib.discovery.tools_server"


class LaunchError(ValueError):
    """The server cannot be started as asked, and the sentence says why."""


# -- how it is started -----------------------------------------------------------------------


@dataclass(frozen=True)
class ServerParams:
    """Everything a server is started with: a store, a coin, and the conditions a trial is measured under.

    The five things plan §2.2 lists and no sixth. ``split`` is the
    experiment's whole split — the view reads the training window off it —
    and has to be the one the arm's experiment was stored with, which is
    what :meth:`for_experiment` is for. ``db`` is made absolute: the child
    is started in :data:`IMPORT_ROOT`, not where the caller stands.
    """

    db: Path
    coin: str
    split: Split
    costs: CostModel
    indicator_lookback: int

    def __post_init__(self) -> None:
        if not isinstance(self.db, (str, Path)) or not str(self.db):
            raise LaunchError(f"db: expected the path of a research store, got {self.db!r}")
        object.__setattr__(self, "db", Path(self.db).resolve())
        if not isinstance(self.coin, str) or not self.coin:
            raise LaunchError(f"coin: expected a coin's name, got {self.coin!r}")
        if not isinstance(self.split, Split) or not isinstance(self.costs, CostModel):
            raise LaunchError(
                f"expected the experiment's split and costs, got {type(self.split).__name__} "
                f"and {type(self.costs).__name__}"
            )
        if isinstance(self.indicator_lookback, bool) or not isinstance(
            self.indicator_lookback, int
        ):
            raise LaunchError(
                f"indicator_lookback: expected a whole number of bars, got "
                f"{self.indicator_lookback!r}"
            )

    @classmethod
    def for_experiment(cls, db: Path, experiment: Experiment) -> ServerParams:
        """The parameters for ``experiment``'s own conditions over the store at ``db``.

        The way a caller builds them: the coin, the split, the costs and
        the window are read off one experiment — the one the arm stored —
        so none of them can be another experiment's.
        """
        return cls(
            db=db,
            coin=experiment.coin,
            split=experiment.split,
            costs=experiment.costs,
            indicator_lookback=experiment.indicator_lookback,
        )

    def argv(self) -> list[str]:
        """The parameters as the server's command line; :meth:`from_argv` reads them back.

        Each as one ``--flag=value`` argument, so a value that begins with
        a dash is still its flag's value.
        """
        return [
            f"--db={self.db}",
            f"--coin={self.coin}",
            f"--split={json.dumps(self.split.to_dict(), sort_keys=True)}",
            f"--costs={json.dumps(self.costs.to_dict(), sort_keys=True)}",
            f"--indicator-lookback={self.indicator_lookback}",
        ]

    def command(self) -> list[str]:
        """The whole command that starts a server on these parameters, to be run in :data:`IMPORT_ROOT`."""
        return [sys.executable, "-m", _MODULE, *self.argv()]

    @classmethod
    def from_argv(cls, argv: Sequence[str]) -> ServerParams:
        args = _parser().parse_args(list(argv))
        return cls(
            db=Path(args.db),
            coin=args.coin,
            split=_read_record("--split", args.split, Split.from_dict),
            costs=_read_record("--costs", args.costs, CostModel.from_dict),
            indicator_lookback=args.indicator_lookback,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"python -m {_MODULE}",
        description=(
            "Serve the training-window look-ups of one experiment over MCP on stdin and "
            "stdout. Started by the benchmark, or by `python -m contrib.discovery tools`."
        ),
    )
    parser.add_argument(
        "--db",
        required=True,
        help="the research store to read: an arm's own copy, never a landed artifact",
    )
    parser.add_argument("--coin", required=True, help="the coin the experiment is on")
    parser.add_argument("--split", required=True, help="the experiment's split, as its JSON record")
    parser.add_argument(
        "--costs", required=True, help="the experiment's costs, as their JSON record"
    )
    parser.add_argument(
        "--indicator-lookback", required=True, type=int, help="the experiment's indicator window"
    )
    return parser


def _read_record(flag: str, text: str, read: Callable[[dict[str, object]], _R]) -> _R:
    """One of the experiment's records off the command line, through its own ``from_dict``.

    The record's own refusal (a key missing, a window out of order, a rate
    below zero) is a launch refusal under the flag that carried it.
    """
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LaunchError(f"{flag}: not JSON ({exc})") from exc
    if not isinstance(record, dict):
        raise LaunchError(f"{flag}: expected a JSON object, got {type(record).__name__}")
    try:
        return read(record)
    except ValueError as exc:
        # The two records refuse with a ``ValueError`` of their own; anything
        # else out of them is a defect, and stays a traceback.
        raise LaunchError(f"{flag}: {exc}") from exc


def load_view(params: ServerParams) -> TrainView:
    """The view a server answers from: the store read to the last training bar, and closed.

    The store has to exist, and to hold something — opening a path creates
    a store there, and an empty file is made into one, and a server that
    did either would report a history with no bars instead of a path that
    was wrong.

    Each part refuses as itself: a path with no store in it and a store
    that does not hold the split's window are a :class:`LaunchError`; a
    file that is no research store is the store's ``StoreError``; a coin
    the store holds nothing of, or a history off its grid, is the research
    package's ``EvaluationError``, and an indicator window it will not
    compute over its ``FeatureError``. :func:`main` ends all four as exit 1.
    """
    if not params.db.is_file() or params.db.stat().st_size == 0:
        raise LaunchError(
            f"{params.db} is not a file with a store in it: the server reads an existing "
            f"research store and creates none"
        )
    with ResearchStore(params.db) as store:
        try:
            return TrainView.load(
                store,
                coin=params.coin,
                split=params.split,
                costs=params.costs,
                indicator_lookback=params.indicator_lookback,
            )
        except ViewError as exc:
            # The view's own refusal of the frame it read: the split names
            # a window this store's bars do not run over. A fact about the
            # parameters, so it is a launch refusal and not a traceback.
            # That class alone: any other ``ValueError`` under the read is
            # a defect, and is not to be told as the operator's mistake.
            raise LaunchError(
                f"{params.db} does not hold the training window the split names for "
                f"{params.coin}: {exc}"
            ) from exc


def warm(view: TrainView) -> None:
    """Compute every feature of the vocabulary once, so no look-up pays for it later.

    A frame computes a feature when it is first read and keeps it; the
    indicator walk is most of the cost, and every backtest needs it for
    the regime whether or not its rule reads an indicator. Left to the
    first request, that is twelve seconds on a 5,000-bar world inside one
    call (plan §9, T1's timing), read by whoever times the call as a slow
    tool.

    A feature whose series this store does not hold at all — no daily bars,
    no settlements — is passed over, and the log says which: there is
    nothing to compute, and a look-up that needs that series will end as
    :data:`STORE_CANNOT_ANSWER` while the rest are answered. (With no
    settlements that is every backtest too, whatever its rule reads: a
    position is charged funding.) That is decided by looking at what the
    store holds, not by catching whatever a computation raises (decided
    2026-11-16). A feature whose series IS held and still cannot be
    computed is no missing series — the indicator engine returning nothing
    above all, but also a series that yields no value at any bar, as a
    funding rate that never moves has no z-score: the error is left to stop
    the launch, where an operator reads it, rather than be met one slow
    look-up at a time. A landed world holds all three series and computes
    every feature.
    """
    bundle = view.frame.bundle
    held = {
        SeriesSource.BARS: bool(bundle.bars),
        SeriesSource.DAILY: bool(bundle.daily),
        SeriesSource.FUNDING: bool(bundle.funding),
    }
    for name in feature_names():
        ref = FeatureRef(*parse_feature_name(name))
        if not held[ref.source]:
            logger.warning("not warmed: %s (the store holds no %s series)", name, ref.source.value)
            continue
        view.frame.series(ref)


# -- what is advertised ------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolSpec:
    """One look-up as a model is shown it: a name, a description, the two schemas, and an example.

    ``example`` is the arguments of the call the description shows, held
    as data so the suite can make that call; a spec whose description does
    not show it is refused. The schemas and the example are built afresh
    for each spec (:func:`tool_specs`), so changing one changes no other.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    example: dict[str, Any]

    def __post_init__(self) -> None:
        if self.name not in TOOL_NAMES:
            raise ValueError(f"{self.name!r} is not a look-up; the look-ups are {list(TOOL_NAMES)}")
        shown = f"Call: {json.dumps(self.example)}"
        if shown not in self.description:
            raise ValueError(f"{self.name}: the description does not show its example ({shown})")


class _Field(NamedTuple):
    """One output field: its name, its JSON type (or types), and what it means.

    The meaning is written once and read twice — into the description a
    model reads and into the output schema.
    """

    name: str
    kind: str | tuple[str, ...]
    meaning: str


def _fields(*rows: tuple[str, str | tuple[str, ...], str]) -> tuple[_Field, ...]:
    return tuple(_Field(*row) for row in rows)


_WINDOW: Final = (
    "Reads the training bars only: no tool can see the validation window. A look-up is not "
    "scored and does not use up a proposal."
)
_ROUNDING: Final = f"Figures are rounded to {SIGNIFICANT_DIGITS} significant digits."

_NUMBER_OR_NULL: Final = ("number", "null")


def _stats_fields() -> tuple[tuple[_Field, ...], tuple[_Field, ...]]:
    """The fields of a numeric feature's answer, and of the regime's."""
    head = _fields(
        ("feature", "string", "the feature asked about"),
        ("bars", "integer", "training bars in the window"),
        ("bars_with_value", "integer", "training bars at which the feature has a value"),
    )
    numeric = (
        *head,
        _Field("min", "number", "the smallest value"),
        *(
            _Field(percentile_key(q), "number", f"the {round(q * 100)}th percentile of the values")
            for q in QUANTILES
        ),
        _Field("max", "number", "the largest value"),
    )
    labelled = (
        *head,
        _Field(
            "counts",
            "object",
            "bars per regime label; every label is listed, with 0 for one that never occurred",
        ),
    )
    return numeric, labelled


_RETURNS_FIELDS: Final = _fields(
    ("bars", "integer", "training bars in the window"),
    ("bars_holding", "integer", "bars at which every condition holds"),
    (
        "bars_unevaluable",
        "integer",
        "bars at which a condition read a feature that has no value there",
    ),
    ("horizon", "integer", "as asked"),
    ("side", "string", "as asked"),
    (
        "samples",
        "integer",
        "how many of the holding bars the next three figures are computed from. Usually fewer "
        "than bars_holding, for two reasons: samples are taken in bar order and at least "
        "`horizon` bars apart, so that no two share a bar of return; and a signal in the last "
        "`horizon` bars has no whole return inside the window and is dropped",
    ),
    (
        "mean_return",
        _NUMBER_OR_NULL,
        "mean over the samples of close[t + horizon] / close[t] - 1, negated for short: a raw "
        "price move per `horizon` bars, before costs and without leverage. Not on the scale of "
        "backtest_train's net_return, which is an account's return after costs. null with no "
        "sample",
    ),
    (
        "std_return",
        _NUMBER_OR_NULL,
        "sample standard deviation of those returns; null with fewer than 2 samples",
    ),
    (
        "t_stat",
        _NUMBER_OR_NULL,
        "mean_return / (std_return / sqrt(samples)): a one-sample t-statistic of mean_return "
        "against zero. It is not corrected for how many conditions you have looked at; read it "
        "together with samples and baseline_mean_return. null with fewer than 2 samples, or "
        "when the samples do not vary",
    ),
    (
        "baseline_samples",
        "integer",
        "how many samples the baseline is computed from: one every `horizon` bars from the "
        "window's first bar",
    ),
    (
        "baseline_mean_return",
        _NUMBER_OR_NULL,
        "the same return over the same horizon and side with NO condition: what the window did "
        "whatever the signal said. null when the window is shorter than the horizon",
    ),
)

_BACKTEST_FIELDS: Final = _fields(
    ("bars", "integer", "training bars in the window"),
    (
        "bars_unevaluable",
        "integer",
        "bars at which the rule could not be asked, because a feature it reads has no value there",
    ),
    ("trades", "integer", "positions opened"),
    ("exposure", "number", "share of the bars on which a position was held, 0 to 1"),
    ("turnover", "number", "notional traded, as a multiple of the account's mean equity"),
    (
        "net_sharpe",
        "number",
        "annualised Sharpe ratio of the per-bar returns after costs: the figure a proposal's "
        "validation sharpe is, measured on training instead",
    ),
    (
        "net_return",
        "number",
        "the account's total return over the window after fees, slippage and funding, at the "
        "rule's sizing and the experiment's leverage, as a fraction (0.05 is +5%)",
    ),
    ("ruined", "boolean", "true if the account was emptied; read no other figure of such a run"),
)


def _listed(fields: Sequence[_Field]) -> list[str]:
    return [f"- {field.name}: {field.meaning}." for field in fields]


def _object_schema(fields: Sequence[_Field], **overrides: dict[str, Any]) -> dict[str, Any]:
    """A closed object carrying exactly ``fields``; ``overrides`` replaces a named field's schema."""
    names = [field.name for field in fields]
    unknown = sorted(set(overrides) - set(names))
    if unknown:
        raise ValueError(f"no field named {unknown} to override among {names}")

    def schema(field: _Field) -> dict[str, Any]:
        if field.name in overrides:
            return overrides[field.name]
        kind = field.kind if isinstance(field.kind, str) else list(field.kind)
        return {"type": kind, "description": field.meaning}

    return {
        "type": "object",
        "properties": {field.name: schema(field) for field in fields},
        "required": names,
        "additionalProperties": False,
    }


# The calls the descriptions show. An example is the one place a
# description could say where to look, so these are written AWAY from what
# the benchmark plants: a world's rule enters on a funding feature, gates on
# the regime or on realised volatility, and leaves on ``max_bars``
# (discovery plan §4.1), and the examples read the RSI alone and leave on
# it. The research package's prompt shows no example rule at all, so
# whatever these suggest, only the tool arm is shown it; pointing away from
# the answer can cost that arm a recovery and cannot hand it one. The suite
# holds the examples to that.
_STATS_EXAMPLE: Final[dict[str, Any]] = {"feature": "rsi_14"}
_RETURNS_EXAMPLE: Final[dict[str, Any]] = {
    "conditions": [{"left": "rsi_14", "op": "<", "right": 30}],
    "horizon": 8,
    "side": "long",
}
_BACKTEST_EXAMPLE: Final[dict[str, Any]] = {
    "rule": {
        "family": "mean_reversion",
        "entry": {"long": [{"left": "rsi_14", "op": "<", "right": 30}]},
        "exit": {"long": [{"left": "rsi_14", "op": ">", "right": 50}]},
        "sizing": {"mode": "fixed_margin_fraction", "fraction": 0.5},
    }
}


def _feature_schema() -> dict[str, Any]:
    """A feature as a condition names one: its name, or its name read ``offset`` bars back.

    Written out whole at each place a condition takes one, with no
    ``$ref`` to a shared definition: a model provider's tool-calling layer
    may not resolve a reference, and one that drops it shows its model a
    condition with no shape. The price is the vocabulary's names four
    times over in one schema.
    """

    def name() -> dict[str, Any]:
        return {"type": "string", "enum": list(feature_names())}

    return {
        "description": "A feature by name, or read `offset` bars back.",
        "anyOf": [
            name(),
            {
                "type": "object",
                "properties": {
                    "feature": name(),
                    "offset": {"type": "integer", "minimum": 0, "maximum": MAX_OFFSET_BARS},
                },
                "required": ["feature"],
                "additionalProperties": False,
            },
        ],
    }


def _feature_stats_spec() -> ToolSpec:
    names = list(feature_names())
    regimes = [regime.value for regime in MarketRegime]
    numeric, labelled = _stats_fields()
    counts = {
        "type": "object",
        "description": labelled[-1].meaning,
        "properties": {label: {"type": "integer"} for label in regimes},
        "required": regimes,
        "additionalProperties": False,
    }
    description = "\n".join(
        [
            "Where one feature's values lie on the training bars. Use it before you choose a "
            "threshold: a condition only fires if the feature actually reaches the number you "
            "compare it with.",
            _WINDOW,
            f"Call: {json.dumps(_STATS_EXAMPLE)}",
            "Returns, for a numeric feature:",
            *_listed(numeric),
            f"For regime, which is a label and not a number, the {len(QUANTILES) + 2} figures "
            f"are replaced by:",
            *_listed(labelled[-1:]),
            _ROUNDING,
        ]
    )
    return ToolSpec(
        name="feature_stats",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "feature": {
                    "type": "string",
                    "enum": names,
                    "description": "A feature name, without an offset.",
                }
            },
            "required": ["feature"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "oneOf": [_object_schema(numeric), _object_schema(labelled, counts=counts)],
        },
        example=copy.deepcopy(_STATS_EXAMPLE),
    )


def _conditional_returns_spec() -> ToolSpec:
    regimes = [regime.value for regime in MarketRegime]
    sides = [side.value for side in Side]
    long, short = Side.LONG.value, Side.SHORT.value
    description = "\n".join(
        [
            "What the close did over the next `horizon` bars after a signal, on the training "
            "bars. Use it to check whether ONE signal has anything after it before you build a "
            "rule on it. It reads the price move alone: no position, no costs. For what a whole "
            "rule scores after costs, use backtest_train.",
            _WINDOW,
            "Arguments:",
            '- conditions: one or more conditions, ANDed, written exactly as under "entry" in a '
            'rule: {"left": feature, "op": op, "right": feature | number | regime label}. A '
            'threshold has to be a number here: {"param": name} is refused, because a bare list '
            "of conditions has no params block to name.",
            f"- horizon: how many bars ahead to look, {MIN_HORIZON} to {MAX_HORIZON} (the range "
            f"of exit.max_bars).",
            f'- side: "{long}" reads the move as it is, "{short}" negates it. The two '
            f"are exact negatives of each other, so asking both tells you nothing new.",
            f"Call: {json.dumps(_RETURNS_EXAMPLE)}",
            "Returns:",
            *_listed(_RETURNS_FIELDS),
            "The samples are not a rule's trades: a rule fills at the next bar's open and does "
            "not enter again on the bar it leaves, so its entries fall later.",
            _ROUNDING,
        ]
    )
    return ToolSpec(
        name="conditional_returns",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "conditions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "left": _feature_schema(),
                            "op": {"type": "string", "enum": [op.value for op in Op]},
                            "right": {
                                "anyOf": [
                                    _feature_schema(),
                                    {"type": "number"},
                                    {"type": "string", "enum": regimes},
                                ]
                            },
                        },
                        "required": ["left", "op", "right"],
                        "additionalProperties": False,
                    },
                    "description": "Conditions that must all hold at a bar.",
                },
                "horizon": {
                    "type": "integer",
                    "minimum": MIN_HORIZON,
                    "maximum": MAX_HORIZON,
                    "description": "Bars to look ahead.",
                },
                "side": {"type": "string", "enum": sides},
            },
            "required": ["conditions", "horizon", "side"],
            "additionalProperties": False,
        },
        output_schema=_object_schema(
            _RETURNS_FIELDS,
            side={"type": "string", "enum": list(sides), "description": "as asked"},
        ),
        example=copy.deepcopy(_RETURNS_EXAMPLE),
    )


def _backtest_train_spec() -> ToolSpec:
    description = "\n".join(
        [
            "One complete rule scored on the training bars, after costs: the backtest that "
            "scores a proposal, run on the training window alone. Use it to see what a whole "
            "rule (entries, exits, filters, sizing) would have made in training before you "
            "propose it.",
            _WINDOW,
            "Arguments:",
            "- rule: the rule as one JSON object, exactly the object you would give as your "
            "answer, in the language a proposal is written in (a params block and "
            '{"param": name} thresholds are read here). A rule tried here can be proposed as '
            "it stands.",
            f"Call: {json.dumps(_BACKTEST_EXAMPLE)}",
            "Returns:",
            *_listed(_BACKTEST_FIELDS),
            _ROUNDING,
        ]
    )
    return ToolSpec(
        name="backtest_train",
        description=description,
        input_schema={
            "type": "object",
            "properties": {
                "rule": {
                    "type": "object",
                    "description": "A complete rule, as a proposal is written.",
                }
            },
            "required": ["rule"],
            "additionalProperties": False,
        },
        output_schema=_object_schema(_BACKTEST_FIELDS),
        example=copy.deepcopy(_BACKTEST_EXAMPLE),
    )


def tool_specs() -> tuple[ToolSpec, ...]:
    """The three look-ups as advertised, in :data:`~.research_tools.TOOL_NAMES`' order.

    Built on each call from the research package's tables, so a feature
    added to the vocabulary is in the next listing — and in the digest the
    suite pins, which is what says the tools' version has to move.
    """
    specs = (_feature_stats_spec(), _conditional_returns_spec(), _backtest_train_spec())
    if tuple(spec.name for spec in specs) != TOOL_NAMES:
        raise RuntimeError(
            f"the tools advertised ({[spec.name for spec in specs]}) are not the look-ups "
            f"there are ({list(TOOL_NAMES)})"
        )
    return specs


# -- the server --------------------------------------------------------------------------------


def build_server(view: TrainView) -> Server[Any]:
    """An MCP server answering the three look-ups from ``view`` (the module docstring's three endings)."""
    from mcp import MCPError, types
    from mcp.server import Server, ServerRequestContext

    tools = [
        types.Tool(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            annotations=types.ToolAnnotations(
                read_only_hint=True, idempotent_hint=True, open_world_hint=False
            ),
        )
        for spec in tool_specs()
    ]

    async def on_list_tools(
        ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def on_call_tool(
        ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        # No arguments at all is an empty object, so the refusal names the
        # keys that are missing rather than the type of nothing.
        arguments = {} if params.arguments is None else params.arguments
        try:
            document = call(view, params.name, arguments)
            # ``allow_nan=False``: a figure that is not a number is a defect
            # on this side, and is not to reach the wire as a token JSON lacks.
            text = json.dumps(document, allow_nan=False)
        except ToolRefusal as exc:
            return types.CallToolResult(content=[types.TextContent(text=str(exc))], is_error=True)
        except (EvaluationError, FeatureError) as exc:
            logger.error("%s: %s: %s", params.name, STORE_CANNOT_ANSWER, exc)
            raise MCPError(types.INTERNAL_ERROR, STORE_CANNOT_ANSWER) from exc
        except Exception as exc:
            # A defect. Not left to the SDK: to a client that opened with the
            # older handshake it answers with the exception's own text
            # (measured on mcp 2.3.0), and that text may name an instant.
            logger.exception("%s: %s", params.name, LOOKUP_FAILED)
            raise MCPError(types.INTERNAL_ERROR, LOOKUP_FAILED) from exc
        return types.CallToolResult(
            content=[types.TextContent(text=text)], structured_content=document
        )

    return Server(
        SERVER_NAME,
        version=str(TOOLS_VERSION),
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


def serve(view: TrainView) -> None:
    """Answer requests on this process's stdin and stdout until stdin closes.

    An interrupt does NOT end this by itself (see :func:`_leave_on_interrupt`):
    the process that runs the module installs that; a program that calls
    this inside itself stops its server its own way.
    """
    import anyio
    from mcp.server.stdio import stdio_server

    server = build_server(view)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)


def _leave_on_interrupt() -> None:
    """End this process at once on an interrupt, with the code the package's commands use.

    The SDK reads stdin on a worker thread and a blocked read cannot be
    interrupted, so a serving process left to ``KeyboardInterrupt`` does
    not stop until its stdin closes: a person's Ctrl-C did nothing
    (measured on Windows, 2026-11-16). Leaving at once loses nothing at any
    point of the process's life — it writes nothing, to the store or
    anywhere else. Installed for the whole of that life by the module's
    entry point and by nothing else: it is the process's to decide, not a
    caller's of :func:`main` or :func:`serve` inside another program. And
    for Windows' Ctrl-Break as for Ctrl-C, which otherwise ends the process
    without a word.
    """
    if threading.current_thread() is not threading.main_thread():
        return  # signals are the main thread's

    def leave(_signum: int, _frame: object) -> None:
        try:
            # ``os.write``: the handler may have interrupted a write to stderr.
            os.write(2, b"interrupted\n")
        finally:
            # Whatever became of the message (a reader that went away): leave.
            os._exit(130)

    for name in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), leave)


def require_sdk() -> None:
    """Refuse without the MCP SDK this module is written against, before anything is read.

    Asked for by the names the server uses, so an older SDK under the same
    package name is refused here too, and not after the store has been
    read and the frame warmed for nothing.
    """
    try:
        import anyio  # noqa: F401
        from mcp import MCPError, types  # noqa: F401
        from mcp.server import Server, ServerRequestContext  # noqa: F401
        from mcp.server.stdio import stdio_server  # noqa: F401
    except ImportError as exc:
        raise LaunchError(
            f"the MCP SDK this server is written against cannot be imported ({exc}); if it is "
            f"not installed, it is this package's optional install: pip install -e "
            f'".[discovery-tools]"'
        ) from exc


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m contrib.discovery.tools_server``: read the store, warm the frame, serve.

    Exit codes as the package's commands have them: ``0`` when the client
    closed stdin; ``1`` a named refusal to START, with the sentence on
    stderr — the parameters, a store that cannot be opened or does not hold
    the window, a history the research package will not measure on, the
    SDK missing; ``2`` argparse's usage errors; ``130`` interrupted. An
    error once serving has begun is no refusal and is left as it is raised
    (a client that stopped reading, say). Nothing but the protocol is
    written to stdout.
    """
    logging.basicConfig(
        level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        try:
            params = ServerParams.from_argv(sys.argv[1:] if argv is None else argv)
            require_sdk()
            view = load_view(params)
            try:
                warm(view)
            except FeatureError as exc:
                # Said as what it is: nothing was wrong with the launch
                # until a feature could not be computed, and the server
                # starts with every feature it holds a series for, or not.
                raise LaunchError(
                    f"the warm-up could not compute a feature of a series this store holds, "
                    f"and the server does not start without it: {exc}"
                ) from exc
        except (LaunchError, StoreError, EvaluationError, FeatureError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        serve(view)
    except KeyboardInterrupt:
        # A caller of this function inside another program, or an interrupt
        # before serving; the module run as a process leaves by its own
        # handler at any point (:func:`_leave_on_interrupt`).
        print("interrupted", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    _leave_on_interrupt()
    raise SystemExit(main())
