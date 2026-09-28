# Escalation Follow-Up Context (Agent-Only)

> Design doc for making the conversational agent **aware of a customer's open and
> recently-closed escalations** — as internal context the customer never sees — so
> that a returning customer's message is answered correctly: as a follow-up on a
> pending hand-off, as an acknowledgement of a human-delivered resolution, or as a
> completely unrelated question.

**Status:** Proposed
**Owner:** Escalation / Conversation Runtime
**Related code:** `utils/escalation_logger.py` (`alog_escalation`), `utils/escalation_helper.py`
(`alog_escalation_from_state`, `build_escalation_metadata`), `core/orchestrator.py`
(`EscalationOrchestrator.aescalate_to_agent`), `nodes/generic_skill_node.py` (`system_blocks`),
`tool_factory.py` (`_create_escalation_tool`), `agent_config.py` (`normalize_escalation_category`),
`utils/tiered_cache.py`, `workers/idempotency.py`
**Related docs:** `ESCALATION_MULTI_NUMBER_AGENT_ROUTING.md`, `ESCALATION_GUPSHUP_TEMPLATES.md`

---

## 1. Problem

Escalation is currently **write-only from the bot's point of view**. `alog_escalation`
inserts a row into `escalations` with `status='unresolved'`; a human later flips it to
`resolved` from the ops dashboard (apiandui `EscalationService.mark_as_resolved`). The
bot never reads any of it back.

So when the customer returns — which they always do, because they are waiting on a human —
the agent starts from zero:

| Customer says | What happens today | What should happen |
|---|---|---|
| "any update?" (2h after a Delivery Query escalation, still unresolved) | Agent has no idea an escalation exists. Re-runs order lookup, repeats the same tracking line the customer already rejected, or escalates again as a *fresh* low-priority ticket. | Acknowledge the pending hand-off honestly, re-escalate as a **repeat follow-up with `immediate_attention=True`**. |
| "any update?" (after ops resolved it with `resolution_text = "reverted and dispatch today"`) | Same blind lookup. The human's actual action is invisible. | Answer *from* the resolution the human recorded. |
| "do you have this kurta in L?" (with an unresolved escalation open) | Correct by accident. | Stay correct — answer the product question and **say nothing** about the escalation. |

The third row is the trap: a naive implementation that dumps escalation state into the
prompt makes the agent open every reply with "our team is still looking into your delivery
issue" when the customer just asked about a size. The design has to make the *classification*
step explicit and mandatory, not incidental.

### Goals

1. Every open (and recently closed) escalation for the customer is visible **to the agent**, on every turn, for every skill.
2. **Never visible to the customer** — not in the transcript, not in the message history, not quotable.
3. The agent decides *first* whether the incoming message is about the escalation or not, and behaves differently in each branch.
4. A genuine follow-up on an **unresolved** escalation re-escalates with `immediate_attention=True`.
5. A follow-up on a **resolved** escalation answers from what the human actually did.
6. Minimal diff. New logic lives in a new module; hot files get an append, not a rewrite.

### Non-goals

- No change to the apiandui service. (See §3 — none is needed.)
- No change to how escalations are raised, routed, or notified (`ESCALATION_MULTI_NUMBER_AGENT_ROUTING.md` stands).
- No new table, no new column, no migration.
- No change to intent detection or graph routing.
- Not a customer-facing "ticket status" feature. There is no ticket ID surfaced to anyone outside staff.

---

## 2. Key insight — the shared event log already exists

The requirement says "whenever an escalation is opened **or closed** it should go to the
agent context". The instinct is to build a notification path: bot → queue → state on open,
apiandui → webhook → bot on close.

That is unnecessary. **The `escalations` table is already the cross-service event log**, and
both transitions are already durably recorded in it:

| Event | Already written by | Columns that carry it |
|---|---|---|
| Opened | `alog_escalation` (this repo) | `escalation_id`, `category`, `escalation_date`, `status='unresolved'`, `escalation_metadata` |
| Closed | apiandui `mark_as_resolved` | `status='resolved'`, `resolved_date`, `resolution_text` |
| Progress update | apiandui "Add Progress Update" | `comments[]` (free text + author + timestamp), `resolution_status` (e.g. `in_progress`) — **`status` stays `unresolved`** |

So the entire feature reduces to **one cached read on the conversation path**. No producer
changes, no consumer changes, no cross-service contract, nothing to keep in sync. Pull beats
push here because the DB write already happened before either event could be published.

```
 ┌──────────────── bot (this repo) ─────────────────┐        ┌──── apiandui ────┐
 │  aescalate_to_agent → alog_escalation ──INSERT──►│        │                  │
 │                              │                   │        │  Resolve Issue   │
 │                              └─ bust cache key   │        │       │          │
 │                                                  │        │    UPDATE        │
 │  next turn:                                      ▼        ▼       │          │
 │  generic_skill_node ──► aget_escalation_snapshot ──► [ escalations ] ◄───────┘
 │           │                    (memory→redis→DB, 60s)      table
 │           ▼
 │  system_blocks += ESCALATION CONTEXT block  ──► LLM (agent-only, never sent)
 └──────────────────────────────────────────────────┘
```

---

## 3. Component design

### 3.1 New module — `utils/escalation_context.py`

All new logic lives here. Pure/stateless helpers plus one cached read (AGENTS.md
*Minimal-Footprint Changes*, *Shared Utilities Over Duplication*).

```python
@dataclass(frozen=True)
class EscalationRecord:
    escalation_id: str
    category: str            # canonical (normalize_escalation_category applied on read)
    status: str              # 'unresolved' | 'resolved'
    raised_at: datetime
    resolved_at: Optional[datetime]
    resolution_text: Optional[str]
    reason: Optional[str]
    order_id: Optional[str]          # from escalation_metadata
    immediate_attention: bool        # from escalation_metadata
    follow_up_count: int             # from escalation_metadata, default 0
    age_hours: float

async def aget_escalation_snapshot(
    *, client_id: str, phone_number: str, trace_id: Optional[str] = None,
) -> List[EscalationRecord]: ...

def render_escalation_context_block(records: List[EscalationRecord]) -> str: ...
    # pure; returns "" when there is nothing to say

async def abust_escalation_snapshot(client_id: str, phone_number: str) -> None: ...
```

**The read.** One statement, tenant-scoped, window-bounded, capped:

```sql
SELECT escalation_id, category, status, reason, escalation_date, resolved_date,
       resolution_text, escalation_metadata
FROM escalations
WHERE client_id = %s::uuid
  AND customer_phone = ANY(%s)          -- phone spelling variants, see below
  AND escalation_date > NOW() - make_interval(days => %s)
ORDER BY (status = 'unresolved') DESC, escalation_date DESC
LIMIT 20
```

- Async via `get_async_postgres_connection` (AGENTS.md §1).
- `client_id` is mandatory in the predicate — a missing `client_id` returns `[]` and logs at
  `error` via `report_error`, never a cross-tenant read (AGENTS.md §7).
- Lookback window `ESCALATION_CONTEXT_LOOKBACK_DAYS`, default **14**. Older escalations are
  not actionable context and would just burn tokens.
