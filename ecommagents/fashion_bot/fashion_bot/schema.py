from typing import List, Dict, Optional, TypedDict, Any, Annotated, Literal
from langchain_core.messages import HumanMessage


# ==================== CONVERSATION CONTEXT SCHEMA ====================
# Contextual memory system for tracking conversation state, entities, and focal points

class EntitySummaryProduct(TypedDict, total=False):
    """Summarized product details for quick reference in prompts."""
    price: Optional[str]
    sizes_available: Optional[List[str]]
    in_stock: Optional[bool]
    product_link: Optional[str]


class EntitySummaryOrder(TypedDict, total=False):
    """Summarized order details for quick reference in prompts."""
    total_price: Optional[str]
    products: Optional[List[str]]  # Product names in order
    delivery_status: Optional[str]  # "Not Dispatched", "Shipped", "Delivered"
    payment_status: Optional[str]   # "Paid", "COD", "Pending"
    expected_delivery: Optional[str]
    tracking_url: Optional[str]


# ==================== CART SNAPSHOT ====================
# Mirror of the live storefront cart, pushed by the widget on every CART_UPDATED.
# Source of truth is Shopify; this is a read-only snapshot for agent context.

class CartItem(TypedDict, total=False):
    line_id: str
    variant_id: str
    product_id: str
    product_handle: str
    product_title: str
    variant_title: str
    selected_options: Dict[str, str]
    quantity: int
    unit_price: float
    line_total: float
    image_url: str
    product_url: str


class CartSnapshot(TypedDict, total=False):
    token: str
    items: List[CartItem]
    item_count: int
    subtotal: float
    currency: str
    updated_at: str
    last_action: str
    last_action_variant_id: str
    last_action_title: str


class EntityDTO(TypedDict, total=False):
    """
    Represents an entity discovered during conversation.
    Entities can be products, orders, categories, etc.
    
    Contains both a summary (key highlights for prompts) and full_data
    (complete JSON for passing to skill node tools).
    """
    entity_type: str  # "product", "order", "category", "discount_code", etc.
    entity_id: Optional[str]  # Unique identifier (order_id, product_id, etc.)
    entity_value: str  # Human-readable value or name
    source: str  # Where discovered: "tool_result", "user_message", "context"
    discovered_at: str  # ISO timestamp when discovered
    
    # Summarized key details (for prompts and quick reference)
    # Type is EntitySummaryProduct | EntitySummaryOrder | Dict based on entity_type
    summary: Optional[Dict[str, Any]]
    
    # Full raw JSON data (for passing back to skill nodes when needed)
    full_data: Optional[Dict[str, Any]]
    
    # Legacy field - kept for backward compatibility
    metadata: Optional[Dict[str, Any]]


class EntityRefDTO(TypedDict, total=False):
    """
    Lightweight entity reference for topic-level tracking.
    Topics store these references to know which entities they worked with,
    while full entity data is stored globally in conversation_context.entities[].
    """
    entity_type: str  # "product", "order", etc.
    entity_id: Optional[str]  # ID reference to global entity
    entity_name: str  # Human-readable name for display


class FocalEntityDTO(TypedDict, total=False):
    """
    The primary entity currently being discussed in conversation.
    This is what the user's current query is "about".
    """
    entity_type: str  # "product", "order", "none"
    entity_id: Optional[str]  # Unique identifier
    entity_value: str  # Human-readable name/value
    confidence: str  # "explicit" (user mentioned), "inferred" (from context), "assumed" (single option)
    set_at: str  # ISO timestamp


class TopicDTO(TypedDict, total=False):
    """
    Represents a conversation topic/thread.
    Multiple topics can exist in a conversation, with one being active.
    
    Each topic maintains:
    - A cumulative summary (updated by LLM each turn)
    - Lightweight entity references (IDs and names of entities worked on)
    - A focal entity ID (current focus within this topic)
    """
    topic_id: str  # Unique identifier for this topic instance
    topic_type: str  # "product_inquiry", "order_status", "return_exchange", "delivery", etc.
    status: str  # "open", "resolved", "pending_info", "escalated"
    started_at: str  # ISO timestamp when topic started
    updated_at: str  # ISO timestamp of last update
    
    # Cumulative summary - LLM updates this each turn, building on previous
    summary: Optional[str]
    
    # Lightweight entity references - IDs and names of entities worked on in this topic
    entity_refs: Optional[List[EntityRefDTO]]
    
    # Current focal entity within this topic
    focal_entity_id: Optional[str]
    
    # Legacy fields - kept for backward compatibility
    related_entity_id: Optional[str]  # ID of entity this topic is about
    related_entity_type: Optional[str]  # Type of related entity


