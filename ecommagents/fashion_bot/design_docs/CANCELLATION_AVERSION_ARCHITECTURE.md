# Cancellation Aversion & RTO Aversion Tracking — Architecture

## What This Does

Tracks two classes of "saved order" events and records them in Postgres for UI analytics.

### Class 1 — Cancellation Aversion
Customer explicitly wanted to cancel. The bot either:
- Offered a concrete alternative (exchange, return, address fix, product change) — detected in real-time via tool calls
- Persuaded through pure conversation text — detected at session-end by LLM classifier

### Class 2 — RTO (Return to Origin) Aversion ← NEW
No "cancel" was mentioned. The customer reported a **delivery problem** (wrong address,
wrong contact, unreachable phone) and the bot fixed it in the same conversation. Without
the fix, the carrier would return the package and the order would effectively be lost.

The update tool call itself is both the trigger and the proof — these events are
**always opened and immediately closed** in the same turn. No pending state, no LLM
classifier needed.

```
event_type = 'cancellation_aversion'   ← user said cancel, bot averted
event_type = 'rto_aversion'            ← bot fixed delivery details, RTO prevented
```

Both live in the same `cancellation_aversion_events` table and are queryable separately
for the analytics dashboard.

---

## Complete Scenario List

There are two broad families of cancellation aversion: **tool-driven** (hard signal, detected in real-time)
and **LLM-driven** (soft signal, detected by classifier at session end). Both count as averted.
Plus a third family: **RTO aversion** (always tool-driven, always immediate close).

---

### FAMILY A — Tool-Driven Aversion (real-time, immediate close)

These are the easy cases. A concrete action tool was called, confirming the alternative.

---

**Scenario A1 — Exchange Offered and Accepted**
```
Turn 1: User  → "cancel my order GV10741, delivery too slow"
        Graph → asks for reason, sets waiting_for_cancellation_reason=True
        Signal: state_flag (HIGH confidence)

Turn 2: User  → "yes ok do exchange"
        Graph → calls initiate_exchange_tool(order_id="GV10741")
        Signal: outcome tool detected

→ status=averted, resolution=exchange, aversion_method=tool_driven
   closed in real-time on Turn 2
```

---

**Scenario A2 — Return Initiated**
```
Turn 1: User  → "I don't want this product anymore, cancel it"
        Graph → gets cancellation reasons, offers return
        Signal: tool_signal (get_order_cancellation_reasons called)

Turn 2: User  → "fine, raise a return"
        Graph → calls initiate_return_tool(order_id="GV10741")

→ status=averted, resolution=return, aversion_method=tool_driven
```

---

**Scenario A3 — Address Fix Averts Cancellation**
```
Turn 1: User  → "want to cancel, wrong address on order"
        Graph → offers to update address instead
        Signal: state_flag

Turn 2: User  → "yes update the address"
        Graph → calls update_order_address_tool(order_id=..., new_address=...)

→ status=averted, resolution=address_fix, aversion_method=tool_driven
```

---

**Scenario A4 — Product Change Averts Cancellation**
```
Turn 1: User  → "cancel this, I ordered wrong size"
        Graph → offers to change size/product

Turn 2: User  → "yes change it to large"
        Graph → calls change_order_product_tool(order_id=..., new_variant=...)

→ status=averted, resolution=product_change, aversion_method=tool_driven
```

---

**Scenario A5 — Single Turn Resolution (intent + aversion in same turn)**
```
Turn 1: User  → "cancel because wrong address"
        Graph → detects fixable issue, immediately calls update_order_address_tool
                without a separate confirmation turn

→ status=averted, resolution=address_fix, aversion_method=tool_driven
   Both intent AND outcome signals fire in same turn — closed immediately
```

---

**Scenario A6 — Escalation During Cancellation Flow**
```
Turn 1: User  → "cancel my order, this is unacceptable"
        Graph → tries to retain, then escalates (can't resolve autonomously)
        Graph → calls trigger_agent_escalation(reason="customer wants cancellation")

→ status=escalated, resolution=escalation, aversion_method=tool_driven
   Not strictly "averted" but tracked separately — human agent takes over
```

---

### FAMILY B — LLM-Driven Aversion (no tool, classifier decides at session end)