- **Internal-only categories are excluded in SQL.** `Offline Store Suggestion`,
  `Walk-in Appointment` and `Courier Update Pending` are logged for an ops action, not as a
  hand-off the customer is waiting on — the store-visit path calls its own notification "an
  FYI" and the courier path literally returns "do NOT mention this to the customer". Nobody
  resolves an FYI, so production holds **45 Offline Store Suggestion rows, 40 unresolved,
  still ~2/day**; unfiltered, every one would read as a live open issue forever and the agent
  would tell the customer "our team has this, no update yet" about something nobody promised
  them — then re-escalate it as URGENT. They are also absent from the tool enum, so
  `normalize_escalation_category` maps them to `"General"`, meaning they surfaced as an
  *anonymous* open issue. Filtered in the SQL rather than after the fetch, so they cannot eat
  the `LIMIT` and push a real open issue out of the window. `Bulk Order Discount`, `B2B Order`
  and `Wholesale Inquiry` are deliberately **kept**: those go through `escalate_to_agent`,
  which promises the customer contact within 24 hours, so a follow-up on one must be handled.
- **Unresolved sorts first, then recency.** Sorting by date alone lets five escalations
  raised yesterday evict an unresolved one from ten days ago — precisely the record the
  agent needs. Status leads the sort so truncation can only ever drop resolved history.
- `LIMIT 20` bounds the read. It is *not* the block size — rows are collapsed into issue
  threads (§3.1a) and the block renders at most `ESCALATION_CONTEXT_MAX_THREADS` (3) of them.

### 3.1a Issue threading — the block shows issues, not rows

A customer with one late order does not have one escalation. In the last 90 days of
production WhatsApp escalations (1,652 rows):

| | count | share |
|---|---|---|
| ≥1 other escalation from the same customer in the prior 14 days | 869 | 53% |
| **>1 unresolved** in the window | 848 | 51% |
| >1 unresolved across **different categories** | 661 | 40% |
| More than 5 in the window | 197 | 12% |
| Worst case in a single 14-day window | 37 | |

The multi-escalation customer is the **majority case, not the tail**. And the multiple rows
are usually one issue wearing different labels: of the 863 windows containing other
unresolved escalations, **532 (62%) share an `order_id`** with the current one — a single
delayed order logged as `Delivery Query`, then `Order Delivery Delayed`, then `Frustration`
as the LLM's category choice drifts turn to turn. Only 188 (22%) genuinely span multiple
orders; 153 (18%) carry no `order_id` at all.

So category is the wrong grouping key — it fragments one problem into three entries and
makes "re-escalate under the same category as [E1]" meaningless. Rows are clustered into
**issue threads** in the pure renderer:

```python
def _thread_key(rec: EscalationRecord) -> str:
    if rec.order_id:
        return f"order:{rec.order_id}"          # 62% of the multi case
    return f"cat:{_slug(rec.category)}"         # order-less: fall back to category
```

Per thread: `status` = unresolved if **any** member is unresolved; `raised_at` = the
*earliest* member (how long the customer has really been waiting); `category` and
`resolution_text` = the *latest* member; `chase_count` = number of members beyond the first.
At most 3 render; any remainder is disclosed as a count, never silently dropped.

#### 3.1a-i The resolution-suppression incident (09-Aug-2026) and what it changed

The rules above shipped to staging and produced a customer-visible failure. A support agent
resolved the customer's issue at 16:41 IST with *"Talked to the customer. He is satisfied
now."* At 17:06 the customer asked **"What is the status of my complaint?"** and the bot
apologised for the continued delay and re-escalated with priority.

The snapshot was not at fault — LangSmith trace `019fe64f` shows `record_count: 15,
unresolved_count: 13`, and both resolved rows were in it, carrying `resolved_date` and the
resolution text. Three rules combined to throw that away:

1. **Bare-category grouping is far too coarse.** Both resolved rows were `Frustration` with
   no `order_id`, so their key was `cat:frustration` — the same key as seven *unrelated*
   open Frustration rows from 1–4 Aug. An `order_id` names one real issue; a category names
   "every complaint this customer has ever made".
2. **`status` = unresolved if any member is unresolved**, so one stale open row from a week
   earlier kept the whole thread open.
3. **`resolution_text = None if unresolved`** then discarded the recorded action outright.

Three changes, all of which had to land together:

- **Gap-split order-less threads** (`_split_orderless_on_gap`). Rows without an `order_id`
  start a new issue when separated by more than `ESCALATION_CONTEXT_THREAD_GAP_HOURS` (72h).
  Justified by the data: of consecutive same-category order-less pairs in the last 90 days,
  **89% arrive within an hour** and the median gap is **36 seconds** — a real issue is a
  burst of category drift. 72h keeps 99% of those pairs together while comfortably spanning
  the two-day follow-up this feature exists to serve. Order-bearing threads are never split.
  Split keys carry a `#<root-id>` suffix so thread-keyed state (the alert cooldown) cannot
  collide across two genuinely different issues — and `find_matching_thread` compares on the
  **base** key, or lineage would silently stop matching on every order-less follow-up.
- **Never discard a recorded action** (`partial_resolved_at` / `partial_resolution_text`).
  When a thread stays open but some member was resolved, the block now renders
  `STATUS: PARTLY RESOLVED`, quotes what was done, states how many earlier reports are still
  open, and instructs the agent to lead with the action and **not** re-escalate.
- **A fresh resolution outranks a stale open issue** in the sort. Splitting alone made this
  worse, not better: it produced a cleanly-resolved Aug-9 thread that then sorted *behind*
  three eight-day-old open threads and fell off the end of `_MAX_THREADS`. Ranking is now
  three tiers — resolved within `ESCALATION_CONTEXT_RECENT_RESOLUTION_HOURS` (24h, newest
  first), then unresolved (oldest first, so the longest-waiting issue still outranks stale
  resolved history), then older resolved.

**The two knobs are coupled.** Splitting is what lets a run resolve cleanly; the recency tier
is what stops that clean resolution being truncated away. Setting
`ESCALATION_CONTEXT_RECENT_RESOLUTION_HOURS=0` while splitting is on is worse than either
alone, and a test pins that combination so the coupling cannot be quietly removed.

The incident is replayed as a regression test from the exact 15 production rows, asserted
against the real pre-fix code to confirm it reproduces the failure.

#### 3.1a-ii The follow-up incident — the block was right and the routing rule was wrong

The fix above shipped (`a04bb810`) and the block came out correct: trace `019fe67b` shows
`record_count: 16, unresolved_count: 13`, and replaying those exact rows renders

```
[E1] Frustration · first raised 2026-08-09 16:23 IST (2 hours ago)
     STATUS: RESOLVED by the support team on 2026-08-09 17:54 IST
     Action taken: "Talked to the customer, explained him the cause. He is satisfied now."
```

The bot still replied *"I sincerely apologize for the delay… I have flagged your request
again as a priority."* Two separate faults, neither of them in the data:

1. **The routing rule steered the agent past the answer.** Step 1 said *"Same subject, no
   order named → the most recent UNRESOLVED issue"*. "Any update on my complaint?" names no
   order and is ambiguous, so the model did exactly what it was told: it skipped the
   resolved `[E1]` and answered about an open entry from a week earlier. The rule also
   contradicted the sort, which orders unresolved *oldest*-first, not most-recent. Step 1
   now says the entries are pre-ordered by likelihood, `[E1]` is the default, and a vague
   status question is explicitly **not** a reason to skip a resolved entry.
