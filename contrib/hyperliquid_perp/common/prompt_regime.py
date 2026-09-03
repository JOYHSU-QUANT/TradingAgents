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
"""

from __future__ import annotations

__all__ = ["PROMPT_REGIME_PREFIX", "prompt_regime_line"]

# The grep handle. ``validate`` has printed it since schema v10; the daemon
# log and ``--context-only`` now carry the same one.
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
