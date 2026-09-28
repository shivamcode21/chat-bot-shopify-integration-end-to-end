"""
Context extraction helpers for skill nodes.

This module provides clean abstractions for:
- Extracting entities from agent ReAct loop intermediate steps
- Building conversation context for state updates
- Handling focal entity resolution
- Topic management (smart matching, creation)
- Entity management (global storage, topic refs)
- Decorator pattern for automatic context extraction
"""

import logging
import uuid
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple, Callable
from functools import wraps

logger = logging.getLogger("context_helpers")


# ==================== TOPIC MANAGEMENT ====================

def find_or_create_topic(context: dict, topic_type: str) -> Tuple[Dict[str, Any], bool]:
    """
    Find an open topic of the same type, or create a new one.
    
    This implements smart topic matching:
    - If an open topic of the same type exists, reuse it
    - Otherwise, create a new topic
    
    Args:
        context: Conversation context dict
        topic_type: Type of topic (e.g., "product_inquiry", "order_status")
        
    Returns:
        Tuple of (topic_dict, is_new) where is_new is True if topic was created
    """
    if not context:
        context = {}
    topics = context.get("topics") or []
    
    # Search for an open topic of the same type
    for topic in topics:
        if topic.get("topic_type") == topic_type and topic.get("status") == "open":
            return topic, False
    
    # Create new topic
    new_topic = create_topic_dto(topic_type)
    return new_topic, True


def create_topic_dto(topic_type: str, summary: str = "", awaiting: str = None) -> Dict[str, Any]:
    """
    Create a new TopicDTO with initialized fields.
    
    Args:
        topic_type: Type of topic (e.g., "product_inquiry", "order_status")
        summary: Optional initial summary
        awaiting: Optional slot the topic is waiting for user to provide
                  Valid values: None, "order_id", "confirmation", "reason", 
                  "size", "gender", "category", "address", "phone_number"
        
    Returns:
        New TopicDTO dict
    """
    timestamp = datetime.now().isoformat()
    return {
        "topic_id": str(uuid.uuid4())[:8],
        "topic_type": topic_type,
        "status": "open",
        "started_at": timestamp,
        "updated_at": timestamp,
        "summary": summary,
        "awaiting": awaiting,  # Slot-filling state: what info is the agent waiting for
        "entity_refs": [],
        "focal_entity_id": None,
        "related_entity_id": None,
        "related_entity_type": None
    }


def get_other_open_topics(context: dict, exclude_topic_id: str) -> List[Dict[str, Any]]:
    """
    Get other open topics excluding the specified one.
    
    Args:
        context: Conversation context dict (or state dict with conversation_context)
        exclude_topic_id: Topic ID to exclude from results
        
    Returns:
        List of other open topics
    """
    # Handle both direct context and state with conversation_context
    if "conversation_context" in context:
        ctx = context.get("conversation_context") or {}
    else:
        ctx = context or {}
    
    topics = ctx.get("topics") or []
    
    return [
        t for t in topics
        if t.get("topic_id") != exclude_topic_id and t.get("status") == "open"
    ]


def get_active_topic(context: dict) -> Optional[Dict[str, Any]]:
    """
    Get the currently active topic from context.
    
    Args:
        context: Conversation context dict
        
    Returns:
        Active topic dict or None
    """
    if not context:
        return None
    active_id = context.get("active_topic_id")
    if not active_id:
        return None
    
    for topic in context.get("topics") or []:
        if topic.get("topic_id") == active_id:
            return topic
    return None


def set_topic_awaiting(context: dict, awaiting: Optional[str]) -> None:
    """
    Set the 'awaiting' field on the active topic to track slot-filling state.
    
    This replaces individual STATE FLAGS (like waiting_for_order_confirmation,
    waiting_for_return_exchange_order_id, etc.) with a single field on the topic.
    
    Args:
        context: Conversation context dict
        awaiting: What the agent is waiting for from the user.
                  Valid values: None (cleared), "order_id", "confirmation", 
                  "reason", "size", "gender", "category", "address", "phone_number"
    """
    active_topic = get_active_topic(context)
    if active_topic:
        active_topic["awaiting"] = awaiting
        active_topic["updated_at"] = datetime.now().isoformat()
        logger.debug(f"📋 Topic {active_topic['topic_id']} awaiting: {awaiting}")


def clear_topic_awaiting(context: dict) -> None:
    """
    Clear the 'awaiting' field on the active topic (set to None).
    
    Call this when the user has provided the information the agent was waiting for.
    
    Args:
        context: Conversation context dict
    """
    set_topic_awaiting(context, None)


# ==================== ENTITY MANAGEMENT ====================

def add_entities_to_global(context: dict, entities: List[Dict[str, Any]]) -> None:
    """
    Add full entity details to global entities list (deduped by entity_id).
    
    Args:
        context: Conversation context dict
        entities: List of EntityDTO dicts to add
    """
    global_entities = context.setdefault("entities", [])
    existing_ids = {e.get("entity_id") for e in global_entities if e.get("entity_id")}
    
    for entity in entities:
        entity_id = entity.get("entity_id")
        if entity_id and entity_id not in existing_ids:
            global_entities.append(entity)
            existing_ids.add(entity_id)
        elif entity_id and entity_id in existing_ids:
            # Update existing entity with new data
            for i, existing in enumerate(global_entities):
                if existing.get("entity_id") == entity_id:
                    # Merge summary and full_data if provided
                    if entity.get("summary"):
                        existing["summary"] = entity["summary"]
                    if entity.get("full_data"):
                        existing["full_data"] = entity["full_data"]
                    existing["discovered_at"] = entity.get("discovered_at", existing.get("discovered_at"))
                    break


def add_entity_refs_to_topic(topic: Dict[str, Any], entities: List[Dict[str, Any]]) -> None:
    """
    Add lightweight entity references to topic (deduped by ID).
    
    Args:
        topic: TopicDTO dict
        entities: List of EntityDTO dicts to create refs from
    """
    entity_refs = topic.setdefault("entity_refs", [])
    existing_ids = {ref.get("entity_id") for ref in entity_refs if ref.get("entity_id")}
    
    for entity in entities:
        entity_id = entity.get("entity_id")
        if entity_id and entity_id not in existing_ids:
            entity_refs.append({
                "entity_type": entity.get("entity_type"),
                "entity_id": entity_id,
                "entity_name": entity.get("entity_value")
            })
            existing_ids.add(entity_id)  # Track added ID to prevent duplicates in same batch


def merge_llm_entity_refs_to_topic(topic: Dict[str, Any], llm_entities: List[str], global_entities: List[Dict] = None) -> None:
    """
    Merge LLM-mentioned entities to topic refs.
    
    LLM provides entities in format: ["Product: Sunfire Denim", "Order: GV1234"]
    Parse these and add to topic refs, enriching with IDs from global entities if available.
    
    Args:
        topic: TopicDTO dict
        llm_entities: List of entity strings from LLM
        global_entities: Optional global entities list for ID enrichment
    """
    entity_refs = topic.setdefault("entity_refs", [])
    # Track both IDs and names to prevent duplicates
    existing_ids = {ref.get("entity_id") for ref in entity_refs if ref.get("entity_id")}
    existing_names = {ref.get("entity_name", "").lower() for ref in entity_refs}
    
    for entity_str in llm_entities:
        if not entity_str or ":" not in entity_str:
            continue
        
        parts = entity_str.split(":", 1)
        if len(parts) != 2:
            continue
        
        entity_type = parts[0].strip().lower()
        entity_name = parts[1].strip()
        
        # Skip if name already exists
        if entity_name.lower() in existing_names:
            continue
        
        # Try to find entity_id from global entities
        entity_id = None
        if global_entities:
            for ge in global_entities:
                if (ge.get("entity_type") == entity_type and 
                    ge.get("entity_value", "").lower() == entity_name.lower()):
                    entity_id = ge.get("entity_id")
                    break
        
        # Skip if ID already exists
        if entity_id and entity_id in existing_ids:
            continue
        
        entity_refs.append({
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name
        })
        existing_names.add(entity_name.lower())


def get_entity_full_data(context: dict, entity_id: str) -> Optional[Dict[str, Any]]:
    """
    Get full entity data for passing to skill node tools.
    
    Args:
        context: Conversation context dict
        entity_id: Entity ID to look up
        
    Returns:
        Full data dict or None
    """
    if not context:
        return None
    for entity in context.get("entities") or []:
        if entity.get("entity_id") == entity_id:
            return entity.get("full_data", {})
    return None


def get_entity_by_id(context: dict, entity_id: str) -> Optional[Dict[str, Any]]:
    """
    Get entity by ID from global entities.
    
    Args:
        context: Conversation context dict
        entity_id: Entity ID to look up
        
    Returns:
        Entity dict or None
    """
    if not context:
        return None
    for entity in context.get("entities") or []:
        if entity.get("entity_id") == entity_id:
            return entity
    return None


def get_entity_summary_string(global_entities: List[Dict], entity_id: str, entity_type: str) -> str:
    """
    Get formatted summary string from global entity.
    
    Args:
        global_entities: List of global entities
        entity_id: Entity ID to look up
        entity_type: Entity type for formatting
        
    Returns:
        Formatted summary string or empty string
    """
    for entity in global_entities:
        if entity.get("entity_id") == entity_id:
            summary = entity.get("summary", {})
            if entity_type == "product":
                parts = []
                if summary.get("price"):
                    parts.append(f"Price: {summary['price']}")
                if summary.get("sizes_available"):
                    sizes = summary['sizes_available']
                    if isinstance(sizes, list):
                        parts.append(f"Sizes: {', '.join(str(s) for s in sizes)}")
                if summary.get("in_stock") is not None:
                    parts.append(f"In Stock: {'Yes' if summary['in_stock'] else 'No'}")
                return " | ".join(parts)
            elif entity_type == "order":
                parts = []
                if summary.get("delivery_status"):
                    parts.append(f"Status: {summary['delivery_status']}")
                if summary.get("fulfillment_status"):
                    parts.append(f"Fulfillment: {summary['fulfillment_status']}")
                if summary.get("payment_status"):
                    parts.append(f"Payment: {summary['payment_status']}")
                if summary.get("expected_delivery"):
                    parts.append(f"ETA: {summary['expected_delivery']}")
                if summary.get("total_price"):
                    parts.append(f"Total: {summary['total_price']}")
                # Add cancellation/update eligibility - CRITICAL for skill nodes
                if summary.get("cancelled_at"):
                    parts.append("⚠️ ALREADY CANCELLED")
                elif summary.get("can_be_cancelled") is not None:
                    if summary.get("can_be_cancelled"):
                        if summary.get("can_be_updated"):
                            parts.append("✅ Can be cancelled/updated")
                        else:
                            parts.append("✅ Can be cancelled (already shipped, cannot update)")
                    else:
                        parts.append("❌ Cannot be cancelled")
                return " | ".join(parts)
    return ""


def _product_entity_has_detail_gaps(global_entities: List[Dict], entity_id: str) -> bool:
    """Check if a product entity is missing detail fields that require a tool fetch."""
    for entity in global_entities:
        if entity.get("entity_id") == entity_id:
            fd = entity.get("full_data") or {}
            sg = fd.get("size_guide")
            has_size_guide = isinstance(sg, dict) and sg.get("has_size_guide") is True and sg.get("content")
            has_fabric = bool(fd.get("fabric") or fd.get("material"))
            has_metafields = bool(fd.get("all_metafields"))
            has_care = bool(fd.get("care_instructions"))
            if not has_size_guide or not has_fabric or not has_metafields or not has_care:
                return True
            return False
    return True


def _get_product_handle_from_entity(global_entities: List[Dict], entity_id: str) -> Optional[str]:
    """Extract the product handle from a global entity."""
    for entity in global_entities:
        if entity.get("entity_id") == entity_id:
            fd = entity.get("full_data") or {}
            return fd.get("handle") or entity.get("entity_name", "").lower().replace(" ", "-")
    return None


