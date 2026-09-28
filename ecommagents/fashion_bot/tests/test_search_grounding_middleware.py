"""
Unit tests for the search-grounding middleware and its shared formatter.

``SearchGroundingMiddleware`` (fashion_bot/utils/agent_middleware.py) auto-invokes a
named tool before the model's first turn and injects its result into the message
history as a synthetic tool call (AIMessage(tool_call) + ToolMessage), so the
recommendation agent starts each turn already grounded with product search results.

These tests use a fake in-memory tool (no live services / state fixtures) and the
real ``langchain_core`` message classes, exercising:
    - format_product_tool_result: the formatter shared with
      ProductObservationFormatterMiddleware (product / summary / repeated / text / None)
    - for_tool: tool resolution by name, fail-open when absent
    - abefore_model: synthetic pair construction (matching ids, sentinel, artifact),
      query + conversation_history derivation, idempotency guards, fail-open, and
      truthful injection of empty / non-product results
    - end-to-end compatibility with messages_to_intermediate_steps (carousel path)
"""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from fashion_bot.utils.product_utils import format_product_tool_result
from fashion_bot.utils.agent_middleware import SearchGroundingMiddleware, _AUTO_GROUNDED_FLAG
from fashion_bot.utils.agent_utils import messages_to_intermediate_steps


PRODUCT_RESULT = {
    "found": True,
    "products": [{"title": "Black Hoodie", "handle": "black-hoodie", "client_id": "x"}],
    "count": 1,
    "qu_query": "black hoodie",
    "follow_up": "What size are you looking for?",
}


class FakeTool:
    """Minimal stand-in for the bound ``search_products`` tool object."""

    name = "search_products"

    def __init__(self, ret=None, exc=None):
        self._ret = ret
        self._exc = exc
        self.last_args = None

    async def ainvoke(self, args):
        self.last_args = args
        if self._exc is not None:
            raise self._exc
        return self._ret


# ---------------------------------------------------------------------------
# Shared formatter
# ---------------------------------------------------------------------------
class TestFormatProductToolResult:
    def test_product_dict_returns_content_and_raw_artifact(self):
        content, artifact = format_product_tool_result(PRODUCT_RESULT)
        assert artifact is PRODUCT_RESULT          # raw kept for carousel extraction
        assert "Black Hoodie" in content
        assert "client_id" not in content          # internal fields stripped
        assert "What size" in content              # follow_up carried through

    def test_summary_prefixed(self):
        content, _ = format_product_tool_result(
            {"products": [{"title": "Tee", "handle": "t"}], "summary": "Top picks:"}
        )
        assert content.startswith("Top picks:\n\n")

    def test_tool_note_is_passed_through(self):
        # The tool's own note is surfaced to the model verbatim (model phrases the reply);
        # no code-side instruction prefix is injected.
        content, _ = format_product_tool_result(
            {"products": [{"title": "Tee", "handle": "t"}],
             "all_products_repeated": True,
             "note": "No new products were found beyond those already shown."}
        )
        assert content.startswith("No new products were found beyond those already shown.")

    def test_no_forced_repeated_prefix(self):
        # all_products_repeated alone (no note) must not inject any code-side prefix.
        content, _ = format_product_tool_result(
            {"products": [{"title": "Tee", "handle": "t"}], "all_products_repeated": True}
        )
        assert "IMPORTANT: All search results" not in content
        assert "These are all the" not in content
        assert "Tee" in content

    def test_text_dict(self):
        assert format_product_tool_result({"text": "hello"}) == ("hello", {"text": "hello"})

    def test_non_product_returns_none(self):
        assert format_product_tool_result({"found": False, "products": [], "count": 0}) is None
        assert format_product_tool_result({"foo": "bar"}) is None
        assert format_product_tool_result("not a dict") is None
        assert format_product_tool_result(None) is None


# ---------------------------------------------------------------------------
# Middleware construction
# ---------------------------------------------------------------------------
class TestForTool:
    def test_resolves_tool_by_name(self):
        mw = SearchGroundingMiddleware.for_tool("search_products", [FakeTool(PRODUCT_RESULT)])
        assert isinstance(mw, SearchGroundingMiddleware)

    def test_missing_tool_returns_none(self):
        assert SearchGroundingMiddleware.for_tool("nope", [FakeTool(PRODUCT_RESULT)]) is None
        assert SearchGroundingMiddleware.for_tool("search_products", []) is None