These are the hard cases. No action tool fires. The bot handled it with pure text.
The event stays `pending` throughout and is resolved by the LLM classifier when the
session expires (Redis TTL=90min) or when the background job runs.

---

**Scenario B1 — Bot Persuades With Delivery Info (pure text)**
```
Turn 1: User  → "cancel my order, delivery is too slow"
        Graph → "Your order has shipped and is out for delivery, arriving today by 7pm.
                 Would you still like to cancel?"
        No tool called — bot used delivery status text to retain

Turn 2: User  → "oh okay, I'll wait then"
        Graph → "Great! Let us know if you need anything else."
        No tool called — pure LLM response

Session ends, Redis TTL fires.
LLM classifier reads conversation_snapshot:
  → User wanted to cancel
  → Bot provided delivery info (no tool)
  → User accepted and stepped back
  → Verdict: averted

→ status=averted, resolution=llm_persuasion, aversion_method=llm_driven
   llm_classified=true, llm_confidence=0.92
```

---

**Scenario B2 — User Shifts Context (topic change after cancel intent)**
```
Turn 1: User  → "I want to cancel order GV10741"
        Graph → "Can I ask why? We may be able to help."
        No tool called (asking for reason)
        Signal: state_flag (waiting_for_cancellation_reason=True)

Turn 2: User  → "actually never mind, do you have this shirt in blue?"
        Graph → routes to product inquiry node
        detected_intents = ["product_inquiry"]
        No cancellation or aversion tool called
        Intermediate signal logged: context_shift detected

Session ends, Redis TTL fires.
LLM classifier reads conversation_snapshot:
  → User expressed intent then shifted topic unprompted
  → Order was not cancelled
  → Verdict: averted (user self-de-escalated)

→ status=averted, resolution=context_shift, aversion_method=llm_driven
   llm_classified=true, llm_confidence=0.78
```

---

**Scenario B3 — Bot Explains Policy, User Accepts (no tool)**
```
Turn 1: User  → "cancel this order"
        Graph → "This order is already dispatched. Cancellation at this stage means
                 you'll need to refuse delivery. The refund takes 7-10 days.
                 Alternatively, you can wait and raise a return after delivery."
        No tool called — pure policy explanation

Turn 2: User  → "fine, I'll raise a return when it arrives"
        Graph → "Perfect! We'll note that. You can initiate a return from the app."
        No return tool called — just acknowledgement text

Session ends.
LLM classifier:
  → User dropped cancellation intent after policy explanation
  → Chose deferred return path (not cancelled)
  → Verdict: averted

→ status=averted, resolution=llm_persuasion, aversion_method=llm_driven
```

---

**Scenario B4 — Implicit Drop (user goes quiet after bot responds)**
```
Turn 1: User  → "please cancel order GV10741"
        Graph → "We'd love to help resolve this. Can you tell us what's wrong
                 with the order so we can find a better solution?"
        No tool called

Turn 2: (no reply — user goes silent for 90+ minutes)

Redis TTL expires → DB row still pending
Background job finds this row
LLM classifier reads snapshot:
  → User raised cancellation, bot asked for reason
  → User went silent (no confirmation either way)
  → Order not cancelled
  → Verdict: abandoned (could not confirm aversion)

→ status=abandoned, aversion_method=llm_driven
   llm_classified=true, llm_confidence=0.55, llm_reasoning="user did not respond"
```

---

**Scenario B5 — Multi-Turn Persuasion, No Tools**
```
Turn 1: User  → "cancel"
        Bot   → "May I know the reason?"

Turn 2: User  → "too expensive"
        Bot   → "We understand. We have a 10% loyalty discount for your next order.
                 Would you reconsider keeping this one?" (no tool)

Turn 3: User  → "ok fine, keep it"
        Bot   → "Thank you! Your order is on its way." (no tool)

Session ends.
LLM classifier:
  → Multi-turn persuasion with no tools
  → User explicitly said "ok fine, keep it"
  → Verdict: averted

→ status=averted, resolution=llm_persuasion, aversion_method=llm_driven
   llm_classified=true, llm_confidence=0.97
```

---

### FAMILY C — RTO Aversion (event_type = 'rto_aversion', always tool-driven)

