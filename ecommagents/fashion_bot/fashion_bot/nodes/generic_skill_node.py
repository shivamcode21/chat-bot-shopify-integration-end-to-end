"""
Generic Skill Node for Fashion Bot.

This module provides a factory function that creates skill nodes dynamically
based on agent configuration. It eliminates hardcoding by:
1. Fetching prompts from database/cache
2. Loading tools from the tool registry
3. Building context from conversation_context
4. Using ContextExtractor for automatic entity extraction
5. Appending structured output instructions for context tracking

The structured output instruction tells the LLM to optionally include a
CONTEXT_UPDATE block in its response, which the context extractor uses
to update conversation topics and entities.

Usage:
    # Create a product details node
    product_node = create_generic_skill_node(
        agent_name="product_details",
        topic="product_inquiry",
        entity_type="product"
    )
    
    # Use in graph
    graph.add_node("product_details", product_node)
"""

from typing import Dict, Any, List, Optional, Callable, Tuple, Awaitable
import asyncio
import inspect
import logging
import traceback
import re
import uuid
import json
from datetime import datetime
from types import SimpleNamespace

from langchain_core.messages import AIMessage, ToolMessage, SystemMessage

from fashion_bot.schema import SupportState
from fashion_bot.env_loader import get_bool
from fashion_bot.utils.utils import (
    log_with_trace_id,
    get_trace_id,
    _resolve_tracking_url_placeholder,
    _URL_RE,
    collect_tool_result_urls,
    strip_unsourced_urls,
)
from fashion_bot.utils.utils import aget_agent_prompt_with_caching
from fashion_bot.utils.agent_utils import run_agent_graph
from langgraph.config import get_stream_writer
from fashion_bot.utils.langsmith_tracing import traced_operation, set_trace_io, snapshot_state_for_trace
from fashion_bot.core.llm_factory import LLMFactory
from fashion_bot.core.tool_registry import aget_tools_for_agent, get_agent_config
from fashion_bot.rollbar_config import report_error
from fashion_bot.utils.context_helpers import (
    create_extractor,
    build_context_from_conversation_context,
    build_state_context_for_prompt,
    get_default_prompt_for_agent,
    # New topic management functions
    find_or_create_topic,
    get_active_topic,
    get_other_open_topics,
    build_context_for_skill_node,
    # New entity management functions
    add_selectable_entities,
    add_entities_to_global,
    add_entity_refs_to_topic,
    merge_llm_entity_refs_to_topic,
    determine_focal_entity_from_topic,
    get_focal_entity_from_global,
    build_entity_summary,
    # Action tracking functions
    is_action_tool,
    create_action_dto,
    add_action_to_context,
    extract_action_parameters,
    extract_action_result_summary,
)


# ==================== E-COMMERCE AGENT ROLE ====================
# NOTE: Removed ECOMMERCE_AGENT_ROLE to avoid duplicate/conflicting instructions.
# Each agent now uses ONLY its DB prompt which contains all necessary instructions.
# This prevents confusion and ensures consistent behavior.

# ==================== SUMMARY INSTRUCTION ====================
# This instruction asks LLM for a cumulative conversation summary
SUMMARY_INSTRUCTION_TEMPLATE = """
==== CONVERSATION TRACKING (INTERNAL — never shown to the customer) ====
After your customer-facing reply, append ONE block on its own lines, wrapped EXACTLY
in <B_C_J> ... </B_C_J>, containing STRICT JSON:

<B_C_J>
{{
  "summary": "Updated overall summary of this conversation thread",
  "entities_used": ["Product: Sunfire Denim", "Order: 1234"],
  "status": "open" or "resolved",
  "awaiting": null or "order_id" or "confirmation" or "reason" or "size" or "gender" or "category" or "address" or "phone_number",
  "show_products": ["handle-1", "handle-2"],
  "is_track_order": "yes",
  "is_phone_provided": "yes",
  "phone_number": "<digits from message>"
}}
</B_C_J>

Previous summary: %s

ALWAYS include: "summary", "entities_used", "status", "awaiting".

CONDITIONAL fields — include a key ONLY when it applies to THIS turn, otherwise OMIT the key entirely (do not emit "no"/empty):
- "show_products": array of product handles whose image cards should appear (see PRODUCT CARD DISPLAY rules for when/which). Omit entirely when no cards should show.
- "is_track_order": "yes" ONLY if the customer's latest message is asking to track / locate / get the status of an order. Omit otherwise.
- "is_phone_provided": "yes" ONLY if the customer provided a phone number in THIS latest message. Omit otherwise.
- "phone_number": the phone digits the customer gave in THIS message (only together with is_phone_provided). Omit otherwise.

AWAITING field — what info you need from the user next: order_id / confirmation / reason / size / gender / category / address / phone_number, or null when nothing is pending.

Build on the previous summary — describe what the customer asked, what you did, and the outcome so far. Keep it concise (1-2 sentences). Mark "resolved" only when the task is fully complete.

FORMAT: STRICT JSON inside <B_C_J>...</B_C_J> — double-quoted keys/strings, use null/true/false (not None/True/False), no trailing commas, no comments, and do NOT wrap it in a ```json fence. Example:
<B_C_J>{{"summary": "Helped place order 1234", "entities_used": ["Order: 1234"], "status": "resolved", "awaiting": null}}</B_C_J>
"""

_SUGGESTIONS_BCJ_ADDON = (
    '\n- "suggestions": 2–4 short, clickable next-step prompts to offer the customer'
    " as tiles. Follow the suggestions guidance in your agent instructions. NEVER"
    " repeat a request the customer has already made or a step you already covered"
    " earlier in THIS conversation — re-read the conversation above and make every"
    " suggestion lead to a genuinely NEW next step. Omit entirely when none are useful."
)

logger = logging.getLogger("generic_skill_node")


def _extract_tool_args(raw_args: Any) -> Any:
    """Normalize tool-call arguments into dict/primitive form."""
    if isinstance(raw_args, (dict, list, int, float, bool)) or raw_args is None:
        return raw_args
    if isinstance(raw_args, str):
        stripped = raw_args.strip()
        if not stripped:
            return {}
        try:
            return json.loads(stripped)
        except Exception:
            return raw_args
    return raw_args


def _trace_preview(value: Any, limit: int = 1200) -> str:
    """Compact preview for LangSmith run IO fields."""
    text = str(value).replace("\n", " ")
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def _normalize_llm_content(content) -> str:
    """Normalize LLM content (str or list of parts) to plain string.

    Mirrors langchain_core BaseMessage.text() logic so that multi-part
    responses from Gemini (and similar models) are collapsed into a
    single string before any downstream parsing.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts) if parts else str(content)
    return str(content)


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _ainvoke_llm(llm: Any, messages: List[Any]) -> Any:
    ainvoke = getattr(llm, "ainvoke", None)
    if callable(ainvoke):
        return await ainvoke(messages)
    return await asyncio.to_thread(llm.invoke, messages)


async def _ainvoke_tool(tool: Any, tool_args: Any) -> Any:
    tool_ainvoke = getattr(tool, "ainvoke", None)
    if callable(tool_ainvoke):
        try:
            return await tool_ainvoke(tool_args)
        except TypeError:
            pass
    # FunctionTool fallback: extract the underlying coroutine/function
    fn = getattr(tool, "coroutine", None) or getattr(tool, "func", None) or getattr(tool, "fn", None)
    if fn and callable(fn):
        args = tool_args if isinstance(tool_args, dict) else {}
        result = fn(**args)
        if asyncio.iscoroutine(result):
            return await result
        return result
    if hasattr(tool, "invoke"):
        return await asyncio.to_thread(tool.invoke, tool_args)
    return await asyncio.to_thread(tool, tool_args)


async def _ainvoke_agent_executor(agent_executor: "AgentExecutor", payload: Dict[str, Any]) -> Dict[str, Any]:
    executor_ainvoke = getattr(agent_executor, "ainvoke", None)
    if callable(executor_ainvoke):
        return await executor_ainvoke(payload)
    return await asyncio.to_thread(agent_executor.invoke, payload)


async def _invoke_native_tool_loop(
    llm: Any,
    tools: List[Any],
    system_messages: List[str],
    chat_history: List[Any],
    user_input: str,
    max_iterations: int,
    client_id: str = "",
    force_first_tool: Optional[str] = None,
    fallback_llm: Any = None,
) -> Tuple[str, List[Tuple[Any, Any]], List[Any]]:
    """
    Execute tool-calling loop natively (without AgentExecutor).

    Args:
        force_first_tool: If set to a tool name that exists in ``tools``, the
            first LLM call is bound with ``tool_choice`` so the model is REQUIRED
            to invoke that tool before it can answer. This prevents ungrounded
            (hallucinated) responses for skills that must read from a data source
            first — e.g. policy nodes that must call ``get_policy_information``
            before quoting return/exchange windows.
        fallback_llm: Optional default chat model used as a native LangChain
            failover target. When provided, every model binding in this loop is
            wrapped with ``Runnable.with_fallbacks([...])`` so a primary model
            that errors transparently retries the SAME request on the default
            model. ``None`` disables failover (primary already IS the default, or
            ``LLM_FAILOVER_ENABLED=false``).

    Returns:
        (final_response_text, intermediate_steps, generated_messages)
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    tool_map = {t.name: t for t in tools if hasattr(t, "name")}
    bound_llm = llm.bind_tools(tools)
    if fallback_llm is not None:
        # Native LangChain failover: if the primary's tool-bound model errors
        # (provider down, 429, transient 5xx, …), retry the SAME request on the
        # default model. The result is still a Runnable, so the .ainvoke calls
        # below are unchanged.
        bound_llm = bound_llm.with_fallbacks([fallback_llm.bind_tools(tools)])

    # Optionally force the grounding tool on the first iteration. Only applies
    # when the named tool is actually available for this agent.
    forced_first_llm = None
    if force_first_tool and force_first_tool in tool_map:
        try:
            forced_first_llm = llm.bind_tools(tools, tool_choice=force_first_tool)
            if fallback_llm is not None:
                forced_first_llm = forced_first_llm.with_fallbacks(
                    [fallback_llm.bind_tools(tools, tool_choice=force_first_tool)]
                )
        except Exception as _bind_err:
            # Provider may not support forced tool_choice; fall back gracefully.
            logger.warning(
                f"⚠️ Could not force first tool '{force_first_tool}': {_bind_err}. "
                f"Proceeding without forcing."
            )
            forced_first_llm = None

    run_messages: List[Any] = []
    for sys_msg in system_messages:
        run_messages.append(SystemMessage(content=sys_msg))
    run_messages.extend(chat_history or [])
    run_messages.append(HumanMessage(content=user_input))

    intermediate_steps: List[Tuple[Any, Any]] = []
    final_text = ""

    for iteration_idx in range(max_iterations):
        import time as _t
        _t0 = _t.monotonic()
        with traced_operation(
            "generic_skill.native_loop_llm_invoke",
            run_type="llm",
            metadata={"iteration": iteration_idx + 1},
        ) as llm_run:
            set_trace_io(
                llm_run,
                inputs={
                    "iteration": iteration_idx + 1,
                    "messages_in_context": len(run_messages),
                    "user_input_preview": _trace_preview(user_input, 600),
                },
            )
            # On the first iteration, use the forced-tool binding when configured
            # so the model must ground its answer in a tool result.
            _llm_for_iter = forced_first_llm if (iteration_idx == 0 and forced_first_llm is not None) else bound_llm
            try:
                ai_msg = await _ainvoke_llm(_llm_for_iter, run_messages)
            except Exception as _forced_err:
                if _llm_for_iter is forced_first_llm:
                    # Forced tool_choice was rejected at call time; retry unforced.
                    logger.warning(
                        f"⚠️ Forced tool '{force_first_tool}' invoke failed ({_forced_err}); "
                        f"retrying without forcing."
                    )
                    ai_msg = await _ainvoke_llm(bound_llm, run_messages)
                else:
                    raise
            set_trace_io(
                llm_run,
                outputs={
                    "llm_content_preview": _trace_preview(getattr(ai_msg, "content", ""), 1200),
                    "tool_calls": [tc.get("name") for tc in (getattr(ai_msg, "tool_calls", None) or [])],
                },
            )
        _elapsed = int((_t.monotonic() - _t0) * 1000)
        _tool_names = [tc.get("name") for tc in (getattr(ai_msg, "tool_calls", None) or [])]

        _meta = getattr(ai_msg, "response_metadata", None) or {}
        _usage = _meta.get("token_usage") or _meta.get("usage", {})
        _prompt_tok = _usage.get("prompt_tokens", 0)
        _cached_tok = 0
        _details = _usage.get("prompt_tokens_details") or {}
        if isinstance(_details, dict):
            _cached_tok = _details.get("cached_tokens", 0)
        _cache_pct = int((_cached_tok / _prompt_tok) * 100) if _prompt_tok else 0
        logger.info(
            f"🤖 [LLM] native_loop iter={iteration_idx+1} elapsed_ms={_elapsed} "
            f"tools={_tool_names or 'none'} prompt_tokens={_prompt_tok} "
            f"cached_tokens={_cached_tok} cache_hit={_cache_pct}%"
        )

        run_messages.append(ai_msg)
        if isinstance(ai_msg, AIMessage) and ai_msg.content:
            final_text = ai_msg.text().strip()

        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            break

        seen_tool_calls: set = set()
        for tool_idx, tc in enumerate(tool_calls):
            tool_name = tc.get("name")
            tool_call_id = tc.get("id")
            tool_args = _extract_tool_args(tc.get("args"))

            dedup_key = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
            if dedup_key in seen_tool_calls:
                logger.warning(f"⚠️ [DEDUP] Skipping duplicate tool call: {tool_name}")
                run_messages.append(
                    ToolMessage(
                        content="Already executed in this turn.",
                        tool_call_id=tool_call_id or f"tc_{uuid.uuid4().hex[:8]}",
                    )
                )
                continue
            seen_tool_calls.add(dedup_key)

            tool = tool_map.get(tool_name)
            tool_status = "ok"
            logger.info(f"🧰 [TOOL_INPUT] {tool_name} client_id={client_id} args={json.dumps(tool_args, default=str)[:500]}")
            with traced_operation(
                tool_name or "unknown_tool",
                run_type="tool",
                metadata={
                    "iteration": iteration_idx + 1,
                    "tool_index": tool_idx + 1,
                    "tool_name": tool_name or "unknown_tool",
                },
            ) as tool_run:
                set_trace_io(
                    tool_run,
                    inputs={
                        "tool_name": tool_name or "unknown_tool",
                        "tool_call_id": tool_call_id,
                        "tool_input": tool_args,
                    },
                )
                if not tool:
                    observation = (
                        f"ERROR: Tool '{tool_name}' is NOT available and was NOT executed. "
                        f"Do NOT tell the customer this action was performed — it was not. "
                        f"If the tool is a cart operation (add_to_cart, remove_from_cart, "
                        f"update_cart_quantity), cart actions are not supported on this channel. "
                        f"Respond honestly to the customer based on what actually succeeded."
                    )
                    tool_status = "not_found"
                else:
                    try:
                        if hasattr(tool, "invoke"):
                            observation = await _ainvoke_tool(tool, tool_args)
                        else:
                            observation = await _await_maybe(tool(tool_args))
                    except Exception as tool_err:
                        observation = f"Tool '{tool_name}' failed: {tool_err}"
                        tool_status = "error"
                set_trace_io(
                    tool_run,
                    outputs={
                        "status": tool_status,
                        "output_preview": _trace_preview(observation, 1800),
                    },
                )
            logger.info(f"🧰 [TOOL_OUTPUT] {tool_name} status={tool_status} output={_trace_preview(observation, 500)}")
            intermediate_steps.append(
                (SimpleNamespace(tool=tool_name, tool_input=tool_args), observation)
            )
            if isinstance(observation, dict) and "products" in observation:
                message_content = _format_products_for_llm(
                    observation["products"], observation.get("follow_up")
                )
                summary = observation.get("formatted_text") or observation.get("summary")
                if summary:
                    message_content = f"{summary}\n\n{message_content}"
            elif isinstance(observation, dict) and "text" in observation:
                message_content = observation["text"]
            else:
                message_content = str(observation)
            run_messages.append(
                ToolMessage(
                    content=message_content,
                    tool_call_id=tool_call_id or f"tc_{uuid.uuid4().hex[:8]}",
                    name=tool_name or "unknown_tool",
                )
            )

    # ============= EMPTY-RESPONSE RECOVERY =============
    # Known failure mode of native tool-calling: the model invokes a tool — e.g.
    # ``annotate_order`` (a silent CRM side-effect), writing the answer it meant
    # to give into the note arg — and then emits an EMPTY final assistant
    # message, producing no customer-facing text even though the answer is right
    # there in the loaded context / tool results. Without this guard the caller
    # substitutes a generic "provide more details about your request"
    # clarification, which discards the order context and forces the customer to
    # repeat themselves. Issue one corrective, tool-free LLM call asking the
    # model to reply to the customer using what it already gathered.
    if not final_text and intermediate_steps:
        try:
            recovery_messages = list(run_messages) + [
                SystemMessage(content=(
                    "You called one or more tools but did not write a reply to the "
                    "customer. Using the information already gathered above, write a "
                    "direct, helpful answer to the customer's last message now. Do "
                    "not call any tools and do not ask the customer to repeat "
                    "themselves."
                ))
            ]
            recovery_llm = llm.with_fallbacks([fallback_llm]) if fallback_llm is not None else llm
            recovery_msg = await _ainvoke_llm(recovery_llm, recovery_messages)
            recovered = _normalize_llm_content(getattr(recovery_msg, "content", "")).strip()
            if recovered:
                logger.info(
                    f"🛟 [native_loop] recovered empty response via corrective reply "
                    f"call after {len(intermediate_steps)} tool call(s)"
                )
                final_text = recovered
                run_messages.append(recovery_msg)
        except Exception as _recover_err:
            logger.warning(
                f"⚠️ [native_loop] empty-response recovery failed: {_recover_err}"
            )

    return final_text, intermediate_steps, run_messages


