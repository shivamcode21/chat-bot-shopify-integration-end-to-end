"""
Escalation Logger - Logs customer escalations to database in real-time.
"""
import logging
import json
import os
from datetime import datetime
from typing import Optional, Dict, Any
from fashion_bot.database_manager import get_async_postgres_connection
import uuid

logger = logging.getLogger(__name__)


def _revised_query_categories_enabled() -> bool:
    """Feature flag mirror of apiandui's REVISED_QUERY_CATEGORIES (default on).

    Set REVISED_QUERY_CATEGORIES=false to disable lead emission for an environment.
    """
    return os.getenv("REVISED_QUERY_CATEGORIES", "true").strip().lower() in ("1", "true", "yes")


# PRD v2.4 R4: these escalation categories are lead-generation intents, not true
# escalations. When the flag is on they are also emitted as conversation_leads so
# they surface on the Lead Generation page. Keyed by lowercased category name.
_ESCALATION_CATEGORY_TO_LEAD_TYPE = {
    "bulk order discount": "discount_interest",
    "b2b order": "b2b_interest",
    "offline store suggestion": "offline_interest",
    "walk-in appointment": "offline_interest",
}


async def amaybe_emit_lead_from_escalation(
    *,
    client_id: str,
    conversation_id: Optional[str],
    phone_number: Optional[str],
    category: str,
) -> None:
    """If ``category`` is a lead-gen escalation category (and the flag is on),
    insert a matching conversation_leads row. Best-effort and never raises — a
    failure here must not affect escalation logging.
    """
    if not _revised_query_categories_enabled():
        return
    if not conversation_id:
        # conversation_leads dedups on conversation_id; skip when it's unknown.
        return
    lead_type = _ESCALATION_CATEGORY_TO_LEAD_TYPE.get((category or "").strip().lower())
    if not lead_type:
        return
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_leads (
                        client_id,
                        conversation_id,
                        phone_number,
                        lead_type,
                        lead_status,
                        lead_source_tags,
                        lead_customer_message_count,
                        lead_details,
                        source,
                        generated_at
                    )
                    VALUES (
                        %s::uuid, %s::uuid, %s,
                        %s, %s, %s,
                        %s, %s::jsonb,
                        'escalation_category',
                        NOW()
                    )
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(client_id),
                        str(conversation_id),
                        (phone_number or "")[:20] or None,
                        lead_type,
                        "in_market",
                        [category],
                        0,
                        json.dumps({"source_category": category}),
                    ),
                )
            await conn.commit()
        logger.info(
            f"[ESCALATION_LOG] Emitted lead ({lead_type}) from escalation category '{category}'"
        )
    except Exception as e:
        logger.warning(f"[ESCALATION_LOG] Lead emission from escalation skipped: {e}")


async def alog_escalation(
    client_id: str,
    customer_phone: str,
    category: str,
    reason: str,
    whatsapp_message: Optional[str] = None,
    customer_name: Optional[str] = None,
    conversation_id: Optional[str] = None,
    channel: Optional[str] = None,
    action_required: Optional[str] = None,
    configuration_gap: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Async variant of log_escalation for request-path logging."""
    try:
        try:
            uuid.UUID(str(client_id))
        except ValueError:
            logger.error(f"[ESCALATION_LOG] Invalid client_id UUID: {client_id}")
            return None

        if conversation_id:
            try:
                uuid.UUID(str(conversation_id))
            except ValueError:
                logger.warning(f"[ESCALATION_LOG] Invalid conversation_id UUID: {conversation_id}, setting to None")
                conversation_id = None

        escalation_id = str(uuid.uuid4())
        escalation_date = datetime.now()
        customer_phone = (customer_phone or "")[:20]
        channel = (channel or None)
        if channel:
            channel = str(channel)[:20]

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO escalations (
                        escalation_id,
                        client_id,
                        conversation_id,
                        channel,
                        customer_phone,
                        customer_name,
                        status,
                        category,
                        reason,
                        whatsapp_message,
                        action_required,
                        configuration_gap,
                        escalation_date,
                        escalation_metadata,
                        created_at,
                        updated_at,
                        created_by
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW(), 'system'
                    )
                    RETURNING escalation_id
                    """,
                    (
                        escalation_id,
                        str(client_id),
                        str(conversation_id) if conversation_id else None,
                        channel,
                        customer_phone,
                        customer_name,
                        "unresolved",
                        category,
                        reason,
                        whatsapp_message,
                        action_required,
                        configuration_gap,
                        escalation_date,
                        json.dumps(metadata) if metadata else None,
                    ),
                )
                result = await cur.fetchone()

        returned_id = result.get("escalation_id") if isinstance(result, dict) else (result[0] if result else escalation_id)
        logger.info(f"[ESCALATION_LOG] ✅ Escalation logged: ID={returned_id}, Phone={customer_phone}, Category={category}")
        logger.info(f"[ESCALATION_LOG] Reason: {reason[:100]}...")

        # PRD v2.4 R4: also surface lead-gen escalation categories on the Lead
        # Generation page (flag-gated, best-effort).
        await amaybe_emit_lead_from_escalation(
            client_id=client_id,
            conversation_id=conversation_id,
            phone_number=customer_phone,
            category=category,
        )
        return returned_id
    except Exception as e:
        logger.error(f"[ESCALATION_LOG] ❌ Async error logging escalation: {e}", exc_info=True)
        return None


