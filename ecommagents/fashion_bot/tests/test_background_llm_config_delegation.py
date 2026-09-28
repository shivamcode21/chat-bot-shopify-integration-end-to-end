"""Onboarding and prompt generation must follow the shared background config.

Both used to pin a model literal, which meant LLM_MODEL / BACKGROUND_LLM_MODEL
could not reach them and a stale slug could only be fixed by a deploy. They now
delegate to ``get_background_llm_config``, so these tests guard against a literal
creeping back in.

Self-contained: no network, no live OpenRouter.
"""

import pytest

from fashion_bot.core.llm_config import (
    BACKGROUND_OPENROUTER_KEY_ENV,
    LLMConfig,
    LLMProvider,
)
from fashion_bot import client_onboarding, prompt_generator


_BUILDERS = [
    ("onboarding", client_onboarding, "_onboard_llm_config", 0.1, 4000),
    ("prompt_gen", prompt_generator, "_prompt_gen_config", 0.1, 16384),
]


def _sentinel_config(**kwargs):
    """Stand-in for get_background_llm_config that records its kwargs."""
    return LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model="sentinel/model-from-shared-helper",
        temperature=kwargs.get("temperature"),
        max_tokens=kwargs.get("max_tokens"),
        api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
        base_url="https://openrouter.ai/api/v1",
    )


@pytest.mark.parametrize("name,module,attr,_temp,_max", _BUILDERS)
def test_model_comes_from_the_shared_helper(name, module, attr, _temp, _max, monkeypatch):
    """The model must not be pinned locally — it comes from the shared helper."""
    monkeypatch.setattr(module, "get_background_llm_config", _sentinel_config)

    config = getattr(module, attr)()

    assert config.model == "sentinel/model-from-shared-helper"


@pytest.mark.parametrize("name,module,attr,temp,max_tokens", _BUILDERS)
def test_temperature_and_max_tokens_are_preserved(
    name, module, attr, temp, max_tokens, monkeypatch
):
    """Delegating must not silently change either workload's tuning."""
    monkeypatch.setattr(module, "get_background_llm_config", _sentinel_config)

    config = getattr(module, attr)()

    assert config.temperature == temp
    assert config.max_tokens == max_tokens


@pytest.mark.parametrize("name,module,attr,_temp,_max", _BUILDERS)
def test_still_on_the_background_key_and_openrouter(
    name, module, attr, _temp, _max
):
    """Unpatched: both stay OpenRouter on the background key, off the chat key."""
    config = getattr(module, attr)()

    assert config.provider is LLMProvider.OPENROUTER
    assert config.api_key_env_var == BACKGROUND_OPENROUTER_KEY_ENV
    assert config.base_url == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("name,module,attr,_temp,_max", _BUILDERS)
def test_no_hardcoded_gemini_slug_remains(name, module, attr, _temp, _max):
    """A literal model slug here is the bug this change removed."""
    config = getattr(module, attr)()

    assert "gemini-3.1-flash-lite-preview" not in (config.model or "")


def test_onboarding_honours_explicit_overrides(monkeypatch):
    """Callers passing temperature/max_tokens still reach the shared helper."""
    monkeypatch.setattr(
        client_onboarding, "get_background_llm_config", _sentinel_config
    )

    config = client_onboarding._onboard_llm_config(temperature=0.9, max_tokens=123)

    assert config.temperature == 0.9
    assert config.max_tokens == 123
