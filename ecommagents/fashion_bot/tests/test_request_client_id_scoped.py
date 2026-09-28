"""
Tests for the scoped ``request_client_id(...)`` context manager added
alongside the cron / onboarding client_id-attribution fix.

Background
----------
The fire-and-forget ``set_request_client_id`` helper is correct for HTTP
request handlers (each request runs in its own asyncio Task, so the
attached OTel baggage context is GC'd when the task ends). It is NOT
correct for long-running paths that loop over many clients inside a
single Task — the weekly product sync, conversation analytics, and
batch onboarding all hit this pattern. Without scoping, each iteration
layers a new baggage context on top of the previous one and the
previous client's id bleeds into the next iteration's downstream
metrics.

The scoped variant pairs the ``context.attach()`` with a ``detach()`` on
exit so each iteration sees only its own client_id in baggage. These
tests pin that behaviour down.
"""

import asyncio

import pytest

from fashion_bot.monitoring.otel_metrics import (
    get_request_client_id,
    request_client_id,
    set_request_client_id,
)


# ── Single-iteration behaviour ────────────────────────────────────────────


def test_scoped_manager_sets_and_restores_baggage():
    """Sanity: inside the block reads back the scoped id; outside reverts."""
    # Establish a known baseline so the test isn't sensitive to fixture
    # leakage from prior tests in the same session.
    set_request_client_id("baseline")
    assert get_request_client_id() == "baseline"

    with request_client_id("client_A"):
        assert get_request_client_id() == "client_A"

    # Restored on exit. The original set_request_client_id semantics
    # (fire-and-forget) means baggage at exit reverts to what was in
    # baggage when the context manager entered — not what was set
    # before in this Task. That value happens to be "baseline" here.
    assert get_request_client_id() == "baseline"


def test_scoped_manager_falls_back_to_unknown_when_none():
    """Defensive: a None / empty id maps to "unknown" rather than crashing."""
    set_request_client_id("baseline")
    with request_client_id(None):
        assert get_request_client_id() == "unknown"
    assert get_request_client_id() == "baseline"


def test_scoped_manager_restores_even_when_block_raises():
    """
    Exception inside the wrapped body must NOT leak the scoped id into
    the rest of the surrounding cron iteration / onboarding. try/finally
    in the context manager handles this.
    """
    set_request_client_id("baseline")
    with pytest.raises(RuntimeError, match="boom"):
        with request_client_id("client_A"):
            assert get_request_client_id() == "client_A"
            raise RuntimeError("boom")
    assert get_request_client_id() == "baseline"


# ── Per-iteration scoping (the actual production scenario) ────────────────


def test_iterating_clients_does_not_leak_between_iterations():
    """
    The motivating scenario: a cron loop over many clients. Without the
    scoped manager (using fire-and-forget set_request_client_id alone),
    iteration N+1 would still see iteration N's client_id in baggage
    until something new attached. The scoped manager prevents that.
    """
    set_request_client_id("baseline")
    seen_inside = []
    seen_between = []

    for cid in ["client_A", "client_B", "client_C"]:
        with request_client_id(cid):
            seen_inside.append(get_request_client_id())
        # Outside the block — should NOT see the just-finished iteration's id
        seen_between.append(get_request_client_id())

    assert seen_inside == ["client_A", "client_B", "client_C"]
    # Every "between" read must show the pre-loop baseline, not the
    # previous iteration's id. This is exactly what the dashboard needs
    # to keep cron telemetry partitioned correctly.
    assert seen_between == ["baseline", "baseline", "baseline"]


def test_nested_scoped_managers_inner_overrides_then_outer_restores():
    """
    Nested calls (e.g. onboard_client → product_sync → per-product
    extractor): inner scope sees its own id, outer scope is restored
    when inner exits.
    """
    set_request_client_id("baseline")
    with request_client_id("outer"):
        assert get_request_client_id() == "outer"
        with request_client_id("inner"):
            assert get_request_client_id() == "inner"
        # Inner exited — restored to outer's value, NOT baseline.
        assert get_request_client_id() == "outer"
    # Outer exited — restored to baseline.
    assert get_request_client_id() == "baseline"


# ── Async behaviour ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scoped_baggage_propagates_across_awaits_in_same_task():
    """
    OTel baggage values propagate across `await` points within the same
    Task. The wrapper is sync but production code does `await` inside
    the with-block — this test pins that down so a future refactor that
    accidentally breaks propagation will fail loud.
    """
    with request_client_id("client_A"):
        await asyncio.sleep(0)
        assert get_request_client_id() == "client_A"

        async def _deep():
            await asyncio.sleep(0)
            return get_request_client_id()

        assert await _deep() == "client_A"


@pytest.mark.asyncio
async def test_scoped_baggage_visible_to_task_spawned_inside_block():
    """
    A Task spawned INSIDE the with-block inherits the scoped baggage
    because asyncio.create_task() snapshots the current Context at
    spawn time — and at that moment, the scoped id IS in baggage.

    This is the actual production scenario: per-client cron loop wraps
    sync_one_client() in request_client_id(client_id); sync_one_client
    spawns sub-tasks for parallel product extraction; those sub-tasks
    correctly see the wrapped client_id in baggage when emitting llm.*
    metrics.
    """
    captured = {}

    async def _spawned():
        # Inherits the snapshot Context, which has the scoped id.
        captured["client_id"] = get_request_client_id()

    with request_client_id("client_A"):
        await asyncio.create_task(_spawned())

    assert captured["client_id"] == "client_A"
