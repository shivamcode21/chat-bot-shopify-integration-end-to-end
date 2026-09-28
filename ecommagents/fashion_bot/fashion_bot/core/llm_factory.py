"""
LLM Factory - Creates LLM instances based on configuration
Supports multiple providers: OpenAI, Anthropic, Groq, etc.
"""
import json
import logging
import threading
from typing import Optional, Dict, Any, Tuple

from fashion_bot.core.llm_config import LLMConfig, LLMProvider, LLMConfigManager, DEFAULT_LLM_CONFIG
from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.env_loader import get_env, get_bool, get_float

logger = logging.getLogger("llm_factory")

_llm_instance_cache: Dict[str, Any] = {}
_llm_cache_lock = threading.Lock()

# ── Streaming stall watchdog ─────────────────────────────────────────────────
# langchain-openai aborts a stream that produces no chunk for this long. Its own
# default is 120s, which is far past what a customer will wait: a provider that
# accepts the request, returns 200 headers and then never emits an SSE chunk
# (TCP alive, no content) burned a full 134s webchat turn before the watchdog
# fired, by which point the browser had already given up. 20s is well beyond a
# healthy time-to-first-chunk for the models we run, so this trips only on real
# stalls. Set LLM_STREAM_CHUNK_TIMEOUT_S=0 to restore the library default.
_STREAM_CHUNK_TIMEOUT_KWARG = "stream_chunk_timeout"
DEFAULT_STREAM_CHUNK_TIMEOUT_S = 20.0
_stream_chunk_timeout_unsupported_logged = False


def _stream_chunk_timeout_kwargs(chat_cls: Any, extra_params: Dict[str, Any]) -> Dict[str, Any]:
    """Constructor kwargs applying the stream stall watchdog, if supported.

    Returns an empty dict — leaving behaviour exactly as it is today — when the
    caller already set the value, when it is disabled by env, or when the
    installed langchain-openai is too old to accept the kwarg (the requirements
    pin is a range, so a deploy can legitimately resolve to a version without
    it and must not start throwing constructor errors).

    Precedence: per-agent ``additional_params`` > ``LLM_STREAM_CHUNK_TIMEOUT_S``
    > this default. Note the constructor kwarg overrides langchain-openai's own
    ``LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S``, so tune ours, not theirs.
    """
    global _stream_chunk_timeout_unsupported_logged

    if _STREAM_CHUNK_TIMEOUT_KWARG in extra_params:
        # An explicit per-client / per-agent value wins over the global default.
        return {}

    timeout_s = get_float("LLM_STREAM_CHUNK_TIMEOUT_S", DEFAULT_STREAM_CHUNK_TIMEOUT_S)
    if not timeout_s or timeout_s <= 0:
        return {}

    try:
        supported = _STREAM_CHUNK_TIMEOUT_KWARG in (getattr(chat_cls, "model_fields", None) or {})
    except Exception:
        supported = False

    if not supported:
        if not _stream_chunk_timeout_unsupported_logged:
            _stream_chunk_timeout_unsupported_logged = True
            logger.warning(
                f"⚠️ {getattr(chat_cls, '__name__', chat_cls)} does not accept "
                f"'{_STREAM_CHUNK_TIMEOUT_KWARG}'; streaming stalls will fall back to the "
                "library default. Upgrade langchain-openai to enable the watchdog."
            )
        return {}

    return {_STREAM_CHUNK_TIMEOUT_KWARG: timeout_s}


# No upstream is excluded by default.
#
# Excluding Google AI Studio would stop the geo-block error, but it would also
# make google/gemini-3.1-flash-lite-preview unroutable — AI Studio is its only
# upstream — so it would force a downgrade to an older model for 100% of
# traffic to avoid an error affecting ~1%. We keep the full provider pool and
# handle the failure with a model fallback instead (see
# DEFAULT_MODEL_FALLBACKS), which only degrades the requests that actually fail.
#
# Kept as an env-settable escape hatch for an upstream that is broken enough to
# be worth excluding outright.
DEFAULT_IGNORED_PROVIDERS = ""

