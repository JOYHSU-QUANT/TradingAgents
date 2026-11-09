"""The strict readers the package's JSON records share: one spelling, parameterised by the refusal.

The manifest (:mod:`.manifest`), the verdict snapshot (:mod:`.verdict`),
``verdicts.json`` (:mod:`.bench`), ``oracle.json`` (:mod:`.oracle`) and
``scores.json`` (:mod:`.scoring`) are frozen values written as JSON and read back EXACTLY — every key present
and no other, every value checked, a refusal naming the path inside the
document. The research package's own records read theirs the same way,
each with a private copy of these helpers; this package has five records,
so the helpers live here once, and each record module instantiates a
:class:`Reader` with its own error class — every record's error but the
manifest's sharing :class:`RecordError`.

The division of labour is the upstream pattern (``Penalty.from_dict``,
``CostModel.from_dict``): a record's ``from_dict`` reads the KEY SET and
the few checks that need the raw document (the version, a written id or
hash against the derived one, the parser call for a nested spec), then
hands raw values to the constructor; ``__post_init__`` is the one gate
each field's own value passes, whether the record was read from a file or
built in code. So a sentence about what a field accepts is written once,
in ``__post_init__``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeVar

from .upstream import SpecError, VocabEnum, require_amount, require_number

__all__ = ["DIGEST_DIGITS", "Reader", "RecordError", "dumps"]

_E = TypeVar("_E", bound=VocabEnum)
_T = TypeVar("_T")

# How many hex digits of a manifest's SHA-256 name its run directory and
# are written into the records read beside it. Eight is the opaque id's
# own width; the digest is a label for people, directories and records,
# not a key anything is looked up by.
DIGEST_DIGITS: Final = 8
_DIGEST: Final = re.compile(rf"[0-9a-f]{{{DIGEST_DIGITS}}}")


class RecordError(RuntimeError):
    """A record this package writes (a verdict, an oracle, a run) cannot be read, built or continued.

    The base of the record modules' refusals, so a caller — the CLI — has
    one class to catch for "this is not a usable record" whichever file it
    came from. A ``RuntimeError`` as the ledger's own refusals are, because
    the refusals that matter most are about the STATE of a run rather than
    a malformed argument. The manifest's refusal is not one of these: a
    manifest is an operator's input, and its error is a ``ValueError`` on
    the lane the research package's refusals use.
    """


def dumps(document: object) -> str:
    """The file: sorted keys, indented, no NaN — a record is read and diffed by people."""
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n"


@dataclass(frozen=True)
class Reader:
    """The readers, raising ``error`` with ``noun`` as the document's name in the JSON refusal."""

    error: type[Exception]
    noun: str

    def version(self, payload: object, key: str, expected: int) -> Mapping[str, Any]:
        """``payload`` as a mapping whose ``key`` is exactly this build's version.

        Read before the key set, so a later version's extra key is refused
        with the sentence that names the real reason — another build wrote
        this — rather than as an unknown key. ``type`` rather than ``==``:
        ``True`` and ``1.0`` both equal 1.
        """
        body = self.mapping(payload, self.noun)
        version = body.get(key)
        if type(version) is not int or version != expected:
            raise self.error(f"{key}: this build reads version {expected}, got {version!r}")
        return body

    def loads(self, text: str) -> Any:
        """The text as JSON, a repeated key refused rather than resolved to the last one.

        ``json.loads`` keeps the last of two equal keys, so a pasted duplicate
        block in a hand-written manifest would change a parameter with no
        refusal — the research package's spec parser closes the same hole.
        """
        try:
            return json.loads(text, object_pairs_hook=self._without_repeats)
        except json.JSONDecodeError as exc:
            raise self.error(f"{self.noun} is not valid JSON: {exc}") from exc

    def _without_repeats(self, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: dict[str, Any] = {}
        for key, value in pairs:
            if key in seen:
                raise self.error(
                    f"{self.noun}: the key {key!r} appears twice in one object; JSON keeps only "
                    f"the last, so the record read would not be the one written"
                )
            seen[key] = value
        return seen

    def mapping(self, payload: object, path: str) -> Mapping[str, Any]:
        if not isinstance(payload, Mapping):
            raise self.error(f"{path}: expected an object, got {payload!r}")
        return payload

    def exact(self, payload: object, keys: Sequence[str], path: str) -> Mapping[str, Any]:
        """``payload`` as a mapping with exactly ``keys`` — a missing key is refused, not defaulted."""
        body = self.mapping(payload, path)
        if set(body) != set(keys):
            raise self.error(
                f"{path}: expected exactly the keys {sorted(keys)}, got {sorted(body)}"
            )
        return body

    def sequence(self, value: object, path: str) -> list[Any]:
        if not isinstance(value, Sequence) or isinstance(value, str):
            raise self.error(f"{path}: expected a list, got {value!r}")
        return list(value)

    def string(self, value: object, path: str) -> str:
        """Any string, the empty one included — a field whose emptiness means something."""
        if not isinstance(value, str):
            raise self.error(f"{path}: expected text, got {value!r}")
        return value

    def text(self, value: object, path: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.error(f"{path}: expected non-empty text, got {value!r}")
        return value

    def whole(self, value: object, path: str, *, minimum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise self.error(
                f"{path}: expected a whole number of at least {minimum}, got {value!r}"
            )
        return value

    def flag(self, value: object, path: str) -> bool:
        if not isinstance(value, bool):
            raise self.error(f"{path}: expected true or false, got {value!r}")
        return value

    def digest(self, value: object, path: str) -> str:
        """A manifest digest as ``landing.manifest_digest`` writes one: the fixed count of lower-case hex digits."""
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise self.error(
                f"{path}: a manifest digest is {DIGEST_DIGITS} lower-case hex digits, got {value!r}"
            )
        return value

    def number(self, value: object, path: str) -> float:
        """A finite number — the research package's one numeric guard, on this record's lane."""
        try:
            return require_number(value, path)
        except SpecError as exc:
            raise self.error(str(exc)) from exc

    def amount(self, value: object, path: str, *, positive: bool = False) -> float:
        """A finite number at or above zero (above, if ``positive``)."""
        try:
            return require_amount(value, path, positive=positive)
        except ValueError as exc:
            raise self.error(str(exc)) from exc

    def enum(self, cls: type[_E], value: object, path: str) -> _E:
        try:
            return cls(value)
        except ValueError as exc:
            raise self.error(f"{path}: {exc}") from exc

    def distinct(
        self, value: object, path: str, read: Callable[[Any, str], _T]
    ) -> tuple[_T, ...]:
        """A non-empty list, every item read by ``read``, every value once."""
        items = [read(item, f"{path}[{i}]") for i, item in enumerate(self.sequence(value, path))]
        if not items:
            raise self.error(f"{path}: at least one value")
        if len(set(items)) != len(items):
            raise self.error(f"{path}: each value once, got {value!r}")
        return tuple(items)
