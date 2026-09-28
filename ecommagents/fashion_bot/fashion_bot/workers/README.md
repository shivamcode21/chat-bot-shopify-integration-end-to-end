# Webhook / background-task queue (Dramatiq)

Implementation of `design_docs/WEBHOOK_QUEUE_DRAMATIQ.md`. Offloads heavy webhook
work to dedicated worker processes so a Shopify storm can't exhaust the DB pool
(the 502 incident). **Default OFF** — deploying changes nothing until enabled.

## Safety / no-break contract

- Webhook routes import only `submit_or_inline` (`enqueue.py`), which is
  `dramatiq`-free at import. With `WEBHOOK_QUEUE_ENABLED` unset/false it simply
  `await`s the existing inline handler — byte-for-byte the legacy path. Dramatiq
  need not even be importable in that state.
- When a lane is enabled but the broker is unreachable, processing falls back to
  **bounded** inline execution (`WEBHOOK_INLINE_MAX_CONCURRENCY`), so a broker
  outage during a storm can't reproduce the original pool exhaustion; beyond the
  cap it defers and lets Shopify retry.
- Workers run the **existing** handlers unchanged; idempotency dedups Shopify
  retries (`idempotency.py`).

## Rollout

1. Deploy with `WEBHOOK_QUEUE_ENABLED=false` (default). Provision the broker.
2. Create the per-lane worker services (below); they idle until enabled.
3. Set `WEBHOOK_QUEUE_ENABLED=true` and `WEBHOOK_QUEUE_LANES=inventory` to enable
   the burstiest lane first. Widen to `inventory,product` once healthy.
4. Rollback at any time: `WEBHOOK_QUEUE_ENABLED=false` → inline behaviour returns.

## Lanes

| Lane token | Queue | Wraps |
|---|---|---|
| `inventory` | `webhooks.shopify.inventory` | `_handle_inventory_level_update` |
| `product` | `webhooks.shopify.product` | `handle_product_upsert` / `handle_product_delete` |
| `order` | `webhooks.shopify.order` | `_process_order_event` (process + conversation inject + attribution) |
| `cart` | `webhooks.shopify.cart` | abandoned-checkout Gupshup send |
| `shiprocket` | `webhooks.shiprocket` | Shiprocket `process_webhook_event` |
| `shiprocket_cart` | `webhooks.shiprocket.cart` | Fastrr/Shiprocket abandon-cart Gupshup send |
| `delhivery` | `webhooks.delhivery` | Delhivery `process_webhook_event` |
| `gupshup_event` | `events.gupshup` | Gupshup delivery/failure event storage |
| `conversation_event` | `events.conversations` | conversation scan + created events |
| `escalation_event` | `events.escalations` | escalation raised events (audit log) |
| `escalation_whatsapp` | `events.escalation_whatsapp` | escalation WhatsApp delivery (`asend_escalation_whatsapp`) |
| `escalation_email` | `events.escalation_email` | escalation email delivery (`asend_escalation_email`) |

A lane is only offloaded when **both** `WEBHOOK_QUEUE_ENABLED=true` **and** the lane
token is in `WEBHOOK_QUEUE_LANES`, **and** a worker consumes its queue. Otherwise
the webhook runs inline exactly as before. `order`/`cart` dedup on the Shopify
webhook id; `shiprocket`/`delhivery` rely on their processors' own dedup (the
same order legitimately emits many status events). `order`/`cart`/`shiprocket`/
`delhivery` actors do **not** retry on a logical `success: False` (only real
exceptions retry) to avoid dead-lettering benign "not handled" events.

## Worker services (Render Background Workers, same image)

