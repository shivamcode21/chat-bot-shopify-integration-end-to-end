"""
Configuration manager for fetching client-specific configurations from PostgreSQL.
"""
import asyncio
import json
import logging
from typing import Optional, Dict, Any
from fashion_bot.database_manager import awith_retry, get_async_postgres_connection, get_postgres_connection, with_retry
from fashion_bot.env_loader import bootstrap_environment, get_bool, get_env, get_int, get_float
from fashion_bot.utils.tiered_cache import aget_with_tiered_cache
from fashion_bot.utils.redis_client import get_shared_async_redis_client
from contextvars import ContextVar

logger = logging.getLogger(__name__)
bootstrap_environment()

REDIS_URL = get_env("REDIS_URL") or get_env("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"

# TTL for gupshup config cache: 3 days = 259200 seconds
GUPSHUP_CONFIG_CACHE_TTL = 259200
SHIPROCKET_CONFIG_CACHE_TTL = 259200
# TTL for generic config cache (memory tier): 10 minutes
CONFIG_MEMORY_TTL = get_int("CONFIG_MEMORY_TTL", 600)
# TTL for generic config cache (Redis tier): 1 hour
CONFIG_REDIS_TTL = get_int("CONFIG_REDIS_TTL", 3600)
# TTL for LLM config cache: 1 hour = 3600 seconds (shorter for faster config changes)
LLM_CONFIG_CACHE_TTL = get_int("LLM_CONFIG_CACHE_TTL", 3600)
# TTL for negative LLM config cache entries (no DB row found)
LLM_CONFIG_NEGATIVE_CACHE_TTL = get_int("LLM_CONFIG_NEGATIVE_CACHE_TTL", 120)
BLOOMERCE_INTEGRATION_REQUIRED_MESSAGE = (
    "We are not able to fetch this data right now. Integration with Bloomerce is required for this client."
)

# Conversation message-limit gate: max user turns per conversation before the
# bot short-circuits with a canned reply. Configurable per client via
# ``client_configs.max_user_messages_per_conversation``; default 30. A value
# <= 0 disables the cap (unlimited).
DEFAULT_MAX_USER_MESSAGES = get_int("DEFAULT_MAX_USER_MESSAGES", 30)
DEFAULT_CONVERSATION_LIMIT_MESSAGE = (
    "You've reached the message limit for this conversation. "
    "Please wait for a few hours to continue this conversation, or reach out to our support team for further help."
)
# Cooldown (in hours) after which the message-limit counter resets, measured
# from when the cap was FIRST reached — NOT from last activity. This lets a
# blocked customer resume after a fixed wait even if they keep messaging (each
# retry, and the bot's own canned reply, would otherwise keep pushing the
# conversation-inactivity boundary out and never let the counter reset).
# Configurable per client via ``client_configs.conversation_limit_reset_hours``.
# A value <= 0 disables the time-based reset (only a brand-new conversation
# after the 90-minute inactivity gap resets the counter).
DEFAULT_CONVERSATION_LIMIT_RESET_HOURS = get_float("DEFAULT_CONVERSATION_LIMIT_RESET_HOURS", 2.0)


# Legacy alias retained so existing call sites keep working. The actual
# client is created by utils.redis_client (one process-wide instance).
_get_async_redis_client = get_shared_async_redis_client


async def aget_gupshup_config_from_cache(client_id: str) -> Optional[Dict[str, Any]]:
    """Async variant of get_gupshup_config_from_cache."""
    try:
        client = await _get_async_redis_client()
        if not client:
            return None

        cache_key = f"gupshup_config:{client_id}"
        cached_data = await client.get(cache_key)

        if cached_data:
            config = json.loads(cached_data)
            logger.info(f"📖 Found gupshup_config in async Redis cache for client: {client_id}")
            return config
        return None

    except Exception as e:
        logger.error(f"❌ Error fetching gupshup_config from async Redis cache: {str(e)}")
        return None


async def acache_gupshup_config(client_id: str, gupshup_config: Dict[str, Any], ttl_seconds: int = GUPSHUP_CONFIG_CACHE_TTL) -> bool:
    """Async variant of cache_gupshup_config."""
    try:
        client = await _get_async_redis_client()
        if not client:
            return False

        cache_key = f"gupshup_config:{client_id}"
        cached_data = json.dumps(gupshup_config)
        await client.setex(cache_key, ttl_seconds, cached_data)
        logger.info(f"📦 Cached gupshup_config for client {client_id} with {ttl_seconds}s TTL (async)")
        return True

    except Exception as e:
        logger.error(f"❌ Error caching gupshup_config in async Redis: {str(e)}")
        return False


async def _atiered_config_read(tiered_key: str, load_from_db) -> Any:
    """Read a config value through memory -> Redis -> DB.

    Owns the Redis serialisation for the config tier so every config reader
    shares one implementation of it. ``load_from_db`` supplies only the
    authoritative lookup. Misses are cached too (``cache_none``), so an
    unconfigured tenant does not re-query the DB on every turn.
    """
    async def _redis_get():
        client = await _get_async_redis_client()
        if not client:
            return None
        raw = await client.get(tiered_key)
        if raw is not None:
            return json.loads(raw)
        return None

    async def _redis_set(value):
        client = await _get_async_redis_client()
        if client:
            await client.setex(tiered_key, CONFIG_REDIS_TTL, json.dumps(value, default=str))

    value, _source = await aget_with_tiered_cache(
        cache_key=tiered_key,
        ttl_seconds=CONFIG_MEMORY_TTL,
        load_from_source_fn=load_from_db,
        get_from_redis_fn=_redis_get,
        set_to_redis_fn=_redis_set,
        cache_none=True,
    )
    return value


# Lets a caller tell "nothing is configured" apart from "we could not look",
# which aget_config otherwise flattens into the same default return.
#
# It holds a mutable dict rather than a bool on purpose: asyncio.gather runs each
# coroutine in its own copy of the context, so a child calling .set() would never
# be seen by the parent. The children inherit the same dict object, so mutating
# it is visible. Callers that do not opt in leave it None and are unaffected.
config_read_state: ContextVar[Optional[Dict[str, bool]]] = ContextVar("config_read_state", default=None)


def begin_config_read_tracking():
    """Start recording store failures for the current task and its children.

    Returns (state, token); pass the token to config_read_state.reset() when done
    and read state["failed"] for the verdict.
    """
    state = {"failed": False}
    return state, config_read_state.set(state)


async def aget_config(config_key: str, default: Optional[str] = None, client_id: Optional[str] = None) -> Optional[str]:
    """Async config lookup with tiered caching: memory → Redis → DB."""
    effective_client_id = client_id or ""
    tiered_key = f"cfg:{effective_client_id}:{config_key}"

    @awith_retry
    async def _load_from_db():
        if not effective_client_id:
            logger.warning("aget_config called without client_id for key=%s; skipping cross-tenant fallback", config_key)
            return None
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT config_value FROM client_configs WHERE client_id = %s AND config_key = %s",
                    (effective_client_id, config_key),
                )
                result = await cur.fetchone()
                if result:
                    return result.get("config_value")
                return None

    try:
        value = await _atiered_config_read(tiered_key, _load_from_db)
        return value if value is not None else default
    except Exception as e:
        logger.error("aget_config error for key=%s client_id=%s: %s", config_key, effective_client_id, e, exc_info=True)
        state = config_read_state.get()
        if state is not None:
            state["failed"] = True
        return default

