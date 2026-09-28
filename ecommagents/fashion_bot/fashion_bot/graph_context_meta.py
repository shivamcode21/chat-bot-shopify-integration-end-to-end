"""
Context-Aware Graph for Fashion Bot.

Uses generic skill nodes with context-aware processing:
1. Automatically extracts entities and focal entity from tool calls
2. Maintains conversation_context for better routing and context awareness
3. Prompts are fetched from DB/cache instead of hardcoded
"""

from langgraph.graph import StateGraph, START, END
from fashion_bot.schema import SupportState
from fashion_bot.nodes import (
    detect_intent_node,
    data_collection_delivery_timeline,
    data_collection_return_policy,
    data_collection_discount,
    final_answer_node,
)
from fashion_bot.nodes.generic_skill_node import create_generic_skill_node
from fashion_bot.nodes.conversation_limit_gate import conversation_limit_gate
from fashion_bot.env_loader import get_bool
import logging
import os
import uuid
from typing import Dict, Any, List, cast, Mapping
from fashion_bot.database_manager import PostgresSaver
from fashion_bot.core.message_persistence import ensure_assistant_message_for_skip_final

# Ensure logs directory exists
logs_dir = 'logs'
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

# Configure logging with proper path — trace_id is injected by TraceIdFilter
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(trace_id)s] %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(logs_dir, 'fashion_bot_context_graph.log')),
        logging.StreamHandler()
    ]
)

# Install trace_id filter on root logger so ALL loggers get %(trace_id)s
from fashion_bot.trace_context import install_trace_filter
install_trace_filter()

# Suppress noisy library loggers — our own timing logs replace httpx request logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Suppress Pydantic V2 deprecation warnings from langchain
import warnings
warnings.filterwarnings("ignore", message=".*__fields__.*", category=DeprecationWarning)

logger = logging.getLogger("context_graph")

# Postgres checkpoint
checkpointer = PostgresSaver()


# ==================== GENERIC SKILL NODES ====================
# Create generic nodes using the factory pattern
# These fetch prompts from DB/cache and use conversation_context
# All nodes now use the generic pattern for consistent context handling

# ==================== PRODUCT-RELATED NODES ====================
generic_product_details_node = create_generic_skill_node(
    agent_name="product_details",
    topic="product_inquiry",
    entity_type="product",
    legacy_fields=["product_link", "inquiry_product_info", "product_selection_matches"]
)

# ==================== ORDER-RELATED NODES ====================
generic_order_status_node = create_generic_skill_node(
    agent_name="order_status",
    topic="order_status",
    entity_type="order",
    legacy_fields=["selected_order_id", "order_status_by_id", "known_orders"],
    # Auto-ground every order-status turn: get_recent_orders is pre-called and its
    # result injected before the model runs (OrderGroundingMiddleware), so the
    # agent always has the customer's real orders — status, ETA and tracking_url
    # — in front of it.
    #
    # Without this the prompt's LOGISTICS STATUS-BASED RESPONSE RULES can fire off
    # a status the model merely read in conversation history, with no order data
    # in the turn to fill the tracking link they promise. That is how the
    # 2026-08-24 Enamor reply reached a customer quoting an invented waybill:
    # "Please deliver this order" routed here, the only tool call was
    # annotate_order, and the model wrote the "Out for Delivery" script from the
    # previous turn's narrative.
    #
    # Kill-switch: set ORDER_GROUNDING_ENABLED=false to disable at runtime (the
    # agent then calls the tool itself, pre-PR behaviour).
    auto_ground_tool="get_recent_orders" if get_bool("ORDER_GROUNDING_ENABLED", True) else None,
)

generic_place_order_node = create_generic_skill_node(
    agent_name="place_order",
    topic="order_placement",
    entity_type="product",
    legacy_fields=["product_link", "inquiry_product_info", "customer_address", "customer_name", "phone_number"]
)

generic_cancel_or_update_order_node = create_generic_skill_node(
    agent_name="cancel_or_update_order",
    topic="order_modification",
    entity_type="order",
    legacy_fields=["selected_order_id"]
)

# ==================== CART NODE ====================
generic_cart_management_node = create_generic_skill_node(
    agent_name="cart_management",
    topic="cart_management",
    entity_type="cart",
    legacy_fields=[]
)

