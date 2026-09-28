"""
LLM Provider Configuration
Defines which LLM provider to use for different tools/operations.
"""
import logging
from typing import Dict, Any, Optional
from dataclasses import dataclass
from enum import Enum
from fashion_bot.env_loader import bootstrap_environment, get_env

logger = logging.getLogger(__name__)
bootstrap_environment()

class LLMProvider(Enum):
    """Supported LLM providers"""
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GROQ = "groq"
    TOGETHER = "together"
    GEMINI = "gemini"
    AZURE_OPENAI = "azure_openai"
    OPENROUTER = "openrouter"  # OpenRouter (multi-model gateway)
    CEREBRAS = "cerebras"  # Cerebras inference (OpenAI-compatible endpoint)
    OLLAMA = "ollama"  # Local models


# Cerebras exposes an OpenAI-compatible API, so it is wired through ChatOpenAI
# with this base URL (same pattern as OpenRouter) — no extra SDK dependency.
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"

# Groq also exposes an OpenAI-compatible API, wired through ChatOpenAI with this
# base URL — no extra langchain-groq dependency.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"


def _api_key_env_for_provider(provider: LLMProvider) -> Optional[str]:
    """Default API key env var by provider."""
    provider_to_key = {
        LLMProvider.OPENAI: "OPENAI_API_KEY",
        LLMProvider.ANTHROPIC: "ANTHROPIC_API_KEY",
        LLMProvider.GROQ: "GROQ_API_KEY",
        LLMProvider.TOGETHER: "TOGETHER_API_KEY",
        LLMProvider.GEMINI: "GOOGLE_API_KEY",
        LLMProvider.AZURE_OPENAI: "AZURE_OPENAI_API_KEY",
        LLMProvider.OPENROUTER: "OPENROUTER_API_KEY",
        LLMProvider.CEREBRAS: "CEREBRAS_API_KEY",
        LLMProvider.OLLAMA: None,
    }
    return provider_to_key.get(provider)


# ─── OpenRouter key isolation ────────────────────────────────────────────────
# Chat workloads (graph_context_meta agents/tools) and background/batch
# workloads use SEPARATE OpenRouter keys. Chat is the highest-priority workload
# and must never 429, so heavy background jobs (product sync, onboarding,
# conversation analytics, etc.) are routed through a second key and can never
# exhaust the chat key's rate limit.
#   CHAT       → OPENROUTER_API_KEY
#   BACKGROUND → OPENROUTER_API_KEY_2
CHAT_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
BACKGROUND_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY_2"

@dataclass
class LLMConfig:
    """Configuration for a specific LLM provider"""
    provider: LLMProvider
    model: str
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    api_key_env_var: Optional[str] = None  # Environment variable name for API key
    base_url: Optional[str] = None  # For custom endpoints (e.g., Ollama, Azure)
    additional_params: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.additional_params is None:
            self.additional_params = {}


# ============================================
# DEFAULT LLM CONFIGURATIONS
# ============================================

# Read LLM configuration from environment variables
# LLM_PROVIDER: "openai" or "google" (default: openai)
# LLM_MODEL: Model name (default depends on provider)
_llm_provider = (get_env("LLM_PROVIDER", "openai") or "openai").lower()

# Default models per provider
_DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "google": "gemini-3.1-flash-lite-preview",
    # Served only by Google AI Studio, ~1% of whose requests hit a geo-blocked
    # region. Handled by the fallback chain in llm_factory.DEFAULT_MODEL_FALLBACKS
    # (falls to gemini-2.5-flash-lite on error only), not by downgrading here.
    "openrouter": "google/gemini-3.1-flash-lite-preview",
    "cerebras": "gpt-oss-120b",
    "groq": "openai/gpt-oss-20b",
}
_llm_model = get_env("LLM_MODEL", _DEFAULT_MODELS.get(_llm_provider, "gpt-4o"))

print(f"🤖 LLM Provider from env: LLM_PROVIDER={_llm_provider}")
print(f"🤖 LLM Model from env: LLM_MODEL={_llm_model}")

# Smaller / cheaper model for utility tasks (tagging, attribute extraction, etc.)
_smaller_llm_provider_str = (get_env("SMALLER_LLM_PROVIDER", "openai") or "openai").lower()
_smaller_llm_model = get_env("SMALLER_LLM_MODEL", "gpt-4o-mini")

