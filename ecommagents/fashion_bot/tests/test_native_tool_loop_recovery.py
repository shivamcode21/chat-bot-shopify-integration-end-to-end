"""Regression tests for the native tool-loop empty-response recovery.

Reproduces the production failure where the model invoked a silent
side-effect tool (``annotate_order``) and then emitted an EMPTY final
assistant message, causing the customer to receive a generic
"provide more details about your request" clarification instead of an
answer. See ``_invoke_native_tool_loop`` in ``generic_skill_node``.
"""

import pytest
from langchain_core.messages import AIMessage

from fashion_bot.nodes.generic_skill_node import _invoke_native_tool_loop


class _ScriptedBoundLLM:
    """Tool-bound LLM that returns a pre-scripted sequence of AIMessages."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def ainvoke(self, _messages):
        msg = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return msg


class _FakeLLM:
    """LLM stub: ``bind_tools`` yields the scripted tool loop; the bare
    instance handles the tool-free recovery call."""

    def __init__(self, bound_responses, recovery_content):
        self._bound = _ScriptedBoundLLM(bound_responses)
        self._recovery_content = recovery_content
        self.recovery_calls = 0

    def bind_tools(self, _tools, tool_choice=None):
        return self._bound

    async def ainvoke(self, _messages):
        # This path is only reached by the empty-response recovery branch,
        # which invokes the bare (unbound) llm.
        self.recovery_calls += 1
        return AIMessage(content=self._recovery_content)


class _FakeTool:
    def __init__(self, name, result):
        self.name = name
        self._result = result

    async def ainvoke(self, _args):
        return self._result

    def invoke(self, _args):
        return self._result


def _annotate_call():
    return AIMessage(
        content="",
        tool_calls=[{
            "name": "annotate_order",
            "args": {"order_id": "gv15718", "note": "explained 3-5 day ETA"},
            "id": "call_1",
            "type": "tool_call",
        }],
    )


@pytest.mark.asyncio
async def test_recovers_when_model_goes_empty_after_tool_call():
    """The bug: tool call -> empty final message. Recovery must synthesize a
    real reply instead of returning empty text."""
    llm = _FakeLLM(
        bound_responses=[
            _annotate_call(),            # iter 1: silent annotate_order, no text
            AIMessage(content=""),       # iter 2: empty, no tool calls -> loop ends
        ],
        recovery_content="Your order #gv15718 is not yet dispatched; ETA 3-5 days.",
    )
    tools = [_FakeTool("annotate_order", {"success": True})]

    final_text, steps, _msgs = await _invoke_native_tool_loop(
        llm=llm,
        tools=tools,
        system_messages=["system"],
        chat_history=[],
        user_input="When will it get dispatched?",
        max_iterations=5,
    )

    assert "gv15718" in final_text
    assert "dispatched" in final_text
    assert llm.recovery_calls == 1
    assert len(steps) == 1  # annotate_order ran


@pytest.mark.asyncio
async def test_no_recovery_when_reply_already_present():
    """A normal turn that produces text must not trigger the recovery call."""
    llm = _FakeLLM(
        bound_responses=[AIMessage(content="Here is your order update.")],
        recovery_content="SHOULD-NOT-BE-USED",
    )

    final_text, _steps, _msgs = await _invoke_native_tool_loop(
        llm=llm,
        tools=[],
        system_messages=["system"],
        chat_history=[],
        user_input="status?",
        max_iterations=5,
    )

    assert final_text == "Here is your order update."
    assert llm.recovery_calls == 0


@pytest.mark.asyncio
async def test_no_recovery_when_no_tools_were_called():
    """Empty response with zero tool calls is left empty (the outer caller
    owns that fallback); recovery only applies after tool calls."""
    llm = _FakeLLM(
        bound_responses=[AIMessage(content="")],
        recovery_content="SHOULD-NOT-BE-USED",
    )

    final_text, steps, _msgs = await _invoke_native_tool_loop(
        llm=llm,
        tools=[],
        system_messages=["system"],
        chat_history=[],
        user_input="hi",
        max_iterations=5,
    )

    assert final_text == ""
    assert steps == []
    assert llm.recovery_calls == 0