_TOOL_TRACE_OUTPUT_CHARS = 220

# Tools whose output the NEXT turn has to read in FULL, not as a 220-char head.
#
# This trace is the only record of a tool call that survives into the next
# turn: ToolMessage has no branch in state_cache.deserialize_message, so real
# tool messages come back typed as HumanMessage and this SystemMessage is what
# the model actually reads. For most tools a head is plenty -- the follow-up
# turn re-calls and gets fresh data.
#
# get_product_reviews is the exception, and it was measured failing. It returns
# up to 15 reviews for the model to show five at a time, so a "show me more"
# turn has to produce reviews 6-10 -- which sit well past 220 characters. On a
# live run the model was handed the head, had no reviews 6-10 anywhere in
# context, did NOT re-call (gemini-flash-lite skips tool calls that a prompt
# merely asks for), and invented three customers: "Aarav K.", "Sneha P." and
# "Vikram", none of whom had reviewed the product. Fabricating a named customer
# is the worst failure this agent has, and the truncation is what left the
# model with nothing true to say.
#
# So the whole review payload crosses the boundary. The point is not to excuse
# the missing call -- the prompt still asks for one, and a fresh result is
# still authoritative -- it is that when the call does not happen, the model
# has the REAL reviews in front of it instead of a gap to fill. That turns the
# worst case from an invented customer into a repeated one.
#
# Bounded on every axis. Only this tool gets the larger budget; only its LAST
# call in a turn gets it (see _build_tool_trace_system_message -- one turn can
# call a tool repeatedly, e.g. comparing the reviews of two products, and a
# per-call allowance would multiply); the trace holds at most
# _TOOL_TRACE_MAX_CALLS calls; and history is windowed to the last 15 messages
# (messages_list[-16:-1]), so the payload ages out rather than accumulating
# across a conversation. Worst case is therefore ONE elevated payload per turn,
# by construction rather than by luck.
_TOOL_TRACE_OUTPUT_CHARS_BY_TOOL = {"get_product_reviews": 6000}

# How many of a turn's tool calls reach the next turn at all.
_TOOL_TRACE_MAX_CALLS = 6

# Fields carried past the truncation as explicit key=value pairs.
#
# sort_used is here because a model that omitted `sort` cannot otherwise tell
# which tenant default it got, and repeating the call with a different ordering
# reshuffles the list underneath the customer. It stays even though the review
# payload is now carried whole -- it is cheap, and it reads as a fact rather
# than as something to dig out of a dict repr.
_TOOL_TRACE_PRESERVED_KEYS = ("sort_used",)


def _build_tool_trace_system_message(intermediate_steps: List[Tuple[Any, Any]]) -> Optional[SystemMessage]:
    """Build compact tool trace message for persisted context continuity."""
    if not intermediate_steps:
        return None
    steps = list(intermediate_steps[:_TOOL_TRACE_MAX_CALLS])

    # An elevated budget is granted to the LAST call of that tool in the turn,
    # not to every call of it. A turn is free to call one tool repeatedly --
    # "compare the reviews of these two" is an ordinary request -- and granting
    # each call its own allowance multiplies the payload riding into the next
    # turn by however many times the model chose to call. Keeping only the last
    # bounds it at one elevated payload per tool, and the last is the one worth
    # keeping: a follow-up continues from the most recent list, so an earlier
    # call in the same turn has already been superseded.
    last_elevated = {}
    for i, (action, _observation) in enumerate(steps):
        name = getattr(action, "tool", "unknown")
        if name in _TOOL_TRACE_OUTPUT_CHARS_BY_TOOL:
            last_elevated[name] = i

    lines: List[str] = []
    for i, (action, observation) in enumerate(steps):
        tool_name = getattr(action, "tool", "unknown")
        tool_input = getattr(action, "tool_input", {})
        obs_txt = str(observation).replace("\n", " ")
        budget = (
            _TOOL_TRACE_OUTPUT_CHARS_BY_TOOL[tool_name]
            if last_elevated.get(tool_name) == i
            else _TOOL_TRACE_OUTPUT_CHARS
        )
        if len(obs_txt) > budget:
            obs_txt = f"{obs_txt[:budget]}..."
        carried = ""
        if isinstance(observation, dict):
            kept = [
                f"{key}={observation[key]!r}"
                for key in _TOOL_TRACE_PRESERVED_KEYS
                if observation.get(key) is not None
            ]
            if kept:
                carried = " " + " ".join(kept)
        lines.append(f"{tool_name} input={tool_input} output={obs_txt}{carried}")
    content = "[ToolTrace]\n" + "\n".join(lines)
    return SystemMessage(content=content, additional_kwargs={"tool_trace": True, "timestamp": datetime.now().isoformat()})


def _build_tool_trace_struct(intermediate_steps: List[Tuple[Any, Any]]) -> List[Dict[str, Any]]:
    """Structured tool trace for logging/observability."""
    trace: List[Dict[str, Any]] = []
    for action, observation in intermediate_steps[:12]:
        tool_name = getattr(action, "tool", "unknown")
        tool_input = getattr(action, "tool_input", {})
        obs = str(observation).replace("\n", " ")
        if len(obs) > 300:
            obs = f"{obs[:300]}..."
        trace.append(
            {
                "tool": tool_name,
                "input": tool_input,
                "observation_preview": obs,
            }
        )
    return trace


# ==================== AGENT-SPECIFIC PLACEHOLDER FILLERS ====================
# Some agents have dynamic placeholders that must be filled before escaping

def _fill_agent_specific_placeholders(agent_name: str, prompt: str, state: SupportState, current_message: str) -> str:
    """
    Fill agent-specific placeholders in prompts before escaping.
    
    Some agents (return_exchange, cancellation) have dynamic placeholders like:
    - {first_action_instruction}
    - {decision_instruction}
    - {special_case_instruction}
    
    This function fills them based on current state.
    """
    if agent_name == "return_exchange":
        return _fill_return_exchange_placeholders(prompt, state, current_message)
    # Add other agent-specific handlers here as needed
    return prompt


def _fill_return_exchange_placeholders(prompt: str, state: SupportState, current_message: str) -> str:
    """Fill return_exchange-specific placeholders with current state values only."""
    
    # Get state values - just pass the facts, let the LLM decide the flow
    order_id = state.get("selected_order_id") or state.get("order_id")
    phone_number = state.get("phone_number")
    return_exchange_preference = state.get("return_exchange_preference")
    reason_category = state.get("return_reason_category", "")
    exchange_already_suggested = state.get("exchange_already_suggested", False)
    delivered_orders_shown = state.get("delivered_orders_shown", False)
    
    # Replace placeholders in prompt with actual state values
    try:
        filled_prompt = prompt.format(
            first_action_instruction="",  # Let prompt handle flow
            banned_action_instruction="DO NOT call get_customers_delivered_orders_by_phone again!" if delivered_orders_shown else "",
            customer_message=current_message,
            order_id_state=order_id if order_id else "Not provided",
            phone_number_state=phone_number if phone_number else "Not provided",
            preference_state=return_exchange_preference if return_exchange_preference else "Not determined",
            return_reason_state=reason_category if reason_category else "Not provided",
            exchange_suggested_state=exchange_already_suggested,
            customer_declined_exchange_state=state.get('customer_declined_exchange', False),
            delivered_orders_shown_state=delivered_orders_shown,
            decision_instruction="",  # Let prompt handle flow
            special_case_instruction=""  # Let the LLM decide based on message content
        )
        return filled_prompt
    except KeyError as e:
        # If placeholder is missing, log and return original
        logger.warning(f"Missing placeholder in return_exchange prompt: {e}")
        return prompt


# ==================== PHONE EXTRACTION (web chat) ====================
# Agents that need a real customer phone. Web chat users start with
# phone_number="" or a synthetic session id ("web_...", "fbw_..."); the real
# phone is extracted from the message for these agents only.
PHONE_REQUIRING_AGENTS = ["cancel_or_update_order", "return_exchange", "place_order", "order_status"]

# Whether this agent can reach order data — and therefore needs the order-access
# verification instructions when the client opts in — is read off the tool schemas
# by ``order_access.agent_has_gated_order_tools`` rather than a name list here, so
# the instructions can never go missing for a tool that is in fact gated.


def _extract_phone_from_message(current_message: str, existing_phone: str) -> Optional[str]:
    """Extract a normalized 10-digit phone from the message (or None).

    Pure: only computes a value. Returns the phone ONLY when ``existing_phone``
    is empty or a synthetic session id (e.g. "web_..." or "fbw_...").
    Regex digit runs, candidates of length >= 10, strip a leading "91" country
    code when the candidate is longer than 10 digits.
    """
    from fashion_bot.utils.phone_number_utils import is_real_phone_number
    phone_number = existing_phone
    if (not is_real_phone_number(phone_number)) and current_message:
        digits = re.findall(r'\d+', current_message.strip())
        candidates = [d for d in digits if len(d) >= 10]
        if candidates:
            extracted_phone = candidates[0]
            if len(extracted_phone) > 10 and extracted_phone.startswith("91"):
                extracted_phone = extracted_phone[-10:]
            return extracted_phone
    return None


def _escape_braces(text: str) -> str:
    """Escape curly braces for LangChain prompt templates."""
    if not text:
        return text
    return text.replace("{", "{{").replace("}", "}}")


# ==================== PROMPT INSTRUCTION CONSTANTS ====================
# These inline instruction strings affect the LLM; the text is preserved
# character-for-character. The node selects among them based on agent_name /
# channel, but never reworded them.

TOOL_CALLING_INSTRUCTION = """🚨 CRITICAL: YOU MUST USE TOOLS TO ANSWER 🚨
You have access to tools. You MUST call the appropriate tool(s) to get information.
DO NOT respond with a generic message asking for details - USE YOUR TOOLS FIRST.

RULES:
1. NEVER respond with empty message or "Could you please provide more details" without first trying tools.
2. Phone number is already available in context - use it to look up orders.
3. After getting data from tools, ALWAYS respond with that data to the user.
4. If a tool returns error, explain the issue - don't return empty message.
5. Follow [TOOL CHAINS - CRITICAL] section in the prompt strictly.
6. CRITICAL - PRESERVE USER INPUT EXACTLY: When calling any tool with the user's message, pass it EXACTLY as typed - character for character. NEVER duplicate, repeat, or modify letters. If user says "S", pass "S" not "SS". If user says "M", pass "M" not "MM".

"""

REPLY_GUIDELINES_INSTRUCTION = """==== REPLY LANGUAGE (MANDATORY) ====
🚨 STRICTLY match the language of the customer's LATEST message:
- Customer writes in ENGLISH → You MUST reply in ENGLISH only. Do NOT use Hindi or Hinglish.
- Customer writes in HINDI → Reply in simple Hindi.
- Customer mixes Hindi + English → Reply in natural Hinglish.
- When in doubt, default to ENGLISH.
NEVER assume a language preference based on phone number or name. ONLY look at the actual words in the latest message."""

# Skills whose replies surface products visually (carousel cards are scoped here).
CAROUSEL_AGENTS = ("product_details", "recommendations", "place_order")

CAROUSEL_SELECTION_INSTRUCTION = """==== PRODUCT CARD DISPLAY (MANDATORY) ====
You decide when product image cards appear. To show cards, set the "show_products" field in the <B_C_J> tracking block (see CONVERSATION TRACKING) to the list of product handles to display. Do NOT put handles anywhere in the customer-facing text.

- Use ONLY the exact `handle` values that appear in the tool results of THIS turn (or in the conversation context's product entities). Copy each handle VERBATIM — character for character. Maximum 5, ordered by relevance.
- 🚫 NEVER invent, guess, slugify, or reconstruct a handle from a product's name, and NEVER use a handle from memory/training. If a product's handle is not literally present in the tool output, do NOT include it.
- If you are recommending or naming products but did NOT call a product tool this turn (so you have no handles to copy), call the search tool first to get real handles, THEN set "show_products". Do NOT present products as text-only when you could show their cards — if your reply shows products, it MUST show their cards. (You still must never guess/slugify a handle to fill "show_products" — get the real one from a tool.)
- The tracking block is internal and stripped before the customer sees your reply — never mention it.

"SHOW MORE" / "MORE" REQUESTS (MANDATORY — ALWAYS SEARCH FIRST):
- When the customer asks for more / "show more" / "more options" / "other products" / "what else", you MUST call the product search tool again THIS turn to fetch the next set, THEN set "show_products" to the handles it returns.
- NEVER answer a "show more" from memory or the conversation context, and NEVER pivot to suggesting categories/collections or replying "that's all"/"that covers it" WITHOUT calling the search tool first.
- Even if you believe everything has already been shown, still run the search — the tool decides whether new items remain and will re-return the existing set when there are none; in that case show those returned products' cards again rather than an empty carousel. Only after the search runs may you decide what to display.

EMIT the block (show cards) ONLY when seeing the product visually helps the customer decide or discover, e.g. presenting search results or recommendations, or when they ask to see / browse / compare specific products — AND you have the real handles from this turn's tool results to copy.

DO NOT emit the block (no cards) when:
- Handling orders: placement, updates/size changes, cancellations, tracking, returns/exchanges, delivery timelines, payment.
- Answering policy, greeting, thanks, or clarifying questions.
- The customer asks a follow-up (price, fabric, fit, size, availability, "tell me more") about a product that already has a card on screen.

ALREADY-DISPLAYED CARDS:
- The conversation context may include a section "CARDS ALREADY DISPLAYED THIS SESSION" listing handles whose images are already on screen.
- Do NOT repeat handles that are ALREADY on screen — the customer can already see them. This applies ONLY to those already-displayed handles; it must NEVER stop you from showing NEW products. On a "show more" / "more like these" turn you MUST still include the NEW products from this turn's results in "show_products". Never let the already-displayed rule cause an empty "show_products" when you have new products to show.
- EXCEPTION (no explicit request needed): whenever you ran a product search THIS turn and it returned products, you MUST put those tool-returned handles in "show_products" — on EVERY "show me X" / "show more" / "more like these" turn, even if some or all of those products were already shown before. If the search tool returns products, show their cards; NEVER leave "show_products" empty just because the items were already on screen. The "do not repeat already-shown" rule above applies ONLY when you did NOT run a product search this turn (an incidental mention or a follow-up question about a card already on screen) — it must never suppress the results of an actual search.

ALWAYS set "show_products" whenever your reply shows products. If your customer-facing reply presents, lists, recommends, names, or browses specific products — including every search-results, "show me X", and "show me more" turn — you MUST include those products' real handles in "show_products" (from this turn's tool results or the conversation context entities). Showing products in your text without their cards is not allowed. ONLY leave "show_products" empty when your reply shows NO products (orders, policy, greetings/thanks, or a pure follow-up question about a product whose card is already on screen)."""

PRODUCT_LINK_INSTRUCTION_WEB = """==== PRODUCT LINK FORMATTING (MANDATORY — WEB CHAT) ====
This is a web chat session — markdown renders here. Whenever your reply lists or names a specific product (from tool results, recent_products, or the conversation context), format the product's NAME as a markdown hyperlink to its product URL so the customer can click the name to open the product page:

  1. [Product Name](<PRODUCT_URL_FROM_TOOL_RESULT>) — ₹price

Apply this EVERY time you mention a specific product by name in the reply — including follow-up turns about a product whose card was already shown earlier. The link is the customer's only clickable path to the product when the carousel card is suppressed.
The URL MUST come verbatim from the product's `url` field in tool results / recent_products / context — never construct one from a handle, never reuse a placeholder domain from these instructions, never guess a domain. If a product's URL is genuinely not available in the tool results or context, show the name plain (no link). NEVER invent or guess URLs.
This SUPERSEDES any earlier prompt instruction about "plain URLs" or "no markdown links" — those rules were for WhatsApp; markdown links are the correct format in this channel."""

