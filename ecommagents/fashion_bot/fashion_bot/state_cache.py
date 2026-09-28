"""
Unified State Cache for Multi-Channel Conversation Management

This module provides a Redis-backed state cache that:
- Supports multiple channels: WhatsApp, Web Chat, Instagram (future), Streamlit
- Uses unified thread_id pattern: {channel}:{tenant_id}:{user_id}
- Persists all state to Redis (survives server restarts)
- Handles session-to-phone migration for web chat
- Compatible with Upstash Redis (uses RedisJSON, no RediSearch)

Key Pattern:
    state:{channel}:{tenant_id}:{user_id}

Examples:
    state:whatsapp:15557872987:919876543210
    state:web:abc123-uuid:session-456
    state:web:abc123-uuid:919876543210 (after phone linking)
"""

import asyncio
import logging
import json
import copy
import time as _time_mod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, Optional, Any, List, Tuple
from threading import Lock
from collections import OrderedDict
from langchain_core.messages import HumanMessage, AIMessage, BaseMessage, SystemMessage

from fashion_bot.schema import SupportState
from fashion_bot.env_loader import bootstrap_environment, get_bool, get_env, get_int
from fashion_bot.utils.redis_guard import RedisGuard, build_degraded_flags

# Configure logging
logger = logging.getLogger(__name__)
bootstrap_environment()

# Redis configuration
try:
    import redis
    import redis.asyncio as redis_async
    import certifi
except ImportError:
    redis = None
    redis_async = None
    certifi = None
    logger.warning("redis package not installed; state cache will use in-memory only")

