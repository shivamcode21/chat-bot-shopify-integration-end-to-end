"""
Async Tag Generator — Runs AFTER reply is sent to user.
Uses the full main LLM (LLM_MODEL) on the BACKGROUND OpenRouter key for tag
classification — the smaller/utility model was too weak at fine-grained
policy-vs-action tag distinctions. Running post-reply keeps it off the reply path.
Scoped by client_id for multi-tenant support.

This module decouples tag generation from intent detection,
so tagging does NOT add latency to the webhook reply path.
"""

import asyncio
import logging
import json
import time
from typing import Optional
from langsmith.run_helpers import tracing_context

logger = logging.getLogger(__name__)

TAG_CLASSIFICATION_PROMPT = """You are a conversation tag classifier for a customer support system.

Classify the interaction with the MOST appropriate tag, based on the CUSTOMER'S
intent. Also extract any order ID mentioned.

AVAILABLE TAGS (name: definition):
{tags_dict}

HOW TO CHOOSE:
- The CUSTOMER MESSAGE is the primary signal. Classify the customer's intent.
- The bot reply is provided only as weak background context to disambiguate a
  terse customer message. NEVER pick a tag just because a word appears in the
  bot reply. The bot may mention "return", "exchange", "delivery", etc. while
  answering a general or policy question — that does NOT make it an action tag.
- Read each tag's definition and match it literally. Prefer the tag whose
  definition fits the customer's actual situation.
- Pick the SINGLE best tag from the list above. Use its EXACT name.

POLICY QUESTION vs ACTION REQUEST (very important):
- A hypothetical or informational question — "can I…?", "what if…?", "how does
  it work?", "is it possible to…?", asking about rules, terms, timelines, or
  eligibility — is a POLICY / general-information intent. Tag it with the
  matching *policy* tag (e.g. a return/exchange policy tag), NOT an action tag.
- An ACTION request tag (e.g. "Exchange Request", "Return Request",
  "Cancellation Requests", "Order Update") requires the customer to actually
  want to perform that action on a SPECIFIC existing/received order. If the
  customer has no order and is only asking whether/how something can be done,
  it is a policy question, not an action request.
- Only extract an order_id and only choose an action tag when the customer is
  clearly acting on a concrete order.

EXAMPLES (illustrative — always pick from the AVAILABLE TAGS above):
- Customer: "If I get a size problem, can I easily change it?" (no order)
  → policy/return-exchange-policy tag (asking about the exchange policy),
    order_id="". NOT "Exchange Request".
- Customer: "What is your return policy?" / "How many days do I have to return?"
  → return/exchange policy tag, order_id="".
- Customer: "I want to exchange order GV1234 for a larger size, it arrived today"
  → "Exchange Request", order_id="GV1234".
- Customer: "Where is my order #GV555?"
  → order status / order details tag, order_id="GV555".

- Extract the order ID if one is mentioned (e.g. #1234, order 1234, GRV-1234,
  ORD-5678, GV16819, etc.). Look for numeric IDs or alphanumeric order refs.

OUTPUT: Return ONLY a JSON object with no extra text:
{{"tag": "<exact tag name from list above>", "order_id": "<order ID if found, else empty string>"}}

If no tag clearly matches, return:
{{"tag": "", "order_id": ""}}
"""


async def classify_interaction_tag(
    *,
    client_id: str,
    user_message: str,
    bot_reply: str = "",
    trace_id: str = "",
) -> dict:
    """
    Classify one customer interaction using the configured client tag dictionary.
    Returns {"tag": str, "order_id": str}.
    """
    from fashion_bot.tag_manager import aensure_tags_loaded

    tags_dict = await aensure_tags_loaded(client_id)
    if not tags_dict:
        logger.info(f"[{trace_id}] No tags configured for client {client_id}, skipping tagging")
        return {"tag": "", "order_id": ""}

    from fashion_bot.env_loader import get_bool
    from langchain_core.messages import SystemMessage, HumanMessage

    if get_bool("LOAD_TEST_MODE", False):
        from load_tests.mock_llm import MockChatModel
        tag_llm = MockChatModel.get_singleton()
    else:
        from fashion_bot.core.llm_config import (
            get_smaller_llm_config,
            BACKGROUND_OPENROUTER_KEY_ENV,
        )
        from fashion_bot.core.llm_factory import LLMFactory

        # Uses SMALLER_LLM_PROVIDER / SMALLER_LLM_MODEL (default: openai/gpt-4o-mini).
        # Background workload → BACKGROUND OpenRouter key (key 2).
        # The openai/ model prefix forces OpenRouter to route to OpenAI,
        # avoiding Google AI Studio geo-restriction on Render's Singapore egress.
        tag_config = get_smaller_llm_config(
            temperature=0,
            max_tokens=80,
            api_key_env_var=BACKGROUND_OPENROUTER_KEY_ENV,
        )

        tag_llm = LLMFactory.get_llm(
            tool_name="tag_generation",
            override_config=tag_config,
        )

    prompt = TAG_CLASSIFICATION_PROMPT.format(tags_dict=json.dumps(tags_dict))
    # Customer message is the PRIMARY classification signal. The bot reply is
    # passed only as weak background context (and truncated) so a word in the
    # reply — "exchange", "return", "delivery" — cannot by itself flip a policy
    # question into an action tag. See TAG_CLASSIFICATION_PROMPT "HOW TO CHOOSE".
    user_input = (
        "CUSTOMER MESSAGE (primary signal — classify this):\n"
        f"{user_message}\n\n"
        "BOT REPLY (weak background context only — do NOT tag based on this):\n"
        f"{(bot_reply or '')[:300]}"
    )

    _t0 = time.monotonic()
    with tracing_context(enabled=False, parent=False):
        output = await tag_llm.ainvoke([
            SystemMessage(content=prompt),
            HumanMessage(content=user_input),
        ])
    _llm_ms = int((time.monotonic() - _t0) * 1000)

    content = output.content.strip()
    try:
        result = json.loads(content)
    except json.JSONDecodeError:
        import re

        match = re.search(r'\{.*\}', content, re.DOTALL)
        result = json.loads(match.group(0)) if match else {"tag": "", "order_id": ""}

    tag = result.get("tag", "")
    order_id = result.get("order_id", "")
    if tag:
        logger.info(f"[{trace_id}] tag={tag} order_id={order_id or 'none'} llm_ms={_llm_ms}")
    return {"tag": tag or "", "order_id": order_id or ""}


