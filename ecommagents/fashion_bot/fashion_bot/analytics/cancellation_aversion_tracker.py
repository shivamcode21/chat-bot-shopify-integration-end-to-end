"""
Cancellation Aversion Tracker
==============================
Detects and records events where a customer expressed cancellation intent
but the chatbot averted the cancellation within the active session window.

Architecture
------------
Called as asyncio.create_task() from gupshup_webhook.process_webhook_payload()
immediately after call_main_bot() returns.  Zero latency impact on the webhook
response path — graph execution and send_message() are never blocked.

Detection strategy
------------------
  Layer 1  — Graph result state signals (free, already computed by graph):
               waiting_for_cancellation_reason, detected_intents, last_tool_calls
  Layer 2  — LLM micro-classifier on the user message (only when Layer 1 misses).
               Language-agnostic, no hardcoded keywords.
  Layer 3  — Retroactive backfill: if a cancel tool fires with no prior intent
               detected, we open and immediately close the event.

Outcome signals
---------------
  Hard (real-time close):  action tools from AVERSION_TOOLS / CANCELLATION_TOOLS
  Soft (accumulated):      context_shift, intent_reasserted — fed to LLM classifier
                           that runs at session end (90 min TTL).

Session state
-------------
  Redis key: cancellation_intent:{client_id}:{phone_number}
  TTL:       5400 seconds (90 min — matches context window)
  Value:     JSON with event_id, turn_count, accumulated signals
  Fallback:  DB query when Redis is unavailable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import certifi
from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)

logger = logging.getLogger("cancellation_aversion_tracker")

# ─── Redis ────────────────────────────────────────────────────────────────────
try:
    import redis.asyncio as _redis_lib
except ImportError:
    _redis_lib = None  # type: ignore

REDIS_URL: str = (
    os.getenv("REDIS_URL")
    or os.getenv("REDIS_CONNECTION_STRING")
    or "redis://localhost:6379/0"
)
INTENT_TTL_SECONDS: int = 5400  # 90 minutes
REDIS_KEY_PREFIX: str = "cancellation_intent"

_redis_client: Optional[Any] = None
_redis_client_lock: Optional[asyncio.Lock] = None


async def _aget_redis_client() -> Optional[Any]:
    """Async Redis singleton — same TLS pattern as config_manager._get_async_redis_client()."""
    global _redis_client, _redis_client_lock
    if _redis_client is not None:
        return _redis_client
    if _redis_lib is None:
        logger.warning("[TRACKER] redis package not installed; Redis unavailable.")
        return None
    if _redis_client_lock is None:
        _redis_client_lock = asyncio.Lock()
    async with _redis_client_lock:
        # Double-check after acquiring lock
        if _redis_client is not None:
            return _redis_client
        try:
            kwargs: Dict[str, Any] = {"decode_responses": True}
            if REDIS_URL.lower().startswith("rediss://"):
                kwargs["ssl_ca_certs"] = certifi.where()
                if os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"):
                    kwargs["ssl_cert_reqs"] = None
            client = _redis_lib.Redis.from_url(REDIS_URL, **kwargs)
            await client.ping()
            _redis_client = client
            logger.info("[TRACKER] Async Redis client created and connected.")
        except Exception as exc:
            logger.warning(f"[TRACKER] Redis unavailable: {exc}")
            _redis_client = None
    return _redis_client


# ─── Tool signal tables ───────────────────────────────────────────────────────

# Tools that confirm a cancellation was COMPLETED
CANCELLATION_TOOLS: Set[str] = {
    "cancel_order_tool",
}

# Tools that confirm the cancellation was AVERTED (event_type = cancellation_aversion)
# Maps tool_name → (status, resolution)
#
# This dictionary includes ALL post-order update tools that can avert cancellations.
# Automatically discovered from tool_factory.py and tools.py.
AVERSION_TOOLS: Dict[str, Tuple[str, str]] = {
    # Exchange/Return tools
    "initiate_exchange_tool":      ("averted",   "exchange"),
    "create_exchange_order_tool":  ("averted",   "exchange"),
    "initiate_return_tool":        ("averted",   "return"),
    
    # Address update tools (multiple aliases)
    "update_order_address":        ("averted",   "address_fix"),   # tool_factory.py (cancel_or_update_tools_factory)
    "update_order_address_tool":   ("averted",   "address_fix"),   # alias used in some agents
    "update_shopify_order_tool":   ("averted",   "address_fix"),   # tools.py (generic, can update address)
    
    # Size/Product change tools
    "update_order_size_tool":      ("averted",   "product_change"), # tool_factory.py - size change
    "update_order_size":           ("averted",   "product_change"), # tools.py - alternative name
    "change_order_product_tool":   ("averted",   "product_change"), # tool_factory.py - product change
    
    # Contact update tools (phone/email)
    "update_order_phone_number_tool": ("averted", "contact_fix"),   # tool_factory.py - phone update
    "update_order_phone_number":     ("averted", "contact_fix"),   # tools.py - alternative name
    "update_order_email_tool":       ("averted", "contact_fix"),   # tool_factory.py - email update
    "update_order_email":            ("averted", "contact_fix"),   # tools.py - alternative name
    
    # Escalation
    "escalate_to_agent":           ("escalated", "escalation"),
}

# Tools that signal the cancellation flow was entered (intent signals)
INTENT_TOOLS: Set[str] = {
    "get_order_cancellation_reasons",
}

# ── RTO Aversion tools ────────────────────────────────────────────────────────
# When these tools fire WITHOUT a prior open cancellation intent, they represent
# an implicit RTO (Return To Origin) aversion: the bot fixed a delivery problem
# that would have caused the package to be returned / the order to be cancelled.
#
# Maps tool_name → resolution label stored in DB
#
# This dictionary includes ALL post-order update tools that can prevent RTO.
# Automatically discovered from tool_factory.py and tools.py.
RTO_AVERSION_TOOLS: Dict[str, str] = {
    # Address corrections — prevents carrier returning due to bad address
    "update_order_address":          "address_update",   # tool_factory.py (cancel_or_update_tools_factory)
    "update_order_address_tool":     "address_update",   # alias
    "update_order_shiprocket":       "address_update",   # direct shiprocket update (tools.py)
    "update_shopify_order_tool":     "order_detail_update",  # tools.py (generic: address / phone / instructions)
    
    # Phone / contact corrections — prevents carrier giving up on delivery
    "update_order_phone_number_tool": "phone_update",    # tool_factory.py
    "update_order_phone_number":      "phone_update",    # tools.py - alternative name
    
    # Email corrections — prevents delivery issues
    "update_order_email_tool":       "contact_update",  # tool_factory.py
    "update_order_email":             "contact_update",  # tools.py - alternative name
    
    # Name corrections — prevents delivery issues
    # Note: Name updates are typically part of address updates, but if there's a separate tool, add it here
    
    # Size/Product changes — prevents wrong item returns
    "update_order_size_tool":        "product_update",  # tool_factory.py
    "update_order_size":             "product_update",  # tools.py - alternative name
    "change_order_product_tool":     "product_update",  # tool_factory.py
}


# ─── Redis helpers ────────────────────────────────────────────────────────────

def _redis_key(client_id: str, phone: str) -> str:
    return f"{REDIS_KEY_PREFIX}:{client_id}:{phone}"


async def _redis_get(client_id: str, phone: str) -> Optional[Dict]:
    """GET the open intent session for this phone+client. Falls back to DB."""
    try:
        rc = await _aget_redis_client()
        if rc:
            raw = await rc.get(_redis_key(client_id, phone))
            if raw:
                return json.loads(raw)
    except Exception as exc:
        logger.warning(f"[TRACKER] Redis GET failed: {exc}")
    # Fallback: query DB for pending events within last 90 min
    return await _db_get_pending_event(phone, client_id)


async def _redis_set(client_id: str, phone: str, data: Dict) -> None:
    """SET the intent session with 90-min TTL."""
    try:
        rc = await _aget_redis_client()
        if rc:
            await rc.set(
                _redis_key(client_id, phone),
                json.dumps(data),
                ex=INTENT_TTL_SECONDS,
            )
    except Exception as exc:
        logger.warning(f"[TRACKER] Redis SET failed (event still in DB): {exc}")


async def _redis_update(client_id: str, phone: str, updates: Dict) -> None:
    """Patch specific fields in the existing Redis intent value."""
    try:
        rc = await _aget_redis_client()
        if not rc:
            return
        raw = await rc.get(_redis_key(client_id, phone))
        if not raw:
            return
        data = json.loads(raw)
        data.update(updates)
        # Preserve remaining TTL
        ttl = await rc.ttl(_redis_key(client_id, phone))
        ttl = ttl if ttl and ttl > 0 else INTENT_TTL_SECONDS
        await rc.set(
            _redis_key(client_id, phone),
            json.dumps(data),
            ex=ttl,
        )
    except Exception as exc:
        logger.warning(f"[TRACKER] Redis UPDATE failed: {exc}")


async def _redis_del(client_id: str, phone: str) -> None:
    """DEL the intent session key when the event is closed."""
    try:
        rc = await _aget_redis_client()
        if rc:
            await rc.delete(_redis_key(client_id, phone))
    except Exception as exc:
        logger.warning(f"[TRACKER] Redis DEL failed: {exc}")


# ─── DB helpers ───────────────────────────────────────────────────────────────

@awith_retry
async def _db_get_pending_event(phone: str, client_id: str) -> Optional[Dict]:
    """
    Fallback when Redis is unavailable.
    Returns the most recent pending event for this phone+client within 90 min.
    """
    sql = """
        SELECT id::text, intent_trigger_type, order_id,
               turn_count, tools_called, intermediate_signals,
               intent_detected_at
        FROM cancellation_aversion_events
        WHERE phone_number = %s
          AND client_id = %s
          AND status = 'pending'
          AND intent_detected_at >= NOW() - INTERVAL '90 minutes'
        ORDER BY intent_detected_at DESC
        LIMIT 1
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (phone, client_id))
                row = await cur.fetchone()
                if row:
                    return {
                        "event_id":             row.get("id"),
                        "intent_trigger_type":  row.get("intent_trigger_type"),
                        "order_id":             row.get("order_id"),
                        "turn_count":           row.get("turn_count") or 0,
                        "tools_accumulated":    row.get("tools_called") or [],
                        "intermediate_signals": row.get("intermediate_signals") or [],
                        "intent_detected_at":   row.get("intent_detected_at").isoformat() if row.get("intent_detected_at") else None,
                        "last_turn_at":         datetime.now(timezone.utc).isoformat(),
                    }
                return None
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.warning(f"[TRACKER] DB pending lookup failed: {exc}")
        return None


