"""Amazon Bedrock — first-class native client via the optional langchain-aws extra.

Auth uses the AWS credential chain (no single key env); the model is a Bedrock
model ID / inference profile ID; langchain-aws is imported lazily with a clear
install hint when the [bedrock] extra is absent.
"""
import sys

import pytest

from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.factory import create_llm_client
from tradingagents.llm_clients.validators import validate_model


@pytest.mark.unit
def test_factory_routes_bedrock():
    client = create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0")
    assert type(client).__name__ == "BedrockClient"


@pytest.mark.unit
def test_bedrock_any_model_and_no_key_env():
    assert validate_model("bedrock", "any.model-id:0") is True
    # Bedrock uses the AWS credential chain, so there is no single key env.
    assert get_api_key_env("bedrock") is None


@pytest.mark.unit
def test_helpful_error_when_langchain_aws_absent(monkeypatch):
    import tradingagents.llm_clients.bedrock_client as bc
    monkeypatch.setattr(bc, "_BEDROCK_CLASS", None)
    monkeypatch.setitem(sys.modules, "langchain_aws", None)  # force ImportError on import
    with pytest.raises(ImportError, match=r"bedrock"):
        create_llm_client("bedrock", "m").get_llm()


def _capture_kwargs(monkeypatch):
    """Stub _bedrock_class so the constructor kwargs are testable without the
    optional langchain-aws extra installed."""
    import tradingagents.llm_clients.bedrock_client as bc
    captured = {}

    class _FakeChat:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(bc, "_bedrock_class", lambda: _FakeChat)
    return captured


@pytest.mark.unit
def test_bearer_token_passed_as_api_key(monkeypatch):
    # #1103: a Bedrock API key authenticates without AWS access keys.
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "bt-secret")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0").get_llm()
    assert captured["api_key"] == "bt-secret"
    assert captured["region_name"] == "us-east-1"


@pytest.mark.unit
def test_no_bearer_token_omits_api_key(monkeypatch):
    # Without a token, fall back to the AWS credential chain (no api_key kwarg).
    captured = _capture_kwargs(monkeypatch)
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    create_llm_client("bedrock", "us.anthropic.claude-opus-4-8-v1:0").get_llm()
    assert "api_key" not in captured


@pytest.mark.unit
def test_construction_when_extra_installed(monkeypatch):
    pytest.importorskip("langchain_aws")
    import tradingagents.llm_clients.bedrock_client as bc
    monkeypatch.setattr(bc, "_BEDROCK_CLASS", None)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    llm = create_llm_client("bedrock", "us.anthropic.claude-sonnet-5").get_llm()
    assert type(llm).__name__ == "NormalizedChatBedrockConverse"
    assert llm.region_name == "eu-west-1"


@pytest.mark.unit
def test_a_forwarded_max_retries_reaches_the_botocore_retry_config(monkeypatch):
    # #263 asked whether ChatBedrockConverse drops a forwarded ``max_retries``
    # the way the #177/#212 knobs were dropped. It does not: langchain-aws
    # declares the field (at 1.5.0, the pin floor, as now) and folds it into
    # the botocore ``Config`` both of its boto clients are built with, and its
    # model config is ``extra="forbid"``, so an unknown kwarg would raise
    # rather than vanish. Pinned through the REAL class and the REAL boto
    # client it builds (constructing one needs no credentials and makes no
    # call), so a langchain-aws release that moves the knob fails here
    # instead of silently reverting a Bedrock deployment to the SDK default.
    pytest.importorskip("langchain_aws")
    import tradingagents.llm_clients.bedrock_client as bc

    monkeypatch.setattr(bc, "_BEDROCK_CLASS", None)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    # A named-but-absent profile in the developer shell would fail boto3's
    # session build before the assertion; credentials themselves are not needed.
    for var in ("AWS_BEARER_TOKEN_BEDROCK", "AWS_PROFILE", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    llm = create_llm_client("bedrock", "us.anthropic.claude-sonnet-5", max_retries=8).get_llm()
    # botocore counts the first attempt too: max_attempts=8 is 9 attempts in all.
    assert llm.client.meta.config.retries["total_max_attempts"] == 9


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stop_reason", "truncated"), [("max_tokens", True), ("end_turn", False)]
)
def test_the_converse_stop_reason_is_read_through_the_real_converter(stop_reason, truncated):
    # ``completion_metadata``'s Bedrock row was transcribed from the Converse
    # docs, not produced. Feed the REAL converter a canned response and read
    # it back through the reader: the key it files is camelCase ``stopReason``
    # (the raw Converse dict), which the transcribed ``stop_reason`` row read
    # as "not truncated" (#214). The extra rides the ``dev`` extra so the
    # suite runs this; importorskip keeps a bare install honest.
    pytest.importorskip("langchain_aws")
    from langchain_aws import ChatBedrockConverse
    from langchain_core.messages import HumanMessage

    from tradingagents.llm_clients.completion_metadata import completion_metadata_of

    from .conftest import llm_result_of

    class CannedConverse:
        def converse(self, **_request):
            return {
                "ResponseMetadata": {"HTTPStatusCode": 200},
                "output": {"message": {"role": "assistant", "content": [{"text": "cut"}]}},
                "stopReason": stop_reason,
                "usage": {"inputTokens": 10, "outputTokens": 8192, "totalTokens": 8202},
                "metrics": {"latencyMs": 5},
            }

    llm = ChatBedrockConverse(
        model="anthropic.claude-x", region_name="us-east-1", client=CannedConverse()
    )
    read = completion_metadata_of(llm_result_of(llm._generate([HumanMessage(content="hi")])))
    assert read.stop_reason == stop_reason
    assert read.truncated is truncated
    assert read.output_tokens == 8192
