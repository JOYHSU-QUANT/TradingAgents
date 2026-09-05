"""The OpenAI-compatible provider registry is the single source of truth for the
family; this guards each provider's resolved config (base URL, subclass, auth,
Responses API) so a future edit can't silently break one.
"""
import pytest

from tradingagents.llm_clients.openai_client import (
    OPENAI_COMPATIBLE_PROVIDERS,
    DeepSeekChatOpenAI,
    MinimaxChatOpenAI,
    NormalizedChatOpenAI,
    is_gateway_provider,
    is_openai_compatible,
)


@pytest.mark.unit
def test_registry_membership():
    assert is_openai_compatible("openai")
    assert is_openai_compatible("openai_compatible")  # the generic endpoint
    # native (different API) clients are intentionally NOT in the registry
    assert not is_openai_compatible("anthropic")
    assert not is_openai_compatible("google")
    assert not is_openai_compatible("azure")


@pytest.mark.unit
@pytest.mark.parametrize(
    "provider,expected",
    [
        ("openrouter", True),
        ("OpenRouter", True),  # case-insensitive like every registry lookup
        ("openai_compatible", True),  # user-supplied URL: the upstream is unknown here
        ("openai", False),  # a single-host API: "no cap" means its own default
        ("ollama", False),  # a fixed local server, not a router
        ("anthropic", False),  # not in the registry at all
        ("no-such-provider", False),
    ],
)
def test_gateway_flag_is_read_from_the_registry(provider, expected):
    # The graph's uncapped-request warning (#183) keys off this flag, so which
    # providers carry it is a registry fact pinned here, next to its siblings.
    assert is_gateway_provider(provider) is expected


@pytest.mark.unit
def test_native_provider_set_matches_the_factory_dispatch_chain():
    # factory._NATIVE_PROVIDERS exists only to keep is_gateway_provider from
    # importing the OpenAI SDK for providers create_llm_client routes to their
    # own clients; derive that chain's names from its source so a sixth native
    # client cannot be added without extending the set.
    import ast
    import inspect
    import textwrap

    from tradingagents.llm_clients import factory

    tree = ast.parse(textwrap.dedent(inspect.getsource(factory.create_llm_client)))

    def mentions(node: ast.AST) -> bool:
        return isinstance(node, ast.Name) and node.id == "provider_lower"

    dispatched: set[str] = set()
    for node in ast.walk(tree):
        # EVERY comparison in the function must be the one dispatch shape,
        # ``provider_lower == "<literal>"``: a reversed ``"x" == provider_lower``,
        # an inline ``provider.lower() == "x"``, ``provider_lower in (...)`` or
        # ``!=`` would all be dispatches this lock cannot count, so they are
        # refused rather than skipped. Method calls on the name
        # (``provider_lower.startswith(...)``) likewise; passing it as a call
        # argument (the registry lookup) is not a comparison and is fine.
        if isinstance(node, ast.Compare):
            [op], [comparator] = node.ops, node.comparators
            assert mentions(node.left), ast.dump(node)
            assert isinstance(op, ast.Eq) and isinstance(comparator, ast.Constant), ast.dump(node)
            assert isinstance(comparator.value, str), ast.dump(node)
            dispatched.add(comparator.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert not mentions(node.func.value), ast.dump(node)
    assert dispatched == set(factory._NATIVE_PROVIDERS)


@pytest.mark.unit
def test_native_providers_answer_without_importing_the_openai_client():
    # The graph asks this for every uncapped config; an Anthropic-only process
    # must not pay a langchain_openai import for a "no". A fresh interpreter,
    # because this suite has long since imported the module itself.
    import subprocess
    import sys

    probe = (
        "import sys\n"
        "from tradingagents.llm_clients.factory import _NATIVE_PROVIDERS, is_gateway_provider\n"
        "assert not any(is_gateway_provider(p) for p in _NATIVE_PROVIDERS)\n"
        "assert 'tradingagents.llm_clients.openai_client' not in sys.modules, 'imported'\n"
        "assert is_gateway_provider('openrouter')\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, timeout=120)


@pytest.mark.unit
def test_gateway_flag_is_declared_on_the_spec():
    assert OPENAI_COMPATIBLE_PROVIDERS["openrouter"].gateway is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].gateway is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai"].gateway is False


@pytest.mark.unit
@pytest.mark.parametrize("provider,base_url,chat_class,responses", [
    ("openai", None, NormalizedChatOpenAI, True),
    ("xai", "https://api.x.ai/v1", NormalizedChatOpenAI, False),
    ("deepseek", "https://api.deepseek.com", DeepSeekChatOpenAI, False),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1", NormalizedChatOpenAI, False),
    ("glm", "https://api.z.ai/api/paas/v4/", NormalizedChatOpenAI, False),
    ("glm-cn", "https://open.bigmodel.cn/api/paas/v4/", NormalizedChatOpenAI, False),
    ("minimax", "https://api.minimax.io/v1", MinimaxChatOpenAI, False),
    ("minimax-cn", "https://api.minimaxi.com/v1", MinimaxChatOpenAI, False),
    ("openrouter", "https://openrouter.ai/api/v1", NormalizedChatOpenAI, False),
    ("mistral", "https://api.mistral.ai/v1", NormalizedChatOpenAI, False),
    ("kimi", "https://api.moonshot.ai/v1", NormalizedChatOpenAI, False),
    ("groq", "https://api.groq.com/openai/v1", NormalizedChatOpenAI, False),
    ("nvidia", "https://integrate.api.nvidia.com/v1", NormalizedChatOpenAI, False),
    ("ollama", "http://localhost:11434/v1", NormalizedChatOpenAI, False),
])
def test_registry_spec(provider, base_url, chat_class, responses):
    spec = OPENAI_COMPATIBLE_PROVIDERS[provider]
    assert spec.base_url == base_url
    assert spec.chat_class is chat_class
    assert spec.use_responses_api is responses


@pytest.mark.unit
def test_key_optionality():
    # Local/generic endpoints are key-optional; hosted APIs require a key.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].key_optional is True
    assert OPENAI_COMPATIBLE_PROVIDERS["openai_compatible"].require_base_url is True
    assert OPENAI_COMPATIBLE_PROVIDERS["xai"].key_optional is False
    # OLLAMA_BASE_URL is the only base-URL env override.
    assert OPENAI_COMPATIBLE_PROVIDERS["ollama"].base_url_env == "OLLAMA_BASE_URL"