These are **implicit** saves. The user never said "cancel" — but without the bot's
intervention the order would have been returned by the carrier.
All RTO events are opened **and immediately closed** in the same webhook turn.
No Redis pending state. No LLM classifier needed.

---

**Scenario C1 — Wrong Shipping Address Fixed**
```
Turn 1: User  → "the address on my order is wrong, please fix it"
        Graph → collects new address, calls update_order_address(order_id=..., address=...)

→ event_type=rto_aversion, status=averted
   resolution=address_update, aversion_method=tool_driven
   Opened + closed in single turn
```

---

**Scenario C2 — Wrong Contact Phone Fixed**
```
Turn 1: User  → "the phone number on my order is wrong, delivery person can't reach me"
        Graph → calls update_order_phone_number_tool(order_id=..., new_phone=...)

→ event_type=rto_aversion, status=averted
   resolution=phone_update, aversion_method=tool_driven
```

---

**Scenario C3 — Reshipment After Failed Delivery**
```
Turn 1: User  → "my order came back, the address was wrong, please reship to new address"
        Graph → calls update_order_address(order_id=..., address=...) to correct and reship

→ event_type=rto_aversion, status=averted
   resolution=address_update, aversion_method=tool_driven
   (Carrier attempted delivery → returned → bot fixed → reship = RTO averted)
```

---

**Scenario C4 — Multiple Fields Fixed in Same Turn**
```
Turn 1: User  → "wrong address and the name on the order is also wrong"
        Graph → calls update_order_address(...) + update_order_phone_number_tool(...)
                (both tools fire in same turn)

→ event_type=rto_aversion, status=averted
   resolution=multi_field_update, outcome_trigger_tool="update_order_address,update_order_phone_number_tool"
```

---

**Scenario C5 — Delivery Instructions Updated via Generic Tool**
```
Turn 1: User  → "please add a landmark to my order, delivery person can't find my flat"
        Graph → calls update_shopify_order_tool(order_id=..., update_type="instructions", ...)

→ event_type=rto_aversion, status=averted
   resolution=order_detail_update, aversion_method=tool_driven
```

---

### FAMILY D — Cancellation + RTO (Same Session, No Double-Count)

**Scenario D1 — User Says Cancel, Bot Fixes Address → Single Cancellation Aversion Event**
```
Turn 1: User  → "cancel my order, the address is wrong anyway"
        Graph → "No need to cancel! Let me fix the address instead."
        waiting_for_cancellation_reason=True
        Signal: state_flag → cancellation_aversion event OPENED

Turn 2: User  → "yes please fix it"
        Graph → calls update_order_address(...)
        AVERSION_TOOLS entry: update_order_address → ("averted", "address_fix")

→ event_type=cancellation_aversion, status=averted
   resolution=address_fix, aversion_method=tool_driven

RTO aversion branch DOES NOT fire because a cancellation intent session was already open.
```

The guard is explicit in the tracker:
> RTO detection only runs when `intent_open = False` (no cancellation session in Redis).
> This prevents double-counting the same turn as both cancellation AND rto aversion.

---

### FAMILY E — Cancellation Proceeded (for reference)

**Scenario E1 — Confirmed Cancellation**
```
Turn 1: User  → "cancel order GV10741"
        Graph → asks for confirmation

Turn 2: User  → "yes cancel it"
        Graph → calls cancel_order_in_shopify_tool(order_id="GV10741", ...)
        Tool succeeds

→ event_type=cancellation_aversion, status=cancelled, aversion_method=null
   closed in real-time on Turn 2
```

---

**Scenario E2 — Re-Asserted Cancellation (user refused alternatives)**
```
Turn 1: User  → "cancel my order"
        Graph → "Would you like to exchange instead?"

Turn 2: User  → "no I definitely want to cancel"
        Graph → calls cancel_order_in_shopify_tool(...)

→ event_type=cancellation_aversion, status=cancelled
   Intent was re-asserted after alternative offer — still tracked.
   The UI can show: "bot tried to retain, customer insisted on cancellation"
```

---

## Architecture Overview