PRODUCT_LINK_INSTRUCTION_DEFAULT = """==== PRODUCT LINK FORMATTING (MANDATORY) ====
Whenever your reply lists or names a specific product (from tool results or conversation context), include the product's plain URL on its own line right after the title so the customer can click through:

  1. Product Name — ₹price
     <PRODUCT_URL_FROM_TOOL_RESULT>

Apply this every time you mention a specific product by name. The URL MUST come verbatim from the product's `url` field in tool results / recent_products / context — never construct one from a handle, never reuse a placeholder domain from these instructions, never guess a domain. Do NOT use markdown link syntax (this channel does not render markdown). If a URL is not available, omit it and just show the name — never invent URLs."""

CART_ACTION_GUARDRAIL_INSTRUCTION = """==== CART ACTION CONFIRMATION (MANDATORY) ====
The cart tools (add_to_cart, remove_from_cart, update_cart_quantity) each return a result with a "success" field. Your confirmation to the customer MUST match it:
- Tell the customer a cart change happened ONLY when the tool returned success=True (queued=True).
- If a cart tool returns success=False, do NOT claim the change was made. Read its "error"/"message", fix the call if you can (e.g. call get_cart to get the exact variant_id of the line, then retry), and only confirm once a retry returns success=True. If it still fails, tell the customer it could not be done and what you need from them — never say it was done.
- SIZE / VARIANT SWAPS: a swap is BOTH an add of the new variant AND a remove of the old one. Do NOT tell the customer you "changed it to <new size>" unless the remove of the OLD variant returned success=True. If the remove failed, the old item is still in the cart — say that and resolve it, rather than claiming a clean swap.
- SIZE / COLOUR CONFIRMATION BEFORE ADDING: A product often comes in multiple variants (sizes and/or colours). NEVER guess, assume, or default a size or colour the customer has not explicitly chosen. When you call add_to_cart, pass the customer's chosen options in `selected_options` (e.g. {"Size": "M"} or {"Color": "Blue", "Size": "S"}), mapping the customer's own words to the product's option value yourself ("small" → "S", "the black one" → "Jet Black"). If the customer has NOT told you their size/colour yet, ASK them first — do not add anything. If add_to_cart returns error "variant_options_required" or "variant_options_mismatch", do NOT tell the customer the item was added: ask for (or correct) the missing size/colour, then retry with the matching variant_id and selected_options.
- VARIANT ID SOURCE (CRITICAL): NEVER call add_to_cart with a variant_id from conversation history or memory. ALWAYS call find_product_by_id (or search_products) FIRST in THIS turn for the product the customer wants, read the exact variant_id from the tool's returned "variants" list, and THEN call add_to_cart with that value. Even if you can see a variant_id in a previous tool result, you MUST re-fetch it — stale IDs are rejected."""

PRODUCT_LINE_INFERENCE_NOTE = """

⚠️ PRODUCT LINE INFERENCE (CRITICAL):
When searching for products, ALWAYS check the conversation history and context for a specific
model or product variant. If the customer previously ordered, browsed, or discussed a product
for a specific model/variant, carry that forward as the `product_line` parameter in
search_products_by_name — even if the current message only mentions a color, material, or style.
This ensures results are filtered to the correct variant and not mixed with other models."""


def _build_channel_context_block(state: Dict, is_web_chat_session: bool) -> str:
    """Authoritative, code-derived channel facts for the LLM.

    The base agent prompts (from Postgres) are channel-agnostic — the only channel
    signal they otherwise receive is implicit in the link-format block. This block
    states the channel and its rendering capabilities EXPLICITLY so a prompt can
    branch deterministically (e.g. "carousel-only on web, numbered text list on
    WhatsApp") without having to infer the channel itself.

    Derived from the same signals the rest of the node already uses — the
    ``web_``/``fbw_`` phone prefix plus ``resolve_channel_from_state`` — so the
    channel label and its capabilities can never drift apart. ``carousel`` and
    ``markdown`` availability track "is this the web chat widget", mirroring the
    existing PRODUCT_LINK_INSTRUCTION_WEB vs _DEFAULT split. Any non-web / unknown
    channel is treated as carousel-UNAVAILABLE, so a prompt that keys off this
    fact always falls back to the text list and products are never lost.
    """
    try:
        from fashion_bot.utils.escalation_helper import resolve_channel_from_state
        channel = resolve_channel_from_state(state)
    except Exception:
        channel = None
    if not channel:
        channel = "web-chat" if is_web_chat_session else "unknown"

    is_web = bool(is_web_chat_session) or channel == "web-chat"
    if is_web:
        carousel_line = (
            "Product carousel (visual image cards): AVAILABLE — a card renders directly "
            'below your message for every handle you put in "show_products".'
        )
        markdown_line = "Markdown: RENDERS — use [text](url) hyperlinks."
        cart_line = "Cart operations (add_to_cart, checkout link): AVAILABLE."
    else:
        carousel_line = (
            "Product carousel (visual image cards): NOT AVAILABLE — your reply text is the "
            "ONLY way products can appear to the customer."
        )
        markdown_line = "Markdown: DOES NOT RENDER — use plain bare URLs (never [text](url) syntax)."
        cart_line = (
            "Cart operations: NOT AVAILABLE on this channel. "
            "Do NOT add items to cart, share /cart or checkout links, or claim items were added to cart. "
            "If the customer wants to buy, help them place a COD order instead."
        )

    return (
        "==== CHANNEL CONTEXT (SYSTEM-PROVIDED, AUTHORITATIVE) ====\n"
        f"Channel: {channel}\n"
        f"{carousel_line}\n"
        f"{markdown_line}\n"
        f"{cart_line}\n"
        "These facts are set by the system for THIS message. When your instructions include "
        "channel-conditional rules, apply them using these facts — never guess the channel yourself."
    )


def _build_current_datetime_block() -> str:
    """Authoritative, system-provided current date/time (IST) for the LLM.

    The base agent prompts (from Postgres/LangSmith) tell the LLM to quote
    delivery / dispatch timelines as day-ranges ("dispatch in 7-10 days",
    "delivery in 15-20 days"). The LLM has NO other notion of what "today" is,
    so when a customer asks "which exact date will it arrive?" it INVENTS a
    calendar date — and, with no anchor, that date can land in the past.

    Production incident: on 25 Jul the bot told a customer their order would
    arrive "15-20 July" (already in the past) because nothing in the prompt
    grounded it to the real current date. This block supplies that anchor and
    the arithmetic rule so any date the LLM emits is computed from today, never
    guessed. Mirrors ``_build_channel_context_block`` — a code-derived fact the
    prompt can rely on instead of hallucinating.
    """
    try:
        import pytz
        now_ist = datetime.now(pytz.timezone("Asia/Kolkata"))
    except Exception:  # pragma: no cover - defensive, pytz should always import
        now_ist = datetime.now()

    today_str = now_ist.strftime("%A, %d %B %Y")  # e.g. "Friday, 25 July 2026"
    iso_str = now_ist.strftime("%Y-%m-%d")

    return (
        "==== CURRENT DATE & TIME (SYSTEM-PROVIDED, AUTHORITATIVE) ====\n"
        f"Today is {today_str} (IST). Current date (ISO): {iso_str}.\n"
        "This is the ONLY correct value for \"today\" — your training data is stale, "
        "so never assume any other current date.\n"
        "Rules for any date you state to the customer:\n"
        "- Delivery / dispatch / return / refund timelines are expressed in the prompts as "
        "day-ranges (e.g. \"7-10 days\", \"15-20 days\"). PREFER quoting them as day-ranges.\n"
        "- If — and only if — you convert a day-range into calendar dates, compute them by "
        "ADDING the days to today's date above. A 15-20 day window from today therefore lands "
        f"15-20 days AFTER {iso_str}.\n"
        "- NEVER state a delivery/dispatch/arrival date that is on or before today's date. "
        "A future order cannot arrive in the past. If your arithmetic produces a past date, you "
        "made an error — recompute from today.\n"
        "- Never guess, invent, or recall a specific calendar date. Only use dates that are "
        "either (a) computed from today's date above, or (b) returned verbatim by a tool."
    )


async def _fetch_place_order_customer_updates(phone_number: str, state) -> Dict[str, Any]:
    """Auto-fetch place_order customer data from Shopify, returning state updates.

    Pure-of-state-mutation: performs the Shopify lookup (I/O) and RETURNS a dict
    of state updates for the node to apply; it never mutates ``state`` itself.
    Reads existing ``state`` values only to preserve the original "only fill if
    not already set" guards. Emits the SAME logs. ``shopify_fetch_attempted`` is
    set to True on both success and exception, matching the original block.
    """
    updates: Dict[str, Any] = {}
    log_with_trace_id(state, f"📱 [place_order] Phone in state: {phone_number} - Auto-fetching customer from Shopify")
    try:
        from fashion_bot.core.orchestrator import CustomerOrchestrator
        result = await CustomerOrchestrator.afetch_customer_by_phone(phone_number, state=state)
        updates["shopify_fetch_attempted"] = True

        if result.get("found") or result.get("success"):
            log_with_trace_id(state, f"✅ Found returning customer in Shopify")

            cust_name = result.get("customer_name")
            cust_address = result.get("customer_address")

            if cust_name and not state.get("customer_name"):
                updates["customer_name"] = cust_name
                log_with_trace_id(state, f"✅ Auto-filled customer name: {cust_name}")

            if cust_address and not state.get("customer_address"):
                updates["customer_address"] = cust_address
                log_with_trace_id(state, f"✅ Auto-filled customer address: {cust_address[:50]}...")

            updates["is_returning_customer"] = True
        else:
            log_with_trace_id(state, f"❌ No customer found in Shopify for {phone_number}")
    except Exception as e:
        log_with_trace_id(state, f"⚠️ Error fetching customer from Shopify: {e}")
        updates["shopify_fetch_attempted"] = True
    return updates


def _infer_focal_from_response_text(response_content: str, entities: list) -> Tuple[Optional[str], int]:
    """Infer the focal product entity first mentioned in the response text.

    Pure: returns (entity_id_or_None, earliest_pos). The node keeps the override
    decision and the exact log line (using the returned position).
    """
    _FOCAL_ENTITY_TYPES = ("product", "selectable_product")
    resp_lower = response_content.lower()
    earliest_pos = len(resp_lower)
    response_inferred_focal = None
    for entity in entities:
        if entity.get("entity_type") not in _FOCAL_ENTITY_TYPES:
            continue
        ename = (entity.get("entity_value") or "").lower()
        if not ename or len(ename) < 4:
            continue
        pos = resp_lower.find(ename)
        if 0 <= pos < earliest_pos:
            earliest_pos = pos
            response_inferred_focal = entity.get("entity_id")
    return response_inferred_focal, earliest_pos


def _extract_recent_products(intermediate_steps) -> list:
    """Return the products list from the first step-observation that has one.

    Pure: does NOT call add_selectable_entities. Same "break on first" logic.
    """
    for step in intermediate_steps:
        if len(step) < 2:
            continue
        obs = step[1]
        if isinstance(obs, dict) and obs.get("products"):
            return obs["products"]
    return []


def _extract_tool_call_names(intermediate_steps) -> List[str]:
    """Return tool names from intermediate steps, deduped, order-preserving."""
    tool_calls: List[str] = []
    for step in intermediate_steps:
        if len(step) < 2:
            continue
        action = step[0]
        tool_name = getattr(action, 'tool', None) or (action.get('tool') if isinstance(action, dict) else None)
        if tool_name and tool_name not in tool_calls:
            tool_calls.append(tool_name)
    return tool_calls


# State fields preserved on the response when truthy in state (order matters for
# observable ordering of response.update keys). Split into the two original
# groups; the needs_escalation check stays between them in the node.
PASSTHROUGH_STATE_FIELDS_PRE_ESCALATION = [
    "selected_order_id",
    "phone_number",
    # Order-access grant written by utils/order_access.py during the tool loop.
    # Carried back so a verified customer isn't re-challenged next message.
    "order_auth",
    "return_exchange_preference",
    "delivered_orders_shown",
    "reason_already_provided",
    "return_reason_category",
    "exchange_already_suggested",
]
PASSTHROUGH_STATE_FIELDS_POST_ESCALATION = [
    "requested_size",
    "selected_variant_id",
    "quantity",
    "payment_mode",
    "awaiting_payment_mode",
    "awaiting_prepaid_confirmation",
    "selected_product",
    "scratchpad",
]


def _passthrough_state_updates(state, fields: List[str]) -> Dict[str, Any]:
    """Return {field: state[field]} for each field that is truthy in state."""
    updates: Dict[str, Any] = {}
    for field in fields:
        if state.get(field):
            updates[field] = state[field]
    return updates


async def _web_chat_escalation_override(response_content, state) -> Tuple[str, str]:
    """Compute the web-chat escalation message + contact details.

    Returns (response_content, web_contact_details). Caller invokes this only
    under the same agent_name=="escalation" and is_web_chat conditions.

    This override REPLACES whatever the LLM wrote, so the prompt cannot change
    the wording here — a client who does not want the default "someone will be
    in touch" promise sets ``escalation_messaging.customer_message`` and that
    text is used instead. The support phone/email from ``vendor_contact_details``
    are appended either way.
    """
    log_with_trace_id(state, f"📱 Web chat escalation detected - using simplified message")
    from fashion_bot.agent_config import aget_escalation_customer_message

    response_content = await aget_escalation_customer_message(
        state.get("client_id"),
        default="This seems important. Someone from our support team will be in touch with you shortly.",
    )
    web_contact_details = ""
    try:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        web_contact_details = await get_contact_details_message(client_id=state.get("client_id"))
        if web_contact_details:
            response_content = f"{response_content}\n\n{web_contact_details}"
        log_with_trace_id(state, f"✅ Web chat escalation message prepared")
    except Exception as contact_err:
        log_with_trace_id(state, f"⚠️ Could not get contact details for web chat escalation: {contact_err}", "warning")
        web_contact_details = ""
    return response_content, web_contact_details


_GUEST_ESCALATION_FOLLOW_UP_HINT = (
    "==== ESCALATION FOLLOW-UP HINT (INTERNAL) ====\n"
    "This customer is chatting from a web browser without a linked phone number, "
    "and no previous escalation was found under their current session.\n"
    "IF — and ONLY if — the customer's message is asking about a previous "
    "escalation, support ticket, complaint, or follow-up (e.g. 'any update?', "
    "'I raised an issue earlier', 'what happened to my complaint?'), then ask:\n"
    "'Could you please share the email address or phone number you provided "
    "when you contacted us earlier? That will help me find your case.'\n"
    "If the customer's message is about something else entirely (product question, "
    "new order, general chat), IGNORE this block completely."
)


def _start_escalation_prefetch(
    state: SupportState,
    client_id: Optional[str],
    current_message: str,
) -> Optional[asyncio.Task]:
    """Kick off the escalation snapshot read as a concurrent background task.

    Started early (right after ``client_id`` is resolved) and awaited ~300 lines
    later when the prompt is assembled.  Everything in between — prompt fetch,
    tool loading, LLM resolution, store context — is awaited I/O, so this rides
    along instead of adding to the turn.

    Returns ``None`` on any failure; the caller treats that as "no escalation
    data" and proceeds unchanged.  The task is read-only and safe to abandon if
    the node returns early.
    """
    try:
        from fashion_bot.utils.escalation_context import aprefetch_escalation_context

        return asyncio.create_task(
            aprefetch_escalation_context(
                client_id=client_id,
                phone_number=str(state.get("phone_number") or ""),
                trace_id=get_trace_id(state),
                user_message=current_message,
            )
        )
    except Exception as _err:  # pragma: no cover - defensive
        log_with_trace_id(
            state,
            f"⚠️ [escalation_context] prefetch not started: {_err}",
            "warning",
        )
        return None


