"""
Cron job to tag conversations with conversion tags and extract product/order references.

Runs hourly. For each client:
1. Loads conversion_tags from client_configs (config_key='conversion_tags')
2. Finds conversations that haven't been conversion-tagged yet
   (conversion_tagged_at IS NULL) and are inactive (>90 min)
3. Fetches the full message history for each conversation
4. Calls the smaller/utility LLM (SMALLER_LLM_MODEL) to classify which (if any) conversion tags apply
   AND extract any product names/links and order IDs mentioned
5. Writes conversion_tags[], products_list_referred[], orders_list_referred[],
   and conversion_tagged_at to the conversations table
"""

import logging
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Conversations must be inactive for this long before we tag them
INACTIVITY_THRESHOLD_MINUTES = 90

# Maximum conversations to process per cron run (across all clients)
BATCH_LIMIT = 200

# Maximum messages to feed into the LLM per conversation
MAX_MESSAGES_PER_CONVERSATION = 30

# Only process conversations created after this date
CONVERSATION_CREATED_AFTER = "2026-03-01 00:00:00"

CONVERSION_TAG_PROMPT = """You are an analyst classifying a customer-support conversation.

Read the full conversation below, then:
1. Decide which (if any) of the CONVERSION TAGS apply.
2. Extract any PRODUCT names, links, or identifiers mentioned in the conversation.
3. Extract any ORDER IDs mentioned in the conversation.

CONVERSION TAGS (with descriptions):
{conversion_tags_json}

CONVERSATION:
{conversation_text}

RULES FOR CONVERSION TAGS:
- A conversation can have ZERO, ONE, or MULTIPLE conversion tags.
- Only assign a tag if there is CLEAR evidence in the conversation.
- "Prevented Cancellation" → Customer wanted to cancel but was convinced to keep the order.
- "Prevented Return" → Customer wanted to return but was convinced to keep the product.
- "Nudged to Order" → Bot guided the customer toward placing a new order or completing a purchase.
- "Updated Order Details" → Bot helped the customer change order details (size, address, product, etc.).
- If the customer merely ASKED about cancellation/return but the conversation does not show prevention, do NOT tag it.
- If the bot simply provided information without influencing the outcome, do NOT tag it.

RULES FOR PRODUCT EXTRACTION:
- Extract product names, product links (URLs), or product identifiers that the customer or bot referred to.
- Use the most descriptive form available (e.g. full product name with variant/color rather than just a generic term).
- If a product URL is present, include the URL as-is.
- Do NOT invent product names that are not explicitly mentioned.

RULES FOR ORDER EXTRACTION:
- Extract order IDs, order numbers, or order references mentioned in the conversation.
- Include the exact order ID as it appears (e.g. "#12345", "ORD-2026-001", "SO-12345").
- Do NOT invent order IDs that are not explicitly mentioned.

OUTPUT: Return ONLY a JSON object with no extra text:
{{"conversion_tags": ["<tag1>", "<tag2>"], "products_referred": ["<product_name_or_link>"], "orders_referred": ["<order_id>"]}}

If NONE apply or are found, use empty arrays:
{{"conversion_tags": [], "products_referred": [], "orders_referred": []}}
"""


def _get_distinct_client_ids(cur) -> List[str]:
    """Get all distinct client_ids that have active conversations."""
    cur.execute("""
        SELECT DISTINCT client_id::text
        FROM conversations
        WHERE status = 'active'
          AND conversion_tagged_at IS NULL
          AND created_at > %s::timestamptz
    """, (CONVERSATION_CREATED_AFTER,))
    rows = cur.fetchall()
    return [row.get("client_id") if isinstance(row, dict) else row[0] for row in rows]


def _get_untagged_conversations(cur, client_id: str, cutoff_time: datetime, limit: int) -> List[Dict]:
    """
    Fetch conversations that need conversion tagging.
    
    Criteria:
    - conversion_tagged_at IS NULL  (never been processed by this job)
    - Last activity > 90 min ago    (conversation has ended)
    - Has at least 2 messages        (need actual conversation, not just a greeting)
    """
    cur.execute("""
        SELECT
            c.conversation_id::text AS conversation_id,
            c.client_id::text AS client_id,
            c.phone,
            c.tags,
            c.created_at,
            COUNT(m.message_id) AS message_count,
            MAX(m.created_at) AS last_message_at
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.conversation_id
        WHERE
            c.client_id = %s::uuid
            AND c.conversion_tagged_at IS NULL
            AND c.status = 'active'
            AND c.created_at > %s::timestamptz
        GROUP BY c.conversation_id, c.client_id, c.phone, c.tags, c.created_at
        HAVING
            COUNT(m.message_id) >= 2
            AND MAX(m.created_at) < %s
        ORDER BY MAX(m.created_at) ASC
        LIMIT %s
    """, (client_id, CONVERSATION_CREATED_AFTER, cutoff_time, limit))

    return cur.fetchall()


