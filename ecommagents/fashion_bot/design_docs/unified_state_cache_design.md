# Unified State Cache Design for Multi-Channel Support

> **Status**: 📋 DESIGN DOCUMENT  
> **Created**: January 11, 2026  
> **Author**: Engineering Team  
> **Redis Provider**: Upstash (RedisJSON ✅, RediSearch ❌)

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Problem Statement](#2-problem-statement)
3. [Current Architecture](#3-current-architecture)
4. [Proposed Architecture](#4-proposed-architecture)
5. [Unified Thread ID Strategy](#5-unified-thread-id-strategy)
6. [Web Chat Session Management](#6-web-chat-session-management)
7. [Redis Key Patterns](#7-redis-key-patterns)
8. [Implementation Details](#8-implementation-details)
9. [Serialization & Deserialization](#9-serialization--deserialization)
10. [Migration Strategy](#10-migration-strategy)
11. [API Reference](#11-api-reference)

---

## 1. Executive Summary

This document outlines a unified state management architecture for the Fashion Bot that:

- ✅ **Survives server restarts** - All state stored in Redis (Upstash)
- ✅ **Supports multiple channels** - WhatsApp, Web Chat, Instagram (future), Streamlit
- ✅ **Unified key strategy** - Consistent `{channel}:{tenant_id}:{user_id}` pattern
- ✅ **Web chat persistence** - Sessions now stored in Redis (not in-memory)
- ✅ **Session-to-phone migration** - Seamless transition when user provides phone
- ✅ **Upstash compatible** - Uses RedisJSON without RediSearch dependency

### Why Not `langgraph-checkpoint-redis`?

The `langgraph-checkpoint-redis` library requires **RediSearch** for indexing, which is **not available in Upstash**. This design provides equivalent functionality using native Redis commands.

---

## 2. Problem Statement

### Current Issues

| Issue | Impact | Affected Channel |
|-------|--------|------------------|
| **Web chat sessions in-memory only** | Lost on server restart | Web Chat |
| **Inconsistent key patterns** | Hard to maintain/query | All |
| **No multi-channel support** | Can't add Instagram easily | Future channels |
| **Manual state updates scattered** | Bug-prone, inconsistent | All |

### Goals

1. **Persistence**: All conversation state survives server restarts
2. **Unification**: Single key strategy for all channels
3. **Extensibility**: Easy to add new channels (Instagram, etc.)
4. **Compatibility**: Works with Upstash Redis (no RediSearch)

---

## 3. Current Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    CURRENT ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐    ┌─────────────────┐                    │
│  │ gupshup_webhook │    │ websocket_chat  │                    │
│  │   (WhatsApp)    │    │   (Web Chat)    │                    │
│  └────────┬────────┘    └────────┬────────┘                    │
│           │                      │                              │
│           ▼                      ▼                              │
│  ┌─────────────────┐    ┌─────────────────┐                    │
│  │  state_cache.py │    │ active_sessions │                    │
│  │  (Redis + Mem)  │    │  (IN-MEMORY!)   │  ⚠️ PROBLEM!       │
│  └────────┬────────┘    └─────────────────┘                    │
│           │                                                     │
│           ▼                                                     │
│  ┌─────────────────┐                                           │
│  │  Upstash Redis  │                                           │
│  │ state:{to}:{from}                                           │
│  └─────────────────┘                                           │
│                                                                 │
│  ❌ Web chat sessions LOST on server restart!                  │
│  ❌ Different key patterns for different channels              │
│  ❌ No unified approach for future channels                    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Current Key Patterns

| Channel | Storage | Key Pattern | Survives Restart? |
|---------|---------|-------------|-------------------|
| WhatsApp | Redis | `state:{to_phone}:{from_phone}` | ✅ Yes |
| Web Chat | In-Memory | `active_sessions[session_id]` | ❌ No |
| Streamlit | Session State | `st.session_state` | ❌ No |

---

## 4. Proposed Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│              PROPOSED ARCHITECTURE (Upstash Compatible)                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │  WhatsApp   │  │  Web Chat   │  │  Instagram  │  │  Streamlit  │        │
│  │  Webhook    │  │  WebSocket  │  │  (Future)   │  │    App      │        │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘        │
│         │                │                │                │                │
│         ▼                ▼                ▼                ▼                │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │                 Unified Thread Manager                           │      │
│  │  ┌────────────────────────────────────────────────────────────┐ │      │
│  │  │ • generate_thread_id(channel, tenant_id, user_id)          │ │      │
│  │  │ • get_or_create_state(...)                                 │ │      │
│  │  │ • migrate_session_to_phone(...)                            │ │      │
│  │  └────────────────────────────────────────────────────────────┘ │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                │                                            │
│                                ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │              UnifiedStateCache (Redis-First)                     │      │
│  │                                                                  │      │
│  │   • Uses unified thread_id pattern across all channels          │      │
│  │   • RedisJSON for state storage (Upstash compatible)           │      │
│  │   • Redis SET for thread indexing (no RediSearch needed)       │      │
│  │   • TTL-based expiry (24 hours, refreshed on activity)         │      │
│  │   • Web chat sessions NOW in Redis!                            │      │
│  │                                                                  │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                │                                            │
│                                ▼                                            │
│  ┌──────────────────────────────────────────────────────────────────┐      │
│  │                      Upstash Redis                               │      │
│  │                                                                  │      │
│  │   ┌─────────────────────────────────────────────────────────┐   │      │
│  │   │ State Storage (RedisJSON)                               │   │      │
│  │   │   state:{channel}:{tenant_id}:{user_id}                 │   │      │
│  │   └─────────────────────────────────────────────────────────┘   │      │
│  │                                                                  │      │
│  │   ┌─────────────────────────────────────────────────────────┐   │      │
│  │   │ Thread Indices (Redis SET)                              │   │      │
│  │   │   thread_index:all, thread_index:{channel}:{tenant}     │   │      │
│  │   └─────────────────────────────────────────────────────────┘   │      │
│  │                                                                  │      │
│  │   ┌─────────────────────────────────────────────────────────┐   │      │
│  │   │ Session-Phone Mapping                                   │   │      │
│  │   │   session_phone:{client_id}:{session_id}                │   │      │
│  │   └─────────────────────────────────────────────────────────┘   │      │
│  │                                                                  │      │
│  └──────────────────────────────────────────────────────────────────┘      │
│                                                                             │
│  ✅ All channels persist to Redis                                          │
│  ✅ Survives server restarts                                               │
│  ✅ Unified key pattern                                                    │
│  ✅ Easy to add new channels                                               │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. Unified Thread ID Strategy

### Thread ID Format

```
{channel}:{tenant_id}:{user_id}
```

| Component | Description | Examples |
|-----------|-------------|----------|
| `channel` | Communication channel | `whatsapp`, `web`, `instagram`, `streamlit` |
| `tenant_id` | Business/client identifier | Phone number or client UUID |
| `user_id` | User identifier | Phone number, session ID, or platform user ID |

### Channel-Specific Mapping

| Channel | Tenant ID | User ID | Example Thread ID |
|---------|-----------|---------|-------------------|
| **WhatsApp** | `to_phone` (source) | `from_phone` (user) | `whatsapp:15557872987:919876543210` |
| **Web Chat** | `client_id` (UUID) | `session_id` or `phone` | `web:abc123-uuid:sess-456` |
| **Web (identified)** | `client_id` (UUID) | `phone_number` | `web:abc123-uuid:919876543210` |
| **Instagram** | `ig_business_id` | `ig_user_id` | `instagram:17841234567890:1784098765432` |
| **Streamlit** | `client_id` | `session_id` | `streamlit:abc123-uuid:st-session-789` |

### Thread ID Generator

```python
from enum import Enum

class Channel(Enum):
    WHATSAPP = "whatsapp"
    WEB = "web"
    INSTAGRAM = "instagram"
    STREAMLIT = "streamlit"

def generate_thread_id(channel: Channel, tenant_id: str, user_id: str) -> str:
    """Generate unified thread_id for any channel."""
    return f"{channel.value}:{tenant_id}:{user_id}"

# Examples
generate_thread_id(Channel.WHATSAPP, "15557872987", "919876543210")
# → "whatsapp:15557872987:919876543210"

generate_thread_id(Channel.WEB, "abc123-uuid", "session-456")
# → "web:abc123-uuid:session-456"
```

---

## 6. Web Chat Session Management

### The Challenge

Web chat users may:
1. Start chatting **anonymously** (only `session_id` available)
2. Later provide their **phone number** (via form or order flow)
3. Return on a **different device** with the same phone

### Session Lifecycle

```
┌─────────────────────────────────────────────────────────────────┐
│                WEB CHAT SESSION LIFECYCLE                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  PHASE 1: Anonymous Session                                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ thread_id = web:{client_id}:{session_id}                 │  │
│  │ Example: web:abc123:sess-456                             │  │
│  │ TTL: 24 hours                                            │  │
│  └──────────────────────────────────────────────────────────┘  │
│                         │                                       │
│                         ▼                                       │
│  User provides phone number (form, order, support request)      │
│                         │                                       │
│                         ▼                                       │
│  PHASE 2: Session Migration                                     │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ 1. Check if phone-based thread exists                    │  │
│  │ 2. If yes: Merge/switch to phone thread                  │  │
│  │ 3. If no: Migrate session state to phone-based thread    │  │
│  │ 4. Store session→phone mapping for future lookups        │  │
│  │ 5. Old session thread expires via TTL                    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                         │                                       │
│                         ▼                                       │
│  PHASE 3: Identified Session                                    │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ thread_id = web:{client_id}:{phone_number}               │  │
│  │ Example: web:abc123:919876543210                         │  │
│  │ Benefits:                                                │  │
│  │   • Cross-device continuity (same phone = same thread)  │  │
│  │   • Can link to order history                           │  │
│  │   • Persists across browser sessions                    │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Migration Logic

```python
async def migrate_session_to_phone(
    client_id: str,
    session_id: str,
    phone_number: str
) -> Tuple[str, bool]:
    """
    Migrate web session state to phone-based thread.
    
    Returns:
        (new_thread_id, was_migrated)
    """
    session_thread = f"web:{client_id}:{session_id}"
    phone_thread = f"web:{client_id}:{phone_number}"
    
    # Check if phone thread already exists (returning user)
    existing = await get_state(phone_thread)
    if existing:
        return phone_thread, False  # Use existing, no migration
    
    # Get session state and migrate
    session_state = await get_state(session_thread)
    if session_state:
        session_state["phone_number"] = phone_number
        await set_state(phone_thread, session_state)
        await link_session_to_phone(client_id, session_id, phone_number)
        return phone_thread, True
    
    # No existing state, create fresh
    return phone_thread, False
```

---

## 7. Redis Key Patterns

### State Storage (RedisJSON)

```
Key:   state:{channel}:{tenant_id}:{user_id}
Value: JSON document containing full SupportState
TTL:   24 hours (refreshed on every access)

Examples:
  state:whatsapp:15557872987:919876543210
  state:web:abc123-uuid:session-456-uuid
  state:web:abc123-uuid:919876543210
  state:instagram:17841234567890:1784098765432
```

### Thread Indices (Redis SET)

Used for listing conversations without RediSearch:

```
# Global index of all active threads
Key:   thread_index:all
Value: SET of thread_ids

# Per-tenant index (for dashboard filtering)
Key:   thread_index:{channel}:{tenant_id}
Value: SET of user_ids

Examples:
  thread_index:all
    → {"whatsapp:15557872987:919876543210", "web:abc123:sess-456", ...}
  
  thread_index:whatsapp:15557872987
    → {"919876543210", "919812345678", ...}
  
  thread_index:web:abc123-uuid
    → {"session-456", "919876543210", ...}
```

### Session-Phone Mapping

For cross-reference when user provides phone:

```
Key:   session_phone:{client_id}:{session_id}
Value: phone_number (string)
TTL:   24 hours

Example:
  session_phone:abc123-uuid:sess-456 → "919876543210"
```

### Redis Commands Used

All commands are **Upstash compatible** (no RediSearch):

```bash
# State Storage (RedisJSON)
JSON.SET state:whatsapp:15557872987:919876543210 $ '{"messages": [...], ...}'
JSON.GET state:whatsapp:15557872987:919876543210
EXPIRE state:whatsapp:15557872987:919876543210 86400
DEL state:whatsapp:15557872987:919876543210

# Thread Indexing (SET)
SADD thread_index:all whatsapp:15557872987:919876543210
SADD thread_index:whatsapp:15557872987 919876543210
SMEMBERS thread_index:all
SMEMBERS thread_index:whatsapp:15557872987
SREM thread_index:whatsapp:15557872987 919876543210

# Session-Phone Mapping (STRING)
SETEX session_phone:abc123:sess456 86400 919876543210
GET session_phone:abc123:sess456
```

---

## 8. Implementation Details

### New Module Structure

```
fashion_bot/
├── state_cache.py              # ENHANCED: UnifiedStateCache class
│   ├── Channel (Enum)
│   ├── ThreadConfig (dataclass)
│   ├── UnifiedStateCache (class)
│   │   ├── generate_thread_id()
│   │   ├── get_state()
│   │   ├── set_state()
│   │   ├── get_or_create_state()
│   │   ├── update_state()
│   │   ├── migrate_session_to_phone()
│   │   ├── list_threads()
│   │   └── list_conversations()
│   └── Backward compatible functions
│
├── gupshup_webhook.py          # MODIFIED: Use unified cache
├── websocket_chat.py           # MODIFIED: Use Redis instead of in-memory
├── streamlit_app.py            # MODIFIED: Use unified cache
└── graph_context_meta.py       # NO CHANGE (graph invocation unchanged)
```

### UnifiedStateCache Class Overview

```python
class UnifiedStateCache:
    """
    Redis-backed state cache supporting all channels.
    Compatible with Upstash Redis (uses RedisJSON, no RediSearch).
    """
    
    # Key prefixes
    STATE_PREFIX = "state"
    INDEX_PREFIX = "thread_index"
    SESSION_PHONE_PREFIX = "session_phone"
    
    def __init__(self, redis_client, ttl_hours: int = 24):
        self.redis = redis_client
        self.ttl_seconds = ttl_hours * 3600
    
    # Thread ID helpers
    @staticmethod
    def generate_thread_id(channel, tenant_id, user_id) -> str: ...
    
    # Core state operations
    async def get_state(self, thread_id) -> Optional[SupportState]: ...
    async def set_state(self, thread_id, state) -> bool: ...
    async def delete_state(self, thread_id) -> bool: ...
    
    # High-level API
    async def get_or_create_state(self, channel, tenant_id, user_id, ...) -> Tuple[SupportState, bool]: ...
    async def update_state(self, thread_id, state) -> None: ...
    
    # Web chat session management
    async def migrate_session_to_phone(self, client_id, session_id, phone) -> Tuple[str, bool]: ...
    async def link_session_to_phone(self, client_id, session_id, phone) -> None: ...
    async def get_phone_for_session(self, client_id, session_id) -> Optional[str]: ...
    
    # Listing and stats
    async def list_threads(self, channel=None, tenant_id=None) -> List[str]: ...
    async def list_conversations(self, channel=None, tenant_id=None) -> List[Dict]: ...
    async def get_stats(self) -> Dict: ...
```

### Flow: WhatsApp Message

```python
# In gupshup_webhook.py

async def process_webhook(sender_phone: str, source_phone: str, message: str):
    # 1. Get unified cache
    cache = await get_unified_cache()
    
    # 2. Generate thread_id
    thread_id = cache.generate_thread_id(
        Channel.WHATSAPP, source_phone, sender_phone
    )
    
    # 3. Get or create state
    state, is_new = await cache.get_or_create_state(
        channel=Channel.WHATSAPP,
        tenant_id=source_phone,
        user_id=sender_phone,
        initial_message=message,
        client_id=get_client_id(source_phone),
    )
    
    # 4. Invoke graph (unchanged)
    result = await graph.ainvoke(state)
    
    # 5. Save updated state
    await cache.update_state(thread_id, result)
    
    # 6. Return response
    return result.get("customer_message")
```

### Flow: Web Chat Message

```python
# In websocket_chat.py

async def handle_message(client_id: str, session_id: str, message: str, phone: Optional[str]):
    cache = await get_unified_cache()
    
    # Determine thread (with potential migration)
    if phone:
        thread_id, was_migrated = await cache.migrate_session_to_phone(
            client_id, session_id, phone
        )
        user_id = phone
    else:
        thread_id = cache.generate_thread_id(Channel.WEB, client_id, session_id)
        user_id = session_id
    
    # Get or create state
    state, is_new = await cache.get_or_create_state(
        channel=Channel.WEB,
        tenant_id=client_id,
        user_id=user_id,
        initial_message=message,
        client_id=client_id,
        session_id=session_id,
    )
    
    # Invoke graph
    result = await graph.ainvoke(state)
    
    # Save state
    await cache.update_state(thread_id, result)
    
    return result
```

---

## 9. Serialization & Deserialization

### Overview

`SupportState` contains LangChain message objects and datetime fields that aren't natively JSON-serializable. The serialization layer handles these transformations for Redis storage.

```
┌─────────────────────────────────────────────────────────────────┐
│                SERIALIZATION FLOW                               │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   SupportState (Python)                                         │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ messages: [HumanMessage(...), AIMessage(...)]            │ │
│   │ last_updated: datetime(2026, 1, 12, 10, 30)             │ │
│   │ phone_number: "919876543210"                            │ │
│   │ intent: "order_status"                                  │ │
│   │ ...                                                     │ │
│   └──────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼ serialize_state()                     │
│                                                                 │
│   JSON String (Redis)                                          │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ {                                                        │ │
│   │   "messages": [                                         │ │
│   │     {"type": "HumanMessage", "content": "...",          │ │
│   │      "additional_kwargs": {...}},                       │ │
│   │     {"type": "AIMessage", "content": "...",             │ │
│   │      "additional_kwargs": {...}}                        │ │
│   │   ],                                                    │ │
│   │   "last_updated": "2026-01-12T10:30:00",               │ │
│   │   "phone_number": "919876543210",                       │ │
│   │   "intent": "order_status",                             │ │
│   │   ...                                                   │ │
│   │ }                                                       │ │
│   └──────────────────────────────────────────────────────────┘ │
│                         │                                       │
│                         ▼ RedisJSON                             │
│                                                                 │
│   Upstash Redis                                                │
│   ┌──────────────────────────────────────────────────────────┐ │
│   │ JSON.SET state:whatsapp:15557872987:919876543210 $ '...' │ │
│   └──────────────────────────────────────────────────────────┘ │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Message Serialization

LangChain messages (HumanMessage, AIMessage, SystemMessage) are converted to dictionaries:

```python
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage

def serialize_message(msg) -> dict:
    """Convert LangChain message to serializable dict."""
    return {
        "type": type(msg).__name__,  # "HumanMessage", "AIMessage", "SystemMessage"
        "content": msg.content,
        "additional_kwargs": msg.additional_kwargs or {}
    }

# Example:
# HumanMessage(content="What's my order status?")
# → {"type": "HumanMessage", "content": "What's my order status?", "additional_kwargs": {}}
```

### Message Deserialization

Reconstruct the appropriate LangChain message type from the stored dict:

```python
def deserialize_message(msg_dict: dict):
    """Reconstruct LangChain message from dict."""
    msg_type = msg_dict.get("type", "HumanMessage")
    content = msg_dict.get("content", "")
    kwargs = msg_dict.get("additional_kwargs", {})
    
    if msg_type == "AIMessage":
        return AIMessage(content=content, additional_kwargs=kwargs)
    elif msg_type == "SystemMessage":
        return SystemMessage(content=content, additional_kwargs=kwargs)
    else:
        return HumanMessage(content=content, additional_kwargs=kwargs)

# Example:
# {"type": "AIMessage", "content": "Your order is shipped!", "additional_kwargs": {}}
# → AIMessage(content="Your order is shipped!")
```

### State Serialization

Full `SupportState` serialization with special handling for messages and datetime:

```python
import json
from datetime import datetime
from fashion_bot.schema import SupportState

def serialize_state(state: SupportState) -> str:
    """Serialize SupportState to JSON string for Redis storage."""
    data = {}
    
    for key, value in state.__dict__.items():
        if key == "messages" and value:
            # Serialize LangChain messages list
            data[key] = [serialize_message(msg) for msg in value]
        elif isinstance(value, datetime):
            # Convert datetime to ISO format string
            data[key] = value.isoformat()
        else:
            # All other fields serialize directly
            data[key] = value
    
    return json.dumps(data, default=str)  # default=str catches any missed types
```

### State Deserialization

Reconstruct `SupportState` from Redis JSON:

```python
def deserialize_state(json_str: str) -> SupportState:
    """Deserialize JSON string from Redis to SupportState."""
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to deserialize state JSON: {e}")
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
    
    # Note: datetime fields remain as strings (SupportState should handle)
    # Or parse them explicitly if needed:
    # if "last_updated" in data and isinstance(data["last_updated"], str):
    #     data["last_updated"] = datetime.fromisoformat(data["last_updated"])
    
    return SupportState(**data)
```

### Full Example: Round-Trip

```python
from fashion_bot.schema import SupportState
from langchain_core.messages import HumanMessage, AIMessage

# 1. Create state
state = SupportState(
    messages=[
        HumanMessage(content="Where is my order?"),
        AIMessage(content="Let me check that for you...")
    ],
    phone_number="919876543210",
    intent="order_status",
    order_id="ORD-12345"
)

# 2. Serialize for Redis
json_str = serialize_state(state)
# → '{"messages": [{"type": "HumanMessage", "content": "Where is my order?", ...}, ...], ...}'

# 3. Store in Redis
await redis.execute_command("JSON.SET", "state:whatsapp:15557872987:919876543210", "$", json_str)

# 4. Retrieve from Redis
stored_json = await redis.execute_command("JSON.GET", "state:whatsapp:15557872987:919876543210")

# 5. Deserialize back to SupportState
restored_state = deserialize_state(stored_json)
# → SupportState with HumanMessage and AIMessage objects reconstructed
```

### Type Handling Summary

| Python Type | Serialized Format | Deserialized Type |
|-------------|-------------------|-------------------|
| `HumanMessage` | `{"type": "HumanMessage", "content": "...", ...}` | `HumanMessage` |
| `AIMessage` | `{"type": "AIMessage", "content": "...", ...}` | `AIMessage` |
| `SystemMessage` | `{"type": "SystemMessage", "content": "...", ...}` | `SystemMessage` |
| `datetime` | ISO format string `"2026-01-12T10:30:00"` | `str` (or parse if needed) |
| `str`, `int`, `bool`, `list`, `dict` | Native JSON | Same type |
| `None` | `null` | `None` |

### Integration with UnifiedStateCache

The serialization functions are used internally by the cache:

```python
class UnifiedStateCache:
    
    async def set_state(self, thread_id: str, state: SupportState) -> bool:
        """Save state to Redis."""
        key = f"{self.STATE_PREFIX}:{thread_id}"
        json_str = serialize_state(state)  # ← Serialization happens here
        
        await self.redis.execute_command("JSON.SET", key, "$", json_str)
        await self.redis.expire(key, self.ttl_seconds)
        
        # Update thread index
        await self._add_to_index(thread_id)
        return True
    
    async def get_state(self, thread_id: str) -> Optional[SupportState]:
        """Get state from Redis."""
        key = f"{self.STATE_PREFIX}:{thread_id}"
        json_str = await self.redis.execute_command("JSON.GET", key)
        
        if not json_str:
            return None
        
        state = deserialize_state(json_str)  # ← Deserialization happens here
        
        # Refresh TTL on access
        await self.redis.expire(key, self.ttl_seconds)
        return state
```

---

## 10. Migration Strategy

### Phase 1: Preparation

- [ ] Add `Channel` enum and `ThreadConfig` dataclass
- [ ] Create `UnifiedStateCache` class
- [ ] Add backward-compatible wrapper functions
- [ ] Write unit tests

### Phase 2: WhatsApp Migration

- [ ] Update `gupshup_webhook.py` to use new cache
- [ ] Deploy with feature flag
- [ ] Monitor for issues
- [ ] Remove feature flag

### Phase 3: Web Chat Migration

- [ ] Update `websocket_chat.py` to use Redis
- [ ] Remove `active_sessions` in-memory dict
- [ ] Test session-to-phone migration
- [ ] Deploy and monitor

### Phase 4: Cleanup

- [ ] Update `streamlit_app.py`
- [ ] Update dashboard to use new listing API
- [ ] Remove old `StateCache` class
- [ ] Update documentation

### Data Migration Script

```python
async def migrate_existing_states():
    """One-time migration from old key pattern to new."""
    old_cache = StateCache()  # Old implementation
    new_cache = UnifiedStateCache(redis_client)
    
    for to_phone, from_map in old_cache.state_cache.items():
        for from_phone, state in from_map.items():
            thread_id = new_cache.generate_thread_id(
                Channel.WHATSAPP, to_phone, from_phone
            )
            await new_cache.set_state(thread_id, state)
    
    logger.info("Migration complete")
```

---

## 11. API Reference

### Channel Enum

```python
class Channel(Enum):
    WHATSAPP = "whatsapp"
    WEB = "web"
    INSTAGRAM = "instagram"
    STREAMLIT = "streamlit"
```

### ThreadConfig Dataclass

```python
@dataclass
class ThreadConfig:
    channel: Channel
    tenant_id: str
    user_id: str
    
    @property
    def thread_id(self) -> str:
        return f"{self.channel.value}:{self.tenant_id}:{self.user_id}"
    
    @classmethod
    def from_thread_id(cls, thread_id: str) -> "ThreadConfig": ...
```

### UnifiedStateCache Methods

| Method | Description | Returns |
|--------|-------------|---------|
| `generate_thread_id(channel, tenant_id, user_id)` | Create thread ID | `str` |
| `get_state(thread_id)` | Get state from Redis | `Optional[SupportState]` |
| `set_state(thread_id, state)` | Save state to Redis | `bool` |
| `delete_state(thread_id)` | Delete state | `bool` |
| `get_or_create_state(channel, tenant_id, user_id, ...)` | Get existing or create new | `Tuple[SupportState, bool]` |
| `update_state(thread_id, state)` | Update after graph execution | `None` |
| `migrate_session_to_phone(client_id, session_id, phone)` | Migrate web session | `Tuple[str, bool]` |
| `link_session_to_phone(client_id, session_id, phone)` | Store mapping | `None` |
| `get_phone_for_session(client_id, session_id)` | Lookup phone | `Optional[str]` |
| `list_threads(channel?, tenant_id?)` | List thread IDs | `List[str]` |
| `list_conversations(channel?, tenant_id?)` | List with details | `List[Dict]` |
| `get_stats()` | Cache statistics | `Dict` |

### Backward Compatible Functions

These maintain the existing API for gradual migration:

```python
def get_or_create_state(from_phone, to_phone, question, product_info, force_new) -> Tuple[SupportState, bool]
def update_state(from_phone, to_phone, updated_state) -> None
def list_conversations() -> List[Dict]
def get_cache_stats() -> Dict
```

---

## Summary

### Key Benefits

| Aspect | Before | After |
|--------|--------|-------|
| **Web chat persistence** | ❌ In-memory (lost on restart) | ✅ Redis (survives restart) |
| **Key pattern** | Different per channel | Unified `{channel}:{tenant}:{user}` |
| **Multi-channel** | Hard to extend | Easy to add channels |
| **Session migration** | Not supported | ✅ Session → Phone |
| **Upstash compatible** | ✅ Yes | ✅ Yes (no RediSearch) |

### Future Considerations

1. **Instagram Integration**: When adding Instagram, simply use `Channel.INSTAGRAM` with `ig_business_id` and `ig_user_id`

2. **Cross-Channel Identity**: If same phone used on WhatsApp and Web, they remain separate threads (different contexts)

3. **RediSearch Migration**: If migrating to Redis Cloud with RediSearch, the key patterns are compatible with `langgraph-checkpoint-redis`

---

## Appendix: Environment Variables

```bash
# Redis Configuration
REDIS_URL=rediss://default:xxx@xxx.upstash.io:6379

# TTL Configuration (optional, default 24 hours)
STATE_CACHE_REDIS_TTL_HOURS=24
```