```
Gupshup Webhook
      │
      ▼
process_webhook_payload()
      │
      ├── call_main_bot()  ←── LangGraph runs, tools fire, state updates
      │         │
      │         └── returns (reply, result_state)
      │
      └── background_tasks.add_task(analyze_turn, ...)   ← HOOK (non-blocking)
                    │
                    ▼
            analyze_turn(user_message, result_state, phone, client_id)
                    │
         ┌──────────┴──────────────────────────────────────────────┐
         │                                                          │
   INTENT DETECTION                                         OUTCOME DETECTION
   (runs every turn)                                        (runs only if Redis key exists)
         │                                                          │
   Layer 1: Free graph signals                           Hard outcome tools:
   - waiting_for_cancellation_reason                     - cancel_order_in_shopify_tool  → cancelled
   - detected_intents has cancellation                   - initiate_exchange_tool         → averted/exchange
   - last_tool_calls has cancel-related tools            - initiate_return_tool           → averted/return
                                                         - update_order_address_tool      → averted/address_fix
   Layer 2: LLM micro-classifier (only if L1 misses)    - change_order_product_tool      → averted/product_change
   - lightweight async LLM call                         - trigger_agent_escalation       → escalated
   - no hardcoded keywords
   - works in any language                      Soft intermediate signals (no outcome yet):
                                                - context_shift: detected_intents ≠ cancellation
   Layer 3: Retroactive backfill               - intent_reasserted: user re-asserted cancel
   - if cancel tool fired but no prior intent  - no_signal: neither intent nor outcome this turn
         │
         ▼
   Intent detected?
   ┌─────┴──────────────────────────────────────────────────────┐
   │ YES                                                        │ NO
   ▼                                                            ▼
Redis GET key                                         Check retroactive backfill
   │                                                  (cancel tool fired with no prior intent)
   ├── NOT FOUND → INSERT DB (pending)                → open + immediately close as cancelled
   │              + Redis SET (TTL=90min)
   │
   └── FOUND → run outcome detection
                    │
         ┌──────────┴──────────────────────────────────────────────┐
         │ Hard outcome found                                       │ No hard outcome
         ▼                                                          ▼
   UPDATE DB (averted/cancelled/escalated)             Log intermediate signal in Redis
   + Redis DEL (event closed)                          (context_shift / intent_reasserted)
   aversion_method = tool_driven                       Redis key stays alive (TTL ticking)
                                                                    │
                                              ┌─────────────────────┘
                                              │  Session expires (Redis TTL=90min)
                                              │  OR background job (every 5 min)
                                              │  finds pending rows > 90min old
                                              ▼
                                    LLM CLASSIFIER runs on conversation_snapshot
                                    + intermediate_signals from Redis
                                              │
                                    Returns verdict: averted / cancelled / abandoned
                                    Returns resolution: llm_persuasion / context_shift /
                                                        implicit_drop / unclear
                                              │
                                    UPDATE DB row
                                    llm_classified=true, llm_verdict, llm_confidence
                                    aversion_method = llm_driven (if averted)
```

---

## Key Design Decision: Two-Path Outcome Detection

```
┌─────────────────────────────┬──────────────────────────────────────────────┐
│ Tool-Driven Path             │ LLM-Driven Path                             │
├─────────────────────────────┼──────────────────────────────────────────────┤
│ When: action tool fires      │ When: no action tool fires in entire session │
│ Timing: real-time, same turn │ Timing: deferred, at session end (90 min)   │
│ Confidence: deterministic    │ Confidence: probabilistic (0.0–1.0)         │
│ Cost: zero (signal from state│ Cost: 1 LLM call per pending event          │
│ Examples: exchange, return,  │ Examples: text persuasion, context shift,   │
│   address fix, product change│   implicit acceptance, silent drop          │
└─────────────────────────────┴──────────────────────────────────────────────┘
```

**The critical rule:**
> If a Redis key is alive and the session ends without a hard outcome tool ever firing,
> the LLM classifier ALWAYS runs. It is the only reliable way to detect pure-text aversion.
> Never try to guess pure-LLM aversion in real-time — too unreliable.

---

## Redis Session State

