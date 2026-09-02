"""Tests for the configurable completion-token cap (#177).

``max_tokens`` is a cross-provider knob: when set it must reach the underlying
chat client; when unset the provider keeps its own default. The engine default
stays None (provider default) — the perp bridge is the layer that always sets
it, because an uncapped request through a gateway lets the upstream substitute
the model's full context length and deterministically reject the call.
"""

import pytest

from tradingagents.llm_clients.factory import create_llm_client


@pytest.mark.unit
class TestMaxTokensForwarding:
    @pytest.mark.parametrize(
        "provider,model,attr",
        [
            # Google's client declares ``max_tokens`` as the alias of its
            # ``max_output_tokens`` field, and has no payload builder to pin —
            # the attribute is the right layer there.
            ("google", "gemini-2.5-flash", "max_output_tokens"),
            # Anthropic already forwarded the kwarg before this change; the
            # row keeps that claim honest.
            ("anthropic", "claude-sonnet-4-6", "max_tokens"),
        ],
    )
    def test_max_tokens_reaches_client_when_set(self, provider, model, attr):
        llm = create_llm_client(
            provider=provider, model=model, max_tokens=1234, api_key="placeholder"
        ).get_llm()
        assert getattr(llm, attr) == 1234

    @pytest.mark.parametrize(
        "provider,model,wire_key",
        [
            # Chat Completions: langchain renames the kwarg on the way out.
            # OpenRouter honors this spelling (verified live against the
            # gateway: finish_reason=length at exactly the cap).
            ("openrouter", "qwen/qwen3-235b-a22b-2507", "max_completion_tokens"),
            # Native OpenAI takes the Responses branch, which renames it
            # again — a third spelling, and the busiest provider.
            ("openai", "gpt-4.1", "max_output_tokens"),
        ],
    )
    def test_max_tokens_reaches_the_request_payload(self, provider, model, wire_key):
        # The pydantic field alone proves nothing about the wire, and every
        # OpenAI-compatible provider (deepseek, ollama, xai, the local
        # endpoints) shares this one payload builder.
        llm = create_llm_client(
            provider=provider, model=model, max_tokens=1234, api_key="placeholder"
        ).get_llm()
        payload = llm._get_request_payload([("human", "hi")])
        assert payload[wire_key] == 1234

    def test_max_tokens_omitted_leaves_provider_default(self):
        # Not passing max_tokens must not force it to a value.
        llm = create_llm_client(
            provider="openrouter", model="qwen/qwen3-235b-a22b-2507", api_key="placeholder"
        ).get_llm()
        assert llm.max_tokens is None


@pytest.mark.unit
class TestMaxTokensEnvOverlay:
    """Asserted on the pure overlay function rather than a module reload,
    which would rebind DEFAULT_CONFIG while every importer keeps the
    pre-reload object."""

    def test_env_sets_max_tokens(self, monkeypatch):
        from tradingagents.default_config import _apply_env_overrides

        monkeypatch.setenv("TRADINGAGENTS_MAX_TOKENS", "4096")
        # Stored as the env string (reference default is None so no coercion
        # applies); _get_provider_kwargs int()s it at consumption.
        assert _apply_env_overrides({"max_tokens": None})["max_tokens"] == "4096"

    def test_absent_env_leaves_none(self, monkeypatch):
        from tradingagents.default_config import _apply_env_overrides

        monkeypatch.delenv("TRADINGAGENTS_MAX_TOKENS", raising=False)
        assert _apply_env_overrides({"max_tokens": None})["max_tokens"] is None


@pytest.mark.unit
class TestProviderKwargsMaxTokens:
    """_get_provider_kwargs validates and forwards max_tokens, or omits it."""

    def _kwargs_for(self, max_tokens):
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        # Call the method without constructing the full graph.
        graph = TradingAgentsGraph.__new__(TradingAgentsGraph)
        graph.config = {"llm_provider": "openrouter", "max_tokens": max_tokens}
        return TradingAgentsGraph._get_provider_kwargs(graph)

    def test_int_string_coerced(self):
        assert self._kwargs_for("8192")["max_tokens"] == 8192

    def test_int_passthrough(self):
        assert self._kwargs_for(8192)["max_tokens"] == 8192

    def test_none_omitted(self):
        assert "max_tokens" not in self._kwargs_for(None)

    def test_empty_string_omitted(self):
        assert "max_tokens" not in self._kwargs_for("")

    @pytest.mark.parametrize("bad", [0, -1, "0", "-1", "8k", "4096.5"])
    def test_non_positive_and_junk_rejected_naming_the_key(self, bad):
        # Forwarding 0 or a negative cap is a deterministic provider 400 on
        # every call — the #177 stall shape — and a typo must not surface as
        # a bare int() ValueError with no key name.
        with pytest.raises(ValueError, match="max_tokens"):
            self._kwargs_for(bad)