# ==================== DELIVERY NODES ====================
generic_delivery_timeline_node = create_generic_skill_node(
    agent_name="delivery_timeline",
    topic="delivery_inquiry",
    entity_type="order",
    legacy_fields=["selected_order_id"]
)

# ==================== RETURN/EXCHANGE NODES ====================
generic_return_exchange_node = create_generic_skill_node(
    agent_name="return_exchange",
    topic="return_exchange",
    entity_type="order",
    legacy_fields=["selected_order_id", "return_exchange_preference"]
)

# ==================== DISCOUNT NODES ====================
generic_discount_node = create_generic_skill_node(
    agent_name="discount",
    topic="discount_inquiry",
    entity_type="discount",
    legacy_fields=["wants_discount"]
)

# ==================== RECOMMENDATIONS NODE ====================
generic_recommendations_node = create_generic_skill_node(
    agent_name="recommendations",
    topic="product_recommendation",
    entity_type="product",
    legacy_fields=[],
    # Auto-ground every recommendation turn: the search tool is pre-called and its
    # results injected as a synthetic tool call before the model runs, so the agent
    # starts with fresh product matches in context (SearchGroundingMiddleware).
    # Kill-switch: set SEARCH_GROUNDING_ENABLED=false to disable grounding at
    # runtime (the agent then calls search_products itself, pre-PR behaviour).
    # Defaults to enabled, so behaviour is unchanged unless explicitly turned off.
    auto_ground_tool="search_products" if get_bool("SEARCH_GROUNDING_ENABLED", True) else None,
)

# ==================== POLICY NODES ====================
# Policy fact nodes force the grounding tool on the first turn so the LLM
# must read the policy from the DB before answering — this prevents
# ungrounded/hallucinated policy values (e.g. quoting a 7-day return window
# when the configured policy is 10 days).
generic_delivery_policy_node = create_generic_skill_node(
    agent_name="delivery_policy",
    topic="policy_inquiry",
    entity_type="policy",
    legacy_fields=[],
    grounding_tool="get_policy_information",
)

generic_payment_policy_node = create_generic_skill_node(
    agent_name="payment_policy",
    topic="policy_inquiry",
    entity_type="policy",
    legacy_fields=[],
    grounding_tool="get_policy_information",
)

generic_return_exchange_policy_node = create_generic_skill_node(
    agent_name="return_exchange_policy",
    topic="policy_inquiry",
    entity_type="policy",
    legacy_fields=[],
    grounding_tool="get_policy_information",
)

generic_vendor_inquiry_node = create_generic_skill_node(
    agent_name="vendor_inquiry",
    topic="vendor_inquiry",
    entity_type="vendor",
    legacy_fields=[]
)

# ==================== FEEDBACK/ESCALATION/UNKNOWN NODES ====================
generic_feedback_node = create_generic_skill_node(
    agent_name="feedback",
    topic="feedback",
    entity_type="feedback",
    legacy_fields=[]
)

generic_escalation_node = create_generic_skill_node(
    agent_name="escalation",
    topic="escalation",
    entity_type="escalation",
    legacy_fields=["callback_scheduled", "callback_time"]
)

generic_unknown_node = create_generic_skill_node(
    agent_name="unknown",
    topic="general",
    entity_type="general",
    legacy_fields=["parent_intent"]
)


# ==================== TRACE ID MANAGEMENT ====================
# Delegated to trace_context module — re-exported for backward compatibility
from fashion_bot.trace_context import generate_trace_id, set_trace_id  # noqa: F811

def get_trace_id(state: Mapping[str, Any]) -> str:
    """Get trace ID from state or generate new one."""
    if state is None:
        return generate_trace_id()
    if 'trace_id' in state and state['trace_id']:
        return state['trace_id']
    return generate_trace_id()


# ==================== LOGGING HELPERS ====================

def format_log_message(trace_id, state, message):
    """Trace ID auto-injected by TraceIdFilter. Just prefix with CONTEXT_GRAPH."""
    return f"[CONTEXT_GRAPH] {message}"