_PROVIDER_STR_TO_ENUM = {
    "openai": LLMProvider.OPENAI,
    "google": LLMProvider.GEMINI,
    "gemini": LLMProvider.GEMINI,
    "anthropic": LLMProvider.ANTHROPIC,
    "openrouter": LLMProvider.OPENROUTER,
    "cerebras": LLMProvider.CEREBRAS,
    "groq": LLMProvider.GROQ,
}
_smaller_llm_provider_enum = _PROVIDER_STR_TO_ENUM.get(_smaller_llm_provider_str, LLMProvider.OPENAI)

print(f"🤖 Smaller LLM: SMALLER_LLM_PROVIDER={_smaller_llm_provider_str}, SMALLER_LLM_MODEL={_smaller_llm_model}")


def get_smaller_llm_config(
    temperature: float = 0,
    max_tokens: int = 500,
    api_key_env_var: Optional[str] = None,
) -> "LLMConfig":
    """
    Build an LLMConfig for the smaller/utility model (tag classification,
    attribute extraction, recommendation copy, etc.).

    Reads SMALLER_LLM_PROVIDER (default openai) and SMALLER_LLM_MODEL
    (default gpt-4o-mini) from the environment.

    Args:
        api_key_env_var: Override the API-key env var for this call. Background/
            batch callers (product sync, async tagging, conversion-tag cron)
            pass ``BACKGROUND_OPENROUTER_KEY_ENV`` so their load stays off the
            chat key. Chat-path callers (e.g. recommendation response copy)
            leave this ``None`` to use the provider default, which under
            ``SMALLER_LLM_PROVIDER=openrouter`` is the chat ``OPENROUTER_API_KEY``.
    """
    return LLMConfig(
        provider=_smaller_llm_provider_enum,
        model=_smaller_llm_model,
        temperature=temperature,
        max_tokens=max_tokens,
        api_key_env_var=api_key_env_var or _api_key_env_for_provider(_smaller_llm_provider_enum),
    )


def get_background_llm_config(
    temperature: float = 0.7,
    max_tokens: Optional[int] = 4000,
) -> "LLMConfig":
    """
    Full-size LLMConfig for background/batch work (conversation analytics,
    onboarding, sync prompt generation), pinned to the BACKGROUND OpenRouter
    key (``OPENROUTER_API_KEY_2``) so it never competes with the chat key.

    Model defaults to the chat model (``LLM_MODEL``); override with
    ``BACKGROUND_LLM_MODEL``.
    """
    return LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model=get_env("BACKGROUND_LLM_MODEL", _llm_model),
        temperature=temperature,
        max_tokens=max_tokens,
        api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
        base_url="https://openrouter.ai/api/v1",
    )


# Optional generic-skill env override.
# Example:
#   GENERIC_SKILL_LLM_PROVIDER=openai
#   GENERIC_SKILL_LLM_MODEL=gpt-5-mini
_generic_skill_model_raw = (get_env("GENERIC_SKILL_LLM_MODEL", "") or "").strip()
_GENERIC_SKILL_MODEL_ALIASES = {
    "5.1-mini": "gpt-5-mini",
    "gpt-5.1-mini": "gpt-5-mini",
    "gpt5.1-mini": "gpt-5-mini",
}
_generic_skill_model = _GENERIC_SKILL_MODEL_ALIASES.get(
    _generic_skill_model_raw.lower(), _generic_skill_model_raw
)
_generic_skill_provider_raw = (get_env("GENERIC_SKILL_LLM_PROVIDER", "openai") or "openai").strip().lower()

OPENAI_GPT4O_CONFIG = LLMConfig(
    provider=LLMProvider.OPENAI,
    model=_llm_model if _llm_provider == "openai" else "gpt-4o",
    temperature=0.7,
    max_tokens=None,  # Use model's default (128k context, no hard limit needed)
    api_key_env_var="OPENAI_API_KEY"
)

OPENAI_GPT41_MINI_CONFIG = LLMConfig(
    provider=LLMProvider.OPENAI,
    model="gpt-4.1-mini",
    temperature=0.2,
    max_tokens=2000,
    api_key_env_var="OPENAI_API_KEY",
)

