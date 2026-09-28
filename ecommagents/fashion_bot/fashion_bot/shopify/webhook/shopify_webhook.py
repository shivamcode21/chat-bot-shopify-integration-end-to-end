from fastapi import APIRouter, Request
import asyncio
import logging
import json
import time
from datetime import datetime
from typing import Optional
import httpx

from .event_processor import ShopifyEventProcessor
from ...core.conversation_runtime import ConversationRuntime, RuntimeResult, RuntimeContext
from ...trace_context import generate_trace_id, set_trace_id
from ...utils.utils import log_with_trace_id
from ...database_manager import get_async_postgres_connection
from ...config_manager import aresolve_client_id
from ...env_loader import get_env, get_int
from ...utils.redis_guard import RedisGuard
from ...utils.client_identity_cache import aget_client_id_by_shop_domain as _aget_cid_by_shop
from ..order_tags import OrderTag
from fashion_bot.monitoring.otel_metrics import set_request_client_id
# Queue producer — dramatiq-free import; offloads to a worker only when the
# 'order' lane is enabled, else awaits inline (unchanged behaviour).
from fashion_bot.workers.enqueue import submit_or_inline
from fashion_bot.workers.config import JOB_ORDER_EVENT

try:
    import redis
except Exception:
    redis = None
try:
    import certifi
except Exception:
    certifi = None

router = APIRouter()
shopify_event_processor = ShopifyEventProcessor()

SHOPIFY_RUNTIME_LOCK_TTL_SECONDS = int(get_env("PROCESSING_LOCK_TTL_SECONDS") or "90")
SHOPIFY_RUNTIME_LOCK_RETRY_DELAY_MS = int(get_env("SHOPIFY_RUNTIME_LOCK_RETRY_DELAY_MS") or "250")
SHOPIFY_RUNTIME_LOCK_MAX_RETRIES = int(get_env("SHOPIFY_RUNTIME_LOCK_MAX_RETRIES") or "8")
_SHOPIFY_RUNTIME_REDIS_URL = get_env("REDIS_URL") or get_env("REDIS_CONNECTION_STRING") or "redis://localhost:6379/0"

_shopify_runtime: Optional[ConversationRuntime] = None
_shopify_runtime_async_redis_client = None
# Monotonic timestamp of the last failed connect attempt; used to throttle
# reconnect storms while Redis is down (see SHOPIFY_RUNTIME_REDIS_RECONNECT_COOLDOWN_SECONDS).
_shopify_runtime_redis_last_connect_fail = 0.0
_shopify_runtime_redis_guard = RedisGuard()

# How long to wait between reconnect attempts after a failed connect. Prevents a
# `ping()` storm on every webhook turn while Redis is unreachable. Set to 0 to
# attempt a reconnect on every turn.
SHOPIFY_RUNTIME_REDIS_RECONNECT_COOLDOWN_SECONDS = float(
    get_int("SHOPIFY_RUNTIME_REDIS_RECONNECT_COOLDOWN_SECONDS", 5)
)


def _shopify_runtime_log(trace_id: str, message: str, level: str = "info", phone: str = "", client_id: str = None):
    log_with_trace_id(trace_id, f"[SHOPIFY_RUNTIME] {message}", level, client_id=client_id)


def _reset_shopify_runtime_redis_client():
    """Drop the cached client so the next turn re-establishes the connection.

    Called when a previously-good client starts failing (e.g. the connection
    dropped) so we don't keep reusing a dead client for the rest of the process
    lifetime.
    """
    global _shopify_runtime_async_redis_client
    _shopify_runtime_async_redis_client = None


async def _aget_shopify_runtime_redis_client():
    global _shopify_runtime_async_redis_client, _shopify_runtime_redis_last_connect_fail
    # Reuse a previously established, healthy client.
    if _shopify_runtime_async_redis_client is not None:
        return _shopify_runtime_async_redis_client
    # Throttle reconnect attempts while Redis is unreachable.
    if SHOPIFY_RUNTIME_REDIS_RECONNECT_COOLDOWN_SECONDS > 0 and _shopify_runtime_redis_last_connect_fail:
        if (time.monotonic() - _shopify_runtime_redis_last_connect_fail) < SHOPIFY_RUNTIME_REDIS_RECONNECT_COOLDOWN_SECONDS:
            return None
    try:
        import redis.asyncio as aioredis
    except Exception:
        return None
    try:
        client_kwargs = {"decode_responses": True}
        if str(_SHOPIFY_RUNTIME_REDIS_URL).lower().startswith("rediss://"):
            if certifi:
                client_kwargs["ssl_ca_certs"] = certifi.where()
            insecure = (str(get_env("REDIS_SSL_INSECURE") or "").lower() in ("1", "true", "yes"))
            if insecure:
                client_kwargs["ssl_cert_reqs"] = None
        client = aioredis.Redis.from_url(_SHOPIFY_RUNTIME_REDIS_URL, **client_kwargs)
        await client.ping()
        # Cache ONLY on a verified-healthy connection.
        _shopify_runtime_async_redis_client = client
        _shopify_runtime_redis_last_connect_fail = 0.0
        return client
    except Exception as redis_err:
        logging.warning(f"[SHOPIFY_RUNTIME] Async Redis unavailable for runtime lock path: {redis_err}")
        # Leave the client uncached so the next turn (after cooldown) retries.
        _shopify_runtime_async_redis_client = None
        _shopify_runtime_redis_last_connect_fail = time.monotonic()
        return None