```
Key:    cancellation_intent:{client_id}:{phone_number}
TTL:    5400 seconds (90 minutes — matches context window)

Value (JSON):
{
  "event_id":             "uuid of the DB row",
  "intent_detected_at":   "2026-03-02T10:15:00Z",
  "intent_trigger_type":  "state_flag",
  "order_id":             "GV10741",
  "turn_count":           2,
  "tools_accumulated":    ["get_order_cancellation_reasons"],
  "intermediate_signals": ["context_shift"],   ← accumulated soft signals per turn
  "last_turn_at":         "2026-03-02T10:17:00Z"
}
```

### Redis Key Lifecycle

```
Intent detected    → SET key (TTL=90min)  + INSERT DB (status=pending)
Hard outcome found → DEL key              + UPDATE DB (averted/cancelled/escalated)

Soft signal only   → UPDATE key value     (accumulate intermediate_signals)
                     Redis key stays alive

Redis TTL expires  → key gone             + background job queries:
                                            SELECT * FROM cancellation_aversion_events
                                            WHERE status='pending'
                                            AND intent_detected_at < NOW() - '90 min'
                                          → LLM classifies → UPDATE DB

Redis down         → DB fallback:          SELECT pending events for this phone+client
                                            within last 90 min
```

---

## Intent Detection: 3-Layer Stack (No Hardcoding)

| Layer | Signal | Source | Extra Cost |
|-------|--------|--------|------------|
| 1 | `waiting_for_cancellation_reason == True` | graph result state | free |
| 1 | `detected_intents` contains cancellation | graph result state | free |
| 1 | `last_tool_calls` has `store_cancellation_reason` or `get_order_cancellation_reasons` | graph result state | free |
| 2 | LLM micro-classifier on user message | async LLM call | ~1 small call, only on L1 miss |
| 3 | `cancel_order_in_shopify_tool` fired (retroactive) | outcome tools | free backfill |

Layer 2 prompt — no keywords, works in any language:
```
"Does the following customer message express an intent to cancel an order?
 Answer JSON only: { "intent": true/false, "confidence": "high"|"medium"|"low" }
 Message: {user_message}"
```

---

## Outcome Signals

### Hard Signals — Cancellation Aversion (real-time close, event_type=cancellation_aversion)

| Tool Called | status | resolution |
|-------------|--------|------------|
| `cancel_order_in_shopify_tool` | `cancelled` | — |
| `cancel_order_in_shiprocket_tool` | `cancelled` | — |
| `initiate_exchange_tool` / `create_exchange_order_tool` | `averted` | `exchange` |
| `initiate_return_tool` | `averted` | `return` |
| `update_order_address` / `update_order_address_tool` | `averted` | `address_fix` |
| `change_order_product_tool` | `averted` | `product_change` |
| `trigger_agent_escalation` | `escalated` | `escalation` |

### Hard Signals — RTO Aversion (always immediate close, event_type=rto_aversion)

These only fire when **no cancellation intent session is open** (to avoid double-counting).

| Tool Called | resolution |
|-------------|-----------|
| `update_order_address` | `address_update` |
| `update_order_shiprocket` | `address_update` |
| `update_shopify_order_tool` | `order_detail_update` |
| `update_order_phone_number_tool` | `phone_update` |
| Multiple RTO tools in same turn | `multi_field_update` |

### Soft Signals (accumulated in Redis, fed to LLM classifier)

| Signal | How Detected | Meaning |
|--------|-------------|---------|
| `context_shift` | `detected_intents` in result ≠ cancellation after intent open | User moved on |
| `intent_reasserted` | L1/L2 fires again on new turn | User doubling down |
| `no_signal` | Neither intent nor outcome this turn | Bot is mid-flow |

### LLM Classifier Verdicts (deferred)

| Verdict | Resolution | Scenario |
|---------|-----------|----------|
| `averted` | `llm_persuasion` | Bot used text to retain (B1, B3, B5) |
| `averted` | `context_shift` | User shifted topic, dropped cancel (B2) |
| `averted` | `implicit_drop` | User accepted without explicit confirmation (B4 borderline) |
| `cancelled` | — | Cancellation was confirmed in text, tool was missed |
| `abandoned` | — | User went silent, outcome unclear (B4) |
| `unclear` | — | Cannot determine with confidence |

---

## Database Table