def _get_conversation_messages(cur, conversation_id: str, limit: int = MAX_MESSAGES_PER_CONVERSATION) -> List[Dict]:
    """Fetch messages for a conversation in chronological order."""
    cur.execute("""
        SELECT message, message_side, created_at
        FROM messages
        WHERE conversation_id = %s::uuid
        ORDER BY created_at ASC
        LIMIT %s
    """, (conversation_id, limit))
    return cur.fetchall()


def _build_conversation_text(messages: List[Dict]) -> str:
    """Build a human-readable conversation transcript from message rows."""
    lines = []
    for msg in messages:
        text = msg.get("message") if isinstance(msg, dict) else msg[0]
        side = msg.get("message_side") if isinstance(msg, dict) else msg[1]

        role = "Bot" if side in ("system_to_user", "bot") else "Customer"
        # Truncate very long messages to keep prompt reasonable
        if text and len(text) > 500:
            text = text[:500] + "..."
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _classify_conversion_tags(
    conversation_text: str,
    conversion_tags: Dict[str, str],
) -> Dict[str, List[str]]:
    """
    Classify conversion tags and extract product/order references
    from the conversation using the smaller/utility LLM.
    
    Returns:
        Dict with keys: conversion_tags, products_referred, orders_referred
        (each a list of strings, may be empty).
    """
    from fashion_bot.core.llm_config import (
        get_smaller_llm_config,
        BACKGROUND_OPENROUTER_KEY_ENV,
    )
    from fashion_bot.core.llm_factory import LLMFactory
    from langchain_core.messages import SystemMessage, HumanMessage

    # Background cron workload → BACKGROUND OpenRouter key (key 2).
    llm = LLMFactory.get_llm(
        tool_name="conversion_tagging",
        override_config=get_smaller_llm_config(
            temperature=0,
            max_tokens=300,
            api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
        ),
    )

    prompt = CONVERSION_TAG_PROMPT.format(
        conversion_tags_json=json.dumps(conversion_tags, indent=2),
        conversation_text=conversation_text,
    )

    output = llm.invoke([
        SystemMessage(content=prompt),
        HumanMessage(content="Classify this conversation."),
    ])

    content = output.content.strip()

    # Parse JSON from response
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r'\{.*\}', content, re.DOTALL)
        result = json.loads(match.group(0)) if match else {}

    # --- Conversion tags ---
    tags = result.get("conversion_tags", [])
    valid_tag_names = set(conversion_tags.keys())
    validated_tags = [t for t in tags if t in valid_tag_names]

    # --- Products referred ---
    products = result.get("products_referred", [])
    # Ensure list of non-empty strings, deduplicated
    validated_products = list(dict.fromkeys(
        p.strip() for p in products if isinstance(p, str) and p.strip()
    ))

    # --- Orders referred ---
    orders = result.get("orders_referred", [])
    validated_orders = list(dict.fromkeys(
        o.strip() for o in orders if isinstance(o, str) and o.strip()
    ))

    return {
        "conversion_tags": validated_tags,
        "products_referred": validated_products,
        "orders_referred": validated_orders,
    }


def _update_conversation_conversion_tags(
    cur,
    conversation_id: str,
    conversion_tags: List[str],
    products_referred: List[str] = None,
    orders_referred: List[str] = None,
) -> None:
    """Write conversion_tags, products/orders referred, and conversion_tagged_at to the conversations table."""
    now = datetime.now(timezone.utc)
    cur.execute("""
        UPDATE conversations
        SET conversion_tags = %s,
            products_list_referred = %s,
            orders_list_referred = %s,
            conversion_tagged_at = %s
        WHERE conversation_id = %s::uuid
    """, (
        conversion_tags if conversion_tags else [],
        products_referred if products_referred else [],
        orders_referred if orders_referred else [],
        now,
        conversation_id,
    ))


def _mark_conversation_tagged(cur, conversation_id: str) -> None:
    """Mark a conversation as processed even if no tags apply (set timestamp only)."""
    _update_conversation_conversion_tags(cur, conversation_id, [])


# ==================== MAIN ENTRY POINT ====================

