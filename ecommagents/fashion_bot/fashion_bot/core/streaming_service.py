"""
Streaming Service for Web Chat

Provides token-by-token streaming for web chat responses.
This enables real-time response display as tokens are generated.

Usage:
    async for chunk in stream_agent_response(state, message):
        await websocket.send_json({"type": "stream", "token": chunk})
"""

import asyncio
import inspect
import logging
from typing import Dict, Any, AsyncGenerator, Optional, List, Tuple
from datetime import datetime
from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from fashion_bot.core.llm_factory import LLMFactory
from fashion_bot.utils.agent_utils import build_agent_graph
from fashion_bot.core.message_persistence import ensure_assistant_message_for_skip_final
from fashion_bot.utils.utils import log_with_trace_id, get_trace_id
from fashion_bot.utils.outbound_guard import sanitize_outbound_text, TOOL_CALL_DIRECTIVE, enrich_after_tool_call_directive
from fashion_bot.langsmith_config import get_langsmith_config
from fashion_bot.utils.langsmith_tracing import snapshot_state_for_trace
from fashion_bot.rollbar_config import report_error

logger = logging.getLogger("streaming_service")

# Custom stream events emitted by skill nodes (via the LangGraph writer) that are
# forwarded verbatim to the websocket/UI layer. Add a type here to surface a new
# node-emitted UI signal end-to-end.
_UI_PASSTHROUGH_EVENTS = frozenset({"products", "suggestions", "track_order", "phone_captured", "tool", "stream_reset"})

_langsmith_runtime_cache: Dict[str, Tuple[Any, bool]] = {}


def _get_langsmith_runtime(service: str) -> Tuple[Any, bool]:
    """Lazily initialize LangSmith config per service to avoid module import side-effects."""
    svc = (service or "general").strip().lower() or "general"
    cached = _langsmith_runtime_cache.get(svc)
    if cached is not None:
        return cached
    cfg = get_langsmith_config(svc)
    # Keep env mutation out of hot path; we pass explicit project_name in trace() calls.
    enabled = bool(getattr(cfg, "api_key", None))
    _langsmith_runtime_cache[svc] = (cfg, enabled)
    return cfg, enabled


def _set_trace_io(run: Any, *, inputs: Optional[Dict[str, Any]] = None, outputs: Optional[Dict[str, Any]] = None) -> None:
    """Best-effort run input/output attachment for LangSmith compatibility across SDK versions."""
    if run is None:
        return
    if inputs is not None:
        try:
            if hasattr(run, "add_inputs"):
                run.add_inputs(inputs)
            else:
                run.inputs = inputs
        except Exception:
            pass
    if outputs is not None:
        try:
            if hasattr(run, "add_outputs"):
                run.add_outputs(outputs)
            else:
                run.outputs = outputs
        except Exception:
            pass