def _get_shopify_runtime() -> ConversationRuntime:
    global _shopify_runtime
    if _shopify_runtime is not None:
        return _shopify_runtime

    async def _try_acquire_lock(client_id: str, user_id: str, trace_id: str):
        redis_client = await _aget_shopify_runtime_redis_client()
        if not redis_client:
            _shopify_runtime_log(trace_id, "single-flight lock unavailable (redis); fail-open", "warning", user_id, client_id=client_id)
            return False, True
        lock_key = f"conv:processing:{client_id}:{user_id}"
        lock_result = await _shopify_runtime_redis_guard.execute_async(
            op_name="shopify_single_flight_lock_setnx",
            fn=lambda: redis_client.set(lock_key, "1", nx=True, ex=SHOPIFY_RUNTIME_LOCK_TTL_SECONDS),
            fallback=False,
        )
        if not lock_result.ok:
            # The cached client may be holding a dead connection; drop it so the
            # next turn reconnects instead of staying degraded for the process life.
            _reset_shopify_runtime_redis_client()
            _shopify_runtime_log(
                trace_id,
                f"single-flight lock degraded: {lock_result.error}",
                "warning",
                user_id,
                client_id=client_id,
            )
            return False, True
        if not lock_result.value:
            return False, False
        return True, False

    def _enqueue_pending(client_id: str, user_id: str, trace_id: str, payload_data: dict) -> bool:
        _shopify_runtime_log(
            trace_id,
            f"lock busy, runtime turn queued for retry (shopify event, user={user_id})",
            "info",
            user_id,
            client_id=client_id,
        )
        return True

    async def _release_lock(client_id: str, user_id: str, trace_id: str):
        redis_client = await _aget_shopify_runtime_redis_client()
        if not redis_client:
            return
        lock_key = f"conv:processing:{client_id}:{user_id}"
        await _shopify_runtime_redis_guard.execute_async(
            op_name="shopify_single_flight_lock_delete",
            fn=lambda: redis_client.delete(lock_key),
            fallback=0,
        )

    def _no_op(*_args, **_kwargs):
        return None

    def _empty_list(*_args, **_kwargs):
        return []

    def _empty_merge(*_args, **_kwargs):
        return {}, 0, 0

    def _summary_disabled(*_args, **_kwargs):
        return False, "disabled"

    _shopify_runtime = ConversationRuntime(
        log_fn=_shopify_runtime_log,
        mark_degraded_state_fn=_no_op,
        try_acquire_lock_fn=_try_acquire_lock,
        enqueue_pending_fn=_enqueue_pending,
        release_lock_fn=_release_lock,
        drain_pending_fn=_empty_list,
        build_merged_payload_fn=_empty_merge,
        should_trigger_summary_fn=_summary_disabled,
        enqueue_summary_job_fn=_no_op,
        redispatch_fn=_no_op,
    )
    return _shopify_runtime


