"""
Conversation Classification System - Helper Functions

This module provides functions to manage conversation classification for the
"Focused/All" view system, distinguishing between:
- user_initiated: Customer starts the conversation
- template_initiated: Template sent, awaiting response
- template_converted: Customer replied to template

This enables accurate billing and cleaner agent views.
"""

import logging
import json
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from fashion_bot.database_manager import awith_retry, get_async_postgres_connection
from fashion_bot.utils.shared_utils import strip_nul_bytes

logger = logging.getLogger(__name__)


async def acreate_template_conversation(
    client_id: str,
    phone: str,
    template_message: str,
    channel_type: str = "whatsapp",
    customer_id: Optional[str] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    template_id: Optional[str] = None,
    template_name: Optional[str] = None
) -> Optional[str]:
    """
    Create a new template-initiated conversation (async).

    This creates a conversation that:
    - Is marked as template_initiated
    - Is non-billable (until customer responds)
    - Hidden from "Focused" view by default
    """
    try:
        import uuid

        logger.info(f"[TEMPLATE_CONV] 📤 Creating template-initiated conversation")
        logger.info(f"[TEMPLATE_CONV] Phone: {phone}, Client: {client_id}")

        now = datetime.now(timezone.utc)
        conversation_id = str(uuid.uuid4())

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    INSERT INTO conversations (
                        conversation_id, client_id, customer_id, channel_type,
                        status, tags, first_message, started_by, phone,
                        customer_info, created_at, updated_at, created_by,
                        conversation_type, is_billable, template_send_count
                    ) VALUES (
                        %s, %s, %s, %s, 'active', %s, %s, 'system', %s, %s, %s, %s, 'system',
                        'template_initiated', FALSE, 1
                    );
                """

                tags = ['Template']
                if template_name:
                    tags.append(f'Template:{template_name}')

                customer_info_json = json.dumps(strip_nul_bytes(customer_info)) if customer_info else None

                await cur.execute(sql, strip_nul_bytes((
                    conversation_id, client_id, customer_id, channel_type,
                    tags, template_message, phone, customer_info_json,
                    now, now
                )))

                message_id = str(uuid.uuid4())
                metadata = {
                    'template_id': template_id,
                    'template_name': template_name,
                    'is_template': True
                }
                metadata_json = json.dumps(strip_nul_bytes(metadata))

                msg_sql = """
                    INSERT INTO messages (
                        message_id, client_id, conversation_id, customer_id,
                        channel_type, tags, message, message_side, phone,
                        customer_info, message_metadata, created_at, updated_at,
                        created_by, message_direction, message_type
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, 'system_to_user', %s, %s, %s, %s, %s, 'bot',
                        'outbound', 'template'
                    );
                """

                await cur.execute(msg_sql, strip_nul_bytes((
                    message_id, client_id, conversation_id, customer_id,
                    channel_type, tags, template_message, phone,
                    customer_info_json, metadata_json, now, now
                )))

        logger.info(f"[TEMPLATE_CONV] ✅ Created template conversation: {conversation_id}")
        logger.info(f"[TEMPLATE_CONV]    Type: template_initiated, Billable: FALSE")

        return conversation_id

    except Exception as e:
        logger.error(f"[TEMPLATE_CONV] ❌ Error creating template conversation: {e}", exc_info=True)
        return None


@awith_retry
async def aconvert_template_to_engaged(conversation_id: str) -> bool:
    """Convert a template-initiated conversation to template_converted (engaged)."""
    logger.info(f"[CONV_CONVERT] 🔄 Converting template conversation to engaged: {conversation_id}")

    now = datetime.now(timezone.utc)

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            sql = """
                UPDATE conversations
                SET conversation_type = 'template_converted',
                    is_billable = TRUE,
                    conversion_date = %s,
                    first_customer_message_at = COALESCE(first_customer_message_at, %s),
                    updated_at = %s
                WHERE conversation_id = %s
                AND conversation_type = 'template_initiated';
            """

            await cur.execute(sql, (now, now, now, conversation_id))
            rows_affected = cur.rowcount

            if rows_affected > 0:
                logger.info(f"[CONV_CONVERT] ✅ Successfully converted conversation {conversation_id}")
                logger.info(f"[CONV_CONVERT]    Type: template_converted, Billable: TRUE")
                return True

            logger.warning(f"[CONV_CONVERT] ⚠️  No rows updated for {conversation_id}")
            logger.warning(f"[CONV_CONVERT]    Conversation may not exist or already converted")
            return False


async def aincrement_template_send_count(conversation_id: str) -> bool:
    """Increment the template send counter for a conversation (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    UPDATE conversations
                    SET template_send_count = template_send_count + 1,
                        updated_at = NOW()
                    WHERE conversation_id = %s;
                """
                await cur.execute(sql, (conversation_id,))
                logger.info(f"[TEMPLATE_COUNT] ✅ Incremented template count for {conversation_id}")
                return True

    except Exception as e:
        logger.error(f"[TEMPLATE_COUNT] ❌ Error incrementing template count: {e}", exc_info=True)
        return False


async def amark_template_no_response(conversation_id: str) -> bool:
    """Mark a template conversation as closed with no response (async)."""
    try:
        logger.info(f"[TEMPLATE_CLOSED] 🔒 Marking template as closed (no response): {conversation_id}")

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    UPDATE conversations
                    SET status = 'closed_no_response',
                        updated_at = NOW()
                    WHERE conversation_id = %s
                    AND conversation_type = 'template_initiated';
                """

                await cur.execute(sql, (conversation_id,))
                rows_affected = cur.rowcount

                if rows_affected > 0:
                    logger.info(f"[TEMPLATE_CLOSED] ✅ Marked conversation as closed_no_response")
                    return True
                else:
                    logger.warning(f"[TEMPLATE_CLOSED] ⚠️  No rows updated")
                    return False

    except Exception as e:
        logger.error(f"[TEMPLATE_CLOSED] ❌ Error marking template closed: {e}", exc_info=True)
        return False


@awith_retry
async def aget_conversation_type(conversation_id: str) -> Optional[str]:
    """Get the conversation type for a given conversation ID (async)."""
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            sql = """
                SELECT conversation_type, is_billable
                FROM conversations
                WHERE conversation_id = %s;
            """

            await cur.execute(sql, (conversation_id,))
            result = await cur.fetchone()

            if result:
                return result.get('conversation_type') if isinstance(result, dict) else result[0]
            return None


async def ais_template_conversation(conversation_id: str) -> bool:
    """Check if a conversation is template-initiated (async)."""
    conv_type = await aget_conversation_type(conversation_id)
    return conv_type == 'template_initiated'


async def aget_billable_conversation_count(
    client_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> int:
    """Get count of billable conversations for a client in a date range (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT COUNT(*)
                    FROM conversations
                    WHERE client_id = %s
                    AND is_billable = TRUE
                """

                params: list = [client_id]

                if start_date:
                    sql += " AND created_at >= %s"
                    params.append(start_date)

                if end_date:
                    sql += " AND created_at <= %s"
                    params.append(end_date)

                await cur.execute(sql, params)
                result = await cur.fetchone()

                if result:
                    count = result.get('count') if isinstance(result, dict) else result[0]
                else:
                    count = 0
                logger.info(f"[BILLING] Billable conversations for client {client_id}: {count}")
                return count

    except Exception as e:
        logger.error(f"[BILLING] ❌ Error getting billable count: {e}")
        return 0


async def aget_template_stats(
    client_id: str,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None
) -> Dict[str, Any]:
    """Get template performance statistics for a client (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT
                        COUNT(CASE WHEN conversation_type = 'template_initiated' THEN 1 END) as templates_sent,
                        COUNT(CASE WHEN conversation_type = 'template_converted' THEN 1 END) as templates_converted,
                        AVG(
                            CASE
                                WHEN conversation_type = 'template_converted' AND conversion_date IS NOT NULL
                                THEN EXTRACT(EPOCH FROM (conversion_date - created_at))
                            END
                        ) as avg_response_time_seconds
                    FROM conversations
                    WHERE client_id = %s
                    AND conversation_type IN ('template_initiated', 'template_converted')
                """

                params: list = [client_id]

                if start_date:
                    sql += " AND created_at >= %s"
                    params.append(start_date)

                if end_date:
                    sql += " AND created_at <= %s"
                    params.append(end_date)

                await cur.execute(sql, params)
                result = await cur.fetchone()

                if result:
                    if isinstance(result, dict):
                        templates_sent = result.get('templates_sent') or 0
                        templates_converted = result.get('templates_converted') or 0
                        avg_response_time = result.get('avg_response_time_seconds') or 0
                    else:
                        templates_sent = result[0] or 0
                        templates_converted = result[1] or 0
                        avg_response_time = result[2] or 0

                    response_rate = (templates_converted / templates_sent * 100) if templates_sent > 0 else 0

                    stats = {
                        "templates_sent": templates_sent,
                        "templates_converted": templates_converted,
                        "templates_no_response": templates_sent - templates_converted,
                        "response_rate_percent": round(response_rate, 2),
                        "avg_response_time_seconds": round(avg_response_time, 2),
                        "avg_response_time_minutes": round(avg_response_time / 60, 2)
                    }

                    logger.info(f"[TEMPLATE_STATS] Stats for client {client_id}: {stats}")
                    return stats

                return {
                    "templates_sent": 0,
                    "templates_converted": 0,
                    "templates_no_response": 0,
                    "response_rate_percent": 0,
                    "avg_response_time_seconds": 0,
                    "avg_response_time_minutes": 0
                }

    except Exception as e:
        logger.error(f"[TEMPLATE_STATS] ❌ Error getting template stats: {e}", exc_info=True)
        return {}


async def aclose_old_template_conversations(hours_threshold: int = 48) -> int:
    """Background job to close template conversations with no response (async)."""
    try:
        logger.info(f"[AUTO_CLOSE] 🔄 Starting auto-close job (threshold: {hours_threshold}h)")

        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=hours_threshold)

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    UPDATE conversations
                    SET status = 'closed_no_response',
                        updated_at = NOW()
                    WHERE conversation_type = 'template_initiated'
                    AND status = 'active'
                    AND created_at < %s;
                """

                await cur.execute(sql, (cutoff_time,))
                rows_affected = cur.rowcount

                logger.info(f"[AUTO_CLOSE] ✅ Closed {rows_affected} old template conversations")
                return rows_affected

    except Exception as e:
        logger.error(f"[AUTO_CLOSE] ❌ Error in auto-close job: {e}", exc_info=True)
        return 0


async def aget_focused_conversations(
    client_id: str,
    limit: int = 50,
    offset: int = 0
) -> List[Dict[str, Any]]:
    """Get conversations for 'Focused' view (engaged conversations only) (async)."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT
                        conversation_id, phone, conversation_type,
                        is_billable, status, tags, first_message,
                        created_at, updated_at
                    FROM conversations
                    WHERE client_id = %s
                    AND conversation_type IN ('user_initiated', 'template_converted')
                    AND status = 'active'
                    ORDER BY updated_at DESC
                    LIMIT %s OFFSET %s;
                """

                await cur.execute(sql, (client_id, limit, offset))
                rows = await cur.fetchall()

                conversations = []
                for row in rows:
                    conversations.append({
                        "conversation_id": str(row[0]),
                        "phone": row[1],
                        "conversation_type": row[2],
                        "is_billable": row[3],
                        "status": row[4],
                        "tags": row[5],
                        "first_message": row[6],
                        "created_at": row[7].isoformat() if row[7] else None,
                        "updated_at": row[8].isoformat() if row[8] else None
                    })

                logger.info(f"[FOCUSED_VIEW] Retrieved {len(conversations)} focused conversations")
                return conversations

    except Exception as e:
        logger.error(f"[FOCUSED_VIEW] ❌ Error getting focused conversations: {e}", exc_info=True)
        return []
