#!/usr/bin/env python3
"""
Backfill ``bloomerce_order_edits`` by scanning the raw messages table and using
an LLM to determine whether an order update was performed in each conversation.

Context
-------
The daily cron ``bloomerce_edited_sync_job.py`` reads Shopify orders tagged
``BLOOMERCE_EDITED``, but that tag only started being written recently. This
script fills the gap for older conversations by:

1. Querying the ``messages`` table for recent conversations (grouped by
   ``conversation_id``).
2. Sending each conversation transcript to an LLM that determines:
   - Was an order update performed? (yes/no)
   - What update types were performed? (address/phone/email/name/size/product)
   - What order name/number was referenced?
3. For conversations where the LLM confirms an order update, resolving the
   order on Shopify and upserting into ``bloomerce_order_edits``.

Reuses ``_upsert_edit_row`` and ``_gql_id_to_numeric`` from the cron job so
backfilled rows are identical in structure.

Usage
-----
    # Preview only (default — writes nothing):
    python scripts/backfill_bloomerce_order_edits.py --days 3

    # Actually write the rows:
    python scripts/backfill_bloomerce_order_edits.py --days 3 --commit

    # Narrow to a single client:
    python scripts/backfill_bloomerce_order_edits.py --days 7 --commit \
        --client-id c3ffcb1b-afb9-4ca4-8746-a06698bec870

    # Handle multi-order conversations:
    python scripts/backfill_bloomerce_order_edits.py --days 7 --commit --multi-order each
"""

import os
import re
import sys
import json
import asyncio
import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

from fashion_bot.config_manager import aget_shopify_config
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.utils.client_id_utils import is_client_blocklisted
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.shopify_throttle import shopify_graphql_post
from fashion_bot.core.llm_factory import LLMInvoker

from fashion_bot.cron_jobs.bloomerce_edited_sync_job import (
    _gql_id_to_numeric,
    _upsert_edit_row,
)

logger = logging.getLogger("backfill_boe")

VALID_UPDATE_TYPES = {"address", "phone", "email", "name", "size", "product"}

_ORDER_FIELDS = """
    id
    name
    cancelledAt
    displayFulfillmentStatus
    createdAt
    totalPriceSet { shopMoney { amount currencyCode } }
    customer { firstName lastName }
    shippingAddress { firstName lastName }
"""

_ORDER_BY_ID_QUERY = "query($id: ID!) { order(id: $id) { %s } }" % _ORDER_FIELDS

_ORDER_SEARCH_QUERY = (
    "query($q: String!) { orders(first: 5, query: $q) { edges { node { %s } } } }"
    % _ORDER_FIELDS
)

# Patterns for extracting order references from messages.
_ORDER_REF_PATTERNS = [
    re.compile(r"#(\d{3,})"),
    re.compile(r"\b([A-Z]{2,4}\d{3,})\b"),
]

# ── LLM Analysis ────────────────────────────────────────────────────────────

_ANALYSIS_SYSTEM_PROMPT = """You are an expert e-commerce conversation analyst. Your task is to determine whether the customer service bot successfully performed an order update/edit on behalf of the customer.

An "order update" means the bot actually CHANGED something on an existing order — not just discussed it. Examples:
- Changed the shipping address
- Changed the phone number on the order
- Changed the email on the order
- Changed the customer name on the order
- Changed the product size/variant
- Changed/swapped the product itself

Do NOT count these as order updates:
- Just providing order status/tracking info
- Discussing return/exchange policies without action
- Recommending products
- Failed update attempts
- The bot saying it will escalate or pass to a human

Return ONLY valid JSON. No text outside the JSON object."""