def get_full_entity_data_for_prompt(global_entities: List[Dict], entity_id: str, entity_type: str) -> Optional[str]:
    """
    Get full entity data formatted for inclusion in prompt context.
    
    This returns the complete entity data (from full_data) so the LLM 
    doesn't need to call tools like get_product_context() for follow-up questions.
    
    Args:
        global_entities: List of global entities
        entity_id: Entity ID to look up
        entity_type: Entity type for formatting
        
    Returns:
        JSON-formatted string of full entity data, or None if not found
    """
    import json

    for entity in global_entities:
        ge_id = entity.get("entity_id")
        if ge_id == entity_id:
            full_data = entity.get("full_data")
            if full_data:
                # For products, include ALL fields from full_data
                if entity_type == "product":
                    # Start with all full_data fields
                    product_context = dict(full_data)
                    
                    # Normalize key fields for consistency
                    # Ensure sizes_in_stock is present with correct data
                    if "sizes_in_stock" not in product_context:
                        product_context["sizes_in_stock"] = (
                            full_data.get("available_sizes") or 
                            full_data.get("sizes") or 
                            []
                        )
                    
                    # Ensure product_link is present
                    if "product_link" not in product_context:
                        product_context["product_link"] = (
                            full_data.get("url") or 
                            full_data.get("link")
                        )
                    
                    # Replace bulky size_guide/size_chart content with a lightweight flag.
                    # The full content is still available via find_product_by_id tool call.
                    for sg_key in ("size_guide", "size_chart"):
                        sg = product_context.get(sg_key)
                        if isinstance(sg, dict) and (sg.get("content") or sg.get("content_html") or sg.get("raw")):
                            product_context[sg_key] = {
                                "has_size_guide": True,
                                "note": "Full size chart available — call find_product_by_id to retrieve it",
                            }
                        elif isinstance(sg, str) and len(sg) > 200:
                            product_context[sg_key] = {
                                "has_size_guide": True,
                                "note": "Full size chart available — call find_product_by_id to retrieve it",
                            }

                    # Remove None values and empty lists to keep prompt clean
                    product_context = {k: v for k, v in product_context.items() if v is not None and v != [] and v != ""}
                    return json.dumps(product_context, indent=2, ensure_ascii=False)
                
                # For orders, return relevant order data including tracking/escalation info
                elif entity_type == "order":
                    order_context = {
                        "order_id": full_data.get("order_id") or full_data.get("name"),
                        "status": full_data.get("status") or full_data.get("shipment_status"),
                        "partner_status": full_data.get("partner_status"),
                        "fulfillment_status": full_data.get("fulfillment_status"),
                        "payment_status": full_data.get("financial_status"),
                        "total": full_data.get("total_price") or full_data.get("total"),
                        "items": full_data.get("items") or full_data.get("line_items"),
                        "shipping_address": full_data.get("shipping_address"),
                        "can_be_cancelled": full_data.get("can_be_cancelled"),
                        "can_be_updated": full_data.get("can_be_updated"),
                        # Tracking/delivery details for follow-up questions
                        "awb": full_data.get("awb"),
                        "courier": full_data.get("courier"),
                        "expected_delivery": full_data.get("expected_delivery"),
                        "current_location": full_data.get("current_location"),
                        "tracking_url": full_data.get("tracking_url"),
                        "days_since_shipped": full_data.get("days_since_shipped"),
                        "order_date": full_data.get("order_date") or full_data.get("created_at"),
                        # Escalation info (if already computed)
                        "escalation": full_data.get("escalation"),
                    }
                    order_context = {k: v for k, v in order_context.items() if v is not None}
                    return json.dumps(order_context, indent=2, ensure_ascii=False)
    
    return None


def build_entity_summary(entity_type: str, full_data: dict) -> Dict[str, Any]:
    """
    Extract key highlights from full data based on entity type.
    
    Creates a summary dict with essential fields for quick reference in prompts.
    
    Args:
        entity_type: Type of entity ("product", "order", etc.)
        full_data: Complete data dict from tool/API
        
    Returns:
        Summary dict with key highlights
    """
    if entity_type == "product":
        # Normalized product dicts use the keys ``in_stock`` / ``sizes_in_stock``
        # / ``all_size_variants`` (see tool_factory._normalize_product). Read
        # those first and fall back to the legacy ``available`` / ``sizes`` keys.
        # Reading ``available`` first defaulted every product to in-stock and
        # lost size info, because the normalizer pops those legacy keys.
        in_stock = full_data.get("in_stock")
        if in_stock is None:
            in_stock = full_data.get("available", True)
        summary = {
            "price": full_data.get("price"),
            "sizes_available": (
                full_data.get("sizes_in_stock")
                or full_data.get("available_sizes")
                or full_data.get("sizes")
            ),
            "in_stock": in_stock,
            "product_link": full_data.get("url") or full_data.get("product_link")
        }
        # rating/rating_count (tool_factory._normalize_product) -- omitted
        # entirely when the product has no reviews, same as at the source, so
        # a follow-up question about an already-discussed product ("what's
        # its rating?") has the same data available as a fresh tool call
        # rather than always deflecting to "check the product page".
        if full_data.get("rating") is not None and full_data.get("rating_count") is not None:
            summary["rating"] = full_data["rating"]
            summary["rating_count"] = full_data["rating_count"]
        return summary
    
    elif entity_type == "order":
        # Determine payment status
        financial_status = full_data.get("financial_status", "")
        if financial_status == "paid":
            payment_status = "Paid"
        elif full_data.get("payment_method") == "COD" or "cod" in str(financial_status).lower():
            payment_status = "COD"
        else:
            payment_status = "Pending"
        
        # Get fulfillment and cancellation status
        fulfillment_status = full_data.get("fulfillment_status") or full_data.get("fulfillment_status_label", "unfulfilled")
        cancelled_at = full_data.get("cancelled_at")
        
        # Determine delivery status
        delivery_status = full_data.get("shipment_status") or full_data.get("status") or ""
        
        # Compute if order can be cancelled/updated
        # Non-cancellable states: delivered, cancelled, RTO
        non_cancellable_statuses = ["delivered", "cancelled", "rto", "rto delivered", "returned"]
        is_cancelled = cancelled_at is not None
        is_non_cancellable_status = delivery_status.lower() in non_cancellable_statuses if delivery_status else False
        is_fulfilled = fulfillment_status and fulfillment_status.lower() in ["fulfilled", "shipped"]
        
        can_be_cancelled = not is_cancelled and not is_non_cancellable_status
        can_be_updated = can_be_cancelled and not is_fulfilled  # Can only update before shipment
        
        return {
            "total_price": full_data.get("total_price"),
            "products": full_data.get("products") or [
                item.get("name") or item.get("title") 
                for item in full_data.get("line_items", [])
            ],
            "delivery_status": delivery_status,
            "fulfillment_status": fulfillment_status,
            "payment_status": payment_status,
            "expected_delivery": full_data.get("etd_date") or full_data.get("delivery_date"),
            "tracking_url": full_data.get("tracking_url"),
            "cancelled_at": cancelled_at,
            "can_be_cancelled": can_be_cancelled,
            "can_be_updated": can_be_updated
        }
    
    elif entity_type == "category":
        return {
            "name": full_data.get("name") or full_data.get("title"),
            "url": full_data.get("url")
        }
    
    elif entity_type == "discount_code":
        return {
            "code": full_data.get("code"),
            "value": full_data.get("value") or full_data.get("discount_value"),
            "type": full_data.get("type") or full_data.get("discount_type")
        }
    
    return {}


def determine_focal_entity_from_topic(
    topic: Dict[str, Any], 
    global_entities: List[Dict] = None,
    llm_entities_used: List[str] = None
) -> Optional[str]:
    """
    Determine the focal entity ID for a topic based on LLM hint or entity refs.
    
    Priority:
    1. LLM's entities_used[0] — the LLM naturally lists the primary/focused entity first
       (e.g., "Product: No Quit Varsity" when the user is ordering that product).
    2. Fallback to entity_refs[-1] (most recently added ref).
    
    Args:
        topic: TopicDTO dict
        global_entities: Optional global entities for validation
        llm_entities_used: Optional list of entity strings from LLM summaryupdate
            (format: ["Product: No Quit Varsity", "Product: Seventh Quarter"])
        
    Returns:
        Entity ID of focal entity or None
    """
    entity_refs = topic.get("entity_refs", [])
    if not entity_refs:
        return None
    
    # Priority 1: Use LLM's first entity as focal hint
    if llm_entities_used:
        for entity_str in llm_entities_used:
            if not entity_str or ":" not in entity_str:
                continue
            parts = entity_str.split(":", 1)
            if len(parts) != 2:
                continue
            entity_name = parts[1].strip().lower()
            
            # Match against entity_refs by name
            for ref in entity_refs:
                ref_name = (ref.get("entity_name") or "").lower()
                if ref_name and ref_name == entity_name and ref.get("entity_id"):
                    return ref.get("entity_id")
            
            # Match against global_entities by name
            if global_entities:
                for ge in global_entities:
                    ge_value = (ge.get("entity_value") or "").lower()
                    if ge_value == entity_name and ge.get("entity_id"):
                        return ge.get("entity_id")
            
            # Only try the first valid entity string as the focal hint
            break
    
    # Priority 2: Fallback to last entity ref (most recent)
    last_ref = entity_refs[-1]
    return last_ref.get("entity_id")


