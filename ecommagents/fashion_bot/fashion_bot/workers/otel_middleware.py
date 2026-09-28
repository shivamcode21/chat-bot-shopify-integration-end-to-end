"""Dramatiq middleware: OpenTelemetry traces + metrics in one (§17.2).

Implements the OneUptime pattern, fixed for production:

* trace context is injected on enqueue and extracted on process so one trace
  spans edge → enqueue → worker handler;
* metrics are **actually recorded** in ``after_process_message`` (duration +
  success/failure) — the tutorial declares the instruments but never emits them;
* the worker app's ``trace_context`` is populated so existing ``%(trace_id)s``
  log correlation keeps working in Loki.

This module imports ``dramatiq`` and is therefore only loaded in the broker /
worker context, never on the producer hot path.
"""
from __future__ import annotations

import logging
import time

from dramatiq.middleware import Middleware
from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace.status import Status, StatusCode

from fashion_bot.trace_context import set_trace_id
from fashion_bot.monitoring.otel_metrics import set_request_client_id
from fashion_bot.workers.metrics import record_task_result

logger = logging.getLogger(__name__)

_SPAN_KEY = "_otel_span"
_START_KEY = "_otel_start_time"
_CTX_KEY = "otel_context"


class OpenTelemetryMiddleware(Middleware):
    """Adds OTel tracing + metrics to every Dramatiq message."""

    def __init__(self) -> None:
        self._tracer = trace.get_tracer("fashion-bot-workers")

    # ── producer ──────────────────────────────────────────────────────────
    def before_enqueue(self, broker, message, delay):
        try:
            with self._tracer.start_as_current_span("dramatiq.enqueue") as span:
                span.set_attribute("messaging.system", "dramatiq")
                span.set_attribute("messaging.operation", "send")
                span.set_attribute("messaging.destination", message.queue_name)
                span.set_attribute("dramatiq.actor_name", message.actor_name)
                span.set_attribute("dramatiq.message_id", message.message_id)
                if delay:
                    span.set_attribute("dramatiq.delay_ms", delay)
                carrier: dict = {}
                inject(carrier)
                message.options[_CTX_KEY] = carrier
        except Exception as ex:  # tracing must never block enqueue
            logger.debug("[OTEL_MW] before_enqueue failed: %r", ex)

    # ── consumer ──────────────────────────────────────────────────────────
    def before_process_message(self, broker, message):
        try:
            ctx = extract(message.options.get(_CTX_KEY, {}) or {})
            span = self._tracer.start_span(
                name=f"dramatiq.process.{message.actor_name}", context=ctx
            )
            span.set_attribute("messaging.system", "dramatiq")
            span.set_attribute("messaging.operation", "receive")
            span.set_attribute("messaging.destination", message.queue_name)
            span.set_attribute("dramatiq.actor_name", message.actor_name)
            span.set_attribute("dramatiq.message_id", message.message_id)
            span.set_attribute("dramatiq.queue_name", message.queue_name)
            span.set_attribute("dramatiq.retries", message.options.get("retries", 0))
            message.options[_SPAN_KEY] = span
            message.options[_START_KEY] = time.time()
            # Keep the app's logging trace_id correlated with the worker run.
            carried = (message.kwargs or {}).get("trace_id")
            set_trace_id(carried or message.message_id)
            # Propagate client_id into OTel baggage so LLM metrics
            # (llm_calls_total, llm_tokens_*) carry the correct label
            # instead of "unknown".
            cid = (message.kwargs or {}).get("client_id")
            if cid:
                set_request_client_id(cid)
                span.set_attribute("client_id", cid)
        except Exception as ex:
            logger.debug("[OTEL_MW] before_process_message failed: %r", ex)

    def after_process_message(self, broker, message, *, result=None, exception=None):
        span = message.options.pop(_SPAN_KEY, None)
        start = message.options.pop(_START_KEY, None)
        duration_ms = (time.time() - start) * 1000 if start else 0.0
        # A message about to be retried carries its (incremented) retry count.
        retries = message.options.get("retries", 0)
        outcome = "success"
        if exception is not None:
            outcome = "retry" if retries and retries > 0 else "failure"
        try:
            record_task_result(
                queue_name=message.queue_name,
                actor_name=message.actor_name,
                duration_ms=duration_ms,
                outcome=outcome,
            )
        except Exception:
            pass
        if span is not None:
            try:
                span.set_attribute("dramatiq.execution_time_ms", duration_ms)
                if exception is not None:
                    span.record_exception(exception)
                    span.set_status(Status(StatusCode.ERROR, str(exception)))
                    span.set_attribute("dramatiq.status", "failed")
                else:
                    span.set_status(Status(StatusCode.OK))
                    span.set_attribute("dramatiq.status", "completed")
                span.end()
            except Exception as ex:
                logger.debug("[OTEL_MW] span end failed: %r", ex)

    def after_skip_message(self, broker, message):
        span = message.options.pop(_SPAN_KEY, None)
        if span is not None:
            try:
                span.set_attribute("dramatiq.status", "skipped")
                span.set_status(Status(StatusCode.ERROR, "Message skipped"))
                span.end()
            except Exception:
                pass