```sql
CREATE TABLE cancellation_aversion_events (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id                UUID NOT NULL,

    -- 'cancellation_aversion' → user said cancel, bot averted
    -- 'rto_aversion'          → bot fixed delivery details, RTO prevented implicitly
    event_type               VARCHAR(30) NOT NULL DEFAULT 'cancellation_aversion',

    -- conversation_id: stored as plain string, NOT FK-linked (informational only)
    conversation_id          VARCHAR(100),
    phone_number             VARCHAR(20),
    order_id                 VARCHAR(100),

    -- Trigger (Moment A)
    intent_detected_at       TIMESTAMPTZ NOT NULL,
    intent_trigger_msg       TEXT,
    intent_trigger_type      VARCHAR(30),
    -- cancellation_aversion: 'state_flag' | 'tool_signal' | 'detected_intent' | 'llm_classifier' | 'retroactive'
    -- rto_aversion:          'order_update_tool'
    intent_confidence        VARCHAR(10),     -- 'high' | 'medium' | 'low'

    -- Outcome (Moment B)
    status                   VARCHAR(20) NOT NULL DEFAULT 'pending',
    -- 'pending' | 'averted' | 'cancelled' | 'escalated' | 'abandoned'
    -- rto_aversion events are always 'averted' (opened+closed same turn)

    resolution               VARCHAR(50),
    -- cancellation_aversion tool-driven:  'exchange' | 'return' | 'address_fix' | 'product_change' | 'escalation'
    -- cancellation_aversion llm-driven:   'llm_persuasion' | 'context_shift' | 'implicit_drop'
    -- rto_aversion:                       'address_update' | 'phone_update' | 'order_detail_update' | 'multi_field_update'
    -- null when cancelled or abandoned

    aversion_method          VARCHAR(20),
    -- 'tool_driven'  → hard signal, action tool confirmed aversion/rto-fix
    -- 'llm_driven'   → no tool, LLM classifier confirmed cancellation aversion
    -- null when cancelled / escalated / abandoned

    -- comma-separated for multi-tool RTO events (e.g. "update_order_address,update_order_phone_number_tool")
    outcome_trigger_tool     VARCHAR(255),
    resolved_at              TIMESTAMPTZ,

    -- LLM classification (only for cancellation_aversion pending events — rto_aversion never needs this)
    llm_classified           BOOLEAN DEFAULT FALSE,
    llm_verdict              VARCHAR(20),     -- 'averted' | 'cancelled' | 'abandoned' | 'unclear'
    llm_confidence           FLOAT,           -- 0.0 – 1.0
    llm_reasoning            TEXT,

    -- Evidence
    tools_called             JSONB,           -- all tools across every turn in this flow
    intermediate_signals     JSONB,           -- soft signals: ["context_shift", ...]  (cancellation_aversion only)
    conversation_snapshot    JSONB,           -- message pairs from the relevant window
    turn_count               INT DEFAULT 0,   -- rto_aversion always = 1
    metadata                 JSONB,           -- trace_id, gupshup_source, etc.

    created_at               TIMESTAMPTZ DEFAULT NOW(),
    updated_at               TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX ON cancellation_aversion_events (client_id, created_at);
CREATE INDEX ON cancellation_aversion_events (client_id, status);
CREATE INDEX ON cancellation_aversion_events (event_type, client_id, created_at);
CREATE INDEX ON cancellation_aversion_events (phone_number, client_id);
CREATE INDEX ON cancellation_aversion_events (status) WHERE status = 'pending';
CREATE INDEX ON cancellation_aversion_events (aversion_method, client_id);
CREATE INDEX ON cancellation_aversion_events (intent_detected_at)
    WHERE status = 'pending' AND llm_classified = FALSE;
```

---

## Worked Example: Tool-Driven Aversion (Turn-by-Turn)

### Conversation
```
Turn 1: User → "I want to cancel my order GV10741, delivery is taking too long"
        Bot  → "Before we cancel, would you like to exchange for faster shipping?"

Turn 2: User → "ok let's do an exchange"
        Bot  → "Done! Exchange initiated for GV10741."
```