@awith_retry
async def _db_open_event(
    client_id: str,
    phone: str,
    order_id: Optional[str],
    intent_trigger_msg: str,
    intent_trigger_type: str,
    intent_confidence: str,
    tools_called: List[str],
    conversation_snapshot: List[Dict],
    metadata: Dict,
    event_type: str = "cancellation_aversion",
    conversation_id: Optional[str] = None,
) -> Optional[str]:
    """INSERT a new pending event row. Returns the new UUID string."""
    # ── LOG DATA BEFORE DB INSERT ─────────────────────────────────────────────
    logger.info(
        f"[TRACKER] 📝 DB INSERT (OPEN EVENT):\n"
        f"  event_type: {event_type}\n"
        f"  client_id: {client_id}\n"
        f"  phone: {phone[-4:]}****\n"
        f"  order_id: {order_id or '(none)'}\n"
        f"  conversation_id: {conversation_id or '(none)'}\n"
        f"  intent_trigger_type: {intent_trigger_type}\n"
        f"  intent_confidence: {intent_confidence}\n"
        f"  intent_trigger_msg: {intent_trigger_msg[:100] if intent_trigger_msg else '(none)'}...\n"
        f"  tools_called: {tools_called}\n"
        f"  conversation_snapshot: {len(conversation_snapshot)} messages\n"
        f"  metadata: {json.dumps(metadata, indent=2) if metadata else '{}'}"
    )
    
    sql = """
        INSERT INTO cancellation_aversion_events (
            event_type, client_id, phone_number, order_id,
            conversation_id,
            intent_detected_at, intent_trigger_msg,
            intent_trigger_type, intent_confidence,
            status, tools_called, intermediate_signals,
            conversation_snapshot, turn_count, metadata
        ) VALUES (
            %s, %s::uuid, %s, %s,
            %s,
            NOW(), %s,
            %s, %s,
            'pending', %s::jsonb, '[]'::jsonb,
            %s::jsonb, 1, %s::jsonb
        ) RETURNING id::text AS id
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT EXISTS (
                        SELECT 1 FROM information_schema.tables
                        WHERE table_name = 'cancellation_aversion_events'
                    ) AS table_exists
                """)
                row_check = await cur.fetchone()
                table_exists = row_check.get("table_exists") if row_check else False
                if not table_exists:
                    logger.error(
                        "[TRACKER] Table 'cancellation_aversion_events' does not exist. "
                        "Run: python3 -m fashion_bot.Tables.cancellation_aversion_table"
                    )
                    return None

                await cur.execute(sql, (
                    event_type, client_id, phone, order_id,
                    conversation_id,
                    intent_trigger_msg[:1000] if intent_trigger_msg else None,
                    intent_trigger_type, intent_confidence,
                    json.dumps(tools_called),
                    json.dumps(conversation_snapshot),
                    json.dumps(metadata),
                ))
                row = await cur.fetchone()
                if not row:
                    logger.error(
                        "[TRACKER] INSERT succeeded but RETURNING id returned no row. "
                        "This should not happen — check table constraints."
                    )
                    return None
                return row.get("id")
    except Exception as exc:
        if is_connection_error(exc):
            raise
        import traceback
        logger.error(
            f"[TRACKER] DB INSERT failed: {type(exc).__name__}: {exc}\n"
            f"Traceback:\n{traceback.format_exc()}"
        )
        return None


