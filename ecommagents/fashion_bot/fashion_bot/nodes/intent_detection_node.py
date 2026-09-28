import asyncio
import uuid
import logging
import json
import re
from typing import Any, Dict
from fashion_bot.schema import SupportState
from fashion_bot.core.llm_factory import LLMFactory
from fashion_bot.utils.utils import check_missing_vars, merge_customer_messages, format_for_whatsapp, _safe_call, log_with_trace_id, get_trace_id, aget_recent_template_messages, should_fetch_template_messages, aget_agent_prompt_with_caching
from fashion_bot.utils.langsmith_tracing import traced_operation, set_trace_io, snapshot_state_for_trace
from fashion_bot.rollbar_config import report_error
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
import datetime
import pytz

logger = logging.getLogger("meta_nodes")

# ==================== OUTPUT EXPANSION MAPS ====================
# Used to expand minimal LLM output back to full format

PARENT_CODE_MAP = {
    "SD": "Sales & Product Discovery",
    "PS": "Post-Purchase Support", 
    "GI": "General Inquiries",
    "GS": "Greetings & Small Talk",
    "FB": "Feedback",
    "FO": "Fallback / Out of Scope",
    "ES": "Escalation"
}

def expand_minimal_response(minimal: Dict[str, Any]) -> Dict[str, Any]:
    """
    Expand minimal LLM response to full format.
    
    Minimal format: {"p":"GI","i":"return_exchange_policy","t":"R&E Policy Query"}
    Full format: {"parent_intent":"General Inquiries","detected_intents":[{"intent":"return_exchange_policy"}],...}
    """
    # Check if it's already in full format (backwards compatibility)
    if "parent_intent" in minimal or "detected_intents" in minimal:
        return minimal
    
    # Expand parent code
    parent_code = minimal.get("p", "")
    parent_intent = PARENT_CODE_MAP.get(parent_code, parent_code)
    
    # Expand intent
    intent = minimal.get("i", "")
    detected_intents = [{"intent": intent}] if intent else []
    
    # Tags are now generated asynchronously after reply is sent
    # (see async_tag_generator.py). Keep empty list for backward compat.
    detected_tags = []
    
    # Expand boolean flags (only present if true)
    is_callback_request = minimal.get("cb", False)
    is_frustrated = minimal.get("fr", False)
    has_unknown = minimal.get("uk", False)
    is_topic_change_from_recommendations = minimal.get("tc", False)
    
    return {
        "parent_intent": parent_intent,
        "detected_intents": detected_intents,
        "detected_tags": detected_tags,
        "is_callback_request": is_callback_request,
        "is_frustrated": is_frustrated,
        "has_unknown": has_unknown,
        "is_topic_change_from_recommendations": is_topic_change_from_recommendations,
        "reason": minimal.get("r", "")  # Optional reason field
    }


async def aget_intent_detection_prompt_from_db(client_id: str = None) -> str:
    """
    Async: fetch intent detection system prompt from database with Redis caching.

    Args:
        client_id: Optional client ID for multi-client support

    Returns:
        System prompt string for intent detection, or None if not found
    """
    try:
        from fashion_bot.config_manager import aresolve_client_id
        effective_client_id = client_id or await aresolve_client_id()

        # Use the async caching mechanism consistent with other nodes
        with traced_operation(
            "detect_intent.fetch_prompt",
            metadata={"client_id": effective_client_id, "prompt_name": "intent_detection_handler"},
        ):
            prompt = await aget_agent_prompt_with_caching(effective_client_id, "intent_detection_handler")

        if prompt:
            logger.debug(f"📖 Loaded intent_detection_handler from DB for client: {effective_client_id}")
            return prompt
        else:
            logger.debug(f"⚠️ intent_detection_handler not found in DB for client: {effective_client_id}, using default")
            return None
    except Exception as e:
        logger.warning(f"Error fetching intent detection prompt from database: {e}")
        return None


# ==================== HELPERS ====================


def _build_conversation_history_string(
    recent_msgs,
    template_messages_context,
    previous_intent,
):
    """
    Build the ``History (last N):`` text block fed to the intent-detection LLM.

    Extracted as a helper so the 400-retry path (which rebuilds the prompt
    with a truncated message list) emits exactly the same format as the
    primary path — no drift, no divergent annotation logic.

    Parameters
    ----------
    recent_msgs:
        The (possibly truncated) list of LangChain message objects to render.
    template_messages_context:
        WhatsApp template messages sent outside this chat — surfaced as
        ``Bot (Template):`` lines so the intent router can correlate
        short user confirmations to the template they were responding to.
    previous_intent:
        Intent detected on the third-to-last user message, annotated inline
        on that message as ``[Detected Intent: X]`` for continuity tracking.
    """
    lines = []
    if template_messages_context:
        lines.append("--- Recent Template Messages (sent outside chat) ---")
        for tmpl_msg in reversed(template_messages_context):  # Oldest first
            lines.append(f"Bot (Template): {tmpl_msg}")
        lines.append("--- End of Template Messages ---")

    for idx, m in enumerate(recent_msgs):
        # Annotate the third-to-last message (= last completed user turn
        # before the current one) with its detected intent, so the LLM has
        # an explicit continuity anchor.
        is_last_user_msg = (
            hasattr(m, "type")
            and m.type == "human"
            and idx == len(recent_msgs) - 5
        )
        if hasattr(m, "type") and m.type == "human":
            if is_last_user_msg and previous_intent:
                lines.append(f"User: {m.content} [Detected Intent: {previous_intent}]")
            else:
                lines.append(f"User: {m.content}")
        else:
            lines.append(f"Bot: {m.content}")

    return "\n".join(lines)