```
# burstiest / heaviest get their own services
dramatiq fashion_bot.workers.run --queues webhooks.shopify.inventory --threads 8
dramatiq fashion_bot.workers.run --queues webhooks.shopify.product   --threads 4
# customer-facing lanes can share one service (or split as volume grows)
dramatiq fashion_bot.workers.run --queues webhooks.shopify.order webhooks.shopify.cart webhooks.shiprocket webhooks.delhivery --threads 4
# Fastrr/Shiprocket cart lane can run separately from Shopify cart
dramatiq fashion_bot.workers.run --queues webhooks.shiprocket.cart --threads 1
# internal event lanes can share the same customer-facing service
dramatiq fashion_bot.workers.run --queues webhooks.shopify.order webhooks.shopify.cart webhooks.shiprocket webhooks.delhivery events.gupshup events.conversations events.escalations events.escalation_whatsapp events.escalation_email --threads 4
```

Set per worker service: `DRAMATIQ_BROKER_URL`, `WEBHOOK_QUEUE_ENABLED=true`, a
small `DB_POOL_MAX` (e.g. 8, ≥ threads), and the usual app env (DB, Upstash
`REDIS_URL`, OTEL).

## Broker

Dedicated **Render Key Value** (paid, `maxmemory-policy=noeviction`) — NOT the
Upstash cache. Configure via `DRAMATIQ_BROKER_URL`, e.g.
`rediss://red-xxxx:<password>@singapore-keyvalue.render.com:6379`. **Never commit
the credentialed URL** — set it in the service environment only.

## Key env vars

| Var | Default | Purpose |
|---|---|---|
| `WEBHOOK_QUEUE_ENABLED` | `false` | master switch (enqueue vs inline) |
| `WEBHOOK_QUEUE_LANES` | `inventory,product` | lanes routed through the queue (add `order,cart,shiprocket,shiprocket_cart,delhivery,gupshup_event,conversation_event,escalation_event,escalation_whatsapp,escalation_email` to enable the rest) |
| `SHIPROCKET_CART_QUEUE_ENABLED` | `false` | dedicated switch for Fastrr/Shiprocket abandon-cart enqueue; when false it sends inline even if general queues are enabled |
| `DRAMATIQ_BROKER_URL` | `REDIS_URL` | dedicated broker (set explicitly in prod) |
| `WEBHOOK_INLINE_MAX_CONCURRENCY` | `5` | bounded inline-fallback cap |
| `WEBHOOK_JOB_MAX_RETRIES` | `5` | per-actor retries before dead-letter |
| `WEBHOOK_DEDUP_TTL_SECONDS` | `86400` | idempotency guard TTL (Upstash) |
| `WEBHOOK_QUEUE_DEPTH_POLL_SECONDS` | `30` | queue-depth exporter interval |
| `WEBHOOK_QUEUE_NAMESPACE` | `dramatiq` | broker key namespace for depth probe |

Full table: design doc §15.

## Observability (§17)

- `OpenTelemetryMiddleware` (traces + metrics) and the queue-depth exporter run
  in every worker; all signals flow through the **existing OTLP pipeline** (no
  new scrape infra). Worker OTel providers are initialized in `run.py`.
- Metrics: `dramatiq.task.duration` (histogram), `dramatiq.task.success/.failure/
  .retry` (counters), `dramatiq.queue.depth` (gauge), plus producer counters
  `webhook.enqueue.queued/.failed`, `webhook.inline.fallback/.deferred`.
- Import the dashboard `monitoring/grafana/dramatiq_webhook_pipeline.json`.
- Inspect the broker: `python -m fashion_bot.workers.dlq stats`.

### Suggested Grafana alerts (§17.5)

| Alert | Expression (PromQL) |
|---|---|
| Backlog not draining | `sum(dramatiq_queue_depth) > 1000 for 10m` (also the autoscale trigger) |
| Failures rising | `sum(rate(dramatiq_task_failure_total[5m])) > 0.1` |
| Broker memory high | `render_service_memory_usage_bytes / <maxmemory_bytes> > 0.7` |
| Broker trouble | `sum(rate(webhook_enqueue_failed_total[5m])) > 0` |

Metric names assume the OTLP→Prometheus convention (dots→underscores, counters
get `_total`, the ms histogram gets `_milliseconds`). Adjust to match your
collector if needed.