2. **The reply claimed an escalation that never happened.** There is no `escalate_to_agent`
   run anywhere in the trace — the tool was loaded (`tool_topped_up: true`) and simply never
   called, while the reply told the customer it had been. The block now forbids claiming to
   have flagged, escalated, prioritised or raised anything without the tool call on that
   turn.

Both are prompt-side, so both are pinned by tests asserting the instruction text rather than
behaviour — the honest limit of what a unit test can hold here, and the reason this feature
needs transcript review on a real tenant before going wide.

#### 3.1a-iii The real obstacle — a contradicting clause in the agent's own prompt

Trace `019fe689` ran on `817aef6` with the routing fix live, and the block was *entirely*
correct: `record_count: 16, unresolved_count: 1`, and every shown entry read
`STATUS: RESOLVED` with the action the team had recorded fifteen minutes earlier. The bot
still answered *"I understand you are still waiting for a resolution… I have re-escalated
your case to our support management team with the highest priority"* — and this time it did
call `escalate_to_agent`, writing a fresh ticket on a closed complaint.

The cause is not in this module. `agents_config.agent_prompt` for `escalation_handler`
(15,093 chars, `system_blocks[0]`, read *before* this block) contains:

> ⚠️ EXCEPTION — RE-ESCALATION: If the conversation history shows an escalation was ALREADY
> raised (a prior "someone will contact you" promise), AND the customer is now saying no one
> contacted them, or is still demanding action, or is expressing continued frustration —
> **escalate AGAIN immediately.** Do not de-escalate a customer who was already promised a
> callback/follow-up and didn't get one.

Every precondition matched, and the clause has **no notion of the issue having since been
closed**. The agent obeyed its role prompt over an appended context block — the third time
in this feature's life that a prompt-level rule beat the data.

Two responses:

- **In this module (done).** The block now leads with a `🟢 [E1] IS CLOSED` directive placed
  *above* the entries, which contradicts the standing rule by name and forbids apologising
  for a delay or re-escalating that entry. Keyed on the **lead** entry rather than
  "everything is closed", because a customer with any history nearly always has some
  unrelated item open and that must not license re-escalating the one they asked about. When
  nothing at all is open it hardens to "do not call `escalate_to_agent`".
- **In the client prompt (recommended, not done).** The durable fix is a precondition on the
  clause itself — *"…unless the escalation context shows that issue has since been resolved"*.
  That is tenant data in `agents_config`, not code, so it is proposed rather than applied.

This also exposed a regression introduced by the recency tier: with five issues resolved
inside the hour and one still open, every slot went to a resolved entry and the customer's
outstanding problem disappeared from the block — the exact mirror of the bug the tier was
added to fix. Selection now guarantees the highest-ranked **open** thread a slot whenever one
exists, so neither direction can starve the other.

#### 3.1a-iv The second schema drift — dashboard progress updates

The dashboard records a human action in **two** places, and only one of them touches
`status`. "Final Resolution" sets `status`/`resolved_date`/`resolution_text` — the pair this
module was built around. **"Add Progress Update" writes free text into `comments[]` and may
set `resolution_status` (`in_progress`) while the row stays `unresolved`.** That second path
was never read.

Production trace `019fe6a7` is the cost. Escalation `#30D002FF` was raised at 16:44 and a
teammate posted *"The product will be dispatched on august 9 at 7pm."* at 16:47 — three
minutes later. The snapshot reported `unresolved_count: 3` and the block told the agent
**"STATUS: UNRESOLVED — no human update recorded yet"** about that exact escalation.

`comments` and `resolution_status` are now in the `SELECT` (verified against prod: 0.18 ms,
still on `ix_escalation_customer_phone`). The newest comment becomes the thread's
`progress_note` and renders as:

```
[E1] Delivery Query · order gv15790 · first raised 2026-08-09 16:44 IST (2 hours ago)
     STATUS: OPEN — the team is working on it and posted an update on 2026-08-09 16:47 IST
     Team update: "The product will be dispatched on august 9 at 7pm."
     THIS is the update the customer is asking for. Restate it in your own words. Do NOT
     say there is no update. Do NOT re-escalate unless the customer says what it promised
     did not happen.
```

Deliberate choices:

- **`in_progress` is not `resolved`.** The thread stays open, keeps its `UNRESOLVED` status
  internally, and remains re-escalatable — if 7pm passes with nothing dispatched, the
  customer's next message can still escalate. Treating it as closed would have silenced that.
- **Keyed on the comment, not on `resolution_status`.** Of the rows carrying comments in
  production, four of five have `resolution_status` NULL — the note is the reliable signal.
- **`user_email` is dropped at the source.** It names a staff member and the block forbids
  revealing those; filtering at extraction means it cannot leak downstream. Asserted.
- **A fresh note is a "team action" for ranking**, alongside a fresh resolution — an issue a
  teammate touched ten minutes ago is at least as likely to be the subject as one nobody has
  looked at in a week.
- `get_escalations` gained `team_update` / `team_update_at` from the same field, so the tool
  and the block can never tell the agent different things.

This also fixes the 12% that blew past the old `LIMIT 5`: 37 rows for one customer collapse
to two or three threads.

**Phone spelling variants.** `customer_phone` is stored in whatever spelling the channel
handed us. Production today: 605 rows bare 10-digit, 724 rows with a country code, and
**110 customers whose escalation history is split across two spellings of their own number**.
An exact-string match would show a customer's escalation only half the time. So the query
matches a small variant set built from the existing helpers — `get_last_n_digits(phone, 10)`
and `strip_country_code` in `utils/phone_number_utils.py` — producing
`{bare10, "91"+bare10, "+91"+bare10}` plus the raw value, mirroring the normalisation
`agent_config._dedup_phones` already does for recipients. `= ANY(array)` keeps
`ix_escalation_customer_phone` usable; a `regexp_replace(...)` predicate would not.

Web-chat identities (`web_…` / `fbw_…`) are passed through unchanged — they are their own
exact key.

**Caching.** Through `aget_with_tiered_cache` (memory → Redis → DB):

- key `esc_ctx:{client_id}:{phone_last10_or_session}` — tenant-scoped (AGENTS.md §7)
- **Redis is the shared tier; memory is deliberately short-lived.** The memory tier is
  process-local, so `ainvalidate_tiered_cache_key` clears Redis globally but only the calling
  pod's memory. AGENTS.md §3 states that model outright — "bust Redis, then local cache
  expires via TTL" — which is fine for client configs on a 10-minute TTL and **not** fine
  here: a stale local `[]` would hide an escalation the bot had just raised and silently
  no-op the feature for any turn that landed on another pod. So the long TTLs live in Redis
  and the memory tier is capped at `ESCALATION_CONTEXT_MEMORY_TTL_SECONDS` (10s) — enough to
  collapse the two or three reads inside one turn, and no more. Worst-case cross-pod
  staleness after a bust is that value, not 900s. Note `aget_with_tiered_cache`'s
  `ttl_seconds` governs the **memory tier only**; the Redis TTL comes from `set_to_redis_fn`,
  which is what makes the split possible without touching the shared helper.
