"""Env-driven configuration for the webhook/background-task queue.

Centralizes every knob from §15 of the design doc so the producer, the broker,
the actors and the worker entrypoint all agree. Importing this module is cheap
(no ``dramatiq`` import) so it is safe on the web/producer side.
"""
from __future__ import annotations

from fashion_bot.env_loader import get_bool, get_env, get_float, get_int

# ── Master switch ─────────────────────────────────────────────────────────
# Default OFF → deploying this code changes nothing until the flag is set and
# workers are running. submit_or_inline() runs the legacy inline path when off.
WEBHOOK_QUEUE_ENABLED: bool = get_bool("WEBHOOK_QUEUE_ENABLED", False)

# Dedicated opt-in for the Fastrr/Shiprocket abandon-cart queue. Default OFF so
# the webhook calls Gupshup inline until this lane is deliberately enabled.
SHIPROCKET_CART_QUEUE_ENABLED: bool = get_bool("SHIPROCKET_CART_QUEUE_ENABLED", False)

# Comma list of lanes routed through the queue when the master switch is on.
# Lets us enable one lane at a time (inventory first — it was the storm source).
# Tokens match the job types below, e.g. "inventory,product".
WEBHOOK_QUEUE_LANES: str = (get_env("WEBHOOK_QUEUE_LANES", "inventory,product") or "").strip()

# ── Broker ────────────────────────────────────────────────────────────────
# Dedicated persistent Redis/Valkey (Render Key Value). NEVER the Upstash
# state/cache instance. Set in the service env, e.g.
#   rediss://red-xxxx:<password>@singapore-keyvalue.render.com:6379
# Falls back to REDIS_URL only for local dev convenience.
DRAMATIQ_BROKER_URL: str | None = (
    get_env("DRAMATIQ_BROKER_URL") or get_env("REDIS_URL")
)

# ── Queue names (lanes) ───────────────────────────────────────────────────
QUEUE_SHOPIFY_INVENTORY = "webhooks.shopify.inventory"
QUEUE_SHOPIFY_PRODUCT = "webhooks.shopify.product"
QUEUE_SHOPIFY_ORDER = "webhooks.shopify.order"
QUEUE_SHOPIFY_CART = "webhooks.shopify.cart"
QUEUE_SHIPROCKET = "webhooks.shiprocket"
QUEUE_SHIPROCKET_CART = "webhooks.shiprocket.cart"
QUEUE_DELHIVERY = "webhooks.delhivery"
QUEUE_GUPSHUP_EVENTS = "events.gupshup"
QUEUE_CONVERSATION_EVENTS = "events.conversations"
QUEUE_ESCALATION_EVENTS = "events.escalations"
# Escalation DELIVERY lanes (one per channel) — separate from the audit-log
# ``events.escalations`` lane above. Each channel is isolated so a slow/failing
# channel never blocks or retries the other (design §5.2a).
QUEUE_ESCALATION_WHATSAPP = "events.escalation_whatsapp"
QUEUE_ESCALATION_EMAIL = "events.escalation_email"
# Heavy cron offload: the weekly product delta sync runs here (on
# queue-background-worker), NOT inline on the webhook tier. One message per
# client; the worker processes them serially and frees memory between clients.
QUEUE_PRODUCT_SYNC = "cron.product.sync"

# ── Job types (transport-stable identifiers) ──────────────────────────────
JOB_PRODUCT_UPSERT = "product_upsert"
JOB_PRODUCT_DELETE = "product_delete"
JOB_INVENTORY_UPDATE = "inventory_update"
JOB_ORDER_EVENT = "order_event"
JOB_CART_EVENT = "cart_event"
JOB_SHIPROCKET_EVENT = "shiprocket_event"
JOB_SHIPROCKET_CART_EVENT = "shiprocket_cart_event"
JOB_DELHIVERY_EVENT = "delhivery_event"
JOB_GUPSHUP_EVENT = "gupshup_event"
JOB_CONVERSATION_SCAN_EVENT = "conversation_scan_event"
JOB_CONVERSATION_CREATED_EVENT = "conversation_created_event"
JOB_CONVERSATION_INACTIVITY_EVENT = "conversation_inactivity_event"
JOB_ESCALATION_EVENT = "escalation_event"
JOB_ESCALATION_WHATSAPP = "escalation_whatsapp"
JOB_ESCALATION_EMAIL = "escalation_email"
JOB_PRODUCT_DELTA_SYNC = "product_delta_sync"

