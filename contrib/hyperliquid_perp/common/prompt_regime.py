"""The ``prompt_regime:`` line — the three segmentation keys, rendered ONE way.

The keys (``prompt_version``, ``context_shape``, ``format_fingerprint``;
RUNBOOK §4) are what the paper and live validators bucket a run's cycles by.
Three surfaces print them and an operator greps across all three:

- ``validate`` — one line per bucket, with its cycle count (both validators,
  through ``paper.validation.prompt_regime_lines``);
- the running daemons — one INFO line the first time a cycle's prompt is
  built, and again whenever the triple flips (``cli._provider``), so a YAML
  edit + restart shows which bucket it landed in without a store query
  (issue #163);
- ``--context-only`` — the keyless preview of the bucket a config edit lands
  in (``main.run_context_only``).

One renderer so the three cannot drift: the same prefix, the same key
order, the same ``n/a`` for a key a row was written without. In ``common/``
for the same reason ``no_decision`` is: the consumers span ``paper``,
``live``, ``cli`` and the one-shot entry point, and none of them owns the
grammar.

The first key's VALUE lives here too (:data:`PROMPT_VERSION`), for the same
reason: the daemon stamps it on every ``ai_inputs`` row and the one-shot
preview prints it, and neither owns it — it used to live in ``cli`` and the
preview had to import the whole ``cli`` package for one string (issue #197).
The other two keys are computed from the prompt itself (``domains/perp``:
``prompt_context.context_shape``, ``target_decision.format_fingerprint``), so
they live beside the text they describe.

One more grep handle lives here for the same reason: the WARNING for a prompt
rendered without its ``Position:`` section (:func:`position_section_omitted`)
— logged from ``cli`` and from ``domains/perp``, read beside the
``prompt_regime:`` line (RUNBOOK §7), owned by neither.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "POSITION_SECTION_OMITTED",
    "PROMPT_REGIME_PREFIX",
    "PROMPT_VERSION",
    "PositionOmission",
    "position_section_omitted",
    "prompt_regime_line",
]

# Version stamp for the ai_inputs.prompt_version column: bump whenever the
# injected context/format CONTRACT changes — its shape, or its wording — i.e.
# whenever a deploy crosses a measurement boundary (RUNBOOK §4; retired values
# are never reused, rollbacks included). The payload hash tracks content.
# History: the CHANGELOG entries for ``phase2-target-v*``. In short —
# v4 (2026-08-27): the context gains the ``Position:`` section; the format
# block is unchanged (v4's digest is v3's).
# v5 (2026-09-01): the FORMAT block no longer renders the three gate
# thresholds as numbers (marginal-cost plan PR-B); the context is unchanged.
# The text this versions is rendered in ``domains/perp/target_decision``; a
# test pins this value to that block's digest so an edit there that forgot
# the bump fails (tests/cli/test_cli.py).
PROMPT_VERSION = "phase2-target-v5"

# The grep handle. ``validate`` has printed it since schema v11 (issue #129,
# when the third key landed); the daemon log and ``--context-only`` now carry
# the same one.
PROMPT_REGIME_PREFIX = "prompt_regime:"


def prompt_regime_line(
    prompt_version: str | None,
    context_shape: str | None,
    format_fingerprint: str | None,
    *,
    cycles: int | None = None,
) -> str:
    """``prompt_regime: prompt_version=… context_shape=… format_fingerprint=…``.

    ``cycles=`` appends the bucket's count (the validators' form); the running
    daemon and the preview leave it off — they describe one prompt, not a
    bucket. A ``None`` key prints ``n/a``: a row from before the column
    existed (v10 for the shape, v11 for the fingerprint), which the reader
    must not mistake for a distinct regime.
    """

    def _key(value: str | None) -> str:
        return "n/a" if value is None else value

    line = (
        f"{PROMPT_REGIME_PREFIX} prompt_version={_key(prompt_version)}"
        f" context_shape={_key(context_shape)}"
        f" format_fingerprint={_key(format_fingerprint)}"
    )
    if cycles is not None:
        line += f" cycles={cycles}"
    return line


# Why a prompt was rendered without its ``Position:`` section — the shape's
# trailing ``position`` token missing. Two live causes render the SAME prompt
# and the SAME ``context_shape``, and no store column tells them apart (issue
# #161, decided: accept, add no column); the WARNING each cause logs is the
# only record, so both go through :func:`position_section_omitted` and an
# operator greps ``reason=`` rather than the English (issue #197). A closed
# vocabulary the way ``persistence.backfill.Reason`` is: the type is the
# guard, both callers pass a literal.
#
# - ``no_books``: the run's ledger does not exist yet (``cli._provider``
#   reading the books) — unreachable on the production wirings, so seeing it
#   means a wiring or store problem;
# - ``non_positive_equity``: the pricer refused an account with equity <= 0
#   (``domains.perp.marginal_cost.build_position_context``) — omitting the
#   section is right, and the gate refuses every directional target on it
#   anyway.
PositionOmission = Literal["no_books", "non_positive_equity"]

POSITION_SECTION_OMITTED = "position section omitted"


def position_section_omitted(reason: PositionOmission, detail: str) -> str:
    """The one WARNING line for a prompt rendered without ``Position:``.

    ``position section omitted (reason=<member>): <detail>`` — the prefix and
    the ``reason=`` handle are the grep contract (RUNBOOK §7); ``detail`` is
    the human sentence, free to say what it likes.
    """
    return f"{POSITION_SECTION_OMITTED} (reason={reason}): {detail}"
