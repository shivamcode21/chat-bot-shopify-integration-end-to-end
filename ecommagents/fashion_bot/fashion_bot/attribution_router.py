"""
Attribution API Router
Handles chatbot attribution events, KPI calculations, and conversion tracking.
"""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging
import json
import uuid
import os

from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.Tables.attribution_table import initialize_attribution_tables
from fashion_bot.utils.client_id_utils import decode_client_id, is_encoded_client_id

logger = logging.getLogger("attribution")
router = APIRouter(prefix="/api/attribution", tags=["Attribution"])

# ============================================================
# FEATURE FLAGS
# ============================================================
# Set DISABLE_ATTRIBUTION_EVENTS=true to stop storing chat events
# (widget_opened, session_started, message_sent, etc.)
# Order attribution will still work.
DISABLE_ATTRIBUTION_EVENTS = os.getenv("DISABLE_ATTRIBUTION_EVENTS", "false").lower() == "true"

# ============================================================
# CLIENT ID CACHE (avoids repeated DB lookups)
# ============================================================
# ============================================================
# MODELS
# ============================================================

class AttributionEvent(BaseModel):
    """Model for attribution event from widget."""
    bot_ref: Optional[str] = Field(None, description="Attribution token (absent until a real chat message is sent)")
    anon_id: str = Field(..., description="Anonymous user ID")
    client_id: str = Field(..., description="Client ID (required)")
    session_id: Optional[str] = None
    
    event_type: str = Field(..., description="Event type")
    event_data: Optional[Dict[str, Any]] = {}
    
    page_url: Optional[str] = None
    page_type: Optional[str] = None
    product_handle: Optional[str] = None
    product_title: Optional[str] = None
    product_price: Optional[str] = None
    
    user_agent: Optional[str] = None
    referrer: Optional[str] = None


class BatchEvents(BaseModel):
    """Batch of attribution events."""
    events: List[AttributionEvent]


class OrderAttribution(BaseModel):
    """Model for order attribution."""
    order_id: str
    order_number: Optional[str] = None
    client_id: str
    bot_ref: Optional[str] = None
    anon_id: Optional[str] = None
    order_total: Optional[float] = None
    order_currency: str = "INR"
    order_items_count: Optional[int] = None
    order_created_at: Optional[datetime] = None


class KPIQuery(BaseModel):
    """Query parameters for KPI dashboard."""
    client_id: str
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    attribution_type: Optional[str] = None  # 'direct', 'assisted', 'all'


# ============================================================
# HELPER FUNCTIONS
# ============================================================

async def alookup_conversation_id(cur, resolved_client_id: str, session_id: Optional[str]) -> Optional[str]:
    """Find the conversation_id already backing this chat session's CURRENT
    (still-live) conversation, if the backend has created one by now.

    The widget's session_id is durable in localStorage for months, but a
    backend conversation is only "live" for 90 minutes of inactivity
    (_astore_conversation_event_internal in postgres_conversations.py)
    before the next message starts a brand new conversation_id under the
    same session_id. So a match is only valid if that conversation was
    actually updated within the last 90 minutes - otherwise we'd be
    stamping a stale, unrelated conversation (possibly days/weeks old)
    onto today's event. Returning None here is correct/expected (not a
    bug) whenever no conversation is currently live for this session; the
    async post-hoc backfill in websocket_chat.py fills it in once the
    backend actually creates/touches one.
    """
    if not session_id:
        return None

    # Web-chat conversations are keyed by (client_id, phone, channel_type),
    # and "phone" holds the session_id until the customer is phone-verified
    # mid-chat.
    await cur.execute("""
        SELECT conversation_id FROM conversations
        WHERE client_id = %s AND phone = %s AND channel_type = 'web-chat'
          AND updated_at >= NOW() - INTERVAL '90 minutes'
        ORDER BY updated_at DESC
        LIMIT 1
    """, (resolved_client_id, session_id))
    row = await cur.fetchone()
    if row:
        return row['conversation_id']

    # Once a customer verifies their phone mid-chat, every conversation the
    # backend creates for them from then on is keyed by that real number, not
    # this session_id, so the lookup above can never match again. state_cache
    # persists that known number onto this session's past attribution rows
    # (phone_number column) - reuse it as a second lookup key. Unlike
    # conversation_id, a verified phone number doesn't go stale, so it's safe
    # to look up here regardless of how old that prior row is.
    await cur.execute("""
        SELECT phone_number FROM chat_attribution_events
        WHERE session_id = %s AND client_id = %s AND phone_number IS NOT NULL
        ORDER BY created_at DESC
        LIMIT 1
    """, (session_id, resolved_client_id))
    phone_row = await cur.fetchone()
    if not phone_row:
        return None

    await cur.execute("""
        SELECT conversation_id FROM conversations
        WHERE client_id = %s AND phone = %s AND channel_type = 'web-chat'
          AND updated_at >= NOW() - INTERVAL '90 minutes'
        ORDER BY updated_at DESC
        LIMIT 1
    """, (resolved_client_id, phone_row['phone_number']))
    row = await cur.fetchone()
    return row['conversation_id'] if row else None