- **Asymmetric TTL.** A *positive* answer is cached for `ESCALATION_CONTEXT_TTL_SECONDS`
  (60s), because a human can flip `status` at any moment and that bounds how long a
  just-resolved escalation still reads as unresolved. An *empty* answer — the response on
  ~95% of turns — is re-primed into both tiers with
  `ESCALATION_CONTEXT_EMPTY_TTL_SECONDS` (900s) instead. That is safe for a reason specific
  to this data: an empty result can only stop being true when an escalation is *inserted*,
  every insert path in this repo busts the key, and nothing a third party does can turn
  "no escalations" into "one escalation" behind our back. It is bounded rather than infinite
  only because apiandui exposes a `POST /escalations` that could in principle insert
  out-of-band. Without this split the 60s TTL would expire between almost every pair of
  customer messages, so the common case would pay a database round trip *every turn*.
- explicitly busted by `abust_escalation_snapshot` right after `alog_escalation` returns an
  id, so an escalation raised on turn *N* is visible on turn *N+1* with no TTL wait

Single-flight (one turn at a time per user) means there is no thundering herd on the key.

**Latency: the read is prefetched, not inline.** `generic_skill_node` starts
`aprefetch_escalation_records` as an `asyncio.create_task` the moment `client_id` is
resolved, and awaits it ~300 lines later when it assembles the prompt. Everything in
between — prompt fetch, tool loading, LLM resolution, store-context lookup — is awaited
I/O, so the snapshot rides along with work already in flight rather than adding to the turn
(AGENTS.md §1: concurrent independent I/O). By the time the block is built the task has
almost always finished, making it a no-wait handoff. The prefetch wrapper can never raise —
the task is abandoned if the node returns early, and an unretrieved task exception would
surface as a noisy asyncio warning.

### 3.2 Injection — `nodes/generic_skill_node.py`

`generic_skill_node` is the single funnel every skill agent passes through, and
`system_blocks` (line ~1266) is where every other internal instruction block is already
assembled (`CART_ACTION_GUARDRAIL_INSTRUCTION`, `channel_context_instruction`,
`product_link_instruction`, …). This feature follows the identical pattern — that is the
whole change to this file:

```python
# 1.9 Open-escalation context (internal, agent-only). Appended last so it sits
# closest to the user turn. Empty string ⇒ block omitted ⇒ prior behaviour.
escalation_context_block = ""
if escalation_context_enabled:
    try:
        _records = await aget_escalation_snapshot(
            client_id=client_id, phone_number=_phone, trace_id=get_trace_id(state)
        )
        escalation_context_block = render_escalation_context_block(_records)
    except Exception as _esc_err:
        log_with_trace_id(state, f"⚠️ [escalation_context] injection failed: {_esc_err}", "warning")

...
system_blocks += [escaped_summary_instruction, context_message]
if escalation_context_block:
    system_blocks.append(escalation_context_block)
```

**Why a system block and not a message.** The block must be agent-only. It therefore must
NOT be:

- appended to `state["messages"]` — it would be replayed as an assistant turn and could be
  paraphrased straight to the customer;
- written via `astore_conversation_event` — that is what the existing
  `[Escalation] {category}: {reason}` write at `orchestrator.py:2128` does, and those rows
  land in the customer transcript the ops dashboard renders.

A system block is invisible to the transcript, invisible to the widget, and never echoed
unless the model chooses to quote it — which the block explicitly forbids.

### 3.2b `get_escalations` — on-demand lookup for a direct question

The block is injected every turn, so the agent already knows about open issues without
asking. `get_escalations` exists for the narrower case where the customer asks point-blank
("what happened to my complaint?") and the agent wants structured fields rather than prose.

It is a **projection of the same cached bundle**, not a second query path — same 14-day
window, same internal-category filter, same issue threading, same caches. Within a turn the
prefetch has already warmed them, so a call costs no extra database work, and "what counts as
an escalation" stays decided in one place.

Returns `found`, `count`, and `issues[]` with `what_it_is_about`, `category`, `order_id`,
`status`, `raised_at_ist`, `waiting_hours`, `times_chased`, `resolution_recorded`,
`resolution` — plus `human_replies_since_raised[]` and a `guidance` string carrying the same
rules as the block.

**No internal ids are returned.** A ticket id must never reach the customer: the id is a UUID
that cannot be read out over WhatsApp, one issue produces many rows so there is no single id
to quote, and with a 27% resolution rate a reference number promises accountability the
process does not deliver. The agent refers to an issue by its order or subject instead. A
test asserts no `escalation_id` appears anywhere in the payload.

Registered on the `escalation` agent (`core/tool_registry._get_escalation_tools`). Adding it
to another agent is one line, but the block already covers those agents.

**Why the 14-day window matters here more than anywhere else.** 2,090 of the 2,505 unresolved
escalations are older than 14 days, and with only 27% of escalations ever resolved those are
abandoned records rather than live issues. A lookup that reached past the window would let the
agent tell a customer "our team has it, no update yet" about something nobody has touched in
months. The window is what keeps the tool honest.

### 3.3 Tool-availability guarantee

Only 6 of the ~17 registered agents load `escalate_to_agent` today (`order_status`,
`return_exchange`, `cart_management`, `product_details`, `cancel_or_update_order`,
`escalation`). A follow-up routed to `delivery_timeline`, `discount`, a policy agent or
`unknown` would *see* the unresolved escalation and have **no way to act on it** — the
instruction in §4 step 3b would be unfulfillable.

Fix, at the same injection site, reusing the existing factory (no factory edits, no registry
edits):

```python
if _has_unresolved(_records) and not any(getattr(t, "name", "") == "escalate_to_agent" for t in (tools or [])):
    from fashion_bot.tool_factory import _create_escalation_tool
    tools = list(tools) + [_create_escalation_tool(state, agent=agent_name)]
```

Scoped deliberately: the tool is added **only** when this customer actually has an
unresolved escalation, so the tool surface of every other conversation is unchanged.

### 3.4 Re-escalation, and the loop guard

The requirement is explicit: a follow-up on an unresolved escalation should "again raise an
escalation with immediate attention required". So the follow-up creates a **new row** — not
a mutation of the old one — which keeps the write path exactly as it is today and threads
cleanly into the ops dashboard's `(phone, date)` merge grouping.

The danger is obvious: a frustrated customer sends five messages in two minutes and staff
get five URGENT WhatsApp alerts. Guard it in `aescalate_to_agent`, where the category is
already normalised.

**Alerting stays immediate.** The guard is reached only when the escalation repeats an
already-open issue, so a new issue always alerts. And the customer's *first chase* is the
call that **sets** the key, so it always alerts too, marked urgent. Only the third and later
messages on one issue inside the window go quiet, and the row is written either way. The
window is deliberately short — of same-thread repeats in the last 90 days, **75% arrive
within 5 minutes and 83% within 15**, while the tail out to 3 hours is a customer coming
back later, which deserves a ping. Hence a 15-minute default rather than hours;
`ESCALATION_FOLLOWUP_COOLDOWN_MINUTES=0` disables suppression entirely.

