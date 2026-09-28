"""Smoke test for the Dramatiq webhook queue plumbing (StubBroker, in-memory OTel).

Exercises the runtime integration the design relies on — WITHOUT the heavy real
handlers — so it is safe to run anywhere dramatiq + opentelemetry are installed:

* broker is configured with AsyncIO + OpenTelemetryMiddleware and the built-in
  Prometheus exposition removed;
* an ``async`` actor is enqueued and processed end-to-end (AsyncIO middleware);
* our OpenTelemetryMiddleware injects trace context on enqueue and records the
  ``dramatiq.task.*`` metrics + spans the design promises;
* the producer ``submit_or_inline`` falls back to inline when the lane is off.

Run directly:  python -m fashion_bot.tests.test_webhook_queue_smoke
Or via pytest.
"""
from __future__ import annotations

import os

# Force the StubBroker path (no real Render broker needed) before importing.
os.environ["DRAMATIQ_BROKER_URL"] = ""
os.environ["REDIS_URL"] = ""
os.environ.pop("REDIS_CONNECTION_STRING", None)

try:
    import pytest
    dramatiq = pytest.importorskip("dramatiq")
    pytest.importorskip("opentelemetry.sdk")
except ModuleNotFoundError:  # allow standalone run without pytest installed
    import importlib
    import dramatiq
    importlib.import_module("opentelemetry.sdk")

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Install in-memory OTel providers BEFORE importing the workers package so the
# middleware's tracer and the metric instruments bind to them.
_READER = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[_READER]))
_SPANS = InMemorySpanExporter()
_tp = TracerProvider()
_tp.add_span_processor(SimpleSpanProcessor(_SPANS))
trace.set_tracer_provider(_tp)


def _metric_points(name):
    data = _READER.get_metrics_data()
    pts = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                if m.name == name:
                    pts.extend(m.data.data_points)
    return pts


def test_broker_is_configured_with_expected_middleware():
    from fashion_bot.workers import broker as broker_mod

    broker = broker_mod.get_broker()
    names = {type(m).__name__ for m in broker.middleware}
    assert type(broker).__name__ == "StubBroker"  # no URL → stub
    assert "AsyncIO" in names
    assert "OpenTelemetryMiddleware" in names
    assert "Prometheus" not in names  # we export via OTLP, not :9191


def test_all_lane_actors_registered():
    from fashion_bot.workers import config
    from fashion_bot.workers.actors import ACTOR_BY_JOB

    expected = {
        config.JOB_INVENTORY_UPDATE, config.JOB_PRODUCT_UPSERT, config.JOB_PRODUCT_DELETE,
        config.JOB_ORDER_EVENT, config.JOB_CART_EVENT,
        config.JOB_SHIPROCKET_EVENT, config.JOB_SHIPROCKET_CART_EVENT,
        config.JOB_DELHIVERY_EVENT,
        config.JOB_GUPSHUP_EVENT,
        # conversation_scan_event / conversation_created_event are disabled
        # (inactivity only) — see actors.py — so they are intentionally NOT in
        # ACTOR_BY_JOB.
        config.JOB_CONVERSATION_INACTIVITY_EVENT,
        config.JOB_ESCALATION_EVENT,
        # Per-channel escalation delivery lanes (design §5.2a).
        config.JOB_ESCALATION_WHATSAPP,
        config.JOB_ESCALATION_EMAIL,
        # Weekly product delta sync, offloaded from the webhook tier.
        config.JOB_PRODUCT_DELTA_SYNC,
    }
    assert set(ACTOR_BY_JOB) == expected, set(ACTOR_BY_JOB)
    # Each maps to a real dramatiq actor on a distinct queue.
    queues = {a.queue_name for a in ACTOR_BY_JOB.values()}
    assert config.QUEUE_SHOPIFY_ORDER in queues
    assert config.QUEUE_DELHIVERY in queues
    assert config.QUEUE_PRODUCT_SYNC in queues


def test_async_actor_processes_and_emits_telemetry():
    from fashion_bot.workers import broker as broker_mod

    broker = broker_mod.get_broker()
    seen = {}

    @dramatiq.actor(queue_name="smoke.test", max_retries=0)
    async def echo(*, value):
        seen["value"] = value
        return {"success": True}

    worker = dramatiq.Worker(broker, worker_threads=1, worker_timeout=100)
    worker.start()
    try:
        echo.send(value=42)
        broker.join(echo.queue_name)
        worker.join()
    finally:
        worker.stop()

    assert seen.get("value") == 42, "async actor did not run via AsyncIO middleware"

    # Our middleware should have recorded a success and a duration sample.
    success = _metric_points("dramatiq.task.success")
    assert any(getattr(p, "value", 0) >= 1 for p in success), "no success metric recorded"
    assert _metric_points("dramatiq.task.duration"), "no duration metric recorded"

    # And produced an enqueue span + a process span.
    span_names = {s.name for s in _SPANS.get_finished_spans()}
    assert "dramatiq.enqueue" in span_names
    assert any(n.startswith("dramatiq.process.") for n in span_names), span_names


def test_submit_or_inline_runs_inline_when_lane_disabled():
    import asyncio
    from fashion_bot.workers import config, enqueue

    assert not config.lane_enabled(config.JOB_INVENTORY_UPDATE)  # flag off by default

    ran = {"n": 0}

    async def inline():
        ran["n"] += 1
        return {"success": True, "action": "inline"}

    res = asyncio.run(
        enqueue.submit_or_inline(config.JOB_INVENTORY_UPDATE, {"x": 1}, inline)
    )
    assert res == {"success": True, "action": "inline"} and ran["n"] == 1


if __name__ == "__main__":
    test_broker_is_configured_with_expected_middleware()
    print("✓ broker configured (StubBroker + AsyncIO + OpenTelemetryMiddleware, no Prometheus)")
    test_all_lane_actors_registered()
    print("✓ all lane actors registered on distinct queues")
    test_async_actor_processes_and_emits_telemetry()
    print("✓ async actor processed; task.success + task.duration metrics + enqueue/process spans emitted")
    test_submit_or_inline_runs_inline_when_lane_disabled()
    print("✓ submit_or_inline runs inline when lane disabled (legacy path intact)")
    print("\nALL SMOKE CHECKS PASSED")