def log_node_execution(node_name: str, state: Mapping[str, Any], result: Dict[str, Any]):
    """Log node execution summary with trace ID (simplified - context details logged by skill node)."""
    trace_id = None
    if result is not None and isinstance(result, dict) and 'trace_id' in result:
        trace_id = result['trace_id']
    else:
        trace_id = get_trace_id(state)
    
    # Only log result summary - detailed context logging happens in skill nodes
    if result is not None:
        result_type = result.get('type', 'N/A')
        ctx = result.get('conversation_context') or {}
        entities_count = len(ctx.get("entities") or [])
        
        # For customer_message responses, show brief summary
        if 'customer_message' in result:
            logger.info(format_log_message(trace_id, state,
                f"[{node_name}] → {result_type}, entities={entities_count}, msg_len={len(result['customer_message'])}"))
        else:
            logger.info(format_log_message(trace_id, state, 
                f"[{node_name}] → {result_type}, entities={entities_count}"))
    elif state is None:
        logger.warning(format_log_message(trace_id, state, f"[{node_name}] State is None"))

def log_routing_decision(function_name: str, state: Mapping[str, Any], decision: str):
    """Log routing decision (single line)."""
    trace_id = get_trace_id(state)
    logger.info(format_log_message(trace_id, state, f"🔀 Route: {decision}"))


# ==================== NODE WRAPPERS WITH LOGGING ====================

async def detect_intent_node_with_logging(state: SupportState) -> Any:
    """Wrapper for detect_intent_node with logging."""
    if 'trace_id' not in state or not state['trace_id']:
        state['trace_id'] = generate_trace_id()

    trace_id = state['trace_id']
    logger.info(format_log_message(trace_id, state, f"🚀 Starting detect_intent_node"))

    result = await detect_intent_node(state)

    if isinstance(result, dict):
        result['trace_id'] = trace_id

    log_node_execution("detect_intent_node", state, result)
    return result

async def conversation_limit_gate_with_logging(state: SupportState) -> Any:
    """Wrapper for conversation_limit_gate with logging."""
    if 'trace_id' not in state or not state['trace_id']:
        state['trace_id'] = generate_trace_id()

    trace_id = state['trace_id']
    logger.info(format_log_message(trace_id, state, "🚦 Starting conversation_limit_gate"))

    result = await conversation_limit_gate(state)

    if isinstance(result, dict):
        result['trace_id'] = trace_id

    log_node_execution("conversation_limit_gate", state, result)
    return result

