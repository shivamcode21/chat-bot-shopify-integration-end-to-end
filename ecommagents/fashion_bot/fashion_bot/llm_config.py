import os
from typing import Optional
from fashion_bot.utils.utils import log_with_trace_id
# LLM configuration - environment variables should be loaded by main entry point

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ── DEPRECATED MODULE-LEVEL `llm` SINGLETON ─────────────────────────────
# The previous `llm = LLMFactory.get_llm()` (no tool_name) singleton was
# removed because it pinned `caller="unknown"` on every llm.* metric
# emitted through it. Five importers (continuity_check_agent,
# final_answer_node, core/orchestrator, utils/shared_utils,
# history/conversation_resolution) were the major contributor to the
# "Message Count by Caller × Client" panel's unknown/unknown row on
# the LLM Call Monitoring dashboard. Each importer has been refactored
# to call LLMFactory.get_llm(tool_name="<call_site>", ...) per-use.
#
# `llm` is kept as None so any straggler import (e.g.
# fashion_bot/archived/nodes.py:7, not loaded in production) raises
# AttributeError on .invoke()/.ainvoke() rather than silently emitting
# untagged metrics.
#
# If you reach for this: don't. Call LLMFactory directly with a
# meaningful tool_name so your caller is visible in dashboards.
llm = None

# Load test mode: override with mock LLM. Preserved because some test
# harnesses still rely on importing `llm` from this module to swap in
# a MockChatModel; if that flag is ever flipped on outside test runs the
# untagged-singleton problem comes back, so this branch is the only
# place a non-None `llm` should ever live.
from fashion_bot.env_loader import get_bool as _get_bool
if _get_bool("LOAD_TEST_MODE", False):
    try:
        from load_tests.mock_llm import MockChatModel
        llm = MockChatModel.get_singleton()
    except ImportError:
        pass


# Multi-client support: ContextVar for client_id
from fashion_bot.client_context import set_client_id as set_context_client_id, get_client_id

def set_client_id_context(client_id: Optional[str]):
    """Set the client_id in context for tools to use.
    
    Args:
        client_id: Client ID to set in shared context
    """
    from fashion_bot.utils.utils import log_with_trace_id
    log_with_trace_id(None, f"🔧 set_client_id_context called with: {client_id}", "info")
    set_context_client_id(client_id)
    # Verify it was set
    verify = get_client_id()
    log_with_trace_id(None, f"🔍 Verified after set_client_id_context: {verify}", "info")

