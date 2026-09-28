"""
OpenTelemetry instrumentation glue for the Fashion Bot platform.

Architecture (standard OTel + OpenLLMetry, minimal custom code):

* LLM calls — auto-instrumented by Traceloop / OpenLLMetry. The instrumentors
  emit standard ``gen_ai.*`` spans and metrics (token usage, latency,
  errors, model, provider) into the existing OTLP pipeline. Per-call
  ``client_id`` labels are attached via Traceloop association properties,
  set once per request via :func:`set_request_client_id`.

* Webhooks — auto-instrumented by ``FastAPIInstrumentor`` (HTTP server
  spans + ``http.server.duration`` metrics). The :func:`webhook_request_hook`
  enriches those spans with ``webhook.type`` / ``webhook.topic`` so RED
  metrics can be derived per webhook in the collector.

* Cron jobs — not HTTP, no auto-instrumentor exists. The :func:`track_cron`
  decorator emits ``cron.*`` counters/histograms.

This module deliberately avoids re-implementing anything Traceloop or
``opentelemetry-instrumentation-fastapi`` already provides.
"""

import base64
import functools
import inspect
import logging
import time as _time
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, NamedTuple, Optional, Sequence, Union

from opentelemetry import baggage, context, metrics, trace
from opentelemetry.metrics import Observation
from langchain_core.callbacks import BaseCallbackHandler

_logger = logging.getLogger(__name__)

meter = metrics.get_meter("fashion-bot", "1.0.0")


# ---------------------------------------------------------------------------
# LLM Call Metrics — custom instruments exported via OTel MeterProvider
# ---------------------------------------------------------------------------
# Traceloop/OpenLLMetry instrumentors only produce SPANS, not Prometheus
# metrics. These counters/histograms give us llm_* metrics in Prometheus.

llm_call_counter = meter.create_counter(
    "llm.calls",
    description="Total LLM invocations",
)
llm_call_duration = meter.create_histogram(
    "llm.call.duration_ms",
    description="LLM call round-trip latency",
    unit="ms",
)
llm_prompt_tokens = meter.create_counter(
    "llm.tokens.prompt",
    description="Prompt / input tokens consumed",
)
llm_completion_tokens = meter.create_counter(
    "llm.tokens.completion",
    description="Completion / output tokens generated",
)
llm_error_counter = meter.create_counter(
    "llm.errors",
    description="LLM call failures",
)
llm_provider_error_counter = meter.create_counter(
    "llm.provider_errors",
    description=(
        "Wrapped upstream provider errors (e.g. OpenRouter envelope "
        "{'message': 'Provider returned error', 'code': N}). Labeled by the "
        "HTTP code so 400 (bad request / context overrun) vs 404 (upstream "
        "model not available) vs 429 (rate limit) can be split in dashboards. "
        "Emitted from node-level except blocks that can introspect the "
        "exception body — distinct from llm.errors which is emitted by the "
        "LangChain callback handler and only knows the Python exception type."
    ),
)
llm_cost_usd_counter = meter.create_counter(
    "llm.cost.usd",
    description="Estimated LLM call cost in USD (computed from token counts × per-model price table)",
)


# ---------------------------------------------------------------------------
# Escalation Metrics — resolution-first observability
# ---------------------------------------------------------------------------
# Emitted once per escalation OUTCOME from
# EscalationOrchestrator.aescalate_to_agent: a delivered escalation (incl. the
# Courier Update Pending internal sync) or a gate soft-block
# (soft_blocked=true). Intermediate returns (the web-chat "please share your
# phone number" turn) are NOT counted, so the counter matches escalations that
# actually happened. Labels stay low-cardinality: category (~30),
# classification (3), actionability (3), soft_blocked (2), client_id (bounded
# per tenant).
escalation_counter = meter.create_counter(
    "escalations.total",
    description=(
        "Escalation outcomes, labelled by category, classification, "
        "human-actionability (actionable/unfulfillable/mandatory), and whether "
        "the default-on actionability gate soft-blocked it (soft_blocked=true "
        "means the customer was offered alternatives instead of a hand-off). "
        "Counted once per delivered or soft-blocked escalation. Track "
        "actionability=unfulfillable to catch escalations a human could not "
        "have fulfilled."
    ),
)


# ---------------------------------------------------------------------------
# LLM Caller (category) propagation
# ---------------------------------------------------------------------------
# A "caller" is the logical originator of an LLM call (e.g. "product_details"
# for a graph agent, "product_ingestion" for the attribute extractor,
# "conversation_analytics" for the post-hoc analyzer). It becomes the
# ``caller`` label on every llm.* metric so dashboards can attribute
# cost/volume per category, per client.
#
# Stored in a ContextVar so it is asyncio-safe (each task gets its own copy
# of the parent context, mutations do not leak across tasks).
_llm_caller_var: ContextVar[str] = ContextVar("llm_caller", default="unknown")


def set_llm_caller(name: Optional[str]) -> None:
    """Set the caller label for all subsequent LLM metrics in this context.

    Called automatically by :class:`LLMFactory` from ``tool_name``. Most code
    will not need to call this directly. Use the :func:`llm_caller` context
    manager when you need scoped overrides.
    """
    _llm_caller_var.set(name or "unknown")


