# AGENTS.md - Development Guidelines

> Development standards and architectural principles for the Fashion Bot e-commerce agent platform.

---

## Core Principles

### 1. Full Async - No Exceptions

Every integration, adapter, service call, and I/O operation **must** be fully async (`async def` / `await`). No synchronous blocking calls are permitted in the hot path.

- Use `async def` for all handler functions, tool implementations, and service methods.
- Use `await` for all I/O: HTTP calls, database queries, Redis operations, file reads.
- Use `asyncio.gather()` for concurrent independent operations (e.g., running multiple enrichers in parallel).
- Use `asyncio.Lock` or Redis distributed locks for mutual exclusion — never `threading.Lock`.
- All new adapters must implement async interfaces (`aget_*`, `aupdate_*`, `asearch_*`).
- Background work should use the async summary worker queue or APScheduler — never `threading.Thread`.

**Why**: Sync calls block the event loop, degrade throughput under concurrency, and have caused production bottlenecks (see `design_docs/LOAD_TEST_IO_BOTTLENECK_REPORT.md`).

```python
# Correct
async def aget_orders_by_phone(self, phone: str) -> List[Order]:
    async with self.session.get(url) as resp:
        return await resp.json()

# Wrong - blocks the event loop
def get_orders_by_phone(self, phone: str) -> List[Order]:
    return requests.get(url).json()
```

---

### 2. Tools Are Stateless - No State Mutation

Tools (adapters, processors, enrichers) **must not** mutate shared state, conversation state, or any global mutable object. They receive input, produce output, and return it. State mutations happen **only** in the runtime layer (`ConversationRuntime`, `StateCache`).

- Tools receive data through function arguments and return results.
- Tools must **never** write directly to Redis, database, or conversation state.
- Tools must **never** hold references to mutable shared objects between invocations.
- Side effects (API calls, webhooks) are acceptable — state mutation is not.
- The `StateCache` and `ConversationRuntime` are the **only** components allowed to persist state changes.

**Why**: Stateful tools create hidden coupling, make testing impossible without mocking global state, and produce race conditions under concurrent execution.

```python
# Correct - stateless tool
async def enrich_order(self, order: dict) -> dict:
    tracking = await self._fetch_tracking(order["id"])
    return {**order, "tracking": tracking}

# Wrong - mutates shared state
async def enrich_order(self, order: dict) -> dict:
    tracking = await self._fetch_tracking(order["id"])
    self.state["orders"][order["id"]]["tracking"] = tracking  # NO
```

---

### 3. Three-Tier Caching for Config I/O

All configuration reads (client config, vendor config, prompt templates, feature flags) **must** use the three-tier cache pattern:

```
Tier 1: In-memory TTL cache (process-local, ~10 min TTL)
    ↓ miss
Tier 2: Redis (distributed, Upstash)
    ↓ miss
Tier 3: PostgreSQL (authoritative source of truth)
```

- Use `tiered_cache.py` for all config reads.
- Never hit the database directly for config that could be cached.
- Cache invalidation flows top-down: write to DB, then bust Redis, then local cache expires via TTL.
- Redis failures must degrade gracefully (fail-open) — fall back to local cache or DB.

**Why**: Config reads are high-frequency, low-change. Hitting the DB on every request wastes resources and adds latency. The tiered approach keeps p99 latency low while ensuring eventual consistency.

---

### 4. AI Pre-Merge Review (Mandatory)

Every PR **must** pass an AI code-review pass (Claude / Copilot / equivalent) on the diff before requesting a human reviewer. The AI reviewer's checklist for this codebase:

