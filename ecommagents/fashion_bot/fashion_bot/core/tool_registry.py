"""
Tool Registry for Generic Skill Nodes.

Maps agent names to their corresponding tool factory functions.
This enables the generic skill node to dynamically load the correct tools
based on the agent/skill being executed.

Prompt Naming Convention:
- All prompts in DB use format: {agent_name}_handler
- Example: product_details -> product_details_handler

Performance Optimization:
- Factory functions are cached at module level after first import
- FACTORY_MAP is built once and reused across all calls
- This avoids repeated import overhead on each tool loading call
"""

import inspect
import logging
from re import search
from typing import List, Dict, Any, Callable, Optional

logger = logging.getLogger("tool_registry")


# ==================== CACHED FACTORY MAP ====================
# Cached reference to avoid repeated imports
_FACTORY_MAP_CACHE: Optional[Dict[str, Callable]] = None


def _get_factory_map() -> Dict[str, Callable]:
    """
    Get the factory map with cached imports.
    
    This function imports factory functions once and caches them.
    Subsequent calls return the cached map without re-importing.
    
    Returns:
        Dict mapping factory names to factory functions
    """
    global _FACTORY_MAP_CACHE
    
    if _FACTORY_MAP_CACHE is not None:
        return _FACTORY_MAP_CACHE
    
    # Import all factory functions once
    from fashion_bot.tool_factory import (
        product_details_tools_factory,
        order_status_tools_factory,
        return_exchange_tools_factory,
        place_order_tools_factory,
        cancel_or_update_tools_factory,
        cart_management_tools_factory,
    )

    # Build and cache the factory map
    _FACTORY_MAP_CACHE = {
        "product_details_tools_factory": product_details_tools_factory,
        "order_status_tools_factory": order_status_tools_factory,
        "return_exchange_tools_factory": return_exchange_tools_factory,
        "place_order_tools_factory": place_order_tools_factory,
        "cancel_or_update_tools_factory": cancel_or_update_tools_factory,
        "cart_management_tools_factory": cart_management_tools_factory,
        # Helper factories (defined in this file)
        "delivery_tools_factory": lambda **kwargs: _get_delivery_tools(**kwargs),
        "discount_tools_factory": lambda **kwargs: _get_discount_tools(**kwargs),
        "recommendations_tools_factory": lambda **kwargs: _get_recommendations_tools(**kwargs),
        "policy_tools_factory": lambda **kwargs: _get_policy_tools(**kwargs),
        "return_exchange_policy_tools_factory": lambda **kwargs: _get_return_exchange_policy_tools(**kwargs),
        "feedback_tools_factory": lambda **kwargs: _get_feedback_tools(**kwargs),
        "escalation_tools_factory": lambda **kwargs: _get_escalation_tools(**kwargs),
    }
    
    logger.debug("factory map initialized")
    return _FACTORY_MAP_CACHE


# ==================== TOOL REGISTRY ====================
# Maps agent_name to (tool_factory_function_name, required_params)
# The params indicate what additional arguments the factory needs
#
# Special values:
# - factory="no_tools": Agent doesn't use tools, just LLM
# - factory="policy_tools_factory": Shared factory for policy handlers

TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    # ==================== PRODUCT-RELATED AGENTS ====================
    "product_details": {
        "factory": "product_details_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "product_inquiry",
        "entity_type": "product",
        "prompt_name": "product_details_handler"
    },
    # ==================== ORDER-RELATED AGENTS ====================
    "order_status": {
        "factory": "order_status_tools_factory",
        "params": ["state", "messages_list"],
        "topic": "order_status",
        "entity_type": "order",
        "prompt_name": "order_status_handler"
    },
    "place_order": {
        "factory": "place_order_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "order_placement",
        "entity_type": "product",
        "prompt_name": "place_order_handler"
    },
    "cancel_or_update_order": {
        "factory": "cancel_or_update_tools_factory",
        "params": ["state", "messages_list"],
        "topic": "order_modification",
        "entity_type": "order",
        "prompt_name": "cancellation_handler"
    },
    "cart_management": {
        "factory": "cart_management_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "cart_management",
        "entity_type": "cart",
        "prompt_name": "cart_management_handler"
    },
    
    # ==================== RETURN/EXCHANGE AGENTS ====================
    "return_exchange": {
        "factory": "return_exchange_tools_factory",
        "params": ["state", "messages_list"],
        "topic": "return_exchange",
        "entity_type": "order",
        "prompt_name": "return_exchange_handler"
    },
    
    # ==================== DELIVERY AGENTS ====================
    "delivery_timeline": {
        "factory": "delivery_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "delivery_inquiry",
        "entity_type": "order",
        "prompt_name": "delivery_timeline_handler"
    },
    
    # ==================== DISCOUNT AGENTS ====================
    "discount": {
        "factory": "discount_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "discount_inquiry",
        "entity_type": "discount",
        "prompt_name": "discount_handler"
    },
    
    # ==================== RECOMMENDATIONS AGENT ====================
    "recommendations": {
        "factory": "recommendations_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "product_recommendation",
        "entity_type": "product",
        "prompt_name": "recommendations_handler"
    },
    
    # ==================== POLICY AGENTS (Shared tools) ====================
    "delivery_policy": {
        "factory": "policy_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "policy_inquiry",
        "entity_type": "policy",
        "prompt_name": "policy_handler"
    },
    "payment_policy": {
        "factory": "policy_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "policy_inquiry",
        "entity_type": "policy",
        "prompt_name": "policy_handler"
    },
    "return_exchange_policy": {
        # Uses specialized factory with full return/exchange toolset
        # Consolidated from legacy return_policy_handler for unified policy handling
        "factory": "return_exchange_policy_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "policy_inquiry",
        "entity_type": "policy",
        "prompt_name": "policy_handler"
    },
    "vendor_inquiry": {
        "factory": "policy_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "vendor_inquiry",
        "entity_type": "vendor",
        "prompt_name": "policy_handler"
    },
    
    # ==================== FEEDBACK/ESCALATION/UNKNOWN AGENTS ====================
    # These agents primarily use LLM reasoning without specialized tools
    "feedback": {
        "factory": "feedback_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "feedback",
        "entity_type": "feedback",
        "prompt_name": "feedback_handler"
    },
    "escalation": {
        "factory": "escalation_tools_factory",
        "params": ["state", "messages_list", "client_id"],
        "topic": "escalation",
        "entity_type": "escalation",
        "prompt_name": "escalation_handler"
    },
    "unknown": {
        "factory": "no_tools",
        "params": ["state", "messages_list", "client_id"],
        "topic": "general",
        "entity_type": "general",
        "prompt_name": "unknown_handler"
    },
}


def prewarm_factory_cache() -> None:
    """
    Pre-warm the factory cache by importing all factory functions.
    
    Call this at application startup to avoid import latency on first request.
    This is optional but recommended for production deployments.
    
    Example:
        ```python
        # In your app startup (e.g., main.py or wsgi.py)
        from fashion_bot.core.tool_registry import prewarm_factory_cache
        prewarm_factory_cache()
        ```
    """
    logger.info("🔥 Pre-warming tool factory cache...")
    _get_factory_map()
    logger.info("✅ Tool factory cache pre-warmed")


def get_registry_entry(agent_name: str) -> Optional[Dict[str, Any]]:
    """
    Get the registry entry for an agent.
    
    Args:
        agent_name: Name of the agent (e.g., "product_details", "order_status")
        
    Returns:
        Registry entry dict with factory name, params, topic, entity_type
        None if agent not found
    """
    return TOOL_REGISTRY.get(agent_name)


def _append_shared_contact_tool(tools: List[Any], state: dict) -> List[Any]:
    """Ensure every agent exposes the shared support-contact tool exactly once.

    Attaching ``get_contact_information`` centrally (instead of in each factory)
    means any agent that deflects a customer to support — recommendations, cart,
    general / fallback, etc. — can fetch the tenant's real contact details rather
    than fabricating them (the cause of the hallucinated wrong-brand support
    contact incident). Idempotent: a no-op if the tool is already present.
    """
    try:
        if any(getattr(t, "name", None) == "get_contact_information" for t in tools):
            return tools
        from fashion_bot.tool_factory import _create_contact_information_tool
        return [*tools, _create_contact_information_tool(state)]
    except Exception as e:
        logger.warning(f"⚠️ Could not attach shared contact tool: {e}")
        return tools


