# Design Document: Payment-Aware Product Change Flow

## Overview

The `change_order_product_tool` preserves the original payment type and handles payment differentials intelligently. When a product change occurs, the system classifies the payment type (COD, prepaid, partial_prepaid), cancels the original order, and creates a new order with the correct `financial_status` and `payment_gateway_names` carried over from the original.

---

## Implementation Status

| Component | Status | Files |
|-----------|--------|-------|
| Payment classification (`_classify_payment_type`) | ✅ Implemented | `tool_factory.py` (line ~6902) |
| Product price retrieval (`_get_product_price_from_url`) | ✅ Implemented | `tool_factory.py` (line ~6949) |
| Scenario 1: COD → COD | ✅ Implemented | `tool_factory.py` (line ~7503) |
| Scenario 2: Customer owes more (COD with differential at delivery) | ✅ Implemented | `tool_factory.py` (line ~7563) |
| Scenario 3: Refund due + create paid order | ✅ Implemented | `tool_factory.py` (line ~7644) |
| Scenario 4: Same price direct swap | ✅ Implemented | `tool_factory.py` (line ~7735) |
| Partial refund processing (`_process_partial_refund`) | ✅ Implemented | `tool_factory.py` |
| Draft order with credit (`_create_draft_order_with_credit`) | ⚠️ Deprecated (no longer used) | `tool_factory.py` (kept for reference) |
| **Prepaid financial_status preservation** | ✅ **Fixed (Feb 2026)** | `order_creation_api.py`, `orchestrator.py`, `tool_factory.py` |
| Phone validation (embedded) | ✅ Implemented | `tool_factory.py` |
| Error handling & partial failures | ✅ Implemented | `tool_factory.py` |

---

## Flow (Implemented)

```
1. Validate phone access (embedded in tool)
2. Get original order details
3. Check order eligibility (not shipped, not cancelled)
4. Fetch new product price from URL
5. Classify payment type (cod / prepaid / partial_prepaid)
6. Calculate payment differential
7. Cancel original order immediately
8. Branch based on payment type & differential:
   ├─ COD → Create new COD order (financial_status="pending")
   ├─ Prepaid/Partial (paid < new price) → Create new COD order (differential to be paid at delivery)
   ├─ Prepaid/Partial (paid > new price) → Process refund first → Create new order (financial_status="paid")
   └─ Prepaid/Partial (paid == new price) → Create new order (preserve original financial_status & payment_gateway_names)
9. Add tags: USER_REQUESTED_PRODUCT_CHANGE, BOT_CREATED
10. Add order note on new order: "Product change from order {old_order}. Original product: {name}. Customer requested product change via bot."
```

---

## Payment Type Detection

### From Shopify Order Data

```python
# Fields to check in order_data:
financial_status: str  # "paid", "partially_paid", "pending", "refunded"
payment_gateway_names: List[str]  # ["Cash on Delivery (COD)"] or ["Razorpay", "Stripe", etc.]
total_price: str  # Total order amount
total_outstanding: str  # Amount still owed (0 for prepaid)

# Transactions (if available):
transactions: [
    {
        "kind": "sale",  # or "capture", "authorization"
        "status": "success",
        "amount": "999.00",
        "gateway": "razorpay"
    }
]
```

### Payment Type Classification Logic (Implemented)

Located in `tool_factory.py` as `_classify_payment_type()`:

```python
def _classify_payment_type(order_data: dict) -> dict:
    """
    Classify order payment type and calculate amounts paid/outstanding.
    
    Returns:
        {
            "payment_type": "cod" | "prepaid" | "partial_prepaid",
            "total_price": Decimal,
            "amount_paid": Decimal,
            "amount_outstanding": Decimal
        }
    """
    financial_status = (order_data.get("financial_status") or "pending").lower()
    total_price = Decimal(str(order_data.get("total_price", "0")))
    
    # Fully paid = prepaid
    if financial_status == "paid":
        return {
            "payment_type": "prepaid",
            "total_price": total_price,
            "amount_paid": total_price,
            "amount_outstanding": Decimal("0")
        }
    
    # Partially paid = partial_prepaid (some prepaid + some COD)
    if financial_status == "partially_paid":
        outstanding = Decimal(str(order_data.get("total_outstanding", "0")))
        return {
            "payment_type": "partial_prepaid",
            "total_price": total_price,
            "amount_paid": total_price - outstanding,
            "amount_outstanding": outstanding  # This COD portion carries over
        }
    
    # Pending, unpaid, etc. = COD
    return {
        "payment_type": "cod",
        "total_price": total_price,
        "amount_paid": Decimal("0"),
        "amount_outstanding": total_price
    }
```