def data_collection_delivery_with_logging(state: SupportState) -> Any:
    """Wrapper for data_collection_delivery_timeline with logging."""
    trace_id = get_trace_id(state)
    result = data_collection_delivery_timeline(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("data_collection_delivery", state, result)
    return result

def data_collection_return_policy_with_logging(state: SupportState) -> Any:
    """Wrapper for data_collection_return_policy with logging."""
    trace_id = get_trace_id(state)
    result = data_collection_return_policy(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("data_collection_return_policy", state, result)
    return result

def data_collection_discount_with_logging(state: SupportState) -> Any:
    """Wrapper for data_collection_discount with logging."""
    trace_id = get_trace_id(state)
    result = data_collection_discount(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("data_collection_discount", state, result)
    return result

async def delivery_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC delivery_timeline node."""
    trace_id = get_trace_id(state)
    result = await generic_delivery_timeline_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("delivery_intent", state, result)
    return result

async def order_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC order_status node."""
    trace_id = get_trace_id(state)
    result = await generic_order_status_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("order_intent", state, result)
    return result

async def return_policy_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC return_exchange_policy node."""
    trace_id = get_trace_id(state)
    result = await generic_return_exchange_policy_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("return_policy_intent", state, result)
    return result

async def product_details_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC product_details node."""
    trace_id = get_trace_id(state)
    result = await generic_product_details_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("product_details_intent", state, result)
    return result

async def after_delivery_return_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC return_exchange node."""
    trace_id = get_trace_id(state)
    result = await generic_return_exchange_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("after_delivery_return_intent", state, result)
    return result

async def discount_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC discount node."""
    trace_id = get_trace_id(state)
    result = await generic_discount_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("discount_intent", state, result)
    return result

async def place_order_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC place_order node."""
    trace_id = get_trace_id(state)
    result = await generic_place_order_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("place_order_intent", state, result)
    return result

async def cancel_or_update_order_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC cancel_or_update_order node."""
    trace_id = get_trace_id(state)
    result = await generic_cancel_or_update_order_node(state)

    if isinstance(result, dict):
        result['trace_id'] = trace_id

    log_node_execution("cancel_or_update_order_intent (GENERIC)", state, result)
    return result

async def cart_management_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC cart_management node."""
    trace_id = get_trace_id(state)
    result = await generic_cart_management_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("cart_management_intent", state, result)
    return result

async def product_change_in_order_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC cancel_or_update_order node (handles product changes too)."""
    trace_id = get_trace_id(state)
    result = await generic_cancel_or_update_order_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("product_change_in_order_intent", state, result)
    return result

async def unknown_handler_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC unknown node."""
    trace_id = get_trace_id(state)
    result = await generic_unknown_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("unknown_handler", state, result)
    return result

async def delivery_policy_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC delivery_policy node."""
    trace_id = get_trace_id(state)
    result = await generic_delivery_policy_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("delivery_policy_intent", state, result)
    return result

async def payment_policy_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC payment_policy node."""
    trace_id = get_trace_id(state)
    result = await generic_payment_policy_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("payment_policy_intent", state, result)
    return result

async def return_exchange_policy_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC return_exchange_policy node."""
    trace_id = get_trace_id(state)
    result = await generic_return_exchange_policy_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("return_exchange_policy_intent", state, result)
    return result

async def vendor_inquiry_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC vendor_inquiry node."""
    trace_id = get_trace_id(state)
    result = await generic_vendor_inquiry_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("vendor_inquiry_intent", state, result)
    return result

async def recommendations_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC recommendations node."""
    trace_id = get_trace_id(state)
    result = await generic_recommendations_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("recommendations_intent", state, result)
    return result

async def feedback_intent_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC feedback node."""
    trace_id = get_trace_id(state)
    result = await generic_feedback_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("feedback_intent", state, result)
    return result

async def final_answer_with_logging(state: SupportState) -> Any:
    """Wrapper for final_answer_node."""
    trace_id = get_trace_id(state)
    skip_final = bool(state.get("_streaming_enabled") or state.get("_skip_final_answer"))
    if skip_final:
        result = ensure_assistant_message_for_skip_final(
            state_before_invoke=state,
            result=state,
        )
    else:
        result = await final_answer_node(state)

    if isinstance(result, dict):
        result['trace_id'] = trace_id

    log_node_execution("final_answer", state, result)
    return result

async def escalation_handler_with_logging(state: SupportState) -> Any:
    """Wrapper for GENERIC escalation node."""
    trace_id = get_trace_id(state)
    result = await generic_escalation_node(state)
    if isinstance(result, dict):
        result['trace_id'] = trace_id
    log_node_execution("escalation_handler", state, result)
    return result


# ==================== ROUTING FUNCTIONS ====================

def route_to_data_collection(state: Dict[str, Any]) -> str:
    """Route to appropriate data collection nodes based on detected intents."""
    detected_intents = state.get("detected_intents", [])
    has_unknown = state.get("has_unknown", False)
    
    # If active_return_exchange_flow is True, ALWAYS route to return/exchange intent
    active_return_exchange_flow = state.get("active_return_exchange_flow", False)
    if active_return_exchange_flow:
        decision = "after_delivery_return_intent"
        log_routing_decision("route_to_data_collection", state, decision)
        return decision

    if has_unknown:
        decision = "unknown_handler"
        log_routing_decision("route_to_data_collection", state, decision)
        return decision

    # Normalize detected_intents
    normalized_intents = []
    for item in detected_intents:
        if isinstance(item, dict):
            normalized_intents.append(item)
        elif isinstance(item, str):
            normalized_intents.append({"intent": item})

    for intent_data in normalized_intents:
        intent = intent_data.get("intent")
        if intent == "delivery_timeline_query" or intent == "delivery_timeline":
            decision = "data_collection_delivery"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "order_status":
            decision = "order_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "return_exchange_policy":
            decision = "data_collection_return_policy"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent in ("size_inquiry", "product_details"):
            decision = "product_details_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent in ("after_delivery_return_exchange", "return_exchange"):
            decision = "after_delivery_return_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "discount":
            decision = "data_collection_discount"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "place_order":
            decision = "place_order_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent in ("cancel_or_update_order", "cancel_or_update", "cancel_order"):
            decision = "cancel_or_update_order_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent in ("cart_management", "cart_modify", "cart_edit"):
            decision = "cart_management_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "product_change_in_order":
            decision = "product_change_in_order_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "vendor_inquiry":
            decision = "vendor_inquiry_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "category_details":
            decision = "product_details_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "recommendations":
            decision = "recommendations_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "customer_feedback":
            decision = "feedback_intent"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision
        elif intent == "escalation":
            decision = "escalation_handler"
            log_routing_decision("route_to_data_collection", state, decision)
            return decision

    decision = "unknown_handler"
    log_routing_decision("route_to_data_collection", state, decision)
    return decision

def route_data_collection_to_intent_or_final(state: Dict[str, Any]) -> str:
    """Route from data collection to intent handler or final answer."""
    skip_final = bool(state.get("_streaming_enabled") or state.get("_skip_final_answer"))
    response_type = state.get("response_type", "")
    node_type = state.get("type", "")

    if response_type == "intent_handle" or node_type == "intent_handle":
        pass  # proceed to intent routing below
    elif node_type == "customer_message":
        decision = "end" if skip_final else "final_answer"
        log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
        return decision

    if response_type == "intent_handle" or node_type == "intent_handle":
        detected_intents = state.get("detected_intents", [])
        
        normalized_intents = []
        for item in detected_intents:
            if isinstance(item, dict):
                normalized_intents.append(item)
            elif isinstance(item, str):
                normalized_intents.append({"intent": item})
        
        for intent_data in normalized_intents:
            intent = intent_data.get("intent")
            if intent == "delivery_timeline_query" or intent == "delivery_timeline":
                decision = "delivery_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "order_status":
                decision = "order_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "return_exchange_policy":
                decision = "return_policy_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent in ("size_inquiry", "product_details"):
                decision = "product_details_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent in ("after_delivery_return_exchange", "return_exchange"):
                decision = "after_delivery_return_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "discount":
                decision = "discount_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "place_order":
                decision = "place_order_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent in ("cancel_or_update_order", "cancel_or_update", "cancel_order"):
                decision = "cancel_or_update_order_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent in ("cart_management", "cart_modify", "cart_edit"):
                decision = "cart_management_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "product_change_in_order":
                decision = "product_change_in_order_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "vendor_inquiry":
                decision = "vendor_inquiry_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "category_details":
                decision = "product_details_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "recommendations":
                decision = "recommendations_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "delivery_policy":
                decision = "delivery_policy_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision
            elif intent == "payment_policy":
                decision = "payment_policy_intent"
                log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
                return decision

    decision = "end" if skip_final else "final_answer"
    log_routing_decision("route_data_collection_to_intent_or_final", state, decision)
    return decision

def route_to_data_collection_with_escalation(state: Dict[str, Any]) -> str:
    """Route with escalation check."""
    if state.get("callback_scheduled"):
        decision = "escalation_handler"
        log_routing_decision("route_to_data_collection_with_escalation", state, decision)
        return decision
    
    detected_intents = state.get("detected_intents", [])
    
    normalized_intents = []
    for item in detected_intents:
        if isinstance(item, dict):
            normalized_intents.append(item)
        elif isinstance(item, str):
            normalized_intents.append({"intent": item})
    
    for intent_data in normalized_intents:
        intent = intent_data.get("intent")
        if intent == "delivery_policy":
            decision = "delivery_policy_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        elif intent == "payment_policy":
            decision = "payment_policy_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        elif intent == "vendor_inquiry":
            decision = "vendor_inquiry_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        elif intent == "category_details":
            decision = "product_details_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        elif intent == "recommendations":
            decision = "recommendations_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        elif intent == "return_exchange_policy":
            decision = "return_exchange_policy_intent"
            log_routing_decision("route_to_data_collection_with_escalation", state, decision)
            return decision
        
    actionable_intents = {"order_status", "place_order", "cart_management", "product_details", "discount", "delivery_timeline_query", "delivery_timeline", "product_change_in_order"}

    if state.get("callback_time"):
        decision = "escalation_handler"
        log_routing_decision("route_to_data_collection_with_escalation", state, decision)
        return decision

    detected_intent_strings = set()
    for item in detected_intents:
        if isinstance(item, dict) and "intent" in item:
            detected_intent_strings.add(item["intent"])
        elif isinstance(item, str):
            detected_intent_strings.add(item)
    
    if (state.get("needs_escalation") or state.get("is_frustrated")) and not (detected_intent_strings & actionable_intents):
        decision = "escalation_handler"
        log_routing_decision("route_to_data_collection_with_escalation", state, decision)
        return decision
    
    return route_to_data_collection(state)

def route_data_collection_to_intent_or_final_with_escalation(state: Dict[str, Any]) -> str:
    """Route with escalation check from data collection."""
    if state.get("waiting_for_order_confirmation"):
        decision = "end" if bool(state.get("_streaming_enabled") or state.get("_skip_final_answer")) else "final_answer"
        log_routing_decision("route_data_collection_to_intent_or_final_with_escalation", state, decision)
        return decision
    
    actionable_intents = {"order_status", "place_order", "cart_management", "product_details", "discount", "delivery_timeline_query", "delivery_timeline", "return_exchange_policy", "size_inquiry", "product_change_in_order"}
    detected_intents = {d.get("intent") for d in state.get("detected_intents", []) if isinstance(d, dict)}
    
    if detected_intents & actionable_intents:
        log_routing_decision("route_data_collection_to_intent_or_final_with_escalation", state, "intent_handler")
        return route_data_collection_to_intent_or_final(state)
    
    if state.get("needs_escalation") or state.get("is_frustrated"):
        decision = "escalation_handler"
        log_routing_decision("route_data_collection_to_intent_or_final_with_escalation", state, decision)
        return decision
    
    return route_data_collection_to_intent_or_final(state)


def route_intent_to_final_or_end(state: Dict[str, Any]) -> str:
    """For streaming web mode, bypass final_answer and end directly."""
    streaming_enabled = bool(state.get("_streaming_enabled"))
    skip_flag = bool(state.get("_skip_final_answer"))
    if streaming_enabled or skip_flag:
        decision = "end"
    else:
        decision = "final_answer"
    log_routing_decision("route_intent_to_final_or_end", state, decision)
    return decision


def route_after_limit_gate(state: Dict[str, Any]) -> str:
    """Route around the agent graph when the conversation message cap is hit."""
    decision = "end" if state.get("conversation_limit_reached") else "detect_intent"
    log_routing_decision("route_after_limit_gate", state, decision)
    return decision


# ==================== BUILD THE GRAPH ====================

builder = StateGraph(SupportState)

# Add nodes with logging
builder.add_node("conversation_limit_gate", conversation_limit_gate_with_logging)
builder.add_node("detect_intent", detect_intent_node_with_logging)

# Data collection nodes
builder.add_node("data_collection_delivery", data_collection_delivery_with_logging)
builder.add_node("data_collection_return_policy", data_collection_return_policy_with_logging)
builder.add_node("data_collection_discount", data_collection_discount_with_logging)

# Intent handler nodes
builder.add_node("delivery_intent", delivery_intent_with_logging)
builder.add_node("order_intent", order_intent_with_logging)
builder.add_node("return_policy_intent", return_policy_intent_with_logging)
builder.add_node("product_details_intent", product_details_intent_with_logging)  # Uses GENERIC node
builder.add_node("after_delivery_return_intent", after_delivery_return_intent_with_logging)
builder.add_node("discount_intent", discount_intent_with_logging)
builder.add_node("place_order_intent", place_order_intent_with_logging)
builder.add_node("cancel_or_update_order_intent", cancel_or_update_order_intent_with_logging)
builder.add_node("cart_management_intent", cart_management_intent_with_logging)
builder.add_node("product_change_in_order_intent", product_change_in_order_intent_with_logging)

# Policy intent handler nodes
builder.add_node("delivery_policy_intent", delivery_policy_intent_with_logging)
builder.add_node("payment_policy_intent", payment_policy_intent_with_logging)
builder.add_node("return_exchange_policy_intent", return_exchange_policy_intent_with_logging)
builder.add_node("vendor_inquiry_intent", vendor_inquiry_intent_with_logging)
builder.add_node("feedback_intent", feedback_intent_with_logging)
builder.add_node("recommendations_intent", recommendations_intent_with_logging)

# Unknown handler
builder.add_node("unknown_handler", unknown_handler_with_logging)

# Final answer node
builder.add_node("final_answer", final_answer_with_logging)

# Escalation handler node
builder.add_node("escalation_handler", escalation_handler_with_logging)

# Add edges
# Entry gate: enforce the per-conversation user-message cap before any agent
# work. Under the limit it falls through to detect_intent as before; over the
# limit it routes straight to END with a canned reply (no intent/skill/LLM run).
builder.add_edge(START, "conversation_limit_gate")
builder.add_conditional_edges(
    "conversation_limit_gate",
    route_after_limit_gate,
    {
        "detect_intent": "detect_intent",
        "end": END,
    },
)

# Detect intent -> Route to appropriate handler
builder.add_conditional_edges(
    "detect_intent",
    route_to_data_collection_with_escalation,
    {
        "escalation_handler": "escalation_handler",
        "data_collection_delivery": "data_collection_delivery",
        "order_intent": "order_intent",
        "data_collection_return_policy": "data_collection_return_policy",
        "product_details_intent": "product_details_intent",
        "after_delivery_return_intent": "after_delivery_return_intent",
        "data_collection_discount": "data_collection_discount",
        "place_order_intent": "place_order_intent",
        "cancel_or_update_order_intent": "cancel_or_update_order_intent",
        "cart_management_intent": "cart_management_intent",
        "product_change_in_order_intent": "product_change_in_order_intent",
        "delivery_policy_intent": "delivery_policy_intent",
        "payment_policy_intent": "payment_policy_intent",
        "vendor_inquiry_intent": "vendor_inquiry_intent",
        "return_exchange_policy_intent": "return_exchange_policy_intent",
        "recommendations_intent": "recommendations_intent",
        "feedback_intent": "feedback_intent",
        "unknown_handler": "unknown_handler",
    },
)

# Data collection nodes -> Route to intent handler or final answer
for node in [
    "data_collection_delivery",
    "data_collection_return_policy",
    "data_collection_discount",
]:
    builder.add_conditional_edges(
        node,
        route_data_collection_to_intent_or_final_with_escalation,
        {
            "escalation_handler": "escalation_handler",
            "delivery_intent": "delivery_intent",
            "order_intent": "order_intent",
            "return_policy_intent": "return_policy_intent",
            "product_details_intent": "product_details_intent",
            "after_delivery_return_intent": "after_delivery_return_intent",
            "discount_intent": "discount_intent",
            "place_order_intent": "place_order_intent",
            "cancel_or_update_order_intent": "cancel_or_update_order_intent",
            "cart_management_intent": "cart_management_intent",
            "product_change_in_order_intent": "product_change_in_order_intent",
            "delivery_policy_intent": "delivery_policy_intent",
            "payment_policy_intent": "payment_policy_intent",
            "return_exchange_policy_intent": "return_exchange_policy_intent",
            "vendor_inquiry_intent": "vendor_inquiry_intent",
            "final_answer": "final_answer",
            "end": END,
        },
    )

# Intent/policy/escalation/unknown nodes -> Final answer (or End in streaming mode)
for node in [
    "delivery_intent",
    "order_intent",
    "return_policy_intent",
    "product_details_intent",
    "after_delivery_return_intent",
    "discount_intent",
    "place_order_intent",
    "cancel_or_update_order_intent",
    "cart_management_intent",
    "product_change_in_order_intent",
    "delivery_policy_intent",
    "payment_policy_intent",
    "return_exchange_policy_intent",
    "vendor_inquiry_intent",
    "feedback_intent",
    "recommendations_intent",
    "unknown_handler",
    "escalation_handler",
]:
    builder.add_conditional_edges(
        node,
        route_intent_to_final_or_end,
        {
            "final_answer": "final_answer",
            "end": END,
        },
    )

# Final answer -> End
builder.add_edge("final_answer", END)

# Compile the graph
graph = builder.compile()

# Note: Agent phone number is now loaded lazily via get_agent_phone_number()
# No startup DB query needed - will be loaded on first use