# Per-model fallback chains, applied via OpenRouter's `models` parameter.
#
# OpenRouter tries the primary model first and only moves down the list if it
# errors ("By default, any error can trigger the use of a fallback model" —
# openrouter.ai/docs/guides/routing/model-fallbacks). That is exactly the shape
# we want for the AI Studio geo-block: the ~1% of requests that hit a blocked
# region get served by the fallback, and the other 99% are untouched, with no
# extra latency and no downgrade on the happy path.
#
# Keyed by primary model so a fallback is only offered where it makes sense.
# Deliberately NOT global: OpenRouter also falls back on context-length errors,
# so a blanket chain could silently answer an over-length gpt-4.1-mini request
# with a different vendor's model.
DEFAULT_MODEL_FALLBACKS = {
    "google/gemini-3.1-flash-lite-preview": ["google/gemini-2.5-flash-lite"],
}


def _openrouter_fallback_models(primary: str) -> list:
    """Build the `models` chain for a primary model, or [] for no fallback.

    OPENROUTER_FALLBACK_MODELS (CSV) overrides the built-in map and applies to
    every model; set it to an empty string to disable fallbacks entirely.
    """
    raw = get_env("OPENROUTER_FALLBACK_MODELS")
    if raw is not None:
        fallbacks = [part.strip() for part in raw.split(",") if part.strip()]
    else:
        fallbacks = list(DEFAULT_MODEL_FALLBACKS.get(primary, []))

    # OpenRouter reads the chain from `models`, so the primary must lead it.
    chain = [primary] + [f for f in fallbacks if f != primary]
    return chain if len(chain) > 1 else []


def _failover_enabled() -> bool:
    """Global kill-switch for provider failover (default on)."""
    return get_bool("LLM_FAILOVER_ENABLED", True)


def _csv_env(key: str, default: str = "") -> list:
    """Parse a comma-separated env var into a list of trimmed, non-empty strings.

    An explicitly-empty env var overrides `default` with an empty list, so a
    default-on setting can be switched off without a code change.
    """
    raw = get_env(key)
    if raw is None:
        raw = default
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _openrouter_provider_routing() -> Optional[Dict[str, Any]]:
    """Build OpenRouter's `provider` routing block from environment config.

    OpenRouter load-balances each request across every upstream serving a model
    slug. When an upstream is unusable for us (e.g. a Google AI Studio endpoint
    whose egress region Google geo-blocks, which returns
    400 FAILED_PRECONDITION "User location is not supported for the API use"),
    that shows up as an intermittent per-request failure rather than a clean
    outage. Pinning the routing removes the bad upstream from the pool.

    Off by default — the geo-block is handled by a model fallback instead, which
    does not cost us the primary model. This stays available for an upstream bad
    enough to exclude outright.

    Env vars (all optional; unset means "let OpenRouter choose"):
      OPENROUTER_IGNORE_PROVIDERS  - CSV of provider names to exclude
      OPENROUTER_PROVIDER_ORDER    - CSV of provider names to prefer, in order
      OPENROUTER_ALLOW_FALLBACKS   - bool, default true. When false, OpenRouter
                                     will not fall back outside the ordered list.

    NOTE: ignoring every provider that serves a slug makes the model
    unroutable (OpenRouter returns 404 "No allowed providers are available").
    google/gemini-3.1-flash-lite-preview is served *only* by Google AI Studio,
    so excluding AI Studio breaks it — check
    https://openrouter.ai/api/v1/models/<slug>/endpoints before setting this.
    """
    provider: Dict[str, Any] = {}

    ignored = _csv_env("OPENROUTER_IGNORE_PROVIDERS", DEFAULT_IGNORED_PROVIDERS)
    if ignored:
        provider["ignore"] = ignored

    order = _csv_env("OPENROUTER_PROVIDER_ORDER")
    if order:
        provider["order"] = order

    # Only meaningful alongside order/ignore, so don't emit a lone allow_fallbacks.
    if provider and not get_bool("OPENROUTER_ALLOW_FALLBACKS", True):
        provider["allow_fallbacks"] = False

    return provider or None


def _set_llm_caller_safe(tool_name: Optional[str]) -> None:
    """Set the llm_caller label for metric attribution. Never raises."""
    try:
        from fashion_bot.monitoring.otel_metrics import set_llm_caller
        set_llm_caller(tool_name or "unknown")
    except Exception:
        pass


def _config_cache_key(config: LLMConfig) -> str:
    """Build a hashable cache key from an LLMConfig."""
    extra = json.dumps(config.additional_params, sort_keys=True) if config.additional_params else ""
    return (
        f"{config.provider.value}|{config.model}|{config.temperature}"
        f"|{config.max_tokens}|{config.api_key_env_var}|{config.base_url}|{extra}"
    )


