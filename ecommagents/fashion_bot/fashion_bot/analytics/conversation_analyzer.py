"""
Conversation Analytics — Prompt-Based Post-Hoc Analyzer
========================================================
Runs as a cron job to analyze completed conversations using a single central
LLM prompt.  Extracts multi-dimensional insights (cancellation aversion,
order conversion, satisfaction, etc.) and stores them in the
``conversation_analytics`` table.

This is a *separate* pipeline from the real-time tool-based
``cancellation_aversion_tracker``.  Both coexist so results can be compared.

Usage (one-shot):
    from fashion_bot.analytics.conversation_analyzer import run_pending_analyses
    await run_pending_analyses()
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)
from fashion_bot.monitoring.otel_metrics import request_client_id

logger = logging.getLogger("conversation_analyzer")

SESSION_EXPIRY_MINUTES: int = 90
BATCH_SIZE: int = 20
ORDER_CONVERSION_WINDOW_HOURS: int = 12
ORDER_ATTRIBUTION_MIN_CUSTOMER_MESSAGES: int = 2
FALLBACK_LEAD_GENERATION_TAGS: List[str] = [
    "Product Query",
    "Product Recommendation",
    "Size Inquiry",
    "Pricing Query",
    "Discount Query",
    "Delivery Query",
    "Delivery Policy Query",
    "Payment Policy Query",
    "Wholesale Inquiry",
    "R&E Policy Query",
    "Cart Addition",
    "Offline Leads",
]
FALLBACK_PREORDER_LEAD_TAGS: List[str] = []
NON_LEAD_SOURCE_TAGS = {
    "cancellation requests",
    "escalations",
    "exchange request",
    "order placed",
    "order details query",
    "order update",
    "return request",
}


# ─── Prompt loading ──────────────────────────────────────────────────────────

async def _aget_prompt_from_db(client_id: str) -> Optional[str]:
    """Try loading the analytics prompt from agents_config via async caching."""
    try:
        from fashion_bot.utils.utils import aget_agent_prompt_with_caching
        return await aget_agent_prompt_with_caching(client_id, "conversation_analytics")
    except Exception as exc:
        logger.warning(f"[ANALYZER] Failed to load prompt from DB: {exc}")
        return None


def _get_default_prompt_parts():
    """Return (system_prompt, user_prompt_template, version) from the canonical file."""
    from fashion_bot.prompts.conversation_analytics_prompt import (
        SYSTEM_PROMPT,
        USER_PROMPT_TEMPLATE,
        PROMPT_VERSION,
    )
    return SYSTEM_PROMPT, USER_PROMPT_TEMPLATE, PROMPT_VERSION


# Business-policy placeholders that tenant-authored analytics prompts reference
# in their "BUSINESS POLICIES" section. Onboarding writes these tokens into
# ``agents_config.agent_prompt`` but nothing used to supply them, so every
# affected tenant silently fell back to the generic default prompt. Maps the
# prompt placeholder to its ``client_configs.config_key``.
_POLICY_PLACEHOLDER_CONFIG_KEYS: Dict[str, str] = {
    "return_exchange_policy": "return_exchange_policy",
    "delivery_policy": "delivery_policy",
    "after_delivery_return_exchange": "after_delivery_return_exchange",
    "support_team_contact_details": "vendor_contact_details",
}

_POLICY_NOT_CONFIGURED = "(not configured for this client)"

# The three constructs ``str.format()`` recognises that appear in these
# prompts. Order matters: ``{{`` must be tried before ``{name}`` so an escaped
# brace is never read as the start of a field. The name pattern is deliberately
# strict, so the JSON output schema in the prompt body (``{\n  "key": ...}``)
# is left alone rather than mistaken for a placeholder.
_PLACEHOLDER_RE = re.compile(
    r"(\{\{)"                            # escaped open brace  -> {
    r"|(\}\})"                           # escaped close brace -> }
    r"|\{([a-zA-Z_][a-zA-Z0-9_]*)\}"     # replacement field   -> value
)


async def _aget_policy_context(client_id: str, placeholders: Any) -> Dict[str, str]:
    """Resolve the business-policy placeholders a tenant's prompt actually uses.

    Reads through ``aget_json_config`` so these land on the standard
    memory -> Redis -> DB config tier rather than hitting Postgres on every
    conversation. A tenant that has not configured a given policy gets an
    explicit "not configured" marker instead of a raw ``{placeholder}``
    leaking into the LLM prompt.

    Only the requested placeholders are fetched, so a tenant whose prompt does
    not mention policies pays no config reads at all.
    """
    from fashion_bot.config_manager import aget_json_config, format_qa_data_for_llm

    async def _aload(placeholder: str) -> Tuple[str, str]:
        config_key = _POLICY_PLACEHOLDER_CONFIG_KEYS[placeholder]
        try:
            data = await aget_json_config(config_key, client_id=client_id)
        except Exception as exc:
            logger.warning(
                "[ANALYZER] Failed to load policy config %s for client %s: %s",
                config_key, client_id, exc,
            )
            return placeholder, _POLICY_NOT_CONFIGURED
        if not data:
            return placeholder, _POLICY_NOT_CONFIGURED
        if isinstance(data, dict):
            return placeholder, format_qa_data_for_llm(data).strip()
        return placeholder, str(data).strip()

    wanted = [p for p in placeholders if p in _POLICY_PLACEHOLDER_CONFIG_KEYS]
    if not wanted:
        return {}
    return dict(await asyncio.gather(*(_aload(p) for p in wanted)))


def _render_prompt(template: str, values: Mapping[str, Any]) -> str:
    """Substitute ``{key}`` / ``{{key}}`` tokens without ``str.format()``.

    ``str.format()`` parses the *whole* template, so one stray brace anywhere
    — a JSON example, a policy token nobody supplies — aborts the entire
    render. Here only known keys are substituted and every other brace is left
    untouched, following the approach already used by the product attribute
    extractor.

    Semantics match ``str.format()`` exactly for every template that already
    formatted cleanly — ``{name}`` is substituted and ``{{``/``}}`` collapse to
    a single brace — so prompts that work today render byte-identically. The
    one deliberate difference is the failure mode: an unknown ``{name}`` is
    left as written instead of raising, which only affects templates that
    previously crashed.

    Substitution is a single pass, so text that gets *inserted* is never
    rescanned: a customer who types ``{delivery_policy}`` into a chat cannot
    have it expanded when the conversation is later analysed.
    """
    def _substitute(match: "re.Match[str]") -> str:
        if match.group(1):
            return "{"
        if match.group(2):
            return "}"
        name = match.group(3)
        if name in values:
            return str(values[name])
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_substitute, template)


def _find_unresolved_placeholders(template: str, supplied: Mapping[str, Any]) -> List[str]:
    """Return replacement fields in ``template`` that nothing supplies.

    Rendering can no longer fail loudly, so this keeps the failure visible: a
    tenant prompt referencing a variable we do not supply still gets reported
    instead of quietly shipping ``{some_token}`` to the model. Escaped
    ``{{name}}`` is intentional literal text and is not reported.

    Takes the *template*, never the rendered output — otherwise a customer
    message containing ``{foo}`` would raise a spurious alert.
    """
    return sorted({
        match.group(3)
        for match in _PLACEHOLDER_RE.finditer(template)
        if match.group(3) and match.group(3) not in supplied
    })


# ─── DB helpers ──────────────────────────────────────────────────────────────

# How many consecutive failures before we stop retrying a conversation every
# single sweep, and how long to back off between retries below that cap.
# Without this, a conversation that always fails analysis (bad data, LLM
# error, etc.) gets re-selected first every run (oldest-first ordering) and
# can stall the whole drain loop indefinitely.
ANALYSIS_FAILURE_MAX_ATTEMPTS: int = 3
ANALYSIS_FAILURE_BACKOFF_MINUTES: int = 60

_failure_table_ensured = False


async def _aensure_analysis_failure_table() -> None:
    """Create the failure-tracking table once per process (idempotent DDL)."""
    global _failure_table_ensured
    if _failure_table_ensured:
        return
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS conversation_analysis_failures (
                        conversation_id UUID PRIMARY KEY,
                        attempt_count INT NOT NULL DEFAULT 1,
                        last_attempted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        last_error TEXT
                    );
                    """
                )
        _failure_table_ensured = True
    except Exception as exc:
        logger.error(f"[ANALYZER] Failed to ensure conversation_analysis_failures table: {exc}")