def get_tools_for_agent(
    agent_name: str,
    state: dict,
    messages_list: list,
    client_id: str,
    **extra_params
) -> List[Any]:
    """
    Dynamically get tools for an agent from the registry.
    
    This function looks up the tool factory for the given agent and calls it
    with the appropriate parameters.
    
    Performance: Uses cached factory map to avoid repeated imports.
    
    Args:
        agent_name: Name of the agent to get tools for
        state: Current SupportState
        messages_list: List of conversation messages
        client_id: Client ID for multi-tenant support
        **extra_params: Additional parameters (unused after ref removal, kept for API compat)
    
    Returns:
        List of tools for the agent (empty list for "no_tools" agents)
        
    Raises:
        ValueError: If agent_name not found in registry
    """
    entry = TOOL_REGISTRY.get(agent_name)
    if not entry:
        logger.error(f"❌ Agent '{agent_name}' not found in tool registry")
        raise ValueError(f"Agent '{agent_name}' not found in tool registry. "
                        f"Available agents: {list(TOOL_REGISTRY.keys())}")
    
    factory_name = entry["factory"]
    
    # Special case: agent doesn't use tools — still expose the shared contact
    # tool so a general/fallback turn can surface real support details.
    if factory_name == "no_tools":
        logger.debug(f"'{agent_name}' LLM-only, contact tool only")
        return _append_shared_contact_tool([], state)

    # Get cached factory map (imports only happen once)
    FACTORY_MAP = _get_factory_map()

    factory_func = FACTORY_MAP.get(factory_name)

    if not factory_func:
        logger.warning(f"⚠️ Factory '{factory_name}' not found, returning empty tools list")
        return _append_shared_contact_tool([], state)
    
    # Build kwargs based on required params
    required_params = entry["params"]
    kwargs = {}
    
    param_sources = {
        "state": state,
        "messages_list": messages_list,
        "client_id": client_id,
    }
    
    for param in required_params:
        if param in param_sources:
            kwargs[param] = param_sources[param]
        else:
            logger.warning(f"⚠️ Parameter '{param}' not found for factory '{factory_name}'")
    
    try:
        tools = factory_func(**kwargs)
        if inspect.isawaitable(tools):
            # An async factory (e.g. _get_escalation_tools) cannot run on this
            # sync path — swallowing the coroutine would silently strip the
            # agent's tools down to contact-only. Fail loudly and point the
            # caller at the async variant.
            tools.close()
            logger.error(
                f"❌ Factory '{factory_name}' for agent '{agent_name}' is async — "
                f"use aget_tools_for_agent() instead of get_tools_for_agent(). "
                f"Falling back to contact-only tools."
            )
            return _append_shared_contact_tool([], state)
        tools = _append_shared_contact_tool(tools, state)
        logger.debug(f"loaded {len(tools)} tools for '{agent_name}'")
        return tools
    except Exception as e:
        logger.error(f"❌ Error loading tools for agent '{agent_name}': {e}")
        logger.warning(f"⚠️ Falling back to contact-only tools list for '{agent_name}'")
        return _append_shared_contact_tool([], state)


