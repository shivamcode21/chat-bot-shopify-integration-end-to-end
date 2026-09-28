"""
Conversation message-limit gate.

Graph entry node that caps the number of user turns per conversation. When a
client's configured limit is exceeded, it short-circuits the graph with a canned
reply so no intent detection / skill node / LLM call runs for that turn.

Counting is per conversational turn: one graph invocation == one user turn. This
matches how rapid-fire messages are merged upstream into a single ``HumanMessage``
(see ``core/gupshup_runtime_support.build_merged_payload``). The count lives in
``state["user_message_count"]`` and persists across turns via the state cache
(``state_cache.serialize_state`` persists every SupportState field generically).

The counter resets on either of two independent triggers:

1. **New conversation** — both channels resolve ``state["conversation_id"]``
   before invoking the graph, minting a fresh id after a 90-minute inactivity
   gap (``history/postgres_conversations``). The gate tracks the id it counts
   under in ``state["message_count_conversation_id"]`` and restarts from zero
   whenever the turn's ``conversation_id`` differs.

2. **Cooldown elapsed** — a fixed window (``conversation_limit_reset_hours``,
   default 2h) measured from when the cap was FIRST reached, tracked in
   ``state["conversation_limit_reached_at"]``. This exists because trigger (1) is
   keyed on *last activity*: every retry while blocked — and the bot's own stored
   canned reply — pushes the 90-minute inactivity boundary forward, so a customer
   who keeps messaging never starts a new conversation and would stay blocked
   indefinitely. Anchoring the cooldown on *first-reached* lifts the block after a
   fixed wait regardless of how often they retry.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from langchain_core.messages import HumanMessage

from fashion_bot.schema import SupportState
from fashion_bot.state_cache import timestamped_ai_message
from fashion_bot.config_manager import (
    aget_max_user_messages,
    aget_conversation_limit_message,
    aget_conversation_limit_reset_seconds,
)

logger = logging.getLogger("context_graph")


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp back to an aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_user_turn(state: SupportState) -> bool:
    """True when this invocation carries a fresh user message at the tail.

    Guards the counter against non-user graph runs (e.g. injected system/order
    events) that would otherwise inflate the per-conversation turn count.
    """
    messages = state.get("messages") or []
    return bool(messages) and isinstance(messages[-1], HumanMessage)


def _stream_reply_token(state: SupportState, reply: str) -> None:
    """Emit the canned limit reply through the LangGraph custom stream writer.

    Web chat paints the bot bubble from live ``{"type":"token"}`` stream frames
    (see ``streaming_service._astream_graph_events`` /
    ``websocket_chat._consume_websocket_runtime_stream_events``). Those frames are
    only produced when a node pushes prose through the writer — the skill node
    does this via ``get_stream_writer()`` (``generic_skill_node``). Because this
    gate short-circuits straight to END without running the skill node, no tokens
    would otherwise stream, so the reply is generated and persisted but the web
    widget renders an empty turn ("agent stopped replying").

    Emitting the whole reply as a single token here makes the streaming path
    deliver it just like a normal reply. The writer is absent on non-streaming
    callers (WhatsApp/gupshup via ``graph.ainvoke``), so this is a no-op there and
    those channels keep delivering via ``full_response``. Best-effort: any failure
    to stream must not break the gate — the reply is still in ``customer_message``.
    """
    if not state.get("_streaming_enabled") or not reply:
        return
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:
        writer = None
    if writer is None:
        return
    try:
        writer({"type": "token", "content": reply})
    except Exception:
        pass


async def conversation_limit_gate(state: SupportState) -> Dict[str, Any]:
    """Increment the per-conversation user-turn counter and enforce the cap.

    Returns a partial state update (LangGraph merges only the returned keys —
    the same convention as the other nodes), so the under-limit path touches
    nothing but the two bookkeeping fields and the existing flow is unchanged.

    On a genuine user turn: increments ``user_message_count`` (restarting from
    zero when a new conversation begins) and, when the client's limit is exceeded,
    sets ``customer_message`` + appends the assistant ``AIMessage`` and flags
    ``conversation_limit_reached`` so the router bypasses the agent graph.
    Otherwise it is a pass-through to intent detection.
    """
    trace_id = state.get("trace_id")
    client_id = state.get("client_id")

    # Only count genuine user turns; pass anything else through untouched.
    if not _is_user_turn(state):
        return {"conversation_limit_reached": False}

    now = datetime.now(timezone.utc)

    # The counter resets on either of two independent triggers:
    #
    # 1. New conversation — a change in conversation_id (minted after a 90-minute
    #    inactivity gap). When conversation_id is absent we cannot detect a
    #    boundary, so we keep counting (fail toward enforcing the cap).
    #
    # 2. Cooldown elapsed — a fixed window (conversation_limit_reset_hours)
    #    measured from when the cap was FIRST reached. This is the key fix for the
    #    "limit never lifts" trap: the new-conversation boundary is keyed on LAST
    #    activity, so a blocked customer who keeps retrying (each retry, plus the
    #    bot's own stored canned reply, advances that boundary) never lets a new
    #    conversation start and never resets. Anchoring on first-reached makes the
    #    block lift after a fixed wait regardless of retries.
    current_conv_id = state.get("conversation_id")
    counted_conv_id = state.get("message_count_conversation_id")
    new_conversation = bool(current_conv_id) and current_conv_id != counted_conv_id

    reset_seconds = await aget_conversation_limit_reset_seconds(client_id)
    reached_at = _parse_iso(state.get("conversation_limit_reached_at"))
    cooldown_elapsed = bool(
        reset_seconds > 0
        and reached_at is not None
        and (now - reached_at).total_seconds() >= reset_seconds
    )

    reset = new_conversation or cooldown_elapsed
    prev_count = 0 if reset else (state.get("user_message_count") or 0)
    count = prev_count + 1

    limit = await aget_max_user_messages(client_id)

    # Keep the under-limit update minimal: only re-emit the tracked conversation
    # id when it changes, and only clear the cooldown anchor when we actually
    # reset (so a fresh block cycle can be timed from its own first-reached).
    conv_update: Dict[str, Any] = {}
    if new_conversation:
        conv_update["message_count_conversation_id"] = current_conv_id
    if reset and state.get("conversation_limit_reached_at"):
        conv_update["conversation_limit_reached_at"] = None

    if limit > 0 and count > limit:
        reply = await aget_conversation_limit_message(client_id)
        # Materialize the assistant reply the same way skill nodes do in
        # skip-final mode, so every channel's reply extraction and the state
        # cache persist it without relying on the final_answer node. `messages`
        # has no reducer (overwrite semantics), so we return the full list.
        messages = list(state.get("messages") or []) + [timestamped_ai_message(reply)]
        # Stream the reply to token-based channels (web chat) so the widget
        # renders it; a no-op on non-streaming channels (gupshup).
        _stream_reply_token(state, reply)
        # Anchor the cooldown on the FIRST turn the cap is exceeded and never move
        # it while the block persists, so retries can't push the reset out. (A
        # cooldown-elapsed turn resets count to 1, which never exceeds a positive
        # limit, so this branch is only reached with the anchor absent/unset.)
        limit_update: Dict[str, Any] = dict(conv_update)
        if not reached_at:
            limit_update["conversation_limit_reached_at"] = now.isoformat()
        logger.info(
            "[CONTEXT_GRAPH] 🛑 Conversation message limit reached — bypassing agent graph",
            extra={
                "trace_id": trace_id,
                "client_id": client_id,
                "user_message_count": count,
                "limit": limit,
                "conversation_id": current_conv_id,
                "reset_seconds": reset_seconds,
            },
        )
        return {
            "user_message_count": count,
            "conversation_limit_reached": True,
            "customer_message": reply,
            "messages": messages,
            **limit_update,
        }

    return {
        "user_message_count": count,
        "conversation_limit_reached": False,
        **conv_update,
    }