async def _arecord_analysis_failure(conversation_id: str, error: str) -> None:
    """Upsert a failed-analysis attempt so the conversation backs off instead
    of being retried at the front of every future sweep."""
    await _aensure_analysis_failure_table()
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_analysis_failures (conversation_id, attempt_count, last_attempted_at, last_error)
                    VALUES (%s::uuid, 1, NOW(), %s)
                    ON CONFLICT (conversation_id) DO UPDATE SET
                        attempt_count = conversation_analysis_failures.attempt_count + 1,
                        last_attempted_at = NOW(),
                        last_error = EXCLUDED.last_error
                    """,
                    (conversation_id, (error or "")[:2000]),
                )
    except Exception as exc:
        logger.error(f"[ANALYZER] Failed to record analysis failure for {conversation_id}: {exc}")


@awith_retry
async def fetch_unanalyzed_conversations(
    batch_size: int = BATCH_SIZE,
    lookback_days: Optional[int] = None,
) -> List[Dict]:
    """
    Return up to ``batch_size`` ended conversations that have NOT yet been
    analyzed (no row in ``conversation_analytics``).

    A conversation is considered "ended" when its last message is older than
    SESSION_EXPIRY_MINUTES.

    Args:
        batch_size: max conversations to return per call.
        lookback_days: if set, only consider conversations with messages
                       within the last N days. Keeps the scheduler focused
                       on recent traffic instead of grinding through years
                       of old test data.
    """
    lookback_clause = ""
    if lookback_days is not None and lookback_days > 0:
        lookback_clause = (
            f"AND m.created_at > NOW() - INTERVAL '{lookback_days} days'"
        )

    sql = """
        SELECT
            c.conversation_id::text,
            c.client_id::text,
            c.phone,
            c.first_message,
            c.channel_type,
            c.created_at  AS conv_started,
            MAX(m.created_at) AS last_msg_at
        FROM conversations c
        JOIN messages m ON m.conversation_id = c.conversation_id
        WHERE c.status = 'active'
          AND NOT EXISTS (
              SELECT 1 FROM conversation_analytics ca
              WHERE ca.conversation_id = c.conversation_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM conversation_analysis_failures f
              WHERE f.conversation_id = c.conversation_id
                AND (
                    f.attempt_count >= {max_attempts}
                    OR f.last_attempted_at > NOW() - INTERVAL '{backoff_minutes} minutes'
                )
          )
          {lookback}
        GROUP BY c.conversation_id, c.client_id, c.phone, c.first_message, c.channel_type, c.created_at
        HAVING MAX(m.created_at) < NOW() - INTERVAL '{minutes} minutes'
        ORDER BY MAX(m.created_at) ASC
        LIMIT {batch}
    """.format(
        lookback=lookback_clause,
        minutes=SESSION_EXPIRY_MINUTES,
        batch=batch_size,
        max_attempts=ANALYSIS_FAILURE_MAX_ATTEMPTS,
        backoff_minutes=ANALYSIS_FAILURE_BACKOFF_MINUTES,
    )

    await _aensure_analysis_failure_table()

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
                return [
                    {
                        "conversation_id": r["conversation_id"],
                        "client_id":       r["client_id"],
                        "phone":           r["phone"],
                        "first_message":   r.get("first_message"),
                        "channel_type":    r.get("channel_type"),
                        "conv_started":    r["conv_started"],
                        "last_msg_at":     r["last_msg_at"],
                    }
                    for r in rows
                ]
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[ANALYZER] Failed to fetch unanalyzed conversations: {exc}")
        return []


@awith_retry
async def _fetch_messages(conversation_id: str) -> List[Dict]:
    """Fetch inbound customer messages for a conversation ordered chronologically."""
    sql = """
        SELECT message, message_side, created_at, created_by, tags
        FROM messages
        WHERE conversation_id = %s::uuid
          AND message_side = 'user_to_system'
        ORDER BY created_at ASC
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (conversation_id,))
                rows = await cur.fetchall()
                return [
                    {
                        "message":      r["message"],
                        "message_side": r["message_side"],
                        "created_at":   r["created_at"],
                        "created_by":   r["created_by"],
                        "tags":         r.get("tags") if isinstance(r, dict) else None,
                    }
                    for r in rows
                ]
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[ANALYZER] Failed to fetch messages for {conversation_id}: {exc}")
        return []