# Which lane token (for WEBHOOK_QUEUE_LANES) each job belongs to.
JOB_LANE = {
    JOB_PRODUCT_UPSERT: "product",
    JOB_PRODUCT_DELETE: "product",
    JOB_INVENTORY_UPDATE: "inventory",
    JOB_ORDER_EVENT: "order",
    JOB_CART_EVENT: "cart",
    JOB_SHIPROCKET_EVENT: "shiprocket",
    JOB_SHIPROCKET_CART_EVENT: "shiprocket_cart",
    JOB_DELHIVERY_EVENT: "delhivery",
    JOB_GUPSHUP_EVENT: "gupshup_event",
    JOB_CONVERSATION_SCAN_EVENT: "conversation_event",
    JOB_CONVERSATION_CREATED_EVENT: "conversation_event",
    JOB_CONVERSATION_INACTIVITY_EVENT: "conversation_event",
    JOB_ESCALATION_EVENT: "escalation_event",
    JOB_ESCALATION_WHATSAPP: "escalation_whatsapp",
    JOB_ESCALATION_EMAIL: "escalation_email",
    JOB_PRODUCT_DELTA_SYNC: "product_sync",
}

# ── Enqueue resilience / inline fallback (§7.1) ───────────────────────────
WEBHOOK_ENQUEUE_MAX_ATTEMPTS: int = get_int("WEBHOOK_ENQUEUE_MAX_ATTEMPTS", 3)
WEBHOOK_ENQUEUE_BACKOFF_BASE: float = get_float("WEBHOOK_ENQUEUE_BACKOFF_BASE", 0.2)
# Bounded inline fallback: when the broker is unreachable we still process
# inline (never drop a webhook) but cap concurrency well under DB_POOL_MAX so a
# broker outage during a storm cannot reproduce the original pool exhaustion.
WEBHOOK_INLINE_MAX_CONCURRENCY: int = get_int("WEBHOOK_INLINE_MAX_CONCURRENCY", 5)

# ── Per-actor retry / time limits ─────────────────────────────────────────
WEBHOOK_JOB_MAX_RETRIES: int = get_int("WEBHOOK_JOB_MAX_RETRIES", 5)
WEBHOOK_JOB_MIN_BACKOFF_MS: int = get_int("WEBHOOK_JOB_MIN_BACKOFF_MS", 500)
WEBHOOK_JOB_MAX_BACKOFF_MS: int = get_int("WEBHOOK_JOB_MAX_BACKOFF_MS", 300_000)
WEBHOOK_JOB_TIME_LIMIT_MS: int = get_int("WEBHOOK_JOB_TIME_LIMIT_MS", 120_000)

# ── Product delta-sync actor limits (heavy, long-running) ─────────────────
# A single client's weekly delta sync (fetch + OCR + LLM + upsert) can run for
# many minutes, so it needs a far larger time limit than a webhook job. Retries
# are kept low: the sync is content-hash idempotent, but re-running a heavy
# sweep on transient failure is expensive — let the next weekly run reconcile.
PRODUCT_SYNC_JOB_MAX_RETRIES: int = get_int("PRODUCT_SYNC_JOB_MAX_RETRIES", 1)
PRODUCT_SYNC_JOB_TIME_LIMIT_MS: int = get_int("PRODUCT_SYNC_JOB_TIME_LIMIT_MS", 1_800_000)  # 30 min

# ── Idempotency (§10) ─────────────────────────────────────────────────────
WEBHOOK_DEDUP_TTL_SECONDS: int = get_int("WEBHOOK_DEDUP_TTL_SECONDS", 86_400)

# ── Queue-depth observability exporter (§17.1.2) ──────────────────────────
QUEUE_DEPTH_POLL_SECONDS: float = get_float("WEBHOOK_QUEUE_DEPTH_POLL_SECONDS", 30.0)
ALL_QUEUES = [
    QUEUE_SHOPIFY_INVENTORY,
    QUEUE_SHOPIFY_PRODUCT,
    QUEUE_SHOPIFY_ORDER,
    QUEUE_SHOPIFY_CART,
    QUEUE_SHIPROCKET,
    QUEUE_SHIPROCKET_CART,
    QUEUE_DELHIVERY,
    QUEUE_GUPSHUP_EVENTS,
    QUEUE_CONVERSATION_EVENTS,
    QUEUE_ESCALATION_EVENTS,
    QUEUE_ESCALATION_WHATSAPP,
    QUEUE_ESCALATION_EMAIL,
    QUEUE_PRODUCT_SYNC,
]


def lane_enabled(job_type: str) -> bool:
    """True if this job type's lane is routed through the queue."""
    if not WEBHOOK_QUEUE_ENABLED:
        return False
    if job_type == JOB_SHIPROCKET_CART_EVENT and not SHIPROCKET_CART_QUEUE_ENABLED:
        return False
    lanes = {tok.strip() for tok in WEBHOOK_QUEUE_LANES.split(",") if tok.strip()}
    if not lanes:  # flag on but no lanes listed → treat as all lanes on
        return True
    return JOB_LANE.get(job_type, job_type) in lanes
