# Shopify Order Management – Business-Oriented Test Cases

This document outlines business-driven test cases for order updation and cancellation APIs in the Shopify integration, with real code output examples.

---

## Order Updation Test Cases (OrderUpdationAPI)

### 1. Update Shipping Address
**Business Goal:** Allow customers to correct or update their address before dispatch, reducing delivery errors and returns.

- **Scenario:** Customer provides a valid order identifier and a new, valid shipping address.
- **Code Output Example:**
```json
{
  "success": true,
  "shopify": { "success": true, "order": { /* ... */ }, "details": { /* ... */ } },
  "shiprocket": { "success": true, "updated": [ {"channel_order_id": "GV1234", "order_id": "123456"} ] }
}
```

---

### 2. Update Note Only
**Business Goal:** Capture customer instructions or requests for fulfillment/packing without affecting logistics.

- **Scenario:** Customer wants to add a note to the order.
- **Code Output Example:**
```json
{
  "success": true,
  "shopify": { "success": true, "order": { /* ... */ }, "details": { /* ... */ } },
  "shiprocket": null
}
```

---

### 3. Invalid Address (Validation Error)
**Business Goal:** Prevent bad data from entering the system, reducing failed deliveries and support costs.

- **Scenario:** Customer provides an address missing required fields (e.g., no PINCODE).
- **Code Output Example:**
```json
{
  "success": false,
  "error": "Validation failed",
  "details": ["PIN/ZIP Code is required"]
}
```

---

### 4. Address Update for Delivered/Canceled/In-Transit Orders
**Business Goal:** Set correct customer expectations and trigger manual intervention for orders that cannot be updated automatically.

- **Scenario:** Order is in a status where address update is not allowed (e.g., DELIVERED, CANCELED, IN TRANSIT).
- **Code Output Example:**
```json
{
  "success": false,
  "skipped": [ {"channel_order_id": "GV1234", "order_id": "123456", "status": "DELIVERED"} ],
  "message": "Someone from our team will contact you soon."
}
```

---

### 5. Special Shiprocket Status (Pickup Scheduled, etc.)
**Business Goal:** Ensure compliance with Shiprocket's workflow, avoid failed pickups, and maintain order traceability.

- **Scenario:** Order is in a Shiprocket status that requires cancel-and-recreate.
- **Code Output Example:**
```json
{
  "success": true,
  "shiprocket": { "cancelled": "123456", "created": { "status": 1, /* ... */ } },
  "shopify": null,
  "note": "Shopify order not updated; Shiprocket order re-created due to special status."
}
```

---

## Order Cancellation Test Cases (OrderCancellationAPI)

### 1. Cancel Order
**Business Goal:** Allow customers to cancel orders, ensuring all systems (Shopify, Shiprocket) are updated and reasons are tracked for analytics and process improvement.

- **Scenario:** Customer requests cancellation for an eligible order.
- **Code Output Example:**
```json
{
  "success": true,
  "shopify": { "success": true, /* ... */ },
  "shiprocket": { "success": true, /* ... */ }
}
```

---

### 2. Cancel Already Delivered/Canceled Order
**Business Goal:** Prevent redundant operations, inform customer of current status, and avoid confusion.

- **Scenario:** Order is already delivered or canceled.
- **Code Output Example:**
```json
{
  "success": false,
  "error": "Order not found for cancellation",
  "details": { "identifier": "#gv1234" }
}
```

---

### 3. Cancel In-Transit Order
**Business Goal:** Ensure compliance with logistics provider, trigger manual follow-up, and avoid system errors.

- **Scenario:** Order is in an in-transit status.
- **Code Output Example:**
```json
{
  "success": true,
  "shopify": { "success": true, /* ... */ },
  "shiprocket": { "success": false, "error": "Cannot cancel order in status: IN TRANSIT" }
}
```

---

### 4. Invalid Order Identifier
**Business Goal:** Prevent confusion, ensure only valid orders are processed, and provide clear feedback to the customer.

- **Scenario:** Customer provides an invalid or non-existent order identifier.
- **Code Output Example:**
```json
{
  "success": false,
  "error": "Order not found for cancellation",
  "details": { "identifier": "badid" }
}
```

---

### 5. API Failure (Shopify/Shiprocket)
**Business Goal:** Surface technical issues for support and monitoring, ensuring reliability and transparency.

- **Scenario:** API returns an error (e.g., network, permission).
- **Code Output Example:**
```json
{
  "success": false,
  "error": "Shiprocket authentication failed: ...",
  "details": { /* ... */ }
}
```

---