REDIS_URL = get_env("REDIS_URL") or get_env("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"
STATE_CACHE_REDIS_TTL_HOURS = get_int("STATE_CACHE_REDIS_TTL_HOURS", 24)
ENABLE_REDIS_DEGRADED_FALLBACKS = get_bool("ENABLE_REDIS_DEGRADED_FALLBACKS", True)
ALLOW_STATELESS_FALLBACK = get_bool("ALLOW_STATELESS_FALLBACK", True)
FALLBACK_STORE_MAX_ENTRIES = get_int("STATE_CACHE_FALLBACK_MAX_ENTRIES", 2000)
FALLBACK_STORE_TTL_SECONDS = get_int("STATE_CACHE_FALLBACK_TTL_SECONDS", 1800)


class _ProcessLocalFallbackStore:
    """Non-authoritative process-local state fallback (LRU + TTL)."""

    def __init__(self, max_entries: int, ttl_seconds: int):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._lock = Lock()
        self._store: "OrderedDict[str, Tuple[float, SupportState]]" = OrderedDict()

    def _prune_locked(self) -> None:
        now = datetime.now(timezone.utc).timestamp()
        expired = [k for k, (ts, _) in self._store.items() if (now - ts) > self.ttl_seconds]
        for key in expired:
            self._store.pop(key, None)
        while len(self._store) > self.max_entries:
            self._store.popitem(last=False)

    def get(self, key: str) -> Optional[SupportState]:
        with self._lock:
            self._prune_locked()
            value = self._store.get(key)
            if not value:
                return None
            ts, state = value
            if (datetime.now(timezone.utc).timestamp() - ts) > self.ttl_seconds:
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return copy.deepcopy(state)

    def set(self, key: str, state: SupportState) -> None:
        with self._lock:
            self._store[key] = (datetime.now(timezone.utc).timestamp(), copy.deepcopy(state))
            self._store.move_to_end(key)
            self._prune_locked()

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def size(self) -> int:
        with self._lock:
            self._prune_locked()
            return len(self._store)


# ==================== ENUMS AND DATA CLASSES ====================

class Channel(Enum):
    """Communication channels supported by the unified state cache."""
    WHATSAPP = "whatsapp"
    WEB = "web"
    INSTAGRAM = "instagram"
    STREAMLIT = "streamlit"


@dataclass
class ThreadConfig:
    """Configuration for a conversation thread."""
    channel: Channel
    tenant_id: str
    user_id: str
    
    @property
    def thread_id(self) -> str:
        """Generate the unified thread_id string."""
        return f"{self.channel.value}:{self.tenant_id}:{self.user_id}"
    
    @classmethod
    def from_thread_id(cls, thread_id: str) -> "ThreadConfig":
        """Parse a thread_id string back to ThreadConfig."""
        parts = thread_id.split(":", 2)
        if len(parts) != 3:
            raise ValueError(f"Invalid thread_id format: {thread_id}")
        
        channel = Channel(parts[0])
        return cls(channel=channel, tenant_id=parts[1], user_id=parts[2])


# ==================== REDIS CLIENT MANAGEMENT ====================

def _sanitize_redis_url(url: str) -> str:
    """Sanitizes a Redis URL for logging by removing sensitive information."""
    if "://" in url:
        parts = url.split("://", 1)
        return f"{parts[0]}://***"
    return url


def _build_redis_client_kwargs() -> Dict[str, Any]:
    client_kwargs: Dict[str, Any] = {"decode_responses": True}
    try:
        if str(REDIS_URL).lower().startswith("rediss://"):
            if certifi:
                client_kwargs["ssl_ca_certs"] = certifi.where()
            insecure = get_bool("REDIS_SSL_INSECURE", False)
            if insecure:
                client_kwargs["ssl_cert_reqs"] = None
    except Exception:
        pass
    return client_kwargs


def _create_redis_client():
    """Create a new Redis client for state caching."""
    if not redis:
        return None
    try:
        client_kwargs = _build_redis_client_kwargs()
        client = redis.Redis.from_url(REDIS_URL, **client_kwargs)
        # Ping once to validate
        client.ping()
        logger.info(f"[REDIS_STATE] Connected to Redis at {_sanitize_redis_url(REDIS_URL)}")
        return client
    except Exception as ex:
        logger.error(f"[REDIS_STATE] Failed to connect to Redis: {ex}")
        return None


async def _create_async_redis_client():
    """Create a new async Redis client for state caching."""
    if not redis_async:
        return None
    try:
        client_kwargs = _build_redis_client_kwargs()
        client = redis_async.Redis.from_url(REDIS_URL, **client_kwargs)
        await client.ping()
        logger.info(f"[REDIS_STATE] Connected to async Redis at {_sanitize_redis_url(REDIS_URL)}")
        return client
    except Exception as ex:
        logger.error(f"[REDIS_STATE] Failed to connect to async Redis: {ex}")
        return None


# ==================== SERIALIZATION HELPERS ====================

def _now_iso() -> str:
    """Return current server-local time as ISO-8601 string."""
    return datetime.now().isoformat()


def timestamped_human_message(content: str) -> HumanMessage:
    """Create a HumanMessage with an execution timestamp in additional_kwargs."""
    return HumanMessage(
        content=content,
        additional_kwargs={"timestamp": _now_iso()},
    )


def timestamped_ai_message(content: str) -> AIMessage:
    """Create an AIMessage with an execution timestamp in additional_kwargs."""
    return AIMessage(
        content=content,
        additional_kwargs={"timestamp": _now_iso()},
    )


async def inject_order_event(
    phone: str,
    client_id: str,
    order_id: str,
    action: str,
    via_agent: bool = False,
) -> bool:
    """
    Append an order-event SystemMessage to an active conversation in Redis.

    Instead of mutating an existing message, this appends a **new** SystemMessage
    with ``additional_kwargs["event_type"] = "order_update"`` so it is clearly
    distinguishable from regular bot responses.

    Using SystemMessage (rather than AIMessage) ensures the LLM treats this as
    background context and will never echo it back as its own reply.  Existing
    conversation-history builders in detect_intent_node, final_answer_node, and
    skill nodes only collect HumanMessage / AIMessage, so SystemMessages are
    naturally excluded from "User:" / "Bot:" history without any extra filtering.

    The ``via_agent`` flag indicates whether the action was triggered by the
    chatbot agent (``True``) or by the customer/admin outside the bot
    (``False``).  This is derived from the Shopify order ``tags`` field — orders
    created by the agent carry the ``BLOOMERCE_CREATED`` tag, and orders
    updated (size/product change) carry the ``BLOOMERCE_UPDATED`` tag.

    Args:
        phone: Customer phone number (will be normalised to last 10 digits).
        client_id: Tenant / client UUID.
        order_id: Display order ID (e.g. ``"#146108"``).
        action: One of ``"created"``, ``"updated"``, ``"cancelled"``.
        via_agent: ``True`` if the action was performed by the chatbot agent.

    Returns:
        ``True`` if the event message was persisted, ``False`` otherwise
        (e.g. no active conversation found).
    """
    import re

    # Normalise phone to last-10 digits (same logic used for Redis keys)
    digits = re.sub(r'\D', '', phone or '')
    normalized_phone = digits[-10:] if len(digits) >= 10 else digits
    if not normalized_phone:
        return False

    try:
        # Late import to avoid circular dependency at module level
        cache = get_unified_cache()
        thread_id = cache.generate_thread_id(Channel.WHATSAPP, client_id, normalized_phone)
        state = await cache.aget_state(thread_id)

        if state is None:
            logger.info(
                f"[ORDER_EVENT_INJECT] No active conversation for "
                f"phone={normalized_phone}, client={client_id} — skipping"
            )
            return False

        messages = state.get("messages") or []
        if not messages:
            logger.info("[ORDER_EVENT_INJECT] Conversation has no messages — skipping")
            return False

        # Build the event message as a SystemMessage
        source = "agent" if via_agent else "user"
        event_content = f"[Order Update] Order {order_id}: {action} by {source}"

        if any(
            hasattr(m, 'content') and m.content == event_content
            for m in messages[-10:]
        ):
            logger.info(
                f"[ORDER_EVENT_INJECT] Duplicate event already in messages — skipping: {event_content}"
            )
            return False

        event_msg = SystemMessage(
            content=event_content,
            additional_kwargs={
                "timestamp": _now_iso(),
                "event_type": "order_update",
                "via_agent": via_agent,
            },
        )

        messages.append(event_msg)
        state["messages"] = messages

        await cache.aupdate_state(thread_id, state)
        logger.info(
            f"[ORDER_EVENT_INJECT] ✅ Appended order event SystemMessage for "
            f"phone={normalized_phone}, order={order_id}, action={action}, via_agent={via_agent}"
        )
        return True

    except Exception as e:
        logger.warning(f"[ORDER_EVENT_INJECT] Failed to inject: {e}")
        return False


def serialize_message(msg: BaseMessage) -> Dict[str, Any]:
    """Serialize a LangChain message to a dictionary."""
    return {
        "type": msg.__class__.__name__,
        "content": msg.content,
        # Include additional_kwargs if present (for tool calls, etc.)
        "additional_kwargs": getattr(msg, 'additional_kwargs', {})
    }


def deserialize_message(msg_dict: Dict[str, Any]) -> BaseMessage:
    """Deserialize a dictionary back to a LangChain message."""
    msg_type = msg_dict.get("type", "HumanMessage")
    content = msg_dict.get("content", "")
    additional_kwargs = msg_dict.get("additional_kwargs", {})
    
    if msg_type == "HumanMessage":
        return HumanMessage(content=content, additional_kwargs=additional_kwargs)
    elif msg_type == "AIMessage":
        return AIMessage(content=content, additional_kwargs=additional_kwargs)
    elif msg_type == "SystemMessage":
        return SystemMessage(content=content, additional_kwargs=additional_kwargs)
    else:
        # Default to HumanMessage for unknown types
        return HumanMessage(content=content, additional_kwargs=additional_kwargs)


def serialize_state(state: SupportState) -> str:
    """
    Serialize SupportState to JSON string for Redis storage.
    
    Handles special types:
    - LangChain messages (HumanMessage, AIMessage, SystemMessage)
    - datetime objects
    - Nested dictionaries and lists
    """
    serializable_state = {}
    
    for key, value in state.items():
        if value is None:
            serializable_state[key] = None
        elif key == "messages" and value:
            # Serialize LangChain messages
            serializable_state[key] = [serialize_message(msg) for msg in value]
        elif isinstance(value, datetime):
            serializable_state[key] = value.isoformat()
        else:
            # Most fields are already JSON-serializable (str, int, bool, dict, list)
            serializable_state[key] = value
    
    return json.dumps(serializable_state, default=str)


def deserialize_state(json_str: str) -> SupportState:
    """
    Deserialize JSON string from Redis to SupportState.
    
    Reconstructs:
    - LangChain messages from serialized format
    - Other fields as-is (they're already correct types)
    """
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"[REDIS_STATE] Failed to deserialize state JSON: {e}")
        raise
    
    # Reconstruct LangChain messages
    if "messages" in data and data["messages"]:
        messages = []
        for msg in data["messages"]:
            if isinstance(msg, dict):
                messages.append(deserialize_message(msg))
            else:
                # Fallback: treat as raw content
                messages.append(HumanMessage(content=str(msg)))
        data["messages"] = messages
    
    return SupportState(**data)


# ==================== UNIFIED STATE CACHE ====================