async def aget_tools_for_agent(
    agent_name: str,
    state: dict,
    messages_list: list,
    client_id: str,
    **extra_params
) -> List[Any]:
    """
    Async variant of ``get_tools_for_agent`` for native async graph execution.

    Factory functions may stay sync for now, but async factories are awaited
    directly instead of being bridged through a thread pool.
    """
    entry = TOOL_REGISTRY.get(agent_name)
    if not entry:
        logger.error(f"❌ Agent '{agent_name}' not found in tool registry")
        raise ValueError(
            f"Agent '{agent_name}' not found in tool registry. "
            f"Available agents: {list(TOOL_REGISTRY.keys())}"
        )

    factory_name = entry["factory"]
    if factory_name == "no_tools":
        logger.debug(f"'{agent_name}' LLM-only, contact tool only")
        return _append_shared_contact_tool([], state)

    factory_map = _get_factory_map()
    factory_func = factory_map.get(factory_name)
    if not factory_func:
        logger.warning(f"⚠️ Factory '{factory_name}' not found, returning empty tools list")
        return _append_shared_contact_tool([], state)

    required_params = entry["params"]
    kwargs = {}
    param_sources = {
        "state": state,
        "messages_list": messages_list,
        "client_id": client_id,
    }

    for param in required_params:
        if param in param_sources:
            kwargs[param] = param_sources[param]
        else:
            logger.warning(f"⚠️ Parameter '{param}' not found for factory '{factory_name}'")

    try:
        tools = factory_func(**kwargs)
        if inspect.isawaitable(tools):
            tools = await tools
        tools = _append_shared_contact_tool(tools, state)
        logger.debug(f"loaded {len(tools)} tools for '{agent_name}'")
        return tools
    except Exception as e:
        logger.error(f"❌ Error loading tools for agent '{agent_name}': {e}")
        logger.warning(f"⚠️ Falling back to contact-only tools list for '{agent_name}'")
        return _append_shared_contact_tool([], state)


# ==================== TOOL FACTORY HELPERS ====================
# These helper functions create tools for agents that don't have dedicated factories yet

def _convert_mcp_to_langchain(mcp_tool, state: Optional[dict] = None):
    """
    Convert an MCP tool to LangChain-compatible format.
    
    MCP tools are wrapped objects with a .fn attribute containing the actual function.
    LangChain's AgentExecutor expects BaseTool objects, not raw functions.
    We need to wrap the function with LangChain's @tool decorator.
    
    If state is provided, it's injected into the tool function if it expects it.
    Uses functools.partial to preserve a clean signature for Gemini/OpenAI schemas.
    """
    from langchain_core.tools import tool as langchain_tool
    from langchain_core.tools.base import BaseTool
    import inspect
    from functools import partial, update_wrapper
    
    # If already a LangChain BaseTool, return as-is
    if isinstance(mcp_tool, BaseTool):
        return mcp_tool
    
    if hasattr(mcp_tool, 'fn'):
        # It's an MCP tool, extract the underlying function
        fn = mcp_tool.fn
    else:
        # Assume it's a raw function
        fn = mcp_tool
        
    # Get function signature to check if it expects 'state'
    try:
        sig = inspect.signature(fn)
        expects_state = 'state' in sig.parameters
    except (ValueError, TypeError):
        expects_state = False
        
    if expects_state and state is not None:
        # Use partial to bind the state argument
        # This keeps the signature clean for the LLM (excludes 'state' from schema)
        p_fn = partial(fn, state=state)
        # update_wrapper is critical: it copies __name__, __doc__, etc. from fn to p_fn
        # and also helps inspect.signature() show the correct remaining parameters
        update_wrapper(p_fn, fn)
        return langchain_tool(p_fn)
    
    # If no state injection needed, just wrap original
    return langchain_tool(fn)