> **Note**: The actual implementation uses `financial_status` from Shopify directly (not `payment_gateway_names`) for classification. This is more reliable since `financial_status` is the source of truth for whether money has been collected.

---

## Scenario Handling

### Scenario 1: COD → COD

**Condition**: `payment_type == "cod"`

**Flow**:
1. Cancel original order immediately
2. Create new COD order
3. Add tags: `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`
4. Return success

**No changes needed** - this is the current flow (just add tags).

---

### Scenario 2: Prepaid/Partial + Customer Owes More → COD Order

**Condition**: `payment_type in ["prepaid", "partial_prepaid"] AND amount_paid < new_product_price`

**Example**:
- Original order: ₹999 (fully paid)
- New product: ₹1,299
- Differential: ₹300 (customer owes)

**Flow**:
1. Calculate differential: `new_price - amount_paid`
2. **Cancel original order immediately**
3. **Create new COD order** (NOT a draft order)
4. Inform customer about the differential amount to be paid at delivery
5. Add tags: `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`, `DIFFERENTIAL_COD`

> **Design Decision (Feb 2026)**: Previously, Scenario 2 created a Draft Order with credit applied as discount and sent a payment link. This was changed to a simpler COD approach: the new order is placed in COD mode and the customer is told to pay the differential amount at the time of delivery. This avoids draft order complexity, payment link expiry issues, and webhook dependencies.

**For Partial Prepaid**: The total COD at delivery includes both the differential and the original outstanding COD amount.

**Implementation** (in `tool_factory.py`):
```python
# Scenario 2: Customer owes more → COD order with differential at delivery
if differential > 0:
    # Cancel original order
    cancel_result = cancel_order_from_data(...)
    
    # Create new COD order (no draft order, no payment link)
    create_result = OrderCreationOrchestrator.create_order_in_shopify(
        product_link=new_product_url,
        quantity=quantity,
        phone_number=phone_number,
        customer_name=customer_name,
        customer_address=customer_address,
        state=state
        # No financial_status or payment_gateway_names → defaults to COD
    )
    
    response = {
        "differential_amount": float(differential),
        "differential_collection": "at_delivery",
        "message": f"... extra amount of ₹{differential} will need to be paid at the time of delivery."
    }
```

**Customer Response**:
```
Your product has been changed successfully! ✅

Old order {old_order_id} has been cancelled.
New order {new_order_id} has been created.

You had previously paid ₹999.
The extra amount of ₹300 will need to be paid at the time of delivery.
```

**For Partial Prepaid Additional Message**:
```
Note: COD amount of ₹{cod_amount} will be collected at delivery.
```

---

### Scenario 3: Prepaid/Partial + Refund Due

**Condition**: `payment_type in ["prepaid", "partial_prepaid"] AND amount_paid > new_product_price`

**Example**:
- Original order: ₹1,299 (fully paid)
- New product: ₹999
- Refund due: ₹300

**Flow**:
1. Calculate refund: `amount_paid - new_price`
2. Cancel original order immediately
3. **Process refund FIRST via Shopify Refund API**
4. After refund initiated, create new prepaid order (marked as paid)
5. Add tags: `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`
6. Return success with refund details

**Refund via Shopify Refund API**:
```python
def process_partial_refund(order_id: str, refund_amount: Decimal, state: dict) -> dict:
    """
    Process partial refund via Shopify Admin API.
    
    Returns:
        {
            "success": bool,
            "refund_id": str,
            "amount": Decimal,
            "status": str
        }
    """
    # Get original order to find transaction details
    order_data = get_shopify_order(order_id, state)
    
    # Find the successful payment transaction
    transactions = order_data.get("transactions", [])
    parent_transaction = next(
        (t for t in transactions if t.get("kind") in ["sale", "capture"] and t.get("status") == "success"),
        None
    )
    
    if not parent_transaction:
        return {"success": False, "error": "No valid transaction found for refund"}
    
    # Create refund via Shopify API
    # POST /admin/api/2024-04/orders/{order_id}/refunds.json
    refund_payload = {
        "refund": {
            "note": f"Refund for product change. Customer requested different product.",
            "notify": True,  # Send email notification to customer
            "shipping": {"full_refund": False},
            "transactions": [{
                "parent_id": parent_transaction["id"],
                "amount": str(refund_amount),
                "kind": "refund",
                "gateway": parent_transaction.get("gateway")
            }]
        }
    }
    
    response = shopify_admin_api_call(
        f"/orders/{order_id}/refunds.json",
        method="POST",
        data=refund_payload
    )
    
    return {
        "success": True,
        "refund_id": response.get("refund", {}).get("id"),
        "amount": refund_amount,
        "status": "initiated"
    }
```