class UnifiedStateCache:
    """
    Redis-backed state cache supporting all channels.
    Compatible with Upstash Redis (uses RedisJSON, no RediSearch).
    
    Key Features:
    - Unified thread_id pattern: {channel}:{tenant_id}:{user_id}
    - TTL-based expiry (24 hours, refreshed on activity)
    - Session-to-phone migration for web chat
    - Thread indexing using Redis SET (no RediSearch needed)
    - Phone number normalization for consistent keys
    """
    
    # Key prefixes
    STATE_PREFIX = "state"
    INDEX_PREFIX = "thread_index"
    SESSION_PHONE_PREFIX = "session_phone"
    
    def __init__(self, ttl_hours: int = 24):
        self.ttl_seconds = ttl_hours * 3600
        self.ttl_hours = ttl_hours
        self._lock = Lock()
        self._redis_guard = RedisGuard()
        self._fallback_store = _ProcessLocalFallbackStore(
            max_entries=FALLBACK_STORE_MAX_ENTRIES,
            ttl_seconds=FALLBACK_STORE_TTL_SECONDS,
        )
        
        # Redis client (lazy initialization)
        self._redis_client = None
        self._redis_initialized = False
        self._async_redis_client = None
        self._async_redis_initialized = False
        self._async_client_lock: Optional[asyncio.Lock] = None

    def _mark_degraded_state(
        self,
        state: SupportState,
        component: str,
        error: Optional[Dict[str, Any]],
        single_flight_degraded: bool = False,
        summary_skipped: bool = False,
    ) -> SupportState:
        state_dict = dict(state or {})
        merged = build_degraded_flags(state_dict, component, error)
        if single_flight_degraded:
            merged["single_flight_degraded"] = True
        if summary_skipped:
            merged["summary_skipped_due_to_redis"] = True
        return SupportState(**merged)
    
    def _get_redis_client(self):
        """Get or create Redis client (lazy initialization)."""
        if not self._redis_initialized:
            self._redis_client = _create_redis_client()
            self._redis_initialized = True
        return self._redis_client

    async def _get_async_redis_client(self):
        """Get or create async Redis client (lazy initialization)."""
        if self._async_redis_initialized:
            return self._async_redis_client
        if self._async_client_lock is None:
            self._async_client_lock = asyncio.Lock()
        async with self._async_client_lock:
            if not self._async_redis_initialized:
                self._async_redis_client = await _create_async_redis_client()
                self._async_redis_initialized = True
        return self._async_redis_client
    
    # ==================== PHONE NORMALIZATION ====================
    
    @staticmethod
    def normalize_phone(phone: str) -> str:
        """
        Normalize phone number for consistent Redis keys.
        
        Removes non-digit characters and keeps last 10 digits.
        This ensures the same user gets the same thread regardless of
        phone format variations (+91, 91, or just 10 digits).
        
        Examples:
            "+919876543210" -> "9876543210"
            "919876543210"  -> "9876543210"
            "9876543210"    -> "9876543210"
            "+91 98765 43210" -> "9876543210"
        
        Args:
            phone: Phone number in any format
            
        Returns:
            Normalized 10-digit phone number (or original digits if < 10)
        """
        if not phone:
            return phone
        digits = ''.join(filter(str.isdigit, str(phone)))
        return digits[-10:] if len(digits) >= 10 else digits
    
    # ==================== THREAD ID HELPERS ====================
    
    @staticmethod
    def is_uuid(value: str) -> bool:
        """Check if a string looks like a UUID."""
        if not value:
            return False
        # UUID format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (36 chars with dashes)
        # or 32 hex chars without dashes
        import re
        uuid_pattern = re.compile(
            r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
        )
        return bool(uuid_pattern.match(str(value)))
    
    @staticmethod
    def generate_thread_id(channel: Channel, tenant_id: str, user_id: str) -> str:
        """
        Generate unified thread_id for any channel.
        
        For WhatsApp channel:
        - Normalizes phone numbers to ensure consistent keys
        - If tenant_id is a UUID (client_id), uses it as-is without normalization
        """
        # Normalize phone numbers for WhatsApp channel
        if channel == Channel.WHATSAPP:
            user_id = UnifiedStateCache.normalize_phone(user_id)
            # Only normalize tenant_id if it's a phone number, not a UUID
            if not UnifiedStateCache.is_uuid(tenant_id):
                tenant_id = UnifiedStateCache.normalize_phone(tenant_id)
            # If tenant_id is a UUID (client_id), use it as-is
        
        return f"{channel.value}:{tenant_id}:{user_id}"
    
    @staticmethod
    def parse_thread_id(thread_id: str) -> ThreadConfig:
        """Parse a thread_id string to ThreadConfig."""
        return ThreadConfig.from_thread_id(thread_id)
    
    def _get_state_key(self, thread_id: str) -> str:
        """Generate Redis key for state storage."""
        return f"{self.STATE_PREFIX}:{thread_id}"
    
    def _get_index_key(self, channel: Optional[Channel] = None, tenant_id: Optional[str] = None) -> str:
        """Generate Redis key for thread indexing."""
        if channel and tenant_id:
            return f"{self.INDEX_PREFIX}:{channel.value}:{tenant_id}"
        elif channel:
            return f"{self.INDEX_PREFIX}:{channel.value}"
        else:
            return f"{self.INDEX_PREFIX}:all"
    
    def _get_session_phone_key(self, client_id: str, session_id: str) -> str:
        """Generate Redis key for session-phone mapping."""
        return f"{self.SESSION_PHONE_PREFIX}:{client_id}:{session_id}"
    
    # ==================== CORE STATE OPERATIONS ====================
    
    def get_state(self, thread_id: str) -> Optional[SupportState]:
        """
        Get state from Redis.
        
        Args:
            thread_id: Unified thread identifier
            
        Returns:
            SupportState if found, None otherwise
        """
        client = self._get_redis_client()
        if not client:
            logger.warning("[REDIS_STATE] Redis not available for get_state; trying process-local fallback")
            return self._fallback_store.get(thread_id)
        
        try:
            key = self._get_state_key(thread_id)
            guard_result = self._redis_guard.execute(
                op_name="state_get",
                fn=lambda: client.get(key),
                fallback=None,
            )
            if not guard_result.ok:
                logger.warning(
                    f"[REDIS_STATE] Guard fallback for get_state thread={thread_id}: {guard_result.error}"
                )
                fallback_state = self._fallback_store.get(thread_id)
                if fallback_state:
                    return self._mark_degraded_state(fallback_state, "state_cache", guard_result.error)
                return None
            json_data = guard_result.value
            
            if json_data:
                state = deserialize_state(json_data)
                # Refresh TTL on access
                _ = self._redis_guard.execute(
                    op_name="state_expire",
                    fn=lambda: client.expire(key, self.ttl_seconds),
                    fallback=False,
                )
                logger.debug(f"[REDIS_STATE] GET hit=true")
                # Keep local hot copy for degraded fallback
                self._fallback_store.set(thread_id, state)
                return state
            else:
                logger.debug(f"[REDIS_STATE] GET hit=false")
                return self._fallback_store.get(thread_id)
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error getting state: {e}")
            fallback_state = self._fallback_store.get(thread_id)
            if fallback_state:
                return self._mark_degraded_state(
                    fallback_state, "state_cache", {"reason": "exception", "op": "state_get", "error": str(e), "latency_ms": 0}
                )
            return None

    async def aget_state(self, thread_id: str) -> Optional[SupportState]:
        """Async variant of get_state()."""
        client = await self._get_async_redis_client()
        if not client:
            logger.warning("[REDIS_STATE] Async Redis not available for get_state; trying process-local fallback")
            return self._fallback_store.get(thread_id)

        try:
            key = self._get_state_key(thread_id)
            _t0 = _time_mod.monotonic()
            guard_result = await self._redis_guard.execute_async(
                op_name="state_get",
                fn=lambda: client.get(key),
                fallback=None,
            )
            _elapsed = int((_time_mod.monotonic() - _t0) * 1000)
            if not guard_result.ok:
                logger.warning(
                    f"[REDIS_STATE] GET fallback elapsed_ms={_elapsed} thread={thread_id[:16]}: {guard_result.error}"
                )
                fallback_state = self._fallback_store.get(thread_id)
                if fallback_state:
                    return self._mark_degraded_state(fallback_state, "state_cache", guard_result.error)
                return None
            json_data = guard_result.value

            if json_data:
                state = deserialize_state(json_data)
                _ = await self._redis_guard.execute_async(
                    op_name="state_expire",
                    fn=lambda: client.expire(key, self.ttl_seconds),
                    fallback=False,
                )
                self._fallback_store.set(thread_id, state)
                logger.debug(f"[REDIS_STATE] GET elapsed_ms={_elapsed} size={len(json_data)} hit=true")
                return state
            logger.debug(f"[REDIS_STATE] GET elapsed_ms={_elapsed} hit=false")
            return self._fallback_store.get(thread_id)
        except Exception as e:
            logger.error(f"[REDIS_STATE] Async error getting state: {e}")
            fallback_state = self._fallback_store.get(thread_id)
            if fallback_state:
                return self._mark_degraded_state(
                    fallback_state, "state_cache", {"reason": "exception", "op": "state_get", "error": str(e), "latency_ms": 0}
                )
            return None
    
    def set_state(self, thread_id: str, state: SupportState) -> bool:
        """
        Save state to Redis with TTL.
        
        Args:
            thread_id: Unified thread identifier
            state: State to save
            
        Returns:
            True if successful, False otherwise
        """
        client = self._get_redis_client()
        if not client:
            logger.warning("[REDIS_STATE] Redis not available for set_state; writing process-local fallback")
            self._fallback_store.set(thread_id, state)
            return ENABLE_REDIS_DEGRADED_FALLBACKS
        
        try:
            key = self._get_state_key(thread_id)
            json_data = serialize_state(state)
            
            # Use SETEX for atomic set with expiry
            ttl_seconds = max(1, self.ttl_seconds)
            guard_result = self._redis_guard.execute(
                op_name="state_setex",
                fn=lambda: client.setex(key, ttl_seconds, json_data),
                fallback=False,
            )
            if not guard_result.ok:
                logger.warning(
                    f"[REDIS_STATE] Guard fallback for set_state thread={thread_id}: {guard_result.error}"
                )
                degraded_state = self._mark_degraded_state(state, "state_cache", guard_result.error)
                self._fallback_store.set(thread_id, degraded_state)
                return ENABLE_REDIS_DEGRADED_FALLBACKS
            
            # Update thread indices
            self._add_to_index(thread_id)
            self._fallback_store.set(thread_id, state)
            
            logger.info(f"[REDIS_STATE] SET {key} - size={len(json_data)} bytes, TTL={self.ttl_hours}h")
            return True
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error saving state: {e}")
            degraded_state = self._mark_degraded_state(
                state, "state_cache", {"reason": "exception", "op": "state_setex", "error": str(e), "latency_ms": 0}
            )
            self._fallback_store.set(thread_id, degraded_state)
            return ENABLE_REDIS_DEGRADED_FALLBACKS

    async def aset_state(self, thread_id: str, state: SupportState) -> bool:
        """Async variant of set_state()."""
        client = await self._get_async_redis_client()
        if not client:
            logger.warning("[REDIS_STATE] Async Redis not available for set_state; writing process-local fallback")
            self._fallback_store.set(thread_id, state)
            return ENABLE_REDIS_DEGRADED_FALLBACKS

        try:
            key = self._get_state_key(thread_id)
            json_data = serialize_state(state)
            ttl_seconds = max(1, self.ttl_seconds)
            _t0 = _time_mod.monotonic()
            guard_result = await self._redis_guard.execute_async(
                op_name="state_setex",
                fn=lambda: client.setex(key, ttl_seconds, json_data),
                fallback=False,
            )
            _elapsed = int((_time_mod.monotonic() - _t0) * 1000)
            if not guard_result.ok:
                logger.warning(
                    f"[REDIS_STATE] SET fallback elapsed_ms={_elapsed} thread={thread_id[:16]}: {guard_result.error}"
                )
                degraded_state = self._mark_degraded_state(state, "state_cache", guard_result.error)
                self._fallback_store.set(thread_id, degraded_state)
                return ENABLE_REDIS_DEGRADED_FALLBACKS

            await self._aadd_to_index(thread_id)
            self._fallback_store.set(thread_id, state)
            logger.info(f"[REDIS_STATE] SET elapsed_ms={_elapsed} size={len(json_data)}b TTL={self.ttl_hours}h")
            return True
        except Exception as e:
            logger.error(f"[REDIS_STATE] Async error saving state: {e}")
            degraded_state = self._mark_degraded_state(
                state, "state_cache", {"reason": "exception", "op": "state_setex", "error": str(e), "latency_ms": 0}
            )
            self._fallback_store.set(thread_id, degraded_state)
            return ENABLE_REDIS_DEGRADED_FALLBACKS
    
    def delete_state(self, thread_id: str) -> bool:
        """
        Delete state from Redis.
        
        Args:
            thread_id: Unified thread identifier
            
        Returns:
            True if successful, False otherwise
        """
        client = self._get_redis_client()
        if not client:
            self._fallback_store.delete(thread_id)
            return ENABLE_REDIS_DEGRADED_FALLBACKS
        
        try:
            key = self._get_state_key(thread_id)
            guard_result = self._redis_guard.execute(
                op_name="state_delete",
                fn=lambda: client.delete(key),
                fallback=0,
            )
            if not guard_result.ok:
                logger.warning(f"[REDIS_STATE] Guard fallback for delete_state thread={thread_id}: {guard_result.error}")
                self._fallback_store.delete(thread_id)
                return ENABLE_REDIS_DEGRADED_FALLBACKS
            
            # Remove from indices
            self._remove_from_index(thread_id)
            self._fallback_store.delete(thread_id)
            
            logger.info(f"[REDIS_STATE] DEL {key}")
            return True
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error deleting state: {e}")
            self._fallback_store.delete(thread_id)
            return ENABLE_REDIS_DEGRADED_FALLBACKS
    
    # ==================== INDEX MANAGEMENT ====================
    
    def _add_to_index(self, thread_id: str) -> None:
        """Add thread_id to relevant indices."""
        client = self._get_redis_client()
        if not client:
            return
        
        try:
            config = self.parse_thread_id(thread_id)
            
            # Add to global index
            _ = self._redis_guard.execute(
                op_name="index_add_global",
                fn=lambda: client.sadd(self._get_index_key(), thread_id),
                fallback=0,
            )

            # Add to channel-specific index
            _ = self._redis_guard.execute(
                op_name="index_add_channel",
                fn=lambda: client.sadd(self._get_index_key(config.channel), thread_id),
                fallback=0,
            )

            # Add to tenant-specific index
            _ = self._redis_guard.execute(
                op_name="index_add_tenant",
                fn=lambda: client.sadd(self._get_index_key(config.channel, config.tenant_id), config.user_id),
                fallback=0,
            )
            
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error adding to index: {e}")

    async def _aadd_to_index(self, thread_id: str) -> None:
        """Async variant of _add_to_index()."""
        client = await self._get_async_redis_client()
        if not client:
            return

        try:
            config = self.parse_thread_id(thread_id)
            _ = await self._redis_guard.execute_async(
                op_name="index_add_global",
                fn=lambda: client.sadd(self._get_index_key(), thread_id),
                fallback=0,
            )
            _ = await self._redis_guard.execute_async(
                op_name="index_add_channel",
                fn=lambda: client.sadd(self._get_index_key(config.channel), thread_id),
                fallback=0,
            )
            _ = await self._redis_guard.execute_async(
                op_name="index_add_tenant",
                fn=lambda: client.sadd(self._get_index_key(config.channel, config.tenant_id), config.user_id),
                fallback=0,
            )
        except Exception as e:
            logger.error(f"[REDIS_STATE] Async error adding to index: {e}")
    
    def _remove_from_index(self, thread_id: str) -> None:
        """Remove thread_id from indices."""
        client = self._get_redis_client()
        if not client:
            return
        
        try:
            config = self.parse_thread_id(thread_id)
            
            # Remove from global index
            _ = self._redis_guard.execute(
                op_name="index_remove_global",
                fn=lambda: client.srem(self._get_index_key(), thread_id),
                fallback=0,
            )

            # Remove from channel-specific index
            _ = self._redis_guard.execute(
                op_name="index_remove_channel",
                fn=lambda: client.srem(self._get_index_key(config.channel), thread_id),
                fallback=0,
            )

            # Remove from tenant-specific index
            _ = self._redis_guard.execute(
                op_name="index_remove_tenant",
                fn=lambda: client.srem(self._get_index_key(config.channel, config.tenant_id), config.user_id),
                fallback=0,
            )
            
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error removing from index: {e}")
    
    # ==================== HIGH-LEVEL API ====================
    
    def get_or_create_state(
        self,
        channel: Channel,
        tenant_id: str,
        user_id: str,
        initial_message: Optional[str] = None,
        product_info: str = "",
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        force_new: bool = False
    ) -> Tuple[SupportState, bool]:
        """
        Get existing state or create a new one.
        
        Args:
            channel: Communication channel
            tenant_id: Business/client identifier
            user_id: User identifier (phone, session_id, etc.)
            initial_message: Initial user message (optional)
            product_info: Product information for new states
            client_id: Client ID for multi-client support
            session_id: Session ID for web chat
            force_new: If True, always create a fresh state
            
        Returns:
            Tuple of (state, is_new_state)
        """
        with self._lock:
            thread_id = self.generate_thread_id(channel, tenant_id, user_id)
            current_time = datetime.now(timezone.utc)
            is_new_state = False
            state = None
            redis_available = self._get_redis_client() is not None
            
            if not force_new:
                state = self.get_state(thread_id)
            
            if state is None:
                logger.info(f"[STATE_CACHE] Creating new state for {thread_id}")
                from fashion_bot.trace_context import generate_trace_id

                initial_messages = [timestamped_human_message(initial_message)] if initial_message else []

                # Build state based on channel
                state = SupportState(
                    messages=initial_messages,
                    product_info=product_info,
                    phone_number=user_id if channel == Channel.WHATSAPP else None,
                    gupshup_source_phone_number=tenant_id if channel == Channel.WHATSAPP else None,
                    client_id=client_id,
                    trace_id=generate_trace_id(),
                    selected_order_id=None,
                    known_orders=None,
                    order_status_by_id=None,
                    is_order_query=None,
                    is_frustrated=None,
                    needs_escalation=None,
                    needs_human_agent=None,
                    scratchpad=None,
                    conversation_context=None,
                    last_message_at=current_time.isoformat()
                )
                
                # Add web-specific fields
                if channel == Channel.WEB:
                    state["session_id"] = session_id or user_id

                if not redis_available:
                    state = self._mark_degraded_state(
                        state,
                        "state_cache",
                        {
                            "reason": "redis_unavailable",
                            "op": "get_or_create_state",
                            "latency_ms": 0,
                            "error": "redis client unavailable",
                        },
                    )
                
                is_new_state = True
            else:
                # State exists - add new message if provided
                if initial_message:
                    if state.get("messages") is None:
                        state["messages"] = []
                    
                    state["messages"].append(timestamped_human_message(initial_message))
                # Update last_message_at timestamp
                state["last_message_at"] = current_time.isoformat()
            
            # Save state
            save_ok = self.set_state(thread_id, state)
            if not save_ok and ALLOW_STATELESS_FALLBACK:
                logger.warning(f"[STATE_CACHE] Failed to persist state for {thread_id}; proceeding in degraded stateless mode")
            
            return state, is_new_state

    async def aget_or_create_state(
        self,
        channel: Channel,
        tenant_id: str,
        user_id: str,
        initial_message: Optional[str] = None,
        product_info: str = "",
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        force_new: bool = False
    ) -> Tuple[SupportState, bool]:
        """Async variant of get_or_create_state()."""
        with self._lock:
            thread_id = self.generate_thread_id(channel, tenant_id, user_id)
            current_time = datetime.now(timezone.utc)
            is_new_state = False
            state = None

        redis_available = (await self._get_async_redis_client()) is not None

        if not force_new:
            state = await self.aget_state(thread_id)

        if state is None:
            logger.info(f"[STATE_CACHE] Creating new state for {thread_id}")
            from fashion_bot.trace_context import generate_trace_id

            initial_messages = [timestamped_human_message(initial_message)] if initial_message else []
            state = SupportState(
                messages=initial_messages,
                product_info=product_info,
                phone_number=user_id if channel == Channel.WHATSAPP else None,
                gupshup_source_phone_number=tenant_id if channel == Channel.WHATSAPP else None,
                client_id=client_id,
                trace_id=generate_trace_id(),
                selected_order_id=None,
                known_orders=None,
                order_status_by_id=None,
                is_order_query=None,
                is_frustrated=None,
                needs_escalation=None,
                needs_human_agent=None,
                scratchpad=None,
                conversation_context=None,
                last_message_at=current_time.isoformat()
            )
            if channel == Channel.WEB:
                state["session_id"] = session_id or user_id
            if not redis_available:
                state = self._mark_degraded_state(
                    state,
                    "state_cache",
                    {
                        "reason": "redis_unavailable",
                        "op": "get_or_create_state",
                        "latency_ms": 0,
                        "error": "redis client unavailable",
                    },
                )
            is_new_state = True
        else:
            if initial_message:
                if state.get("messages") is None:
                    state["messages"] = []
                state["messages"].append(timestamped_human_message(initial_message))
            state["last_message_at"] = current_time.isoformat()

        save_ok = await self.aset_state(thread_id, state)
        if not save_ok and ALLOW_STATELESS_FALLBACK:
            logger.warning(f"[STATE_CACHE] Failed to persist state for {thread_id}; proceeding in degraded stateless mode")

        return state, is_new_state
    
    def update_state(self, thread_id: str, state: SupportState) -> None:
        """
        Update state after graph execution.
        
        Args:
            thread_id: Unified thread identifier
            state: Updated state to save
        """
        with self._lock:
            save_ok = self.set_state(thread_id, state)
            if not save_ok and ALLOW_STATELESS_FALLBACK:
                logger.warning(f"[STATE_CACHE] update_state failed for {thread_id}; degraded fallback in use")
            logger.debug(f"[STATE_CACHE] Updated state for {thread_id}")

    async def aupdate_state(self, thread_id: str, state: SupportState) -> None:
        """Async variant of update_state()."""
        save_ok = await self.aset_state(thread_id, state)
        if not save_ok and ALLOW_STATELESS_FALLBACK:
            logger.warning(f"[STATE_CACHE] async update_state failed for {thread_id}; degraded fallback in use")
        logger.debug(f"[STATE_CACHE] Updated state for {thread_id}")
    
    # ==================== WEB CHAT SESSION MANAGEMENT ====================
    
    def migrate_session_to_phone(
        self,
        client_id: str,
        session_id: str,
        phone_number: str
    ) -> Tuple[str, bool]:
        """
        Migrate web session state to phone-based thread.
        
        When a web chat user provides their phone number, we can:
        1. Link their session to the phone for cross-device continuity
        2. Merge with existing phone-based state if they've chatted before
        
        Args:
            client_id: Client UUID
            session_id: Current session ID
            phone_number: User's phone number
            
        Returns:
            Tuple of (new_thread_id, was_migrated)
        """
        session_thread = self.generate_thread_id(Channel.WEB, client_id, session_id)
        phone_thread = self.generate_thread_id(Channel.WEB, client_id, phone_number)
        
        # Check if phone thread already exists (returning user)
        existing = self.get_state(phone_thread)
        if existing:
            self.link_session_to_phone(client_id, session_id, phone_number)
            return phone_thread, False  # Use existing, no migration
        
        # Get session state and migrate
        session_state = self.get_state(session_thread)
        if session_state:
            session_state["phone_number"] = phone_number
            self.set_state(phone_thread, session_state)
            self.link_session_to_phone(client_id, session_id, phone_number)
            logger.info(f"[STATE_CACHE] Migrated session {session_id[:8]}... to phone {phone_number}")
            return phone_thread, True
        
        # No existing state, create fresh
        return phone_thread, False
    
    def link_session_to_phone(self, client_id: str, session_id: str, phone_number: str) -> None:
        """
        Store session-to-phone mapping for future lookups.
        
        Args:
            client_id: Client UUID
            session_id: Session ID
            phone_number: Linked phone number
        """
        client = self._get_redis_client()
        if not client:
            return
        
        try:
            key = self._get_session_phone_key(client_id, session_id)
            _ = self._redis_guard.execute(
                op_name="session_link_setex",
                fn=lambda: client.setex(key, self.ttl_seconds, phone_number),
                fallback=False,
            )
            logger.debug(f"[REDIS_STATE] Linked session {session_id[:8]}... to phone {phone_number}")
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error linking session to phone: {e}")

    async def alink_session_to_phone(self, client_id: str, session_id: str, phone_number: str) -> None:
        """Async variant of link_session_to_phone()."""
        client = await self._get_async_redis_client()
        if client:
            try:
                key = self._get_session_phone_key(client_id, session_id)
                _ = await self._redis_guard.execute_async(
                    op_name="session_link_setex",
                    fn=lambda: client.setex(key, self.ttl_seconds, phone_number),
                    fallback=False,
                )
                logger.debug(f"[REDIS_STATE] Linked session {session_id[:8]}... to phone {phone_number}")
            except Exception as e:
                logger.error(f"[REDIS_STATE] Async error linking session to phone: {e}")

        # Redis is TTL-bound; also persist durably onto the attribution event
        # rows for this session so order webhooks can recover the chat session
        # by phone even after the Redis link expires or the customer checks
        # out from a different device.
        await self._apersist_phone_on_attribution_events(client_id, session_id, phone_number)

    async def _apersist_phone_on_attribution_events(
        self, client_id: str, session_id: str, phone_number: str
    ) -> None:
        try:
            from fashion_bot.database_manager import get_async_postgres_connection

            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE chat_attribution_events
                        SET phone_number = %s
                        WHERE session_id = %s AND client_id = %s AND phone_number IS NULL
                        """,
                        (phone_number, session_id, client_id),
                    )
        except Exception as e:
            logger.error(f"[STATE_CACHE] Failed to persist phone onto attribution events: {e}")
    
    def get_phone_for_session(self, client_id: str, session_id: str) -> Optional[str]:
        """
        Lookup phone number for a session.
        
        Args:
            client_id: Client UUID
            session_id: Session ID
            
        Returns:
            Phone number if linked, None otherwise
        """
        client = self._get_redis_client()
        if not client:
            return None
        
        try:
            key = self._get_session_phone_key(client_id, session_id)
            guard_result = self._redis_guard.execute(
                op_name="session_link_get",
                fn=lambda: client.get(key),
                fallback=None,
            )
            return guard_result.value
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error getting phone for session: {e}")
            return None

    async def aget_phone_for_session(self, client_id: str, session_id: str) -> Optional[str]:
        """Async variant of get_phone_for_session()."""
        client = await self._get_async_redis_client()
        if not client:
            return None

        try:
            key = self._get_session_phone_key(client_id, session_id)
            guard_result = await self._redis_guard.execute_async(
                op_name="session_link_get",
                fn=lambda: client.get(key),
                fallback=None,
            )
            return guard_result.value
        except Exception as e:
            logger.error(f"[REDIS_STATE] Async error getting phone for session: {e}")
            return None
    
    # ==================== LISTING AND STATS ====================
    
    def list_threads(
        self,
        channel: Optional[Channel] = None,
        tenant_id: Optional[str] = None
    ) -> List[str]:
        """
        List thread IDs matching the filter criteria.
        
        Args:
            channel: Filter by channel (optional)
            tenant_id: Filter by tenant (optional, requires channel)
            
        Returns:
            List of thread_id strings
        """
        client = self._get_redis_client()
        if not client:
            return []
        
        try:
            if channel and tenant_id:
                # Get user_ids from tenant index, construct full thread_ids
                index_key = self._get_index_key(channel, tenant_id)
                guard_result = self._redis_guard.execute(
                    op_name="list_threads_tenant",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                user_ids = guard_result.value or set()
                return [self.generate_thread_id(channel, tenant_id, uid) for uid in user_ids]
            elif channel:
                # Get from channel index
                index_key = self._get_index_key(channel)
                guard_result = self._redis_guard.execute(
                    op_name="list_threads_channel",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                return list(guard_result.value or set())
            else:
                # Get from global index
                index_key = self._get_index_key()
                guard_result = self._redis_guard.execute(
                    op_name="list_threads_global",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                return list(guard_result.value or set())
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error listing threads: {e}")
            return []
    
    def list_conversations(
        self,
        channel: Optional[Channel] = None,
        tenant_id: Optional[str] = None
    ) -> List[Dict]:
        """
        List conversations with full details.
        
        Args:
            channel: Filter by channel (optional)
            tenant_id: Filter by tenant (optional)
            
        Returns:
            List of conversation summary dicts
        """
        thread_ids = self.list_threads(channel, tenant_id)
        conversations = []
        
        for thread_id in thread_ids:
            state = self.get_state(thread_id)
            if state:
                try:
                    config = self.parse_thread_id(thread_id)
                    
                    # Extract last message
                    messages = state.get("messages", []) or []
                    last_msg_content = None
                    if messages:
                        last = messages[-1]
                        last_msg_content = getattr(last, 'content', str(last))
                    
                    # Build context summary
                    context = state.get("conversation_context")
                    context_summary = self._build_context_summary(context)
                    
                    conversations.append({
                        "thread_id": thread_id,
                        "channel": config.channel.value,
                        "tenant_id": config.tenant_id,
                        "user_id": config.user_id,
                        # Legacy field names for backward compatibility
                        "to_phone": config.tenant_id if config.channel == Channel.WHATSAPP else None,
                        "from_phone": config.user_id if config.channel == Channel.WHATSAPP else None,
                        "last_updated": state.get("last_message_at"),
                        "trace_id": state.get("trace_id"),
                        "needs_escalation": state.get("needs_escalation"),
                        "needs_human_agent": state.get("needs_human_agent"),
                        "is_frustrated": state.get("is_frustrated"),
                        "selected_order_id": state.get("selected_order_id"),
                        "last_message": last_msg_content,
                        "conversation_context": context_summary,
                        "source": "redis",
                    })
                except Exception as e:
                    logger.error(f"[STATE_CACHE] Error processing thread {thread_id}: {e}")
        
        return conversations

    async def amigrate_session_to_phone(
        self,
        client_id: str,
        session_id: str,
        phone_number: str
    ) -> tuple:
        """Async variant of migrate_session_to_phone()."""
        session_thread = self.generate_thread_id(Channel.WEB, client_id, session_id)
        phone_thread = self.generate_thread_id(Channel.WEB, client_id, phone_number)

        existing = await self.aget_state(phone_thread)
        if existing:
            await self.alink_session_to_phone(client_id, session_id, phone_number)
            return phone_thread, False

        session_state = await self.aget_state(session_thread)
        if session_state:
            session_state["phone_number"] = phone_number
            await self.aset_state(phone_thread, session_state)
            await self.alink_session_to_phone(client_id, session_id, phone_number)
            logger.info(f"[STATE_CACHE] Async migrated session {session_id[:8]}... to phone {phone_number}")
            return phone_thread, True

        return phone_thread, False

    async def alist_threads(
        self,
        channel: Optional[Channel] = None,
        tenant_id: Optional[str] = None
    ) -> List[str]:
        """Async variant of list_threads()."""
        client = await self._get_async_redis_client()
        if not client:
            return []

        try:
            if channel and tenant_id:
                index_key = self._get_index_key(channel, tenant_id)
                guard_result = await self._redis_guard.execute_async(
                    op_name="list_threads_tenant",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                user_ids = guard_result.value or set()
                return [self.generate_thread_id(channel, tenant_id, uid) for uid in user_ids]
            elif channel:
                index_key = self._get_index_key(channel)
                guard_result = await self._redis_guard.execute_async(
                    op_name="list_threads_channel",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                return list(guard_result.value or set())
            else:
                index_key = self._get_index_key()
                guard_result = await self._redis_guard.execute_async(
                    op_name="list_threads_global",
                    fn=lambda: client.smembers(index_key),
                    fallback=set(),
                )
                return list(guard_result.value or set())
        except Exception as e:
            logger.error(f"[REDIS_STATE] Async error listing threads: {e}")
            return []

    async def alist_conversations(
        self,
        channel: Optional[Channel] = None,
        tenant_id: Optional[str] = None
    ) -> List[Dict]:
        """Async variant of list_conversations()."""
        thread_ids = await self.alist_threads(channel, tenant_id)
        conversations = []

        for thread_id in thread_ids:
            state = await self.aget_state(thread_id)
            if state:
                try:
                    config = self.parse_thread_id(thread_id)

                    messages = state.get("messages", []) or []
                    last_msg_content = None
                    if messages:
                        last = messages[-1]
                        last_msg_content = getattr(last, 'content', str(last))

                    context = state.get("conversation_context")
                    context_summary = self._build_context_summary(context)

                    conversations.append({
                        "thread_id": thread_id,
                        "channel": config.channel.value,
                        "tenant_id": config.tenant_id,
                        "user_id": config.user_id,
                        "to_phone": config.tenant_id if config.channel == Channel.WHATSAPP else None,
                        "from_phone": config.user_id if config.channel == Channel.WHATSAPP else None,
                        "last_updated": state.get("last_message_at"),
                        "trace_id": state.get("trace_id"),
                        "needs_escalation": state.get("needs_escalation"),
                        "needs_human_agent": state.get("needs_human_agent"),
                        "is_frustrated": state.get("is_frustrated"),
                        "selected_order_id": state.get("selected_order_id"),
                        "last_message": last_msg_content,
                        "conversation_context": context_summary,
                        "source": "redis",
                    })
                except Exception as e:
                    logger.error(f"[STATE_CACHE] Async error processing thread {thread_id}: {e}")

        return conversations

    def _build_context_summary(self, context: Optional[Dict]) -> Optional[Dict]:
        """Build a summary of conversation context for display."""
        if not context:
            return None
        
        context_summary = {}
        focal = context.get("focal_entity")
        
        if context.get("topic"):
            context_summary["topic"] = context["topic"]
        if focal:
            context_summary["focal"] = f"{focal.get('entity_type')}:{focal.get('entity_value')}"
        entities = context.get("entities", [])
        if entities:
            context_summary["entities"] = len(entities)
        topics = context.get("topics", [])
        if topics:
            context_summary["open_topics"] = len([t for t in topics if t.get("status") == "open"])
        if context.get("last_skill_node"):
            context_summary["last_skill"] = context["last_skill_node"]
        actions = context.get("recent_actions", [])
        if actions:
            context_summary["actions"] = len(actions)
        
        return context_summary if context_summary else None
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics.
        
        Returns:
            Dict with cache statistics
        """
        client = self._get_redis_client()
        
        stats = {
            "redis_connected": False,
            "total_threads": 0,
            "threads_by_channel": {},
            "ttl_hours": self.ttl_hours,
            "memory_used": "N/A",
        }
        
        if not client:
            stats["fallback_store_size"] = self._fallback_store.size()
            return stats
        
        try:
            stats["redis_connected"] = True
            
            # Count threads by channel
            for channel in Channel:
                index_key = self._get_index_key(channel)
                guard_result = self._redis_guard.execute(
                    op_name=f"stats_scard_{channel.value}",
                    fn=lambda: client.scard(index_key),
                    fallback=0,
                )
                count = int(guard_result.value or 0)
                stats["threads_by_channel"][channel.value] = count
            
            # Total threads
            total_result = self._redis_guard.execute(
                op_name="stats_scard_total",
                fn=lambda: client.scard(self._get_index_key()),
                fallback=0,
            )
            stats["total_threads"] = int(total_result.value or 0)
            
            # Memory info
            info_result = self._redis_guard.execute(
                op_name="stats_memory_info",
                fn=lambda: client.info("memory"),
                fallback={},
            )
            info = info_result.value or {}
            stats["memory_used"] = info.get("used_memory_human", "N/A")
            stats["fallback_store_size"] = self._fallback_store.size()
            stats["redis_guard"] = self._redis_guard.health()
            
        except Exception as e:
            logger.error(f"[REDIS_STATE] Error getting stats: {e}")
            stats["error"] = str(e)
        
        return stats


