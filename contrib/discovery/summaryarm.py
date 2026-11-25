"""The summary arm's hypothesist (tools plan §3.7, T6): a model handed a fixed summary of the training window, and no tools.

The tool arm (:mod:`.toolarm`) differs from the blind model arm in, above
all, two things at once: its model can SEE figures of the training window,
and it CHOOSES which to look at. This arm takes the first and leaves the
second out. Before every answer the model is shown the same block of
figures — where each feature's values lie on the training bars — and
answers once, as the blind arm does: no tool, no server, no second
message. Set beside the blind arm it says what the plain sight of those
figures is worth, at this length and in this place (the last section
below). Set beside the tool arm it says less cleanly what that
arm's choosing adds, because the two differ in more than the choosing (the
last section below).

**What the block holds** (decided 2026-11-24): one line for EVERY feature
of the vocabulary, in the vocabulary's order, and nothing else. No feature
is picked or dropped here — a choice of which features are worth showing
would be a piece of the search done for the model, and one made by someone
who knows what the benchmark plants. And each line is the answer the
``feature_stats`` look-up gives for that feature, in the text the look-ups'
server sends it as: the same fields, the same rounding, the same spelling.
So for any one feature the two arms read the same text. They do not read
the same AMOUNT of it: this arm has every line before every answer, where
the tool arm may make up to as many look-ups a round as its manifest
allows (the plan starts from five, of any tool) and keeps none for the
next — at any limit short of the vocabulary's size, this arm is shown
where MORE FEATURES' values lie than that arm can have this look-up answer
in a round. (More features, not more of everything: that arm's other
look-ups answer what no line here does.) Nothing of the other two
look-ups is in the block — no return after a signal, no backtest of a
rule: computing those for the model is searching for it (plan §3.7).

**Where it goes** (decided 2026-11-24): appended to the research package's
system prompt, where the tool arm's paragraph goes, and not to the user
prompt. The block is the same every round, the system prompt is the half
that is; and the user prompt's last line stays the instruction to answer
with one rule. The paragraph that opens the block states facts and gives
no advice, as the tool arm's does (decided 2026-11-19, for that arm): what
the figures are of, that the validation bars stay hidden, that the block
does not change, what a field means — the last in the look-ups' server's
own sentences (:func:`~.tools_server.stats_fields`) for every field but
``feature``, which the server calls "the feature asked about" and nobody
asked for a line of this. It also answers
the one line of the system prompt the figures make untrue as written,
"history you cannot see".

**The line** is the tool arm's. The figures come from a
:class:`~.research_tools.TrainView`, which holds no bar after the training
window, through the JSON layer the server stands on
(:func:`~.research_tools.call`); nothing here reads a store, a manifest or
a trial. A feature the stored history cannot answer is that layer's error
(``EvaluationError``, ``FeatureError``) and is left to propagate: a block
with a feature missing would be another condition under the same version.

:data:`SUMMARY_VERSION` versions what this module decides of what the
arm's model reads beyond the two prompts — the paragraph, which features
are listed, how an answer is written out as a line, and what stands
between the system prompt and the block — and the suite pins a digest of
exactly that beside it (:func:`wording_digest`), so an edit of any of it
without a bump is red: one here, one to the server's sentences, or a
feature added to the vocabulary. What the digest does NOT cover is the
figures' own making — how a cut is interpolated, how a figure is brought
to its digits, which keys an answer in fact carries: those are the
look-up's (:mod:`.research_tools`), held by its own tests and by the test
that a line here is the text the server answers with, and a change to them
is a change to the tool arm's pack as well. (What the paragraph SAYS of
them — the fields it lists, the number of digits — is in the paragraph,
and so is under the digest.) A manifest names the version
(``arms.llm_summary.summary_version``) and the bench refuses one that is
not this build's. Unlike the tool arm's pack, the digest is written to no
record: this arm leaves no trace, and a block is recomputed from the arm's
own store.

**The definition is tied to the tools' wording, by a rule** (decided
2026-11-25). The field sentences are the server's, and the server's
wording is frozen only after the tool arm's first trial with a real model
(plan §5). If that freeze edits one of those sentences, this arm's version
is raised with it and its digest re-pinned — and that is the ONLY edit of
this arm's wording allowed once a tool-arm result has been seen: the
paragraph's own sentences, the features listed and where the block goes
are not moved with it. The alternative, a copy of the sentences here,
would keep this text still and let the two arms describe one field in two
ways.

**What a comparison with this arm can and cannot say** is said where
results are written up, and comes to this. Against the blind arm, this arm
adds the figures and nothing else — but a long block of them, 42 lines of
which about half are price levels, placed before a list of scored rules
that grows: if length or position costs a model anything, this arm
understates what sight of the window is worth. Against this arm, the tool
arm differs in several things at once that cannot be told apart, these
four above all: it chooses what to look up; it has the two look-ups that
are not in the block; it may write a line before each call; and its tool
is described with a sentence on what the figures are for ("use it before
you choose a threshold…"), where this arm's paragraph gives no advice
(decided 2026-11-25: not added here, so that "summary above blind" stays
the figures alone). (It is also told other words than this arm is: its
own paragraph on the tools, and a line after each result saying how many
calls are left.) And at any limit short of the vocabulary's size it sees
fewer features' figures a round than this arm, so the tool arm coming out
below this one is a possible outcome, not a contradiction — and the two
coming out level may be gains and losses cancelling, not a sign that
nothing in the list matters. Whichever way it comes out, what is measured
is the net of every difference between the two arms, this arm's length
and position among them; the sign of no one of them is known beforehand.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Final

from .research_tools import TrainView, call
from .tools_server import StatsFields, stats_fields
from .upstream import (
    ChatHypothesist,
    FeatureRef,
    FeatureUnit,
    MarketRegime,
    feature_names,
    parse_feature_name,
)

__all__ = [
    "AFTER_SYSTEM_PROMPT",
    "STATS_TOOL",
    "SUMMARY_VERSION",
    "SummaryHypothesist",
    "figures",
    "preamble",
    "summary",
    "wording",
    "wording_digest",
]

# The version of what THIS MODULE decides of what the arm's model reads
# beyond the research package's two prompts: the paragraph below, the
# features listed, how an answer is written out as a line, what stands
# before the block (:func:`wording`; the figures' own making is the
# look-up's, and is not under this number). The suite pins a digest beside
# it, so an edit without a bump is red. Settled before any result of the
# tool arm is seen (plan §3.7); after one is, it moves only with the
# server's field sentences when those are frozen (the module docstring's
# rule), and for no other reason.
SUMMARY_VERSION: Final = 1

# The look-up each line of the block is an answer of.
STATS_TOOL: Final = "feature_stats"

# What stands between the research package's system prompt and the block:
# an empty line, as before the tool arm's paragraph.
AFTER_SYSTEM_PROMPT: Final = "\n\n"

# An answer of no world, written out as a line: what the digest holds the
# spelling of a line by (the separators, the order of the keys kept, how a
# number is printed).
_SAMPLE_ANSWER: Final = {"feature": "a_feature", "bars": 2, "bars_with_value": 1, "min": 0.5}

_HEADING: Final = "TRAINING-BAR FIGURES you are given before you answer:"
_WHAT: Final = (
    "The prompt above says a rule is scored on history you cannot see. Part of that history is "
    "described below: the training bars. Every feature of the vocabulary above is listed once, "
    "with where its values lie on the training bars; no bar after them was read for it. The "
    "validation bars stay hidden from you, and the validation figures a scored rule is listed "
    "with are measured on them."
)
_SAME: Final = (
    "- The figures are the same every round: they describe the training bars, and nothing you "
    "have proposed."
)
_ANSWER: Final = (
    "- Your answer is still what the prompt asks for: ONE new rule as a single JSON object, and "
    "nothing else."
)
_LINES: Final = "Each line below is one JSON object, with:"
_FEATURE: Final = "- feature: the feature the line describes."


def preamble() -> str:
    """The paragraph that opens the block: what the figures are, and what each field of a line means.

    Facts, and no advice: a sentence on how to use a figure would be read
    by this arm alone, and a result could not then be told from the
    sentence. Every field but ``feature`` is described in the look-ups'
    server's own line, so this arm and the tool arm are told what ``p25``
    is in one sentence; ``feature`` has a line of this module's.
    """
    fields = stats_fields()
    return "\n".join(
        [
            _HEADING,
            _WHAT,
            _SAME,
            _ANSWER,
            _LINES,
            _FEATURE,
            *fields.numeric,
            fields.regime_intro,
            *fields.regime,
            fields.rounding,
        ]
    )


def _line(answer: object) -> str:
    """A look-up's answer as one line of the block: as the look-ups' server writes it into a tool result."""
    return json.dumps(answer, allow_nan=False)


def figures(view: TrainView) -> tuple[str, ...]:
    """One line for every feature of the vocabulary, in its order: the ``feature_stats`` answer over ``view``.

    Each is the look-up's answer through the JSON layer, written as the
    look-ups' server writes it into a tool result (``json.dumps`` with
    ``allow_nan=False``), so a line here and the tool arm's answer for the
    same feature are one text. A feature the history cannot answer raises
    as that layer does; none is skipped.
    """
    return tuple(_line(call(view, STATS_TOOL, {"feature": name})) for name in feature_names())


def summary(view: TrainView) -> str:
    """The whole block appended to the system prompt: the paragraph, then the lines."""
    return "\n".join([preamble(), *figures(view)])


def wording() -> dict[str, object]:
    """What :data:`SUMMARY_VERSION` versions: everything of what the model is sent that this module decides.

    The paragraph, the features listed, how an answer is written out as a
    line (shown on an answer of no world) and what stands between the
    system prompt and the block. Not a world's figures, and not how the
    look-up makes them (the module docstring says who holds that).
    """
    return {
        "summary_version": SUMMARY_VERSION,
        "preamble": preamble(),
        "features": list(feature_names()),
        "tool": STATS_TOOL,
        "sample_line": _line(_SAMPLE_ANSWER),
        "after_system_prompt": AFTER_SYSTEM_PROMPT,
    }


def wording_digest() -> str:
    """A digest of :func:`wording`, pinned by the suite beside the version."""
    written = json.dumps(wording(), sort_keys=True)
    return hashlib.sha256(written.encode("utf-8")).hexdigest()


def _no_constant(token: str) -> object:
    """Refuse ``NaN`` and the infinities where a line is read back: a line is written with none of them."""
    raise ValueError(f"{token} is not a figure")


def _count(value: object) -> bool:
    return type(value) is int and value >= 0


def _figure(value: object) -> bool:
    """A finite ``float``: what the look-up writes every figure as.

    Its figures are rounded through ``float`` whatever they came from, so a
    whole number spelled without its ``.0`` — a ``bool`` among them, an
    ``int`` to ``isinstance`` — is no figure of its.
    """
    return type(value) is float and math.isfinite(value)


def _written_as(line: str, answer: object) -> bool:
    """Whether ``line`` is ``answer`` as :func:`_line` writes it, character for character.

    Reading a line back is not enough to hold it: a reader keeps the LAST
    of a key written twice, and takes any spacing and any escape, so a
    line can parse to a faultless answer and still put other characters in
    front of a model — the first value of the doubled key above all. What
    is sent is the line, so the line is what is held.
    """
    try:
        return _line(answer) == line
    except ValueError:
        # A number the reader made that JSON has no token for (``1e999``
        # reads as an infinity): no line of a look-up's.
        return False


def _reads_as_figures(name: str, answer: dict[str, object], fields: StatsFields) -> bool:
    """Whether ``answer`` has the shape ``feature_stats`` answers ``name`` in: its keys, and a value of each key's kind.

    The regime's keys for the regime and a number's for every other
    feature — a line in the other's shape describes nothing the look-up
    would say of that feature. Counts are whole and not negative, a figure
    is a finite ``float``, and the regime's counts are a whole number for
    every label of the vocabulary, in its order, and for nothing else — as
    the look-up lists them, so no word of a caller's own can stand where a
    label does. The values themselves are a world's and are not judged.
    """
    labelled = FeatureRef(*parse_feature_name(name)).unit is FeatureUnit.REGIME
    if tuple(answer) != (fields.regime_keys if labelled else fields.numeric_keys):
        return False
    counted = ("bars", "bars_with_value")
    if not all(_count(answer[key]) for key in counted):
        return False
    if labelled:
        counts = answer["counts"]
        return (
            isinstance(counts, dict)
            and tuple(counts) == tuple(regime.value for regime in MarketRegime)
            and all(_count(count) for count in counts.values())
        )
    return all(
        _figure(answer[key])
        for key in fields.numeric_keys
        if key != "feature" and key not in counted
    )


@dataclass(frozen=True)
class SummaryHypothesist:
    """A model asked once a round, with the training window's figures appended to its system prompt.

    ``asked`` is the research package's own adapter over a chat client,
    the one the blind model arm is; ``block`` is :func:`summary` of the
    arm's own training window, computed once, since that window does not
    change between rounds. :meth:`propose` hands on the two prompts with
    the block after the first, and returns what the adapter returns: a
    failed seam is the adapter's ``HypothesistError``, and an answer is
    text the loop parses. Nothing is kept between rounds.

    The block is held to this build's shape at construction: the
    paragraph, then one line for each feature of the vocabulary in its
    order, each a JSON object carrying exactly the keys ``feature_stats``
    answers THAT feature with, under each a value of its kind (a count, a
    finite number, the regime's counts by label) — and each WRITTEN as the
    look-up's answer is, character for character, since what a model is
    sent is the line and not what the line reads back as. So a hypothesist
    built by hand cannot put another paragraph, a feature more or fewer, a
    line with anything but those fields, or words where a figure goes in
    front of a model.

    What is NOT checked is which numbers they are, nor how they stand to
    one another (that the values with a count do not outnumber the bars,
    that the cuts ascend, that every line counts the same bars): those are
    a world's, the look-up's own records hold them where it answers, and
    :meth:`over` is how a block of this arm's own is made.
    """

    asked: ChatHypothesist
    block: str

    def __post_init__(self) -> None:
        if not isinstance(self.asked, ChatHypothesist):
            raise TypeError(
                f"a summary arm asks a ChatHypothesist, got {type(self.asked).__name__}"
            )
        opening = preamble() + "\n"
        if not isinstance(self.block, str) or not self.block.startswith(opening):
            raise ValueError(
                "the block does not open with this build's paragraph; build it with summary()"
            )
        names = list(feature_names())
        # The keys a line carries are the look-up's, as its own description
        # lists them: a numeric feature's, or the regime's.
        fields = stats_fields()
        shapes = (fields.numeric_keys, fields.regime_keys)
        answers: list[dict[str, object]] = []
        for line in self.block[len(opening) :].split("\n"):
            try:
                answer = json.loads(line, parse_constant=_no_constant)
            except ValueError:
                answer = None
            if (
                not isinstance(answer, dict)
                or tuple(answer) not in shapes
                or not _written_as(line, answer)
            ):
                raise ValueError(
                    f"a line of the block is not a feature's figures — a JSON object with the "
                    f"keys {list(shapes[0])}, or {list(shapes[1])} for the regime, each key "
                    f"once and the whole written as the look-up's answer is; build it with "
                    f"summary()"
                )
            answers.append(answer)
        listed = [answer["feature"] for answer in answers]
        if listed != names:
            raise ValueError(
                f"the block lists {len(listed)} feature(s) and not the vocabulary's "
                f"{len(names)}, each once and in its order; build it with summary()"
            )
        for name, answer in zip(names, answers, strict=True):
            if not _reads_as_figures(name, answer, fields):
                raise ValueError(
                    f"the block's line for {name} is not that feature's figures: counts of "
                    f"bars, and finite numbers or the regime's counts by label, under the "
                    f"keys its look-up answers with; build it with summary()"
                )

    @classmethod
    def over(cls, asked: ChatHypothesist, view: TrainView) -> SummaryHypothesist:
        """The hypothesist that asks ``asked`` with the figures of ``view``'s training window."""
        return cls(asked=asked, block=summary(view))

    @property
    def label(self) -> str:
        """The model's own label: what the loop files this arm's answers under."""
        return self.asked.label

    @property
    def model(self) -> object:
        """The client the adapter holds — a counted one, where the bench put a counter around it."""
        return self.asked.model

    def propose(self, system: str, user: str) -> str:
        return self.asked.propose(f"{system}{AFTER_SYSTEM_PROMPT}{self.block}", user)