**New Order Creation (marked as paid)** — ✅ Implemented:
```python
# Original payment gateways preserved from order_data
original_gateways = order_data.get("payment_gateway_names", [])

new_order = OrderCreationOrchestrator.create_order_in_shopify(
    product_link=new_product_url,
    quantity=quantity,
    requested_size="",
    phone_number=phone_number,
    customer_name=customer_name,
    customer_address=customer_address,
    state=state,
    financial_status="paid",  # Explicitly mark as paid
    payment_gateway_names=original_gateways if original_gateways else None
)
```

> **Bug Fix (Feb 2026)**: Previously, `create_order_in_shopify` hardcoded `financial_status="pending"` and `payment_gateway_names=["Cash on Delivery (COD)"]` in `_assemble_order_data()`. This caused prepaid orders to be created as COD. Fixed by threading `financial_status` and `payment_gateway_names` through the entire chain: `tool_factory.py` → `orchestrator.py` → `order_creation_api.py` → `_assemble_order_data()`.

**Customer Response**:
```
Your product change is complete! ✅

Original order {old_order_id} cancelled.
New order {new_order_id} created and confirmed.

Refund of ₹300 has been initiated to your original payment method.
Expected refund timeline: 5-7 business days.
```

---

### Scenario 4: Prepaid/Partial + Same Price — ✅ Implemented

**Condition**: `payment_type in ["prepaid", "partial_prepaid"] AND amount_paid == new_product_price`

**Flow**:
1. Cancel original order immediately (no refund needed)
2. Create new order directly (NOT draft order)
3. **Preserve original `financial_status` and `payment_gateway_names`** from original order
4. Add tags: `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`
5. Return success

**Implementation**:
```python
# Preserve original payment info exactly
original_gateways = order_data.get("payment_gateway_names", [])
original_financial_status = order_data.get("financial_status", "pending")

create_result = OrderCreationOrchestrator.create_order_in_shopify(
    product_link=new_product_url,
    quantity=quantity,
    requested_size="",
    phone_number=phone_number,
    customer_name=customer_name,
    customer_address=customer_address,
    state=state,
    financial_status=original_financial_status,
    payment_gateway_names=original_gateways if original_gateways else None
)
```

**No Draft Order** - Direct order creation since no payment differential exists.

**For Partial Prepaid**: COD portion carries over unchanged.

**Customer Response**:
```
Your product change is complete! ✅

Original order {old_order_id} cancelled.
New order {new_order_id} created and confirmed.

Your previous payment of ₹999 has been applied to the new order.
```

**For Partial Prepaid Additional Message**:
```
COD amount of ₹{cod_amount} will be collected at delivery (same as original order).
```

---

## New Product Price Retrieval

Need to fetch new product price before proceeding:

```python
def get_product_price_from_url(product_url: str, state: dict) -> dict:
    """
    Fetch product price from URL.
    
    Returns:
        {
            "success": bool,
            "price": Decimal,
            "compare_at_price": Decimal | None,
            "variant_id": str,
            "product_id": str,
            "product_title": str
        }
    """
    # Use existing product fetching logic from product_details_tools
    # Extract price from variant data
```

---

## Updated Tool Signature

```python
@tool
def change_order_product_tool(order_id: str, new_product_url: str) -> dict:
    """
    Change product or color for an existing order.
    Preserves original payment type and handles payment differentials.
    
    Payment Handling:
    - COD orders → New COD order created
    - Prepaid (customer owes more) → Cancels order, returns payment link for differential (24hr expiry)
    - Prepaid (refund due) → Processes refund first, then creates paid order
    - Prepaid (same price) → Direct swap, creates paid order
    - Partial Prepaid → COD portion carries over to new order
    
    Tags Added: USER_REQUESTED_PRODUCT_CHANGE, BOT_CREATED
    
    Args:
        order_id: Order ID to change (e.g., 'gv10741')
        new_product_url: Full URL of the new product
    
    Returns:
        - For COD: {"success": True, "new_order_id": "..."}
        - For differential payment: {"success": True, "payment_required": True, "payment_link": "...", "amount": 300, "expires_in": "24 hours"}
        - For refund: {"success": True, "refund_amount": 300, "new_order_id": "..."}
        - For same price: {"success": True, "new_order_id": "..."}
    """
```