# ==================== GLOBAL INSTANCE ====================

# Global unified state cache instance
_unified_cache: Optional[UnifiedStateCache] = None


def get_unified_cache() -> UnifiedStateCache:
    """Get or create the global unified state cache instance."""
    global _unified_cache
    if _unified_cache is None:
        _unified_cache = UnifiedStateCache(ttl_hours=STATE_CACHE_REDIS_TTL_HOURS)
    return _unified_cache


# ==================== BACKWARD COMPATIBLE FUNCTIONS ====================
# These maintain the existing API for gradual migration
# NOTE: tenant_id can now be either client_id (UUID) or source phone number
# When client_id is passed, it's used directly as the tenant_id for Redis keys

async def aget_or_create_state(
    from_phone_number: str,
    tenant_id: str = None,
    question: str = None,
    product_info: str = "",
    to_phone_number: str = None,
    force_new: bool = False
) -> Tuple[SupportState, bool]:
    """Async variant of get_or_create_state()."""
    cache = get_unified_cache()

    if not tenant_id:
        logger.warning(
            "[STATE_CACHE] Deprecated caller path: tenant_id missing, using to_phone_number as fallback tenant_id"
        )
        tenant_id = to_phone_number

    return await cache.aget_or_create_state(
        channel=Channel.WHATSAPP,
        tenant_id=tenant_id,
        user_id=from_phone_number,
        initial_message=question,
        product_info=product_info,
        client_id=tenant_id,
        force_new=force_new
    )


