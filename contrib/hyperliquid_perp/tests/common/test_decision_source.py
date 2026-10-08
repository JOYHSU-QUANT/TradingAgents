"""The ``decision_source:`` block: which provider a run asks, and whether it needs a key."""

from __future__ import annotations

import pytest

from contrib.hyperliquid_perp.common.decision_source import (
    ENGINE_PROVIDER,
    FILE_TARGET_PROVIDER,
    PROVIDERS,
    DecisionSourceConfig,
    decision_source,
)


def test_the_default_is_the_engine_with_no_path():
    # An absent block, a null block and an empty block are the run every
    # config before this block existed described.
    for block in (None, {}):
        cfg = DecisionSourceConfig.from_dict(block)
        assert cfg.provider == ENGINE_PROVIDER
        assert cfg.target_path is None
        assert cfg.drives_engine


def test_decision_source_reads_the_block_off_a_loaded_config():
    config = {"decision_source": {"provider": "file_target", "target_path": "/srv/h.json"}}
    cfg = decision_source(config)
    assert cfg.provider == FILE_TARGET_PROVIDER
    assert cfg.target_path == "/srv/h.json"
    assert not cfg.drives_engine
    assert decision_source({}).drives_engine


def test_the_file_target_provider_needs_a_path():
    with pytest.raises(ValueError, match="target_path must name the handoff file"):
        DecisionSourceConfig.from_dict({"provider": "file_target"})


@pytest.mark.parametrize("path", ["", "   ", " /srv/h.json", "/srv/h.json\n"])
def test_a_blank_or_padded_path_is_refused_not_stripped(path):
    # A path nobody will ever write to has exactly the symptoms of a
    # coordinator that never ran, so it is refused at load, by name.
    with pytest.raises(ValueError, match="no surrounding whitespace"):
        DecisionSourceConfig(provider="file_target", target_path=path)


def test_a_path_under_the_engine_is_dead_config_and_refused():
    with pytest.raises(ValueError, match="read only by the 'file_target' provider"):
        DecisionSourceConfig.from_dict({"provider": "engine", "target_path": "/srv/h.json"})


def test_an_unknown_provider_is_refused_over_the_whole_vocabulary():
    with pytest.raises(ValueError) as excinfo:
        DecisionSourceConfig.from_dict({"provider": "llm"})
    message = str(excinfo.value)
    assert "decision_source.provider" in message
    for name in PROVIDERS:
        assert repr(name) in message


@pytest.mark.parametrize("value", [True, 1, ["file_target"]])
def test_a_non_string_provider_is_refused_as_a_type_not_looked_up(value):
    # ``str_from_yaml`` refuses before the vocabulary check: YAML ``true``
    # must not reach ``check_enum`` as the string "True", and a list must
    # not turn the membership test into a TypeError.
    with pytest.raises(ValueError):
        DecisionSourceConfig.from_dict({"provider": value})


def test_an_unknown_key_in_the_block_is_refused():
    with pytest.raises(ValueError, match="unknown config key"):
        DecisionSourceConfig.from_dict({"provider": "engine", "target": "/srv/h.json"})


def test_a_non_mapping_block_is_a_named_error():
    with pytest.raises(ValueError, match="expected a mapping"):
        DecisionSourceConfig.from_dict("file_target")  # type: ignore[arg-type]