async def aget_max_user_messages(client_id: Optional[str] = None) -> int:
    """Per-client cap on user turns per conversation (memory → Redis → DB).

    Reads ``client_configs.max_user_messages_per_conversation`` via the tiered
    config cache. Falls back to ``DEFAULT_MAX_USER_MESSAGES`` (30) when the key
    is unset or non-numeric. A value <= 0 means the cap is disabled (unlimited).
    """
    raw = await aget_config(
        "max_user_messages_per_conversation",
        default=str(DEFAULT_MAX_USER_MESSAGES),
        client_id=client_id,
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid max_user_messages_per_conversation=%r for client_id=%s; using default %s",
            raw,
            client_id,
            DEFAULT_MAX_USER_MESSAGES,
        )
        return DEFAULT_MAX_USER_MESSAGES


async def aget_conversation_limit_message(client_id: Optional[str] = None) -> str:
    """Per-client canned reply sent once the conversation message cap is reached.

    Reads ``client_configs.conversation_limit_message`` via the tiered config
    cache, falling back to ``DEFAULT_CONVERSATION_LIMIT_MESSAGE``. Kept as plain
    text (no Markdown/links) so it renders correctly on every channel.
    """
    msg = await aget_config(
        "conversation_limit_message",
        default=DEFAULT_CONVERSATION_LIMIT_MESSAGE,
        client_id=client_id,
    )
    return str(msg or DEFAULT_CONVERSATION_LIMIT_MESSAGE)


async def aget_conversation_limit_reset_seconds(client_id: Optional[str] = None) -> float:
    """Per-client cooldown (seconds) after which the message-limit counter resets.

    Reads ``client_configs.conversation_limit_reset_hours`` via the tiered config
    cache and converts to seconds. Falls back to
    ``DEFAULT_CONVERSATION_LIMIT_RESET_HOURS`` (2h) when unset or non-numeric. A
    value <= 0 disables the time-based reset (returns 0.0), leaving only the
    new-conversation (90-min inactivity) reset path.

    The window is measured from when the cap was FIRST reached, so repeated
    attempts during the cooldown do not extend it.
    """
    raw = await aget_config(
        "conversation_limit_reset_hours",
        default=str(DEFAULT_CONVERSATION_LIMIT_RESET_HOURS),
        client_id=client_id,
    )
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid conversation_limit_reset_hours=%r for client_id=%s; using default %s",
            raw,
            client_id,
            DEFAULT_CONVERSATION_LIMIT_RESET_HOURS,
        )
        hours = DEFAULT_CONVERSATION_LIMIT_RESET_HOURS
    return max(0.0, hours * 3600.0)


async def aget_shopify_config(client_id: Optional[str] = None) -> Dict[str, str]:
    """Async variant of get_shopify_config."""
    shopify_details = await aget_config("shopify_details", client_id=client_id)
    if not shopify_details:
        return {}
    try:
        config = json.loads(shopify_details) if isinstance(shopify_details, str) else shopify_details
        if not isinstance(config, dict):
            return {}
        # Handle both old and new key formats (matches sync get_shopify_config)
        return {
            "access_token": config.get("access_token") or config.get("SHOPIFY_TOKEN", ""),
            "shop_url": config.get("shop_url") or config.get("SHOPIFY_DOMAIN", ""),
            "api_version": config.get("api_version", "2024-04"),
        }
    except (json.JSONDecodeError, Exception) as e:
        logger.error("aget_shopify_config error: %s", e)
        return {}

async def aget_shiprocket_config(client_id: Optional[str] = None) -> Dict[str, str]:
    """Async variant of get_shiprocket_config."""
    shiprocket_details = await aget_config("shiprocket_details", client_id=client_id)
    if not shiprocket_details:
        return {}
    try:
        data = json.loads(shiprocket_details) if isinstance(shiprocket_details, str) else shiprocket_details
        if not isinstance(data, dict):
            return {}
        # Handle both old and new key formats (matches sync get_shiprocket_config)
        return {
            "email": data.get("email") or data.get("SHIPROCKET_EMAIL", ""),
            "password": data.get("password") or data.get("SHIPROCKET_PASSWORD", ""),
            "api_base": data.get("api_base") or data.get("SHIPROCKET_API_BASE", "https://apiv2.shiprocket.in/v1/external"),
        }
    except (json.JSONDecodeError, Exception) as e:
        logger.error("aget_shiprocket_config error: %s", e)
        return {}