async def _arun_tag_generation(
    client_id: str,
    conversation_id: str,
    phone_number: str,
    user_message: str,
    bot_reply: str,
    trace_id: str = ""
):
    """
    Async tag generation — runs as a background task.

    1. Load tag definitions (cached — near-zero cost)
    2. Call gemini-2.5-flash-lite
    3. Write to PostgreSQL + Redis state
    """
    try:
        classification = await classify_interaction_tag(
            client_id=client_id,
            user_message=user_message,
            bot_reply=bot_reply,
            trace_id=trace_id,
        )
        tag = classification.get("tag", "")
        order_id = classification.get("order_id", "")

        if not tag and not order_id:
            logger.info(f"[{trace_id}] No tag or order_id classified for conversation {conversation_id}")
            return

        detected_tags = [tag] if tag else []
        logger.info(f"[{trace_id}] tag={tag or 'none'} order_id={order_id or 'none'} conv={conversation_id[:8] if conversation_id else '?'}")

        # 4. Write to PostgreSQL
        from fashion_bot.history.postgres_conversations import (
            aupdate_conversation_tags,
            aupdate_message_tags,
            aupdate_message_metadata,
        )

        if conversation_id:
            _t1 = time.monotonic()
            if detected_tags:
                await aupdate_conversation_tags(conversation_id, detected_tags)
                await aupdate_message_tags(conversation_id, "customer", detected_tags)

            if order_id:
                await aupdate_message_metadata(
                    conversation_id, "customer", {"order_id": order_id}
                )

            logger.info(f"[{trace_id}] Tags/metadata written to DB elapsed_ms={int((time.monotonic() - _t1) * 1000)}")

        # Ensure a cancellation_aversion_events row exists whenever the LLM
        # detects cancellation intent, so the Cancellation Requests page always
        # shows data for every tagged conversation (single source of truth).
        #
        # Also run on a non-cancellation turn that carries an order number, in
        # backfill-only mode. The cancellation tag lands on the turn the customer
        # asks to cancel, which is normally before they have identified the order
        # ("How to cancel my order" → no order yet), so the row is opened with
        # order_id NULL. The order typically arrives a turn or two later under a
        # different tag such as "Order Details Query"; without this second call
        # that turn never reaches the tracker and the row stays blank forever.
        is_cancellation_tag = tag == "Cancellation Requests"
        if conversation_id and (is_cancellation_tag or order_id):
            try:
                from fashion_bot.analytics.cancellation_aversion_tracker import (
                    ensure_event_for_conversation,
                )
                await ensure_event_for_conversation(
                    client_id=client_id,
                    phone=phone_number,
                    conversation_id=conversation_id,
                    order_id=order_id or None,
                    intent_trigger_msg=user_message[:500] if user_message else "",
                    metadata={"trace_id": trace_id, "source": "async_tag_generator"},
                    create_if_missing=is_cancellation_tag,
                )
            except Exception as _evt_err:
                logger.warning(f"[{trace_id}] Failed to ensure cancellation event (non-fatal): {_evt_err}")

        # 5. Update Redis state (so next message has tag context)
        if detected_tags:
            try:
                from fashion_bot.state_cache import aget_state_by_numbers, aupdate_state

                state = await aget_state_by_numbers(phone_number, client_id)
                if state:
                    # Merge with any existing tags (e.g., hardcoded escalation tags)
                    existing_tags = state.get("conversation_tags") or []
                    merged_tags = list(set(existing_tags + detected_tags))
                    state["conversation_tags"] = merged_tags
                    await aupdate_state(phone_number, client_id, state)
            except Exception as e:
                logger.warning(f"[{trace_id}] Failed to update Redis state with async tags: {e}")

    except Exception as e:
        logger.error(f"[{trace_id}] ❌ Async tag generation failed: {e}")


def generate_tags_async(
    client_id: str,
    conversation_id: Optional[str],
    phone_number: str,
    user_message: str,
    bot_reply: str,
    trace_id: str = "",
):
    """
    Fire-and-forget tag generation.
    Creates an asyncio background task (callers are always in async context).

    Args:
        client_id: Client ID for multi-tenant tag lookup
        conversation_id: Postgres conversation ID to update
        phone_number: User's phone number for Redis state lookup
        user_message: The customer's message text
        bot_reply: The bot's reply text
        trace_id: Trace ID for log correlation
    """
    if not conversation_id:
        logger.warning(f"[{trace_id}] No conversation_id — skipping async tagging")
        return

    from fashion_bot.env_loader import get_bool
    if get_bool("LOAD_TEST_MODE", False):
        return

    task = asyncio.create_task(
        _arun_tag_generation(
            client_id, conversation_id, phone_number,
            user_message, bot_reply, trace_id,
        ),
        name=f"tag-gen-{trace_id}",
    )
    # Suppress unhandled-exception warnings on the fire-and-forget task
    task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)
    logger.info(f"[{trace_id}] 🚀 Async tag generation started (task: {task.get_name()})")
