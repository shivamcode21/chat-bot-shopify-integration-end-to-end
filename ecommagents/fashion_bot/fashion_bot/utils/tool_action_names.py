"""Tool → UI action-label mapping for the streaming status indicator.

When the agent calls a tool mid-turn we surface a friendly "what the bot is
doing right now" label to the end user. It lands in the same place the web
widget otherwise cycles through "Looking for the answer" / "Typing your
response": the typing indicator. Instead of a generic spinner the user sees a
per-tool action name resolved from :data:`TOOL_ACTION_NAMES`.

Everything here is pure and idempotent (per AGENTS.md "Idempotent by Default"):
same inputs → same event, no shared state. The only side effect is the explicit
``writer`` push in :func:`emit_tool_action_events`, which fires at most once per
tool-call id thanks to the caller-threaded ``announced`` set — re-scanning a
message list that grows across supersteps never double-emits.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Iterable, List, Optional, Set, Tuple

# Maps the internal tool name (the ``@tool`` function name surfaced on an
# ``AIMessage.tool_calls`` entry) to the customer-facing action label rendered
# in the end UI. Keep labels short, present-tense, and free of internal jargon.
# Source mapping (module-private). Exposed below as an immutable view so the
# "shared, never mutated at runtime" invariant is enforceable, not conventional
# (AGENTS.md: idempotent-by-default / immutable shared state across tenants).
_TOOL_ACTION_NAMES: dict = {
    # product discovery
    "search_products": "Searching for products",
    "find_product_by_url": "Fetching the product details",
    "find_product_by_id": "Fetching the product details",
    "get_available_categories": "Looking for answer",
    "get_customization_config": "Checking customization options",
    # store / vendor info
    "get_vendor_information": "Fetching store information",
    "get_contact_information": "Fetching contact details",
    "get_nearest_store": "Finding your nearest store",
    # order lookups
    "get_order_details": "Fetching your order",
    "get_recent_orders": "Fetching your recent orders",
    "get_customers_delivered_orders_by_phone": "Fetching your orders",
    "fetch_customer_data": "Fetching your details",
    "annotate_order": "Updating the order details",
    "escalate_ndr_order": "Reporting your delivery issue",
    "check_grace_period_eligibility": "Checking your return or exchange options",
    "get_final_return_exchange_message": "Fetching your return and exchange details",
    # order placement
    "create_order": "Placing your order",
    "create_draft_order_for_prepaid": "Creating your order",
    "confirm_cod_order": "Confirming your order",
    "create_cart_order": "Placing your order",
    # cart
    "get_cart": "Checking your cart",
    "add_to_cart": "Adding this to your cart",
    "remove_from_cart": "Removing this from your cart",
    "update_cart_quantity": "Updating your cart",
    "show_cart": "Opening your cart",
    # order modifications
    "update_order_name_tool": "Updating the name on your order",
    "update_order_address": "Updating your delivery address",
    "update_order_size_tool": "Updating the size on your order",
    "update_order_phone_number_tool": "Updating your phone number",
    "update_order_email_tool": "Updating your email address",
    "change_order_product_tool": "Updating the item in your order",
    "cancel_order_tool": "Cancelling your order",
    # discounts / policy / feedback
    "get_discount_information": "Checking available offers",
    "get_repeated_discount_message": "Checking available offers",
    "get_sales_policy": "Checking our policy",
    "get_policy_information": "Checking our policy",
    "log_customer_feedback": "Saving your feedback",
    # support
    "escalate_to_agent": "Connecting you to our support team",
}

# Public read-only view — shared across all tenants/turns by reference, so it is
# exposed immutably to make accidental runtime mutation impossible.
TOOL_ACTION_NAMES = MappingProxyType(_TOOL_ACTION_NAMES)

# Shown when a tool has no explicit mapping, so an unmapped/new tool still reads
# naturally in the UI rather than leaking the raw function name.
DEFAULT_TOOL_ACTION = "Working on your request"

# Custom stream-event type for these labels. Additive to the streaming wire
# contract; forwarded verbatim to the widget, which renders ``action``.
TOOL_ACTION_EVENT_TYPE = "tool"


def resolve_tool_action_name(tool_name: Optional[str]) -> str:
    """Pure: map a tool name to its UI action label (or the default)."""
    if not tool_name:
        return DEFAULT_TOOL_ACTION
    return TOOL_ACTION_NAMES.get(str(tool_name).strip(), DEFAULT_TOOL_ACTION)


def build_tool_action_event(tool_name: str) -> dict:
    """Pure: build the custom stream event for a tool call.

    Shape (forwarded verbatim to the widget)::

        {"type": "tool", "tool": <name>, "action": <label>}
    """
    return {
        "type": TOOL_ACTION_EVENT_TYPE,
        "tool": str(tool_name or ""),
        "action": resolve_tool_action_name(tool_name),
    }


def iter_new_tool_calls(
    messages: Iterable[Any], announced: Optional[Set[str]] = None
) -> List[Tuple[str, str]]:
    """Pure: return ``(call_id, tool_name)`` for tool calls not in ``announced``.

    Reads ``AIMessage.tool_calls`` (LangChain normalises provider differences
    into ``[{"id", "name", "args"}, ...]``). Deduped by call id — both against
    the ``announced`` set and within this scan — so a message list that grows
    across supersteps never yields the same call twice.
    """
    new_calls: List[Tuple[str, str]] = []
    seen: Set[str] = set(announced or ())
    for msg in messages or []:
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "").strip()
            call_id = str(call.get("id") or name)
            if not name or call_id in seen:
                continue
            seen.add(call_id)
            new_calls.append((call_id, name))
    return new_calls


def emit_tool_action_events(
    writer: Any, messages: Iterable[Any], announced: Optional[Set[str]] = None
) -> Set[str]:
    """Idempotent: push a ``tool`` action event for each newly-seen tool call.

    Returns the updated set of announced call ids; thread it back in on the next
    call so re-scanning the (growing) message list never double-emits. A no-op
    when ``writer`` is ``None`` (non-streaming channels) or there are no new
    calls, so streaming and non-streaming paths stay byte-consistent downstream.
    """
    seen: Set[str] = set(announced or ())
    if writer is None:
        return seen
    for call_id, tool_name in iter_new_tool_calls(messages, seen):
        writer(build_tool_action_event(tool_name))
        seen.add(call_id)
    return seen