class LLMFactory:
    """
    Factory for creating LLM instances.
    Returns the appropriate LLM client based on configuration.
    Instances are cached by config fingerprint to avoid per-turn object creation.
    """
    
    @staticmethod
    def get_llm(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        override_config: Optional[LLMConfig] = None,
    ):
        """
        Get LLM instance based on configuration.

        Args:
            tool_name: Name of the tool calling the LLM. Doubles as the
                ``caller`` label on llm.* OTel metrics — pass a meaningful
                string even when using ``override_config`` so metric attribution
                is correct.
            client_id: Client ID for client-specific LLM
            state: State dict (can extract client_id from here)
            override_config: Override config (for testing or special cases)

        Returns:
            LLM instance (langchain or direct API client)
        """
        # Load test mode: return mock LLM (no real API calls)
        if get_bool("LOAD_TEST_MODE", False):
            from load_tests.mock_llm import MockChatModel
            return MockChatModel.get_singleton()

        # Get configuration
        if override_config:
            config = override_config
        else:
            config = LLMConfigManager.get_llm_config(tool_name, client_id, state)

        log_with_trace_id(
            state,
            f"LLM: {config.provider.value}/{config.model} tool={tool_name}",
            "debug"
        )

        _set_llm_caller_safe(tool_name)
        return LLMFactory._get_or_create(config)

    @staticmethod
    async def aget_llm(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        override_config: Optional[LLMConfig] = None,
    ):
        """
        Async version of get_llm. Uses async DB lookup for LLM configuration.

        Args:
            tool_name: Name of the tool calling the LLM. Doubles as the
                ``caller`` label on llm.* OTel metrics — pass a meaningful
                string even when using ``override_config`` so metric attribution
                is correct.
            client_id: Client ID for client-specific LLM
            state: State dict (can extract client_id from here)
            override_config: Override config (for testing or special cases)

        Returns:
            LLM instance (langchain or direct API client)
        """
        # Load test mode: return mock LLM (no real API calls)
        if get_bool("LOAD_TEST_MODE", False):
            from load_tests.mock_llm import MockChatModel
            return MockChatModel.get_singleton()

        # Get configuration
        if override_config:
            config = override_config
        else:
            config = await LLMConfigManager.aget_llm_config(tool_name, client_id, state)

        log_with_trace_id(
            state,
            f"LLM: {config.provider.value}/{config.model} tool={tool_name}",
            "debug"
        )

        _set_llm_caller_safe(tool_name)
        return LLMFactory._get_or_create(config)

    # ============================================
    # Provider Failover
    # ============================================

    @staticmethod
    def get_default_llm():
        """Return the cached default chat model (``LLM_PROVIDER`` / ``LLM_MODEL``)
        used as the failover target, or ``None`` when failover is disabled
        (``LLM_FAILOVER_ENABLED=false``).

        Call sites attach it with LangChain's native
        ``Runnable.with_fallbacks([...])`` so a per-client / per-agent model that
        errors out (provider down, 429, transient 5xx, …) transparently retries
        the SAME request on the default model — without changing anything else.
        The returned object is a real chat model (so ``bind_tools`` / LCEL /
        AgentExecutor keep working); identity-compare it against the primary to
        skip wrapping when the primary already IS the default.
        """
        if not _failover_enabled():
            return None
        return LLMFactory._get_or_create(DEFAULT_LLM_CONFIG)

    # ============================================
    # Cached Instance Resolution
    # ============================================

    @staticmethod
    def _get_or_create(config: LLMConfig):
        """Return a cached LLM instance or create one and cache it."""
        key = _config_cache_key(config)
        cached = _llm_instance_cache.get(key)
        if cached is not None:
            return cached

        provider_value = config.provider.value if hasattr(config.provider, 'value') else str(config.provider)

        creators = {
            LLMProvider.OPENAI.value: LLMFactory._create_openai_llm,
            LLMProvider.ANTHROPIC.value: LLMFactory._create_anthropic_llm,
            LLMProvider.GROQ.value: LLMFactory._create_groq_llm,
            LLMProvider.TOGETHER.value: LLMFactory._create_together_llm,
            LLMProvider.GEMINI.value: LLMFactory._create_gemini_llm,
            LLMProvider.AZURE_OPENAI.value: LLMFactory._create_azure_openai_llm,
            LLMProvider.OPENROUTER.value: LLMFactory._create_openrouter_llm,
            LLMProvider.CEREBRAS.value: LLMFactory._create_cerebras_llm,
            LLMProvider.OLLAMA.value: LLMFactory._create_ollama_llm,
        }

        creator = creators.get(provider_value)
        if creator is None:
            raise ValueError(f"Unsupported LLM provider: {config.provider}")

        instance = creator(config)

        try:
            from fashion_bot.monitoring.otel_metrics import get_llm_callback_handler
            handler = get_llm_callback_handler()
            if handler is not None and hasattr(instance, "callbacks"):
                existing = instance.callbacks or []
                if handler not in existing:
                    instance.callbacks = [*existing, handler]
        except Exception:
            pass

        with _llm_cache_lock:
            _llm_instance_cache[key] = instance
        logger.debug(f"Cached new LLM instance: {config.provider.value}/{config.model} (cache size={len(_llm_instance_cache)})")
        return instance

    # ============================================
    # Provider-Specific LLM Creation
    # ============================================
    
    @staticmethod
    def _create_openai_llm(config: LLMConfig):
        """Create OpenAI LLM instance."""
        try:
            from langchain_openai import ChatOpenAI
            
            api_key = get_env(config.api_key_env_var or "OPENAI_API_KEY")

            return ChatOpenAI(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                openai_api_key=api_key,
                **_stream_chunk_timeout_kwargs(ChatOpenAI, config.additional_params),
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")
    
    @staticmethod
    def _create_anthropic_llm(config: LLMConfig):
        """Create Anthropic Claude LLM instance."""
        try:
            from langchain_anthropic import ChatAnthropic
            
            api_key = get_env(config.api_key_env_var or "ANTHROPIC_API_KEY")
            
            return ChatAnthropic(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens or 4096,
                anthropic_api_key=api_key,
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-anthropic not installed. Run: pip install langchain-anthropic")
    
    @staticmethod
    def _create_groq_llm(config: LLMConfig):
        """Create Groq LLM instance.

        Groq serves an OpenAI-compatible endpoint, so we reuse ChatOpenAI with
        the Groq base URL — same pattern as Cerebras/OpenRouter — which keeps
        tool-calling / streaming behaviour identical and avoids the extra
        langchain-groq SDK dependency. Set GROQ_API_KEY. Model e.g.
        "llama-3.3-70b-versatile" or "openai/gpt-oss-120b".
        """
        try:
            from langchain_openai import ChatOpenAI

            api_key = get_env(config.api_key_env_var or "GROQ_API_KEY")
            base_url = config.base_url or "https://api.groq.com/openai/v1"

            extra_params = dict(config.additional_params)
            default_headers = extra_params.pop("extra_headers", {})

            return ChatOpenAI(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                openai_api_key=api_key,
                openai_api_base=base_url,
                default_headers=default_headers,
                **_stream_chunk_timeout_kwargs(ChatOpenAI, extra_params),
                **extra_params,
            )
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")
    
    @staticmethod
    def _create_together_llm(config: LLMConfig):
        """Create Together AI LLM instance."""
        try:
            from langchain_together import ChatTogether
            
            api_key = get_env(config.api_key_env_var or "TOGETHER_API_KEY")
            
            return ChatTogether(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                together_api_key=api_key,
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-together not installed. Run: pip install langchain-together")
    
    @staticmethod
    def _create_gemini_llm(config: LLMConfig):
        """Create Google Gemini LLM instance."""
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI
            
            api_key = get_env(config.api_key_env_var or "GOOGLE_API_KEY")
            
            return ChatGoogleGenerativeAI(
                model=config.model,
                temperature=config.temperature,
                max_output_tokens=config.max_tokens,
                google_api_key=api_key,
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-google-genai not installed. Run: pip install langchain-google-genai")
    
    @staticmethod
    def _create_azure_openai_llm(config: LLMConfig):
        """Create Azure OpenAI LLM instance."""
        try:
            from langchain_openai import AzureChatOpenAI
            
            api_key = get_env(config.api_key_env_var or "AZURE_OPENAI_API_KEY")
            azure_endpoint = config.base_url or get_env("AZURE_OPENAI_ENDPOINT")
            
            return AzureChatOpenAI(
                azure_deployment=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                azure_endpoint=azure_endpoint,
                api_key=api_key,
                api_version=config.additional_params.get("api_version", "2024-02-15-preview"),
                **_stream_chunk_timeout_kwargs(AzureChatOpenAI, config.additional_params),
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")
    
    @staticmethod
    def _create_openrouter_llm(config: LLMConfig):
        """Create OpenRouter LLM instance (OpenAI-compatible API)."""
        try:
            from langchain_openai import ChatOpenAI

            api_key = get_env(config.api_key_env_var or "OPENROUTER_API_KEY")
            base_url = config.base_url or "https://openrouter.ai/api/v1"

            # Filter out openrouter-specific headers from additional_params
            extra_params = dict(config.additional_params)
            default_headers = extra_params.pop("extra_headers", {})

            # OpenRouter upstream routing. A per-config `provider` block (e.g. from
            # client_agent_llm_config.additional_params) wins over the env default.
            extra_body = dict(extra_params.pop("extra_body", {}) or {})
            provider_routing = extra_params.pop("provider", None) or _openrouter_provider_routing()
            if provider_routing and "provider" not in extra_body:
                extra_body["provider"] = provider_routing

            # Model fallback chain. OpenRouter only moves past the primary when
            # it errors, so this costs nothing on a healthy request. A per-config
            # `models` wins over the default chain.
            models_chain = extra_params.pop("models", None) or _openrouter_fallback_models(config.model)
            if models_chain and "models" not in extra_body:
                extra_body["models"] = models_chain

            if extra_body:
                extra_params["extra_body"] = extra_body

            return ChatOpenAI(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                openai_api_key=api_key,
                openai_api_base=base_url,
                default_headers=default_headers,
                **_stream_chunk_timeout_kwargs(ChatOpenAI, extra_params),
                **extra_params
            )
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")

    @staticmethod
    def _create_cerebras_llm(config: LLMConfig):
        """Create Cerebras LLM instance.

        Cerebras serves an OpenAI-compatible endpoint, so we reuse ChatOpenAI
        with the Cerebras base URL — same pattern as OpenRouter — which keeps
        tool-calling / streaming behaviour identical and avoids an extra SDK
        dependency. Set CEREBRAS_API_KEY. Model e.g. "gpt-oss-120b".
        """
        try:
            from langchain_openai import ChatOpenAI

            api_key = get_env(config.api_key_env_var or "CEREBRAS_API_KEY")
            base_url = config.base_url or "https://api.cerebras.ai/v1"

            extra_params = dict(config.additional_params)
            default_headers = extra_params.pop("extra_headers", {})

            return ChatOpenAI(
                model=config.model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                openai_api_key=api_key,
                openai_api_base=base_url,
                default_headers=default_headers,
                **_stream_chunk_timeout_kwargs(ChatOpenAI, extra_params),
                **extra_params,
            )
        except ImportError:
            raise ImportError("langchain-openai not installed. Run: pip install langchain-openai")

    @staticmethod
    def _create_ollama_llm(config: LLMConfig):
        """Create Ollama (local) LLM instance."""
        try:
            from langchain_community.llms import Ollama
            
            return Ollama(
                model=config.model,
                temperature=config.temperature,
                base_url=config.base_url or "http://localhost:11434",
                **config.additional_params
            )
        except ImportError:
            raise ImportError("langchain-community not installed. Run: pip install langchain-community")


class LLMInvoker:
    """
    Helper class for invoking LLMs with common patterns.
    Provides a simple interface for making LLM calls.
    """
    
    @staticmethod
    def invoke(
        prompt: str,
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        system_prompt: Optional[str] = None
    ) -> str:
        """
        Invoke LLM with a prompt.
        
        Args:
            prompt: User prompt
            tool_name: Name of calling tool (for config lookup)
            client_id: Client ID (for config lookup)
            state: State dict
            system_prompt: Optional system prompt
        
        Returns:
            LLM response as string
        """
        llm = LLMFactory.get_llm(tool_name, client_id, state)
        
        if system_prompt:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        else:
            messages = [{"role": "user", "content": prompt}]
        
        try:
            response = llm.invoke(messages)
            return response.content if hasattr(response, 'content') else str(response)
        except Exception as e:
            log_with_trace_id(state, f"LLM invocation error: {e}", "error")
            raise
    
    @staticmethod
    async def ainvoke(
        prompt: str,
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
        system_prompt: Optional[str] = None,
        config: Optional[Dict] = None,
    ) -> str:
        """Async version of invoke."""
        llm = await LLMFactory.aget_llm(tool_name, client_id, state)
        
        if system_prompt:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ]
        else:
            messages = [{"role": "user", "content": prompt}]
        
        try:
            kwargs = {}
            if config is not None:
                kwargs["config"] = config
            response = await llm.ainvoke(messages, **kwargs)
            return response.content if hasattr(response, 'content') else str(response)
        except Exception as e:
            log_with_trace_id(state, f"LLM invocation error: {e}", "error")
            raise
