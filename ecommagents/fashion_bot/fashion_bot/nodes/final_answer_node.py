"""
Final Answer handler node.
1. Deterministic: URL replacement, markdown cleaning, WhatsApp formatting
2. Single LLM call: Language rules, nudges, character limits
"""

from typing import Dict, Any
from fashion_bot.schema import SupportState
from fashion_bot.utils.utils import log_with_trace_id
from langchain_core.messages import AIMessage, SystemMessage, HumanMessage
from fashion_bot.state_cache import timestamped_ai_message
from fashion_bot.utils.langsmith_tracing import traced_operation, set_trace_io, snapshot_state_for_trace
from fashion_bot.rollbar_config import report_error
import logging
import re
from time import perf_counter

logger = logging.getLogger("final_answer_node")


def get_final_answer_llm(client_id: str = None):
    """
    Get LLM instance for final_answer node using the LLM factory.
    Uses gpt-4.1-mini for fast, cost-effective formatting.
    
    Args:
        client_id: Optional client ID for client-specific LLM config
        
    Returns:
        LLM instance configured for final_answer
    """
    try:
        from fashion_bot.core.llm_factory import LLMFactory
        return LLMFactory.get_llm(tool_name="final_answer", client_id=client_id)
    except Exception as e:
        logger.warning(f"Failed to get final_answer LLM from factory: {e}, falling back to default")
        report_error(
            "Failed to get final_answer LLM from factory",
            level="warning",
            exc_info=(type(e), e, e.__traceback__),
            component="final_answer_llm_factory",
            client_id=client_id,
        )
        # Fallback bypasses any per-client config lookup (which is the
        # likely failure mode) but still routes through LLMFactory so
        # llm.* metrics are tagged with caller="final_answer". Previously
        # used `from fashion_bot.llm_config import llm` which is a
        # module-level singleton with NO tool_name → caller="unknown".
        from fashion_bot.core.llm_factory import LLMFactory
        from fashion_bot.core.llm_config import DEFAULT_LLM_CONFIG
        return LLMFactory.get_llm(
            tool_name="final_answer",
            override_config=DEFAULT_LLM_CONFIG,
        )


async def aget_final_answer_llm(client_id: str = None):
    """
    Async version of get_final_answer_llm.
    Get LLM instance for final_answer node using the async LLM factory.
    Uses gpt-4.1-mini for fast, cost-effective formatting.

    Args:
        client_id: Optional client ID for client-specific LLM config

    Returns:
        LLM instance configured for final_answer
    """
    try:
        from fashion_bot.core.llm_factory import LLMFactory
        return await LLMFactory.aget_llm(tool_name="final_answer", client_id=client_id)
    except Exception as e:
        logger.warning(f"Failed to get final_answer LLM from async factory: {e}, falling back to default")
        report_error(
            "Failed to get final_answer LLM from async factory",
            level="warning",
            exc_info=(type(e), e, e.__traceback__),
            component="final_answer_llm_factory_async",
            client_id=client_id,
        )
        # See sync get_final_answer_llm: bypass per-client config but keep
        # caller=final_answer in llm.* metrics. The previous module-level
        # `llm` shim had no tool_name and pinned caller="unknown".
        from fashion_bot.core.llm_factory import LLMFactory
        from fashion_bot.core.llm_config import DEFAULT_LLM_CONFIG
        return await LLMFactory.aget_llm(
            tool_name="final_answer",
            override_config=DEFAULT_LLM_CONFIG,
        )


