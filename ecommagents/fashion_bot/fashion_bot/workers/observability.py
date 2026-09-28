"""Worker-process OpenTelemetry bootstrap.

Worker processes are started by the ``dramatiq`` CLI, not by FastAPI, so they do
not run ``agent_controller``'s module-level OTel setup. This mirrors that setup
(traces + metrics + logs → OTLP) so worker spans/metrics/logs land in the same
Grafana stack, using the same env gating (``ENABLE_OTEL`` / production).

Idempotent: calling :func:`init_worker_observability` more than once is a no-op.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_initialized = False

ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
IS_PRODUCTION = ENVIRONMENT in ("production", "prod")
ENABLE_OTEL = IS_PRODUCTION or os.getenv("ENABLE_OTEL_LOCAL", "false").lower() == "true"


def init_worker_observability(service_name: str | None = None) -> None:
    """Configure OTLP tracer/meter/logger providers for a worker process."""
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not ENABLE_OTEL:
        logger.info("[WORKER_OTEL] disabled (environment=%s)", ENVIRONMENT)
        return

    try:
        from opentelemetry import _logs, metrics, trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from fashion_bot.trace_context import install_trace_filter

        name = service_name or os.getenv("OTEL_SERVICE_NAME", "fashion-bot-worker")
        resource = Resource.create({
            "service.name": name,
            "service.instance.id": os.uname().nodename,
            "deployment.environment": ENVIRONMENT,
            "service.type": "task_queue",
        })

        # Traces
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        # Metrics
        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
        metrics.set_meter_provider(meter_provider)

        # Logs → Grafana (only when running in production, like the web app)
        if IS_PRODUCTION:
            logger_provider = LoggerProvider(resource=resource)
            _logs.set_logger_provider(logger_provider)
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(OTLPLogExporter())
            )
            logging.getLogger().addHandler(
                LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
            )
            # Same noise-suppression as agent_controller for transient OTLP hiccups.
            logging.getLogger(
                "opentelemetry.sdk._logs._internal.export"
            ).setLevel(logging.CRITICAL)
            logging.getLogger(
                "opentelemetry.exporter.otlp.proto.http._log_exporter"
            ).setLevel(logging.CRITICAL)
            # Mirror the web app: inject trace/span ids into log records so the
            # AGENTS.md §5 "trace_id in every log line" rule holds for workers too.
            try:
                from opentelemetry.instrumentation.logging import LoggingInstrumentor
                LoggingInstrumentor().instrument(set_logging_format=True)
            except Exception as _li_ex:
                logger.debug("[WORKER_OTEL] logging instrumentation skipped: %r", _li_ex)
            install_trace_filter()

        logger.info("[WORKER_OTEL] enabled (service=%s environment=%s)", name, ENVIRONMENT)
    except Exception as ex:  # never let observability setup crash a worker
        logger.warning("[WORKER_OTEL] init failed, continuing without OTel: %r", ex)
