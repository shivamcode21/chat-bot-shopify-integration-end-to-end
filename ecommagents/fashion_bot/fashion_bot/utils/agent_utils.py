"""
Agent utility functions for creating provider-agnostic agents.

LangChain 1.0 native: agents are built with ``langchain.agents.create_agent``
(a compiled LangGraph tool-calling agent). This replaces the v0 stack
(``create_openai_tools_agent`` / ``create_tool_calling_agent`` + ``AgentExecutor``)
that used to live in ``langchain``/``langchain-classic`` — so we no longer depend
on the ``langchain-classic`` package.

``create_agent`` works across providers (OpenAI, Gemini, Anthropic, ...) because it
calls ``model.bind_tools`` internally, so a single code path covers every LLM.
"""

import ast
import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Set, Tuple

from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import (
    AIMessage, AIMessageChunk, HumanMessage, SystemMessage, ToolMessage, BaseMessage,
)
from langgraph.errors import GraphRecursionError
from openai import APIError as OpenAIAPIError, APIConnectionError as OpenAIConnectionError

from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.utils.product_utils import format_product_tool_result
from fashion_bot.utils.agent_middleware import (
    OrderAuthMiddleware, grounding_middleware_for, _loads_observation,
)
from fashion_bot.utils.tool_action_names import emit_tool_action_events

_TRANSIENT_OPENAI_ERRORS = (OpenAIAPIError, OpenAIConnectionError)
_STREAM_RETRY_ATTEMPTS = 2
_STREAM_RETRY_BACKOFF_S = 1.0

logger = logging.getLogger(__name__)


class ProductObservationFormatterMiddleware(AgentMiddleware):
    """Restore native-loop tool-result formatting under ``create_agent``.

    The deleted native loop fed the MODEL ``format_products_for_llm(products)``
    (internal fields stripped, empty values dropped) — NOT the raw tool dict.
    create_agent's ToolNode serializes the raw dict instead, leaking internal
    retrieval fields (qu_query, qu_filter, content_hash, product_id, ...) into the
    model context and inflating tokens. This rewrites the ToolMessage in place:
      - ``content``  -> cleaned text the model reads (parity with the native loop)
      - ``artifact`` -> the raw dict, preserved for downstream carousel extraction
                        (``messages_to_intermediate_steps`` reads ``.artifact``).
    Non-product tool results are returned untouched.
    """

    def __init__(self, state=None):
        super().__init__()
        self._state = state

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result  # Command or other: leave as-is
        # This wrapper runs ONLY for model-initiated tool calls (the grounding
        # middleware injects its result in abefore_model and bypasses this path),
        # so this log unambiguously marks an LLM-driven call — complementing the
        # "🔎 [GROUNDING] auto-called" line emitted for the middleware's call.
        log_with_trace_id(
            self._state,
            f"🤖 [LLM-TOOL] model called '{getattr(result, 'name', None) or 'unknown'}'",
        )
        formatted = format_product_tool_result(_loads_observation(result.content))
        if formatted is not None:
            # content -> cleaned text the model reads; artifact -> raw dict kept
            # for downstream carousel extraction (messages_to_intermediate_steps).
            result.content, result.artifact = formatted
        return result


def build_agent_graph(
    llm, tools, system_prompt: Optional[str] = None, state=None,
    auto_ground_tool: Optional[str] = None,
    fallback_llm=None,
):
    """Build a native LangChain 1.0 agent graph.

    Returns a compiled LangGraph agent that runs the tool-calling loop internally.
    Invoke with ``{"messages": [...]}`` and stream with
    ``.astream_events({"messages": [...]}, version="v2")``.

    Args:
        llm: The chat model instance (ChatOpenAI, ChatGoogleGenerativeAI, ...).
        tools: List of tools available to the agent.
        system_prompt: Optional system prompt string prepended to the conversation.
        state: Optional state for trace-id logging.
        auto_ground_tool: When set, the named tool is auto-invoked before the first
            model call and its result injected into the message history as a
            synthetic tool call (``ToolGroundingMiddleware``) — the agent starts
            already grounded. Opt-in per agent; no-op (with a warning) if the named
            tool isn't in ``tools``.
        fallback_llm: Optional default chat model used as a native LangChain
            failover target.  When provided the primary model is wrapped with
            ``Runnable.with_fallbacks([fallback_llm])`` so a provider error
            (429, SSE corruption, transient 5xx) transparently retries the SAME
            request on the default model.  ``None`` disables failover.
    """
    log_with_trace_id(state, f"🔧 Building create_agent graph ({len(tools)} tools)")
    model = llm
    if fallback_llm is not None:
        model = llm.with_fallbacks([fallback_llm])
    # OrderAuthMiddleware applies verification grants the order tools return
    # (tools stay stateless — AGENTS.md §2); it must see every model-initiated
    # tool result, so it is always registered.
    middleware = [ProductObservationFormatterMiddleware(state), OrderAuthMiddleware(state)]
    if auto_ground_tool:
        grounding = grounding_middleware_for(auto_ground_tool, tools, state)
        if grounding is not None:
            middleware.append(grounding)
    return create_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt,
        middleware=middleware,
    )