### Turn 1 — Webhook fires
```
1. call_main_bot() runs → graph sets waiting_for_cancellation_reason=True

2. background analyze_turn() runs:
   Layer 1: waiting_for_cancellation_reason=True → HIGH confidence intent

3. Redis GET cancellation_intent:{client_id}:{phone} → nil (new session)

4. DB INSERT:
   status='pending', intent_trigger_type='state_flag',
   intent_trigger_msg="I want to cancel...", tools_called=["get_order_cancellation_reasons"]

5. Redis SET key → { event_id: "uuid-abc", turn_count: 1, ... }, TTL=5400s

6. Webhook response sent (tracking was background, zero delay)
```

### Turn 2 — Webhook fires
```
1. call_main_bot() runs → graph calls initiate_exchange_tool(order_id="GV10741")

2. background analyze_turn() runs:
   Outcome: initiate_exchange_tool in last_tool_calls → HARD SIGNAL

3. Redis GET → found: { event_id: "uuid-abc", ... }

4. DB UPDATE WHERE id="uuid-abc":
   status='averted', resolution='exchange', aversion_method='tool_driven',
   outcome_trigger_tool='initiate_exchange_tool', turn_count=2, resolved_at=NOW()

5. Redis DEL key — event closed

6. Webhook response sent
```

---

## Worked Example: LLM-Driven Aversion (Topic Shift, No Tools)

### Conversation
```
Turn 1: User → "cancel my order"
        Bot  → "Can I ask why? We may be able to help."

Turn 2: User → "actually forget it, do you have this jacket in black?"
        Bot  → "Yes! Here are the black variants..."
```

### Turn 1
```
1. Graph sets waiting_for_cancellation_reason=True
2. analyze_turn(): intent detected → Redis SET + DB INSERT (pending)
```

### Turn 2
```
1. Graph routes to product_inquiry node, calls product search tool
   detected_intents = ["product_inquiry"]
   NO cancellation or aversion tool called

2. analyze_turn():
   - Redis GET → found (open intent)
   - Outcome check: no hard outcome tool → soft signal detected
   - detected_intents shows "product_inquiry" → context_shift signal
   - Redis UPDATE: intermediate_signals=["context_shift"], turn_count=2
   - DB UPDATE: turn_count=2, tools_called=[...], conversation_snapshot updated

3. Event stays pending (no hard close yet)
```

### 90 Minutes Later — Session Expires
```
1. Redis TTL auto-fires → key deleted
2. Background job (every 5 min) queries:
   SELECT * FROM cancellation_aversion_events
   WHERE status='pending' AND intent_detected_at < NOW() - INTERVAL '90 minutes'

3. LLM classifier runs on conversation_snapshot:
   Input: [ user: "cancel my order", bot: "Can I ask why?",
            user: "actually forget it, do you have this jacket in black?",
            bot: "Yes! Here are the black variants..." ]
   intermediate_signals: ["context_shift"]

   Prompt:
   "Analyze this conversation. Did the user intend to cancel an order?
    Was the cancellation averted, completed, or abandoned?
    If averted, how? Answer JSON:
    { verdict, resolution, confidence, reasoning }"

4. LLM response:
   { verdict: "averted", resolution: "context_shift", confidence: 0.82,
     reasoning: "User expressed cancel intent then unprompted shifted to product inquiry.
                 Order was not cancelled. Bot's clarifying question likely prompted reconsideration." }

5. DB UPDATE:
   status='averted', resolution='context_shift', aversion_method='llm_driven',
   llm_classified=true, llm_verdict='averted', llm_confidence=0.82,
   llm_reasoning="User expressed cancel intent..."
```

---

## Files to Build

