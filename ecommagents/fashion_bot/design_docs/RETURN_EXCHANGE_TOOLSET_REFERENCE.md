# `return_exchange` Toolset — Code Reference & Defect List

> Documented by reading the deployed source at commit **`37bb122`** (service
> `webchat` / `gupshup`). Every signature, gate, data source and response shape
> below was traced through the code at that commit — nothing here is
> reconstructed from production payloads.

**Scope**: the 17 tools the `return_exchange` agent hands to the LLM.

**Registry path**: `TOOL_REGISTRY["return_exchange"] → "return_exchange_tools_factory"`,
resolved lazily by `core/tool_registry.py::_get_factory_map()`
(`tool_registry.py:60`). The factory itself is
`tool_factory.py:3466::return_exchange_tools_factory(state, messages_list)`.
`get_contact_information` is not built by the factory — it is appended to every
agent centrally by `_append_shared_contact_tool` (`core/tool_registry.py:252`).

The factory returns, in order (`tool_factory.py:3661-3670`):

```python
return [
    get_customers_delivered_orders_by_phone,
    get_recent_orders,
    *return_partner_tools,      # the 10 Return Prime tools, in registration order
    get_order_details,
    get_final_return_exchange_message,
    get_nearest_store,
    escalate_to_agent,
]
# + get_contact_information appended by the registry
```

---

## Contents

