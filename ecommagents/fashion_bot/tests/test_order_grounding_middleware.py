"""Unit tests for ``OrderGroundingMiddleware`` and the grounding dispatcher.

``order_status`` had no forced grounding, so the prompt's LOGISTICS STATUS-BASED
RESPONSE RULES could fire off a status the model read in conversation history
with no order data in the turn — the Enamor incident of 2026-08-24. These tests
cover the pre-call that closes that gap, and pin the pre-existing
``search_products`` grounding behaviour so the shared base class refactor cannot
change it silently.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fashion_bot.utils.agent_middleware import (
    OrderGroundingMiddleware,
    SearchGroundingMiddleware,
    ToolGroundingMiddleware,
    grounding_middleware_for,
)


class FakeTool:
    """Minimal stand-in for a LangChain tool: records the args it was called with."""

    def __init__(self, name, result=None, raises=None):
        self.name = name
        self._result = result if result is not None else {"orders": []}
        self._raises = raises
        self.calls = []

    async def ainvoke(self, args):
        self.calls.append(args)
        if self._raises is not None:
            raise self._raises
        return self._result


ORDER_RESULT = {
    "success": True,
    "orders": [{"order_id": "ENAMOR-234676", "tracking_url": "https://shiprocket.co/tracking/34791189074634"}],
}


def _user_turn():
    return {"messages": [HumanMessage(content="Please deliver this order")]}


class TestGroundingDispatcher:
    def test_search_products_gets_the_search_style(self):
        tools = [FakeTool("search_products")]
        assert isinstance(grounding_middleware_for("search_products", tools), SearchGroundingMiddleware)

    def test_get_recent_orders_gets_the_order_style(self):
        tools = [FakeTool("get_recent_orders")]
        assert isinstance(grounding_middleware_for("get_recent_orders", tools), OrderGroundingMiddleware)

    def test_unregistered_tool_falls_back_to_the_no_arg_base(self):
        tools = [FakeTool("some_other_tool")]
        middleware = grounding_middleware_for("some_other_tool", tools)
        assert type(middleware) is ToolGroundingMiddleware

    def test_missing_tool_returns_none_instead_of_raising(self):
        # A misconfigured auto_ground_tool must build a graph without grounding,
        # never fail the turn.
        assert grounding_middleware_for("get_recent_orders", [FakeTool("something_else")]) is None
        assert grounding_middleware_for("get_recent_orders", []) is None
        assert grounding_middleware_for("get_recent_orders", None) is None


class TestOrderGroundingMiddleware:
    async def test_pre_calls_the_tool_and_injects_the_result(self):
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        update = await OrderGroundingMiddleware(tool).abefore_model(_user_turn())

        assert len(tool.calls) == 1
        messages = update["messages"]
        assert isinstance(messages[0], AIMessage)
        assert messages[0].tool_calls[0]["name"] == "get_recent_orders"
        assert isinstance(messages[1], ToolMessage)
        # The raw dict is preserved on .artifact so messages_to_intermediate_steps
        # (and therefore the tracking-link attestation) sees structured data.
        assert messages[1].artifact == ORDER_RESULT

    async def test_grounds_across_all_statuses(self):
        # The tool's own default is actionable-only, which hides delivered /
        # cancelled / RTO orders — the wrong scope for "where is my order?".
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        await OrderGroundingMiddleware(tool).abefore_model(_user_turn())
        assert tool.calls == [{"include_all_statuses": True}]

    async def test_does_not_request_the_eta_lookup(self):
        # include_eta costs an extra courier call; the prompt asks for it only on
        # delivery-timing questions, which the model still judges for itself.
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        await OrderGroundingMiddleware(tool).abefore_model(_user_turn())
        assert tool.calls[0].get("include_eta") is None

    async def test_injects_only_once_per_turn(self):
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        middleware = OrderGroundingMiddleware(tool)
        first = await middleware.abefore_model(_user_turn())

        # Second model step of the same turn: the tail is now the ToolMessage.
        state = {"messages": [HumanMessage(content="hi")] + first["messages"]}
        assert await middleware.abefore_model(state) is None
        assert len(tool.calls) == 1

    async def test_skips_when_the_turn_does_not_end_on_the_user(self):
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        state = {"messages": [HumanMessage(content="hi"), AIMessage(content="hello")]}
        assert await OrderGroundingMiddleware(tool).abefore_model(state) is None
        assert tool.calls == []

    async def test_empty_state_is_safe(self):
        tool = FakeTool("get_recent_orders", ORDER_RESULT)
        assert await OrderGroundingMiddleware(tool).abefore_model({}) is None
        assert await OrderGroundingMiddleware(tool).abefore_model(None) is None

    async def test_tool_failure_proceeds_ungrounded(self):
        # Grounding is best-effort: a Shopify outage must not break the turn.
        tool = FakeTool("get_recent_orders", raises=RuntimeError("shopify down"))
        assert await OrderGroundingMiddleware(tool).abefore_model(_user_turn()) is None

    async def test_needs_phone_result_is_injected_not_swallowed(self):
        # A cold web-chat turn: the agent should see the tool's own guidance and
        # ask for the phone, exactly as if it had made the call itself.
        result = {"success": False, "needs_phone": True, "orders": [],
                  "message": "No phone number on file for this customer."}
        tool = FakeTool("get_recent_orders", result)
        update = await OrderGroundingMiddleware(tool).abefore_model(_user_turn())
        assert update["messages"][1].artifact == result


class TestSearchGroundingUnchanged:
    async def test_still_sends_query_and_history(self):
        tool = FakeTool("search_products", {"products": []})
        state = {
            "messages": [
                HumanMessage(content="show me bras"),
                AIMessage(content="here are some"),
                HumanMessage(content="in white"),
            ]
        }
        await SearchGroundingMiddleware(tool).abefore_model(state)

        args = tool.calls[0]
        assert args["query"] == "in white"
        assert args["conversation_history"] == [
            {"role": "user", "content": "show me bras"},
            {"role": "assistant", "content": "here are some"},
        ]

    async def test_blank_query_skips_grounding(self):
        tool = FakeTool("search_products", {"products": []})
        state = {"messages": [HumanMessage(content="   ")]}
        assert await SearchGroundingMiddleware(tool).abefore_model(state) is None
        assert tool.calls == []