def _format_messages(messages: List[Dict]) -> str:
    """Convert message rows into a readable transcript for the LLM."""
    lines: List[str] = []
    for msg in messages:
        role = "Customer" if msg["message_side"] == "user_to_system" else "Bot"
        content = (msg.get("message") or "").strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(empty conversation)"


# ─── Post-conversation order check ───────────────────────────────────────────

async def _check_post_conversation_orders(
    phone: str,
    client_id: str,
    last_msg_at: Optional[datetime],
) -> List[Dict]:
    """
    Query Shopify for orders created by this phone within
    ORDER_CONVERSION_WINDOW_HOURS after the conversation ended.

    Returns a list of dicts with order_id, created_at, total_price for each
    matching order, or an empty list.

    Deliberately bypasses ``CustomerOrderOrchestrator.aget_customer_orders``:
    that orchestrator runs the full enrichment pipeline (Shopify GraphQL line
    items, Shiprocket/Delhivery tracking status, tracking-URL backfill) on
    every returned order, each a sequential network call. This check only
    needs id/created_at/total_price, so it calls the raw order service and
    processor directly — cutting per-conversation latency in the analytics
    cron from several enrichment round-trips down to a single order lookup.
    """
    if not phone or not last_msg_at:
        return []

    try:
        from fashion_bot.core.factory import ServiceFactory
        from fashion_bot.utils.phone_number_utils import is_real_phone_number

        if not is_real_phone_number(phone):
            return []

        state = {"client_id": client_id}
        primary_vendor = ServiceFactory.get_primary_vendor(state)
        order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
        raw_orders = await order_service.aget_orders_by_customer_phone(phone, limit=50, state=state)
        if not raw_orders:
            return []

        processor = ServiceFactory.get_order_processor(primary_vendor)
        orders = processor.process_orders(raw_orders, state=state)
        if not orders:
            return []

        cutoff_start = last_msg_at
        cutoff_end = last_msg_at + timedelta(hours=ORDER_CONVERSION_WINDOW_HOURS)

        # Ensure cutoffs are tz-aware
        if cutoff_start.tzinfo is None:
            cutoff_start = cutoff_start.replace(tzinfo=timezone.utc)
        if cutoff_end.tzinfo is None:
            cutoff_end = cutoff_end.replace(tzinfo=timezone.utc)

        matched: List[Dict] = []
        for order in orders:
            raw_created = order.get("created_at") or ""
            if not raw_created:
                continue
            try:
                if isinstance(raw_created, str):
                    created = datetime.fromisoformat(
                        raw_created.replace("Z", "+00:00")
                    )
                elif isinstance(raw_created, datetime):
                    created = raw_created
                else:
                    continue

                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)

                if cutoff_start <= created <= cutoff_end:
                    matched.append({
                        "order_id": order.get("name") or order.get("order_id") or str(order.get("id", "")),
                        "created_at": created.isoformat(),
                        "total_price": order.get("total_price"),
                    })
            except Exception:
                continue

        if matched:
            logger.info(
                f"[ANALYZER] Found {len(matched)} post-conversation order(s) for "
                f"{phone[-4:]}**** within {ORDER_CONVERSION_WINDOW_HOURS}h window"
            )
        return matched

    except Exception as exc:
        logger.warning(f"[ANALYZER] Post-conversation order check failed: {exc}")
        return []