OPENAI_GPT4_CONFIG = LLMConfig(
    provider=LLMProvider.OPENAI,
    model="gpt-4",
    temperature=0.7,
    max_tokens=None,  # Let OpenAI handle defaults
    api_key_env_var="OPENAI_API_KEY"
)

OPENAI_GPT35_CONFIG = LLMConfig(
    provider=LLMProvider.OPENAI,
    model="gpt-3.5-turbo",
    temperature=0.7,
    max_tokens=None,  # Let OpenAI handle defaults
    api_key_env_var="OPENAI_API_KEY"
)

# Anthropic Claude
ANTHROPIC_CLAUDE_CONFIG = LLMConfig(
    provider=LLMProvider.ANTHROPIC,
    model="claude-3-5-sonnet-20241022",
    temperature=0.7,
    max_tokens=4000,
    api_key_env_var="ANTHROPIC_API_KEY"
)

# Groq (Fast inference, OpenAI-compatible). Driven by LLM_MODEL when
# LLM_PROVIDER=groq; defaults to openai/gpt-oss-20b.
GROQ_LLAMA_CONFIG = LLMConfig(
    provider=LLMProvider.GROQ,
    model=_llm_model if _llm_provider == "groq" else "openai/gpt-oss-20b",
    temperature=0.7,
    max_tokens=2000,
    api_key_env_var="GROQ_API_KEY",
    base_url=GROQ_BASE_URL,
)

# Together AI
TOGETHER_LLAMA_CONFIG = LLMConfig(
    provider=LLMProvider.TOGETHER,
    model="meta-llama/Llama-3-70b-chat-hf",
    temperature=0.7,
    max_tokens=2000,
    api_key_env_var="TOGETHER_API_KEY"
)

# Google Gemini
GEMINI_PRO_CONFIG = LLMConfig(
    provider=LLMProvider.GEMINI,
    model="gemini-1.5-pro",
    temperature=0.7,
    max_tokens=2000,
    api_key_env_var="GOOGLE_API_KEY"
)

# Google Gemini 2.5 Flash Lite (Fast, cost-effective)
# Uses model from LLM_MODEL env var when LLM_PROVIDER=google
GEMINI_FLASH_LITE_CONFIG = LLMConfig(
    provider=LLMProvider.GEMINI,
    model=_llm_model if _llm_provider == "google" else "gemini-2.5-flash-lite",
    temperature=0.2,
    max_tokens=2000,
    api_key_env_var="GOOGLE_API_KEY"
)

# Google Gemini 2.5 Flash (More capable, used as fallback for flash-lite)
GEMINI_FLASH_CONFIG = LLMConfig(
    provider=LLMProvider.GEMINI,
    model="gemini-2.5-flash",
    temperature=0.2,
    max_tokens=2000,
    api_key_env_var="GOOGLE_API_KEY"
)

# Google Gemini 2.5 Flash with Thinking Budget (for complex reasoning tasks)
# Note: thinking_budget specifies max tokens for thinking (e.g., 1024)
GEMINI_FLASH_CONFIG_WITH_BUDGET = LLMConfig(
    provider=LLMProvider.GEMINI,
    model="gemini-2.5-flash",
    temperature=0.2,
    max_tokens=2000,
    api_key_env_var="GOOGLE_API_KEY",
    additional_params={
        "thinking_budget": 128
    }
)

# OpenRouter (Multi-model gateway - access any model via openrouter.ai)
# Set OPENROUTER_API_KEY env var. Model format: "provider/model" e.g. "anthropic/claude-3.5-sonnet"
OPENROUTER_DEFAULT_CONFIG = LLMConfig(
    provider=LLMProvider.OPENROUTER,
    model="anthropic/claude-sonnet-4",
    temperature=0.7,
    max_tokens=4000,
    api_key_env_var="OPENROUTER_API_KEY",
    base_url="https://openrouter.ai/api/v1",
)

