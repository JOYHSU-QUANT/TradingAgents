
from .base_client import BaseLLMClient


def create_llm_client(
    provider: str,
    model: str,
    base_url: str | None = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    # Native (non-OpenAI) APIs are matched first so their string check doesn't
    # import the OpenAI client. Everything else is OpenAI-compatible and routes
    # through the provider registry (single source of truth).
    if provider_lower == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(model, base_url, **kwargs)

    if provider_lower == "google":
        from .google_client import GoogleClient
        return GoogleClient(model, base_url, **kwargs)

    if provider_lower == "azure":
        from .azure_client import AzureOpenAIClient
        return AzureOpenAIClient(model, base_url, **kwargs)

    if provider_lower == "bedrock":
        from .bedrock_client import BedrockClient
        return BedrockClient(model, base_url, **kwargs)

    from .openai_client import OpenAIClient, is_openai_compatible
    if is_openai_compatible(provider_lower):
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    raise ValueError(f"Unsupported LLM provider: {provider}")


# The providers ``create_llm_client`` routes to their own clients above; none is
# in the OpenAI-compatible registry, so none can be a gateway.
_NATIVE_PROVIDERS = frozenset({"anthropic", "google", "azure", "bedrock"})


def is_gateway_provider(provider: str) -> bool:
    """Lazy re-export of the registry predicate ``openai_client.is_gateway_provider``.

    The same import discipline as ``create_llm_client``: native providers are
    answered without touching the OpenAI SDK (the graph asks this for every
    uncapped config, so an Anthropic-only process must not pay a
    ``langchain_openai`` import for a "no"); everything else consults the
    registry, which that provider's own client would import anyway.
    """
    if provider.lower() in _NATIVE_PROVIDERS:
        return False
    from .openai_client import is_gateway_provider as _is_gateway_provider

    return _is_gateway_provider(provider)