def process_conversion_tags() -> Dict[str, Any]:
    """
    Main cron job function — processes untagged conversations across all clients.
    
    Returns:
        Dictionary with processing statistics.
    """
    stats = {
        "processed": 0,
        "tagged": 0,
        "no_tags": 0,
        "skipped_no_config": 0,
        "errors": 0,
        "clients_processed": 0,
        "tag_counts": {},
        "products_extracted": 0,
        "orders_extracted": 0,
    }

    conn = None
    cur = None
    try:
        from fashion_bot.database_manager import get_direct_postgres_cursor
        import asyncio
        from fashion_bot.utils.utils import aget_conversion_tags_with_caching

        conn, cur = get_direct_postgres_cursor()
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=INACTIVITY_THRESHOLD_MINUTES)

        # 1. Get all client_ids with untagged conversations
        client_ids = _get_distinct_client_ids(cur)
        print(f"\n{'='*80}", flush=True)
        print(f"[CONVERSION_TAGS] Found {len(client_ids)} client(s) with untagged conversations", flush=True)
        print(f"{'='*80}", flush=True)

        remaining_limit = BATCH_LIMIT

        for client_id in client_ids:
            if remaining_limit <= 0:
                break

            # 2. Load conversion_tags config for this client
            conversion_tags_config = asyncio.run(aget_conversion_tags_with_caching(client_id))
            if not conversion_tags_config:
                logger.info(f"[CONVERSION_TAGS] No conversion_tags configured for client {client_id}, skipping")
                stats["skipped_no_config"] += 1
                continue

            stats["clients_processed"] += 1
            print(f"\n[CONVERSION_TAGS] Client {client_id}: {len(conversion_tags_config)} conversion tags configured", flush=True)

            # 3. Fetch untagged conversations for this client
            conversations = _get_untagged_conversations(cur, client_id, cutoff_time, remaining_limit)
            print(f"[CONVERSION_TAGS] Client {client_id}: {len(conversations)} conversations to process", flush=True)

            for idx, row in enumerate(conversations, 1):
                conv_id = row.get("conversation_id") if isinstance(row, dict) else row[0]
                phone = row.get("phone") if isinstance(row, dict) else row[2]
                msg_count = row.get("message_count") if isinstance(row, dict) else row[5]

                try:
                    # 4. Fetch full message history
                    messages = _get_conversation_messages(cur, conv_id)
                    if len(messages) < 2:
                        # Shouldn't happen due to HAVING clause, but defensive
                        _mark_conversation_tagged(cur, conv_id)
                        stats["no_tags"] += 1
                        stats["processed"] += 1
                        continue

                    conversation_text = _build_conversation_text(messages)

                    # 5. Classify via LLM (returns dict with conversion_tags, products_referred, orders_referred)
                    classification = _classify_conversion_tags(conversation_text, conversion_tags_config)
                    detected_tags = classification.get("conversion_tags", [])
                    products_referred = classification.get("products_referred", [])
                    orders_referred = classification.get("orders_referred", [])

                    # 6. Write results
                    _update_conversation_conversion_tags(
                        cur, conv_id, detected_tags,
                        products_referred=products_referred,
                        orders_referred=orders_referred,
                    )

                    stats["processed"] += 1
                    remaining_limit -= 1

                    if products_referred:
                        stats["products_extracted"] += len(products_referred)
                    if orders_referred:
                        stats["orders_extracted"] += len(orders_referred)

                    has_any_data = detected_tags or products_referred or orders_referred
                    if has_any_data:
                        stats["tagged"] += 1
                        detail_parts = []
                        if detected_tags:
                            detail_parts.append(f"🏷️ {detected_tags}")
                        if products_referred:
                            detail_parts.append(f"📦 products={products_referred}")
                        if orders_referred:
                            detail_parts.append(f"🧾 orders={orders_referred}")
                        print(f"  [{idx}] {conv_id[:8]}... ({msg_count} msgs) → {', '.join(detail_parts)}", flush=True)
                        for tag in detected_tags:
                            stats["tag_counts"][tag] = stats["tag_counts"].get(tag, 0) + 1
                    else:
                        stats["no_tags"] += 1
                        print(f"  [{idx}] {conv_id[:8]}... ({msg_count} msgs) → (none)", flush=True)

                except Exception as e:
                    logger.error(f"[CONVERSION_TAGS] Error processing conversation {conv_id}: {e}", exc_info=True)
                    print(f"  [{idx}] {conv_id[:8]}... ❌ Error: {e}", flush=True)
                    stats["errors"] += 1

                    # Still mark as tagged so we don't retry endlessly
                    try:
                        _mark_conversation_tagged(cur, conv_id)
                    except Exception:
                        pass

        print(f"\n{'='*80}", flush=True)
        print(f"[CONVERSION_TAGS] ✅ Processing complete", flush=True)
        print(f"  Processed:  {stats['processed']}", flush=True)
        print(f"  Tagged:     {stats['tagged']}", flush=True)
        print(f"  No tags:    {stats['no_tags']}", flush=True)
        print(f"  Errors:     {stats['errors']}", flush=True)
        print(f"  Products extracted: {stats['products_extracted']}", flush=True)
        print(f"  Orders extracted:   {stats['orders_extracted']}", flush=True)
        if stats["tag_counts"]:
            print(f"  Breakdown: {stats['tag_counts']}", flush=True)
        print(f"{'='*80}\n", flush=True)

        return {
            "success": True,
            "stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as e:
        error_msg = f"Error in conversion tag cron job: {e}"
        logger.error(f"[CONVERSION_TAGS] {error_msg}", exc_info=True)
        print(f"\n[CONVERSION_TAGS] ❌ {error_msg}\n", flush=True)
        return {
            "success": False,
            "error": str(e),
            "stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    finally:
        if cur:
            try:
                cur.close()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = process_conversion_tags()
    print(json.dumps(result, indent=2))
