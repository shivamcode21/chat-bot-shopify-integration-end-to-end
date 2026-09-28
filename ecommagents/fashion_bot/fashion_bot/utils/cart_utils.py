"""
Cart Utilities Module

Pure data-transformation helpers for normalizing Shopify cart snapshots,
building cart context messages, and mirroring cart state into
conversation_context entities/topics.

These functions are stateless and do not perform I/O.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _cart_context_option_summary(event: Dict) -> str:
    if not isinstance(event, dict):
        return ""
    summary = str(
        event.get("option_summary")
        or event.get("optionSummary")
        or event.get("variant_title")
        or event.get("variantTitle")
        or ""
    ).strip()
    if summary:
        return summary
    selected_options = event.get("selected_options") or event.get("selectedOptions")
    if isinstance(selected_options, dict):
        parts = []
        for key, value in selected_options.items():
            if value is None:
                continue
            k = str(key or "").strip()
            v = str(value or "").strip()
            if not v:
                continue
            parts.append(f"{k}: {v}" if k else v)
        return ", ".join(parts)
    if isinstance(selected_options, list):
        parts = []
        for item in selected_options:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            parts.append(f"{name}: {value}" if name else value)
        return ", ".join(parts)
    return ""


def build_cart_context_messages(event: Dict) -> Optional[Tuple[str, str]]:
    """Build (human_text, bot_text) pair describing a cart mutation event."""
    if not isinstance(event, dict):
        return None
    action = str(event.get("action") or "").strip().lower()
    title = str(event.get("product_title") or event.get("productTitle") or "this product").strip()
    quantity = int(event.get("quantity") or 1)
    option_summary = _cart_context_option_summary(event)
    option_suffix = f" ({option_summary})" if option_summary else ""
    human_text = ""
    bot_text = ""

    if action == "add":
        human_text = f"Add {title}{option_suffix} to cart"
        bot_text = f"Added {title}{option_suffix} to cart. Quantity: {quantity}."
    elif action == "remove":
        human_text = f"Remove {title}{option_suffix} from cart"
        bot_text = f"Removed {title}{option_suffix} from cart."
    elif action == "qty":
        cart_quantity = event.get("cart_quantity")
        if cart_quantity is None:
            cart_quantity = event.get("cartQuantity")
        qty_direction = str(event.get("qty_direction") or event.get("qtyDirection") or "").strip().lower()
        if qty_direction == "increase":
            human_text = f"Increase quantity for {title}{option_suffix}"
        elif qty_direction == "decrease":
            human_text = f"Decrease quantity for {title}{option_suffix}"
        else:
            human_text = f"Set quantity for {title}{option_suffix} to {quantity}"
        if cart_quantity is not None:
            bot_text = f"Updated {title}{option_suffix} quantity to {cart_quantity}."
        else:
            bot_text = f"Updated {title}{option_suffix} quantity."
    else:
        return None

    return human_text, bot_text


def normalize_cart_item(raw: Dict) -> Dict:
    """Project a widget cart item into the normalized CartItem shape."""
    if not isinstance(raw, dict):
        return {}

    selected_options = raw.get("selected_options") or raw.get("selectedOptions") or {}
    if isinstance(selected_options, list):
        opts = {}
        for o in selected_options:
            if not isinstance(o, dict):
                continue
            name = str(o.get("name") or "").strip()
            value = str(o.get("value") or "").strip()
            if name and value:
                opts[name] = value
        selected_options = opts
    elif not isinstance(selected_options, dict):
        selected_options = {}

    def _to_float(val) -> float:
        try:
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _to_int(val, default: int = 1) -> int:
        try:
            return int(val) if val is not None else default
        except (TypeError, ValueError):
            return default

    # Shopify /cart.js returns prices in the smallest currency unit (e.g. paisa for INR, cents for USD).
    unit_price_raw = _to_float(raw.get("unit_price") or raw.get("price"))
    line_total_raw = _to_float(raw.get("line_total") or raw.get("line_price") or raw.get("final_line_price"))

    return {
        "line_id": str(raw.get("line_id") or raw.get("key") or "").strip(),
        "variant_id": str(raw.get("variant_id") or raw.get("variantId") or "").strip(),
        "product_id": str(raw.get("product_id") or raw.get("productId") or "").strip(),
        "product_handle": str(raw.get("product_handle") or raw.get("productHandle") or raw.get("handle") or "").strip(),
        "product_title": str(raw.get("product_title") or raw.get("productTitle") or raw.get("title") or "").strip(),
        "variant_title": str(raw.get("variant_title") or raw.get("variantTitle") or "").strip(),
        "selected_options": selected_options,
        "quantity": _to_int(raw.get("quantity"), 1),
        "unit_price": unit_price_raw / 100,
        "line_total": line_total_raw / 100,
        "image_url": str(raw.get("image_url") or raw.get("image") or "").strip(),
        "product_url": str(raw.get("product_url") or raw.get("url") or "").strip(),
    }


def normalize_cart_snapshot(raw: Dict) -> Dict:
    """Project a widget cart payload into the normalized CartSnapshot shape."""
    if not isinstance(raw, dict):
        raw = {}
    items_raw = raw.get("items") or []
    if not isinstance(items_raw, list):
        items_raw = []
    items = [normalize_cart_item(it) for it in items_raw if isinstance(it, dict)]
    items = [it for it in items if it.get("variant_id") or it.get("product_title")]

    def _to_float(val) -> float:
        try:
            return float(val) if val is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    subtotal_raw = _to_float(raw.get("subtotal") or raw.get("total_price") or raw.get("items_subtotal_price"))
    subtotal = subtotal_raw / 100  # Shopify returns smallest currency unit
    if not subtotal and items:
        subtotal = sum(it.get("line_total") or 0.0 for it in items)

    return {
        "token": str(raw.get("token") or "").strip(),
        "items": items,
        "item_count": int(raw.get("item_count") if raw.get("item_count") is not None else len(items)),
        "subtotal": subtotal,
        "currency": str(raw.get("currency") or "").strip(),
        "updated_at": str(raw.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "last_action": str(raw.get("last_action") or "").strip().lower(),
        "last_action_variant_id": str(raw.get("last_action_variant_id") or "").strip(),
        "last_action_title": str(raw.get("last_action_title") or "").strip(),
    }


def upsert_cart_entity_in_context(state: Dict, snapshot: Dict) -> None:
    """Mirror the cart snapshot into conversation_context as a single 'cart' entity
    plus a 'cart_management' topic. Closes the topic when the cart is empty."""
    from fashion_bot.utils.context_helpers import find_or_create_topic

    ctx = state.get("conversation_context")
    if not isinstance(ctx, dict):
        ctx = {}
        state["conversation_context"] = ctx

    entities = ctx.setdefault("entities", [])
    items = snapshot.get("items") or []
    item_count = snapshot.get("item_count") or len(items)
    titles = [it.get("product_title") for it in items if it.get("product_title")]
    summary_value = (
        f"{item_count} item(s)" + (f": {', '.join(titles[:3])}" if titles else "")
    ) if items else "empty"

    timestamp = datetime.now(timezone.utc).isoformat()
    cart_entity = {
        "entity_type": "cart",
        "entity_id": "cart",
        "entity_value": summary_value,
        "source": "widget",
        "discovered_at": timestamp,
        "summary": {
            "item_count": item_count,
            "subtotal": snapshot.get("subtotal"),
            "currency": snapshot.get("currency"),
        },
        "full_data": snapshot,
    }

    replaced = False
    for i, e in enumerate(entities):
        if e.get("entity_type") == "cart" and (e.get("entity_id") or "cart") == "cart":
            entities[i] = cart_entity
            replaced = True
            break
    if not replaced:
        entities.append(cart_entity)

    topic, _is_new = find_or_create_topic(ctx, "cart_management")
    refs = topic.setdefault("entity_refs", [])
    has_cart_ref = any(r.get("entity_id") == "cart" for r in refs if isinstance(r, dict))
    if not has_cart_ref:
        refs.append({"entity_type": "cart", "entity_id": "cart", "entity_name": "cart"})
    topic["focal_entity_id"] = "cart"
    topic["updated_at"] = timestamp
    if items:
        topic["status"] = "open"
    else:
        topic["status"] = "resolved"

    topics = ctx.setdefault("topics", [])
    if not any(t.get("topic_id") == topic.get("topic_id") for t in topics):
        topics.append(topic)

    if items and ctx.get("active_topic_id") is None:
        ctx["active_topic_id"] = topic.get("topic_id")

    ctx["context_updated_at"] = timestamp


def build_cart_context_section(state: Dict) -> Optional[str]:
    """Format the live cart snapshot into a text section for the LLM context prompt.

    Returns None when the cart is empty or absent.
    """
    cart = state.get("cart") or {}
    if not isinstance(cart, dict):
        return None

    cart_items = cart.get("items") or []
    if not cart_items:
        return None

    cart_lines = []
    for idx, it in enumerate(cart_items[:8], start=1):
        if not isinstance(it, dict):
            continue
        title = it.get("product_title") or "Item"
        opts = it.get("selected_options") or {}
        opts_str = ""
        if isinstance(opts, dict) and opts:
            opts_str = " — " + ", ".join(f"{k}: {v}" for k, v in opts.items() if v)
        qty = it.get("quantity") or 1
        line_total = it.get("line_total")
        vid = it.get("variant_id") or ""
        price_str = ""
        if line_total:
            price_str = f", {line_total:g}" if isinstance(line_total, (int, float)) else f", {line_total}"
        vid_str = f" [variant_id={vid}]" if vid else ""
        cart_lines.append(f"  {idx}. {title}{opts_str}, Qty: {qty}{price_str}{vid_str}")

    header = f"Cart (live): {cart.get('item_count', len(cart_items))} item(s)"
    subtotal = cart.get("subtotal")
    if subtotal:
        cur = cart.get("currency") or ""
        header += f", subtotal {subtotal:g} {cur}".rstrip()

    last_action = cart.get("last_action") or ""
    last_title = cart.get("last_action_title") or ""
    tail = ""
    if last_action and last_title:
        tail = f"\n  Last cart action: {last_action} {last_title}"
    elif last_action:
        tail = f"\n  Last cart action: {last_action}"

    return header + "\n" + "\n".join(cart_lines) + tail