async def aget_delhivery_config(client_id: Optional[str] = None) -> Dict[str, str]:
    """
    Load Delhivery API credentials from `client_configs.delhivery_details`.

    Expected JSON shape (uppercase keys also tolerated for parity with the
    Shiprocket loader):

        {
          "api_token":       "<DELHIVERY_TOKEN>",       // required
          "client_name":     "<warehouse_name>",        // required for create
          "pickup_location": "Primary",                  // optional
          "api_base":        "https://track.delhivery.com"
        }

    Returns an empty dict if not configured (callers should fail-open).
    """
    delhivery_details = await aget_config("delhivery_details", client_id=client_id)
    if not delhivery_details:
        return {}
    try:
        data = json.loads(delhivery_details) if isinstance(delhivery_details, str) else delhivery_details
        if not isinstance(data, dict):
            return {}
        # ``order_id_prefix`` controls how channel order ids are formatted
        # when calling Delhivery's tracking API (``?ref_ids=``). Default is
        # ``"#"`` which matches Shopify's default ``name`` convention (e.g.
        # ``#gv15361``) — what Delhivery's Shopify connector stores for
        # most tenants. Set to ``""`` for tenants whose Delhivery
        # integration uses ``order_number`` (no prefix) or to a custom
        # value (e.g. ``"BLOOM-"``) for tenants with a non-standard
        # Shopify ``order_prefix``. Diagnose per tenant during onboarding
        # via the curl matrix in the Delhivery integration runbook.
        order_id_prefix = data.get("order_id_prefix")
        if order_id_prefix is None:
            order_id_prefix = "#"
        return {
            "api_token": data.get("api_token") or data.get("DELHIVERY_API_TOKEN", ""),
            "client_name": data.get("client_name") or data.get("DELHIVERY_CLIENT_NAME", ""),
            "pickup_location": (
                data.get("pickup_location")
                or data.get("DELHIVERY_PICKUP_LOCATION")
                or "Primary"
            ),
            "api_base": (
                data.get("api_base")
                or data.get("DELHIVERY_API_BASE")
                or "https://track.delhivery.com"
            ),
            "order_id_prefix": str(order_id_prefix),
        }
    except (json.JSONDecodeError, Exception) as e:
        logger.error("aget_delhivery_config error: %s", e)
        return {}


# Config flags reach us from hand-edited JSON, so a flag may arrive as a real
# boolean, a number, or a string spelled any number of ways. These are the
# spellings we recognise; anything else is deliberately NOT guessed at.
_TRUTHY_FLAG_STRINGS = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_FALSY_FLAG_STRINGS = frozenset({"0", "false", "no", "n", "off", "disabled", ""})


