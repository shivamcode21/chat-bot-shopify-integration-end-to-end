# 🎭 Dynamic Mock System - Complete Guide

## 📋 Overview

A comprehensive, JSON-based mocking system for testing that supports:
- ✅ **Client-specific mocks** (different data per client)
- ✅ **Vendor-specific mocks** (Shopify, Shiprocket, etc.)
- ✅ **Method-level control** (enable/disable per API method)
- ✅ **Multiple scenarios** (fulfilled, pending, cancelled orders)
- ✅ **Hierarchical fallback** (client → default → hardcoded)
- ✅ **Hot-reloading** (update JSON without restarting)
- ✅ **Zero code changes** (all configuration in JSON)

---

## 🗂️ Directory Structure

```
fashion_bot/mock/
├── __init__.py                 # Module exports
├── mock_manager.py             # Core mock system
├── config/
│   └── mock_config.json        # Enable/disable mocks
├── data/
│   ├── clients/                # Client-specific mocks
│   │   └── groovee/            # Example: Groovee client
│   │       ├── shopify/
│   │       │   ├── get_order_details.json
│   │       │   ├── get_orders_by_phone.json
│   │       │   └── cancel_order.json
│   │       └── shiprocket/
│   │           └── get_tracking.json
│   └── default/                # Fallback mocks
│       ├── shopify/
│       │   └── get_order_details.json
│       └── shiprocket/
│           └── get_tracking.json
└── tools/
    ├── order_adapter.py        # Mock order adapter
    └── product_adapter.py      # Mock product adapter
```

---

## ⚙️ Configuration

### `mock_config.json`

Controls which mocks are enabled at global, client, vendor, and method levels.

```json
{
  "global": {
    "enabled": true
  },
  "clients": {
    "c3ffcb1b-afb9-4ca4-8746-a06698bec870": {
      "name": "Groovee",
      "enabled": true,
      "vendors": {
        "shopify": {
          "enabled": true,
          "methods": {
            "get_order_details": true,
            "get_orders_by_phone": true,
            "cancel_order": false
          }
        }
      }
    }
  }
}
```

**Hierarchy:**
1. ✅ Global enabled → Check client
2. ✅ Client enabled → Check vendor
3. ✅ Vendor enabled → Check method
4. ✅ Method enabled (defaults to `true` if not specified)

---

## 📄 Mock Data Format

### Example: `get_order_details.json`

```json
{
  "scenarios": {
    "fulfilled_order": {
      "id": 5678901234,
      "name": "#GRV1001",
      "financial_status": "paid",
      "fulfillment_status": "fulfilled",
      "total_price": "2999.00",
      "line_items": [...]
    },
    "pending_order": {
      "id": 5678901235,
      "name": "#GRV1002",
      "financial_status": "paid",
      "fulfillment_status": "pending",
      "total_price": "1499.00"
    },
    "cancelled_order": {
      "id": 5678901236,
      "name": "#GRV1003",
      "financial_status": "refunded",
      "fulfillment_status": "cancelled",
      "total_price": "799.00",
      "cancelled_at": "2025-12-06T10:00:00Z"
    }
  },
  "default_scenario": "fulfilled_order",
  "mapping": {
    "GRV1001": "fulfilled_order",
    "GRV1002": "pending_order",
    "GRV1003": "cancelled_order",
    "1001": "fulfilled_order",
    "1002": "pending_order"
  }
}
```

**Key Components:**
- **scenarios**: Named response variants
- **default_scenario**: Used when no match found
- **mapping**: Maps parameter values to scenarios

---

## 🎯 Usage

### 1. **Using Mock Manager Directly**

```python
from fashion_bot.mock import get_mock, is_mock_enabled

# Check if mocking is enabled
if is_mock_enabled('groovee-uuid', 'shopify', 'get_order_details'):
    # Get mock data
    mock_data = get_mock(
        client_id='groovee-uuid',
        vendor='shopify',
        method='get_order_details',
        params={'order_id': 'GRV1001'}  # Used for scenario selection
    )
    print(mock_data)
```

### 2. **Using Mock Adapters**