---

## Return Value Structures

### COD Success
```python
{
    "success": True,
    "payment_type": "cod",
    "old_order_id": "GV10741",
    "old_order_cancelled": True,
    "new_order_id": "GV10742",
    "tags_added": ["USER_REQUESTED_PRODUCT_CHANGE", "BOT_CREATED"],
    "message": "Product changed successfully! New order GV10742 created."
}
```

### Differential Payment Required (Prepaid/Partial - Customer Owes More)
```python
{
    "success": True,
    "payment_required": True,
    "payment_type": "prepaid",  # or "partial_prepaid"
    "old_order_id": "GV10741",
    "old_order_cancelled": True,
    "draft_order_id": "D123456",
    "payment_link": "https://shop.com/checkouts/...",
    "payment_link_expires_in": "24 hours",
    "original_amount_paid": 999.00,
    "new_product_price": 1299.00,
    "credit_applied": 999.00,
    "differential_amount": 300.00,
    "cod_amount_carryover": 0.00,  # Non-zero for partial_prepaid
    "tags_to_add": ["USER_REQUESTED_PRODUCT_CHANGE", "BOT_CREATED"],
    "message": "Original order cancelled. Credit of ₹999 applied. Please pay ₹300 within 24 hours to confirm new order."
}
```

### Refund Due (Prepaid/Partial - Customer Owed)
```python
{
    "success": True,
    "refund_processed": True,
    "payment_type": "prepaid",  # or "partial_prepaid"
    "old_order_id": "GV10741",
    "old_order_cancelled": True,
    "new_order_id": "GV10742",
    "original_amount_paid": 1299.00,
    "new_product_price": 999.00,
    "refund_amount": 300.00,
    "refund_id": "R123456",
    "refund_status": "initiated",
    "refund_timeline": "5-7 business days",
    "cod_amount_carryover": 0.00,  # Non-zero for partial_prepaid
    "tags_added": ["USER_REQUESTED_PRODUCT_CHANGE", "BOT_CREATED"],
    "message": "Product changed! Refund of ₹300 initiated. New order GV10742 confirmed."
}
```

### Same Price (Prepaid/Partial - No Differential)
```python
{
    "success": True,
    "payment_type": "prepaid",  # or "partial_prepaid"
    "old_order_id": "GV10741",
    "old_order_cancelled": True,
    "new_order_id": "GV10742",
    "original_amount_paid": 999.00,
    "new_product_price": 999.00,
    "credit_applied": 999.00,
    "cod_amount_carryover": 0.00,  # Non-zero for partial_prepaid
    "tags_added": ["USER_REQUESTED_PRODUCT_CHANGE", "BOT_CREATED"],
    "message": "Product changed successfully! Previous payment applied. New order GV10742 confirmed."
}
```

---

## Edge Cases & Error Handling

