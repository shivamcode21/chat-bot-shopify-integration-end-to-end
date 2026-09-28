# Return Partner Orchestration

## Purpose

This document defines the architecture for return/exchange flows across multiple return partners. Return Prime is the first partner, but the chat, webhook, escalation, and validation paths must stay partner-agnostic so a client can later use another return partner without adding partner-specific chat tools.

The guiding pattern is the existing delivery/logistics architecture:

```text
chat tool -> orchestrator -> router -> selected partner adapter/service
webhook -> webhook orchestrator -> router/partner normalizer -> notification/escalation side effects
```

The tool layer must remain stateless. It receives input, calls the orchestrator, and returns a structured result. It must not mutate conversation state.

## Current Gap

The current Return Prime integration is directly stitched into `return_exchange_tools_factory`:

```text
return_exchange tool -> Return Prime workflow service -> Return Prime adapter
```

This is useful for proving API connectivity, but it is not the final architecture because:

- The tool names are Return Prime-specific.
- The partner is selected by code, not by client configuration.
- `return_prime/workflow/service.py` is not a workflow. It is a partner application service/facade: it normalizes Return Prime responses, resolves portal config, and delegates webhook storage.
- Multiple return partners would require more partner-specific tools unless we add an orchestration layer.

## Implementation Status

The first orchestration slice is implemented:

- `return_partners/interfaces.py` defines the stateless return partner service contract.
- `return_partners/registry.py` registers available return partners.
- `return_partners/router.py` resolves the partner from explicit input, client config, or Return Prime config/env.
- `return_partners/orchestrator.py` provides generic return status/request/portal methods with bounded retry and system escalation on partner failures.
- `return_partners/tools.py` exposes generic chat tools.
- `return_prime/service.py` registers Return Prime behind the generic return partner interface.
- `ReturnExchangeOrchestrator` exposes thin generic methods that tools call.
- `return_exchange_tools_factory` now uses generic return partner tools instead of Return Prime-specific tools.
- Return pickup/RTO status workflow is implemented as `aget_return_pickup_status`.
- Exchange order automation is implemented as `aensure_exchange_order`, gated by `return_exchange_automation` config and pickup/RTO policy.

Not implemented in this slice:

- Generic webhook route/orchestrator.
- Slack/client-channel alert helper.
- Full delivery-partner reverse-shipment lookup when Return Prime does not expose pickup AWB/tracking data.
- WhatsApp template send on exchange order auto-creation.
- Rename of `return_prime/workflow/service.py`.

## Target Architecture

```text
Normal chat
  -> return_exchange agent
  -> generic return tools
  -> ReturnExchangeOrchestrator
  -> ReturnPartnerRouter
  -> return partner service
  -> partner adapter

Return partner webhook
  -> FastAPI webhook route
  -> ReturnWebhookOrchestrator
  -> partner webhook normalizer
  -> DB event store
  -> notification/escalation policy
  -> WhatsApp template / system escalation / Slack alert
```

## Package Layout

```text
fashion_bot/
  return_partners/
    interfaces.py
    registry.py
    router.py
    orchestrator.py
    workflow.py
    models.py
    failures.py

  return_prime/
    adapter/client.py
    service.py
    webhook/
      service.py
    notifications/
      service.py
```

`return_prime/workflow/service.py` should eventually be renamed to `return_prime/service.py` or `return_prime/application_service.py`. The word "workflow" should be reserved for multi-step orchestration and state transitions.

## Core Concepts

### Partner Service

A partner service wraps one external return partner. It is stateless and exposes normalized operations:

```python
class ReturnPartnerService(Protocol):
    partner_name: str

    async def get_status_by_order_number(
        self,
        client_id: str,
        order_number: str,
        request_type: str | None = None,
    ) -> ReturnStatusResult: ...

    async def get_request_by_id(
        self,
        client_id: str,
        request_id: str,
    ) -> ReturnRequestResult: ...

    async def get_portal_link(
        self,
        client_id: str,
        order_number: str,
        customer_email: str,
    ) -> ReturnPortalResult: ...

    async def normalize_webhook(
        self,
        client_id: str,
        payload: dict,
        headers: dict,
    ) -> ReturnWebhookEvent: ...
```

The Return Prime implementation calls `ReturnPrimeAdapter`, handles Return Prime-specific response shapes such as `data.list` and `data.request`, and returns common DTOs.

### Router

The router selects the partner for a client and request:

```text
explicit partner argument
  -> request/order metadata if available
  -> client return partner config
  -> no-partner configured result
```

For the first version, client config can be:

```json
{
  "primary_return_partner": "return_prime",
  "return_partners": [
    {
      "name": "return_prime",
      "connected": true,
      "priority": 1
    }
  ]
}
```

Longer term this can move to a `return_partner_integrations` table, mirroring `delivery_partner_integrations`.

### Orchestrator

The orchestrator owns the real workflow. It coordinates validation, partner selection, retries, customer-safe responses, escalation, and alerts.

It is the only layer tools should call.

```text
tool -> ReturnExchangeOrchestrator.get_return_status(...)
tool -> ReturnExchangeOrchestrator.get_return_or_exchange_portal(...)
tool -> ReturnExchangeOrchestrator.get_return_pickup_status(...)
```

The orchestrator does not know Return Prime API details. It knows the workflow and calls the selected partner service via the router.

## Chat Tool Contract

The tools exposed to the LLM should be generic:

- `get_return_status_by_order_number`
- `list_return_requests_by_order_number`
- `get_return_or_exchange_portal_link`
- `get_return_pickup_status`
- `get_exchange_order_eta`
- `escalate_return_exchange_issue`

The tools should not be named `get_return_prime_*`. Return Prime should be hidden behind the router.

Example:

```python
@tool
async def get_return_status_by_order_number(order_number: str, request_type: str = "") -> dict:
    client_id = state["client_id"]
    return await ReturnExchangeOrchestrator.aget_return_status(
        client_id=client_id,
        order_number=order_number,
        request_type=request_type or None,
        state=state,
    )
```

The tool may read `client_id`, `phone_number`, or `conversation_id` from state, but it must not write to state.

## Workflows

### 1. Return/Exchange Status Query

```text
Validate customer and order
  -> infer request type: return | exchange | unknown
  -> run eligibility/identity validations
  -> route to configured return partner
  -> call partner status API with retry
  -> normalize partner response
  -> return customer-safe message
```

Failure handling:

```text
partner API fails
  -> retry bounded times
  -> retry fails
  -> apologize to customer
  -> show configured customer support details
  -> raise system escalation
  -> raise Slack/client-channel alert
```

### 2. Start Return/Exchange Request

```text
Validate customer and order
  -> infer return vs exchange
  -> run validations:
       delivered order
       within return/exchange window
       item eligible
       customer identity matches
       client policy allows requested type
  -> validation fails:
       return specific configured message
       optionally raise configured escalation
  -> validation passes:
       fetch partner portal link
       explain how customer should initiate return/exchange
```

For Return Prime, the partner does not mutate state or create a return from chat. It returns a portal link and the customer completes the request in Return Prime.

### 3. Webhook: Request Created

```text
Return partner webhook received
  -> verify tenant and signature if configured
  -> normalize event
  -> dedupe by stable event key
  -> persist event
  -> identify return vs exchange
  -> send configured WhatsApp template:
       return: "your return request has been taken"
       exchange: "your exchange request has been taken"
```

Failure handling:

```text
webhook storage/processing fails
  -> mark event failed if event id exists
  -> apologize only if this is customer-facing path
  -> raise system escalation
  -> raise Slack/client-channel alert
```

Webhook ingestion itself should still return a fast 2xx after durable storage whenever possible.

### 4. Webhook: Request Rejected

```text
webhook passes
  -> normalized status = rejected
  -> persist event
  -> send configured rejection template
  -> store rejection reason/comment
```

If the customer later complains:

```text
fetch latest return request
  -> explain rejection reason clearly
  -> if customer remains frustrated/escalates
       show support details
       raise frustrated-customer escalation
```

### 5. Webhook: Request Approved

```text
webhook passes
  -> normalized status = approved
  -> persist event
  -> update request/event tables
  -> send configured approval template if enabled
```

If customer asks about refund:

```text
query money/refund status from partner
  -> normalize amount/status/timeline
  -> answer customer
```

### 6. Return Pickup Status Query

```text
Validate customer and order
  -> identify return request
  -> fetch pickup/shipment status from delivery partner
  -> normalize status:
       pickup scheduled
       out for pickup
       picked up
       in transit to origin
       return to origin
       pickup failed/exception
  -> respond using status-specific message
```

Escalation branch:

```text
pickup delayed or customer frustrated
  -> apologize
  -> show support number
  -> raise frustrated-customer escalation
  -> email/notify delivery partner if configured
```