def _content_to_text(content: Any) -> str:
    """Flatten a message ``content`` (str or list-of-blocks) into plain text.

    Mirrors generic_skill_node._normalize_llm_content / langchain BaseMessage.text():
    join text parts with newlines and include ONLY blocks whose ``type == "text"``.
    This matters for multi-part (Gemini/Anthropic) responses:
      - newline join preserves separators between parts (was ``""``),
      - the type filter keeps reasoning/thinking blocks (which also carry a "text"
        key) OUT of the customer-visible answer (was: any dict's text leaked).
    Empty/non-text list -> "" (not str(content)) so the streaming-chunk feed never
    pushes a list repr to StreamGuard; the final-text path tolerates "" (the node
    has an empty-response fallback).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


# Anchors that begin a machine-readable trailing block. Everything from the
# earliest anchor onward is the "machine tail" (parsed later by the node, never
# shown raw). `<B_C_J>` is the consolidated signal block (summary + show_products +
# track_order/phone/suggestions). The bare triple-backtick fence + ###SHOW_PRODUCTS
# are kept for back-compat so any legacy block a model still emits is also held back.
# Customer replies never contain these literally, so this cannot truncate prose.
# (Fence-less trailing tracking JSON is still stripped post-hoc by the node's
# _strip_internal_tracking_leak on the stored message; see streaming notes.)
_MACHINE_BLOCK_ANCHORS = ("<B_C_J>", "###SHOW_PRODUCTS", "```")
_MACHINE_HOLDBACK = max(len(a) for a in _MACHINE_BLOCK_ANCHORS) - 1


class StreamGuard:
    """Split a streamed answer into prose (emit) vs machine-block (buffer).

    ``feed()`` returns only prose safe to surface, holding back the shortest
    suffix that could still grow into a block anchor (so an anchor split across
    two chunks is never leaked). Once any anchor appears, all later text accrues
    to ``machine_tail`` and ``feed()`` returns ``""`` forever after. Transport
    only: it decides what STREAMS to the user; correctness of the blocks is
    ``parse_agent_output``'s job on the full returned text.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._in_machine = False
        self.machine_tail = ""
        self.prose = ""

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        if self._in_machine:
            self.machine_tail += delta
            return ""
        buf = self._pending + delta
        idx = self._earliest_anchor(buf)
        if idx is not None:                       # first delimiter -> stop streaming
            emit, self._in_machine = buf[:idx], True
            self.machine_tail, self._pending = buf[idx:], ""
            self.prose += emit
            return emit
        hold = self._anchor_prefix_suffix_len(buf)        # 0 for normal prose
        emit = buf[: len(buf) - hold] if hold else buf
        self._pending = buf[len(buf) - hold :] if hold else ""
        self.prose += emit
        return emit

    @staticmethod
    def _earliest_anchor(text: str):
        best = None
        for anchor in _MACHINE_BLOCK_ANCHORS:
            i = text.find(anchor)
            if i != -1 and (best is None or i < best):
                best = i
        return best

    @staticmethod
    def _anchor_prefix_suffix_len(text: str) -> int:
        for k in range(min(len(text), _MACHINE_HOLDBACK), 0, -1):
            if any(a.startswith(text[-k:]) for a in _MACHINE_BLOCK_ANCHORS):
                return k
        return 0


async def _force_grounding_tool(
    llm, tools, messages: List[BaseMessage], tool_name: str, state=None,
    system_prompt: Optional[str] = None,
) -> List[BaseMessage]:
    """Force the first tool call (parity with the deleted native loop's
    ``force_first_tool``).

    Policy skills MUST read a data source before answering (e.g. quote a real
    return window) — the native loop guaranteed this via ``tool_choice``.
    ``create_agent`` has no first-turn force, so we run ONE bound call here and
    append the AI(tool_call) + ToolMessage to history, then hand the augmented
    messages to ``create_agent`` to continue. Best-effort: any failure leaves the
    messages unchanged and the agent proceeds ungrounded (logged by caller).

    The forced call is made WITH the skill ``system_prompt`` prepended (parity with
    the native loop, which included the system blocks) so the model picks correct
    tool args; the SystemMessage is used only for this call and is NOT persisted
    into the returned history (create_agent injects its own system prompt).
    """
    tool_map = {t.name: t for t in tools if hasattr(t, "name")}
    if tool_name not in tool_map:
        log_with_trace_id(state, f"⚠️ grounding tool '{tool_name}' not available; skipping", "warning")
        return messages
    forced = llm.bind_tools(tools, tool_choice=tool_name)
    call_messages = (
        [SystemMessage(content=system_prompt)] + list(messages) if system_prompt else list(messages)
    )
    ai = await forced.ainvoke(call_messages)
    out = list(messages) + [ai]
    for tool_call in (getattr(ai, "tool_calls", None) or []):
        tool = tool_map.get(tool_call.get("name"))
        try:
            obs = await tool.ainvoke(tool_call.get("args") or {}) if tool is not None \
                else f"Tool '{tool_call.get('name')}' not found"
        except Exception as exc:  # noqa: BLE001 - surface as observation, never crash grounding
            obs = f"Tool '{tool_call.get('name')}' failed: {exc}"
        content = obs if isinstance(obs, str) else json.dumps(obs, default=str)
        out.append(ToolMessage(
            content=content,
            tool_call_id=tool_call.get("id") or "grounding_call",
            name=tool_call.get("name") or "unknown_tool",
        ))
    return out