| Scenario | Handling |
|----------|----------|
| New product out of stock | Return error before any cancellation |
| New product price fetch fails | Return error before any cancellation |
| Draft order creation fails | Return error (original already cancelled - flag for manual intervention) |
| Payment link generation fails | Return error with draft order ID for manual follow-up |
| Refund API fails | Return error, suggest manual refund, still create new order |
| Original order already cancelled | Return error |
| Original order already shipped | Return error (can't change shipped orders) |
| New product price = 0 (free item) | Block - return error "Cannot change to free items" |
| Payment link expires (24 hours) | Draft order remains - customer can request new link or cancel |

**Critical Error Handling** (when original order already cancelled but subsequent step fails):
```python
{
    "success": False,
    "partial_failure": True,
    "old_order_cancelled": True,
    "old_order_id": "GV10741",
    "error": "Failed to create draft order after cancellation",
    "requires_manual_intervention": True,
    "customer_credit": 999.00,  # Amount to be credited back or applied
    "message": "Original order cancelled but new order creation failed. Our team will contact you to resolve this."
}
```

---

## Order Tags Strategy

### Tags to Add on New Order
- `USER_REQUESTED_PRODUCT_CHANGE` - Indicates this order was created via product change flow
- `BOT_CREATED` - Indicates order was created by the bot
- `changed_from_{original_order_id}` - Links back to original order

### Tags to Add on Original Order (before cancellation)
- `PRODUCT_CHANGE_CANCELLED` - Indicates why order was cancelled
- `changed_to_{new_order_id}` - Links to new order (if created successfully)

### Notes to Add
**Original Order Note**:
```
Order cancelled at customer request for product change.
New order: {new_order_id} (or "Draft order: {draft_order_id} - pending payment")
Cancelled by: Bot
Timestamp: {ISO timestamp}
```

**New Order Note** (✅ set at order creation time via `note` param):

| Scenario | Note Content |
|----------|-------------|
| COD → COD | `Product change from order {old}. Original product: {name}. Customer requested product change via bot.` |
| Customer owes more (COD differential) | `Product change from order {old}. Original product: {name}. Previously paid ₹{paid}. Differential ₹{diff} to be collected at delivery. Customer requested product change via bot.` |
| Refund due | `Product change from order {old}. Original product: {name}. Refund of ₹{refund} processed to customer. Customer requested product change via bot.` |
| Same price swap | `Product change from order {old}. Original product: {name}. Same price swap - previous payment of ₹{paid} applied. Customer requested product change via bot.` |

---

## Dependencies

### Existing Functions Reused
- `_get_shopify_order` - Fetch order details (in `tool_factory.py`)
- `cancel_order_from_data` - Cancel order in Shopify + Shiprocket
- `OrderCreationOrchestrator.create_order_in_shopify` - For COD and prepaid orders (updated to accept `financial_status`, `payment_gateway_names`, and `note`)

### Functions Implemented

1. **`_classify_payment_type(order_data: dict) -> dict`** — `tool_factory.py` line ~6902
   - Determines payment type (cod/prepaid/partial_prepaid) from `financial_status`
   - Calculates amount_paid and amount_outstanding

2. **`_get_product_price_from_url(url: str, state: dict) -> dict`** — `tool_factory.py` line ~6949
   - Fetches new product variant and price via `ProductOrchestrator.get_product_details_from_url()`
   - Returns variant_id, price, product details

3. **`_process_partial_refund(order_id: str, amount: Decimal, order_data: dict, state: dict) -> dict`** — `tool_factory.py`
   - Processes refund via Shopify Refund API
   - Returns refund_id and status; gracefully handles failures

4. **`_create_draft_order_with_credit(new_product_info, original_order_data, credit_amount, ...) -> dict`** — `tool_factory.py`
   - Creates draft order with applied discount (credit from cancelled order)
   - Generates 24-hour payment link via `create_draft_order_for_prepaid()`
   - Handles COD carryover for partial prepaid

---

## Key Architecture Change: Financial Status Propagation

### Problem Identified (Feb 2026)

When a prepaid order undergoes a product change, the new order was being created as **COD/pending** instead of **prepaid/paid**. Root cause: `_assemble_order_data()` in `order_creation_api.py` hardcoded:

```python
"financial_status": "pending",
"payment_gateway_names": ["Cash on Delivery (COD)"],
```

### Fix: Thread Financial Status Through the Chain

The fix adds optional `financial_status`, `payment_gateway_names`, and `note` parameters through the entire call chain:

```
tool_factory.py (Scenario 1/2/3/4)
    ↓ passes financial_status + payment_gateway_names + note
OrderCreationOrchestrator.create_order_in_shopify()  [orchestrator.py]
    ↓ passes financial_status + payment_gateway_names + note
OrderCreationAPI.create_order()  [order_creation_api.py]
    ↓ passes financial_status + payment_gateway_names + note
_assemble_order_data()  [order_creation_api.py]
    ↓ uses effective_financial_status = financial_status or "pending"
    ↓ uses effective_payment_gateways = payment_gateway_names or ["Cash on Delivery (COD)"]
    ↓ sets order_data["note"] = note (if provided)
Shopify REST API  →  Order created with correct payment status and note
```

**Files Modified**:
| File | Change |
|------|--------|
| `order_creation_api.py` | `create_order()` and `_assemble_order_data()` accept optional `financial_status`, `payment_gateway_names`, and `note`; default to COD if not provided |
| `orchestrator.py` | `create_order_in_shopify()` accepts and passes through `financial_status`, `payment_gateway_names`, and `note` |
| `tool_factory.py` (Scenario 1) | Passes `note` with product change context |
| `tool_factory.py` (Scenario 2) | Passes `note` with differential/COD context |
| `tool_factory.py` (Scenario 3) | Passes `financial_status="paid"`, original `payment_gateway_names`, and `note` with refund context |
| `tool_factory.py` (Scenario 4) | Passes original `financial_status`, `payment_gateway_names`, and `note` with same-price swap context |

**Backward Compatible**: Regular order creation (not product change) passes `None` for both params, so they default to COD as before.

---

## Implementation Phases

### Phase 1: Payment Classification & Price Retrieval — ✅ Complete
- Added `_classify_payment_type()` helper function in `tool_factory.py`
- Added `_get_product_price_from_url()` helper function in `tool_factory.py`
- Tool detects payment type and fetches new product price
- Validation: fails fast if product unavailable, price fetch fails, order shipped/cancelled

### Phase 2: COD Flow Enhancement — ✅ Complete
- Added tags `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`
- Added order notes linking old and new orders via cancellation note

### Phase 3: Same Price Prepaid Flow — ✅ Complete
- Implements direct order creation (no draft order)
- Preserves original `financial_status` and `payment_gateway_names`
- Handles partial prepaid COD carryover

### Phase 4: Differential Payment Flow (Customer Owes) — ✅ Complete
- Creates Draft Order via Shopify Draft Orders API
- Applies credit (previous payment) as fixed_amount discount
- Returns `invoice_url` as 24-hour payment link
- Handles partial prepaid COD carryover

### Phase 5: Refund Flow (Customer Owed) — ✅ Complete
- Implemented `_process_partial_refund()` via Shopify Refund API
- Processes refund BEFORE creating new order
- Creates new order with `financial_status="paid"` and original payment gateways
- Gracefully handles refund failures (flags for manual intervention)

### Phase 6: Error Handling & Edge Cases — ✅ Complete
- Partial failure handling with `requires_manual_intervention` flag
- Phone validation embedded in tool (no separate call)
- Pre-validation checks (shipped, cancelled, free items, invalid URLs)

### Phase 7: Prepaid Financial Status Fix — ✅ Complete (Feb 2026)
- Fixed `order_creation_api.py` → `_assemble_order_data()` to accept optional `financial_status` and `payment_gateway_names`
- Fixed `orchestrator.py` → `create_order_in_shopify()` to pass through financial params
- Fixed `tool_factory.py` Scenario 3 → passes `financial_status="paid"` + original gateways
- Fixed `tool_factory.py` Scenario 4 → passes original `financial_status` + original gateways

---

## Summary of Key Decisions

| Decision | Choice |
|----------|--------|
| Cancellation timing | Immediate (before payment link/refund) |
| Draft Order usage | Only when customer owes differential |
| Refund timing | Before new order confirmation |
| Partial prepaid COD handling | Carries over to new order |
| Payment link expiry | N/A (no longer using draft orders for Scenario 2) |
| Refund method | Shopify Refund API |
| Order tags | `USER_REQUESTED_PRODUCT_CHANGE`, `BOT_CREATED`, `DIFFERENTIAL_COD` (Scenario 2) |
| Webhook requirement | Not required |

---

## Document Info

- **Version**: 3.1
- **Created**: February 8, 2026
- **Last Updated**: February 8, 2026
- **Status**: ✅ Implemented

### Changelog
| Version | Date | Changes |
|---------|------|---------|
| 1.0 | Feb 8, 2026 | Initial design document |
| 2.0 | Feb 8, 2026 | Updated to reflect full implementation. Added: implementation status table, financial status propagation fix details, actual code references, Phase 7 (prepaid fix). Changed status from "Ready for Review" to "Implemented". |
| 3.0 | Feb 8, 2026 | **Scenario 2 redesign**: Replaced draft order + payment link approach with COD order + differential at delivery. Deprecated `_create_draft_order_with_credit`. Updated prompt to instruct agent to inform customer about differential COD payment. |
| 3.1 | Feb 8, 2026 | **Order notes on new orders**: Added `note` parameter through `_assemble_order_data` → `create_order` → `create_order_in_shopify`. All 4 scenarios now set contextual notes on new Shopify orders describing the product change. |