1. **No new event loops.** Cron and background work must run on the API event loop via `AsyncIOScheduler` — no `asyncio.run(...)`, no `loop.run_until_complete(...)`, no per-call `new_event_loop()`.
2. **DB reads are cached.** Any read from `clients`, `client_configs`, `gupshup_templates`, `agents_config`, or similar low-change config tables must go through `aget_with_tiered_cache` (memory → Redis → DB). New direct `await cur.execute("SELECT ... FROM clients ...")` calls without a cache wrapper are a blocker.
3. **Client/tenant resolution is mandatory.** When `client_id` cannot be resolved from a webhook / WS / API call, the path must either (a) match an explicit allowlist (and only then log at DEBUG), or (b) escalate via `report_error(..., level="error")`. Silent `WARNING` returns are not acceptable — they hide real tenant misconfiguration.
4. **One async client per resource type.** Shared `httpx.AsyncClient` lives in `utils/http_client.py`; shared `redis.asyncio.Redis` lives in `utils/redis_client.py`. Do not introduce a per-module `_get_async_redis_client` / `_shared_async_http_client` duplicate — extend the shared helper instead.
5. **Trace-ID propagation** (see §5 below).
6. **UI / widget lifecycle.** Any PR touching the chat widget's WebSocket, message queue, or response handling must pass the *UI / Widget Code Review — Must Check (WebSocket & Message/Response Lifecycle)* checklist below.
7. **No `asyncio.Lock` for lazy singleton init.** Locks are loop-affine and break when a second loop touches them. Accept the benign double-init race instead (covered by `database_manager.get_async_pool` and the shared Redis client).
8. **Cron jobs are guarded + single-pod.** A new `AsyncIOScheduler` job must be registered via `_guarded(...)` (heavy-job semaphore) and acquire a Redis lease (`cron_lock`) in its body so it can't overlap itself or run on every pod — see Core Principle 8.

Capture the AI review summary (which items passed, which were waived and why) in the PR description before requesting human review.

---

### 5. Trace ID in Every Log Line

All log output **must** include a `trace_id` for end-to-end request tracing and debugging.

- Use the `TraceIdFilter` to inject trace IDs into log records automatically.
- Every request entering the system (webhook, WebSocket, API) must generate or propagate a trace ID.
- Pass `trace_id` through the full call chain — it must appear in LangSmith traces, Rollbar errors, and application logs.
- Use structured logging (JSON format) with consistent field names: `trace_id`, `client_id`, `channel`, `user_id`.
- Log levels: `DEBUG` for internal flow, `INFO` for business events, `WARNING` for recoverable issues, `ERROR` for failures, `CRITICAL` for state corruption or data integrity issues.

```python
logger.info("Order lookup completed",
    extra={"trace_id": trace_id, "client_id": client_id, "order_count": len(orders)})
```

**Why**: Without trace IDs, debugging multi-tenant, multi-channel, async systems is impossible. A single trace ID must connect the webhook request to the LLM call to the Shopify API call to the response.

---

### 6. Dramatiq Queue ↔ Consumer Mapping (One Lane Per Job Type)

The background/webhook pipeline uses a strict one-to-one lane model. Every job type maps to **exactly one queue (lane) and one actor** — never multiplex job types onto a shared queue or fan one queue out to multiple actors.

- Lane definitions are centralized in `workers/config.py` (`QUEUE_*`, `JOB_*`, `JOB_LANE`). Add new lanes there — never hardcode a queue name in an actor or producer.
- The producer reaches the queue **only** via `workers/enqueue.submit_or_inline`. With `WEBHOOK_QUEUE_ENABLED` off (or a lane not listed in `WEBHOOK_QUEUE_LANES`), it runs the legacy inline path byte-for-byte.
- A worker process consumes one lane at a time, e.g. `dramatiq fashion_bot.workers.run --queues webhooks.shopify.inventory`. Scale a lane by adding worker replicas, not by merging lanes.
- Every actor must be **idempotent** — webhooks redeliver. Use the dedup key (`WEBHOOK_DEDUP_TTL_SECONDS`); never assume exactly-once delivery.
- A broker outage must never drop a webhook: fall back to the bounded inline path (`WEBHOOK_INLINE_MAX_CONCURRENCY`), capped well under the DB pool.

**Why**: One-to-one lanes give per-lane isolation, backpressure, and independent scaling. The inventory storm that motivated the queue (see `LOAD_TEST_IO_BOTTLENECK_REPORT.md`) is only contained if a noisy lane cannot starve the others.

---

### 7. Tenant Isolation Is Sacred

This is a multi-tenant platform. Cross-tenant data leakage is the highest-severity class of bug.

- Every Redis key, DB query, cache entry, vector namespace, and metric label **must** be scoped by `client_id`. Never read or write across tenants.
- A missing `client_id` is an **error, not a default** — escalate via `report_error(..., level="error")`. Never fall back to a "default" tenant or silently return.
- The reference/template client (`c3ffcb1b-...`, Groovee) is copied **from**, never written **to**, during onboarding.
- Never clone integration/credential configs across tenants (`shopify_details`, `shiprocket_details`, `gupshup_details`) — see `EXCLUDED_REFERENCE_CONFIG_KEYS` in `client_onboarding.py`.

