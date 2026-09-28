"""
Helpers for consistent transcript persistence.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from langchain_core.messages import AIMessage

from fashion_bot.state_cache import timestamped_ai_message


def should_skip_final_answer(state: Optional[Dict[str, Any]]) -> bool:
    """Return True when graph routing is configured to bypass final_answer."""
    state = state or {}
    return bool(state.get("_streaming_enabled") or state.get("_skip_final_answer"))


def ensure_assistant_message_for_skip_final(
    *,
    state_before_invoke: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Ensure assistant reply is persisted to `result["messages"]` when final node is skipped.

    Behavior:
    - No-op when final_answer is not skipped.
    - No-op when no customer_message is present.
    - Appends a timestamped AIMessage(customer_message) only if not already present at tail.
    """
    safe_result: Dict[str, Any] = dict(result or {})
    if not should_skip_final_answer(state_before_invoke):
        return safe_result

    customer_message = str(safe_result.get("customer_message") or "").strip()
    if not customer_message:
        return safe_result

    base_messages = safe_result.get("messages")
    if base_messages is None:
        state_messages = (state_before_invoke or {}).get("messages", []) or []
        messages = list(state_messages)
    else:
        messages = list(base_messages or [])

    recent_tail = messages[-3:] if len(messages) > 3 else messages
    for msg in reversed(recent_tail):
        if isinstance(msg, AIMessage) and str(getattr(msg, "content", "")).strip() == customer_message:
            safe_result["messages"] = messages
            return safe_result

    assistant_msg = timestamped_ai_message(customer_message)
    additional = dict(getattr(assistant_msg, "additional_kwargs", {}) or {})
    additional["source"] = "runtime_skip_final_fallback"
    assistant_msg.additional_kwargs = additional
    messages.append(assistant_msg)
    safe_result["messages"] = messages
    return safe_result

