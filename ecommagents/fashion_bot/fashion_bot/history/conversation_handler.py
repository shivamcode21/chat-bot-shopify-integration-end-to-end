"""
Enhanced Conversation Handler with Classification Support

This module provides wrapper functions around postgres_conversations.py
that add support for the conversation classification system.

Key Features:
- Auto-converts template conversations when customers reply
- Properly classifies user-initiated vs template-initiated conversations
- Tracks billable vs non-billable conversations
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from fashion_bot.history.postgres_conversations import (
    astore_message_event_with_conversation_resolution as _astore_message_event_with_conversation_resolution,
)
from fashion_bot.history.conversation_classification import (
    aconvert_template_to_engaged,
    aget_conversation_type,
)
from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)

logger = logging.getLogger(__name__)


async def astore_conversation_event_with_classification(
    *,
    client_id: str,
    phone: str,
    sender: str,
    text: str,
    channel_type: str,
    started_by: Optional[str] = None,
    tags: Optional[list[str]] = None,
    customer_id: Optional[str] = None,
    customer_info: Optional[Dict[str, Any]] = None,
    conversation_id: Optional[str] = None,
    auto_convert_templates: bool = True,
    langsmith_id: Optional[str] = None
) -> str:
    """Async variant of store_conversation_event_with_classification."""
    try:
        sender_normalized = (sender or "").lower()
        is_customer_message = sender_normalized in ("customer", "user")

        logger.info(f"[CONV_HANDLER] 📨 Processing message (async)")
        logger.info(f"[CONV_HANDLER] Phone: {phone}, Sender: {sender}")
        logger.info(f"[CONV_HANDLER] Is customer message: {is_customer_message}")

        if is_customer_message and auto_convert_templates and conversation_id:
            conv_type = await aget_conversation_type(conversation_id)
            if conv_type == 'template_initiated':
                logger.info(f"[CONV_HANDLER] 🔄 Customer replied to template conversation!")
                logger.info(f"[CONV_HANDLER] Converting {conversation_id} to engaged (billable)")
                success = await aconvert_template_to_engaged(conversation_id)
                if success:
                    logger.info(f"[CONV_HANDLER] ✅ Conversion successful")
                else:
                    logger.warning(f"[CONV_HANDLER] ⚠️  Conversion failed, continuing anyway")

        conv_id = await _astore_message_event_with_conversation_resolution(
            client_id=client_id,
            phone=phone,
            sender=sender,
            text=text,
            channel_type=channel_type,
            started_by=started_by,
            tags=tags,
            customer_id=customer_id,
            customer_info=customer_info,
            conversation_id=conversation_id,
            langsmith_id=langsmith_id
        )

        if not conv_id:
            logger.error(f"[CONV_HANDLER] ❌ Failed to store conversation event")
            return None

        await _aupdate_message_metadata(conv_id, sender, text)
        if is_customer_message:
            await _arecord_first_customer_message(conv_id)

        logger.info(f"[CONV_HANDLER] ✅ Message stored in conversation: {conv_id}")
        return conv_id

    except Exception as e:
        logger.error(f"[CONV_HANDLER] ❌ Error in astore_conversation_event_with_classification: {e}", exc_info=True)
        logger.info(f"[CONV_HANDLER] 📜 Falling back to async legacy store_conversation_event")
        return await _astore_message_event_with_conversation_resolution(
            client_id=client_id,
            phone=phone,
            sender=sender,
            text=text,
            channel_type=channel_type,
            started_by=started_by,
            tags=tags,
            customer_id=customer_id,
            customer_info=customer_info,
            conversation_id=conversation_id,
            langsmith_id=langsmith_id
        )


async def astore_message_event_with_conversation_resolution(*args, **kwargs):
    """
    Async preferred explicit API name.

    Behavior mirrors the sync variant.
    """
    return await astore_conversation_event_with_classification(*args, **kwargs)


@awith_retry
async def _aupdate_message_metadata(conversation_id: str, sender: str, text: str) -> None:
    """Async variant of _update_message_metadata."""
    try:
        sender_normalized = (sender or "").lower()

        if sender_normalized in ("customer", "user"):
            message_direction = "inbound"
            message_type = "user_message"
        elif sender_normalized == "bot":
            message_direction = "outbound"
            message_type = "system_message"
        elif sender_normalized in ("agent", "support"):
            message_direction = "outbound"
            message_type = "agent_message"
        else:
            message_direction = "outbound"
            message_type = "system_message"

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    UPDATE messages
                    SET message_direction = %s,
                        message_type = %s,
                        updated_at = NOW()
                    WHERE conversation_id = %s
                    AND message_id = (
                        SELECT message_id
                        FROM messages
                        WHERE conversation_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    );
                """
                await cur.execute(sql, (message_direction, message_type, conversation_id, conversation_id))
                logger.info(f"[METADATA_UPDATE] ✅ Updated message metadata: direction={message_direction}, type={message_type}")
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.warning(f"[METADATA_UPDATE] ⚠️  Error updating message metadata: {e}")


@awith_retry
async def _arecord_first_customer_message(conversation_id: str) -> None:
    """Async variant of _record_first_customer_message."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    UPDATE conversations
                    SET first_customer_message_at = NOW()
                    WHERE conversation_id = %s
                    AND first_customer_message_at IS NULL;
                """
                await cur.execute(sql, (conversation_id,))
                if cur.rowcount > 0:
                    logger.info(f"[FIRST_MESSAGE] ✅ Recorded first customer message timestamp")
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.warning(f"[FIRST_MESSAGE] ⚠️  Error recording first customer message: {e}")


@awith_retry
async def afind_or_convert_template_conversation(
    client_id: str,
    phone: str,
    channel_type: str
) -> Optional[str]:
    """
    Find if there's an active template conversation for this phone number
    that should be converted when the customer replies.

    Returns:
        conversation_id of template conversation if found, None otherwise
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                sql = """
                    SELECT conversation_id, created_at
                    FROM conversations
                    WHERE client_id = %s
                    AND phone = %s
                    AND channel_type = %s
                    AND conversation_type = 'template_initiated'
                    AND status = 'active'
                    ORDER BY created_at DESC
                    LIMIT 1;
                """
                await cur.execute(sql, (client_id, phone, channel_type))
                result = await cur.fetchone()

                if result:
                    conv_id = result.get('conversation_id') if isinstance(result, dict) else result[0]
                    conv_id = str(conv_id)
                    logger.info(f"[TEMPLATE_FIND] 📍 Found template conversation: {conv_id}")
                    return conv_id

                logger.info(f"[TEMPLATE_FIND] No template conversation found for {phone}")
                return None

    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"[TEMPLATE_FIND] ❌ Error finding template conversation: {e}")
        return None


async def astore_conversation_event(*args, **kwargs):
    """Async drop-in replacement for store_conversation_event."""
    return await astore_message_event_with_conversation_resolution(*args, **kwargs)
