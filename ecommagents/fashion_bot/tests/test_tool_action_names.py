"""Unit tests for utils.tool_action_names.

Covers the tool → UI action-label mapping and the idempotency contract of the
custom-stream-writer emit helper (AGENTS.md "Idempotent by Default"): re-scanning
a message list that grows across supersteps must never double-emit a tool event.
"""

from types import SimpleNamespace

import pytest

from fashion_bot.utils.tool_action_names import (
    DEFAULT_TOOL_ACTION,
    TOOL_ACTION_EVENT_TYPE,
    TOOL_ACTION_NAMES,
    build_tool_action_event,
    emit_tool_action_events,
    iter_new_tool_calls,
    resolve_tool_action_name,
)


def _ai(tool_calls):
    """A minimal AIMessage stand-in carrying ``.tool_calls``."""
    return SimpleNamespace(tool_calls=tool_calls)


def test_resolve_known_and_unknown_and_blank():
    assert resolve_tool_action_name("search_products") == "Searching for products"
    assert resolve_tool_action_name("get_order_details") == "Fetching your order"
    # whitespace tolerant
    assert resolve_tool_action_name("  add_to_cart ") == "Adding this to your cart"
    # unmapped / empty fall back to the generic label
    assert resolve_tool_action_name("totally_new_tool") == DEFAULT_TOOL_ACTION
    assert resolve_tool_action_name("") == DEFAULT_TOOL_ACTION
    assert resolve_tool_action_name(None) == DEFAULT_TOOL_ACTION


def test_build_event_shape():
    assert build_tool_action_event("get_cart") == {
        "type": TOOL_ACTION_EVENT_TYPE,
        "tool": "get_cart",
        "action": "Checking your cart",
    }


def test_iter_dedupes_against_announced_and_within_scan():
    m1 = _ai([{"id": "a", "name": "search_products", "args": {}}])
    m2 = _ai([{"id": "b", "name": "get_cart", "args": {}}])
    plain = SimpleNamespace(content="hello")  # no tool_calls

    # duplicate message + a no-tool message -> each call id once, in order
    assert iter_new_tool_calls([m1, m1, m2, plain], announced=set()) == [
        ("a", "search_products"),
        ("b", "get_cart"),
    ]
    # already-announced ids are skipped
    assert iter_new_tool_calls([m1, m2], announced={"a"}) == [("b", "get_cart")]


def test_iter_falls_back_to_name_when_id_missing():
    m = _ai([{"name": "show_cart", "args": {}}])
    assert iter_new_tool_calls([m], announced=set()) == [("show_cart", "show_cart")]


def test_emit_is_idempotent_across_growing_message_list():
    m1 = _ai([{"id": "a", "name": "search_products", "args": {}}])
    m2 = _ai([{"id": "b", "name": "get_cart", "args": {}}])
    emitted = []
    writer = emitted.append

    seen = emit_tool_action_events(writer, [m1], announced=None)
    assert seen == {"a"}
    assert emitted == [build_tool_action_event("search_products")]

    # message list has grown (m1 still present) — only the NEW call emits
    seen = emit_tool_action_events(writer, [m1, m2], announced=seen)
    assert seen == {"a", "b"}
    assert emitted == [
        build_tool_action_event("search_products"),
        build_tool_action_event("get_cart"),
    ]

    # re-running with the same inputs emits nothing further (idempotent)
    seen = emit_tool_action_events(writer, [m1, m2], announced=seen)
    assert len(emitted) == 2


def test_emit_noop_when_writer_none():
    m1 = _ai([{"id": "a", "name": "search_products", "args": {}}])
    # writer=None (non-streaming channel): no emit, announced returned unchanged
    assert emit_tool_action_events(None, [m1], announced={"x"}) == {"x"}


def test_mapping_is_immutable_and_ascii():
    # Shared across tenants/turns by reference -> must be read-only so it can't be
    # mutated at runtime, and every label must be clean ASCII (no smart quotes).
    with pytest.raises(TypeError):
        TOOL_ACTION_NAMES["new_tool"] = "x"  # type: ignore[index]
    assert all(ord(c) < 128 for label in TOOL_ACTION_NAMES.values() for c in label)


def test_discount_policy_feedback_tools_are_mapped():
    # Coverage for tools that previously fell back to the generic label.
    assert resolve_tool_action_name("get_discount_information") == "Checking available offers"
    assert resolve_tool_action_name("get_repeated_discount_message") == "Checking available offers"
    assert resolve_tool_action_name("get_sales_policy") == "Checking our policy"
    assert resolve_tool_action_name("get_policy_information") == "Checking our policy"
    assert resolve_tool_action_name("log_customer_feedback") == "Saving your feedback"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