### 7. Exchange Order Timing

Client policy controls when exchange order should be created:

```json
{
  "exchange_creation_policy": "on_pickup | on_rto | manual_after_inspection | existing_customer_immediate"
}
```

#### On Pickup

```text
return picked up
  -> create exchange order
  -> send WhatsApp template
  -> chat says exchange order is created
```

#### On Return To Origin

```text
return in transit to origin
  -> fetch estimated RTO date
  -> tell customer expected dispatch date

return status = return_to_origin
  -> create exchange order automatically
  -> send WhatsApp template
```

#### Stock Failure

```text
exchange order creation fails due to stock unavailable
  -> create user-defined escalation
  -> update Shopify note
  -> tell customer support will assist
```

#### Existing Customer Immediate

```text
identify existing customer
  -> policy allows immediate exchange
  -> create exchange as soon as customer asks
```

## Validation Layer

Validation should be a separate composable pipeline, not embedded in partner adapters:

```text
CustomerIdentityRule
OrderExistsRule
OrderDeliveredRule
ReturnWindowRule
ItemEligibilityRule
RequestTypeAllowedRule
ExistingRequestRule
ExchangeStockRule
ClientPolicyRule
```

Each rule returns:

```json
{
  "passed": true,
  "code": "ORDER_DELIVERED",
  "message": "Order is delivered and eligible.",
  "customer_message": null,
  "escalation": null
}
```

When a rule fails, the orchestrator returns the rule's configured customer message and raises escalation only if the client policy says so.

## Failure Handling

Failures should be classified:

| Failure | Customer Response | Internal Action |
|---|---|---|
| Validation failure | Specific configured message | Optional configured escalation |
| Partner API timeout/5xx | Apology + support details | Retry, system escalation, Slack alert |
| Partner API 4xx/config error | Apology + support details | System escalation, config gap |
| Webhook parse/signature failure | No customer message | Security/system log, optional alert |
| Webhook processing failure after store | No immediate customer message | Mark failed, retry/requeue, alert |
| Template send failure | No duplicate customer sends | Notification failed row, alert |
| Customer frustration | Apology + support details | Frustrated-customer escalation |

## Retry Policy

Partner API retries should be in the partner service or a shared async retry decorator:

```text
retry only transient failures:
  timeout
  connection error
  HTTP 429
  HTTP 5xx

do not retry:
  validation failures
  missing config
  HTTP 400/401/403 unless classified as transient by partner
```

Retries must be bounded and async. No `time.sleep`.

## Escalation and Slack Alerts

Escalation should use existing helpers:

```text
utils.escalation_helper.alog_escalation_from_state
EscalationOrchestrator.escalate_to_agent
```

Slack/client-channel alerting does not currently have a generic outbound helper in this codebase. The architecture should introduce one:

```text
notifications/slack_alerts.py
  async def asend_client_alert(client_id, category, title, message, metadata)
```

Config:

```json
{
  "slack_alerts": {
    "enabled": true,
    "channel": "#client-return-alerts",
    "webhook_url_secret_key": "SLACK_WEBHOOK_URL_CLIENT_X"
  }
}
```

Until that helper exists, system escalation should still be logged in DB and emitted through the existing escalation event publisher.

## Webhook Tables

Existing Return Prime tables are a good start:

- `return_prime_webhook_events`
- `return_prime_whatsapp_notifications`

For multi-partner architecture, add generic tables or a generic envelope:

```text
return_partner_events
  id
  client_id
  partner
  event_type
  request_id
  request_number
  request_type
  request_status
  order_name
  customer_phone
  payload_json
  dedupe_key
  processing_status
```

Partner-specific raw payload can remain in partner tables during migration, but the orchestrator should consume the generic event model.

## Tool Invocation Flow

The LLM should only see generic tools.

## Orchestrator Method Contract

Tools call orchestrator methods. Orchestrator methods call workflows. Workflows call routers and side-effect services.