def _final_text(messages: List[BaseMessage]) -> str:
    """Extract the final assistant text from a create_agent message list.

    Reads ``.content`` directly (avoiding the deprecated ``AIMessage.text()``
    method) and flattens multimodal list content.
    """
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return _content_to_text(msg.content).strip()
    return ""


def _parse_observation(content: Any) -> Any:
    """Best-effort restore a structured observation (dict/list) from a ToolMessage's
    stringified content.

    ``create_agent`` serializes non-string tool returns (e.g. ``{"products": [...]}``)
    into the ToolMessage ``content`` as JSON. The native tool loop, by contrast, keeps
    the raw object. Several downstream consumers branch on ``isinstance(obs, dict)``
    (product extraction, escalation detection), so we re-hydrate the structure here to
    preserve native-loop behavior. Plain-text tool outputs are returned unchanged.
    """
    if not isinstance(content, str):
        return content
    stripped = content.strip()
    if not stripped or stripped[0] not in "{[":
        return content
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    try:
        return ast.literal_eval(stripped)
    except (ValueError, SyntaxError):
        return content


def messages_to_intermediate_steps(messages: List[BaseMessage]) -> List[Tuple[Any, Any]]:
    """Convert create_agent output messages into ``(action, observation)`` tuples.

    Matches the shape produced by the native tool loop in ``generic_skill_node`` so
    all downstream consumers (``_build_tool_trace_struct``, ``_extract_*`` helpers,
    escalation checks, ...) keep working unchanged. ``action`` exposes ``.tool`` and
    ``.tool_input``; ``observation`` is the tool's output (re-hydrated to a dict/list
    when the tool returned structured data — see ``_parse_observation``).
    """
    tool_messages_by_id = {
        m.tool_call_id: m for m in messages if isinstance(m, ToolMessage)
    }
    steps: List[Tuple[Any, Any]] = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            for tool_call in (msg.tool_calls or []):
                tool_msg = tool_messages_by_id.get(tool_call.get("id"))
                # Prefer the raw dict the formatter middleware preserved on
                # .artifact (ToolMessage.content is now the cleaned model-facing
                # text); fall back to parsing content for untouched tools.
                if tool_msg is None:
                    observation = ""
                elif getattr(tool_msg, "artifact", None) is not None:
                    observation = tool_msg.artifact
                else:
                    observation = _parse_observation(tool_msg.content)
                action = SimpleNamespace(
                    tool=tool_call.get("name"),
                    tool_input=tool_call.get("args") or {},
                )
                steps.append((action, observation))
    return steps


