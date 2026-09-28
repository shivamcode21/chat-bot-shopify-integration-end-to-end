"""Unit tests for OpenRouter upstream provider routing.

OpenRouter load-balances every request across all upstreams serving a model
slug. When one of those upstreams is unusable for us the failure is
intermittent and per-request rather than a clean outage — the case that
motivated this code was a Google AI Studio endpoint whose egress region Google
geo-blocks, returning:

    400 - Provider returned error ... Google AI Studio:
    "User location is not supported for the API use." (FAILED_PRECONDITION)

``_openrouter_provider_routing`` turns env config into OpenRouter's ``provider``
routing block so a known-bad upstream can be excluded from the pool.

These tests are fully self-contained — no network, no live OpenRouter.
"""

import pytest

from fashion_bot.core import llm_factory
from fashion_bot.core.llm_config import LLMConfig, LLMProvider


@pytest.fixture(autouse=True)
def _clear_routing_env(monkeypatch):
    """Every test starts from "no routing configured"."""
    for key in (
        "OPENROUTER_IGNORE_PROVIDERS",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
    ):
        monkeypatch.setattr(
            llm_factory,
            "get_env",
            _env_stub({}),
            raising=False,
        )
    yield


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


# ---------------------------------------------------------------- routing block


def test_no_env_means_no_provider_routing(monkeypatch):
    """Exclusion is off by default — the geo-block is handled by model fallback."""
    _patch_env(monkeypatch, {})
    assert llm_factory._openrouter_provider_routing() is None


def test_ignore_list_is_parsed(monkeypatch):
    _patch_env(monkeypatch, {"OPENROUTER_IGNORE_PROVIDERS": "Google AI Studio"})
    assert llm_factory._openrouter_provider_routing() == {
        "ignore": ["Google AI Studio"]
    }


def test_ignore_list_trims_and_drops_empties(monkeypatch):
    _patch_env(
        monkeypatch,
        {"OPENROUTER_IGNORE_PROVIDERS": " Google AI Studio , , DeepInfra "},
    )
    assert llm_factory._openrouter_provider_routing() == {
        "ignore": ["Google AI Studio", "DeepInfra"]
    }


def test_order_and_ignore_combine(monkeypatch):
    _patch_env(
        monkeypatch,
        {
            "OPENROUTER_IGNORE_PROVIDERS": "Google AI Studio",
            "OPENROUTER_PROVIDER_ORDER": "Google,Together",
        },
    )
    assert llm_factory._openrouter_provider_routing() == {
        "ignore": ["Google AI Studio"],
        "order": ["Google", "Together"],
    }


def test_allow_fallbacks_false_is_emitted(monkeypatch):
    _patch_env(
        monkeypatch,
        {
            "OPENROUTER_PROVIDER_ORDER": "Google",
            "OPENROUTER_ALLOW_FALLBACKS": "false",
        },
    )
    assert llm_factory._openrouter_provider_routing() == {
        "order": ["Google"],
        "allow_fallbacks": False,
    }


def test_lone_allow_fallbacks_does_not_create_a_block(monkeypatch):
    """allow_fallbacks alone is meaningless — don't send an otherwise-empty block."""
    _patch_env(monkeypatch, {"OPENROUTER_ALLOW_FALLBACKS": "false"})
    assert llm_factory._openrouter_provider_routing() is None


# ------------------------------------------------------------- factory wiring


def _capture_chat_openai(monkeypatch):
    """Intercept the ChatOpenAI kwargs the factory builds."""
    captured = {}

    class _FakeChatOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    import langchain_openai

    monkeypatch.setattr(langchain_openai, "ChatOpenAI", _FakeChatOpenAI)
    return captured


def _config(**extra):
    """A model with no default fallback chain, to isolate provider routing."""
    return LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model="openai/gpt-4.1-mini",
        temperature=0.7,
        additional_params=extra or {},
    )


def test_env_routing_reaches_extra_body(monkeypatch):
    _patch_env(monkeypatch, {"OPENROUTER_IGNORE_PROVIDERS": "Google AI Studio"})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config())

    assert captured["extra_body"] == {"provider": {"ignore": ["Google AI Studio"]}}


def test_no_extra_body_when_unconfigured(monkeypatch):
    """No routing and no fallback chain -> payload unchanged from before."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(_config())

    assert "extra_body" not in captured


def test_per_config_provider_overrides_env(monkeypatch):
    """A `provider` in additional_params (e.g. from the DB) wins over env."""
    _patch_env(monkeypatch, {"OPENROUTER_IGNORE_PROVIDERS": "Google AI Studio"})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(
        _config(provider={"order": ["Google"]})
    )

    assert captured["extra_body"] == {"provider": {"order": ["Google"]}}


def test_provider_is_not_leaked_as_a_chatopenai_kwarg(monkeypatch):
    """`provider` must be consumed into extra_body, not forwarded to ChatOpenAI."""
    _patch_env(monkeypatch, {})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(
        _config(provider={"ignore": ["Google AI Studio"]})
    )

    assert "provider" not in captured
    assert captured["extra_body"] == {"provider": {"ignore": ["Google AI Studio"]}}


def test_existing_extra_body_is_preserved(monkeypatch):
    _patch_env(monkeypatch, {"OPENROUTER_IGNORE_PROVIDERS": "Google AI Studio"})
    captured = _capture_chat_openai(monkeypatch)

    llm_factory.LLMFactory._create_openrouter_llm(
        _config(extra_body={"transforms": ["middle-out"]})
    )

    assert captured["extra_body"] == {
        "transforms": ["middle-out"],
        "provider": {"ignore": ["Google AI Studio"]},
    }