def _extract_provider_error_code(exc):
    """
    Best-effort extraction of the upstream HTTP code from an LLM exception.

    Recognises two shapes:

      1. OpenAI Python SDK's ``APIStatusError`` subclasses, which expose a
         ``.status_code`` attribute (e.g. ``openai.BadRequestError``,
         ``openai.NotFoundError``). LangChain's ChatOpenAI / OpenRouter
         clients propagate these unchanged.
      2. Gateway-wrapped envelopes that surface as exceptions whose ``str()``
         form is the dict
         ``{'message': 'Provider returned error', 'code': 404}``. OpenRouter
         in particular forwards upstream-provider errors this way.

    Returns ``None`` if no code can be extracted — callers should treat that
    as "transport/unknown" rather than assume a specific failure mode.
    """
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    match = re.search(r"['\"]code['\"]\s*:\s*(\d{3})", str(exc))
    if match:
        try:
            return int(match.group(1))
        except (TypeError, ValueError):
            return None
    return None


# Upstream errors that are transient (availability hiccups, not request-shape):
# gateway "Provider returned error" 404s, rate limits, and 5xx. A code of None
# means transport/timeout/connection — also transient. We deliberately exclude
# 400 (handled by the truncated-history retry) and auth (401/403), which a
# plain re-attempt won't fix.
_INTENT_TRANSIENT_PROVIDER_CODES = frozenset({404, 408, 409, 425, 429, 500, 502, 503, 504})
_INTENT_TRANSIENT_RETRY_BACKOFFS = (0.5, 1.0)  # len == number of retries after the first attempt


def _is_transient_intent_error(provider_code) -> bool:
    return provider_code is None or provider_code in _INTENT_TRANSIENT_PROVIDER_CODES


async def _ainvoke_intent_with_transient_retry(intent_llm, messages, state):
    """Invoke the intent LLM, retrying briefly on TRANSIENT upstream errors.

    A transient 404/429/5xx (or a connection/timeout with no HTTP code) usually
    succeeds on an immediate re-attempt — without this, the customer's message
    dead-ends on the unknown-handler fallback even though the very next turn
    would route fine. Non-transient errors (400, auth) are re-raised immediately
    so the existing 400 truncated-history retry / safe-default path still runs.
    """
    last_exc: Exception = None
    for attempt in range(len(_INTENT_TRANSIENT_RETRY_BACKOFFS) + 1):
        try:
            return await intent_llm.ainvoke(messages)
        except Exception as exc:
            provider_code = _extract_provider_error_code(exc)
            is_last_attempt = attempt == len(_INTENT_TRANSIENT_RETRY_BACKOFFS)
            if is_last_attempt or not _is_transient_intent_error(provider_code):
                raise
            last_exc = exc
            backoff = _INTENT_TRANSIENT_RETRY_BACKOFFS[attempt]
            log_with_trace_id(
                state,
                (
                    f"🔁 Transient intent-detection LLM error (code={provider_code}); "
                    f"retry {attempt + 1}/{len(_INTENT_TRANSIENT_RETRY_BACKOFFS)} in {backoff}s"
                ),
                "warning",
            )
            await asyncio.sleep(backoff)
    raise last_exc  # defensive; loop either returns or raises above


# ==================== DEFAULT INTENT DETECTION PROMPT ====================
# Used as fallback if not found in database

DEFAULT_INTENT_DETECTION_PROMPT = """CRITICAL: You MUST respond with a single valid JSON object and nothing else. Never respond with prose, markdown, or partial output.

Router: Route message to ONE agent. Output JSON only.

CONTINUITY: If [Detected Intent: X] in history + user provides related info → CONTINUE same intent.
TOPIC CHANGE: Only new intent if completely unrelated.
CONFIRMATION: yes/ok/confirm → route to active topic's agent

TEMPLATE MESSAGE CONTEXT:
When "Bot (Template):" messages appear before user's message, the user is likely RESPONDING to that template.
Analyze the template message TYPE and user's response TOGETHER to determine intent.
🔴 CRITICAL: If user sends simple confirmation (yes/ok/confirm/haan) AND there's a Bot (Template) message asking for confirmation:
- DO NOT default to order_status just because it seems order-related
- MATCH the template's ask to the correct agent as specified below

AGENTS (CAN→route | NEVER→exclude):
product_details: price/sizes/fabric/colors/reviews/fit/alterations, product search queries ("show me X", "I want X", "looking for X", "X product/denim/dress/shirt"), browsing products by name/type/category, category/collection inquiries ("show me all dresses", "what categories do you have") | ✗orders,timelines,policies
delivery_timeline: pre-purchase ETA/pincode | ✗serviceability→delivery_policy, tracking→order_status
order_status: status/tracking/ETA+orderID, cancel threats, delivery attempt confirmations, template "reply YES to receive"+yes/confirm, template "couldn't reach you"+yes/confirm, template "shipped/tracking"+status question | ✗pre-purchase,policies
place_order: new order/collect size/color/address, COD order confirmations, template "confirm your COD order"+yes/confirm, template "confirm your order for ₹X"+yes/confirm | ✗payment questions→payment_policy, modify placed, cancel placed order, cart edits→cart_management
cart_management: edit/remove/change quantity of items in the CART (no order placed yet) — "remove from cart", "take out of cart", "change quantity in cart", "empty cart", "delete from basket", "update cart". REQUIRES cart non-empty OR explicit cart vocabulary ("cart", "basket", "bag"). | ✗"cancel my order"+orderID→cancel_or_update, returns
cancel_or_update: post-placement cancel/update+orderID+explicit REQUEST, cancel response to COD template, template COD confirmation+no/cancel | ✗threats→order_status, returns, cart edits→cart_management
return_exchange_policy: policy questions NO orderID ("can I return?","what if?") | ✗action+orderID→after_delivery_rne
delivery_policy: coverage/serviceability | ✗timelines→delivery_timeline, tracking→order_status
payment_policy: payment modes | ✗COD pincode→delivery_timeline, status→order_status
recommendations: ONLY when user explicitly asks for recommendations/suggestions ("recommend me", "suggest something", "what should I buy", "help me choose"), personalized picks, "best for me" | ✗product search queries→product_details, policies/orders/payment
discount: offers/coupons/bulk/"too expensive" | ✗price question→product_details, applied→order_status
vendor_inquiry: store/contacts/authenticity | ✗wholesale→discount, orders
after_delivery_return_exchange: return/exchange ACTION+orderID, template "has been delivered"+return/exchange mention | ✗policy questions→return_exchange_policy
customer_feedback: collect feedback, template "has been delivered"+any feedback | ✗operational
escalation: human request/anger/tech issues/conditional cancels, media messages (video/image sent as proof for return/exchange/defect) | ✗self-serve
continuity_agent: ambiguous acks ("ok/thanks/bye") LOW clarity, no active flow | ✗clear question/active flow

CALLBACK: is_callback_request=true ONLY for explicit "call me/please call/want callback"
- ✗ contact info inquiry, update details
- 🔴 In cancel_or_update (awaiting:confirmation) + "phone/address/email" → route to cancel_or_update (NOT callback)

FRUSTRATION: is_frustrated=true only for clear anger/explicit human demand

MEDIA MESSAGES (VIDEO/IMAGE):
When message contains "[Video message]" or "[Image message]":
- Customer is sending visual proof (product defect, damage, wrong item, exchange reason)
- ALWAYS route to → escalation (needs human review of media)
- If caption has context (e.g., "[Video message]: product is damaged"), use it to enrich the escalation
- Set is_frustrated=false unless caption shows explicit anger

KEY DISTINCTIONS:
- Product search "show me denim/I want X/product X" → product_details | "recommend me/suggest/what should I buy" → recommendations
- Delivery ETA+orderID → order_status | ETA no order → delivery_timeline
- "deliver to X?" → delivery_policy | "how long?" → delivery_timeline
- Price question → product_details | Offer/coupon → discount | Paid amount → order_status  
- Policy "can I return?" → return_exchange_policy | Action "return GV123" → after_delivery_return_exchange
- "I will cancel" threat → order_status | "please cancel" request → cancel_or_update
- "cancel" after COD template → cancel_or_update | "cancel" during place_order flow → cancel_or_update
- "remove/change/update X in cart" + cart non-empty → cart_management (NEVER cancel_or_update)
- "cancel order GV1234" / "cancel my order" with orderID → cancel_or_update | "remove from cart" → cart_management
- "is COD accepted?" → payment_policy | "I'll pay COD" → place_order
- Wholesale/bulk → discount | Store/authenticity → vendor
- Pre-purchase alteration → product_details | Post-order product change → product_change_in_order
- After recs "more like that" → recommendations
- "[Video message]" or "[Image message]" → escalation (media proof needs human review)

OUTPUT (minimal JSON - only include non-default values):
{"p":"<CODE>","i":"<intent>"}

Add ONLY if true: "cb":true (callback), "fr":true (frustrated), "uk":true (unknown), "tc":true (topic_change_from_recs)

Parent codes: SD=Sales & Product Discovery|PS=Post-Purchase Support|GI=General Inquiries|GS=Greetings & Small Talk|FB=Feedback|FO=Fallback / Out of Scope|ES=Escalation
Intents: product_details,delivery_timeline_query,recommendations,discount,place_order,cart_management,vendor_inquiry,order_status,cancel_or_update_order,return_exchange_policy,payment_policy,delivery_policy,product_change_in_order,after_delivery_return_exchange,customer_feedback,escalation,continuity_agent"""

