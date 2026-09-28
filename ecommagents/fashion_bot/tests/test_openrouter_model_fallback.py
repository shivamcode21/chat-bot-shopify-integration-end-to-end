"""Unit tests for OpenRouter model fallback chains.

google/gemini-3.1-flash-lite-preview is served only by Google AI Studio, and
~1% of those requests land on a region Google geo-blocks:

    400 - Provider returned error ... Google AI Studio:
    "User location is not supported for the API use." (FAILED_PRECONDITION)

Excluding AI Studio would fix the error but make the model unroutable, forcing a
downgrade for 100% of traffic to avoid a failure affecting 1%. Instead we send
OpenRouter a `models` chain: it serves the primary and only moves to the
fallback on error, so the healthy 99% is untouched.

These tests are fully self-contained — no network, no live OpenRouter.
"""

import pytest

from fashion_bot.core import llm_factory
from fashion_bot.core.llm_config import LLMConfig, LLMProvider


GEMINI_31 = "google/gemini-3.1-flash-lite-preview"
GEMINI_25 = "google/gemini-2.5-flash-lite"


def _env_stub(mapping):
    def _get_env(key, default=None):
        return mapping.get(key, default)

    return _get_env


def _patch_env(monkeypatch, mapping):
    monkeypatch.setattr(llm_factory, "get_env", _env_stub(mapping))

    def _get_bool(key, default=False):
        value = mapping.get(key)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    monkeypatch.setattr(llm_factory, "get_bool", _get_bool)


# ------------------------------------------------------------- chain building


def test_geo_blocked_model_gets_a_fallback(monkeypatch):
    """The primary must lead the chain — OpenRouter reads priority order."""
    _patch_env(monkeypatch, {})
    assert llm_factory._openrouter_fallback_models(GEMINI_31) == [
        GEMINI_31,
        GEMINI_25,
    ]


def test_models_without_a_configured_chain_get_none(monkeypatch):
    """No blanket fallback: an unrelated model must be left alone."""
    _patch_env(monkeypatch, {})
    assert llm_factory._openrouter_fallback_models("openai/gpt-4.1-mini") == []


def test_the_fallback_model_itself_has_no_chain(monkeypatch):
    """Guards against a self-referential or circular chain."""
    _patch_env(monkeypatch, {})
    assert llm_factory._openrouter_fallback_models(GEMINI_25) == []


def test_env_overrides_the_builtin_map(monkeypatch):
    _patch_env(monkeypatch, {"OPENROUTER_FALLBACK_MODELS": "openai/gpt-4o-mini"})
    assert llm_factory._openrouter_fallback_models(GEMINI_31) == [
        GEMINI_31,
        "openai/gpt-4o-mini",
    ]


def test_env_empty_string_disables_fallbacks(monkeypatch):
    """Kill switch — no deploy needed to turn fallbacks off."""
    _patch_env(monkeypatch, {"OPENROUTER_FALLBACK_MODELS": ""})
    assert llm_factory._openrouter_fallback_models(GEMINI_31) == []


def test_primary_is_not_duplicated_in_the_chain(monkeypatch):
    """A fallback equal to the primary would be a wasted retry on the same model."""
    _patch_env(monkeypatch, {"OPENROUTER_FALLBACK_MODELS": f"{GEMINI_31},{GEMINI_25}"})
    assert llm_factory._openrouter_fallback_models(GEMINI_31) == [
        GEMINI_31,
        GEMINI_25,
    ]


def test_env_chain_is_trimmed(monkeypatch):
    _patch_env(monkeypatch, {"OPENROUTER_FALLBACK_MODELS": f"  {GEMINI_25} , , "})
    assert llm_factory._openrouter_fallback_models(GEMINI_31) == [
        GEMINI_31,
        GEMINI_25,
    ]


# ------------------------------------------------------------- factory wiring


def _capture_chat_openai(monkeypatch):
    captured = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    return captured


def _config(model=GEMINI_31, **extra):
    return LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model=model,
        temperature=0.7,
        additional_params=extra or {},
    )


def test_chain_reaches_extra_body(monkeypatch):
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config())

    assert captured["extra_body"]["models"] == [GEMINI_31, GEMINI_25]


def test_primary_model_is_still_sent_as_model(monkeypatch):
    """`models` must not replace `model` — they have to agree on the primary."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config())

    assert captured["model"] == GEMINI_31
    assert captured["extra_body"]["models"][0] == GEMINI_31


def test_models_is_not_leaked_as_a_chatopenai_kwarg(monkeypatch):
    """`models` must be consumed into extra_body, not forwarded to ChatOpenAI."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(
        _config(models=[GEMINI_31, "openai/gpt-4o-mini"])
    )

    assert "models" not in captured
    assert captured["extra_body"]["models"] == [GEMINI_31, "openai/gpt-4o-mini"]


def test_per_config_chain_overrides_the_default(monkeypatch):
    """A `models` in additional_params (e.g. from the DB) wins over the map."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(
        _config(models=[GEMINI_31, "anthropic/claude-sonnet-4"])
    )

    assert captured["extra_body"]["models"] == [GEMINI_31, "anthropic/claude-sonnet-4"]


def test_no_models_key_for_a_model_without_a_chain(monkeypatch):
    """Unrelated models keep a byte-for-byte unchanged payload."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config(model="openai/gpt-4.1-mini"))

    assert "extra_body" not in captured


def test_fallback_and_provider_routing_coexist(monkeypatch):
    """Both blocks must survive together if someone sets an ignore as well."""
    _patch_env(monkeypatch, {"OPENROUTER_IGNORE_PROVIDERS": "DeepInfra"})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config())

    assert captured["extra_body"] == {
        "provider": {"ignore": ["DeepInfra"]},
        "models": [GEMINI_31, GEMINI_25],
    }
