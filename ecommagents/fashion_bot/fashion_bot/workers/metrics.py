"""OTel metric instruments for the Dramatiq pipeline (§17).

Names follow the OneUptime guide so existing dashboards/queries line up:

* ``dramatiq.task.duration``  — histogram (ms), labelled queue_name/actor_name
* ``dramatiq.task.success``   — counter
* ``dramatiq.task.failure``   — counter
* ``dramatiq.task.retry``     — counter
* ``dramatiq.queue.depth``    — gauge (pending messages), labelled queue

All flow through the process-wide OTLP ``MeterProvider`` (set by
``observability.init_worker_observability`` in workers, or by ``agent_controller``
in the web app). Importing this module is cheap and dramatiq-free.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# OpenTelemetry is a hard dependency of the running app, but we degrade to
# no-ops if it is unavailable (e.g. a minimal context) so importing this module
# — and therefore the producer hot path — never fails on observability.
try:
    from opentelemetry import metrics as _otel_metrics
    _meter = _otel_metrics.get_meter("fashion-bot-workers", "1.0.0")
    _OTEL = True
except Exception:  # pragma: no cover
    _meter = None
    _OTEL = False
    logger.warning("[WORKER_METRICS] opentelemetry unavailable; metrics are no-ops")


def _histogram(name, **kw):
    return _meter.create_histogram(name, **kw) if _OTEL else None


def _counter(name, **kw):
    return _meter.create_counter(name, **kw) if _OTEL else None


task_duration = _histogram(
    "dramatiq.task.duration",
    description="Dramatiq task execution duration",
    unit="ms",
)
task_success = _counter(
    "dramatiq.task.success",
    description="Successfully processed dramatiq tasks",
    unit="tasks",
)
task_failure = _counter(
    "dramatiq.task.failure",
    description="Failed dramatiq tasks (raised / reported failure)",
    unit="tasks",
)
task_retry = _counter(
    "dramatiq.task.retry",
    description="Dramatiq tasks that were retried",
    unit="tasks",
)

# Producer-side signals (§17.5 alerts) — emitted from the web tier.
enqueue_queued = _counter(
    "webhook.enqueue.queued",
    description="Webhook jobs successfully enqueued",
    unit="jobs",
)
enqueue_failed = _counter(
    "webhook.enqueue.failed",
    description="Webhook enqueue attempts that exhausted retries (broker trouble)",
    unit="jobs",
)
inline_fallback = _counter(
    "webhook.inline.fallback",
    description="Webhook jobs processed inline because the broker was unreachable",
    unit="jobs",
)
inline_deferred = _counter(
    "webhook.inline.deferred",
    description="Webhook jobs deferred to source retry (inline fallback saturated)",
    unit="jobs",
)


def record_producer_event(kind: str, job_type: str) -> None:
    """Emit a producer-side counter. ``kind`` ∈ queued/failed/fallback/deferred."""
    attrs = {"job_type": job_type}
    try:
        {
            "queued": enqueue_queued,
            "failed": enqueue_failed,
            "fallback": inline_fallback,
            "deferred": inline_deferred,
        }[kind].add(1, attrs)
    except Exception as ex:
        logger.debug("[WORKER_METRICS] producer event failed: %r", ex)


# Queue depth is exported as an OTel observable gauge (see queue_depth.py),
# registered in the worker process — not a synchronous gauge set from a thread.


def record_task_result(*, queue_name: str, actor_name: str,
                       duration_ms: float, outcome: str) -> None:
    """Emit duration + the appropriate outcome counter for one processed task.

    ``outcome`` is one of ``success`` / ``failure`` / ``retry``. This is the
    recording step the OneUptime tutorial omits (it declares the instruments but
    never records them).
    """
    attrs = {"queue_name": queue_name, "actor_name": actor_name}
    try:
        task_duration.record(max(0.0, duration_ms), attrs)
        if outcome == "success":
            task_success.add(1, attrs)
        elif outcome == "retry":
            task_retry.add(1, attrs)
        else:
            task_failure.add(1, attrs)
    except Exception as ex:  # metrics must never break processing
        logger.debug("[WORKER_METRICS] record failed: %r", ex)
