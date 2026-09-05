"""Tests for the cross-provider scalar LLM knobs: ``temperature`` (#178/#168)
and ``max_tokens`` (#177).

Both follow one contract — set, the value reaches the underlying chat client;
unset, the provider keeps its own default; an env string is coerced where the
graph reads it — so they share one parametrized module: a third knob
(``top_p``, ``seed``) extends the ``KNOBS`` table instead of copying a file
(#184). ``max_tokens`` additionally pins the request payload, because langchain
renames that kwarg on the wire (Chat Completions and the Responses API each
spell it differently) and the pydantic field alone proves nothing.

The engine default for both stays None (provider default). For ``max_tokens``
the perp bridge always resolves a cap and the interactive CLI fills
``DEFAULT_MAX_TOKENS`` in; a library caller on a gateway provider gets the one
warning pinned at the bottom instead (#183).
"""

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from langchain_core.callbacks import BaseCallbackHandler

from tradingagents.default_config import DEFAULT_CONFIG, DEFAULT_MAX_TOKENS, _apply_env_overrides
from tradingagents.llm_clients.base_client import _COMMON_PASSTHROUGH_KWARGS
from tradingagents.llm_clients.factory import create_llm_client

from .conftest import provider_kwargs_for


@dataclass(frozen=True)
class Knob:
    key: str
    env_var: str
    value: Any  # a programmatic value that must reach the client verbatim
    env_string: str  # the same knob as an env string
    parsed: Any  # what _get_provider_kwargs forwards for env_string
    # Client attribute per provider, where it differs from ``key``.
    attr_overrides: dict[str, str] = field(default_factory=dict)
    # Request-payload key per OpenAI-compatible provider.
    wire_keys: dict[str, str] = field(default_factory=dict)

    def attr(self, provider: str) -> str:
        return self.attr_overrides.get(provider, self.key)


TEMPERATURE = Knob(
    key="temperature",
    env_var="TRADINGAGENTS_TEMPERATURE",
    value=0.0,
    env_string="0.3",
    parsed=0.3,
    wire_keys={"openrouter": "temperature", "openai": "temperature"},
)
MAX_TOKENS = Knob(
    key="max_tokens",
    env_var="TRADINGAGENTS_MAX_TOKENS",
    value=1234,
    env_string="8192",
    parsed=8192,
    # Google's client declares ``max_tokens`` as the alias of its
    # ``max_output_tokens`` field and has no payload builder to pin — the
    # attribute is the right layer there.
    attr_overrides={"google": "max_output_tokens"},
    # Chat Completions renames the kwarg on the way out (OpenRouter honours
    # this spelling — verified live against the gateway: finish_reason=length
    # at exactly the cap); native OpenAI takes the Responses branch, which
    # renames it again — a third spelling, on the busiest provider.
    wire_keys={"openrouter": "max_completion_tokens", "openai": "max_output_tokens"},
)
KNOBS = [TEMPERATURE, MAX_TOKENS]
KNOB_IDS = [knob.key for knob in KNOBS]

# Every client that constructs without an optional extra installed. Azure needs
# its endpoint/version env first (``_azure_env``); Bedrock is covered separately
# because langchain-aws is optional.
PROVIDERS = [
    ("openai", "gpt-4.1"),
    ("anthropic", "claude-sonnet-4-6"),
    ("google", "gemini-2.5-flash"),
    ("deepseek", "deepseek-chat"),
    ("azure", "gpt-4.1"),
]
PAYLOAD_PROVIDERS = [("openrouter", "qwen/qwen3-235b-a22b-2507"), ("openai", "gpt-4.1")]


def _provider_kwargs(provider: str = "openai", **config) -> dict:
    return provider_kwargs_for({"llm_provider": provider, **config})