async def aupdate_state(from_phone_number: str, tenant_id: str, updated_state: SupportState) -> None:
    """Async variant of update_state()."""
    cache = get_unified_cache()
    thread_id = cache.generate_thread_id(Channel.WHATSAPP, tenant_id, from_phone_number)
    await cache.aupdate_state(thread_id, updated_state)


def cleanup_expired_states() -> int:
    """
    Clean up expired states.
    
    BACKWARD COMPATIBLE: Redis TTL handles expiry automatically.
    This function is now a no-op but kept for API compatibility.
    """
    # Redis TTL handles expiry automatically, nothing to clean up
    logger.debug("[STATE_CACHE] cleanup_expired_states called - Redis TTL handles expiry automatically")
    return 0


def get_cache_stats() -> Dict[str, Any]:
    """Get cache statistics using the global cache instance."""
    cache = get_unified_cache()
    stats = cache.get_stats()
    
    # Add backward-compatible field names
    stats["total_conversations"] = stats.get("total_threads", 0)
    stats["redis_state_count"] = stats.get("total_threads", 0)
    
    return stats


def list_conversations() -> List[Dict]:
    """List conversation summaries using the global cache instance."""
    cache = get_unified_cache()
    return cache.list_conversations()


# ==================== ORDER CREATION DEDUP HELPERS ====================
# Standalone Redis keys (not inside SupportState) keyed by client_id + phone.
# This keeps dedup state independent of the graph state object.

