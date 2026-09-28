# Order webhook test payloads (Postman)

**Endpoint:** `POST /order/webhook`

**Headers (required):**
| Header | Example |
|--------|---------|
| `Content-Type` | `application/json` |
| `X-Shopify-Topic` | `orders/updated` |
| `X-Shopify-Shop-Domain` | `iconic-india.myshopify.com` |
| `X-Shopify-Webhook-Id` | `postman-test-fulfilled-1` (use a new value per run) |

**How event keys are resolved** (`financial_status` + `fulfillment_status`):

| File | Expected event_key | financial_status | fulfillment_status | cancelled_at |
|------|-------------------|------------------|--------------------|--------------|
| `01_fulfilled.json` | `FULFILLED` | `paid` | `fulfilled` | `null` |
| `02_voided.json` | `VOIDED` | any (overridden) | any (overridden) | set |
| `03_paid_unfulfilled.json` | `PAID_UNFULFILLED` | `paid` | `null` | `null` |
| `04_partially_paid_unfulfilled.json` | `PARTIALLY_PAID_UNFULFILLED` | `partially_paid` | `null` | `null` |
| `05_payment_pending_unfulfilled.json` | `PAYMENT_PENDING_UNFULFILLED` | `pending` | `null` | `null` |
| `06_payment_pending.json` | `PAYMENT_PENDING` | `pending` | `null` | `null` |

**Note on `PAYMENT_PENDING`:** Same body as `05`, but only resolves to `PAYMENT_PENDING` if the shipping phone (`9811087654`) is in `client_whitelisted_numbers` for channel `shopify` (demo override in `event_processor.py`). Otherwise you get `PAYMENT_PENDING_UNFULFILLED`.

**Unfulfilled payloads:** `fulfillment_status` is `null`, `fulfillments` is `[]`, line item `fulfillment_status` is `null`.

Test one file at a time in Postman. Change `X-Shopify-Webhook-Id` on each request to avoid dedup if re-testing the same event.
