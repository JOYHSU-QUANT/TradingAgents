"""Guards for "value must be one of an allowed set", shared across layers.

Two sites, two shapes:

- :func:`check_enum` — a plain string against a caller-owned collection of
  the legal spellings (a persistence column's ``frozenset``, the accounting
  result fields, the ``LEGAL_NETWORKS`` tuple). The caller names the field;
  the sentence is ``<name> must be one of [<sorted>]``.
- :class:`VocabEnum` — the base for the ``str`` enums that ARE a vocabulary
  (:mod:`..domains.perp.schema`'s market regime, profile shape, position side
  and candle interval; the persistence layer's fill :class:`Side`; the live
  submitter's :class:`SubmitOutcomeKind`). Looking a member up by an unknown
  value fails with ``unsupported <noun> 'X'; choose from [<members>]``
  wherever the lookup is written (issue #166); ``Enum``'s own "'X' is not a
  valid MarketRegime" names neither the vocabulary nor the fix.

The two sentences differ on purpose, by audience: ``check_enum`` guards a
value the PROGRAM supplied (a column literal, a result-type tag — an
assertion, worded as one, over a set that has no order to show), while a
``VocabEnum`` guards a value an operator wrote or a row recorded, so it says
what to write instead, in the author's order. A caller that knows more than
the enum (a YAML key, a phase policy) re-words the ``ValueError`` itself
(``live/config._coerce_enum``, ``risk_gate.RiskConfig``).

One deliberate exception to the audience rule (issue #226): the
operator-written ``network`` is refused through ``check_enum`` over the flat
``LEGAL_NETWORKS`` tuple — the two YAML loaders passing their key as ``name``
(``'network'``, ``live.network``), the clients and the agent-key lookup the
parameter name — rather than through a ``Network`` enum. Every consumer of
the accepted spelling (the exchange clients' URL table, the agent-key env-var
table, the CLI's drift comparisons) reads it as a plain ``str``; an enum
there would ripple ``.value`` through all of them to buy a sentence that
already lists the choices. The one refusal of this family still built by
hand is ``live/fills.py``'s ``fill side must be one of ['A', 'B'] (bid/ask)``:
that is the VENUE's side vocabulary on a wire payload, raised as a
``MalformedResponseError``, not a ``ValueError`` over a local table.

Extracted here (a neutral, dependency-free shared module) so the persistence
write boundary (:mod:`..persistence.repository`), the paper accounting result
dataclasses (:class:`..paper.accounting.FundingResult`) and the DTO module
validate the same way — without any of them reaching into another's private
helpers.

Pure: no I/O, no clock, no domain knowledge of *which* values are allowed
(each caller owns its own collection; each enum declares its own members).
"""

from __future__ import annotations

from enum import Enum
from typing import ClassVar, NoReturn

__all__ = ["VocabEnum", "check_enum"]


def check_enum(value: object, allowed: frozenset[str] | tuple[str, ...], *, name: str) -> None:
    """Raise ``ValueError`` naming ``name`` unless ``value`` is in ``allowed``.

    ``allowed`` is the persistence layer's frozenset or a tuple such as
    ``LEGAL_NETWORKS`` (not a bare ``str``, whose ``in`` is a substring test);
    the sentence lists it sorted either way, so the container never shows.
    ``value`` is typed ``object`` because the YAML loaders hand over whatever
    the file said: a non-``str`` is refused by the same sentence WITHOUT
    being looked up, so an unhashable value (a list) cannot turn a frozenset
    membership test into a ``TypeError`` that escapes the caller's
    ``ValueError`` lane.
    """
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {value!r}")


class VocabEnum(str, Enum):
    """A ``str`` enum whose failed lookup names the vocabulary and the fix.

    A subclass declares the noun its sentence uses as a class keyword::

        class ProfileShape(VocabEnum, noun="volume profile shape"):
            D = "D"
            ...

        ProfileShape("x")
        # ValueError: unsupported volume profile shape 'x'; choose from ['D', 'P', 'b', 'thin']

    A class keyword rather than a class attribute because ``Enum`` turns a
    plain ``_noun = "..."`` into a MEMBER (one leading underscore is neither
    the reserved ``_sunder_`` form nor a dunder); the signature makes it
    required, so a subclass that forgot it fails at definition, not with an
    ``unsupported None`` sentence at its first bad lookup. The noun is stored
    under a name-mangled private attribute so no member a subclass declares
    can collide with it — on 3.10 a member named ``_noun`` would silently
    replace a plain ``_noun`` attribute (3.11+ refuses the reassignment
    loudly), and a name-mangled one is out of a subclass's reach. The members are
    listed in DECLARATION order, not sorted (PR #165: the intervals ascend,
    the shapes follow the source article). Raised from ``_missing_`` rather
    than translated at each lookup site, so a direct ``CandleInterval(x)``
    and a ``parse_interval(x)`` are told the same thing (issue #155);
    ``Enum.__new__`` re-raises a ``ValueError`` from ``_missing_`` intact.
    """

    __noun: ClassVar[str]  # ``_VocabEnum__noun`` once mangled; set per subclass below

    def __init_subclass__(cls, *, noun: str, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if not isinstance(noun, str) or not noun.strip():
            raise TypeError(f"{cls.__name__}: noun must be a non-blank str, got {noun!r}")
        cls.__noun = noun

    @classmethod
    def _missing_(cls, value: object) -> NoReturn:
        if cls is VocabEnum:
            # The base has no noun and no members; 3.11+'s ``EnumType`` refuses
            # the lookup before reaching here, 3.10 does not — same answer on both.
            raise TypeError("VocabEnum is the base, not a vocabulary; look up a subclass")
        raise ValueError(
            f"unsupported {cls.__noun} {value!r}; choose from {[m.value for m in cls]}"
        )