async def _resolve_escalation_context(
    state: SupportState,
    prefetch_task: Optional[asyncio.Task],
    tools: Optional[List],
    agent_name: str,
) -> Tuple[str, List, int]:
    """Await the prefetched escalation snapshot, render the block, and top up tools.

    Returns ``(block_text, updated_tools)`` where ``block_text`` is empty if the
    customer has no escalation history (or the feature is disabled/errored).
    Extracted to keep the main node body focused on the orchestration flow.

    Sample block (unresolved)::

        ==== ESCALATION CONTEXT (INTERNAL — NEVER MENTION … TO THE CUSTOMER) ====
        This customer has 1 issue that was handed to the human support team.

        [E1] Delivery Query · order gv16384 · first raised 2026-07-24 11:20 IST (2 days ago)
             STATUS: UNRESOLVED — no human update recorded yet
             Handed over as: "Customer waiting 13 days, no tracking movement"
             Customer has already chased this 1 time.

        HOW TO USE THIS — follow in order:
        1. FIRST decide which ONE of these … the customer's CURRENT message is about.
        …

    Sample block (resolved)::

        [E1] Cancellation Requests · order gv16101 · first raised 2026-07-20 09:12 IST
             STATUS: RESOLVED by the support team on 2026-07-21 14:03 IST
             Action taken: "reverted and dispatch today"

    When no escalation exists the block is ``""`` and the turn is byte-identical
    to the pre-feature behaviour.
    """
    escalation_context_block = ""
    tools = list(tools or [])
    with traced_operation(
        "generic_skill.resolve_escalation_context",
        metadata={
            "agent_name": agent_name,
            "client_id": state.get("client_id"),
            "has_prefetch": prefetch_task is not None,
        },
    ) as esc_run:
        try:
            from fashion_bot.utils.escalation_context import (
                build_threads,
                render_escalation_context_block,
            )

            _esc_bundle = await prefetch_task if prefetch_task else None
            record_count = len(_esc_bundle.records) if _esc_bundle and _esc_bundle.records else 0
            unresolved_count = (
                sum(1 for r in _esc_bundle.records if r.is_unresolved)
                if _esc_bundle and _esc_bundle.records
                else 0
            )

            if _esc_bundle and _esc_bundle.records:
                escalation_context_block = render_escalation_context_block(
                    build_threads(_esc_bundle.records),
                    human_replies=_esc_bundle.human_replies,
                )
                if _esc_bundle.has_unresolved and not any(
                    getattr(t, "name", "") == "escalate_to_agent" for t in tools
                ):
                    from fashion_bot.tool_factory import _create_escalation_tool

                    tools = tools + [_create_escalation_tool(state, agent=agent_name)]
                    log_with_trace_id(
                        state,
                        f"🔔 [escalation_context] added escalate_to_agent to {agent_name} "
                        f"(customer has an unresolved escalation)",
                    )
            elif not record_count:
                _phone = str(state.get("phone_number") or "")
                if _phone.startswith(("web_", "fbw_")):
                    escalation_context_block = _GUEST_ESCALATION_FOLLOW_UP_HINT

            set_trace_io(
                esc_run,
                inputs={"agent_name": agent_name, "had_prefetch": prefetch_task is not None},
                outputs={
                    "record_count": record_count,
                    "unresolved_count": unresolved_count,
                    "block_length": len(escalation_context_block),
                    "tool_topped_up": any(
                        getattr(t, "name", "") == "escalate_to_agent" for t in tools
                    ),
                },
            )
        except Exception as _esc_err:
            unresolved_count = 0
            set_trace_io(esc_run, outputs={"error": str(_esc_err)})
            log_with_trace_id(
                state, f"⚠️ [escalation_context] injection failed: {_esc_err}", "warning"
            )
    return escalation_context_block, tools, unresolved_count