# Cerebras (OpenAI-compatible inference, e.g. gpt-oss-120b)
# Set CEREBRAS_API_KEY. Model defaults to gpt-oss-120b; driven by LLM_MODEL
# when LLM_PROVIDER=cerebras.
CEREBRAS_DEFAULT_CONFIG = LLMConfig(
    provider=LLMProvider.CEREBRAS,
    model=_llm_model if _llm_provider == "cerebras" else "gpt-oss-120b",
    temperature=0.7,
    max_tokens=None,  # Use model default
    api_key_env_var="CEREBRAS_API_KEY",
    base_url=CEREBRAS_BASE_URL,
)

# Ollama (Local)
OLLAMA_LLAMA_CONFIG = LLMConfig(
    provider=LLMProvider.OLLAMA,
    model="llama3.1:70b",
    temperature=0.7,
    base_url="http://localhost:11434"
)


# ============================================
# TOOL-SPECIFIC LLM CONFIGURATIONS
# ============================================

# Determine the config to use based on LLM_PROVIDER and LLM_MODEL env vars
# This allows switching all tools between providers via environment
if _llm_provider == "openai":
    _PROVIDER_CONFIG = OPENAI_GPT4O_CONFIG
elif _llm_provider == "openrouter":
    _PROVIDER_CONFIG = LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model=_llm_model,
        temperature=0.7,
        max_tokens=4000,
        api_key_env_var="OPENROUTER_API_KEY",
        base_url="https://openrouter.ai/api/v1",
    )
    print(f"🤖 Using OpenRouter as provider: {_llm_model}")
elif _llm_provider == "cerebras":
    _PROVIDER_CONFIG = LLMConfig(
        provider=LLMProvider.CEREBRAS,
        model=_llm_model,
        temperature=0.7,
        max_tokens=None,
        api_key_env_var="CEREBRAS_API_KEY",
        base_url=CEREBRAS_BASE_URL,
    )
    print(f"🤖 Using Cerebras as provider: {_llm_model}")
elif _llm_provider == "groq":
    _PROVIDER_CONFIG = LLMConfig(
        provider=LLMProvider.GROQ,
        model=_llm_model,
        temperature=0.7,
        max_tokens=None,
        api_key_env_var="GROQ_API_KEY",
        base_url=GROQ_BASE_URL,
    )
    print(f"🤖 Using Groq as provider: {_llm_model}")
elif _llm_model == "gemini-2.5-flash":
    # When explicitly using gemini-2.5-flash, use the config with thinking budget
    _PROVIDER_CONFIG = GEMINI_FLASH_CONFIG_WITH_BUDGET
    print(f"🤖 Using gemini-2.5-flash with thinking budget (100 tokens)")
else:
    # Default to flash-lite config (which uses _llm_model from env)
    _PROVIDER_CONFIG = GEMINI_FLASH_LITE_CONFIG

# Fixed config for final_answer — a lightweight formatting task with low
# latency and stable quality needs. Part of graph_context_meta, so it is routed
# through the CHAT OpenRouter key (OPENROUTER_API_KEY) to keep the whole
# conversation path on one key. Model is pinned (independent of LLM_MODEL) for
# stable output; override with FINAL_ANSWER_LLM_MODEL.
FINAL_ANSWER_CONFIG = LLMConfig(
    provider=LLMProvider.OPENROUTER,
    model=get_env("FINAL_ANSWER_LLM_MODEL", "openai/gpt-4.1-mini"),
    temperature=0.2,
    max_tokens=2000,
    api_key_env_var=CHAT_OPENROUTER_KEY_ENV,
    base_url="https://openrouter.ai/api/v1",
)

# Background/batch config used by post-hoc analytics that resolve through
# LLMConfigManager (e.g. conversation_analytics). Pinned to OPENROUTER_API_KEY_2.
BACKGROUND_OPENROUTER_CONFIG = get_background_llm_config(temperature=0.7, max_tokens=4000)

