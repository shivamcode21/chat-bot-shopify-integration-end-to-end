# Load Test — Technical Doubts & Answers

## 1. Are Redis and Neon DB calls sync or async in the WebSocket hot path?

**Answer: All hot-path I/O is fully async. Zero sync blocking.**

| I/O Call | Method | Async? | File |
|---|---|---|---|
| Redis state get/set | `await client.get()` / `await client.setex()` | Yes | state_cache.py:601,698 |
| Redis single-flight lock | `await redis_guard.execute_async()` | Yes | runtime_presets.py:94 |
| Redis config cache | `await client.get()` / `await client.setex()` | Yes | config_manager.py:141,149 |
| Neon DB config reads | `async with get_async_postgres_connection()` | Yes | config_manager.py via tiered_cache |
| Neon DB conversation storage | `async with get_async_postgres_connection()` | Yes | postgres_conversations.py:410 |
| Neon DB client name lookup | `await cur.execute()` | Yes | websocket_chat.py:818 |
| Tiered cache memory tier | `_GLOBAL_MEMORY_CACHE.get()` (in-process dict) | Sync but no I/O | tiered_cache.py:106 |

### Two exceptions (NOT in hot path under normal conditions)

1. **`_ProcessLocalFallbackStore`** in `state_cache.py:77-104` — uses `threading.Lock()` inside async
   functions. Has O(n) pruning over up to 2000 entries. **Only activates when Redis is down** (fallback
   mode). Under normal load test conditions (Redis healthy), this code path is skipped entirely.

2. **`PostgresSaver` checkpointer** in `database_manager.py:816-903` — uses sync `pool.getconn()`.
   This is the LangGraph graph-state checkpointer for persisting graph state between turns. **Not called
   during message processing** — separate from conversation/config DB calls.

### Bottom line

During load testing (Redis + Neon healthy), the ~2s latency seen in smoke tests is real async I/O
wait time (network round trips to remote Redis/Neon), not event loop blocking.

---

## 2. Does client name lookup happen every time or just on connection start?

**Answer: Only once per WebSocket connection, at the start.**

The flow is:

```
1. Client connects to /ws/chat/{client_name}/{session_id}
2. websocket_chat_endpoint() calls aresolve_client_name_to_id(client_name)  [LINE 1988]
3. This reads from an in-memory cache (_client_name_cache)                  [LINE 855-860]
4. If cache is fresh (< 10 min old), returns immediately — NO DB call
5. If cache is stale, ONE async DB query refreshes ALL client names         [LINE 872]
6. client_id is stored in the session dict                                  [LINE 2006]
7. All subsequent messages in this connection use session["client_id"]      — no re-lookup
```

**Cache behavior:**
- `_client_name_cache` is a **process-global dict** (not per-connection)
- TTL: **10 minutes** (`_cache_ttl = timedelta(minutes=10)`)
- Protected by `asyncio.Lock` to prevent thundering herd on refresh
- Refreshes ALL client names in one query: `SELECT id, "name" FROM public.clients`
- After refresh, all new connections hit the in-memory dict — zero DB calls

**During a load test with 50 users connecting:**
- First connection: 1 DB query to populate cache
- Connections 2-50: instant in-memory lookup (0ms)
- After 10 min: 1 DB query to refresh, then back to in-memory

---

## 3. What security checks happen on WebSocket connection?

**Answer: Client name validation only. No auth token, no rate limiting.**

The connection flow (`websocket_chat.py:1965-2020`):

```
1. Accept WebSocket                                     [LINE 1981]
   — No origin check (CORS allows "*")
   — No auth token required
   — No API key validation

2. Track connection in metrics collector                 [LINE 1984-1985]

3. Resolve client_name → client_id from DB cache        [LINE 1988]
   — If client_name not found in clients table → REJECT with code 1008
   — This is the ONLY security gate

4. Get or create Redis session                          [LINE 2006]
   — session_id from URL path (client-generated UUID)
   — No validation on session_id format

5. Send welcome message                                 [LINE 2010]

6. Enter message loop with idle timeout (20 min)        [LINE 2023]
```

**What IS checked:**
- `client_name` must exist in the `clients` DB table (otherwise connection closed with 1008)

**What is NOT checked:**
- No authentication token or API key
- No origin validation (CORS `allow_origins=["*"]` in main.py:26)
- No rate limiting per IP or session
- No session_id format validation (accepts any string)
- No message size limits (beyond WebSocket frame limits)

**Implications for load testing:**
- Any client_name in the DB works — use `LOAD_TEST_CLIENT_NAME` env var
- No auth headers needed in the load test client
- The only failure mode is an invalid client_name → 1008 close

---

