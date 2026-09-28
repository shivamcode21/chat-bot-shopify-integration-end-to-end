"""Worker-side processing for inactive conversation events."""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.database_manager import get_async_postgres_connection
from langsmith.run_helpers import tracing_context

logger = logging.getLogger(__name__)


NON_LEAD_SOURCE_TAGS = {
    "cancellation requests",
    "escalations",
    "exchange request",
    "order created",
    "order status query",
    "order update",
    "return request",
}


def _normalize_phone_for_lead_identity(phone: Optional[str]) -> Optional[str]:
    """Canonicalize real lead phones to 10 digits while preserving guest ids."""
    if phone is None:
        return None

    value = str(phone).strip()
    if not value:
        return value

    if value.startswith(("web_", "fbw_")):
        return value

    digits = re.sub(r"\D", "", value)
    if len(digits) >= 10:
        return digits[-10:]

    return value


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def _ensure_processing_tables(cur) -> None:
    from fashion_bot.Tables.conversation_inactivity_processing_table import DDL

    await cur.execute(DDL)


async def _start_processing(
    *,
    conversation_id: str,
    to_inbound_message_at: str,
    to_inbound_message_id: str,
) -> bool:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await _ensure_processing_tables(cur)
            await cur.execute(
                """
                UPDATE conversation_inactivity_cursors
                SET status = 'processing',
                    started_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                WHERE conversation_id = %s::uuid
                  AND last_queued_inbound_message_at = %s::timestamptz
                  AND last_queued_inbound_message_id = %s::uuid
                  AND (
                    last_processed_inbound_message_at IS NULL
                    OR %s::timestamptz > last_processed_inbound_message_at
                    OR (
                        %s::timestamptz = last_processed_inbound_message_at
                        AND %s::uuid::text > last_processed_inbound_message_id::text
                    )
                  )
                """,
                (
                    conversation_id,
                    to_inbound_message_at,
                    to_inbound_message_id,
                    to_inbound_message_at,
                    to_inbound_message_at,
                    to_inbound_message_id,
                ),
            )
            updated = cur.rowcount
        await conn.commit()
    return updated > 0


async def _mark_failed(conversation_id: str, error: str) -> None:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_inactivity_cursors
                SET status = 'error',
                    last_error = %s,
                    updated_at = NOW()
                WHERE conversation_id = %s::uuid
                """,
                (error[:1000], conversation_id),
            )
        await conn.commit()


async def _advance_cursor(
    *,
    conversation_id: str,
    to_inbound_message_at: str,
    to_inbound_message_id: str,
) -> None:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE conversation_inactivity_cursors
                SET last_processed_inbound_message_at = %s::timestamptz,
                    last_processed_inbound_message_id = %s::uuid,
                    status = 'done',
                    processed_at = NOW(),
                    updated_at = NOW(),
                    last_error = NULL
                WHERE conversation_id = %s::uuid
                """,
                (to_inbound_message_at, to_inbound_message_id, conversation_id),
            )
        await conn.commit()