# Map tool names to LLM configs
# If a tool is not specified here, it uses the DEFAULT_LLM_CONFIG
# When LLM_PROVIDER=openai, these all use OpenAI; otherwise Gemini
TOOL_LLM_CONFIGS: Dict[str, LLMConfig] = {
    # ==================== AGENT/NODE CONFIGURATIONS ====================
    # order_status agent → Uses provider from env
    "order_status": _PROVIDER_CONFIG,
    
    # final_answer agent → lightweight task on the CHAT OpenRouter key
    "final_answer": FINAL_ANSWER_CONFIG,

    # conversation_analytics (post-hoc batch analyzer) → BACKGROUND key.
    # Listed here (Priority 3) so it beats the per-client default (Priority 4),
    # keeping the analytics batch off the chat key for every client.
    "conversation_analytics": BACKGROUND_OPENROUTER_CONFIG,

    # product_content_summarizer (product ingestion batch) → BACKGROUND key.
    # Runs during product_delta_sync with concurrent LLM calls; must not
    # compete with the chat key's rate limit.
    "product_content_summarizer": BACKGROUND_OPENROUTER_CONFIG,
    
    # product_details agent → Uses provider from env
    "product_details": _PROVIDER_CONFIG,
    
    # All other agents → Use DEFAULT_LLM_CONFIG
    # intent_detection, place_order, etc.
    
    # ==================== TOOL-SPECIFIC CONFIGURATIONS ====================
    # Complex reasoning tasks
    "get_order_status": _PROVIDER_CONFIG,
    "get_customer_orders_by_phone": _PROVIDER_CONFIG,
    "get_order_status_summary": _PROVIDER_CONFIG,
    
    # Product search
    "search_products": _PROVIDER_CONFIG,
    
    # Simple tasks
    "detect_frustration": _PROVIDER_CONFIG,
    "get_discount_information_tool": _PROVIDER_CONFIG,
    "get_sales_policy_tool": _PROVIDER_CONFIG,
    
    # Product-related
    "get_product_details_from_url_graphql": _PROVIDER_CONFIG,
    "get_product_availability": _PROVIDER_CONFIG,
    
    # Cancellation
    "cancel_order_shopify": _PROVIDER_CONFIG,
    "cancel_order_shiprocket": _PROVIDER_CONFIG,
}

# Default LLM config (used when tool not specified in TOOL_LLM_CONFIGS)
# Dynamically selects based on LLM_PROVIDER and LLM_MODEL environment variables
if _llm_provider == "openai":
    DEFAULT_LLM_CONFIG = OPENAI_GPT4O_CONFIG
    print(f"🤖 Using OpenAI as default LLM provider: {_llm_model}")
elif _llm_provider == "openrouter":
    DEFAULT_LLM_CONFIG = _PROVIDER_CONFIG
    print(f"🤖 Using OpenRouter as default LLM provider: {_llm_model}")
elif _llm_provider == "cerebras":
    DEFAULT_LLM_CONFIG = _PROVIDER_CONFIG
    print(f"🤖 Using Cerebras as default LLM provider: {_llm_model}")
elif _llm_provider == "groq":
    DEFAULT_LLM_CONFIG = _PROVIDER_CONFIG
    print(f"🤖 Using Groq as default LLM provider: {_llm_model}")
elif _llm_model == "gemini-2.5-flash":
    DEFAULT_LLM_CONFIG = GEMINI_FLASH_CONFIG_WITH_BUDGET
    print(f"🤖 Using Google as default LLM provider: {_llm_model} (with thinking budget)")
else:
    DEFAULT_LLM_CONFIG = GEMINI_FLASH_LITE_CONFIG
    print(f"🤖 Using Google as default LLM provider: {_llm_model}")

# Generic-skill agents (routed through generic_skill_node factory)
GENERIC_SKILL_AGENT_NAMES = {
    "product_details",
    "order_status",
    "place_order",
    "cancel_or_update_order",
    "delivery_timeline",
    "return_exchange",
    "discount",
    "recommendations",
    "delivery_policy",
    "payment_policy",
    "return_exchange_policy",
    "vendor_inquiry",
    "feedback",
    "escalation",
    "unknown",
}

_GENERIC_SKILL_PROVIDER_MAP = {
    "openai": LLMProvider.OPENAI,
    "anthropic": LLMProvider.ANTHROPIC,
    "groq": LLMProvider.GROQ,
    "together": LLMProvider.TOGETHER,
    "google": LLMProvider.GEMINI,
    "gemini": LLMProvider.GEMINI,
    "azure_openai": LLMProvider.AZURE_OPENAI,
    "openrouter": LLMProvider.OPENROUTER,
    "cerebras": LLMProvider.CEREBRAS,
    "ollama": LLMProvider.OLLAMA,
}