_ANALYSIS_USER_PROMPT = """Analyze this conversation and determine if an order update was successfully performed.

CONVERSATION:
{conversation_text}

Return JSON in exactly this format:
{{
    "order_update_performed": true/false,
    "update_types": ["address", "phone", "email", "name", "size", "product"],
    "order_references": ["#12345", "GV15470"],
    "confidence": "high" | "medium" | "low",
    "reasoning": "brief explanation"
}}

Rules:
- "order_update_performed": true ONLY if the bot confirmed it successfully made the change
- "update_types": list only the types that were actually changed (subset of: address, phone, email, name, size, product)
- "order_references": any order numbers/names mentioned in the conversation
- If no update was performed, return update_types as [] and order_references as []"""


async def _analyze_conversation_with_llm(
    conversation_text: str,
    client_id: str,
) -> Optional[Dict[str, Any]]:
    """Send conversation to LLM and parse the order-update analysis."""
    if not conversation_text or conversation_text == "(empty conversation)":
        return None

    user_prompt = _ANALYSIS_USER_PROMPT.format(conversation_text=conversation_text)

    try:
        raw = await LLMInvoker.ainvoke(
            prompt=user_prompt,
            tool_name="conversation_analytics",
            client_id=client_id,
            system_prompt=_ANALYSIS_SYSTEM_PROMPT,
            config={"callbacks": []},
        )
    except Exception as exc:
        logger.warning("LLM call failed for client=%s: %s", client_id, exc)
        return None

    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        result = json.loads(cleaned)
        return result
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Failed to parse LLM JSON: %s | raw=%s", exc, raw[:200])
        return None


# ── Shopify order resolution ────────────────────────────────────────────────