def get_llm_caller() -> str:
    """Return the current caller label (or ``"unknown"`` if none set)."""
    return _llm_caller_var.get()


@contextmanager
def llm_caller(name: str):
    """Context manager that scopes the LLM caller label.

    Use when you need to override the auto-set caller for a specific block,
    e.g. when reusing a cached LLM instance across multiple semantic callers.
    """
    token = _llm_caller_var.set(name or "unknown")
    try:
        yield
    finally:
        _llm_caller_var.reset(token)


# ---------------------------------------------------------------------------
# LLM Pricing Table — USD per token (prompt, completion)
# ---------------------------------------------------------------------------
# Prices are best-effort, kept in code so cost is computed at metric-emit
# time (no fragile PromQL math, no recording rules to maintain). Values are
# per-token (USD/1M tokens / 1e6). Update when vendor prices change.
#
# Lookup uses a substring match on the model name — first match wins, so list
# more-specific patterns before generic ones.
_LLM_PRICING_PER_TOKEN: list = [
    # (substring matcher, prompt_usd_per_token, completion_usd_per_token)
    ("gpt-4o-mini",                 0.15 / 1_000_000,  0.60 / 1_000_000),
    ("gpt-4o",                      2.50 / 1_000_000, 10.00 / 1_000_000),
    ("openai/gpt-4o-mini",          0.15 / 1_000_000,  0.60 / 1_000_000),
    ("openai/gpt-4o",               2.50 / 1_000_000, 10.00 / 1_000_000),
    ("gemini-2.5-flash-lite",       0.075 / 1_000_000, 0.30 / 1_000_000),
    ("gemini-2.5-flash",            0.30 / 1_000_000,  2.50 / 1_000_000),
    ("gemini-3.1-flash-lite",       0.10 / 1_000_000,  0.40 / 1_000_000),
    ("gemini-3.1-flash",            0.30 / 1_000_000,  2.50 / 1_000_000),
    ("gemini",                      0.10 / 1_000_000,  0.40 / 1_000_000),
    ("claude-3-5-sonnet",           3.00 / 1_000_000, 15.00 / 1_000_000),
    ("claude-3-5-haiku",            0.80 / 1_000_000,  4.00 / 1_000_000),
    ("claude",                      3.00 / 1_000_000, 15.00 / 1_000_000),
]


def _estimate_llm_cost_usd(model: str, prompt_tok: int, completion_tok: int) -> float:
    """Estimate USD cost for a single LLM call. Returns 0.0 if model unknown."""
    if not model:
        return 0.0
    name = model.lower()
    for matcher, prompt_rate, completion_rate in _LLM_PRICING_PER_TOKEN:
        if matcher in name:
            return (prompt_tok or 0) * prompt_rate + (completion_tok or 0) * completion_rate
    return 0.0


# ---------------------------------------------------------------------------
# LLM usage extraction — streaming vs non-streaming
# ---------------------------------------------------------------------------
# LangChain surfaces token usage in two different places depending on how the
# model was invoked, and only one of them carries the provider's real cost:
#
#   ainvoke  (non-streaming) -> response.llm_output["token_usage"] holds the raw
#       provider usage block. For OpenRouter that includes ``cost`` — the actual
#       USD charged (https://openrouter.ai/docs/use-cases/usage-accounting) — so
#       we bill from it directly instead of guessing from the price table.
#
#   astream  (streaming)     -> response.llm_output is **None**. Usage survives
#       only as the normalized ``message.usage_metadata``, and LangChain drops
#       non-standard keys during that normalization, so ``cost`` is unavailable
#       and we fall back to the price-table estimate. The model name survives on
#       ``message.response_metadata["model_name"]``.
#
# Before this handler read the streaming path, every streamed call (the whole
# conversational agent graph, ~30% of traffic) recorded model="unknown" with
# zero tokens and zero cost — invisible spend rather than free spend.


def _coerce_token_count(value: Any) -> int:
    """Best-effort non-negative int for a provider-supplied token count."""
    if isinstance(value, bool) or value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _coerce_cost_usd(value: Any) -> Optional[float]:
    """Non-negative float cost, or ``None`` when absent/unparseable.

    ``None`` (not ``0.0``) signals "provider did not report a cost", which is
    what makes the caller fall back to the price-table estimate. A genuine
    0.0 (free model) is preserved as an actual cost.
    """
    if isinstance(value, bool) or value is None:
        return None
    try:
        cost = float(value)
    except (TypeError, ValueError):
        return None
    if cost != cost or cost < 0:  # NaN or negative — treat as not reported
        return None
    return cost