```text
get_return_status_by_order_number
  -> ReturnExchangeOrchestrator.aget_return_status
  -> ReturnStatusWorkflow.run
  -> ReturnPartnerRouter.aresolve_partner
  -> selected_partner.get_status_by_order_number

get_return_or_exchange_portal_link
  -> ReturnExchangeOrchestrator.aget_return_or_exchange_portal
  -> ReturnRequestWorkflow.run
  -> ValidationPipeline.run
  -> ReturnPartnerRouter.aresolve_partner
  -> selected_partner.get_portal_link

get_return_pickup_status
  -> ReturnExchangeOrchestrator.aget_return_pickup_status
  -> ReturnPickupWorkflow.run
  -> ReturnPartnerRouter.aresolve_partner
  -> selected_partner.get_status_by_order_number
  -> LogisticsRouter.aget_order_details_first_valid

get_exchange_order_eta
  -> ReturnExchangeOrchestrator.aget_exchange_order_eta
  -> ExchangeOrderWorkflow.run
  -> selected_partner.get_status_by_order_number
  -> LogisticsRouter for return pickup/RTO status
  -> ShopifyOrderAdapter only if policy permits exchange creation
```

Workflow side effects must be explicit:

| Workflow | Reads | Writes/Side Effects |
|---|---|---|
| `ReturnStatusWorkflow` | Return partner API, config | System escalation + Slack alert only on partner failure |
| `ReturnRequestWorkflow` | Shopify/order data, validation config, return partner API | Optional configured escalation on validation failure |
| `ReturnWebhookWorkflow` | Webhook payload/config | Event DB rows, WhatsApp templates, failure escalation/alert |
| `ReturnPickupWorkflow` | Return partner API, delivery partner API | Frustration escalation, delivery-partner notification if configured |
| `ExchangeOrderWorkflow` | Return pickup/RTO status, stock, client policy | Shopify exchange order, Shopify note, WhatsApp template, escalation on failure |

The chat tools themselves never write state or DB rows directly.

### Status Query

```text
Customer: "What is my return status for #gv15265?"
  -> intent routes to return_exchange
  -> tool: get_return_status_by_order_number(order_number="#gv15265")
  -> ReturnExchangeOrchestrator.aget_return_status(...)
  -> ReturnPartnerRouter selects return_prime
  -> ReturnPrimeService.get_status_by_order_number(...)
  -> ReturnPrimeAdapter.list_requests(...)
  -> response normalized
  -> tool returns:
       success=true
       request_type=return
       status=requested
       message="Return request RET616 is requested."
```

### Start Return/Exchange

```text
Customer: "I want to exchange order #1234"
  -> intent routes to return_exchange
  -> tool: get_return_or_exchange_portal_link(order_number="#1234", request_type="exchange")
  -> orchestrator validates order/customer/policy
  -> router selects return_prime
  -> service builds portal link
  -> tool returns customer-safe instructions
```

### Pickup Query

```text
Customer: "Has my return pickup happened?"
  -> tool: get_return_pickup_status(order_number="#1234")
  -> orchestrator finds return request
  -> delivery partner router fetches return pickup shipment
  -> status normalizer maps partner status
  -> response says pickup scheduled / picked up / delayed / returned to origin
```

## Naming

Use "workflow" for multi-step state transitions only.

Recommended rename:

```text
ReturnPrimeWorkflowService -> ReturnPrimeService
return_prime/workflow/service.py -> return_prime/service.py
```

New workflow classes:

```text
ReturnRequestWorkflow
ReturnWebhookWorkflow
ExchangeOrderWorkflow
ReturnPickupWorkflow
```

These classes should orchestrate validation, partner calls, escalation, notification, and state transitions.

## Migration Plan

1. Keep current Return Prime tools temporarily for live API testing.
2. Add `return_partners` package with interfaces, registry, router, and orchestrator.
3. Register Return Prime as a return partner.
4. Add generic chat tools and replace direct Return Prime tools in `return_exchange_tools_factory`.
5. Move Return Prime normalization from `workflow/service.py` to `return_prime/service.py`.
6. Add generic webhook route/orchestrator for partner webhooks.
7. Add Slack alert helper/config.
8. Add tests:
   - router selects Return Prime for configured client
   - validation failure returns configured customer message
   - API timeout retries and escalates
   - webhook created/rejected/approved events send correct template
   - pickup status maps delivery partner statuses correctly

## First Code Slice

The first reviewable implementation should be small:

```text
return_partners/interfaces.py
return_partners/registry.py
return_partners/router.py
return_partners/orchestrator.py
return_prime/service.py
return_prime/__init__.py registers return_prime
return_exchange_tools_factory uses generic tools
```

Do not wire exchange order creation automation in the first slice. That is a separate workflow because it mutates Shopify and needs stronger tests and stock-failure handling.