async def detect_intent_node(state: SupportState) -> Dict[str, Any]:
    # Capture input state for LangSmith
    with traced_operation(
        "detect_intent.state_snapshot_input",
        metadata={"client_id": state.get("client_id")},
    ) as _state_in_run:
        set_trace_io(_state_in_run, inputs={"state": snapshot_state_for_trace(state)})

    # Get the current message and recent conversation history
    messages = state.get("messages", [])
    current_message = messages[-1].content if messages else ""
    msg_lower = str(current_message).lower().strip()

    # ============= TEMPLATE MESSAGE CONTEXT =============
    # Fetch recent template messages from template_delivery_logs to provide
    # conversation context from messages sent outside the AI agent graph.
    #
    # This is called when:
    # 1. New conversation (messages <= 1)
    # 2. OR user returned after 30+ minutes of inactivity (may have received templates)
    TEMPLATE_MSG_PREFIX = "This is a template message that was sent to the user corresponding to their order and not an actual AIMessage: "

    template_messages_context = []
    if should_fetch_template_messages(state, inactivity_threshold_minutes=30):
        phone_number = state.get("phone_number", "")
        client_id = state.get("client_id")
        if phone_number:
            with traced_operation(
                "detect_intent.fetch_template_messages",
                metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:], "limit": 3},
            ):
                template_messages_context = await aget_recent_template_messages(
                    phone_number=phone_number,
                    client_id=client_id,
                    limit=3
                )
            if template_messages_context:
                log_with_trace_id(state, f"📨 Using {len(template_messages_context)} template messages as context")

                messages[:] = [
                    m for m in messages
                    if not (hasattr(m, 'content') and isinstance(m.content, str) and m.content.startswith(TEMPLATE_MSG_PREFIX))
                ]
                insert_pos = max(0, len(messages) - 1)
                for tmpl_msg in reversed(template_messages_context):
                    messages.insert(insert_pos, AIMessage(content=f"{TEMPLATE_MSG_PREFIX}{tmpl_msg}"))
                log_with_trace_id(state, f"📨 Injected {len(template_messages_context)} template messages into conversation history")
            else:
                log_with_trace_id(state, f"📭 No template messages found for phone {phone_number} in last 10 days")
    
    # ============= CONTEXTUAL MEMORY: Extract conversation context =============
    conversation_context = state.get("conversation_context", {})
    
    # Extract active topic and its awaiting state (replaces individual STATE FLAGS)
    topics = conversation_context.get("topics", []) if conversation_context else []
    active_topic_id = conversation_context.get("active_topic_id") if conversation_context else None
    active_topic = None
    active_topic_type = None
    active_awaiting = None
    active_topic_summary = None
    
    # Find active topic
    for topic in topics:
        if topic.get("topic_id") == active_topic_id and topic.get("status") == "open":
            active_topic = topic
            active_topic_type = topic.get("topic_type")
            active_awaiting = topic.get("awaiting")
            active_topic_summary = topic.get("summary")
            break
    
    # Extract recommendations context (needed for recommendations_shown check)
    recommendation_data = state.get("recommendation_data", {})
    recommendations_shown = recommendation_data.get("recommendations_shown", False) if recommendation_data else False
    
    focal_entity = conversation_context.get("focal_entity") if conversation_context else None
    recent_entities = conversation_context.get("entities", []) if conversation_context else []
    
    # Check if escalation has occurred and current message indicates fresh start
    escalation_occurred = (state.get("needs_escalation") or 
                          state.get("needs_human_agent") or 
                          state.get("is_frustrated"))
    
    # Check conversation mode - if in agent mode, check if user wants to return to bot
    phone_number = state.get("phone_number", "")
    client_id = state.get("client_id")
    if phone_number:
        try:
            from fashion_bot.repository import bot_user_agent_mode
            with traced_operation(
                "detect_intent.get_conversation_mode",
                metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
            ):
                conversation_state = await bot_user_agent_mode.aget_conversation_state(phone_number, client_id=client_id)
            current_mode = conversation_state.get("mode", "bot") if isinstance(conversation_state, dict) else "bot"
            if current_mode == "agent":
                # In agent mode - check if user wants to return to bot
                bot_keywords = ["bot", "chatbot", "automated", "auto", "back to bot", "switch to bot"]
                if any(keyword in msg_lower for keyword in bot_keywords):
                    log_with_trace_id(state, f"🤖 User wants to switch back to bot mode: '{current_message}'")
                    # Switch back to bot mode and reset escalation flags
                    with traced_operation(
                        "detect_intent.set_conversation_mode_bot",
                        metadata={"client_id": client_id, "phone_suffix": str(phone_number)[-4:]},
                    ):
                        await bot_user_agent_mode.aset_conversation_mode(phone_number, "bot", client_id=client_id)
                    result = {
                        "parent_intent": "Greetings & Small Talk",
                        "detected_intents": [],
                        "has_unknown": False,
                        "detected_tags": [""],
                        "is_frustrated": False,
                        "needs_escalation": False,
                        "needs_human_agent": False,
                        "callback_time": None,
                        "callback_scheduled": None,
                        "trace_id": get_trace_id(state)
                    }
                    # Preserve conversation_context
                    if state.get("conversation_context"):
                        result["conversation_context"] = state["conversation_context"]
                    return result
                # else:
                #     # Stay in agent mode, don't process through bot logic
                #     log_with_trace_id(state, f"👤 Message in agent mode, passing through: '{current_message}'")
                #     result = {
                #         "parent_intent": "Escalation",
                #         "detected_intents": [{"intent": "escalation"}],
                #         "has_unknown": False,
                #         "detected_tags": [""],
                #         "trace_id": get_trace_id(state)
                #     }
                #     # Preserve conversation_context
                #     if state.get("conversation_context"):
                #         result["conversation_context"] = state["conversation_context"]
                #     return result
        except Exception as e:
            log_with_trace_id(state, f"⚠️ Error checking conversation mode: {e}")
    
    recent_messages = messages[-20:] if len(messages) >= 20 else messages
    
    # Enhanced conversation history with intent tracking
    # Try to extract previous intent from scratchpad for context
    previous_intent = None
    try:
        scratchpad_data = state.get("scratchpad", "")
        if scratchpad_data:
            scratchpad_obj = json.loads(scratchpad_data)
            previous_intent = scratchpad_obj.get("last_intents", [])
            if previous_intent and len(previous_intent) > 0:
                previous_intent = previous_intent[0].get("intent", None)
    except:
        previous_intent = None
    
    # Build conversation history with intent annotations
    # For the last 4-5 messages, include detected intent information to help LLM understand context.
    # Extracted into _build_conversation_history_string so the 400-retry path
    # in the except block below can rebuild a truncated version with the same
    # formatting and annotation rules.
    conversation_history = _build_conversation_history_string(
        recent_messages,
        template_messages_context,
        previous_intent,
    )
    
    # Add recommendations context information for the LLM
    recommendations_context_info = ""
    if recommendations_shown:
        recommendations_context_info = """
RECOMMENDATIONS CONTEXT:
Determine if message is FOLLOW-UP (about shown recommendations) or TOPIC CHANGE (unrelated).

FOLLOW-UP → keep intent=recommendations: questions about shown products, sizes, prices, "more like that", "similar ones"
TOPIC CHANGE → detect new intent, set is_topic_change_from_recommendations=true: order queries, returns, cancellations, policies, escalation
"""
    
    # ============= CONTEXTUAL MEMORY: Build context summary for LLM =============
    # Only include context sections if there's meaningful prior context
    has_meaningful_context = (active_topic_type or focal_entity or recent_entities)
    
    conversation_context_info = ""
    context_state_info = ""
    
    if has_meaningful_context:
        # Build focal entity summary
        focal_summary = "None"
        if focal_entity:
            focal_type = focal_entity.get("entity_type", "unknown")
            focal_value = focal_entity.get("entity_value", "Unknown")
            focal_confidence = focal_entity.get("confidence", "unknown")
            focal_summary = f"{focal_type}: {focal_value} (confidence: {focal_confidence})"
        
        # Build recent entities summary (last 5)
        entities_summary = []
        for e in recent_entities[:5]:
            e_type = e.get("entity_type", "")
            e_value = e.get("entity_value", "")
            if e_type and e_value:
                entities_summary.append(f"{e_type}:{e_value}")
        entities_str = ", ".join(entities_summary) if entities_summary else "None"
        
        # Build topic-based context (replaces old STATE FLAGS)
        if active_topic_type:
            awaiting_str = f" (awaiting: {active_awaiting})" if active_awaiting else ""
            topic_context = f"{active_topic_type}{awaiting_str}"
        else:
            topic_context = "None"
        
        conversation_context_info = f"""
CONVERSATION CONTEXT:
- Active Topic: {topic_context}
- Topic Summary: {active_topic_summary or 'None'}
- Focal Entity: {focal_summary}
- Recent Entities: {entities_str}
"""
        
        # Only include topic-based routing rules if there's an active topic with awaiting
        if active_topic_type and active_awaiting:
            context_state_info = f"""
{conversation_context_info}
TOPIC-BASED ROUTING (prioritize CONTINUITY - user is in active flow):
Active topic "{active_topic_type}" is awaiting "{active_awaiting}" from user.

🔴 HIGHEST PRIORITY: Route user's response to {active_topic_type} agent UNLESS user clearly changes topic.
- Confirmation words (yes/ok/confirm/haan) → {active_topic_type}
- Providing requested info ({active_awaiting}) → {active_topic_type}
- Topic change signals: "I want to", "now I need", "different question", explicit new topic

Special rule for return/exchange: If active_topic="after_delivery_return_exchange", NEVER route to cancel_or_update_order.
"""
        elif active_topic_type:
            # Active topic but not awaiting - use follow-up detection 
            context_state_info = f"""
{conversation_context_info}
CONTEXT-AWARE ROUTING: User was discussing {active_topic_type}. Prefer continuity unless clear topic change.
"""
        else:
            # Has entities but no active topic
            context_state_info = conversation_context_info
    
    # ============= LOAD SYSTEM PROMPT FROM DATABASE =============
    # Load intent detection prompt from Redis cache / PostgreSQL
    # Falls back to DEFAULT_INTENT_DETECTION_PROMPT if not found
    client_id = state.get("client_id")
    with traced_operation(
        "detect_intent.load_prompt_for_turn",
        metadata={"client_id": client_id},
    ):
        db_prompt = await aget_intent_detection_prompt_from_db(client_id)
    system_prompt = db_prompt if db_prompt else DEFAULT_INTENT_DETECTION_PROMPT
    
    # Tags are now generated asynchronously after reply is sent (async_tag_generator.py)
    # Remove any leftover {{TAGS_PLACEHOLDER}} from DB-stored prompts for clean output
    system_prompt = system_prompt.replace("TAGS: {{TAGS_PLACEHOLDER}}\n\n", "")
    system_prompt = system_prompt.replace("TAGS: {{TAGS_PLACEHOLDER}}", "")

    # Build context section (dynamic per conversation)
    context_section = ""
    if context_state_info:
        context_section = context_state_info
    if recommendations_context_info:
        context_section += f"\n{recommendations_context_info}"
    
    # Conversation History (dynamic per conversation)
    history_section = f"""History (last 20):
{conversation_history}"""
    
    user_message = f"""Current Message: {current_message}"""
    
    try:
        log_with_trace_id(state, "🤖 Invoking LLM for intent detection", "debug")
        
        # Build messages list: System prompt (from DB) + dynamic context + history + user message
        messages_to_send = [
            SystemMessage(content=system_prompt),  # Main system prompt from DB (includes role, agents, output format)
        ]
        
        # Add context only if meaningful (dynamic per conversation)
        if context_section:
            messages_to_send.append(SystemMessage(content=context_section))
        
        # Add conversation history (dynamic per conversation)
        messages_to_send.append(SystemMessage(content=history_section))
        
        # Add user's current message
        messages_to_send.append(HumanMessage(content=user_message))
        
        import time as _t
        _t0 = _t.monotonic()
        # Resolve this client's router model (client_agent_llm_config → env →
        # default), then attach the default chat model (LLM_PROVIDER / LLM_MODEL)
        # as a native LangChain fallback. If the router model is down / 429s /
        # errors, .ainvoke transparently retries the SAME request on the default
        # model. No-op when the router already IS the default, or when
        # LLM_FAILOVER_ENABLED=false (get_default_llm returns None).
        intent_llm = await LLMFactory.aget_llm(tool_name="intent_detection", state=state)
        _intent_fallback = LLMFactory.get_default_llm()
        if _intent_fallback is not None and _intent_fallback is not intent_llm:
            intent_llm = intent_llm.with_fallbacks([_intent_fallback])
        with traced_operation(
            "detect_intent.llm_invoke",
            run_type="llm",
            metadata={"client_id": client_id},
        ):
            output = await _ainvoke_intent_with_transient_retry(
                intent_llm, messages_to_send, state,
            )
        _elapsed = int((_t.monotonic() - _t0) * 1000)
        content = output.content.strip()
        log_with_trace_id(state, f"🤖 [LLM] detect_intent elapsed_ms={_elapsed}")
        logger.debug(f"raw LLM intent: {content[:120]}")

        def _parse_intent_json(text: str) -> dict:
            """Parse JSON from LLM response, with regex fallback."""
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
                raise

        _MIN_VALID_INTENT_LEN = 7  # shortest valid: {"p":"X"}
        _is_truncated = len(content) < _MIN_VALID_INTENT_LEN or (
            content.startswith("{") and "}" not in content
        )

        if _is_truncated:
            log_with_trace_id(
                state,
                f"⚠️ Intent LLM returned truncated response "
                f"(len={len(content)}, content={repr(content)}); "
                f"retrying with fallback model",
                "warning",
            )
            _fallback_llm = LLMFactory.get_default_llm()
            if _fallback_llm is None:
                _fallback_llm = intent_llm
            with traced_operation(
                "detect_intent.llm_invoke_retry_truncated",
                run_type="llm",
                metadata={"client_id": client_id},
            ):
                retry_output = await _ainvoke_intent_with_transient_retry(
                    _fallback_llm, messages_to_send, state,
                )
            content = retry_output.content.strip()
            log_with_trace_id(
                state,
                f"🔁 Truncation retry returned len={len(content)}",
            )

        try:
            result = _parse_intent_json(content)
        except (json.JSONDecodeError, Exception):
            raise Exception(
                f"No valid JSON found in response "
                f"(len={len(content)}, content={repr(content[:120])})"
            )
        
        # Expand minimal response to full format
        result = expand_minimal_response(result)
        intents_str = ",".join(i.get("intent", "?") for i in result.get("detected_intents", []))
        log_with_trace_id(state, f"intent={intents_str} parent={result.get('parent_intent', '?')}")
        
        parent_intent = result.get("parent_intent", "")
        detected_intents = result.get("detected_intents", [])
        has_unknown = result.get("has_unknown", False)
        detected_tags = result.get("detected_tags", [])
        reason = result.get("reason", "")
        
        # Extract the new callback and frustration detection results
        is_callback_request = result.get("is_callback_request", False)
        is_frustrated = result.get("is_frustrated", False)
        
        # Extract topic change detection for recommendations flow
        is_topic_change_from_recommendations = result.get("is_topic_change_from_recommendations", False)

        # Handle recommendations flow continuation or topic change
        if recommendations_shown:
            if is_topic_change_from_recommendations:
                # User is changing topic - clear recommendations context and proceed with new intent
                log_with_trace_id(state, f"🔄 TOPIC CHANGE: User changing from recommendations to '{parent_intent}' - clearing context")
                state["recommendation_data"] = {}
                # Continue to process the new intent below
            else:
                # User is still in recommendations context but may have different intent
                # Check if the detected intent is actually 'recommendations' (true follow-up)
                # or a different intent about the recommended products (e.g., delivery_timeline_query, product_details)
                current_intent = detected_intents[0].get("intent") if detected_intents else None
                
                if current_intent == "recommendations":
                    # True follow-up question about recommendations
                    log_with_trace_id(state, f"🎯 FOLLOW-UP: User asking for more recommendations - preserving intent")
                    result = {
                        "parent_intent": "Sales & Product Discovery",
                        "detected_intents": [{"intent": "recommendations"}],
                        "has_unknown": False,
                        "detected_tags": ["Product Recommendation"],
                        "trace_id": get_trace_id(state)
                    }
                    if state.get("conversation_context"):
                        result["conversation_context"] = state["conversation_context"]
                    return result
                else:
                    # User asking about recommended products but with different intent (delivery, details, etc.)
                    # Keep the recommendation context but use the detected intent
                    log_with_trace_id(state, f"🎯 CONTEXT-AWARE: User asking '{current_intent}' about recommended products - using detected intent")
                    # Don't clear recommendation_data, just continue with detected intent
                    # Continue to process the detected intent below

        # Check for escalation loop prevention: if we had previous escalation but LLM detected legitimate intent
        if escalation_occurred and parent_intent != "Escalation" and not is_frustrated and not is_callback_request:
            log_with_trace_id(state, f"🔄 Previous escalation detected, but LLM found legitimate intent '{parent_intent}' - resetting escalation flags")
            # Reset escalation flags since LLM detected a legitimate intent
            escalation_occurred = False  # This will prevent adding reset flags to result later
        
        # Handle callback request - highest priority, BUT NOT when awaiting update confirmation
        # When in cancel_or_update_order topic awaiting confirmation, "phone number" etc. are answers to "what do you want to update?"
        is_awaiting_update_confirmation = (active_topic_type == "cancel_or_update_order" and active_awaiting == "confirmation")
        
        if is_callback_request and not is_awaiting_update_confirmation:
            log_with_trace_id(state, f"📞 CALLBACK REQUEST DETECTED: '{current_message}' - triggering direct escalation")
            
            result = {
                "parent_intent": "Escalation",
                "detected_intents": [{"intent": "escalation"}],
                "has_unknown": False,
                "is_frustrated": False,
                "needs_escalation": True,
                "detected_tags": ["Escalations"],
                "trace_id": get_trace_id(state)
            }
            if state.get("conversation_context"):
                result["conversation_context"] = state["conversation_context"]
            return result
        elif is_callback_request and is_awaiting_update_confirmation:
            log_with_trace_id(state, f"📞 Callback request detected but user is in update flow - treating '{current_message}' as update field, not callback")

        # Handle frustration - second priority (but only if not already in escalation loop)
        if is_frustrated:
            # If we already had escalation and LLM still detects frustration, might be stuck in loop
            if escalation_occurred:
                log_with_trace_id(state, f"⚠️ Frustration detected again after escalation - checking if legitimate intent exists")
                # If there are legitimate intents detected alongside frustration, prioritize the intent
                if detected_intents and any(intent.get("intent") != "escalation" for intent in detected_intents):
                    log_with_trace_id(state, f"✅ Found legitimate intents alongside frustration - processing intent instead of re-escalating")
                    # Don't escalate again, process the legitimate intent
                    pass  
                else:
                    log_with_trace_id(state, f"🔄 No legitimate intent found - staying escalated")
                    result = {
                        "parent_intent": "Escalation",
                        "detected_intents": [{"intent": "escalation"}],
                        "has_unknown": False,
                        "detected_tags": ["Escalations"],
                        "trace_id": get_trace_id(state)
                    }
                    if state.get("conversation_context"):
                        result["conversation_context"] = state["conversation_context"]
                    return result
            else:
                # First time frustration (no prior escalation this conversation).
                # Resolution-first: a frustrated customer who ALSO
                # expressed a resolvable intent ("where's my order, this is
                # terrible") should have that intent handled — the resolving
                # agent de-escalates far better by actually helping. Only a
                # STANDALONE frustration (pure anger / explicit escalation with
                # nothing to resolve) escalates on the first turn. Resolution
                # gets exactly ONE chance: the scratchpad ``frustration_streak``
                # counter (maintained below) records consecutive frustrated
                # turns, so a customer still angry on the NEXT turn escalates
                # even with a resolvable intent (the loop-breaker for the
                # resolve-instead route).
                from fashion_bot.agent_config import frustration_should_escalate

                _prev_streak = 0
                _sp_raw = state.get("scratchpad", "")
                if _sp_raw:
                    try:
                        _sp_prev = json.loads(_sp_raw) if isinstance(_sp_raw, str) else (_sp_raw if isinstance(_sp_raw, dict) else {})
                        if isinstance(_sp_prev, dict):
                            _prev_streak = int(_sp_prev.get("frustration_streak", 0) or 0)
                    except (ValueError, TypeError):
                        _prev_streak = 0

                if frustration_should_escalate(detected_intents, consecutive_frustrated_turns=_prev_streak):
                    _why = "persisted after a resolve attempt" if _prev_streak >= 1 else "standalone"
                    log_with_trace_id(state, f"🚨 FRUSTRATION DETECTED ({_why}): '{current_message}' - triggering escalation")
                    result = {
                        "parent_intent": "Escalation",
                        "detected_intents": [{"intent": "escalation"}],
                        "has_unknown": False,
                        "is_frustrated": True,
                        "needs_escalation": True,
                        "detected_tags": ["Escalations"],
                        "scratchpad": json.dumps({
                            "frustration_detected": True,
                            "frustration_streak": _prev_streak + 1,
                            "trigger_message": current_message,
                        })
                    }
                    if state.get("conversation_context"):
                        result["conversation_context"] = state["conversation_context"]
                    return result
                else:
                    # Resolvable intent present alongside frustration → fall
                    # through to normal routing so the resolving agent handles
                    # it. The scratchpad merge below records the frustration
                    # (streak + flag) so the next turn's loop-breaker and the
                    # resolving agent both see it. is_frustrated is deliberately
                    # NOT set on state: graph_context_meta.py:737/758 send any
                    # is_frustrated turn to escalation_handler unless the intent
                    # is in its narrower ``actionable_intents`` set, which would
                    # defeat this route for e.g. cancel_or_update_order or
                    # after_delivery_return_exchange.
                    log_with_trace_id(
                        state,
                        f"😤→🛠️ First-turn frustration with a resolvable intent "
                        f"({intents_str}) — routing to resolve instead of escalating",
                    )
        
        # Merge intent data into existing scratchpad (preserving flags set by tools)
        _existing_sp = state.get("scratchpad", "")
        _sp_obj = {}
        if _existing_sp:
            try:
                _sp_obj = json.loads(_existing_sp) if isinstance(_existing_sp, str) else (_existing_sp if isinstance(_existing_sp, dict) else {})
            except (ValueError, TypeError):
                _sp_obj = {}
        _sp_obj.update({"last_parent_intent": parent_intent, "last_intents": detected_intents, "reason": reason})
        # Consecutive-frustration streak (loop-breaker): bump on a
        # frustrated turn that routed to resolution, reset the moment a turn
        # arrives without frustration. Read back by the first-time-frustration
        # branch above so a SECOND consecutive frustrated turn escalates.
        if is_frustrated:
            try:
                _streak_prev = int(_sp_obj.get("frustration_streak", 0) or 0)
            except (ValueError, TypeError):
                _streak_prev = 0
            _sp_obj["frustration_streak"] = _streak_prev + 1
            _sp_obj["frustration_detected"] = True
        else:
            _sp_obj.pop("frustration_streak", None)
            _sp_obj.pop("frustration_detected", None)
        scratchpad_entry = json.dumps(_sp_obj)
        
        result = {
            "parent_intent": parent_intent,
            "detected_intents": detected_intents,
            "has_unknown": has_unknown,
            "detected_tags": detected_tags,
            "scratchpad": scratchpad_entry
        }
        
        # Template messages are already injected into state["messages"] as AIMessages
        # with the prefix, so skill nodes see them naturally in conversation history.
        
        # ============= CONTEXTUAL MEMORY: Preserve conversation context =============
        # Pass through existing conversation_context so skill nodes can access it
        if conversation_context:
            # ============= UPDATE FOCAL ENTITY BASED ON MESSAGE =============
            # If user mentions a known entity by name, switch focal to that entity.
            # Uses best-match scoring: the entity with the most matching words wins,
            # preventing generic words like "hoodie" from matching the wrong entity.
            entities = conversation_context.get("entities", [])
            if entities and current_message:
                msg_lower_for_match = current_message.lower()
                best_match_entity = None
                best_match_score = 0
                
                msg_words = [w for w in msg_lower_for_match.replace("#", "").split() if len(w) >= 4]

                for entity in entities:
                    entity_value = str(entity.get("entity_value") or "").lower()
                    entity_id = str(entity.get("entity_id") or "").lower().lstrip("#")
                    
                    name_words = entity_value.replace(":", " ").replace("-", " ").replace("#", "").split()
                    id_words = entity_id.replace("-", " ").split()
                    
                    score = 0
                    for word in name_words + id_words:
                        if len(word) >= 4 and word in msg_lower_for_match:
                            score += 1

                    # Reverse check: if a message word is a numeric-heavy
                    # substring of the entity_id (e.g. user says "14409",
                    # entity_id is "gv14409"), count it as a match.
                    if score == 0:
                        for mw in msg_words:
                            if any(c.isdigit() for c in mw) and mw in entity_id:
                                score += 1
                                break
                    
                    if score > best_match_score:
                        best_match_score = score
                        best_match_entity = entity
                
                if best_match_entity and best_match_score > 0:
                    current_focal = conversation_context.get("focal_entity", {})
                    current_focal_id = current_focal.get("entity_id") if current_focal else None
                    
                    if best_match_entity.get("entity_id") != current_focal_id:
                        from datetime import datetime
                        conversation_context["focal_entity"] = {
                            "entity_type": best_match_entity.get("entity_type"),
                            "entity_id": best_match_entity.get("entity_id"),
                            "entity_value": best_match_entity.get("entity_value"),
                            "confidence": "inferred_from_message",
                            "set_at": datetime.now().isoformat()
                        }
                        log_with_trace_id(state, f"🎯 Updated focal_entity to '{best_match_entity.get('entity_value')}' based on message mention (score={best_match_score})")
                        focal_entity = conversation_context["focal_entity"]
            
            result["conversation_context"] = conversation_context
            log_with_trace_id(state, f"📊 conv_ctx: topic={active_topic_type} awaiting={active_awaiting} focal={focal_entity.get('entity_value') if focal_entity else 'None'}", "debug")
        
        # STATE FLAGS are no longer used. Flow state is now managed via:
        # - active_topic_type: which agent is handling the conversation
        # - active_awaiting: what info the agent is waiting for from user
        
        # Check if we need to reset escalation flags (either original escalation_occurred or we detected legitimate intent)
        original_escalation_occurred = (state.get("needs_escalation") or 
                                      state.get("needs_human_agent") or 
                                      state.get("is_frustrated"))
        
        # If we had escalation but now have legitimate intent, reset escalation flags
        if original_escalation_occurred:
            log_with_trace_id(state, f"🔄 Had previous escalation - adding reset flags to output")
            # Include all the state variables that should be reset
            result.update({
                # Escalation flags - reset them
                "is_frustrated": False,
                "needs_escalation": False,
                "needs_human_agent": False,
                # Callback related fields
                "callback_time": None,
                "callback_scheduled": None,
                # Product and order related state
                "product_info": "",
                "selected_order_id": None,
                "known_orders": None,
                "order_status_by_id": None,
                "is_order_query": None,
                # Reset messages to only contain the current message
                "messages": state.get("messages", [])
            })
        
        # Always add trace_id to result
        result["trace_id"] = get_trace_id(state)

        with traced_operation(
            "detect_intent.state_snapshot_output",
            metadata={"client_id": state.get("client_id")},
        ) as _state_out_run:
            set_trace_io(_state_out_run, outputs={"state": snapshot_state_for_trace(result)})

        return result
    except Exception as e:
        # Pull the upstream HTTP code out of the exception so we can:
        #   (a) emit per-code OTel metrics (split 400 vs 404 vs 429 in dashboards)
        #   (b) decide whether to retry with a shorter context (400 only)
        # See _extract_provider_error_code for the supported exception shapes.
        provider_code = _extract_provider_error_code(e)

        # Diagnostic dimensions — log once with everything needed to triage
        # the next occurrence without having to instrument again. Lengths are
        # cheap to compute, never None, and let us distinguish context-overrun
        # (huge sysprompt+history) from content-policy / transport failures.
        msg_len = len(current_message or "")
        sysprompt_len = len(system_prompt or "")
        history_chars = len(conversation_history or "")
        history_turns = len(recent_messages)

        log_with_trace_id(
            state,
            (
                f"❌ Error in intent detection: {str(e)} "
                f"[code={provider_code} client_id={state.get('client_id')} "
                f"msg_len={msg_len} sysprompt_len={sysprompt_len} "
                f"history_chars={history_chars} history_turns={history_turns}]"
            ),
            "error",
        )
        _raw = output.content if 'output' in locals() else None
        log_with_trace_id(
            state,
            f"📄 Full response was: {repr(_raw)} (len={len(_raw) if _raw else 0})",
            "error",
        )

        # Emit per-code counter. Wrapped so a broken metrics pipeline can
        # never break the request path.
        try:
            from fashion_bot.monitoring.otel_metrics import (
                llm_provider_error_counter,
                get_llm_caller,
            )
            llm_provider_error_counter.add(
                1,
                {
                    "client_id": str(state.get("client_id") or "unknown"),
                    "caller": get_llm_caller(),
                    "code": str(provider_code) if provider_code is not None else "unknown",
                },
            )
        except Exception:
            pass

        # ── 400-only retry with truncated history ─────────────────────────
        # 400 from the upstream provider is most commonly context-overrun
        # (huge system prompt + 20-turn history + long user message exceeding
        # the model window). Retry once with last 5 turns only — keeps the
        # current user message and the most recent context, drops older
        # history that is unlikely to change the routing decision anyway.
        # We do NOT retry on 404 (transient upstream availability, not
        # request-shape) or other codes (auth, network, etc.).
        if provider_code == 400 and len(recent_messages) > 5:
            try:
                short_recent = recent_messages[-5:]
                short_history = _build_conversation_history_string(
                    short_recent,
                    template_messages_context,
                    previous_intent,
                )
                short_messages = [SystemMessage(content=system_prompt)]
                if context_section:
                    short_messages.append(SystemMessage(content=context_section))
                short_messages.append(
                    SystemMessage(
                        content=f"History (last {len(short_recent)}):\n{short_history}"
                    )
                )
                short_messages.append(HumanMessage(content=user_message))

                log_with_trace_id(
                    state,
                    (
                        f"🔁 Retrying intent detection with truncated history "
                        f"({history_turns} → {len(short_recent)} turns, "
                        f"history_chars: {history_chars} → {len(short_history)})"
                    ),
                    "warning",
                )

                with traced_operation(
                    "detect_intent.llm_invoke_retry_400",
                    run_type="llm",
                    metadata={"client_id": state.get("client_id")},
                ):
                    retry_output = await intent_llm.ainvoke(short_messages)

                retry_content = retry_output.content.strip()
                try:
                    retry_result = json.loads(retry_content)
                except json.JSONDecodeError:
                    retry_match = re.search(r"\{.*\}", retry_content, re.DOTALL)
                    if not retry_match:
                        raise Exception("No valid JSON in truncated-history retry response")
                    retry_result = json.loads(retry_match.group(0))

                retry_result = expand_minimal_response(retry_result)
                retry_result["trace_id"] = get_trace_id(state)
                if state.get("conversation_context"):
                    retry_result["conversation_context"] = state["conversation_context"]

                log_with_trace_id(
                    state,
                    f"✅ Intent detection recovered via truncated-history retry: "
                    f"parent={retry_result.get('parent_intent', '?')}",
                    "info",
                )

                with traced_operation(
                    "detect_intent.state_snapshot_output",
                    metadata={"client_id": state.get("client_id"), "retry": "truncated_history"},
                ) as _state_retry_run:
                    set_trace_io(
                        _state_retry_run,
                        outputs={"state": snapshot_state_for_trace(retry_result)},
                    )

                return retry_result
            except Exception as retry_exc:
                # Retry failed too — fall through to the safe-default below.
                # Log at error so the failure is visible without changing
                # the user-facing behaviour.
                log_with_trace_id(
                    state,
                    f"❌ Truncated-history retry also failed: {str(retry_exc)}",
                    "error",
                )

        report_error(
            "Error in intent detection node",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            trace_id=get_trace_id(state),
            client_id=state.get("client_id"),
            node="intent_detection_node",
        )
        result = {
            "parent_intent": "",
            "detected_intents": [],
            "has_unknown": True,
            "detected_tags": []
        }
        # Preserve conversation_context even on error
        if state.get("conversation_context"):
            result["conversation_context"] = state["conversation_context"]

        with traced_operation(
            "detect_intent.state_snapshot_output",
            metadata={"client_id": state.get("client_id"), "error": True},
        ) as _state_err_run:
            set_trace_io(_state_err_run, outputs={"state": snapshot_state_for_trace(result)})

        return result
