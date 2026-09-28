import json
import logging
import re
from typing import Any, Dict, List, Optional
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.config_manager import aget_client_id_by_app_name

logger = logging.getLogger(__name__)

# Per-client cache: { client_id: phone_number_or_None }
_AGENT_PHONE_CACHE: Dict[str, Optional[str]] = {}


async def aload_agent_phone_number(client_id: Optional[str] = None) -> None:
    """Load agent phone number from Postgres client_configs table (async)."""
    resolved_cid = client_id or ""
    if not resolved_cid:
        from fashion_bot.env_loader import get_env
        app_name = get_env("GUPSHUP_APP_NAME") or get_env("APP_NAME") or ""
        if app_name:
            resolved_cid = await aget_client_id_by_app_name(app_name) or ""

    if not resolved_cid:
        logger.warning("Cannot load agent phone number: no client_id available")
        return

    if resolved_cid in _AGENT_PHONE_CACHE:
        return

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT config_value
                    FROM client_configs
                    WHERE client_id = %s AND config_key = 'escalation_contact'
                    """,
                    (resolved_cid,)
                )

                result = await cur.fetchone()
                if result:
                    config_value = result.get('config_value') if isinstance(result, dict) else result[0]
                    if config_value:
                        config_data = json.loads(config_value) if isinstance(config_value, str) else config_value
                        phone = config_data.get("AGENT_PHONE_NUMBER")
                        _AGENT_PHONE_CACHE[resolved_cid] = phone
                        logger.info(f"Loaded agent phone number from database for client {resolved_cid}: {phone}")
                        return
                    
                logger.warning(f"No escalation_contact config found for client {resolved_cid}")
                _AGENT_PHONE_CACHE[resolved_cid] = None

    except Exception as e:
        logger.error(f"Failed to load agent phone number from database: {e}")
        _AGENT_PHONE_CACHE[resolved_cid] = None


def get_agent_phone_number(client_id: Optional[str] = None) -> Optional[str]:
    """Get the cached agent phone number. Returns None if not yet loaded."""
    if client_id and client_id in _AGENT_PHONE_CACHE:
        return _AGENT_PHONE_CACHE[client_id]
    for v in _AGENT_PHONE_CACHE.values():
        if v:
            return v
    return None


async def aget_agent_phone_number(client_id: Optional[str] = None) -> Optional[str]:
    """Get the agent phone number, loading from DB if not cached (async)."""
    resolved_cid = client_id or ""
    if resolved_cid and resolved_cid in _AGENT_PHONE_CACHE:
        return _AGENT_PHONE_CACHE[resolved_cid]
    await aload_agent_phone_number(client_id=resolved_cid or None)
    return _AGENT_PHONE_CACHE.get(resolved_cid)


async def areload_agent_phone_number(client_id: Optional[str] = None) -> Optional[str]:
    """Force reload agent phone number from database (async)."""
    resolved_cid = client_id or ""
    if resolved_cid in _AGENT_PHONE_CACHE:
        del _AGENT_PHONE_CACHE[resolved_cid]
    await aload_agent_phone_number(client_id=resolved_cid or None)
    return _AGENT_PHONE_CACHE.get(resolved_cid)


# ==========================================================================
# Multi-number, agent-routed escalation notifications
# (design_docs / ESCALATION_MULTI_NUMBER_AGENT_ROUTING)
#
# These resolvers read the existing ``escalation_contact`` config key through
# the tiered cache (memory -> Redis -> DB) via ``aget_config`` and route an
# escalation to the number(s) / email(s) configured for the agent or category
# that raised it. They are fully backward compatible: a legacy
# ``{"AGENT_PHONE_NUMBER": "<one number>"}`` config resolves to that single
# number (the legacy fallback), so behaviour is unchanged until a client adds
# an ``ESCALATION_ROUTING`` block.
# ==========================================================================

# Routing keys are the exact agent names from ``core/tool_registry.TOOL_REGISTRY``.
# Maps the LLM/operational escalation ``category`` to the agent bucket whose
# configured contacts should receive it. Code-owned default; a client may
# override the *numbers* per agent purely through DB config.
CATEGORY_TO_AGENT: Dict[str, str] = {
    # cancel / update order
    "Cancellation Requests": "cancel_or_update_order",
    "Order Update": "cancel_or_update_order",
    "Order Cancellation - Non-Integrated Partner": "cancel_or_update_order",
    "System Error - Order Update/Cancel Failed": "cancel_or_update_order",
    # delivery timeline (pre-purchase ETA questions)
    "Delivery Timeline Inquiry": "delivery_timeline",
    # return / exchange
    "Pickup Query": "return_exchange",
    "Return Request": "return_exchange",
    "Exchange Request": "return_exchange",
    "Return Delayed": "return_exchange",
    "Exchange Delayed": "return_exchange",
    # product details
    "Restocking Query": "product_details",
    "Product Complaint": "product_details",
    "Custom Sizing Request": "product_details",
    "Offline Store Suggestion": "product_details",
    "Walk-in Appointment": "product_details",
    # recommendations
    "Recommendation Hand-off": "recommendations",
    # discount / wholesale
    "Bulk Order Discount": "discount",
    "Bulk Order": "discount",
    "Wholesale Inquiry": "discount",
    "B2B Order": "discount",
    # cart
    "Cart Issue": "cart_management",
    # order status (logistics exceptions + post-purchase operational)
    "Order Status Query": "order_status",
    "Delivery Query": "order_status",
    "Order Delivery Delayed": "order_status",
    "Courier Update Pending": "order_status",
    "Misrouted Order": "order_status",
    "Undelivered Order": "order_status",
    "Delay in Dispatch": "order_status",
    "Damaged in Transit": "order_status",
    "Earlier Delivery Request": "order_status",
    "Payment/Refund Status": "order_status",
    "Refund Delayed": "order_status",
    "Warranty Claim": "order_status",
    # escalation agent (bot capability gap)
    "Callback Request": "escalation",
    "Frustration": "escalation",
}

# Coarse last-resort hint mapping ``state.parent_intent`` to an agent bucket.
PARENT_INTENT_TO_AGENT: Dict[str, str] = {
    "Sales & Product Discovery": "product_details",
    "Post-Purchase Support": "order_status",
}

# Operational bucket a category falls into. Code-owned default; a client may
# override/extend via the ``escalation_group_categories`` config key.
CATEGORY_TO_ESCALATION_GROUP: Dict[str, str] = {
    # pre_sales — before a purchase / bot capability gap
    "Restocking Query": "pre_sales",
    "Product Complaint": "pre_sales",
    "Custom Sizing Request": "pre_sales",
    "Recommendation Hand-off": "pre_sales",
    "Delivery Timeline Inquiry": "pre_sales",
    "Bulk Order Discount": "pre_sales",
    "Bulk Order": "pre_sales",
    "Wholesale Inquiry": "pre_sales",
    "B2B Order": "pre_sales",
    "Cart Issue": "pre_sales",
    "Callback Request": "pre_sales",
    "Frustration": "pre_sales",
    "General": "pre_sales",
    # post_sales — after a purchase / operational failure
    "Courier Update Pending": "post_sales",
    "Cancellation Requests": "post_sales",
    "Order Update": "post_sales",
    "Order Cancellation - Non-Integrated Partner": "post_sales",
    "System Error - Order Update/Cancel Failed": "post_sales",
    "Delivery Query": "post_sales",
    "Order Delivery Delayed": "post_sales",
    "Payment/Refund Status": "post_sales",
    "Refund Delayed": "post_sales",
    "Pickup Query": "post_sales",
    "Return Request": "post_sales",
    "Exchange Request": "post_sales",
    "Return Delayed": "post_sales",
    "Exchange Delayed": "post_sales",
    "Misrouted Order": "post_sales",
    "Undelivered Order": "post_sales",
    "Delay in Dispatch": "post_sales",
    "Damaged in Transit": "post_sales",
    "Earlier Delivery Request": "post_sales",
    "Order Status Query": "post_sales",
    "Warranty Claim": "post_sales",
    # offline_leads — physical-store / offline channel intent
    "Offline Store Suggestion": "offline_leads",
    "Walk-in Appointment": "offline_leads",
}

# Categories whose operational group depends on whether the customer already has
# an order in play. These default to ``pre_sales`` (a bot-capability gap with no
# purchase context), but when an ``order_id`` is present the escalation is really
# about an existing order — a post-purchase concern — so it routes to
# ``post_sales`` instead. ``Frustration`` is the canonical case: a frustrated
# customer with no order is a pre-sales lead, while a frustrated customer chasing
# an existing order (e.g. "waiting 13 days for gv16384") is post-sales.
# ``General`` (the catch-all) is included for the same reason: an otherwise
# unclassified escalation that references an order is a post-purchase concern.
# ``Delivery Timeline Inquiry`` is context-sensitive: without an order it's a
# pre-purchase "how long does shipping take?" question; with an order it's a
# post-purchase "where is my package?" concern.
# ``Callback Request`` is context-sensitive: without an order it's a pre-sales
# lead wanting to talk; with an order it's a customer needing help with their
# existing purchase.
ORDER_CONTEXT_SENSITIVE_CATEGORIES: frozenset = frozenset({"Frustration", "General", "Delivery Timeline Inquiry", "Callback Request", "Recommendation Hand-off"})

# Closed set of categories the ``escalate_to_agent`` tool advertises. Kept as the
# single source of truth; ``tests/test_escalation_routing.py`` asserts that every
# value except ``"General"`` is a key in both ``CATEGORY_TO_AGENT`` and
# ``CATEGORY_TO_ESCALATION_GROUP`` so the maps can't drift apart.
ESCALATION_TOOL_CATEGORIES = (
    # pre_sales
    "Bulk Order Discount",
    "B2B Order",
    "Callback Request",
    "Cart Issue",
    "Custom Sizing Request",
    "Delivery Timeline Inquiry",
    "Frustration",
    "Product Complaint",
    "Recommendation Hand-off",
    "Restocking Query",
    # post_sales
    "Cancellation Requests",
    "Damaged in Transit",
    "Delay in Dispatch",
    "Courier Update Pending",
    "Delivery Query",
    "Earlier Delivery Request",
    "Exchange Delayed",
    "Exchange Request",
    "Misrouted Order",
    "Order Cancellation - Non-Integrated Partner",
    "Order Delivery Delayed",
    "Order Status Query",
    "Order Update",
    "Payment/Refund Status",
    "Pickup Query",
    "Refund Delayed",
    "Return Delayed",
    "Return Request",
    "System Error - Order Update/Cancel Failed",
    "Undelivered Order",
    "Warranty Claim",
    # catch-all
    "General",
)

VALID_ESCALATION_GROUPS = ("pre_sales", "post_sales", "offline_leads")


def _slugify_category(value: str) -> str:
    """Normalize a category string to a comparison slug (lower, underscores)."""
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


# Precomputed slug -> canonical lookup so case / spacing / punctuation variants
# of a real category (e.g. "delivery query", "Delivery-Query") resolve back to
# the canonical form.
_CATEGORY_SLUG_LOOKUP: Dict[str, str] = {
    _slugify_category(c): c for c in ESCALATION_TOOL_CATEGORIES
}

# Off-enum category slugs the platform or the LLM may emit, mapped to the
# closest canonical ``ESCALATION_TOOL_CATEGORIES`` value. Keys are slug form
# (see ``_slugify_category``). These mirror the internal ``escalation_type``
# slugs used by the specialized escalation paths (e.g. ``escalate_urgent_delivery``
# logs ``escalation_type="urgent_delivery"`` under the ``"Delivery Query"``
# category). Extend as new aliases surface.
ESCALATION_CATEGORY_ALIASES: Dict[str, str] = {
    "urgent_delivery": "Delivery Query",
    "urgent_delivery_request": "Delivery Query",
    "cancellation_threat": "Cancellation Requests",
    "callback": "Callback Request",
    "restocking_query": "Restocking Query",
}


def normalize_escalation_category(category: Optional[str]) -> str:
    """Normalize a free-form escalation category to a canonical value.

    ``escalate_to_agent`` accepts a free-form ``category`` string (no enum
    validation), so the LLM sometimes passes an off-enum slug (e.g.
    ``"urgent_delivery"``) or a case/spacing variant of a real category.
    Resolving it to a canonical ``ESCALATION_TOOL_CATEGORIES`` value keeps the
    staff-card title, group routing, and analytics consistent instead of
    surfacing a raw slug that also misroutes the escalation group.

    Resolution order: exact canonical match → case/spacing/punctuation-insensitive
    match against canonical names → known alias slug → conservative ``"General"``
    fallback.
    """
    if not category or not str(category).strip():
        return "General"
    raw = str(category).strip()
    # 1. Exact canonical match (fast path — already valid).
    if raw in ESCALATION_TOOL_CATEGORIES:
        return raw
    slug = _slugify_category(raw)
    # 2. Loose match against canonical names (case / spacing / punctuation).
    if slug in _CATEGORY_SLUG_LOOKUP:
        return _CATEGORY_SLUG_LOOKUP[slug]
    # 3. Known off-enum alias slugs.
    if slug in ESCALATION_CATEGORY_ALIASES:
        return ESCALATION_CATEGORY_ALIASES[slug]
    # 4. Conservative catch-all.
    return "General"

_TRUTHY_STRINGS = {"1", "true", "yes", "on", "enabled"}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return False


# ==========================================================================
# Resolution-first escalation — actionability gate
# ==========================================================================
# The escalation_handler prompt owns "try to resolve before handing off". Code
# only enforces the one thing a prompt cannot: an escalation the agent itself
# flagged as impossible to fulfil is turned back into an "offer the real
# alternatives" reply instead of a hand-off a human could not action either.

# Categories whose escalation is a MANDATORY hand-off — the bot is not permitted
# to perform the action, so a human must. The gate ALWAYS lets these through.
# Cancellation is the canonical case (the cancellation_handler prompt states
# "You NEVER cancel orders directly; all cancellation requests must be
# escalated"); the two system-generated leads are hand-offs the bot cannot
# "resolve" at all.
MANDATORY_ESCALATION_CATEGORIES: frozenset = frozenset(
    {
        "Cancellation Requests",
        "Order Cancellation - Non-Integrated Partner",
        "Callback Request",
        "Courier Update Pending",
        "Offline Store Suggestion",
        "Walk-in Appointment",
    }
)


def is_mandatory_escalation_category(category: Optional[str]) -> bool:
    """True when a category's escalation is a mandatory human hand-off (pure).

    An EXACT match on the raw category wins first, so valid escalation
    categories that are NOT in the LLM tool enum (system categories like
    ``"Offline Store Suggestion"``) are recognised instead of being collapsed to
    ``"General"`` by ``normalize_escalation_category``.
    """
    raw = (category or "").strip()
    if raw in MANDATORY_ESCALATION_CATEGORIES:
        return True
    return normalize_escalation_category(category) in MANDATORY_ESCALATION_CATEGORIES


def classify_escalation_actionability(
    category: Optional[str], *, human_can_resolve: bool = True
) -> str:
    """Classify whether a human can actually fulfil this escalation (pure).

    **LLM-driven — no keyword matching.** The agent that decided to escalate is
    the one that judged the situation, so it declares whether a human can act on
    the request via the ``human_can_resolve`` argument of ``escalate_to_agent``
    (exactly like it already sets ``escalation_classification``). This maps that
    signal, plus the category, onto the three labels the gate uses:

    * ``"mandatory"``     — a mandatory hand-off (cancellation, callback, system
      lead, …). Checked FIRST and independent of the LLM signal, so a legitimate
      hand-off is never suppressed.
    * ``"unfulfillable"`` — the agent set ``human_can_resolve=False`` (e.g. a
      variant/SKU that does not exist, which a human cannot create either).
    * ``"actionable"``    — a normal escalation a human can act on (the default).
    """
    if is_mandatory_escalation_category(category):
        return "mandatory"
    if human_can_resolve is False:
        return "unfulfillable"
    return "actionable"


# ==========================================================================
# Resolution-first escalation — first-turn frustration routing
# ==========================================================================
# Intents that cannot RESOLVE a frustrated customer's underlying issue:
# ``escalation`` is the hand-off itself, and ``continuity_agent`` is the
# small-talk/ambiguous-ack handler — routing an angry customer there would
# neither help nor de-escalate.
NON_RESOLVING_INTENTS: frozenset = frozenset({"", "escalation", "continuity_agent"})


def frustration_should_escalate(
    detected_intents: Optional[List[Dict[str, Any]]],
    consecutive_frustrated_turns: int = 0,
) -> bool:
    """Router rule: should a frustration-flagged message escalate on this turn?

    Resolution-first: a frustrated customer who ALSO expressed a resolvable
    intent ("where's my order, this is terrible") should have that intent
    handled — the resolving agent de-escalates far better by actually helping.
    Only a STANDALONE frustration (pure anger, or an explicit escalation /
    small-talk intent with nothing to resolve) escalates on the first turn.

    ``consecutive_frustrated_turns`` is the number of PRIOR consecutive turns
    that were already frustration-flagged (the router's ``frustration_streak``
    scratchpad counter). Resolution-first gets exactly ONE chance: if the
    previous turn was already frustrated (streak >= 1), the resolving agent
    failed to de-escalate, so this turn escalates even when a resolvable
    intent is present — the loop-breaker for a customer who stays angry while
    still mentioning their order.

    Returns ``True`` (escalate) when there is no resolvable intent, or when
    frustration persisted from the previous turn. Pure/stateless — the
    intent-detection node does the routing and owns the streak counter.
    """
    if consecutive_frustrated_turns >= 1:
        return True
    intent_names = [
        i.get("intent") if isinstance(i, dict) else None for i in (detected_intents or [])
    ]
    has_resolvable_intent = any(
        name is not None and name not in NON_RESOLVING_INTENTS for name in intent_names
    )
    return not has_resolvable_intent


def escalation_resolution_tools_enabled(escalation_policy_config: Any) -> bool:
    """Whether the escalation node should get the expanded resolution toolset.

    **On by default** — the resolution-first escalation_handler prompt expects
    the read-only resolution tools (policy / product / delivery lookups), so they
    ship on. A client can opt OUT with
    ``escalation_policy.resolution_first_tools = false``; an absent key keeps the
    default.

    Accepts the raw ``escalation_policy`` config value (dict, JSON string, or
    ``None``) and degrades to ``True`` on anything malformed, so a bad config
    never silently strips the resolution tools.
    """
    data = escalation_policy_config
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return True
    if not isinstance(data, dict):
        return True
    if "resolution_first_tools" in data:
        return _is_truthy(data.get("resolution_first_tools"))
    return True


async def aescalation_order_notes_enabled(client_id: Optional[str] = None) -> bool:
    """Whether an escalation may stamp a note onto the order in the shop.

    **On by default** — the ``[Bloomerce] … Requires manual intervention`` /
    ``[Bot - FAILED] …`` notes are the ops audit trail for a hand-off, so they
    ship on. A client opts out with
    ``escalation_policy.order_notes_enabled = false``; an absent key keeps the
    default.

    Only ESCALATION-path notes are gated. A note the agent writes deliberately
    through ``annotate_order`` (e.g. an alternate mobile number) and the
    BLOOMERCE_EDITED stamp on a *successful* edit are unaffected — they are not
    escalations.

    Missing/malformed config degrades to ``True``, so a bad config can never
    silently drop the audit trail.
    """
    from fashion_bot.config_manager import aget_config

    try:
        raw = await aget_config("escalation_policy", client_id=client_id)
        if not raw:
            return True
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict) and "order_notes_enabled" in data:
            return _is_truthy(data.get("order_notes_enabled"))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(
            "aescalation_order_notes_enabled config read failed for %s: %s", client_id, e
        )
    return True


async def aget_escalation_customer_message(
    client_id: Optional[str] = None, *, default: str = ""
) -> str:
    """The customer-facing escalation confirmation, overridable per client.

    Reads ``escalation_messaging.customer_message``. When set it REPLACES the
    hard-coded confirmation on every escalation path — the escalate_to_agent
    tool result, the web-chat override, and the specialised escalation helpers
    — so a client who does not want a "someone will reach out to you" promise
    can say instead "your issue has been escalated, please contact support at
    …". The support phone/email are appended separately from
    ``vendor_contact_details``; do NOT repeat them here.

    Returns *default* (today's wording) when the key is unset, empty, or the
    config is malformed, so a bad config never blanks a customer reply.
    """
    from fashion_bot.config_manager import aget_config

    try:
        raw = await aget_config("escalation_messaging", client_id=client_id)
        if not raw:
            return default
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            message = data.get("customer_message")
            if isinstance(message, str) and message.strip():
                return message.strip()
    except Exception as e:  # pragma: no cover - defensive
        logger.debug(
            "aget_escalation_customer_message config read failed for %s: %s", client_id, e
        )
    return default


async def aevaluate_escalation_gate(
    *,
    client_id: Optional[str],
    category: Optional[str],
    human_can_resolve: bool = True,
) -> Dict[str, Any]:
    """Decide whether an escalation should be soft-blocked as unfulfillable.

    Always returns ``actionability`` for observability. ``soft_block`` is
    ``True`` ONLY when all three hold:

    1. the gate is enabled for the client — **on by default**, opt out with
       ``escalation_policy.gate_enabled = false``;
    2. the category is NOT a mandatory hand-off (cancellation, callback, …); and
    3. the escalating agent flagged the request unfulfillable
       (``human_can_resolve=False`` on the ``escalate_to_agent`` call).

    Missing/malformed config degrades to the default (gate on), so a bad config
    never silently disables the guard.
    """
    gate_enabled = True
    from fashion_bot.config_manager import aget_config

    try:
        raw = await aget_config("escalation_policy", client_id=client_id)
        if raw:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict) and "gate_enabled" in data:
                gate_enabled = _is_truthy(data.get("gate_enabled"))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("aevaluate_escalation_gate config read failed for %s: %s", client_id, e)

    actionability = classify_escalation_actionability(
        category, human_can_resolve=human_can_resolve
    )
    soft_block = bool(gate_enabled and actionability == "unfulfillable")
    message = None
    if soft_block:
        message = (
            "Do NOT escalate — this request cannot be fulfilled because the "
            "item or variant the customer asked for is not offered. Tell the "
            "customer clearly which options ARE available and ask which they'd "
            "like, or suggest the closest alternative. Only escalate if the "
            "customer explicitly asks for a human afterwards."
        )
    return {
        "actionability": actionability,
        "gate_enabled": gate_enabled,
        "soft_block": soft_block,
        "message": message,
    }


async def aget_escalation_routing(client_id: Optional[str] = None) -> Dict[str, Any]:
    """Read the ``escalation_contact`` config through the tiered cache.

    Returns the parsed config dict (carrying the legacy ``AGENT_PHONE_NUMBER``
    and/or the ``ESCALATION_ROUTING`` block). Returns ``{}`` on any
    missing/malformed config — callers degrade to the legacy fallback chain.
    """
    from fashion_bot.config_manager import aget_config

    try:
        raw = await aget_config("escalation_contact", client_id=client_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("aget_escalation_routing read failed for client %s: %s", client_id, e)
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Malformed escalation_contact JSON for client %s", client_id)
        return {}
    return data if isinstance(data, dict) else {}


def _parse_email_node(email_raw: Any) -> Dict[str, List[str]]:
    """Normalise an ``email`` config value into ``{"to": [...], "cc": [...]}``.

    Accepts the object shape ``{"to": [...], "cc": [...]}`` and the legacy list
    shape ``["a@b.com"]`` (treated as ``to``).
    """
    if not email_raw:
        return {"to": [], "cc": []}
    if isinstance(email_raw, list):
        return {"to": [str(e) for e in email_raw if e], "cc": []}
    if isinstance(email_raw, dict):
        to = email_raw.get("to") or []
        cc = email_raw.get("cc") or []
        if isinstance(to, str):
            to = [to]
        if isinstance(cc, str):
            cc = [cc]
        return {
            "to": [str(e) for e in to if e],
            "cc": [str(e) for e in cc if e],
        }
    return {"to": [], "cc": []}


def _parse_template_node(tmpl_raw: Any) -> Optional[Dict[str, Any]]:
    """Normalise a ``template`` block from a routing node.

    Returns the parsed dict or ``None`` when the node carries no template
    config.  ``param_order`` is intentionally omitted — it lives in the global
    ``escalation_template`` config key (one per client) and is inherited at
    send time by ``amaybe_send_escalation_template``.
    """
    if not tmpl_raw or not isinstance(tmpl_raw, dict):
        return None
    return {
        "enabled": tmpl_raw.get("enabled"),
        "template_id": tmpl_raw.get("template_id"),
        "image_url": tmpl_raw.get("image_url"),
    }


def _parse_contacts_node(node: Any) -> Dict[str, Any]:
    """Normalise a ``default`` / ``routes[key]`` node into channel lists.

    Handles the ``{"contacts": {"phone": [...], "email": {...}}}`` shape and the
    backward-compatible bare-list shape (phone-only, no email).  Also preserves
    the optional ``template`` block for per-route template resolution.
    """
    _empty: Dict[str, Any] = {"phone": [], "email": {"to": [], "cc": []}, "template": None}
    if node is None:
        return _empty
    if isinstance(node, list):
        return {"phone": [str(p) for p in node if p], "email": {"to": [], "cc": []}, "template": None}
    if isinstance(node, dict):
        contacts = node.get("contacts") if isinstance(node.get("contacts"), dict) else node
        phone = contacts.get("phone") or []
        if isinstance(phone, str):
            phone = [phone]
        return {
            "phone": [str(p) for p in phone if p],
            "email": _parse_email_node(contacts.get("email")),
            "template": _parse_template_node(node.get("template")),
        }
    return _empty


def _dedup_phones(phones: List[str]) -> List[str]:
    """De-dup a phone list preserving order.

    Keys on the country-code-stripped, digits-only form (matching the
    ``send_message`` normalisation) so the bare 10-digit, ``91...`` and
    ``+91...`` spellings of the same number collapse to a single recipient.
    """
    from fashion_bot.utils.phone_number_utils import (
        normalize_phone_number,
        strip_country_code,
    )

    seen = set()
    out: List[str] = []
    for p in phones:
        digits = normalize_phone_number(str(p))
        key = strip_country_code(digits) if digits else str(p)
        if not key:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(str(p))
    return out


def _dedup_preserve(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for it in items:
        if it in seen:
            continue
        seen.add(it)
        out.append(it)
    return out


async def aget_escalation_contacts(
    client_id: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    category: Optional[str] = None,
    parent_intent: Optional[str] = None,
    order_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve escalation contacts per channel for ``(agent, category, parent_intent)``.

    Each channel is resolved independently by walking candidate nodes
    most-specific-first — ``routes[category]`` -> ``routes[agent]`` ->
    ``routes[CATEGORY_TO_AGENT[category]]`` -> ``routes[PARENT_INTENT_TO_AGENT[parent_intent]]``
    -> ``buckets[escalation_group]`` -> ``default`` — taking the first node that
    has a value for that channel.
    Phone additionally falls back to the legacy ``AGENT_PHONE_NUMBER``.

    ``order_id`` feeds the ``buckets[escalation_group]`` step through the same
    order-aware resolver the staff card uses (:func:`aget_escalation_group`), so
    an order-context escalation (``Frustration`` / ``General`` with an order, or
    an unmapped category) is routed to the ``post_sales`` bucket its "Type" label
    advertises — not the static ``pre_sales`` default.

    Template resolution follows the same cascade: the first node whose
    ``template`` block exists determines the template config. A node with
    ``template.enabled = false`` explicitly disables templates (stops the
    cascade). When no routing node carries a template block the caller may
    still fall back to the separate ``escalation_template`` config key.

    Returns ``{"phone": [...], "email": {"to": [...], "cc": [...]},
    "template": {...} | None}`` with phone numbers de-duplicated.
    """
    routing = await aget_escalation_routing(client_id)
    block = routing.get("ESCALATION_ROUTING")
    block = block if isinstance(block, dict) else {}
    routes = block.get("routes") if isinstance(block.get("routes"), dict) else {}
    buckets = block.get("buckets") if isinstance(block.get("buckets"), dict) else {}
    default_node = block.get("default")
    legacy_phone = routing.get("AGENT_PHONE_NUMBER")

    # Candidate nodes, most-specific-first.
    candidates: List[Any] = []
    if category and category in routes:
        candidates.append(routes[category])
    if agent and agent in routes:
        candidates.append(routes[agent])
    if category:
        mapped = CATEGORY_TO_AGENT.get(category)
        if mapped and mapped in routes:
            candidates.append(routes[mapped])
    if parent_intent:
        pi_agent = PARENT_INTENT_TO_AGENT.get(parent_intent)
        if pi_agent and pi_agent in routes:
            candidates.append(routes[pi_agent])

    # Bucket-level fallback: resolve escalation_group for the category and check
    # if the config defines contacts at that bucket level. Order-aware (same
    # resolver as the staff card) so a Frustration/General/unmapped escalation
    # with an order in play routes to the post_sales bucket its label advertises.
    if buckets:
        group = await aget_escalation_group(client_id, category, order_id=order_id)
        if group in buckets:
            candidates.append(buckets[group])

    if default_node is not None:
        candidates.append(default_node)

    parsed = [_parse_contacts_node(c) for c in candidates]

    # Phone: first candidate with a non-empty phone list, else legacy fallback.
    phone: List[str] = []
    for p in parsed:
        if p["phone"]:
            phone = list(p["phone"])
            break
    if not phone and legacy_phone:
        phone = [str(legacy_phone)]

    # Email: first candidate whose email channel has a non-empty ``to`` list;
    # its ``cc`` travels with it.
    email_to: List[str] = []
    email_cc: List[str] = []
    for p in parsed:
        if p["email"]["to"]:
            email_to = list(p["email"]["to"])
            email_cc = list(p["email"]["cc"])
            break

    # Template: first candidate with a ``template`` block. ``enabled: false``
    # is an explicit disable (stops cascade, resolves to None).
    # ``param_order`` is NOT resolved here — it comes from the global
    # ``escalation_template`` config at send time.
    resolved_template: Optional[Dict[str, Any]] = None
    for p in parsed:
        tmpl = p.get("template")
        if tmpl is not None:
            if _is_truthy(tmpl.get("enabled")) and tmpl.get("template_id"):
                resolved_template = {
                    "template_id": str(tmpl["template_id"]),
                    "image_url": tmpl.get("image_url"),
                }
            break

    email_to = _dedup_preserve(email_to)
    email_cc = [c for c in _dedup_preserve(email_cc) if c not in set(email_to)]
    return {
        "phone": _dedup_phones(phone),
        "email": {"to": email_to, "cc": email_cc},
        "template": resolved_template,
    }


async def aget_escalation_recipients(
    client_id: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    category: Optional[str] = None,
    parent_intent: Optional[str] = None,
    order_id: Optional[str] = None,
) -> List[str]:
    """Backward-compatible thin wrapper returning the resolved ``phone`` channel."""
    contacts = await aget_escalation_contacts(
        client_id, agent=agent, category=category,
        parent_intent=parent_intent, order_id=order_id,
    )
    return contacts.get("phone", [])


async def aget_escalation_group(
    client_id: Optional[str] = None,
    category: Optional[str] = None,
    *,
    agent: Optional[str] = None,
    order_id: Optional[str] = None,
) -> str:
    """Resolve the operational ``escalation_group`` for a category.

    Checks the per-client ``escalation_group_categories`` override (tiered
    cache) first, then applies order-context inference, then falls back to the
    code-default ``CATEGORY_TO_ESCALATION_GROUP``.

    Order-context inference (only when an ``order_id`` is present) routes to
    ``post_sales`` in two cases:

    * the category is explicitly order-context-sensitive
      (``ORDER_CONTEXT_SENSITIVE_CATEGORIES`` — ``Frustration`` and the
      catch-all ``General``), overriding its static ``pre_sales`` mapping; or
    * the category is **unmapped** — an off-enum slug the LLM may pass
      (e.g. ``"urgent_delivery"``). With an order in play such an escalation is
      a post-purchase concern, so it defaults to ``post_sales`` rather than the
      conservative ``pre_sales`` fallback.

    A category that IS mapped keeps its configured group (so an explicit
    pre-sales category like ``"Bulk Order Discount"`` stays ``pre_sales`` even
    when an order is referenced). Categories with no order context, and unknown
    categories with no order, default to ``"pre_sales"`` (conservative).

    An explicit client override always wins: a client that deliberately places a
    category in a group keeps that group regardless of order context.
    """
    if not category:
        return "pre_sales"
    from fashion_bot.config_manager import aget_config

    try:
        raw = await aget_config("escalation_group_categories", client_id=client_id)
        if raw:
            data = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(data, dict):
                groups = data.get("escalation_group_categories", data)
                if isinstance(groups, dict):
                    for grp, cats in groups.items():
                        if grp in VALID_ESCALATION_GROUPS and category in (cats or []):
                            return grp
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("escalation_group_categories override read failed for %s: %s", client_id, e)

    # Order-context inference — an escalation tied to an existing order is a
    # post-purchase concern, not a pre-sales lead. Applies when the category is
    # explicitly order-context-sensitive (overriding its static pre_sales
    # mapping) OR unmapped (an off-enum slug like "urgent_delivery"). A mapped
    # category keeps its configured group. Only kicks in when the client did not
    # explicitly configure the category above.
    if order_id and (
        category in ORDER_CONTEXT_SENSITIVE_CATEGORIES
        or category not in CATEGORY_TO_ESCALATION_GROUP
    ):
        return "post_sales"

    return CATEGORY_TO_ESCALATION_GROUP.get(category, "pre_sales")


async def aget_store_visit_contacts(
    client_id: Optional[str], store: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve contacts for a store-visit notification (design §5.4b).

    Store-manager-first: the specific store's own contacts (``store["phone"]`` ->
    WhatsApp, ``store["manager_email"]`` -> email) are the primary recipients,
    PLUS the configured ``ESCALATION_ROUTING`` contacts for
    ``Offline Store Suggestion`` / ``product_details`` (so the ops/retail team
    also gets visibility). De-duplicated across both sources so no one is
    double-pinged.

    Template is included only when ``template_enabled_for_notification`` is
    truthy in the ``store_locations`` config (global flag for all stores).
    """
    store = store or {}
    phones: List[str] = []
    email_to: List[str] = []
    email_cc: List[str] = []

    # Priority 1 — the specific store's manager contacts (primary recipient).
    if store.get("phone"):
        phones.append(str(store["phone"]))
    if store.get("manager_email"):
        email_to.append(str(store["manager_email"]))

    # Priority 2+3 — configured routing (category -> agent -> default -> legacy).
    routing = await aget_escalation_contacts(
        client_id, agent="product_details", category="Offline Store Suggestion"
    )
    phones.extend(routing.get("phone") or [])
    routing_email = routing.get("email") or {}
    email_to.extend(routing_email.get("to") or [])
    email_cc.extend(routing_email.get("cc") or [])

    phones = _dedup_phones(phones)
    email_to = _dedup_preserve(email_to)
    email_cc = [c for c in _dedup_preserve(email_cc) if c not in set(email_to)]

    # Template: from the store_locations config (template_enabled_for_notification
    # + template_id), not from escalation_contact routing nodes.
    template: Optional[Dict[str, Any]] = None
    if client_id:
        from fashion_bot.utils.store_locations import aget_store_notification_template

        template = await aget_store_notification_template(client_id)

    return {"phone": phones, "email": {"to": email_to, "cc": email_cc}, "template": template}