def create_generic_skill_node(
    agent_name: str,
    topic: str = None,
    entity_type: str = None,
    legacy_fields: Optional[List[str]] = None,
    max_iterations: int = 20,
    verbose: bool = True,
    grounding_tool: Optional[str] = None,
    auto_ground_tool: Optional[str] = None,
) -> Callable[[SupportState], Awaitable[Dict[str, Any]]]:
    """
    Factory function to create a generic skill node for any agent.
    
    This creates a node that:
    1. Fetches prompt from DB/cache using agent_name
    2. Loads tools from tool_registry based on agent_name
    3. Builds context from conversation_context (not hardcoded)
    4. Uses ContextExtractor for automatic entity extraction
    5. Returns response with updated conversation_context
    
    Args:
        agent_name: Name of the agent (maps to DB prompt and tool registry)
                   e.g., "product_details", "order_status"
        topic: Conversation topic for context extraction
               If None, fetched from tool_registry
        entity_type: Type of entity to focus on ("product", "order", etc.)
                    If None, fetched from tool_registry
        legacy_fields: Legacy state fields to preserve for backward compatibility
        max_iterations: Maximum agent iterations (default: 20)
        verbose: Whether to enable verbose agent output (default: True)
        grounding_tool: If set, the named tool is forced on the first LLM call
                        so the agent cannot answer without first reading data
                        from it. Used for policy nodes that must call
                        ``get_policy_information`` before quoting policy details,
                        preventing hallucinated values (e.g. wrong return window).
        auto_ground_tool: If set, the named tool is auto-invoked before the first
                        model turn and its result injected as a synthetic tool
                        call (``SearchGroundingMiddleware``), so the agent starts
                        already grounded. Used by the recommendation agent to
                        pre-fetch ``search_products`` results each turn.

    Returns:
        A skill node function compatible with LangGraph
    
    Example:
        >>> product_node = create_generic_skill_node("product_details")
        >>> result = product_node(state)
    """
    
    # Get defaults from tool registry if not provided
    agent_config = get_agent_config(agent_name)
    _topic = topic or agent_config.get("topic", "general")
    _entity_type = entity_type or agent_config.get("entity_type", "product")
    _legacy_fields = legacy_fields or []
    
    # Create context extractor for this node
    extractor = create_extractor(
        skill_name=agent_name,
        topic=_topic,
        entity_type=_entity_type,
        legacy_fields=_legacy_fields
    )
    
    async def generic_skill_node(state: SupportState) -> Dict[str, Any]:
        """
        Generic skill node that handles any agent type dynamically.
        
        This node implements the new context flow:
        1. Smart topic matching (find/create topic of this type)
        2. Build context with topic info for LLM
        3. Execute agent with tools
        4. Extract entities from tool results → global + topic refs
        5. Parse LLM summary update
        6. Update topic and return response
        """
        try:
            # ============= STATE SNAPSHOT (input) =============
            with traced_operation(
                f"generic_skill.{agent_name}.state_snapshot_input",
                metadata={"agent_name": agent_name, "client_id": state.get("client_id")},
            ) as _skill_state_in:
                set_trace_io(_skill_state_in, inputs={"state": snapshot_state_for_trace(state)})

            # ============= SETUP =============
            messages_list = state.get("messages", [])
            current_message = messages_list[-1].content if messages_list else ""
            
            log_with_trace_id(state, f"🚀 GENERIC_SKILL_NODE [{agent_name}]: {current_message[:100]}...")

            # ============= CONDITIONAL PHONE EXTRACTION (for web chat) =============
            # Web chat users start with phone_number="" or "web_...". Extract the
            # real phone from the message so downstream tools can validate access.
            if agent_name in PHONE_REQUIRING_AGENTS:
                extracted_phone = _extract_phone_from_message(
                    current_message, state.get("phone_number", "")
                )
                if extracted_phone:
                    state["phone_number"] = extracted_phone
                    log_with_trace_id(state, f"📞 PRE-EXTRACTED phone from message: {extracted_phone}")
            # ============= END CONDITIONAL PHONE EXTRACTION =============

            # Get client_id and ensure it's in state for tools
            client_id = state.get("client_id")

            _escalation_prefetch = _start_escalation_prefetch(
                state, client_id, current_message
            )

            # ============= 1. SMART TOPIC MATCHING =============
            context = state.get("conversation_context", {}) or {}
            # Ensure topics list exists
            if "topics" not in context or context["topics"] is None:
                context["topics"] = []
            if "entities" not in context or context["entities"] is None:
                context["entities"] = []
            
            # Log INPUT context at start
            _log_input_context(state, context, agent_name)
            
            current_topic, is_new_topic = find_or_create_topic(context, _topic)
            if is_new_topic:
                context["topics"].append(current_topic)
            context["active_topic_id"] = current_topic["topic_id"]
            context["topic"] = _topic  # Legacy field

            # ============= AUTO-FETCH CUSTOMER DATA FOR PLACE_ORDER =============
            # If this is place_order agent and phone exists, try to fetch customer from Shopify
            if agent_name == "place_order":
                phone_number = state.get("phone_number", "")
                customer_address = state.get("customer_address", "")
                shopify_fetch_attempted = state.get("shopify_fetch_attempted", False)

                from fashion_bot.utils.phone_number_utils import is_real_phone_number
                if phone_number and is_real_phone_number(phone_number) and not customer_address and not shopify_fetch_attempted:
                    place_order_updates = await _fetch_place_order_customer_updates(phone_number, state)
                    state.update(place_order_updates)

            # ============= GET PROMPT FROM DB/CACHE =============
            prompt_name = agent_config.get("prompt_name") or f"{agent_name}_handler"
            with traced_operation(
                "generic_skill.fetch_prompt",
                metadata={"agent_name": agent_name, "prompt_name": prompt_name},
            ):
                base_prompt = await aget_agent_prompt_with_caching(client_id, prompt_name)
            
            if not base_prompt:
                log_with_trace_id(state, f"⚠️ No prompt found for '{prompt_name}', using default")
                base_prompt = get_default_prompt_for_agent(agent_name)
            
            # ============= FILL AGENT-SPECIFIC PLACEHOLDERS =============
            # Some agents (return_exchange, cancellation) have dynamic placeholders that must be filled
            base_prompt = _fill_agent_specific_placeholders(agent_name, base_prompt, state, current_message)

            # Resolve {{support_team_contact_details}} with real vendor config values
            from fashion_bot.utils.utils import resolve_support_contact_placeholder
            base_prompt = await resolve_support_contact_placeholder(base_prompt, client_id)

            # ============= 2. BUILD CONTEXT WITH TOPIC INFO =============
            # Build topic-centric context for prompt
            topic_context = build_context_for_skill_node(state, current_topic)
            
            # Log topic context summary (full context is in LangSmith trace)
            _ctx_lines = topic_context.count('\n') + 1
            _ctx_topic = current_topic.get('topic_type', '?')
            _ctx_status = current_topic.get('status', '?')
            _ctx_refs = len(current_topic.get('entity_refs', []))
            log_with_trace_id(state, f"📋 [{agent_name}] topic={_ctx_topic} status={_ctx_status} refs={_ctx_refs} context_lines={_ctx_lines}")
            
            # Build summary instruction with previous summary
            previous_summary = current_topic.get("summary", "None - this is a new topic")
            summary_instruction = SUMMARY_INSTRUCTION_TEMPLATE % previous_summary

            # Include suggestions in <B_C_J> only when the agent prompt from
            # Postgres explicitly contains the suggestions guidance section.
            if "NEXT-STEP SUGGESTIONS" in base_prompt:
                summary_instruction = summary_instruction.replace(
                    "\nAWAITING field",
                    _SUGGESTIONS_BCJ_ADDON + "\n\nAWAITING field",
                )
            
            # Legacy context for prompt_context (backward compatibility)
            prompt_context = build_state_context_for_prompt(state, _entity_type)
            
            # No brace-escaping: create_agent takes a plain system_prompt string
            # (there is no ChatPromptTemplate substitution anymore). Escaping here
            # would corrupt the literal JSON examples in the prompt — e.g. the
            # summary_update block would reach the LLM as {{{...}}} and fail to parse.
            # The real variable substitution (str.format / %) already ran above in
            # _fill_agent_specific_placeholders and SUMMARY_INSTRUCTION_TEMPLATE, so
            # the text here is final and must keep its single braces.
            escaped_prompt = base_prompt
            escaped_topic_context = topic_context
            escaped_summary_instruction = summary_instruction

            # Build separate prompt components for better LLM understanding
            # 1. Agent Instructions from DB (contains all role + workflow instructions)
            # Prepend critical tool-calling instruction for Gemini models
            agent_instructions = TOOL_CALLING_INSTRUCTION + escaped_prompt

            # 1.5 Reply guidelines (language handling) - shared across all skills
            reply_guidelines_instruction = REPLY_GUIDELINES_INSTRUCTION

            # 1.55 Support-contact safety (global, all skills). The model must never
            # fabricate support details — every agent now carries the
            # get_contact_information tool, so the only correct source of a phone/
            # email is that tool's output. This block is the prompt-side guardrail
            # that pairs with the tool wiring (see core/tool_registry).
            contact_safety_instruction = """==== SUPPORT CONTACT DETAILS (MANDATORY) ====
🚫 NEVER invent, guess, or use placeholder support contact details — no phone number, email, website/domain, or brand name from memory or training.
- BEFORE you tell the customer to contact / reach out to support (escalations, bulk / wholesale / B2B, or anything you cannot resolve), you MUST call the get_contact_information tool and use ONLY the phone/email it returns. Copy them VERBATIM.
- If get_contact_information returns nothing usable, simply say a team member will reach out to them — do NOT output any phone number, email, or domain.
- Never reference a brand name, website, or contact that does not belong to THIS store."""

            # 1.6 Product card display (product-discovery skills ONLY). The carousel
            # is fully LLM-driven: a product image card is shown to the customer ONLY
            # when you emit the block below. No block = no card. There is no
            # title-matching fallback, so an order confirmation that names a product
            # will NOT show a card unless you ask for it.
            # Scoped to product_details + recommendations + place_order — the skills
            # whose replies surface products visually. Cart/policy/order-status/etc.
            # skills never emit cards, so the block is omitted there (saves tokens,
            # removes any chance of a stray carousel on a non-discovery turn).
            carousel_selection_instruction = ""
            if agent_name in CAROUSEL_AGENTS:
                carousel_selection_instruction = CAROUSEL_SELECTION_INSTRUCTION

            # 1.7 Product link formatting (channel-aware) — ensures the customer
            # always has a clickable path to a product, including on follow-up turns
            # where the carousel card is intentionally suppressed (already shown).
            _phone = str(state.get("phone_number") or "")
            is_web_chat_session = _phone.startswith(("web_", "fbw_")) if state else False
            if is_web_chat_session:
                product_link_instruction = PRODUCT_LINK_INSTRUCTION_WEB
            else:
                product_link_instruction = PRODUCT_LINK_INSTRUCTION_DEFAULT

            # 1.75 Channel context (product-surfacing skills). States the channel and
            # its rendering capabilities (carousel available? markdown?) explicitly so
            # the agent prompt can branch deterministically on them — e.g. show only
            # the carousel on web vs a numbered text list where no carousel exists.
            # Scoped to CAROUSEL_AGENTS: the skills whose prompts choose how products
            # are presented. Purely additive — no effect unless a prompt reads it.
            channel_context_instruction = ""
            if agent_name in CAROUSEL_AGENTS:
                channel_context_instruction = _build_channel_context_block(state, is_web_chat_session)

            # 1.8 Cart action confirmation guardrail — applied to ALL agents
            # that have the add_to_cart tool (see system_blocks append below,
            # gated on tools rather than a hardcoded agent name).

            # 2. Conversation Context (what the LLM knows - entities, topics, customer info)
            context_message = f"""==== CONVERSATION CONTEXT ====
{escaped_topic_context}

IMPORTANT: Use the entity information above before calling tools to fetch data you already have.
If an order shows "✅ Can be cancelled" or "❌ Cannot be cancelled", use that info directly."""

            # ============= STOREFRONT CHECKOUT URL (place_order, web chat) =============
            # The place_order prompt tells the LLM to share the /cart URL directly
            # when items are already in cart (Prepaid flow). Without injecting the
            # actual domain the LLM hallucinates a placeholder like "store.com".
            if agent_name == "place_order":
                try:
                    from fashion_bot.utils.escalation_helper import resolve_channel_from_state
                    _channel = resolve_channel_from_state(state)
                    _is_web = _channel == "web-chat" or is_web_chat_session
                    if _is_web:
                        from fashion_bot.utils.product_utils import aget_shopify_to_website_mapping
                        from urllib.parse import urlparse
                        _storefront_url = await aget_shopify_to_website_mapping(client_id)
                        if not _storefront_url:
                            _page_url = state.get("current_page_url") or (state.get("page_context") or {}).get("url") or ""
                            _parsed = urlparse(_page_url)
                            if _parsed.scheme and _parsed.netloc:
                                _storefront_url = f"{_parsed.scheme}://{_parsed.netloc}"
                        if _storefront_url:
                            _cart_checkout_url = f"{_storefront_url.rstrip('/')}/cart"
                            context_message += (
                                f"\n\n==== STOREFRONT CHECKOUT URL ===="
                                f"\nThis store's checkout URL is: {_cart_checkout_url}"
                                f"\nUse this EXACT URL when sharing the prepaid checkout link with the customer."
                                f"\nNEVER guess or fabricate a store URL."
                            )
                except Exception as _url_err:
                    log_with_trace_id(state, f"⚠️ [place_order] storefront URL injection failed: {_url_err}", "warning")
            
            # ============= PRODUCT LINE CONTEXT CARRY-FORWARD =============
            # When the product_details agent is invoked, remind LLM to infer product_line
            # from conversation history (orders, previous product browsing)
            if agent_name == "product_details":
                context_message += PRODUCT_LINE_INFERENCE_NOTE
            
            # Template messages are injected directly into conversation history as AIMessages
            # with the prefix "This is a template message..." by intent_detection_node.
            # The LLM can reason about them naturally from the chat history.
            
            # 3. Summary instruction stays at the end (output format)
            
            # ============= GET TOOLS FROM REGISTRY =============
            try:
                with traced_operation(
                    "generic_skill.load_tools",
                    metadata={"agent_name": agent_name},
                ):
                    tools = await aget_tools_for_agent(
                        agent_name=agent_name,
                        state=state,
                        messages_list=messages_list,
                        client_id=client_id,
                    )
            except ValueError as e:
                log_with_trace_id(state, f"❌ Failed to load tools: {e}", "error")
                return {
                    "type": "customer_message",
                    "customer_message": "I apologize, but I'm having trouble processing your request. Please try again.",
                    "trace_id": get_trace_id(state),
                    "conversation_context": state.get("conversation_context", {})
                }
            
            # Tools loaded silently - only log errors
            
            # ============= CREATE AND EXECUTE AGENT =============
            # Resolve this agent's model (client_agent_llm_config → env →
            # default). Keep the raw primary here (it must stay a real chat model
            # so bind_tools / AgentExecutor keep working) and resolve the default
            # chat model (LLM_PROVIDER / LLM_MODEL) as a native fallback. The
            # native tool loop attaches it with Runnable.with_fallbacks([...]) at
            # each bind site, so a per-client model that is down / 429s / errors
            # transparently retries the SAME request on the default — without
            # changing anything else. fallback_llm is None when the agent already
            # IS the default, or when LLM_FAILOVER_ENABLED=false.
            llm = await LLMFactory.aget_llm(tool_name=agent_name, state=state)
            fallback_llm = LLMFactory.get_default_llm()
            if fallback_llm is llm:
                fallback_llm = None  # already the default → nothing to fall back to
            
            recent_messages = messages_list[-16:-1] if len(messages_list) > 1 else []
            
            # Template messages are already in messages_list as AIMessages (injected by intent_detection_node)

            # ── Nearest offline store context injection ──
            _injected_store = None  # set when browser-geo resolves a nearby store
            if agent_name in ("product_details", "recommendations", "return_exchange", "vendor_inquiry", "place_order"):
                try:
                    from fashion_bot.utils.store_locations import afind_nearest_store, aget_all_stores
                    user_loc = state.get("user_location") or {}
                    if user_loc.get("latitude") and user_loc.get("longitude"):
                        _stores = await afind_nearest_store(
                            client_id,
                            latitude=user_loc["latitude"],
                            longitude=user_loc["longitude"],
                        )
                        if _stores:
                            _s = _stores[0]
                            _injected_store = _s
                            log_with_trace_id(state, f"📍 [store_context] injecting nearest store: {_s['name']} ({_s.get('distance_km')} km)")
                            context_message += (
                                "\n\n==== NEAREST OFFLINE STORE ===="
                                f"\nStore: {_s['name']}"
                                f"\nAddress: {_s['address']}"
                                f"\nPhone: {_s.get('phone', 'N/A')}"
                                f"\nManager: {_s.get('manager_name', 'N/A')}"
                                f"\nManager Email: {_s.get('manager_email', 'N/A')}"
                                f"\nHours: {_s.get('hours', 'N/A')}"
                                f"\nDistance: ~{_s.get('distance_km', '?')} km from customer"
                                f"\nGoogle Maps: {_s['google_maps_url']}"
                                "\n\nUSE THIS when: product/variant is out of stock, user is asking for a store to visit or confused with size before buying a product (not a return/exchange), "
                                "customer asks about store/office/warehouse/authenticity. "
                                "Always include the Google Maps link."
                            )
                        else:
                            log_with_trace_id(state, "📍 [store_context] user has geo but no store within range")
                    else:
                        _all = await aget_all_stores(client_id)
                        if _all:
                            log_with_trace_id(state, f"📍 [store_context] no user geo, injecting stores-available hint ({len(_all)} stores)")
                            context_message += (
                                "\n\n==== OFFLINE STORES AVAILABLE (MANDATORY ACTION) ===="
                                "\nThis brand has physical stores. You MUST mention the option to visit "
                                "a physical store in the following situations:"
                                "\n- When a product/variant is out of stock"
                                "\n- When the customer is confused with size before buying a product (not a return/exchange)"
                                "\n- When the customer asks about store/office/warehouse/authenticity"
                                "\nTo find the nearest store, ask the customer for their city or pincode, "
                                "then call the get_nearest_store tool. Do NOT skip this step."
                            )
                        else:
                            log_with_trace_id(state, "📍 [store_context] no stores configured for this client")
                except Exception as e:
                    log_with_trace_id(state, f"⚠️ [store_context] injection failed: {e}", "warning")

            # ── Open-escalation context (internal, agent-only) ──
            # See design_docs/ESCALATION_FOLLOW_UP_CONTEXT.md.
            escalation_context_block, tools, unresolved_count = await _resolve_escalation_context(
                state, _escalation_prefetch, tools, agent_name
            )

            system_blocks = [
                agent_instructions,
                reply_guidelines_instruction,
                contact_safety_instruction,
            ]
            # Authoritative current date/time (IST). Grounds every relative
            # timeline ("delivery in 15-20 days") to the real today so the LLM
            # can't invent — or emit a past — calendar date when a customer asks
            # for an exact arrival date. Applied to ALL agents: any of them may
            # be asked "when will it arrive / when did it ship".
            system_blocks.append(_build_current_datetime_block())
            # Channel facts go before the product-rendering guidance so the LLM reads
            # them first and any channel-conditional prompt rule resolves against them.
            if channel_context_instruction:
                system_blocks.append(channel_context_instruction)
            if carousel_selection_instruction:  # product_details + recommendations + place_order
                system_blocks.append(carousel_selection_instruction)
            system_blocks.append(product_link_instruction)
            # Cart action guardrail — applied to ANY agent that can write to the
            # cart this turn (product_details, place_order, cart_management,
            # recommendations on web chat), gated on the loaded tools rather than a
            # hardcoded agent list so it can't drift. The write tools dispatch
            # asynchronously and CAN fail (unrecognized variant, unconfirmed
            # size/colour); this keeps the LLM from claiming a change that didn't
            # happen or defaulting a variant the customer never chose.
            if any(getattr(t, "name", "") == "add_to_cart" for t in (tools or [])):
                system_blocks.append(CART_ACTION_GUARDRAIL_INSTRUCTION)
            # Order-access verification instructions. Injected from code (never
            # from the per-client agents_config prompt) so the instruction can't
            # drift from the config flag that enforces it — a client with the
            # flag on but a stale prompt would otherwise loop on a tool that
            # refuses. Gated on the loaded tools, like the cart guardrail above.
            try:
                from fashion_bot.utils.order_access import (
                    agent_has_gated_order_tools, aget_verification_policy,
                    build_verification_instruction_block,
                )
                if agent_has_gated_order_tools(tools):
                    _verification_block = build_verification_instruction_block(
                        await aget_verification_policy(state)
                    )
                    if _verification_block:
                        system_blocks.append(_verification_block)
            except Exception as _verify_block_err:
                log_with_trace_id(
                    state,
                    f"⚠️ Could not build order verification block: {_verify_block_err}",
                    "warning",
                )
            system_blocks += [
                escaped_summary_instruction,
                context_message,
            ]
            # Appended last so it sits closest to the user turn. The per-turn
            # context_message already breaks the prompt-cache prefix above it, so
            # this costs no additional cache miss.
            if escalation_context_block:
                system_blocks.append(escalation_context_block)

            agent_result: Dict[str, Any]
            response_content = ""

            # ============= STAGE A: SINGLE NATIVE create_agent PATH (streams) =====
            # Acquire the LangGraph custom stream writer so prose tokens stream
            # live to the channel adapter. None on non-streaming callers (WhatsApp
            # via graph.ainvoke) — run_agent_graph then runs without streaming and
            # parse_agent_output below behaves identically for both.
            writer = None
            if state.get("_streaming_enabled"):
                try:
                    writer = get_stream_writer()
                except Exception:
                    writer = None

            import time as _t
            _t0 = _t.monotonic()
            _system_prompt = "\n\n".join(system_blocks)
            with traced_operation(
                "generic_skill.create_agent",
                run_type="chain",
                metadata={"agent_name": agent_name, "streaming": writer is not None},
            ):
                agent_result = await run_agent_graph(
                    llm=llm,
                    tools=tools,
                    system_prompt=_system_prompt,
                    chat_history=recent_messages,
                    user_input=current_message,
                    max_iterations=max_iterations,
                    state=state,
                    writer=writer,                 # streams prose when present
                    grounding_tool=grounding_tool,  # forces first tool for policy skills
                    auto_ground_tool=auto_ground_tool,  # pre-fetches search for recommendations
                    fallback_llm=fallback_llm,     # transparent failover on provider errors
                )
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            logger.info(f"🤖 [LLM] {agent_name} create_agent elapsed_ms={_elapsed} streaming={writer is not None}")
            response_content = _normalize_llm_content(agent_result.get("output", "")).strip()

            # ============= HANDLE MAX ITERATIONS =============
            # When agent hits max iterations, try to extract useful content from intermediate steps
            if response_content == "Agent stopped due to max iterations." or response_content == "Agent stopped due to iteration limit or time limit.":
                log_with_trace_id(state, f"⚠️ Agent hit max iterations, attempting to recover response from intermediate steps")
                recovered_response = _recover_response_from_intermediate_steps(agent_result.get("intermediate_steps", []), agent_name)
                if recovered_response:
                    response_content = recovered_response
                    log_with_trace_id(state, f"✅ Recovered response from intermediate steps")
                else:
                    # Escalate to human agent when we can't recover
                    log_with_trace_id(state, f"❌ Could not recover response, escalating to human")
                    return {
                        "type": "customer_message",
                        "customer_message": "I apologize, but I'm having trouble processing your request right now. Let me connect you with our support team who can assist you better.",
                        "needs_human_agent": True,
                        "needs_escalation": True,
                        "trace_id": get_trace_id(state),
                        "conversation_context": state.get("conversation_context", {})
                    }
            
            steps_count = len(agent_result.get('intermediate_steps', []))
            log_with_trace_id(state, f"📝 response: tools={steps_count} len={len(response_content)}", "debug")
            
            # ============= STAGE B: PARSE DELIMITED BLOCKS (after stream ends) =====
            # Pure parse of the COMPLETE text. Single source of truth for the
            # reply; identical for streaming (web) and non-streaming (whatsapp).
            # clean_prose == the prose StreamGuard streamed live.
            parsed = parse_agent_output(response_content)
            response_content = parsed.clean_prose
            summary_update = parsed.summary_update
            show_product_handles = parsed.show_product_handles
            if show_product_handles:
                log_with_trace_id(state, f"🛒 SHOW_PRODUCTS handles from LLM: {show_product_handles}")
            # UI/state signals from the <B_C_J> block (suggestions / track_order / phone)
            signals = _signals_from_summary(summary_update)

            # ============= HANDLE ESCALATION =============
            if "ESCALATION_REQUIRED:" in response_content:
                return await _handle_escalation(state, response_content, agent_name)
            
            if not response_content:
                log_with_trace_id(state, f"⚠️ [{agent_name}] LLM returned empty response after {steps_count} tool calls", "warning")
                response_content = "I'd be happy to help! Could you please provide more details about your request?"
            
            # ============= 4. EXTRACT ENTITIES FROM TOOL RESULTS =============
            # Extract entities from intermediate steps
            intermediate_steps = agent_result.get("intermediate_steps", [])

            # Defense-in-depth: never let a literal [tracking_url] placeholder
            # reach the customer. The order_status_handler prompt template carries
            # "track the order here: [tracking_url]" and relies on the LLM to
            # substitute the real link; when the model lacks the URL it emits the
            # raw placeholder. Resolve it from tool output, or drop it gracefully.
            response_content = _resolve_tracking_url_placeholder(
                response_content, intermediate_steps, state
            )

            # order_status only: every URL this agent sends must be one we gave
            # it — a tool result this turn, or a link written into its own
            # prompt. The 2026-08-24 Enamor reply quoted a tracking link that was
            # neither. Scoped to this one agent because it is the only one whose
            # prompt hands the model a link-shaped slot to fill.
            if agent_name == "order_status":
                response_content = strip_unsourced_urls(
                    response_content,
                    collect_tool_result_urls(intermediate_steps) + _URL_RE.findall(_system_prompt),
                    state,
                )

            tool_trace_struct = _build_tool_trace_struct(intermediate_steps)
            if tool_trace_struct:
                context["last_tool_calls"] = [t.get("tool") for t in tool_trace_struct]
                tool_names = [t.get("tool") for t in tool_trace_struct[:4]]
                log_with_trace_id(state, f"🧰 [{agent_name}] tools={tool_names}")
            tool_entities = _extract_entities_from_intermediate_steps(intermediate_steps, _entity_type)
            
            # Add full entity details to global storage
            add_entities_to_global(context, tool_entities)
            
            # Add lightweight refs to current topic
            add_entity_refs_to_topic(current_topic, tool_entities)
            
            # ============= SELECTABLE ENTITIES FROM PRODUCT CARDS =============
            recent_products = _extract_recent_products(intermediate_steps)
            if recent_products:
                add_selectable_entities(
                    context, recent_products,
                    entity_type="product", source="recommendation"
                )

            # ============= STAGE C: surface the carousel + signals (after blocks parsed) =====
            # Manual yield of processed context — emitted after the prose stream so
            # the carousel and signal tiles arrive once the customer text has finished.
            if writer is not None:
                writer({"type": "products", "handles": show_product_handles or [],
                        "recent_products": recent_products})
                _emit_ui_signals(writer, signals)
            
            # ============= EXTRACT AND TRACK ACTIONS =============
            # Extract state-changing actions from tool calls
            actions = _extract_actions_from_intermediate_steps(
                intermediate_steps, 
                current_topic.get("topic_id")
            )
            for action in actions:
                add_action_to_context(context, action)
            
            if actions:
                log_with_trace_id(state, f"🎯 Tracked {len(actions)} actions: {[a['action_name'] for a in actions]}")
            
            # ============= STORE TOOL CALLS FOR TRACKING =============
            # Extract tool names from intermediate_steps and store in conversation_context
            # This is needed for cancellation_aversion_tracker and other analytics
            tool_calls = _extract_tool_call_names(intermediate_steps)
            if tool_calls:
                context["last_tool_calls"] = tool_calls
                log_with_trace_id(state, f"🔧 tool_calls={tool_calls}", "debug")

            # ============= MONITOR: PREPAID CART-LINK CHAINING =============
            # The web prepaid flow (create_draft_order_for_prepaid) returns
            # requires_add_to_cart=True and relies on the LLM chaining add_to_cart
            # in the SAME turn so the customer lands on a populated /cart page. If
            # that chaining is skipped, the customer gets an EMPTY cart — a silent,
            # user-visible failure no other check catches. Emit a warning so it is
            # alertable. (Read-only; does not change behaviour.)
            try:
                prepaid_cart_requested = False
                for step in intermediate_steps:
                    if len(step) < 2:
                        continue
                    action, observation = step[0], step[1]
                    tname = getattr(action, "tool", None) or (
                        action.get("tool") if isinstance(action, dict) else None
                    )
                    if tname == "create_draft_order_for_prepaid" and isinstance(observation, dict):
                        if observation.get("requires_add_to_cart") is True:
                            prepaid_cart_requested = True
                            break
                if prepaid_cart_requested and "add_to_cart" not in tool_calls:
                    log_with_trace_id(
                        state,
                        "⚠️ Prepaid cart-link returned requires_add_to_cart=True but "
                        "add_to_cart was NOT called this turn — customer may land on an "
                        "EMPTY cart. (LLM chaining gap)",
                        "warning",
                    )
            except Exception as _monitor_err:
                log_with_trace_id(
                    state,
                    f"prepaid chaining monitor skipped: {_monitor_err}",
                    "debug",
                )
            
            # ============= 6. UPDATE TOPIC WITH LLM SUMMARY =============
            timestamp = datetime.now().isoformat()
            
            if summary_update:
                # Update topic summary (cumulative)
                current_topic["summary"] = summary_update.get("summary", current_topic.get("summary", ""))
                current_topic["status"] = summary_update.get("status", "open")
                current_topic["updated_at"] = timestamp
                
                # Update awaiting field - what info is the agent waiting for from user
                # Valid values: None, "order_id", "confirmation", "reason", "size", "gender", "category", "address", "phone_number"
                awaiting_value = summary_update.get("awaiting")
                if awaiting_value is not None:  # Explicitly set (can be null to clear)
                    current_topic["awaiting"] = awaiting_value if awaiting_value else None
                    log_with_trace_id(state, f"📋 Topic awaiting: {current_topic.get('awaiting')}")
                
                # Merge LLM-mentioned entities to topic refs
                llm_entities = summary_update.get("entities_used", [])
                merge_llm_entity_refs_to_topic(current_topic, llm_entities, context.get("entities", []))
            else:
                # Fallback: generate basic summary if LLM didn't provide one
                current_topic["updated_at"] = timestamp
                if not current_topic.get("summary"):
                    entity_names = [ref.get("entity_name", "") for ref in current_topic.get("entity_refs", [])[:2]]
                    entities_str = f" for {', '.join(filter(None, entity_names))}" if entity_names else ""
                    current_topic["summary"] = f"Handled {agent_name.replace('_', ' ')}{entities_str}"
            
            # ============= 7. SET FOCAL ENTITY =============
            # Priority 1: LLM entities_used (explicit signal — always overrides)
            # Priority 2: First product mentioned in LLM response text (implicit signal)
            # Priority 3: entity_refs[-1] (weak fallback — only sets if no focal exists)
            llm_entities_for_focal = summary_update.get("entities_used", []) if summary_update else []
            focal_entity_id = determine_focal_entity_from_topic(
                current_topic, 
                global_entities=context.get("entities", []),
                llm_entities_used=llm_entities_for_focal
            )

            response_inferred_focal = None
            if not llm_entities_for_focal and response_content:
                response_inferred_focal, earliest_pos = _infer_focal_from_response_text(
                    response_content, context.get("entities", [])
                )
                if response_inferred_focal and response_inferred_focal != focal_entity_id:
                    log_with_trace_id(state, f"🎯 Response-text focal override: {response_inferred_focal} (first mentioned in response at pos {earliest_pos})")
                    focal_entity_id = response_inferred_focal

            has_llm_hint = bool(llm_entities_for_focal)
            has_response_hint = response_inferred_focal is not None
            if focal_entity_id:
                existing_focal = context.get("focal_entity")
                if has_llm_hint or has_response_hint or not existing_focal or not existing_focal.get("entity_id"):
                    current_topic["focal_entity_id"] = focal_entity_id
                    context["focal_entity"] = get_focal_entity_from_global(context, focal_entity_id)
            
            # Update legacy fields
            context["last_skill_node"] = agent_name
            context["context_updated_at"] = timestamp
            
            # ============= WEB CHAT ESCALATION MESSAGE OVERRIDE =============
            # For web chat escalation: Replace "transfer to team member" language with simplified message
            # Web chat users have phone_number starting with "web_" or "fbw_"
            # A session_id alone does NOT mean web-only — the user may have provided
            # a real phone during the conversation (phone migration).
            phone_number = str(state.get("phone_number", ""))
            session_id = state.get("session_id")
            from fashion_bot.utils.phone_number_utils import is_real_phone_number as _is_real_phone
            is_web_chat = bool(phone_number) and not _is_real_phone(phone_number)
            
            # Debug logging for escalation detection
            web_contact_details = ""
            if agent_name == "escalation":
                log_with_trace_id(state, f"🔍 [ESCALATION DEBUG] phone_number='{phone_number}', session_id='{session_id}', is_web_chat={is_web_chat}")
            
            # Skip override if the tool asked for the phone number — force-set
            # the response to a simple phone-collection question instead of relying
            # on the LLM to relay the directive (it often writes its own response).
            # But if a later step in the same turn successfully completed the
            # escalation, the phone was already collected — don't override.
            phone_collection_active = any(
                isinstance(step, tuple) and len(step) > 1
                and isinstance(step[1], dict)
                and step[1].get("phone_number_required")
                for step in intermediate_steps
            ) if intermediate_steps else False

            escalation_succeeded_in_turn = any(
                isinstance(step, tuple) and len(step) > 1
                and isinstance(step[1], dict)
                and step[1].get("success") is True
                and not step[1].get("phone_number_required")
                # A gate soft-block returns success=True with escalated=False:
                # the call worked, it just did not hand off.
                and step[1].get("escalated") is not False
                and (getattr(step[0], 'tool', None) or (step[0].get('tool') if isinstance(step[0], dict) else None)) == "escalate_to_agent"
                for step in intermediate_steps
            ) if intermediate_steps else False

            if phone_collection_active and escalation_succeeded_in_turn:
                phone_collection_active = False

            if is_web_chat and phone_collection_active:
                response_content = (
                    "I'd like to connect you with our support team. "
                    "Could you please share your phone number or email address so they can reach out to you?"
                )
            # Only rewrite the reply with the "a team member will contact you"
            # boilerplate when the turn actually escalated. With the
            # resolution-first prompt the escalation node also asks clarifying
            # questions and resolves issues outright — those replies must survive.
            elif agent_name == "escalation" and is_web_chat and escalation_succeeded_in_turn:
                response_content, web_contact_details = await _web_chat_escalation_override(
                    response_content, state
                )

            # Append when a new escalation was created this turn OR the
            # escalation handler is referencing existing unresolved ones
            # (LLM may confirm "I have escalated" without re-calling the
            # tool because a duplicate is unnecessary).
            _append_esc_footer = escalation_succeeded_in_turn or (
                agent_name == "escalation"
                and unresolved_count > 0
                and not phone_collection_active
            )
            if _append_esc_footer:
                try:
                    from fashion_bot.config_manager import aget_config
                    from fashion_bot.utils.escalation_helper import (
                        append_escalation_footer,
                    )
                    escalation_footer = await aget_config(
                        "escalation_success_footer",
                        client_id=state.get("client_id"),
                    )
                    if escalation_footer:
                        # Idempotent: the LLM often closes with this same line
                        # (it has seen it on every prior escalation turn), so a
                        # blind append would send it to the customer twice.
                        response_content = append_escalation_footer(
                            response_content, escalation_footer
                        )
                except Exception:
                    pass
            
            # ============= BUILD RESPONSE =============
            tool_trace_msg = _build_tool_trace_system_message(intermediate_steps)
            updated_messages = list(state.get("messages", []) or [])
            if tool_trace_msg:
                updated_messages.append(tool_trace_msg)
            response = {
                "type": "customer_message",
                "customer_message": response_content,
                "trace_id": get_trace_id(state),
                "conversation_context": context,
                "messages": updated_messages,
                "recent_products": recent_products,
                "show_product_handles": show_product_handles,
                # Per-turn UI signals. ALWAYS set (empty/None when N/A) so they are
                # reset every turn and can never re-send on a later turn. 'suggestions'
                # is also popped by the websocket after the carousel. 'captured_phone'
                # is a SEPARATE field — it never overwrites the real 'phone_number'.
                "suggestions": signals.get("suggestions") or [],
                "captured_phone": signals.get("phone_number") or None,
                "pending_widget_actions": state.get("pending_widget_actions") or None,
            }
            if agent_name == "escalation" and is_web_chat and web_contact_details:
                response["support_team_contact_details"] = web_contact_details
            if tool_trace_struct:
                response["tool_call_trace"] = tool_trace_struct
            
            # Preserve key state fields that may have been set by tools
            # These fields are critical for state persistence across invocations
            response.update(_passthrough_state_updates(state, PASSTHROUGH_STATE_FIELDS_PRE_ESCALATION))
            
            # ============= STORE VISIT NOTIFICATION (browser-geo path) =============
            if _injected_store and response_content:
                _store_name_lower = (_injected_store.get("name") or "").lower()
                if _store_name_lower and _store_name_lower in response_content.lower():
                    try:
                        from fashion_bot.tool_factory import _anotify_agent_store_visit
                        _uloc = (state.get("user_location") or {})
                        _loc_query = f"browser geolocation ({_uloc.get('latitude')}, {_uloc.get('longitude')})"
                        _recent = [m.content for m in state.get("messages", []) if getattr(m, "type", "") == "human"][-3:]
                        _focal = (context.get("focal_entity") or {})
                        _p_name = _focal.get("entity_value")
                        _p_url = (_focal.get("data") or {}).get("url") if _focal.get("data") else None
                        _store_result = await _anotify_agent_store_visit(
                            state, client_id, _injected_store, _loc_query,
                            recent_user_messages=_recent,
                            product_name=_p_name, product_url=_p_url,
                        )
                        if isinstance(_store_result, dict) and _store_result.get("phone_number_required"):
                            _phone_ask = (
                                "\n\nCould you please share your phone number or email address "
                                "so our store team can reach out to you?"
                            )
                            response_content += _phone_ask
                            response["customer_message"] = response_content
                    except Exception as _notify_err:
                        log_with_trace_id(state, f"⚠️ [store_visit_notify] geo-path notification failed: {_notify_err}", "warning")


            # ============= ESCALATION FROM TOOL RESULTS =============
            if _check_escalation_in_tool_results(intermediate_steps):
                response["needs_escalation"] = True

            # ============= PLACE ORDER STATE FIELDS =============
            # Transfer order-related fields for prepaid checkout flow
            response.update(_passthrough_state_updates(state, PASSTHROUGH_STATE_FIELDS_POST_ESCALATION))
            
            # Apply legacy state updates (product_link, inquiry_product_info,
            # product_selection_matches) from extracted tool entities
            response = _apply_legacy_state_updates(response, tool_entities, _entity_type, state)
            
            # Log final context summary (single consolidated log)
            _log_conversation_context(state, context, agent_name, len(tool_entities), len(actions) if actions else 0)
            log_with_trace_id(state, f"💬 [{agent_name}] reply={response_content[:150]}")

            # ============= STATE SNAPSHOT (output) =============
            with traced_operation(
                f"generic_skill.{agent_name}.state_snapshot_output",
                metadata={"agent_name": agent_name, "client_id": state.get("client_id")},
            ) as _skill_state_out:
                set_trace_io(_skill_state_out, outputs={"state": snapshot_state_for_trace(response)})

            return response
            
        except Exception as e:
            logger.error(f"❌ Error in generic_skill_node [{agent_name}]: {str(e)}")
            logger.error(f"❌ Traceback: {traceback.format_exc()}")
            report_error(
                "Error in generic_skill_node",
                level="error",
                exc_info=(type(e), e, e.__traceback__),
                agent_name=agent_name,
                client_id=state.get("client_id"),
                trace_id=get_trace_id(state),
            )
            
            # Customer-safe fallback: do NOT expose raw "encountered an error"
            # framing to the end user. Route the conversation to human
            # escalation so the customer receives a follow-up instead of a
            # dead-end retry prompt.
            error_result = {
                "type": "customer_message",
                "customer_message": (
                    "I'm having a little trouble looking that up for you right now. "
                    "Our team has been notified and will get back to you shortly to help. 🙏"
                ),
                "needs_escalation": True,
                "trace_id": get_trace_id(state),
                "conversation_context": state.get("conversation_context", {})
            }
            with traced_operation(
                f"generic_skill.{agent_name}.state_snapshot_output",
                metadata={"agent_name": agent_name, "error": True},
            ) as _skill_state_err:
                set_trace_io(_skill_state_err, outputs={"state": snapshot_state_for_trace(error_result)})
            return error_result
    
    # Set function name for debugging
    generic_skill_node.__name__ = f"generic_skill_node_{agent_name}"
    generic_skill_node.__doc__ = f"Generic skill node for {agent_name}"
    
    return generic_skill_node


