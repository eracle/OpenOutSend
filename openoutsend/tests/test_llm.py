from __future__ import annotations

import pytest

from openoutsend.core import llm


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
