"""The summary arm's hypothesist (tools plan §3.7, T6): a model handed a fixed summary of the training window, and no tools.

The tool arm (:mod:`.toolarm`) differs from the blind model arm in two
things at once: its model can SEE figures of the training window, and it
CHOOSES which to look at. This arm takes the first and leaves the second
out. Before every answer the model is shown the same block of figures —
where each feature's values lie on the training bars — and answers once,
as the blind arm does: no tool, no server, no second message. Set beside
the blind arm it says what the plain sight of those figures is worth; set
beside the tool arm, what is left for the look-ups a model picks itself.

**What the block holds** (decided 2026-11-24): one line for EVERY feature
of the vocabulary, in the vocabulary's order, and nothing else. No feature
is picked or dropped here — a choice of which features are worth showing
would be a piece of the search done for the model, and one made by someone
who knows what the benchmark plants. And each line is the answer the
``feature_stats`` look-up gives for that feature, in the text the look-ups'
server sends it as: the same fields, the same rounding, the same spelling.
So what this arm is handed is what the tool arm would read had it asked
that one tool about every feature, and a difference between the two arms
is not a difference in the figures. Nothing of the other two look-ups is in
it — no return after a signal, no backtest of a rule: computing those for
the model is searching for it (plan §3.7).

**Where it goes** (decided 2026-11-24): appended to the research package's
system prompt, where the tool arm's paragraph goes, and not to the user
prompt. The block is the same every round, the system prompt is the half
that is; and the user prompt's last line stays the instruction to answer
with one rule. The paragraph that opens the block states facts and gives
no advice, as the tool arm's does (decided 2026-11-19, for that arm): what
the figures are of, that the validation bars stay hidden, that the block
does not change, what a field means — the last in the look-ups' server's
own sentences (:func:`~.tools_server.stats_fields`). It also answers
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
figures' own making — which fields an answer carries, how a cut is
interpolated, how a figure is rounded: those are the look-up's
(:mod:`.research_tools`), held by its own tests and by the test that a line
here is the text the server answers with, and a change to them is a change
to the tool arm's pack as well. A manifest names the version
(``arms.llm_summary.summary_version``) and the bench refuses one that is
not this build's. Unlike the tool arm's pack, the digest is written to no
record: this arm leaves no trace, and a block is recomputed from the arm's
own store.

What this arm does NOT control for is said where results are written up:
the tool arm also has the two look-ups that are not in the block, and room
to write between calls, so "tools above summary" is the worth of choosing
AND of those two; "summary above blind" is the worth of the figures alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from .research_tools import TrainView, call
from .tools_server import stats_fields
from .upstream import ChatHypothesist, feature_names

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

# The version of everything a model of this arm reads beyond the research
# package's two prompts: the paragraph below, the features listed and the
# fields of a line. One number for the whole of it, as the tool arm's pack
# has; the suite pins a digest beside it, so an edit without a bump is red.
# Settled before any result of the tool arm is seen (plan §3.7) and not
# moved while results of the two are set side by side.
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
    sentence. The fields are described in the look-ups' server's own lines,
    so this arm and the tool arm are told what ``p25`` is in one sentence.
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
    order, each a JSON object carrying exactly a ``feature_stats`` answer's
    keys. So a hypothesist built by hand cannot put another paragraph, a
    feature more or fewer, or a line with anything but those fields in
    front of a model. The VALUES are not checked here — they are a world's,
    and :meth:`over` is how a block of real ones is made.
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
        listed: list[object] = []
        for line in self.block[len(opening) :].split("\n"):
            try:
                answer = json.loads(line)
            except ValueError:
                answer = None
            if not isinstance(answer, dict) or tuple(answer) not in shapes:
                raise ValueError(
                    f"a line of the block is not a feature's figures — a JSON object with the "
                    f"keys {list(shapes[0])}, or {list(shapes[1])} for the regime; build it "
                    f"with summary()"
                )
            listed.append(answer["feature"])
        if listed != names:
            raise ValueError(
                f"the block lists {len(listed)} feature(s) and not the vocabulary's "
                f"{len(names)}, each once and in its order; build it with summary()"
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