```
fashion_bot/
  analytics/
    __init__.py
    CANCELLATION_AVERSION_ARCHITECTURE.md     this file

    cancellation_aversion_tracker.py          core module (called per webhook turn)
        ├── _get_redis_client()               reuses pattern from gupshup_webhook.py
        ├── _redis_get_intent(phone, cid)     GET with DB fallback
        ├── _redis_set_intent(phone, cid, v)  SET with TTL=5400s
        ├── _redis_update_intent(phone, cid)  UPDATE intermediate_signals, turn_count
        ├── _redis_del_intent(phone, cid)     DEL on hard close
        ├── _extract_intent_signals(result)   Layer 1 — reads graph result dict
        ├── _llm_classify_intent(msg)         Layer 2 — LLM micro-classifier
        ├── _extract_outcome_signals(result)  hard outcome tools from last_tool_calls
        ├── _extract_soft_signals(result)     context_shift, intent_reasserted detection
        ├── _open_event(...)                  DB INSERT + Redis SET
        ├── _update_event_signals(...)        DB UPDATE turn_count + snapshot per turn
        ├── _close_event(...)                 DB UPDATE final status + Redis DEL
        └── analyze_turn()                    main entry point (background task)

    cancellation_classifier.py                LLM fallback for session-end classification
        ├── classify_event(event_id, snap, signals)   runs LLM on full snapshot
        ├── run_pending_classifications()             batch job for pending > 90min events
        └── _build_classifier_prompt(snap, signals)   constructs LLM prompt

  Tables/
    cancellation_aversion_table.py            DB migration — CREATE TABLE + indexes
```

---

## Integration Point in gupshup_webhook.py

One `background_tasks.add_task` after `call_main_bot()` — zero latency impact:

```python
# After: reply, langsmith_trace_id = call_main_bot(...)

from fashion_bot.analytics.cancellation_aversion_tracker import analyze_turn

background_tasks.add_task(
    analyze_turn,
    user_message = message_content,
    result_state = result,          # full state dict from graph.invoke()
    bot_response = reply,           # bot's text reply (for LLM snapshot)
    phone_number = sender_phone,
    client_id    = client_id,
    trace_id     = trace_id,
)
```

`result` contains `conversation_context`, `waiting_for_cancellation_reason`,
`detected_intents`, `last_tool_calls`, and everything else needed.

---

## What the UI Dashboard Gets From DB

### Cancellation Aversion KPIs

| Query | Metric |
|-------|--------|
| `WHERE event_type='cancellation_aversion' AND status='averted'` | Cancellations averted |
| `WHERE event_type='cancellation_aversion' AND status='cancelled'` | Cancellations completed |
| `averted / (averted + cancelled)` — cancellation_aversion only | **Aversion rate %** |
| `WHERE aversion_method='tool_driven'` | Hard aversions (exchange / return / product change) |
| `WHERE aversion_method='llm_driven'` | Soft aversions (pure text / topic shift) |
| `GROUP BY resolution` | Exchange vs Return vs Address fix vs LLM persuasion breakdown |
| `GROUP BY DATE(created_at)` | Day-by-day trend |
| `AVG(turn_count) WHERE status='averted'` | Avg turns to avert |
| `WHERE intermediate_signals @> '["context_shift"]'` | Topic-shift aversions |

### RTO Aversion KPIs

| Query | Metric |
|-------|--------|
| `WHERE event_type='rto_aversion'` | Total delivery-fix events (all = averted) |
| `WHERE event_type='rto_aversion' AND resolution='address_update'` | Address fixes |
| `WHERE event_type='rto_aversion' AND resolution='phone_update'` | Phone fixes |
| `WHERE event_type='rto_aversion' AND resolution='multi_field_update'` | Multi-field fixes |
| `GROUP BY DATE(created_at)` — rto_aversion only | Day-by-day RTO prevention trend |

### Combined "Orders Saved" KPI

| Query | Metric |
|-------|--------|
| `WHERE status='averted'` — both event types | **Total orders saved by bot** |
| `WHERE event_type='cancellation_aversion' AND status='averted'` | Saved from explicit cancellation |
| `WHERE event_type='rto_aversion'` | Saved from delivery failure |

### Common Filters

| Filter | Use |
|--------|-----|
| `WHERE client_id = X` | Per-client filtering |
| `WHERE created_at BETWEEN X AND Y` | Date range |
| `conversation_snapshot` | Full replay in UI |

---

## What Is Explicitly Out of Scope

- ❌ API endpoints (built separately in the UI codebase)
- ❌ Cross-session tracking (conversations > 90 min apart are separate events)
- ❌ conversation_id FK constraint (stored as plain string, no join enforced)
- ❌ Revenue saved calculation (can be added later by joining order_id → Shopify order value)
- ❌ Real-time push to dashboard (DB polling is sufficient)