class CustomerIdentifiersDTO(TypedDict, total=False):
    """
    Personal identifiers for the customer.
    These are stored as explicit state variables, not in the entities list.
    
    Important distinction:
    - whatsapp_phone: The phone number they're chatting FROM (gupshup_source_phone_number)
    - delivery_phone: Phone number provided for delivery during order placement
    """
    whatsapp_phone: Optional[str]  # Phone number from WhatsApp chat (gupshup_source_phone_number)
    delivery_phone: Optional[str]  # Phone number provided for delivery (may differ from WhatsApp)
    delivery_name: Optional[str]  # Name provided for delivery
    delivery_address: Optional[str]  # Address provided for delivery
    email: Optional[str]  # Customer's email address
    customer_name: Optional[str]  # Customer's name (may come from order or conversation)


class ActionDTO(TypedDict, total=False):
    """
    Represents a state-changing action taken during conversation.
    
    Actions are different from queries - they modify state (place order, cancel, update size).
    Tracking actions prevents the agent from repeating operations unnecessarily.
    
    Examples of actions:
    - place_order: Created order GV1234
    - cancel_order: Cancelled order GV1234
    - update_order_size: Changed size from 32 to 34 for order GV1234
    - initiate_return: Started return for order GV1234
    """
    action_type: str  # "place_order", "cancel_order", "update_size", "initiate_return", etc.
    action_name: str  # Human-readable name: "Order Placed", "Order Cancelled"
    performed_at: str  # ISO timestamp
    
    # Key parameters that identify what the action was performed on
    parameters: Dict[str, Any]  # {"order_id": "GV1234", "new_size": "34", "product": "Sunfire Denim"}
    
    # Result of the action
    success: bool  # Whether the action succeeded
    result_summary: str  # "Order GV1234 placed successfully" or "Cancellation failed: already shipped"
    
    # Link to topic where action was performed
    topic_id: Optional[str]


class ConversationContext(TypedDict, total=False):
    """
    Main contextual memory structure for tracking conversation state.
    
    This replaces manual state updates in skill nodes with a structured
    context that persists and evolves across the conversation.
    """
    # Topic management - supports multiple topics with one active
    topics: Optional[List[TopicDTO]]  # List of all topics in this conversation
    active_topic_id: Optional[str]  # ID of the currently active topic
    
    # Legacy single topic fields (for backward compatibility)
    topic: str  # "product_inquiry", "order_status", "return_exchange", "delivery", etc.
    topic_status: str  # "open", "resolved", "pending_info", "escalated"
    
    # The primary entity being discussed right now
    focal_entity: Optional[FocalEntityDTO]
    
    # All entities discovered in this conversation
    entities: List[EntityDTO]
    
    # Personal identifiers (these also update explicit state vars)
    customer_identifiers: Optional[CustomerIdentifiersDTO]
    
    # Conversation flow tracking
    last_skill_node: Optional[str]  # Last skill node that processed a query
    last_tool_calls: Optional[List[str]]  # Last tools called in the skill node
    pending_question: Optional[str]  # If bot asked a question, what is it waiting for
    
    # Recent actions taken (state-changing operations) - last 5 actions
    # Used to prevent repeating actions and give agent context of what's been done
    recent_actions: Optional[List[ActionDTO]]
    
    # Context history (last N focal entities for continuity)
    recent_focal_history: Optional[List[FocalEntityDTO]]  # Last 3-5 focal entities
    
    # Timestamps
    context_created_at: str  # When this context was created
    context_updated_at: str  # Last update time