async def aresolve_client_id(client_id_input: str, cursor=None) -> str:
    """Resolve a client identifier (encoded token, UUID, or name) to a UUID.

    Delegates name-based lookups to the unified tiered cache in
    ``utils.client_identity_cache``.  The ``cursor`` parameter is
    accepted for backward compatibility but ignored (the shared module
    manages its own connections).
    """
    if not client_id_input or not client_id_input.strip():
        raise ValueError("client_id cannot be empty")

    client_id_input = client_id_input.strip()

    if is_encoded_client_id(client_id_input):
        try:
            decoded_client_id = decode_client_id(client_id_input)
            logger.debug("✅ client_id decoded from encoded identifier")
            client_id_input = decoded_client_id
        except ValueError:
            logger.warning("⚠️ Invalid encoded client_id received for attribution; falling back to standard resolution")

    try:
        uuid.UUID(client_id_input)
        logger.debug(f"✅ client_id is already a UUID: {client_id_input}")
        return client_id_input
    except (ValueError, AttributeError):
        pass

    from fashion_bot.utils.client_identity_cache import aget_client_id_by_client_name
    resolved = await aget_client_id_by_client_name(client_id_input)
    if resolved:
        logger.info(f"✅ Found client_id '{resolved}' for name '{client_id_input}'")
        return resolved

    raise ValueError(f"Client not found with name '{client_id_input}' in clients table")


# ============================================================
# INITIALIZATION
# ============================================================

def ensure_tables():
    """Ensure attribution tables exist. Uses direct connection for init operations."""
    try:
        from fashion_bot.database_manager import get_direct_postgres_cursor
        conn, cur = get_direct_postgres_cursor()
        try:
            initialize_attribution_tables(cur)
            logger.info("✅ Attribution tables initialized")
        finally:
            cur.close()
            conn.close()
    except Exception as e:
        logger.error(f"❌ Failed to initialize attribution tables: {e}")


# ============================================================
# EVENT STORAGE ENDPOINTS
# ============================================================

@router.post("/event")
async def store_event(event: AttributionEvent, request: Request):
    """
    Store a single attribution event.
    Called by the chat widget for each trackable interaction.
    
    Optimized: Uses single DB connection and caches client_id lookups.
    
    Can be disabled by setting DISABLE_ATTRIBUTION_EVENTS=true
    """
    # Check if attribution events are disabled
    if DISABLE_ATTRIBUTION_EVENTS:
        logger.debug(f"📊 Attribution events disabled, skipping: {event.event_type}")
        return {"success": True, "event_id": None, "disabled": True}
    
    # Validate client_id is provided and not empty (no DB needed)
    if not event.client_id or not event.client_id.strip():
        raise HTTPException(
            status_code=400,
            detail="client_id is required and cannot be empty"
        )
    
    # Use context manager for automatic connection cleanup
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Resolve client_id using the SAME cursor (avoids extra connection)
                try:
                    resolved_client_id = await aresolve_client_id(event.client_id, cursor=cur)
                except ValueError as e:
                    raise HTTPException(status_code=400, detail=str(e))
                
                # Get user agent from request if not provided
                user_agent = event.user_agent or request.headers.get("user-agent", "")
                referrer = event.referrer or request.headers.get("referer", "")

                conversation_id = await alookup_conversation_id(cur, resolved_client_id, event.session_id)

                await cur.execute("""
                    INSERT INTO chat_attribution_events (
                        bot_ref, anon_id, session_id, client_id,
                        event_type, event_data,
                        page_url, page_type, product_handle, product_title, product_price,
                        user_agent, referrer, conversation_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (anon_id) WHERE event_type = 'session_started' DO NOTHING
                    RETURNING id
                """, (
                    event.bot_ref,
                    event.anon_id,
                    event.session_id,
                    resolved_client_id,
                    event.event_type,
                    json.dumps(event.event_data or {}),
                    event.page_url,
                    event.page_type,
                    event.product_handle,
                    event.product_title,
                    event.product_price,
                    user_agent,
                    referrer,
                    conversation_id
                ))
                
                result = await cur.fetchone()
                event_id = result['id'] if result else None
                
                logger.info(f"📊 Attribution event stored: {event.event_type} (bot_ref={event.bot_ref[:8] + '...' if event.bot_ref else None})")
                
                return {"success": True, "event_id": event_id}
        
    except HTTPException:
        # Re-raise HTTP exceptions (like validation errors)
        raise
    except Exception as e:
        logger.error(f"❌ Failed to store attribution event: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/events/batch")