async def _filter_unlinked_conversion_orders(
    client_id: str,
    conversation_id: str,
    post_conversation_orders: List[Dict],
) -> List[Dict]:
    """
    Keep only orders not already linked as a conversion for this client.

    This prevents a later post-order/status conversation from re-attributing
    the same order that an earlier pre-sales conversation already assisted.
    """
    order_ids = [
        str(order.get("order_id") or order.get("name") or order.get("id") or "").strip()
        for order in (post_conversation_orders or [])
    ]
    order_ids = [oid for oid in order_ids if oid]
    if not order_ids:
        return []

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT order_id, conversation_id::text
                    FROM order_conversation_links
                    WHERE client_id = %s::uuid
                      AND link_type = 'conversion'
                      AND order_id = ANY(%s)
                    """,
                    (client_id, order_ids),
                )
                linked_rows = await cur.fetchall()
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.warning(
            "[ANALYZER] Failed to check existing conversion links; "
            "leaving post-conversation orders unfiltered: %s",
            exc,
        )
        return post_conversation_orders

    linked_elsewhere = {
        row["order_id"]
        for row in linked_rows
        if row.get("conversation_id") != conversation_id
    }
    if not linked_elsewhere:
        return post_conversation_orders

    filtered = [
        order for order in post_conversation_orders
        if str(order.get("order_id") or order.get("name") or order.get("id") or "").strip()
        not in linked_elsewhere
    ]
    logger.info(
        "[ANALYZER] Filtered %d already-attributed order(s) for conversation %s: %s",
        len(post_conversation_orders) - len(filtered),
        conversation_id,
        sorted(linked_elsewhere),
    )
    return filtered


def _format_post_orders(orders: List[Dict]) -> str:
    """Format matched orders into a readable string for the LLM prompt."""
    if not orders:
        return "No new orders were placed by this customer within 12 hours after the conversation."
    lines = [
        f"The following order(s) were placed within 12 hours AFTER this conversation ended:"
    ]
    for o in orders:
        lines.append(
            f"  - Order {o['order_id']}, created {o['created_at']}, "
            f"total {o.get('total_price', 'N/A')}"
        )
    lines.append(
        "This strongly suggests the bot influenced the customer's purchase decision."
    )
    return "\n".join(lines)


# ─── Core analysis ───────────────────────────────────────────────────────────

async def analyze_conversation(
    conversation_id: str,
    messages: List[Dict],
    client_id: str,
    phone: str,
    conv_started: Optional[datetime] = None,
    last_msg_at: Optional[datetime] = None,
    post_conversation_orders: Optional[List[Dict]] = None,
) -> Optional[Dict]:
    """
    Send the conversation to the LLM with the central analytics prompt.

    Returns the parsed JSON analysis dict or None on failure.
    """
    conversation_text = _format_messages(messages)
    message_count = len(messages)
    post_orders_text = _format_post_orders(post_conversation_orders or [])

    duration_minutes = 0
    if conv_started and last_msg_at:
        try:
            delta = last_msg_at - conv_started
            duration_minutes = max(int(delta.total_seconds() / 60), 0)
        except Exception:
            pass

    format_kwargs = dict(
        conversation_text=conversation_text,
        client_id=client_id,
        phone=phone or "(unknown)",
        message_count=message_count,
        duration_minutes=duration_minutes,
        post_conversation_orders=post_orders_text,
    )

    # Try DB prompt first, fall back to canonical default
    db_prompt = await _aget_prompt_from_db(client_id)

    user_prompt = None
    if db_prompt:
        system_prompt = (
            "You are an expert e-commerce conversation analyst. "
            "Always return valid JSON. Never include text outside the JSON object."
        )
        # Tenant prompts may additionally reference the client's business
        # policies. Resolve only the ones this template actually uses, so a
        # prompt without a policies section pays no extra config reads.
        db_kwargs = dict(format_kwargs)
        db_kwargs.update(await _aget_policy_context(
            client_id, _find_unresolved_placeholders(db_prompt, format_kwargs)
        ))
        user_prompt = _render_prompt(db_prompt, db_kwargs)
        prompt_version = "db"

        unresolved = _find_unresolved_placeholders(db_prompt, db_kwargs)
        if unresolved:
            # Rendering no longer aborts, so the prompt is still usable — but a
            # token we cannot fill would reach the LLM verbatim, which silently
            # degrades this tenant's analytics. Keep it loud.
            logger.error(
                "[ANALYZER] DB prompt for client %s references placeholder(s) "
                "%s that are not supplied; they will reach the LLM unfilled "
                "(conversation %s).",
                client_id, ", ".join(unresolved), conversation_id,
            )

    if user_prompt is None:
        system_prompt, user_template, prompt_version = _get_default_prompt_parts()
        user_prompt = _render_prompt(user_template, format_kwargs)

    try:
        from fashion_bot.core.llm_factory import LLMInvoker

        # Scope OTel baggage to this analysis so llm.* metrics get tagged
        # with the conversation's client_id (LLMInvoker passes client_id
        # for LLM-config selection only — it does not set baggage).
        # See request_client_id docstring.
        with request_client_id(client_id):
            raw = await LLMInvoker.ainvoke(
                prompt=user_prompt,
                tool_name="conversation_analytics",
                client_id=client_id,
                system_prompt=system_prompt,
            )

        raw = raw.strip()
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning(
                f"[ANALYZER] No JSON found in LLM response for conversation {conversation_id}"
            )
            return None

        parsed: Dict = json.loads(raw[start:end])
        parsed["_prompt_version"] = prompt_version
        return parsed

    except json.JSONDecodeError as exc:
        logger.warning(f"[ANALYZER] JSON parse error for {conversation_id}: {exc}")
    except Exception as exc:
        logger.error(
            f"[ANALYZER] LLM call failed for {conversation_id}: {exc}", exc_info=True
        )
    return None


# ─── Store results ───────────────────────────────────────────────────────────

def _normalize_tag_list(raw: Any) -> List[str]:
    """Normalize tag config/message values into clean, de-duplicated strings."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, (list, dict)):
                return _normalize_tag_list(parsed)
        except Exception:
            pass
        values = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, dict):
        values = list(raw.keys())
    elif isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        return []

    normalized: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            normalized.append(text)
    return normalized