- [Architecture at a glance](#architecture-at-a-glance)
- [The two identity gates](#the-two-identity-gates)
- [Tool reference (1–17)](#tool-reference)
- [Defect list](#defect-list)
- [Suggested fixes](#suggested-fixes)

---

## Architecture at a glance

The 10 "Return Prime" tools in `return_partners/tools.py` are **pure pass-through
wrappers**. Each is ~6 lines: pull `client_id` off graph state, forward every
argument to a `ReturnExchangeOrchestrator` classmethod, return the dict verbatim.
All validation, identity checking, caching and error shaping happens below them.

```
LLM tool call
  │
  ├─ return_partners/tools.py                 ← @tool wrapper, docstring only
  │     _client_id_from_state(state)
  │
  ├─ core/orchestrator.py::ReturnExchangeOrchestrator   (orchestrator.py:751)
  │     thin delegation, one method per tool
  │
  ├─ return_partners/orchestrator.py::ReturnPartnerOrchestrator
  │     ← IDENTITY GATE B, retry policy, escalation, response shaping
  │     ├─ return_partners/identity.py     averify_order_identity
  │     ├─ return_partners/rules.py        avalidate_return_exchange_request  (non-Return-Prime only)
  │     ├─ return_partners/refunds.py      aget_refund_visibility
  │     ├─ return_partners/shipments.py    aenrich_return_pickup_leg
  │     ├─ return_partners/stock.py        acheck_shopify_variant_stock
  │     └─ return_partners/instructions.py aget_return_exchange_request_instructions
  │
  ├─ return_partners/router.py             ReturnPartnerRouter.aresolve_partner
  │     → registry.py → return_prime/service.py::ReturnPrimePartnerService
  │
  ├─ return_prime/workflow/service.py      normalization, Postgres-first lookup, Redis cache
  │     ├─ return_prime/workflow/rules.py  avalidate_return_prime_rules
  │     └─ return_prime/shopify_lookup.py  fetch_order_by_name
  │
  └─ return_prime/adapter/client.py        HTTP transport to admin.returnprime.com
```

The other 7 tools (`get_customers_delivered_orders_by_phone`, `get_recent_orders`,
`get_order_details`, `get_final_return_exchange_message`, `get_nearest_store`,
`escalate_to_agent`, `get_contact_information`) live in `tool_factory.py` and go
through the Shopify order service and `EscalationOrchestrator` instead.

### External systems touched

| System | How | Where |
|---|---|---|
| Return Prime REST | `GET {base}/return-exchange/v2` (list), `GET .../v2/{id}` (by id), 20s timeout, `x-rp-token` header | `return_prime/adapter/client.py:228,260` |
| Return Prime portal | link built, not called: `https://admin.returnprime.com/external/fetch-order?order_number=&email=&store=&channel_id=` | `adapter/client.py:38,19-21` |
| Postgres | `return_prime_webhook_events` (primary read path), `escalations` (writes) | `workflow/service.py:470,838`; `utils/escalation_logger.py` |
| Redis | `return_prime:list_requests:{client_id}:{json filters}`, TTL `RETURN_PRIME_LIST_CACHE_TTL_SECONDS` (default 600s), through `aget_with_tiered_cache` | `workflow/service.py:588-650` |
| Shopify Admin REST | order lookup / notes / order clone | `core/factory.py::ServiceFactory.aget_order_service` |
| Shopify Admin GraphQL | `ExchangeVariantStock` variant inventory query | `return_partners/stock.py:75-103` |
| Logistics partners | Delhivery / Shiprocket AWB tracking via `LogisticsRouter.aget_tracking_first_valid` | `return_partners/shipments.py:222` |
| Client config | `aget_json_config` / `aget_config` (3-tier: memory → Redis → Postgres `client_configs`) | throughout |

### Config keys read by this toolset

| Key | Read by |
|---|---|
| `return_exchange_rules` | Gate B toggle (`identity.require_customer_identity`), refund SLA/destination |
| `return_prime_return_exchange_rules` | Return Prime eligibility rules, `customer_instructions` |
| `return_prime_details` | `x_rp_token`, `api_base`, `store`, `channel_id`, `portal_url` |
| `return_partner_details` | `primary_return_partner`, `return_partners[]` priority list |
| `return_exchange_automation` | `auto_create_exchange_order`, `exchange_creation_policy` |
| `return_exchange_policy` | free-text refund Q&A fallback |
| `after_delivery_return_exchange` | final return/exchange message + grace days |
| `vendor_contact_details` | support contact |
| `order_display_limit` | default order list size |

---

## The two identity gates

This is the structural finding. Two independent gates guard order data, and they
do not agree on what counts as an identity.

### Gate A — order tools (correct)

`tool_factory.py:2364::_avalidate_phone_for_order_access`, and
`core/orchestrator.py:1009::ReturnExchangeOrchestrator.aget_customers_delivered_orders`.

```python
# tool_factory.py:2394-2402
from fashion_bot.utils.phone_number_utils import is_real_phone_number
customer_phone = phone_number or current_state.get("phone_number", "")
if not customer_phone or not is_real_phone_number(customer_phone):   # ← SHAPE GUARD
    return {
        "valid": False,
        "message": "Customer phone number not available. Please ask customer for their phone number.",
        "needs_phone": True,
        "should_block": True,
        "order_data": None,
    }
```

`is_real_phone_number` (`utils/phone_number_utils.py:78`) exists precisely for
this and explicitly names the failure mode:

```python
def is_real_phone_number(phone: str) -> bool:
    """Web chat sessions use 'web_<UUID>' or 'fbw_<ID>' as a fallback phone when
    the user skips the phone prompt. These must not be treated as real phone numbers."""
    if not phone:
        return False
    if phone.startswith(("web_", "fbw_")):
        return False
    clean = re.sub(r'\D', '', phone)
    return len(clean) >= 10
```

So a webchat session id lands in the **"no identity"** branch → the bot asks for
a phone number. Correct.

### Gate B — Return Prime tools (broken)

`return_partners/identity.py:99::averify_order_identity`. Same
`arg or state["phone_number"]` fallback, **no shape guard**:

```python
# identity.py:111-112
provided_phone = customer_phone or _state_phone(state)
provided_email = customer_email or _state_email(state)
```

`_state_phone` (`identity.py:33`) reads `state["phone_number"]` first — which on
webchat holds the widget session id (`fbw_msoa5322pp8k876dt`). It is then
normalised by digit-stripping:

```python
# identity.py:17-19
def _normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits
```

`"fbw_msoa5322pp8k876dt"` → `"53228876"` — **8 digits, but truthy**. The
"no identity supplied" branch is guarded on emptiness, not validity:

```python
# identity.py:180
if not normalized_phone and not normalized_email:
    ... needs_identity=True ...      # ← never reached for a session id
```

so control falls through to the mismatch branch (`identity.py:193-209`) and the
tool reports *"The details shared do not match this order. The order phone ends
in 5447."* to a visitor who shared nothing.

**Net effect**: on anonymous webchat, Gate A says "please share your phone" and
Gate B says "the details you shared are wrong" — for the same conversation, on
the same turn.

### Gate B can be turned off entirely

`_identity_required` (`return_partners/orchestrator.py:110`) reads
`return_exchange_rules.identity.require_customer_identity`, defaulting to `True`.
When set false, `averify_order_identity` returns `verified=True, matched_on="bypass"`
without any check (`identity.py:141-148`).

### Gate B's partner-identity fallback

Only `aget_return_status` and `alist_return_requests` have a second chance. When
the *Shopify* order lookup fails (`failed_reason` in `{"order_not_found",
"order_lookup_failed"}`) and some contact was resolved, they call Return Prime
directly with that contact; a successful partner hit is then marked
`identity_verified: True, matched_on: "return_partner_customer_contact"`
(`orchestrator.py:269-297`, `orchestrator.py:377-412`). A genuine
`identity_mismatch` never reaches this path.

---

## Tool reference

Conventions below:

- **Reads from** lists external systems in call order.
- Response objects are the literal dicts the code returns; `…` marks a nested
  structure documented elsewhere in the file.
- Every tool is `async`, every one returns a `dict` except
  `get_final_return_exchange_message` (returns `str`).

---

### 1. `get_customers_delivered_orders_by_phone(phone_number: str, limit: int = 3) -> dict`
`tool_factory.py:3483`

**Description** (verbatim docstring):

> Fetch the most recent DELIVERED orders for the customer by phone number.
> Returns orders with DELIVERED status that are eligible for return/exchange.
>
> Use this tool FIRST before asking for order ID to show customer their recent delivered orders.
>
> If only 1 delivered order is found, ask customer to confirm that order. If multiple delivered
> orders are found, ask customer which order they want to return/exchange. If no delivered orders
> are found, call get_recent_orders (include_all_statuses=True) next — the customer may have an
> order that hasn't been delivered yet. Only ask for an order ID if that also comes back empty, and
> never guess one from a number in their message.
>
> IMPORTANT: order_placed_date and delivered_date are different dates — never say "delivered on
> {order_placed_date}". order_placed_date is when the order was placed/checked out. delivered_date is
> when courier tracking confirms the order actually arrived (may be "unavailable" if no tracking data
> exists yet — in that case do not state a delivery date to the customer at all).

**Validation** (ordered):

1. `if not phone_number` → `{"success": False, "message": "Phone number is required", "orders": []}`
2. **Gate A shape guard** — `is_real_phone_number(phone_number)` false →
   `{"success": False, "invalid_phone": True, "message": "That doesn't look like a complete 10-digit mobile number. Ask the customer to re-enter their 10-digit phone number, or provide their Order ID.", "total_orders": 0, "orders": []}`
3. `ReturnExchangeOrchestrator.aget_customers_delivered_orders` re-applies the
   same guard (`orchestrator.py:1018`) and prefixes its refusals `INVALID_PHONE:`
   / `NO_ORDERS:` / `NO_DELIVERED_ORDERS:` / `ERROR:`.

**Reads from**: Shopify `aget_orders_by_customer_phone` (limit 10) → `filter_delivered_orders`;
per order `_adelivery_datetime` (`return_partners/rules.py:246`) for the real delivery date;
config `order_display_limit` (only when `limit == 3`, i.e. the default is a
sentinel — an explicit `limit=3` from the model is indistinguishable from no
argument and gets overridden by config).

**Pseudo-code**

```
if not phone_number:                 → error "Phone number is required"
if not is_real_phone_number(phone):  → invalid_phone error
if limit == 3: limit = int(aget_config("order_display_limit", default=3))
result = ReturnExchangeOrchestrator.aget_customers_delivered_orders(phone, state, limit)
     └─ Shopify aget_orders_by_customer_phone(phone, limit=10) → filter_delivered_orders
if not success or count == 0:        → {"success": False, message: result["response"]}
for order in result["delivered_orders"]:
    order_placed_date = fmt(order["created_at"])            # placement, NOT delivery
    delivered_date    = fmt(_adelivery_datetime(order)) or "unavailable"
    items = titles of first 3 line_items with current_quantity > 0 (+"+N more")
return {success: True, total_orders, orders: [...], showing_top}
```

**Response object — success**

```json
{
  "success": true,
  "message": "Found delivered orders",
  "total_orders": 2,
  "showing_top": 2,
  "orders": [
    {
      "order_id": "#gv17083",
      "order_placed_date": "14 Jul 2026",
      "delivered_date": "21 Jul 2026",
      "status": "delivered",
      "total_price": "1499.00",
      "currency": "INR",
      "items": ["Cotton Kurta - M", "Silk Scarf", "+2 more"],
      "financial_status": "paid"
    }
  ]
}
```

**Failure branches**

```json
{"success": false, "message": "Phone number is required", "orders": []}
```
```json
{"success": false, "invalid_phone": true, "total_orders": 0, "orders": [],
 "message": "That doesn't look like a complete 10-digit mobile number. Ask the customer to re-enter their 10-digit phone number, or provide their Order ID."}
```
```json
{"success": false, "total_orders": 0, "orders": [],
 "message": "NO_DELIVERED_ORDERS: No delivered orders found. Only delivered orders are eligible for return/exchange. The customer may still have orders in progress — call get_recent_orders with include_all_statuses=True to see them before asking anything. Never guess an order ID from a number in the customer's message."}
```
```json
{"success": false, "message": "<exception text>", "orders": []}
```

---

### 2. `get_recent_orders(phone_number: str = "", limit: int = 3, include_all_statuses: bool = False, include_eta: bool = False) -> dict`
`tool_factory.py:2934` (built by `_create_get_recent_orders_tool(state)`, `tool_factory.py:2912`)

Created with `enrich_eta=False` for this agent, so **`include_eta` is inert here** —
the ETA branch is hard-gated by the factory argument, which only
`order_status_tools_factory` passes as `True`.

**Description** (verbatim docstring):

> Get the customer's most recent orders.
>
> By default, returns only actionable orders (excludes delivered, cancelled, RTO) — ideal for
> update / cancel workflows. Set include_all_statuses=True when the customer asks for their full
> order history (e.g. "where is my order?", "show my orders").
>
> Set include_eta=True ONLY when the customer is asking WHEN their order will arrive — i.e. a
> delivery-timing question in any language or phrasing ("when will I get it?", "how many days?",
> "expected delivery date?", "kab aayega?", "how long does shipping take?"). You are the judge of
> that intent from the customer's message — do NOT set it for a plain status check or a "track my
> order" / "where is my order" request. When True, each dispatched order's `expected_delivery` (ETA)
> is fetched from the courier; it costs an extra call, so leave it False otherwise.
>
> IMPORTANT: The `order_id` field in each order is the UNIQUE IDENTIFIER. Always use this value when
> calling other tools (get_order_details, update_order_address, cancel_order_tool, etc.).
>
> [Args and Returns as documented in the source; fulfillment_status='fulfilled' means SHIPPED, not
> delivered. Check shipment_status for actual delivery status.]

**Validation**

1. `phone = phone_number.strip() or state.get("phone_number", "")` — **Gate A fallback**
2. `if not phone or not is_real_phone_number(phone)` → blocked with `needs_phone: True`

**Reads from**: Shopify order service (orders by customer phone); config `order_display_limit`
(same `limit == 3` sentinel); carrier tracking only when `enrich_eta` **and** `include_eta`.

**Pseudo-code**

```
phone = phone_number.strip() or state["phone_number"]
if not is_real_phone_number(phone):  → needs_phone block
if limit == 3: limit = config order_display_limit
orders = Shopify orders for phone
if not include_all_statuses: drop delivered / cancelled / RTO
if enrich_eta and include_eta: for each dispatched order, fetch courier ETA
return {success, total_orders, orders: [ ... rich line items ... ]}
```

**Response object — success**

```json
{
  "success": true,
  "total_orders": 1,
  "orders": [
    {
      "order_id": "#gv17083",
      "created_at": "2026-07-14T10:22:31Z",
      "formatted_date": "14 Jul 2026",
      "status": "in_transit",
      "shipment_status": "in_transit",
      "tracking_url": "https://www.delhivery.com/track-v2/package/1234567890",
      "courier": "Delhivery",
      "awb": "1234567890",
      "financial_status": "paid",
      "fulfillment_status": "fulfilled",
      "total_price": "1499.00",
      "currency": "INR",
      "expected_delivery": "",
      "line_items": [
        {"title": "Cotton Kurta", "variant_title": "M", "quantity": 1,
         "price": "1499.00", "sku": "CK-M", "product_id": "8123…", "variant_id": "4456…"}
      ]
    }
  ]
}
```

**Failure branch — no usable phone (the `fbw_…` case)**

```json
{
  "success": false,
  "needs_phone": true,
  "orders": [],
  "message": "No phone number on file for this customer. Ask the customer for their phone number or order ID so you can look up their order."
}
```

---

### 3. `get_return_status_by_order_number(order_number: str, request_type: str = "", customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:18` → `ReturnPartnerOrchestrator.aget_return_status` (`return_partners/orchestrator.py:231`)

**Description** (verbatim docstring):

> Use this when the customer asks for return/exchange status for an order.
>
> Examples: "what is my return status", "where is my exchange order", "is my return under process",
> "status for return on order #1234".

Note the schema the LLM sees marks only `order_number` required; the three
identity/type arguments default to `""` and are therefore routinely omitted.

**Validation** (ordered)

1. `cid = client_id or state["client_id"]` — empty → `{"success": False, "message": "Missing client_id for return lookup."}`
2. **Gate B** — `_verify_identity` → `averify_order_identity`:
   - Shopify order fetch fails → `failed_reason: "order_lookup_failed"`, `should_block: True`
   - order not found → `failed_reason: "order_not_found"`
   - `require_customer_identity == false` → bypass, verified
   - email match against `{order.email, order.customer_email, order.customer.email}` → verified
   - phone match (last-10, via `validate_phone_number_access`) against order phone, customer phone,
     billing phone, shipping/billing address phones, `customer.default_address.phone` → verified
   - **neither supplied** → `needs_identity: True`
   - **otherwise** → `identity_mismatch` + `"The details shared do not match this order. The order phone ends in {last4}."`
   - ⚠️ **no `is_real_phone_number` guard anywhere in this chain** — see [Defect 1](#1-gate-b-has-no-phone-shape-guard)
3. Partner resolution — no configured partner → `{"success": False, "message": "Return/exchange partner is not configured for this client.", "partner": null}`
4. On block: partner-identity fallback only for `order_not_found` / `order_lookup_failed`
5. Partner call with 2 attempts, retrying only on status `{0, 408, 425, 429, 500, 502, 503, 504}`, 0.2s backoff

**Reads from**: Shopify order (identity) → config `return_exchange_rules`,
`return_partner_details` → Postgres `return_prime_webhook_events` (primary) →
Return Prime `GET /return-exchange/v2/{id}` per row for live data → Redis
`return_prime:list_requests:*` / Return Prime list API (fallback when no webhook rows).

**Pseudo-code**

```
cid = client_id or state.client_id                                  → else missing-client error
identity = averify_order_identity(cid, order_number, state, phone, email)
     └─ ServiceFactory.aget_order_service("shopify").aget_order_details(order_number)
resolved_phone, resolved_email = customer_phone|state.*, customer_email|state.*
partner, service = ReturnPartnerRouter.aresolve_partner(cid, state)  → else not-configured error
if identity.should_block:
    if failed_reason in {order_not_found, order_lookup_failed} and (resolved_phone or resolved_email):
        r = service.get_status_by_order_number(...)   # partner-identity fallback
        if r.success: mark identity_verified=True, matched_on="return_partner_customer_contact"; return r
    if failed_reason in {order_not_found, order_lookup_failed}: return needs_identity block
    return {success: True, identity_verified: False, needs_identity, message, identity}   # ← leaks `identity.order`
result = _call_with_retry(service.get_status_by_order_number(cid, order_number, request_type, phone, email))
     └─ ReturnPrimeWorkflowService.get_status_by_order_number
          └─ list_requests_by_order_number
               ├─ Postgres return_prime_webhook_events (latest 10, filtered)
               │    └─ per row: Return Prime GET /return-exchange/v2/{request_id}, else DB row
               └─ if no rows: tiered cache (memory → Redis → Return Prime GET /return-exchange/v2)
if result.success: return result
escalation_id = _raise_system_escalation("Return Partner Failure", ...)
return {**result, success: False, message: support_message, escalation_id, partner}
```

**Response object — success, single request**

```json
{
  "success": true,
  "status_code": 200,
  "identity_verified": true,
  "partner": "return_prime",
  "order_name": "#gv17083",
  "source": "return_prime_api",
  "message": "Return Prime request RET777 is approved.",
  "request": {
    "request_id": "6712ab…",
    "request_number": "RET777",
    "request_type": "return",
    "status": "approved",
    "order_id": "5567…",
    "order_name": "#gv17083",
    "customer_name": "…",
    "customer_email": "…",
    "customer_phone": "…",
    "status_checkpoints": {"approved": true, "received": false, "inspected": false,
                           "rejected": false, "archived": false},
    "line_items": [
      {"original_product": "Cotton Kurta", "original_variant": "M",
       "exchange_product": null, "exchange_variant": null, "quantity": 1,
       "reason": "Size issue", "refund": {"status": "pending", "requested_mode": "wallet"},
       "return_fee": null, "exchange_fee": null, "shipping": {…},
       "awb": "1234567890", "tracking_url": "…", "shipping_company": "Delhivery",
       "shipment_status": "picked up", "exchange_order": {}}
    ],
    "refund": {"status": "pending", "requested_mode": "wallet"},
    "return_fee": null, "exchange_fee": null, "shipping": {…},
    "exchange_order": null,
    "delivery": {"status": null, "date": null},
    "rejection_comment": null,
    "raw": { …full Return Prime request payload… }
  }
}
```

**Response object — multiple requests** (`workflow/service.py:792`) replaces
`request` with a stripped `requests` summary list **and** a full `requests_full`
list carrying refund/line-item/shipping data.

**Response object — no request on the order**

```json
{"success": true, "status_code": 200, "identity_verified": true, "partner": "return_prime",
 "order_name": "#gv17083", "requests": [], "source": "return_prime_webhook_events",
 "message": "<NO_REQUEST_MESSAGE from workflow/constants.py>"}
```

**Failure branch — identity mismatch (the trace `8b099dc2` shape)**

```json
{
  "success": true,
  "identity_verified": false,
  "needs_identity": false,
  "message": "The details shared do not match this order. The order phone ends in 5447.",
  "identity": {
    "success": true,
    "verified": false,
    "needs_identity": false,
    "should_block": true,
    "matched_on": "not_matched",
    "message": "The details shared do not match this order. The order phone ends in 5447.",
    "order_number": "gv17083",
    "order": { "…THE ENTIRE SHOPIFY ORDER…": "including customer.phone, email, full shipping_address" },
    "provided_phone": "53228876",
    "provided_email": null,
    "masked_order_phone": "5447",
    "order_email": "customer@example.com",
    "failed_reason": "identity_mismatch"
  }
}
```

Note `success: true` on a blocked call, `provided_phone: "53228876"` (the
digits of `fbw_msoa5322pp8k876dt`), and the unmasked `identity.order` —
[Defect 2](#2-the-full-shopify-order-is-returned-inside-the-blocked-response).

**Failure branch — identity required, nothing supplied**

```json
{"success": true, "identity_verified": false, "needs_identity": true,
 "message": "Please share the phone number or email linked to this order so I can verify it.",
 "identity": {"…": "matched_on: not_provided, failed_reason: identity_required, order: {…}"}}
```

**Failure branch — order not found / lookup failed**

```json
{"success": true, "identity_verified": false, "needs_identity": true,
 "message": "Please share the phone number or email linked to this return/exchange so I can safely look it up.",
 "identity": {"…": "failed_reason: order_not_found"}}
```

**Failure branch — no partner configured**

```json
{"success": false, "message": "Return/exchange partner is not configured for this client.", "partner": null}
```

**Failure branch — partner API failed after retries** (raises an escalation)

```json
{"success": false, "status_code": 502, "partner": "return_prime", "escalation_id": "…",
 "message": "I'm sorry, I couldn't fetch your return/exchange details right now. Please contact customer support and we'll help you with this."}
```

---

### 4. `list_return_requests_by_order_number(order_number: str, request_type: str = "", customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:42` → `ReturnPartnerOrchestrator.alist_return_requests` (`orchestrator.py:354`)

**Description** (verbatim docstring):

> List all return/exchange requests for an order.
>
> Use this when the customer may have multiple return/exchange requests on one order or asks which
> items/requests exist.

**Validation**: identical Gate B chain to tool 3, with the same partner-identity
fallback. One ordering difference: the partner is resolved *after* the identity
check here (`orchestrator.py:382`), *before* it in tool 3.

**This is the tool that disclosed `RET777` in trace `8b099dc2`.** The mechanism is
now confirmed: tool 3's blocked response carried the order's real phone inside
`identity.order`; the model read it and re-called this tool with
`customer_phone="9511785447"`; that value matches the order, Gate B returns
`verified`, and the full request list is disclosed. The gate is working exactly
as written — the leak is upstream, in what the *blocked* response handed the model.

**Reads from**: same as tool 3.

**Pseudo-code**

```
cid = client_id or state.client_id                       → else missing-client error
identity = averify_order_identity(...)
resolved_phone, resolved_email = args or state fallbacks
if identity.should_block:
    if failed_reason in {order_not_found, order_lookup_failed}:
        if resolved_phone or resolved_email:
            partner, service = resolve()
            r = service.list_requests_by_order_number(...)
            if r.success: mark verified; return r
        return needs_identity block
    return {success: True, identity_verified: False, needs_identity, message, identity}
partner, service = resolve()                             → else not-configured error
result = _call_with_retry(service.list_requests_by_order_number(...))
result.setdefault("partner", partner); result.setdefault("identity_verified", True)
return result        # NOTE: no escalation on failure here, unlike tool 3
```

**Response object — success**

```json
{
  "success": true,
  "status_code": 200,
  "identity_verified": true,
  "partner": "return_prime",
  "order_name": "#gv17083",
  "customer_email": "customer@example.com",
  "customer_phone": "9511785447",
  "shopify_lookup_used": false,
  "source": "return_prime_api",
  "requests": [ { "…normalized request as in tool 3…": "" } ]
}
```

`source` is one of `return_prime_api`, `return_prime_webhook_events`,
`return_prime_list_api`, `return_prime_list_cache_redis`,
`return_prime_list_cache_memory` (`workflow/service.py:709,750-754`).

**Failure branches**: the three identity blocks are byte-identical to tool 3.
Partner failure is **not** escalated and returns the sanitized adapter failure:

```json
{"success": false, "status_code": 502, "order_name": "#gv17083",
 "message": "Unable to fetch Return Prime requests right now. Please try again later."}
```

`_sanitize_adapter_failure` (`workflow/service.py:56`) floors the status code at
502 so a Return Prime 401/404 never surfaces as a 4xx.

---

### 5. `get_return_request_by_id(request_id: str) -> dict`
`return_partners/tools.py:66` → `ReturnPartnerOrchestrator.aget_return_request_by_id` (`orchestrator.py:588`)

**Description** (verbatim docstring):

> Fetch exact return/exchange request details when a request id or request number is already known.

**Validation** — this tool **does** have an identity gate, but it is *post-hoc*
and *weaker* than the others:

1. `cid` present
2. Partner resolved
3. **Partner called first** — `service.get_request_by_id(cid, request_id)` runs
   before any identity check
4. Only if the call succeeded **and** the returned request carries an
   `order_name`/`order_number` is `_verify_identity` invoked — and it is called
   **without `customer_phone` / `customer_email`** (`orchestrator.py:618-622`),
   so it can only ever use the state fallback. A phone the customer typed this
   turn is not consulted.
5. If the partner response has no order name, **no identity check runs at all**
   and the request is returned in full.

**Reads from**: Return Prime `GET /return-exchange/v2/{request_id}` → on failure,
Postgres `return_prime_webhook_events WHERE request_id = %s OR return_request_id = %s`
(latest 1) → Shopify order (identity, only if an order name was found).

**Pseudo-code**

```
cid = client_id or state.client_id                        → else missing-client error
partner, service = resolve()                              → else not-configured error
result = _call_with_retry(service.get_request_by_id(cid, request_id))
     └─ Return Prime GET /return-exchange/v2/{id}
     └─ on failure: Postgres return_prime_webhook_events latest row → normalize
order_name = result.request.order_name or result.request.order_number
if result.success and order_name:
    identity = averify_order_identity(cid, order_name, state)      # ← NO phone/email args
    if identity.should_block: return identity block
    result.setdefault("identity_verified", True)
return result        # ← if order_name is absent, returned unguarded
```

**Response object — success**

```json
{
  "success": true, "status_code": 200, "partner": "return_prime",
  "identity_verified": true, "source": "return_prime_api",
  "request": { "…normalized request…": "" },
  "raw": { "…raw adapter payload…": "" }
}
```

**Response object — Return Prime down, DB fallback used**

```json
{"success": true, "status_code": 200, "partner": "return_prime", "identity_verified": true,
 "source": "return_prime_webhook_events",
 "message": "Return Prime API failed, so latest webhook data was used.",
 "request": {"…": ""}}
```

**Failure branches**

```json
{"success": false, "message": "Missing client_id for return lookup."}
```
```json
{"success": false, "message": "Return/exchange partner is not configured for this client.", "partner": null}
```
```json
{"success": false, "status_code": 502, "request_id": "6712ab…", "partner": "return_prime",
 "message": "Unable to fetch Return Prime request details right now. Please try again later."}
```
```json
{"success": true, "identity_verified": false, "needs_identity": false, "partner": "return_prime",
 "message": "The details shared do not match this order. The order phone ends in 5447.",
 "identity": {"…": "with full `order`"}}
```

---

### 6. `get_return_or_exchange_portal_link(order_number: str, customer_email: str = "", customer_phone: str = "", request_type: str = "", selected_line_items: list[dict] | None = None, return_reason: str = "", proof_provided: bool | None = None, tag_intact_confirmed: bool | None = None, desired_resolution: str = "") -> dict`
`return_partners/tools.py:80` → `ReturnPartnerOrchestrator.aget_return_or_exchange_portal` (`orchestrator.py:454`)

**Description** (verbatim docstring):

> Use this when the customer wants to start a new return or exchange request for an order.

Nine parameters, eight optional, and the docstring documents none of them —
which is why the model guesses at `selected_line_items` (see
[Defect 6](#6-argument-type-failures-reach-production)).

**Validation** (ordered)

1. `cid` present
2. **Gate B** — full identity check; on block returns `eligible: False` *and*
   `success: True`
3. Partner resolved
4. **Rules validation is partner-dependent** (`orchestrator.py:507`):
   - `partner_name != "return_prime"` → `avalidate_return_exchange_rules`
     (`return_partners/rules.py:349`) runs in the orchestrator
   - `partner_name == "return_prime"` → **skipped here**; instead
     `ReturnPrimePartnerService.get_portal_link` runs
     `avalidate_return_prime_rules` (`return_prime/workflow/rules.py:108`) against
     the `return_prime_return_exchange_rules` config
5. Inside `get_portal_link` (`return_prime/service.py:52`), **before** rules:
   an existing-request check. Any existing request short-circuits with
   `eligible: False, already_exists: True`
6. Return Prime rule checks, in order: `{type}_disabled`, `multiple_item_returns_disabled`
   (or `item_selection_required` when the model passed no selection),
   `order_not_delivered`, `window_disabled` / `delivery_date_missing` / `outside_window`,
   `blocked_discount_code`, `blocked_order_created_between`, `blocked_product_tag`
7. Link construction — needs a resolved email plus `store` and `channel_id`;
   falls back to a generic `portal_url` when either is missing

**Reads from**: Shopify order (identity, rules, delivery date) → config
`return_exchange_rules`, `return_prime_return_exchange_rules`, `return_prime_details`,
Shopify config → Postgres/Return Prime existing-request lookup → Shopify order
lookup for the customer email when not supplied (`shopify_lookup.fetch_order_by_name`)
→ Shopify product tags when `blocked_product_tags` is configured.

**Pseudo-code**

```
cid = client_id or state.client_id                          → else missing-client error
identity = averify_order_identity(...)
if identity.should_block: return {success: True, eligible: False, identity_verified: False, …, identity}
partner, service = resolve()                                → else not-configured error
if partner != "return_prime":
    validation = avalidate_return_exchange_rules(...)
    if not validation.valid: return {success: True, eligible: False, validation, message}
result = _call_with_retry(service.get_portal_link(cid, order_number, email, phone, request_type,
                                                  state, order=identity.order, selected_line_items,
                                                  return_reason, desired_resolution))
  └─ ReturnPrimePartnerService.get_portal_link:
       existing = workflow.list_requests_by_order_number(...)
       if not existing.success:  → existing_request_check_failed
       if existing.requests:     → _existing_request_response (already_exists)
       validation = avalidate_return_prime_rules(cid, order_number, request_type, state, order, selected_line_items)
       if not validation.valid:  → {success: True, eligible: False, validation, message}
       link = workflow.get_return_portal_link(cid, order_number, customer_email)
            ├─ resolve store / channel_id / portal_url from return_prime_details + shopify config
            ├─ resolve email: arg, else Shopify order lookup by name
            ├─ if no email  and portal_url → generic link;  else 400
            ├─ if store/channel_id missing and portal_url → generic link;  else 400
            └─ else adapter.build_return_portal_link(order, email, store, channel_id)   # deep link
       return {**link, validation}
return result (+ partner, identity_verified defaults)
```

**Response object — deep link generated**

```json
{
  "success": true, "status_code": 200, "partner": "return_prime", "identity_verified": true,
  "order_name": "#gv17083",
  "customer_email": "customer@example.com",
  "portal_url": "https://admin.returnprime.com/external/fetch-order?order_number=%23gv17083&email=customer%40example.com&store=groovee.myshopify.com&channel_id=123",
  "portal_mode": "deep_link",
  "shopify_lookup_used": true,
  "message": "Return Prime portal link generated. Share this link with the customer to raise a return/exchange request.",
  "validation": {"success": true, "valid": true, "validation_applied": true,
                 "source": "return_prime_return_exchange_rules", "request_type": "return",
                 "message": "Order is eligible for Return Prime return.",
                 "details": {"order_name": "#gv17083", "delivered": true, "window_days": 7,
                             "delivered_at": "2026-07-21T09:00:00+00:00", "days_since_delivery": 3,
                             "selected_item_count": 1}}
}
```

**Response object — generic link (store/channel_id not configured)**

```json
{"success": true, "status_code": 200, "portal_url": "https://groovee.in/returns", "portal_mode": "generic",
 "missing_deep_link_fields": ["store", "channel_id"], "order_name": "#gv17083",
 "customer_email": "customer@example.com", "shopify_lookup_used": false,
 "message": "Return Prime portal link available. Share this link with the customer to raise a return/exchange request."}
```

**Failure branch — a request already exists (single)**

```json
{"success": true, "eligible": false, "already_exists": true, "portal_url": null,
 "order_name": "#gv17083", "source": "return_prime_api", "request": {"…": ""},
 "message": "A return request already exists for order #gv17083: RET777 is approved. Please use the existing request status instead of creating a new one."}
```

**Failure branch — a request already exists (multiple)**

```json
{"success": true, "eligible": false, "already_exists": true, "portal_url": null,
 "order_name": "#gv17083", "source": "return_prime_api",
 "requests": [{"request_id": "…", "request_number": "RET777", "request_type": "return", "status": "approved"}],
 "message": "Multiple return/exchange requests already exist for order #gv17083. Please use the existing request status instead of creating a new one."}
```

**Failure branch — rule failure** (`message` picked by `_failure_message`, `workflow/rules.py:291`)

```json
{"success": true, "eligible": false,
 "message": "This order is outside the 7-day return window, so it is not eligible for return.",
 "validation": {"success": true, "valid": false, "validation_applied": true,
                "source": "return_prime_return_exchange_rules", "request_type": "return",
                "failed_rules": ["outside_window"],
                "details": {"window_days": 7, "days_since_delivery": 41, "delivered": true}}}
```

Other `failed_rules` values and their messages: `return_disabled`/`exchange_disabled`/`window_disabled`
→ *"Return requests are disabled by policy."*; `order_not_delivered` → *"This order is not eligible for
return yet because it is not marked delivered."*; `delivery_date_missing` → *"I couldn't verify the
delivery date for this order, so I can't start the return right now."*; `blocked_product_tag` →
*"…matches a restricted Return Prime product tag: {tags}."*; `blocked_discount_code` →
*"…discount code {codes} is restricted by policy."*; `blocked_order_created_between` →
*"…its order date falls in a restricted policy window."*; `multiple_item_returns_disabled` →
*"Multiple-item returns are disabled by policy. Please select one item."*

**Failure branch — item selection needed**

```json
{"success": true, "eligible": false,
 "validation": {"success": true, "valid": false, "needs_customer_input": true,
                "needs_input": ["item_selection_required"], "failed_rules": [],
                "message": "Please confirm which item you want to return/exchange."},
 "message": "Please confirm which item you want to return/exchange."}
```

**Failure branch — existing-request check failed**

```json
{"success": false, "eligible": false, "portal_url": null, "order_name": "#gv17083",
 "existing_request_check_failed": true, "details": {"…": ""},
 "message": "I couldn't verify whether this order already has a return/exchange request. Please try again in a few minutes."}
```

**Failure branch — no email and no fallback portal URL**

```json
{"success": false, "status_code": 400, "order_name": "#gv17083", "customer_email": null,
 "portal_url": null, "shopify_lookup_used": true,
 "message": "Missing customer email. Provide customer_email or ensure Shopify order lookup can resolve it."}
```

**Failure branch — identity blocked**

```json
{"success": true, "eligible": false, "identity_verified": false, "needs_identity": false,
 "message": "The details shared do not match this order. The order phone ends in 5447.",
 "identity": {"…": "with full `order`"}}
```

---

### 7. `get_return_pickup_status(order_number: str, customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:112` → `ReturnPartnerOrchestrator.aget_return_pickup_status` (`orchestrator.py:739`)

**Description** (verbatim docstring):

> Use this when the customer asks about return pickup, reverse pickup, return shipment, or
> return-to-origin status.

**Validation** — delegates entirely to `aget_return_status` (tool 3), then
checks **only** `status_result["success"]`:

```python
# orchestrator.py:756-757
if not status_result.get("success"):
    return status_result
```

Because an identity block returns `success: True`, **the block does not stop this
tool.** It proceeds with an empty request and reports "no pickup status" instead
of asking for identity — see [Defect 3](#3-identity-blocks-leak-through-get_return_pickup_status).

**Reads from**: everything tool 3 reads, plus `LogisticsRouter.aget_tracking_first_valid`
(Delhivery / Shiprocket AWB tracking) when an AWB is present.

**Pseudo-code**

```
status_result = aget_return_status(cid, order_number, state, partner, phone, email)
if not status_result.success: return status_result           # ← does NOT catch identity blocks
request = status_result.request | requests_full[0] | requests[0] | {}
pickup  = aenrich_return_pickup_leg(request, state)
     ├─ extract_return_shipping(request)  → awb, tracking_url, raw_status, carrier
     ├─ if no awb: classify from raw_status alone, return early
     ├─ LogisticsRouter.aget_tracking_first_valid(awb, order_dto, fallback_to_all=True)
     ├─ classify_pickup_status(live_status or raw_status, request.status)
     └─ build_tracking_url(awb, carrier/partner)  → delhivery.com/track-v2/… | shiprocket.co/tracking/…
return {success: True, partner, identity_verified, order_name, request, pickup, message: pickup.message}
```

**Pickup status classification** (`shipments.py:148`), in match order:
`exception|failed|cancel` → `pickup_issue`; `return to origin|rto|delivered` →
`return_to_origin`; `picked|in transit` → `picked_up`; `out for pickup` →
`out_for_pickup`; `scheduled` → `pickup_scheduled`; `requested|approved` →
`request_under_process`; empty → `unknown`; otherwise the raw normalised string.

**Response object — success**

```json
{
  "success": true, "partner": "return_prime", "identity_verified": true,
  "order_name": "#gv17083",
  "request": {"…normalized request…": ""},
  "pickup": {
    "success": true, "leg": "return_pickup", "status": "picked_up",
    "message": "Your return pickup is completed and the item is in transit. Tracking ID: 1234567890. Tracking link: https://www.delhivery.com/track-v2/package/1234567890",
    "awb": "1234567890",
    "tracking_url": "https://www.delhivery.com/track-v2/package/1234567890",
    "carrier": "Delhivery", "partner": "delhivery", "raw_status": "In Transit",
    "logistics_result": {"…": ""}, "per_partner": {"…": ""}
  },
  "message": "Your return pickup is completed and the item is in transit. Tracking ID: 1234567890. Tracking link: https://www.delhivery.com/track-v2/package/1234567890"
}
```

**Response object — identity was blocked (the defect)**

```json
{"success": true, "partner": "return_prime", "identity_verified": false, "order_name": null,
 "request": {},
 "pickup": {"success": true, "leg": "return_pickup", "status": "unknown",
            "message": "I could not find a pickup status for this return yet.",
            "awb": null, "tracking_url": null, "carrier": null},
 "message": "I could not find a pickup status for this return yet."}
```

The identity message is silently discarded and replaced with a "no data" message.

**Failure branch — upstream hard failure** returns tool 3's failure dict verbatim.

---

### 8. `get_refund_status_by_order_number(order_number: str, request_type: str = "return", customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:132` → `ReturnPartnerOrchestrator.aget_refund_status` (`orchestrator.py:774`)

**Description** (verbatim docstring):

> Use this when the customer asks whether refund has started, where the refund is, when money will
> arrive, refund SLA, wallet credit, or refund escalation for a return/exchange.

**Validation** — correctly guards on both flags (`orchestrator.py:796`):

```python
if not status_result.get("success") or status_result.get("identity_verified") is False:
    return status_result
```

**Reads from**: everything tool 3 reads, plus config `return_exchange_rules.refund`
(and per-type override) and, when no structured refund config exists, the free-text
`return_exchange_policy` Q&A; writes an escalation row when the SLA is breached.

**Pseudo-code**

```
cid required
status_result = aget_return_status(cid, order_number, request_type or "return", state, partner, phone, email)
if not success or identity_verified is False: return status_result      # ← correct guard
request = _resolve_request_from_status_result(status_result)
refund  = aget_refund_visibility(cid, request, request_type)
     ├─ refund_cfg = {**rules.refund, **rules[type].refund}
     ├─ visibility_mode = refund_cfg.visibility_mode | mode | "none"
     ├─ if mode == "return_partner" and partner reports a refund → partner_reported result, return
     ├─ if no refund_cfg at all → scan return_exchange_policy Q&A for a "refund … when|money" answer
     ├─ anchor = refund_initiated_at | refunded_at | approved_at | received_at | updated_at | created_at
     ├─ sla_days = wallet_sla_days | source_sla_business_days | sla_business_days | policy days | 7
     └─ evaluate_sla(anchor, sla_days, business_days=True)  → within_sla | due_soon | breached | unknown
if refund.should_escalate: escalation_id = _raise_system_escalation("Return Refund SLA", …)
return {success: True, partner, identity_verified: True, order_name, request, refund, escalation_id, message}
```

**Response object — policy-based (no live confirmation)**

```json
{
  "success": true, "partner": "return_prime", "identity_verified": true,
  "order_name": "#gv17083",
  "request": {"…": ""},
  "refund": {
    "success": true, "status": "unknown_with_sla", "confidence": "policy_based",
    "source": "none", "destination": "wallet", "amount": null, "currency": null,
    "anchor_at": "2026-07-25T11:04:00+00:00", "sla_due_at": "2026-08-03T11:04:00+00:00",
    "sla_status": "within_sla", "should_escalate": false,
    "message": "I do not have live bank/gateway refund confirmation yet. Based on the configured policy, refunds to wallet usually take up to 7 business days after approval/QC.",
    "raw": {"visibility_mode": "none", "refund": {"…": ""}, "partner_destination": "wallet"}
  },
  "escalation_id": null,
  "message": "I do not have live bank/gateway refund confirmation yet. Based on the configured policy, refunds to wallet usually take up to 7 business days after approval/QC."
}
```

**Response object — partner-reported** (`visibility_mode == "return_partner"`)

```json
{"refund": {"status": "processed", "confidence": "partner_reported", "source": "return_partner",
            "destination": "wallet", "amount": "1499.00", "currency": "INR",
            "message": "The return partner reports that your refund is processed.",
            "raw": {"refund": {"…": ""}, "partner_destination": "wallet"}},
 "…": "rest as above"}
```

**Response object — SLA breached** (escalation raised)

```json
{"refund": {"status": "sla_breached", "sla_status": "breached", "should_escalate": true,
            "message": "I do not have live refund confirmation yet, and this appears past the configured 7-business-day refund window. I will escalate this to support."},
 "escalation_id": "3f2c…",
 "…": ""}
```

**Failure branches**: `{"success": false, "message": "Missing client_id for refund lookup."}`,
or tool 3's failure/identity dicts returned verbatim (so an identity block from
here still carries the full `identity.order`).

---

### 9. `ensure_exchange_order_created(order_number: str, force: bool = False, customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:155` → `ReturnPartnerOrchestrator.aensure_exchange_order` (`orchestrator.py:857`)

**Description** (verbatim docstring):

> Use this when an exchange customer asks when the exchange order will be created or when configured
> policy says exchange order should be created.
>
> By default this will not create anything unless the client's return_exchange_automation config
> enables auto_create_exchange_order and the pickup/RTO status satisfies the policy. Use force only
> for internal testing or explicitly approved operational flows.

**⚠️ This is the only write-capable data tool in the set** — it can clone a
Shopify order. `force=True` is exposed to the LLM and bypasses the
`auto_create_exchange_order` config flag (`orchestrator.py:925`:
`auto_enabled = bool(policy_config.get("auto_create_exchange_order")) or force`).
The policy-vs-pickup-status check still applies.

**Validation** (ordered)

1. `cid` present
2. Delegates to `aget_return_pickup_status` (tool 7) and checks **only `success`** —
   so it inherits tool 7's identity leak
3. `request.request_type != "exchange"` → refused
4. `avalidate_return_exchange_rules` — note this passes `order=request` (a
   *Return Prime request*, not a Shopify order), so order-shaped rules degrade
5. Existing exchange order → refused as `already_exists`
6. `auto_create_exchange_order` (or `force`)
7. `_can_create_exchange_for_status(policy, pickup_status)`:
   `on_pickup` → `{picked_up, return_to_origin}`; `on_rto` → `{return_to_origin}`;
   `existing_customer_immediate` → always; anything else → never
8. Exchange variant data must exist on the request

**Reads from**: everything tool 7 reads → config `return_exchange_automation`,
`return_exchange_rules` → **writes**: Shopify order clone (`aclone_order`),
Shopify order note (`aadd_order_note`), Postgres escalation rows.

**Pseudo-code**

```
cid required
pickup_result = aget_return_pickup_status(...)
if not pickup_result.success: return pickup_result             # ← inherits tool 7's identity leak
request = pickup_result.request or {}
if request.request_type != "exchange":  → {success: False, exchange_created: False, "not an exchange request"}
validation = avalidate_return_exchange_rules(cid, order_number, "exchange", state, order=request)
if not validation.valid: → {success: True, exchange_created: False, eligible: False, validation}
existing = _exchange_order_from_request(request)
if existing.name or existing.id: → {success: True, exchange_created: False, already_exists: True}
policy_config = config return_exchange_automation
policy       = policy_config.exchange_creation_policy | "manual_after_inspection"
auto_enabled = policy_config.auto_create_exchange_order or force
pickup_status = pickup_result.pickup.status
if not auto_enabled:                     → automation_enabled: False
if not _can_create_exchange_for_status(policy, pickup_status): → "not due for creation yet"
→ _create_exchange_order_from_request:
      new_line_items = exchange variant_id/quantity pairs from request.line_items
      if none: escalate + Shopify note   → requires_manual_intervention
      original = Shopify aget_order_details(order_number)
      create   = Shopify aclone_order(original, new_line_items, note, tags=[f"return_exchange_{policy}", "bloomerce_exchange_auto"])
      on success → exchange_created: True
      on failure → escalate + Shopify note → requires_manual_intervention
```

**Response object — created**

```json
{"success": true, "exchange_created": true,
 "exchange_order": {"name": "#gv17099", "id": "5599…"},
 "message": "Exchange order #gv17099 has been created.",
 "request": {"…": ""}, "create_result": {"…": ""}}
```

**Response object — automation off (the normal production path)**

```json
{"success": true, "exchange_created": false, "automation_enabled": false,
 "policy": "manual_after_inspection", "pickup_status": "picked_up",
 "message": "Exchange order automation is not enabled for this client.",
 "request": {"…": ""}}
```

**Other branches**

```json
{"success": true, "exchange_created": false, "already_exists": true,
 "exchange_order": {"id": "5599…", "name": "#gv17099"},
 "message": "Exchange order #gv17099 is already created.", "request": {"…": ""}}
```
```json
{"success": true, "exchange_created": false, "policy": "on_rto", "pickup_status": "picked_up",
 "message": "Exchange order is not due for creation yet based on the configured policy.", "request": {"…": ""}}
```
```json
{"success": false, "exchange_created": false, "request": {},
 "message": "This return request is not an exchange request."}
```
```json
{"success": false, "exchange_created": false, "requires_manual_intervention": true,
 "escalation_id": "3f2c…", "request": {"…": ""},
 "message": "Exchange order needs manual support because variant data is missing."}
```
```json
{"success": false, "exchange_created": false, "requires_manual_intervention": true,
 "escalation_id": "3f2c…", "create_result": {"success": false, "error": "…"}, "request": {"…": ""},
 "message": "Exchange order creation failed and has been escalated to support."}
```
```json
{"success": false, "message": "Missing client_id for exchange automation."}
```

---

### 10. `request_exchange_size_change(order_number: str, desired_size: str, customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:182` → `ReturnPartnerOrchestrator.arequest_exchange_size_change` (`orchestrator.py:959`)

**Description** (verbatim docstring):

> Use this when a customer with an existing exchange request wants a DIFFERENT size than what they
> originally selected (e.g. they picked the wrong size on the Return Prime portal and now want a
> different one).
>
> This does not change the size automatically — Return Prime does not provide an API to edit an
> existing request, and this store does not have exchange-order automation enabled. It captures the
> requested size, adds a note to the Shopify order, and raises an escalation so a human can make the
> correction in Return Prime's dashboard.

**Validation**

1. `cid` present
2. `desired_size` non-empty after strip → else `{"success": False, "message": "No size was provided for the exchange."}`
3. Delegates to `aget_return_status` with `request_type="exchange"` and guards
   **both** flags (`orchestrator.py:995`) — correct

**Reads from**: everything tool 3 reads → **writes**: Shopify order note, Postgres escalation.

**Pseudo-code**

```
cid required; desired_size non-empty
status_result = aget_return_status(cid, order_number, "exchange", state, partner, phone, email)
if identity_verified is False or needs_identity: return status_result     # ← correct guard
if not status_result.success: return status_result
request        = _resolve_request_from_status_result(status_result)
request_number = request.request_number
_add_shopify_note(order_number, "[Bloomerce] Customer requested a different exchange size: …")
escalation_id  = _raise_system_escalation(category="Exchange Size Change", …)
return {success: True, escalation_id, order_number, request_number, requested_size, message}
```

**Response object — success**

```json
{
  "success": true,
  "escalation_id": "3f2c8a41-…",
  "order_number": "gv17083",
  "request_number": "RET777",
  "requested_size": "L",
  "message": "Noted — I've flagged your order (gv17083) for a size L exchange instead, with your order details, to our team so they can update it on their end."
}
```

`escalation_id` originates from `alog_escalation`'s `RETURNING escalation_id`
(`utils/escalation_logger.py:158,179`) — see [Defect 5](#5-escalation_id-is-whatever-the-driver-returns).
Note the message is unconditionally optimistic even when `escalation_id is None`
(the escalation helper swallows every exception and returns `None`,
`return_partners/orchestrator.py:206-212`).

**Failure branches**

```json
{"success": false, "message": "Missing client_id for exchange size change."}
```
```json
{"success": false, "message": "No size was provided for the exchange."}
```
Identity/partner failures are tool 3's dicts returned verbatim.

---

### 11. `get_exchange_delivery_status(order_number: str, customer_phone: str = "", customer_email: str = "") -> dict`
`return_partners/tools.py:212` → `ReturnPartnerOrchestrator.aget_exchange_delivery_status` (`orchestrator.py:1042`)

**Description** (verbatim docstring):

> Use this when the customer asks when the exchanged/replacement product will arrive, where the
> exchange delivery is, or the status of the forward shipment for an exchange item.
>
> This first checks the return/exchange partner to confirm whether an exchange order exists, then
> fetches the delivery status for that exchange order when available.

**Validation**: `cid` present; then guards **both** flags on the status result
(`orchestrator.py:1064`) — correct. `order_number` is a **required** argument with
no default; omitting it produces a LangChain tool-invocation error before any of
this code runs (see [Defect 6](#6-argument-type-failures-reach-production)).

**Reads from**: everything tool 3 reads → when the exchange order is missing but
pickup is complete: `aenrich_return_pickup_leg` (logistics) + `acheck_shopify_variant_stock`
(Shopify GraphQL) + config `return_prime_return_exchange_rules` for the support email
→ when the exchange order exists: `OrderStatusOrchestrator.aget_order_status` on it.

**Pseudo-code**

```
cid required
status_result = aget_return_status(cid, order_number, "exchange", state, partner, phone, email)
if identity_verified is False or needs_identity: return status_result     # ← correct guard
if not status_result.success: return status_result
request = _resolve_request_from_status_result(status_result)
if request.request_type and != "exchange": → {success: True, exchange_order_exists: False, "not an exchange request"}
exchange_order = request.exchange_order | raw.exchange.order
number = exchange_order.name | order_number | id
if not number:
    pickup = aenrich_return_pickup_leg(request, state)
    if pickup.status in {picked_up, return_to_origin}:
        stock = acheck_shopify_variant_stock(cid, exchange line items)     # Shopify GraphQL
        support_email = return_prime_return_exchange_rules.customer_instructions.support_email | "support@groovee.in"
        escalation_id = _raise_system_escalation("Exchange Order Not Created", …)
        return {success: True, exchange_order_exists: False, requires_manual_intervention: True, …}
    return {success: True, exchange_order_exists: False, "exchange order has not been created yet"}
delivery_status = OrderStatusOrchestrator.aget_order_status(number, state)     # exceptions captured, not raised
return {success: True, exchange_order_exists: True, exchange_order, exchange_delivery_status, request, return_status, message}
```

**Response object — exchange order exists**

```json
{
  "success": true, "exchange_order_exists": true,
  "exchange_order": {"id": "5599…", "name": "#gv17099", "status": "exchanged", "created_at": "…", "raw": {"…": ""}},
  "exchange_delivery_status": {"…OrderStatusOrchestrator.aget_order_status payload…": ""},
  "request": {"…": ""},
  "return_status": {"…full tool-3 response…": ""},
  "message": "Exchange order #gv17099 is created. Here is the latest delivery status I found."
}
```

**Response object — pickup done, exchange order missing** (escalates)

```json
{
  "success": true, "exchange_order_exists": false, "requires_manual_intervention": true,
  "escalation_id": "3f2c…",
  "pickup": {"leg": "return_pickup", "status": "return_to_origin", "…": ""},
  "stock": {"success": true, "stock_available": false, "message": "Requested exchange item stock is not currently available.",
            "variants": [{"variant_id": "gid://shopify/ProductVariant/4456…", "title": "L", "sku": "CK-L",
                          "product_title": "Cotton Kurta", "requested_quantity": 1,
                          "inventory_quantity": 0, "inventory_policy": "DENY", "available": false}],
            "raw": {"…": ""}},
  "message": "Your return pickup is completed, but the exchange order has not been created yet. The requested exchange item is not currently available in stock. I have raised this with support for manual review. Please contact support@groovee.in for faster help.",
  "request": {"…": ""}, "return_status": {"…": ""}
}
```

**Other branches**

```json
{"success": true, "exchange_order_exists": false, "request": {"…": ""}, "return_status": {"…": ""},
 "message": "Your exchange request is present, but the exchange order has not been created yet."}
```
```json
{"success": true, "exchange_order_exists": false, "request": {"…": ""}, "return_status": {"…": ""},
 "message": "This return request is not an exchange request."}
```
```json
{"success": false, "message": "Missing client_id for exchange delivery lookup."}
```

---

### 12. `get_return_exchange_request_instructions(request_type: str = "") -> dict`
`return_partners/tools.py:237` → `ReturnPartnerOrchestrator.aget_return_exchange_instructions` (`orchestrator.py:1200`)
→ `return_partners/instructions.py:75::aget_return_exchange_request_instructions`

**Description** (verbatim docstring):

> Use this when the customer asks how to raise a return, exchange, or refund request, especially when
> they are asking for the process/link rather than the status of an existing request.

**Validation**: `cid` present. **No identity gate — by design.** This is
correct: the tool returns only tenant-level policy text and a public portal URL,
no order or customer data. It never takes an order number.

There is, however, **no validation that the config it renders is populated** —
see [Defect 7](#7-instructions-degrade-silently-to-an-empty-link).

**Reads from**: config `return_prime_return_exchange_rules` (`customer_instructions`
or `instructions`, plus `{type}.window_days`) and `return_prime_details` (portal URL,
support email). No network calls, no order lookup.

**Pseudo-code**

```
cid required
config = aget_return_prime_instruction_config(cid, request_type)
    portal_url    = instructions.portal_url | details.portal_url | details.return_portal_url
                    | details.return_exchange_portal_url | ""            ← may be ""
    support_email = instructions.support_email | details.support_email | "support@groovee.in"
    approval_sla  = instructions.approval_sla | "24-48 hrs"
    window_days   = instructions.{type}_window_days | rules[type].window_days | instructions.window_days   ← may be None
template = instructions.{type}_how_to_message | instructions.how_to_message
message  = template.format(**values) if template else <hardcoded default paragraph>
return {success: True, partner: "return_prime", request_type, portal_url, support_email,
        approval_sla, window_days, message, source: "return_prime_return_exchange_rules"}
```

`_template_format` swallows any `KeyError` and returns the **unrendered template**
verbatim (`instructions.py:19-23`), so a template with a typo'd placeholder is
shown to the customer with literal `{braces}`.

**Response object — configured**

```json
{
  "success": true, "partner": "return_prime", "request_type": "return",
  "portal_url": "https://groovee.in/returns", "support_email": "support@groovee.in",
  "approval_sla": "24-48 hrs", "window_days": 7,
  "source": "return_prime_return_exchange_rules",
  "message": "You may click the link to raise your request: https://groovee.in/returns. Enter the order ID and your mobile number. Select the item along with the reason. Update the image of the item with tags intact. Your return/exchange request will be approved/rejected in 24-48 hrs. NOTE: Return/Exchange can be done within 7 days of delivery.\n\nIf you face any issue, please email to support@groovee.in. Please note that you can only return or exchange of the product delivered."
}
```

**Response object — unconfigured (the observed production degradation)**

```json
{
  "success": true, "partner": "return_prime", "request_type": "return",
  "portal_url": "", "support_email": "support@groovee.in",
  "approval_sla": "24-48 hrs", "window_days": null,
  "source": "return_prime_return_exchange_rules",
  "message": "You may click the link to raise your request: . Enter the order ID and your mobile number. …"
}
```

Note the `: . ` — the customer is told to click a link that is not there, and
`success` is still `true`.

**Failure branch**

```json
{"success": false, "message": "Missing client_id for return/exchange instructions."}
```

---

### 13. `get_order_details(order_id: str, phone_number: str = "") -> dict`
`tool_factory.py:2574` (built by `_create_get_order_details_tool(state)`, `tool_factory.py:2562`)

**Description** (verbatim docstring, abridged where it lists fields):

> Get comprehensive order details. Phone validation is built-in.
>
> Do NOT use this tool for return status, exchange status, return pickup, reverse shipment, refund
> status, wallet credit, bank refund timeline, or starting a return/exchange. In the return_exchange
> agent, use the dedicated return partner tools for those cases:
> get_return_status_by_order_number, get_return_pickup_status, get_refund_status_by_order_number, or
> get_return_or_exchange_portal_link.
>
> IMPORTANT: The `order_id` field in the response is the UNIQUE IDENTIFIER for the order. Always use
> this value when calling other tools […]
>
> Returns on failure: error="access_denied" → phone mismatch, stop and ask for verification;
> error="Order X not found" → invalid order ID
>
> NEVER invent an order_id from a number that happens to be in the customer's message. "200 ka
> payment", "₹3498", "2 items" are amounts and quantities, not order IDs. […]

**Validation** (ordered)

1. `if not order_id` → `{"error": "Order ID not provided"}`
2. `_looks_like_customer_phone(order_id, state, phone_number)` → refuses a phone
   passed as an order id
3. Shopify fetch; empty → `{"success": False, "error": "Order {id} not found"}`
4. **Gate A** — `_avalidate_phone_for_order_access(order_id, state, phone_number=…, cached_base_result=…, prefetched_raw_record=…)`,
   which applies `is_real_phone_number` and matches last-10 across DTO customer/billing
   phone, raw order phone, shipping/billing address phone, `customer.phone`,
   `customer.default_address.phone` (`tool_factory.py:2426-2450`)

**Reads from**: Shopify order service (`aget_order_details`), the vendor order
processor, and logistics tracking through `OrderStatusOrchestrator`.

**Pseudo-code**

```
if not order_id:                                  → {"error": "Order ID not provided"}
if _looks_like_customer_phone(order_id, …):       → {"success": False, "error": "invalid_order_id", message}
raw_record = Shopify aget_order_details(order_id) → unwrap → mapping
if not order_data:                                → {"success": False, "error": "Order X not found"}
dto = processor.process_order(raw_record)
phone_check = _avalidate_phone_for_order_access(order_id, state, phone_number, cached_base_result=dto, prefetched_raw_record=raw_record)
     ├─ customer_phone = phone_number or state["phone_number"]
     ├─ if not is_real_phone_number(customer_phone): → needs_phone / should_block
     └─ match last-10 against every phone attached to the order
if phone_check.should_block:                      → {"success": False, "error": "access_denied", message, phone_validated: False}
return {success: True, phone_validated: True, order_id, created_at, …, line_items, tracking, delivered_date, …}
```

**Response object — success** (abridged; field list per docstring)

```json
{
  "success": true, "phone_validated": true,
  "order_id": "#gv17083", "created_at": "2026-07-14T10:22:31Z", "cancelled_at": null,
  "financial_status": "paid", "fulfillment_status": "fulfilled",
  "total_price": "1499.00", "currency": "INR",
  "customer": {"name": "…", "email": "…", "phone": "…"},
  "shipping_address": {"address1": "…", "city": "…", "province": "…", "zip": "…", "country": "IN", "phone": "…"},
  "line_items": [{"title": "Cotton Kurta", "variant_title": "M", "quantity": 1, "price": "1499.00",
                  "sku": "CK-M", "product_id": "8123…", "variant_id": "4456…"}],
  "tags": "…", "note": "…",
  "shipment_status": "delivered",
  "tracking": {"awb": "1234567890", "courier": "Delhivery", "expected_delivery": "…", "current_location": "…"},
  "delivered_date": "21 Jul 2026",
  "logistics_status": "…", "logistics_order_id": "…"
}
```

**Failure branches**

```json
{"error": "Order ID not provided"}
```
```json
{"success": false, "error": "invalid_order_id",
 "message": "That is the customer's phone number, not an order ID. Do not retry get_order_details with it. Call get_recent_orders to list this customer's orders, then use the order_id field from those results."}
```
```json
{"success": false, "error": "Order gv99999 not found"}
```
```json
{"success": false, "error": "access_denied", "phone_validated": false,
 "message": "Customer phone number not available. Please ask customer for their phone number."}
```

That last message is the Gate A response to a `fbw_…` session id — compare with
tool 3's mismatch accusation for the identical situation.

---

### 14. `get_final_return_exchange_message(request_type: str = "return") -> str`
`tool_factory.py:3626`

**The only tool in this set that returns a bare `str`, not a `dict`.**

**Description** (verbatim docstring):

> Get the final return/exchange message from database configuration.
> Use this AFTER customer confirms they want to proceed with return OR exchange.
>
> For same-day deliveries (0 days ago), returns special message that customer needs to wait 24 hours.
>
> Args:
>     request_type: Either 'return' or 'exchange' to get the appropriate message.
> Returns the configured message with website link and contact details.

**Validation**: none in the tool. The orchestrator
(`UtilityOrchestrator.get_final_return_exchange_message`, `core/orchestrator.py:6502`)
reads config and tolerates both raw-string and JSON forms. No identity gate — it
returns tenant policy text only.

**Reads from**: config `after_delivery_return_exchange` (`client_configs`), keys
`return`, `exchange`, `status`, `Grace days for product return validity`,
`Grace days for product exchange validity`, and the two same-day response strings.

**Pseudo-code**

```
result = UtilityOrchestrator.get_final_return_exchange_message(request_type, state)
    config = aget_config("after_delivery_return_exchange", client_id)   # str or dict
    parse if str
    if days_since_delivery == 0: return the configured same-day response for this type
    return config[request_type]
return result.get("message", f"Error fetching {request_type} message")
```

**Response — success**: the raw configured string, e.g.

```
To raise a return, visit https://groovee.in/returns and enter your order ID. …
```

**Response — failure**: `"Error fetching return message"` (missing key) or
`"Error fetching message: <exception>"` (exception path, `tool_factory.py:3650`).
Because the return type is `str`, a failure is indistinguishable from a
successful message to any downstream consumer that isn't matching on the prefix.

---

### 15. `get_nearest_store(pincode: str) -> dict`
`tool_factory.py:2038` (built by `_create_nearest_store_tool(state, client_id)`, `tool_factory.py:2028`)

**Signature confirmed against the deployed commit: `pincode`, not `city_or_pincode`.**
The June clone's `(city_or_pincode)` was stale; `city=None` is now hardcoded in
the call to `afind_nearest_store` (`tool_factory.py:2091`).

**Description** (verbatim docstring):

> Find the nearest offline store for this brand based on a pincode.
>
> Use this tool when:
> - A product/variant is out of stock and you want to suggest a physical store
> - Customer asks about store location, office, warehouse, or brand authenticity
> - Customer is confused with size before buying a product (not a return/exchange) and you want to
>   mention the in-store option
>
> Call this AFTER asking the customer for their pincode (6-digit Indian pincode). Do NOT ask for
> city — always ask for pincode only.
>
> IMPORTANT: The result includes a google_maps_url for the store. You MUST always include this link
> in your response so the customer can navigate directly.
>
> Args:
>     pincode: Customer's 6-digit pincode (e.g. "411038")

**Validation** (ordered)

1. `cid` resolvable → else `{"success": False, "error": "Client not identified"}`
2. **Contact-collection gate** — `check_phone_collection_gate(state, "store_visit_phone_requested")`;
   when it fires, the store lookup is skipped and the LLM is told to ask for a
   phone or email first
3. `aget_all_stores(cid)` empty → brand has no offline stores
4. Radius — `afind_nearest_store(..., limit=2)`; empty → "No stores found within 30 km"

**Side effect**: `_anotify_agent_store_visit` fires on every successful lookup,
notifying the store team with the last 3 human messages and the focal product.

**Reads from**: `utils/store_locations` (`aget_all_stores`, `afind_nearest_store`),
graph state `user_location` (lat/long, when present) and `conversation_context.focal_entity`.

**Pseudo-code**

```
cid = client_id or state.client_id                       → else "Client not identified"
phone_required, _ = check_phone_collection_gate(state, "store_visit_phone_requested")
if phone_required:                                       → phone_number_required block
all_stores = aget_all_stores(cid)                        → else "This brand does not have offline stores."
results = afind_nearest_store(cid, lat, long, city=None, pincode, limit=2)
if not results:                                          → "No stores found within 30 km of this location."
_anotify_agent_store_visit(state, cid, nearest, pincode, recent_user_messages, product_name, product_url)
return {success: True, nearest_store, other_stores, presentation_hint}
```

**Response object — success**

```json
{
  "success": true,
  "nearest_store": {"name": "Groovee Koregaon Park", "address": "…", "city": "Pune",
                    "pincode": "411001", "phone": "…", "distance_km": 4.2,
                    "google_maps_url": "https://maps.google.com/?q=…"},
  "other_stores": [{"…": ""}],
  "presentation_hint": "ALWAYS include the google_maps_url link in your response so the customer can navigate to the store directly."
}
```

**Failure branches**

```json
{"success": false, "error": "Client not identified"}
```
```json
{"success": false, "phone_number_required": true,
 "message": "Before looking up the nearest store, please ask the customer for their phone number or email address so the store team can reach out to them. Once you have the contact info, call this tool again with the pincode."}
```
```json
{"success": false, "message": "This brand does not have offline stores."}
```
```json
{"success": true, "message": "No stores found within 30 km of this location."}
```

---

### 16. `escalate_to_agent(reason: str, category: str = "General", details: str = "", phone_number: str = "", order_id: str = "", escalation_classification: str = "system", immediate_attention: bool = False, human_can_resolve: bool = True) -> dict`
`tool_factory.py:2149` (built by `_create_escalation_tool(state, agent="return_exchange")`, `tool_factory.py:2129`)

The `agent="return_exchange"` closure argument routes the escalation to the
number(s)/email(s) configured for this agent.

**Description** (verbatim docstring, abridged at the category list):

> Escalate to a human agent when the bot cannot resolve the customer's request. This tool sends a
> WhatsApp notification to the agent, logs the escalation to the database, stores the escalation in
> conversation history, and switches conversation mode to human agent — all in one call.
>
> Use when: […] Customer asks about restocking / back-in-stock timelines; Order issue requires human
> judgement; Customer explicitly asks for a human agent; Customer is frustrated or dissatisfied;
> Callback scheduling is requested; Any situation the bot cannot handle autonomously.
>
> 🚫 DO NOT escalate just because information you need is missing. A missing phone number or order ID
> is NOT an escalation trigger — it is a normal clarifying question. When an order-lookup tool
> returns needs_phone (or you otherwise lack the phone/order ID), simply ASK the customer for their
> phone number or order ID and continue. Only escalate once you have the information AND still cannot
> resolve the request.
>
> Note: Courier Update Pending notifications are handled automatically by order update tools — you do
> NOT need to call this tool for that.

Relevant categories for this agent: `Exchange Delayed`, `Exchange Request`,
`Pickup Query`, `Refund Delayed`, `Return Delayed`, `Return Request`,
`Payment/Refund Status`, `Damaged in Transit`, `Warranty Claim`.

`escalation_classification` ∈ `{user_configured, agentic, system}`.
`human_can_resolve=False` is documented as "the request cannot be fulfilled at
all" and diverts to presenting real alternatives instead of escalating.

**Validation**: none in the tool wrapper — everything is delegated. The whole
body is a try/except around `EscalationOrchestrator.aescalate_to_agent`
(`core/orchestrator.py:2160`), which owns actionability scoring, soft-blocking,
metric emission and routing.

**Reads from / writes to**: Postgres `escalations` (write), Gupshup WhatsApp
notification, escalation email fan-out, conversation-mode switch, escalation
snapshot cache invalidation.

**Pseudo-code**

```
try:
    return EscalationOrchestrator.aescalate_to_agent(
        category, reason, details or reason, state, phone_number or None, order_id or None,
        escalation_classification, agent="return_exchange", immediate_attention, human_can_resolve)
except Exception as e:
    return {"success": False, "error": str(e), "message": "I'll have our team reach out to you about this."}
```

**Response object — success** (shape owned by `EscalationOrchestrator`)

```json
{"success": true, "escalation_id": "3f2c…", "category": "Return Delayed",
 "message": "<customer-facing acknowledgement>"}
```

For `"Courier Update Pending"` the response also carries `notified` (bool) and
`partners` (list).

**Failure branch**

```json
{"success": false, "error": "<exception text>",
 "message": "I'll have our team reach out to you about this."}
```

**Note the conflict with Defect 1**: the docstring explicitly bans escalating for
a missing phone, but Gate B never emits `needs_phone` — it emits a *mismatch*.
The model is not in the state the docstring describes, so the ban does not bind.
See [Defect 8](#8-the-escalation-ban-does-not-cover-the-state-defect-1-produces).

---

### 17. `get_contact_information() -> dict`
`tool_factory.py:1808` (built by `_create_contact_information_tool(state)`, `tool_factory.py:1792`;
attached by `_append_shared_contact_tool`, `core/tool_registry.py:252`)

Not produced by `return_exchange_tools_factory` — appended centrally to every
agent, idempotently (a no-op if the tool is already present).

**Description** (verbatim docstring):

> Get customer support contact information (email and phone numbers).
>
> ALWAYS call this before telling a customer to reach out to support — never invent, guess, or use
> placeholder contact details (phone, email, URL, or brand name). Use it when escalating, when you
> cannot resolve a request, or for bulk / wholesale / B2B inquiries, so the reply contains the real
> support contact instead of placeholders.

**Validation**: none. No arguments, no gate. Tenant-level policy config only.

**Reads from**: config `vendor_contact_details` via
`UtilityOrchestrator.get_policy_config` (memory → Redis → Postgres `client_configs`).

**Pseudo-code**

```
return UtilityOrchestrator.get_policy_config("vendor_contact_details", state=state)
```

**Response object**: whatever `get_policy_config` returns for the tenant, e.g.

```json
{"success": true, "config": {"support_email": "support@groovee.in",
                             "support_phone": "+91 …", "brand_name": "Groovee",
                             "website": "https://groovee.in"}}
```

---

## Defect list

Ordered by severity. Each item states what the handoff expected, what the code
actually does, and the evidence.

### 1. Gate B has no phone-shape guard

**Confirmed, and it is the root cause of trace `8b099dc2`.**

`return_partners/identity.py` never calls `is_real_phone_number`. `_state_phone`
(`identity.py:33`) pulls `state["phone_number"]`, which on webchat is the widget
session id; `_normalize_phone` (`identity.py:17`) digit-strips it to a short but
**truthy** string, so the `not normalized_phone and not normalized_email` guard at
`identity.py:180` never fires and control reaches the mismatch branch at
`identity.py:193`.

**Blast radius** — every Gate B tool: tools 3, 4, 5, 6, 7, 8, 9, 10, 11. Tools 1,
2 and 13 (Gate A) are unaffected. Tools 12, 14, 16, 17 have no gate.

**Customer-visible effect**: an anonymous visitor who supplied nothing is told
*"The details shared do not match this order. The order phone ends in NNNN."*
The bot never asks for a phone number, because `needs_identity` is `false`.

**Severity**: this is both a UX failure and the enabler for Defect 2.

### 2. The full Shopify order is returned inside the blocked response

**Confirmed, and it is worse than the handoff estimated — it is not just the phone.**

`ReturnIdentityResult` carries `order: dict[str, Any]` (`models.py:24`), and every
branch of `averify_order_identity` — including both failure branches
(`identity.py:187`, `identity.py:202`) — populates it with the entire Shopify order
payload. `_verify_identity` returns `.model_dump()` unchanged, and every blocked
orchestrator branch embeds it as `"identity": identity`
(`orchestrator.py:300-314`, `414-425`, `480-487`, `623-629`).

So while the customer-facing `message` correctly masks to last-4, the structured
`identity.order` handed to the LLM contains the customer's **full phone number,
email, name and complete shipping address**.

**This closes the loop on the `RET777` disclosure.** The model read
`9511785447` out of `identity.order`, passed it to
`list_return_requests_by_order_number` as `customer_phone`, satisfied Gate B, and
disclosed the request to an unverified visitor. The gate did exactly what it was
written to do — the blocked response handed the model the key.

Note the same `identity.order` is deliberately reused as a *performance*
optimisation on the success path (`orchestrator.py:513`, `531` pass
`order=identity.get("order")` into rules validation and portal-link building), so
the field cannot simply be deleted — it must be stripped on the block path only.

### 3. Identity blocks leak through `get_return_pickup_status`

**New — not in the handoff.**

`aget_return_pickup_status` (`orchestrator.py:757`) guards on `success` alone:

```python
if not status_result.get("success"):
    return status_result
```

An identity block returns `success: True`, so execution continues.
`_resolve_request_from_status_result` finds no request keys and returns `{}`,
`aenrich_return_pickup_leg({})` classifies an empty status as `unknown`, and the
tool returns `success: True` with *"I could not find a pickup status for this
return yet."* — **the identity message is discarded entirely.**

`aensure_exchange_order` (tool 9) inherits this: it calls
`aget_return_pickup_status` and also checks only `success`
(`orchestrator.py:879`), so on an identity block it proceeds with `request = {}`
and returns *"This return request is not an exchange request."*

Tools 8, 10 and 11 get this right — they check
`status_result.get("identity_verified") is False` as well
(`orchestrator.py:796`, `990`, `1063`). Tools 7 and 9 are the outliers.

**Effect**: on a blocked identity, a customer asking about pickup is told there
is no pickup data, and a customer asking about their exchange is told they have
no exchange request. Both are false and neither prompts for identity.

### 4. Identity enforcement is inconsistent — but not as described

**Partially corrected.** The handoff said `get_return_request_by_id` "shows no
identity gate at all". It does have one (`orchestrator.py:617-632`), but it is
materially weaker than the others in three ways:

1. **Post-hoc** — the partner API is called *before* any identity check, so the
   data is fetched regardless.
2. **State-only** — `_verify_identity` is invoked without `customer_phone` /
   `customer_email` (`orchestrator.py:618-622`). A phone the customer supplied
   this turn cannot be used to pass the gate, and the state fallback is the only
   input — which on webchat is the `fbw_…` session id.
3. **Skippable** — if the partner response carries no `order_name`/`order_number`,
   the `if result.get("success") and order_name` condition is false and the full
   request is returned with **no identity check at all**.

`get_return_exchange_request_instructions` (tool 12) having no gate is correct —
it exposes no order or customer data.

Full enforcement matrix:

| Tool | Gate | Notes |
|---|---|---|
| 1 `get_customers_delivered_orders_by_phone` | A | shape guard, twice |
| 2 `get_recent_orders` | A | shape guard |
| 3 `get_return_status_by_order_number` | B | pre-call; partner fallback |
| 4 `list_return_requests_by_order_number` | B | pre-call; partner fallback |
| 5 `get_return_request_by_id` | B (weak) | post-call, state-only, skippable |
| 6 `get_return_or_exchange_portal_link` | B | pre-call |
| 7 `get_return_pickup_status` | B (leaks) | inherits, guards on `success` only |
| 8 `get_refund_status_by_order_number` | B | inherits, guards both flags |
| 9 `ensure_exchange_order_created` | B (leaks) | inherits tool 7's leak |
| 10 `request_exchange_size_change` | B | inherits, guards both flags |
| 11 `get_exchange_delivery_status` | B | inherits, guards both flags |
| 12 `get_return_exchange_request_instructions` | none | correct by design |
| 13 `get_order_details` | A | shape guard |
| 14 `get_final_return_exchange_message` | none | policy text only |
| 15 `get_nearest_store` | contact gate | different purpose |
| 16 `escalate_to_agent` | none | by design |
| 17 `get_contact_information` | none | policy text only |

### 5. `escalation_id` is whatever the driver returns

**Confirmed, and it is not confined to `request_exchange_size_change`.**

`alog_escalation` generates `escalation_id = str(uuid.uuid4())`
(`utils/escalation_logger.py:126`) but does **not** return that string. It returns
what Postgres gives back from `RETURNING escalation_id`
(`escalation_logger.py:158`, `179`):

```python
returned_id = result.get("escalation_id") if isinstance(result, dict) else (result[0] if result else escalation_id)
```

On a `uuid`-typed column the driver hands back a `uuid.UUID` object, which
propagates unchanged through `alog_escalation_from_state` (declared
`-> Optional[str]`, `escalation_helper.py:443`) and `_raise_system_escalation`
(declared `-> str | None`, `return_partners/orchestrator.py:183`) into the tool
response. Both type annotations are wrong.

**Affected tools**: 3 (partner-failure escalation), 8 (SLA-breach escalation),
9 (two automation-failure escalations), 10, 11 — not just tool 10.

`json.dumps` on any of those responses raises `TypeError: Object of type UUID is
not JSON serializable`. The one-line fix is to coerce at the source:
`return str(returned_id) if returned_id else None`.

### 6. Argument-type failures reach production

**Confirmed by signature inspection.**

- `get_return_or_exchange_portal_link` takes nine parameters and its docstring
  documents **none** of them (`return_partners/tools.py:80-110`). The LLM sees only
  the JSON-schema types. `selected_line_items: list[dict] | None` is the one most
  likely to be guessed wrong, and a string there fails LangChain validation before
  the tool body runs.
- `get_exchange_delivery_status(order_number: str, …)` has `order_number` as a
  required positional with no default, so an omission is a hard invocation error
  rather than a handled `{"success": False}`.

Both surface as LangChain *"Error invoking tool … Please fix the error and try
again"* turns, which cost a round trip and can derail the conversation.

Contrast tools 1, 2 and 13, whose docstrings carry full `Args:` blocks — those
are not observed failing this way.

### 7. Instructions degrade silently to an empty link

**Confirmed.**

`aget_return_exchange_request_instructions` (`instructions.py:75`) has no
validation on the config it renders. `portal_url` defaults to `""`
(`instructions.py:43-51`) and `window_days` to `None`
(`instructions.py:67-71`), and neither is checked before the default message
template interpolates them (`instructions.py:112-119`). The result is
`success: true` alongside:

> You may click the link to raise your request: **.** Enter the order ID and your mobile number. …

Two related silent-degradation paths in the same module: `_template_format`
(`instructions.py:19`) returns the **unrendered template** on any formatting
exception, so a bad placeholder reaches the customer as literal `{braces}`; and
the `window_days` sentence is dropped entirely when the value is falsy, so a
misconfigured window silently becomes "no window mentioned".

### 8. The escalation ban does not cover the state Defect 1 produces

**Confirmed, with the mechanism now clear.**

`escalate_to_agent`'s docstring bans escalating for a missing phone or order id
and specifically names the signal to watch for:

> When an order-lookup tool returns **needs_phone** (or you otherwise lack the phone/order ID),
> simply ASK the customer for their phone number

But Gate B never emits `needs_phone`. On the `fbw_…` path it emits
`identity_verified: false, needs_identity: false` plus a mismatch message. From
the model's point of view the customer *did* supply details and they were
*wrong* — which is not the state the ban describes, so the ban does not bind and
escalating is a reasonable read of the situation.

Fixing Defect 1 (so Gate B returns `needs_identity: true` for a session id)
resolves this without touching the prompt.

### 9. Signature drift — resolved

`get_nearest_store` is `(pincode: str)` at the deployed commit
(`tool_factory.py:2038`), matching production. The June clone's
`(city_or_pincode)` was stale; `city=None` is now hardcoded at the call site
(`tool_factory.py:2091`). All 17 signatures in this document are read from
`37bb122`.

### 10. Failures are reported as `success: true` with a negative flag

**Confirmed** — and this is why LangSmith shows the failing runs green.

The negative-flag vocabulary, by tool:

| Flag | Tools |
|---|---|
| `identity_verified: false` | 3, 4, 5, 6, 7, 8, 9, 10, 11 |
| `needs_identity: true` | 3, 4, 5, 6 |
| `eligible: false` | 6, 9 |
| `already_exists: true` | 6, 9 |
| `exchange_created: false` | 9 |
| `exchange_order_exists: false` | 11 |
| `requires_manual_intervention: true` | 9, 11 |
| `automation_enabled: false` | 9 |
| `needs_customer_input: true` | 6 (inside `validation`) |
| `invalid_phone: true` | 1 |
| `needs_phone: true` | 2 |
| `phone_number_required: true` | 15 |

Any alerting on this toolset must filter on these flags, not on `success` or on
run status. In particular, **an identity failure is never an error-rate signal**
in the current shape.

---

## Suggested fixes

Ordered by value-to-effort. Defects 1 and 2 together are the security fix and
should ship as one change.

**1. Add the shape guard to Gate B** — `return_partners/identity.py`, after line 112:

```python
from fashion_bot.utils.phone_number_utils import is_real_phone_number

provided_phone = customer_phone or _state_phone(state)
provided_email = customer_email or _state_email(state)

# A webchat session id (`fbw_…` / `web_…`) is not an identity. Without this,
# _normalize_phone digit-strips it into a short-but-truthy string that falls
# through to the mismatch branch and accuses the customer of supplying
# wrong details they never supplied.
if provided_phone and not is_real_phone_number(str(provided_phone)):
    provided_phone = None
```

That single change routes the `fbw_…` case to the existing `needs_identity`
branch at `identity.py:180`, which already returns the right message and the
right flags. It also resolves Defect 8.

**2. Strip the order from blocked identity results.** The `order` field must not
reach the model on a block. Since the success path legitimately reuses it
(`orchestrator.py:513`, `531`), strip at the response boundary rather than in the
model — e.g. a helper applied in each blocked branch:

```python
def _safe_identity(identity: dict) -> dict:
    return {k: v for k, v in identity.items() if k not in ("order", "order_email")}
```

Applied at `orchestrator.py:300-314`, `414-425`, `480-487`, `623-629`.
`masked_order_phone` (last-4) can stay; `provided_phone` should stay too, since
it is the customer's own input. Consider also dropping `order_email`, which is
currently returned in full.

**3. Guard `identity_verified` in tools 7 and 9** — match what tools 8, 10 and 11
already do:

```python
# orchestrator.py:756  (aget_return_pickup_status)
if not status_result.get("success") or status_result.get("identity_verified") is False:
    return status_result

# orchestrator.py:876  (aensure_exchange_order)
if not pickup_result.get("success") or pickup_result.get("identity_verified") is False:
    return pickup_result
```

**4. Coerce `escalation_id` to `str`** at `utils/escalation_logger.py:179`:

```python
return str(returned_id) if returned_id else None
```

Fixes JSON serialisation for all five affected tools at once and makes the
existing `-> Optional[str]` annotation true.

**5. Tighten `get_return_request_by_id`** — pass the caller's phone/email through
to `_verify_identity`, and treat a partner response with no order name as
unverifiable rather than as unguarded.

**6. Document the arguments of `get_return_or_exchange_portal_link`** — add an
`Args:` block naming the shape of `selected_line_items` (a list of
`{line_item_id, product_id, variant_id, title, quantity}` per
`ReturnLineItemSelection`, `models.py:32`), and consider coercing a stray string
rather than failing the invocation.

**7. Fail loudly in `get_return_exchange_request_instructions`** when
`portal_url` is empty — return `success: False` with a configuration-gap message
instead of a sentence containing a missing link.

**8. Alerting** — dashboards over this toolset must key on the negative flags in
the Defect 10 table. A run with `success: true, identity_verified: false` is a
failed customer interaction and currently counts as a success everywhere.