- **Cooldown gates the NOTIFICATION, never the row.** Before
  `asend_escalation_notification`, check a Redis key
  `escalation_followup:{client_id}:{phone10}:{thread_key}` where `thread_key` is the §3.1a
  key (`order:<id>`, else `cat:<slug>`). If set, skip the **staff alert** and continue —
  `alog_escalation` still inserts the row exactly as today. TTL
  `ESCALATION_FOLLOWUP_COOLDOWN_MINUTES`, default **15**; `0` disables suppression.

  An earlier draft suppressed the insert too. Measured against production, that would have
  dropped **348 of the last 1,919 escalations (18%)** — every one of them a same-thread
  repeat, i.e. exactly the spam we want to stop, but also 18% off the dashboard's escalation
  counts and resolution-rate denominators. Silently changing a metric the business reads is
  not an acceptable side effect of an alerting fix. Gating only the notification gets the
  whole benefit with none of the data loss: the table keeps the complete record, and because
  the ops dashboard merges by `(phone, date)` the chases surface as a rising
  `escalation_count` on one card — a *better* urgency signal than five separate pings.

  Keying on the thread rather than the category matters: 62% of multi-escalation windows are
  one order wearing several category labels, so a category-keyed cooldown would let the same
  late order raise three URGENT alerts in one conversation simply because the LLM picked
  `Delivery Query` on one turn and `Order Delivery Delayed` on the next. An order-keyed
  cooldown collapses those into one.