def get_focal_entity_from_global(context: dict, focal_entity_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Get focal entity details from global entities.
    
    Args:
        context: Conversation context dict
        focal_entity_id: Entity ID of focal entity
        
    Returns:
        FocalEntityDTO-like dict or None
    """
    if not focal_entity_id:
        return None
    
    entity = get_entity_by_id(context, focal_entity_id)
    if not entity:
        return None
    
    return {
        "entity_type": entity.get("entity_type"),
        "entity_id": entity.get("entity_id"),
        "entity_value": entity.get("entity_value"),
        "confidence": "inferred",
        "set_at": datetime.now().isoformat()
    }


# ==================== SELECTABLE ENTITY MANAGEMENT ====================
# Used for product/order selection scenarios where multiple options are shown

def add_selectable_entities(
    context: dict, 
    items: List[Dict[str, Any]], 
    entity_type: str = "product",
    source: str = "search"
) -> List[Dict[str, Any]]:
    """
    Add selectable entities to context for numbered selection scenarios.
    
    Creates 'selectable_product' or 'selectable_order' entities with selection_index.
    User can say "1", "2", etc. to select, and we promote that entity to focal_entity.
    
    Args:
        context: Conversation context dict (will be mutated)
        items: List of product/order dicts to make selectable
        entity_type: Base type ("product", "order") - will be prefixed with "selectable_"
        source: Where items came from ("search", "recommendations", "recent_orders")
        
    Returns:
        List of created entity dicts (for logging/reference)
    """
    entities = context.setdefault("entities", [])
    created_entities = []
    
    # Clear existing selectable entities of same type
    selectable_type = f"selectable_{entity_type}"
    entities[:] = [e for e in entities if e.get("entity_type") != selectable_type]
    
    for idx, item in enumerate(items, start=1):
        # Get entity ID based on type
        if entity_type == "product":
            entity_id = item.get("handle") or item.get("id") or item.get("product_id")
            entity_value = item.get("title") or item.get("name") or "Unknown Product"
        elif entity_type == "order":
            # Prefer order name (e.g., #gv14007) > order_name > order_id > id
            # Shopify returns 'name' as the customer-facing order identifier
            entity_id = item.get("name") or item.get("order_name") or item.get("order_id") or item.get("id")
            entity_value = entity_id or "Unknown Order"
        else:
            entity_id = item.get("id") or str(idx)
            entity_value = item.get("name") or item.get("title") or f"Item {idx}"
        
        # Build summary based on type
        summary = build_entity_summary(entity_type, item)
        
        entity = {
            "entity_type": selectable_type,
            "entity_id": entity_id,
            "entity_value": entity_value,
            "source": source,
            "discovered_at": datetime.now().isoformat(),
            "selection_index": idx,  # Key field for selection!
            "summary": summary,
            "full_data": item
        }
        
        entities.append(entity)
        created_entities.append(entity)
    
    return created_entities


def get_selectable_entities(context: dict, entity_type: str = "product") -> List[Dict[str, Any]]:
    """
    Get all selectable entities of a given type.
    
    Args:
        context: Conversation context dict
        entity_type: Base type ("product", "order")
        
    Returns:
        List of selectable entity dicts, sorted by selection_index
    """
    entities = context.get("entities") or []
    selectable_type = f"selectable_{entity_type}"
    
    selectable = [e for e in entities if e.get("entity_type") == selectable_type]
    return sorted(selectable, key=lambda e: e.get("selection_index", 999))


def select_entity_by_index(
    context: dict, 
    selection_index: int, 
    entity_type: str = "product"
) -> Optional[Dict[str, Any]]:
    """
    Select an entity by its selection_index and promote to focal_entity.
    
    This:
    1. Finds the selectable entity with matching index
    2. Changes its entity_type from 'selectable_X' to 'X' (confirmed)
    3. Sets it as focal_entity
    4. Clears other selectable entities of same type
    
    Args:
        context: Conversation context dict (will be mutated)
        selection_index: 1-based index (user said "1", "2", etc.)
        entity_type: Base type ("product", "order")
        
    Returns:
        Selected entity dict, or None if not found
    """
    entities = context.setdefault("entities", [])
    selectable_type = f"selectable_{entity_type}"
    
    # Find the selected entity
    selected = None
    for entity in entities:
        if (entity.get("entity_type") == selectable_type and 
            entity.get("selection_index") == selection_index):
            selected = entity
            break
    
    if not selected:
        return None
    
    # Promote: change type from selectable_product → product
    selected["entity_type"] = entity_type
    selected.pop("selection_index", None)  # Remove selection_index
    
    # Clear other selectable entities of same type
    entities[:] = [
        e for e in entities 
        if e.get("entity_type") != selectable_type
    ]
    
    # Set as focal_entity
    context["focal_entity"] = {
        "entity_type": entity_type,
        "entity_id": selected.get("entity_id"),
        "entity_value": selected.get("entity_value"),
        "confidence": "explicit",  # User explicitly selected
        "set_at": datetime.now().isoformat()
    }
    
    return selected


def clear_selectable_entities(context: dict, entity_type: str = "product") -> int:
    """
    Clear all selectable entities of a given type.
    
    Use when:
    - User changes topic
    - Selection times out
    - New search replaces old results
    
    Args:
        context: Conversation context dict (will be mutated)
        entity_type: Base type ("product", "order")
        
    Returns:
        Number of entities cleared
    """
    entities = context.get("entities") or []
    selectable_type = f"selectable_{entity_type}"
    
    original_count = len(entities)
    context["entities"] = [
        e for e in entities 
        if e.get("entity_type") != selectable_type
    ]
    
    return original_count - len(context["entities"])


def has_pending_selection(context: dict, entity_type: str = "product") -> bool:
    """
    Check if there are pending selectable entities.
    
    Args:
        context: Conversation context dict
        entity_type: Base type ("product", "order")
        
    Returns:
        True if there are selectable entities awaiting selection
    """
    entities = context.get("entities") or []
    selectable_type = f"selectable_{entity_type}"
    
    return any(e.get("entity_type") == selectable_type for e in entities)


def get_selection_count(context: dict, entity_type: str = "product") -> int:
    """
    Get count of pending selectable entities.
    
    Args:
        context: Conversation context dict
        entity_type: Base type ("product", "order")
        
    Returns:
        Number of selectable entities
    """
    entities = context.get("entities") or []
    selectable_type = f"selectable_{entity_type}"
    
    return sum(1 for e in entities if e.get("entity_type") == selectable_type)


# ==================== ACTION TRACKING ====================
# Actions are state-changing operations (place order, cancel, update size)
# Tracking them prevents the agent from repeating operations unnecessarily

# Tools that are considered "actions" (state-changing operations)
ACTION_TOOLS = {
    # Order actions
    "place_order": "Order Placed",
    "create_order": "Order Created",
    "create_cart_order": "Order Created",
    "cancel_order": "Order Cancelled",
    "request_cancellation": "Cancellation Requested",
    
    # Order modifications
    "update_order_size": "Size Updated",
    "update_order_address": "Address Updated",
    "modify_order": "Order Modified",
    
    # Return/exchange actions
    "initiate_return": "Return Initiated",
    "initiate_exchange": "Exchange Initiated",
    "create_return_request": "Return Request Created",
    "schedule_pickup": "Pickup Scheduled",
    
    # Payment actions
    "process_refund": "Refund Processed",
    "apply_discount": "Discount Applied",
    
    # Customer actions
    "schedule_callback": "Callback Scheduled",
    "escalate_to_human": "Escalated to Human",
}


def is_action_tool(tool_name: str) -> bool:
    """
    Check if a tool is an action (state-changing operation).
    
    Args:
        tool_name: Name of the tool
        
    Returns:
        True if the tool is an action
    """
    tool_lower = tool_name.lower()
    return any(action_key in tool_lower for action_key in ACTION_TOOLS.keys())


def get_action_type(tool_name: str) -> Optional[str]:
    """
    Get the action type for a tool.
    
    Args:
        tool_name: Name of the tool
        
    Returns:
        Action type string or None
    """
    tool_lower = tool_name.lower()
    for action_key in ACTION_TOOLS.keys():
        if action_key in tool_lower:
            return action_key
    return None


def get_action_name(tool_name: str) -> str:
    """
    Get the human-readable action name for a tool.
    
    Args:
        tool_name: Name of the tool
        
    Returns:
        Human-readable action name
    """
    tool_lower = tool_name.lower()
    for action_key, action_name in ACTION_TOOLS.items():
        if action_key in tool_lower:
            return action_name
    return tool_name.replace("_", " ").title()


def create_action_dto(
    tool_name: str,
    parameters: Dict[str, Any],
    success: bool,
    result_summary: str,
    topic_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an ActionDTO from a tool call result.
    
    Args:
        tool_name: Name of the tool that was called
        parameters: Parameters passed to the tool
        success: Whether the action succeeded
        result_summary: Human-readable summary of the result
        topic_id: Optional topic ID where action was performed
        
    Returns:
        ActionDTO dict
    """
    return {
        "action_type": get_action_type(tool_name) or tool_name,
        "action_name": get_action_name(tool_name),
        "performed_at": datetime.now().isoformat(),
        "parameters": parameters,
        "success": success,
        "result_summary": result_summary,
        "topic_id": topic_id
    }


def add_action_to_context(context: dict, action: Dict[str, Any], max_actions: int = 5) -> None:
    """
    Add an action to the recent_actions list in context.
    
    Maintains only the last N actions.
    
    Args:
        context: Conversation context dict
        action: ActionDTO dict to add
        max_actions: Maximum number of actions to keep (default 5)
    """
    recent_actions = context.setdefault("recent_actions", [])
    recent_actions.append(action)
    
    # Keep only last N actions
    if len(recent_actions) > max_actions:
        context["recent_actions"] = recent_actions[-max_actions:]


def extract_action_parameters(tool_name: str, tool_input: Any, obs_data: dict) -> Dict[str, Any]:
    """
    Extract relevant parameters from a tool call for action tracking.
    
    Args:
        tool_name: Name of the tool
        tool_input: Input passed to the tool
        obs_data: Observation/result data from the tool
        
    Returns:
        Dict of relevant parameters
    """
    params = {}
    tool_lower = tool_name.lower()
    
    # Parse tool input if it's a dict
    if isinstance(tool_input, dict):
        # Common parameters
        if tool_input.get("order_id"):
            params["order_id"] = tool_input["order_id"]
        if tool_input.get("product_link") or tool_input.get("product_url"):
            params["product"] = tool_input.get("product_link") or tool_input.get("product_url")
        if tool_input.get("size") or tool_input.get("new_size"):
            params["size"] = tool_input.get("size") or tool_input.get("new_size")
        if tool_input.get("quantity"):
            params["quantity"] = tool_input["quantity"]
        if tool_input.get("reason"):
            params["reason"] = tool_input["reason"]
    
    # Extract from observation data
    if obs_data:
        # Order ID from result
        if obs_data.get("order_id") and "order_id" not in params:
            params["order_id"] = obs_data["order_id"]
        if obs_data.get("channel_order_id") and "order_id" not in params:
            params["order_id"] = obs_data["channel_order_id"]
        
        # Product info
        if obs_data.get("product_name") or obs_data.get("product"):
            product_info = obs_data.get("product") or {}
            params["product_name"] = obs_data.get("product_name") or product_info.get("title") or product_info.get("name")
    
    return params


def extract_action_result_summary(tool_name: str, obs_data: dict, success: bool) -> str:
    """
    Extract a human-readable result summary from tool observation.
    
    Args:
        tool_name: Name of the tool
        obs_data: Observation/result data
        success: Whether the action succeeded
        
    Returns:
        Human-readable summary string
    """
    if not success:
        error_msg = obs_data.get("error") or obs_data.get("message") or "Operation failed"
        return f"Failed: {error_msg}"
    
    tool_lower = tool_name.lower()
    
    # Order placement
    if "place_order" in tool_lower or "create_order" in tool_lower or "cart_order" in tool_lower:
        order_id = obs_data.get("order_id") or obs_data.get("channel_order_id")
        if order_id:
            return f"Order {order_id} placed successfully"
        return "Order placed successfully"
    
    # Cancellation
    if "cancel" in tool_lower:
        order_id = obs_data.get("order_id") or obs_data.get("channel_order_id")
        if order_id:
            return f"Order {order_id} cancellation requested"
        return "Cancellation requested"
    
    # Size update
    if "size" in tool_lower and "update" in tool_lower:
        order_id = obs_data.get("order_id")
        new_size = obs_data.get("new_size") or obs_data.get("size")
        if order_id and new_size:
            return f"Order {order_id} size updated to {new_size}"
        return "Size updated successfully"
    
    # Return/exchange
    if "return" in tool_lower or "exchange" in tool_lower:
        order_id = obs_data.get("order_id")
        if order_id:
            return f"Return/exchange initiated for order {order_id}"
        return "Return/exchange initiated"
    
    # Generic success
    message = obs_data.get("message") or obs_data.get("status")
    if message:
        return str(message)[:100]
    
    return f"{get_action_name(tool_name)} completed"


def format_recent_actions_for_prompt(context: dict) -> str:
    """
    Format recent actions for inclusion in the LLM prompt.
    
    Args:
        context: Conversation context dict
        
    Returns:
        Formatted string showing recent actions
    """
    recent_actions = context.get("recent_actions") or []
    if not recent_actions:
        return ""
    
    lines = ["=== RECENT ACTIONS (do NOT repeat these) ==="]
    
    for action in recent_actions[-5:]:
        action_name = action.get("action_name", "Unknown")
        params = action.get("parameters", {})
        success = action.get("success", True)
        result = action.get("result_summary", "")
        
        status_icon = "✅" if success else "❌"
        
        # Format parameters
        param_parts = []
        if params.get("order_id"):
            param_parts.append(f"Order: {params['order_id']}")
        if params.get("product_name"):
            param_parts.append(f"Product: {params['product_name']}")
        if params.get("size"):
            param_parts.append(f"Size: {params['size']}")
        
        param_str = f" ({', '.join(param_parts)})" if param_parts else ""
        
        lines.append(f"{status_icon} {action_name}{param_str}: {result}")
    
    lines.append("")  # Empty line after actions
    return "\n".join(lines)


# ==================== CONTEXT BUILDER FOR SKILL NODES ====================

def build_context_for_skill_node(state: dict, current_topic: Dict[str, Any]) -> str:
    """
    Build context string showing current topic with entity summaries.
    
    This provides structured context to the LLM with:
    - Current topic (type, status, summary)
    - PAGE CONTEXT (web chat only - what product/page user is viewing)
    - Entities in this topic with key highlights
    - Other open topics with their summaries
    
    Args:
        state: Current SupportState
        current_topic: Current TopicDTO dict
        
    Returns:
        Formatted context string for injection into prompts
    """
    context = state.get("conversation_context") or {}
    global_entities = context.get("entities", []) or []
    sections = []
    
    # ==================== WEB CHAT PAGE CONTEXT ====================
    # CRITICAL: For web chat, include what page/product the user is currently viewing
    # This enables context-aware responses like "what is this product?" without asking for URL
    page_context = state.get("page_context")
    current_page_type = state.get("current_page_type")
    current_product_handle = state.get("current_product_handle")
    current_product_title = state.get("current_product_title")
    current_page_url = state.get("current_page_url")
    inquiry_product_info = state.get("inquiry_product_info")
    
    # Check if user is on a product page (web chat only)
    is_web_chat_product_page = (
        current_page_type == "product" and 
        current_product_handle and 
        current_product_handle.strip()
    )
    
    if is_web_chat_product_page:
        sections.append("=== 🌐 WEB CHAT - CURRENT PAGE CONTEXT ===")
        sections.append(f"⚠️ USER IS CURRENTLY VIEWING A PRODUCT PAGE!")
        sections.append(f"📦 Product: {current_product_title or current_product_handle}")
        sections.append(f"🔗 Handle: {current_product_handle}")
        if current_page_url:
            sections.append(f"🔗 URL: {current_page_url}")
        sections.append("")
        
        # If product info is already pre-fetched, include it directly so LLM doesn't need to call tool
        if inquiry_product_info and isinstance(inquiry_product_info, dict):
            _has_sg = bool(inquiry_product_info.get("size_chart") or inquiry_product_info.get("size_guide"))
            _has_fb = bool(inquiry_product_info.get("fabric") or inquiry_product_info.get("material"))
            if _has_sg and _has_fb:
                sections.append("📦 PRE-FETCHED PRODUCT DATA (use this directly for follow-up questions):")
            else:
                sections.append("📦 PRE-FETCHED PRODUCT DATA (use for price, sizes, availability — but fabric/size_chart/care may be incomplete; call find_product_by_id if user asks about those):")
            # The "use this directly, no need to call a tool" licence above is
            # what makes this block worth injecting, but it has to stop short
            # of reviews. Nothing here carries review TEXT -- only the
            # aggregate rating/rating_count -- while a customer accepting an
            # offer ("yes, show me the reviews") reads as exactly the kind of
            # follow-up this block says to answer directly. Observed live: the
            # model skipped get_product_reviews on that turn and reprinted the
            # previous batch from the transcript, which repeats reviews the
            # customer has already seen and puts words in a named customer's
            # mouth if it paraphrases. Saying where the licence ends costs one
            # line and is the only part of this block that concerns reviews.
            sections.append("   ⚠️ REVIEWS ARE NOT PRE-FETCHED: this block has no review text. Any reviewer name or review body requires a get_product_reviews call THIS turn — never reuse reviews from earlier in the conversation.")
            
            # Include key product details (supports both raw and normalized field names)
            if inquiry_product_info.get("name") or inquiry_product_info.get("title"):
                sections.append(f"   Name: {inquiry_product_info.get('name') or inquiry_product_info.get('title')}")
            raw_price = inquiry_product_info.get("price")
            if raw_price:
                if isinstance(raw_price, dict):
                    p_min = raw_price.get("min")
                    p_max = raw_price.get("max")
                    if p_min is not None and p_max is not None and p_min != p_max:
                        sections.append(f"   Price: Rs. {p_min} - Rs. {p_max}")
                    elif p_min is not None:
                        sections.append(f"   Price: Rs. {p_min}")
                else:
                    sections.append(f"   Price: {raw_price}")
            if inquiry_product_info.get("compare_at_price"):
                sections.append(f"   Compare Price: {inquiry_product_info.get('compare_at_price')}")
            if inquiry_product_info.get("description"):
                desc = inquiry_product_info.get("description", "")[:500]
                sections.append(f"   Description: {desc}")
            sizes = (
                inquiry_product_info.get("sizes_in_stock")
                or inquiry_product_info.get("sizes")
                or inquiry_product_info.get("available_sizes")
                or []
            )
            all_sizes = (
                inquiry_product_info.get("all_size_variants")
                or inquiry_product_info.get("total_sizes")
                or []
            )
            variants = inquiry_product_info.get("variants") or []
            if sizes:
                sections.append(f"   Available Sizes: {', '.join(str(s) for s in sizes)}")
                if all_sizes and len(all_sizes) > len(sizes):
                    oos = [str(s) for s in all_sizes if s not in sizes]
                    if oos:
                        sections.append(f"   Out of Stock Sizes: {', '.join(oos)}")
            elif variants:
                variant_sizes = [v.get("size") or v.get("title") for v in variants if isinstance(v, dict)]
                variant_sizes = [s for s in variant_sizes if s]
                if variant_sizes:
                    sections.append(f"   Available Sizes: {', '.join(str(s) for s in variant_sizes[:10])}")
            if inquiry_product_info.get("stock_message"):
                sections.append(f"   Stock: {inquiry_product_info.get('stock_message')}")
            if inquiry_product_info.get("product_link") or inquiry_product_info.get("url"):
                sections.append(f"   Product Link: {inquiry_product_info.get('product_link') or inquiry_product_info.get('url')}")
            sg = inquiry_product_info.get("size_guide") or inquiry_product_info.get("size_chart")
            if sg:
                if isinstance(sg, dict) and sg.get("has_size_guide"):
                    sections.append(f"   Size Chart: Available")
                elif not isinstance(sg, dict):
                    sections.append(f"   Size Chart: Available")
            if inquiry_product_info.get("fabric") or inquiry_product_info.get("material"):
                sections.append(f"   Material: {inquiry_product_info.get('fabric') or inquiry_product_info.get('material')}")
            
            sections.append("")
        else:
            # Product info not pre-fetched, instruct LLM to call tool
            sections.append("💡 IMPORTANT: When user asks 'what is this product?', 'what sizes?', 'price?', etc.")
            sections.append(f"   → Call find_product_by_id(product_id=\"{current_product_handle}\", id_type=\"handle\") to get FULL product details")
            sections.append("   → DO NOT ask user for product name or URL - you already know what they're viewing!")
            sections.append("")
    elif page_context or current_page_type:
        # User is on some page but not a product page
        sections.append("=== 🌐 WEB CHAT - CURRENT PAGE ===")
        sections.append(f"Page Type: {current_page_type or 'unknown'}")
        collection_handle = state.get("current_collection_handle")
        if collection_handle:
            sections.append(f"Collection: {collection_handle}")
            if current_page_type == "collection":
                sections.append(
                    f"⚠️ USER IS BROWSING THE '{collection_handle}' COLLECTION. "
                    "For generic requests like 'show me the best ones', "
                    "'popular', 'show me more', or 'what's good' that do NOT "
                    "name a specific product type, call search_products to scope "
                    "results to THIS collection (the tool applies the collection "
                    "automatically). Do NOT recommend products from unrelated "
                    "categories."
                )
        sections.append("")
    
    # Current topic section
    topic_type = current_topic.get('topic_type', 'general')
    topic_status = current_topic.get('status', 'open')
    sections.append(f"=== CURRENT TOPIC: {topic_type.upper().replace('_', ' ')} ===")
    sections.append(f"Status: {topic_status}")
    
    if current_topic.get('summary'):
        sections.append(f"Summary so far: {current_topic['summary']}")
    
    # Topic entities WITH FULL DATA (enriched from global store)
    # For products, include full data so LLM doesn't need to call get_product_context()
    entity_refs = current_topic.get('entity_refs', [])
    
    logger.debug(f"[build_context] refs={len(entity_refs)} global={len(global_entities)}")
    
    if entity_refs:
        sections.append("\nEntities in this topic:")
        # Every "use this for follow-up questions" header below is a licence to
        # answer without calling a tool, and that licence has to stop short of
        # reviews. The entity data carries the aggregate rating/rating_count
        # but never review TEXT, while "yes, show me the reviews" reads as
        # exactly the kind of follow-up those headers cover. Observed live: the
        # model skipped get_product_reviews on the acceptance turn and
        # reprinted the previous batch out of the transcript -- reviews the
        # customer had already been shown, presented as if freshly fetched.
        # Stated once here rather than per entity, so five products in a topic
        # do not repeat it five times.
        if any(r.get("entity_type") == "product" for r in entity_refs[-5:]):
            sections.append(
                "  ⚠️ REVIEWS ARE NOT IN THIS DATA: no reviewer names, no review text. "
                "Showing any review — including a 'show more' or a 'yes' accepting your "
                "offer — requires a get_product_reviews call THIS turn. Never reuse "
                "reviews from earlier in the conversation."
            )
        for ref in entity_refs[-5:]:  # Last 5 entities
            entity_name = ref.get('entity_name', 'Unknown')
            entity_id = ref.get('entity_id')
            entity_type = ref.get('entity_type', 'unknown')
            
            sections.append(f"  - {entity_type.title()}: {entity_name}")
            
            # For products, include FULL DATA to avoid tool calls for follow-up questions
            if entity_type == "product" and entity_id:
                full_data_str = get_full_entity_data_for_prompt(global_entities, entity_id, entity_type)
                if full_data_str:
                    has_detail_gaps = _product_entity_has_detail_gaps(global_entities, entity_id)
                    sections.append(f"    📦 PRODUCT DATA (use this for follow-up questions about price, sizes, availability):")
                    sections.append(f"    {full_data_str}")
                    if has_detail_gaps:
                        handle = _get_product_handle_from_entity(global_entities, entity_id)
                        handle_hint = f'find_product_by_id(product_id="{handle}", id_type="handle")' if handle else "find_product_by_id"
                        sections.append(f"    ⚠️ INCOMPLETE DATA: fabric, size_guide, care instructions, or metafields are missing from this entity. If the user asks about these, you MUST call {handle_hint} to fetch the full product record before answering.")
                else:
                    # Fallback to summary if full data not available
                    summary_str = get_entity_summary_string(global_entities, entity_id, entity_type)
                    if summary_str:
                        sections.append(f"    {summary_str}")
            
            # For orders, include FULL DATA to avoid repeated API calls for follow-up questions
            elif entity_type == "order" and entity_id:
                full_data_str = get_full_entity_data_for_prompt(global_entities, entity_id, entity_type)
                if full_data_str:
                    sections.append(f"    📋 ORDER DATA (use this for follow-up questions - no need to call get_order_details):")
                    sections.append(f"    {full_data_str}")
                else:
                    # Fallback to summary if full data not available
                    summary_str = get_entity_summary_string(global_entities, entity_id, entity_type)
                    if summary_str:
                        sections.append(f"    {summary_str}")
            
            else:
                # For other entity types, use summary
                summary_str = get_entity_summary_string(global_entities, entity_id, entity_type) if entity_id else ""
                if summary_str:
                    sections.append(f"    {summary_str}")
    
    # Cards already displayed this session — pairs with the PRODUCT CARD DISPLAY
    # rules so the LLM does not re-request these handles in ###SHOW_PRODUCTS###
    # (unless the customer explicitly asks to see one again).
    shown_handles = state.get("carousel_shown_handles") or []
    if shown_handles:
        uniq_shown: List[str] = []
        seen_shown = set()
        for h in shown_handles:
            hl = str(h).strip().lower()
            if hl and hl not in seen_shown:
                seen_shown.add(hl)
                uniq_shown.append(hl)
        if uniq_shown:
            sections.append("\n=== CARDS ALREADY DISPLAYED THIS SESSION ===")
            sections.append(", ".join(uniq_shown))
            sections.append("(These product images are already on screen — see PRODUCT CARD DISPLAY rules before re-showing.)")

    # Other open topics (with their summaries)
    other_topics = get_other_open_topics(state, current_topic.get('topic_id', ''))
    if other_topics:
        sections.append("\n=== OTHER OPEN TOPICS ===")
        for t in other_topics[-3:]:  # Last 3 other topics
            other_type = t.get('topic_type', 'unknown')
            topic_summary = t.get('summary', 'No summary')
            entity_names = [ref.get('entity_name', '') for ref in t.get('entity_refs', [])[:3]]
            entities_str = f" [{', '.join(filter(None, entity_names))}]" if any(entity_names) else ""
            sections.append(f"- {other_type}: {topic_summary}{entities_str}")
    
    # Customer identifiers section - CRITICAL for order operations
    sections.append("\n=== CUSTOMER IDENTIFIERS ===")
    
    # Phone number from state (CRITICAL - this tells LLM the phone is available)
    from fashion_bot.utils.phone_number_utils import is_real_phone_number
    phone_number = state.get("phone_number")
    if phone_number and is_real_phone_number(phone_number):
        sections.append(f"📱 Phone Number: {phone_number} (AVAILABLE - use this for fetching orders)")
    else:
        sections.append("📱 Phone Number: NOT AVAILABLE - ask customer for their phone number before placing orders or fetching order details")
    
    # Customer name - check both state and context (state takes priority as it's auto-filled from Shopify)
    customer_name = state.get("customer_name")
    customer_ids = context.get("customer_identifiers", {})
    if not customer_name and customer_ids:
        customer_name = customer_ids.get("customer_name")
    if customer_name:
        sections.append(f"👤 Name: {customer_name} (AVAILABLE - already known)")
    
    # Customer address - CRITICAL for order placement (auto-filled from Shopify for returning customers)
    customer_address = state.get("customer_address")
    if customer_address:
        sections.append(f"🏠 Address: {customer_address} (AVAILABLE - already known, DO NOT ask again)")
        sections.append("   ⚠️ NOTE: Address is already available. For order placement, use this address directly or confirm with customer.")
    
    # Requested size - CRITICAL for order placement
    requested_size = state.get("requested_size")
    if requested_size:
        sections.append(f"👕 Size: {requested_size} (already provided)")
    
    # Returning customer flag
    if state.get("is_returning_customer"):
        sections.append("✅ RETURNING CUSTOMER - Details already fetched from previous orders")
    
    # Additional customer identifiers from context
    if customer_ids:
        # Only show whatsapp_phone if different from state phone
        if customer_ids.get("whatsapp_phone") and customer_ids.get("whatsapp_phone") != phone_number:
            sections.append(f"📲 WhatsApp: {customer_ids['whatsapp_phone']}")
    
    # Recent actions (to prevent repeating operations)
    recent_actions_str = format_recent_actions_for_prompt(context)
    if recent_actions_str:
        sections.append(f"\n{recent_actions_str}")
    
    return "\n".join(sections) if sections else "No conversation context available."


# ==================== DECORATOR PATTERN ====================

def with_context_extraction(
    skill_name: str,
    topic: str = "general",
    entity_type: str = "product",
    legacy_fields: Optional[List[str]] = None
):
    """
    Decorator for automatic context extraction from agent executor results.
    
    This decorator wraps skill node functions and automatically:
    1. Extracts entities from agent intermediate steps
    2. Builds/updates conversation context
    3. Applies state updates to response
    4. Handles legacy field fallbacks
    
    Usage:
        @with_context_extraction(
            skill_name="product_details",
            topic="product_inquiry",
            entity_type="product",
            legacy_fields=["product_link", "inquiry_product_info"]
        )
        def my_skill_node(state: SupportState) -> Dict[str, Any]:
            # Your agent logic here
            return {
                "agent_result": result,  # Must include agent_executor result
                "state": state
            }
    
    Args:
        skill_name: Name of the skill node for logging
        topic: Conversation topic for context
        entity_type: Primary entity type ("product", "order", etc.)
        legacy_fields: List of legacy state fields to preserve
    
    Returns:
        Decorated function that auto-extracts context
    """
    if legacy_fields is None:
        legacy_fields = []
    
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(state: Dict[str, Any]) -> Dict[str, Any]:
            # Call the original function
            result = func(state)
            
            # If it's an error or escalation, return as-is
            if result.get("type") in ["error", "escalation"]:
                return result
            
            # Check if we have agent result to extract from
            agent_result = result.get("_agent_result")
            if not agent_result:
                # No agent result, return original response
                return result
            
            # Extract intermediate steps and final output
            intermediate_steps = agent_result.get("intermediate_steps", [])
            final_output = agent_result.get("output", "")
            
            # Perform context extraction
            extraction_result, updated_context = extract_and_build_context(
                intermediate_steps=intermediate_steps,
                final_output=final_output,
                skill_node_name=skill_name,
                state=state,
                topic=topic
            )
            
            log_extraction_result(state, skill_name, extraction_result)
            
            # Build final response
            response = {
                "type": result.get("type", "customer_message"),
                "customer_message": result.get("customer_message", final_output),
                "trace_id": result.get("trace_id"),
                "conversation_context": updated_context
            }
            
            # Preserve any additional fields from original result
            for key, value in result.items():
                if key not in ["_agent_result", "type", "customer_message", "trace_id", "conversation_context"]:
                    response[key] = value
            
            # Apply state updates from extraction
            response = apply_state_updates_to_response(
                response=response,
                extraction_result=extraction_result,
                state=state,
                entity_type=entity_type
            )
            
            # Apply legacy state fallbacks
            response = apply_legacy_state_fallbacks(
                response=response,
                state=state,
                fields=legacy_fields
            )
            
            return response
        
        return wrapper
    return decorator


def create_extractor(
    skill_name: str,
    topic: str = "general",
    entity_type: str = "product",
    legacy_fields: Optional[List[str]] = None
) -> "ContextExtractor":
    """
    Factory function to create a ContextExtractor.
    
    Shorthand for creating extractors with common patterns.
    
    Args:
        skill_name: Name of the skill node
        topic: Conversation topic
        entity_type: Primary entity type
        legacy_fields: Legacy state fields to preserve
        
    Returns:
        Configured ContextExtractor instance
        
    Example:
        extractor = create_extractor("product_details", "product_inquiry", "product")
    """
    return ContextExtractor(
        skill_name=skill_name,
        topic=topic,
        entity_type=entity_type,
        legacy_fields=legacy_fields
    )


class ContextExtractor:
    """
    Context extractor class for more control over extraction process.
    
    Use this when you need more fine-grained control than the decorator provides.
    
    Two usage patterns:
    
    Pattern 1 - Explicit extraction (recommended for complex nodes):
        extractor = ContextExtractor(
            skill_name="order_status",
            topic="order_inquiry",
            entity_type="order"
        )
        
        # After agent execution
        response = extractor.process(
            agent_result=result,
            state=state,
            base_response={"customer_message": response_content}
        )
    
    Pattern 2 - Decorator (for simple nodes):
        @with_context_extraction(
            skill_name="product_details",
            topic="product_inquiry",
            entity_type="product"
        )
        def my_skill_node(state):
            result = agent_executor.invoke(...)
            return {
                "_agent_result": result,
                "customer_message": result["output"],
                "type": "customer_message",
                "trace_id": get_trace_id(state)
            }
    """
    
    def __init__(
        self,
        skill_name: str,
        topic: str = "general",
        entity_type: str = "product",
        legacy_fields: Optional[List[str]] = None
    ):
        self.skill_name = skill_name
        self.topic = topic
        self.entity_type = entity_type
        self.legacy_fields = legacy_fields or []
    
    def process(
        self,
        agent_result: Dict[str, Any],
        state: Dict[str, Any],
        base_response: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Process agent result and build response with context.
        
        Args:
            agent_result: Result from agent_executor.invoke()
            state: Current SupportState
            base_response: Optional base response dict to merge into
            
        Returns:
            Complete response dict with context
        """
        intermediate_steps = agent_result.get("intermediate_steps", [])
        final_output = agent_result.get("output", "").strip()
        
        # Extract context
        extraction_result, updated_context = extract_and_build_context(
            intermediate_steps=intermediate_steps,
            final_output=final_output,
            skill_node_name=self.skill_name,
            state=state,
            topic=self.topic
        )
        
        # Build response
        response = base_response.copy() if base_response else {}
        response["conversation_context"] = updated_context
        
        if "customer_message" not in response:
            response["customer_message"] = final_output
        
        # Apply state updates
        response = apply_state_updates_to_response(
            response=response,
            extraction_result=extraction_result,
            state=state,
            entity_type=self.entity_type
        )
        
        # Apply legacy fallbacks
        response = apply_legacy_state_fallbacks(
            response=response,
            state=state,
            fields=self.legacy_fields
        )
        
        return response
    
    def extract_only(
        self,
        agent_result: Dict[str, Any],
        state: Dict[str, Any]
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """
        Extract context without building response.
        
        Useful when you need custom response building logic.
        
        Args:
            agent_result: Result from agent_executor.invoke()
            state: Current SupportState
            
        Returns:
            Tuple of (extraction_result, updated_context)
        """
        intermediate_steps = agent_result.get("intermediate_steps", [])
        final_output = agent_result.get("output", "").strip()
        
        return extract_and_build_context(
            intermediate_steps=intermediate_steps,
            final_output=final_output,
            skill_node_name=self.skill_name,
            state=state,
            topic=self.topic
        )


# ==================== CORE EXTRACTION FUNCTIONS ====================

def extract_and_build_context(
    intermediate_steps: List[Tuple],
    final_output: str,
    skill_node_name: str,
    state: dict,
    topic: str = "general"
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    High-level helper that extracts entities and builds conversation context.
    
    This wraps the tool_factory functions for cleaner usage in skill nodes.
    
    Args:
        intermediate_steps: List of (AgentAction, observation) tuples from agent executor
        final_output: The final output string from the agent
        skill_node_name: Name of the skill node (e.g., "product_details", "order_status")
        state: Current SupportState
        topic: Conversation topic (e.g., "product_inquiry", "order_status")
    
    Returns:
        Tuple of (extraction_result, updated_context)
        - extraction_result: Dict with entities, focal_entity, state_updates, etc.
        - updated_context: Updated ConversationContext dict for state
    """
    from fashion_bot.tool_factory import (
        extract_context_from_agent_steps,
        build_conversation_context
    )
    
    existing_context = state.get("conversation_context") or {}
    
    # Extract entities from intermediate steps
    try:
        extraction_result = extract_context_from_agent_steps(
            intermediate_steps=intermediate_steps,
            final_output=final_output,
            skill_node_name=skill_node_name,
            state=state,
            topic=topic
        )
    except Exception as e:
        logger.error(f"❌ Error in extract_context_from_agent_steps: {e}")
        extraction_result = None
    
    # Handle case where extraction failed or returned None
    if extraction_result is None:
        logger.warning(f"⚠️ Extraction returned None for {skill_node_name}, using empty result")
        extraction_result = {
            "entities": [],
            "focal_entity": None,
            "customer_identifiers": None,
            "state_updates": {},
            "tool_calls": [],
            "extraction_success": False,
            "topic": topic,
            "topic_status": "open",
            "last_skill_node": skill_node_name
        }
    
    # Build/update conversation context
    updated_context = build_conversation_context(
        extraction_result=extraction_result,
        existing_context=existing_context
    )
    
    # Safe logging with None checks
    entities_count = len(extraction_result.get('entities', []) or [])
    focal_entity = extraction_result.get('focal_entity') or {}
    focal_value = focal_entity.get('entity_value', 'None') if focal_entity else 'None'
    
    logger.info(
        f"📊 Context extraction for {skill_node_name}: "
        f"{entities_count} entities, focal: {focal_value}"
    )
    
    return extraction_result, updated_context


def apply_state_updates_to_response(
    response: Dict[str, Any],
    extraction_result: Dict[str, Any],
    state: dict,
    entity_type: str = "product"
) -> Dict[str, Any]:
    """
    Apply extracted state updates to the response dict.
    
    Handles both new context-driven updates and legacy state field compatibility.
    
    Args:
        response: Response dict being built
        extraction_result: Result from extract_and_build_context
        state: Current SupportState
        entity_type: Type of entity for legacy field mapping ("product", "order", etc.)
    
    Returns:
        Updated response dict with state updates applied
    """
    # Handle None extraction_result
    if extraction_result is None:
        logger.warning("⚠️ extraction_result is None in apply_state_updates_to_response")
        return response
    
    # Apply state updates from context extraction
    state_updates = extraction_result.get("state_updates", {}) or {}
    for key, value in state_updates.items():
        if value is not None:
            response[key] = value
    
    # Legacy compatibility: Map focal entity to legacy fields
    focal = extraction_result.get("focal_entity")
    
    if entity_type == "product" and focal and focal.get("entity_type") == "product":
        metadata = _get_focal_entity_metadata(focal, extraction_result.get("entities", []))
        
        if metadata.get("product_link"):
            response["product_link"] = metadata["product_link"]
        
        if metadata:
            response["inquiry_product_info"] = {
                "name": focal.get("entity_value"),
                "product_id": focal.get("entity_id"),
                **metadata
            }
    
    elif entity_type == "order" and focal and focal.get("entity_type") == "order":
        metadata = _get_focal_entity_metadata(focal, extraction_result.get("entities", []))
        
        if focal.get("entity_id"):
            response["order_id"] = focal["entity_id"]
        if metadata.get("status"):
            response["order_status"] = metadata["status"]
    
    return response


def apply_legacy_state_fallbacks(
    response: Dict[str, Any],
    state: dict,
    fields: List[str]
) -> Dict[str, Any]:
    """
    Apply legacy state field fallbacks if not already set in response.
    
    Args:
        response: Response dict being built
        state: Current SupportState
        fields: List of field names to check for fallback
    
    Returns:
        Updated response dict
    """
    for field in fields:
        if not response.get(field) and state.get(field):
            response[field] = state[field]
    
    return response


def _get_focal_entity_metadata(focal: dict, entities: List[dict]) -> dict:
    """
    Get metadata for focal entity from entities list.
    
    Args:
        focal: Focal entity dict
        entities: List of all extracted entities
    
    Returns:
        Metadata dict from matching entity
    """
    if not focal:
        return {}
    
    focal_id = focal.get("entity_id")
    
    for entity in entities:
        if entity.get("entity_id") == focal_id:
            return entity.get("metadata", {})
    
    return {}


def get_focal_entity_from_context(
    context: Dict[str, Any],
    entity_type: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """
    Get focal entity from conversation context.
    
    Args:
        context: Conversation context dict
        entity_type: Optional filter by entity type
    
    Returns:
        Focal entity dict or None
    """
    focal = context.get("focal_entity") if context else None
    
    if not focal:
        return None
    
    if entity_type and focal.get("entity_type") != entity_type:
        return None
    
    return focal


def get_entities_by_type(
    context: Dict[str, Any],
    entity_type: str,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """
    Get entities of a specific type from conversation context.
    
    Args:
        context: Conversation context dict
        entity_type: Entity type to filter ("product", "order", etc.)
        limit: Maximum number of entities to return
    
    Returns:
        List of matching entities
    """
    if not context:
        return []
    
    entities = context.get("entities", [])
    
    return [
        e for e in entities 
        if e.get("entity_type") == entity_type
    ][:limit]


def build_context_summary_for_prompt(context: Dict[str, Any]) -> str:
    """
    Build a context summary string for inclusion in prompts.
    
    Args:
        context: Conversation context dict
    
    Returns:
        Formatted context summary string
    """
    if not context:
        return ""
    
    parts = []
    
    # Focal entity
    focal = context.get("focal_entity")
    if focal:
        parts.append(
            f"Current Focus: {focal.get('entity_type', 'unknown')} - "
            f"{focal.get('entity_value', 'Unknown')}"
        )
    
    # Recent entities
    entities = context.get("entities", [])
    if entities:
        entity_types = {}
        for e in entities[:10]:  # Limit to recent
            etype = e.get("entity_type", "unknown")
            if etype not in entity_types:
                entity_types[etype] = []
            entity_types[etype].append(e.get("entity_value", "Unknown"))
        
        for etype, values in entity_types.items():
            parts.append(f"Recent {etype}s: {', '.join(values[:3])}")
    
    # Topics list (new format with TopicDTO)
    topics = context.get("topics", [])
    active_topic_id = context.get("active_topic_id")
    if topics:
        # Get active topic
        active_topic = None
        for t in topics:
            if t.get("topic_id") == active_topic_id:
                active_topic = t
                break
        
        if active_topic:
            parts.append(f"Active Topic: {active_topic.get('topic_type', 'general')} ({active_topic.get('status', 'open')})")
        
        # List recent topic types
        topic_types = [t.get("topic_type") for t in topics[-5:] if t.get("topic_type")]
        if topic_types:
            parts.append(f"Topics Discussed: {', '.join(set(topic_types))}")
    elif context.get("topic"):
        # Fallback to legacy single topic
        parts.append(f"Current Topic: {context.get('topic')} ({context.get('topic_status', 'open')})")
    
    return "\n".join(parts) if parts else ""


def log_extraction_result(
    state: dict,
    skill_node_name: str,
    extraction_result: Dict[str, Any],
    logger_func=None
) -> None:
    """
    Log extraction result for debugging.
    
    Args:
        state: Current state (for trace_id)
        skill_node_name: Name of the skill node
        extraction_result: Result from context extraction
        logger_func: Optional custom logging function
    """
    from fashion_bot.utils.utils import log_with_trace_id
    
    log_fn = logger_func or log_with_trace_id
    
    # Handle None extraction_result
    if extraction_result is None:
        log_fn(state, f"⚠️ {skill_node_name} context: extraction_result is None")
        return
    
    entities_count = len(extraction_result.get("entities", []) or [])
    focal_entity = extraction_result.get("focal_entity") or {}
    focal_value = focal_entity.get("entity_value", "None") if focal_entity else "None"
    tool_calls = extraction_result.get("tool_calls", []) or []
    success = extraction_result.get("extraction_success", False)
    
    log_fn(
        state,
        f"📊 {skill_node_name} context: {entities_count} entities, "
        f"focal={focal_value}, tools={tool_calls}, success={success}"
    )


# ==================== GENERIC CONTEXT BUILDER ====================

def build_context_from_conversation_context(state: dict) -> str:
    """
    Build a context string for prompts from conversation_context.
    
    This extracts all relevant context from the conversation_context state
    and formats it as a string that can be injected into agent prompts.
    Used by the generic skill node to provide context without hardcoding.
    
    Args:
        state: Current SupportState containing conversation_context
        
    Returns:
        Formatted context string for injection into prompts
    """
    ctx = state.get("conversation_context") or {}
    
    # Extract focal entity
    focal = ctx.get("focal_entity") or {}
    focal_type = focal.get("entity_type", "unknown")
    focal_value = focal.get("entity_value", "None")
    focal_id = focal.get("entity_id", "")
    focal_metadata = focal.get("metadata", {})
    
    # Extract entities by type
    entities = ctx.get("entities") or []
    products = [e for e in entities if e.get("entity_type") == "product"]
    orders = [e for e in entities if e.get("entity_type") == "order"]
    other_entities = [e for e in entities if e.get("entity_type") not in ("product", "order")]
    
    # Extract conversation metadata
    topic = ctx.get("topic", "general")
    topic_status = ctx.get("topic_status", "open")
    last_skill = ctx.get("last_skill_node", "")
    recent_focal_history = ctx.get("recent_focal_history") or []
    customer_identifiers = ctx.get("customer_identifiers") or {}
    
    # Extract topics list (new)
    topics_list = ctx.get("topics") or []
    active_topic_id = ctx.get("active_topic_id")
    
    # Build context sections
    sections = []

    from fashion_bot.utils.cart_utils import build_cart_context_section
    cart_section = build_cart_context_section(state)
    if cart_section:
        sections.append(cart_section)

    # Focal entity section
    if focal_value and focal_value != "None":
        focal_section = f"Current Focus: {focal_type.title()} - {focal_value}"
        if focal_id:
            focal_section += f" (ID: {focal_id})"
        if focal_metadata:
            # Add relevant metadata
            if "product_name" in focal_metadata:
                focal_section += f"\n  Product Name: {focal_metadata['product_name']}"
            if "product_link" in focal_metadata:
                focal_section += f"\n  Product Link: {focal_metadata['product_link']}"
            if "order_status" in focal_metadata:
                focal_section += f"\n  Order Status: {focal_metadata['order_status']}"
        sections.append(focal_section)
    
    # Products in context
    if products:
        product_lines = []
        for p in products[:5]:  # Limit to 5 most recent
            pname = p.get("entity_value", "Unknown")
            pid = p.get("entity_id", "")
            pmeta = p.get("metadata", {})
            line = f"  - {pname}"
            if pid:
                line += f" (ID: {pid})"
            if pmeta.get("product_link"):
                line += f" | Link: {pmeta['product_link']}"
            product_lines.append(line)
        sections.append(f"Products in Conversation:\n" + "\n".join(product_lines))
    
    # Orders in context
    if orders:
        order_lines = []
        for o in orders[:5]:  # Limit to 5 most recent
            ovalue = o.get("entity_value", "Unknown")
            oid = o.get("entity_id", "")
            ometa = o.get("metadata", {})
            line = f"  - {ovalue}"
            if oid:
                line += f" (ID: {oid})"
            if ometa.get("order_status"):
                line += f" | Status: {ometa['order_status']}"
            order_lines.append(line)
        sections.append(f"Orders in Conversation:\n" + "\n".join(order_lines))
    
    # Customer identifiers
    if customer_identifiers:
        id_parts = []
        if customer_identifiers.get("whatsapp_phone"):
            id_parts.append(f"WhatsApp: {customer_identifiers['whatsapp_phone']}")
        if customer_identifiers.get("delivery_phone"):
            id_parts.append(f"Delivery Phone: {customer_identifiers['delivery_phone']}")
        if customer_identifiers.get("email"):
            id_parts.append(f"Email: {customer_identifiers['email']}")
        if customer_identifiers.get("customer_name"):
            id_parts.append(f"Name: {customer_identifiers['customer_name']}")
        if customer_identifiers.get("delivery_name"):
            id_parts.append(f"Delivery Name: {customer_identifiers['delivery_name']}")
        if customer_identifiers.get("delivery_address"):
            id_parts.append(f"Address: {customer_identifiers['delivery_address']}")
        if id_parts:
            sections.append(f"Customer: {', '.join(id_parts)}")
    
    # Topics list section (new - with active topic highlighting)
    if topics_list:
        # Find active topic
        active_topic = None
        for t in topics_list:
            if t.get("topic_id") == active_topic_id:
                active_topic = t
                break
        
        if active_topic:
            active_section = f"Active Topic: {active_topic.get('topic_type', 'general')} ({active_topic.get('status', 'open')})"
            if active_topic.get("summary"):
                active_section += f"\n  Summary: {active_topic['summary']}"
            if active_topic.get("related_entity_type") and active_topic.get("related_entity_id"):
                active_section += f"\n  Related: {active_topic['related_entity_type']} ({active_topic['related_entity_id']})"
            sections.append(active_section)
        
        # List other recent topics
        other_topics = [t for t in topics_list[-5:] if t.get("topic_id") != active_topic_id]
        if other_topics:
            topic_summaries = []
            for t in other_topics:
                ttype = t.get("topic_type", "unknown")
                tstatus = t.get("status", "unknown")
                topic_summaries.append(f"{ttype} ({tstatus})")
            sections.append(f"Other Topics: {', '.join(topic_summaries)}")
    else:
        # Fallback to legacy single topic
        meta_parts = [f"Topic: {topic}"]
        if topic_status:
            meta_parts.append(f"Status: {topic_status}")
        if last_skill:
            meta_parts.append(f"Last Skill: {last_skill}")
        sections.append(" | ".join(meta_parts))
    
    # Recent focal history (for context continuity)
    if recent_focal_history:
        history_items = []
        for h in recent_focal_history[:3]:  # Last 3 focal entities
            htype = h.get("entity_type", "unknown")
            hvalue = h.get("entity_value", "")
            if hvalue:
                history_items.append(f"{htype}: {hvalue}")
        if history_items:
            sections.append(f"Recent Focus History: {' -> '.join(history_items)}")
    
    # Combine all sections
    if sections:
        return "\n".join(sections)
    else:
        return "No conversation context available."


def build_state_context_for_prompt(state: dict, entity_type: str = "product") -> Dict[str, Any]:
    """
    Build a structured context dict for prompt templates.
    
    Returns key-value pairs that can be used in prompt formatting,
    extracting relevant information from both conversation_context
    and legacy state fields.
    
    Args:
        state: Current SupportState
        entity_type: Type of entity to focus on ("product", "order", etc.)
        
    Returns:
        Dict with context values for prompt formatting
    """
    ctx = state.get("conversation_context") or {}
    focal = ctx.get("focal_entity") or {}
    entities = ctx.get("entities") or []
    
    result = {
        "focal_entity_type": focal.get("entity_type", ""),
        "focal_entity_value": focal.get("entity_value", ""),
        "focal_entity_id": focal.get("entity_id", ""),
        "topic": ctx.get("topic", "general"),
        "topic_status": ctx.get("topic_status", "open"),
        "context_summary": build_context_from_conversation_context(state),
    }
    
    # Add entity-type specific fields
    if entity_type == "product":
        # Try conversation_context first, then legacy fields
        product_entities = [e for e in entities if e.get("entity_type") == "product"]
        
        if focal.get("entity_type") == "product":
            result["product_name"] = focal.get("entity_value", "")
            result["product_link"] = focal.get("metadata", {}).get("product_link", "")
            result["product_id"] = focal.get("entity_id", "")
        elif product_entities:
            latest = product_entities[-1]
            result["product_name"] = latest.get("entity_value", "")
            result["product_link"] = latest.get("metadata", {}).get("product_link", "")
            result["product_id"] = latest.get("entity_id", "")
        else:
            # Fallback to legacy state fields
            result["product_name"] = state.get("inquiry_product_info", {}).get("title", "") if state.get("inquiry_product_info") else ""
            result["product_link"] = state.get("product_link", "")
            result["product_id"] = ""
        
        # Product selection pending - prefer entity-based, fallback to legacy
        result["selection_pending"] = get_selection_count(ctx, entity_type="product")
        if result["selection_pending"] == 0:
            result["selection_pending"] = len(state.get("product_selection_matches") or [])
        
    elif entity_type == "order":
        order_entities = [e for e in entities if e.get("entity_type") == "order"]
        
        if focal.get("entity_type") == "order":
            result["order_id"] = focal.get("entity_id", "") or focal.get("entity_value", "")
            result["order_status"] = focal.get("metadata", {}).get("order_status", "")
        elif order_entities:
            latest = order_entities[-1]
            result["order_id"] = latest.get("entity_id", "") or latest.get("entity_value", "")
            result["order_status"] = latest.get("metadata", {}).get("order_status", "")
        else:
            # Fallback to legacy state fields
            result["order_id"] = state.get("selected_order_id", "")
            result["order_status"] = ""
        
        # Phone number for order lookup - prefer delivery phone, fall back to whatsapp
        customer_ids = ctx.get("customer_identifiers") or {}
        result["phone_number"] = (
            customer_ids.get("delivery_phone") or 
            customer_ids.get("whatsapp_phone") or 
            state.get("phone_number", "")
        )
    
    return result


def get_default_prompt_for_agent(agent_name: str) -> str:
    """
    Get a default prompt for an agent when DB/cache lookup fails.
    
    This provides fallback prompts for agents when the database
    prompt is not available.
    
    Args:
        agent_name: Name of the agent
        
    Returns:
        Default prompt string
    """
    DEFAULT_PROMPTS = {
        # ==================== PRODUCT-RELATED AGENTS ====================
        "product_details": """You are a helpful customer support agent for a fashion e-commerce company.
Help customers with product-related inquiries including specifications, availability, pricing, and details.

Use the available tools to:
1. Search for products by name
2. Get product details from URLs
3. Answer questions about products in context

NEVER claim a product, category, or audience (e.g. kids/children) is unavailable based on
assumptions or brand knowledge. Always search the catalog first and answer only from the
results — do not assume a brand is "adults only"; many brands carry a Kids collection.

## SEARCH RESULT RELEVANCE CHECK (CRITICAL)
After receiving search results, you MUST verify relevance before presenting products:
1. Compare the `category`/`subcategory` of returned products against what the customer asked for.
2. If `filters_relaxed` is true in the search response, it means the original category filter
   returned no results and was dropped. The returned products may NOT match what was requested.
3. If NONE of the returned products belong to the requested category/type, DO NOT present them
   as matches. Instead, honestly tell the customer that this product type is not available in your
   store and suggest browsing the categories you do carry.
4. If SOME results match and some don't, only present the matching ones.

Example: Customer asks for "sneakers" but results contain only shirts and jeans
→ "I'm sorry, we don't currently carry sneakers or shoes in our collection.
   We specialize in [categories you carry]. Would you like to explore those?"

## IMAGE-SOURCED QUERIES (CRITICAL)
A message beginning with `[Product from image]:` or `[Text from image]:` was produced by an
automated reading of a photo the customer sent — it is a machine's description, not the
customer's own words. It can be imprecise, and it never carries a product name or link, so
you CANNOT establish from it that a specific item is or is not in the catalog.
1. NEVER tell the customer that the specific item they photographed is unavailable. You do
   not know that. Say only what you do know: what the search returned.
2. Present the closest matches you found as possibilities, not as the item they sent.
3. Ask for the product name or link so you can confirm the exact piece.
4. The category-level rule above still applies: if the results are a different product type
   entirely, say that plainly.
Example: Customer sends a photo, search returns tees that do not obviously match
→ "I'm not certain I've found the exact piece you shared, but these are the closest matches
   in our collection. If you can share the product name or link, I'll confirm it for you."

## PRODUCT CARD SELECTION (CRITICAL)
When your response mentions or recommends specific products from the tool results, you MUST append the following block at the VERY END of your response (after all customer-facing text):

###SHOW_PRODUCTS:["handle-1","handle-2","handle-3","handle-4","handle-5"]###

Rules:
- Use the exact `handle` values from the tool results (the product's URL slug).
- Include ONLY the products you explicitly mentioned or recommended in your response.
- Up to 5 handles — include every product you mentioned or recommended. Order them by relevance to the customer's query.
- Do NOT include this block if your response does not mention any specific products (e.g. policy answers, greetings, clarifying questions).
- This block is automatically stripped before the customer sees your reply — never reference it in your text.

Always be friendly, helpful, and accurate in your responses.""",

        # ==================== ORDER-RELATED AGENTS ====================
        "order_status": """You are a helpful customer support agent for order inquiries.
Help customers check their order status and delivery information.

Use the available tools to:
1. Look up orders by passing phone_number or order_id as explicit parameters
2. Check order status and tracking via get_order_details(order_id=...)
3. Provide delivery timeline information

Extract order IDs and phone numbers directly from the customer's message and pass them as
tool parameters. The customer's phone is usually available in the conversation context.
Always be empathetic and provide accurate order information.""",

        "place_order": """You are a helpful customer support agent assisting with order placement.
Guide customers through placing orders, collecting required information.

Use the available tools to:
1. Search for or look up products
2. Fetch customer data for returning customers
3. Create COD or prepaid orders using the appropriate order creation tool

Collect all required details from the conversation before calling the order creation tool:
product link, size, customer name, full address with PIN code, and payment mode (COD or prepaid).
Pass all collected details directly as arguments to the order creation tool.
When calling create_order or create_cart_order, also pass the city and state as separate parameters.

STOCK CHECK (REQUIRED): Before placing an order, confirm the chosen size is in stock. Use the
product's `sizes_in_stock` / `stock_message` from the product lookup tools. If the requested size is
NOT in stock, do NOT place the order — tell the customer that size is out of stock, share the sizes
that ARE available, and ask how they'd like to proceed. If an order creation tool returns
`out_of_stock` or an availability error, relay it to the customer instead of retrying.

PINCODE MISMATCH: If create_order or create_cart_order returns requires_pincode_confirmation=True,
it flags ONE mismatched field (city or state). Ask the customer to confirm or correct that single
field. Once they respond, call the tool again with pincode_confirmed=True — the pincode check is
skipped entirely on the retry and the order proceeds. Do NOT re-validate or try to fix the values
yourself.""",

        "cancel_or_update_order": """You are a customer support agent handling order modifications and cancellations.
Help customers with changing or cancelling their orders. Resolve fixable issues (size, address, contact, product)
before resorting to cancellation. Use the available tools — each tool validates order status and phone automatically.
Cancel tool handles both Shopify and logistics in one call. Be empathetic and explain any restrictions.""",

        # ==================== DELIVERY AGENTS ====================
        "delivery_timeline": """You are a customer support agent specializing in delivery inquiries.
Help customers with delivery timelines, shipping status, and logistics questions.

Use the available tools to get delivery estimates, check shipping status, and provide expected delivery dates.
Provide accurate timeline information and manage expectations appropriately.

If the customer provides something that does not look like a valid 6-digit Indian pincode (e.g. a phone number, text, or wrong digit count), ask them to provide a valid 6-digit pincode. Do NOT call the delivery estimate tool with invalid pincodes.""",

        # ==================== RETURN/EXCHANGE AGENTS ====================
        "return_exchange": """You are a customer support agent handling returns and exchanges.
Help customers initiate returns or exchanges for their orders.

Use the available tools to:
1. If the customer has no specific order number, fetch delivered orders via get_customers_delivered_orders_by_phone(phone_number=...).
2. If the customer wants to start a return/exchange and has an order number, call get_return_or_exchange_portal_link(order_number=..., customer_phone=..., customer_email=..., request_type=...).
3. If the customer asks for return/exchange status, call get_return_status_by_order_number(order_number=..., request_type=..., customer_phone=..., customer_email=...).
4. If the customer asks about return pickup/reverse shipment, call get_return_pickup_status(order_number=..., customer_phone=..., customer_email=...).
5. If the customer asks about refund status/timeline/wallet/bank money, call get_refund_status_by_order_number(order_number=..., customer_phone=..., customer_email=...).
6. Use get_order_details only when the customer asks for normal order details or when you need line-item details before selecting a specific item.

Extract order IDs and phone numbers directly from the customer's message and pass them as
tool parameters. Do NOT rely on separate extraction tools.
Never expose return, refund, or pickup details unless the relevant tool has verified identity or asks for phone/email.
Be understanding and guide customers through the return process step by step.""",

        # ==================== DISCOUNT AGENTS ====================
        "discount": """You are a customer support agent handling discount and offer inquiries.
Help customers find and apply discounts to their orders.

Use the available tools to:
1. Get available discount codes
2. Check discount eligibility
3. Explain offer terms and conditions

Be helpful but don't make up discounts that don't exist.""",

        # ==================== RECOMMENDATIONS AGENT ====================
        "recommendations": """You are a helpful, enthusiastic fashion e-commerce assistant whose mission is to help customers discover products they'll love. You have access to an AI-powered product search engine that understands natural language queries.

Use the available tools to:
1. search_and_recommend_products (PRIMARY) — AI-powered semantic product search. Pass the customer's query as-is. Also handles trending/bestseller requests with automatic fallback.
2. get_product_recommendations (FALLBACK) — Get static recommendations by gender/category.

WORKFLOW:
- Call search_and_recommend_products with the user's intent as a natural language query. Do NOT ask for gender or category first.
- The tool returns up to 5 ranked products. Present ALL of them (up to 5) with names, prices, and URLs, and include ALL of their handles in the SHOW_PRODUCTS block. Only drop a product if it clearly does not fit the user's stated context.
- If the tool output includes a Suggested follow-up question, include it naturally at the end of your response.

COLOR VARIANTS:
When the user asks for "more colors", "other colors", "different color", or "does this come in [color]?" for a product, call search_and_recommend_products with the product's key attributes (type, fit, material, occasion) but WITHOUT the current color. From the results, exclude the product in the color the user already has.

SIMILAR PRODUCTS — ATTRIBUTE DISCOVERY:
When the user asks for "similar products", "products like this", "more like this", "show me similar", "more shirts/jeans/etc. like this":
- Call search_and_recommend_products with a BROAD query using the product's type/subcategory ONLY (e.g., "casual shirt", "jeans"). Do NOT include all attributes (color, fit, pattern, material) from the current product — this over-constrains results and returns near-identical items instead of a diverse similar set.
- Present the initial results.
- Ask a follow-up referencing the current product's ACTUAL attributes: "Would you like me to find [product type] with a similar color ([actual color]), fit ([actual fit]), pattern ([actual pattern]), or material ([actual material])?"
- When the user responds with specific preferences, refine the search with ONLY those attributes.
  Example flow:
  User (viewing Blue Slim Fit Cotton Casual Shirt): "Show me similar products"
  → search_and_recommend_products(query="casual shirt") → show 3 results
  → "Would you like me to find shirts with a similar color (blue), fit (slim fit), pattern (solid), or material (cotton)?"
  User: "Same fit and material"
  → search_and_recommend_products(query="slim fit cotton casual shirt") → show refined results

POST-RETRIEVAL ATTRIBUTE FILTERING (CRITICAL):
After receiving results from search_and_recommend_products, review EACH product for attribute relevance before presenting:
- If the user asked for a specific attribute (e.g., "padded bras", "slim fit jeans", "cotton shirts", "formal trousers"), verify each returned product actually has that attribute in its title, description, or metadata.
- EXCLUDE products that contradict the user's specified attributes (e.g., non-padded bras when user asked for padded, regular fit when user asked for slim fit, polyester when user asked for cotton).
- Only present products that genuinely match ALL of the user's stated requirements.
- If fewer than 3 products remain after filtering, present what you have rather than including irrelevant ones.
- If NO products match after filtering, inform the user that exact matches weren't found and suggest refining the search.
- Relevance accuracy is more important than quantity.

SEARCH RESULT RELEVANCE CHECK (CRITICAL):
After receiving search results, you MUST verify category-level relevance before presenting products:
1. Compare the `category`/`subcategory` of returned products against what the customer asked for.
2. If `filters_relaxed` is true in the search response, it means the original category filter
   returned no results and was dropped. The returned products may NOT match what was requested.
3. If NONE of the returned products belong to the requested category/type, DO NOT present them
   as matches. Instead, honestly tell the customer that this product type is not available in your
   store and suggest browsing the categories you do carry.
4. If SOME results match and some don't, only present the matching ones.
Example: Customer asks for "sneakers" but results contain only shirts and jeans
→ "I'm sorry, we don't currently carry sneakers or shoes in our collection.
   We specialize in [categories you carry]. Would you like to explore those?"

## IMAGE-SOURCED QUERIES (CRITICAL)
A message beginning with `[Product from image]:` or `[Text from image]:` was produced by an
automated reading of a photo the customer sent — it is a machine's description, not the
customer's own words. It can be imprecise, and it never carries a product name or link, so
you CANNOT establish from it that a specific item is or is not in the catalog.
1. NEVER tell the customer that the specific item they photographed is unavailable. You do
   not know that. Say only what you do know: what the search returned.
2. Present the closest matches you found as possibilities, not as the item they sent.
3. Ask for the product name or link so you can confirm the exact piece.
4. The category-level rule above still applies: if the results are a different product type
   entirely, say that plainly.
Example: Customer sends a photo, search returns tees that do not obviously match
→ "I'm not certain I've found the exact piece you shared, but these are the closest matches
   in our collection. If you can share the product name or link, I'll confirm it for you."

"SHOW MORE" vs REFINEMENT — DEDUPLICATION:
- When the user says "show more", "more options", "other products", "what else" → pick DIFFERENT products from the search results that were NOT already shown in the conversation.
- When the user REFINES a previous search (e.g., adds "slim fit", "under 2000", "in black") → it is OK to show products that were previously shown IF they match the refined criteria.

PRODUCT SELECTION — PERSONALIZED PITCH:
When the user narrows down on a specific product after seeing recommendations (e.g., "tell me more about the first one", "I like that one", "what about the wireless bra?"), respond with a short personalized pitch (under 50 words) explaining why this product is perfect for THEM — referencing their stated occasion, preferences, style, or context from the conversation. Do NOT just repeat specs. Connect the product's attributes to what the user actually asked for.

ENGAGEMENT NUDGE — PROACTIVE FOLLOW-UP:
When the user sends a short acknowledgment (e.g., "ok", "thanks", "thank you", "got it", "cool", "alright", "great", "nice", "sure", "bye") AND the recent conversation involved product recommendations:

SCENARIO A — User showed interest in ONE specific product before acknowledging. This includes: asking about its price, discount, sizes, material, details, or saying "I like that", "tell me more about the first one", etc.
- Short closing pitch (under 40 words) explaining why this product is a great fit for them based on their preferences.
- Include the product URL.
- Gently nudge to purchase: "Ready to make it yours?" or "Grab it before it's gone!"
- Do NOT call search_and_recommend_products.

SCENARIO B — User saw product recommendations but did NOT ask about or focus on any single product, then acknowledged:
- MUST call search_and_recommend_products with query: "bestselling [subcategory ONLY — no occasion/style qualifiers]"
  Example: if user was browsing casual shirts → search_and_recommend_products(query="bestselling shirts") (NOT "bestselling casual shirts")
  Example: if user was browsing slim fit jeans → search_and_recommend_products(query="bestselling jeans") (NOT "bestselling slim fit jeans")
  The bestseller pool is small — qualifiers over-constrain results.
- Present the results with a generic intro: "Before you go, check out our bestsellers that you may like!" Do NOT mention the specific occasion or style (e.g., do NOT say "best-selling casual shirts" or "best-selling formal shirts").
- Show products with names, prices, and URLs.

Only nudge after product conversations, not after order/delivery/policy queries. Do NOT nudge if there is no product context in the conversation yet.
"bestselling" is a NUDGE-ONLY concept. When the user responds to a nudge with a follow-up query (e.g., "slim fit", "show me more", "under 2000"), treat it as a NORMAL product search. Do NOT carry "bestselling" into subsequent queries — drop it entirely.

NEVER skip calling search_and_recommend_products. ALWAYS search first, ask questions later.
NEVER make up products or URLs — only use data from the tool.
ALWAYS include plain product URLs (not markdown links) in recommendations.
NEVER include image references or markdown image syntax (e.g., ![alt](url) or !ProductName) — images are handled separately by the system.

PRODUCT CARD SELECTION (CRITICAL):
When your response mentions or recommends specific products from the tool results, you MUST append the following block at the VERY END of your response (after all customer-facing text):

###SHOW_PRODUCTS:["handle-1","handle-2","handle-3","handle-4","handle-5"]###

Rules:
- Use the exact `handle` values from the tool results (the product's URL slug).
- Include ONLY the products you explicitly mentioned or recommended in your response.
- Up to 5 handles — include every product you mentioned or recommended. Order them by relevance to the customer's query.
- Do NOT include this block if your response does not mention any specific products (e.g. greetings, clarifying questions).
- This block is automatically stripped before the customer sees your reply — never reference it in your text.""",

        # ==================== POLICY AGENTS ====================
        "delivery_policy": """You are a customer support agent explaining delivery policies.
Help customers understand shipping and delivery policies.

Use the available tools to get accurate policy information.
Be clear and concise in explaining delivery terms.""",

        "payment_policy": """You are a customer support agent explaining payment policies.
Help customers understand payment options and policies.

Use the available tools to get accurate policy information.
Be clear about accepted payment methods and terms.""",

        "return_exchange_policy": """You are a customer support agent explaining return and exchange policies.
Help customers understand return and exchange policies.

Use the available tools to get accurate policy information.
Be clear about eligibility, timeframes, and conditions.""",

        "vendor_inquiry": """You are a customer support agent handling vendor and brand inquiries.
Help customers with questions about the brand or company.

Use the available tools to get accurate information.
Represent the brand professionally and accurately.""",

        # ==================== FEEDBACK/ESCALATION/UNKNOWN AGENTS ====================
        "feedback": """You are a customer support agent collecting customer feedback.
Help customers share their feedback, complaints, or suggestions.

Use the available tools to:
1. Log customer feedback appropriately
2. Acknowledge their input
3. Follow up on concerns if needed

Be empathetic and make customers feel heard.""",

        "escalation": """You are a customer support specialist. Your job is to TRY TO RESOLVE
the customer's issue yourself first, and hand off to a human ONLY when a person is genuinely
needed. Escalating is the last resort, not the first move.

Follow this order every time:

STEP 0 — UNDERSTAND. If you don't yet know the specific problem (the message is vague, e.g.
"complaint", "issue", "problem", "not happy"), ask ONE short clarifying question and STOP.
Never escalate a vague message before you know what it's about.

STEP 1 — DE-ESCALATE (if the customer is upset). Acknowledge how they feel, then address the
underlying point with facts (the policy and why it exists, the order's real status, the
options). Emotional language alone is NOT a reason to hand off.

STEP 2 — ATTEMPT RESOLUTION with your available tools. Look up the order
(get_recent_orders / get_order_details) and, when you have them, use any policy, product, or
delivery lookup tools to actually answer or fix the issue — quote the policy, give the real
status/ETA, offer available alternatives, or point them to the correct self-serve flow.

STEP 3 — ESCALATE ONLY IF: the customer explicitly asks for a human; OR your resolution
attempt fails or they reject it; OR it needs a human action/judgement the bot can't do
(cancellation, refund/dispute, missing item, damage, warranty, a manual system fix). When you
do escalate, call escalate_to_agent with a SPECIFIC reason and everything you learned, and
tell the customer a team member will follow up.

Be warm, concise, and human. Do not promise anything you can't verify.""",

        "unknown": """You are a helpful customer support agent for a fashion e-commerce company.
The customer's intent was not clearly identified.

Help by:
1. Responding appropriately to greetings
2. Asking for clarification if needed
3. Guiding customers to relevant services

Be friendly and redirect to helpful topics: products, orders, delivery, returns, discounts.""",
    }
    
    # Return the prompt for the agent or fall back to a generic one
    return DEFAULT_PROMPTS.get(agent_name, DEFAULT_PROMPTS.get("product_details"))