async def _aget_global_lead_generation_config() -> Dict[str, List[str]]:
    """
    Load global lead tag config from global_configs.

    Supported config_value shapes:
      ["Product Query", "Recommendation"]
      {"lead_generation_tags": [...], "preorder_lead_tags": [...]}

    Source of truth:
      global_configs.config_key = 'lead_generation_tags'
    """
    fallback = {
        "lead_generation_tags": FALLBACK_LEAD_GENERATION_TAGS[:],
        "preorder_lead_tags": FALLBACK_PREORDER_LEAD_TAGS[:],
    }

    async def _load_from_db() -> Optional[Any]:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT config_value
                    FROM global_configs
                    WHERE config_key = 'lead_generation_tags'
                    LIMIT 1
                    """
                )
                row = await cur.fetchone()
                if not row:
                    return None
                config_value = row.get("config_value") if isinstance(row, dict) else row[0]
                if isinstance(config_value, str):
                    return json.loads(config_value)
                return config_value

    async def _get_from_redis() -> Optional[Any]:
        try:
            from fashion_bot.utils.redis_client import get_shared_async_redis_client

            client = await get_shared_async_redis_client()
            raw = await client.get("global_config:lead_generation_tags")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _set_to_redis(value: Any) -> None:
        try:
            from fashion_bot.utils.redis_client import get_shared_async_redis_client

            client = await get_shared_async_redis_client()
            await client.setex("global_config:lead_generation_tags", 3600, json.dumps(value or {}))
        except Exception:
            pass

    try:
        from fashion_bot.utils.tiered_cache import aget_with_tiered_cache

        raw, source = await aget_with_tiered_cache(
            cache_key="global_config:lead_generation_tags",
            ttl_seconds=600,
            load_from_source_fn=_load_from_db,
            get_from_redis_fn=_get_from_redis,
            set_to_redis_fn=_set_to_redis,
        )
        if not raw:
            logger.warning("[ANALYZER] No global lead_generation_tags config found; using fallback lead tags")
            return fallback

        lead_tags = fallback["lead_generation_tags"]
        preorder_tags = fallback["preorder_lead_tags"]
        if isinstance(raw, dict):
            configured_lead_tags = _normalize_tag_list(
                raw.get("lead_generation_tags") or raw.get("tags")
            )
            configured_preorder_tags = _normalize_tag_list(raw.get("preorder_lead_tags"))
            if configured_lead_tags:
                lead_tags = configured_lead_tags
            if configured_preorder_tags:
                preorder_tags = configured_preorder_tags
        else:
            configured_lead_tags = _normalize_tag_list(raw)
            if configured_lead_tags:
                lead_tags = configured_lead_tags

        logger.debug("[ANALYZER] Lead generation config loaded from %s tier", source)
        return {
            "lead_generation_tags": lead_tags,
            "preorder_lead_tags": preorder_tags,
        }
    except Exception as exc:
        logger.warning("[ANALYZER] Failed to load global lead_generation_tags: %s", exc)
        return fallback


def _calculate_lead_metadata(
    messages: List[Dict],
    lead_config: Dict[str, List[str]],
    *,
    converted: bool,
) -> Dict[str, Any]:
    """Deterministically classify lead status from customer-message tags."""
    customer_messages = [
        msg for msg in messages
        if msg.get("message_side") == "user_to_system"
    ]
    customer_message_count = len(customer_messages)

    customer_tags: List[str] = []
    seen = set()
    for msg in customer_messages:
        for tag in _normalize_tag_list(msg.get("tags")):
            key = tag.lower()
            if key not in seen:
                seen.add(key)
                customer_tags.append(tag)

    lead_tags = lead_config.get("lead_generation_tags") or FALLBACK_LEAD_GENERATION_TAGS
    preorder_tags = lead_config.get("preorder_lead_tags") or FALLBACK_PREORDER_LEAD_TAGS
    lead_tag_lookup = {tag.lower(): tag for tag in lead_tags}
    preorder_tag_lookup = {tag.lower(): tag for tag in preorder_tags}

    matched_lead_tags = [
        tag for tag in customer_tags
        if tag.lower() in lead_tag_lookup
    ]
    matched_preorder_tags = [
        tag for tag in customer_tags
        if tag.lower() in preorder_tag_lookup
    ]
    presales_message_count = 0
    for msg in customer_messages:
        msg_tags = {
            tag.lower()
            for tag in _normalize_tag_list(msg.get("tags"))
        }
        if msg_tags & (set(lead_tag_lookup) | set(preorder_tag_lookup)):
            presales_message_count += 1

    lead_source_tags = list(dict.fromkeys(matched_preorder_tags + matched_lead_tags))
    denied_tags = [
        tag for tag in customer_tags
        if tag.strip().lower() in NON_LEAD_SOURCE_TAGS
    ]

    is_lead = customer_message_count > 2 and bool(lead_source_tags) and not denied_tags
    lead_type = None
    if is_lead:
        if matched_preorder_tags:
            lead_type = "preorder_interest"
        elif any(tag.lower() == "wholesale inquiry" for tag in matched_lead_tags):
            lead_type = "wholesale_interest"
        else:
            lead_type = "product_interest"

    return {
        "is_lead": is_lead,
        "lead_type": lead_type,
        "lead_status": "converted" if is_lead and converted else ("in_market" if is_lead else None),
        "lead_source_tags": lead_source_tags,
        "lead_customer_message_count": customer_message_count,
        "lead_generated_at": datetime.now(timezone.utc) if is_lead else None,
        "lead_details": {
            "rule": "customer_message_count > 2 AND customer_message_tags overlap lead_generation_tags",
            "customer_message_count": customer_message_count,
            "customer_message_tags": customer_tags,
            "matched_lead_tags": matched_lead_tags,
            "matched_preorder_tags": matched_preorder_tags,
            "presales_message_count": presales_message_count,
            "denied_lead_tags": denied_tags,
            "configured_lead_generation_tags": lead_tags,
            "configured_preorder_lead_tags": preorder_tags,
        },
    }


def _determine_order_conversion_status(
    conversion: Dict,
    converted_order_ids: List[str],
    lead: Dict[str, Any],
) -> Dict[str, Any]:
    """Return the final order-conversion flags for conversation analytics."""
    lead_details = lead.get("lead_details") or {}
    customer_message_count = int(lead_details.get("customer_message_count") or 0)
    has_presales_context = (
        customer_message_count >= ORDER_ATTRIBUTION_MIN_CUSTOMER_MESSAGES
        and bool(lead.get("lead_source_tags"))
    )
    has_data_driven_conversion = bool(converted_order_ids) and has_presales_context
    llm_says_conversion = bool(conversion.get("conversion_assisted"))

    if has_data_driven_conversion and llm_says_conversion:
        detection_via = "both"
    elif has_data_driven_conversion:
        detection_via = "data_driven"
    elif llm_says_conversion:
        detection_via = "llm_inferred"
    else:
        detection_via = None

    return {
        "conversion_assisted": llm_says_conversion or has_data_driven_conversion,
        "conversion_detected_via": detection_via,
    }


@awith_retry
async def store_analysis(
    conversation_id: str,
    client_id: str,
    phone: str,
    message_count: int,
    analysis: Dict,
    messages: Optional[List[Dict]] = None,
    post_conversation_orders: Optional[List[Dict]] = None,
    first_message: Optional[str] = None,
    channel_type: Optional[str] = None,
) -> bool:
    """Write analysis results into conversation_analytics. Returns True on success."""

    cancel = analysis.get("cancellation_analysis") or {}
    conversion = analysis.get("order_conversion") or {}
    updates = analysis.get("order_updates") or {}
    satisfaction = analysis.get("customer_satisfaction") or {}
    effectiveness = analysis.get("bot_effectiveness") or {}
    escalation = analysis.get("escalation") or {}

    converted_order_ids = [o.get("order_id") for o in (post_conversation_orders or []) if o.get("order_id")]
    lead_config = await _aget_global_lead_generation_config()
    lead = _calculate_lead_metadata(messages or [], lead_config, converted=False)

    # Determine conversion detection method. Post-chat orders are data-driven
    # assisted conversions when the conversation had enough customer engagement
    # and at least one configured pre-sales/preorder tag. Later post-order/support
    # tags can block lead status, but should not erase an earlier pre-sales signal.
    # Pure post-order support can happen immediately before a Shopify webhook
    # arrives, but it should not be counted as AI-assisted revenue.
    conversion_status = _determine_order_conversion_status(
        conversion,
        converted_order_ids,
        lead,
    )
    detection_via = conversion_status["conversion_detected_via"]
    final_conversion_assisted = conversion_status["conversion_assisted"]
    lead = _calculate_lead_metadata(
        messages or [],
        lead_config,
        converted=final_conversion_assisted,
    )

    sql = """
        INSERT INTO conversation_analytics (
            client_id, conversation_id, phone_number,
            cancellation_attempted, cancellation_averted, cancellation_aversion_method,
            order_conversion_assisted, order_conversion_method,
            converted_order_ids, conversion_detected_via,
            is_lead, lead_type, lead_status, lead_source_tags,
            lead_customer_message_count, lead_generated_at, lead_details,
            order_update_performed, order_update_types,
            customer_satisfaction, bot_effectiveness,
            escalation_needed, escalation_reason,
            llm_analysis, llm_confidence, llm_reasoning,
            prompt_version, message_count,
            first_message, channel_type
        ) VALUES (
            %s::uuid, %s::uuid, %s,
            %s, %s, %s,
            %s, %s,
            %s::jsonb, %s,
            %s, %s, %s, %s,
            %s, %s, %s::jsonb,
            %s, %s::jsonb,
            %s, %s,
            %s, %s,
            %s::jsonb, %s, %s,
            %s, %s,
            %s, %s
        )
        ON CONFLICT (conversation_id) DO NOTHING
    """

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    client_id,
                    conversation_id,
                    phone,
                    cancel.get("cancellation_attempted"),
                    cancel.get("cancellation_averted"),
                    cancel.get("aversion_method"),
                    final_conversion_assisted,
                    conversion.get("conversion_method"),
                    json.dumps(converted_order_ids),
                    detection_via,
                    lead["is_lead"],
                    lead["lead_type"],
                    lead["lead_status"],
                    lead["lead_source_tags"],
                    lead["lead_customer_message_count"],
                    lead["lead_generated_at"],
                    json.dumps(lead["lead_details"], default=str),
                    updates.get("update_performed"),
                    json.dumps(updates.get("update_types") or []),
                    satisfaction.get("sentiment"),
                    effectiveness.get("verdict"),
                    escalation.get("needed"),
                    (escalation.get("reason") or "")[:100] or None,
                    json.dumps(analysis),
                    float(analysis.get("confidence", 0.0)),
                    (analysis.get("reasoning") or "")[:1000] or None,
                    str(analysis.get("_prompt_version", "unknown"))[:20],
                    message_count,
                    first_message,
                    channel_type,
                ))
            return True
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[ANALYZER] Failed to store analysis for {conversation_id}: {exc}")
        return False


# ─── Order ↔ Conversation linking ────────────────────────────────────────────

@awith_retry
async def _link_orders_to_conversation(
    client_id: str,
    conversation_id: str,
    phone: str,
    conv_started: Optional[datetime],
    last_msg_at: Optional[datetime],
    message_count: int,
    post_conversation_orders: List[Dict],
    analysis: Dict,
    channel_type: Optional[str] = None,
) -> None:
    """
    Insert rows into order_conversation_links for every order associated with
    this conversation.  Sources:
      - post_conversation_orders  -> link_type = 'conversion'
      - LLM cancellation_analysis -> link_type = 'cancellation_aversion'
      - LLM order_updates         -> link_type = 'order_update'
    """
    links: List[Dict] = []

    # 1. Post-conversation conversions (data-driven)
    for order in (post_conversation_orders or []):
        oid = order.get("order_id")
        if oid:
            links.append({"order_id": oid, "link_type": "conversion"})

    # 2. Cancellation aversion — use structured order_ids_involved from LLM
    cancel = analysis.get("cancellation_analysis") or {}
    if cancel.get("cancellation_averted"):
        order_ids = cancel.get("order_ids_involved") or []
        for oid in order_ids:
            if oid:
                links.append({"order_id": str(oid).upper(), "link_type": "cancellation_aversion"})
        # Fallback: extract from key_actions if LLM didn't populate order_ids_involved
        if not order_ids:
            import re
            for action in (analysis.get("key_actions") or []):
                oids = re.findall(r'(?:#?)((?:gv|grv|GV|GRV)\d+)', str(action), re.IGNORECASE)
                for oid in oids:
                    links.append({"order_id": oid.upper(), "link_type": "cancellation_aversion"})

    # 3. Order updates — use structured order_ids_updated from LLM
    updates = analysis.get("order_updates") or {}
    if updates.get("update_performed"):
        order_ids = updates.get("order_ids_updated") or []
        for oid in order_ids:
            if oid:
                links.append({"order_id": str(oid).upper(), "link_type": "order_update"})
        # Fallback: extract from key_actions
        if not order_ids:
            import re
            for action in (analysis.get("key_actions") or []):
                action_str = str(action).lower()
                if "update" in action_str or "change" in action_str or "address" in action_str:
                    oids = re.findall(r'(?:#?)((?:gv|grv|GV|GRV)\d+)', str(action), re.IGNORECASE)
                    for oid in oids:
                        links.append({"order_id": oid.upper(), "link_type": "order_update"})

    if not links:
        return

    # Deduplicate
    seen = set()
    unique_links = []
    for link in links:
        key = (link["order_id"], link["link_type"])
        if key not in seen:
            seen.add(key)
            unique_links.append(link)

    sql = """
        INSERT INTO order_conversation_links
            (client_id, order_id, conversation_id, phone_number,
             link_type, conversation_started_at, conversation_ended_at,
             message_count, channel_type)
        VALUES
            (%s::uuid, %s, %s::uuid, %s,
             %s, %s, %s,
             %s, %s)
        ON CONFLICT (order_id, conversation_id, link_type) DO NOTHING
    """

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                for link in unique_links:
                    await cur.execute(sql, (
                        client_id,
                        link["order_id"],
                        conversation_id,
                        phone,
                        link["link_type"],
                        conv_started,
                        last_msg_at,
                        message_count,
                        channel_type or "whatsapp",
                    ))
        logger.info(
            f"[ANALYZER] Linked {len(unique_links)} order(s) to conversation {conversation_id}"
        )
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.warning(f"[ANALYZER] Failed to link orders to conversation: {exc}")


@awith_retry
async def aget_conversations_for_order(
    order_id: str,
    client_id: str,
) -> List[Dict]:
    """
    Public lookup: given an order ID, return all linked conversations.

    Call from a sales-channel API endpoint to power "View conversation" buttons.

    Returns list of dicts:
        [{
            "conversation_id": "uuid",
            "phone_number": "+91...",
            "link_type": "conversion" | "cancellation_aversion" | "order_update",
            "conversation_started_at": "iso-datetime",
            "conversation_ended_at": "iso-datetime",
            "message_count": 12,
            "channel_type": "whatsapp",
            "linked_at": "iso-datetime",
        }, ...]
    """
    sql = """
        SELECT
            conversation_id::text,
            phone_number,
            link_type,
            conversation_started_at,
            conversation_ended_at,
            message_count,
            channel_type,
            linked_at
        FROM order_conversation_links
        WHERE order_id = %s AND client_id = %s::uuid
        ORDER BY conversation_started_at DESC
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (order_id, client_id))
                rows = await cur.fetchall()
                return [
                    {
                        "conversation_id":          r["conversation_id"],
                        "phone_number":             r["phone_number"],
                        "link_type":                r["link_type"],
                        "conversation_started_at":  r["conversation_started_at"].isoformat() if r["conversation_started_at"] else None,
                        "conversation_ended_at":    r["conversation_ended_at"].isoformat() if r["conversation_ended_at"] else None,
                        "message_count":            r["message_count"],
                        "channel_type":             r["channel_type"],
                        "linked_at":                r["linked_at"].isoformat() if r["linked_at"] else None,
                    }
                    for r in rows
                ]
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[ANALYZER] Failed to get conversations for order {order_id}: {exc}")
        return []


