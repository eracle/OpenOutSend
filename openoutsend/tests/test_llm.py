from __future__ import annotations

import json
import typing

import httpx2
import pytest

from openoutsend.core import llm


def _reply(text):
    """An Anthropic Messages API response that answers *text*."""
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "some-model",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


@pytest.mark.parametrize("provider", list(llm._PROVIDER_BUILDERS))
def test_every_provider_builds_offline(provider):
    """Each builder imports and constructs its pydantic-ai model without a request.

    pydantic-ai renamed `OpenAIModel` to `OpenAIChatModel`, and both OpenAI builders
    kept importing the old name: every `openai:*` and `openai_compatible:*` install
    died on an `ImportError` while the Anthropic ones carried on (OpenOutreach #959).
    """
    model = llm.build_llm_model(f"{provider}:some-model", "sk-key", "https://llm.invalid/v1")
    assert model.model_name == "some-model"


def test_openai_compatible_requires_an_api_base():
    with pytest.raises(ValueError, match="openai_compatible"):
        llm.build_llm_model("openai_compatible:some-model", "sk-key")


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llm.build_llm_model("nope:some-model", "sk-key")


def test_anthropic_request_survives_union_message_param(monkeypatch):
    """A full Anthropic request goes out and its answer comes back on `anthropic` 2.x.

    2.0 made `BetaMessageParam` a `Union` that pydantic-ai calls as a constructor:
    every `send_sequences` tick died on the credentials ping with `TypeError: Cannot
    instantiate typing.Union`, and no email went out for a day.
    """
    from anthropic import AsyncAnthropic
    from pydantic_ai import Agent
    # Imported before the client is patched: the provider module builds a type union
    # over `AsyncAnthropic` at import time.
    import pydantic_ai.models.anthropic  # noqa: F401
    import pydantic_ai.providers.anthropic  # noqa: F401

    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return _reply("pong")

    client = AsyncAnthropic(
        api_key="sk-key", http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(reply)),
    )
    monkeypatch.setattr("anthropic.AsyncAnthropic", lambda **kwargs: client)

    model = llm.build_llm_model("anthropic:some-model", "sk-key")
    result = llm.run_agent_sync(Agent(model).run("ping"))

    assert result.output == "pong"
    assert requests[0]["messages"] == [
        {"role": "user", "content": [{"text": "ping", "type": "text"}]},
    ]


def test_message_param_shim_leaves_a_constructible_type_alone(monkeypatch):
    import pydantic_ai.models.anthropic as pydantic_ai_anthropic

    class MessageParam(typing.TypedDict):
        role: str

    monkeypatch.setattr(pydantic_ai_anthropic, "BetaMessageParam", MessageParam)
    llm._accept_union_message_param()

    assert pydantic_ai_anthropic.BetaMessageParam is MessageParam