GENERIC_SKILL_ENV_DEFAULT_CONFIG: Optional[LLMConfig] = None
if _generic_skill_model:
    _provider_enum = _GENERIC_SKILL_PROVIDER_MAP.get(_generic_skill_provider_raw)
    if _provider_enum is None:
        logger.warning(
            "⚠️ Invalid GENERIC_SKILL_LLM_PROVIDER='%s'. Ignoring generic-skill override.",
            _generic_skill_provider_raw,
        )
    else:
        GENERIC_SKILL_ENV_DEFAULT_CONFIG = LLMConfig(
            provider=_provider_enum,
            model=_generic_skill_model,
            temperature=0.7,
            max_tokens=None,
            api_key_env_var=_api_key_env_for_provider(_provider_enum),
        )
        logger.info(
            "🤖 Generic skill env override enabled: %s/%s",
            _provider_enum.value,
            _generic_skill_model,
        )


# ============================================
# CLIENT-SPECIFIC LLM CONFIGURATIONS
# ============================================

# Default LLM for each client (used when tool not in TOOL_LLM_CONFIGS)
# If client_id not listed here, uses DEFAULT_LLM_CONFIG
CLIENT_LLM_CONFIGS: Dict[str, LLMConfig] = {
    # Follow the global chat default so chat agents NOT listed in
    # TOOL_LLM_CONFIGS (intent_detection, place_order, etc.) stay on the CHAT
    # OpenRouter key rather than falling back to a separate Gemini/Google key.
    "default": DEFAULT_LLM_CONFIG,
    "groovee": DEFAULT_LLM_CONFIG,
    "premium_client": OPENAI_GPT4O_CONFIG,    # Premium client uses GPT-4o
}

# Client-specific tool overrides
# Format: { "client_id": { "tool_name": LLMConfig } }
# This allows different clients to use different LLMs for the same tool
CLIENT_TOOL_LLM_CONFIGS: Dict[str, Dict[str, LLMConfig]] = {
    # Example: Premium client uses GPT-4o for everything including product search
    "premium_client": {
        "search_products": OPENAI_GPT4O_CONFIG,
        "product_details": OPENAI_GPT4O_CONFIG,
    },
    # Example: Budget client uses Gemini for everything including order_status
    "budget_client": {
        "order_status": GEMINI_FLASH_LITE_CONFIG,
        "get_order_status": GEMINI_FLASH_LITE_CONFIG,
    },
}