async def run_agent_graph(
    llm,
    tools,
    system_prompt: Optional[str],
    chat_history: List[Any],
    user_input: str,
    max_iterations: int = 10,
    state=None,
    writer=None,
    grounding_tool: Optional[str] = None,
    auto_ground_tool: Optional[str] = None,
    fallback_llm=None,
) -> Dict[str, Any]:
    """Run a ``create_agent`` graph to completion.

    Returns an AgentExecutor-compatible result so existing callers don't change:
    ``{"output": <final assistant text>, "intermediate_steps": [(action, observation), ...]}``.

    Args:
        writer: When provided (LangGraph custom stream writer), prose tokens are
            streamed live via ``writer({"type":"token","content":...})`` and cut
            off at the first delimited block by ``StreamGuard``. The returned
            ``output`` is still the COMPLETE text (prose + blocks) — the guard
            governs only what streams, not the return value.
        grounding_tool: When set, that tool is forced on the first call before
            ``create_agent`` runs (parity with the deleted native loop).
        auto_ground_tool: When set, that tool is auto-invoked and its result
            injected as a synthetic tool call before the first model turn via
            ``ToolGroundingMiddleware`` (opt-in per agent; used by the
            recommendation agent to pre-fetch product search results).
        fallback_llm: Optional default chat model for transparent failover (see
            ``build_agent_graph``).
    """
    graph = build_agent_graph(
        llm, tools, system_prompt=system_prompt, state=state,
        auto_ground_tool=auto_ground_tool,
        fallback_llm=fallback_llm,
    )
    messages: List[BaseMessage] = list(chat_history or [])
    messages.append(HumanMessage(content=user_input))

    if grounding_tool:
        try:
            messages = await _force_grounding_tool(
                llm, tools, messages, grounding_tool, state, system_prompt=system_prompt
            )
        except Exception as exc:  # noqa: BLE001 - grounding must never break the turn
            log_with_trace_id(
                state, f"⚠️ grounding '{grounding_tool}' failed ({exc}); proceeding ungrounded", "warning"
            )

    # create_agent uses ~2 graph supersteps per tool iteration (model + tools).
    # Tie the recursion limit to the requested tool-iteration count (plus a small
    # buffer for the final answer turn) so max_iterations stays meaningful — rather
    # than flooring at langgraph's default 25 (~12 loops) regardless of the request.
    # create_agent uses 2 supersteps per tool loop (model + tools) + 1 final model
    # turn, so 2*max_iterations + 1 reproduces the old `range(max_iterations)` cap
    # (escalation-on-max-iters fires at the same point, not ~3 turns later).
    recursion_limit = 2 * max_iterations + 1

    # Stream state values so we retain the partial message list if the graph hits
    # the recursion limit — the caller's recovery path needs those intermediate
    # steps to salvage a reply (AgentExecutor returned accumulated steps on max-iters).
    last_messages: List[BaseMessage] = []
    stream_modes = ["messages", "values"] if writer is not None else "values"
    # Tool-call ids already surfaced to the UI as a "tool" action event. Threaded
    # across supersteps so the growing `values` message list never double-emits.
    announced_tool_calls: Set[str] = set()

    for attempt in range(_STREAM_RETRY_ATTEMPTS):
        last_messages = []
        guard = StreamGuard() if writer is not None else None
        announced_tool_calls = set()

        try:
            async for item in graph.astream(
                {"messages": messages},
                stream_mode=stream_modes,
                config={"recursion_limit": recursion_limit},
            ):
                mode, chunk = item if writer is not None else ("values", item)
                if mode == "messages":
                    # Native token stream: forward guard-approved PROSE only. Tokens
                    # from the delimited blocks are buffered (machine_tail), not sent.
                    #
                    # CRITICAL: only stream the MAIN agent's answer node ('model').
                    # Tools may invoke their own nested LLMs (e.g. query_understanding
                    # inside search_products); those chunks arrive under langgraph_node
                    # 'tools' and must NOT be streamed, or their raw JSON output leaks
                    # into the customer message.
                    msg, meta = chunk if isinstance(chunk, tuple) else (chunk, {})
                    if (
                        isinstance(msg, AIMessageChunk)
                        and (meta or {}).get("langgraph_node") == "model"
                    ):
                        emit = guard.feed(_content_to_text(msg.content))
                        if emit:
                            writer({"type": "token", "content": emit})
                elif mode == "values":
                    if isinstance(chunk, dict) and chunk.get("messages"):
                        last_messages = chunk["messages"]
                        # Surface "what the bot is doing" labels for each new tool
                        # call (idempotent via announced_tool_calls). No-op when not
                        # streaming (writer is None).
                        announced_tool_calls = emit_tool_action_events(
                            writer, last_messages, announced_tool_calls
                        )

            return {
                "output": _final_text(last_messages),
                "intermediate_steps": messages_to_intermediate_steps(last_messages),
            }
        except GraphRecursionError:
            # Mirror AgentExecutor's max-iterations sentinel so the caller's existing
            # recovery path (recover-from-intermediate-steps / escalate) still triggers,
            # and hand back the steps accumulated so far so recovery can actually work.
            log_with_trace_id(
                state, "⚠️ create_agent hit recursion limit (max iterations reached)", "warning"
            )
            return {
                "output": "Agent stopped due to max iterations.",
                "intermediate_steps": messages_to_intermediate_steps(last_messages),
            }
        except _TRANSIENT_OPENAI_ERRORS as exc:
            is_last = attempt >= _STREAM_RETRY_ATTEMPTS - 1
            log_with_trace_id(
                state,
                f"⚠️ Transient OpenAI stream error (attempt {attempt + 1}/{_STREAM_RETRY_ATTEMPTS}): {exc}"
                + (" — retrying" if not is_last else " — exhausted retries, raising"),
                "warning",
            )
            if is_last:
                raise
            if writer is not None:
                writer({"type": "stream_reset"})
            await asyncio.sleep(_STREAM_RETRY_BACKOFF_S * (attempt + 1))