async def _fetch_unprocessed_inbound_messages(
    *,
    conversation_id: str,
    from_cursor_at: Optional[str],
    from_cursor_message_id: Optional[str],
    to_inbound_message_at: str,
    to_inbound_message_id: str,
) -> List[Dict[str, Any]]:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT
                    m.message_id::text,
                    m.client_id::text,
                    m.conversation_id::text,
                    m.phone,
                    m.message,
                    m.tags,
                    m.created_at,
                    (
                        SELECT b.message
                        FROM messages b
                        WHERE b.conversation_id = m.conversation_id
                          AND b.message_side = 'system_to_user'
                          AND b.created_at >= m.created_at
                        ORDER BY b.created_at ASC, b.message_id ASC
                        LIMIT 1
                    ) AS bot_reply
                FROM messages m
                WHERE m.conversation_id = %s::uuid
                  AND m.message_side = 'user_to_system'
                  AND (
                    %s::timestamptz IS NULL
                    OR m.created_at > %s::timestamptz
                    OR (
                        m.created_at = %s::timestamptz
                        AND m.message_id::text > COALESCE(%s, '')
                    )
                  )
                  AND (
                    m.created_at < %s::timestamptz
                    OR (
                        m.created_at = %s::timestamptz
                        AND m.message_id::text <= %s
                    )
                  )
                ORDER BY m.created_at ASC, m.message_id ASC
                """,
                (
                    conversation_id,
                    from_cursor_at,
                    from_cursor_at,
                    from_cursor_at,
                    from_cursor_message_id,
                    to_inbound_message_at,
                    to_inbound_message_at,
                    to_inbound_message_id,
                ),
            )
            return [dict(row) for row in await cur.fetchall()]


async def _fetch_conversation_transcript(conversation_id: str) -> List[Dict[str, Any]]:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT message_id::text, message, message_side, created_at, created_by, tags
                FROM messages
                WHERE conversation_id = %s::uuid
                ORDER BY created_at ASC, message_id ASC
                """,
                (conversation_id,),
            )
            return [dict(row) for row in await cur.fetchall()]


async def _lead_exists(
    *,
    conversation_id: str,
    client_id: Optional[str],
    phone: Optional[str],
) -> bool:
    """Return true when this conversation or customer already has a non-closed lead within the last 3 days."""
    phone = _normalize_phone_for_lead_identity(phone)
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT 1
                FROM conversation_leads
                WHERE LOWER(COALESCE(lead_status, '')) <> 'closed'
                  AND created_at >= NOW() - INTERVAL '3 days'
                  AND (
                    conversation_id = %s::uuid
                    OR (
                        %s::uuid IS NOT NULL
                        AND %s::text IS NOT NULL
                        AND client_id = %s::uuid
                        AND (
                            phone_number = %s::text
                            OR (
                                %s::text ~ '^[0-9]{10}$'
                                AND RIGHT(regexp_replace(COALESCE(phone_number, ''), '\\D', '', 'g'), 10) = %s::text
                            )
                        )
                    )
                  )
                LIMIT 1
                """,
                (conversation_id, client_id, phone, client_id, phone, phone, phone),
            )
            return await cur.fetchone() is not None


async def _insert_lead_if_absent(
    *,
    conversation_id: str,
    client_id: str,
    phone: Optional[str],
    lead: Dict[str, Any],
) -> bool:
    if not lead.get("is_lead"):
        return False

    phone = _normalize_phone_for_lead_identity(phone)

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await _ensure_processing_tables(cur)
            if phone:
                await cur.execute(
                    """
                    UPDATE conversation_leads
                    SET lead_status = 'closed'
                    WHERE client_id = %s::uuid
                      AND phone_number = %s
                      AND LOWER(COALESCE(lead_status, '')) <> 'closed'
                      AND created_at < NOW() - INTERVAL '3 days'
                    """,
                    (client_id, phone),
                )
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
                    'inactive_conversation',
                    COALESCE(%s, NOW())
                )
                ON CONFLICT DO NOTHING
                """,
                (
                    client_id,
                    conversation_id,
                    phone,
                    lead.get("lead_type"),
                    lead.get("lead_status"),
                    lead.get("lead_source_tags") or [],
                    lead.get("lead_customer_message_count") or 0,
                    json.dumps(lead.get("lead_details") or {}, default=str),
                    lead.get("lead_generated_at"),
                ),
            )
            inserted = cur.rowcount > 0
        await conn.commit()
    return inserted


def _normalize_tag_values(value: Any) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _tag_lookup(tags: Any) -> set[str]:
    return {tag.strip().lower() for tag in _normalize_tag_values(tags) if tag.strip()}