async def store_events_batch(batch: BatchEvents, request: Request):
    """
    Store multiple attribution events in a single request.
    More efficient for high-volume tracking.
    
    Optimized: Uses single DB connection for all operations.
    
    Can be disabled by setting DISABLE_ATTRIBUTION_EVENTS=true
    """
    # Check if attribution events are disabled
    if DISABLE_ATTRIBUTION_EVENTS:
        logger.debug(f"📊 Attribution events disabled, skipping batch of {len(batch.events)} events")
        return {"success": True, "event_ids": [], "count": 0, "disabled": True}
    
    # Validate all events have client_id (no DB needed for this check)
    for idx, event in enumerate(batch.events):
        if not event.client_id or not event.client_id.strip():
            raise HTTPException(
                status_code=400,
                detail=f"client_id is required and cannot be empty for event at index {idx}"
            )
    
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Resolve all unique client_ids using the SAME cursor
                resolved_client_ids = {}
                for idx, event in enumerate(batch.events):
                    client_id_key = event.client_id.strip()
                    if client_id_key not in resolved_client_ids:
                        try:
                            resolved_client_ids[client_id_key] = await aresolve_client_id(client_id_key, cursor=cur)
                        except ValueError as e:
                            raise HTTPException(
                                status_code=400,
                                detail=f"Error resolving client_id for event at index {idx}: {str(e)}"
                            )
                
                user_agent = request.headers.get("user-agent", "")
                referrer = request.headers.get("referer", "")

                event_ids = []
                conversation_id_cache: Dict[tuple, Optional[str]] = {}

                for idx, event in enumerate(batch.events):
                    # Get resolved client_id
                    resolved_client_id = resolved_client_ids[event.client_id.strip()]

                    cache_key = (resolved_client_id, event.session_id)
                    if cache_key not in conversation_id_cache:
                        conversation_id_cache[cache_key] = await alookup_conversation_id(
                            cur, resolved_client_id, event.session_id
                        )
                    conversation_id = conversation_id_cache[cache_key]

                    # This connection is autocommit, so each statement commits
                    # or fails independently - no shared transaction for one
                    # bad row to poison. A plain try/except is enough to keep
                    # one failing event (e.g. an unexpected constraint hit)
                    # from stopping the rest of the batch from being stored.
                    try:
                        await cur.execute("""
                            INSERT INTO chat_attribution_events (
                                bot_ref, anon_id, session_id, client_id,
                                event_type, event_data,
                                page_url, page_type, product_handle, product_title, product_price,
                                user_agent, referrer, conversation_id
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (anon_id) WHERE event_type = 'session_started' DO NOTHING
                            RETURNING id
                        """, (
                            event.bot_ref,
                            event.anon_id,
                            event.session_id,
                            resolved_client_id,
                            event.event_type,
                            json.dumps(event.event_data or {}),
                            event.page_url or "",
                            event.page_type or "",
                            event.product_handle or "",
                            event.product_title or "",
                            event.product_price or "",
                            event.user_agent or user_agent,
                            event.referrer or referrer,
                            conversation_id
                        ))

                        result = await cur.fetchone()
                        if result:
                            event_ids.append(result['id'])
                    except Exception as row_err:
                        logger.warning(
                            f"⚠️ Skipping event at index {idx} in batch (event_type={event.event_type}): {row_err}"
                        )

                logger.info(f"📊 Batch stored: {len(event_ids)} attribution events")
                
                return {"success": True, "event_ids": event_ids, "count": len(event_ids)}
        
    except HTTPException:
        # Re-raise HTTP exceptions (like validation errors)
        raise
    except Exception as e:
        logger.error(f"❌ Failed to store batch events: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# ORDER ATTRIBUTION ENDPOINTS
# ============================================================

@router.post("/order")
async def attribute_order(order: OrderAttribution):
    """
    Attribute an order to a chat session.
    Called by Shopify webhook when order is created.
    
    Optimized: Uses single DB connection for all operations.
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Resolve client_id using the SAME cursor
                try:
                    resolved_client_id = await aresolve_client_id(order.client_id, cursor=cur)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error resolving client_id: {str(e)}"
                    )
                
                # Determine attribution type
                attribution_type = "none"
                attribution_window_hours = None
                chat_session_id = None
                last_chat_event_at = None
                chat_messages_count = 0
                
                # bot_ref match — only "assisted" if there's a real (non
                # session_started) chat event behind it, not just a token
                # that got attached without a conversation.
                chat_conversation_id = None

                if order.bot_ref:
                    await cur.execute("""
                        SELECT session_id, (array_agg(conversation_id ORDER BY created_at DESC))[1] as conversation_id, MAX(created_at) as last_event, COUNT(*) as event_count
                        FROM chat_attribution_events
                        WHERE bot_ref = %s AND client_id = %s AND event_type != 'session_started'
                        GROUP BY session_id
                        ORDER BY last_event DESC
                        LIMIT 1
                    """, (order.bot_ref, resolved_client_id))

                    result = await cur.fetchone()
                    if result:
                        attribution_type = "assisted"
                        chat_session_id = result['session_id']
                        chat_conversation_id = result['conversation_id']
                        last_chat_event_at = result['last_event']
                        chat_messages_count = result['event_count']

                        if order.order_created_at and last_chat_event_at:
                            delta = order.order_created_at - last_chat_event_at
                            attribution_window_hours = int(delta.total_seconds() / 3600)

                # anon_id match within window (also requires real activity)
                if attribution_type == "none" and order.anon_id:
                    window_start = (order.order_created_at or datetime.utcnow()) - timedelta(hours=72)

                    await cur.execute("""
                        SELECT session_id, bot_ref, (array_agg(conversation_id ORDER BY created_at DESC))[1] as conversation_id, MAX(created_at) as last_event, COUNT(*) as event_count
                        FROM chat_attribution_events
                        WHERE anon_id = %s AND client_id = %s AND created_at >= %s AND event_type != 'session_started'
                        GROUP BY session_id, bot_ref
                        ORDER BY last_event DESC
                        LIMIT 1
                    """, (order.anon_id, resolved_client_id, window_start))

                    result = await cur.fetchone()
                    if result:
                        attribution_type = "assisted"
                        chat_session_id = result['session_id']
                        chat_conversation_id = result['conversation_id']
                        last_chat_event_at = result['last_event']
                        chat_messages_count = result['event_count']

                        if order.order_created_at and last_chat_event_at:
                            delta = order.order_created_at - last_chat_event_at
                            attribution_window_hours = int(delta.total_seconds() / 3600)

                # Final gate: require at least one pre-sales tagged customer
                # message in the matched conversation before the order — a
                # matched session that's only "hi" or order-status questions
                # should not be attributed.
                if attribution_type == "assisted":
                    from fashion_bot.shopify.webhook.shopify_webhook import _ahas_presales_tagged_message

                    order_time = order.order_created_at or datetime.utcnow()
                    try:
                        has_presales_signal = await _ahas_presales_tagged_message(
                            cur, chat_conversation_id, order_time,
                            client_id=resolved_client_id,
                            session_id=chat_session_id,
                            last_chat_event_at=last_chat_event_at,
                        )
                    except Exception as presales_err:
                        logger.warning(f"Pre-sales tag check failed: {presales_err}")
                        has_presales_signal = False

                    if not has_presales_signal:
                        attribution_type = "none"
                        chat_session_id = None
                        last_chat_event_at = None
                        chat_messages_count = 0
                        attribution_window_hours = None

                # Insert order attribution
                await cur.execute("""
                    INSERT INTO order_attribution (
                        order_id, order_number, client_id,
                        bot_ref, anon_id,
                        attribution_type, attribution_window_hours,
                        order_total, order_currency, order_items_count,
                        chat_session_id, last_chat_event_at, chat_messages_count,
                        order_created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (order_id, client_id) DO UPDATE SET
                        attribution_type = EXCLUDED.attribution_type,
                        attributed_at = CURRENT_TIMESTAMP
                    RETURNING id
                """, (
                    order.order_id,
                    order.order_number,
                    resolved_client_id,
                    order.bot_ref,
                    order.anon_id,
                    attribution_type,
                    attribution_window_hours,
                    order.order_total,
                    order.order_currency,
                    order.order_items_count,
                    chat_session_id,
                    last_chat_event_at,
                    chat_messages_count,
                    order.order_created_at
                ))
                
                result = await cur.fetchone()
                attribution_id = result['id'] if result else None
                
                logger.info(f"📦 Order attributed: {order.order_id} -> {attribution_type}")
                
                return {
                    "success": True,
                    "attribution_id": attribution_id,
                    "attribution_type": attribution_type,
                    "attribution_window_hours": attribution_window_hours
                }
        
    except HTTPException:
        # Re-raise HTTP exceptions (like validation errors)
        raise
    except Exception as e:
        logger.error(f"❌ Failed to attribute order: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# KPI QUERY ENDPOINTS
# ============================================================

@router.get("/kpi/summary/{client_id}")
async def get_kpi_summary(client_id: str, days: int = 30):
    """
    Get KPI summary for a client.
    Returns key metrics for the attribution dashboard.
    
    Optimized: Uses single DB connection for all queries.
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Resolve client_id using the SAME cursor
                try:
                    resolved_client_id = await aresolve_client_id(client_id, cursor=cur)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error resolving client_id: {str(e)}"
                    )
                
                start_date = datetime.utcnow() - timedelta(days=days)
                
                logger.info(f"📊 KPI Summary query: client_id={client_id} (resolved: {resolved_client_id}), days={days}, start_date={start_date}")
                
                # Debug: Check total orders for this client (without date filter)
                await cur.execute("""
                    SELECT COUNT(*) as total_count
                    FROM order_attribution
                    WHERE client_id::text = %s
                """, (resolved_client_id,))
                debug_result = await cur.fetchone()
                total_orders_debug = debug_result['total_count'] if debug_result else 0
                logger.info(f"📊 Debug: Total orders for client (no date filter): {total_orders_debug}")
                
                # Get order attribution summary
                await cur.execute("""
                    SELECT 
                        attribution_type,
                        COUNT(*) as order_count,
                        COALESCE(SUM(order_total), 0) as total_revenue,
                        COALESCE(AVG(order_total), 0) as avg_order_value,
                        COALESCE(AVG(chat_messages_count), 0) as avg_messages
                    FROM order_attribution
                    WHERE client_id::text = %s 
                    AND attributed_at >= %s
                    GROUP BY attribution_type
                """, (resolved_client_id, start_date))
                
                attribution_summary = await cur.fetchall()
                logger.info(f"📊 Found {len(attribution_summary)} attribution type groups: {[r['attribution_type'] for r in attribution_summary]}")
                
                # Get event counts
                await cur.execute("""
                    SELECT 
                        event_type,
                        COUNT(*) as count
                    FROM chat_attribution_events
                    WHERE client_id::text = %s AND created_at >= %s
                    GROUP BY event_type
                """, (resolved_client_id, start_date))
                
                event_counts = await cur.fetchall()
                
                # Get unique sessions
                await cur.execute("""
                    SELECT COUNT(DISTINCT bot_ref) as unique_sessions
                    FROM chat_attribution_events
                    WHERE client_id::text = %s AND created_at >= %s
                """, (resolved_client_id, start_date))
                
                sessions_result = await cur.fetchone()
                unique_sessions = sessions_result['unique_sessions'] if sessions_result else 0
                
                # Calculate totals
                total_orders = sum(row['order_count'] for row in attribution_summary)
                total_revenue = sum(row['total_revenue'] for row in attribution_summary)
                direct_orders = next((row['order_count'] for row in attribution_summary if row['attribution_type'] == 'direct'), 0)
                assisted_orders = next((row['order_count'] for row in attribution_summary if row['attribution_type'] == 'assisted'), 0)
                direct_revenue = next((row['total_revenue'] for row in attribution_summary if row['attribution_type'] == 'direct'), 0)
                assisted_revenue = next((row['total_revenue'] for row in attribution_summary if row['attribution_type'] == 'assisted'), 0)
                
                return {
                    "client_id": client_id,
                    "period_days": days,
                    "summary": {
                        "total_attributed_orders": total_orders,
                        "total_attributed_revenue": float(total_revenue),
                        "direct_orders": direct_orders,
                        "direct_revenue": float(direct_revenue),
                        "assisted_orders": assisted_orders,
                        "assisted_revenue": float(assisted_revenue),
                        "unique_chat_sessions": unique_sessions,
                        "conversion_rate": (total_orders / unique_sessions * 100) if unique_sessions > 0 else 0
                    },
                    "event_breakdown": {row['event_type']: row['count'] for row in event_counts},
                    "attribution_breakdown": {row['attribution_type']: {
                        "orders": row['order_count'],
                        "revenue": float(row['total_revenue']),
                        "avg_order_value": float(row['avg_order_value']),
                        "avg_messages": float(row['avg_messages'])
                    } for row in attribution_summary}
                }
        
    except HTTPException:
        # Re-raise HTTP exceptions (like validation errors)
        raise
    except Exception as e:
        logger.error(f"❌ Failed to get KPI summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/kpi/daily/{client_id}")
async def get_daily_kpi(client_id: str, days: int = 30):
    """
    Get daily KPI breakdown for charting.
    
    Optimized: Uses single DB connection for all queries.
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Resolve client_id using the SAME cursor
                try:
                    resolved_client_id = await aresolve_client_id(client_id, cursor=cur)
                except ValueError as e:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Error resolving client_id: {str(e)}"
                    )
                
                start_date = datetime.utcnow() - timedelta(days=days)
                
                # Get daily order attribution
                await cur.execute("""
                    SELECT 
                        DATE(COALESCE(order_created_at, attributed_at)) as date,
                        attribution_type,
                        COUNT(*) as order_count,
                        COALESCE(SUM(order_total), 0) as revenue
                    FROM order_attribution
                    WHERE client_id::text = %s AND attributed_at >= %s
                    GROUP BY DATE(COALESCE(order_created_at, attributed_at)), attribution_type
                    ORDER BY date DESC
                """, (resolved_client_id, start_date))
                
                daily_orders = await cur.fetchall()
                
                # Get daily chat sessions
                await cur.execute("""
                    SELECT 
                        DATE(created_at) as date,
                        COUNT(DISTINCT bot_ref) as sessions,
                        COUNT(*) as events
                    FROM chat_attribution_events
                    WHERE client_id::text = %s AND created_at >= %s
                    GROUP BY DATE(created_at)
                    ORDER BY date DESC
                """, (resolved_client_id, start_date))
                
                daily_sessions = await cur.fetchall()
                
                return {
                    "client_id": client_id,
                    "period_days": days,
                    "daily_orders": [{
                        "date": str(row['date']),
                        "attribution_type": row['attribution_type'],
                        "order_count": row['order_count'],
                        "revenue": float(row['revenue'])
                    } for row in daily_orders],
                    "daily_sessions": [{
                        "date": str(row['date']),
                        "sessions": row['sessions'],
                        "events": row['events']
                    } for row in daily_sessions]
                }
        
    except HTTPException:
        # Re-raise HTTP exceptions (like validation errors)
        raise
    except Exception as e:
        logger.error(f"❌ Failed to get daily KPI: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/session/{bot_ref}")
async def get_session_events(bot_ref: str):
    """
    Get all events for a specific chat session.
    Useful for debugging and detailed analysis.
    
    Optimized: Uses single DB connection for all queries.
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT *
                    FROM chat_attribution_events
                    WHERE bot_ref = %s
                    ORDER BY created_at ASC
                """, (bot_ref,))
                
                events = await cur.fetchall()
                
                # Get any attributed orders
                await cur.execute("""
                    SELECT *
                    FROM order_attribution
                    WHERE bot_ref = %s
                """, (bot_ref,))
                
                orders = await cur.fetchall()
                
                return {
                    "bot_ref": bot_ref,
                    "event_count": len(events),
                    "events": [{
                        "id": e['id'],
                        "event_type": e['event_type'],
                        "page_type": e['page_type'],
                        "product_title": e['product_title'],
                        "created_at": str(e['created_at'])
                    } for e in events],
                    "attributed_orders": [{
                        "order_id": o['order_id'],
                        "order_total": float(o['order_total']) if o['order_total'] else None,
                        "attribution_type": o['attribution_type']
                    } for o in orders]
                }
        
    except Exception as e:
        logger.error(f"❌ Failed to get session events: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# HEALTH & INITIALIZATION
# ============================================================

@router.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "attribution"}


@router.post("/init-tables")
async def init_tables():
    """Initialize attribution tables (for setup)."""
    try:
        ensure_tables()
        return {"success": True, "message": "Attribution tables initialized"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