def _get_delivery_tools(state, messages_list, client_id) -> List:
    """
    Get tools for delivery timeline agent.
    
    Uses shared tools from tool_factory plus delivery-specific MCP tools:
    - Domain/URL validation
    - Product search and details (via _create_product_search_tools)
    - Order details (via _create_get_order_details_tool)
    - Recent orders (via _create_get_recent_orders_tool)
    - Delivery estimates
    - Product availability

    ``get_recent_orders`` is the ONLY tool that resolves a customer's orders from
    their phone number, and "when will my order arrive?" is asked far more often
    without an order ID than with one. Without it bound here the node had no
    phone → order path at all: the model would call it anyway (the shared prompt
    and ``get_order_details``' own docstring tell it to), get back
    "get_recent_orders is not a valid tool", and fall back to passing the
    customer's phone number as ``order_id`` — answering "I couldn't find an
    active order associated with your phone number" for a customer who had one.
    ``enrich_eta=True`` matches this agent's purpose: it lets the model fetch the
    courier ETA for dispatched orders, which is exactly what a delivery-timing
    question is asking for.
    """
    from fashion_bot.tool_factory import (
        _create_product_search_tools,
        _create_get_order_details_tool,
        _create_get_recent_orders_tool,
        _create_cart_tools,
    )
    from fashion_bot.tools import (
        is_url_in_valid_domain_tool,
        get_delivery_estimate_tool_enhanced,
        check_product_availability_tool,
    )

    search_products, find_product_by_url, find_product_by_id = _create_product_search_tools(state, client_id)
    get_order_details = _create_get_order_details_tool(state)
    get_recent_orders = _create_get_recent_orders_tool(state, enrich_eta=True)
    cart_tools = _create_cart_tools(state, include_writes=False)

    mcp_tools = [
        is_url_in_valid_domain_tool,
        get_delivery_estimate_tool_enhanced,
        check_product_availability_tool,
    ]

    return [_convert_mcp_to_langchain(tool, state=state) for tool in mcp_tools] + [
        search_products, find_product_by_url, find_product_by_id,
        get_order_details, get_recent_orders,
        *cart_tools,
    ]