@awith_retry
async def aget_conversation_messages_for_order(
    order_id: str,
    client_id: str,
) -> List[Dict]:
    """
    Return the full message history for all conversations linked to an order.

    Useful for rendering a "View conversation" panel on the sales page.

    Returns list of dicts:
        [{
            "conversation_id": "uuid",
            "link_type": "conversion",
            "messages": [
                {"role": "Customer", "content": "...", "timestamp": "..."},
                {"role": "Bot",      "content": "...", "timestamp": "..."},
            ]
        }, ...]
    """
    links = await aget_conversations_for_order(order_id, client_id)
    if not links:
        return []

    result = []
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                for link in links:
                    cid = link["conversation_id"]
                    await cur.execute("""
                        SELECT message, message_side, created_at
                        FROM messages
                        WHERE conversation_id = %s::uuid
                        ORDER BY created_at ASC
                    """, (cid,))
                    msgs = [
                        {
                            "role": "Customer" if r["message_side"] == "user_to_system" else "Bot",
                            "content": r["message"],
                            "timestamp": r["created_at"].isoformat() if r["created_at"] else None,
                        }
                        for r in await cur.fetchall()
                    ]
                    result.append({
                        "conversation_id": cid,
                        "link_type": link["link_type"],
                        "phone_number": link["phone_number"],
                        "messages": msgs,
                    })
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[ANALYZER] Failed to fetch messages for order {order_id}: {exc}")

    return result