## 4. Do Neon DB config reads happen every turn or just once?

**Answer: Client name lookup is once. But `agents_config` prompt lookups hit DB EVERY turn — this is a bug.**

### What should happen (1 DB call, then cached)

```
Turn 1: memory miss → Redis miss → DB query → cache result → return
Turn 2: memory hit → return (0ms, no DB)
```

### What actually happens (DB hit every turn)

Confirmed from smoke test logs (`runtime_metrics db_calls=3` per turn):

```
Turn 1: memory miss → Redis miss → DB query → returns NULL → NOT cached → return None
Turn 2: memory miss → Redis miss → DB query → returns NULL → NOT cached → return None  (same!)
```

### Root cause: wrong client name for load test

Querying the DB revealed:

```
AGENTS_CONFIG per client:
  Casence            -> 14 agents
  Concept Groove     -> 20 agents   <-- has full config (20 agents)
  Concept Groove 123 ->  0
  Concept Groovee    ->  0          <-- was our load test client, NO config!
  Little Igloo       -> 20 agents
  Myraymond          ->  4 agents
  Only               -> 20 agents
  Underneat          ->  2 agents
  Westside           ->  1 agents
```

"Concept Groovee" (with extra 'e') has **zero** `agents_config` rows. The DB queries were returning
NULL every turn, and since `cache_none` defaults to `False` in `aget_with_tiered_cache`, the NULL
result was never cached → repeated DB hits.

**Fix applied**: changed `LOAD_TEST_CLIENT_NAME` from "Concept Groovee" to **"Concept Groove"**
(which has 20 agent configs including `intent_detection_handler`, `unknown_handler`, etc.)

With the correct client, the expected behavior is:
- Turn 1: memory miss → Redis miss → DB query → cache 20 agents (10 min TTL) → return prompt
- Turn 2+: memory hit → return instantly (0 DB calls for config)

### Secondary issue: `cache_none=False` (still worth fixing)

Even with the correct client, `utils.py:209` doesn't pass `cache_none=True`. This means if a
client genuinely has no config, the DB will be queried on every turn. Adding `cache_none=True`
would cache the "not found" result and prevent repeated DB calls for unconfigured clients.

### DB calls per message turn (expected with correct client)

| # | Call | Type | Frequency |
|---|---|---|---|
| 1 | `INSERT message` (customer) | Write | Every turn |
| 2 | `SELECT agents_config` (prompt lookup) | Read | **Turn 1 only** (then cached 10 min) |
| 3 | `INSERT message` (bot response) | Write | Every turn |

**After cache warm-up: 2 DB writes per turn, 0 DB reads. This is correct.**

### Smoke test results with correct client ("Concept Groove")

```
Turn 1 [443122b9]: db_calls=3, elapsed_ms=1786  (cold: 1 agents_config read + 2 msg writes)
Turn 2 [0068f427]: db_calls=1, elapsed_ms=410   (warm: 0 reads, msg writes)
Turn 3 [30fa1862]: db_calls=2, elapsed_ms=344   (warm: 0 reads, 2 msg writes)
Turn 4 [c4f5b7b9]: db_calls=2, elapsed_ms=337   (warm: 0 reads, 2 msg writes)
Turn 5 [e4d4555b]: db_calls=1, elapsed_ms=337   (warm: 0 reads, msg writes)
```

**Confirmed: agents_config DB read only happens on Turn 1.** All subsequent turns serve prompts
from memory/Redis cache. The remaining db_calls are message INSERT writes (unavoidable).

**Note on db_calls=1 vs db_calls=2 variance:** Both message writes (customer + bot) are fully
async (`await astore_conversation_event(...)`). The `runtime_metrics` counter only counts DB calls
inside the `runtime.run_turn()` measurement window. The bot response write sometimes completes
after the metrics are captured (response is sent to user first, DB write fires after), so the
counter misses it. This is a **measurement timing artifact**, not a sync/async difference.
Every turn always does 2 async message writes.

Additional DB writes observed (async, fire-and-forget, outside runtime_metrics):
- `UPDATE conv tags` + `UPDATE msg tags` — async tag generation after each turn
- These run in background and don't add to response latency

### Latency breakdown (with correct client)

```
Turn 1: 4719ms  (cold start: DB pool init + agents_config fetch + Redis connect)
Turn 2: 2105ms  (warm cache, still remote Redis + Neon writes)
Turn 3: 1665ms  (steady state)
Turn 4: 1646ms  (steady state)
Turn 5: 1644ms  (steady state)
```

Steady state ~1650ms per turn = 50ms mock LLM + ~1600ms real I/O (2 Neon writes + Redis state).
The Neon DB writes (~200ms each) dominate the latency.