def deletable_field_reducer(current: Optional[Any], new: Optional[Any]) -> Optional[Any]:
    """
    Custom reducer that allows deleting state fields.
    
    If new value is the special sentinel DELETE_FIELD, return None and mark for deletion.
    Otherwise, return the new value if provided, else keep current.
    """
    # Special sentinel value to indicate field deletion
    if new == "DELETE_FIELD":
        return None
    # If new value is explicitly None, keep it as None
    if new is None:
        return None
    # If new value is provided, use it
    if new is not None:
        return new
    # Otherwise keep current value
    return current


# Special sentinel constant for deleting fields
DELETE_FIELD = "DELETE_FIELD"



class OrderStatusState(TypedDict, total=False):
    messages: List[HumanMessage]
    product_info: str
    phone_number: Optional[str]
class SupportState(TypedDict, total=False):
    messages: List[HumanMessage]
    product_info: str
    phone_number: Optional[str]
    gupshup_source_phone_number: Optional[str]  # Business phone number (GUPSHUP_SOURCE)
    client_id: Optional[str]  # Client ID for multi-client support
    conversation_id: Optional[str]  # Conversations row this turn belongs to; set by channel
    # adapters before graph.invoke() and must be a declared channel so LangGraph
    # preserves it through graph execution (used for escalation linkage).
    selected_order_id: Optional[str]
    known_orders: Optional[List[Dict]]
    order_status_by_id: Optional[Dict[str, str]]
    is_order_query: Optional[bool]
    is_frustrated: Optional[bool]
    needs_escalation: Optional[bool]
    
    # --- Web Chat Page Context Fields ---
    # These fields track what page the user is currently viewing in web chat
    page_context: Optional[Dict[str, Any]]  # Full page context from widget
    current_page_type: Optional[str]  # "product", "collection", "cart", etc.
    current_product_handle: Optional[str]  # Product handle when on product page
    current_product_title: Optional[str]  # Product title when on product page
    current_product_type: Optional[str]  # Product type when on product page
    current_collection_handle: Optional[str]  # Collection handle when on collection page
    current_page_url: Optional[str]  # Full URL of the current page
    session_id: Optional[str]  # Web chat session ID (starts with "web_")

    # Live cart snapshot from the widget. Updated by the websocket layer only.
    cart: Optional[CartSnapshot]
    session_created_at: Optional[str]  # When session was created
    needs_human_agent: Optional[bool]
    callback_time: Optional[str]
    scratchpad: Optional[str]
    
    # NEW FIELD: top level intent (e.g., Sales, Enquiry)
    parent_intent: Optional[str]
    
    continuity_closure_shown: Optional[bool]  # Flag to track if continuity closure message was shown

    # --- Conversation message-limit gate ---
    # Per-conversation user-turn counter (one graph turn = one user message;
    # merged rapid-fire messages count as one). Persisted across turns via the
    # state cache. Enforced at graph entry by conversation_limit_gate. The count
    # restarts from zero when conversation_id changes (new conversation after a
    # 90-minute inactivity gap).
    user_message_count: Optional[int]
    conversation_limit_reached: Optional[bool]  # True on the turn the cap was exceeded
    message_count_conversation_id: Optional[str]  # conversation_id the counter is counting under
    # ISO-8601 UTC timestamp of when the cap was FIRST reached. Anchors the
    # time-based cooldown (conversation_limit_reset_hours) so repeated attempts
    # during the block don't push the reset out. Cleared when the counter resets.
    conversation_limit_reached_at: Optional[str]
    
    order_status_state: Optional[OrderStatusState]


    product_info_product_info: Optional[Dict]
    
    # --- dynamic fields that flow between nodes ---
    detected_intents: Optional[List[Dict]]   # output from detect_intent_node
    detected_tags: Optional[List[str]]       # tags classified from user message
    conversation_tags: Optional[List[str]]   # accumulated tags for conversation storage
    has_unknown: Optional[bool]              # flag from detect_intent_node
    customer_message: Optional[str]          # message that may be asked back to user
    type: Optional[str]                      # helper routing flag (customer_message / intent_handle)
    response_type: Optional[str]             # secondary routing flag (intent_handle)
    
    # --- Order-specific fields with deletable reducer ---
    # These fields can be cleared by returning DELETE_FIELD
    product_link: Annotated[Optional[str], deletable_field_reducer]
    requested_size: Annotated[Optional[str], deletable_field_reducer]
    selected_variant_id: Annotated[Optional[str], deletable_field_reducer]  # Shopify variant ID for selected size
    quantity: Annotated[Optional[str], deletable_field_reducer]
    inquiry_product_info: Annotated[Optional[Dict], deletable_field_reducer]
    customer_name: Annotated[Optional[str], deletable_field_reducer]
    customer_address: Annotated[Optional[str], deletable_field_reducer]
    
    # --- Product selection from search results ---
    # Stores multiple product matches when search returns >1 result
    # User can then select by number (1, 2, 3, etc.)
    product_selection_matches: Annotated[Optional[List[Dict]], deletable_field_reducer]

    # Product cards returned by the recommendation tool for carousel rendering.
    # Cleared each turn via deletable_field_reducer when no products are returned.
    recent_products: Annotated[Optional[List[Dict]], deletable_field_reducer]

    # Explicit product handles emitted by the LLM via ###SHOW_PRODUCTS### block.
    # When present, carousel matching uses these handles instead of title matching.
    show_product_handles: Optional[List[str]]

    # LLM-guided next-step suggestion tiles (from the <B_C_J> block). Set each turn
    # by generic_skill_node; consumed (popped) by the websocket after the carousel.
    suggestions: Optional[List[str]]

    # Phone the customer provided THIS turn (from the <B_C_J> block). Separate from
    # phone_number — a per-turn signal that NEVER overwrites the real/verified phone.
    captured_phone: Optional[str]

    # Handles whose carousel cards have already been displayed this session.
    # Surfaced to the LLM (via conversation context) so it avoids re-emitting them
    # in ###SHOW_PRODUCTS### unless the customer explicitly asks to see them again.
    carousel_shown_handles: Optional[List[str]]

    # Variant IDs surfaced by product-lookup tools (search_products, find_product_by_id,
    # find_product_by_url). Persisted across turns so add_to_cart's guard recognises
    # variants that were shown in a prior turn (e.g. recommendations → "add the first one").
    session_surfaced_variant_ids: Optional[List[str]]

    # Cart actions queued by add_to_cart / remove_from_cart / update_cart_quantity tools.
    # Flushed to the widget via WebSocket after the graph turn completes.
    pending_widget_actions: Optional[List[Dict]]
    
    # --- User location (browser geolocation / widget-provided) ---
    user_location: Optional[Dict[str, Any]]

    # --- Other fields ---
    pincode: Optional[str]
    waiting_for_order_confirmation: Optional[bool]    # flag to track if bot is waiting for order placement confirmation
    waiting_for_cancellation_reason: Optional[bool]  # flag to track if bot is waiting for cancellation reason
    waiting_for_update_confirmation: Optional[bool]   # flag to track if bot is waiting for update confirmation
    
    # --- CRITICAL: Context preservation flags for return/exchange flow ---
    active_return_exchange_flow: Optional[bool]       # flag to indicate user is in active return/exchange flow
    waiting_for_return_exchange_order_id: Optional[bool]  # flag to track if bot is waiting for order ID in return/exchange flow
    
    # --- Order access verification (web chat, per-client) ---
    # Grant issued by utils/order_access.py once the customer proves an order is
    # theirs (Order ID, or the delivery pincode when they don't know the ID).
    # Scoped to one client + conversation + phone, lists the order IDs it covers,
    # and expires. Absent for clients that have not opted in.
    # See design_docs/ORDER_ACCESS_VERIFICATION.md.
    order_auth: Optional[Dict[str, Any]]

    # --- Phone validation flow fields ---
    waiting_for_phone_validation: Optional[bool]      # flag to track if bot is waiting for phone number re-validation
    phone_validation_order_id: Optional[str]          # order ID for which phone validation is pending
    phone_validation_order_phone: Optional[str]       # order phone number for validation
    
    # --- Recommendations flow fields ---
    waiting_for_gender: Optional[bool]                # flag to track if bot is waiting for gender preference
    waiting_for_category: Optional[bool]              # flag to track if bot is waiting for category preference
    customer_gender: Optional[str]                    # customer's gender preference (men/women/unisex)
    customer_category: Optional[str]                  # customer's category preference (hoodies/jeans/etc.)
    recommendation_data: Optional[Dict[str, Any]]     # multi-step flow data for recommendations (gender_asked, category_asked, raw_gender, raw_category)
    
    # --- Payment mode fields for prepaid/COD selection ---
    payment_mode: Optional[str]                       # "cod" | "prepaid" - selected payment mode
    checkout_url: Optional[str]                       # Checkout URL for prepaid orders (from draft order)
    draft_order_id: Optional[str]                     # Draft order ID from Shopify for prepaid flow
    awaiting_payment_mode: Optional[bool]             # Flag to track if bot is waiting for payment mode selection
    
    # --- transaction tracing ---
    trace_id: Optional[str]                  # unique ID for tracing entire conversation flow
    
    # --- Contextual Memory System ---
    # Structured context tracking for entities and conversation state
    conversation_context: Optional[ConversationContext]  # Main context structure

    # --- Redis degraded-mode observability ---
    degraded_mode: Optional[bool]               # True when any Redis-backed operation degraded
    degraded_components: Optional[List[str]]    # Components degraded: state_cache, dedup, single_flight, summary, cache
    last_redis_error_at: Optional[str]          # ISO-ish timestamp of last Redis error
    single_flight_degraded: Optional[bool]      # True when processing lock/queue had to fail-open
    summary_skipped_due_to_redis: Optional[bool]  # True when summarization was skipped due to Redis failures
    
    # --- Timestamp tracking for template message fetching ---
    # ISO timestamp of the last message in this conversation
    # Used to determine if user returned after inactivity (e.g., 30+ mins)
    last_message_at: Optional[str]

    # --- Tool call observability ---
    # Structured traces from generic skill node tool loop (for debugging/analytics)
    tool_call_trace: Optional[List[Dict[str, Any]]]

    # --- Runtime routing controls (transient, channel-driven) ---
    # These flags are set by channel adapters before graph.invoke().
    # They must be part of SupportState so LangGraph preserves them across node transitions.
    _skip_final_answer: Optional[bool]
    _streaming_enabled: Optional[bool]


