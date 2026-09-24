"""A variant file: what it may say, what its identity is, and where its cutoff falls."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from contrib.replay.upstream import epoch_ms, parse_instant
from contrib.replay.variant import DEFAULT_MAX_TOKENS, VariantError, load_variant

from .papers import variant as make_variant

MINIMAL = "name: v\nmodel:\n  provider: openrouter\n  id: a/b\nsystem_prompt_path: s.md\n"


def _write(tmp_path: Path, text: str, prompt: str = "Decide.") -> Path:
    (tmp_path / "s.md").write_text(prompt, encoding="utf-8")
    path = tmp_path / "v.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_minimal_file_takes_the_defaults(tmp_path):
    variant = load_variant(_write(tmp_path, MINIMAL))
    assert (variant.name, variant.provider, variant.model, variant.system_prompt) == (
        "v",
        "openrouter",
        "a/b",
        "Decide.",
    )
    assert (variant.temperature, variant.max_tokens, variant.extra_context) == (
        None,
        DEFAULT_MAX_TOKENS,
        None,
    )
    assert variant.model_cutoff is None and variant.cutoff_ms is None


def test_every_key_is_read(tmp_path):
    text = MINIMAL + (
        "temperature: 0.2\nmax_tokens: 4096\nextra_context: |\n  Fade funding spikes.\n"
        "model_cutoff: 2026-03-31\n"
    )
    variant = load_variant(_write(tmp_path, text))
    assert (variant.temperature, variant.max_tokens) == (0.2, 4096)
    assert variant.extra_context == "Fade funding spikes.\n"
    assert variant.model_cutoff == date(2026, 3, 31)
    assert variant.describe() == (
        f"variant v ({variant.short_sha}): openrouter/a/b, temperature 0.2, max_tokens 4096, "
        "with extra context"
    )


def test_the_prompt_is_read_relative_to_the_variant_file(tmp_path, monkeypatch):
    path = _write(tmp_path, MINIMAL)
    monkeypatch.chdir(tmp_path.parent)
    assert load_variant(Path(tmp_path.name) / path.name).system_prompt == "Decide."


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (MINIMAL + "temprature: 0.2\n", r"unknown variant key\(s\) \['temprature'\]"),
        ("name: v\nmodel:\n  provider: x\n  id: y\n", r"lacks \['system_prompt_path'\]"),
        (MINIMAL.replace("  id: a/b\n", ""), "model must be a mapping with exactly"),
        (MINIMAL + "temperature: -0.1\n", "temperature must be a finite, non-negative number"),
        (MINIMAL + "temperature: .inf\n", "temperature must be a finite, non-negative number"),
        (MINIMAL + "temperature: true\n", "temperature must be a finite, non-negative number"),
        (MINIMAL + "max_tokens: 0\n", "max_tokens must be a positive integer, got 0"),
        (MINIMAL + "extra_context: '  '\n", "extra_context must be non-empty text"),
        (MINIMAL + "model_cutoff: soon\n", r"model_cutoff must be a date \(YYYY-MM-DD\)"),
        (MINIMAL + "model_cutoff: 2026-03-31 12:00:00\n", "model_cutoff must be a date"),
        ("- a list\n", "must hold a mapping"),
        ("name: [unclosed\n", "is not YAML"),
    ],
    ids=[
        "typo",
        "missing",
        "model-shape",
        "negative",
        "infinite",
        "bool-temperature",
        "no-tokens",
        "blank-context",
        "cutoff-word",
        "cutoff-timestamp",
        "list",
        "yaml",
    ],
)
def test_a_bad_file_is_refused_by_key(tmp_path, text, message):
    with pytest.raises(VariantError, match=message):
        load_variant(_write(tmp_path, text))


def test_a_missing_prompt_is_refused_by_path(tmp_path):
    path = _write(tmp_path, MINIMAL)
    (tmp_path / "s.md").unlink()
    with pytest.raises(VariantError, match=r"system prompt .*s\.md.* cannot be read"):
        load_variant(path)


@pytest.mark.parametrize(
    "change",
    [
        {"provider": "anthropic"},
        {"model": "a/c"},
        {"system_prompt": "Decide carefully."},
        {"temperature": 0.5},
        {"max_tokens": 4096},
        {"extra_context": "A lesson."},
    ],
    ids=["provider", "model", "prompt", "temperature", "cap", "extra-context"],
)
def test_whatever_reaches_the_model_changes_the_sha(change):
    assert make_variant(**change).sha != make_variant().sha


@pytest.mark.parametrize(
    "change", [{"name": "renamed"}, {"model_cutoff": date(2026, 1, 1)}], ids=["name", "cutoff"]
)
def test_the_label_and_the_cutoff_leave_the_sha_alone(change):
    assert make_variant(**change).sha == make_variant().sha


def test_the_sha_is_a_prefixed_sha256():
    sha = make_variant().sha
    assert sha.startswith("sha256:") and len(sha) == len("sha256:") + 64
    assert make_variant().short_sha == sha.removeprefix("sha256:")[:12]


def test_the_cutoff_is_the_start_of_the_next_utc_day():
    variant = make_variant(model_cutoff=date(2026, 3, 31))
    assert variant.cutoff_ms == epoch_ms(parse_instant("2026-04-01T00:00:00+00:00"), what="t")


def test_a_temperature_above_one_is_the_providers_to_refuse():
    """No ceiling here: the engine sets none, and the ceiling differs between providers."""
    assert make_variant(temperature=1.5).temperature == 1.5


def test_the_default_cap_is_the_paper_daemons():
    """``DEFAULT_MAX_TOKENS`` repeats the daemon's cap: pinned, so the two cannot drift apart."""
    from contrib.hyperliquid_perp.engine_bridge import _DEFAULT_MAX_COMPLETION_TOKENS

    assert DEFAULT_MAX_TOKENS == _DEFAULT_MAX_COMPLETION_TOKENS


def test_the_shipped_example_loads():
    """The example under ``variants/`` is the one the README points at: it must stay valid."""
    path = Path(__file__).resolve().parents[1] / "variants" / "current-sonnet.yaml"
    variant = load_variant(path)
    assert (variant.name, variant.provider, variant.model) == (
        "current-sonnet",
        "openrouter",
        "anthropic/claude-sonnet-4-6",
    )
    assert (variant.temperature, variant.max_tokens, variant.model_cutoff) == (None, 8192, None)
    assert variant.system_prompt.startswith("As the Portfolio Manager,")
