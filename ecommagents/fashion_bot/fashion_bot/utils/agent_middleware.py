"""Custom LangChain ``AgentMiddleware`` hooks for ``create_agent`` graphs.

``ToolGroundingMiddleware`` auto-invokes a named tool (e.g. ``search_products``)
before the model's first turn and appends its result to the message history as a
synthetic tool call — an ``AIMessage`` carrying the tool_call plus a matching
``ToolMessage`` — so the agent starts each turn already *grounded* with results
instead of waiting for the model to decide to call the tool.

This mirrors the imperative ``_force_grounding_tool`` path in ``agent_utils`` but
as a declarative, per-agent middleware. It is opt-in via the ``auto_ground_tool``
argument of ``build_agent_graph`` / ``run_agent_graph`` and is currently enabled
for the recommendation and order-status agents (see ``graph_context_meta.py``).

Design notes (AGENTS.md):
- Fully async (``abefore_model``) and fail-open — a grounding failure logs a
  warning and proceeds ungrounded; the agent still has the tool to call itself.
- The instance holds read-only references to the per-request tool object and the
  conversation ``state`` (for trace logging only). It is constructed fresh per
  request in ``build_agent_graph`` (same lifecycle as ``_force_grounding_tool``)
  and NEVER mutates state — it only returns a ``{"messages": [...]}`` update that
  the agent's ``add_messages`` reducer appends. The tool object cannot be read
  from agent state (only ``messages`` live there), so constructor injection is
  required.
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fashion_bot.utils.utils import log_with_trace_id
from fashion_bot.utils.product_utils import format_product_tool_result

logger = logging.getLogger(__name__)

def _loads_observation(content: Any) -> Any:
    """Best-effort parse a tool result back to a dict/list (mirrors _parse_observation)."""
    if not isinstance(content, str):
        return content
    s = content.strip()
    if not s or s[0] not in "{[":
        return content
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return content


# Marker on the synthetic AIMessage so we never inject twice in one turn.
_AUTO_GROUNDED_FLAG = "_auto_grounded"
# Recent conversation passed to the search tool to sharpen Query Understanding.
_MAX_HISTORY_TURNS = 10
_MAX_CONTENT_CHARS = 300


def _message_text(content: Any) -> str:
    """Flatten a message ``content`` (str or list-of-blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


def _latest_human_text(messages: List[Any]) -> str:
    """Return the most recent user message text (the live query)."""
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            return _message_text(msg.content).strip()
    return ""


def _build_conversation_history(messages: List[Any]) -> List[Dict[str, str]]:
    """Convert recent Human/AI turns to ``[{"role", "content"}]`` for the search tool.

    Drops the trailing user turn (that's the live query, passed separately) and
    caps both the number of turns and per-message length to keep tokens bounded.
    """
    history: List[Dict[str, str]] = []
    for msg in messages:
        if isinstance(msg, HumanMessage):
            role = "user"
        elif isinstance(msg, AIMessage):
            role = "assistant"
        else:
            continue
        text = _message_text(msg.content).strip()
        if not text:
            continue
        history.append({"role": role, "content": text[:_MAX_CONTENT_CHARS]})
    if history and history[-1]["role"] == "user":
        history = history[:-1]  # the live query is sent as `query`, not history
    return history[-_MAX_HISTORY_TURNS:]


def _already_grounded(messages: List[Any]) -> bool:
    """True if a synthetic grounding AIMessage is already present this turn."""
    for msg in messages:
        if isinstance(msg, AIMessage) and (
            getattr(msg, "additional_kwargs", None) or {}
        ).get(_AUTO_GROUNDED_FLAG):
            return True
    return False


class ToolGroundingMiddleware(AgentMiddleware):
    """Auto-ground an agent by pre-calling a tool and injecting its result.

    Override ``abefore_model`` (rather than ``before_agent``) with an idempotency
    guard so the injection happens exactly once — before the first model call of
    the turn — and never repeats across the tool-calling loop's later model steps.

    Subclasses supply the tool arguments via ``_build_args``.
    """

    _CALL_ID_PREFIX = "grounding"
    _LOG_LABEL = "GROUNDING"

    def __init__(self, tool, state=None):
        super().__init__()
        self._tool = tool
        self._state = state

    @classmethod
    def for_tool(cls, tool_name: str, tools, state=None) -> "Optional[ToolGroundingMiddleware]":
        """Build a middleware bound to the tool named ``tool_name`` from ``tools``.

        Returns ``None`` (logging a warning) when the tool isn't available, so the
        graph is built without grounding rather than failing.
        """
        tool_map = {getattr(t, "name", None): t for t in (tools or [])}
        tool = tool_map.get(tool_name)
        if tool is None:
            log_with_trace_id(
                state,
                f"⚠️ auto_ground_tool '{tool_name}' not found in agent tools; skipping grounding",
                "warning",
            )
            return None
        return cls(tool, state)

    def _build_args(self, messages: List[Any]) -> Optional[Dict[str, Any]]:
        """Arguments for the grounding call, or ``None`` to skip grounding."""
        return {}

    async def abefore_model(self, state, runtime=None) -> Optional[Dict[str, Any]]:
        messages = list((state or {}).get("messages") or [])
        # Inject exactly once, before the first model call: only when the latest
        # message is the user's turn (after injection the tail is the ToolMessage,
        # so subsequent model steps in the loop fall through untouched).
        if not messages or not isinstance(messages[-1], HumanMessage):
            return None
        if _already_grounded(messages):
            return None
        args = self._build_args(messages)
        if args is None:
            return None

        try:
            raw = await self._tool.ainvoke(args)
        except Exception as exc:  # noqa: BLE001 - grounding must never break the turn
            log_with_trace_id(
                self._state,
                f"⚠️ grounding '{self._tool.name}' failed ({exc}); proceeding ungrounded",
                "warning",
            )
            return None

        formatted = format_product_tool_result(raw)
        if formatted is not None:
            content, artifact = formatted
        else:  # non-product tool result — pass through as JSON, keep raw artifact
            content = raw if isinstance(raw, str) else json.dumps(raw, default=str)
            artifact = raw

        call_id = f"{self._CALL_ID_PREFIX}_{uuid.uuid4().hex}"
        ai = AIMessage(
            content="",
            tool_calls=[
                {"name": self._tool.name, "args": args, "id": call_id, "type": "tool_call"}
            ],
            additional_kwargs={_AUTO_GROUNDED_FLAG: True},
        )
        tool_message = ToolMessage(
            content=content,
            tool_call_id=call_id,
            name=self._tool.name,
            artifact=artifact,
        )
        log_with_trace_id(
            self._state,
            f"🔎 [{self._LOG_LABEL}] auto-called '{self._tool.name}'; injected tool result",
        )
        return {"messages": [ai, tool_message]}


class SearchGroundingMiddleware(ToolGroundingMiddleware):
    """Ground the recommendation agent with fresh ``search_products`` results."""

    _CALL_ID_PREFIX = "search_grounding"
    _LOG_LABEL = "SEARCH GROUNDING"

    def _build_args(self, messages: List[Any]) -> Optional[Dict[str, Any]]:
        query = _latest_human_text(messages)
        if not query:
            return None
        return {
            "query": query,
            "conversation_history": _build_conversation_history(messages),
        }


class OrderGroundingMiddleware(ToolGroundingMiddleware):
    """Ground the order agent with the customer's real orders every turn.

    ``order_status`` reaches its LOGISTICS STATUS-BASED RESPONSE RULES from the
    prompt, and those rules quote a tracking link. Nothing forced the agent to
    read order data first, so it could apply them from a status it remembered out
    of conversation history — the Enamor incident of 2026-08-24, where "Please
    deliver this order" produced an "Out for Delivery" reply with an invented
    tracking link after a turn whose only tool call was ``annotate_order``.
    Pre-calling ``get_recent_orders`` puts the real orders (status, ETA,
    tracking_url) in front of the model before it answers.

    Deliberately NOT ``get_order_details``: that needs an order_id the model
    would have to guess on a cold turn. ``get_recent_orders`` needs only the
    phone, which it reads from conversation state, and it self-gates on the
    order-access verification rules — so it is safe to pre-call even when the
    customer has not identified themselves yet (it returns a ``needs_phone``
    result the agent can act on).
    """

    _CALL_ID_PREFIX = "order_grounding"
    _LOG_LABEL = "ORDER GROUNDING"

    def _build_args(self, messages: List[Any]) -> Optional[Dict[str, Any]]:
        # include_all_statuses=True matches what the order_status prompt tells the
        # agent to fetch ("recent orders across all statuses"). The tool's own
        # default is False — actionable orders only, which drops delivered,
        # cancelled and RTO — so grounding with the default would hand the model a
        # SHORTER list than it would have fetched itself and invite "no orders
        # found" on the single most common query there is: "where is my delivered
        # order?".
        #
        # include_eta is left at its default False: the ETA lookup costs an extra
        # courier call and the prompt asks for it only on delivery-timing
        # questions, which the model still judges for itself.
        return {"include_all_statuses": True}


# Grounding style is a property of the tool being pre-called, not of the caller,
# so the mapping lives here next to the classes rather than in build_agent_graph.
_GROUNDING_MIDDLEWARE_BY_TOOL: Dict[str, type] = {
    "search_products": SearchGroundingMiddleware,
    "get_recent_orders": OrderGroundingMiddleware,
}


def grounding_middleware_for(
    tool_name: str, tools, state=None
) -> Optional[ToolGroundingMiddleware]:
    """Build the grounding middleware appropriate for ``tool_name``.

    Falls back to the no-argument base for any tool without a registered style.
    """
    cls = _GROUNDING_MIDDLEWARE_BY_TOOL.get(tool_name, ToolGroundingMiddleware)
    return cls.for_tool(tool_name, tools, state)


class OrderAuthMiddleware(AgentMiddleware):
    """Apply order-access verification grants emitted by the order tools.

    AGENTS.md §2 keeps tools stateless, so a gated tool that verifies a customer
    cannot write the grant itself — it returns it under
    ``order_access.GRANT_RESULT_KEY`` in its own result. This middleware is the
    runtime layer that puts it on conversation state.

    It runs per tool call rather than once at the end of the turn for a reason:
    one turn routinely chains ``get_recent_orders`` → ``get_order_details`` →
    ``update_order_address``, and the tools all close over the same ``state``
    object, so applying the grant here makes it visible to the NEXT tool in the
    same turn. A once-per-turn harvest would leave that gap open.

    ``generic_skill_node`` then returns ``order_auth`` in its response dict so
    LangGraph and the state cache persist it across turns.

    The marker is stripped from the ToolMessage the model reads — it is runtime
    bookkeeping, not something the agent should reason about. Stripping is
    best-effort: if the content cannot be parsed back to a dict, the grant is
    still applied and only the (harmless) marker remains visible.
    """

    def __init__(self, state=None):
        super().__init__()
        self._state = state

    async def awrap_tool_call(self, request, handler):
        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result

        from fashion_bot.utils.order_access import GRANT_RESULT_KEY, apply_grant_update

        for payload in (result.artifact, _loads_observation(result.content)):
            if not isinstance(payload, dict) or GRANT_RESULT_KEY not in payload:
                continue
            apply_grant_update(self._state, payload.get(GRANT_RESULT_KEY))
            if payload is result.artifact:
                payload.pop(GRANT_RESULT_KEY, None)
            else:
                stripped = {k: v for k, v in payload.items() if k != GRANT_RESULT_KEY}
                try:
                    result.content = json.dumps(stripped, default=str)
                except (TypeError, ValueError):
                    pass  # leave content as-is; the grant is already applied
            break
        return result
