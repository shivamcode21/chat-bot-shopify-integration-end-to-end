"""Worker entrypoint — the Dramatiq CLI target.

Run a per-lane worker service with, e.g.::

    dramatiq fashion_bot.workers.run --queues webhooks.shopify.inventory --threads 8
    dramatiq fashion_bot.workers.run --queues webhooks.shopify.product   --threads 4
    dramatiq fashion_bot.workers.run --queues cron.product.sync          --threads 1

This module initializes worker-process OpenTelemetry (traces/metrics/logs →
OTLP) BEFORE importing the actors, then imports them so Dramatiq discovers and
consumes them. It is the only place worker observability is initialized, so the
web/producer process (which already has its own OTel providers) is unaffected.
"""
from __future__ import annotations

import os

from fashion_bot.env_loader import bootstrap_environment

bootstrap_environment()

# Disable LangSmith tracing in workers — product ingestion LLM calls
# (attribute extraction, OCR, summarisation) are high-volume / low-value
# for tracing and drive significant LangSmith costs.
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING_V2"] = "false"

from fashion_bot.workers.observability import init_worker_observability  # noqa: E402

init_worker_observability()

# Register the queue-depth observable gauge in the worker process only (so the
# web/producer process does not also emit it). Safe no-op if OTel is disabled.
from fashion_bot.workers.queue_depth import register_queue_depth_observer  # noqa: E402

register_queue_depth_observer()

# Importing the actors registers them on the broker so the Dramatiq CLI consumes
# them. (Names are imported so the CLI's module scan discovers them.)
from fashion_bot.workers.actors import (  # noqa: E402,F401
    inventory_update,
    product_upsert,
    product_delete,
    order_event,
    cart_event,
    shiprocket_event,
    shiprocket_cart_event,
    delhivery_event,
    gupshup_event,
    conversation_inactivity_event,
    escalation_event,
    escalation_whatsapp,
    escalation_email,
    product_delta_sync,
)
