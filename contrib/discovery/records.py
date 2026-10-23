"""The strict readers the package's two JSON records share: one spelling, parameterised by the refusal.

The manifest (:mod:`.manifest`) and the verdict snapshot (:mod:`.verdict`)
are both frozen values written as JSON and read back EXACTLY — every key
present and no other, every value checked, a refusal naming the path
inside the document. The research package's own records read theirs the
same way, each with a private copy of these helpers; this package has two
records and will have more (plan PR 2's world, PR 4's scores), so the
helpers live here once, and each record module instantiates a
:class:`Reader` with its own error class.

The division of labour is the upstream pattern (``Penalty.from_dict``,
``CostModel.from_dict``): a record's ``from_dict`` holds the KEY SET and
hands raw values to the constructor, and ``__post_init__`` is the one gate
every value passes, whether the record was read from a file or built in
code. So the readers here are called from ``__post_init__``, not from
``from_dict``, and a sentence about what a field accepts is written once.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from .upstream import SpecError, VocabEnum, require_amount, require_number

__all__ = ["Reader", "dumps"]

_E = TypeVar("_E", bound=VocabEnum)
_T = TypeVar("_T")


def dumps(document: object) -> str:
    """The file: sorted keys, indented, no NaN — a record is diffed and hashed."""
    return json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n"


@dataclass(frozen=True)
class Reader:
    """The readers, raising ``error`` with ``noun`` as the document's name in the JSON refusal."""

    error: type[Exception]
    noun: str

    def loads(self, text: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise self.error(f"{self.noun} is not valid JSON: {exc}") from exc

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