class _LLMUsage(NamedTuple):
    """Normalized view of one LLM call's usage, across invoke/stream paths."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    actual_cost_usd: Optional[float]  # None => provider did not report cost


def _extract_llm_usage(response: Any) -> _LLMUsage:
    """Pull model, token counts and (when available) real cost off an LLMResult.

    Pure and total — never raises, so a provider returning an unexpected shape
    degrades to ``model="unknown"`` with zero tokens rather than breaking the
    callback (and with it the caller's LLM request).
    """
    model = "unknown"
    prompt_tokens = 0
    completion_tokens = 0
    actual_cost: Optional[float] = None

    # --- Path 1: non-streaming. Raw provider usage, includes OpenRouter cost.
    llm_output = getattr(response, "llm_output", None)
    if isinstance(llm_output, dict) and llm_output:
        model = llm_output.get("model_name") or llm_output.get("model") or model
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, dict):
            prompt_tokens = _coerce_token_count(usage.get("prompt_tokens"))
            completion_tokens = _coerce_token_count(usage.get("completion_tokens"))
            actual_cost = _coerce_cost_usd(usage.get("cost"))

    # --- Path 2: streaming. llm_output is None; read the accumulated message.
    # Each field falls back independently, so a response that carries only some
    # of them (tokens but no model name, say) still fills in the rest. Tokens are
    # only summed here when path 1 produced none — llm_output already reports
    # them aggregated across generations, so adding both would double-count.
    tokens_from_llm_output = bool(prompt_tokens or completion_tokens)
    if tokens_from_llm_output and model != "unknown" and actual_cost is not None:
        return _LLMUsage(model, prompt_tokens, completion_tokens, actual_cost)

    for batch in getattr(response, "generations", None) or []:
        for generation in batch or []:
            message = getattr(generation, "message", None)
            if message is None:
                continue

            metadata = getattr(message, "response_metadata", None)
            if isinstance(metadata, dict):
                if model == "unknown":
                    model = metadata.get("model_name") or metadata.get("model") or model
                if actual_cost is None:
                    # Some providers echo the raw usage block here too.
                    meta_usage = metadata.get("token_usage") or metadata.get("usage")
                    if isinstance(meta_usage, dict):
                        actual_cost = _coerce_cost_usd(meta_usage.get("cost"))

            usage_metadata = getattr(message, "usage_metadata", None)
            if not tokens_from_llm_output and isinstance(usage_metadata, dict):
                # Summed across generations: n>1 bills one call per generation.
                prompt_tokens += _coerce_token_count(usage_metadata.get("input_tokens"))
                completion_tokens += _coerce_token_count(usage_metadata.get("output_tokens"))

    return _LLMUsage(model or "unknown", prompt_tokens, completion_tokens, actual_cost)


# ---------------------------------------------------------------------------
# Client-id propagation — single entry point used by all request handlers
# ---------------------------------------------------------------------------
def set_request_client_id(client_id: Optional[str]) -> None:
    """Attach ``client_id`` to all telemetry emitted in the current context.

    Three propagation channels, in order of preference:

    1. **Current span attribute** — labels the FastAPI-auto-generated server
       span so HTTP RED metrics derived in the collector are partitioned by
       client.
    2. **Traceloop association property** — propagates to every downstream
       LLM span (OpenAI, Anthropic, LangChain) emitted by OpenLLMetry's
       auto-instrumentors.
    3. **OTel baggage** — propagates across process boundaries and to any
       custom metric (``cron.*``) that reads via :func:`get_request_client_id`.
    """
    cid = client_id or "unknown"

    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("client_id", cid)

    try:
        from traceloop.sdk import Traceloop
        Traceloop.set_association_properties({"client_id": cid})
    except Exception as exc:
        _logger.debug("Traceloop association property set failed: %s", exc)

    ctx = baggage.set_baggage("client_id", cid)
    context.attach(ctx)

    # On-loop entry point: capture the event loop and warm the client_id->name
    # snapshot so the (off-loop) LLM callback can resolve client_name. Throttled
    # internally, so this is cheap to call on every request.
    try:
        from fashion_bot.utils.client_identity_cache import prewarm_client_name_cache
        prewarm_client_name_cache()
    except Exception as exc:
        _logger.debug("client_name cache prewarm failed: %s", exc)


def get_request_client_id() -> str:
    """Return the ``client_id`` set via :func:`set_request_client_id`, or ``"unknown"``."""
    return str(baggage.get_baggage("client_id") or "unknown")


def _client_name_label(client_id: str) -> str:
    """Human-readable ``client_name`` for a metric label, or ``"unknown"``.

    Resolution is non-blocking (process-local snapshot + throttled async
    refresh) so it's safe to call from the synchronous LLM callback. Any
    failure degrades to ``"unknown"`` and never breaks metric emission.
    """
    try:
        from fashion_bot.utils.client_identity_cache import get_client_name_sync
        return get_client_name_sync(client_id)
    except Exception:
        return "unknown"


@contextmanager
def request_client_id(client_id: Optional[str]):
    """Scoped variant of :func:`set_request_client_id`.

    Use in long-running paths that iterate over multiple clients within a
    single asyncio task — cron jobs (product sync, conversation analytics),
    onboarding, batch backfills. ``set_request_client_id`` is fire-and-
    forget (it calls ``context.attach()`` and never detaches, relying on
    the surrounding request task ending to clean up). That's correct for
    web handlers, but in a per-client loop it would layer a new baggage
    context on every iteration and bleed the previous client's id into
    the next iteration's downstream calls.

    This manager pairs the attach with a detach so the previous baggage
    context is restored on exit — keeping cron telemetry correctly
    partitioned by client without context-stack growth.

    Example::

        for client_id in client_ids:
            with request_client_id(client_id):
                await sync_one_client(client_id)
    """
    cid = client_id or "unknown"

    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("client_id", cid)

    try:
        from traceloop.sdk import Traceloop
        Traceloop.set_association_properties({"client_id": cid})
    except Exception as exc:
        _logger.debug("Traceloop association property set failed: %s", exc)

    ctx = baggage.set_baggage("client_id", cid)
    token = context.attach(ctx)
    try:
        from fashion_bot.utils.client_identity_cache import prewarm_client_name_cache
        prewarm_client_name_cache()
    except Exception as exc:
        _logger.debug("client_name cache prewarm failed: %s", exc)
    try:
        yield
    finally:
        try:
            context.detach(token)
        except Exception:
            # Detach can fail if the context was modified outside this
            # block (e.g. a nested `set_request_client_id` from a deeper
            # call). Telemetry-only — swallow so we never break the cron.
            pass


# ---------------------------------------------------------------------------
# FastAPI server-span enrichment for webhooks
# ---------------------------------------------------------------------------
_WEBHOOK_PATH_TYPES = (
    ("/webhook/products", "product"),
    ("/webhook/inventory", "inventory"),
    ("/webhook/abandoned", "abandoned_checkout"),
    ("/shopify/webhook", "order"),
    ("/shopify-webhook", "order"),
    ("/shiprocket", "shipping"),
)


def webhook_request_hook(span, scope) -> None:
    """``server_request_hook`` for ``FastAPIInstrumentor``.

    Adds ``webhook.type`` and ``webhook.topic`` attributes to the auto-generated
    HTTP server span so collector-side span-to-metrics processors can derive
    per-webhook RED metrics without per-handler decorators.
    """
    if span is None or not span.is_recording():
        return

    path = scope.get("path", "") or ""
    if "/webhook" not in path and "/shopify" not in path:
        return

    for prefix, webhook_type in _WEBHOOK_PATH_TYPES:
        if prefix in path:
            span.set_attribute("webhook.type", webhook_type)
            break

    for raw_name, raw_value in scope.get("headers") or []:
        try:
            name = raw_name.decode("latin-1").lower()
        except Exception:
            continue
        if name == "x-shopify-topic":
            try:
                span.set_attribute("webhook.topic", raw_value.decode("latin-1"))
            except Exception:
                pass
            break


# ---------------------------------------------------------------------------
# Webhook RED metrics with business dimensions (Shopify store, Shiprocket client)
# ---------------------------------------------------------------------------
# The auto-instrumented ``http.server.duration`` metric carries a FIXED label
# set (method, route/http_target, status_code) — the ``server_request_hook``
# above can only enrich the SPAN, not the metric. To slice the webhook
# dashboard by the Shopify store (``X-Shopify-Shop-Domain`` header) and the
# Shiprocket client_id (base64 segment in the shipping webhook path) we emit a
# dedicated webhook metric that carries those as labels.
#
# Emitted only for webhook requests (path contains "webhook") to bound
# cardinality. ``shopify_store`` and ``shiprocket_client_id`` are mutually
# exclusive per request (one is "none"), and each is expected to stay well
# under ~100 distinct values, so the series count is comfortable for Prometheus.
webhook_request_counter = meter.create_counter(
    "webhook.requests",
    description=(
        "Webhook HTTP requests, labelled by endpoint, status_code, webhook_type, "
        "shopify_store (X-Shopify-Shop-Domain) and shiprocket_client_id (decoded)."
    ),
)
webhook_request_duration = meter.create_histogram(
    "webhook.duration_milliseconds",
    description="Webhook handler wall-clock duration",
    unit="ms",
)


def _normalize_webhook_endpoint(path: str) -> str:
    """Collapse the high-cardinality base64 client-id segment in shipping paths.

    Mirrors the templated ``http_target`` the existing dashboard already shows,
    e.g. ``/shipping/event/webhook/<b64>`` -> ``/shipping/event/webhook/{encoded_client_id}``.
    All other webhook routes are already static and pass through unchanged.
    """
    for base in (
        "/shipping/delhivery/event/webhook/bulk",
        "/shipping/delhivery/event/webhook",
        "/shipping/event/webhook/bulk",
        "/shipping/event/webhook",
    ):
        if path == base:
            return base
        if path.startswith(base + "/"):
            return base + "/{encoded_client_id}"
    return path


def _webhook_type_for(endpoint: str) -> str:
    """Coarse webhook family for an already-normalized endpoint."""
    if endpoint.startswith("/shipping/delhivery"):
        return "shipping_delhivery"
    if endpoint.startswith("/shipping"):
        return "shipping_shiprocket"
    if endpoint.startswith("/product/webhook/inventory"):
        return "inventory"
    if endpoint.startswith("/product/webhook/products"):
        return "product"
    if endpoint.startswith("/order/webhook"):
        return "order"
    if endpoint.startswith("/cart/checkout/webhook") or "abandoned" in endpoint:
        return "abandoned_checkout"
    if endpoint.startswith("/gupshup/webhook"):
        return "gupshup"
    return "other"


def _extract_shiprocket_client_id(path: str) -> str:
    """Decode the actual Shiprocket client_id from the webhook path.

    Shiprocket posts to ``/shipping/event/webhook/<base64(client_id)>`` (and a
    ``/bulk/<...>`` variant). Returns the decoded client_id, or "none" for
    non-Shiprocket paths / the legacy path that omits the segment, or "invalid"
    if the segment will not decode.
    """
    for base in ("/shipping/event/webhook/bulk/", "/shipping/event/webhook/"):
        if path.startswith(base):
            seg = path[len(base):].split("/", 1)[0]
            if not seg:
                return "none"
            try:
                padded = seg + "=" * (-len(seg) % 4)
                decoded = base64.b64decode(padded).decode("utf-8").strip()
                return decoded or "invalid"
            except Exception:
                return "invalid"
    return "none"


def _record_webhook(scope, status_code: int, elapsed_s: float) -> None:
    """Emit the webhook counter + duration histogram for one request."""
    path = scope.get("path", "") or ""
    endpoint = _normalize_webhook_endpoint(path)

    shopify_store = "none"
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name == b"x-shopify-shop-domain":
            try:
                shopify_store = raw_value.decode("latin-1") or "none"
            except Exception:
                shopify_store = "none"
            break

    dims = {
        "endpoint": endpoint,
        "webhook_type": _webhook_type_for(endpoint),
        "shopify_store": shopify_store,
        "shiprocket_client_id": _extract_shiprocket_client_id(path),
    }
    # status_code only on the counter (errors/RED); kept off the histogram to
    # avoid multiplying latency series.
    webhook_request_counter.add(1, {**dims, "status_code": str(status_code)})
    webhook_request_duration.record(elapsed_s * 1000.0, dims)


class WebhookMetricsMiddleware:
    """Pure-ASGI middleware emitting webhook RED metrics with business dims.

    Lightweight on purpose: it never reads/buffers the request body (it passes
    ``receive`` through untouched) and only wraps ``send`` to capture the final
    status code. Non-webhook traffic short-circuits with zero overhead.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or "webhook" not in (scope.get("path") or ""):
            await self.app(scope, receive, send)
            return

        t0 = _time.monotonic()
        status_holder = {"code": 500}

        async def _send(message):
            if message.get("type") == "http.response.start":
                status_holder["code"] = message.get("status", 500)
            await send(message)

        try:
            await self.app(scope, receive, _send)
        finally:
            try:
                _record_webhook(scope, status_holder["code"], _time.monotonic() - t0)
            except Exception as exc:  # telemetry must never break the request
                _logger.debug("webhook metric record failed: %s", exc)


# ---------------------------------------------------------------------------
# Cron Job Metrics (the one signal not covered by an off-the-shelf instrumentor)
# ---------------------------------------------------------------------------
cron_run_counter = meter.create_counter(
    "cron.runs.total",
    description="Cron job execution count by job name and outcome",
)
cron_duration = meter.create_histogram(
    "cron.duration_seconds",
    description="Cron job wall-clock duration",
    unit="s",
)
cron_items_processed = meter.create_counter(
    "cron.items_processed",
    description="Items processed per cron run (users switched, conversations analysed, etc.)",
)


# ---------------------------------------------------------------------------
# Cron "last successful run" gauge — restart-durable liveness for sparse jobs
# ---------------------------------------------------------------------------
# ``cron.runs.total`` is a counter, so dashboards read it with
# rate()/increase(). That works for frequent jobs (e.g. auto_switch, hourly)
# but silently reads ZERO for low-frequency jobs: conversation_analytics runs
# twice a day, pods are recycled often, and a counter that only ever reaches 1
# before its pod is replaced exposes no scrapeable increment for increase() to
# resolve. The job is healthy, yet the panel shows 0 / "No data".
#
# Fix: also publish the absolute Unix epoch of each job's last SUCCESS as an
# observable gauge. A gauge of state (not a rate over a counter) is immune to
# pod churn and cadence — ``time() - max by (job_name)(cron_last_success_
# timestamp_seconds)`` yields "seconds since the job last succeeded", which is
# the signal you actually want to alert on.
#
# Durability: the timestamp is mirrored to Redis (shared by all pods — the
# same store the cron leases already use) so it survives restarts and any pod
# can report any job, including one this pod has not run since it booted (e.g.
# right after a redeploy, before a twice-daily job has fired). The in-process
# map is the fast path and the fallback when Redis is unavailable.
_REDIS_LAST_SUCCESS_PREFIX = "cron:last_success:"
# Long TTL so a job that stops running eventually drops off the gauge instead
# of pinning a stale "last success" forever; comfortably longer than the
# rarest cadence (weekly product vector sync).
_REDIS_LAST_SUCCESS_TTL_SECONDS = 60 * 60 * 24 * 40  # 40 days

_cron_last_success: dict = {}
_cron_last_success_lock = threading.Lock()

_REDIS_SUCCESS_COUNT_PREFIX = "cron:success_count:"
_REDIS_FAILURE_COUNT_PREFIX = "cron:failure_count:"
_REDIS_LAST_DURATION_PREFIX = "cron:last_duration:"
_REDIS_LAST_ITEMS_PREFIX = "cron:last_items:"

# Cumulative successful-run count and last-run duration per job — same
# restart-durable pattern as the timestamp above. Low-frequency jobs lose their
# run count to increase()/rate() (the counter never climbs past 1 before a pod
# recycles) and their duration to histogram rate() (too few samples to resolve).
# A Redis INCR gives a true cluster-wide cumulative count; the last duration is
# kept as a gauge so it stays visible between sparse runs.
_cron_success_count: dict = {}
_cron_failure_count: dict = {}
_cron_last_duration: dict = {}
_cron_last_items: dict = {}
_cron_stats_lock = threading.Lock()


def _mark_cron_success(job_name: str, duration_seconds: float, items_processed: float = 0.0) -> None:
    """Record a successful run: last-success epoch, cumulative count, duration, items.

    All are mirrored to Redis so they are restart-durable and cluster-wide
    (see the timestamp gauge rationale above). The count uses Redis INCR so it
    stays correct even when each pod runs the job only once before being
    recycled. ``items_processed`` is the work done by this run (0 = no-op),
    kept as a gauge so a short duration can be read as "nothing to do" rather
    than a measurement glitch.
    """
    now = _time.time()
    duration_seconds = float(duration_seconds)
    items_processed = float(items_processed)
    with _cron_last_success_lock:
        _cron_last_success[job_name] = now
    with _cron_stats_lock:
        _cron_success_count[job_name] = _cron_success_count.get(job_name, 0.0) + 1.0
        _cron_last_duration[job_name] = duration_seconds
        _cron_last_items[job_name] = items_processed
    try:
        from fashion_bot.utils.redis_client import get_shared_sync_redis_client

        client = get_shared_sync_redis_client()
        if client is not None:
            ttl = _REDIS_LAST_SUCCESS_TTL_SECONDS
            client.set(f"{_REDIS_LAST_SUCCESS_PREFIX}{job_name}", repr(now), ex=ttl)
            client.set(f"{_REDIS_LAST_DURATION_PREFIX}{job_name}", repr(duration_seconds), ex=ttl)
            client.set(f"{_REDIS_LAST_ITEMS_PREFIX}{job_name}", repr(items_processed), ex=ttl)
            count_key = f"{_REDIS_SUCCESS_COUNT_PREFIX}{job_name}"
            new_count = client.incr(count_key)
            client.expire(count_key, ttl)
            with _cron_stats_lock:
                _cron_success_count[job_name] = float(new_count)
    except Exception as exc:  # telemetry must never break a cron
        _logger.debug("cron success Redis persist failed (%s): %s", job_name, exc)


def _mark_cron_failure(job_name: str) -> None:
    """Record a failed run: durable cumulative failure count (in-process + Redis)."""
    with _cron_stats_lock:
        _cron_failure_count[job_name] = _cron_failure_count.get(job_name, 0.0) + 1.0
    try:
        from fashion_bot.utils.redis_client import get_shared_sync_redis_client

        client = get_shared_sync_redis_client()
        if client is not None:
            count_key = f"{_REDIS_FAILURE_COUNT_PREFIX}{job_name}"
            new_count = client.incr(count_key)
            client.expire(count_key, _REDIS_LAST_SUCCESS_TTL_SECONDS)
            with _cron_stats_lock:
                _cron_failure_count[job_name] = float(new_count)
    except Exception as exc:  # telemetry must never break a cron
        _logger.debug("cron failure Redis persist failed (%s): %s", job_name, exc)


def _hydrate_cron_last_success_from_redis() -> None:
    """Merge cluster-wide last-success epochs from Redis into the local map."""
    try:
        from fashion_bot.utils.redis_client import get_shared_sync_redis_client

        client = get_shared_sync_redis_client()
        if client is None:
            return
        updates: dict = {}
        for key in client.scan_iter(match=f"{_REDIS_LAST_SUCCESS_PREFIX}*", count=100):
            key_str = key if isinstance(key, str) else key.decode("utf-8", "ignore")
            raw = client.get(key)
            if raw is None:
                continue
            try:
                updates[key_str[len(_REDIS_LAST_SUCCESS_PREFIX):]] = float(raw)
            except (TypeError, ValueError):
                continue
        if updates:
            with _cron_last_success_lock:
                for job, epoch in updates.items():
                    if epoch > _cron_last_success.get(job, 0.0):
                        _cron_last_success[job] = epoch
    except Exception as exc:
        _logger.debug("cron last-success Redis hydrate failed: %s", exc)


def _observe_cron_last_success(options):
    """Observable-gauge callback: one observation per job (value = last-success epoch).

    Invoked by the metric reader once per export interval. Refreshes from Redis
    first so the value is durable and cluster-wide, then yields the snapshot.
    """
    _hydrate_cron_last_success_from_redis()
    with _cron_last_success_lock:
        snapshot = dict(_cron_last_success)
    return [Observation(epoch, {"job_name": job}) for job, epoch in snapshot.items()]


cron_last_success_timestamp = meter.create_observable_gauge(
    "cron.last_success.timestamp_seconds",
    callbacks=[_observe_cron_last_success],
    description=(
        "Unix epoch (seconds) of each cron job's most recent successful run. "
        "Restart-durable gauge (mirrored via Redis) so low-frequency jobs that "
        "rate()/increase() over cron.runs.total cannot resolve still show "
        "liveness. Alert on `time() - max by (job_name)(this)`."
    ),
    unit="s",
)


def _hydrate_overwrite_from_redis(prefix: str, dest: dict, lock) -> None:
    """Adopt the latest cluster-wide values for ``prefix`` keys into ``dest``.

    Redis is authoritative (INCR for counts, last-writer-wins SET for the
    duration), so values are overwritten rather than max-merged.
    """
    try:
        from fashion_bot.utils.redis_client import get_shared_sync_redis_client

        client = get_shared_sync_redis_client()
        if client is None:
            return
        updates: dict = {}
        for key in client.scan_iter(match=f"{prefix}*", count=100):
            key_str = key if isinstance(key, str) else key.decode("utf-8", "ignore")
            raw = client.get(key)
            if raw is None:
                continue
            try:
                updates[key_str[len(prefix):]] = float(raw)
            except (TypeError, ValueError):
                continue
        if updates:
            with lock:
                dest.update(updates)
    except Exception as exc:
        _logger.debug("cron Redis hydrate failed (%s): %s", prefix, exc)


def _observe_cron_success_count(options):
    _hydrate_overwrite_from_redis(_REDIS_SUCCESS_COUNT_PREFIX, _cron_success_count, _cron_stats_lock)
    with _cron_stats_lock:
        snapshot = dict(_cron_success_count)
    return [Observation(v, {"job_name": j}) for j, v in snapshot.items()]


def _observe_cron_last_duration(options):
    _hydrate_overwrite_from_redis(_REDIS_LAST_DURATION_PREFIX, _cron_last_duration, _cron_stats_lock)
    with _cron_stats_lock:
        snapshot = dict(_cron_last_duration)
    return [Observation(v, {"job_name": j}) for j, v in snapshot.items()]


def _observe_cron_last_items(options):
    _hydrate_overwrite_from_redis(_REDIS_LAST_ITEMS_PREFIX, _cron_last_items, _cron_stats_lock)
    with _cron_stats_lock:
        snapshot = dict(_cron_last_items)
    return [Observation(v, {"job_name": j}) for j, v in snapshot.items()]


cron_success_count_gauge = meter.create_observable_gauge(
    "cron.success.count",
    callbacks=[_observe_cron_success_count],
    description=(
        "Restart-durable cumulative count of successful runs per cron job "
        "(Redis INCR). Use `max by (job_name)(cron_success_count)` for a true "
        "run count of low-frequency jobs that increase() over cron.runs.total "
        "under-counts due to pod churn."
    ),
)


def _observe_cron_failure_count(options):
    _hydrate_overwrite_from_redis(_REDIS_FAILURE_COUNT_PREFIX, _cron_failure_count, _cron_stats_lock)
    with _cron_stats_lock:
        snapshot = dict(_cron_failure_count)
    return [Observation(v, {"job_name": j}) for j, v in snapshot.items()]


cron_failure_count_gauge = meter.create_observable_gauge(
    "cron.failure.count",
    callbacks=[_observe_cron_failure_count],
    description=(
        "Restart-durable cumulative count of FAILED runs per cron job (Redis "
        "INCR). Counterpart to cron.success.count so failure totals and success "
        "rate are correct for sparse jobs too."
    ),
)

cron_last_duration_gauge = meter.create_observable_gauge(
    "cron.last_duration_seconds",
    callbacks=[_observe_cron_last_duration],
    description=(
        "Wall-clock duration (seconds) of each cron job's most recent "
        "successful run. Restart-durable gauge so duration stays visible "
        "between sparse runs that histogram rate() cannot resolve."
    ),
    unit="s",
)

cron_last_items_gauge = meter.create_observable_gauge(
    "cron.last_items_processed",
    callbacks=[_observe_cron_last_items],
    description=(
        "Items processed by each cron job's most recent successful run (users "
        "switched, conversations analysed, etc.). Restart-durable gauge; pairs "
        "with cron.last_duration_seconds so a short run reads as 'nothing to "
        "do' (0 items) rather than a measurement glitch."
    ),
)


def track_cron(
    job_name: str,
    *,
    items_key: Optional[Union[str, Sequence[str]]] = None,
):
    """Decorator that records cron-job OTel metrics (sync + async aware)."""

    def decorator(fn):
        is_async = inspect.iscoroutinefunction(fn)

        @functools.wraps(fn)
        async def _async_wrapper(*args, **kwargs):
            t0 = _time.monotonic()
            try:
                result = await fn(*args, **kwargs)
            except Exception:
                _record_cron(job_name, None, t0, items_key, failure=True)
                raise
            _record_cron(job_name, result, t0, items_key)
            return result

        @functools.wraps(fn)
        def _sync_wrapper(*args, **kwargs):
            t0 = _time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception:
                _record_cron(job_name, None, t0, items_key, failure=True)
                raise
            _record_cron(job_name, result, t0, items_key)
            return result

        return _async_wrapper if is_async else _sync_wrapper

    return decorator


def _record_cron(job_name, result, t0, items_key, *, failure: bool = False) -> None:
    elapsed = _time.monotonic() - t0
    success = (not failure) and isinstance(result, dict) and result.get("success", False)

    # Lock-skips are NOT runs. Leader-locked jobs (conversation_analytics,
    # product_vector_sync) fire on every pod but only one acquires the Redis
    # lease; the losers return {"success": True, "skipped": True}. Counting
    # those as successful runs inflates the count by the pod count (e.g. 4
    # instead of 1 per scheduled fire), so treat a skip as a no-op: no run
    # count, no duration, no items, no liveness bump.
    if isinstance(result, dict) and result.get("skipped"):
        return

    status = "success" if success else "failure"

    labels = {"job_name": job_name, "status": status}
    cron_run_counter.add(1, labels)
    cron_duration.record(elapsed, labels)

    # Items processed this run (0 when the job had nothing to do).
    items_total = 0.0
    if success and items_key and isinstance(result, dict):
        keys = [items_key] if isinstance(items_key, str) else list(items_key)
        items_total = float(sum(result.get(k, 0) for k in keys))

    if success:
        # Restart-durable signals (liveness, run count, duration, items) for
        # sparse jobs — see cron.last_success.timestamp_seconds / cron.success.count.
        _mark_cron_success(job_name, elapsed, items_total)
    else:
        # Durable failure count so failure totals / success rate are correct
        # for sparse jobs too — see cron.failure.count.
        _mark_cron_failure(job_name)

    if success and items_total:
        cron_items_processed.add(items_total, {"job_name": job_name})


# ---------------------------------------------------------------------------
# LLM Callback Handler — records Prometheus metrics for every LangChain call
# ---------------------------------------------------------------------------

class OTelLLMCallbackHandler(BaseCallbackHandler):
    """LangChain callback handler that records LLM metrics via OTel.

    Reads ``client_id`` from OTel baggage (set by :func:`set_request_client_id`
    at request entry points) so every metric is labelled with the originating
    client.
    """

    raise_error = False
    # Required by langchain-core>=0.3 callback manager (manager.py reads
    # handler.run_inline). Defaulting to False keeps callbacks dispatched
    # via the standard async executor, matching BaseCallbackHandler.
    run_inline = False

    def __init__(self):
        super().__init__()
        self._call_start: dict = {}

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._call_start[str(run_id)] = _time.monotonic()

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self._call_start[str(run_id)] = _time.monotonic()

    def on_llm_end(self, response, *, run_id, **kwargs):
        rid = str(run_id)
        t0 = self._call_start.pop(rid, None)
        elapsed_ms = ((_time.monotonic() - t0) * 1000) if t0 else 0

        client_id = get_request_client_id()
        caller = get_llm_caller()

        usage = _extract_llm_usage(response)
        model = usage.model
        prompt_tok = usage.prompt_tokens
        completion_tok = usage.completion_tokens

        labels = {
            "client_id": client_id,
            "client_name": _client_name_label(client_id),
            "model": model,
            "caller": caller,
        }
        llm_call_counter.add(1, labels)
        llm_call_duration.record(elapsed_ms, labels)
        if prompt_tok:
            llm_prompt_tokens.add(prompt_tok, labels)
        if completion_tok:
            llm_completion_tokens.add(completion_tok, labels)

        # Cost: the provider's actual charge when it reported one (OpenRouter
        # returns ``usage.cost`` on the non-streaming path), else the per-model
        # price-table estimate. ``cost_source`` lets dashboards separate billed
        # truth from estimate instead of silently mixing them.
        if usage.actual_cost_usd is not None:
            cost_usd = usage.actual_cost_usd
            cost_source = "actual"
        else:
            cost_usd = _estimate_llm_cost_usd(model, prompt_tok, completion_tok)
            cost_source = "estimated"
        if cost_usd > 0:
            llm_cost_usd_counter.add(cost_usd, {**labels, "cost_source": cost_source})

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._call_start.pop(str(run_id), None)
        client_id = get_request_client_id()
        caller = get_llm_caller()
        error_type = type(error).__name__ if error else "unknown"
        llm_error_counter.add(
            1,
            {
                "client_id": client_id,
                "client_name": _client_name_label(client_id),
                "error_type": error_type,
                "caller": caller,
            },
        )


_llm_handler_singleton = None
_llm_handler_lock = threading.Lock()


def get_llm_callback_handler() -> Optional[OTelLLMCallbackHandler]:
    """Return a singleton callback handler (lazy-init, thread-safe)."""
    global _llm_handler_singleton
    if _llm_handler_singleton is None:
        with _llm_handler_lock:
            if _llm_handler_singleton is None:
                _llm_handler_singleton = OTelLLMCallbackHandler()
    return _llm_handler_singleton