def _check_escalation_in_tool_results(intermediate_steps: List[Tuple[Any, Any]]) -> bool:
    """Return True if escalate_to_agent was called successfully (excluding internal syncs).

    Also escalates when order-access verification locked out this conversation:
    a customer who cannot prove the order is theirs still needs a human, and the
    agent has no tool left that can help them.
    """
    for step in intermediate_steps:
        if len(step) < 2:
            continue
        action = step[0]
        tool_name = getattr(action, 'tool', None) or (action.get('tool') if isinstance(action, dict) else None)
        obs = step[1]
        if isinstance(obs, dict) and obs.get("error") == "verification_locked":
            return True
        if tool_name == "escalate_to_agent":
            if isinstance(obs, dict) and obs.get("success") and not obs.get("internal_sync"):
                return True
    return False


def _recover_response_from_intermediate_steps(intermediate_steps: list, agent_name: str) -> Optional[str]:
    """
    Try to recover a useful response from intermediate steps when agent hits max iterations.
    
    This examines tool results to construct a meaningful response instead of showing
    the error message to the customer.
    
    Args:
        intermediate_steps: List of (AgentAction, observation) tuples from agent execution
        agent_name: Name of the agent for context-aware recovery
        
    Returns:
        Recovered response string if useful content found, None otherwise
    """
    if not intermediate_steps:
        return None
    
    import json
    
    # Get the last tool observation - most likely contains the useful info
    last_step = intermediate_steps[-1]
    if len(last_step) < 2:
        return None
    
    _, observation = last_step
    
    # Try to parse observation as JSON
    try:
        if isinstance(observation, str):
            obs_data = json.loads(observation)
        elif isinstance(observation, dict):
            obs_data = observation
        else:
            return None
    except (json.JSONDecodeError, TypeError):
        return None
    
    # Context-aware recovery based on agent type
    if agent_name in ["product_details"]:
        # Product-related agents - extract product info
        if obs_data.get("found"):
            product = obs_data.get("product", {})
            if product:
                name = product.get("name") or product.get("title", "")
                url = product.get("url") or product.get("product_link", "")
                if name:
                    return f"I found {name}. You can view it here: {url}" if url else f"I found {name}."
            
            # Multiple products
            products = obs_data.get("products", [])
            if products:
                product_list = "\n".join([f"{i+1}. {p.get('title', 'Product')}" for i, p in enumerate(products[:5])])
                return f"I found these products that might match your search:\n{product_list}\n\nWhich one would you like to know more about?"
    
    elif agent_name in ["order_status", "delivery_timeline"]:
        # Order-related agents - extract order info
        order = obs_data.get("order", {})
        if order:
            status = order.get("status", "")
            order_id = order.get("order_id") or order.get("channel_order_id", "")
            if status and order_id:
                return f"Your order {order_id} is currently {status}."
        
        # Multiple orders
        orders = obs_data.get("orders", [])
        if orders:
            order_list = "\n".join([f"• Order {o.get('order_id', 'N/A')}: {o.get('status', 'Unknown')}" for o in orders[:5]])
            return f"Here are your recent orders:\n{order_list}"
    
    elif agent_name == "discount":
        # Discount agent - extract discount info
        discounts = obs_data.get("discounts", {})
        if discounts:
            coupons = discounts.get("discount_coupons", {})
            if coupons:
                return "We have some great offers for you! Let me share the available discounts."
    
    elif agent_name == "place_order":
        # Place order - check for success
        if obs_data.get("success"):
            order_id = obs_data.get("order_id", "")
            if order_id:
                return f"Great news! Your order has been placed successfully. Your order ID is {order_id}."
    
    # Generic fallback - check for success/message pattern
    if obs_data.get("success") and obs_data.get("message"):
        return obs_data.get("message")
    
    return None


# Tokens that appear ONLY in our internal tracking note, never in customer
# prose. We key the leak-guard on these fingerprints rather than the fence
# label, because the label is exactly what the LLM mislabels (summary_update →
# json_summary_update). Anchoring on the field names makes the guard robust to
# any future label variant.
_INTERNAL_TRACKING_FINGERPRINT = r'(?:summary_update|context_update|entities_used|"awaiting")'
_LEAK_GUARD_PATTERNS = (
    # The consolidated <B_C_J>...</B_C_J> signal block (defense-in-depth: strips it
    # even if the primary parser / StreamGuard missed it, incl. an unclosed tail).
    re.compile(r'<B_C_J>[\s\S]*?(?:</B_C_J>|$)', re.IGNORECASE),
    # A fenced block (any language tag, or none) whose body carries a tracking
    # fingerprint — catches ```summary_update, ```json_summary_update, or an
    # unlabelled ``` block that contains "entities_used"/"awaiting".
    re.compile(r'```[^\n`]*\n?[\s\S]*?' + _INTERNAL_TRACKING_FINGERPRINT + r'[\s\S]*?```', re.IGNORECASE),
    # A fence-less trailing JSON object that carries a tracking fingerprint.
    re.compile(r'\{[^{}]*' + _INTERNAL_TRACKING_FINGERPRINT + r'[\s\S]*?\}\s*$', re.IGNORECASE),
    # A bare trailing label the LLM emitted without fences or JSON body —
    # including the case where it started an incomplete JSON body the parser
    # could not match (e.g. "summary_update\n{") which previously slipped
    # through because the prior pattern anchored on $ immediately after the
    # label.
    #
    # The leading `*` tolerates an inline-code wrapper the LLM sometimes adds
    # around the label (e.g. "`summary_update\n{`"). A single stray backtick
    # before the label is neither whitespace nor part of the optional json
    # prefix, so without this the whole guard breaks and the unterminated
    # block leaks to the customer.
    re.compile(r'\n\s*`*\s*(?:json[_\s]*)?(?:summary_update|context_update)\b[\s\S]*\Z', re.IGNORECASE),
    # A bare "show_products: [...]" tail the LLM emitted OUTSIDE the <B_C_J>
    # wrapper. Incident (Enamor, web chat, 2026-08-16, phone …0955): two
    # product-carousel replies ended with `\n\nshow_products: ["handle-1",
    # "handle-2", …]` — a JSON *array* literal, not a JSON object, so the
    # brace-anchored guard above did not match; the label was `show_products`,
    # not `summary_update`/`context_update`, so the bare-label guard above
    # did not match either. The internal field name and its bracketed handle
    # list were streamed straight to the customer. `show_products` is an
    # internal tracking-block field and never appears as a top-level line in
    # legitimate customer prose, so anchoring on `\nshow_products:\s*[` at
    # end-of-string is a safe fingerprint (tolerates optional inline-code
    # backticks like the sibling `summary_update` guard).
    re.compile(r'\n\s*`*\s*(?:json[_\s]*)?show_products\s*:\s*\[[\s\S]*\Z', re.IGNORECASE),
)


