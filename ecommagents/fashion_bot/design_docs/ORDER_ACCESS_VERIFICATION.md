# Order Access Verification (Two-Factor Order Auth)

> Status: **Implemented** — `fashion_bot/utils/order_access.py`, wired into the
> order tools, `generic_skill_node`, and `SupportState.order_auth`. Inert until a
> client sets the `order_access_verification` config key.

---

## 1. Problem

Today an order is released to whoever asserts the phone number on it:

- `get_recent_orders` (`tool_factory.py:2934`) resolves a customer's orders from a
  **phone number alone** and returns order IDs, statuses, line items, prices.
- `get_customers_delivered_orders_by_phone` (`tool_factory.py:3483`) does the same
  for delivered orders.
- Every other order tool runs `_avalidate_phone_for_order_access`
  (`tool_factory.py:2364`), which grants access when the entered phone matches
  **any** phone attached to the order (shipping / billing / order-level /
  `customer.phone` / `customer.default_address.phone`).
- The return/exchange partner tools (`return_partners/tools.py:17-250`) take an
  `order_number` and return return / refund / pickup status with **no phone check
  at all** — `customer_phone` is an optional passthrough to the partner API.

On **WhatsApp** the phone is asserted by the channel itself, so "the phone matches
the order" is a real possession proof. On **web chat** the phone is self-declared:
the customer types it and `_webchat_link_phone_number` (`websocket_chat.py:267`)
links the thread. Anyone who knows a customer's mobile number can list that
customer's orders, harvest an order ID, and from there view or modify the order.

A client has asked for a second factor before order data is released.

## 2. Scope

**In scope — web chat only.** On WhatsApp (and any non-web channel) behaviour is
unchanged: the existing phone match remains the only check.

**Enabled per client.** Clients that do not opt in keep today's behaviour
byte-for-byte, including when the LLM passes null verification parameters.

**Two verification methods**, in strict order of preference:

| Method | Customer supplies | Check | Grant |
|---|---|---|---|
| `order_id` | phone + order ID | the order's phones include the customer's phone (existing check) | that one order |
| `pincode` | phone + 6-digit pincode | fetch the N most recent orders for the phone; compare each order's `shipping_address.zip` | every matching order |

The agent must ask for the **order ID first**; pincode is offered only when the
customer says they don't know it.

Once verified, the grant is stored in conversation state so follow-up turns
("now change the address on that order") don't re-challenge the customer.

## 3. Threat model and the decisions that follow