- **Reuse the shared guard.** `workers/idempotency.already_processed` is exactly this
  primitive (`SET NX EX`, fail-open, shared Redis client) but hardcodes
  `WEBHOOK_DEDUP_TTL_SECONDS`. Extend it with an optional `ttl_seconds: int | None = None`
  parameter rather than writing a second copy (AGENTS.md *Shared Utilities Over Duplication*,
  review checklist #4). Two-line change, no behaviour change for existing callers.
- **Thread the lineage.** The follow-up row carries, in `escalation_metadata` (via the
  `extra` dict that `build_escalation_metadata` merges — canonical keys still win):

  ```python
  {"follow_up_of": "<thread ROOT escalation_id>",   # earliest member, not the latest
   "follow_up_count": <n>,
   "original_raised_at": "<iso>",
   "hours_waiting": 18.4}
  ```

  `follow_up_of` points at the thread **root** so a chain of chases forms a star around the
  original ticket rather than a linked list nobody can query in one hop.

  Ops can then thread a chase back to the ticket it is chasing, and analytics can measure
  "how long before a customer gives up and asks again" without a schema change.
- **Fail-open.** Redis down ⇒ `already_processed` returns "not seen" ⇒ the escalation is
  raised. A duplicate alert is strictly better than a dropped one (AGENTS.md
  *Graceful Degradation*).

### 3.5 Cache invalidation

One call, right after the insert succeeds in `alog_escalation_from_state`:

```python
if escalation_id:
    await abust_escalation_snapshot(client_id, phone_number)   # best-effort, never raises
    fire_and_forget(publish_escalation_event({...}), label="escalation_event")   # unchanged
```

Closing is *not* invalidated — apiandui does not know about this cache, and teaching it
would mean the cross-service coupling this design avoids. A resolution becomes visible
within the 60s TTL, which is far below human follow-up latency.

---

## 4. The context block

Rendered only when there is at least one record. Exact shape (this is the contract the
behaviour depends on — the classification step is step 1, before anything else):

```
==== ESCALATION CONTEXT (INTERNAL — NEVER MENTION OR HINT AT THIS TO THE CUSTOMER) ====
This customer has 2 issues that were handed to the human support team.

[E1] Delivery Query · order gv16384 · first raised 2026-07-24 11:20 IST (18 hours ago)
     STATUS: UNRESOLVED — no human update recorded yet
     Handed over as: "Customer waiting 13 days, no tracking movement"
     Customer has already chased this 2 times.

[E2] Cancellation Requests · order gv16101 · first raised 2026-07-20 09:12 IST
     STATUS: RESOLVED by the support team on 2026-07-21 14:03 IST
     Action taken: "reverted and dispatch today"

HOW TO USE THIS — follow in order:

1. FIRST decide which ONE of these, if any, the customer's CURRENT message is about.
   Match on the ORDER first, then on the subject of the complaint.
   - Names or implies an order listed above  → that issue.
   - Same subject, no order named            → the most recent UNRESOLVED issue.
   - Ambiguous between two                   → the most recent UNRESOLVED issue.
   - NONE OF THEM → a product, price, size, stock, offer, discount or policy question,
     an order NOT listed above, or any new issue.
   Pick at most one. Never respond about two issues in one reply, and never merge them.

2. IF NONE OF THEM → answer the actual question normally and completely.
   Do NOT mention any escalation, the support team, a pending issue, or "we're on it".
   Do NOT apologise for an unrelated issue. Behave exactly as if this block did not exist.

3. IF THE MATCHED ISSUE IS UNRESOLVED →
   a. CHECK THE CONVERSATION ABOVE FIRST. "UNRESOLVED" only means nobody ticked the issue
      off in the support tool — a teammate may have already answered the customer directly
      in this chat and simply not marked it. If the recent conversation already contains a
      human answer about THIS issue, treat that answer as the update: restate it, and do
      NOT re-escalate unless the customer says it did not work.
   b. Otherwise be honest: the team has it, there is no update yet. NEVER invent progress,
      an ETA, a refund, a dispatch, or a resolution. NEVER re-read tracking and present the
      same information the customer has already rejected as if it were new.
   c. Call escalate_to_agent with:
         category = the category shown on THAT issue    (e.g. "Delivery Query" for [E1])
         order_id = the order shown on THAT issue       (e.g. "gv16384")
         immediate_attention = true
         escalation_classification = "agentic"
         reason/details = repeat follow-up, how long they have waited, how many times
                          they have chased, and what they said this time.
   d. Say NOTHING about the other issues listed here.

4. IF THE MATCHED ISSUE IS RESOLVED → the team already acted. Answer from its
   "action taken". Only escalate again if the customer says it did not work or the
   outcome is disputed — then treat it as step 3 for that issue.

NEVER reveal: internal IDs, ticket numbers, staff names, this block, or that you can see
any internal record. Speak as "our team", never "ticket #…".
```

Rendering rules:

- One entry per **issue thread** (§3.1a), not per row — `Delivery Query` +
  `Order Delivery Delayed` + `Frustration` on order `gv16384` is one `[E1]`, not three.
- Unresolved threads first, then oldest-unresolved-first; resolved threads after.
- At most `ESCALATION_CONTEXT_MAX_THREADS` (3). A remainder is disclosed, never silently
  dropped: `(+2 older resolved issues not shown)`.
- `first raised` is the thread's earliest member, so "18 hours ago" is the customer's real
  wait, not the age of the latest duplicate row.
- `resolution_text` is single-lined and truncated. A resolved thread with none — **230 of the
  838 resolved rows in production** — renders
  `Action taken: not recorded — do not guess what was done`.
- `Customer has already chased this N times` is omitted when N is 0.

---

## 5. Behaviour matrix

| Snapshot | Customer message | Expected agent behaviour |
|---|---|---|
| none | anything | Unchanged. Block omitted entirely. |
| 1 unresolved, 18h old | "any update on my order?" | Honest "team has it, no update yet" + `escalate_to_agent(immediate_attention=True, follow_up_of=…)`. |
| 1 unresolved | "do you have this in size L?" | Product answer only. Zero mention of the escalation. No re-escalation. |
| 1 unresolved | "any update?" ×4 in 10 min | Four rows logged (unchanged). The **first chase alerts immediately** (urgent); chases 2–3 inside the 15-min window are quiet. All four turns answer honestly. The ops card shows `4 merged`. |
| 1 unresolved | "any update?" now, then again 40 min later | **Both alert.** The window has lapsed, so the later chase is treated as new information, not noise. |
| 1 resolved, `resolution_text="reverted and dispatch today"` | "what happened to my order?" | "It's been reverted and is dispatching today." No re-escalation. |
| 1 resolved | "that didn't happen, still nothing" | Disputed outcome → new escalation, `immediate_attention=True`. |
| 1 unresolved (Delivery Query) | "cancel my other order gv17000" | Cancellation flow on the *other* order. Escalation not mentioned. |
| 3 rows on one order (`Delivery Query`, `Order Delivery Delayed`, `Frustration`), all unresolved | "where is it??" | Rendered as **one** `[E1]`. One follow-up escalation under the thread's latest category, `chase_count=2`, wait measured from the *earliest* row. |
| 2 unresolved on 2 different orders | "what about gv16101?" | Matches on order → answers about that thread only. Silent on the other. |
| 2 unresolved on 2 different orders | "any update?" (no order named) | Matches the most recent unresolved thread; does not merge or list both. |
| 1 unresolved + 6 resolved in window | anything | Unresolved sorts first and can never be truncated out; block shows 3 threads + `(+4 older resolved issues not shown)`. |

---

## 6. Configuration

| Key | Where | Default | Purpose |
|---|---|---|---|
| `ESCALATION_CONTEXT_ENABLED` | env | `true` | **Hard** kill switch. Off ⇒ no config read, no snapshot read, no Redis call, no block, no tool top-up. Env-backed, so `env_loader`'s cached bootstrap snapshot means flipping it needs a restart. |
| `ESCALATION_CONTEXT_TTL_SECONDS` | env | `60` | Redis TTL for a **positive** snapshot — bounds resolution-visibility lag. |
| `ESCALATION_CONTEXT_MEMORY_TTL_SECONDS` | env | `10` | Memory-tier TTL. Short so a bust on another pod wins within seconds; the memory tier is process-local and cannot be invalidated remotely. |
| `ESCALATION_CONTEXT_EMPTY_TTL_SECONDS` | env | `900` | TTL for an **empty** snapshot. Longer because only an insert (which busts the key) can invalidate it. This is what keeps the ~95% no-escalation case off the DB. |
| `ESCALATION_CONTEXT_LOOKBACK_DAYS` | env | `14` | How far back an escalation stays contextually relevant. |
| `ESCALATION_CONTEXT_ROW_LIMIT` | env | `20` | Rows read before threading. Not the block size. |
| `ESCALATION_CONTEXT_MAX_THREADS` | env | `3` | Issue threads rendered in the block. Remainder disclosed as a count, never dropped silently. |
| `ESCALATION_CONTEXT_THREAD_GAP_HOURS` | env | `72` | Rows **without an order id** start a new issue when this far apart. Stops one `cat:frustration` thread swallowing every complaint the customer ever made. `0` disables splitting. |
| `ESCALATION_CONTEXT_RECENT_RESOLUTION_HOURS` | env | `24` | A resolution recorded this recently outranks stale open issues, so it can never be truncated away. **Coupled to the row above** — see §3.1a-i; do not set to `0` while splitting is on. |
| `ESCALATION_FOLLOWUP_COOLDOWN_MINUTES` | env | `15` | Burst absorber for repeat chases on one issue. New issues and the first chase always alert immediately; only the 3rd+ message inside the window goes quiet. Never gates the row. `0` disables it. |
| `escalation_context_enabled` | `client_configs` (via `aget_config`, tiered-cached) | enabled | Per-tenant **opt-out**, effective without a restart. Checked only when the env switch is on — that ordering is what keeps the env switch a true no-I/O hard off. |

---

## 6a. Blast radius — what this can and cannot change

The feature is inert for any conversation where the customer has no escalation in the
lookback window: the snapshot returns `[]`, the block renders `""`, no extra tool is loaded,
no cooldown key is touched. That is the overwhelming majority of turns. What follows is the
audit of the paths that *do* change when the customer does have one.

| Path | Verdict | Detail |
|---|---|---|
| `system_blocks` composition | **Safe** | Append-only, and only when the block is non-empty. Prompt-cache prefix is already broken upstream by the per-turn `context_message`, so appending after it costs no additional cache miss. |
| Factory tool-contract tests | **Safe** | `test_place_order_tools.py:2070` asserts `len(tools) == 7`, `test_cancel_or_update_tools.py:780` asserts `16`, `test_return_exchange_tools.py:199` asserts `5`. The top-up happens in `generic_skill_node` **after** the factory returns, so every factory-level assertion is untouched. Doing the top-up inside the factories instead would break `place_order`'s count — a good reason not to. |
| Agent-mode switch on WhatsApp | **Safe — already dead** | An agent newly able to call `escalate_to_agent` sets `needs_escalation=True` (`generic_skill_node.py:1639`), which reaches `_should_switch_to_agent_mode_based_on_parent_intent` (`gupshup_webhook.py:1486`). That feeds `_apply_agent_mode_switch_if_parent_intent_requires_escalation`, which is an explicit **no-op** (`gupshup_webhook.py:1495-1507`) — auto-switch is disabled platform-wide. The bot keeps replying. |
| Next-turn routing | **Changed, bounded** | `needs_escalation=True` biases the following turn toward `escalation_handler` when the detected intent is not in `actionable_intents` (`graph_context_meta.py:737`, `:758`). Mitigated by the existing reset in `intent_detection_node.py:829-840`, which clears the flag as soon as a legitimate intent is detected. Net effect: one turn may route to the escalation handler after a re-escalation — the same thing that already happens today for the 6 agents that carry the tool. |
| `escalations` row volume | **Unchanged** | The cooldown gates the alert, not the insert (§3.4). Dashboard counts, resolution rate, and category breakdowns keep their current denominators. |
| Staff alert volume | **Changed, intended** | Repeat chases on one issue inside 3h collapse to one WhatsApp/email. This is the point of the feature. |
| apiandui | **Untouched** | No API, schema, or service change. It keeps writing `status`/`resolved_date`/`resolution_text`; the bot only reads them. |
| Existing escalation raise path | **Unchanged** | `alog_escalation`, `build_escalation_metadata`, routing, and template delivery are not modified. The only edit to `alog_escalation_from_state` is one best-effort cache bust after a successful insert. |
| `already_processed` callers | **Unchanged** | New `ttl_seconds` parameter defaults to `None` ⇒ existing `WEBHOOK_DEDUP_TTL_SECONDS` behaviour. |
| Turn latency | **~0 added wall clock** | The read is prefetched at `client_id` resolution and awaited at prompt assembly, overlapping tool loading / LLM resolution / store context. It is not on the serial path. |
| DB load | **≤1 query/customer/15min** for the ~95% with no escalations; ≤1/60s for the rest | Asymmetric TTL (above). Tenant-scoped, `LIMIT 20`, served by `ix_escalation_customer_phone`. Single-flight per conversation means no stampede. |
| Token cost | **+~350 tokens/turn**, only for customers with an open issue | Bounded by `ESCALATION_CONTEXT_MAX_THREADS=3`. Zero for everyone else. |

**Kill switch.** `ESCALATION_CONTEXT_ENABLED=false` restores byte-for-byte prior behaviour:
no config read, no snapshot read, no block, no tool top-up, no cooldown. It returns before
any I/O — `test_env_off_performs_no_io_at_all` asserts that by making every I/O seam raise.
Because `env_loader` caches its bootstrap snapshot, flipping it takes a restart; the
per-tenant `escalation_context_enabled=false` client_config disables one noisy tenant without
one.

## 7. Failure modes

| Failure | Behaviour |
|---|---|
| DB unavailable | `aget_escalation_snapshot` raises → caught at the injection site → block omitted → turn proceeds exactly as today. |
| Redis unavailable | Tiered cache falls through to DB (fail-open); cooldown guard fails open and allows the escalation. |
| `client_id` unresolved | `[]` returned, `report_error(level="error")` — never an unscoped query (AGENTS.md §7, review checklist #3). |
| Snapshot stale (resolved <60s ago) | Agent treats it as unresolved for at most one turn: it re-escalates a just-resolved issue. Bounded, self-correcting, and strictly safer than the inverse. |
| LLM ignores the block and leaks it | Prompt-level guardrail only, consistent with how every other internal block in `system_blocks` is protected. If leakage is observed in production, add a post-response scrub — do not pre-build one. |

---

## 8. AGENTS.md compliance

| Rule | How it is met |
|---|---|
| §1 Full async | `aget_escalation_snapshot`, `abust_escalation_snapshot`, cooldown guard all `async def`; DB via `get_async_postgres_connection`. |
| §2 Stateless tools | The new helpers are pure in/out; nothing writes conversation state. The block is built and handed to the LLM, not stored. |
| §3 Tiered caching | `aget_with_tiered_cache` (memory → Redis → DB) with explicit top-down bust on write. |
| §5 Trace ID | `trace_id` threaded into `aget_escalation_snapshot` and every log line. |
| §7 Tenant isolation | `client_id` in the SQL predicate *and* the cache key; missing `client_id` is an error, not a default. |
| Review #2 (cached DB reads) | Cached; not a `client_configs` read but held to the same standard. |
| Review #4 (one client per resource) | Uses the shared `get_shared_async_redis_client` via the existing `already_processed`. |
| Minimal footprint | New module + append-only edits to 3 existing files. No refactors, no reordering. |
| Idempotent by default | Re-running a turn re-renders the same block; the cooldown makes repeat escalation attempts converge to one row. |
| Shared utilities | Extends `already_processed`; reuses `get_last_n_digits`, `strip_country_code`, `normalize_escalation_category`, `_create_escalation_tool`. |

---

## 9. Diff footprint

| File | Change | Lines |
|---|---|---|
| `utils/escalation_context.py` | **new** — flags, snapshot + teammate-reply reads, cache, threading, render, lineage, alert guard | +830 |
| `nodes/generic_skill_node.py` | append: build block, append to `system_blocks`, conditional tool top-up | +49 |
| `core/orchestrator.py` | follow-up lineage, alert-only cooldown, contact in metadata; both contact gates collapsed onto the shared helper | +112/−69 |
| `tool_factory.py` | **new** `_create_get_escalations_tool` | +69 |
| `core/tool_registry.py` | register `get_escalations` on the escalation agent | +8/−1 |
| `history/postgres_conversations.py` | migrate `escalations` on guest→phone link | +28 |
| `websocket_chat.py` | bust both caches after a phone link; link escalations on a typed email | +58/−4 |
| `utils/phone_number_utils.py` | **new** `aresolve_contact_collection_gate` — one place the ask decision is made | +84 |
| `utils/utils.py` | **new** `extract_email_candidate`, beside the phone one | +20 |
| `utils/escalation_helper.py` (`alog_escalation_from_state`) | one best-effort cache bust | +7 |
| `workers/idempotency.py` | optional `ttl_seconds` param | +12/−3 |
| `tests/test_escalation_context.py` | **new** — 110 tests | +2085 |

No migration. No apiandui change. No prompt-table change — the block is code-side, so it
lands for every tenant at once and cannot drift per client.

---

## 10. Testing

Unit (`tests/test_escalation_context.py`, mock DB per `tests/README_TESTING.md`):

1. Empty snapshot ⇒ `render_...` returns `""` ⇒ `system_blocks` unchanged.
2. Unresolved record ⇒ block contains `STATUS: UNRESOLVED`, the category, the order id, and the age in hours.
3. Resolved record with `resolution_text` ⇒ renders the action taken; without ⇒ renders the "not recorded" line.
4. Phone variants: rows stored as `9876543210` and `919876543210` both match one lookup.
5. Tenant isolation: an escalation belonging to client B never appears for client A, and the two use distinct cache keys.
6. Lookback: a record older than the window is excluded.
7. **Threading**: three rows sharing an `order_id` under three different categories collapse to one entry; `raised_at` is the earliest, `category` the latest, `chase_count` = 2.
8. **Threading fallback**: rows with no `order_id` group by category slug; two categories ⇒ two threads.
9. **Sort/truncation**: one unresolved record 10 days old + six resolved from yesterday ⇒ the unresolved thread is rendered, and the remainder is disclosed as a count.
10. Cooldown: two follow-ups inside the window ⇒ one insert, one notification; outside ⇒ two. Two follow-ups on the same order under *different* categories ⇒ still one (thread-keyed).
11. Cache bust: raise ⇒ next snapshot read hits the DB, not memory.
12. Degradation: DB raises ⇒ block is `""`, no exception escapes the node.
13. Tool top-up: an agent without `escalate_to_agent` gets it when and only when an unresolved record exists.
14. **Contact gate**: still asks when nothing is on file (and records the flag); does *not* ask when an earlier escalation carries a contact; a contact typed this turn beats the one on file; once the flag is set the snapshot is never consulted; a real phone number is inert with no I/O.
15. **Contact linking**: guest rows move onto the current identity and both cache keys are busted; nothing to move ⇒ no write issued; a second run is a no-op; a non-email or a real-phone target does no I/O; the feature being off does no I/O; a missing `client_id` reports an error rather than querying unscoped; a DB error yields `0`; an address behind more than `ESCALATION_CONTACT_LINK_MAX_IDENTITIES` guest identities is refused so one customer cannot inherit another's escalations.

The two escalation call sites are wired to `aresolve_contact_collection_gate` rather than
inlining the decision, so the gate is covered by unit tests without needing to import
`core/orchestrator.py` — which does not import in the test sandbox (network/DB at import
time), a pre-existing limitation. Their wiring is checked by compile and inspection only; CI
is the gate for it.

Agent-behaviour (`tests/agent_test_runner.py`, escalation handler contracts) — the four rows
of §5 that matter most: follow-up-while-unresolved re-escalates; unrelated-product-question
does **not** mention the escalation; resolved answers from `resolution_text`; disputed
resolution re-escalates. Plus the multi-issue selection cases — since >1 unresolved issue is
the majority state (§3.1a), the "picks the right thread and stays silent on the others"
contract needs a scenario of its own, not just a unit test.

---

## 11. Limitations

- **Guest escalations follow the customer when they link a phone.** A web guest who
  escalates before giving a number has the row keyed on the session id. When the session
  later links to a real phone, `amigrate_webchat_guest_to_phone` now backfills
  `escalations.customer_phone` alongside `conversations` and `messages` — matching both the
  full session id and its VARCHAR(20) prefix, since `alog_escalation` truncates — and
  `_webchat_link_phone_number` busts the snapshot cache for **both** identities (the guest
  key still lists rows that moved away; the phone key may hold a long-lived "no escalations"
  answer that would hide them). Without this the earlier escalation is orphaned and the bot
  greets a customer chasing a hand-off as if nothing had happened. The backfill is
  best-effort: a failure there never fails the identity migration.
- **An email-only web escalation keeps a reachable contact.** Web chat has no reachable
  identifier of its own — the row's `customer_phone` is the truncated session id.
  `escalation_metadata.customer_contact` carries the address the customer gave, surfaced as
  `contact_on_file` on `get_escalations`, so the dashboard and the agent have something to
  reach them by instead of it being buried in `whatsapp_message` free text. Latest-wins within
  a thread, so a corrected address supersedes a typo. It is for the agent's reasoning only and
  is never read back to the customer.
- **The contact is asked for once, not once per conversation.** The "already asked" flag lives
  in the scratchpad, which is conversation state on a 24h sliding TTL — so a customer chasing
  a two-day-old issue used to be asked for the same address they had already given, on a
  ticket already carrying it. `aresolve_contact_collection_gate` now checks
  `aget_contact_on_file` before asking, and both escalation call sites share it rather than
  inlining the decision. The lookup reads the cached snapshot, so on a turn where the prefetch
  already ran it costs nothing, and it returns `None` with no I/O when the feature is off —
  which collapses the gate to exactly its previous behaviour. Anything typed *this* turn still
  wins, so a customer correcting their address is never overridden by history.
- **An email links a returning customer across devices.** A web customer's identity is the id
  in their browser's localStorage, so the same person on a second device is a different
  customer to the lookup. A phone fixes that via the session migration. An email now does too:
  `alink_escalations_by_contact` re-keys the earlier rows onto the identity in front of us,
  triggered by `_webchat_link_detected_email_if_present` *before* the runtime turn so the
  context block is right on the customer's very first message. Deliberately narrow — only rows
  whose `customer_phone` is itself a guest id are moved, so a row already keyed to a real
  phone is never downgraded; only within one `client_id` and the same 14-day window, which
  keeps it on `ix_escalation_client_created` (~0.5 ms measured against production); and only
  onto a guest identity. Idempotent, and fails open to `0`.
  **The trade-off:** an address several customers typed — a store's own support address, most
  likely — is not an identity, and merging on it would show one customer another's
  escalations. Above `ESCALATION_CONTACT_LINK_MAX_IDENTITIES` (3) distinct guest identities
  behind one address the link is refused rather than guessed. Three covers phone + laptop +
  a cleared cache for one person. This bounds the failure but does not eliminate it: two
  customers who both gave the same address as their own would still merge, exactly as two
  customers who both gave the same phone number already do.
- **Web chat across sessions.** Absent a phone or a previously recorded email, a returning
  customer on a new device gets a new identifier and will not match their earlier escalation.
  Same limitation every web-identity-keyed feature has
  (`WEB_WIDGET_SESSION_AND_PHONE_IDENTITY.md`). WhatsApp is unaffected.
- **Resolution visibility lag** is bounded by the cache TTL (60s), not zero.
- **`resolution_text` quality is ops-dependent.** Production values are terse ("reverted",
  "rto", "delivered", "it came back"), and ~230 of the 838 resolved rows have none at all.
  The block never paraphrases beyond what is recorded, and says "not recorded" rather than
  guessing — but the ceiling on step 4's answer quality is what the human typed.
### 3.1b Teammate replies — read from Postgres, not from the transcript

Step 3a tells the agent to defer to a human answer before claiming "no update yet". Pointing
it at the chat history is not enough: history lives in the Redis conversation state, which
has a **24h sliding TTL**, and the customer chasing a days-old issue is precisely the case
where it has expired. At that point the agent sees an empty conversation and an UNRESOLVED
status, and confidently contradicts an answer a teammate gave yesterday.

So the replies are fetched from Postgres, which does not expire, and rendered **into the
block**:

```sql
SELECT message, created_at FROM messages
WHERE client_id = %s::uuid
  AND conversation_id = ANY(%s::uuid[])
  AND created_at > %s                                     -- the issue's raised_at
  AND (created_by = 'support' OR created_by ~ '^[0-9a-f]{8}-')
ORDER BY created_at DESC LIMIT 3
```

- **Scoped by `conversation_id`, not phone.** `messages.phone` has no index;
  `ix_message_conversation_created` does. The escalation row carries the conversation it was
  raised in, so this is an index scan — measured at **~0.1 ms** — rather than a scan over a
  client's whole message history. The conversation the escalation was raised in is also the
  right scope: it is where ops opened the chat to reply.
- **Only runs when something is unresolved.** A resolved-only or empty snapshot cannot be
  changed by a teammate reply, so the ~95% of turns with nothing open never issue this query.
- **Inside the same prefetch task**, so when it does run it is a second round trip within the
  window already overlapped with tool loading and LLM resolution — not a second serial wait.
- **Short TTL (60s)**, same as a positive snapshot: a teammate can reply at any moment, and a
  stale "no replies" would put the agent straight back to claiming there is no update.
- **Degrades independently.** If the messages query fails, the escalation still surfaces; only
  the reply section is missing.

`created_by` is `'support'` for dashboard replies and a user UUID on some surfaces;
`'bot'` / `'user'` / `'system'` are excluded.

### Remaining limitations

- **`status` can disagree with the transcript.** Human-agent mode writes both sides of the
  conversation into the same `state["messages"]` the bot reads: the customer's inbound is
  appended by `gupshup_webhook` (`aget_or_create_state(..., message_content, ...)`), and the
  human's reply is appended as an **AIMessage** by `/dashboard/api/send-reply`
  (`dashboard.py`, "persist agent reply in in-memory state for continuity when bot takes
  over"). Neither touches `escalations.status` — marking resolved is a separate click in
  apiandui that ops routinely skips (73% of all rows sit unresolved). So the block can read
  "UNRESOLVED" while a teammate answered minutes ago. §3.1b closes this: the reply is pulled
  from Postgres into the block, so the agent defers to it even when the chat history is
  long gone. Small today — 17 escalations in 90 days had a human reply
  afterwards, 11 still unresolved — because human-agent mode is barely used (~157 of ~70k
  messages). It scales with adoption of that mode, not with escalation volume.
  Note the human's reply is indistinguishable from a bot reply *in state* (both are
  AIMessages); only Postgres distinguishes them, via `messages.created_by='support'`.
- **Order-less issues thread coarsely.** 18% of multi-escalation windows have no `order_id`
  on any row, so those fall back to grouping by category slug. Two genuinely different
  order-less complaints that happen to land under `General` will merge into one thread. The
  cost is a slightly muddled entry, not a wrong action — the agent still re-escalates under
  a category that fits.
- **Category is trusted as stored.** The bot applies `normalize_escalation_category` on read;
  it does not replicate apiandui's tag-based `_enrich_escalation_categories`. Pre-2026-07-19
  rows may show a legacy category string; they normalise to `General` rather than misroute.

## 12. Possible phase 2 (not required)

- apiandui publishes a resolve event onto the existing `events.escalations` lane so the bot
  can bust the snapshot key on close and drop the TTL lag to ~0. Only worth it if 60s ever
  proves too slow in practice.
- Feed `follow_up_count` / `hours_waiting` into the staff alert template
  (`ESCALATION_GUPSHUP_TEMPLATES.md`) so a chased ticket visibly outranks a fresh one.
