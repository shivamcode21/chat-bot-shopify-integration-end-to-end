"""
Regression tests for the removal of the untagged module-level `llm`
singleton in fashion_bot.llm_config.

Background
----------
fashion_bot/fashion_bot/llm_config.py previously defined::

    llm = LLMFactory.get_llm()  # ← no tool_name

Five importers (continuity_check_agent, final_answer_node fallback,
core/orchestrator AnalysisOrchestrator._get_llm, utils/shared_utils
_extract_order_id_with_llm, history/conversation_resolution loop
detection) imported that singleton directly. Because the
singleton's `set_llm_caller(None)` ran exactly once at module import
time (caller default = "unknown"), every llm.* metric emitted through
it was tagged with caller="unknown". This was the dominant contributor
to the "Message Count by Caller × Client" panel's unknown/unknown row
on the LLM Call Monitoring dashboard.

The fix: each importer now resolves the LLM per-call via
``LLMFactory.get_llm(tool_name=<call_site>, ...)`` and the module-level
``llm`` is permanently None (unless ``LOAD_TEST_MODE`` is set, where
it's swapped to a MockChatModel for legacy test compatibility).

These tests pin that state down so a future PR can't silently
re-introduce the singleton.
"""

import importlib

import pytest

import fashion_bot.llm_config as llm_config_module


# ── The shim itself must be inert in normal (non-load-test) runs ──────────


def test_module_level_llm_is_none():
    """
    The module-level ``fashion_bot.llm_config.llm`` must be None outside
    LOAD_TEST_MODE. Any straggler import that calls ``.invoke()`` on it
    will raise AttributeError loud and clear, rather than silently
    emitting untagged metrics like the old singleton did.
    """
    assert llm_config_module.llm is None


def test_llm_attribute_exists_for_load_test_mode_swap():
    """
    The ``llm`` attribute must still be defined at module level so the
    LOAD_TEST_MODE branch (which conditionally swaps in a
    MockChatModel) keeps working. Tests that ``hasattr`` only — value
    is checked separately above.
    """
    assert hasattr(llm_config_module, "llm")


# ── No active importer of the untagged shim ───────────────────────────────


def test_no_active_importer_of_untagged_shim():
    """
    Walks the production code tree and asserts that nothing imports
    ``llm`` from ``fashion_bot.llm_config`` (the comment references in
    the refactored sites and the archived module are explicitly
    excluded).

    If this test fails, a new import has been added — fix it by calling
    LLMFactory.get_llm(tool_name="<your_call_site>", ...) directly so
    your caller is visible in dashboards. Don't just add the file to
    the exception list below.
    """
    import re
    from pathlib import Path

    # Project root resolved from this test file (tests/ is a sibling of
    # the fashion_bot/ package directory inside the fashion_bot project
    # root).
    repo_root = Path(__file__).resolve().parents[1]
    pkg_root = repo_root / "fashion_bot"

    forbidden_pat = re.compile(r"^from\s+fashion_bot\.llm_config\s+import\s+llm")

    # Allow: the archived module is not loaded in production; comments
    # mentioning the historical import pattern are allowed too because
    # they document the refactor.
    allowed_files = {
        pkg_root / "archived" / "nodes.py",
    }

    offenders = []
    for py_file in pkg_root.rglob("*.py"):
        if py_file in allowed_files:
            continue
        if "__pycache__" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            stripped = line.lstrip()
            if forbidden_pat.match(stripped):
                offenders.append(f"{py_file.relative_to(repo_root)}: {stripped}")

    assert not offenders, (
        "Found new importer(s) of the deprecated untagged shim "
        "`from fashion_bot.llm_config import llm`. Replace with "
        "`LLMFactory.get_llm(tool_name=...)` so llm.* metrics are "
        "tagged correctly:\n  " + "\n  ".join(offenders)
    )


# ── Each refactored importer can be imported and references a real LLM ────
# These are smoke tests — they don't invoke any LLM, just confirm the
# refactored modules import cleanly post-shim-removal.


def test_continuity_check_agent_imports_cleanly():
    """The module was rewritten to import LLMFactory instead of the shim."""
    mod = importlib.import_module("fashion_bot.nodes.continuity_check_agent")
    # The shim import was at module top; LLMFactory must replace it.
    assert hasattr(mod, "LLMFactory"), (
        "continuity_check_agent must import LLMFactory at module level "
        "for the per-call resolution pattern to work"
    )


def test_final_answer_node_does_not_import_shim_at_module_load():
    """Imports the module and confirms no shim symbol leaks in."""
    mod = importlib.import_module("fashion_bot.nodes.final_answer_node")
    # The fallback paths used to assign `llm = <from shim>` at module
    # scope. The refactor moved those to inline LLMFactory calls.
    # Confirm `llm` is not a stale module-level attribute pointing at
    # the shim (it would be the literal None now if reassigned).
    module_llm = getattr(mod, "llm", None)
    assert module_llm is None, (
        f"final_answer_node has unexpected module-level `llm` = {module_llm}; "
        "the fallback path was supposed to be inline-only"
    )


def test_conversation_resolution_imports_cleanly():
    importlib.import_module("fashion_bot.history.conversation_resolution")


def test_shared_utils_imports_cleanly():
    importlib.import_module("fashion_bot.utils.shared_utils")
