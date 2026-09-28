from datetime import datetime as _real_datetime, timezone, timedelta
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from fashion_bot.graph_context_meta import (
    route_intent_to_final_or_end,
    route_data_collection_to_intent_or_final,
    route_after_limit_gate,
)
from fashion_bot.nodes import conversation_limit_gate as gate_mod
from fashion_bot.state_cache import timestamped_human_message


def test_route_intent_skips_final_when_streaming_enabled():
    state = {"_streaming_enabled": True}
    assert route_intent_to_final_or_end(state) == "end"


def test_data_collection_routes_end_for_customer_message_when_skip_final():
    state = {"_skip_final_answer": True, "type": "customer_message"}
    assert route_data_collection_to_intent_or_final(state) == "end"


def test_data_collection_routes_final_answer_when_no_skip():
    state = {"type": "customer_message"}
    assert route_data_collection_to_intent_or_final(state) == "final_answer"


# ==================== Conversation message-limit gate ====================


def test_limit_gate_router_bypasses_graph_when_reached():
    assert route_after_limit_gate({"conversation_limit_reached": True}) == "end"


def test_limit_gate_router_defaults_to_detect_intent():
    assert route_after_limit_gate({"conversation_limit_reached": False}) == "detect_intent"
    assert route_after_limit_gate({}) == "detect_intent"


def _run_gate(state, limit=30, message="LIMIT REACHED", reset_seconds=0.0, now=None):
    # reset_seconds defaults to 0.0 (time-based cooldown disabled) so existing
    # assertions about the conversation-boundary reset stay isolated from it.
    async def _go():
        with patch.object(gate_mod, "aget_max_user_messages", AsyncMock(return_value=limit)), \
             patch.object(gate_mod, "aget_conversation_limit_message", AsyncMock(return_value=message)), \
             patch.object(gate_mod, "aget_conversation_limit_reset_seconds", AsyncMock(return_value=reset_seconds)):
            if now is not None:
                with patch.object(gate_mod, "datetime") as dt:
                    dt.now.return_value = now
                    dt.fromisoformat = _real_datetime.fromisoformat
                    return await gate_mod.conversation_limit_gate(state)
            return await gate_mod.conversation_limit_gate(state)

    import asyncio
    return asyncio.get_event_loop().run_until_complete(_go())


def test_limit_gate_increments_and_allows_under_limit():
    state = {"client_id": "c1", "messages": [timestamped_human_message("hi")]}
    result = _run_gate(state, limit=30)
    assert result["user_message_count"] == 1
    assert result["conversation_limit_reached"] is False
    assert not result.get("customer_message")


def test_limit_gate_allows_exactly_at_limit():
    state = {"client_id": "c1", "user_message_count": 29,
             "messages": [timestamped_human_message("m30")]}
    result = _run_gate(state, limit=30)
    assert result["user_message_count"] == 30
    assert result["conversation_limit_reached"] is False


def test_limit_gate_blocks_over_limit_with_canned_reply():
    state = {"client_id": "c1", "user_message_count": 30,
             "messages": [timestamped_human_message("m31")]}
    result = _run_gate(state, limit=30, message="Please start a new chat")
    assert result["user_message_count"] == 31
    assert result["conversation_limit_reached"] is True
    assert result["customer_message"] == "Please start a new chat"
    assert isinstance(result["messages"][-1], AIMessage)
    assert result["messages"][-1].content == "Please start a new chat"


# ---- Time-based cooldown reset (anchored on first-reached) ----

_T0 = _real_datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)


def test_limit_gate_anchors_reached_at_on_first_breach():
    state = {"client_id": "c1", "user_message_count": 30,
             "messages": [timestamped_human_message("m31")]}
    result = _run_gate(state, limit=30, reset_seconds=3 * 3600, now=_T0)
    assert result["conversation_limit_reached"] is True
    assert result["conversation_limit_reached_at"] == _T0.isoformat()


def test_limit_gate_retry_within_cooldown_stays_blocked_and_keeps_anchor():
    # A retry 49 min into a 3h cooldown must NOT move the anchor (that is the
    # trap being fixed) and must stay blocked.
    anchor = _T0.isoformat()
    state = {"client_id": "c1", "user_message_count": 33,
             "conversation_limit_reached_at": anchor, "conversation_id": "conv1",
             "message_count_conversation_id": "conv1",
             "messages": [timestamped_human_message("retry")]}
    result = _run_gate(state, limit=30, reset_seconds=3 * 3600,
                       now=_T0 + timedelta(minutes=49))
    assert result["conversation_limit_reached"] is True
    # Anchor is neither moved nor re-emitted.
    assert "conversation_limit_reached_at" not in result


def test_limit_gate_cooldown_elapsed_resets_and_clears_anchor():
    anchor = _T0.isoformat()
    state = {"client_id": "c1", "user_message_count": 39,
             "conversation_limit_reached_at": anchor, "conversation_id": "conv1",
             "message_count_conversation_id": "conv1",
             "messages": [timestamped_human_message("later")]}
    result = _run_gate(state, limit=30, reset_seconds=3 * 3600,
                       now=_T0 + timedelta(hours=3, minutes=1))
    assert result["conversation_limit_reached"] is False
    assert result["user_message_count"] == 1
    assert result["conversation_limit_reached_at"] is None