| Risk | Decision |
|---|---|
| The LLM supplies the verification value, and the pincode is already in its context — `get_order_details` returns `shipping_address` verbatim (`tool_factory.py:2711`) and tool observations are replayed into the message list (`generic_skill_node.py:429`). A confused or injected model could "verify" the customer against data the customer never gave. | The gate accepts a pincode **only if it also appears in the customer's own recent messages**. The LLM parameter is a hint; the user turn is the authority. Mirrors the existing `_extract_phone_from_message` precedent (`generic_skill_node.py:572`). |
| A pincode is ~1 guess for anyone who knows the customer's city, and an attacker targeting a specific person usually knows their address. | Failed attempts are capped (default 3) per conversation, then the flow hard-blocks and escalates. |
| A pincode grant covers *several* orders and can authorize mutations (address change on an in-transit COD order is the highest-value fraud). | `pincode_grants` config knob: `read_write` (default, matches the client's ask) or `read_only` (pincode gets status/tracking; mutations need an order ID). One config edit to tighten. |
| A grant leaking across customers/conversations. | The grant is bound to `client_id` + normalized phone + `conversation_id`, carries `verified_at`, and expires (`grant_ttl_minutes`, default 60). Any mismatch = no grant. |
| Gating `get_recent_orders` but leaving the other phone-only / unchecked paths open. | `get_customers_delivered_orders_by_phone` **and** the return-partner order tools are gated in the same change. Otherwise the control is trivially bypassable. |
| System-initiated order reads (webhooks, NDR escalation, template sends) breaking. | The gate lives at the **chat-tool** layer only. Orchestrators, adapters, webhook processors and `escalate_ndr_order` are untouched. |

## 4. Configuration

One `client_configs` row, read through the tiered cache (`aget_config`,
`config_manager.py:97`) — AGENTS.md §3.

`config_key = "order_access_verification"`, `config_value` = JSON:

```json
{
  "enabled": true,
  "channels": ["web-chat"],
  "methods": ["order_id", "pincode"],
  "recent_window": 3,
  "max_attempts": 3,
  "grant_ttl_minutes": 60,
  "pincode_grants": "read_write"
}
```

Defaults when the key is absent or unparseable: `{"enabled": false}` — i.e. every
existing client keeps today's behaviour. A parse failure logs a warning and
fails **open to legacy behaviour** (never a hard error mid-conversation), which is
safe because the key's absence is the norm. The read is bounded (3s) so a Redis /
Postgres stall degrades to legacy behaviour instead of hanging the turn.

**The channel is resolved before any I/O.** A WhatsApp (or unknown-channel) turn
returns "disabled" without reading config at all, so the majority channel pays
nothing for this feature — no extra latency, no extra cache traffic. The
consequence to know: the `channels` list can switch verification **off** for web
chat, but cannot switch it **on** for WhatsApp. That is deliberate — the whole
premise is that a web-chat phone number is self-declared while WhatsApp asserts
its own.

`recent_window` defaults to the existing `order_display_limit` config
(`tool_factory.py:3009`) when unset, so the pincode window and the display window
can't drift.

## 5. State: the grant

New `SupportState` field (`schema.py`). The dead `waiting_for_phone_validation` /
`phone_validation_order_id` pair next to it was left in place — removing it is
unrelated cleanup and AGENTS.md asks for minimal diffs:

```python
# --- Order access verification (web chat, per-client) ---
# Grant issued by utils/order_access.py after the customer proves order
# ownership with an order ID or their shipping pincode. Scoped to one
# client + phone + conversation and expires; see design_docs/
# ORDER_ACCESS_VERIFICATION.md.
order_auth: Optional[Dict[str, Any]]
```

Shape:

```python
{
  "client_id": "c3ffcb1b-...",
  "conversation_id": "conv_...",
  "phone": "9876543210",            # normalized 10-digit the grant was issued to
  "method": "order_id" | "pincode",
  "order_ids": ["63084", "63102"],  # normalized (lowercased, '#'/prefix stripped)
  "scope": "read_write" | "read_only",
  "verified_at": "2026-08-11T10:12:33Z",
  "failed_attempts": 0,
}
```

**Why the failed-attempt counter lives in the grant, not a new Redis key:** only
one conversation turn per user executes at a time (single-flight lock, AGENTS.md
"Single-Flight Semantics"), so a state-resident counter cannot race, and state is
already Redis-backed per conversation. It travels the same tool → middleware →
state path as the grant itself (`bump_failure` is pure too). No new key, no new
TTL to manage.

**Propagation — and why tools never write it.** AGENTS.md §2 keeps tools
stateless, so the gate is pure: it *returns* the grant, it does not store it.

1. *The tool returns it.* A gated tool puts the grant in its own result under
   `order_access.GRANT_RESULT_KEY` (`_order_auth`). Nothing in `order_access.py`
   or in any tool writes conversation state.
2. *The runtime applies it.* `OrderAuthMiddleware`
   (`utils/agent_middleware.py`, registered in `build_agent_graph`) reads that
   key in `awrap_tool_call`, calls `apply_grant_update(state, …)`, and strips the
   marker from the `ToolMessage` the model reads. This is the single place a
   verification result becomes state.
3. *The node persists it.* `order_auth` is in
   `PASSTHROUGH_STATE_FIELDS_PRE_ESCALATION`, so `generic_skill_node` returns it
   and LangGraph + the state cache carry it to the next turn.

**Why the middleware runs per tool call, not once at the end of the turn.** One
turn routinely chains `get_recent_orders` → `get_order_details` →
`update_order_address`, and a LangGraph state update only lands when the node
*returns*. Applying the grant inside `awrap_tool_call` puts it on the same
`state` object every tool in the turn closes over, so the next tool sees it
immediately. An end-of-turn harvest would leave that gap open.

Both gates emit grants. The listing gate emits one when a pincode or order ID
matches; the order-scoped gate emits one when the phone-vs-order check passes, so
that a follow-up message about the same order is not re-challenged (instruction
block rule 6). For the order-scoped tools the grant is an optimisation rather than
the authority — they can always re-derive their answer from the order ID and
phone — which is why a missing grant only ever costs a repeat check, never access.

**Scopes never merge.** A grant accumulates order IDs only while the scope stays
the same. Folding a `read_write` order-ID proof into a `read_only` pincode grant
would silently make the pincode-granted orders mutable, so a scope change starts a
fresh order list instead. `failed_attempts` is carried across the change
regardless — it is a rate limit, and switching method must not reset it.

**Invalidation.** The grant is ignored (and cleared) when any of `client_id`,
`conversation_id`, or normalized `phone` differs from the current turn, or when
`verified_at` is older than `grant_ttl_minutes`. A customer switching phone
numbers mid-conversation ("check my wife's order, her number is …") therefore
re-verifies.

## 6. New module: `utils/order_access.py`

All logic lands here, not in `tool_factory.py` (6811 lines — AGENTS.md
*Minimal-Footprint Changes*). `_avalidate_phone_for_order_access` becomes a thin
delegate so its ten existing call sites keep working unchanged.

```python
# --- policy ---
async def aget_verification_policy(state) -> dict     # channel check first, then bounded config read

# --- pure helpers ---
def normalize_pincode(value) -> str                   # "" unless exactly 6 digits
def normalize_order_key(value) -> str                 # "#GV63084" -> "gv63084"
def order_keys_match(left, right) -> bool             # key match, then digit-run match
def order_pincode(order) -> str                       # shipping_address.zip, normalized
def value_from_customer(value, state, lookback=6) -> bool

# --- grant ---
def grant_is_valid(grant, state, ttl_minutes, phone="") -> bool
def grant_covers(grant, order_id, mutating=False) -> bool
def record_grant(state, *, method, order_ids, phone, scope) -> dict   # accumulates
def record_failure(state, phone="") -> int
def filter_orders_to_grant(orders, allowed_keys) -> list

# --- the two gates ---
async def averify_listing_access(state, *, phone, verification_identifier_type,
                                 verification_identifier_value, policy=None) -> dict
    """For the phone-only listing tools. Returns
    {"allowed": bool, "response": dict|None, "filter_order_ids": set|None}."""

async def averify_order_scoped_access(state, *, order_id, phone, customer_email="",
                                      ..., mutating=False) -> dict
    """For tools that already name one order. Borrows the phone-vs-order match
    from tool_factory._avalidate_phone_for_order_access, checks a supplied pincode
    against that order via averify_listing_access, counts real mismatches toward
    max_attempts, and accepts an order-matching email as the identity half."""

# --- wrapper + prompt ---
def order_access_guarded(state, *, order_arg="order_number", mutating=False)
def agent_has_gated_order_tools(tools) -> bool    # schema-derived, not a name list
def build_verification_instruction_block(policy) -> str
```

### 6.0 Where each gate is called

The rules live in one module; only the call sites differ, and there are three:

| Surface | Integration |
|---|---|
| `get_recent_orders`, `get_customers_delivered_orders_by_phone` | explicit `averify_listing_access` call + `filter_orders_to_grant` on the result |
| The 9 `tool_factory` order tools | none of their own — the gate hooks into `_avalidate_phone_for_order_access`, which they already called |
| The 8 return-partner tools | one `@order_access_guarded(state)` decorator each |

**Why the order-scoped tools needed almost nothing.** Those tools already receive
an `order_id` and already prove the phone matches that order — that *is* the
order-ID path. The only genuinely new enforcement is on the listing tools, which
had no second factor at all. So the allow/deny outcome of `get_order_details`,
`update_order_*` and `cancel_order_tool` is unchanged; what they gained is
honouring a `read_only` grant on mutation. This is what keeps the blast radius
small.

**What the order-scoped gate adds on top of that phone match** (all of it inert
while the policy is off):

- **A pincode is checked, not just carried.** `verification_identifier_type=
  "pincode"` on an order-scoped tool runs the same match the listing gate runs and
  then requires the resulting grant to cover the order the tool named. Without
  this the pincode was never read on this path and a wrong one still passed on the
  phone match alone. `read_only` is enforced here too, so a pincode cannot drive a
  mutating return/exchange tool when the client has tightened the scope.
- **A mismatch costs an attempt.** `_avalidate_phone_for_order_access` returns
  `failed_verification: True` for the two real mismatches (order not found, phone
  not on the order) and the gate feeds them to `bump_failure`, so `max_attempts`
  and the lockout apply here as well. Order numbers are largely sequential, so
  without a cap they could be guessed against a known phone indefinitely.
  `needs_phone` is *not* a mismatch — it is a prompt for input and must not burn
  the customer's budget.
- **Email is accepted as the identity half.** `return_partners/identity.py` already
  treats an email that matches the order as proof, and web-chat customers who lead
  with their email would otherwise be refused by every gated tool the day a client
  opts in. Email + named order is the same two-factor shape as phone + named order.

### 6.1 `averify_listing_access` decision order

```
1. policy = await aget_verification_policy(state)
   policy.enabled is False  ->  ALLOW, filter_order_ids = None (no filtering).
   (non-web channel, client not opted in, or config absent — all land here)

2. grant = state.get("order_auth")
   grant_is_valid(...) and grant_covers(order_id, mutating=...)  ->  ALLOW.
   (a pincode grant with scope=read_only and mutating=True does NOT cover)

3. failed_attempts >= policy.max_attempts  ->  BLOCK ("verification_locked"),
   flag for escalation, do not fetch anything.

4. No verification_identifier_type/value supplied
      -> BLOCK ("verification_required") with an actionable message.

5. type == "order_id":
      Match the value against the customer's recent orders (the same fetch as
      step 6). Match -> grant {method:"order_id", order_ids:[that order]}.
      No match -> failed_attempts += 1, BLOCK.
      (On an order-SCOPED tool the equivalent check is the phone-vs-order match
      the tool already runs, plus a fail-closed conflict check when the caller
      names one order and verifies another.)

6. type == "pincode":
      normalize_pincode; must be 6 digits.
      pincode_in_user_messages(...) must be True, else BLOCK
        ("verification_value_not_from_customer") and log at WARNING with trace_id.
      Fetch the customer's recent orders by phone
        (UtilityOrchestrator.aget_recent_orders_all_statuses, limit=recent_window).
      Compare normalize_pincode(order.shipping_address.zip) for each.
      >=1 match -> grant {method:"pincode", order_ids:[matching orders],
                          scope: policy.pincode_grants}
      Grants ACCUMULATE within a conversation: verifying a second order does not
      revoke access to the first.
      0 matches -> failed_attempts += 1, BLOCK.
      Orders with no shipping address are skipped (fail closed).

7. Unknown type -> BLOCK ("verification_required").
```

Every ALLOW path writes the grant to `state["order_auth"]` (see §5) and returns
`allowed=True`. Every BLOCK path returns a ready-made tool payload (§7).

**Cost.** The pincode match itself is trivial — a normalized string compare over
at most `recent_window` (3) orders. The cost that matters is the phone→orders
lookup behind it, which is ~2 sequential Shopify round-trips
(`customers/search.json`, then `orders.json` per matched customer record) drawing
on the shared per-shop rate-limit budget.

That lookup happens **once** per verification turn: the gate fetches (at the same
width the listing paths use) and hands the list back as `prefetched_orders`, and
the tool passes it straight through as `cached_orders` to
`aget_recent_orders_all_statuses` / `aget_recent_actionable_orders` /
`aget_customers_delivered_orders` rather than fetching again. Later turns skip
verification entirely — the grant short-circuits before any fetch.

`get_order_details` and the mutating tools add no Shopify call at all: they reuse
the caller-supplied `cached_base_result` / `prefetched_raw_record`
(`tool_factory.py:2365-2366`), and the update-rule check reuses
`phone_check["order_data"]`.

### 6.2 Refusal payloads (tool return contracts)

Shape mirrors the existing `access_denied` / `invalid_order_id` responses
(`tool_factory.py:2656`, `:2689`) so current prompts already know how to read them.

```python
# no identifier supplied yet
{"success": False, "error": "verification_required", "phone_validated": False,
 "message": "Before sharing order details, ask the customer for their Order ID. "
            "If they don't know it, ask for the 6-digit pincode of the delivery "
            "address on their order. Then call this tool again with "
            "verification_identifier_type and verification_identifier_value."}

# supplied but wrong
{"success": False, "error": "verification_failed", "attempts_remaining": 2, ...}

# out of attempts
{"success": False, "error": "verification_locked", "needs_escalation": True, ...}

# pincode not traceable to the customer's own messages
{"success": False, "error": "verification_required", ...}   # same text as above;
# the distinction is logged, never surfaced (don't teach an attacker the rule)
```

`attempts_remaining` is for the agent's phrasing, not a security control.

## 7. Tool surface

Two new optional parameters on each gated tool, exactly as specified:

```python
verification_identifier_type: str = "",    # "order_id" | "pincode" | ""
verification_identifier_value: str = "",
```

Both default to `""` so old prompts and old clients are unaffected.

### 7.1 Tools to gate

| Tool | File | `mutating` |
|---|---|---|
| `get_order_details` | `tool_factory.py:2574` | no |
| `get_recent_orders` | `tool_factory.py:2934` | no |
| `get_customers_delivered_orders_by_phone` | `tool_factory.py:3483` | no |
| `annotate_order` | `tool_factory.py:3196` | yes |
| `update_order_name_tool` | `tool_factory.py:5448` | yes |
| `update_order_address` | `tool_factory.py:5505` | yes |
| `update_order_size_tool` | `tool_factory.py:5675` | yes |
| `update_order_phone_number_tool` | `tool_factory.py:5794` | yes |
| `update_order_email_tool` | `tool_factory.py:5859` | yes |
| `change_order_product_tool` | `tool_factory.py:6063` | yes |
| `cancel_order_tool` | `tool_factory.py:6703` | yes |
| `get_return_status_by_order_number` | `return_partners/tools.py:18` | no |
| `list_return_requests_by_order_number` | `return_partners/tools.py:42` | no |
| `get_return_pickup_status` | `return_partners/tools.py:112` | no |
| `get_refund_status_by_order_number` | `return_partners/tools.py:132` | no |
| `get_return_or_exchange_portal_link` | `return_partners/tools.py:80` | yes |
| `ensure_exchange_order_created` | `return_partners/tools.py:155` | yes |
| `request_exchange_size_change` | `return_partners/tools.py:182` | yes |
| `get_exchange_delivery_status` | `return_partners/tools.py:212` | no |

Explicitly **not** gated: `escalate_ndr_order` (`tool_factory.py:3303`),
`get_return_request_by_id` (needs a request id the customer already holds — but
see Open Questions), `create_order` / cart / draft-order tools (they create, they
don't read someone else's data), and everything under `shopify/webhook/`,
`delhivery/`, `shiprocket/`, `cron_jobs/`.

`get_return_request_by_id` still runs Gate B (`_verify_identity` on the request's
order) and now **fails closed** when the partner returns a request carrying no
order reference: that request cannot be identity-checked, and returning it anyway
handed the customer name, email, phone, line items and refund to whoever quoted
the request number. Reachable in practice through the cached webhook row, whose
`order_number` column is nullable. A definitive "no such request" is untouched —
that is a `request: None` answer, not an unverifiable one.

**How an agent gets the instruction block.** Keyed off the tool *schemas* —
`order_access.agent_has_gated_order_tools(tools)` looks for the
`verification_identifier_type` parameter — rather than a hardcoded list of tool
names. A name list silently omits tools that are in fact gated, which leaves the
agent asking for nothing while the tool keeps refusing.

### 7.2 Two shapes, two behaviours

- **Order-scoped tools** (everything with an `order_id` / `order_number` arg): the
  decorator reads that argument and asks `averify_order_access(order_id=...)`.
- **Phone-scoped listing tools** (`get_recent_orders`,
  `get_customers_delivered_orders_by_phone`): no order id exists yet. They call the
  gate with `order_id=""`; on the pincode path the gate's grant defines which
  orders may be returned, and the tool **filters its result to the granted order
  IDs**. On the `order_id` path, the listing is filtered to that single order. This
  is the change that actually closes the hole — a phone alone must never produce a
  list.

### 7.3 Parameter conflict rule

When `verification_identifier_type == "order_id"` and the tool also has an
`order_id` argument, the two must normalize to the same order. A mismatch is
**fail closed** (`verification_failed`), never "trust one of them".

> Simplification worth considering later: for order-scoped tools the `order_id`
> argument *is* the proof, so `verification_identifier_value` is redundant there
> and the only genuinely new input is the pincode. The uniform two-parameter shape
> is kept here because it is what the prompts will be written against and it keeps
> one contract across all tools.

## 8. `generic_skill_node` changes

Three small edits, all in `nodes/generic_skill_node.py`:

1. **Instruction block** — `build_verification_instruction_block(policy)` is
   appended to `system_blocks` alongside `_build_current_datetime_block()` and the
   cart guardrail, gated on the agent actually having order tools
   (`agent_has_gated_order_tools`) and on the policy being enabled. Wrapped in
   try/except so a config hiccup can never break a turn.
2. **Grant harvest** — `"order_auth"` added to `PASSTHROUGH_STATE_FIELDS_PRE_ESCALATION`.
3. **Escalation on lockout** — `_check_escalation_in_tool_results` treats an
   observation with `error == "verification_locked"` as escalation-worthy.

No pincode pre-extraction step was needed: `value_from_customer` scans the last
few human messages in `state["messages"]` directly, which is the same source a
pre-extraction step would have read.

**Why a code-injected block instead of editing prompts:** agent prompts are
per-client rows in `agents_config` (`prompt_generator.py:76`, Redis-cached).
Editing N clients × M agents is heavy, drifts the moment a prompt is regenerated,
and a client with the flag ON plus a stale prompt gets an agent that never passes
the parameters and a tool that refuses everything — an infinite ask-loop. A code
block cannot drift from the flag. The DB prompts stay untouched.

## 9. Flows

**A. Web, flag on, customer knows the order ID**

```
user: where is my order
bot : sure — could you share your Order ID?           (no tool call yet)
user: gv63084, my number is 9876543210
LLM  -> get_order_details(order_id="gv63084", phone_number="9876543210",
                          verification_identifier_type="order_id",
                          verification_identifier_value="gv63084")
gate : policy on -> no grant -> order_id path -> phone matches order -> ALLOW
       state["order_auth"] = {method:"order_id", order_ids:["63084"], ...}
bot  : <status>
--- next turn ---
user: change the address to <...>
LLM  -> update_order_address(order_id="gv63084", ..., verification_* = "")
gate : grant valid, covers 63084, mutating allowed -> ALLOW (no re-challenge)
```

**B. Web, flag on, customer doesn't know the order ID**

```
user: where is my order
bot : could you share your Order ID?
user: i don't have it
bot : no problem — what's the 6-digit pincode of the delivery address?
user: 560103
LLM  -> get_recent_orders(phone_number="9876543210",
                          verification_identifier_type="pincode",
                          verification_identifier_value="560103")
gate : pincode seen in user message -> fetch 3 recent orders ->
       2 of 3 have zip 560103 -> ALLOW, grant order_ids = those 2
tool : returns ONLY those 2 orders
```

**C. Web, flag on, pincode wrong** → `verification_failed`, `failed_attempts` 1→2→3,
then `verification_locked` + escalation. No order data at any point.

**D. WhatsApp, or flag off** → step 1 of §6.1 short-circuits to the legacy phone
check. Identical to today, including when the model passes null parameters.

## 10. Edge-case matrix

| Case | Behaviour |
|---|---|
| Order has no `shipping_address` (digital / pickup) | Skipped in the pincode comparison — cannot be granted via pincode |
| `zip` formatted `"560 103"` / `"560103-"` | `normalize_pincode` strips non-digits before comparing |
| Matching order sits outside `recent_window` | Denied; refusal message offers the Order-ID route |
| Customer's only orders are delivered | Pincode path uses `aget_recent_orders_all_statuses`, so they still verify |
| Customer places a new order mid-conversation | Not covered by an existing grant; re-verify (grant lists explicit order IDs) |
| Model passes a 6-digit pincode as `type="order_id"` | Normalization + phone-match fails → `verification_failed`; the block text re-teaches the model |
| Model passes the pincode it read from a prior tool result | `pincode_in_user_messages` fails → refuse, log at WARNING with trace_id |
| Config JSON malformed | Warn, treat as disabled (legacy behaviour) |
| Redis down (grant unreadable) | State cache degrades per existing fail-open path; customer re-verifies once — annoying, not unsafe |

## 11. Observability

- One `INFO` per verification decision with `trace_id`, `client_id`, `channel`,
  `method`, `outcome`, `orders_granted` — never the pincode itself (log
  `pincode_len` / a masked `****03` at most), per AGENTS.md §5.
- `WARNING` + `report_error(level="warning")` when a verification value cannot be
  traced to the customer's messages — that signal is either a prompt bug or an
  injection attempt and should be visible.
- `verification_locked` raises the existing escalation path so a genuinely locked-out
  customer reaches a human.
- Optional counter in `monitoring/otel_metrics.py`:
  `order_verification_total{client_id,method,outcome}`.

## 12. Tests

New `tests/test_order_access_verification.py`, following the offline style of
`tests/test_order_access_phone_validation.py` (cached DTO + raw record, no Shopify).

Unit:
- `normalize_pincode`, `normalize_order_key`, `pincode_in_user_messages`,
  `grant_is_valid`, `grant_covers` (incl. `read_only` × `mutating`).

Gate matrix:
- flag off → identical result to `_avalidate_phone_for_order_access` today
  (parametrized against the existing test's cases — this is the regression guard
  for old clients);
- WhatsApp state + flag on → legacy path;
- web + order_id match / mismatch / conflicting with the `order_id` argument;
- web + pincode match on 2-of-3 / no match / order without shipping address /
  pincode absent from user messages;
- attempts 1→2→3 → `verification_locked`;
- grant reuse on a second call; grant rejected after phone change, after
  `conversation_id` change, after TTL expiry;
- `get_recent_orders` returns only granted orders on the pincode path.

Order-scoped gate (the path the return/exchange tools take):
- flag off → allowed, no grant (the regression guard for existing clients);
- no order id → refused;
- phone match → allowed **and** a grant recorded, so the follow-up is not re-asked;
- wrong order / wrong phone → refused **and** counted; three of them → locked;
- `needs_phone` → refused but **not** counted;
- pincode that matches the named order → allowed; pincode that matches a
  different order → refused, not counted; pincode the customer never typed →
  refused; `pincode_grants=read_only` → reads allowed, mutations refused;
- an order-matching email with no phone → allowed; a non-matching one → refused.

Real return/exchange tools: untouched with the flag off, partner never called for
an unverified order, grant handed back under `GRANT_RESULT_KEY`, and every gated
tool keeps its `order_number` parameter and its docstring.

## 13. Rollout

1. Ship with **no** `order_access_verification` rows → zero behaviour change
   anywhere. Verify on staging with the flag on for one test client.
2. Enable for the requesting client (`{"enabled": true, "channels": ["web-chat"]}`).
3. Watch `order_verification_total` and the escalation queue for a week: a spike in
   `verification_failed` means the ask-copy or the recent-window is wrong, not that
   customers are attacking.
4. If fraud pressure justifies it, flip `pincode_grants` to `read_only` — one config
   edit, no deploy.

## 14. What shipped

| Change | Files |
|---|---|
| Policy, grant builders (pure), both gates, decorator, prompt block | `utils/order_access.py` (new) |
| `OrderAuthMiddleware` — the runtime hop that applies a returned grant to state | `utils/agent_middleware.py`, registered in `utils/agent_utils.py` |
| Verification hooked into the shared phone check; params on 11 tools; listing gate + result filter on the 2 listing tools | `tool_factory.py` (+216 / -20) |
| `@order_access_guarded` on 8 partner tools + params | `return_partners/tools.py` (+26) |
| `order_auth` state field | `schema.py` (+8) |
| Instruction block, grant passthrough, lockout escalation | `nodes/generic_skill_node.py` (+46) |
| 45 offline tests (module, real-tool functional, middleware hand-off) | `tests/test_order_access_verification.py` (new) |

Follow-up hardening of the order-scoped path (the one the return/exchange tools
take), after review found it was enforcing only the phone-vs-order match:

| Change | Files |
|---|---|
| Pincode actually checked against the named order; mismatches counted toward `max_attempts`; grant recorded on success; order-matching email accepted as the identity half; `read_only` honoured on the pincode path | `utils/order_access.py` |
| `failed_verification` marker so the gate can tell a real mismatch from "no phone yet" | `tool_factory.py` (+6) |
| Scopes no longer merge in a grant; `failed_attempts` carried across a method switch | `utils/order_access.py` |
| Instruction block keyed off tool schemas instead of a hardcoded name list | `nodes/generic_skill_node.py`, `utils/order_access.py` |
| `aget_return_request_by_id` fails closed on a request with no order reference | `return_partners/orchestrator.py` (+19) |
| `value_from_customer` no longer accepts a digit window inside the customer's own phone number | `utils/order_access.py` |
| 20 further offline tests covering the order-scoped gate and the real return tools | `tests/test_order_access_verification.py` |

Verified: all 45 new tests plus the 5 pre-existing `test_order_access_phone_validation`
tests pass; every affected agent's tool list still builds with intact schemas and
docstrings; and with the policy off, `get_recent_orders` returns all 3 orders and
writes no grant, on both web and WhatsApp.

## 15. Open questions for the client

1. **Pincode grant scope** — default here is `read_write` (view *and* edit), per
   the stated requirement. Recommendation is `read_only`, with order-ID proof
   required for address changes and cancellations, since that's where fraud pays.
   Confirm which they want at launch.
2. **Recent-order window** — 3 (matching `order_display_limit`). A customer whose
   matching order is 4th-most-recent is denied on the pincode path and pushed to
   the Order-ID route. Acceptable?
3. **Return-request tools** — `get_return_request_by_id` takes a request id the
   customer normally has from an email/SMS. Gate it too, or treat the request id as
   its own proof? (It fails closed on an unverifiable request either way — §7.1 —
   so this is now a question about tightening, not about a hole.)
4. **Lockout duration** — currently for the remainder of the conversation
   (`conversation_id` resets after the 90-minute inactivity gap). Longer?
