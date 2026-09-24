"""The direction probe: the model's up / down / flat probabilities, asked on their own (plan PR 2.1).

The decision a variant gives (:mod:`.replay`) mixes three things: which
way the model thinks the price will go, how big a position it wants, and
what the gate lets through. The scorecard's confidence calibration sees
the first only through ``confidence``, which is a gate threshold rather
than a probability of being right, and only on the questions where the
model asked for a target. The probe asks for the direction alone, as
probabilities, so that "does the model know anything about where BTC goes
next" can be scored on every question with a payload.

**A separate call** (decided 2026-09-24): the probe is never folded into
the decision prompt, because that would change the trader being measured.
Each question and repeat gets one more completion, from the same variant's
model, temperature and completion cap:

- the SYSTEM message is the probe's own ``system`` text (the variant's
  system prompt is the portfolio manager's decision role, which asks for
  something else);
- the HUMAN message is the payload's ``context_text`` (and the variant's
  ``extra_context`` after it, as in the decision replay) under the engine's
  heading, with the probe's ``instructions`` as the last block, where the
  decision replay puts the format block. The format block itself is not
  sent: it asks for a decision.

**A probe is data** (plan §3-5's rule for variants): a YAML file with a
``name``, a ``system`` text and an ``instructions`` text. :attr:`Probe.sha`
is a digest of the two texts; change either and it is a new probe, which
needs a new name, and its answers are never pooled with the old one's.

**The answer** is one JSON object holding a forecast for each key of
:data:`PROBE_KEYS`, and each forecast holds exactly the three classes of
:data:`CLASSES`. Other top-level keys are ignored (a model that adds a
note has still answered). A forecast is taken when every probability is
a JSON number in [0, 1] and the three sum to within
:data:`SUM_TOLERANCE` of 1; it is then normalised to sum to exactly 1. Any
other answer is ``invalid_probe``: counted, not scored, and NOT asked again,
since asking again until a question is answered well would leave the
scored sample leaning towards the questions that are easy to answer.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

from .upstream import extract_json_block
from .variant import short_sha

__all__ = [
    "CLASSES",
    "INVALID_PROBE",
    "PROBE_KEYS",
    "PROBE_STEP_MS",
    "REFUSED",
    "SUM_TOLERANCE",
    "Forecast",
    "Probe",
    "ProbeAnswer",
    "ProbeError",
    "ProbeReading",
    "load_probe",
    "parse_probe",
]

# The outcome classes a forecast spreads its probability over, in report order.
CLASSES: Final = ("up", "down", "flat")

# The keys an answer carries, each the horizon it forecasts in bars of
# :data:`PROBE_STEP_MS`: one bar (4h) and six bars (24h), the scorecard's
# two horizons (``score.HORIZONS``) on the paper cadence.
PROBE_KEYS: Final = {"h4": 1, "h24": 6}

# The bar the keys are named in hours of. The probe asks in hours, so it
# can only be scored on a run whose bar is this long; ``replay --probe``
# refuses any other run.
PROBE_STEP_MS: Final = 4 * 3_600_000

# How far from 1 the three probabilities of one forecast may sum before
# the answer is refused rather than normalised.
SUM_TOLERANCE: Final = 0.02

# The two reasons an answer is not scored, as ``probe_answers.invalid_reason`` stores them.
INVALID_PROBE: Final = "invalid_probe"
REFUSED: Final = "refused"

# One forecast per key of ``PROBE_KEYS``: class -> probability, summing to 1.
Forecast = Mapping[str, Mapping[str, float]]

_KEYS: Final = frozenset({"name", "system", "instructions"})


class ProbeError(ValueError):
    """A probe file that cannot be used, and the sentence says which key."""


@dataclass(frozen=True)
class Probe:
    """One probe, its file already read and checked."""

    name: str
    system: str
    instructions: str

    def __post_init__(self) -> None:
        for field in ("name", "system", "instructions"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ProbeError(f"{field} must be a non-empty string, got {value!r}")

    @property
    def sha(self) -> str:
        """``sha256:<hex>`` over the two texts the model is sent, as canonical JSON."""
        identity = {"system": self.system, "instructions": self.instructions}
        canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"

    @property
    def short_sha(self) -> str:
        return short_sha(self.sha)

    def describe(self) -> str:
        return f"probe {self.name} ({self.short_sha})"


def load_probe(path: Path) -> Probe:
    """Read and check the probe file at ``path``. Unknown keys are refused, as a variant's are."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ProbeError(f"probe file {str(path)!r} cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ProbeError(f"probe file {str(path)!r} is not YAML ({exc})") from exc
    if not isinstance(document, dict):
        raise ProbeError(f"probe file {str(path)!r} must hold a mapping")
    unknown = set(document) - _KEYS
    if unknown:
        raise ProbeError(f"unknown probe key(s) {sorted(unknown)}; allowed: {sorted(_KEYS)}")
    missing = _KEYS - set(document)
    if missing:
        raise ProbeError(f"probe file {str(path)!r} lacks {sorted(missing)}")
    return Probe(
        name=document["name"], system=document["system"], instructions=document["instructions"]
    )


