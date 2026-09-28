# Design: Carry the Original Order's Payment Reference into the Cloned Order's Tags

## Overview

When the bot updates **size / email / phone / address / name** on a prepaid (or
partially-paid) order via the **cancel-and-recreate** strategy, the original
Shopify order is cancelled and a brand-new order is cloned in its place. The new
order gets a **new Shopify order id** and a **fresh transaction list** — the link
back to the customer's *actual money* (the gateway payment that was captured on
the original order) is lost from the new order's own record.

This design adds that link: extract the **Shopify transaction id** and the
**gateway payment id** from the original order's transaction and stamp them onto
the **new order's `tags`** so finance/ops can reconcile the new order against the
real payment, and reporting can pair "new order ↔ original captured payment".

- **Scope**: **both** cancel-and-recreate clone paths —
  (1) `CancelAndRecreateOrchestrator.aupdate_via_clone` (address / phone / email
  / name, and size when routed here), tagged `CANCEL_AND_RECREATE`; and
  (2) the size-change clone in `shopify/modules/order_editing_graphql.py`
  (`_acancel_and_recreate_order_with_new_size`), tagged `SIZE_CHANGE_CLONED`.
  Both call `aclone_order`. Direct order creation and the in-place GraphQL
  size-edit (no clone) are unaffected.
- **Non-goals**: no change to refund logic, no change to how payments are
  *re-attached* to the clone (the existing `transactions=[{kind:"sale",…}]`
  re-credit is untouched). We only **annotate** the new order with the original
  payment reference.

> **Note (post-deploy fix):** the size update has its *own* clone module
> (`order_editing_graphql.py`) separate from the orchestrator. The first cut
> only patched the orchestrator, so size-change clones (tagged
> `SIZE_CHANGE_CLONED`, e.g. gv15787→gv15788) shipped without the payment tags.
> Both clone sites in that module now append `payment_ref_tags` too.

---

## Current Flow (verified)

| Step | Code | Notes |
|------|------|-------|
| Entry | `CancelAndRecreateOrchestrator.aupdate_via_clone()` — `core/orchestrator.py:4229` | Dispatched from `update_order_{address,phone,email,name,size}` tools. |
| Fetch original | `order_service.aget_order_details(order_id)` → `_aresolve_order_record` — `shopify/tools/order_adapter.py:162` | Uses REST order **search/get**. **Does NOT include the `transactions` array.** |
| Classify payment | `classify_payment_type(raw_order)` — `utils/order_utils.py:5` | Reads `financial_status`, `total_price`, `total_outstanding`. |
| Cancel original | `order_service.acancel_order(..., skip_refund=True)` | Preserves customer money. |
| Clone | `order_service.aclone_order(..., additional_tags=[OrderTag.CANCEL_AND_RECREATE, OrderTag.BLOOMERCE_UPDATED], transactions=clone_transactions, …)` — `orchestrator.py:4390` | |
| Tag write | `aclone_order` — `order_adapter.py:794-828` | Merges `additional_tags` into the original comma-separated `tags` → `order_payload["tags"]`. |

**Key insight**: the new order's tags are simply the original tags **plus**
`additional_tags`. So the entire feature reduces to *appending two more strings
to the `additional_tags` list* — **no change to `aclone_order`'s signature or
payload shape.**

**Second key insight**: `raw_order` does **not** carry transactions, so we must
fetch them separately (exactly as the refund flow already does at
`order_adapter.py:1628-1640`).

---

## Shopify Transaction Field Names (cross-checked)

Source: live `OrderTransaction` GraphQL schema + REST `/transactions.json`
reference (`shopify.dev/docs/api/admin-rest/latest/resources/transaction`). The
codebase fetches the **REST** endpoint, so REST field names apply.

The two ids come from **two different places** — verified against live order
`#gv15779` (PayU prepaid, imported via groove_fastrr):

### (a) Shopify transaction id — from the transactions API

| Concept | REST field | `#gv15779` value | Notes |
|---------|-----------|------------------|-------|
| **Shopify transaction id** | `id` | `8404506214722` | Numeric. From `GET /orders/{id}/transactions.json`. GraphQL equiv: `gid://shopify/OrderTransaction/8404506214722`. |
| Gateway name | `gateway` | `PayU` | |
| Kind | `kind` | `sale` | Parent selection (REST is lowercase). |
| Status | `status` | `success` | Parent selection. |
| Shopify payment id | `payment_id` | `#gv15779.1` | Shopify's *internal* order-scoped id — **not** the gateway's reference. **Not used.** |