class LLMConfigManager:
    """
    Central manager for LLM configurations.
    Determines which LLM to use based on priority:
    0. Database config (client_agent_llm_config table, cached in Redis) ← HIGHEST
    1. Generic-skill env default (GENERIC_SKILL_LLM_*)
    2. Client-specific tool override (CLIENT_TOOL_LLM_CONFIGS)
    3. Global tool-specific config (TOOL_LLM_CONFIGS)
    4. Client-specific default (CLIENT_LLM_CONFIGS)
    5. Global default (DEFAULT_LLM_CONFIG)
    """
    
    @staticmethod
    def get_llm_config(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None
    ) -> LLMConfig:
        """
        Get LLM configuration based on priority:
        0. Database config (client_agent_llm_config table, cached in Redis) ← HIGHEST
        1. Generic-skill env default (if enabled and tool_name is a generic skill agent)
        2. Client-specific tool override (if client has custom config for this tool)
        3. Global tool-specific config (if tool_name in TOOL_LLM_CONFIGS)
        4. Client-specific default (if client_id in CLIENT_LLM_CONFIGS)
        5. Global default (DEFAULT_LLM_CONFIG)
        """
        # Extract client_id from state if not provided
        if not client_id and state:
            client_id = state.get('client_id')
        
        # Priority 0: Database config (highest priority — per-client per-agent from DB)
        if client_id and tool_name:
            db_config = load_llm_config_from_database(client_id, tool_name)
            if db_config:
                return db_config

        # Priority 1: Generic-skill env default (optional)
        if (
            tool_name
            and tool_name in GENERIC_SKILL_AGENT_NAMES
            and GENERIC_SKILL_ENV_DEFAULT_CONFIG is not None
        ):
            return GENERIC_SKILL_ENV_DEFAULT_CONFIG
        
        # Priority 2: Client-specific tool override (hardcoded)
        if client_id and tool_name and client_id in CLIENT_TOOL_LLM_CONFIGS:
            client_tool_configs = CLIENT_TOOL_LLM_CONFIGS[client_id]
            if tool_name in client_tool_configs:
                return client_tool_configs[tool_name]
        
        # Priority 3: Global tool-specific configuration
        if tool_name and tool_name in TOOL_LLM_CONFIGS:
            return TOOL_LLM_CONFIGS[tool_name]
        
        # Priority 4: Client-specific default
        if client_id and client_id in CLIENT_LLM_CONFIGS:
            return CLIENT_LLM_CONFIGS[client_id]
        
        # Priority 5: Global default configuration
        return DEFAULT_LLM_CONFIG
    
    @staticmethod
    async def aget_llm_config(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None
    ) -> LLMConfig:
        """
        Async version of get_llm_config.
        Get LLM configuration based on priority:
        0. Database config (client_agent_llm_config table, cached in Redis) <- HIGHEST
        1. Generic-skill env default (if enabled and tool_name is a generic skill agent)
        2. Client-specific tool override (if client has custom config for this tool)
        3. Global tool-specific config (if tool_name in TOOL_LLM_CONFIGS)
        4. Client-specific default (if client_id in CLIENT_LLM_CONFIGS)
        5. Global default (DEFAULT_LLM_CONFIG)
        """
        # Extract client_id from state if not provided
        if not client_id and state:
            client_id = state.get('client_id')

        # Priority 0: Database config (highest priority - per-client per-agent from DB)
        if client_id and tool_name:
            db_config = await aload_llm_config_from_database(client_id, tool_name)
            if db_config:
                return db_config

        # Priority 1: Generic-skill env default (optional)
        if (
            tool_name
            and tool_name in GENERIC_SKILL_AGENT_NAMES
            and GENERIC_SKILL_ENV_DEFAULT_CONFIG is not None
        ):
            return GENERIC_SKILL_ENV_DEFAULT_CONFIG

        # Priority 2: Client-specific tool override (hardcoded)
        if client_id and tool_name and client_id in CLIENT_TOOL_LLM_CONFIGS:
            client_tool_configs = CLIENT_TOOL_LLM_CONFIGS[client_id]
            if tool_name in client_tool_configs:
                return client_tool_configs[tool_name]

        # Priority 3: Global tool-specific configuration
        if tool_name and tool_name in TOOL_LLM_CONFIGS:
            return TOOL_LLM_CONFIGS[tool_name]

        # Priority 4: Client-specific default
        if client_id and client_id in CLIENT_LLM_CONFIGS:
            return CLIENT_LLM_CONFIGS[client_id]

        # Priority 5: Global default configuration
        return DEFAULT_LLM_CONFIG

    @staticmethod
    def get_provider(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None
    ) -> str:
        """Get LLM provider string."""
        config = LLMConfigManager.get_llm_config(tool_name, client_id, state)
        return config.provider.value
    
    @staticmethod
    def get_model(
        tool_name: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None
    ) -> str:
        """Get LLM model string."""
        config = LLMConfigManager.get_llm_config(tool_name, client_id, state)
        return config.model
    
    @staticmethod
    def should_use_tool_specific_llm(tool_name: str) -> bool:
        """Check if a tool has a specific LLM configuration."""
        return tool_name in TOOL_LLM_CONFIGS


# ============================================
# HELPER FUNCTIONS
# ============================================

def register_tool_llm_config(tool_name: str, config: LLMConfig):
    """Register or update LLM config for a specific tool."""
    TOOL_LLM_CONFIGS[tool_name] = config

def register_client_llm_config(client_id: str, config: LLMConfig):
    """Register or update LLM config for a specific client."""
    CLIENT_LLM_CONFIGS[client_id] = config