@dataclass(frozen=True)
class ProbeReading:
    """What one probe answer said: a normalised forecast, or why there is none."""

    forecast: Forecast | None
    invalid_detail: str | None


@dataclass(frozen=True)
class ProbeAnswer:
    """One stored probe answer as the scorer reads it.

    ``forecast`` is set exactly when ``invalid_reason`` is ``None``;
    otherwise the reason is :data:`INVALID_PROBE` or :data:`REFUSED`.
    """

    input_id: str
    forecast: Forecast | None
    invalid_reason: str | None

    def __post_init__(self) -> None:
        if (self.forecast is None) == (self.invalid_reason is None):
            raise ProbeError(f"{self.input_id}: exactly one of forecast / invalid_reason is set")
        if self.invalid_reason not in (None, INVALID_PROBE, REFUSED):
            raise ProbeError(f"{self.input_id}: unknown invalid_reason {self.invalid_reason!r}")


def _invalid(detail: str, truncated: bool) -> ProbeReading:
    if truncated:
        detail = f"{detail} (the completion was cut off at its token cap)"
    return ProbeReading(None, detail)


def parse_probe(text: object, *, truncated: bool) -> ProbeReading:
    """The forecast in ``text``, normalised, or the sentence saying why there is none.

    The JSON object is found the way the decision parser finds its block
    (the last fenced object, else the last balanced one), so a model that
    reasons before it answers is read the same way in both calls.
    """
    if not isinstance(text, str):
        return _invalid(f"the response is a {type(text).__name__}, not text", truncated)
    source = extract_json_block(text)
    if source is None:
        return _invalid("no JSON object in the response", truncated)
    document = json.loads(source)  # extract_json_block returns only sources that parse
    forecast: dict[str, dict[str, float]] = {}
    for key in PROBE_KEYS:
        given = document.get(key)
        if not isinstance(given, dict):
            return _invalid(f"no {key} forecast (an object of {', '.join(CLASSES)})", truncated)
        if set(given) != set(CLASSES):
            return _invalid(
                f"{key} must hold exactly {', '.join(CLASSES)}, got {', '.join(sorted(given))}",
                truncated,
            )
        values: dict[str, float] = {}
        for name in CLASSES:
            value = given[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
                or not 0 <= value <= 1
            ):
                return _invalid(
                    f"{key}.{name} must be a number in [0, 1], got {value!r}", truncated
                )
            values[name] = float(value)
        total = math.fsum(values.values())
        # The slack is for binary floats, not for the model: 0.5 + 0.2 + 0.32
        # is 1.0200000000000002, and a sum written as 1.02 is inside the bound.
        if abs(total - 1) > SUM_TOLERANCE + 1e-9:
            return _invalid(
                f"{key} sums to {total:.4f}, more than {SUM_TOLERANCE} away from 1", truncated
            )
        forecast[key] = {name: value / total for name, value in values.items()}
    return ProbeReading(forecast, None)