async def aget_final_answer_prompt_from_db(client_id: str) -> str:
    """
    Async: fetch final answer prompt using async tiered caching.

    Args:
        client_id: Client ID for multi-tenant support

    Returns:
        Final answer prompt string
    """
    try:
        from fashion_bot.utils.utils import aget_agent_prompt_with_caching

        with traced_operation(
            "final_answer.fetch_prompt",
            metadata={"prompt_name": "final_answer_handler", "client_id": client_id},
        ):
            prompt = await aget_agent_prompt_with_caching(client_id, 'final_answer_handler')

        if prompt:
            logger.info(f"📖 Loaded final_answer_handler prompt via caching for client: {client_id}")
            return prompt

        logger.warning(f"⚠️ No final_answer_handler prompt found via caching for client: {client_id}, using default")
        return get_default_final_answer_prompt()

    except Exception as e:
        logger.error(f"❌ Error fetching final_answer_handler prompt from database: {str(e)}")
        report_error(
            "Error fetching final_answer_handler prompt from database",
            level="warning",
            exc_info=(type(e), e, e.__traceback__),
            component="final_answer_prompt_fetch",
            client_id=client_id,
        )
        return get_default_final_answer_prompt()


def get_default_final_answer_prompt() -> str:
    """
    Get the default final answer prompt (fallback when database fetch fails).
    
    Returns:
        Default prompt string
    """
    return """You are a WhatsApp customer-support & sales assistant for fashion e-commerce.

TASK: Process the draft reply and produce the final WhatsApp-ready message.

CRITICAL: Do NOT change the draft reply fundamentally. The meaning and intent must remain the same.
Only apply formatting, language adjustments, and style improvements. Never alter the core message or facts.

The draft has already been formatted (URLs replaced, markdown cleaned). Your job is to:
1. Apply language rules based on customer's messages
2. Apply character limits
3. Add purchase nudge if appropriate
4. Remove generic closers

OUTPUT: Return ONLY the final message. NO analysis, commentary, or prefixes.

LANGUAGE RULES:
DEFAULT = ENGLISH. Use Hinglish ONLY if 90%+ certain customer prefers it.

HINGLISH CRITERIA (ALL must be met):
• ALL recent messages contain MULTIPLE Hindi words (kab, kya, hai, hogi, aayega, mujhe, nahi, haan, kitna, kaise, chahiye, milega, abhi, jaldi, yeh, woh, etc.)
• NO English sentence structure
• Hindi is PRIMARY language, not 1-2 mixed words

ENGLISH TRIGGERS (any = respond in ENGLISH):
• Single English words: order, exchange, return, refund, cancel, confirm, delivery, price, discount, size, quality, cod, upi, payment, status, track, yes, no, ok, thanks
• English sentences: "I want...", "Where is...", "Can you...", "How to..."
• E-commerce terms: cart, checkout, shipping, tracking, customer, support

When in doubt → ENGLISH. Use ONLY English letters (A-Z).

PURCHASE NUDGE: Add ONE gentle nudge ("place the order", "order for you") IF:
• Customer asked 2+ product/discount questions in recent messages
• No nudge in recent assistant messages
Never repeat nudges.

AVOID REPETITION: If recent responses are similar, rephrase. Show empathy if customer seems frustrated.

EMPTY RESPONSE HANDLING:
If the draft reply is marked as [EMPTY_RESPONSE], analyze the customer's recent messages to determine what they were asking about and generate an appropriate apologetic response:
• Product inquiry (product, item, dress, shirt, size, color, price, stock, available, collection, show me, looking for): "I'm not able to fetch the product details right now. Please try again later."
• Order inquiry (order, tracking, delivery, shipped, status, where is my, when will, dispatch): "I'm not able to fetch your order details right now. Please try again later."
• Return/Exchange inquiry (return, exchange, refund, cancel): "I'm not able to process your return/exchange request right now. Please try again later."
• Payment inquiry (payment, COD, UPI, pay, transaction): "I'm not able to fetch the payment details right now. Please try again later."
• General/Unknown: "I'm not able to help with that right now. Please try again later."

GUIDELINES:
• ≤200 chars (max 250 for long responses, max 400 for size guides), max 2 emojis
• Professional, conversational tone
• Keep draft facts intact, no new info
• NO generic closers ("If you have any questions...", "Let me know if you need help", "Feel free to ask", etc.)
• NO fake contact details

Return ONLY the customer-facing message."""