def load_llm_config_from_database(client_id: str, agent_name: str) -> Optional[LLMConfig]:
    """
    Load LLM configuration from database (client_agent_llm_config table).
    Uses Redis caching via config_manager.get_client_agent_llm_config().
    
    Args:
        client_id: The client UUID string
        agent_name: The agent/tool name (e.g. 'cancellation_handler', 'order_status')
        
    Returns:
        LLMConfig if found in DB, None otherwise (caller falls through to hardcoded defaults)
    """
    try:
        from fashion_bot.config_manager import aget_client_agent_llm_config
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            # Already in an async context - can't call await from sync function
            # Fall through to hardcoded defaults
            logger.debug(
                f"Skipping DB LLM config lookup (sync path in async context) for "
                f"client={client_id}, agent={agent_name}. Use aload_llm_config_from_database instead."
            )
            return None
        else:
            config_dict = asyncio.run(aget_client_agent_llm_config(client_id, agent_name))

        if not config_dict:
            return None
        
        # Convert provider string to LLMProvider enum
        provider_str = config_dict.get("provider", "").lower()
        try:
            provider = LLMProvider(provider_str)
        except ValueError:
            # Handle common aliases
            provider_map = {
                "google": LLMProvider.GEMINI,
                "gpt": LLMProvider.OPENAI,
                "claude": LLMProvider.ANTHROPIC,
                "openrouter": LLMProvider.OPENROUTER,
                "cerebras": LLMProvider.CEREBRAS,
                "groq": LLMProvider.GROQ,
            }
            provider = provider_map.get(provider_str)
            if not provider:
                logger.warning(
                    f"⚠️ Unknown LLM provider '{provider_str}' in DB config for "
                    f"client={client_id}, agent={agent_name}. Skipping DB config."
                )
                return None
        
        llm_config = LLMConfig(
            provider=provider,
            model=config_dict["model"],
            temperature=config_dict.get("temperature", 0.7),
            max_tokens=config_dict.get("max_tokens"),
            api_key_env_var=config_dict.get("api_key_env_var"),
            base_url=config_dict.get("base_url"),
            additional_params=config_dict.get("additional_params", {}),
        )
        
        logger.info(
            f"🤖 Using DB LLM config: client={client_id}, agent={agent_name} "
            f"→ {provider.value}/{llm_config.model}"
        )
        return llm_config
        
    except Exception as e:
        logger.error(f"❌ Error loading LLM config from database: {e}")
        return None


async def aload_llm_config_from_database(client_id: str, agent_name: str) -> Optional[LLMConfig]:
    """
    Async version of load_llm_config_from_database.
    Load LLM configuration from database (client_agent_llm_config table).
    Uses Redis caching via config_manager.aget_client_agent_llm_config().

    Args:
        client_id: The client UUID string
        agent_name: The agent/tool name (e.g. 'cancellation_handler', 'order_status')

    Returns:
        LLMConfig if found in DB, None otherwise (caller falls through to hardcoded defaults)
    """
    try:
        from fashion_bot.config_manager import aget_client_agent_llm_config

        config_dict = await aget_client_agent_llm_config(client_id, agent_name)
        if not config_dict:
            return None

        # Convert provider string to LLMProvider enum
        provider_str = config_dict.get("provider", "").lower()
        try:
            provider = LLMProvider(provider_str)
        except ValueError:
            # Handle common aliases
            provider_map = {
                "google": LLMProvider.GEMINI,
                "gpt": LLMProvider.OPENAI,
                "claude": LLMProvider.ANTHROPIC,
                "openrouter": LLMProvider.OPENROUTER,
                "cerebras": LLMProvider.CEREBRAS,
                "groq": LLMProvider.GROQ,
            }
            provider = provider_map.get(provider_str)
            if not provider:
                logger.warning(
                    f"⚠️ Unknown LLM provider '{provider_str}' in DB config for "
                    f"client={client_id}, agent={agent_name}. Skipping DB config."
                )
                return None

        llm_config = LLMConfig(
            provider=provider,
            model=config_dict["model"],
            temperature=config_dict.get("temperature", 0.7),
            max_tokens=config_dict.get("max_tokens"),
            api_key_env_var=config_dict.get("api_key_env_var"),
            base_url=config_dict.get("base_url"),
            additional_params=config_dict.get("additional_params", {}),
        )

        logger.info(
            f"🤖 Using DB LLM config (async): client={client_id}, agent={agent_name} "
            f"→ {provider.value}/{llm_config.model}"
        )
        return llm_config

    except Exception as e:
        logger.error(f"❌ Error loading LLM config from database (async): {e}")
        return None
