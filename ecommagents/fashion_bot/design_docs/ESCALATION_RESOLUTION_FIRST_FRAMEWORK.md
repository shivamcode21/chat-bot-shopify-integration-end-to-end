# Resolution-First Escalation

## Problem

The escalation node collected complaints and handed them to a human instead of
trying to resolve them. Three production incidents share one shape:

| # | Customer said | Bot did | Should have done |
|---|---|---|---|
| A | "Complaint" (one word) | Escalated immediately | Asked what went wrong and which order |
| B | "It means you are doing fraud" (about the wallet-refund policy) | Escalated as a fraud complaint | Explained why refunds go to the wallet and how to use it |
| C | "I buy pack of 3 16g" (only Pack of 1 / Pack of 2 exist) | Escalated a request no human can fulfil | Offered the two real pack sizes |

## Decision

**The escalation_handler prompt owns the behaviour.** Clarify → de-escalate →
attempt → escalate-last is prompt work: it is judgement, it varies per tenant,
and it is edited without a deploy. The prompt lives per-client in the
`agents_config` DB table.

**Code ships only the three things a prompt cannot do:**

### 1. The resolution toolset (`core/tool_registry.py::_get_escalation_tools`)

A prompt cannot bind tools. Previously the escalation node had exactly four:
`get_order_details`, `get_recent_orders`, `escalate_to_agent`,
`get_contact_information` — no policy answers, no product lookup, no delivery
info. Telling that agent to "resolve first" is unexecutable: it can only look up
an order or escalate. The node now also gets a **read-only** set —
`get_policy_information`, `get_vendor_information`, `get_nearest_store`,
`search_products`, `find_product_by_url`, `find_product_by_id`,
`get_delivery_partner_information` — with `escalate_to_agent` last.

No mutating tool is ever added to this node. **On by default**; opt out per
client with `escalation_policy.resolution_first_tools = false`.

### 2. The actionability gate (`agent_config.py::aevaluate_escalation_gate`)

Incident C is the one case where the hand-off is not just premature but
*impossible*: a human cannot create a variant that does not exist. The agent
declares this itself via `human_can_resolve=False` on `escalate_to_agent`
(LLM-driven — **no keyword matching** of the reason text, which would misfire on
"the order does not exist", a case a human genuinely can fix).

The gate soft-blocks that escalation and returns an instruction to offer the real
options instead. It returns `success=True, escalated=False` — `success=False`
would read as a *technical failure* to the agent, whose prompt guardrail would
then answer "I won't be able to answer your query due to a technical error".

**Mandatory hand-offs are never blocked**, whatever the agent flags:

```
Cancellation Requests · Order Cancellation - Non-Integrated Partner
Callback Request · Courier Update Pending
Offline Store Suggestion · Walk-in Appointment
```

The bot is not permitted to cancel an order (`cancellation_handler`: "You NEVER
cancel orders directly"), and the two system leads are things a human follows up
on — none is bot-resolvable. **On by default**; opt out per client with
`escalation_policy.gate_enabled = false`.

### 3. First-turn frustration routing (`agent_config.py::frustration_should_escalate`)

A prompt cannot change which agent gets the turn. Previously any first
frustration flag short-circuited to the Escalation route, so *"where's my order,
this is terrible"* never reached `order_status` — the customer got a hand-off
instead of the tracking link that would have defused it.

Now a frustrated turn that also carries a **resolvable** intent falls through to
normal routing. Only a standalone frustration — pure anger, or an `escalation` /
`continuity_agent` intent with nothing to resolve — escalates on the first turn.

Resolution-first gets exactly **one** chance: `intent_detection_node` keeps a
`frustration_streak` counter in the scratchpad, and a customer still frustrated
on the next consecutive turn escalates even with a resolvable intent present.

The fall-through deliberately does **not** set `is_frustrated` on state:
`graph_context_meta.py:737/758` route any `is_frustrated` turn to
`escalation_handler` unless the intent is in their narrower `actionable_intents`
set, which would defeat the re-route for e.g. `cancel_or_update_order` or
`after_delivery_return_exchange`.

Note this is independent of the *re-escalation* path: the `escalation_occurred`
branch above it is untouched, so a customer following up on an unhonoured
promise still behaves as before.

## Explicitly not in code

- **Escalation tiers / per-category "how hard to try" taxonomy.** It duplicates
  the judgement the prompt already makes, in a second place that drifts, and it
  changed no routing — only a metric label.

## Observability

`escalations.total` (`monitoring/otel_metrics.py`), emitted once per escalation
**outcome** — a delivered escalation, a courier internal sync, or a soft-block.
The web-chat "please share your phone number" turn is not an escalation and is
not counted. Labels: `category`, `classification`, `actionability`
(actionable/unfulfillable/mandatory), `soft_blocked`, `client_id`.

Watch `actionability=unfulfillable` — those are escalations no human could have
fulfilled. A soft-block also writes a `[Escalation soft-blocked]` conversation
event so a suppressed hand-off is auditable without log retention; it is
deliberately **not** an `escalations` table row (that would surface in the
support dashboard and trigger lead-gen).

## Rollout

1. Apply the resolution-first prompt to a client's `escalation_handler` row in
   `agents_config`. Keep the previous prompt as the rollback.
2. The toolset and gate are already on — nothing else to enable.
3. Measure with `fashion_bot/evals/escalation_resolution_first.py`:
   - `--check` runs the deterministic layer (actionability + gate, no LLM, CI-safe)
   - `--upload` pushes the golden set to LangSmith for the full-agent run, scored
     on `escalates_correctly` / `no_over_escalation` / `no_missed_escalation`.
4. Watch `escalations.total` per client for the drop in delivered escalations
   without a rise in repeat contacts.
