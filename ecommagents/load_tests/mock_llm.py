"""
Mock LLM for load testing.

Replaces all real LLM calls with deterministic dummy responses.
Implements LangChain BaseChatModel interface so it slots in transparently.
"""
import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from fashion_bot.env_loader import get_int

logger = logging.getLogger(__name__)

MOCK_DELAY_MS = get_int("MOCK_LLM_DELAY_MS", 50)

# Canned intent detection response — routes to greeting/continuity (simplest path, no tools)
_INTENT_RESPONSE = json.dumps({"p": "GS", "i": "continuity_agent"})

# Canned skill node response — plain text, no tool calls
_SKILL_RESPONSE = "Hello! I'm happy to help you. Is there anything specific you'd like to know?"

# Singleton holder
_singleton: Optional["MockChatModel"] = None


def _detect_caller(messages: List[BaseMessage]) -> str:
    """Inspect system messages to determine which graph node is calling."""
    for msg in messages:
        text = getattr(msg, "content", "") or ""
        if not isinstance(text, str):
            text = str(text)
        lower = text[:500].lower()
        # Intent detection node — system prompt contains routing instructions
        if "parent_intent" in lower or "route message" in lower or "detected_intents" in lower:
            return "intent"
        # Final answer node — system prompt contains formatting instructions
        if "draft reply" in lower or "whatsapp customer" in lower or "format the reply" in lower:
            return "final_answer"
    return "skill"


class MockChatModel(BaseChatModel):
    """
    Drop-in replacement for any LangChain ChatModel during load tests.

    - Returns canned responses based on which node is calling
    - Supports bind_tools() (returns self, never emits tool calls)
    - Adds configurable async delay to simulate real latency
    """

    delay_ms: int = MOCK_DELAY_MS

    class Config:
        arbitrary_types_allowed = True

    @property
    def _llm_type(self) -> str:
        return "mock-load-test"

    @classmethod
    def get_singleton(cls) -> "MockChatModel":
        global _singleton
        if _singleton is None:
            _singleton = cls(delay_ms=MOCK_DELAY_MS)
            logger.info(f"[LOAD_TEST] MockChatModel created (delay={MOCK_DELAY_MS}ms)")
        return _singleton

    def bind_tools(self, tools: Any = None, **kwargs: Any) -> "MockChatModel":
        """Return self — mock never emits tool calls so native loop breaks immediately."""
        return self

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Sync generation — used by LLMFactory.get_llm() path."""
        import time
        if self.delay_ms > 0:
            time.sleep(self.delay_ms / 1000.0)
        content = self._route_response(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Async generation — used by ainvoke() in all hot-path nodes."""
        if self.delay_ms > 0:
            await asyncio.sleep(self.delay_ms / 1000.0)
        content = self._route_response(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    def _route_response(self, messages: List[BaseMessage]) -> str:
        """Pick canned response based on which node is calling."""
        caller = _detect_caller(messages)
        if caller == "intent":
            return _INTENT_RESPONSE
        elif caller == "final_answer":
            # Pass through the last human message or a default
            for msg in reversed(messages):
                if hasattr(msg, "content") and msg.type == "human":
                    return str(msg.content)[:200]
            return _SKILL_RESPONSE
        else:
            return _SKILL_RESPONSE