async def _aresolve_conversation_id_for_presales_check(
    cur, client_id: Optional[str], session_id: Optional[str], around_time
) -> Optional[str]:
    """Fallback conversation lookup for when the matched chat_attribution_events
    row never got conversation_id populated (missed insert-time lookup, failed
    async backfill, event predates either mechanism, etc).

    This is a retrospective/post-hoc check, not a real-time "is this still
    live" check, so unlike alookup_conversation_id() there's no 90-minute
    freshness requirement - we just need the conversation whose active
    window actually brackets the moment the matched chat activity happened.
    """
    if not client_id or not session_id or not around_time:
        return None
    await cur.execute(
        """
        SELECT conversation_id FROM conversations
        WHERE client_id = %s AND phone = %s AND channel_type = 'web-chat'
          AND created_at <= %s AND updated_at >= %s
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (client_id, session_id, around_time, around_time),
    )
    row = await cur.fetchone()
    return row['conversation_id'] if row else None


async def _ahas_presales_tagged_message(
    cur,
    conversation_id,
    order_time,
    *,
    client_id: Optional[str] = None,
    session_id: Optional[str] = None,
    last_chat_event_at=None,
) -> bool:
    """
    True if the matched chat conversation had at least one customer message,
    before the order was placed, tagged with a configured pre-sales/preorder
    tag (product query, pricing, discount, availability, etc.).

    Reuses the same tag config the conversation_analytics pipeline uses
    (global_configs.lead_generation_tags, falling back to a built-in list),
    so "hi" / "where is my order" don't count, but real product discussion does.

    conversation_id is frequently NULL on the matched chat_attribution_events
    row (insert-time lookup only fills it in for still-live conversations;
    the async backfill is fire-and-forget and can miss). Treating "we don't
    know the conversation" the same as "no pre-sales signal" silently drops
    genuinely assisted orders to attribution_type=none, so when it's missing
    we fall back to resolving it directly via client_id/session_id instead
    of failing closed.
    """
    if not conversation_id:
        conversation_id = await _aresolve_conversation_id_for_presales_check(
            cur, client_id, session_id, last_chat_event_at
        )
    if not conversation_id:
        return False

    from fashion_bot.analytics.conversation_analyzer import _aget_global_lead_generation_config

    lead_config = await _aget_global_lead_generation_config()
    presales_tags = {
        tag.lower()
        for tag in (lead_config.get("lead_generation_tags") or []) + (lead_config.get("preorder_lead_tags") or [])
    }
    if not presales_tags:
        return False

    await cur.execute(
        """
        SELECT tags
        FROM messages
        WHERE conversation_id = %s AND message_side = 'user_to_system' AND created_at <= %s
        """,
        (conversation_id, order_time),
    )
    rows = await cur.fetchall()
    for row in rows:
        msg_tags = row['tags'] if isinstance(row, dict) else row[0]
        if not msg_tags:
            continue
        if {str(t).lower() for t in msg_tags} & presales_tags:
            return True
    return False


async def attribute_order_from_webhook(order_data: dict, client_id: str, trace_id: str):
    """
    Extract attribution data from Shopify order and call attribution API.

    Classification (all require at least one real `message_sent` event —
    bot_ref/anon_id presence alone is NOT enough, since the widget can attach
    those tokens without the customer ever opening the chat):
    - Assisted Attribution: customer exchanged real messages with the bot
      (matched via bot_ref, then anon_id within a 72h window, then phone as a
      last resort if note_attributes were lost) before completing checkout on
      the storefront.
    - Direct Attribution: reserved for orders placed via an in-widget
      checkout flow. Not wired up yet (no in-widget checkout signal exists),
      so this currently never fires.
    - none: tokens present but no real chat session found, or nothing to go on.

    IMPORTANT: This function MUST NOT raise exceptions - always returns a dict.
    The order webhook should never fail due to attribution errors.

    Args:
        order_data: Shopify order webhook payload
        client_id: Client UUID
        trace_id: Request trace ID for logging

    Returns:
        dict with success status, or None if no attribution tokens found
    """
    try:
        # Validate inputs
        if not order_data:
            logging.warning(f"[ATTRIBUTION] [{trace_id}] No order_data provided")
            return {"success": False, "error": "No order data"}

        if not client_id:
            logging.warning(f"[ATTRIBUTION] [{trace_id}] No client_id provided")
            return {"success": False, "error": "No client_id"}

        # Extract note_attributes from order
        note_attributes = order_data.get('note_attributes') or []

        # Look for bot_ref and anon_id in note_attributes
        bot_ref = None
        anon_id = None

        for attr in note_attributes:
            if not isinstance(attr, dict):
                continue
            attr_name = str(attr.get('name', '')).lower()
            attr_value = str(attr.get('value', ''))

            if attr_name == 'bot_ref' and attr_value:
                bot_ref = attr_value
            elif attr_name == 'anon_id' and attr_value:
                anon_id = attr_value

        # Phone fallback: lets us recover the chat session when bot_ref/anon_id
        # never made it onto the order (different device, cleared cart, etc.)
        order_phone = None
        if order_data.get('shipping_address'):
            order_phone = order_data.get('shipping_address', {}).get('phone')
        if not order_phone and order_data.get('customer'):
            order_phone = order_data.get('customer', {}).get('phone')

        # Skip attribution if there's nothing at all to match on
        if not bot_ref and not anon_id and not order_phone:
            logging.info(f"[ATTRIBUTION] [{trace_id}] No attribution tokens (bot_ref/anon_id/phone) found in order")
            return None

        # Extract order details safely
        order_id = str(order_data.get('id', '') or '')
        order_number = str(order_data.get('name', '') or '')  # e.g., "#1234"
        total_price = order_data.get('total_price', 0)
        currency = str(order_data.get('currency', 'INR') or 'INR')
        items_count = len(order_data.get('line_items') or [])
        created_at = order_data.get('created_at', '')

        logging.info(f"[ATTRIBUTION] [{trace_id}] Attribution tokens found:")
        logging.info(f"[ATTRIBUTION] [{trace_id}]   bot_ref: {bot_ref[:20]}..." if bot_ref else f"[ATTRIBUTION] [{trace_id}]   bot_ref: None")
        logging.info(f"[ATTRIBUTION] [{trace_id}]   anon_id: {anon_id[:20]}..." if anon_id else f"[ATTRIBUTION] [{trace_id}]   anon_id: None")

        # Import dependencies inside try block to catch import errors
        try:
            from fashion_bot.attribution_router import aresolve_client_id
            from datetime import timedelta
        except ImportError as ie:
            logging.error(f"[ATTRIBUTION] [{trace_id}] Import error: {ie}")
            return {"success": False, "error": f"Import error: {ie}"}

        # Resolve client_id
        try:
            resolved_client_id = await aresolve_client_id(client_id)
        except Exception as resolve_err:
            logging.error(f"[ATTRIBUTION] [{trace_id}] Failed to resolve client_id: {resolve_err}")
            return {"success": False, "error": f"Client resolution failed: {resolve_err}"}

        # Determine attribution type
        attribution_type = "none"
        attribution_window_hours = None
        chat_session_id = None
        last_chat_event_at = None
        chat_messages_count = 0

        from datetime import datetime as dt
        try:
            order_time = dt.fromisoformat(created_at.replace('Z', '+00:00')) if created_at else dt.utcnow()
        except Exception:
            order_time = dt.utcnow()
        window_start = order_time - timedelta(hours=72)

        # Use context manager for database connection
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Path A: bot_ref match — only counts as a real chat if at
                # least one non-session_started event exists (i.e. the
                # customer actually engaged, not just loaded the widget).
                chat_conversation_id = None

                if bot_ref:
                    try:
                        await cur.execute("""
                            SELECT session_id, (array_agg(conversation_id ORDER BY created_at DESC))[1] as conversation_id, MAX(created_at) as last_event, COUNT(*) as event_count
                            FROM chat_attribution_events
                            WHERE bot_ref = %s AND client_id = %s AND event_type != 'session_started'
                            GROUP BY session_id
                            ORDER BY last_event DESC
                            LIMIT 1
                        """, (bot_ref, resolved_client_id))

                        result = await cur.fetchone()
                        if result:
                            attribution_type = "assisted"
                            chat_session_id = result['session_id']
                            chat_conversation_id = result['conversation_id']
                            last_chat_event_at = result['last_event']
                            chat_messages_count = result['event_count']
                            logging.info(f"[ATTRIBUTION] [{trace_id}] Assisted attribution (bot_ref match) - session: {chat_session_id}")
                        else:
                            logging.info(f"[ATTRIBUTION] [{trace_id}] bot_ref present but no real chat activity found; not attributing")
                    except Exception as direct_err:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] bot_ref attribution lookup failed: {direct_err}")

                # Path B: anon_id match within window (also requires real activity)
                if attribution_type == "none" and anon_id:
                    try:
                        await cur.execute("""
                            SELECT session_id, bot_ref, (array_agg(conversation_id ORDER BY created_at DESC))[1] as conversation_id, MAX(created_at) as last_event, COUNT(*) as event_count
                            FROM chat_attribution_events
                            WHERE anon_id = %s AND client_id = %s AND created_at >= %s AND event_type != 'session_started'
                            GROUP BY session_id, bot_ref
                            ORDER BY last_event DESC
                            LIMIT 1
                        """, (anon_id, resolved_client_id, window_start))

                        result = await cur.fetchone()
                        if result:
                            attribution_type = "assisted"
                            chat_session_id = result['session_id']
                            chat_conversation_id = result['conversation_id']
                            last_chat_event_at = result['last_event']
                            chat_messages_count = result['event_count']
                            logging.info(f"[ATTRIBUTION] [{trace_id}] Assisted attribution (anon_id match) - session: {chat_session_id}")
                    except Exception as assisted_err:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] anon_id attribution lookup failed: {assisted_err}")

                # Path C: phone fallback — recovers the session when bot_ref/anon_id
                # didn't survive to checkout (different device, cart cleared, etc.)
                if attribution_type == "none" and order_phone:
                    try:
                        await cur.execute("""
                            SELECT session_id, bot_ref, anon_id, (array_agg(conversation_id ORDER BY created_at DESC))[1] as conversation_id, MAX(created_at) as last_event, COUNT(*) as event_count
                            FROM chat_attribution_events
                            WHERE phone_number = %s AND client_id = %s AND created_at >= %s AND event_type != 'session_started'
                            GROUP BY session_id, bot_ref, anon_id
                            ORDER BY last_event DESC
                            LIMIT 1
                        """, (order_phone, resolved_client_id, window_start))

                        result = await cur.fetchone()
                        if result:
                            attribution_type = "assisted"
                            chat_session_id = result['session_id']
                            chat_conversation_id = result['conversation_id']
                            last_chat_event_at = result['last_event']
                            chat_messages_count = result['event_count']
                            bot_ref = bot_ref or result['bot_ref']
                            anon_id = anon_id or result['anon_id']
                            logging.info(f"[ATTRIBUTION] [{trace_id}] Assisted attribution (phone match) - session: {chat_session_id}")
                    except Exception as phone_err:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] phone attribution lookup failed: {phone_err}")

                # Path D: WhatsApp-native match — bot_ref/anon_id/session_id are
                # web-widget-only constructs, so a customer who discusses
                # products purely on WhatsApp and later orders under the same
                # phone number never produces any chat_attribution_events row
                # at all (Paths A-C all key off that table). WhatsApp
                # conversations are keyed by their own real phone number from
                # message one, so match directly against conversations/messages.
                if attribution_type == "none" and order_phone:
                    try:
                        from fashion_bot.repository.bot_user_agent_mode import normalize_phone_for_db
                        normalized_order_phone = normalize_phone_for_db(order_phone)

                        await cur.execute("""
                            SELECT conversation_id
                            FROM conversations
                            WHERE client_id = %s AND phone = %s AND channel_type = 'whatsapp'
                              AND updated_at >= %s
                            ORDER BY updated_at DESC
                            LIMIT 1
                        """, (resolved_client_id, normalized_order_phone, window_start))

                        conv_row = await cur.fetchone()
                        if conv_row:
                            wa_conversation_id = conv_row['conversation_id']
                            await cur.execute("""
                                SELECT COUNT(*) AS event_count, MAX(created_at) AS last_event
                                FROM messages
                                WHERE conversation_id = %s AND message_side = 'user_to_system' AND created_at <= %s
                            """, (wa_conversation_id, order_time))
                            msg_row = await cur.fetchone()
                            if msg_row and msg_row['event_count']:
                                attribution_type = "assisted"
                                chat_session_id = normalized_order_phone
                                chat_conversation_id = wa_conversation_id
                                last_chat_event_at = msg_row['last_event']
                                chat_messages_count = msg_row['event_count']
                                logging.info(
                                    f"[ATTRIBUTION] [{trace_id}] Assisted attribution (WhatsApp phone match) - "
                                    f"conversation: {wa_conversation_id}"
                                )
                    except Exception as wa_err:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] WhatsApp phone attribution lookup failed: {wa_err}")

                # Final gate: a matched chat session only counts if it actually
                # contained pre-sales talk (product/price/discount/etc.), not
                # just a greeting or a post-purchase support question like
                # "where is my order". Without this, any chat at all (even
                # "hi") would falsely attribute the order.
                if attribution_type == "assisted":
                    try:
                        has_presales_signal = await _ahas_presales_tagged_message(
                            cur, chat_conversation_id, order_time,
                            client_id=resolved_client_id,
                            session_id=chat_session_id,
                            last_chat_event_at=last_chat_event_at,
                        )
                    except Exception as presales_err:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] Pre-sales tag check failed: {presales_err}")
                        has_presales_signal = False

                    if not has_presales_signal:
                        logging.info(
                            f"[ATTRIBUTION] [{trace_id}] Chat session {chat_session_id} matched but had no "
                            f"pre-sales tagged messages before the order; not attributing"
                        )
                        attribution_type = "none"
                        chat_session_id = None
                        last_chat_event_at = None
                        chat_messages_count = 0

                if attribution_type == "assisted":
                    attribution_window_hours = 72

                # Insert order attribution
                attribution_id = None
                try:
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
                        order_id,
                        order_number,
                        resolved_client_id,
                        bot_ref,
                        anon_id,
                        attribution_type,
                        attribution_window_hours,
                        float(total_price) if total_price else 0,
                        currency,
                        items_count,
                        chat_session_id,
                        last_chat_event_at,
                        chat_messages_count,
                        created_at or None
                    ))
                    
                    result = await cur.fetchone()
                    attribution_id = result['id'] if result else None
                except Exception as insert_err:
                    logging.error(f"[ATTRIBUTION] [{trace_id}] Failed to insert attribution: {insert_err}")
                    return {"success": False, "error": f"Insert failed: {insert_err}"}
        
        logging.info(f"[ATTRIBUTION] [{trace_id}] ✅ Order attributed: {order_number} -> {attribution_type}")
        logging.info(f"[ATTRIBUTION] [{trace_id}]   Order ID: {order_id}")
        logging.info(f"[ATTRIBUTION] [{trace_id}]   Total: {currency} {total_price}")
        logging.info(f"[ATTRIBUTION] [{trace_id}]   Attribution ID: {attribution_id}")
        
        return {
            "success": True,
            "attribution_id": str(attribution_id) if attribution_id else None,
            "attribution_type": attribution_type,
            "order_number": order_number
        }
        
    except Exception as e:
        logging.error(f"[ATTRIBUTION] [{trace_id}] ❌ Attribution failed: {e}", exc_info=True)
        return {"success": False, "error": str(e)}


async def aget_client_id_from_shop_domain(shop_domain: str) -> Optional[str]:
    """Resolve shop domain to client_id via unified tiered cache."""
    return await _aget_cid_by_shop(shop_domain)


async def _inject_order_event_into_conversation(
    phone: str,
    client_id: str,
    order_id: str,
    shopify_topic: str,
    financial_status: str,
    fulfillment_status: str,
    trace_id: str,
    cancelled_at: str = None,
    tags: str = "",
    note: str = "",
) -> None:
    """
    Append an order-event SystemMessage through ConversationRuntime single-flight.

    This keeps Shopify-originated state mutation on the same runtime lock path
    used by interactive chat turns.

    ``via_agent`` is derived from **multiple signals** because different
    agent operations leave different markers:

    * **Order creation** → ``BLOOMERCE_CREATED`` tag  (order_creation_api.py)
    * **Order update (size/product change)** → ``BLOOMERCE_UPDATED`` tag
    * **Any agent-performed update** → ``BLOOMERCE_EDITED`` tag
    * **Size update**    → note contains ``"(Automated via chatbot)"``
                           (order_editing_graphql.py)
    * **Cancellation**   → note contains ``"(Order cancelled by chatbot)"``
                           (order_cancellation_api.py)
    * **Address / phone / email update** → ``ORDER_ASSISTANCE`` tag
                           (order_updation_api.py)
    """
    from fashion_bot.state_cache import inject_order_event, UnifiedStateCache

    # Derive action from webhook topic
    if cancelled_at:
        action = "cancelled"
    elif "create" in shopify_topic or "paid" in shopify_topic:
        action = "created"
    else:
        action = "updated"

    # Determine if the order was created/modified by the chatbot agent.
    # Check multiple signals — different agent operations leave different markers.
    _tags = (tags or "").upper()
    _note = (note or "").lower()
    via_agent = (
        OrderTag.BLOOMERCE_CREATED in _tags                # new order creation
        or OrderTag.BLOOMERCE_UPDATED in _tags             # size/product change clone
        or OrderTag.BLOOMERCE_EDITED in _tags              # any agent-performed order update
        or OrderTag.ORDER_ASSISTANCE in _tags              # address/phone/email update
        or "chatbot" in _note                              # size update / cancellation notes
    )

    runtime = _get_shopify_runtime()
    lock_user_id = UnifiedStateCache.normalize_phone(phone or "")
    if not lock_user_id:
        lock_user_id = str(phone or "")

    payload_data = {
        "_runtime_type": "shopify_order_event_inject",
        "_runtime_message_text": f"[order_event] {order_id}:{action}",
        "phone": phone,
        "order_id": order_id,
        "action": action,
    }

    async def _execute_event_update(_ctx: RuntimeContext) -> RuntimeResult:
        injected = await inject_order_event(
            phone=phone,
            client_id=client_id,
            order_id=order_id,
            action=action,
            via_agent=via_agent,
        )
        if injected:
            logging.info(
                f"[ORDER_EVENT_INJECT] [{trace_id}] ✅ Order event injected via runtime for "
                f"order={order_id}, action={action}, via_agent={via_agent}"
            )
        else:
            logging.info(
                f"[ORDER_EVENT_INJECT] [{trace_id}] No active conversation — skipped"
            )
        return RuntimeResult(
            handled=bool(injected),
            queued=False,
            reply_text="",
            state_snapshot=None,
        )

    attempt = 0
    while attempt <= SHOPIFY_RUNTIME_LOCK_MAX_RETRIES:
        runtime_result = await runtime.run_turn(
            channel="whatsapp",
            client_id=client_id,
            user_id=lock_user_id,
            inbound_payload=payload_data,
            execute_fn=_execute_event_update,
            trace_id=trace_id,
        )
        if not runtime_result.queued:
            return
        attempt += 1
        if attempt > SHOPIFY_RUNTIME_LOCK_MAX_RETRIES:
            logging.critical(
                "[ORDER_EVENT_INJECT] [%s] [STATE_FLOW_FATAL] runtime lock busy beyond retry budget; "
                "order event injection skipped (order=%s, action=%s, retries=%s)",
                trace_id,
                order_id,
                action,
                SHOPIFY_RUNTIME_LOCK_MAX_RETRIES,
            )
            return
        await asyncio.sleep(max(0.01, SHOPIFY_RUNTIME_LOCK_RETRY_DELAY_MS / 1000.0))


async def _process_order_event(
    webhook_data: dict, client_id: str, trace_id: str, shopify_topic: str
) -> dict:
    """Core order-webhook processing, runnable inline or in a Dramatiq worker.

    Dedup-aware event processing (process_webhook_event has its own multi-layer
    dedup) + non-blocking conversation-context injection + attribution. Extracted
    verbatim from the route so the queued and inline paths are identical. Returns
    the processor result dict.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    order_id = webhook_data.get('name', 'UNKNOWN')
    financial_status = webhook_data.get('financial_status', 'N/A')
    fulfillment_status = webhook_data.get('fulfillment_status', 'N/A')
    phone = None
    if webhook_data.get('shipping_address'):
        phone = webhook_data.get('shipping_address', {}).get('phone')
    if not phone and webhook_data.get('customer'):
        phone = webhook_data.get('customer', {}).get('phone')

    result = await shopify_event_processor.process_webhook_event(webhook_data, client_id=client_id)

    # INJECT ORDER EVENT CONTEXT INTO ACTIVE CONVERSATION (NON-BLOCKING)
    try:
        dedup_layer = result.get('dedup_layer')
        is_duplicate = dedup_layer in ('memory_cache', 'redis_cache', 'database')
        if phone and client_id and result.get('success') and not is_duplicate:
            await _inject_order_event_into_conversation(
                phone=phone,
                client_id=client_id,
                order_id=order_id,
                shopify_topic=shopify_topic,
                financial_status=financial_status,
                fulfillment_status=fulfillment_status,
                cancelled_at=webhook_data.get('cancelled_at'),
                trace_id=trace_id,
                tags=webhook_data.get('tags', ''),
                note=webhook_data.get('note', ''),
            )
    except Exception as inject_err:
        logging.warning(f"[ORDER_EVENT_INJECT] [{trace_id}] Non-critical error: {inject_err}")

    # CHATBOT ATTRIBUTION TRACKING (NON-BLOCKING) — must never break the webhook.
    try:
        if shopify_topic in ['orders/create', 'orders/paid'] and client_id:
            try:
                attribution_result = await attribute_order_from_webhook(webhook_data, client_id, trace_id)
                if attribution_result:
                    if attribution_result.get('success'):
                        attr_type = attribution_result.get('attribution_type', 'unknown')
                        logging.info(f"[ATTRIBUTION] [{trace_id}] ✅ Order attributed: {attr_type}")
                        print(f"[{timestamp}] 📊 ATTRIBUTION: {attr_type.upper()} - Order: {order_id}")
                    else:
                        logging.warning(f"[ATTRIBUTION] [{trace_id}] Attribution returned failure: {attribution_result.get('error', 'unknown')}")
                else:
                    logging.info(f"[ATTRIBUTION] [{trace_id}] No attribution tokens found in order")
            except Exception as attr_err:
                logging.error(f"[ATTRIBUTION] [{trace_id}] Attribution error (non-fatal): {attr_err}", exc_info=True)
                print(f"[{timestamp}] ⚠️ ATTRIBUTION ERROR (ignored): {str(attr_err)[:100]}")
    except Exception as outer_attr_err:
        logging.error(f"[ATTRIBUTION] [{trace_id}] Unexpected attribution error (ignored): {outer_attr_err}")

    return result


@router.post("/webhook")
# NOTE: @traceable removed - only conversation traces should go to LangSmith, not webhook handlers
async def shopify_webhook(request: Request):
    trace_id = generate_trace_id()
    set_trace_id(trace_id)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    log_with_trace_id(trace_id, f"Webhook started", "info")

    try:
        webhook_data = await request.json()
        
        # ============================================================================
        # SHOPIFY HEADERS LOGGING - Extract Shopify-specific headers
        # ============================================================================
        shopify_topic = request.headers.get('X-Shopify-Topic', 'N/A')
        shopify_hmac = request.headers.get('X-Shopify-Hmac-Sha256', 'N/A')
        shopify_shop_domain = request.headers.get('X-Shopify-Shop-Domain', 'N/A')
        shopify_webhook_id = request.headers.get('X-Shopify-Webhook-Id', 'N/A')
        shopify_triggered_at = request.headers.get('X-Shopify-Triggered-At', 'N/A')
        shopify_event_id = request.headers.get('X-Shopify-Event-Id', 'N/A')
        
        from fashion_bot.utils.client_id_utils import is_client_blocklisted, is_shop_domain_blocklisted
        if is_shop_domain_blocklisted(shopify_shop_domain):
            logging.info(
                f"🚫 Blocked Shopify webhook for blocklisted Shopify domain: "
                f"{shopify_shop_domain} topic={shopify_topic}"
            )
            return {"status": "blocked", "message": "Shop domain is blocklisted"}

        # ============================================================================
        # FETCH CLIENT ID FROM SHOP DOMAIN
        # ============================================================================
        client_id = None
        if shopify_shop_domain and shopify_shop_domain != 'N/A':
            client_id = await aget_client_id_from_shop_domain(shopify_shop_domain)
        
        # Fallback to default client_id if not found
        if not client_id:
            client_id = await aresolve_client_id()
            logging.warning(f"Using default client_id: {client_id} for shop domain: {shopify_shop_domain}")
        else:
            logging.info(f"[CLIENT: {client_id}] Mapped shop domain: {shopify_shop_domain}")

        set_request_client_id(client_id)
        
        if is_client_blocklisted(client_id):
            logging.info(f"🚫 Blocked Shopify webhook for blocklisted client_id: {client_id}")
            return {"status": "blocked", "message": "Client is blocklisted"}
        # ============================================================================

        # Log headers to console (print)
        print(f"\n{'='*80}")
        print(f"[{timestamp}] SHOPIFY WEBHOOK HEADERS")
        print(f"{'='*80}")
        print(f"  X-Shopify-Topic:        {shopify_topic}")
        print(f"  X-Shopify-Shop-Domain:  {shopify_shop_domain} ⭐")
        print(f"  X-Shopify-Webhook-Id:   {shopify_webhook_id}")
        print(f"  X-Shopify-Event-Id:     {shopify_event_id}")
        print(f"  X-Shopify-Triggered-At: {shopify_triggered_at}")
        print(f"  X-Shopify-Hmac-Sha256:  {shopify_hmac[:20]}..." if len(shopify_hmac) > 20 else f"  X-Shopify-Hmac-Sha256:  {shopify_hmac}")
        print(f"{'='*80}\n")
        
        # Log headers to file
        logging.info(f"{'='*80}")
        logging.info(f"[WEBHOOK-HEADERS] Shopify webhook headers received")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Topic: {shopify_topic}")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Shop-Domain: {shopify_shop_domain}")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Webhook-Id: {shopify_webhook_id}")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Event-Id: {shopify_event_id}")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Triggered-At: {shopify_triggered_at}")
        logging.info(f"[WEBHOOK-HEADERS] X-Shopify-Hmac-Sha256: {shopify_hmac[:20]}..." if len(shopify_hmac) > 20 else f"[WEBHOOK-HEADERS] X-Shopify-Hmac-Sha256: {shopify_hmac}")
        logging.info(f"{'='*80}")
        
        order_id = webhook_data.get('name', 'UNKNOWN')
        order_number = webhook_data.get('order_number', 'N/A')
        financial_status = webhook_data.get('financial_status', 'N/A')
        fulfillment_status = webhook_data.get('fulfillment_status', 'N/A')
        
        # Extract phone for tracking
        phone = None
        if webhook_data.get('shipping_address'):
            phone = webhook_data.get('shipping_address', {}).get('phone')
        if not phone and webhook_data.get('customer'):
            phone = webhook_data.get('customer', {}).get('phone')
        phone_display = f"{phone[:4]}***{phone[-3:]}" if phone and len(phone) > 7 else phone
        
        # Log to console (print)
        print(f"\n{'='*80}")
        print(f"[{timestamp}] SHOPIFY WEBHOOK RECEIVED - Order: {order_id}")
        print(f"{'='*80}")
        print(f"  Client ID:          {client_id} 🔑")
        print(f"  Shop Domain:        {shopify_shop_domain} ⭐")
        print(f"  Order ID:           {order_id}")
        print(f"  Order Number:       {order_number}")
        print(f"  Financial Status:   {financial_status}")
        print(f"  Fulfillment Status: {fulfillment_status}")
        print(f"  Phone:              {phone_display}")
        print(f"  Trace ID:           {trace_id}")
        print(f"  Payload Received:   {json.dumps(webhook_data)[:500]}...")
        print(f"{'='*80}\n")
        
        # Log to file (logging.info)
        logging.info(f"{'='*80}")
        logging.info(f"[WEBHOOK-IN] [{timestamp}] [CLIENT: {client_id}] Shopify webhook received")
        logging.info(f"[WEBHOOK-IN] Client ID: {client_id}")
        logging.info(f"[WEBHOOK-IN] Shop Domain: {shopify_shop_domain}")
        logging.info(f"[WEBHOOK-IN] Order ID: {order_id}")
        logging.info(f"[WEBHOOK-IN] Order Number: {order_number}")
        logging.info(f"[WEBHOOK-IN] Financial Status: {financial_status}")
        logging.info(f"[WEBHOOK-IN] Fulfillment Status: {fulfillment_status}")
        logging.info(f"[WEBHOOK-IN] Phone: {phone_display}")
        logging.info(f"[WEBHOOK-IN] Customer Email: {webhook_data.get('email', 'N/A')}")
        logging.info(f"[WEBHOOK-IN] Total Price: {webhook_data.get('total_price', 'N/A')}")
        logging.info(f"[WEBHOOK-IN] Created At: {webhook_data.get('created_at', 'N/A')}")
        logging.info(f"[WEBHOOK-IN] Updated At: {webhook_data.get('updated_at', 'N/A')}")
        logging.info(f"[WEBHOOK-IN] Trace ID: {trace_id}")
        logging.info(f"[WEBHOOK-IN] Payload: {json.dumps(webhook_data)}")
        
        # Log full payload (sanitized) - useful for debugging
        sanitized_data = webhook_data.copy()
        # Remove sensitive fields if any
        if 'customer' in sanitized_data and sanitized_data['customer']:
            if 'email' in sanitized_data['customer']:
                sanitized_data['customer']['email'] = '***@***.com'
                
        # ============================================================================
        
        # Core processing (event + conversation injection + attribution) is
        # offloaded to a worker when the 'order' lane is enabled, and run inline
        # otherwise — identical behaviour with the flag off. See
        # _process_order_event below.
        result = await submit_or_inline(
            JOB_ORDER_EVENT,
            {
                "webhook_data": webhook_data,
                "client_id": client_id,
                "trace_id": trace_id,
                "shopify_topic": shopify_topic,
                "shopify_webhook_id": shopify_webhook_id,
            },
            lambda: _process_order_event(webhook_data, client_id, trace_id, shopify_topic),
        )

        # When offloaded, return fast — the inline-result logging below only
        # applies to inline processing.
        if result.get("action") == "queued":
            logging.info(f"[WEBHOOK-OUT] [{trace_id}] Order {order_id} queued for async processing")
            return {"status": "ok", "queued": True, "order_id": order_id, "client_id": client_id}

        dedup_layer = result.get('dedup_layer', 'unknown')
        if result.get('success'):
            notification_sent = result.get('notification_sent', False)
            if notification_sent:
                print(f"[{timestamp}] ✅ WEBHOOK PROCESSED - Order: {order_id}, Event: {result.get('event_name')}, Notification: SENT ✅")
            else:
                print(f"[{timestamp}] 🚫 WEBHOOK DUPLICATE BLOCKED - Order: {order_id}, Layer: {dedup_layer}")
            
            logging.info(f"[WEBHOOK-OUT] ✅ Shopify event processed: {result.get('event_name', 'Unknown')} - Order: {result.get('order_id')}")
            logging.info(f"[WEBHOOK-OUT] Notification sent: {notification_sent}")
            logging.info(f"[WEBHOOK-OUT] Deduplication layer: {dedup_layer}")
            logging.info(f"[WEBHOOK-OUT] Message: {result.get('message', 'Event processed')}")
        else:
            print(f"[{timestamp}] ❌ WEBHOOK FAILED - Order: {order_id}, Error: {result.get('error')}")
            logging.error(f"[WEBHOOK-OUT] ❌ Shopify event processing failed: {result.get('error', 'Unknown error')} - Order: {result.get('order_id')}")
        
        response = {
            "status": "ok" if result.get('success') else "error",
            "message": result.get('message', 'Event processed'),
            "order_id": result.get('order_id'),
            "event_name": result.get('event_name'),
            "notification_sent": result.get('notification_sent', False),
            "client_id": client_id,
        }
        
        print(f"[{timestamp}] 📤 WEBHOOK RESPONSE: {json.dumps(response)}\n")
        logging.info(f"[WEBHOOK-OUT] Response: {json.dumps(response)}")
        
        return response
        
    except Exception as e:
        print(f"[{timestamp}] ❌ WEBHOOK EXCEPTION: {str(e)}\n")
        logging.error(f"[WEBHOOK-OUT] ❌ Shopify webhook error: {e}", exc_info=True)
        return {"status": "error", "error": str(e)} 