def _get_discount_tools(state, messages_list, client_id) -> List:
    """Get tools for discount agent."""
    from langchain_core.tools import tool
    from fashion_bot.config_manager import aget_discount_coupons, aget_additional_discounts, aget_payment_offers
    
    @tool
    async def get_discount_information() -> dict:
        """Get available discount codes, coupons, and payment offers."""
        try:
            # Fetch all three types of discount information
            discount_coupons = await aget_discount_coupons(client_id)
            additional_discounts = await aget_additional_discounts(client_id)
            payment_offers = await aget_payment_offers(client_id)
            
            # Combine all discount information
            combined_discounts = {
                "discount_coupons": discount_coupons,
                "additional_discounts": additional_discounts,
                "payment_offers": payment_offers
            }
            
            return {
                "success": True,
                "discounts": combined_discounts
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    @tool
    async def get_sales_policy() -> dict:
        """Get sales policy information."""
        from fashion_bot.core.orchestrator import UtilityOrchestrator
        return await UtilityOrchestrator.get_policy_config("conduct_sales", state=state)
    
    @tool
    async def get_repeated_discount_message() -> dict:
        """Get repeated discount request configuration from database. Returns full config JSON with message, tone, etc."""
        from fashion_bot.config_manager import aget_config
        import json
        
        try:
            config_value = await aget_config('repeated_discount_request', client_id=client_id)
            
            if config_value:
                if isinstance(config_value, str):
                    config_data = json.loads(config_value)
                else:
                    config_data = config_value
                
                if config_data:
                    return config_data
        except Exception as e:
            logger.error(f"Error fetching repeated discount message: {e}")
        
        return {"message": "We appreciate your interest! The current offers we have are the best we can provide at this time."}

    # NOTE: get_contact_information is attached centrally to every agent in
    # get_tools_for_agent / aget_tools_for_agent — do not re-add it here.
    # escalate_to_agent lets a bulk / wholesale / B2B discount request hand off
    # to staff; routed to routes["discount"] and grouped as pre_sales (design §5.4a).
    from fashion_bot.tool_factory import _create_cart_tools, _create_escalation_tool
    cart_tools = _create_cart_tools(state, include_writes=False)
    escalate_to_agent = _create_escalation_tool(state, agent="discount")
    return [get_discount_information, get_sales_policy, get_repeated_discount_message, escalate_to_agent, *cart_tools]


def _get_recommendations_tools(state, messages_list, client_id) -> List:
    """Get tools for recommendations agent."""
    from fashion_bot.tool_factory import _create_product_search_tools, _create_cart_tools, _create_nearest_store_tool

    search_products, find_product_by_url, find_product_by_id = _create_product_search_tools(state, client_id)
    cart_tools = _create_cart_tools(state, include_writes=True)
    get_nearest_store = _create_nearest_store_tool(state, client_id)

    return [search_products, find_product_by_url, find_product_by_id, get_nearest_store, *cart_tools]


def _get_policy_tools(state, messages_list, client_id) -> List:
    """Get tools for policy inquiry agents."""
    from langchain_core.tools import tool
    
    # Map short policy types to actual config keys in client_configs table
    POLICY_KEY_MAPPING = {
        "return_exchange": "return_exchange_policy",
        "delivery": "delivery_policy",
        "payment": "payment_policy",
        "return_exchange_policy": "return_exchange_policy",
        "delivery_policy": "delivery_policy",
        "payment_policy": "payment_policy",
        "refund": "return_exchange_policy",
        "sales": "sales_policy",
        "contact": "vendor_inquiry",
        "vendor": "vendor_inquiry", 
        "vendor_inquiry": "vendor_inquiry"
    }
    
    @tool
    async def get_policy_information(policy_type: str) -> dict:
        """
        Get policy information.
        policy_type can be: delivery, payment, return_exchange, refund, sales, vendor
        """
        from fashion_bot.core.orchestrator import UtilityOrchestrator
        # Map to actual config key
        config_key = POLICY_KEY_MAPPING.get(policy_type, f"{policy_type}_policy")
        return await UtilityOrchestrator.get_policy_config(config_key, state=state)
    
    from fashion_bot.tool_factory import _create_vendor_information_tool, _create_nearest_store_tool
    get_vendor_information = _create_vendor_information_tool(client_id)
    get_nearest_store = _create_nearest_store_tool(state, client_id)

    # NOTE: get_contact_information is attached centrally to every agent in
    # get_tools_for_agent / aget_tools_for_agent — do not re-add it here.
    return [get_policy_information, get_vendor_information, get_nearest_store]


def _get_return_exchange_policy_tools(state, messages_list, client_id) -> List:
    """
    Get specialized tools for return/exchange policy agent.
    
    This includes all the tools needed for handling return and exchange inquiries:
    - get_policy_information: Generic policy lookup (supports return_exchange, delivery, payment, etc.)
      For return/exchange types, also fetches the process details from after_delivery_return_exchange config.
    - find_product_by_url: Shared product lookup from _create_product_search_tools
    - get_order_details: Shared order details from _create_get_order_details_tool
    """
    from langchain_core.tools import tool
    from fashion_bot.tool_factory import _create_product_search_tools, _create_get_order_details_tool
    
    search_products, find_product_by_url, find_product_by_id = _create_product_search_tools(state, client_id)
    get_order_details = _create_get_order_details_tool(state)

    RETURN_EXCHANGE_POLICY_TYPES = frozenset({
        "return_exchange", "return_exchange_policy", "refund",
    })
    
    @tool
    async def get_policy_information(policy_type: str) -> dict:
        """
        Get general policy information.
        policy_type can be: delivery, payment, return_exchange, refund, sales, vendor
        """
        from fashion_bot.core.orchestrator import UtilityOrchestrator

        POLICY_KEY_MAPPING = {
            "return_exchange": "return_exchange_policy",
            "delivery": "delivery_policy",
            "payment": "payment_policy",
            "return_exchange_policy": "return_exchange_policy",
            "refund": "return_exchange_policy",
        }
        config_key = POLICY_KEY_MAPPING.get(policy_type, f"{policy_type}_policy")
        result = await UtilityOrchestrator.get_policy_config(config_key, state=state)

        if policy_type in RETURN_EXCHANGE_POLICY_TYPES:
            process_result = await UtilityOrchestrator.get_policy_config(
                "after_delivery_return_exchange", state=state
            )
            if process_result.get("success") and process_result.get("policy_data"):
                result["return_exchange_process"] = process_result["policy_data"]

        return result
    
    return [
        search_products,
        find_product_by_url,
        find_product_by_id,
        get_order_details,
        get_policy_information
    ]


def _get_feedback_tools(state, messages_list, client_id) -> List:
    """Get tools for feedback agent."""
    from langchain_core.tools import tool
    
    @tool
    async def log_customer_feedback(feedback_type: str, feedback_text: str) -> dict:
        """Log customer feedback. feedback_type can be: complaint, suggestion, compliment"""
        from fashion_bot.history.conversation_handler import astore_conversation_event
        try:
            await astore_conversation_event(
                phone_number=state.get("phone_number"),
                event_type=f"feedback_{feedback_type}",
                event_data={"feedback": feedback_text}
            )
            return {"success": True, "message": "Feedback recorded successfully"}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    return [log_customer_feedback]


async def _get_escalation_tools(state, messages_list, client_id) -> List:
    """Get tools for the escalation agent.

    Base set: order lookup + ``escalate_to_agent`` (the shared
    ``get_contact_information`` is attached centrally by the dispatcher — do not
    re-add it here).

    Resolution-first set (**on by default**; a client can opt out with
    ``escalation_policy.resolution_first_tools = false``): the node ALSO gets a
    **read-only** resolution toolset — policy answers, product search, delivery-
    partner info — so the resolution-first ``escalation_handler`` prompt can
    actually resolve / de-escalate before handing off. Without these the prompt's
    "attempt resolution first" step is unexecutable: the node can only look up an
    order or escalate. No mutating tools are added here; ``escalate_to_agent``
    stays as the last-resort actuator.
    """
    from fashion_bot.tool_factory import (
        _create_escalation_tool,
        _create_get_escalations_tool,
        _create_get_order_details_tool,
        _create_get_recent_orders_tool,
    )

    get_order_details = _create_get_order_details_tool(state)
    get_recent_orders = _create_get_recent_orders_tool(state)
    escalate_to_agent = _create_escalation_tool(state, agent="escalation")
    # Answers "what happened to the complaint I raised?" from structured fields.
    # Read-only, and reads the same cached snapshot as the agent-only escalation
    # context block, so it adds no DB work. Placed with the other resolution
    # tools — ahead of escalate_to_agent, which stays last as the actuator.
    get_escalations = _create_get_escalations_tool(state)
    base = [get_order_details, get_recent_orders, get_escalations, escalate_to_agent]

    try:
        from fashion_bot.config_manager import aget_config
        from fashion_bot.agent_config import escalation_resolution_tools_enabled

        cid = (state or {}).get("client_id") or client_id
        cfg = await aget_config("escalation_policy", client_id=cid) if cid else None
        if not escalation_resolution_tools_enabled(cfg):
            return base

        from fashion_bot.tool_factory import (
            _create_product_search_tools,
            _create_delivery_partner_info_tool,
        )

        search_products, find_product_by_url, find_product_by_id = _create_product_search_tools(state, cid)
        get_delivery_partner_information = _create_delivery_partner_info_tool(state)
        # policy answers (returns / refund / delivery / payment) + vendor + store
        policy_tools = _get_policy_tools(state, messages_list, cid)

        return [
            get_order_details,
            get_recent_orders,
            get_escalations,
            *policy_tools,
            search_products,
            find_product_by_url,
            find_product_by_id,
            get_delivery_partner_information,
            escalate_to_agent,  # last-resort actuator stays last
        ]
    except Exception as exp_err:  # never break tool loading — fall back to base
        logger.warning(f"⚠️ escalation resolution-tools check failed, using base set: {exp_err}")
        return base


def get_agent_config(agent_name: str) -> Dict[str, Any]:
    """
    Get the full configuration for an agent.
    
    Args:
        agent_name: Name of the agent
        
    Returns:
        Dict with topic, entity_type, factory info, prompt_name
    """
    entry = TOOL_REGISTRY.get(agent_name, {})
    return {
        "topic": entry.get("topic", "general"),
        "entity_type": entry.get("entity_type", "product"),
        "factory": entry.get("factory"),
        "params": entry.get("params", []),
        "prompt_name": entry.get("prompt_name", f"{agent_name}_handler")
    }


def list_available_agents() -> List[str]:
    """List all agents available in the registry."""
    return list(TOOL_REGISTRY.keys())