ORDER_DEDUP_TTL_SECONDS = 120  # 2-minute dedup window


async def aset_order_dedup(
    client_id: str,
    phone: str,
    order_id: str,
    product_link: str = "",
    focal_entity_id: str = "",
    size: str = "",
) -> None:
    import time as _time, json as _json
    try:
        cache = get_unified_cache()
        rc = await cache._get_async_redis_client()
        if not rc:
            return
        key = f"dedup:order:{client_id}:{phone}"
        payload = _json.dumps({
            "order_id": order_id,
            "product_link": product_link,
            "focal_entity_id": focal_entity_id,
            "size": size,
            "ts": _time.time(),
        })
        guard_result = await cache._redis_guard.execute_async(
            op_name="order_dedup_set",
            fn=lambda: rc.set(key, payload, ex=ORDER_DEDUP_TTL_SECONDS),
            fallback=False,
        )
        if guard_result.ok:
            logger.info(f"[ORDER_DEDUP] SET {key} → order_id={order_id}")
        else:
            logger.warning(f"[ORDER_DEDUP] SET degraded for {key}: {guard_result.error}")
    except Exception as exc:
        logger.warning(f"[ORDER_DEDUP] aset_order_dedup failed (non-blocking): {exc}")


async def aget_order_dedup(client_id: str, phone: str) -> Optional[Dict]:
    import json as _json
    try:
        cache = get_unified_cache()
        rc = await cache._get_async_redis_client()
        if not rc:
            return None
        key = f"dedup:order:{client_id}:{phone}"
        guard_result = await cache._redis_guard.execute_async(
            op_name="order_dedup_get",
            fn=lambda: rc.get(key),
            fallback=None,
        )
        raw = guard_result.value
        if raw:
            return _json.loads(raw)
        return None
    except Exception as exc:
        logger.warning(f"[ORDER_DEDUP] aget_order_dedup failed (non-blocking): {exc}")
        return None


async def aget_state_by_numbers(from_phone_number: str, tenant_id: str) -> Optional[SupportState]:
    """Async variant of get_state_by_numbers()."""
    cache = get_unified_cache()
    thread_id = cache.generate_thread_id(Channel.WHATSAPP, tenant_id, from_phone_number)
    return await cache.aget_state(thread_id)
