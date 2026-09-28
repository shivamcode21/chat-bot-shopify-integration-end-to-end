"""
Tests for the llm_caller tag propagation bug fixed by wrapping ainvoke()
calls in the `llm_caller(...)` context manager.

Background
----------
`LLMFactory.get_llm(tool_name=X)` sets the `caller` ContextVar via
`set_llm_caller(X)` *at call time*. The OTel LLM callback handler reads
that ContextVar *at call time* of `ainvoke()` to label `llm_calls_total`,
`llm_tokens_*`, etc.

Lazy-init classes (like `ProductAttributeExtractor` and
`ProductImageOCRExtractor`) cache the LLM instance after the first
`LLMFactory.get_llm(tool_name=X)` call. That means `set_llm_caller(X)`
runs **once**, in whatever asyncio Context happened to touch the property
first. Subsequent `ainvoke()` calls run in *different* Contexts — fresh
task trees from new webhooks, cron iterations, etc. — and those Contexts
inherit the ContextVar's default value (`"unknown"`) instead of `X`.

The result was Grafana's *Message Count by Caller × Client* panel showing
a huge `caller=unknown / client_id=unknown` row dominated by the
ingestion extractors, even though both call `LLMFactory.get_llm(
tool_name="product_ingestion")` at instantiation.

Fix: wrap each `ainvoke()` call site in `with llm_caller(tool_name):`
so the tag is set in the *call-time* context. This is what the tests
below lock down.
"""

import asyncio
import contextvars

import pytest

from fashion_bot.monitoring.otel_metrics import (
    _llm_caller_var,
    get_llm_caller,
    llm_caller,
    set_llm_caller,
)


@pytest.fixture(autouse=True)
def _reset_llm_caller_between_tests():
    """
    Reset the caller ContextVar before each test.

    The caller value is held in a module-level ContextVar; once any test
    in this file calls ``set_llm_caller(...)`` without a corresponding
    reset, the value persists for the rest of the pytest session and
    poisons later tests' assertions about the default state. This
    fixture pins it back to the default before every test runs.
    """
    token = _llm_caller_var.set("unknown")
    try:
        yield
    finally:
        try:
            _llm_caller_var.reset(token)
        except (ValueError, RuntimeError):
            # If the test ran inside copy_context().run(...) the token is
            # from a different Context; benign on teardown.
            pass


# ── The bug: caller set at init time is invisible to later contexts ───────


def test_set_in_one_context_is_invisible_to_a_sibling_context():
    """
    Reproduces the lazy-init failure mode without needing a real LLM.

    set_llm_caller is called inside a child Context (simulating the
    lazy-init touch of an extractor's `self.llm` property from one
    webhook). A *sibling* child Context (simulating a later webhook
    invoking the same cached extractor) reads back the ContextVar — and
    sees the default, not the value the first sibling set.

    Without this property, the dashboards would attribute correctly.
    With this property, they don't — which is exactly why the wrapper
    fix is needed.
    """
    sibling_a_seen = {}
    sibling_b_seen = {}

    def _sibling_a():
        # Mimics "first access to ProductAttributeExtractor.llm" during
        # some webhook handler. Tags caller, then "ends".
        set_llm_caller("product_ingestion")
        sibling_a_seen["caller"] = get_llm_caller()

    def _sibling_b():
        # Mimics a *different* webhook later invoking ainvoke() on the
        # already-cached extractor instance. Reads caller — and sees the
        # default because the ContextVar was set in a sibling Context.
        sibling_b_seen["caller"] = get_llm_caller()

    contextvars.copy_context().run(_sibling_a)
    contextvars.copy_context().run(_sibling_b)

    assert sibling_a_seen["caller"] == "product_ingestion"
    assert sibling_b_seen["caller"] == "unknown"  # ← the bug


# ── The fix: llm_caller context manager applied at call time ──────────────


def test_llm_caller_context_manager_sets_and_resets():
    """Sanity: the wrapper sets the tag inside the block and restores after."""
    # Pre-existing value the wrapper must restore on exit.
    set_llm_caller("preexisting")
    assert get_llm_caller() == "preexisting"

    with llm_caller("product_ingestion"):
        assert get_llm_caller() == "product_ingestion"

    assert get_llm_caller() == "preexisting"