def test_limit_gate_reset_hours_zero_disables_cooldown():
    anchor = _T0.isoformat()
    state = {"client_id": "c1", "user_message_count": 31,
             "conversation_limit_reached_at": anchor, "conversation_id": "conv1",
             "message_count_conversation_id": "conv1",
             "messages": [timestamped_human_message("way later")]}
    result = _run_gate(state, limit=30, reset_seconds=0.0,
                       now=_T0 + timedelta(hours=99))
    assert result["conversation_limit_reached"] is True


def test_limit_gate_new_conversation_clears_anchor():
    anchor = _T0.isoformat()
    state = {"client_id": "c1", "user_message_count": 33,
             "conversation_limit_reached_at": anchor, "conversation_id": "conv2",
             "message_count_conversation_id": "conv1",
             "messages": [timestamped_human_message("new conv")]}
    result = _run_gate(state, limit=30, reset_seconds=3 * 3600, now=_T0 + timedelta(minutes=10))
    assert result["conversation_limit_reached"] is False
    assert result["user_message_count"] == 1
    assert result["message_count_conversation_id"] == "conv2"
    assert result["conversation_limit_reached_at"] is None


def test_limit_gate_streams_reply_token_when_streaming_enabled():
    # Web chat renders the bubble from live token frames, so the gate must push
    # its canned reply through the LangGraph writer when streaming is enabled.
    state = {"client_id": "c1", "user_message_count": 30, "_streaming_enabled": True,
             "messages": [timestamped_human_message("m31")]}
    writer = Mock()
    with patch("langgraph.config.get_stream_writer", return_value=writer):
        result = _run_gate(state, limit=30, message="Please start a new chat")
    assert result["conversation_limit_reached"] is True
    writer.assert_called_once_with({"type": "token", "content": "Please start a new chat"})


def test_limit_gate_does_not_stream_when_streaming_disabled():
    # Non-streaming channels (gupshup via ainvoke) deliver via full_response; the
    # gate must not attempt to acquire/use the writer there.
    state = {"client_id": "c1", "user_message_count": 30,
             "messages": [timestamped_human_message("m31")]}
    writer = Mock()
    with patch("langgraph.config.get_stream_writer", return_value=writer):
        result = _run_gate(state, limit=30, message="Please start a new chat")
    assert result["conversation_limit_reached"] is True
    writer.assert_not_called()


def test_limit_gate_streaming_failure_does_not_break_gate():
    # A writer error must not prevent the gate from returning the canned reply.
    state = {"client_id": "c1", "user_message_count": 30, "_streaming_enabled": True,
             "messages": [timestamped_human_message("m31")]}
    writer = Mock(side_effect=RuntimeError("boom"))
    with patch("langgraph.config.get_stream_writer", return_value=writer):
        result = _run_gate(state, limit=30, message="Please start a new chat")
    assert result["conversation_limit_reached"] is True
    assert result["customer_message"] == "Please start a new chat"


def test_limit_gate_disabled_when_limit_non_positive():
    state = {"client_id": "c1", "user_message_count": 999,
             "messages": [timestamped_human_message("x")]}
    result = _run_gate(state, limit=0)
    assert result["conversation_limit_reached"] is False
    assert result["user_message_count"] == 1000


def test_limit_gate_ignores_non_user_turns():
    # Tail is an AIMessage (e.g. injected system/order event) — the counter is
    # not returned, so LangGraph leaves the existing value untouched.
    state = {"client_id": "c1", "user_message_count": 5,
             "messages": [timestamped_human_message("hi"), AIMessage(content="event")]}
    result = _run_gate(state, limit=30)
    assert "user_message_count" not in result
    assert result["conversation_limit_reached"] is False


def test_limit_gate_returns_partial_update_only():
    # Under the limit, within the same conversation, the gate must touch nothing
    # but its two bookkeeping keys, so the existing flow downstream is unchanged.
    state = {"client_id": "c1", "conversation_id": "conv-1",
             "message_count_conversation_id": "conv-1",
             "messages": [timestamped_human_message("hi")]}
    result = _run_gate(state, limit=30)
    assert set(result.keys()) == {"user_message_count", "conversation_limit_reached"}


def test_limit_gate_resets_count_on_new_conversation():
    # A previously-capped user starts a NEW conversation (new conversation_id):
    # the counter restarts at 1 and they are no longer gated.
    state = {"client_id": "c1", "user_message_count": 45,
             "conversation_id": "conv-2",
             "message_count_conversation_id": "conv-1",
             "messages": [timestamped_human_message("fresh start")]}
    result = _run_gate(state, limit=30)
    assert result["user_message_count"] == 1
    assert result["conversation_limit_reached"] is False
    assert result["message_count_conversation_id"] == "conv-2"


def test_limit_gate_keeps_counting_within_same_conversation():
    # Same conversation_id -> no reset, counter advances and stays gated.
    state = {"client_id": "c1", "user_message_count": 30,
             "conversation_id": "conv-1",
             "message_count_conversation_id": "conv-1",
             "messages": [timestamped_human_message("m31")]}
    result = _run_gate(state, limit=30)
    assert result["user_message_count"] == 31
    assert result["conversation_limit_reached"] is True
    assert "message_count_conversation_id" not in result  # unchanged, not re-emitted


def test_limit_gate_does_not_reset_when_conversation_id_missing():
    # No conversation_id on the turn -> cannot detect a boundary, keep counting.
    state = {"client_id": "c1", "user_message_count": 30,
             "messages": [timestamped_human_message("m31")]}
    result = _run_gate(state, limit=30)
    assert result["user_message_count"] == 31
    assert result["conversation_limit_reached"] is True
    assert "message_count_conversation_id" not in result