# DTO for order information
class OrderInfoDTO(TypedDict, total=False):
    order_id: str
    channel_order_id: str
    status: str
    partner_status: str
    shipment_status: str
    customer: str
    delivery_date: Optional[str]
    out_for_delivery_date: Optional[str]  # When order went out for delivery
    items: List[str]
    courier: str
    tracking_url: str
    awb: Optional[str]
    products: List[str]
    created_at: str
    updated_at: str
    source: Optional[str]  # For Shopify fallback
    
    # Financial fields
    financial_status: Optional[str]
    fulfillment_status: Optional[str]
    total_price: Optional[str]
    currency: Optional[str]
    
    # Detailed items for pricing view
    line_items: Optional[List[Dict[str, Any]]]
    cancelled_at: Optional[str]


# Extended DTO for order status summary (includes fulfillment details)
class OrderStatusSummaryDTO(TypedDict, total=False):
    order_id: str
    channel_order_id: str
    status: str
    partner_status: str
    shipment_status: str
    customer_name: str
    fulfillment_status: str
    fulfilled_items: List[str]
    pending_items: List[str]
    courier: str
    tracking_url: str
    awb: Optional[str]
    etd_date: Optional[str]
    delivery_date: Optional[str]
    out_for_delivery_date: Optional[str]  # When order went out for delivery
    cancelled_at: Optional[str]
    products: List[str]
    created_at: str
    updated_at: str
    source: Optional[str]


# Extended DTO for delivery timeline (includes ETD and messaging)
class DeliveryTimelineDTO(TypedDict, total=False):
    order_id: str
    channel_order_id: str
    status: str
    partner_status: str
    shipment_status: str
    customer_name: str
    awb: Optional[str]
    etd_date: Optional[str]
    formatted_etd: Optional[str]
    message: str
    courier: str
    products: List[str]
    created_at: str
    updated_at: str