def _azure_env(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com/")
    monkeypatch.setenv("OPENAI_API_VERSION", "2025-03-01-preview")


def _forwarded(provider: str, model: str, sent: dict) -> dict:
    """What the constructed client holds for each key in ``sent``."""
    llm = create_llm_client(provider=provider, model=model, api_key="placeholder", **sent).get_llm()
    attr_for = {knob.key: knob.attr(provider) for knob in KNOBS}
    return {key: getattr(llm, attr_for.get(key, key)) for key in sent}


@pytest.mark.unit
class TestKnobForwarding:
    """Every provider forwards the shared allowlist (#184), not only the knobs
    that happened to be edited last.

    The allowlist loops drop unrecognised keys silently, so a client whose
    tuple lost a common key would never forward it and nothing would raise —
    this is the test that goes red instead.
    """

    COMMON = {"temperature": 0.0, "max_tokens": 1234, "max_retries": 7}

    def _sent(self) -> dict:
        sent = {**self.COMMON, "callbacks": [BaseCallbackHandler()]}
        # The declaration IS the table: a key added there must be added here.
        assert set(sent) == set(_COMMON_PASSTHROUGH_KWARGS)
        return sent

    @pytest.mark.parametrize("provider,model", PROVIDERS)
    def test_every_provider_forwards_the_common_set(self, provider, model, monkeypatch):
        _azure_env(monkeypatch)
        sent = self._sent()
        assert _forwarded(provider, model, sent) == sent

    def test_bedrock_forwards_the_common_set(self, monkeypatch):
        # langchain-aws is an optional extra: capture the constructor call the
        # client would make instead of requiring the dependency.
        import tradingagents.llm_clients.bedrock_client as bc

        class Capture:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        monkeypatch.setattr(bc, "_BEDROCK_CLASS", Capture)
        sent = self._sent()
        received = create_llm_client(
            provider="bedrock", model="us.anthropic.claude-sonnet-4-6-v1:0", **sent
        ).get_llm().kwargs
        assert {key: received[key] for key in sent} == sent

    def test_api_key_stays_out_of_the_common_set(self):
        # Bedrock's chat class takes no api_key (AWS credential chain) and
        # Google maps it to google_api_key; putting it in the shared tuple
        # would break both constructors at get_llm().
        assert "api_key" not in _COMMON_PASSTHROUGH_KWARGS

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    @pytest.mark.parametrize("provider,model", PAYLOAD_PROVIDERS)
    def test_knob_reaches_the_request_payload(self, knob, provider, model):
        # Every OpenAI-compatible provider (deepseek, ollama, xai, the local
        # endpoints) shares the Chat Completions payload builder; native
        # OpenAI is the Responses branch.
        llm = create_llm_client(
            provider=provider, model=model, api_key="placeholder", **{knob.key: knob.value}
        ).get_llm()
        payload = llm._get_request_payload([("human", "hi")])
        assert payload[knob.wire_keys[provider]] == knob.value

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    @pytest.mark.parametrize("provider,model", PAYLOAD_PROVIDERS)
    def test_knob_omitted_leaves_provider_default(self, knob, provider, model):
        # Not passing the knob must not force it to a value — on the gateway
        # too: the gateway flag is a warning hook, never a client-side default.
        llm = create_llm_client(provider=provider, model=model, api_key="placeholder").get_llm()
        assert getattr(llm, knob.key) is None


@pytest.mark.unit
class TestKnobEnvOverlay:
    """Asserted on the pure overlay function rather than a module reload, which
    would rebind DEFAULT_CONFIG while every importer keeps the pre-reload
    object."""

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_env_sets_the_knob(self, knob, monkeypatch):
        monkeypatch.setenv(knob.env_var, knob.env_string)
        # Stored as the env string (the reference default is None so no
        # coercion applies); _get_provider_kwargs coerces at consumption.
        assert _apply_env_overrides({knob.key: None})[knob.key] == knob.env_string

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_absent_env_leaves_none(self, knob, monkeypatch):
        monkeypatch.delenv(knob.env_var, raising=False)
        assert _apply_env_overrides({knob.key: None})[knob.key] is None

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_library_default_is_none(self, knob):
        # Library neutrality: the shipped default leaves each provider at its
        # own value (the CLI, not DEFAULT_CONFIG, fills the completion cap in).
        # DEFAULT_CONFIG is built at import, so an env value already present
        # in this process is not a regression of the declaration.
        if os.environ.get(knob.env_var):
            pytest.skip(f"{knob.env_var} is set in this environment")
        assert DEFAULT_CONFIG[knob.key] is None


@pytest.mark.unit
class TestProviderKwargs:
    """_get_provider_kwargs coerces and forwards each knob, or omits it."""

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_env_string_coerced(self, knob):
        assert _provider_kwargs(**{knob.key: knob.env_string})[knob.key] == knob.parsed

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_value_passthrough(self, knob):
        assert _provider_kwargs(**{knob.key: knob.value})[knob.key] == knob.value

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_none_omitted(self, knob):
        assert knob.key not in _provider_kwargs(**{knob.key: None})

    @pytest.mark.parametrize("knob", KNOBS, ids=KNOB_IDS)
    def test_empty_string_omitted(self, knob):
        assert knob.key not in _provider_kwargs(**{knob.key: ""})

    @pytest.mark.parametrize(
        "bad", [0, -1, "0", "-1", "8k", "4096.5", 4096.7, Decimal("4096.5"), True]
    )
    def test_non_positive_and_junk_max_tokens_rejected_naming_the_key(self, bad):
        # Forwarding 0 or a negative cap is a deterministic provider 400 on
        # every call — the #177 stall shape — and a typo must not surface as
        # a bare int() ValueError with no key name. A programmatic 4096.7 (or
        # any non-integral numeric, Decimal included) must not be silently
        # int()-truncated either. Temperature has no such gate: 0.0 is a legal
        # value there.
        with pytest.raises(ValueError, match="max_tokens"):
            _provider_kwargs(max_tokens=bad)


@pytest.mark.unit
class TestGatewayUncappedWarning:
    """A gateway provider about to run uncapped warns (#183); a single-host
    provider, or a gateway with a cap, stays quiet.

    ``pytest.warns`` installs an ``always`` filter, so ``len == 1`` here pins
    "one ``warn`` call per graph"; under Python's default filter the same
    text from the same call site shows once per process, which is the intent.
    """

    def test_gateway_without_a_cap_warns_naming_the_fix(self):
        with pytest.warns(RuntimeWarning) as record:
            kwargs = _provider_kwargs(provider="openrouter", max_tokens=None)
        assert "max_tokens" not in kwargs  # still the provider default: a warning, not a cap
        ours = [w for w in record if "#177" in str(w.message)]
        assert len(ours) == 1
        message = str(ours[0].message)
        assert "'openrouter'" in message
        assert "TRADINGAGENTS_MAX_TOKENS" in message
        assert str(DEFAULT_MAX_TOKENS) in message  # a value to reach for, not just a knob name
        # openai_compatible carries the flag too, and a local vLLM is no
        # gateway — the text must stay true for it.
        assert "is a gateway" not in message

    @pytest.mark.parametrize(
        "provider,max_tokens",
        [("openai", None), ("deepseek", None), ("openrouter", 8192), ("openrouter", "8192")],
    )
    def test_no_warning_off_the_gateway_or_with_a_cap(self, provider, max_tokens, recwarn):
        _provider_kwargs(provider=provider, max_tokens=max_tokens)
        assert not [w for w in recwarn if "#177" in str(w.message)]
