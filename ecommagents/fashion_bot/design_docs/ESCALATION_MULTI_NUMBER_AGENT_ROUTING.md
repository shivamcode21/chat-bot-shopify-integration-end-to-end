# Multi-Number, Agent-Routed Escalation Notifications

> Design doc for adding (a) **multiple escalation mobile numbers per client** and
> (b) **agent-based routing** of escalation notifications, so that an escalation
> raised by `return_exchange`, `order_status`, `product_details`, etc. is
> delivered to the number(s) responsible for that area instead of a single
> shared agent phone.

**Status:** Proposed
**Owner:** Escalation / Conversation Runtime
**Related code:** `core/orchestrator.py` (`EscalationOrchestrator`), `agent_config.py`,
`utils/escalation_helper.py`, `gupshup_webhook.send_message`, `core/tool_registry.py`,
`tool_factory.py` (`_create_escalation_tool`).

---

## 1. Problem

Today every escalation notification goes to exactly **one** WhatsApp number per
client:

- Config lives in Postgres `client_configs`, `config_key = 'escalation_contact'`,
  value `{"AGENT_PHONE_NUMBER": "<one number>"}`.
- `agent_config.aget_agent_phone_number(client_id)` returns that single string
  (cached in a process-local dict — it does **not** use the tiered cache that
  `AGENTS.md` §3 / review-checklist #2 require for `client_configs` reads).
- `EscalationOrchestrator` (in `core/orchestrator.py`) sends notifications from
  **~15 near-identical call sites**, each:
  ```python
  await send_message(await aget_agent_phone_number(client_id=client_id),
                     notification, client_id=client_id)
  ```
- `gupshup_webhook.send_message(to, message, ...)` sends to a **single** `to`.

Each escalation already carries a `category` (e.g. `"Cancellation Requests"`,
`"Delivery Query"`, `"Restocking Query"`, `"Product Complaint"`,
`"Delivery Partner Sync"`, `"General"`), and `state` carries `parent_intent`,
but **none of this is used for routing** — every category lands on the same
number.

### Goals

1. **Multiple numbers** per client for escalations.
2. **Agent-based routing** — route to the number(s) configured for the agent
   that raised the escalation (`return_exchange`, `order_status`,
   `product_details`, …). A single number may serve multiple agents.
3. Fully **backward compatible** with existing single-number configs.

### Non-goals

- No change to the `Delivery Partner Sync` path — it has its own notifier
  (`utils/delivery_utils.anotify_agent_for_non_integrated_partners`) and is
  explicitly out of scope.
- No on-call rotation / round-robin (rejected — would require persisted state,
  violating the stateless-tool contract). All matched numbers are notified.

---

## 2. Routing keys = exact agent names

Routing buckets are the **exact agent names** from `core/tool_registry.py`
(`TOOL_REGISTRY`) — the same identifiers `generic_skill_node` runs under — not
invented coarse buckets. This keeps config self-explanatory and avoids a
translation layer drifting out of sync with the agent set.

Canonical agent names (routing keys):

```
product_details        order_status            place_order
cancel_or_update_order cart_management         return_exchange
delivery_timeline      discount                recommendations
delivery_policy        payment_policy          return_exchange_policy
vendor_inquiry         feedback                escalation
unknown
```

A client lists numbers under any subset of these; a number may appear under
several agents (e.g. one "pre-sales" number under `product_details`,
`recommendations`, and `place_order`).

---

## 3. Config schema (backward compatible)

Extends the existing `escalation_contact` config key. **Old configs that contain
only `AGENT_PHONE_NUMBER` keep working unchanged** — that value becomes the
default fallback.

Each `default` / `routes[agent]` node is a `contacts` object carrying one list
per channel (`phone`, `email`). The `phone` numbers receive **both** WhatsApp
and SMS notifications — there is no separate `sms` key. Escalations are
delivered over **all three** channels (WhatsApp, SMS, email) concurrently:

```json
{
  "AGENT_PHONE_NUMBER": "9111111111",
  "ESCALATION_ROUTING": {
    "default": {
      "contacts": {
        "phone": ["9111111111", "9999999999"],
        "email": {
          "to": ["support@example.com"],
          "cc": ["escalations@example.com"]
        }
      }
    },
    "routes": {
      "return_exchange": {
        "contacts": {
          "phone": ["9111111112", "9999999998"],
          "email": {
            "to": ["returns@example.com"],
            "cc": ["returns-escalation@example.com"]
          }
        }
      },
      "order_status": {
        "contacts": {
          "phone": ["9111111113"],
          "email": {
            "to": ["orders@example.com"]
          }
        }
      },
      "product_details": {
        "contacts": {
          "phone": ["9111111115"],
          "email": {
            "to": ["catalog@example.com"]
          }
        }
      },
      "discount": {
        "contacts": {
          "phone": ["9111111116"],
          "email": {
            "to": ["wholesale@example.com"]
          }
        }
      },

      "Misrouted Order": {
        "contacts": {
          "phone": ["9111111114"],
          "email": {
            "to": ["logistics@example.com"],
            "cc": ["ops-lead@example.com"]
          }
        }
      },
      "Undelivered Order": {
        "contacts": {
          "phone": ["9111111114"],
          "email": {
            "to": ["logistics@example.com"],
            "cc": ["ops-lead@example.com"]
          }
        }
      },
      "Cancellation Requests": {
        "contacts": {
          "phone": ["9111111117"],
          "email": {
            "to": ["retention@example.com"]
          }
        }
      },
      "Restocking Query": {
        "contacts": {
          "phone": ["9111111118"],
          "email": {
            "to": ["inventory@example.com"]
          }
        }
      },
      "Bulk Order Discount": {
        "contacts": {
          "phone": ["9111111116"],
          "email": {
            "to": ["wholesale@example.com"],
            "cc": ["bulk-orders@example.com"]
          }
        }
      },
      "Offline Store Suggestion": {
        "contacts": {
          "phone": ["9222222222"],
          "email": {
            "to": ["retail-ops@example.com"]
          }
        }
      }
    }
  }
}
```

In the example above:
- **Agent-level keys** (`return_exchange`, `order_status`, `product_details`,
  `discount`) act as coarse defaults — any escalation from that agent routes
  there unless a more specific category key overrides it.
- **Category-level keys** (`Misrouted Order`, `Undelivered Order`,
  `Cancellation Requests`, `Restocking Query`, `Bulk Order Discount`,
  `Offline Store Suggestion`) are the most specific — these match the exact
  LLM-generated category from `ESCALATION_TOOL_CATEGORIES` (or auto-escalation
  categories) and override the agent-level routing. For example, an
  `order_status` escalation normally goes to the orders team, **except** when
  its category is `Misrouted Order` or `Undelivered Order`, which route to the
  logistics team instead.

- `phone` is a **list** of numbers → multiple recipients for WhatsApp and SMS.
  SMS delivery requires the client to have `gupshup_sms_config` enabled
  (see §5.2b); when it's not configured, only WhatsApp is sent.
- `email` is a **JSON object** with `to` (primary recipients) and `cc`
  (carbon-copy recipients), both lists of email addresses. The `cc` field is
  optional — omit it for no CC. Primary recipients appear in the `To:` header;
  CC recipients appear in `Cc:` and receive the same escalation email for
  visibility without being the primary actionee.
- `routes` keys may be an exact **category** (most specific — lets one agent's
  categories fan out to different teams, e.g. `product_details` → `Restocking
  Query` to ops vs `Offline Store Suggestion` to retail) **or** an **agent**
  name (coarser default for that agent). Category keys and agent keys never
  collide (categories are Title Case with spaces; agents are `snake_case`).
- Each channel is resolved **independently** by walking candidate nodes
  most-specific-first — `routes[category]` → `routes[agent]` →
  `routes[CATEGORY_TO_AGENT[category]]` → `routes[parent_intent_agent]` →
  `default` — and taking the first node that has a value for that channel
  (phone additionally falls back to legacy `AGENT_PHONE_NUMBER`). So a
  category route that lists only `phone` still gets its `email` (both `to` and
  `cc`) from the agent or default node.
- Phone numbers are normalised by the existing `send_message` logic (strips
  `+91` / prefixes `91`), so configs may store bare 10-digit numbers.
- **Backward compatible node shapes.** A node may also be a bare list of phone
  numbers (the earlier `"default": [...]`/`"routes": {agent: [...]}` shape);
  such nodes are treated as phone-only with no email or SMS. Legacy
  `AGENT_PHONE_NUMBER`-only configs continue to route to that single number.
  The `email` field also accepts the **legacy list shape** (`"email":
  ["a@b.com"]`) — treated as `{"to": ["a@b.com"], "cc": []}` for backward
  compatibility.
- **Sender address** for email comes from env `ESCALATION_FROM_EMAIL` (default
  `escalations@bloomerce.ai`); delivery uses the existing `utils.send_email`.
- **SMS credentials** are per-client, stored in `client_configs` under key
  `gupshup_sms_config` (see §5.2b for schema). SMS is sent to the same
  `phone` numbers as WhatsApp, but only when the config exists **and**
  `enabled` is `true`; otherwise the SMS channel is silently skipped (no
  error, no fallback).

### Resolution order

`aget_escalation_recipients(client_id, *, agent, category, parent_intent)`
resolves the recipient list in priority order, stopping at the first non-empty
result:

1. **`routes[agent]`** when `agent` is supplied and present.
2. **`routes[CATEGORY_TO_AGENT[category]]`** — map the escalation `category` to
   an agent name (code-default map, below) for call sites that have only a
   category (the auto-escalations inside order flows).
3. **`routes[PARENT_INTENT_TO_AGENT[parent_intent]]`** — coarse last hint
   (e.g. `Sales → product_details`, `Enquiry → order_status`).
4. **`default`** list.
5. **`[AGENT_PHONE_NUMBER]`** (legacy fallback).

The function never returns an empty list when *any* number is configured, and
de-duplicates numbers before returning.

### `CATEGORY_TO_AGENT` (code default)

A small, code-owned map covers the categories the orchestrator already emits, so
routing works even before any client adds an `ESCALATION_ROUTING` block:

| Escalation `category`                        | Agent name              |
|----------------------------------------------|-------------------------|
| `Cancellation Requests`                      | `cancel_or_update_order`|
| `Order Update`, `Order Update - *`           | `cancel_or_update_order`|
| `Order Cancellation - Non-Integrated Partner`| `cancel_or_update_order`|
| `System Error - Order Update/Cancel Failed`  | `cancel_or_update_order`|
| `Delivery Query`                             | `delivery_timeline`     |
| `Pickup Query`                               | `return_exchange`       |
| `Restocking Query`                           | `product_details`       |
| `Product Complaint`                          | `product_details`       |
| `General` / unmapped                         | *(none → `default`)*    |

This map lives in code (per the agreed "per-client DB config + code default"
model). A client may later override numbers per agent purely through the DB
config; the code map only decides *which agent bucket a category falls into* and
changes rarely.

#### Category is a constrained enum (not free text)

The `escalate_to_agent` tool's `category` parameter is typed as
`Literal[ESCALATION_TOOL_CATEGORIES]` (in `agent_config.py`), so the LLM is
constrained by the function-calling schema to pick **exactly one** of a closed
set rather than emitting free text. This makes a tool-chosen category a
*reliable* routing signal, not just a fallback. `ESCALATION_TOOL_CATEGORIES` is
the single source of truth; every value except `"General"` must be a key in
`CATEGORY_TO_AGENT` **and** must appear in exactly one group in the code-default
`CATEGORY_TO_ESCALATION_GROUP` — both constraints are enforced by
`tests/test_escalation_routing.py` so the enum, the agent routing map, and the
escalation group map can't drift apart. The orchestrator method
`aescalate_to_agent(category: str)` stays free-text on purpose: the automatic
escalation paths call it directly with operational categories
(e.g. `"Order Update - Non-Integrated Partner"`) that are not LLM-facing.

---

## 4. Threading the agent name — no state mutation

`AGENTS.md` §2 forbids tools/adapters from mutating conversation state. We honor
this by passing the agent name as a **function argument through a closure** — we
do **not** write an `active_agent` field onto `state`.

`escalate_to_agent` is already built per-agent: every factory in
`tool_factory.py` calls `_create_escalation_tool(state)`
(`order_status_tools_factory:2703`, `return_exchange_tools_factory:2968`,
`cart_management_tools_factory:3929`, `product_details_tools_factory:4058`,
`cancel_or_update_tools_factory:5470`). The factory knows its own agent name, so
we capture it in the closure:

```
return_exchange_tools_factory
  └─ _create_escalation_tool(state, agent="return_exchange")   # captured in closure
        └─ escalate_to_agent(...) tool                         # reads nothing new from state
              └─ EscalationOrchestrator.aescalate_to_agent(..., agent="return_exchange")
                    └─ aget_escalation_recipients(client_id, agent=..., category=..., parent_intent=...)
```

- `_create_escalation_tool(state, agent: str = "")` gains an optional `agent`
  param (default keeps current behavior for any caller that omits it).
- `parent_intent` is **read** from `state` (read-only) as a fallback hint.
- The auto-escalation call sites inside order-update flows pass the `category`
  they already construct (and an `agent` where one is in scope); no state write.

Net result: routing context flows entirely as arguments — the exact stateless
contract §2 describes ("Tools receive data through function arguments and return
results").

---

## 5. New / changed components

### 5.1 `agent_config.py` — resolver on the tiered cache

- `aget_escalation_routing(client_id) -> dict` — reads `escalation_contact`
  through `aget_with_tiered_cache` (memory → Redis → DB), per `AGENTS.md` §3 /
  review-checklist #2. Returns the parsed `ESCALATION_ROUTING` block (plus the
  legacy `AGENT_PHONE_NUMBER`).
- `aget_escalation_contacts(client_id, *, agent=None, category=None, parent_intent=None) -> {"phone": [...], "email": {"to": [...], "cc": [...]}}`
  — pure per-channel resolution per §3. Handles the new `contacts` node shape
  (including the `email` object with `to`/`cc`), the legacy email-as-list
  shape, and the legacy `AGENT_PHONE_NUMBER`-only shape. The resolved `phone`
  numbers are used for **both** WhatsApp and SMS delivery (SMS only when the
  client's `gupshup_sms_config` is enabled).
- `aget_escalation_recipients(...) -> List[str]` — thin backward-compatible
  wrapper returning the `phone` channel only.
- `aget_agent_phone_number(client_id)` — **retained**, now implemented as
  "first number of the resolved `default` list", so existing non-escalation
  callers (`utils/delivery_utils`, store-visit notify in `tool_factory.py`)
  keep working unchanged.

> Migrating the read onto the tiered cache also fixes the current
> review-checklist #2 violation in `agent_config.py` (raw module dict + direct
> `SELECT` on `client_configs`).

### 5.2 `utils/escalation_helper.py` — single fan-out helper

```python
async def asend_escalation_notification(
    notification: str,
    *,
    client_id: Optional[str],
    state: Optional[dict] = None,
    agent: Optional[str] = None,
    category: Optional[str] = None,
) -> dict:
    """Resolve escalation recipients and notify ALL of them concurrently."""
```

- Resolves contacts via `aget_escalation_contacts(...)` (deriving
  `parent_intent` from `state`).
- Sends to **all** resolved `phone` numbers via **both** WhatsApp
  (`send_message(...)`) and SMS (Gupshup Enterprise SMS API, see §5.2b), and
  sends email to all `to` recipients (with `cc` recipients in the `Cc:`
  header) via `utils.send_email` run off the event loop with
  `asyncio.to_thread` (`AGENTS.md` §1). All three channels fire concurrently.
  SMS is only attempted when the client's `gupshup_sms_config` is enabled.
- **Per-recipient isolation**: each channel's sends are wrapped per-recipient;
  a failure in one channel never blocks/aborts the others. All failures logged
  with `trace_id` (`AGENTS.md` §5, Graceful Degradation).
- Returns `{"sent": [...], "failed": [...], "sms_sent": [...],
  "sms_failed": [...], "email_sent": [...], "email_failed": [],
  "email_cc": [...]}` (`sent`/`failed` = WhatsApp, `sms_sent`/`sms_failed` =
  SMS to the same phone numbers, `email_cc` = CC addresses included in the
  email).

This **replaces the ~15 duplicated send sites** in `EscalationOrchestrator`
(`AGENTS.md` — Shared Utilities Over Duplication / Minimal-Footprint Changes).

### 5.2a Queue-backed delivery (per-channel Dramatiq lanes)

Escalation delivery is a **side effect**, not part of the customer reply, so it
runs through the queue/consumer architecture (AGENTS.md §6) instead of blocking
the conversation turn on Gupshup HTTP + SMTP. WhatsApp, SMS, and email each get
**one lane** so a slow/failing channel never blocks or retries the others, and
each can be scaled and retried independently.

- Lanes `events.escalation_whatsapp` / `events.escalation_sms` /
  `events.escalation_email` and jobs `escalation_whatsapp` / `escalation_sms` /
  `escalation_email` (`workers/config.py`) + actors `escalation_whatsapp` /
  `escalation_sms` / `escalation_email` (`workers/actors.py`).
- The request path calls `publish_escalation_notification(payload)` once
  (`workers/event_publishers.py`); it dispatches **all three** lanes
  concurrently via `submit_or_inline`:
  - **lane disabled (default):** that channel is delivered **inline** as the
    fallback — byte-for-byte the previous behavior. Deploying changes nothing
    until ops flip `WEBHOOK_QUEUE_ENABLED` and add `escalation_whatsapp` /
    `escalation_sms` / `escalation_email` to `WEBHOOK_QUEUE_LANES`
    (independently — you can queue just SMS, say).
  - **lane enabled:** enqueues and returns fast; the actor sends on the worker
    with Dramatiq retry/backoff for transient failures.
- **Idempotency:** each actor dedups on `notify_id` under a per-channel
  namespace (`already_processed`, `WEBHOOK_DEDUP_TTL_SECONDS`), so the three
  channels share one `notify_id` without colliding and a redelivery of one
  channel can't re-send the others. Because channels are now separate jobs, a
  transient SMTP failure retries **email only** — it never re-sends WhatsApp
  or SMS.
- Send logic lives in the state-free `asend_escalation_whatsapp` /
  `asend_escalation_sms` / `asend_escalation_email` (`escalation_helper.py`);
  the inline fallbacks and the actors call them, and
  `asend_escalation_notification_core` runs all three concurrently for the
  inline/test path. The request-path wrapper
  `asend_escalation_notification(state=...)` delegates to the core. The payload
  carries `notification`, `client_id`, `agent`, `category`, `parent_intent`,
  `trace_id`, `notify_id` — no `state` (not serializable).

#### Retry mechanism (per-channel, independent)

Each Dramatiq actor uses the built-in `Retries` middleware with
channel-appropriate settings. If the actor raises (i.e. the downstream send
failed), Dramatiq automatically re-enqueues the job on the **same lane** with
exponential backoff — other channels are completely unaffected.

| Channel | Actor | Max retries | Backoff | Dead-letter behavior |
|---------|-------|-------------|---------|----------------------|
| WhatsApp | `escalation_whatsapp` | 5 | Exponential: 1s → 2s → 4s → 8s → 16s (jittered) | After max retries: dead-letter the message, `report_error(...)` with `trace_id` and `notify_id` for Rollbar alerting |
| SMS | `escalation_sms` | 5 | Exponential: 1s → 2s → 4s → 8s → 16s (jittered) | Same — dead-letter + `report_error` |
| Email | `escalation_email` | 3 | Exponential: 2s → 4s → 8s (jittered) | Same — dead-letter + `report_error` |

**How it works end-to-end:**

1. `publish_escalation_notification(payload)` dispatches three jobs — one per
   lane — via `submit_or_inline`. Each job carries the same `notify_id`.
2. Each actor runs independently on its worker process:
   - **Success**: marks `notify_id` as processed in the per-channel dedup
     namespace (Redis, `WEBHOOK_DEDUP_TTL_SECONDS` TTL) and returns.
   - **Transient failure** (network timeout, Gupshup 5xx, SMTP connection
     refused): the actor raises, Dramatiq re-enqueues on the same lane after
     the backoff delay. The dedup guard ensures a successful prior delivery
     isn't re-sent on retry.
   - **Permanent failure** (invalid number, DLT template rejected, Gupshup
     auth error): the actor logs the error, marks as processed (to prevent
     infinite retries on the same bad input), and returns without raising.
   - **Max retries exhausted**: Dramatiq dead-letters the message. The actor's
     `on_failure` callback calls `report_error(...)` with full context
     (`trace_id`, `client_id`, `notify_id`, channel, last error) so ops is
     alerted via Rollbar.
3. Because each channel is a separate job on a separate queue, a WhatsApp
   failure retries **only WhatsApp**, an SMS failure retries **only SMS**, and
   an email failure retries **only email**. They never interfere.

**Inline fallback retries** (when the lane is disabled): the inline path wraps
each channel's send in `tenacity` with 2 retries and exponential backoff
(1s, 2s) for transient failures, matching the async behavior but keeping
latency bounded since it runs on the request path.

### 5.2b SMS delivery via Gupshup Enterprise SMS API

SMS notifications use the **Gupshup Enterprise SMS API** — the same vendor
already used for WhatsApp, keeping credential management and billing unified.

#### API endpoint

```
POST https://enterprise.smsgupshup.com/GatewayAPI/rest
Content-Type: application/x-www-form-urlencoded
```

| Parameter | Value | Notes |
|-----------|-------|-------|
| `method` | `sendMessage` | Fixed |
| `userid` | per-client | From `gupshup_sms_config` |
| `password` | per-client | From `gupshup_sms_config` (never logged) |
| `send_to` | `91XXXXXXXXXX` | Country code + 10-digit number |
| `msg` | URL-encoded notification text | Truncated to 160 chars for single-part SMS; multi-part allowed up to config cap |
| `msg_type` | `TEXT` (English) or `Unicode_Text` (regional) | Auto-detected from message content |
| `auth_scheme` | `PLAIN` | Gupshup supports only plain auth; security via HTTPS |
| `v` | `1.1` | API version |
| `format` | `JSON` | Response format |
| `principalEntityId` | per-client DLT entity ID | Required for TRAI DLT compliance |
| `dltTemplateId` | per-template | Required — see DLT templates below |

#### Per-client SMS config (`client_configs`, key `gupshup_sms_config`)

Read through the tiered cache (§3). SMS is **opt-in**: no config → no SMS
delivery (the channel is silently skipped).

```json
{
  "gupshup_sms_config": {
    "enabled": true,
    "userid": "20000XXXXX",
    "password": "encrypted:aes256:...",
    "principal_entity_id": "1234567890123456789",
    "sender_id": "BLMRCE",
    "dlt_templates": {
      "escalation_immediate": {
        "template_id": "1107XXXXXXXXXXXX",
        "template": "URGENT: {#alphanumeric#} escalation for order {#alphanumeric#}. Customer: {#numeric#}. Trace: {#alphanumeric#}. Action required."
      },
      "escalation_normal": {
        "template_id": "1107XXXXXXXXXXXX",
        "template": "{#alphanumeric#} escalation for order {#alphanumeric#}. Customer: {#numeric#}. Trace: {#alphanumeric#}."
      },
      "escalation_presales": {
        "template_id": "1107XXXXXXXXXXXX",
        "template": "Pre-sales enquiry: {#alphanumeric#}. Customer: {#numeric#}. Trace: {#alphanumeric#}. Please follow up."
      }
    }
  }
}
```

- **`password`** is stored AES-256-encrypted in the DB and decrypted at
  runtime — it is **never** logged or included in error reports.
- **`sender_id`** is the DLT-registered header (6-char alphanumeric) displayed
  as the SMS sender name.
- **`dlt_templates`** maps logical template keys to DLT-registered template
  IDs and their content patterns. The send helper selects the template based
  on `immediate_attention` and `escalation_group`.

#### TRAI DLT compliance (India)

All commercial SMS in India must go through DLT-registered templates (TRAI
TCCCPR 2018, updated Jan 2026). Requirements:

1. **Principal Entity registration** — the client must register on a DLT
   platform (Jio, Airtel/Vilpower, BSNL, Vodafone-Idea) and provide their
   `principal_entity_id`.
2. **Sender ID (Header) registration** — a 6-character alphanumeric sender
   name, registered and approved on the DLT platform.
3. **Template registration** — each SMS body must match a pre-approved DLT
   template. Variable fields use typed tags per the Jan 2026 TRAI directive:
   `{#numeric#}` (OTPs, amounts, phone numbers), `{#alphanumeric#}` (IDs,
   names, categories), `{#url#}` (links), `{#email#}` (email addresses).
4. **URL and callback number whitelisting** — any URL or phone number in the
   SMS body must be whitelisted on the DLT platform.

The `asend_escalation_sms` helper fills the template variables at runtime and
passes the `dltTemplateId` + `principalEntityId` with every API call. If the
client's `gupshup_sms_config` is missing or `enabled` is `false`, SMS
delivery is skipped entirely — no error, no fallback.

#### SMS message formatting

Unlike WhatsApp (rich text, emojis, multi-line), SMS has a 160-character
single-part limit (or 70 for Unicode). The notification text sent over SMS is
a **condensed version** of the WhatsApp notification, built by
`_format_sms_notification(notification, metadata)`:

- Strips emoji prefixes and markdown formatting.
- Uses the DLT template matching the `escalation_group` and
  `immediate_attention` values.
- Fills template variables: category, order ID (if present), customer phone,
  trace ID.
- Falls back to the `escalation_normal` template if no group-specific
  template is configured.

#### Implementation (`escalation_helper.py`)

```python
async def asend_escalation_sms(
    phone_numbers: List[str],
    notification: str,
    *,
    client_id: str,
    metadata: Dict[str, Any],
    trace_id: str,
) -> Dict[str, List[str]]:
    """Send SMS to all phone numbers via Gupshup Enterprise SMS API.

    Receives the same phone numbers as WhatsApp — no separate SMS recipient
    list. SMS is a parallel channel to the same contacts.
    """
```

- Loads `gupshup_sms_config` via `aget_with_tiered_cache`.
- If config is missing or `enabled` is `false`, returns
  `{"sms_sent": [], "sms_failed": []}` immediately (no-op).
- Selects DLT template based on `metadata["escalation_group"]` and
  `metadata["immediate_attention"]`.
- Sends to each phone number via `asyncio.gather` using the shared
  `httpx.AsyncClient` (`utils/http_client.py`, `AGENTS.md` §1).
- Parses the Gupshup response (`success | <id>` or `error | <code> | <msg>`)
  and classifies each send as success or failure.
- **Per-number isolation**: one number's failure does not suppress the others.
- Logs each send with `trace_id`, `client_id`, and the Gupshup response ID.

### 5.2c One common escalation flow (`_adispatch_escalation`)

Every escalation method on `EscalationOrchestrator` —
`aescalate_to_agent` (the LLM hand-off), `aescalate_undelivered_order`, and
`aescalate_misrouted_order` — shares one private helper,
`EscalationOrchestrator._adispatch_escalation(...)`. It is the single place that
**(1) delivers** the notification to ALL configured WhatsApp numbers, SMS
numbers, AND emails (routed by agent/category through the per-channel queue
lanes) and **(2) logs**
the escalation with uniform metadata. Each method now only builds its own
notification text and any post-steps (e.g. `aescalate_to_agent` additionally
stores conversation history and switches to human-agent mode); the
deliver-and-log core is identical for all. This removed the duplicated
build→publish→log boilerplate (and the dead `send_message` /
`aget_agent_phone_number` imports) that the delivery escalations previously
carried, so multi-number + SMS + email support is inherent to every path rather than
re-implemented per method.

### 5.3 `core/orchestrator.py` — call-site swaps

Replace each:

```python
await send_message(await aget_agent_phone_number(client_id=client_id),
                   notification, client_id=client_id)
```

with:

```python
await asend_escalation_notification(
    notification, client_id=client_id, state=state,
    agent=<agent if in scope>, category=<category at that site>,
)
```

The `Delivery Partner Sync` branch is left untouched.

> **Implementation note.** An audit of call sites found that only **3**
> escalation send paths are live: `aescalate_to_agent` (the LLM-tool path —
> receives full `agent` + `category`), `aescalate_undelivered_order`, and
> `aescalate_misrouted_order` (both `Delivery Query`). The remaining sync
> `escalate_*` and async `aescalate_*` variants have **zero callers** (verified
> by grep) and were left untouched to keep the diff minimal; if revived they
> retain their original single-number behavior until swapped to the helper.

### 5.4 `tool_factory.py` — pass agent into the escalation tool

`_create_escalation_tool(state, agent="")`; each factory passes its own agent
name. The tool forwards `agent` to `EscalationOrchestrator.aescalate_to_agent`.

### 5.4a Bulk / B2B / wholesale discount escalation

Bulk / wholesale / B2B requests are routed to the **`discount`** agent by intent
detection. Previously that agent only surfaced support contact details to the
customer (`get_contact_information`) and could not notify staff. The
`escalate_to_agent` tool is now added to the discount agent
(`agent="discount"`), so a bulk/B2B discount hand-off fires an escalation
notification routed to `routes["discount"]` (falling back to `default` →
legacy). `CATEGORY_TO_AGENT` also maps `Bulk Order Discount`, `Bulk Order`,
`Wholesale Inquiry`, and `B2B Order` to the `discount` bucket.

### 5.4b Physical-store-visit suggestion → store-manager-first routing

`_anotify_agent_store_visit` (`tool_factory.py`) — fired when a store is
suggested via the `get_nearest_store` tool or the browser-geolocation path in
`generic_skill_node.py` — previously sent to a single `AGENT_PHONE_NUMBER` via
`send_message`, outside this design. It now flows through the unified escalation
pipeline as an **`Offline Store Suggestion`**, with one key enhancement:
**the notification routes to the specific store manager first**, because the
system already knows *which* store was suggested and that store's record carries
its own `phone` and `manager_email`.

#### Store data carries manager contacts

Each store object in the `store_locations` config (`client_configs`, JSON array)
already includes per-store contact fields:

```json
{
  "name": "Flagship Store",
  "address": "...",
  "city": "Mumbai",
  "phone": "+91-98765-43210",
  "manager_name": "Rahul Sharma",
  "manager_email": "rahul@example.com",
  "latitude": 19.0596,
  "longitude": 72.8295,
  "hours": "11 AM – 9 PM"
}
```

Both `afind_nearest_store` and the context-injection path return the full store
object (including `phone` and `manager_email`) to the caller.

#### Resolution order (store-manager-first)

When `_anotify_agent_store_visit` fires, the recipient contacts are resolved in
a **store-manager-first** priority order:

| Priority | Source | What it uses |
|----------|--------|--------------|
| 1 | **Store object itself** | `store["phone"]` → WhatsApp, `store["manager_email"]` → email. The most specific: the manager of the actual store the customer is being directed to. |
| 2 | `ESCALATION_ROUTING` routes | `routes["Offline Store Suggestion"]` (category) or `routes["product_details"]` (agent) — the configured retail/store-ops team as a fallback or **CC**. |
| 3 | `default` / `AGENT_PHONE_NUMBER` | Legacy catch-all. |

The helper `aget_store_visit_contacts(client_id, store)` merges these:

```python
async def aget_store_visit_contacts(
    client_id: str, store: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve contacts for a store-visit notification.

    Priority: store manager contacts (most specific) PLUS the configured
    escalation routing contacts (so the ops team also gets visibility).
    De-duplicates across both sources.
    """
    phones: List[str] = []
    email_to: List[str] = []
    email_cc: List[str] = []

    # Priority 1 — the specific store's manager contacts (primary recipient)
    store_phone = store.get("phone")
    if store_phone:
        phones.append(normalize_phone(store_phone))
    store_email = store.get("manager_email")
    if store_email:
        email_to.append(store_email)

    # Priority 2+3 — configured ESCALATION_ROUTING (category/agent/default)
    routing_contacts = await aget_escalation_contacts(
        client_id, agent="product_details", category="Offline Store Suggestion"
    )
    for p in (routing_contacts.get("phone") or []):
        if normalize_phone(p) not in phones:
            phones.append(normalize_phone(p))
    routing_email = routing_contacts.get("email") or {}
    for e in (routing_email.get("to") or []):
        if e not in email_to:
            email_to.append(e)
    for e in (routing_email.get("cc") or []):
        if e not in email_cc and e not in email_to:
            email_cc.append(e)

    return {"phone": phones, "email": {"to": email_to, "cc": email_cc}}
```

This means:
- The **store manager** always gets the notification (they need to know a
  customer is coming).
- The **ops/retail team** (from `ESCALATION_ROUTING`) also gets it (for
  visibility/tracking) — unless they are the same person.
- Numbers and emails are de-duplicated so no one gets double-pinged.

#### Example scenarios

**Scenario A — Store manager configured, no ESCALATION_ROUTING override:**

- Store = `{"name": "Connaught Place", "phone": "9876543210", "manager_email": "cp-mgr@brand.com"}`
- `ESCALATION_ROUTING` has no `"Offline Store Suggestion"` key, `default` has `email: {"to": ["support@brand.com"]}`
- **Result:** WhatsApp → `9876543210` (store manager) + default phones;
  Email To: `cp-mgr@brand.com` (store manager) + `support@brand.com` (default)

**Scenario B — Store manager configured, dedicated ops route with CC:**

- Store = `{"phone": "9876543210", "manager_email": "cp-mgr@brand.com"}`
- `routes["Offline Store Suggestion"]` = `{"phone": ["9222222222"], "email": {"to": ["retail-ops@brand.com"], "cc": ["store-leads@brand.com"]}}`
- **Result:** WhatsApp → `9876543210` + `9222222222`; Email To: `cp-mgr@brand.com` + `retail-ops@brand.com`; Cc: `store-leads@brand.com`

**Scenario C — Store has no manager contacts (legacy data):**

- Store = `{"name": "Old Store", "city": "Delhi"}` (no `phone`/`manager_email`)
- Falls through entirely to `ESCALATION_ROUTING` → `routes["Offline Store Suggestion"]`
  → `routes["product_details"]` → `default` → `AGENT_PHONE_NUMBER`
- **Result:** Identical to the generic routing (backward compatible)

**Scenario D — Same person is both store manager and ops contact:**

- Store `phone` = `9876543210`, `ESCALATION_ROUTING` default also = `["9876543210"]`
- De-duplication ensures **one** WhatsApp message, not two.

#### Delivery pipeline (unchanged structure)

- **Delivery** via `publish_escalation_notification(...)` with
  `category="Offline Store Suggestion"`, `agent="product_details"`, and
  `contacts_override=aget_store_visit_contacts(client_id, store)` — the
  override injects the store-manager-first contact list into the standard
  pipeline, so it still gets per-channel queue lanes and the webhook channel.
- **Logged** to the escalations table via `alog_escalation_from_state` for
  reporting (with `store_name` and `manager_name` in metadata), but it
  deliberately does **not** switch the conversation to human-agent mode (a
  store suggestion is an FYI, not a hand-off).
- **Deduped** per `(client_id, conversation, store)` via `already_processed`
  (namespace `store_visit_notify`) so repeated suggestions of the same store in
  one conversation — including the tool path and the geo path both firing —
  don't re-ping the team.
- **Webhook payload** includes the store object (name, address, manager) in
  `metadata.store` so the client's CRM can auto-create a task for the right
  store manager.

### 5.4c Uniform escalation metadata

Every escalation — regardless of path (LLM `escalate_to_agent`, auto delivery
escalations, Delivery Partner Sync, store suggestion) — gets its metadata built
by one helper, `build_escalation_metadata` (`escalation_helper.py`), invoked
inside `alog_escalation_from_state`. It **guarantees six canonical fields are
always present**: `trace_id`, `phone_number`, `escalation_type`
(`category` slugified), `escalation_classification` (validated to
`user_configured` / `agentic` / `system`, default `system`),
`escalation_group` (validated to `pre_sales` / `post_sales` /
`offline_leads`), and `immediate_attention` (boolean — `true` when customer
is frustrated, `false` otherwise). Caller-supplied metadata provides extras
(`order_id`, `store_name`, …) and can **never** clobber the six required keys
(they are merged last). The LLM path passes its chosen classification via
the `escalation_classification` argument; other paths default to `system`.
This removes the previous divergence where only the `escalate_to_agent` path
carried `escalation_type` / `escalation_classification`.

### 5.4d Escalation groups: pre-sales, post-sales, and offline leads

Beyond the existing `escalation_classification` (which captures *who triggered*
the escalation — `user_configured` / `agentic` / `system`), we add two
orthogonal fields in `escalation_metadata`:

1. **`escalation_group`** — captures *what operational bucket* the escalation
   falls into. Set deterministically from the category and agent at escalation
   time.
2. **`immediate_attention`** — a boolean flag set to `true` when the customer
   shows frustration signals, `false` otherwise. Applies to **every** group
   equally (pre-sales, post-sales, and offline leads can all have frustrated
   customers).

Both fields live inside `escalation_metadata` in the `escalations` table.

#### `escalation_group` values

| `escalation_group` | When | Examples |
|---------------------|------|----------|
| `pre_sales` | Issues arising **before** a purchase — the customer is exploring, asking questions, or requesting information that the bot lacks config/data to serve. | Product details missing, recommendation hand-off, delivery timeline inquiry, discount/B2B/bulk request, restocking query |
| `post_sales` | Issues arising **after** a purchase — a third party (logistics, payment gateway, warehouse) or an operational process has failed the customer, or post-order actions require human intervention. | Order delivery delayed, refund delayed, return delayed, exchange delayed, misrouted order, undelivered order, cancellation escalation, delivery partner sync |
| `offline_leads` | Customer interactions that indicate intent to visit or engage with a **physical store** or an offline sales channel. | Offline store suggestion, store-visit notification, walk-in appointment request |

#### Category → escalation_group mapping (extensible, client-configurable)

The base mapping is code-owned (`CATEGORY_TO_ESCALATION_GROUP` in
`agent_config.py`), but clients can **override and extend** it via a
per-client config key `escalation_group_categories` in Postgres
`client_configs` (read through the tiered cache, §3). This lets each client
add domain-specific categories without a code deploy.

**Code-default `pre_sales` categories:**
- `Restocking Query` (product_details)
- `Product Complaint` (product_details — missing info / quality concern)
- `Recommendation Hand-off` (recommendations — bot couldn't narrow results)
- `Delivery Timeline Inquiry` (delivery_timeline — pre-purchase ETA questions)
- `Bulk Order Discount` / `Bulk Order` / `Wholesale Inquiry` / `B2B Order` (discount)
- `Cart Issue` (cart_management)
- `Callback Request` (escalation — the bot itself couldn't resolve; capability gap)
- `General` (catch-all — likely a bot capability gap)

**Code-default `post_sales` categories:**
- `Delivery Partner Sync` (delivery_timeline — logistics partner coordination)
- `Cancellation Requests` (cancel_or_update_order — cancellation escalation)
- `Delivery Query` / `Order Delivery Delayed` (delivery_timeline — delayed/stuck shipment)
- `Payment/Refund Status` (order_status — refund delayed)
- `Pickup Query` (return_exchange — return delayed / pickup not scheduled)
- `Return Request` / `Exchange Request` (return_exchange — exchange delayed)
- `Misrouted Order` (order_status — misrouted)
- `Undelivered Order` (order_status — undelivered)
- `Delay in Dispatch` (order_status — warehouse/fulfillment delay)
- `Damaged in Transit` (order_status — carrier damage)
- `Earlier Delivery Request` (order_status — customer frustrated with timeline)
- `Order Status Query` (order_status — when escalated due to partner issues)

**Code-default `offline_leads` categories:**
- `Offline Store Suggestion` (product_details — store-visit notification)
- `Walk-in Appointment` (product_details — in-store appointment request)

**Client-configurable override** (`client_configs`, key
`escalation_group_categories`):

```json
{
  "escalation_group_categories": {
    "pre_sales": [
      "Restocking Query", "Product Complaint", "Recommendation Hand-off",
      "Delivery Timeline Inquiry", "Bulk Order Discount", "Cart Issue",
      "Custom Sizing Request"
    ],
    "post_sales": [
      "Delivery Partner Sync", "Cancellation Requests", "Order Delivery Delayed",
      "Refund Delayed", "Return Delayed", "Exchange Delayed",
      "Misrouted Order", "Undelivered Order", "Warranty Claim"
    ],
    "offline_leads": [
      "Offline Store Suggestion", "Walk-in Appointment", "Trunk Show RSVP"
    ]
  }
}
```

When present, the client config **replaces** the code-default map entirely for
that client, giving full control over which categories belong to which group.
Categories not in any group default to `pre_sales` (conservative: assume it's
our gap until proven otherwise). The config is read via `aget_with_tiered_cache`
(memory → Redis → DB) per §3.

#### `immediate_attention` — frustration-driven urgency flag

Every escalation carries an **`immediate_attention`** boolean in
`escalation_metadata`, regardless of `escalation_group`:

| `immediate_attention` | When |
|------------------------|------|
| `true` | Customer shows frustration signals: explicit anger, repeated follow-ups on the same issue, threats, cancellation demands, or has been escalated before on this issue. |
| `false` | Normal escalation — no frustration signals detected. Default. |

**How it's determined:**

The LLM (within the skill node's tool loop, or via a lightweight classification
call at escalation time) assesses frustration based on:
- Repeated follow-ups on the same issue (2+ turns on the same topic)
- Explicit anger / threats / cancellation mentions
- Time elapsed since order (> 7 days overdue for post-sales)
- Whether customer has already been escalated before on this issue

The `escalate_to_agent` tool gains an `immediate_attention` parameter
(`bool`, default `False`) so the LLM can set it directly. Auto-escalation
paths (undelivered, misrouted) default to `True` since the system itself
detected a failure serious enough to auto-escalate. The `Frustration`
escalation category (triggered by frustration-detection in intent detection)
always sets `immediate_attention = True` — the `escalation_group` is still
determined by the underlying category/agent context.

**Impact on notification:**

When `immediate_attention` is `true`, the WhatsApp and email notification text
is prefixed with:

```
⚠️ IMMEDIATE ATTENTION REQUIRED ⚠️
```

This prefix is injected by `_adispatch_escalation` before publishing to the
notification channels — the caller doesn't format it; the dispatch helper adds
it automatically based on the metadata field. The structured webhook payload
also carries `immediate_attention` in `data.metadata` so the client's CRM can
auto-prioritize tickets.

**Example notification (`immediate_attention: true`, post-sales):**

```
⚠️ IMMEDIATE ATTENTION REQUIRED ⚠️

[Undelivered Order Escalation]:
⏰ Time: 2026-06-28 14:30:22 IST
🔍 Trace ID: tr_abc123
📦 Order: #GV10741
🏷️ Group: Post-sales
💬 Last Message: "It's been 12 days, no one is responding. I want my money back!"
📱 Phone: 9876543210
```

vs. normal (`immediate_attention: false`, pre-sales):

```
[Delivery Timeline Inquiry Escalation]:
⏰ Time: 2026-06-28 14:30:22 IST
🔍 Trace ID: tr_xyz789
🏷️ Group: Pre-sales
💬 Last Message: "Can you tell me when this product will be back in stock?"
📱 Phone: 9123456789
```

#### Cancellation-aversion as `post_sales`

When a client has configured `cancellation_aversion_enabled = true` and a
customer requests cancellation, the system does **not** cancel the order.
Instead it escalates with:
- `category` = `"Cancellation Requests"`
- `escalation_group` = `"post_sales"` (this is a post-purchase operational
  scenario — the client policy requires human retention intervention)
- `immediate_attention` = determined by frustration signals (if customer is
  angry about wanting to cancel → `true`; calm inquiry → `false`)
- `escalation_metadata.cancellation_aversion` = `true` (explicit marker for
  dashboards/CRM to identify retention-type escalations)

#### Implementation in `build_escalation_metadata`

`build_escalation_metadata` now guarantees **six** canonical fields (was four):

```python
{
    "trace_id": "...",
    "phone_number": "...",
    "escalation_type": "cancellation_requests",
    "escalation_classification": "user_configured",
    "escalation_group": "post_sales",          # NEW
    "immediate_attention": True,               # NEW
}
```

The `escalation_group` is resolved by first checking the client-configurable
`escalation_group_categories` map (from `client_configs` via tiered cache),
then falling back to the code-default `CATEGORY_TO_ESCALATION_GROUP[category]`.
`immediate_attention` is passed as an argument and defaults to `False`.

### 5.6 Outbound escalation webhooks (client CRM subscriptions)

A client can **subscribe** to receive every escalation as a signed HTTP webhook
at their own endpoint, so escalations show up in their CRM / ticketing tool
alongside WhatsApp, SMS, and email. Webhooks are **opt-in**: if a client has no
active subscription, nothing is sent over this channel (WhatsApp + SMS + email
only).

This is modeled on how Shopify / Stripe send webhooks, mapped onto our existing
per-channel queue-lane architecture (it becomes a **fourth delivery channel**
next to WhatsApp, SMS, and email).

#### Patterns adopted from Shopify / Stripe
| Concern | Industry practice | Our choice |
|---|---|---|
| Transport | HTTPS POST, JSON body | HTTPS-only POST, JSON |
| Auth / integrity | HMAC-SHA256 of the raw body with a shared secret (`X-Shopify-Hmac-SHA256`); Stripe adds a timestamp (`t=…,v1=…`) to stop replay | Per-endpoint secret; `X-Bloomerce-Signature: t=<unix>,v1=<hex>` over `"{t}.{raw_body}"` (replay-resistant) |
| Idempotency | Stable event id so the consumer can dedup; at-least-once delivery | `id` = `escalation_id`, echoed in `X-Bloomerce-Webhook-Id` |
| Retries | Retry non-2xx with backoff over hours/days; auto-disable a chronically failing endpoint | Dramatiq retry/backoff on its own lane; per-endpoint success marker so retries only re-post failed endpoints; optional auto-disable after N consecutive failures |
| Versioning | Pinned API version header | `X-Bloomerce-Api-Version` + `api_version` in payload |
| Latency | Endpoint must respond fast (Shopify ~5s) | Short timeout (5–10s); delivery runs on the worker, never the conversation turn |

#### Subscription config (per-client, opt-in)
New `client_configs` key `escalation_webhooks`, read via the tiered cache (§3):
```json
{
  "escalation_webhooks": {
    "enabled": true,
    "endpoints": [
      {
        "id": "crm-primary",
        "url": "https://client-crm.example.com/hooks/escalations",
        "secret": "whsec_…",            // per-endpoint HMAC secret (never logged)
        "active": true,
        "categories": ["*"],            // optional filter; default = all
        "api_version": "2026-01-01"
      }
    ]
  }
}
```
No `escalation_webhooks` block / no `active` endpoint ⇒ the webhook channel is
skipped entirely.

#### Payload (structured, not the WhatsApp text)
Unlike WhatsApp/email (which get the formatted notification string), the webhook
carries the **structured** escalation record:
```json
{
  "id": "<escalation_id>",
  "type": "escalation.created",
  "api_version": "2026-01-01",
  "created_at": "2026-06-16T04:07:35Z",
  "client_id": "…",
  "data": {
    "escalation_id": "…", "category": "Return Request", "agent": "return_exchange",
    "escalation_type": "return_request", "escalation_classification": "user_configured",
    "escalation_group": "post_sales", "immediate_attention": false,
    "reason": "…", "action_required": "…", "status": "unresolved",
    "customer_phone": "…", "conversation_id": "…", "order_id": "…",
    "trace_id": "…", "channel": "whatsapp|web-chat",
    "metadata": { … }, "recent_messages": ["…"]
  }
}
```
Signed headers on each POST: `X-Bloomerce-Event`, `X-Bloomerce-Webhook-Id`
(= escalation_id, the idempotency key), `X-Bloomerce-Timestamp`,
`X-Bloomerce-Signature`, `X-Bloomerce-Api-Version`, `Content-Type: application/json`.

#### Delivery — a fourth per-channel lane
- New lane `events.escalation_webhook` / job `escalation_webhook` / actor
  `escalation_webhook` (mirrors the WhatsApp/email lanes, AGENTS.md §6). External
  HTTP is the flakiest channel, so isolating it on its own lane with its own
  retry/backoff matters most here.
- New publisher `publish_escalation_webhook(event)` (`event_publishers.py`) via
  `submit_or_inline` — inline fallback posts when the lane is off (default), so
  behavior is consistent with the other channels. It **first checks the
  subscription config and no-ops when the client isn't subscribed** ("only if
  subscribed do we send").
- Delivery client (`utils/webhook_delivery.py`): POSTs the signed body to each
  active endpoint via the shared `httpx.AsyncClient` (`utils/http_client.py`)
  with `follow_redirects=False`, a hard timeout, and a body-size cap.
- **Idempotency / retries:** the actor dedups per `(escalation_id, endpoint_id)`
  and **marks an endpoint delivered only on a 2xx** — so a Dramatiq retry
  re-posts *only* the endpoints that haven't yet succeeded (no double-posting to
  ones that did). If any endpoint is still undelivered it raises so Dramatiq
  backs off; after max retries it dead-letters and `report_error`s.

#### Security
- **HTTPS only**; reject `http://`.
- **SSRF guard:** resolve the host and reject private / loopback / link-local /
  cloud-metadata ranges; `follow_redirects=False` so a 302 can't bounce to an
  internal address.
- Per-endpoint **secret** for HMAC; secrets are never logged and live only in the
  client's config (tenant-scoped, AGENTS.md §7).
- Replay protection via the signed timestamp; consumers reject stale signatures.

#### Integration with the common flow
`_adispatch_escalation` already (1) delivers WhatsApp + SMS + email and (2) logs
the escalation (returning `escalation_id`). Add a fourth step: after logging, build
the structured event (it already has category, agent, classification, reason,
metadata, escalation_id, and `state` for phone/conversation/recent messages) and
call `publish_escalation_webhook(event)`. Because that publisher self-skips when
the client isn't subscribed, **every** escalation path (LLM hand-off, undelivered,
misrouted, store suggestion, …) gains CRM webhooks for free, with zero change at
the call sites.

### 5.5 Not changed

- `schema.py` — **no** `active_agent` field (deliberately avoided, §4).
- `generic_skill_node.py` — **no** state write; the node already has the agent
  name locally and only needs to keep passing it to the factory.

---

## 6. Backward compatibility & multi-tenancy

- **Legacy configs** (`{"AGENT_PHONE_NUMBER": "..."}`) → that number becomes the
  one-element `default`; behavior is byte-for-byte the same as today.
- **Tenant isolation** (`AGENTS.md` §7) — all reads/sends remain keyed by
  `client_id`; fallbacks stay within the same tenant and never default to a
  "default" tenant. A missing `client_id` is handled exactly as today.
- **Graceful degradation** — malformed `ESCALATION_ROUTING` JSON, an unknown
  agent, or an empty route all fall back down the chain to `default` →
  `AGENT_PHONE_NUMBER`; a per-number send failure is isolated and logged.
- **Idempotent** — resolution is deterministic for the same inputs; sends are
  read-only side effects.

---

## 7. Testing

- Unit-test `aget_escalation_recipients` resolution order: agent hit →
  category-map hit → parent_intent hit → default → legacy; de-dup; empty/bad
  config.
- Fan-out: multiple numbers all receive the message; one failing number does not
  suppress the others.
- Backward compat: a legacy single-number config still routes correctly through
  every path.
- Use the mock system (`USE_MOCK_SERVICES`) to assert `send_message` recipients
  per scenario (`AGENTS.md` — Testing).

---

## 8. Rollout

1. Ship code with the legacy fallback active — zero behavior change for clients
   that haven't added `ESCALATION_ROUTING`.
2. Add `ESCALATION_ROUTING` to a pilot client's `escalation_contact` config;
   verify per-agent delivery in staging.
3. Document the config block in client onboarding once validated.

---

## 9. `AGENTS.md` compliance summary

| Rule | How addressed |
|------|---------------|
| §1 Full Async | `async def` helpers; `asyncio.gather` fan-out. |
| §2 Stateless Tools | Agent name threaded as a **function argument** via closure; **no state mutation** anywhere; new helpers only read state. |
| §3 Tiered Cache | Routing config read moves onto `aget_with_tiered_cache` (fixes an existing violation). |
| §5 Trace ID | Per-number send results logged with `trace_id`. |
| §7 Tenant Isolation | Everything keyed by `client_id`; fallbacks stay in-tenant. |
| Shared Utilities / Minimal Footprint | ~15 duplicated send sites collapse into one helper; hot files get call-site swaps only. |
| Graceful Degradation | Fallback chain + per-number send isolation. |
| Testing | Mock-based unit + fan-out + backward-compat coverage. |
</content>
</invoke>