Parent-transaction selection reuses the exact predicate the refund flow already
trusts (`order_adapter.py:1637-1640`): `kind in ("capture","sale") and status == "success"`.

### (b) Gateway payment id — from order `note_attributes`

**Critical finding**: the merchant's real gateway reference (e.g.
`PayU_txn_id = 29092610009`) is **not** on the transaction object. The
transaction's `authorization`/`payment_id` only carry Shopify-internal values
(`#gv15779.1`). The gateway reference is stored in the order's
**`note_attributes`** (Shopify admin "Additional details"), written by the
groove_fastrr / checkout import:

| Source | REST field | `#gv15779` |
|--------|-----------|------------|
| Order `note_attributes` | list of `{"name","value"}` | `{"name":"PayU_txn_id","value":"29092610009"}` |

Keys vary by gateway, so we match an **explicit curated set** of known keys
(case-insensitive) and **preserve the original key name** in the tag — so the
gateway and id-type stay legible. **Every** matching key on the order is emitted
(an order can carry both a payment id and an order id). Curated set:

| Gateway | note_attribute key(s) |
|---------|------------------------|
| PayU | `PayU_txn_id` |
| Razorpay | `razorpay_payment_id`, `razorpay_order_id` |
| Cashfree | `cf_payment_id`, `cf_order_id` |
| PhonePe | `transactionId`, `phonepe_transaction_id`, `merchantTransactionId` |
| Paytm | `TXNID`, `ORDERID` |
| Easebuzz | `easebuzz_payment_id` |
| CCAvenue | `tracking_id` |
| Instamojo | `payment_id` |

(An earlier draft used a loose regex; replaced with this explicit list per
review — the regex both over-matched and missed the order-id–style keys.)

`note_attributes` is already present on `raw_order` (the order search at
`order_adapter.py:149` applies no `fields=` filter), so reading the gateway ids
needs **no extra API call** — only the transaction id needs the transactions
fetch. Per-attribute key is `name`/`value` in REST (confirmed against the
existing webhook parser at `shopify_webhook.py:227`).

The transaction `receipt` field is intentionally excluded (Shopify documents it
as "not a stable contract").

---

## Proposed Change (minimal footprint)

Three small, isolated edits. Net new logic lives in pure/`utils` + adapter
helpers; the hot orchestrator file gains ~4 lines.

### 1. Tag prefixes — `shopify/order_tags.py`

The values are **dynamic** (one per order), so they can't be `OrderTag` enum
members. Add grep-able module-level prefix constants next to `OrderTag`, keeping
the "no bare string literals for tags" rule:

```python
# All share the "orig_" namespace. The Shopify transaction id uses a fixed key;
# gateway references preserve their original note_attribute key name, e.g.
# "orig_txn_id:8404506214722", "orig_PayU_txn_id:29092610009".
PAYMENT_REFERENCE_TAG_PREFIX = "orig_"
PAYMENT_TXN_TAG_PREFIX = PAYMENT_REFERENCE_TAG_PREFIX + "txn_id:"  # Shopify OrderTransaction.id
```

> Tag format note: Shopify tags are **comma-separated**, max 255 chars each, and
> a tag may not contain a comma. `key:value` with a colon is safe and
> searchable (`tag:'orig_PayU_txn_id:29092610009'`). Ids are alphanumeric, so no
> escaping is required. We still defensively drop any value containing a comma.

### 2. Pure helper — `utils/order_utils.py`