async def final_answer_intent_node(state: SupportState) -> Dict[str, Any]:
    """
    Formulate final response for WhatsApp.
    
    Flow:
    1. Apply deterministic formatting (URL replacement, markdown cleaning)
    2. Single LLM call for language/nudge/character rules
    3. Return final message
    """
    node_start = perf_counter()
    step_ms: Dict[str, int] = {}

    with traced_operation(
        "final_answer.state_snapshot_input",
        metadata={"client_id": state.get("client_id")},
    ) as _fa_state_in:
        set_trace_io(_fa_state_in, inputs={"state": snapshot_state_for_trace(state)})

    try:
        from fashion_bot.config_manager import aresolve_client_id

        customer_message = state.get("customer_message", "")
        messages_list = state.get("messages", [])
        client_id = state.get("client_id")

        if not client_id:
            client_id = await aresolve_client_id()
        
        # Get recent messages for context
        last_10_customer = [m.content for m in messages_list if getattr(m, "type", "human") == "human"][-10:]
        last_5_bot = [m.content for m in messages_list if getattr(m, "type", "ai") != "human"][-5:]
        last_3_user_messages = last_10_customer[-3:] if last_10_customer else []
        
        # Get the user's original message for size guide detection
        user_message = messages_list[-1].content if messages_list and len(messages_list) > 0 else ""
        
        # Skip LLM processing for phone validation failures to preserve exact message
        phone_validation_keywords = [
            "phone number does not match",
            "phone number doesn't match", 
            "number does not match",
            "number doesn't match"
        ]
        is_phone_validation_failure = any(keyword in customer_message.lower() for keyword in phone_validation_keywords) if customer_message else False
        
        if is_phone_validation_failure:
            log_with_trace_id(state, f"🚫 Skipping LLM processing for phone validation failure message")
            # Still apply basic formatting without LLM
            from fashion_bot.utils.utils import format_for_whatsapp
            whatsapp_message = format_for_whatsapp(customer_message)
            result = {
                "messages": state.get("messages", []) + [timestamped_ai_message(whatsapp_message)]
            }
            # Preserve conversation_context
            if state.get("conversation_context"):
                result["conversation_context"] = state["conversation_context"]
            return result
        
        # ============= APPLY FORMATTING FIRST (deterministic) =============
        async def _format_message(msg: str) -> str:
            """Apply URL replacement and markdown cleaning."""
            if not msg:
                return msg

            from fashion_bot.utils.utils import format_for_whatsapp

            msg_lower = msg.lower()
            needs_url_formatting = any(p in msg_lower for p in ['shopify', 'myshopify'])
            needs_markdown_cleaning = any(p in msg for p in ['](', '**', '__', '```', '| ---'])

            if needs_url_formatting or needs_markdown_cleaning:
                log_with_trace_id(state, f"🔧 Applying URL/markdown formatting")
                from fashion_bot.config_manager import aget_config, aget_shopify_config
                from fashion_bot.tools import (
                    replace_urls_in_message_tool,
                    clean_markdown_links_tool,
                    format_message_for_whatsapp_tool
                )

                def _call(tool_fn, *args, **kwargs):
                    fn = tool_fn.fn if hasattr(tool_fn, "fn") else tool_fn
                    return fn(*args, **kwargs)

                current_client_id = state.get("client_id")

                if needs_url_formatting:
                    with traced_operation(
                        "final_answer.fetch_formatting_config",
                        metadata={"client_id": current_client_id},
                    ):
                        shopify_config = await aget_shopify_config(current_client_id)
                        shopify_url = shopify_config.get("shop_url", "")
                        website_config = await aget_config('website_urls', client_id=current_client_id)
                        website_url = (website_config.get("website_url") or website_config.get("website") or website_config.get("home") or "") if isinstance(website_config, dict) else (website_config or "")
                    msg = _call(replace_urls_in_message_tool, msg, shopify_url, website_url, state)

                if needs_markdown_cleaning:
                    msg = _call(clean_markdown_links_tool, msg)

                return _call(format_message_for_whatsapp_tool, msg, state)
            else:
                return format_for_whatsapp(msg)

        # Format the message first (URL/markdown cleanup)
        format_start = perf_counter()
        with traced_operation(
            "final_answer.format_draft",
            metadata={"client_id": client_id},
            require_parent=False,
        ):
            formatted_message = await _format_message(customer_message) if customer_message else customer_message
        step_ms["format_draft"] = int((perf_counter() - format_start) * 1000)
        
        # If no customer_message, try to build from parallel_results
        if not formatted_message:
            parallel_results = state.get("parallel_results", {})
            if parallel_results:
                responses = []
                for node_name, result in parallel_results.items():
                    if isinstance(result, dict) and "customer_message" in result:
                        responses.append(result['customer_message'])
                if responses:
                    formatted_message = responses[0] if len(responses) == 1 else "I can help with that! " + " ".join(responses[:2])
        
        # If draft reply is empty (e.g., from generic_skill_node), mark it for LLM to generate contextual fallback
        is_empty_response = not formatted_message or not formatted_message.strip()
        if is_empty_response:
            log_with_trace_id(state, f"⚠️ Empty draft reply received, will generate contextual fallback via LLM")
            formatted_message = "[EMPTY_RESPONSE]"
        
        # ============= LLM CALL: Language/Nudge/Character rules =============
        # Single LLM call (no tools) to apply language rules, nudges, and character limits
        
        prompt_start = perf_counter()
        with traced_operation(
            "final_answer.load_prompt_for_turn",
            metadata={"client_id": client_id},
            require_parent=False,
        ):
            FINAL_ANSWER_PROMPT = await aget_final_answer_prompt_from_db(client_id)
        step_ms["load_prompt_for_turn"] = int((perf_counter() - prompt_start) * 1000)
        
        last_3_messages_text = "\n".join([f"- {msg}" for msg in last_3_user_messages]) if last_3_user_messages else "No previous messages"
        bot_messages_text = "\n".join([f"- {m}" for m in last_5_bot]) if last_5_bot else "No messages"
        
        # ============= SEPARATED PROMPT COMPONENTS =============
        # 1. Role & Instructions (from DB or default)
        role_instructions = FINAL_ANSWER_PROMPT
        
        # 2. Context: Conversation history (customer + bot messages combined)
        conversation_history = f"""# CONVERSATION HISTORY:
## Customer's last 3 messages (for language detection):
{last_3_messages_text}

## Recent assistant messages (last 5, for nudge/repetition detection):
{bot_messages_text}"""
        
        # 3. Draft message to process
        draft_message = f"""# DRAFT REPLY (already formatted, apply language/nudge/character rules):
{formatted_message}

Return ONLY the final message. No prefixes, no analysis."""
        
        log_with_trace_id(state, f"🤖 Calling LLM for language/nudge/character rules")
        
        # Build messages list with separated concerns
        messages_to_send = [
            SystemMessage(content=role_instructions),    # 1. Role & instructions
            SystemMessage(content=conversation_history), # 2. Conversation history (combined)
            SystemMessage(content=draft_message),        # 3. Draft to process
            HumanMessage(content="Process the draft and return the final WhatsApp message.")
        ]
        
        # Get LLM configured for final_answer (uses gpt-4.1-mini for fast, cost-effective formatting)
        client_id = state.get("client_id")
        llm_resolve_start = perf_counter()
        with traced_operation(
            "final_answer.resolve_llm",
            metadata={"client_id": client_id},
            require_parent=False,
        ):
            final_answer_llm = await aget_final_answer_llm(client_id)
        step_ms["resolve_llm"] = int((perf_counter() - llm_resolve_start) * 1000)

        llm_invoke_start = perf_counter()
        with traced_operation(
            "final_answer.llm_invoke",
            run_type="llm",
            metadata={"client_id": client_id},
            require_parent=False,
        ):
            response = await final_answer_llm.ainvoke(messages_to_send)
        step_ms["llm_invoke"] = int((perf_counter() - llm_invoke_start) * 1000)
        log_with_trace_id(state, f"🤖 [LLM] final_answer elapsed_ms={step_ms['llm_invoke']}")

        final_message = response.content.strip()
        
        # Safety check: ensure [EMPTY_RESPONSE] marker is never shown to user
        if "[EMPTY_RESPONSE]" in final_message:
            log_with_trace_id(state, f"⚠️ LLM returned raw [EMPTY_RESPONSE] marker, using fallback")
            final_message = "I'm not able to help with that right now. Please try again later."
        
        log_with_trace_id(state, f"✅ LLM Response: {len(final_message)} chars")
        log_with_trace_id(state, f"📤 Final Message: {final_message[:100]}...")
        
        # ============= PRESERVE CONVERSATION CONTEXT =============
        result = {
            "messages": state.get("messages", []) + [timestamped_ai_message(final_message)]
        }
        
        if state.get("conversation_context"):
            result["conversation_context"] = state["conversation_context"]
            log_with_trace_id(state, f"📦 [FINAL_NODE] Preserving conversation_context with {len(state['conversation_context'].get('entities', []))} entities")

        total_ms = int((perf_counter() - node_start) * 1000)
        log_with_trace_id(
            state,
            f"⏱️ [FINAL_NODE] total_ms={total_ms} steps_ms={step_ms}",
        )

        with traced_operation(
            "final_answer.state_snapshot_output",
            metadata={"client_id": state.get("client_id")},
        ) as _fa_state_out:
            set_trace_io(_fa_state_out, outputs={"state": snapshot_state_for_trace(result)})

        return result
        
    except Exception as e:
        total_ms = int((perf_counter() - node_start) * 1000)
        logger.error(f"❌ Error in final_answer_node: {str(e)}")
        log_with_trace_id(state, f"🔍 FINAL ANSWER NODE - Error: {e} | total_ms={total_ms} steps_ms={step_ms}")
        report_error(
            "Error in final_answer_node",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            trace_id=state.get("trace_id"),
            client_id=state.get("client_id"),
            node="final_answer_node",
            total_ms=total_ms,
        )
        
        # Fallback: try to use the customer_message with basic formatting
        customer_message = state.get("customer_message", "")
        if customer_message:
            try:
                from fashion_bot.utils.utils import format_for_whatsapp
                from fashion_bot.utils.product_utils import replace_shopify_url, aget_shopify_to_website_mapping
                current_client_id = state.get("client_id")
                if current_client_id:
                    _website_url = await aget_shopify_to_website_mapping(current_client_id)
                    if _website_url:
                        customer_message = replace_shopify_url(customer_message, _website_url)
                # Clean markdown links
                customer_message = re.sub(r'\[([^\]]*)\]\s*\(([^)]+)\)', r'\2', customer_message)
                whatsapp_message = format_for_whatsapp(customer_message)
                result = {
                    "messages": state.get("messages", []) + [timestamped_ai_message(whatsapp_message)]
                }
                # Preserve conversation_context
                if state.get("conversation_context"):
                    result["conversation_context"] = state["conversation_context"]
                return result
            except Exception as exc:
                log_with_trace_id(state, f"⚠️ Error formatting final answer: {exc}", "warning")
        
        fallback_message = "Sorry, something went wrong. Please try again later."
        result = {
            "messages": state.get("messages", []) + [timestamped_ai_message(fallback_message)],
            "customer_message": fallback_message
        }
        # Preserve conversation_context
        if state.get("conversation_context"):
            result["conversation_context"] = state["conversation_context"]

        with traced_operation(
            "final_answer.state_snapshot_output",
            metadata={"client_id": state.get("client_id"), "error": True},
        ) as _fa_state_err:
            set_trace_io(_fa_state_err, outputs={"state": snapshot_state_for_trace(result)})

        return result


# Legacy alias for backward compatibility
async def final_answer_node(state: SupportState) -> Dict[str, Any]:
    """Legacy wrapper - calls the new agentic final_answer_intent_node."""
    return await final_answer_intent_node(state)