**Why**: A single unscoped query or shared cache key can serve one client's orders, prices, or PII to another. Tenant scoping must be impossible to forget, not merely conventional.

---

### 8. Cron Jobs Run Guarded + Single-Pod

Scheduled jobs are registered on the shared `AsyncIOScheduler` in `cron_jobs/scheduler.py` (never a new event loop — see review checklist #1). **Every new cron job must register through `_guarded(...)`** and protect itself with a Redis lease:

- **Wrap the job in `_guarded(...)` at registration** — `_scheduler.add_job(func=_guarded(my_job), ...)`. `_guarded` runs the job inside the process-wide heavy-job semaphore (`asyncio.Semaphore(1)`), so only one heavy cron executes per pod at a time — a slow job can't overlap itself or another cron and starve request handlers / the DB pool. Guarding is the default; only a trivially short, non-I/O job may register the bare function, and that exception should be deliberate.
- **Guard single-pod execution with a Redis lease** — the scheduler starts on **every** replica, so the job body must acquire a lease via `cron_jobs/cron_lock.py` (`acquire_cron_lock` / `release_cron_lock`, owner-safe) and no-op when another pod holds it. The semaphore bounds concurrency *within* a pod; the lease ensures exactly one pod *across the cluster* does the work.

```python
# scheduler.py — registration (guarded, single in-flight instance)
_scheduler.add_job(func=_guarded(bestseller_refresh_monthly), trigger=..., max_instances=1)

# the job body — single-pod lease
lease = await acquire_cron_lock(lock_key="bestseller_refresh_lock", ttl_seconds=...)
if not lease:
    return {"skipped": True}  # another pod owns this run
try:
    ...  # do the work
finally:
    await release_cron_lock(lease)
```

**Why**: without `_guarded` a long job can overlap itself (or the weekly sync) and exhaust the event loop / DB pool; without the Redis lease every pod would run the same job, duplicating writes and Shopify/Upstash load.

---

## Architecture Standards

### Configuration-Driven, Vendor-Agnostic Design

- All vendor/client behavior is defined in `core/vendor_config.py`. Zero hardcoded vendor logic in high-level code.
- New vendors are added by implementing interfaces (`OrderInterface`, `ProductInterface`, `LogisticsInterface`) and registering in the config.
- The factory pattern (`core/factory.py`) resolves adapters at runtime based on config. High-level code never imports vendor-specific modules directly.

### Single-Flight Semantics

- Only one conversation turn executes at a time per user (enforced via Redis distributed lock).
- Concurrent messages are queued in the pending queue and merged after the active turn completes.
- This prevents race conditions on conversation state.

### Graceful Degradation

- Redis failures must **never** crash the bot. Use fail-open patterns — degrade to local cache or proceed without cache.
- External API failures (Shopify, Shiprocket, Gupshup) must be caught, logged with trace ID, and return a user-friendly fallback response.
- Use circuit breakers (`pybreaker`) and retry with exponential backoff (`tenacity`) for external calls.

### Channel-Specific Response Formatting

LLM responses are rendered differently on each delivery channel. Format-aware prompts and post-processing must account for these differences.

| Feature | Web Chat | WhatsApp |
|---------|----------|----------|
| Hyperlinks | Markdown `[text](url)` → rendered as clickable `<a>` tags by the widget formatter | Bare URLs only — WhatsApp auto-links them. Markdown `[text](url)` renders as ugly raw text with brackets. |
| Bold | `**text**` → `<strong>` | `*text*` (single asterisk, WhatsApp native) |
| Italic | `*text*` → `<em>` | `_text_` (underscore, WhatsApp native) |
| Lists | `- item` → `<ul><li>` | `- item` (plain text, no rendering) |
| Images | Product carousel via `###PRODUCT_IMAGES###` delimiter; `![alt](url)` converted to link | Not supported inline; use template messages with media headers |
| Product cards | Rendered as interactive carousels with variant picker | Not available; use numbered text list with bare product URLs |
| Cart actions | Widget actions dispatched via WebSocket (`add`, `remove`, `qty`, `show_cart`) | Not applicable — no storefront cart integration |

- The web chat widget (`chat-widget-frame.html`) has a built-in formatter that converts Markdown to HTML, auto-links bare URLs, normalizes product recommendation blocks, and strips CDN asset links.
- WhatsApp responses should avoid Markdown link syntax and use plain text with bare URLs instead.
- When writing prompts that generate links, use channel-aware instructions or apply post-processing to strip Markdown links for WhatsApp.

### Interface-First Development

When adding a new vendor or integration:

1. Define or implement the abstract interface in `interfaces/`.
2. Create the adapter in `<vendor>/tools/`.
3. Create the processor in `<vendor>/processors/`.
4. Create enrichers in `<vendor>/enrichers/` (if needed).
5. Register in `vendor_config.py`.
6. The core runtime, graph, and nodes remain untouched.

---

## Code Standards

### Shared Utilities Over Duplication

- If logic is used in **two or more** places (or a reusable block exceeds ~40 lines), extract it into `utils/` and import it. Never copy-paste logic across the WebSocket and Demo-chat surfaces.
- Check `utils/` before writing a helper — `product_utils`, `order_utils`, `cart_utils`, `context_helpers`, `agent_utils`, `phone_number_utils`, `client_id_utils`, `delivery_partner_utils`, etc. likely already cover it.
- Keep helpers **pure and stateless** (same contract as tools): inputs → outputs, no hidden state.
- One shared client per resource type (`utils/http_client.py`, `utils/redis_client.py`) — extend the shared helper, never re-create a per-module duplicate.

**Why**: Duplicated logic drifts out of sync across surfaces and doubles the fix surface for every bug.

### Outbound Shopify API Rate Limiting

- Every outbound Shopify Admin API call goes through **two layers**, both in `utils/`:
  - **Proactive** — `utils/shopify_rate_limiter.py` (`get_shopify_rate_limiter().acquire(shop)` / `.run(shop, fn, …)` / `@shopify_rate_limited`). A Redis token bucket, **keyed per shop domain** and **shared across all pods/services**, paces calls *before* they leave the process so we stay under Shopify's limit by design rather than discovering it via 429s. Fail-open: degrades to a per-process bucket when Redis is down.
  - **Reactive** — `utils/shopify_throttle.py` (`shopify_graphql_post(..., rate_limit_key=shop)`) still wraps the call in `tenacity` backoff on 429 / 5xx / network / GraphQL `THROTTLED` as a safety net.
- New Shopify call paths must pace through the shared limiter (pass `rate_limit_key=self.shop_domain` to `shopify_graphql_post`, or `acquire()` before a raw `client.post`) — never call Shopify unthrottled. The budget is shared, so a bulk sweep (full sync, bestseller refresh) and interactive tools draw from the same per-shop allowance.
- Tunables (env): `SHOPIFY_RATE_LIMIT_RPS` (default 2.0), `SHOPIFY_RATE_LIMIT_BURST` (default 4), `SHOPIFY_RATE_LIMIT_MAX_WAIT` (default 30s), `SHOPIFY_RATE_LIMIT_ENABLED`.

### Minimal-Footprint Changes

- Prefer the smallest diff that solves the problem. Add behavior by **importing a new helper** into the existing file rather than rewriting in-place blocks.
- Do not refactor, reformat, or re-order unrelated code in the same PR — it hides the real change from reviewers and pollutes `git blame`.
- For large hot files (`tool_factory.py`, `orchestrator.py`, `generic_skill_node.py`), put new logic in a separate module and call into it. Keep these files as thin orchestration layers.
- When a change touches per-invocation behavior shared by many call sites (e.g. an LLM `config=` argument), thread it through the existing call instead of forking the function.

**Why**: Minimal diffs are reviewable, reversible, and lower-risk in a codebase with multi-thousand-line hot files.

### Idempotent by Default

- Write new functions to be **idempotent**: calling them twice with the same inputs produces the same result and no extra side effects. Re-running a sync, retrying a webhook, or replaying a queued job must not double-write, double-charge, or corrupt state.
- Guard mutations with existence/version checks (upsert on a stable key, `INSERT ... ON CONFLICT`, "already applied?" guard) instead of blind appends or unconditional writes.
- Make the change read clean and concise: compute the desired end state and converge to it, rather than scattering conditional patch-up branches across the call site.
- Keep helpers pure and stateless (same contract as tools) so idempotency is easy to reason about — inputs → outputs, no hidden accumulation.

**Why**: Retries, at-least-once queues (Dramatiq), and cron re-runs are routine in this platform — non-idempotent code turns a harmless replay into duplicate orders, double sends, or drifted state.

### Decorators Over Repeated Wrapping

- When the **same wrapping logic** is applied around multiple functions — retries, rate limiting, tracing, auth/tenant checks, caching, timing — factor it into a **decorator** instead of repeating the boilerplate inline (follow the existing `@shopify_rate_limited` pattern).
- Reach for a decorator once the pattern appears in **two or more** call sites; keep the decorator in `utils/` so every surface shares one implementation (see *Shared Utilities Over Duplication*).
- Decorators must be async-aware (`functools.wraps`, `await` the wrapped coroutine) and preserve the wrapped function's signature, name, and type hints.

**Why**: Cross-cutting concerns expressed as decorators keep the business logic readable and ensure a fix (e.g. backoff tuning) lands in one place instead of every copy.

### LLM Call Hygiene

- High-volume, non-conversational LLM calls (product ingestion, OCR, attribute extraction, onboarding, prompt generation) must **not** push traces to LangSmith. Pass `config={"callbacks": []}` per-invocation, or disable tracing process-wide in worker entrypoints.
- Reserve LangSmith traces for **conversation** flows where prompt debugging adds value.
- Never flip `LANGCHAIN_TRACING_V2` at runtime inside a shared process — it affects concurrent conversation calls. Scope tracing per-call instead.
- Route background LLM workloads to the background key (`OPENROUTER_API_KEY_2`) so ingestion never competes with live-chat quota.
- Tag every LLM call with `client_id` (OTel baggage via `request_client_id`) so cost dashboards never show `client_id=unknown`.

**Why**: Tracing and token spend on batch/background LLM calls are pure cost with little debugging value, and uncontrolled tracing has driven significant LangSmith bills.

### Error Handling

- Catch specific exceptions, never bare `except:`.
- Always include `trace_id` and context in error logs.
- Report to Rollbar for production errors with full context.
- Graceful degradation over hard failures — the bot should always respond, even if degraded.

### Type Safety

- Use Pydantic models for all data structures crossing boundaries (API responses, state objects, configs).
- Use `TypedDict` or dataclasses for internal structures.
- Type-hint all function signatures.

### Testing

- Use the mock system (`fashion_bot/mock/`) for unit tests — controlled via `USE_MOCK_SERVICES` flag.
- Integration tests hit real services in staging environments.
- Test files live in `tests/` with the naming convention `test_<module>.py`.
- See `tests/README_TESTING.md` and `tests/AGENT_TESTING_ARCHITECTURE.md` for test patterns.

### Dependencies

- Pin all dependencies in `requirements.txt`.
- Prefer well-maintained, async-native libraries.
- No synchronous HTTP libraries (`requests`) in production code — use `aiohttp` or `httpx`.

### Git & Branching

- `main` is the production branch.
- Feature branches for all work — no direct commits to `main`.
- Commit messages should describe *why*, not just *what*.

---

## UI / Widget Code Review — Must Check (WebSocket & Message/Response Lifecycle)

The web chat widget (`fashion_bot/static/chat-widget-frame.html` and the loader bundles `chat-widget.vNN.js`) carries the most lifecycle-sensitive client code in the platform. Connection churn, stuck queues, and hung "Thinking…" spinners are **user-visible breakage that no server-side test catches**. Every PR that touches the widget's socket, message queue, or response handling **must** be reviewed against this checklist, and the result (pass / waived-with-reason) captured in the PR description. Each item below exists because it has been a real bug.

### A. WebSocket connection lifecycle

1. **Reconnect is bounded, then stops cleanly.** Auto-reconnect must cap at `MAX_AUTO_RECONNECT_ATTEMPTS` and surface a user-actionable terminal state ("Send a message or reopen chat to retry"). The attempt counter must actually increment on **every** retry path — including connect-timeout retries, not just `onclose`.
2. **No unbounded / runaway socket creation.** `connect()` must never call itself synchronously; every retry goes through a single `setTimeout` guarded by a reconnect-timer flag so `onerror`+`onclose` (which both fire on failure) coalesce into **one** pending reconnect, not two.
3. **Connect-stall watchdog.** A socket stuck in `CONNECTING` must time out (`WS_CONNECT_TIMEOUT_MS`), close, and retry — a `CONNECTING` socket never fires `onclose` on its own, so without this the widget hangs forever with no reconnect.
4. **Stale-socket identity guard (every handler, not just the timeout).** `onopen` / `onclose` / `onerror` are closures bound to the socket that created them. Because a `CLOSING` socket coexists with a freshly-created one (the `connect()` guard only blocks on `OPEN`/`CONNECTING`), a *stale* handler firing late will mutate global state for the **wrong** socket — e.g. `ws = null` clobbering the live socket, `ws.close()` closing the current socket, or a stale `code 1008` setting `websocketFatalRejectReason` and permanently disabling reconnect. **Every** handler must begin with an identity check (`if (ws !== connectingSocket) return;`) — the same guard the connect-timeout already uses. Checking "the current global `ws`" is **not** sufficient; the handler must confirm it *is* the current socket.
5. **Attempt-counter resets are intentional and event-driven only.** Resetting `reconnectAttempts = 0` (e.g. in `reconnectIfUseful`, `sendMessage`, online/visibility handlers) is acceptable only on discrete external events (user send, tab focus, network online) — never on a self-repeating timer, which would defeat the cap.
6. **Heartbeat is single-instance.** `startHeartbeat()` must `stopHeartbeat()` first; no path may leak a second `setInterval`. Heartbeat send-failure must funnel through the same bounded reconnect.
7. **Fatal vs retryable close codes are distinguished.** Policy closes (`1008`, `unauthorized`/`forbidden`/`origin`) stop reconnect; idle-timeout (`1000` + "idle timeout") parks quietly; everything else reconnects.

### B. Outbound message queue

8. **Single enqueue path — no raw `messageQueue.push`.** Every enqueue must go through the one wrapper (`enqueueSocketPayload`) so each entry carries a `queuedAt` timestamp and is subject to cap + TTL. A raw `push({...})` that skips the wrapper produces an **immortal entry** (its missing `queuedAt` defaults to "now" on every TTL check, so it never expires) and bypasses the cap. Grep the diff for `messageQueue.push` / `.unshift` and confirm each goes through the wrapper.
9. **Queue is capped and entries expire.** Enforce `MAX_QUEUED_PAYLOADS` and `QUEUED_PAYLOAD_TTL_MS`. The prune loop must strictly make progress (every branch removes ≥1 element) so it can't spin.
10. **Re-queue preserves the wrapper.** On a failed flush, `unshift` the **wrapped entry** (with its original `queuedAt`), never the bare payload — otherwise TTL resets and the entry becomes immortal.
11. **Queued messages aren't stranded.** Any branch that stops reconnect (idle-timeout, max attempts) while `messageQueue.length > 0` must leave a recovery trigger (`reconnectPendingUntilVisible`, or re-arm on user action) so queued sends are eventually flushed or expired — never silently held with no path forward.
12. **Drain loop can't tight-spin.** `processMessageQueue` must terminate: a send failure must change socket state (or break) so the `while (… readyState === OPEN)` loop exits rather than re-attempting the same poison entry forever.

### C. Inbound message / response lifecycle

13. **Malformed inbound is non-fatal.** `onmessage` must `try/catch` the `JSON.parse` and validate shape (`typeof data === 'object' && data.type`) — one bad frame must never throw and kill the widget.
14. **No-reply watchdog.** If a message is sent successfully but the backend never replies **while the socket stays OPEN**, the "Thinking…/Typing…" indicator must be cleared by a wall-clock timeout (with a retry/affordance). Reconnect logic does **not** cover this case (the socket is healthy), so a dedicated response watchdog is required — otherwise the UI hangs indefinitely.
15. **Typing indicator is cleared on every terminal path.** Reply received, error, reconnect give-up, phone-required, and the no-reply watchdog must all clear it. Never leave it spinning.

### D. Fetch / async hygiene in the loader bundle

16. **Every storefront/config `fetch` has a timeout.** Use the shared `fetchWithTimeout` (AbortController + timer cleared in `.finally`). A slow Shopify call must not hang cart/product operations.
17. **In-flight flags reset on all outcomes.** Any "in-flight" guard (`cartSyncInFlight`, promise singletons) must be reset in `.finally()` — a timed-out/aborted/ rejected fetch must not leave the flag stuck `true` and block all future operations.
18. **Iframe readiness is bounded and self-healing.** The loader's iframe-ready watchdog and retry/backoff must reset their attempt counter on `fashionbot-ready` and must not depend on telemetry/config that may load later (the ready signal is intentionally sent *before* non-critical dynamic config).

### E. Bundle versioning

19. **No silent divergence between widget versions.** When a new `chat-widget.vNN.js` is introduced, it must be a deliberate, diffable copy (ideally only the version constant changes) — confirm with `diff` that no lifecycle logic was unintentionally forked. Duplicated socket/queue logic across `vNN` bundles drifts and doubles the fix surface (see *Shared Utilities Over Duplication*).

**Why**: These are the exact failure modes that survive backend tests and ship to users — runaway reconnects, zombie sockets corrupting the live connection, immortal/stranded queue entries, and spinners that never stop. A structured checklist makes them mechanical to catch in review instead of relying on a deep manual trace each time.

---

## Observability Stack

| Layer | Tool | Purpose |
|-------|------|---------|
| LLM Tracing | LangSmith | Full execution traces, prompt debugging, latency tracking |
| Error Tracking | Rollbar | Production error reporting with context |
| Distributed Tracing | OpenTelemetry | Cross-service span collection |
| Application Logs | Structured JSON | Trace-ID-tagged logs for debugging |
| Analytics | PostgreSQL | Conversation audit trails |

---

## Design Documents

Architecture decisions are documented in `fashion_bot/design_docs/`. Read the relevant design doc before modifying a subsystem.

### Active & Implemented

| Document | Purpose |
|----------|---------|
| `REFACTOR_ARCHITECTURE.md` | Core architecture blueprint — vendor-agnostic, config-driven design with adapters, processors, enrichers, and factory pattern. **Read this first.** |
| `GRAPH_AND_RUNTIME_STATE_MANAGEMENT.md` | Production runtime design — single-flight lock, pending merge, summary worker, Redis fail-open, tiered cache. Implemented in `conversation_runtime.py` and `state_cache.py`. |
| `unified_state_cache_design.md` | Multi-channel state management — unified Redis key strategy (`{channel}:{tenant_id}:{user_id}`), supports WhatsApp/Web/Instagram/Streamlit. Implemented in `state_cache.py`. |
| `vector_search_design_doc.md` | Semantic product search — Upstash Vector DB with BAAI/bge-m3 embeddings, multi-tenant design, delta sync. Implemented in `services/product_ingestion/`. |
| `PRODUCT_INGESTION_VECTOR_DB.md` | Vector DB ingestion pipeline — full ingestion, delta sync via content hash, scheduled cron sync. Uses orchestrator/factory pattern. |
| `ATTRIBUTION_FRAMEWORK.md` | Chatbot conversion attribution for Shopify — direct (bot_ref) and assisted (anon_id) tracking with 72-hour TTL. |
| `ATTRIBUTION_EXPLAINED.md` | Plain-language attribution guide for stakeholders — anon_id vs session_id vs bot_ref, direct vs assisted, examples, precision without session_id in localStorage. |
| `CANCELLATION_AVERSION_ARCHITECTURE.md` | Strategy for reducing order cancellations through conversational patterns. Tracked in `analytics/cancellation_aversion_tracker.py`. |
| `GUPSHUP_SETUP_FINAL.md` | WhatsApp/Gupshup template integration — API endpoint, template fetching, parameter rendering. |
| `WEB_CHAT_IMPLEMENTATION_SUMMARY.md` | WebSocket chat widget — single codebase for web, WhatsApp, Streamlit. Describes `websocket_chat.py` and session management. |
| `WIDGET_INTEGRATION_GUIDE.md` | Chat widget embedding guide — 2-line integration for third-party websites. |
| `WIDGET_CDN_STRATEGY.md` | CDN deployment strategy for widget assets — caching, versioning, fallback. |
| `WIDGET_SECURITY_HTTP_AND_WS.md` | Widget security rollout — CORS env, max body / WS size, Redis sliding-window HTTP rate limit, per-client widget API key and embed origins on WebSocket, CDN vs API base URLs in `/widget/config.json`. Automated checks: `fashion_bot/scripts/security_smoke_tests.py`. |
| `WEB_WIDGET_SESSION_AND_PHONE_IDENTITY.md` | Web widget session vs phone identity — `sessionStorage` vs `localStorage`, Redis `web:` guest vs phone threads, cross-tab behavior, WhatsApp separation, phone migration pointers. |
| `ENV_LOADING_AND_CLIENT_CONTEXT_SETUP.md` | Environment configuration and client context initialization — `env_loader.py` and bootstrap process. |
| `UPDATE_PRODUCT_IN_ORDER.md` | Product modification workflow — handles item changes in existing orders. |
| `DYNAMIC_MOCK_SYSTEM_README.md` | Mock system for testing without real API calls. |
| `DYNAMIC_MOCK_IMPLEMENTATION_SUMMARY.md` | Implementation details of mock system with `USE_MOCK_SERVICES` flag. |
| `LOAD_TEST_IO_BOTTLENECK_REPORT.md` | Performance analysis — Redis connection pooling, async patterns, bottleneck identification. |
| `VECTOR_SEARCH_DEMO_CHAT.md` | Demo usage of vector search in the chat product discovery flow. |
| `MANUAL_TEST_CASES_GRAPH_RUNTIME.md` | Manual test cases for QA of the graph runtime. |
| `design-product-image-delimiter.md` | Product image formatting and delimiting strategy for display rendering. |
| `DELHIVERY_INTEGRATION_AND_MULTI_PARTNER_LOGISTICS.md` | **MANDATORY READ** — Multi-partner logistics design: Delhivery integration, partner resolution via `delivery_partner_utils.py`, unified `DeliveryPartnerAdapter` interface, and tracking status normalization. **You must read this doc before modifying any order status, delivery tracking, or logistics partner logic.** |

### Aspirational / Not Yet Implemented

| Document | Status | Notes |
|----------|--------|-------|
| `plans/context_plan.md` | **Partially implemented** | Conversation context design — some patterns adopted in `context_helpers.py`, but the full `ConversationContext` class as designed is not in use. Treat as reference for future context system work. |
| `scope/Memory_Scope.md` | **Not implemented** | Async memory layer design with Neon JSON store + Upstash Vector for user preference recall. No `Memory` class exists in codebase. Treat as a future roadmap item. |

---

## Adding a New Integration Checklist

1. Confirm the integration will be **fully async** end-to-end.
2. Define or implement the relevant interface in `interfaces/`.
3. Create stateless adapter, processor, and enricher classes.
4. Register the vendor in `vendor_config.py`.
5. Add mock implementations in `mock/` for testing.
6. Ensure all logging includes `trace_id`.
7. Ensure config reads go through the three-tier cache.
8. Write a design doc in `design_docs/` if the integration introduces new patterns.
9. Add test coverage using the mock system.
10. Verify graceful degradation — the bot must respond even if the new integration fails.

---

## Do NOT

- Use synchronous I/O in any handler or tool.
- Mutate conversation state from within a tool or adapter.
- Hardcode vendor-specific logic outside the vendor's own module.
- Skip trace ID propagation in log statements.
- Bypass the factory pattern to import vendor modules directly.
- Use bare `except:` — always catch specific exceptions.
- Commit secrets, API keys, or `.env` files.
- Push directly to `main` without review.
- Multiplex multiple job types onto one Dramatiq queue, or hardcode a queue name outside `workers/config.py`.
- Read or write data across tenants, or fall back to a "default" `client_id` when resolution fails.
- Copy-paste reusable logic instead of extracting it into `utils/`.
- Push LangSmith traces for batch/background LLM calls (ingestion, OCR, onboarding, prompt generation).
- Modify order status, delivery tracking, or logistics partner logic without first reading `fashion_bot/design_docs/DELHIVERY_INTEGRATION_AND_MULTI_PARTNER_LOGISTICS.md`.
- Mutate global socket state (`ws = null`, `ws.close()`, reconnect flags) from a WebSocket handler without an identity guard confirming it is the current socket.
- `messageQueue.push(...)` a raw payload — always enqueue through the wrapper so the entry carries `queuedAt` and is bound by cap + TTL.
- Leave a "Thinking…/Typing…" indicator without a no-reply watchdog, or fire a storefront `fetch` without a timeout and an in-flight flag reset in `.finally()`.
- Ship a widget change touching the socket, message queue, or response lifecycle without running the *UI / Widget Code Review — Must Check* checklist and recording the result in the PR.