Stateless, side-effect-free, unit-testable (AGENTS.md "pure helpers / shared
utilities"). `build_payment_reference_tags` takes the transactions list **and**
the order's `note_attributes`. `extract_gateway_payment_reference_tags` emits one
`orig_<key>:<value>` tag per known gateway key found, preserving the key name:

```python
from fashion_bot.shopify.order_tags import (
    PAYMENT_REFERENCE_TAG_PREFIX, PAYMENT_TXN_TAG_PREFIX,
)

# Known gateway payment-reference note_attribute keys (lower-cased for matching).
_GATEWAY_PAYMENT_NOTE_KEYS = frozenset({
    "payu_txn_id", "razorpay_payment_id", "razorpay_order_id",
    "cf_payment_id", "cf_order_id", "transactionid", "phonepe_transaction_id",
    "merchanttransactionid", "txnid", "orderid", "easebuzz_payment_id",
    "tracking_id", "payment_id",
})

def extract_gateway_payment_reference_tags(note_attributes: list) -> list[str]:
    """orig_<key>:<value> for every known gateway key on the order. REST key is
    `name`. Skips empty/comma values; de-dups. PayU_txn_id → orig_PayU_txn_id:…"""
    tags = []
    for attr in (note_attributes or []):
        if not isinstance(attr, dict):
            continue
        name = str(attr.get("name") or "").strip()
        if name.lower() not in _GATEWAY_PAYMENT_NOTE_KEYS:
            continue
        value = str(attr.get("value") or "").strip()
        if not value or "," in value:
            continue
        tag = f"{PAYMENT_REFERENCE_TAG_PREFIX}{name}:{value}"
        if tag not in tags:
            tags.append(tag)
    return tags

def build_payment_reference_tags(transactions, note_attributes=None) -> list[str]:
    """orig_txn_id:<txn.id> + one orig_<key>:<value> per known gateway note key.
    Each omitted when its source is absent (COD / no gateway note)."""
    tags = []
    parent = next(
        (t for t in (transactions or [])
         if t.get("kind") in ("capture", "sale") and t.get("status") == "success"),
        None,
    )
    if parent and parent.get("id") not in (None, ""):
        tags.append(f"{PAYMENT_TXN_TAG_PREFIX}{parent['id']}")
    tags.extend(extract_gateway_payment_reference_tags(note_attributes))
    return tags
```

> Worked example — `#gv15779` →
> `["orig_txn_id:8404506214722", "orig_PayU_txn_id:29092610009"]`.

### 3. Adapter fetch helper — `shopify/tools/order_adapter.py`

The orchestrator must not make raw HTTP calls (vendor logic belongs in the
adapter). Add a thin async method that returns the transactions list, paced
through the existing rate-limit/`_get_headers` plumbing the refund flow already
uses. It takes the **numeric** Shopify order id — exactly what the refund flow
uses (`numeric_id = order["id"]`, `order_adapter.py:1616`) — so the caller reuses
the already-fetched `raw_order["id"]` and we avoid a second order lookup:

```python
async def aget_order_transactions(
    self, numeric_order_id: Any, state: Optional[Dict] = None,
) -> List[Dict[str, Any]]:
    """Fetch the REST transactions for an order. [] on any failure (fail-open)."""
    try:
        config = await self._aget_config(state)
        access_token, shop_url = config.get("access_token"), config.get("shop_url")
        api_version = config.get("api_version", "2024-04")
        if not access_token or not shop_url or not numeric_order_id:
            return []
        client = await get_shared_async_http_client()
        url = f"https://{shop_url}/admin/api/{api_version}/orders/{numeric_order_id}/transactions.json"
        resp = await client.get(url, headers=self._get_headers(access_token), timeout=30)
        self._raise_for_shopify_response(resp)
        return resp.json().get("transactions", [])
    except ShopifyRateLimitError:
        raise
    except Exception as exc:
        log_with_trace_id(state, f"aget_order_transactions failed for {numeric_order_id}: {exc}", "warning")
        return []
```

> This is the same URL + headers + `_raise_for_shopify_response` the refund flow
> already issues at `order_adapter.py:1628-1633` — just lifted into a reusable
> method that the refund flow can later adopt (see *Optional follow-up*).

### 4. Wire into the clone path — `core/orchestrator.py`

In `aupdate_via_clone`, **after** the original order is fetched and **before**
the `aclone_order` call (it can run right after `classify_payment_type`, while
the original order still exists), fetch transactions and extend the tag list.
Only ~4 lines, and it touches only the `additional_tags` argument already there:

```python
from fashion_bot.utils.order_utils import (
    classify_payment_type, build_payment_reference_tags,
)

# … after classify_payment_type(raw_order):
payment_ref_tags: List[str] = []
if float(amount_paid) > 0:                      # COD has no captured txn to pin
    txns = await order_service.aget_order_transactions(raw_order.get("id"), state=state)
    payment_ref_tags = build_payment_reference_tags(
        txns, note_attributes=raw_order.get("note_attributes"),
    )

# … in the aclone_order call:
additional_tags=[OrderTag.CANCEL_AND_RECREATE, OrderTag.BLOOMERCE_UPDATED] + payment_ref_tags,
```

Fetch transactions **before** cancelling is preferable but not required — the
transactions stay readable on a cancelled order. Placing it right after
`classify_payment_type` keeps the read next to the other payment reads and gives
one clean `if amount_paid > 0` gate shared with the existing
`clone_transactions` block.

---

## Result

A clone produced by an address/phone/email/name/size update on a prepaid order
carries, e.g.:

```
tags: "...existing tags..., CANCEL_AND_RECREATE, BLOOMERCE_UPDATED,
       orig_txn_id:8404506214722, orig_PayU_txn_id:29092610009"
```

Searchable in Shopify admin as `tag:'orig_PayU_txn_id:29092610009'`. A Razorpay
order would instead carry `orig_razorpay_payment_id:…` (and
`orig_razorpay_order_id:…` if present).

---

## Why this is safe (AGENTS.md alignment)

- **Minimal footprint**: no signature changes; orchestrator hot-file gains ~4
  lines; new logic isolated in `utils` (pure) + adapter (vendor).
- **Fully async**: `aget_order_transactions` is `async`/`await`; no blocking I/O.
- **Stateless tools / pure helpers**: `build_payment_reference_tags` has no side
  effects; the adapter method only reads.
- **Graceful degradation / idempotent**: transaction fetch is **fail-open**
  (`[]` on error) → if Shopify is slow or the order has no parent txn, the clone
  proceeds exactly as today with just the two static tags. Re-running produces
  the same tag set (dedup is handled — `aclone_order` skips tags already
  present, `order_adapter.py:796-798`).
- **No tenant leakage**: reads/writes scoped to the same `order_service`
  (already `client_id`-scoped via `state`).
- **Shopify rate limiting**: the new GET reuses the adapter's existing
  `_get_headers` + shared client path used by every other call (same as the
  refund flow). Pace through `rate_limit_key=shop` consistent with neighbours.
- **No bare string tags**: prefixes live in `order_tags.py`.

## Failure modes

| Case | Behaviour |
|------|-----------|
| COD order (`amount_paid == 0`) | Fetch skipped; no payment tags (nothing to pin). |
| Transactions endpoint errors / times out | `[]` → only the gateway note tag (if any) is added. No exception bubbles. |
| No `success` sale/capture txn (auth-only, manual) | No `orig_txn_id:` tag; gateway note tags still added if present. |
| No known gateway key in `note_attributes` | No `orig_<gateway>:` tags; `orig_txn_id:` still added if a txn exists. |
| Multiple known keys (e.g. `cf_payment_id` + `cf_order_id`) | **All** are emitted as separate tags (de-duplicated). |
| Value contains a comma | That tag is skipped (protects the comma-delimited tag string). |

## Testing

- **Unit (pure)** for `build_payment_reference_tags` / `extract_gateway_payment_reference_tags`:
  real `#gv15779` data → `[orig_txn_id:8404506214722, orig_PayU_txn_id:29092610009]`;
  razorpay/cashfree/paytm/phonepe/instamojo/ccavenue keys each → `orig_<key>:<val>`
  (both ids emitted where present); no parent txn → gateway-only; no known key →
  txn-only; comma/empty value → dropped; COD → `[]`.
- **Adapter**: mock `transactions.json` → assert list returned; non-200 →
  `[]`; rate-limit error re-raised.
- **Integration**: cancel-and-recreate on a staging prepaid order; assert the
  new order's `tags` contain `orig_txn_id:` and (if the order carries a known
  gateway note_attribute) the corresponding `orig_<key>:` tag.