def parse_agent_output(response_content):
    """STAGE B — separate the customer prose from trailing machine blocks.

    Runs AFTER the native create_agent stream completes, on the FULL returned
    text. Pure: no streaming, no state mutation. The single source of truth for
    the persisted reply — ``clean_prose`` equals the prose ``StreamGuard``
    streamed live (both are "everything before the first delimiter", post
    leak-guard), so streaming and non-streaming callers produce identical text.
    """
    # 1) <B_C_J>{...}</B_C_J> (or legacy ```summary_update```) -> dict + text stripped
    summary_update, text = _parse_summary_update_from_response(response_content)
    # 2) defense-in-depth: strip any mislabelled internal block the parser missed
    text = _strip_internal_tracking_leak(text)
    # 3) product handles now live in summary_update["show_products"]; still strip a
    #    legacy ###SHOW_PRODUCTS### block (and use it only as a back-compat fallback).
    legacy_handles, text = _parse_show_products_from_response(text)
    show_product_handles = _coerce_str_list((summary_update or {}).get("show_products"), cap=5) or legacy_handles
    return SimpleNamespace(
        clean_prose=text,
        summary_update=summary_update,
        show_product_handles=show_product_handles or [],
    )


def _coerce_str_list(value, cap: int = 4) -> List[str]:
    """Coerce an LLM-provided value into a clean list of non-empty strings (capped)."""
    if not isinstance(value, list):
        return []
    out = [str(s).strip() for s in value if isinstance(s, str) and str(s).strip()]
    return out[:cap]


def _clean_phone(value) -> Optional[str]:
    """Normalize a phone the LLM reported into 10 digits (strip a leading 91), or None."""
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[-10:]
    return digits if len(digits) >= 10 else None


def _signals_from_summary(summary_update) -> Dict[str, Any]:
    """Pure extraction of UI/state signals from the parsed <B_C_J> block.

    Returns only keys that are present & meaningful — conditional fields the LLM
    omitted simply don't appear. No side effects (idempotent).
    """
    su = summary_update or {}
    truthy = ("yes", "true", "1")
    signals: Dict[str, Any] = {}
    if str(su.get("is_track_order", "")).strip().lower() in truthy:
        signals["is_track_order"] = True
    phone = _clean_phone(su.get("phone_number"))
    if str(su.get("is_phone_provided", "")).strip().lower() in truthy or phone:
        signals["is_phone_provided"] = True
        if phone:
            signals["phone_number"] = phone
    suggestions = _coerce_str_list(su.get("suggestions"))
    if suggestions:
        signals["suggestions"] = suggestions
    return signals


def _emit_ui_signals(writer, signals: Dict[str, Any]) -> None:
    """Emit deliver-only signals (track_order / phone_captured) as custom stream
    events. NOTE: ``suggestions`` are intentionally NOT emitted here — they are
    carried in state and sent AFTER the product carousel (see the websocket
    carousel send) so the tiles render below the product images, in order.
    """
    if writer is None or not signals:
        return
    if signals.get("is_track_order"):
        writer({"type": "track_order", "value": "yes"})
    if signals.get("is_phone_provided"):
        writer({"type": "phone_captured", "phone": signals.get("phone_number")})


def _strip_internal_tracking_leak(text):
    """Final, label-agnostic safety net for the customer-facing message.

    Removes any leaked internal tracking note keyed on its field fingerprints
    rather than the fence label. This is defense-in-depth: even if the primary
    parser fails to recognise a mislabelled block (and therefore does not strip
    it), this guard ensures the internal note can never reach the customer.
    Cheap (pure regex) and node-agnostic.
    """
    if not isinstance(text, str) or not text:
        return text
    cleaned = text
    for pat in _LEAK_GUARD_PATTERNS:
        cleaned = pat.sub('', cleaned)
    return cleaned.strip()


def _parse_summary_update_from_response(response_content) -> tuple:
    """
    Parse optional SUMMARY_UPDATE block from LLM response.

    The LLM includes a summary_update code block at the end of its response
    with cumulative summary, entities used, and status.

    IMPORTANT: The block is ALWAYS stripped from the response regardless of
    whether JSON parsing succeeds. Internal tracking data must never reach
    the customer.

    Args:
        response_content: Raw LLM response (str or list of content parts)

    Returns:
        Tuple of (summary_update_dict_or_None, clean_response_without_block)
    """
    import json

    # Normalize list content (e.g., from Gemini multi-part responses)
    if isinstance(response_content, list):
        response_content = _normalize_llm_content(response_content)
    if not isinstance(response_content, str):
        response_content = str(response_content)

    # Primary: the consolidated <B_C_J>...</B_C_J> signal block. Tolerate an
    # unterminated closing tag (…$) so a truncated stream still strips cleanly.
    # Legacy fences (```summary_update / ```context_update, with mislabel-tolerant
    # (?:json[_\s]*)? prefix) are kept so any older model output still parses.
    strip_pattern = (
        r'<B_C_J>[\s\S]*?(?:</B_C_J>|$)'
        r'|```(?:json[_\s]*)?(?:summary_update|context_update)\s*[\s\S]*?```'
    )

    # Extract the JSON body — try the new delimiter first, then legacy fences.
    match = re.search(r'<B_C_J>\s*([\s\S]*?)\s*</B_C_J>', response_content, re.IGNORECASE)
    if not match:
        match = re.search(r'```(?:json[_\s]*)?summary_update\s*([\s\S]*?)```', response_content)
    if not match:
        # Old context_update format for backward compatibility
        match = re.search(r'```(?:json[_\s]*)?context_update\s*([\s\S]*?)```', response_content)

    if not match:
        return None, response_content

    # ALWAYS strip the block from response — it must never reach the customer
    clean_response = re.sub(strip_pattern, '', response_content, flags=re.IGNORECASE).strip()

    json_str = match.group(1).strip()

    summary_update, err = _coerce_summary_update_json(json_str)
    if summary_update is not None:
        return summary_update, clean_response

    # Log the offending payload (truncated) alongside the error so the actual
    # malformed output is visible — previously only the error was logged, which
    # made it impossible to see what the LLM emitted.
    payload_preview = json_str if len(json_str) <= 500 else f"{json_str[:500]}…"
    logger.warning(
        f"⚠️ Failed to parse summary_update JSON: {err} | payload={payload_preview!r}"
    )
    # Still return clean_response — block is stripped even if unparseable
    return None, clean_response


def _coerce_summary_update_json(json_str: str):
    """Best-effort parse of an LLM-emitted summary_update JSON blob.

    LLMs frequently deviate from strict JSON. This tries a sequence of
    increasingly lenient strategies and returns the first dict it can
    recover, along with the last error encountered (for logging).

    Returns:
        Tuple of (parsed_obj_or_None, last_error_or_None)
    """
    import ast

    last_err: Optional[Exception] = None

    # Strip an inner markdown fence the LLM may have nested inside the block,
    # e.g. ```summary_update\n```json\n{...}\n```\n```
    candidate = json_str.strip()
    fence = re.match(r'^```(?:json)?\s*([\s\S]*?)```\s*$', candidate)
    if fence:
        candidate = fence.group(1).strip()

    # Strategy 1: strict JSON as-is.
    try:
        return json.loads(candidate), None
    except json.JSONDecodeError as e:
        last_err = e

    # Strategy 2: unescape doubled braces ({{ → {, }} → }) that occur when
    # the native tool loop passes escaped prompt content directly and the
    # LLM mimics the doubled braces in its output.
    unescaped = candidate.replace("{{", "{").replace("}}", "}")
    if unescaped != candidate:
        try:
            return json.loads(unescaped), None
        except json.JSONDecodeError as e:
            last_err = e

    # Strategy 3: "Extra data" — a valid JSON object followed by trailing
    # text or additional blocks. raw_decode parses the first value and
    # ignores whatever comes after it.
    for text in (candidate, unescaped):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text)
            if isinstance(obj, (dict, list)):
                return obj, None
        except json.JSONDecodeError as e:
            last_err = e

    # Strategy 4: Python-style dict (single-quoted strings / True/False/None).
    # ast.literal_eval safely evaluates Python literals without code exec.
    # Note: ast.literal_eval can raise TypeError (e.g. "unhashable type:
    # 'dict'" when the LLM emits something the parser reads as a set of
    # dicts), so we must catch it here — otherwise it escapes this helper and
    # crashes the node with a full traceback instead of degrading gracefully.
    for text in (candidate, unescaped):
        try:
            obj = ast.literal_eval(text)
            if isinstance(obj, (dict, list)):
                return obj, None
        except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError) as e:
            last_err = e

    return None, last_err


_SHOW_PRODUCTS_RE = re.compile(
    r"###\s*SHOW_PRODUCTS\s*:\s*(\[.*?\])\s*###",
    re.DOTALL,
)


def _parse_show_products_from_response(
    response_content: str,
) -> Tuple[Optional[List[str]], str]:
    """Extract ``###SHOW_PRODUCTS:[...]###`` block from LLM response.

    The block contains a JSON array of product handles that the LLM selected
    for the carousel.  The block is always stripped from the customer-facing
    text regardless of parse success.

    Returns:
        (list_of_handles_or_None, clean_response_without_block)
    """
    import json

    if not isinstance(response_content, str):
        return None, str(response_content)

    match = _SHOW_PRODUCTS_RE.search(response_content)
    if not match:
        return None, response_content

    clean_response = _SHOW_PRODUCTS_RE.sub("", response_content).strip()

    try:
        handles = json.loads(match.group(1))
        if isinstance(handles, list):
            handles = [str(h).strip() for h in handles if h]
            return handles, clean_response
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning(f"⚠️ Failed to parse SHOW_PRODUCTS JSON: {e}")

    return None, clean_response


def _extract_entities_from_intermediate_steps(intermediate_steps: list, entity_type: str) -> List[Dict[str, Any]]:
    """
    Extract entities from agent intermediate steps (tool results).
    
    Creates EntityDTO objects with summary and full_data fields.
    
    Args:
        intermediate_steps: List of (AgentAction, observation) tuples
        entity_type: Expected entity type for this node
        
    Returns:
        List of EntityDTO dicts with summary and full_data
    """
    import json
    
    entities = []
    timestamp = datetime.now().isoformat()
    
    for step in intermediate_steps:
        if len(step) < 2:
            continue
        
        action, observation = step[0], step[1]
        
        # Get tool name
        tool_name = getattr(action, 'tool', None) or (action.get('tool') if isinstance(action, dict) else None)
        if not tool_name:
            continue
        
        # Parse observation
        obs_data = _parse_tool_observation(observation)
        if not obs_data:
            continue
        
        tool_lower = tool_name.lower()
        
        # Extract product entities
        if any(kw in tool_lower for kw in ["product", "search", "size"]):
            # Handle nested product
            product = obs_data.get("product") if isinstance(obs_data.get("product"), dict) else obs_data
            if product and (product.get("title") or product.get("name")):
                full_data = product if isinstance(obs_data.get("product"), dict) else obs_data
                entities.append({
                    "entity_type": "product",
                    "entity_id": product.get("id") or product.get("product_id") or product.get("handle"),
                    "entity_value": product.get("title") or product.get("name"),
                    "source": "tool_result",
                    "discovered_at": timestamp,
                    "summary": build_entity_summary("product", full_data),
                    "full_data": full_data
                })
            
            # Handle multiple products
            products = obs_data.get("products") or obs_data.get("matches") or []
            for p in products[:5]:
                if isinstance(p.get("product_data"), dict):
                    p = p.get("product_data")
                entities.append({
                    "entity_type": "product",
                    "entity_id": str(p.get("id") or p.get("product_id") or p.get("handle") or p.get("number") or ""),
                    "entity_value": p.get("title") or p.get("name") or f"Product #{p.get('number', 'Unknown')}",
                    "source": "tool_result",
                    "discovered_at": timestamp,
                    "summary": build_entity_summary("product", p),
                    "full_data": p
                })
        
        # Extract order entities
        elif any(kw in tool_lower for kw in ["order", "status", "track", "fetch"]):
            # Handle nested order
            order = obs_data.get("order") if isinstance(obs_data.get("order"), dict) else None
            if order:
                # Prefer order name (e.g., #gv14007) > channel_order_id > internal order_id
                # Shopify returns 'name' as the customer-facing order identifier
                order_name = order.get("name")  # e.g., "#gv14007"
                channel_id = order.get("channel_order_id")
                internal_id = order.get("order_id") or order.get("id")
                order_id = order_name or channel_id or internal_id
                entities.append({
                    "entity_type": "order",
                    "entity_id": order_id,
                    "entity_value": f"Order {order_id}" if order_id else "Unknown Order",
                    "source": "tool_result",
                    "discovered_at": timestamp,
                    "summary": build_entity_summary("order", order),
                    "full_data": order
                })
            
            # Handle direct order fields
            elif obs_data.get("order_id") or obs_data.get("channel_order_id") or obs_data.get("name"):
                # Prefer order name (e.g., #gv14007) > channel_order_id > internal order_id
                order_name = obs_data.get("name")  # e.g., "#gv14007"
                channel_id = obs_data.get("channel_order_id")
                internal_id = obs_data.get("order_id")
                order_id = order_name or channel_id or internal_id
                entities.append({
                    "entity_type": "order",
                    "entity_id": order_id,
                    "entity_value": f"Order {order_id}",
                    "source": "tool_result",
                    "discovered_at": timestamp,
                    "summary": build_entity_summary("order", obs_data),
                    "full_data": obs_data
                })
            
            # Handle multiple orders
            orders = obs_data.get("orders") or obs_data.get("data") or []
            for o in orders[:5]:
                # Prefer order name (e.g., #gv14007) > channel_order_id > internal order_id
                # Shopify returns 'name' as the customer-facing order identifier
                order_name = o.get("name")  # e.g., "#gv14007"
                channel_id = o.get("channel_order_id")
                internal_id = o.get("order_id") or o.get("id")
                order_id = order_name or channel_id or internal_id
                entities.append({
                    "entity_type": "order",
                    "entity_id": order_id,
                    "entity_value": f"Order {order_id}" if order_id else "Unknown Order",
                    "source": "tool_result",
                    "discovered_at": timestamp,
                    "summary": build_entity_summary("order", o),
                    "full_data": o
                })
    
    return entities


def _parse_tool_observation(observation) -> dict:
    """
    Parse tool observation into a dict.
    
    Handles string JSON, dict, and other formats.
    """
    import json
    
    if isinstance(observation, dict):
        return observation
    
    if isinstance(observation, str):
        try:
            return json.loads(observation)
        except json.JSONDecodeError:
            # Try to extract JSON from string
            import re
            json_match = re.search(r'\{[\s\S]*\}', observation)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except json.JSONDecodeError:
                    pass
    
    return {}