def test_llm_caller_wrapper_tags_correctly_in_a_fresh_context():
    """
    The production scenario the fix targets: a cached extractor's
    ainvoke() runs in a fresh Context (no prior set_llm_caller call).
    The wrapper around ainvoke() must set the tag *in that Context* so
    the OTel handler reads the right value at metric-emit time.
    """
    seen = {}

    def _fresh_context_invocation():
        # Default at entry — would have produced caller="unknown" without
        # the wrapper.
        assert get_llm_caller() == "unknown"

        with llm_caller("product_ingestion"):
            # This is what the OTel handler reads inside ainvoke().
            seen["caller_at_emit"] = get_llm_caller()

        # Restored to default on exit so we don't leak into whatever
        # runs next in this context.
        assert get_llm_caller() == "unknown"

    contextvars.copy_context().run(_fresh_context_invocation)
    assert seen["caller_at_emit"] == "product_ingestion"


def test_llm_caller_wrapper_overrides_a_preset_value_only_in_block():
    """
    If the surrounding Context already has a caller tag (e.g. the
    extractor was lazy-initialised in this very Context), the wrapper
    must temporarily override it for the duration of the block and
    restore the original on exit.
    """
    set_llm_caller("outer_caller")
    with llm_caller("product_ingestion"):
        assert get_llm_caller() == "product_ingestion"
    assert get_llm_caller() == "outer_caller"


def test_llm_caller_resets_even_when_block_raises():
    """
    The wrapper uses a try/finally pattern. If the wrapped ainvoke()
    raises (network error, provider 5xx, parser failure), the tag must
    still be reset so the exception doesn't poison subsequent metric
    emissions in this Context.
    """
    set_llm_caller("baseline")
    with pytest.raises(RuntimeError, match="boom"):
        with llm_caller("product_ingestion"):
            assert get_llm_caller() == "product_ingestion"
            raise RuntimeError("boom")
    assert get_llm_caller() == "baseline"


# ── The fix under async (the actual production code path) ─────────────────


@pytest.mark.asyncio
async def test_llm_caller_wrapper_propagates_across_awaits():
    """
    ContextVar values propagate across `await` points within the same
    Task. The wrapper is a sync context manager but the production code
    awaits inside it, so this must hold — otherwise the OTel handler
    reading the ContextVar deep inside the awaited ainvoke() chain would
    see "unknown".
    """
    with llm_caller("product_ingestion"):
        await asyncio.sleep(0)  # forces an event-loop switch
        assert get_llm_caller() == "product_ingestion"
        # Simulate a deeper await chain (e.g. langchain → http client)
        async def _deep():
            await asyncio.sleep(0)
            return get_llm_caller()
        assert await _deep() == "product_ingestion"


@pytest.mark.asyncio
async def test_caller_is_unknown_in_a_new_task_spawned_outside_the_wrapper():
    """
    Negative-space test that pins down WHY the bug existed and WHERE the
    wrapper has to live.

    A Task spawned *outside* the wrapper inherits the surrounding
    Context — which has caller="unknown". Even though some other code
    elsewhere is busy inside `with llm_caller(...)`, that doesn't help
    a sibling Task. This is exactly the failure mode the lazy-init
    pattern triggers: extractor.__init__ is in one Task, extractor.aextract
    runs in another Task spawned by a different webhook. Wrapping the
    ainvoke() *inside* aextract is what fixes it.
    """
    set_llm_caller("baseline")
    captured = {}

    async def _spawned_task():
        # Inherits the baseline Context; the with-block in the parent
        # below has not affected this Task's inherited copy.
        captured["caller"] = get_llm_caller()

    # Spawn before entering the wrapper. The spawned Task's Context is
    # a snapshot of "now" — it does NOT see the upcoming with-block.
    task = asyncio.create_task(_spawned_task())

    with llm_caller("product_ingestion"):
        # Parent sees the wrapped value.
        assert get_llm_caller() == "product_ingestion"
        await task

    # The spawned Task saw the inherited Context value, not the wrapper's.
    assert captured["caller"] == "baseline"