## Optional follow-up (not required, keeps blast radius small)

The refund flow (`order_adapter.py:1628-1640`) inlines the same transactions
fetch + parent-selection. Once this lands, it can be refactored to call
`aget_order_transactions` + the shared parent predicate to remove the
duplication (AGENTS.md "Shared Utilities Over Duplication"). Deferred here so the
payment-tag change carries zero risk to the refund path.

---

## Files Touched

| File | Change | LOC |
|------|--------|-----|
| `fashion_bot/shopify/order_tags.py` | prefix constants | +3 |
| `fashion_bot/utils/order_utils.py` | `build_payment_reference_tags()` + `extract_gateway_payment_reference_tags()` + key set | +70 |
| `fashion_bot/shopify/tools/order_adapter.py` | `aget_order_transactions()` | +20 |
| `fashion_bot/core/orchestrator.py` | fetch + extend `additional_tags` (address/phone/email/name path) | +4 |
| `fashion_bot/shopify/modules/order_editing_graphql.py` | fetch + extend `additional_tags` at both size-clone sites | +20 |

## Document Info

- **Status**: Proposed (design only — no code changed)
- **Created**: 2026-06-16
- **Scope**: `CancelAndRecreateOrchestrator.aupdate_via_clone` (size/email/phone/address/name updates)