def _extract_actions_from_intermediate_steps(
    intermediate_steps: list, 
    topic_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Extract state-changing actions from agent intermediate steps.
    
    Identifies action tools (place_order, cancel_order, etc.) and creates
    ActionDTO objects for tracking.
    
    Args:
        intermediate_steps: List of (AgentAction, observation) tuples
        topic_id: Optional topic ID to link actions to
        
    Returns:
        List of ActionDTO dicts
    """
    actions = []
    
    for step in intermediate_steps:
        if len(step) < 2:
            continue
        
        action, observation = step[0], step[1]
        
        # Get tool name and input
        tool_name = getattr(action, 'tool', None) or (action.get('tool') if isinstance(action, dict) else None)
        tool_input = getattr(action, 'tool_input', None) or (action.get('tool_input') if isinstance(action, dict) else {})
        
        if not tool_name:
            continue
        
        # Check if this is an action tool
        if not is_action_tool(tool_name):
            continue
        
        # Parse observation
        obs_data = _parse_tool_observation(observation)
        
        # Determine success
        success = True
        if isinstance(obs_data, dict):
            # Check for explicit error/failure
            if obs_data.get("error") or obs_data.get("success") is False:
                success = False
            elif "failed" in str(observation).lower() or "error" in str(observation).lower():
                success = False
        
        # Extract parameters
        parameters = extract_action_parameters(tool_name, tool_input, obs_data)
        
        # Generate result summary
        result_summary = extract_action_result_summary(tool_name, obs_data, success)
        
        # Create action DTO
        action_dto = create_action_dto(
            tool_name=tool_name,
            parameters=parameters,
            success=success,
            result_summary=result_summary,
            topic_id=topic_id
        )
        
        actions.append(action_dto)
    
    return actions


def _apply_legacy_state_updates(
    response: Dict[str, Any],
    entities: List[Dict[str, Any]],
    entity_type: str,
    state: dict
) -> Dict[str, Any]:
    """
    Apply legacy state field updates for backward compatibility.
    
    Updates fields like product_link, selected_order_id, etc.
    Note: inquiry_product_info is now handled via entity system - see get_product_context tool.
    
    Args:
        response: Response dict being built
        entities: Extracted entities
        entity_type: Expected entity type
        state: Current state
        
    Returns:
        Updated response dict
    """
    if not entities:
        return response
    
    # Get the focal entity (last one)
    focal = None
    for e in reversed(entities):
        if e.get("entity_type") == entity_type:
            focal = e
            break
    
    if not focal:
        focal = entities[-1] if entities else None
    
    if not focal:
        return response
    
    if entity_type == "product":
        full_data = focal.get("full_data", {})
        summary = focal.get("summary", {})

        if summary.get("product_link") or full_data.get("url") or full_data.get("product_link"):
            response["product_link"] = summary.get("product_link") or full_data.get("url") or full_data.get("product_link")

        if full_data and (full_data.get("name") or full_data.get("title")):
            response["inquiry_product_info"] = full_data

        product_entities = [e for e in entities if e.get("entity_type") == "product" and e.get("full_data")]
        if len(product_entities) > 1:
            existing = list(response.get("product_selection_matches") or [])
            existing_handles = {p.get("handle") for p in existing if p.get("handle")}
            for pe in product_entities:
                pd = pe.get("full_data", {})
                h = pd.get("handle")
                if h and h not in existing_handles:
                    existing.append(pd)
                    existing_handles.add(h)
                elif not h:
                    existing.append(pd)
            response["product_selection_matches"] = existing

    elif entity_type == "order":
        if focal.get("entity_id"):
            response["selected_order_id"] = focal["entity_id"]
        
        summary = focal.get("summary", {})
        if summary.get("delivery_status"):
            response["order_status"] = summary["delivery_status"]
    
    return response


# ==================== DEPRECATED FUNCTIONS (kept for backward compatibility) ====================

def _merge_llm_context_update(
    context: Dict[str, Any],
    llm_update: Dict[str, Any],
    default_topic: str,
    skill_name: str = ""
) -> Dict[str, Any]:
    """
    Merge LLM-provided context updates into the conversation context.
    
    Smart merger that:
    - Detects topic changes and creates/updates topics accordingly
    - Parses simple entity strings like "Product: Blue Hoodie"
    - Updates focal entity from entities_worked_on
    
    Args:
        context: Current conversation context
        llm_update: Context update from LLM response (topic, summary, entities_worked_on)
        default_topic: Default topic type for this node
        skill_name: Name of the skill node
        
    Returns:
        Updated context dict
    """
    timestamp = datetime.now().isoformat()
    
    # Initialize topics list if not exists
    if "topics" not in context or context["topics"] is None:
        context["topics"] = []
    if "entities" not in context or context["entities"] is None:
        context["entities"] = []
    
    # ============= 1. GET LLM'S TOPIC ASSESSMENT =============
    llm_topic = llm_update.get("topic", default_topic)
    llm_summary = llm_update.get("summary", "")
    llm_entities = llm_update.get("entities_worked_on", [])
    
    # Store summary in context for reference
    if llm_summary:
        context["conversation_history_summary"] = llm_summary
        logger.info(f"📝 LLM summary: {llm_summary[:80]}...")
    
    # ============= 2. SMART TOPIC DETECTION =============
    # Compare LLM's topic with the active topic
    active_topic_type = _get_active_topic_type(context)
    topic_changed = (llm_topic != active_topic_type) if active_topic_type else True
    
    logger.info(f"🔍 Topic detection: LLM says '{llm_topic}', active is '{active_topic_type}', changed={topic_changed}")
    
    # ============= 3. TOPIC MANAGEMENT =============
    if topic_changed:
        # Create new topic
        new_topic = _create_topic_dto(llm_topic, llm_summary, timestamp)
        context["topics"].append(new_topic)
        context["active_topic_id"] = new_topic["topic_id"]
        context["topic"] = llm_topic
        logger.info(f"📋 Created new topic: {llm_topic} (id={new_topic['topic_id']})")
    else:
        # Update existing topic's summary
        _update_active_topic_summary(context, llm_summary, timestamp)
        logger.info(f"📋 Updated existing topic summary")
    
    # ============= 3b. TOPIC STATUS/CLOSURE =============
    # Get status from LLM or infer from summary keywords
    llm_status = llm_update.get("status", "open")
    
    # Auto-infer "resolved" from summary keywords if LLM didn't specify
    if llm_status == "open" and llm_summary:
        resolved_keywords = ["successfully", "completed", "placed", "cancelled", "created", "done", "answered"]
        summary_lower = llm_summary.lower()
        if any(keyword in summary_lower for keyword in resolved_keywords):
            llm_status = "resolved"
            logger.info(f"📋 Auto-inferred topic status as 'resolved' from summary keywords")
    
    # Update active topic status
    active_topic = _get_active_topic(context)
    if active_topic:
        active_topic["status"] = llm_status
        context["topic_status"] = llm_status
        if llm_status == "resolved":
            logger.info(f"✅ Topic '{llm_topic}' marked as RESOLVED")
    
    # ============= 4. PARSE LLM ENTITIES =============
    # Format: "Product: Blue Hoodie" or "Order: GV1234"
    for entity_str in llm_entities:
        parsed = _parse_entity_string(entity_str)
        if parsed:
            _add_entity_to_context(context, parsed, timestamp)
    
    # ============= 5. UPDATE FOCAL ENTITY =============
    # Use first entity from LLM as focal
    if llm_entities:
        first_entity = _parse_entity_string(llm_entities[0])
        if first_entity:
            context["focal_entity"] = {
                "entity_type": first_entity["type"],
                "entity_id": first_entity.get("id"),
                "entity_value": first_entity["value"],
                "confidence": "llm_explicit",
                "set_at": timestamp
            }
            # Link focal entity to active topic
            active_topic = _get_active_topic(context)
            if active_topic:
                active_topic["related_entity_type"] = first_entity["type"]
                active_topic["related_entity_id"] = first_entity.get("id")
    
    # Update last skill node
    context["last_skill_node"] = skill_name
    context["context_updated_at"] = timestamp
    
    return context


def _get_active_topic_type(context: Dict[str, Any]) -> Optional[str]:
    """Get the topic_type of the currently active topic."""
    active_id = context.get("active_topic_id")
    if not active_id:
        return None
    for topic in context.get("topics", []):
        if topic.get("topic_id") == active_id:
            return topic.get("topic_type")
    return None


def _get_active_topic(context: Dict[str, Any]) -> Optional[Dict]:
    """Get the currently active topic dict."""
    active_id = context.get("active_topic_id")
    if not active_id:
        return None
    for topic in context.get("topics", []):
        if topic.get("topic_id") == active_id:
            return topic
    return None


def _create_topic_dto(topic_type: str, summary: str, timestamp: str) -> Dict[str, Any]:
    """Create a new TopicDTO."""
    return {
        "topic_id": str(uuid.uuid4())[:8],
        "topic_type": topic_type,
        "status": "open",
        "started_at": timestamp,
        "updated_at": timestamp,
        "summary": summary,
        "related_entity_id": None,
        "related_entity_type": None
    }


def _update_active_topic_summary(context: Dict[str, Any], summary: str, timestamp: str) -> None:
    """Update the summary of the currently active topic."""
    active_topic = _get_active_topic(context)
    if active_topic:
        active_topic["summary"] = summary
        active_topic["updated_at"] = timestamp


def _parse_entity_string(entity_str: str) -> Optional[Dict[str, Any]]:
    """
    Parse entity string like 'Product: Blue Hoodie' or 'Order: GV1234'.
    
    Returns:
        Dict with 'type' and 'value', or None if parsing fails
    """
    if not entity_str or ":" not in entity_str:
        return None
    
    parts = entity_str.split(":", 1)
    if len(parts) != 2:
        return None
    
    entity_type = parts[0].strip().lower()
    entity_value = parts[1].strip()
    
    # Try to extract ID from value (e.g., "GV1234" or "#GV1234")
    entity_id = None
    if entity_type == "order":
        # Order IDs often start with # or are alphanumeric
        id_match = re.search(r'#?([A-Z0-9-]+)', entity_value, re.IGNORECASE)
        if id_match:
            entity_id = id_match.group(1)
    
    return {
        "type": entity_type,
        "value": entity_value,
        "id": entity_id
    }


def _add_entity_to_context(context: Dict[str, Any], parsed_entity: Dict, timestamp: str) -> None:
    """Add a parsed entity to the context's entities list (avoiding duplicates)."""
    entities = context.setdefault("entities", [])
    
    # Check for duplicates by value (since LLM entities may not have IDs)
    for existing in entities:
        if (existing.get("entity_type") == parsed_entity["type"] and 
            existing.get("entity_value") == parsed_entity["value"]):
            return  # Already exists
    
    entities.append({
        "entity_type": parsed_entity["type"],
        "entity_id": parsed_entity.get("id"),
        "entity_value": parsed_entity["value"],
        "source": "llm_response",
        "discovered_at": timestamp,
        "metadata": {}
    })


def _enrich_llm_entities_with_tool_ids(context: Dict[str, Any], tool_entities: List[Dict]) -> None:
    """
    Enrich LLM-provided entities with IDs from tool-extracted entities.
    
    Tool entities have accurate IDs from API responses. LLM entities have semantic understanding.
    Match them by entity_value similarity and copy IDs where missing.
    """
    if not tool_entities:
        return
    
    entities = context.get("entities", [])
    
    for entity in entities:
        if entity.get("entity_id"):  # Already has ID
            continue
        
        entity_type = entity.get("entity_type", "").lower()
        entity_value = entity.get("entity_value", "").lower()
        
        # Look for matching tool entity
        for tool_entity in tool_entities:
            tool_type = tool_entity.get("entity_type", "").lower()
            tool_value = tool_entity.get("entity_value", "").lower()
            tool_id = tool_entity.get("entity_id")
            
            # Match by type and value similarity
            if tool_type == entity_type and tool_id:
                if tool_value == entity_value or tool_value in entity_value or entity_value in tool_value:
                    entity["entity_id"] = tool_id
                    entity["source"] = "llm_enriched_by_tool"
                    logger.debug(f"🔗 Enriched entity '{entity_value}' with ID '{tool_id}'")
                    break
    
    # Also update focal entity if it was enriched
    focal = context.get("focal_entity", {})
    if focal and not focal.get("entity_id"):
        focal_type = focal.get("entity_type", "").lower()
        focal_value = focal.get("entity_value", "").lower()
        
        for tool_entity in tool_entities:
            tool_type = tool_entity.get("entity_type", "").lower()
            tool_id = tool_entity.get("entity_id")
            tool_value = tool_entity.get("entity_value", "").lower()
            
            if tool_type == focal_type and tool_id:
                if tool_value == focal_value or tool_value in focal_value or focal_value in tool_value:
                    focal["entity_id"] = tool_id
                    break


def _fallback_topic_management(
    context: Dict[str, Any],
    topic_type: str,
    skill_name: str,
    customer_message: str = ""
) -> Dict[str, Any]:
    """
    Fallback topic management when LLM doesn't provide context_update.
    
    IMPORTANT: This function only UPDATES existing topics of the SAME TYPE.
    Topic creation is LLM-driven only (via context_update block).
    If topic type doesn't match, we update the legacy 'topic' field but don't
    corrupt the existing topic's data.
    """
    timestamp = datetime.now().isoformat()
    
    # Initialize topics list if not exists
    if "topics" not in context or context["topics"] is None:
        context["topics"] = []
    
    # Generate simple fallback summary
    entity_name = context.get("focal_entity", {}).get("entity_value", "")
    fallback_summary = f"Handled {skill_name.replace('_', ' ')}{' for ' + entity_name if entity_name else ''}"
    
    # Get active topic and check if type matches
    active_topic = _get_active_topic(context)
    active_topic_type = active_topic.get("topic_type") if active_topic else None
    
    if active_topic and active_topic_type == topic_type:
        # Topic type matches - safe to update summary
        active_topic["summary"] = fallback_summary
        active_topic["updated_at"] = timestamp
        logger.debug(f"📋 Fallback: Updated existing topic '{topic_type}' summary")
    elif active_topic and active_topic_type != topic_type:
        # Topic type MISMATCH - don't corrupt the existing topic!
        # Just update the legacy 'topic' field to track current skill type
        logger.debug(f"📋 Fallback: Topic type mismatch (active={active_topic_type}, current={topic_type})")
        logger.debug(f"📋 Fallback: Not updating topic summary to avoid corruption")
        # Update legacy field only
        context["topic"] = topic_type
    else:
        # No active topic and LLM didn't provide context - just log
        logger.debug(f"📋 Fallback: No active topic to update, skipping")
        context["topic"] = topic_type
    
    context["last_skill_node"] = skill_name
    context["context_updated_at"] = timestamp
    
    return context


async def _handle_escalation(state: SupportState, response_content: str, agent_name: str) -> Dict[str, Any]:
    """
    Handle escalation requests from agent responses.

    The customer-facing line honours ``escalation_messaging.customer_message``
    so this path cannot promise a callback a client has asked us not to promise.

    Args:
        state: Current state
        response_content: Agent response containing escalation
        agent_name: Name of the agent that triggered escalation
        
    Returns:
        Response dict with escalation flags
    """
    log_with_trace_id(state, f"🚨 ESCALATION detected in {agent_name}")
    
    # Extract reason if provided
    reason = "Customer request requires human assistance"
    if ":" in response_content:
        parts = response_content.split(":", 1)
        if len(parts) > 1:
            reason = parts[1].strip()

    from fashion_bot.agent_config import aget_escalation_customer_message

    customer_message = await aget_escalation_customer_message(
        state.get("client_id"),
        default=f"I'll connect you with a team member who can better assist you. {reason}",
    )

    return {
        "type": "escalation",
        "customer_message": customer_message,
        "needs_escalation": True,
        "escalation_reason": reason,
        "trace_id": get_trace_id(state),
        "conversation_context": state.get("conversation_context", {})
    }


def _log_input_context(state: SupportState, ctx: Dict[str, Any], agent_name: str) -> None:
    """Log a single-line INPUT context summary."""
    if not ctx or (not ctx.get("topics") and not ctx.get("entities")):
        log_with_trace_id(state, f"📥 [{agent_name}] ctx=empty")
        return

    topics = ctx.get("topics", [])
    entities = ctx.get("entities", [])
    actions = ctx.get("recent_actions", [])
    active = ctx.get("topic", "?")
    focal = ctx.get("focal_entity", {})
    focal_str = f" focal={focal.get('entity_id')}" if focal and focal.get("entity_id") else ""
    log_with_trace_id(
        state,
        f"📥 [{agent_name}] topic={active} topics={len(topics)} entities={len(entities)} actions={len(actions)}{focal_str}"
    )


def _log_conversation_context(
    state: SupportState,
    ctx: Dict[str, Any],
    agent_name: str,
    entities_extracted: int = 0,
    actions_tracked: int = 0
) -> None:
    """Log a single-line UPDATED context summary."""
    topics = ctx.get("topics", [])
    entities = ctx.get("entities", [])
    focal = ctx.get("focal_entity", {})
    focal_str = focal.get("entity_id", "none") if focal else "none"
    actions = ctx.get("recent_actions", [])
    last_action = actions[-1].get("action_name") if actions else "none"
    log_with_trace_id(
        state,
        f"📤 [{agent_name}] topics={len(topics)} entities={len(entities)} focal={focal_str} +{entities_extracted}ent +{actions_tracked}act last_action={last_action}"
    )


# ==================== PRE-CONFIGURED NODES ====================
# These are convenience functions that create commonly used nodes

def create_product_details_node() -> Callable[[SupportState], Dict[str, Any]]:
    """Create a product details skill node."""
    return create_generic_skill_node(
        agent_name="product_details",
        topic="product_inquiry",
        entity_type="product",
        legacy_fields=["product_link", "product_selection_matches"]
    )


def create_order_status_node() -> Callable[[SupportState], Dict[str, Any]]:
    """Create an order status skill node."""
    return create_generic_skill_node(
        agent_name="order_status",
        topic="order_status",
        entity_type="order",
        legacy_fields=["selected_order_id", "order_status_by_id", "known_orders"]
    )


def create_place_order_node() -> Callable[[SupportState], Dict[str, Any]]:
    """Create a place order skill node."""
    return create_generic_skill_node(
        agent_name="place_order",
        topic="order_placement",
        entity_type="product",
        legacy_fields=["product_link"]
    )


# Export commonly used node creators
__all__ = [
    "create_generic_skill_node",
    "create_product_details_node",
    "create_order_status_node",
    "create_place_order_node",
]