```python
from fashion_bot.mock.tools.order_adapter import MockOrderAdapter

# Initialize adapter
adapter = MockOrderAdapter(
    client_id='c3ffcb1b-afb9-4ca4-8746-a06698bec870',
    vendor='shopify'
)

# Get order details - automatically uses JSON mocks
order = adapter.get_order_details('GRV1001')
print(f"Order: {order['name']}, Status: {order['fulfillment_status']}")

# Get orders by phone
orders = adapter.get_orders_by_customer_phone('+919876543210')
print(f"Found {len(orders)} orders")
```

### 3. **Integrating with Real Code**

```python
from fashion_bot.mock.tools.order_adapter import MockOrderAdapter
from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter
import os

# Use mock in test/dev, real in production
if os.getenv('USE_MOCK_SERVICES') == 'true':
    adapter = MockOrderAdapter(client_id=client_id, vendor='shopify')
else:
    adapter = ShopifyOrderAdapter(client_id=client_id)

# Same interface, different implementations
order = adapter.get_order_details('GRV1001')
```

---

## 🔄 Hierarchical Fallback

When looking for mock data, the system follows this order:

```
1. Client-specific mock
   → fashion_bot/mock/data/clients/groovee/shopify/get_order_details.json

2. Default vendor mock
   → fashion_bot/mock/data/default/shopify/get_order_details.json

3. Hardcoded fallback
   → In the adapter code
```

**Example:**
```python
# For Groovee client requesting order GRV1001:
1. Try: clients/groovee/shopify/get_order_details.json
   ✅ Found! Use "fulfilled_order" scenario (mapped from GRV1001)

# For unknown client:
1. Try: clients/unknown/shopify/get_order_details.json
   ❌ Not found
2. Try: default/shopify/get_order_details.json
   ✅ Found! Use "default_order" scenario

# If JSON files don't exist:
3. Use hardcoded fallback in adapter
```

---

## 🎨 Scenario Selection

Scenarios are selected based on parameters using the `mapping` field:

```json
{
  "mapping": {
    "GRV1001": "fulfilled_order",
    "GRV1002": "pending_order",
    "+919876543210": "multiple_orders"
  }
}
```

**How it works:**
```python
# Request: get_mock(..., params={'order_id': 'GRV1001'})
# System checks mapping for 'GRV1001' → finds 'fulfilled_order'
# Returns scenarios['fulfilled_order']

# Request: get_mock(..., params={'phone': '+919876543210'})
# System checks mapping for '+919876543210' → finds 'multiple_orders'
# Returns scenarios['multiple_orders']

# Request: get_mock(..., params={'order_id': 'unknown'})
# No mapping found → returns default_scenario
```

---

## 🧪 Testing Scenarios

### **Test Case 1: Fulfilled Order**

```python
adapter = MockOrderAdapter(client_id='groovee-uuid', vendor='shopify')
order = adapter.get_order_details('GRV1001')

assert order['name'] == '#GRV1001'
assert order['fulfillment_status'] == 'fulfilled'
assert order['financial_status'] == 'paid'
```

### **Test Case 2: Pending Order**

```python
order = adapter.get_order_details('GRV1002')

assert order['name'] == '#GRV1002'
assert order['fulfillment_status'] == 'pending'
```

### **Test Case 3: Cancelled Order**

```python
order = adapter.get_order_details('GRV1003')

assert order['fulfillment_status'] == 'cancelled'
assert order['cancelled_at'] is not None
```

### **Test Case 4: Multiple Orders by Phone**

```python
orders = adapter.get_orders_by_customer_phone('+919876543210')

assert len(orders) == 2
assert orders[0]['name'] == '#GRV1001'
assert orders[1]['name'] == '#GRV1002'
```

---

## 🔧 Creating New Mocks

### **Step 1: Add Method to Config**

Edit `mock/config/mock_config.json`:

```json
{
  "clients": {
    "groovee-uuid": {
      "vendors": {
        "shopify": {
          "methods": {
            "get_product_by_id": true  // ← New method
          }
        }
      }
    }
  }
}
```

### **Step 2: Create JSON File**

Create `mock/data/clients/groovee/shopify/get_product_by_id.json`:

```json
{
  "scenarios": {
    "in_stock": {
      "id": 123456,
      "title": "Designer Hoodie",
      "price": "1499.00",
      "available": true,
      "inventory_quantity": 50
    },
    "out_of_stock": {
      "id": 123456,
      "title": "Designer Hoodie",
      "price": "1499.00",
      "available": false,
      "inventory_quantity": 0
    }
  },
  "default_scenario": "in_stock",
  "mapping": {
    "123456": "in_stock",
    "999999": "out_of_stock"
  }
}
```

### **Step 3: Use in Code**

```python
from fashion_bot.mock import get_mock

product = get_mock('groovee-uuid', 'shopify', 'get_product_by_id', {'product_id': '123456'})
print(f"Product: {product['title']}, Available: {product['available']}")
```

---

## 🔥 Hot-Reloading

Update mock data without restarting the server:

```python
from fashion_bot.mock import reload_mocks

# Reload configuration and clear cache
reload_mocks()

# Subsequent calls use updated JSON files
mock_data = get_mock('groovee-uuid', 'shopify', 'get_order_details', {'order_id': 'GRV1001'})
```

---

## 🐛 Debugging

### **List Available Mocks**

```python
from fashion_bot.mock import list_mocks

mocks = list_mocks()
print(json.dumps(mocks, indent=2))
```

**Output:**
```json
{
  "clients": {
    "groovee": {
      "shopify": ["get_order_details", "get_orders_by_phone"],
      "shiprocket": ["get_tracking"]
    }
  },
  "default": {
    "shopify": ["get_order_details"]
  }
}
```

### **Enable Mock Logging**

Edit `mock_config.json`:

```json
{
  "logging": {
    "log_mock_calls": true,
    "log_fallbacks": true
  }
}
```

**Log Output:**
```
🎭 MOCK CALL: groovee-uuid/shopify/get_order_details → {'order_id': 'GRV1001'}
📋 Selected scenario: fulfilled_order
✅ Loaded mock from clients/groovee/shopify/get_order_details.json
```

---

## 📊 Benefits

### **Before (Hardcoded Mocks):**
```python
# ❌ Hardcoded in code
def get_order_details(order_id):
    if order_id == 'TEST1':
        return {"status": "fulfilled", ...}
    elif order_id == 'TEST2':
        return {"status": "pending", ...}
    ...
```

**Problems:**
- ❌ Need code changes to update mocks
- ❌ All clients share same mock data
- ❌ Can't test different vendors separately
- ❌ Hard to maintain

### **After (JSON-Based Mocks):**
```python
# ✅ JSON-based
adapter = MockOrderAdapter(client_id='groovee-uuid', vendor='shopify')
order = adapter.get_order_details('GRV1001')  # Loads from JSON
```

**Benefits:**
- ✅ Edit JSON files without code changes
- ✅ Different data per client
- ✅ Control mocks per vendor/method
- ✅ Easy to maintain and extend
- ✅ Hot-reload support
- ✅ Realistic test data

---

## 🎯 Summary

| Feature | Description | Status |
|---------|-------------|--------|
| **Client-Specific Mocks** | Different data per client | ✅ |
| **Vendor-Specific Mocks** | Separate mocks for Shopify, Shiprocket | ✅ |
| **Method-Level Control** | Enable/disable per API method | ✅ |
| **Multiple Scenarios** | Test different states (fulfilled, pending, etc.) | ✅ |
| **Hierarchical Fallback** | Client → Default → Hardcoded | ✅ |
| **JSON-Based** | Zero code changes for mock updates | ✅ |
| **Hot-Reloading** | Update mocks without restart | ✅ |
| **Debug Tools** | List mocks, logging | ✅ |

---

## 🚀 Quick Start

```bash
# 1. Enable mocks
export USE_MOCK_SERVICES=true

# 2. Configure (edit mock_config.json)
fashion_bot/mock/config/mock_config.json

# 3. Add mock data (create JSON file)
fashion_bot/mock/data/clients/groovee/shopify/get_order_details.json

# 4. Use in code
from fashion_bot.mock.tools.order_adapter import MockOrderAdapter
adapter = MockOrderAdapter(client_id='groovee-uuid', vendor='shopify')
order = adapter.get_order_details('GRV1001')

# 5. Test!
python -m pytest tests/
```

**Your mocking system is now dynamic, maintainable, and production-ready!** 🎉