# ---------------------------------------------------------------------------
# abefore_model
# ---------------------------------------------------------------------------
class TestAbeforeModel:
    async def test_injects_synthetic_tool_call_pair(self):
        tool = FakeTool(PRODUCT_RESULT)
        mw = SearchGroundingMiddleware(tool, state={})
        history = [
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
            HumanMessage(content="show me black hoodies"),
        ]
        out = await mw.abefore_model({"messages": history})
        ai, tm = out["messages"]

        assert isinstance(ai, AIMessage) and isinstance(tm, ToolMessage)
        assert ai.tool_calls[0]["name"] == "search_products"
        assert ai.tool_calls[0]["id"] == tm.tool_call_id          # valid pair / reassembly
        assert ai.additional_kwargs[_AUTO_GROUNDED_FLAG] is True
        assert tm.name == "search_products"
        assert tm.artifact is PRODUCT_RESULT                       # raw dict for carousel
        assert "Black Hoodie" in tm.content

    async def test_query_and_history_derivation(self):
        tool = FakeTool(PRODUCT_RESULT)
        mw = SearchGroundingMiddleware(tool, state={})
        history = [
            HumanMessage(content="hi"),
            AIMessage(content="hello"),
            HumanMessage(content="show me black hoodies"),
        ]
        await mw.abefore_model({"messages": history})
        assert tool.last_args["query"] == "show me black hoodies"  # latest human turn
        # conversation_history excludes the trailing live query
        assert tool.last_args["conversation_history"] == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]

    async def test_idempotent_when_not_user_turn(self):
        mw = SearchGroundingMiddleware(FakeTool(PRODUCT_RESULT), state={})
        # last message is a ToolMessage (mid tool-loop) -> no injection
        msgs = [HumanMessage(content="q"), AIMessage(content="", tool_calls=[]),
                ToolMessage(content="{}", tool_call_id="x", name="t")]
        assert await mw.abefore_model({"messages": msgs}) is None

    async def test_idempotent_when_sentinel_present(self):
        mw = SearchGroundingMiddleware(FakeTool(PRODUCT_RESULT), state={})
        grounded = AIMessage(content="", tool_calls=[], additional_kwargs={_AUTO_GROUNDED_FLAG: True})
        assert await mw.abefore_model({"messages": [grounded, HumanMessage(content="again")]}) is None

    async def test_empty_or_missing_query_is_noop(self):
        mw = SearchGroundingMiddleware(FakeTool(PRODUCT_RESULT), state={})
        assert await mw.abefore_model({"messages": [HumanMessage(content="   ")]}) is None
        assert await mw.abefore_model({"messages": []}) is None

    async def test_fail_open_on_tool_error(self):
        mw = SearchGroundingMiddleware(FakeTool(exc=RuntimeError("boom")), state={})
        # grounding must never break the turn: returns None, agent proceeds ungrounded
        assert await mw.abefore_model({"messages": [HumanMessage(content="q")]}) is None

    async def test_empty_result_injected_truthfully(self):
        empty = {"found": False, "products": [], "count": 0}
        mw = SearchGroundingMiddleware(FakeTool(empty), state={})
        out = await mw.abefore_model({"messages": [HumanMessage(content="xyz")]})
        _, tm = out["messages"]
        # non-product result still injected (as JSON) so the model knows nothing matched
        assert json.loads(tm.content)["found"] is False
        assert tm.artifact == empty


# ---------------------------------------------------------------------------
# Downstream compatibility
# ---------------------------------------------------------------------------
class TestIntermediateStepsCompatibility:
    async def test_injected_pair_surfaces_as_intermediate_step(self):
        """The synthetic pair must reassemble into an (action, observation) step
        whose observation is the raw dict — what carousel extraction reads."""
        mw = SearchGroundingMiddleware(FakeTool(PRODUCT_RESULT), state={})
        out = await mw.abefore_model({"messages": [HumanMessage(content="hoodies")]})
        full = out["messages"] + [AIMessage(content="Here are some options...")]

        steps = messages_to_intermediate_steps(full)
        assert len(steps) == 1
        action, observation = steps[0]
        assert action.tool == "search_products"
        assert observation is PRODUCT_RESULT       # prefers ToolMessage.artifact
