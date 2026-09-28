"""
Continuity Agent Handler

This module contains the continuity_agent_handler that handles ambiguous query resolution situations.

The handler determines if:
1. Conversation is truly resolved → Send closure message
2. Resolution is unclear or pending actions exist → Ask if query was resolved
"""

import logging
from typing import Dict, Any
from fashion_bot.schema import SupportState
from fashion_bot.utils.utils import log_with_trace_id, get_trace_id
from fashion_bot.core.llm_factory import LLMFactory
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

logger = logging.getLogger("primary_agent")

# ============================================================================
# PROMPT FETCHING FUNCTIONS
# ============================================================================

async def aget_continuity_check_prompt_from_db(client_id: str) -> str:
    """
    Async: fetch continuity check prompt using async tiered caching.

    Args:
        client_id: Client ID for multi-tenant support

    Returns:
        Continuity check prompt string
    """
    try:
        from fashion_bot.utils.utils import aget_agent_prompt_with_caching

        prompt = await aget_agent_prompt_with_caching(client_id, 'continuity_check_agent')

        if prompt:
            logger.info(f"📖 Loaded continuity_check_agent prompt via caching for client: {client_id}")
            return prompt

        logger.warning(f"⚠️ No continuity_check_agent prompt found via caching for client: {client_id}, using default")
        return get_default_continuity_check_prompt()

    except Exception as e:
        logger.error(f"❌ Error fetching continuity_check_agent prompt from database: {str(e)}")
        return get_default_continuity_check_prompt()


def get_default_continuity_check_prompt() -> str:
    """
    Get the default continuity check prompt (fallback when database fetch fails).
    
    Returns:
        Default prompt string
    """
    return """You are analyzing a customer support conversation to determine the appropriate response.

ANALYZE THE SITUATION:

1. **Is the conversation TRULY RESOLVED?**
   - All questions answered
   - No pending confirmations or actions
   - Customer appears satisfied
   - Customer just acknowledged with "ok", "thanks", etc.
   → Generate a warm CLOSURE MESSAGE thanking them and inviting continued engagement

2. **Are there PENDING ACTIONS or is resolution UNCLEAR?**
   - Customer might be waiting for something
   - Context suggests unfinished business
   - Not clear if customer is satisfied or just acknowledging
   → Generate a message asking if their query was resolved or if they need more help

YOUR TASK:
Generate the appropriate message to send to the customer.

RESPONSE GUIDELINES:

**If RESOLVED (all clear, customer satisfied):**
- Thank them warmly
- Invite them back
- Keep it brief (1-2 sentences)
- Use friendly emojis (😊, ✨, 🙏, 💝, 🛍️, 📦)
- Examples:
  * "Thank you for chatting with us! Feel free to reach out if you need anything else. 😊"
  * "Glad I could help! Let me know if there's anything else you need. ✨"
  * "All set! We're here whenever you need us. 🙏"

**If UNCLEAR/PENDING (not sure if resolved, or pending actions):**
- Politely ask if query was resolved
- Offer to help with anything else
- Keep it friendly and non-pushy
- Examples:
  * "Great! Was your query resolved, or is there anything else I can help you with? 😊"
  * "I hope that helps! Is there anything else you'd like to know? ✨"
  * "Has your question been answered, or would you like more information? 🙏"

TONE: Friendly, helpful, not pushy
LENGTH: Keep messages brief (1-2 sentences max)

Respond with ONLY the message text, no other content or formatting.

RECENT CONVERSATION (last 10 messages):
{conversation_context}
"""

# ============================================================================
# CONTINUITY AGENT HANDLER
# ============================================================================

async def continuity_agent_handler(state: SupportState) -> Dict[str, Any]:
    """
    Handle ambiguous query resolution situations.
    
    This handler is called when user says acknowledgment phrases (ok, thanks, cool, great)
    but intent_detection is unsure which specific phase/intent this applies to.
    
    Responsibilities:
    - Analyze conversation to determine if query is truly resolved
    - Check if pending actions remain (order confirmation, delivery tracking, etc.)
    - If RESOLVED → Send contextual closure message
    - If UNCLEAR → Ask user "Was your query resolved?" or "Is there anything else I can help with?"
    """
    try:
        log_with_trace_id(state, "🔄 CONTINUITY AGENT - Analyzing conversation resolution status")
        
        # Check if already shown closure
        if state.get("continuity_closure_shown", False):
            log_with_trace_id(state, "⚠️ Continuity closure already shown - skipping")
            return {
                "type": "pass_through"
            }
        
        # Get conversation context
        messages = state.get("messages", [])
        last_20_messages = messages[-20:] if len(messages) >= 20 else messages
        
        # Build conversation context
        conversation_context = "\n".join([
            ("User: " + m.content) if getattr(m, "type", "human") == "human" else ("Bot: " + m.content)
            for m in last_20_messages
        ])
        
        # Get client_id for fetching the prompt
        client_id = state.get("client_id", "default")
        
        # Fetch prompt from database with fallback to default
        analysis_prompt_template = await aget_continuity_check_prompt_from_db(client_id)
        
        # Replace the conversation context placeholder
        analysis_prompt = analysis_prompt_template.format(conversation_context=conversation_context)
        
        log_with_trace_id(state, f"📝 Using continuity check prompt for client: {client_id}")

        # Resolve LLM per-invocation (not at module load) so the
        # `caller=continuity_check` and `client_id=<...>` labels reach
        # llm.* metrics. The previous `from fashion_bot.llm_config import llm`
        # module-level singleton was created with NO tool_name and pinned
        # caller="unknown" forever — major contributor to the dashboard's
        # unknown/unknown row.
        llm = await LLMFactory.aget_llm(tool_name="continuity_check", state=state)
        output = await llm.ainvoke([
            SystemMessage(content=analysis_prompt),
            HumanMessage(content="Generate appropriate response:")
        ])
        
        suggested_message = output.content.strip()
        
        log_with_trace_id(state, f"✅ CONTINUITY AGENT RESPONSE: {suggested_message}")
        
        # Mark closure as shown (we can reset this later if needed)
        should_mark_closure = True
        
        return {
            "type": "customer_message",
            "customer_message": suggested_message,
            "continuity_closure_shown": should_mark_closure,
            "trace_id": get_trace_id(state)
        }
            
    except Exception as e:
        logger.error(f"❌ Error in continuity_agent_handler: {str(e)}")
        log_with_trace_id(state, f"❌ Error in continuity_agent_handler: {str(e)}", level="error")
        
        # Fallback response
        return {
            "type": "customer_message",
            "customer_message": "Is there anything else I can help you with? 😊",
            "continuity_closure_shown": False,
            "trace_id": get_trace_id(state)
        }