def _lead_disqualification_reason(
    *,
    lead: Dict[str, Any],
    lead_config: Dict[str, List[str]],
) -> Optional[str]:
    source_lookup = _tag_lookup(lead.get("lead_source_tags"))
    if not source_lookup:
        return "missing_source_tags"

    try:
        customer_message_count = int(lead.get("lead_customer_message_count") or 0)
    except (TypeError, ValueError):
        customer_message_count = 0
    if customer_message_count < 2:
        return "customer_message_count_less_than_2"

    allowed_tags = _normalize_tag_values(lead_config.get("lead_generation_tags"))
    allowed_tags.extend(_normalize_tag_values(lead_config.get("preorder_lead_tags")))
    allowed_lookup = _tag_lookup(allowed_tags)

    denied_source_tags = source_lookup & NON_LEAD_SOURCE_TAGS
    if denied_source_tags:
        has_qualifying_lead_tag = bool(source_lookup & allowed_lookup)
        if not has_qualifying_lead_tag:
            return f"denied_source_tags={sorted(denied_source_tags)}"

    if not source_lookup & allowed_lookup:
        return "source_tags_not_configured_for_leads"

    return None


def _extract_json_object(content: str) -> Dict[str, Any]:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        import re

        match = re.search(r"\{.*\}", content, re.DOTALL)
        return json.loads(match.group(0)) if match else {}