def is_config_flag_enabled(value: Any, *, default: bool) -> bool:
    """Interpret a per-tenant on/off config flag.

    Single source of truth for this parse -- previously each flag rolled its
    own, and the "anything not in ('false','0','no','') is on" variants meant
    ``"off"``, ``"disabled"`` and ``"n"`` all silently switched a feature ON.

    ``default`` is what an UNRECOGNISED value falls back to, and it must
    match the flag's own default so an unparseable value can never be more
    permissive than an absent one:

    - An opt-IN flag (default False) stays off unless the value is a
      recognised "on" -- a typo can't enable a feature for a tenant.
    - An opt-OUT flag (default True) stays on unless the value is a
      recognised "off" -- a typo can't disable something a tenant relies on.

    Numbers follow C-style truthiness (``1`` on, ``0`` off) since a
    hand-edited ``"display_rating": 1`` plainly means on; ``None`` is
    treated as absent and returns ``default``.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUTHY_FLAG_STRINGS:
            return True
        if normalized in _FALSY_FLAG_STRINGS:
            return False
        logger.warning(
            "Unrecognised config flag value %r; falling back to default=%s", value, default
        )
        return default
    return default


async def aget_judgeme_rating_display_enabled(client_id: Optional[str] = None) -> bool:
    """Per-tenant on/off switch for the product-card rating/count display.

    Reads the `display_rating` flag from `client_configs.judgeme_details`.
    This flag is independent of `aget_judgeme_config()` below -- the card's
    rating/count comes via the generic Shopify product-ingestion pipeline
    (business-agnostic, works with any reviews app's metafields), while
    `aget_judgeme_config()` is only for the Judge.me API read integration
    (fetching actual review text). Defaults to True whenever the key is
    absent -- for a tenant that never set it (every tenant configured before
    this switch existed), the display must keep working exactly as it did
    before this switch was added.
    """
    judgeme_details = await aget_config("judgeme_details", client_id=client_id)
    if not judgeme_details:
        return True
    try:
        data = json.loads(judgeme_details) if isinstance(judgeme_details, str) else judgeme_details
        if not isinstance(data, dict):
            return True
        value = data.get("display_rating")
        if value is None:
            value = data.get("DISPLAY_RATING")
        # default=True: an opt-OUT flag, so only a recognised "off" value
        # turns the display off -- an unrecognised one leaves it as it was
        # for every tenant configured before this switch existed.
        return is_config_flag_enabled(value, default=True)
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("aget_judgeme_rating_display_enabled error: %s", e)
        return True


# The orderings aget_judgeme_default_review_sort may return. Kept next to the
# reader so an unrecognised config value is checked against the same list the
# review tool actually understands.
_VALID_REVIEW_SORTS = ("top_rated", "recent")
# Fallback when a tenant has not chosen. "recent" rather than "top_rated"
# because a rating-ordered default is not neutral: on a product with more
# reviews than the page size it puts the highest-rated first and pushes every
# complaint past the end of the slice, so a tenant who never configures
# anything gets a systematically flattering listing. Newest-first has no such
# bias. A tenant who WANTS the flattering order can still ask for it.
_DEFAULT_REVIEW_SORT = "recent"


async def aget_judgeme_default_review_sort(client_id: Optional[str] = None) -> str:
    """The ordering get_product_reviews uses when the customer expressed no
    preference.

    Reads `default_review_sort` from `client_configs.judgeme_details`;
    returns ``_DEFAULT_REVIEW_SORT`` when absent or unrecognised.

    This exists because the alternative -- letting each tenant's PROMPT carry
    the preference -- cannot work reliably: `sort` has to have some value by
    the time it reaches the tool, so a prompt-level preference must actively
    override a hardcoded default on every single call, and any turn where the
    model omits it silently reverts. Resolving it from config instead makes
    the tenant's choice the thing that fills in, so omitting `sort` produces
    the tenant's ordering rather than a global one.

    An explicit customer request ("show me the latest reviews") still wins --
    this only decides what happens when nothing was asked for.
    """
    judgeme_details = await aget_config("judgeme_details", client_id=client_id)
    if not judgeme_details:
        return _DEFAULT_REVIEW_SORT
    try:
        data = json.loads(judgeme_details) if isinstance(judgeme_details, str) else judgeme_details
        if not isinstance(data, dict):
            return _DEFAULT_REVIEW_SORT
        value = data.get("default_review_sort") or data.get("DEFAULT_REVIEW_SORT")
        if value is None:
            return _DEFAULT_REVIEW_SORT
        normalised = str(value).strip().lower()
        if normalised in _VALID_REVIEW_SORTS:
            return normalised
        logger.warning(
            "aget_judgeme_default_review_sort: unrecognised default_review_sort %r for client %s; "
            "using %s", value, client_id, _DEFAULT_REVIEW_SORT,
        )
        return _DEFAULT_REVIEW_SORT
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("aget_judgeme_default_review_sort error: %s", e)
        return _DEFAULT_REVIEW_SORT


async def aget_judgeme_config(client_id: Optional[str] = None) -> Dict[str, str]:
    """
    Load Judge.me API credentials from `client_configs.judgeme_details`.

    Expected JSON shape (uppercase keys also tolerated for parity with the
    Delhivery/Shiprocket/BlueDart loaders):

        {
          "shop_domain":     "<shop>.myshopify.com",
          "api_token":       "<private token>",
          "public_token":    "<public token>",
          "api_base":        "https://judge.me/api/v1",
          "webhook_secret":  "<OAuth app secret Judge.me signs webhooks with>"
        }

    ``webhook_secret`` is distinct from ``api_token`` -- it's the key Judge.me
    uses to sign the ``JUDGEME-HMAC-SHA256`` header on webhook deliveries
    (verified against Judge.me's own docs: the secret comes from the OAuth
    app, not the private API token), not a value we call Judge.me with.

    Returns ``{}`` when config is absent/malformed -- callers (the reviews
    adapter treats a missing/placeholder config as `configuration_missing`
    rather than firing a real HTTP call).
    """
    judgeme_details = await aget_config("judgeme_details", client_id=client_id)
    if not judgeme_details:
        return {}
    try:
        data = json.loads(judgeme_details) if isinstance(judgeme_details, str) else judgeme_details
        if not isinstance(data, dict):
            return {}
        return {
            "shop_domain": _clean_credential(data.get("shop_domain") or data.get("SHOP_DOMAIN", "")),
            "api_token": _clean_credential(data.get("api_token") or data.get("API_TOKEN", "")),
            "public_token": _clean_credential(data.get("public_token") or data.get("PUBLIC_TOKEN", "")),
            "api_base": _clean_credential(
                data.get("api_base") or data.get("API_BASE") or "https://judge.me/api/v1"
            ),
            "webhook_secret": _clean_credential(data.get("webhook_secret") or data.get("WEBHOOK_SECRET", "")),
        }
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("aget_judgeme_config error: %s", e)
        return {}


def _clean_credential(value: Any) -> str:
    """Coerce a stored credential to a trimmed string.

    Values saved through the delivery-integration UI can carry leading or
    trailing whitespace. Untrimmed they are silently fatal: credentials
    travel as query parameters, so a leading space is encoded as %20 and
    the partner rejects the request as a bad key.
    """
    if value is None:
        return ""
    return str(value).strip()


async def aget_partner_integration_config(
    partner_name: str,
    client_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Load a delivery partner's saved credentials from
    `delivery_partner_integrations.data`.

    This is where the delivery-integration UI writes when a tenant connects a
    partner, as opposed to the `client_configs.<partner>_details` rows the
    older loaders read. Reading it directly keeps one source of truth rather
    than requiring the same secret to be copied into two tables. Same tiered
    caching as `aget_config` (memory -> Redis -> DB). Returns an empty dict
    when the tenant has no active integration, so callers fail open.
    """
    effective_client_id = client_id or ""
    normalized_partner = (partner_name or "").strip().lower()
    tiered_key = f"dpi:{effective_client_id}:{normalized_partner}"

    @awith_retry
    async def _load_from_db():
        if not effective_client_id or not normalized_partner:
            # An unresolvable client_id is a tenant misconfiguration, not a
            # normal branch -- report it rather than returning quietly.
            try:
                from fashion_bot.rollbar_config import report_error

                report_error(
                    f"aget_partner_integration_config: unresolved client_id/partner "
                    f"(partner={partner_name!r})",
                    level="error",
                    state=None,
                )
            except Exception:  # pragma: no cover - reporting must never break the read
                logger.error(
                    "aget_partner_integration_config called without client_id/partner (partner=%s)",
                    partner_name,
                )
            return None
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT i.data
                    FROM delivery_partner_integrations i
                    JOIN delivery_partners p ON p.id = i.partner_id
                    WHERE i.client_id = %s
                      AND lower(p.name) = %s
                      AND i.status = 'active'
                      AND i.is_connected = 1
                    ORDER BY i.priority ASC, i.created_at DESC
                    LIMIT 1
                    """,
                    (effective_client_id, normalized_partner),
                )
                result = await cur.fetchone()
                if not result:
                    return None
                return result.get("data") if isinstance(result, dict) else result[0]

    try:
        value = await _atiered_config_read(tiered_key, _load_from_db)
    except Exception as e:
        logger.error(
            "aget_partner_integration_config error for partner=%s client_id=%s: %s",
            partner_name, effective_client_id, e, exc_info=True,
        )
        return {}

    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


async def aget_clickpost_config(client_id: Optional[str] = None) -> Dict[str, str]:
    """
    Load ClickPost API credentials for a tenant.

    Read primarily from the delivery-integration record
    (``aget_partner_integration_config``), falling back to a hand-provisioned
    ``client_configs.clickpost_details`` row for tenants set up before that UI
    existed. A tenant with real credentials here issues live ClickPost calls,
    so this decides whether the adapter talks to production shipments.

    Returns an empty dict when nothing is configured, so callers fail open and
    the adapter reports ``configuration_missing`` instead of firing a request
    it knows will be rejected.

    Values are passed through ``_clean_credential``: entries saved from the UI
    have been seen carrying surrounding whitespace, which ClickPost rejects as
    an invalid key rather than ignoring, so trimming is required and not
    cosmetic.

    Expected JSON shape (uppercase keys also tolerated for parity with the
    other loaders). ClickPost splits its API across separate hosts -- the
    dashboard/cancel API on ``www.clickpost.in``, tracking reads on
    ``api.clickpost.in``, predicted SLA on ``ds.clickpost.in`` -- so the bases
    default independently rather than sharing one ``api_base``:

        {
          "api_base":          "<optional, defaults to https://www.clickpost.in>",
          "tracking_api_base": "<optional, defaults to https://api.clickpost.in>",
          "sla_api_base":      "<optional, defaults to https://ds.clickpost.in>",
          "username":          "<CLICKPOST_USERNAME>",
          "key":               "<CLICKPOST_API_KEY>",
          "cp_id":             "<CLICKPOST_CP_ID>",
          "account_code":      "<CLICKPOST_ACCOUNT_CODE>"
        }

    Only ``username`` and ``key`` are required to track: a shipment's
    ``cp_id`` is recovered from the tracking URL on the Shopify fulfillment.
    ``cp_id`` and ``account_code`` are needed by the cancel endpoint, so a
    tenant configured without them can read status but not cancel.
    """
    data: Any = await aget_partner_integration_config("clickpost", client_id=client_id)

    if not data:
        # Fall back to a hand-provisioned client_configs row so a tenant set up
        # before the delivery-integration UI keeps working.
        clickpost_details = await aget_config("clickpost_details", client_id=client_id)
        if not clickpost_details:
            return {}
        data = clickpost_details

    try:
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            return {}
        return {
            "api_base": _clean_credential(data.get("api_base") or data.get("CLICKPOST_API_BASE", "")),
            "tracking_api_base": _clean_credential(
                data.get("tracking_api_base") or data.get("CLICKPOST_TRACKING_API_BASE", "")
            ),
            "sla_api_base": _clean_credential(
                data.get("sla_api_base") or data.get("CLICKPOST_SLA_API_BASE", "")
            ),
            "username": _clean_credential(data.get("username") or data.get("CLICKPOST_USERNAME", "")),
            # `api_key` is the name the delivery-integration UI saves the key
            # under; `key` is kept for hand-provisioned rows.
            "key": _clean_credential(
                data.get("key") or data.get("api_key") or data.get("CLICKPOST_API_KEY", "")
            ),
            "cp_id": _clean_credential(data.get("cp_id") or data.get("CLICKPOST_CP_ID", "")),
            "account_code": _clean_credential(
                data.get("account_code") or data.get("CLICKPOST_ACCOUNT_CODE", "")
            ),
        }
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("aget_clickpost_config error: %s", e)
        return {}


def _parse_json_config_value(raw) -> Optional[Dict[str, Any]]:
    """Parse a config value that may be a JSON string or already a dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


async def aget_discount_coupons(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Async variant of get_discount_coupons."""
    return _parse_json_config_value(await aget_config("discount_coupons", client_id=client_id)) or {}


async def aget_additional_discounts(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Async variant of get_additional_discounts."""
    return _parse_json_config_value(await aget_config("additional_discount", client_id=client_id)) or {}


async def aget_payment_offers(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Async variant of get_payment_offers."""
    return _parse_json_config_value(await aget_config("payment_offers", client_id=client_id)) or {}


# ─── Per-merchant order-update strategy ───────────────────────────────
# Selects how OrderUpdateOrchestrator handles address/phone/email/name/
# size updates for partners that have limited or no pre-AWB edit APIs.
#
# Stored in client_configs.order_update_strategy as either:
#   • JSON dict  (recommended, per-partner):
#       {"delhivery": "cancel_and_recreate", "xpressbees": "escalate_for_manual_update"}
#   • Plain string (legacy, applies to all limited-API partners):
#       "cancel_and_recreate"  or  "escalate_for_manual_update"

ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE = "cancel_and_recreate"
ORDER_UPDATE_STRATEGY_ESCALATE = "escalate_for_manual_update"
# Explicit "update via partner API, no escalation" strategy — used by partners
# whose edit APIs work for any order state (e.g. Shiprocket). Listing this
# value in the per-partner map makes the partner's behaviour discoverable
# instead of relying on the partner being absent from the dict.
ORDER_UPDATE_STRATEGY_UPDATE_INPLACE = "update_inplace"
# Global fallback used only when a partner has not *declared* its own default
# (see ``PartnerRegistration.default_update_strategy``) and the tenant has no
# explicit config. ``escalate`` is the safe choice for a genuinely unknown
# partner — but registered partners (Shiprocket, Delhivery) override it with a
# type-appropriate default so unconfigured tenants behave correctly.
ORDER_UPDATE_STRATEGY_DEFAULT = ORDER_UPDATE_STRATEGY_ESCALATE

_VALID_STRATEGIES = frozenset({
    ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE,
    ORDER_UPDATE_STRATEGY_ESCALATE,
    ORDER_UPDATE_STRATEGY_UPDATE_INPLACE,
})


def _default_strategy_for_partner(partner_name: Optional[str]) -> str:
    """Resolve the default update strategy for a partner with no explicit config.

    The default is driven by *partner type*, not a hard-coded name: each
    partner declares ``default_update_strategy`` in its ``register_partner(...)``
    call (Shiprocket → ``update_inplace``; Delhivery →
    ``escalate_for_manual_update``). Falls back to the global
    ``ORDER_UPDATE_STRATEGY_DEFAULT`` for unregistered/undeclared partners.

    This fixes the regression where unconfigured Shiprocket-only tenants were
    escalated on every update because the global default was ``escalate``.
    """
    if not partner_name:
        return ORDER_UPDATE_STRATEGY_DEFAULT
    try:
        # Lazy import: logistics_registry's autodiscover imports partner
        # packages which import config_manager, so a top-level import here
        # would be circular.
        from fashion_bot.core.logistics_registry import get_partner
        reg = get_partner(partner_name)
        declared = getattr(reg, "default_update_strategy", None) if reg else None
        if declared and declared in _VALID_STRATEGIES:
            return declared
    except Exception:  # pragma: no cover — never let registry lookup break config reads
        pass
    return ORDER_UPDATE_STRATEGY_DEFAULT


def _coerce_strategy_config(value: Any) -> Optional[Dict[str, Any]]:
    """Normalise the raw ``order_update_strategy`` config value into a partner
    dict or ``None`` (for plain-string / unknown formats).

    The ``config_value`` column is JSONB in PostgreSQL.  Depending on the DB
    driver and the Redis round-trip the value may arrive as:
      • a Python ``dict``  — returned by psycopg2 for a JSONB column, or by
        ``json.loads`` in the Redis cache layer.
      • a JSON ``str``     — e.g. ``'{"delhivery": "cancel_and_recreate"}'``
      • a plain ``str``    — e.g. ``"cancel_and_recreate"`` (legacy format).

    Returns the dict when the value is / can be parsed as one, otherwise None
    so the caller falls through to plain-string handling.
    """
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass
    return None


async def aget_partner_update_strategy(
    partner_name: str,
    client_id: Optional[str] = None,
) -> str:
    """Read the order-update strategy for a specific delivery partner.

    Supports two formats in ``client_configs.order_update_strategy``:

    **Per-partner JSON dict (recommended, stored as JSONB):**
        ``{"delhivery": "cancel_and_recreate", "xpressbees": "escalate_for_manual_update"}``

        Returns the strategy for *partner_name*.  If the partner is absent from
        the dict the function returns that partner's *declared* default
        (``PartnerRegistration.default_update_strategy`` — e.g. Shiprocket's
        ``update_inplace``), falling back to ``ORDER_UPDATE_STRATEGY_DEFAULT``
        only for partners that declare none.

    **Legacy plain string:**
        ``"cancel_and_recreate"``  or  ``"escalate_for_manual_update"``

        Applied uniformly to every partner that calls this function, preserving
        the original single-strategy behaviour.

    Unknown / unset values fall back to the partner's declared default (then the
    global ``ORDER_UPDATE_STRATEGY_DEFAULT``).

    Note: ``config_value`` is a JSONB column — the DB driver may return a Python
    dict directly.  ``_coerce_strategy_config`` handles both dict and string forms
    so ``str(value)`` is never called on a dict (which would produce invalid JSON).
    """
    if not partner_name:
        return ORDER_UPDATE_STRATEGY_DEFAULT
    if not client_id:
        return _default_strategy_for_partner(partner_name)

    value = await aget_config("order_update_strategy", client_id=client_id)
    if not value:
        return _default_strategy_for_partner(partner_name)

    # ── JSON / JSONB dict format ──────────────────────────────────────
    partner_map = _coerce_strategy_config(value)
    if partner_map is not None:
        raw = partner_map.get(partner_name.strip().lower())
        if raw is None:
            # Partner not listed → use that partner's declared default
            # (e.g. Shiprocket → update_inplace) rather than blanket-escalating.
            return _default_strategy_for_partner(partner_name)
        norm = str(raw).strip().lower()
        if norm in _VALID_STRATEGIES:
            return norm
        logger.warning(
            "Unknown per-partner strategy %r for partner=%s client_id=%s; "
            "falling back to partner default",
            raw, partner_name, client_id,
        )
        return _default_strategy_for_partner(partner_name)

    # ── Legacy plain string ───────────────────────────────────────────
    norm = str(value).strip().lower()
    if norm in _VALID_STRATEGIES:
        return norm
    logger.warning(
        "Unknown order_update_strategy=%r for client_id=%s; "
        "falling back to default %r",
        value, client_id, ORDER_UPDATE_STRATEGY_DEFAULT,
    )
    return ORDER_UPDATE_STRATEGY_DEFAULT


async def aget_order_update_strategy(client_id: Optional[str] = None) -> str:
    """Deprecated: prefer ``aget_partner_update_strategy(partner_name, client_id)``.

    Legacy entry-point for callers that do not have a specific partner name.
    For plain-string configs returns that value directly.  For per-partner JSON /
    JSONB dict configs returns the first valid strategy found, or
    ``ORDER_UPDATE_STRATEGY_DEFAULT``.
    """
    if not client_id:
        return ORDER_UPDATE_STRATEGY_DEFAULT
    value = await aget_config("order_update_strategy", client_id=client_id)
    if not value:
        return ORDER_UPDATE_STRATEGY_DEFAULT
    partner_map = _coerce_strategy_config(value)
    if partner_map is not None:
        for v in partner_map.values():
            norm = str(v).strip().lower()
            if norm in _VALID_STRATEGIES:
                return norm
        return ORDER_UPDATE_STRATEGY_DEFAULT
    norm = str(value).strip().lower()
    if norm in _VALID_STRATEGIES:
        return norm
    return ORDER_UPDATE_STRATEGY_DEFAULT


async def aget_json_config(config_key: str, client_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Async variant of get_json_config."""
    return _parse_json_config_value(await aget_config(config_key, client_id=client_id))


async def aget_conduct_sales(client_id: Optional[str] = None) -> str:
    """Async variant of get_conduct_sales."""
    raw = await aget_config("conduct_sales", client_id=client_id)
    if not raw:
        return "Please contact our customer support for information about sales and promotional events."
    parsed = _parse_json_config_value(raw)
    if parsed:
        for key in ('policy', 'Sales', 'sales', 'message', 'description', 'info'):
            if key in parsed:
                return str(parsed[key])
        if parsed:
            return str(next(iter(parsed.values())))
    return str(raw)


async def aget_primary_discount_coupon(client_id: Optional[str] = None) -> Optional[str]:
    """Get the primary discount coupon code from database (async)."""
    data = _parse_json_config_value(await aget_config("primary_discount_coupon", client_id=client_id))
    if data:
        return list(data.keys())[0]
    return None


def format_qa_data_for_llm(qa_data: Dict[str, str]) -> str:
    """
    Format the Q&A configuration data into a string for LLM consumption.
    
    Args:
        qa_data: Dictionary with questions as keys and answers as values
    
    Returns:
        Formatted string for LLM
    """
    formatted_lines = []
    for question, answer in qa_data.items():
        formatted_lines.append(f"Q: {question}")
        formatted_lines.append(f"A: {answer}")
        formatted_lines.append("")  # Empty line for readability
    
    return "\n".join(formatted_lines)


@with_retry
def resolve_client_id(gupshup_source: Optional[str] = None) -> str:
    """
    Resolve the client ID based on Gupshup source number from the clients table.
    If no match is found or gupshup_source is not provided, returns an empty string.
    Automatically retries on connection errors.
    
    Args:
        gupshup_source: The Gupshup source number to look up (e.g., "15557872987")
    
    Returns:
        Client ID string
    """
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            # Query the clients table to find client_id by gupshup_source_number
            cur.execute("""
                SELECT id as client_id
                FROM clients 
                WHERE gupshup_source_number = %s
                LIMIT 1
            """, (gupshup_source,))
            
            result = cur.fetchone()
            
            if result and result.get('client_id'):
                client_id = str(result['client_id'])  # Convert UUID to string
                logger.info(f"✅ Found client_id '{client_id}' for gupshup_source '{gupshup_source}'")
                return client_id
            else:
                logger.debug(f"No client found for gupshup_source '{gupshup_source}'")
                return ""


async def aresolve_client_id(gupshup_source: Optional[str] = None) -> str:
    """Async variant of resolve_client_id.

    Delegates to the unified tiered cache in
    ``utils.client_identity_cache``. Returns ``""`` (not ``None``) on
    miss for backward compatibility with existing callers.
    """
    from fashion_bot.utils.client_identity_cache import aget_client_id_by_gupshup_source
    result = await aget_client_id_by_gupshup_source(gupshup_source)
    if result:
        logger.info(f"✅ Found client_id '{result}' for gupshup_source '{gupshup_source}' (async)")
        return result
    logger.debug(f"No client found for gupshup_source '{gupshup_source}'")
    return ""


async def aget_client_id_by_app_name(app_name: str) -> Optional[str]:
    """Resolve Gupshup APP_NAME to client_id via unified tiered cache."""
    from fashion_bot.utils.client_identity_cache import aget_client_id_by_gupshup_app_name
    return await aget_client_id_by_gupshup_app_name(app_name)


async def aget_client_id_by_enterprise_app(app_id: str) -> Optional[str]:
    """Resolve Gupshup enterprise app id to client_id via unified tiered cache."""
    from fashion_bot.utils.client_identity_cache import (
        aget_client_id_by_gupshup_enterprise_app,
    )
    return await aget_client_id_by_gupshup_enterprise_app(app_id)


async def aget_gupshup_config_by_client_id(client_id: str) -> Optional[Dict[str, Any]]:
    """Get Gupshup configuration for a specific client ID (async)."""
    if not client_id:
        return None
    try:
        gupshup_details = await aget_config("gupshup_details", client_id=client_id)
        if not gupshup_details:
            return None
        config = json.loads(gupshup_details) if isinstance(gupshup_details, str) else gupshup_details
        if isinstance(config, dict):
            return config
        return None
    except Exception as e:
        logger.error("aget_gupshup_config_by_client_id error for client_id=%s: %s", client_id, e)
        return None


async def aget_gupshup_config_by_source(gupshup_source: str) -> Optional[Dict[str, Any]]:
    """Get Gupshup configuration for a specific source phone number (async)."""
    try:
        client_id = await aresolve_client_id(gupshup_source)
        if not client_id:
            logger.debug(f"No client_id found for gupshup_source '{gupshup_source}'")
            return None

        cached_config = await aget_gupshup_config_from_cache(client_id)
        if cached_config:
            return cached_config

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT config_value
                    FROM client_configs
                    WHERE client_id = %s AND config_key = %s
                    """,
                    (client_id, "gupshup_details"),
                )
                result = await cur.fetchone()

        if result:
            gupshup_details = result.get("config_value") if isinstance(result, dict) else result[0]
            config = json.loads(gupshup_details) if isinstance(gupshup_details, str) else gupshup_details
            if config and isinstance(config, dict):
                logger.info(f"✅ Loaded gupshup_details for client '{client_id}' (source: {gupshup_source}) (async)")
                await acache_gupshup_config(client_id, config)
                return config

        logger.warning(f"⚠️ No gupshup_details found for client '{client_id}'")
        return None

    except Exception as e:
        logger.error(f"❌ Error fetching Gupshup config for source '{gupshup_source}' asynchronously: {str(e)}")
        return None


async def avalidate_gupshup_app_name(gupshup_source: str, incoming_app_name: str) -> bool:
    """Validate if the incoming APP_NAME matches the expected APP_NAME for the given source (async)."""
    config = await aget_gupshup_config_by_source(gupshup_source)

    if config:
        expected_app_name = config.get("APP_NAME")
        if expected_app_name:
            is_valid = incoming_app_name == expected_app_name
            if not is_valid:
                logger.warning(f"⚠️ APP_NAME mismatch for source {gupshup_source}: expected '{expected_app_name}', got '{incoming_app_name}'")
            return is_valid

    logger.debug(f"No APP_NAME config found for source {gupshup_source}, skipping validation")
    return True


async def avalidate_gupshup_app_for_client(client_id: str, incoming_app: str) -> bool:
    """Validate an incoming Gupshup app identifier for a resolved client.

    Enterprise webhooks send the enterprise app id in ``app``. Legacy webhooks
    send the configured ``APP_NAME``. Accept either tenant-scoped config value.
    """
    if not client_id or not incoming_app:
        return False
    try:
        enterprise_details = await aget_config(
            "gupshup_enterprise_app_details", client_id=client_id
        )
        if enterprise_details:
            enterprise_config = (
                json.loads(enterprise_details)
                if isinstance(enterprise_details, str)
                else enterprise_details
            )
            if isinstance(enterprise_config, dict):
                expected_app = str(enterprise_config.get("app") or "").strip()
                if expected_app:
                    return incoming_app == expected_app

        legacy_config = await aget_gupshup_config_by_client_id(client_id)
        if legacy_config and legacy_config.get("APP_NAME"):
            return incoming_app == legacy_config.get("APP_NAME")

        logger.debug(
            "No Gupshup app config found for client_id=%s, skipping validation",
            client_id,
        )
        return True
    except Exception as e:
        logger.error(
            "❌ Error validating Gupshup app for client_id=%s app=%s: %s",
            client_id,
            incoming_app,
            e,
        )
        return False


# ============================================
# CLIENT-AGENT LLM CONFIGURATION (from DB + Redis cache)
# ============================================

async def aget_client_agent_llm_config(client_id: str, agent_name: str) -> Optional[Dict[str, Any]]:
    """Async variant of get_client_agent_llm_config with tiered caching."""
    if not client_id or not agent_name:
        return None

    tiered_key = f"mem:llm_config:{client_id}:{agent_name}"

    async def _db_get():
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT provider, model, temperature, max_tokens,
                           api_key_env_var, base_url, additional_params
                    FROM client_agent_llm_config
                    WHERE client_id = %s
                      AND agent_name = %s
                      AND is_active = TRUE
                    """,
                    (client_id, agent_name),
                )
                row = await cur.fetchone()
                if not row:
                    return None
                return {
                    "provider": row["provider"],
                    "model": row["model"],
                    "temperature": row["temperature"],
                    "max_tokens": row["max_tokens"],
                    "api_key_env_var": row["api_key_env_var"],
                    "base_url": row["base_url"],
                    "additional_params": row["additional_params"] or {},
                }

    async def _redis_get():
        client = await _get_async_redis_client()
        if not client:
            return None
        raw = await client.get(f"llm_config:{client_id}:{agent_name}")
        if raw:
            config = json.loads(raw)
            return config if config else None
        return None

    async def _redis_set(value):
        client = await _get_async_redis_client()
        if client:
            is_negative = not bool(value)
            ttl = min(LLM_CONFIG_CACHE_TTL, LLM_CONFIG_NEGATIVE_CACHE_TTL) if is_negative else LLM_CONFIG_CACHE_TTL
            await client.setex(f"llm_config:{client_id}:{agent_name}", ttl, json.dumps(value or {}))

    try:
        config_dict, source = await aget_with_tiered_cache(
            cache_key=tiered_key,
            ttl_seconds=600,
            load_from_source_fn=_db_get,
            get_from_redis_fn=_redis_get,
            set_to_redis_fn=_redis_set,
            cache_none=True,
        )
        if config_dict:
            if source == "source":
                logger.info(
                    f"🤖 Loaded LLM config from DB (async): client={client_id}, "
                    f"agent={agent_name} → {config_dict['provider']}/{config_dict['model']}"
                )
            return config_dict
    except Exception as e:
        logger.error(f"❌ Error loading LLM config (async) for client={client_id}, agent={agent_name}: {e}")
    return None