def _derive_row_fields(node: Dict[str, Any]) -> Dict[str, Any]:
    """Mirror the cron's per-order field derivation."""
    shopify_order_id = _gql_id_to_numeric(node.get("id", ""))
    order_name = node.get("name") or ""

    shopify_created_at: Optional[datetime] = None
    if node.get("createdAt"):
        try:
            shopify_created_at = datetime.fromisoformat(
                node["createdAt"].replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    shipping = node.get("shippingAddress") or {}
    customer = node.get("customer") or {}
    customer_name = (
        f"{shipping.get('firstName') or customer.get('firstName') or ''} "
        f"{shipping.get('lastName') or customer.get('lastName') or ''}"
    ).strip()

    order_status = (node.get("displayFulfillmentStatus") or "UNFULFILLED").lower()

    price_set = (node.get("totalPriceSet") or {}).get("shopMoney") or {}
    order_value = ""
    if price_set.get("amount"):
        order_value = f"{price_set['amount']} {price_set.get('currencyCode', 'INR')}"

    return {
        "shopify_order_id": shopify_order_id,
        "order_name": order_name,
        "customer_name": customer_name,
        "order_status": order_status,
        "order_value": order_value,
        "shopify_created_at": shopify_created_at,
        "cancelled": bool(node.get("cancelledAt")),
    }


async def _gql(http_client, url, headers, shop_url, query, variables) -> Dict[str, Any]:
    payload = {"query": query, "variables": variables}
    return await shopify_graphql_post(
        http_client, url, headers, payload, rate_limit_key=shop_url,
    )


async def _resolve_order(
    http_client, graphql_url: str, headers: dict, shop_url: str, raw_order_id: str,
) -> Optional[Dict[str, Any]]:
    """Resolve a stored order-id string to a live Shopify order node."""
    raw = (raw_order_id or "").strip().lstrip("#")
    if not raw:
        return None

    # Direct id fetch for long numeric strings.
    if raw.isdigit() and len(raw) >= 8:
        try:
            resp = await _gql(
                http_client, graphql_url, headers, shop_url,
                _ORDER_BY_ID_QUERY, {"id": f"gid://shopify/Order/{raw}"},
            )
            node = (resp.get("data") or {}).get("order")
            if node:
                return node
        except Exception as exc:
            logger.debug("id-fetch failed for %s: %s", raw, exc)

    # Search by order name.
    seen_queries: List[str] = []
    for q in (f"name:{raw_order_id.strip()}", f"name:{raw}", raw):
        if q in seen_queries:
            continue
        seen_queries.append(q)
        try:
            resp = await _gql(
                http_client, graphql_url, headers, shop_url,
                _ORDER_SEARCH_QUERY, {"q": q},
            )
            edges = ((resp.get("data") or {}).get("orders") or {}).get("edges") or []
        except Exception as exc:
            logger.debug("name-search failed for %s: %s", q, exc)
            continue
        if not edges:
            continue
        nodes = [e.get("node") for e in edges if e.get("node")]
        for node in nodes:
            nm = (node.get("name") or "").lstrip("#")
            if nm == raw or node.get("name") == raw_order_id.strip():
                return node
        if len(nodes) == 1:
            return nodes[0]
    return None


# ── Conversation fetching ───────────────────────────────────────────────────

async def _fetch_conversations(days: int, client_id: Optional[str]) -> List[Dict[str, Any]]:
    """Fetch distinct conversations with their messages from the last N days.

    Returns a list of dicts with conversation metadata and a messages list.
    """
    params: List[Any] = [days]
    client_filter = ""
    if client_id:
        client_filter = "AND m.client_id = %s::uuid"
        params.append(client_id)

    query = f"""
        SELECT
            m.client_id::text       AS client_id,
            m.conversation_id::text AS conversation_id,
            m.phone                 AS phone_number,
            MIN(m.created_at)       AS conversation_started_at,
            MAX(m.created_at)       AS conversation_ended_at,
            COUNT(*)                AS message_count
        FROM messages m
        WHERE m.created_at >= NOW() - (%s || ' days')::interval
          {client_filter}
        GROUP BY m.client_id, m.conversation_id, m.phone
        HAVING COUNT(*) >= 4
        ORDER BY MAX(m.created_at) DESC
    """

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            rows = await cur.fetchall()

    return [dict(row) for row in rows]


async def _fetch_messages_for_conversation(conversation_id: str) -> List[Dict[str, Any]]:
    """Fetch all messages for a conversation ordered by time."""
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT message, message_side, created_at
                FROM messages
                WHERE conversation_id = %s::uuid
                ORDER BY created_at ASC
                """,
                (conversation_id,),
            )
            rows = await cur.fetchall()

    return [dict(row) for row in rows]


def _format_messages(messages: List[Dict]) -> str:
    """Convert message rows into a readable transcript for the LLM."""
    lines: List[str] = []
    for msg in messages:
        role = "Customer" if msg["message_side"] == "user_to_system" else "Bot"
        content = (msg.get("message") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(empty conversation)"


def _extract_order_refs_from_messages(messages: List[Dict]) -> List[str]:
    """Best-effort: mine order references from message text."""
    found: List[str] = []
    for msg in messages:
        text = msg.get("message") or ""
        for pat in _ORDER_REF_PATTERNS:
            for m in pat.findall(text):
                if m not in found:
                    found.append(m)
    return found[:10]


# ── Already-backfilled check ────────────────────────────────────────────────

async def _get_already_backfilled_conversations(days: int, client_id: Optional[str]) -> set:
    """Return conversation_ids already in bloomerce_order_edits to skip re-analysis."""
    params: List[Any] = [days]
    client_filter = ""
    if client_id:
        client_filter = "AND client_id = %s::uuid"
        params.append(client_id)

    query = f"""
        SELECT DISTINCT conversation_id
        FROM bloomerce_order_edits
        WHERE fetched_at >= NOW() - (%s || ' days')::interval
          AND conversation_id != ''
          {client_filter}
    """

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
        return {row[0] for row in rows if row[0]}
    except Exception:
        return set()


# ── Backfill driver ─────────────────────────────────────────────────────────

async def _process_conversation(
    conv: Dict[str, Any],
    http_client,
    graphql_url: str,
    headers: dict,
    shop_url: str,
    multi_order: str,
    commit: bool,
    stats: Dict[str, int],
) -> None:
    conversation_id = conv["conversation_id"]
    client_id = conv["client_id"]

    messages = await _fetch_messages_for_conversation(conversation_id)
    if not messages:
        stats["empty"] += 1
        return

    conversation_text = _format_messages(messages)

    # Truncate very long conversations to stay within token limits.
    max_chars = 12000
    if len(conversation_text) > max_chars:
        conversation_text = conversation_text[:max_chars] + "\n...(truncated)"

    analysis = await _analyze_conversation_with_llm(conversation_text, client_id)
    stats["llm_calls"] += 1

    if not analysis:
        stats["llm_failed"] += 1
        return

    if not analysis.get("order_update_performed"):
        stats["no_update"] += 1
        return

    update_types = [
        t for t in (analysis.get("update_types") or [])
        if t.strip().lower() in VALID_UPDATE_TYPES
    ]
    if not update_types:
        stats["no_valid_types"] += 1
        logger.info(
            "conv=%s LLM says update performed but no valid types: %s",
            conversation_id, analysis.get("update_types"),
        )
        return

    # Gather order references: from LLM output + mined from messages.
    order_refs = list(analysis.get("order_references") or [])
    mined_refs = _extract_order_refs_from_messages(messages)
    for ref in mined_refs:
        if ref not in order_refs:
            order_refs.append(ref)

    if not order_refs:
        stats["no_order_ref"] += 1
        logger.info(
            "conv=%s LLM confirmed update types=%s but no order reference found",
            conversation_id, update_types,
        )
        return

    # Resolve each order reference on Shopify.
    live_orders: Dict[str, Dict[str, Any]] = {}
    for raw in order_refs:
        node = await _resolve_order(http_client, graphql_url, headers, shop_url, raw)
        if not node:
            stats["unresolved"] += 1
            continue
        fields = _derive_row_fields(node)
        if fields["cancelled"]:
            stats["cancelled"] += 1
            continue
        live_orders[fields["shopify_order_id"]] = fields

    if not live_orders:
        stats["no_live_orders"] += 1
        return

    if len(live_orders) > 1 and multi_order == "skip":
        stats["ambiguous_multi_order"] += 1
        logger.warning(
            "conv=%s resolves to %d live orders with types=%s → skipped",
            conversation_id, len(live_orders), update_types,
        )
        return

    base_ts: datetime = conv["conversation_ended_at"] or conv["conversation_started_at"]
    if base_ts.tzinfo is None:
        base_ts = base_ts.replace(tzinfo=timezone.utc)

    for fields in live_orders.values():
        for i, update_type in enumerate(sorted(update_types)):
            updated_at = base_ts + timedelta(seconds=i)
            stats["rows"] += 1
            if not commit:
                logger.info(
                    "[dry-run] would upsert client=%s order=%s (%s) type=%s "
                    "confidence=%s at=%s",
                    client_id, fields["shopify_order_id"],
                    fields["order_name"], update_type,
                    analysis.get("confidence", "?"), updated_at.isoformat(),
                )
                continue
            try:
                await _upsert_edit_row(
                    client_id=client_id,
                    shopify_order_id=fields["shopify_order_id"],
                    order_name=fields["order_name"],
                    customer_name=fields["customer_name"],
                    order_status=fields["order_status"],
                    update_type=update_type,
                    updated_at=updated_at,
                    conversation_id=conversation_id,
                    phone_number=conv.get("phone_number") or "",
                    session_id="",
                    order_value=fields["order_value"],
                    shopify_created_at=fields["shopify_created_at"],
                )
                stats["upserted"] += 1
            except Exception as exc:
                stats["errors"] += 1
                logger.error(
                    "Upsert failed conv=%s order=%s type=%s: %s",
                    conversation_id, fields["shopify_order_id"], update_type, exc,
                    exc_info=True,
                )


async def _amain(
    days: int,
    client_id: Optional[str],
    multi_order: str,
    commit: bool,
    skip_already_backfilled: bool,
) -> None:
    set_trace_id(generate_trace_id())

    # 1. Fetch all conversations with enough messages in the window.
    conversations = await _fetch_conversations(days, client_id)
    logger.info(
        "Found %d conversation(s) with >=4 messages in last %d days | commit=%s",
        len(conversations), days, commit,
    )
    if not conversations:
        return

    # 2. Optionally skip conversations already in bloomerce_order_edits.
    already_done: set = set()
    if skip_already_backfilled:
        already_done = await _get_already_backfilled_conversations(days, client_id)
        if already_done:
            before = len(conversations)
            conversations = [
                c for c in conversations if c["conversation_id"] not in already_done
            ]
            logger.info(
                "Skipped %d already-backfilled conversations (%d remaining)",
                before - len(conversations), len(conversations),
            )

    if not conversations:
        logger.info("No conversations left to process after filtering.")
        return

    # 3. Group by client for Shopify credential loading.
    by_client: Dict[str, List[Dict[str, Any]]] = {}
    for c in conversations:
        by_client.setdefault(c["client_id"], []).append(c)

    stats = {
        "conversations": len(conversations),
        "clients": len(by_client),
        "llm_calls": 0,
        "llm_failed": 0,
        "no_update": 0,
        "no_valid_types": 0,
        "no_order_ref": 0,
        "unresolved": 0,
        "cancelled": 0,
        "no_live_orders": 0,
        "ambiguous_multi_order": 0,
        "empty": 0,
        "rows": 0,
        "upserted": 0,
        "errors": 0,
        "clients_skipped": 0,
    }

    http_client = await get_shared_async_http_client()

    for cid, convs in by_client.items():
        if is_client_blocklisted(cid):
            stats["clients_skipped"] += 1
            logger.info("client=%s blocklisted → skipped", cid)
            continue

        config = await aget_shopify_config(client_id=cid)
        shop_url = config.get("shop_url", "")
        access_token = config.get("access_token", "")
        api_version = config.get("api_version", "2024-04")
        if not shop_url or not access_token:
            stats["clients_skipped"] += 1
            logger.warning("client=%s missing Shopify creds → skipped", cid)
            continue

        graphql_url = f"https://{shop_url}/admin/api/{api_version}/graphql.json"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        }
        logger.info("Processing client=%s (%d conversation(s))", cid, len(convs))

        for conv in convs:
            try:
                await _process_conversation(
                    conv, http_client, graphql_url, headers, shop_url,
                    multi_order, commit, stats,
                )
            except Exception as exc:
                stats["errors"] += 1
                logger.error(
                    "Failed conv=%s: %s",
                    conv["conversation_id"], exc, exc_info=True,
                )

    verb = "UPSERTED" if commit else "DRY-RUN (nothing written)"
    logger.info("✅ Backfill %s. stats=%s", verb, json.dumps(stats, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Backfill bloomerce_order_edits by scanning messages and "
                    "using LLM to detect order updates.",
    )
    parser.add_argument(
        "--days", type=int, default=7,
        help="Look back this many days (default: 7).",
    )
    parser.add_argument(
        "--client-id", default=None,
        help="Backfill a single client UUID (default: all clients).",
    )
    parser.add_argument(
        "--multi-order", choices=["skip", "each"], default="skip",
        help="When a conversation resolves to >1 live order: 'skip' (default) "
             "or 'each' (apply every update type to every order).",
    )
    parser.add_argument(
        "--commit", action="store_true",
        help="Actually write rows. Without this the script is a dry run.",
    )
    parser.add_argument(
        "--no-skip-existing", action="store_true",
        help="Don't skip conversations already in bloomerce_order_edits.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    asyncio.run(_amain(
        days=args.days,
        client_id=args.client_id,
        multi_order=args.multi_order,
        commit=args.commit,
        skip_already_backfilled=not args.no_skip_existing,
    ))


if __name__ == "__main__":
    main()