def _message_payload(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload = []
    for msg in messages:
        payload.append(
            {
                "message_id": msg.get("message_id"),
                "customer_message": msg.get("message") or "",
                "bot_reply_context": msg.get("bot_reply") or "",
                "created_at": msg.get("created_at").isoformat()
                if hasattr(msg.get("created_at"), "isoformat")
                else str(msg.get("created_at") or ""),
            }
        )
    return payload


def _transcript_payload(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payload = []
    for msg in messages:
        payload.append(
            {
                "role": "customer" if msg.get("message_side") == "user_to_system" else "bot",
                "message": msg.get("message") or "",
                "tags": msg.get("tags") or [],
                "created_at": msg.get("created_at").isoformat()
                if hasattr(msg.get("created_at"), "isoformat")
                else str(msg.get("created_at") or ""),
            }
        )
    return payload


async def _get_conversation_analytics_prompt_reference(client_id: str) -> str:
    from fashion_bot.analytics.conversation_analyzer import _aget_prompt_from_db
    from fashion_bot.prompts.conversation_analytics_prompt import USER_PROMPT_TEMPLATE

    prompt = await _aget_prompt_from_db(client_id)
    return prompt or USER_PROMPT_TEMPLATE


async def _run_batch_tag_and_lead_prompt(
    *,
    client_id: str,
    conversation_id: str,
    phone: Optional[str],
    unprocessed_messages: List[Dict[str, Any]],
    transcript_messages: List[Dict[str, Any]],
    lead_already_exists: bool,
    trace_id: str,
) -> Dict[str, Any]:
    """One LLM call for message tags plus optional lead generation."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from fashion_bot.analytics.conversation_analyzer import _aget_global_lead_generation_config
    from fashion_bot.core.llm_config import (
        BACKGROUND_OPENROUTER_KEY_ENV,
        get_smaller_llm_config,
    )
    from fashion_bot.core.llm_factory import LLMFactory
    from fashion_bot.tag_manager import aensure_tags_loaded

    tags_dict = await aensure_tags_loaded(client_id)
    if not tags_dict:
        logger.info("[%s] No tags configured for client %s, skipping batch prompt", trace_id, client_id)
        return {"message_tags": [], "lead": {"is_lead": False}}

    lead_config = await _aget_global_lead_generation_config()
    analytics_prompt_reference = await _get_conversation_analytics_prompt_reference(client_id)

    system_prompt = (
        "You are an e-commerce conversation tagging and lead analyst. "
        "Return only valid JSON. Do not include markdown fences or commentary."
    )
    human_prompt = f"""
Classify unprocessed inbound customer messages and, only if allowed, generate one conversation lead.

== Conversation Analytics Prompt Reference ==
Use the following analytics prompt as the source of truth for interpreting conversation outcomes and lead intent:
{analytics_prompt_reference}

== Available Message Tags ==
{json.dumps(tags_dict, ensure_ascii=False)}

== Lead Generation Tag Config ==
{json.dumps(lead_config, ensure_ascii=False)}

== Job Rules ==
- For each message in `unprocessed_messages`, choose at most one exact tag from Available Message Tags.
- Use the customer message intent first; use `bot_reply_context` only as supporting context.
- Do not invent tags.
- `lead_already_exists` is {json.dumps(lead_already_exists)}.
- If `lead_already_exists` is true, set `"lead": {{"is_lead": false, "skipped": true, "reason": "lead_already_exists"}}`.
- If `lead_already_exists` is false, evaluate the whole conversation transcript for lead intent using the analytics prompt reference and Lead Generation Tag Config.
- Lead generation must be conversation-level, not message-level.
- Lead generation is allowed only when customer message count is at least 2.
- Lead generation is allowed only when the lead source tags overlap Lead Generation Tag Config.
- Never generate a lead if the lead source tags include post-order/support intent such as Order Status Query, Return Request, Exchange Request, Cancellation Requests, or Escalations, even when other tags are also present.
- Older support/order tags elsewhere in the conversation should not block a later qualified product, preorder, wholesale, pricing, delivery, or payment-options lead. In that case, include only the qualifying lead tags in `lead_source_tags`.
- Preorder-related tags should produce `lead_type = "preorder_interest"` when they qualify.
- Wholesale intent should produce `lead_type = "wholesale_interest"` when it qualifies.
- Otherwise product/purchase intent should produce `lead_type = "product_interest"`.

== Conversation Metadata ==
- conversation_id: {conversation_id}
- client_id: {client_id}
- phone: {phone or ""}

== Unprocessed Messages To Tag ==
{json.dumps(_message_payload(unprocessed_messages), ensure_ascii=False)}

== Full Conversation Transcript For Lead Decision ==
{json.dumps(_transcript_payload(transcript_messages), ensure_ascii=False)}

== Output Schema ==
Return exactly this JSON shape:
{{
  "message_tags": [
    {{"message_id": "uuid from unprocessed_messages", "tag": "exact tag or empty string"}}
  ],
  "lead": {{
    "is_lead": true | false,
    "lead_type": "preorder_interest" | "wholesale_interest" | "product_interest" | null,
    "lead_status": "in_market" | null,
    "lead_source_tags": ["tags that triggered the lead"],
    "lead_customer_message_count": 0,
    "reason": "brief reason",
    "skipped": false
  }}
}}
"""

    llm = LLMFactory.get_llm(
        tool_name="conversation_inactivity_tagging",
        override_config=get_smaller_llm_config(
            temperature=0,
            max_tokens=1600,
            api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
        ),
    )

    started = time.monotonic()
    with tracing_context(enabled=False, parent=False):
        output = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=human_prompt),
        ])
    elapsed_ms = int((time.monotonic() - started) * 1000)
    logger.info(
        "[%s] batch tag+lead prompt completed conversation_id=%s elapsed_ms=%s",
        trace_id,
        conversation_id,
        elapsed_ms,
    )
    return _extract_json_object(output.content.strip())


HIGH_INTENT_LEAD_TAGS: Dict[str, str] = {
    "wholesale inquiry": "wholesale_interest",
    "offline leads": "product_interest",
}


async def _force_lead_from_high_intent_tags(
    *,
    conversation_id: str,
    client_id: str,
    phone: Optional[str],
    classified_tags: List[str],
    lead_config: Dict[str, List[str]],
    trace_id: str,
) -> bool:
    """Force-insert a lead when high-intent tags are detected but the LLM declined."""
    classified_lookup = _tag_lookup(classified_tags)
    matched_type: Optional[str] = None
    matched_tag: Optional[str] = None
    for tag_lower, lead_type in HIGH_INTENT_LEAD_TAGS.items():
        if tag_lower in classified_lookup:
            matched_type = lead_type
            matched_tag = tag_lower
            break

    if not matched_type:
        return False

    allowed_tags = _normalize_tag_values(lead_config.get("lead_generation_tags"))
    allowed_tags.extend(_normalize_tag_values(lead_config.get("preorder_lead_tags")))
    allowed_lookup = _tag_lookup(allowed_tags)
    if matched_tag not in allowed_lookup:
        return False

    logger.info(
        "[%s] force-inserting lead from high-intent tag=%s conversation_id=%s",
        trace_id,
        matched_tag,
        conversation_id,
    )
    return await _insert_lead_if_absent(
        conversation_id=conversation_id,
        client_id=client_id,
        phone=phone,
        lead={
            "is_lead": True,
            "lead_type": matched_type,
            "lead_status": "in_market",
            "lead_source_tags": [t for t in classified_tags if t.lower() in allowed_lookup],
            "lead_customer_message_count": 0,
            "lead_generated_at": datetime.now(timezone.utc),
            "lead_details": {
                "source": "high_intent_tag_fallback",
                "reason": f"LLM declined but high-intent tag '{matched_tag}' detected",
            },
        },
    )


async def _apply_batch_tag_and_lead_result(
    *,
    client_id: str,
    conversation_id: str,
    phone: Optional[str],
    result: Dict[str, Any],
    lead_already_exists: bool,
    lead_config: Dict[str, List[str]],
    trace_id: str,
) -> Dict[str, Any]:
    from fashion_bot.history.postgres_conversations import (
        aupdate_conversation_tags,
        aupdate_message_tags_by_id,
    )

    detected_tags: List[str] = []
    for item in result.get("message_tags") or []:
        message_id = item.get("message_id")
        tag = item.get("tag")
        if not message_id or not tag:
            continue
        await aupdate_message_tags_by_id(message_id, [tag])
        detected_tags.append(tag)

    unique_tags = list(dict.fromkeys(detected_tags))
    if unique_tags:
        await aupdate_conversation_tags(conversation_id, unique_tags)
        await _update_redis_conversation_tags(
            client_id=client_id,
            phone=phone,
            tags=unique_tags,
            trace_id=trace_id,
        )

    lead_inserted = False
    lead = result.get("lead") or {}
    if not lead_already_exists and lead.get("is_lead"):
        disqualification_reason = _lead_disqualification_reason(
            lead=lead,
            lead_config=lead_config,
        )
        if disqualification_reason is None:
            lead_inserted = await _insert_lead_if_absent(
                conversation_id=conversation_id,
                client_id=client_id,
                phone=phone,
                lead={
                    "is_lead": True,
                    "lead_type": lead.get("lead_type"),
                    "lead_status": lead.get("lead_status") or "in_market",
                    "lead_source_tags": lead.get("lead_source_tags") or [],
                    "lead_customer_message_count": lead.get("lead_customer_message_count") or 0,
                    "lead_generated_at": datetime.now(timezone.utc),
                    "lead_details": {
                        "source": "conversation_inactivity_batch_prompt",
                        "reason": lead.get("reason"),
                        "raw_lead": lead,
                    },
                },
            )
        else:
            logger.info(
                "[%s] lead skipped: %s conversation_id=%s source_tags=%s classified_tags=%s",
                trace_id,
                disqualification_reason,
                conversation_id,
                lead.get("lead_source_tags") or [],
                unique_tags,
            )

    if not lead_already_exists and not lead_inserted:
        lead_inserted = await _force_lead_from_high_intent_tags(
            conversation_id=conversation_id,
            client_id=client_id,
            phone=phone,
            classified_tags=unique_tags,
            lead_config=lead_config,
            trace_id=trace_id,
        )

    return {
        "tags": unique_tags,
        "lead_inserted": lead_inserted,
    }


async def _tag_messages_and_generate_lead(
    *,
    client_id: str,
    conversation_id: str,
    phone: Optional[str],
    messages: List[Dict[str, Any]],
    trace_id: str,
) -> Dict[str, Any]:
    lead_already_exists = await _lead_exists(
        conversation_id=conversation_id,
        client_id=client_id,
        phone=phone,
    )
    from fashion_bot.analytics.conversation_analyzer import _aget_global_lead_generation_config

    lead_config = await _aget_global_lead_generation_config()
    transcript_messages = [] if lead_already_exists else await _fetch_conversation_transcript(conversation_id)
    result = await _run_batch_tag_and_lead_prompt(
        client_id=client_id,
        conversation_id=conversation_id,
        phone=phone,
        unprocessed_messages=messages,
        transcript_messages=transcript_messages,
        lead_already_exists=lead_already_exists,
        trace_id=trace_id,
    )
    applied = await _apply_batch_tag_and_lead_result(
        client_id=client_id,
        conversation_id=conversation_id,
        phone=phone,
        result=result,
        lead_already_exists=lead_already_exists,
        lead_config=lead_config,
        trace_id=trace_id,
    )
    applied["lead_already_exists"] = lead_already_exists
    return applied


async def _update_redis_conversation_tags(
    *,
    client_id: str,
    phone: Optional[str],
    tags: List[str],
    trace_id: str,
) -> None:
    if not phone or not tags:
        return
    try:
        from fashion_bot.state_cache import aget_state_by_numbers, aupdate_state

        state = await aget_state_by_numbers(phone, client_id)
        if state:
            existing_tags = state.get("conversation_tags") or []
            state["conversation_tags"] = list(set(existing_tags + tags))
            await aupdate_state(phone, client_id, state)
    except Exception as exc:
        logger.warning("[%s] Failed to update Redis tags from inactivity worker: %s", trace_id, exc)


async def process_conversation_inactivity_event(
    *,
    conversation_id: str,
    client_id: str,
    phone: Optional[str],
    from_cursor_at: Optional[str],
    from_cursor_message_id: Optional[str],
    to_inbound_message_at: str,
    to_inbound_message_id: str,
    trace_id: str,
) -> Dict[str, Any]:
    """Process one claimed quiet-window range for a conversation."""
    started = await _start_processing(
        conversation_id=conversation_id,
        to_inbound_message_at=to_inbound_message_at,
        to_inbound_message_id=to_inbound_message_id,
    )
    if not started:
        logger.info(
            "[%s] inactivity event skipped; cursor already advanced conversation_id=%s",
            trace_id,
            conversation_id,
        )
        return {"success": True, "skipped": True, "reason": "cursor_already_advanced"}

    try:
        messages = await _fetch_unprocessed_inbound_messages(
            conversation_id=conversation_id,
            from_cursor_at=from_cursor_at,
            from_cursor_message_id=from_cursor_message_id,
            to_inbound_message_at=to_inbound_message_at,
            to_inbound_message_id=to_inbound_message_id,
        )
        processed = await _tag_messages_and_generate_lead(
            client_id=client_id,
            conversation_id=conversation_id,
            phone=phone,
            messages=messages,
            trace_id=trace_id,
        )
        await _advance_cursor(
            conversation_id=conversation_id,
            to_inbound_message_at=to_inbound_message_at,
            to_inbound_message_id=to_inbound_message_id,
        )
        logger.info(
            "[%s] inactivity event processed conversation_id=%s messages=%s tags=%s lead_inserted=%s",
            trace_id,
            conversation_id,
            len(messages),
            len(processed.get("tags") or []),
            processed.get("lead_inserted"),
        )
        return {
            "success": True,
            "messages_tagged": len(processed.get("tags") or []),
            "messages_seen": len(messages),
            "lead_inserted": bool(processed.get("lead_inserted")),
            "lead_already_exists": bool(processed.get("lead_already_exists")),
        }
    except Exception as exc:
        await _mark_failed(conversation_id, str(exc))
        raise