@awith_retry
async def _db_update_turn(
    event_id: str,
    turn_count: int,
    tools_called: List[str],
    intermediate_signals: List[str],
    conversation_snapshot: List[Dict],
) -> None:
    """UPDATE per-turn fields (snapshot, tool accumulation, signals) on an open event."""
    sql = """
        UPDATE cancellation_aversion_events
        SET turn_count          = %s,
            tools_called        = %s::jsonb,
            intermediate_signals = %s::jsonb,
            conversation_snapshot = %s::jsonb,
            updated_at          = NOW()
        WHERE id = %s::uuid
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    turn_count,
                    json.dumps(tools_called),
                    json.dumps(intermediate_signals),
                    json.dumps(conversation_snapshot),
                    event_id,
                ))
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.warning(f"[TRACKER] DB turn update failed: {exc}")


@awith_retry
async def _db_close_event(
    event_id: str,
    status: str,
    resolution: Optional[str],
    aversion_method: Optional[str],
    outcome_trigger_tool: Optional[str],
    turn_count: int,
    tools_called: List[str],
    intermediate_signals: List[str],
    conversation_snapshot: List[Dict],
    cancellation_reason: Optional[str] = None,
    order_id: Optional[str] = None,
) -> None:
    """UPDATE the event to its final status.

    When ``cancellation_reason`` is provided it is merged into the metadata JSONB
    (key ``cancellation_reason``) so the dashboard can surface it. Passing None
    leaves any existing metadata untouched.

    ``order_id`` only fills a gap: it is applied via COALESCE so a late-arriving
    order completes a row opened without one, and never overwrites the order the
    event was opened against.
    """
    # ── LOG DATA BEFORE DB UPDATE (CLOSE) ───────────────────────────────────
    logger.info(
        f"[TRACKER] 📝 DB UPDATE (CLOSE EVENT):\n"
        f"  event_id: {event_id}\n"
        f"  status: {status}\n"
        f"  resolution: {resolution or '(none)'}\n"
        f"  aversion_method: {aversion_method or '(none)'}\n"
        f"  outcome_trigger_tool: {outcome_trigger_tool or '(none)'}\n"
        f"  turn_count: {turn_count}\n"
        f"  tools_called: {tools_called}\n"
        f"  intermediate_signals: {intermediate_signals}\n"
        f"  conversation_snapshot: {len(conversation_snapshot)} messages"
    )
    
    # Optionally merge the cancellation reason into metadata without clobbering
    # existing keys (jsonb concatenation, right-hand side wins on conflict).
    reason_set = ""
    reason_params: tuple = ()
    if cancellation_reason:
        reason_set = ", metadata = COALESCE(metadata, '{}'::jsonb) || jsonb_build_object('cancellation_reason', %s::text)"
        reason_params = (cancellation_reason,)

    # Fill an order_id the event was opened without; never clobber an existing one.
    order_set = ""
    order_params: tuple = ()
    if order_id:
        order_set = ", order_id = COALESCE(order_id, %s)"
        order_params = (order_id,)

    sql = f"""
        UPDATE cancellation_aversion_events
        SET status               = %s,
            resolution           = %s,
            aversion_method      = %s,
            outcome_trigger_tool = %s,
            resolved_at          = NOW(),
            turn_count           = %s,
            tools_called         = %s::jsonb,
            intermediate_signals = %s::jsonb,
            conversation_snapshot = %s::jsonb,
            updated_at           = NOW(){reason_set}{order_set}
        WHERE id = %s::uuid
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    status, resolution, aversion_method,
                    outcome_trigger_tool,
                    turn_count,
                    json.dumps(tools_called),
                    json.dumps(intermediate_signals),
                    json.dumps(conversation_snapshot),
                    *reason_params,
                    *order_params,
                    event_id,
                ))
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[TRACKER] DB close event failed: {exc}")


