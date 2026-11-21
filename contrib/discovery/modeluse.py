"""What a model arm with no tools spent (issue #433): its calls counted as they come back, and the record a table reads.

The tool arm's spending is read off its trace into ``tool_use.json``
(:mod:`.tooluse`). The blind model arm (``arms.llm``) writes no trace: it is
asked through the research package's ``ChatHypothesist``, which hands back a
reply's text and nothing else, so what the provider reported with the reply
was dropped and a table could print no tokens on that row. The comparison
the tool arm exists for is cost and result side by side, and it had one
side.

So for the length of one search the bench puts a counter around the client
that adapter holds (:class:`MeteredModel`) — the adapter itself is not
changed, and is not asked anything it was not asked before — and when a
world's searches are over it keeps the sums in a record of its own,
``model_use.json``, beside ``verdicts.json``.

``verdicts.json`` is not touched: what the arm FOUND is read there, as for
every arm, and what it SPENT is read here. A run with no blind model arm
writes no such file. Neither did a search made before this record existed,
and a directory of one is read as it always was: the record is taken where
it is, and its absence means "not recorded" (decided 2026-11-22: the
manifest's version is not raised for it). A search whose counts did not
make a record leaves none either (``bench._model_use``). A table says how
many of a row's searches have none (:func:`model_use_lines`), so a missing
record is not a blank that reads as nothing spent — and it does not say
why, which the absence of a file cannot tell it.

**What is counted** is the tool arm's rule, so that the two arms' sums are
floors of the same kind and one sentence says both
(:func:`~.tooluse.tokens_line`). A call is counted when its reply CAME BACK
AND WAS READ — here, when its content was text, which is what the adapter
takes for an answer — a failed search's calls included: they were paid
for. A call the client retried is in the sums once, a reply that was not
text not at all, and a call whose reply was short of a count is counted as
unmetered and adds what it did report.

Every call that is counted became an answer of its search — the loop asks
once a round, and a reply that is text is a round spent, whatever the
parser then made of it — so a search's count is held to its verdict's
answers where the record is read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from .records import Reader, RecordError, dumps
from .tooluse import Prices, reported_count, tokens_line

__all__ = [
    "MODEL_USE_FILENAME",
    "MODEL_USE_VERSION",
    "MeteredModel",
    "ModelSearch",
    "ModelUse",
    "ModelUseError",
    "model_use_lines",
]

MODEL_USE_FILENAME: Final = "model_use.json"

# Bumped when a key of ``model_use.json`` is added, removed or re-read.
MODEL_USE_VERSION: Final = 1


class ModelUseError(RecordError):
    """A model-use record cannot be read or built, and the sentence says why."""


_read = Reader(ModelUseError, "model use")

_SEARCH_KEYS: Final = (
    "replicate",
    "seam_failed",
    "model_calls",
    "unmetered_calls",
    "input_tokens",
    "output_tokens",
)


# -- the record ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSearch:
    """One replicate's search: how many of its model calls came back, and the tokens they reported.

    ``seam_failed`` says the search ended on a round the model did not
    answer; the calls before it are here, and so are their tokens.
    ``unmetered_calls`` counts the calls that reported no count, or one of
    the two only; what such a call did report is in the sums.
    """

    replicate: int
    seam_failed: bool
    model_calls: int
    unmetered_calls: int
    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        _read.whole(self.replicate, "search.replicate", minimum=0)
        _read.flag(self.seam_failed, "search.seam_failed")
        _read.whole(self.model_calls, "search.model_calls", minimum=0)
        if not self.model_calls and not self.seam_failed:
            # A budget is at least one round, and a search ends short of
            # it only on a failed seam (or an interrupt, which is no record).
            raise ModelUseError(
                "search: a search no call of which came back ended on a failed seam, and this "
                "one is said not to have"
            )
        _read.whole(self.unmetered_calls, "search.unmetered_calls", minimum=0)
        if self.unmetered_calls > self.model_calls:
            raise ModelUseError(
                f"search.unmetered_calls: {self.unmetered_calls} of {self.model_calls} model "
                f"call(s)"
            )
        _read.whole(self.input_tokens, "search.input_tokens", minimum=0)
        _read.whole(self.output_tokens, "search.output_tokens", minimum=0)
        if not self.model_calls and (self.input_tokens or self.output_tokens):
            raise ModelUseError("search: tokens are counted, and no model call came back")

    def to_dict(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in _SEARCH_KEYS}

    @classmethod
    def from_dict(cls, payload: object) -> ModelSearch:
        return cls(**_read.exact(payload, _SEARCH_KEYS, "search"))


@dataclass(frozen=True)
class ModelUse:
    """``model_use.json``: every blind-model-arm search of one world, and the model they asked.

    ``model`` is the label the research package builds a model under and
    files its answers by (``provider/model``): one manifest names one such
    arm, and its replicates share the model.
    """

    world_id: str
    manifest_digest: str
    model: str
    searches: tuple[ModelSearch, ...]

    def __post_init__(self) -> None:
        _read.text(self.world_id, "world_id")
        _read.digest(self.manifest_digest, "manifest_digest")
        _read.text(self.model, "model")
        searches = tuple(_read.sequence(self.searches, "searches"))
        if not searches or any(not isinstance(search, ModelSearch) for search in searches):
            raise ModelUseError("searches: at least one ModelSearch")
        replicates = [search.replicate for search in searches]
        if len(set(replicates)) != len(replicates):
            raise ModelUseError(f"searches: each replicate once, got {replicates}")
        object.__setattr__(self, "searches", searches)

    def to_dict(self) -> dict[str, object]:
        return {
            "model_use_version": MODEL_USE_VERSION,
            "world_id": self.world_id,
            "manifest_digest": self.manifest_digest,
            "model": self.model,
            "searches": [search.to_dict() for search in self.searches],
        }

    @classmethod
    def from_dict(cls, payload: object) -> ModelUse:
        _read.version(payload, "model_use_version", MODEL_USE_VERSION)
        body = _read.exact(
            payload,
            ("model_use_version", "world_id", "manifest_digest", "model", "searches"),
            "model use",
        )
        searches = []
        for index, item in enumerate(_read.sequence(body["searches"], "searches")):
            try:
                searches.append(ModelSearch.from_dict(item))
            except ModelUseError as exc:
                # Which of several: a world has a replicate for each.
                raise ModelUseError(f"searches[{index}]: {exc}") from exc
        return cls(
            world_id=body["world_id"],
            manifest_digest=body["manifest_digest"],
            model=body["model"],
            searches=tuple(searches),
        )

    def dumps(self) -> str:
        return dumps(self.to_dict())

    @classmethod
    def loads(cls, text: str) -> ModelUse:
        return cls.from_dict(_read.loads(text))


# -- the counter ----------------------------------------------------------------------------------


class MeteredModel:
    """A chat client's ``invoke``, with what its replies reported added up as they come back.

    Put around the client a ``ChatHypothesist`` holds for one search. The
    reply is handed on as it came — the same object; nothing is read out of
    it but whether its content is text, and its counts — and a call that
    raises is let through untouched. Reading the counts cannot fail the
    call either: raised from here, a failure would reach the adapter as the
    provider not answering, and an answer that was paid for would be lost.
    So a reply whose counts cannot be read is counted as unmetered.

    ``invoke`` is all the adapter asks of a client, and all this passes on.
    A caller that needs anything else of the client asks the client itself,
    before it is wrapped (the bench checks its temperature there).
    """

    def __init__(self, model: Any) -> None:
        self._model = model
        self._calls = 0
        self._unmetered = 0
        self._input_tokens = 0
        self._output_tokens = 0

    def invoke(self, messages: Any) -> Any:
        reply = self._model.invoke(messages)
        try:
            text = isinstance(getattr(reply, "content", None), str)
        except Exception:  # noqa: BLE001 - whatever reading it raises, the adapter meets next
            text = False
        if text:
            # A reply the adapter will take for an answer. One that is not
            # text it refuses as the seam's failure, and the tool arm's
            # trace has no model line for such a reply either: counted in
            # neither.
            self._calls += 1
            try:
                usage = getattr(reply, "usage_metadata", None)
                reported = usage if isinstance(usage, Mapping) else {}
                tokens_in = reported_count(reported.get("input_tokens"))
                tokens_out = reported_count(reported.get("output_tokens"))
            except Exception:  # noqa: BLE001 - the counts are a floor, and the call came back
                tokens_in = tokens_out = None
            # Short of either count is unmetered; what it did report is
            # kept, the sums being a floor either way.
            self._unmetered += tokens_in is None or tokens_out is None
            self._input_tokens += tokens_in or 0
            self._output_tokens += tokens_out or 0
        return reply

    def spent(self, replicate: int, *, seam_failed: bool) -> ModelSearch:
        """What has come back through this counter, as the search ``replicate``'s line of the record."""
        return ModelSearch(
            replicate=replicate,
            seam_failed=seam_failed,
            model_calls=self._calls,
            unmetered_calls=self._unmetered,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
        )


# -- what a table says of it ----------------------------------------------------------------------


def model_use_lines(
    searches: Sequence[ModelSearch],
    unrecorded: int,
    prices: Prices | None = None,
    *,
    model: str | None = None,
) -> list[str]:
    """What a blind model arm's row spent, as the lines a table prints under the row.

    The tokens line is the tool arm's own sentence
    (:func:`~.tooluse.tokens_line`), over every search that has a record,
    failed seams included. ``unrecorded`` is how many searches of the row
    have none beside their verdicts — they were made before this record
    existed, or by a search whose counts did not make one; the line names
    both, since a missing file does not say which — and is said, so the
    sums are not read as the whole row's when they are not, and a row with
    no record at all is not read as one that spent nothing.
    """
    lines = [tokens_line(searches, prices, model=model)] if searches else []
    if unrecorded:
        lines.append(
            f"  tokens not recorded for {unrecorded} "
            + ("more " if searches else "")
            + f"search(es) of this row: no {MODEL_USE_FILENAME} beside their verdicts "
            "(searched before that record existed, or their search did not record its counts)"
        )
    return lines