async def _await_maybe(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _queued_oldest_ts_for_trace_name(raw_ts: Any) -> Optional[str]:
    """Convert ISO-ish timestamp to trace-name-safe compact format."""
    txt = str(raw_ts or "").strip()
    if not txt:
        return None
    try:
        dt = datetime.fromisoformat(txt)
        return dt.strftime("%Y%m%d-%H%M%S")
    except Exception:
        return None


class StreamingService:
    """
    Service for streaming LLM responses in web chat.
    
    The streaming flow:
    1. Execute tools (non-streaming) to gather data
    2. Generate final response with streaming enabled
    3. Yield tokens as they arrive
    """

    @staticmethod
    def _system_prompt_from_template(
        prompt_template: ChatPromptTemplate,
        current_message: str,
        recent_messages: Optional[List] = None,
    ) -> Optional[str]:
        """Best-effort extraction of a single system-prompt string from a
        ChatPromptTemplate so it can be handed to create_agent (which takes a
        ``system_prompt`` string instead of a scratchpad-based template)."""
        try:
            rendered = prompt_template.format_messages(
                input=current_message,
                chat_history=recent_messages or [],
                agent_scratchpad=[],
            )
            system_text = "\n\n".join(
                str(m.content) for m in rendered if isinstance(m, SystemMessage)
            )
            return system_text or None
        except Exception:
            return None

    @staticmethod
    async def stream_response(
        state: Dict[str, Any],
        agent_name: str,
        tools: List,
        prompt_template: ChatPromptTemplate,
        current_message: str,
        recent_messages: List = None,
        max_iterations: int = 6
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Stream the agent response token by token.
        
        This is a hybrid approach:
        1. First, run the agent normally to execute tools
        2. Then, stream the final response generation
        
        Args:
            state: Current conversation state
            agent_name: Name of the agent (for LLM config)
            tools: List of tools available to the agent
            prompt_template: Chat prompt template
            current_message: Current user message
            recent_messages: Recent conversation history
            max_iterations: Max tool call iterations
            
        Yields:
            Dict with streaming events:
            - {"type": "tool_start", "tool": "tool_name", "input": {...}}
            - {"type": "tool_end", "tool": "tool_name", "output": "..."}
            - {"type": "token", "content": "..."}
            - {"type": "end", "full_response": "..."}
            - {"type": "error", "message": "..."}
        """
        try:
            # Get streaming-capable LLM
            llm = await LLMFactory.aget_llm(tool_name=agent_name, state=state)

            # Build a native LangChain 1.0 agent graph (create_agent). The system
            # prompt is derived from the passed ChatPromptTemplate's system blocks;
            # the tool-calling loop runs inside the graph.
            system_prompt = StreamingService._system_prompt_from_template(
                prompt_template, current_message, recent_messages
            )
            agent_graph = build_agent_graph(llm, tools, system_prompt=system_prompt, state=state)

            agent_messages = list(recent_messages or [])
            agent_messages.append(HumanMessage(content=current_message))

            # create_agent runs ~2 supersteps per tool iteration; tie the limit to
            # max_iterations (plus a buffer) so it stays meaningful.
            recursion_limit = 2 * max_iterations + 5

            # Use astream_events for token-level streaming
            full_response = ""
            tool_outputs = []

            async for event in agent_graph.astream_events(
                {"messages": agent_messages},
                version="v2",
                config={"recursion_limit": recursion_limit},
            ):
                kind = event.get("event")
                
                if kind == "on_tool_start":
                    tool_name = event.get("name", "unknown")
                    tool_input = event.get("data", {}).get("input", {})
                    yield {
                        "type": "tool_start",
                        "tool": tool_name,
                        "input": tool_input
                    }
                    
                elif kind == "on_tool_end":
                    tool_name = event.get("name", "unknown")
                    tool_output = event.get("data", {}).get("output", "")
                    # create_agent surfaces the ToolMessage object here; expose its
                    # textual content (not the object repr) to the UI/consumers.
                    if hasattr(tool_output, "content"):
                        tool_output = tool_output.content
                    tool_outputs.append({"tool": tool_name, "output": tool_output})
                    yield {
                        "type": "tool_end",
                        "tool": tool_name,
                        "output": str(tool_output)[:200]  # Truncate for UI
                    }
                    
                elif kind == "on_chat_model_stream":
                    # This is where we get the actual tokens
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        content = chunk.content
                        full_response += content
                        yield {
                            "type": "token",
                            "content": content
                        }
            
            # Send completion event
            yield {
                "type": "end",
                "full_response": full_response,
                "tool_outputs": tool_outputs
            }
            
        except Exception as e:
            logger.error(f"Streaming error: {str(e)}")
            report_error(
                "StreamingService stream_response error",
                level='error',
                exc_info=(type(e), e, e.__traceback__),
                agent_name=agent_name,
            )
            yield {
                "type": "error",
                "message": f"Error generating response: {str(e)}"
            }


async def _invoke_graph_with_langsmith(
    graph,
    state: Dict[str, Any],
    trace_id: str,
    user_identifier: str,
    langsmith_service: str = "general",
    input_message: Optional[str] = None,
    trace_client_id: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Async helper to invoke graph with LangSmith tracing.
    
    Returns:
        Tuple of (result dict, langsmith_trace_id string)
    """
    langsmith_trace_id = None
    result = None
    
    langsmith_config, langsmith_enabled = _get_langsmith_runtime(langsmith_service)

    if langsmith_enabled and langsmith_config.is_enabled:
        try:
            from langsmith import trace
            
            queued_oldest_ts = state.get("_queued_oldest_ts")
            queued_oldest_suffix = _queued_oldest_ts_for_trace_name(queued_oldest_ts)
            trace_name = (
                (
                    f"gupshup-fashion-bot-conversation-queued-processing-oldest-{queued_oldest_suffix}"
                    if queued_oldest_suffix
                    else "gupshup-fashion-bot-conversation-queued-processing"
                )
                if langsmith_service == "gupshup" and state.get("_queued_processing")
                else (
                    "gupshup-fashion-bot-conversation"
                    if langsmith_service == "gupshup"
                    else f"{langsmith_service}-streaming-conversation"
                )
            )
            with trace(
                name=trace_name,
                run_type="chain",
                project_name=langsmith_config.project_name,
                metadata={
                    "trace_id": trace_id,
                    "user_identifier": user_identifier,
                    "client_id": trace_client_id or state.get("client_id"),
                    "service": f"{langsmith_service}-streaming",
                    "queued_processing": bool(state.get("_queued_processing")),
                    "queued_processing_count": int(state.get("_queued_processing_count") or 0),
                    "queued_oldest_ts": queued_oldest_ts,
                }
            ) as run:
                if run is not None and hasattr(run, 'id'):
                    langsmith_trace_id = str(run.id)
                clean_input = str(input_message or "").strip()
                if not clean_input:
                    msgs = state.get("messages") if isinstance(state, dict) else None
                    if isinstance(msgs, list):
                        for msg in reversed(msgs):
                            content = getattr(msg, "content", None)
                            if content:
                                clean_input = str(content).strip()
                                if clean_input:
                                    break
                _set_trace_io(
                    run,
                    inputs={
                        "input": clean_input[:2000],
                        "state": snapshot_state_for_trace(state),
                    },
                )
                
                result = await graph.ainvoke(state)
                result = ensure_assistant_message_for_skip_final(
                    state_before_invoke=state,
                    result=result,
                )
                reply_preview = ""
                if isinstance(result, dict):
                    if result.get("customer_message"):
                        reply_preview = str(result.get("customer_message") or "")
                    elif result.get("messages"):
                        last_msg = result["messages"][-1]
                        reply_preview = str(getattr(last_msg, "content", last_msg) or "")
                _set_trace_io(
                    run,
                    outputs={
                        "output": reply_preview[:2000],
                        "state": snapshot_state_for_trace(result),
                    },
                )
                
        except Exception as trace_err:
            logger.warning(f"LangSmith trace error for service={langsmith_service}: {trace_err}, invoking without tracing")
            result = await graph.ainvoke(state)
            result = ensure_assistant_message_for_skip_final(
                state_before_invoke=state,
                result=result,
            )
    else:
        result = await graph.ainvoke(state)
        result = ensure_assistant_message_for_skip_final(
            state_before_invoke=state,
            result=result,
        )
    
    if not langsmith_trace_id:
        langsmith_trace_id = trace_id
    
    return result, langsmith_trace_id


async def _astream_graph_events(
    graph,
    state: Dict[str, Any],
    trace_id: str,
    user_identifier: str,
    langsmith_service: str = "general",
    input_message: Optional[str] = None,
    trace_client_id: Optional[str] = None,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Streaming variant of ``_invoke_graph_with_langsmith``.

    Drives ``graph.astream(stream_mode=["custom","values"])`` so the skill node's
    writer events (``token`` / ``products``) stream live, while ``values`` yields
    the full final state (equivalent to ``ainvoke``'s return). Yields, in order:
      - ``{"type":"token","content":...}``     prose deltas (from node writer)
      - ``{"type":"products", ...}``           carousel (from node writer)
      - ``{"type":"__final__","result":<full state>,"full_response":...,
            "langsmith_trace_id":...}``        terminal sentinel

    LangSmith span is opened/closed manually so it spans the whole async stream
    without wrapping it in a try that could re-run the graph on a genuine error.
    """
    langsmith_config, langsmith_enabled = _get_langsmith_runtime(langsmith_service)
    langsmith_trace_id = None
    trace_cm = None
    run = None

    if langsmith_enabled and langsmith_config.is_enabled:
        try:
            from langsmith import trace

            queued_oldest_ts = state.get("_queued_oldest_ts")
            queued_oldest_suffix = _queued_oldest_ts_for_trace_name(queued_oldest_ts)
            trace_name = (
                (
                    f"gupshup-fashion-bot-conversation-queued-processing-oldest-{queued_oldest_suffix}"
                    if queued_oldest_suffix
                    else "gupshup-fashion-bot-conversation-queued-processing"
                )
                if langsmith_service == "gupshup" and state.get("_queued_processing")
                else (
                    "gupshup-fashion-bot-conversation"
                    if langsmith_service == "gupshup"
                    else f"{langsmith_service}-streaming-conversation"
                )
            )
            trace_cm = trace(
                name=trace_name,
                run_type="chain",
                project_name=langsmith_config.project_name,
                metadata={
                    "trace_id": trace_id,
                    "user_identifier": user_identifier,
                    "client_id": trace_client_id or state.get("client_id"),
                    "service": f"{langsmith_service}-streaming",
                    "queued_processing": bool(state.get("_queued_processing")),
                    "queued_processing_count": int(state.get("_queued_processing_count") or 0),
                    "queued_oldest_ts": queued_oldest_ts,
                },
            )
            run = trace_cm.__enter__()
            if run is not None and hasattr(run, "id"):
                langsmith_trace_id = str(run.id)
            clean_input = str(input_message or "").strip()
            if not clean_input:
                msgs = state.get("messages") if isinstance(state, dict) else None
                if isinstance(msgs, list):
                    for msg in reversed(msgs):
                        content = getattr(msg, "content", None)
                        if content:
                            clean_input = str(content).strip()
                            if clean_input:
                                break
            _set_trace_io(run, inputs={"input": clean_input[:2000], "state": snapshot_state_for_trace(state)})
        except Exception as trace_err:
            logger.warning(f"LangSmith trace setup error for service={langsmith_service}: {trace_err}, streaming without tracing")
            if trace_cm is not None:
                try:
                    trace_cm.__exit__(None, None, None)
                except Exception:
                    pass
            trace_cm = None
            run = None

    full_parts: List[str] = []
    result: Dict[str, Any] = {}
    try:
        async for mode, payload in graph.astream(state, stream_mode=["custom", "values"]):
            if mode == "custom" and isinstance(payload, dict):
                etype = payload.get("type")
                if etype == "token":
                    content = str(payload.get("content") or "")
                    if content:
                        full_parts.append(content)
                        yield {"type": "token", "content": content}
                elif etype in _UI_PASSTHROUGH_EVENTS:
                    yield payload
            elif mode == "values" and isinstance(payload, dict):
                result = payload  # full state snapshot; last one wins == ainvoke result

        result = ensure_assistant_message_for_skip_final(state_before_invoke=state, result=result)
        if run is not None:
            reply_preview = ""
            if isinstance(result, dict):
                if result.get("customer_message"):
                    reply_preview = str(result.get("customer_message") or "")
                elif result.get("messages"):
                    last_msg = result["messages"][-1]
                    reply_preview = str(getattr(last_msg, "content", last_msg) or "")
            _set_trace_io(run, outputs={"output": reply_preview[:2000], "state": snapshot_state_for_trace(result)})
        yield {
            "type": "__final__",
            "result": result if isinstance(result, dict) else {},
            "full_response": "".join(full_parts),
            "langsmith_trace_id": langsmith_trace_id or trace_id,
        }
    finally:
        if trace_cm is not None:
            try:
                trace_cm.__exit__(None, None, None)
            except Exception:
                pass


async def stream_graph_response(
    state: Dict[str, Any],
    message: str,
    client_id: str,
    langsmith_service: Optional[str] = None,
    trace_graph_internally: bool = True,
) -> AsyncGenerator[Dict[str, Any], None]:
    """
    Adapter-facing stream shim for graph execution.

    Current behavior:
    1. Invokes the selected graph once (in executor) with current state.
    2. Extracts final reply text from graph result.
    3. Emits only runtime stream contract events: start -> token* -> end (or error).

    Notes:
    - Final-node behavior is graph/state driven (`_skip_final_answer`, `_streaming_enabled`).
    - No `temporary_response` or `partial_response` events are emitted.
    
    Args:
        state: Current conversation state
        message: User message to process
        client_id: Client ID for configuration
        
    Yields:
        Streaming events for channel adapters:
        - {"type": "start", "message": "..."}
        - {"type": "token", "content": "..."}
        - {"type": "end", "full_response": "...", "result": {...}}
        - {"type": "error", "message": "..."}
    """
    from fashion_bot.client_context import set_client_id
    from fashion_bot.monitoring.otel_metrics import set_request_client_id

    # Set client context
    set_client_id(client_id)
    set_request_client_id(client_id)
    
    # Variables to capture LangSmith trace ID
    langsmith_trace_id = None
    
    try:
        # Import graph components
        from fashion_bot.graph_context_meta import graph
        
        # Current approach: run full graph once, then stream extracted final reply text.
        
        # NOTE: Message is already added to state by the caller (websocket_chat.py)
        # Do NOT add it again here
        # state["messages"] = state.get("messages", []) + [HumanMessage(content=message)]
        
        # Signal that we want streaming (nodes can check this)
        state["_streaming_enabled"] = True
        
        # Get trace_id and user_identifier from state for LangSmith
        trace_id = state.get("trace_id", "unknown")
        phone_number = state.get("phone_number")
        user_identifier = phone_number if phone_number else state.get("session_id", "anonymous")
        
        # Run graph first, then stream final extracted response text.
        log_with_trace_id(state, "🚀 Running graph with streaming...", "debug")
        
        # Yield a starting event
        yield {"type": "start", "message": "Processing your request..."}
        
        effective_langsmith_service = (
            (langsmith_service or state.get("_langsmith_service") or state.get("langsmith_service") or "general")
            .strip()
            .lower()
        )

        # TRUE STREAMING: drive graph.astream(["custom","values"]). The skill node
        # streams prose via the LangGraph custom writer; `values` carries the full
        # final state. Tokens/products flow to the adapter as they arrive; the
        # terminal `__final__` sentinel carries the full state + trace id.
        result: Dict[str, Any] = {}
        full_response = ""
        langsmith_trace_id = None

        if trace_graph_internally:
            async for ev in _astream_graph_events(
                graph, state, trace_id, user_identifier,
                effective_langsmith_service, message, client_id,
            ):
                if ev.get("type") == "__final__":
                    result = ev.get("result") or {}
                    full_response = ev.get("full_response") or ""
                    langsmith_trace_id = ev.get("langsmith_trace_id")
                else:
                    yield ev  # token / products
        else:
            full_parts: List[str] = []
            async for mode, payload in graph.astream(state, stream_mode=["custom", "values"]):
                if mode == "custom" and isinstance(payload, dict):
                    etype = payload.get("type")
                    if etype == "token":
                        c = str(payload.get("content") or "")
                        if c:
                            full_parts.append(c)
                            yield {"type": "token", "content": c}
                    elif etype in _UI_PASSTHROUGH_EVENTS:
                        yield payload
                elif mode == "values" and isinstance(payload, dict):
                    result = payload
            result = ensure_assistant_message_for_skip_final(state_before_invoke=state, result=result)
            full_response = "".join(full_parts)
            langsmith_trace_id = str(state.get("_langsmith_trace_id") or state.get("trace_id") or "")

        # Store langsmith_trace_id in state for later use (e.g., database storage)
        state["_langsmith_trace_id"] = langsmith_trace_id
        if isinstance(result, dict):
            # Persist run id in graph result so channel handlers that update Redis
            # state from result snapshots keep the actual LangSmith run id.
            result["_langsmith_trace_id"] = langsmith_trace_id

        # Authoritative delivered/persisted reply = the node's leak-guarded
        # customer_message (== parse_agent_output.clean_prose). The live-streamed
        # token concat is best-effort UX only: StreamGuard splits prose at the
        # first delimiter but cannot match every fence-less/spacing variant the
        # parser strips, so the raw concat may transiently include a tracking blob
        # (or omit a ```-fenced span). It must NOT be the source of the sent/stored
        # text — gupshup sends full_response, webchat stores it. Baseline derived
        # full_response from this same clean customer_message, so this preserves it.
        clean_reply = ""
        if isinstance(result, dict):
            if result.get("customer_message"):
                clean_reply = str(result.get("customer_message") or "")
            elif result.get("messages"):
                final_message = result["messages"][-1]
                if isinstance(final_message, AIMessage):
                    clean_reply = final_message.content or ""
        full_response = clean_reply or full_response or "I apologize, but I couldn't generate a response."

        # Last-mile leak guard. This is the one point both channels share —
        # gupshup sends `full_response`, webchat stores it — so a prompt
        # placeholder that survived substitution, or a dummy phone the model
        # invented to fill one, is stripped here for both. Idempotent, so the
        # WhatsApp send path can re-apply it as a backstop.
        guarded = sanitize_outbound_text(full_response)
        if guarded.blocked:
            log_with_trace_id(
                state,
                f"🛡️ [outbound_guard] blocked {','.join(guarded.kinds)} in reply: "
                f"{[v.matched for v in guarded.violations]}",
                "warning",
            )
            report_error(
                "Outbound guard stripped leaked content from a customer reply",
                level="warning",
                trace_id=str(state.get("trace_id") or "unknown"),
                client_id=client_id,
                violations=[{"kind": v.kind, "matched": v.matched} for v in guarded.violations],
            )
            full_response = guarded.text
            if TOOL_CALL_DIRECTIVE in guarded.kinds:
                full_response = await enrich_after_tool_call_directive(
                    full_response, client_id, violations=guarded.violations
                )
            # Keep the persisted transcript identical to what the customer got.
            if isinstance(result, dict) and result.get("customer_message"):
                result["customer_message"] = full_response

        yield {
            "type": "end",
            "full_response": full_response,
            "result": result,
            "langsmith_trace_id": langsmith_trace_id,
        }
        
    except Exception as e:
        logger.error(f"Graph streaming error: {str(e)}")
        report_error(
            "Graph streaming error",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            trace_id=str((state or {}).get("trace_id") or "unknown"),
            client_id=client_id,
            service=str((state or {}).get("_langsmith_service") or (state or {}).get("langsmith_service") or "general"),
        )
        yield {
            "type": "error",
            "message": f"Error processing request: {str(e)}"
        }


async def stream_llm_response(
    llm,
    prompt: str,
    system_prompt: str = None
) -> AsyncGenerator[str, None]:
    """
    Simple LLM streaming without agents.
    Useful for final answer formatting or simple responses.
    
    Args:
        llm: LangChain LLM instance
        prompt: User prompt
        system_prompt: Optional system prompt
        
    Yields:
        Token strings as they're generated
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    
    messages = []
    if system_prompt:
        messages.append(SystemMessage(content=system_prompt))
    messages.append(HumanMessage(content=prompt))
    
    try:
        async for chunk in llm.astream(messages):
            if hasattr(chunk, "content") and chunk.content:
                yield chunk.content
    except Exception as e:
        logger.error(f"LLM streaming error: {str(e)}")
        report_error(
            "LLM streaming error",
            level="error",
            exc_info=(type(e), e, e.__traceback__),
            component="stream_llm_response",
        )
        yield f"Error: {str(e)}"