# ─── Public helper: ensure event exists for a conversation ─────────────────────

@awith_retry
async def ensure_event_for_conversation(
    client_id: str,
    phone: str,
    conversation_id: str,
    order_id: Optional[str] = None,
    intent_trigger_msg: str = "",
    metadata: Optional[Dict] = None,
    create_if_missing: bool = True,
) -> Optional[str]:
    """Idempotent: ensure at least one cancellation_aversion_events row exists
    for the given conversation. If a row (from the tracker or escalation path)
    already exists, returns None without inserting — but backfills its order_id
    first if the row has none and this turn supplies one. Otherwise opens a new
    pending event and returns the new event ID.

    Called by the async tag generator when tag == "Cancellation Requests" so
    the Cancellation Requests page always has data for every detected intent.

    `create_if_missing=False` makes this backfill-only: use it on turns that
    carry an order but a non-cancellation tag, so a later "Order Details Query"
    turn can complete an existing cancellation row without minting a spurious
    cancellation event for a conversation that never asked to cancel.

    The backfill matters because the tag fires on the turn the customer asks to
    cancel, which is typically before they have identified the order — so the
    row is opened with order_id NULL. The order usually surfaces a turn or two
    later, and order_id is otherwise written only on INSERT, so without this the
    row stays blank for the life of the conversation.
    """
    if not conversation_id or not client_id:
        return None
    if not create_if_missing and not order_id:
        return None  # nothing to create, nothing to backfill

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT id::text AS id, order_id
                       FROM cancellation_aversion_events
                       WHERE conversation_id = %s AND client_id = %s::uuid
                       ORDER BY intent_detected_at DESC NULLS LAST, id DESC
                       LIMIT 1""",
                    (str(conversation_id), client_id),
                )
                row = await cur.fetchone()
                if row:
                    if order_id and not row.get("order_id"):
                        await cur.execute(
                            """UPDATE cancellation_aversion_events
                               SET order_id = %s, updated_at = NOW()
                               WHERE id = %s::uuid AND order_id IS NULL""",
                            (order_id, row["id"]),
                        )
                        logger.info(
                            "[TRACKER] Backfilled order_id onto existing cancellation event",
                            extra={
                                "trace_id": (metadata or {}).get("trace_id"),
                                "client_id": client_id,
                                "conversation_id": str(conversation_id),
                                "event_id": row["id"],
                                "order_id": order_id,
                            },
                        )
                    else:
                        logger.info(
                            f"[TRACKER] Event already exists for conversation {str(conversation_id)[:8]}… — skipping"
                        )
                    return None
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.warning(f"[TRACKER] Dedup check failed, will attempt insert: {exc}")

    if not create_if_missing:
        # Backfill-only caller and no row to backfill — this conversation has no
        # cancellation intent, so do not mint one off the back of an order query.
        return None

    event_id = await _db_open_event(
        client_id=client_id,
        phone=phone,
        order_id=order_id,
        intent_trigger_msg=intent_trigger_msg or "Cancellation intent detected via conversation tag",
        intent_trigger_type="conversation_tag",
        intent_confidence="medium",
        tools_called=[],
        conversation_snapshot=[],
        metadata=metadata or {},
        event_type="cancellation_aversion",
        conversation_id=str(conversation_id),
    )
    if event_id:
        logger.info(
            f"[TRACKER] Event {event_id} created via tag-generator hook "
            f"(conv={str(conversation_id)[:8]}…, phone={phone[-4:]}****)"
        )
    return event_id


# ─── Signal extraction ────────────────────────────────────────────────────────

def _extract_intent_signals_layer1(result_state: Dict) -> Optional[Tuple[str, str]]:
    """
    Layer 1 — Read signals already computed by the graph (zero cost).

    Returns (trigger_type, confidence) if intent is found, else None.
    """
    # Signal 1: explicit state flag set by cancellation/update node
    if result_state.get("waiting_for_cancellation_reason"):
        return ("state_flag", "high")

    # Signal 2: LangGraph detected_intents list
    for intent_entry in (result_state.get("detected_intents") or []):
        label = str(intent_entry.get("intent", "")).lower()
        if "cancel" in label or "cancellation" in label:
            confidence = intent_entry.get("confidence", 0.5)
            level = "high" if confidence >= 0.8 else "medium"
            return ("detected_intent", level)

    # Signal 3: cancellation flow tool called this turn
    last_tools: List[str] = _get_last_tool_calls(result_state)
    for tool_name in last_tools:
        if tool_name in INTENT_TOOLS:
            return ("tool_signal", "high")

    return None


async def _llm_classify_intent_layer2(
    user_message: str,
    client_id: Optional[str],
) -> Optional[Tuple[str, str]]:
    """
    Layer 2 — Lightweight LLM call, fires only when Layer 1 found nothing.
    No hardcoded keywords. Works for any language.

    Returns (trigger_type, confidence) or None if intent not found.
    """
    prompt = (
        "Does the following customer message express an intent to cancel an order? "
        "Respond with ONLY a JSON object: "
        '{"intent": true or false, "confidence": "high" or "medium" or "low"}\n\n'
        f'Message: """{user_message}"""'
    )
    try:
        from fashion_bot.core.llm_factory import LLMInvoker
        raw = await LLMInvoker.ainvoke(
            prompt=prompt,
            tool_name="cancellation_intent_classifier",
            client_id=client_id,
        )
        # Parse JSON — be lenient about surrounding text
        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            return None
        parsed = json.loads(raw[start:end])
        if parsed.get("intent") is True:
            conf_str = str(parsed.get("confidence", "medium")).lower()
            level = conf_str if conf_str in ("high", "medium", "low") else "medium"
            return ("llm_classifier", level)
    except Exception as exc:
        logger.warning(f"[TRACKER] LLM intent classifier failed: {exc}")
    return None


def _extract_outcome_signals(result_state: Dict) -> Optional[Tuple[str, str, Optional[str]]]:
    """
    Check last_tool_calls for hard outcome signals.

    Returns (status, resolution, tool_name) or None.
    Priority: aversion tools > cancellation tools.
    """
    last_tools: List[str] = _get_last_tool_calls(result_state)
    # Check aversion first (exchange/return takes precedence if both appear)
    for tool_name in last_tools:
        if tool_name in AVERSION_TOOLS:
            status, resolution = AVERSION_TOOLS[tool_name]
            return (status, resolution, tool_name)
    # Then check cancellation
    for tool_name in last_tools:
        if tool_name in CANCELLATION_TOOLS:
            return ("cancelled", None, tool_name)
    return None


def _extract_soft_signals(result_state: Dict, intent_open: bool) -> List[str]:
    """
    Detect soft (non-conclusive) signals to accumulate when an intent is open
    but no hard outcome fired yet.
    """
    signals: List[str] = []
    if not intent_open:
        return signals

    # Context shift: graph routed away from cancellation node this turn
    for intent_entry in (result_state.get("detected_intents") or []):
        label = str(intent_entry.get("intent", "")).lower()
        if label and "cancel" not in label and "cancellation" not in label:
            signals.append("context_shift")
            break

    # Intent re-asserted: user is doubling down
    if result_state.get("waiting_for_cancellation_reason"):
        signals.append("intent_reasserted")

    return signals


def _get_last_tool_calls(result_state: Dict) -> List[str]:
    """
    Extract the list of tool names called in the latest graph turn.
    
    Tool names are stored in conversation_context.last_tool_calls by generic_skill_node
    after extracting them from intermediate_steps.
    """
    ctx = result_state.get("conversation_context") or {}
    tools = list(ctx.get("last_tool_calls") or [])
    if tools:
        logger.info(f"[TRACKER] Found tools in conversation_context.last_tool_calls: {tools}")
    else:
        logger.info(f"[TRACKER] No tools found in conversation_context.last_tool_calls")
    return tools


def _extract_order_id(result_state: Dict, user_message: Optional[str] = None) -> Optional[str]:
    """
    Best-effort order ID extraction from multiple sources.
    
    Priority (most reliable first):
    1. selected_order_id from state (explicitly selected)
    2. Extract from tool call arguments (if tools were called this turn)
    3. Extract from conversation_context.recent_actions (recent tool calls)
    4. Extract from conversation_context.focal_entity (current focus)
    5. Extract from conversation_context.entities (order entities)
    6. Extract from user_message (regex patterns)
    7. Single order from known_orders (if only one exists)
    """
    # Priority 1: selected_order_id from state (most reliable - explicitly set)
    if result_state.get("selected_order_id"):
        order_id = str(result_state["selected_order_id"])
        logger.info(f"[TRACKER] Extracted order_id from selected_order_id: {order_id}")
        return order_id
    
    # Priority 2: Extract from tool call arguments (if tools were called this turn)
    ctx = result_state.get("conversation_context") or {}
    last_tools = ctx.get("last_tool_calls") or []
    if last_tools:
        # Check recent_actions for tool parameters
        recent_actions = ctx.get("recent_actions") or []
        for action in recent_actions:
            params = action.get("parameters") or {}
            if params.get("order_id"):
                order_id = str(params["order_id"])
                logger.info(f"[TRACKER] Extracted order_id from recent_action parameters: {order_id}")
                return order_id
    
    # Priority 3: Extract from focal_entity (current conversation focus)
    focal_entity = ctx.get("focal_entity")
    if focal_entity:
        entity_type = focal_entity.get("entity_type")
        entity_id = focal_entity.get("entity_id")
        if entity_type == "order" and entity_id:
            # entity_id might be "gv14203" or "order:gv14203"
            order_id = str(entity_id).replace("order:", "").replace("#", "").strip()
            if order_id:
                logger.info(f"[TRACKER] Extracted order_id from focal_entity: {order_id}")
                return order_id
    
    # Priority 4: Extract from entities list (find order entities)
    entities = ctx.get("entities") or []
    order_entities = [e for e in entities if e.get("entity_type") == "order"]
    if len(order_entities) == 1:
        entity_id = order_entities[0].get("entity_id") or order_entities[0].get("entity_value")
        if entity_id:
            order_id = str(entity_id).replace("order:", "").replace("#", "").strip()
            if order_id:
                logger.info(f"[TRACKER] Extracted order_id from single order entity: {order_id}")
                return order_id
    
    # Priority 5: Extract from user message (regex patterns)
    if user_message:
        import re
        # Client-agnostic. This previously only matched `gv1234`, which is one
        # client's order format — every client numbering orders differently
        # (#71379, 70849, ...) fell through this branch entirely.
        patterns = [
            # Alphanumeric order codes: gv16956, #GV-1234, order ab12345.
            r'(?:order\s*|#)?\b([a-z]{2,4}-?\d{3,})\b',
            # Purely numeric orders: #71379, "order 71379", "order no 70849".
            # An explicit marker is required — a bare run of digits is far more
            # likely to be the phone number the bot just asked the customer for.
            r'(?:order\s*(?:no\.?|number|id)?\s*#?\s*|#)(\d{4,})\b',
        ]
        for pattern in patterns:
            match = re.search(pattern, user_message, re.IGNORECASE)
            if match:
                order_id = match.group(1).upper().replace('-', '').replace('#', '')
                logger.info(f"[TRACKER] Extracted order_id from message: {order_id}")
                return order_id
    
    # Priority 6: Single order from known_orders (fallback)
    orders = result_state.get("known_orders") or []
    if len(orders) == 1:
        order_id = str(orders[0].get("order_id") or orders[0].get("name") or "")
        if order_id:
            logger.info(f"[TRACKER] Extracted order_id from single known_order: {order_id}")
            return order_id
    
    logger.info(f"[TRACKER] Could not extract order_id from any source")
    return None


# Structured cancellation-reason enum the cancel tool captures (see
# tool_factory.cancel_order_tool / prompts.cancel_or_update_prompt). Persisting it
# onto the event lets the dashboard's Cancellation Requests page show a Reason chip.
_CANCELLATION_REASON_VALUES = {
    "ordered_by_mistake", "not_needed", "delivery_too_slow", "wrong_size",
    "wrong_address", "wrong_product", "found_better_price", "too_expensive",
    "quality_concerns", "changed_mind", "other",
}


def _extract_cancellation_reason(result_state: Dict) -> Optional[str]:
    """Best-effort extraction of the customer's cancellation reason enum from the
    cancel tool's arguments on this turn. Returns None when no cancel tool ran or
    no reason was supplied.
    """
    ctx = result_state.get("conversation_context") or {}
    recent_actions = ctx.get("recent_actions") or []
    for action in recent_actions:
        params = action.get("parameters") or {}
        reason = params.get("cancellation_reason")
        if reason:
            reason_norm = str(reason).strip().lower()
            if reason_norm:
                logger.info(f"[TRACKER] Extracted cancellation_reason: {reason_norm}")
                return reason_norm
    return None


def _detect_rto_outcome(last_tools: List[str]) -> Optional[Tuple[str, List[str]]]:
    """
    Check whether order-update tools fired that implicitly prevent an RTO.

    Returns (resolution, fired_tool_names) or None.

    Multi-tool rule: if multiple RTO tools fired in the same turn (e.g. address +
    phone updated together), resolution is 'multi_field_update'.
    """
    fired: List[str] = [t for t in last_tools if t in RTO_AVERSION_TOOLS]
    if not fired:
        return None
    if len(fired) > 1:
        return ("multi_field_update", fired)
    return (RTO_AVERSION_TOOLS[fired[0]], fired)


def _build_snapshot(result_state: Dict, max_messages: int = 20) -> List[Dict]:
    """Serialize the last N LangChain messages to plain dicts for DB storage."""
    snapshot: List[Dict] = []
    for msg in (result_state.get("messages") or [])[-max_messages:]:
        try:
            if hasattr(msg, "type") and hasattr(msg, "content"):
                content = msg.content
                if isinstance(content, list):
                    # Multi-part AI message (tool call blocks)
                    content = " ".join(
                        str(p.get("text", p) if isinstance(p, dict) else p)
                        for p in content
                    )
                snapshot.append({
                    "role":    msg.type,
                    "content": str(content)[:800],
                })
            elif isinstance(msg, dict):
                snapshot.append({
                    "role":    msg.get("type", "unknown"),
                    "content": str(msg.get("content", ""))[:800],
                })
        except Exception:
            pass  # skip unparseable messages
    return snapshot


# ─── Main entry point ─────────────────────────────────────────────────────────

async def analyze_turn(
    user_message: str,
    bot_response: str,
    phone_number: str,
    client_id: str,
    trace_id: str = "",
) -> None:
    """
    Analyse one webhook turn for cancellation aversion signals.

    Called via asyncio.create_task() from process_webhook_payload()
    immediately after call_main_bot() returns.  Never awaited — pure fire-and-forget.

    Execution cost on the happy path (no cancellation signal):
        1 × in-memory state lookup  (~0 ms)
        1 × Redis GET               (~1–2 ms, in thread pool)
        early return                → total ≈ 2–3 ms, zero event loop blocking.
    """
    try:
        await _analyze_turn_inner(
            user_message=user_message,
            bot_response=bot_response,
            phone_number=phone_number,
            client_id=client_id,
            trace_id=trace_id,
        )
    except Exception as exc:
        # Last-resort catch — tracker must never crash the webhook
        logger.error(f"[TRACKER] Unhandled error in analyze_turn: {exc}", exc_info=True)


async def _analyze_turn_inner(
    user_message: str,
    bot_response: str,
    phone_number: str,
    client_id: str,
    trace_id: str,
) -> None:
    logger.info(f"[TRACKER] analyze_turn started for {phone_number[-4:]}****: {user_message[:50]}...")
    
    # ── 1. Get graph result state from in-memory cache ────────────────────────
    try:
        from fashion_bot.state_cache import aget_state_by_numbers
        result_state: Dict = await aget_state_by_numbers(phone_number, client_id) or {}
    except Exception as exc:
        logger.warning(f"[TRACKER] Could not fetch result_state: {exc}")
        result_state = {}

    # ── 2. Extract outcome signals first (cheap, tool-list check) ─────────────
    last_tools: List[str] = _get_last_tool_calls(result_state)
    logger.info(f"[TRACKER] Tools called this turn: {last_tools}")
    outcome = _extract_outcome_signals(result_state)  # (status, resolution, tool) | None
    if outcome:
        logger.info(f"[TRACKER] Outcome detected: {outcome}")

    # ── 3. Check if there's an existing open cancellation intent session ───────
    redis_intent = await _redis_get(client_id, phone_number)
    intent_open = redis_intent is not None

    # ── 4. If open intent → process outcome or accumulate soft signals ─────────
    if intent_open:
        event_id:     str       = redis_intent["event_id"]
        event_order_id: Optional[str] = redis_intent.get("order_id")  # Order ID from the open event
        turn_count:   int       = redis_intent.get("turn_count", 1) + 1
        tools_accum:  List[str] = redis_intent.get("tools_accumulated", [])
        signals_accum: List[str] = redis_intent.get("intermediate_signals", [])

        # Extract order_id from current turn
        current_order_id = _extract_order_id(result_state, user_message)
        
        # Log order_id comparison for debugging
        logger.info(
            f"[TRACKER] 🔍 Order ID validation (open event exists):\n"
            f"  event_order_id (from Redis): {event_order_id or '(none)'}\n"
            f"  current_order_id (extracted): {current_order_id or '(none)'}\n"
            f"  tools_called this turn: {last_tools}"
        )
        
        # Merge new tools
        for t in last_tools:
            if t not in tools_accum:
                tools_accum.append(t)

        snapshot = _build_snapshot(result_state)

        if outcome:
            # Hard outcome found — check if order_id matches
            status, resolution, trigger_tool = outcome
            
            # Validate order_id match before closing
            order_ids_match = False
            if event_order_id and current_order_id:
                # Normalize order IDs for comparison (remove #, case-insensitive)
                event_oid_normalized = str(event_order_id).upper().replace('#', '').strip()
                current_oid_normalized = str(current_order_id).upper().replace('#', '').strip()
                order_ids_match = (event_oid_normalized == current_oid_normalized)
            
            # Only a genuine conflict — two different known orders — justifies a
            # second event. If either side is unknown this is still the same
            # cancellation: the open event simply predates the order being
            # identified (the common case, since intent is usually detected
            # before the customer supplies an order), or this turn happened not
            # to surface one. Splitting those into a new event both double-counts
            # the request and strands the order on a row the UI can't reach.
            different_orders = bool(event_order_id and current_order_id and not order_ids_match)

            if not different_orders:
                aversion_method = "tool_driven" if status in ("averted", "escalated") else None
                await _db_close_event(
                    event_id       = event_id,
                    status         = status,
                    resolution     = resolution,
                    aversion_method= aversion_method,
                    outcome_trigger_tool = trigger_tool,
                    turn_count     = turn_count,
                    tools_called   = tools_accum,
                    intermediate_signals = signals_accum,
                    conversation_snapshot = snapshot,
                    cancellation_reason = _extract_cancellation_reason(result_state),
                    # Fills the gap when the event was opened before the order
                    # was known; COALESCE keeps an existing value intact.
                    order_id       = current_order_id,
                )
                await _redis_del(client_id, phone_number)
                logger.info(
                    f"[TRACKER] Event {event_id} CLOSED → {status}/{resolution} "
                    f"via {trigger_tool} (turn {turn_count}, order={current_order_id}) [{phone_number[-4:]}****]"
                )
            else:
                # Order IDs don't match - create a NEW event for the current order
                # The old event stays open (it's for a different order)
                logger.info(
                    f"[TRACKER] Order ID mismatch: event={event_order_id}, current={current_order_id}. "
                    f"Creating NEW event for current order {current_order_id} (old event {event_id} stays open)."
                )
                
                # Create new event for the current order
                new_snapshot = _build_snapshot(result_state)
                new_metadata = {
                    "trace_id": trace_id,
                    "gupshup_source": result_state.get("gupshup_source_phone_number", ""),
                }
                new_event_id = await _db_open_event(
                    client_id            = client_id,
                    phone                = phone_number,
                    order_id             = current_order_id,
                    intent_trigger_msg   = user_message,
                    intent_trigger_type  = "outcome_tool",
                    intent_confidence    = "high",
                    tools_called         = list(last_tools),
                    conversation_snapshot = new_snapshot,
                    metadata             = new_metadata,
                    event_type           = "cancellation_aversion",
                )
                
                if new_event_id:
                    # Immediately close the new event (outcome already happened)
                    aversion_method = "tool_driven" if status in ("averted", "escalated") else None
                    await _db_close_event(
                        event_id             = new_event_id,
                        status               = status,
                        resolution           = resolution,
                        aversion_method      = aversion_method,
                        outcome_trigger_tool = trigger_tool,
                        turn_count           = 1,
                        tools_called         = list(last_tools),
                        intermediate_signals = [],
                        conversation_snapshot = new_snapshot,
                    )
                    logger.info(
                        f"[TRACKER] New Event {new_event_id} OPENED+CLOSED → {status}/{resolution} "
                        f"via {trigger_tool} (order={current_order_id}) [{phone_number[-4:]}****]"
                    )
            return
        else:
            # No hard outcome yet — accumulate soft signals, update DB
            new_signals = _extract_soft_signals(result_state, intent_open=True)
            for s in new_signals:
                if s not in signals_accum:
                    signals_accum.append(s)

            await _db_update_turn(
                event_id             = event_id,
                turn_count           = turn_count,
                tools_called         = tools_accum,
                intermediate_signals = signals_accum,
                conversation_snapshot = snapshot,
            )
            await _redis_update(client_id, phone_number, {
                "turn_count":          turn_count,
                "tools_accumulated":   tools_accum,
                "intermediate_signals": signals_accum,
                "last_turn_at":        datetime.now(timezone.utc).isoformat(),
            })
            logger.info(
                f"[TRACKER] Event {event_id} updated → turn {turn_count}, "
                f"signals={signals_accum} [{phone_number[-4:]}****]"
            )
        return

    # ── 5. No open intent — check Layer 1 for new intent ──────────────────────
    intent_signal = _extract_intent_signals_layer1(result_state)

    # ── 6. Layer 2: LLM micro-classifier (only if Layer 1 missed) ─────────────
    if intent_signal is None:
        intent_signal = await _llm_classify_intent_layer2(user_message, client_id)

    # ── 7. No intent at all but cancel tool fired → retroactive backfill ───────
    if intent_signal is None and outcome:
        status, resolution, trigger_tool = outcome
        if status == "cancelled":
            # Open and immediately close with retroactive intent
            intent_signal = ("retroactive", "high")
        else:
            # Aversion tool fired with no prior intent detected.
            # BUT: if it's an RTO tool (address/phone update), it should be tracked
            # as RTO aversion, not ignored. Check for RTO first before returning.
            rto = _detect_rto_outcome(last_tools)
            if rto:
                # This is an RTO aversion event, not a cancellation aversion
                # Fall through to RTO handling below
                intent_signal = None  # Keep it None so RTO branch executes
            else:
                # Aversion tool fired with no prior intent detected — likely a
                # proactive alternative offer; not a cancellation event.
                return

    if intent_signal is None:
        # ── 7b. RTO Aversion check ────────────────────────────────────────────
        # No cancellation intent was found and no open session exists.
        # Check if an order-update tool fired — that means the bot silently fixed
        # a delivery problem, implicitly preventing an RTO / future cancellation.
        # Guard: skip if a cancellation intent session IS open — that scenario is
        # already handled above as cancellation_aversion/address_fix to avoid
        # double-counting the same turn as both event types.
        logger.info(f"[TRACKER] Checking for RTO tools in: {last_tools}")
        rto = _detect_rto_outcome(last_tools)
        logger.info(f"[TRACKER] RTO detection result: {rto}")
        if rto:
            rto_resolution, rto_tools_fired = rto
            rto_order_id = _extract_order_id(result_state, user_message)
            rto_snapshot  = _build_snapshot(result_state)
            rto_metadata  = {
                "trace_id":       trace_id,
                "gupshup_source": result_state.get("gupshup_source_phone_number", ""),
            }
            rto_event_id = await _db_open_event(
                client_id            = client_id,
                phone                = phone_number,
                order_id             = rto_order_id,
                intent_trigger_msg   = user_message,
                intent_trigger_type  = "order_update_tool",
                intent_confidence    = "high",
                tools_called         = list(last_tools),
                conversation_snapshot = rto_snapshot,
                metadata             = rto_metadata,
                event_type           = "rto_aversion",
            )
            if rto_event_id:
                # RTO events always close immediately — the tool call IS the outcome
                await _db_close_event(
                    event_id             = rto_event_id,
                    status               = "averted",
                    resolution           = rto_resolution,
                    aversion_method      = "tool_driven",
                    outcome_trigger_tool = ",".join(rto_tools_fired),
                    turn_count           = 1,
                    tools_called         = list(last_tools),
                    intermediate_signals = [],
                    conversation_snapshot = rto_snapshot,
                )
                logger.info(
                    f"[TRACKER] RTO Event {rto_event_id} OPENED+CLOSED → "
                    f"averted/{rto_resolution} via {rto_tools_fired} "
                    f"[{phone_number[-4:]}****]"
                )
        # Genuinely no cancellation signal this turn
        logger.info(f"[TRACKER] No cancellation or RTO signal detected for {phone_number[-4:]}****")
        return

    trigger_type, confidence = intent_signal

    # ── 8. Open a new event ───────────────────────────────────────────────────
    order_id  = _extract_order_id(result_state, user_message)
    
    # Log order_id extraction for debugging
    logger.info(
        f"[TRACKER] 🔍 Order ID extraction (opening new event):\n"
        f"  extracted order_id: {order_id or '(none)'}\n"
        f"  user_message: {user_message[:100] if user_message else '(none)'}...\n"
        f"  selected_order_id (state): {result_state.get('selected_order_id') or '(none)'}\n"
        f"  tools_called: {last_tools}"
    )
    
    # Check if there's a stale open event for a different order - close it first
    if intent_open:
        event_order_id = redis_intent.get("order_id")
        if event_order_id and order_id:
            event_oid_normalized = str(event_order_id).upper().replace('#', '').strip()
            current_oid_normalized = str(order_id).upper().replace('#', '').strip()
            if event_oid_normalized != current_oid_normalized:
                logger.info(
                    f"[TRACKER] Closing stale event {redis_intent['event_id']} for order {event_order_id} "
                    f"before opening new event for order {order_id}"
                )
                # Close the old event as abandoned (different order)
                stale_snapshot = _build_snapshot(result_state)
                await _db_close_event(
                    event_id       = redis_intent["event_id"],
                    status         = "abandoned",
                    resolution     = "order_mismatch",
                    aversion_method= None,
                    outcome_trigger_tool = None,
                    turn_count     = redis_intent.get("turn_count", 1),
                    tools_called   = redis_intent.get("tools_accumulated", []),
                    intermediate_signals = redis_intent.get("intermediate_signals", []),
                    conversation_snapshot = stale_snapshot,
                )
                await _redis_del(client_id, phone_number)
    
    snapshot  = _build_snapshot(result_state)
    metadata  = {
        "trace_id":       trace_id,
        "gupshup_source": result_state.get("gupshup_source_phone_number", ""),
    }
    # If the cancel tool already ran this turn, capture its reason enum so the
    # dashboard can show it (same-turn open+close path below preserves this).
    _open_reason = _extract_cancellation_reason(result_state)
    if _open_reason:
        metadata["cancellation_reason"] = _open_reason

    event_id = await _db_open_event(
        client_id            = client_id,
        phone                = phone_number,
        order_id             = order_id,
        intent_trigger_msg   = user_message,
        intent_trigger_type  = trigger_type,
        intent_confidence    = confidence,
        tools_called         = list(last_tools),
        conversation_snapshot = snapshot,
        metadata             = metadata,
    )
    if not event_id:
        logger.error("[TRACKER] Failed to open DB event — aborting.")
        return

    # ── 9. If outcome already fired on this same turn, close immediately ───────
    if outcome:
        status, resolution, trigger_tool = outcome
        aversion_method = "tool_driven" if status in ("averted", "escalated") else None
        await _db_close_event(
            event_id             = event_id,
            status               = status,
            resolution           = resolution,
            aversion_method      = aversion_method,
            outcome_trigger_tool = trigger_tool,
            turn_count           = 1,
            tools_called         = list(last_tools),
            intermediate_signals = [],
            conversation_snapshot = snapshot,
        )
        logger.info(
            f"[TRACKER] Event {event_id} OPENED+CLOSED in 1 turn → "
            f"{status}/{resolution} [{phone_number[-4:]}****]"
        )
        return

    # ── 10. Persist session in Redis (TTL = 90 min) ───────────────────────────
    await _redis_set(client_id, phone_number, {
        "event_id":            event_id,
        "intent_detected_at":  datetime.now(timezone.utc).isoformat(),
        "intent_trigger_type": trigger_type,
        "order_id":            order_id,
        "turn_count":          1,
        "tools_accumulated":   list(last_tools),
        "intermediate_signals": [],
        "last_turn_at":        datetime.now(timezone.utc).isoformat(),
    })
    logger.info(
        f"[TRACKER] Event {event_id} OPENED (pending) via {trigger_type}/{confidence} "
        f"[{phone_number[-4:]}****]"
    )