# ─── Batch runner ────────────────────────────────────────────────────────────

async def run_pending_analyses(
    batch_size: int = BATCH_SIZE,
    lookback_days: Optional[int] = None,
) -> int:
    """
    Fetch ended, unanalyzed conversations → run LLM analysis → store.

    Args:
        batch_size: max conversations per batch.
        lookback_days: only analyze conversations from the last N days.

    Returns the number of conversations analyzed.
    """
    conversations = await fetch_unanalyzed_conversations(
        batch_size, lookback_days=lookback_days
    )
    if not conversations:
        logger.debug("[ANALYZER] No unanalyzed conversations found.")
        return 0

    logger.info(f"[ANALYZER] Analyzing {len(conversations)} conversation(s).")
    analyzed = 0

    for conv in conversations:
        cid = conv["conversation_id"]
        client_id = conv["client_id"]
        phone = conv.get("phone") or ""
        first_message = conv.get("first_message")
        channel_type = conv.get("channel_type")
        conv_started = conv.get("conv_started")
        last_msg_at = conv.get("last_msg_at")

        messages = await _fetch_messages(cid)
        if not messages:
            logger.info(f"[ANALYZER] Skipping {cid} — no messages found.")
            continue

        # Data-driven: check if customer placed orders after the chat
        post_orders = await _check_post_conversation_orders(
            phone=phone, client_id=client_id, last_msg_at=last_msg_at,
        )
        post_orders = await _filter_unlinked_conversion_orders(
            client_id=client_id,
            conversation_id=cid,
            post_conversation_orders=post_orders,
        )

        try:
            result = await analyze_conversation(
                conversation_id=cid,
                messages=messages,
                client_id=client_id,
                phone=phone,
                conv_started=conv_started,
                last_msg_at=last_msg_at,
                post_conversation_orders=post_orders,
            )
        except Exception as exc:
            # A single conversation must never abort the whole sweep — that
            # would starve every other client queued in this batch. Record
            # it like any other soft failure and move on.
            logger.error(
                f"[ANALYZER] analyze_conversation raised for {cid} (client {client_id}): {exc}",
                exc_info=True,
            )
            await _arecord_analysis_failure(cid, f"analyze_conversation raised: {exc}")
            continue

        if result:
            stored = await store_analysis(
                conversation_id=cid,
                client_id=client_id,
                phone=phone,
                message_count=len(messages),
                analysis=result,
                messages=messages,
                post_conversation_orders=post_orders,
                first_message=first_message,
                channel_type=channel_type,
            )
            if stored:
                # Link orders to this conversation for sales-page lookups
                await _link_orders_to_conversation(
                    client_id=client_id,
                    conversation_id=cid,
                    phone=phone,
                    conv_started=conv_started,
                    last_msg_at=last_msg_at,
                    message_count=len(messages),
                    post_conversation_orders=post_orders,
                    analysis=result,
                    channel_type=channel_type,
                )
                logger.info(
                    f"[ANALYZER] {cid} analyzed — "
                    f"cancel_attempted={result.get('cancellation_analysis', {}).get('cancellation_attempted')}, "
                    f"effectiveness={result.get('bot_effectiveness', {}).get('verdict')}, "
                    f"confidence={result.get('confidence', 0):.2f}"
                )
                analyzed += 1
            else:
                logger.warning(f"[ANALYZER] Failed to store analysis for {cid}, will retry with backoff.")
                await _arecord_analysis_failure(cid, "store_analysis returned falsy")
        else:
            logger.warning(f"[ANALYZER] LLM analysis failed for {cid}, will retry with backoff.")
            await _arecord_analysis_failure(cid, "analyze_conversation returned falsy")

        await asyncio.sleep(0.05)

    logger.info(f"[ANALYZER] Batch complete — {analyzed}/{len(conversations)} conversation(s) analyzed.")
    return analyzed


async def run_pending_analyses_until_empty(
    batch_size: int = BATCH_SIZE,
    lookback_days: Optional[int] = None,
) -> int:
    """
    Drain all ended, unanalyzed conversations in the lookback window.

    Work is still fetched in small batches to keep DB reads and LLM calls
    bounded, but the cron no longer stops after the first batch.
    """
    total_analyzed = 0
    batches = 0

    while True:
        analyzed = await run_pending_analyses(
            batch_size=batch_size,
            lookback_days=lookback_days,
        )
        if analyzed <= 0:
            break

        total_analyzed += analyzed
        batches += 1

        logger.info(
            "[ANALYZER] Backlog drain progress — batches=%d total_analyzed=%d",
            batches,
            total_analyzed,
        )

    logger.info(
        "[ANALYZER] Backlog drain complete — total_analyzed=%d batches=%d",
        total_analyzed,
        batches,
    )
    return total_analyzed
