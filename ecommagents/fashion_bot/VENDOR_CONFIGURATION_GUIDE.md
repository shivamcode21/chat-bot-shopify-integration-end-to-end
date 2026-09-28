# Vendor Configuration Guide

## Overview
The system uses a **configuration-driven dependency graph** to determine:
1. Which vendor to fetch data from (Shopify, WooCommerce, etc.)
2. Whether to use a logistics provider (Shiprocket, Delhivery, etc.)
3. What enrichment pipeline to run (GraphQL, tracking, status, etc.)
4. The order of enrichment steps

## Configuration Location

```
fashion_bot/core/vendor_config.py
```

This is the **SINGLE SOURCE OF TRUTH** for all vendor configurations.

## How It Works

### 1. Request Flow

```
User Request (with client_id in state)
  ↓
Tool (get_customer_orders_by_phone)
  ↓
Orchestrator
  ↓ asks Factory
Factory.get_primary_vendor(state) 
  ↓ asks VendorConfigManager
VendorConfigManager.get_config(client_id)
  ↓ looks up in CLIENT_VENDOR_CONFIGS
Returns VendorConfig for client
```

### 2. Configuration Structure

```python
VendorConfig(
    client_id="your_client",
    primary_vendor=VendorType.SHOPIFY,        # Where to fetch orders from
    logistics_provider=LogisticsProvider.SHIPROCKET,  # Logistics system
    enrichment_pipeline=[                      # Dependency graph (in order!)
        EnrichmentStep("shopify_graphql", enabled=True, config={...}),
        EnrichmentStep("shiprocket_status", enabled=True, config={...}),
    ],
    order_status_primary_source="logistics"   # Where to check order status first
)
```

## Example Configurations

### Example 1: Shopify + Shiprocket (Default)

```python
SHOPIFY_SHIPROCKET_CONFIG = VendorConfig(
    client_id="groovee",
    primary_vendor=VendorType.SHOPIFY,
    logistics_provider=LogisticsProvider.SHIPROCKET,
    enrichment_pipeline=[
        # Step 1: Fetch current line items via GraphQL (top 5 orders)
        EnrichmentStep(
            enricher_type="shopify_graphql",
            enabled=True,
            config={"top_n_orders": 5}
        ),
        # Step 2: Add tracking info if missing
        EnrichmentStep(
            enricher_type="shopify_tracking",
            enabled=True,
            config={"only_if_missing": True}
        ),
        # Step 3: Get real-time status from Shiprocket (top 3 orders)
        EnrichmentStep(
            enricher_type="shiprocket_status",
            enabled=True,
            config={"top_n_orders": 3}
        ),
    ],
    order_status_primary_source="logistics"  # Check Shiprocket first
)
```

**Execution Flow:**
```
1. Fetch orders from Shopify (primary_vendor)
2. Process with ShopifyProcessor
3. Enrich with GraphQL line items (top 5)
4. Enrich with Shopify tracking (if missing)
5. Enrich with Shiprocket status (top 3)
6. Return enriched DTOs
```

### Example 2: WooCommerce Only (No Logistics)

```python
WOOCOMMERCE_ONLY_CONFIG = VendorConfig(
    client_id="woocommerce_client",
    primary_vendor=VendorType.WOOCOMMERCE,
    logistics_provider=LogisticsProvider.NONE,  # No logistics provider!
    enrichment_pipeline=[
        # No enrichers - WooCommerce has all data
    ],
    order_status_primary_source="order_vendor"
)
```

**Execution Flow:**
```
1. Fetch orders from WooCommerce (primary_vendor)
2. Process with WooCommerceProcessor
3. No enrichers run
4. Return processed DTOs
```

### Example 3: WooCommerce + Shiprocket

```python
WOOCOMMERCE_SHIPROCKET_CONFIG = VendorConfig(
    client_id="woocommerce_shiprocket_client",
    primary_vendor=VendorType.WOOCOMMERCE,
    logistics_provider=LogisticsProvider.SHIPROCKET,
    enrichment_pipeline=[
        # Only enrich with Shiprocket status
        EnrichmentStep(
            enricher_type="shiprocket_status",
            enabled=True,
            config={"top_n_orders": 5}
        ),
    ],
    order_status_primary_source="logistics"
)
```

**Execution Flow:**
```
1. Fetch orders from WooCommerce (primary_vendor)
2. Process with WooCommerceProcessor
3. Enrich with Shiprocket status (top 5)
4. Return enriched DTOs
```

## Registering New Clients

### Option 1: Static Configuration (Current)

Add to `vendor_config.py`:

```python
# Define configuration
MY_CLIENT_CONFIG = VendorConfig(
    client_id="my_new_client",
    primary_vendor=VendorType.SHOPIFY,
    logistics_provider=LogisticsProvider.SHIPROCKET,
    enrichment_pipeline=[
        EnrichmentStep("shopify_graphql", enabled=True),
        EnrichmentStep("shiprocket_status", enabled=True),
    ],
)

# Register in CLIENT_VENDOR_CONFIGS
CLIENT_VENDOR_CONFIGS = {
    "default": SHOPIFY_SHIPROCKET_CONFIG,
    "my_new_client": MY_CLIENT_CONFIG,  # Add here
}
```

### Option 2: Dynamic Registration (Runtime)

```python
from fashion_bot.core.vendor_config import VendorConfig, register_client_config, VendorType, LogisticsProvider, EnrichmentStep

# Create config
config = VendorConfig(
    client_id="runtime_client",
    primary_vendor=VendorType.SHOPIFY,
    logistics_provider=LogisticsProvider.SHIPROCKET,
    enrichment_pipeline=[
        EnrichmentStep("shopify_graphql", enabled=True),
    ],
)

# Register at runtime
register_client_config(config)
```

### Option 3: Database Configuration (Future)

```python
# TODO: Implement
def load_config_from_database(client_id: str) -> VendorConfig:
    # SELECT * FROM client_vendor_configs WHERE client_id = ?
    # Parse JSON/dict into VendorConfig object
    pass
```

## How Client ID is Determined

The `client_id` flows through the system in the `state` dictionary:

```python
# 1. Request comes in with client context
state = {
    "client_id": "groovee",
    "trace_id": "abc123",
    ...
}

# 2. Tool passes state to Orchestrator
result = CustomerOrderOrchestrator.get_customer_orders(phone, state=state)

# 3. Orchestrator passes state to Factory
primary_vendor = ServiceFactory.get_primary_vendor(state=state)

# 4. Factory extracts client_id from state
config = VendorConfigManager.get_config(state=state)
# Internally: client_id = state.get('client_id')

# 5. Config lookup happens
return CLIENT_VENDOR_CONFIGS.get(client_id, DEFAULT_CONFIG)
```

## Adding New Enricher Types

### Step 1: Create Enricher Implementation

```python
# fashion_bot/delhivery/enrichers/status_enricher.py
from fashion_bot.interfaces.enricher import OrderEnricherInterface

class DelhiveryStatusEnricher(OrderEnricherInterface):
    def enrich(self, orders, state):
        # Your enrichment logic
        return orders
```

### Step 2: Register in Factory

```python
# fashion_bot/core/factory.py

@staticmethod
def _create_enricher(enricher_type: str, config: Dict):
    # ... existing enrichers ...
    
    elif enricher_type == "delhivery_status":
        from fashion_bot.delhivery.enrichers.status_enricher import DelhiveryStatusEnricher
        return DelhiveryStatusEnricher()
```

### Step 3: Use in Configuration

```python
# fashion_bot/core/vendor_config.py

MAGENTO_DELHIVERY_CONFIG = VendorConfig(
    client_id="magento_client",
    primary_vendor=VendorType.MAGENTO,
    logistics_provider=LogisticsProvider.DELHIVERY,
    enrichment_pipeline=[
        EnrichmentStep("delhivery_status", enabled=True),  # Now available!
    ],
)
```

## Configuration Options

### VendorConfig Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `client_id` | str | Unique client identifier |
| `primary_vendor` | VendorType | Where to fetch orders from (Shopify, WooCommerce, etc.) |
| `logistics_provider` | LogisticsProvider | Logistics system (Shiprocket, Delhivery, None) |
| `enrichment_pipeline` | List[EnrichmentStep] | Ordered list of enrichment steps |
| `order_status_primary_source` | str | "logistics" or "order_vendor" - where to check order status first |
| `fetch_from_multiple_sources` | bool | If True, fetch from both order + logistics (advanced) |

### EnrichmentStep Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `enricher_type` | str | Type identifier (e.g., "shopify_graphql", "shiprocket_status") |
| `enabled` | bool | Whether to run this enricher |
| `config` | Dict | Enricher-specific configuration (e.g., `{"top_n_orders": 5}`) |

## Available Enricher Types

| Enricher Type | Description | Config Options |
|---------------|-------------|----------------|
| `shopify_graphql` | Fetch current line items via Shopify GraphQL | `top_n_orders`: Number of recent orders to enrich (default: 5) |
| `shopify_tracking` | Add Shopify tracking info | `only_if_missing`: Only enrich if tracking URL is missing (default: True) |
| `shiprocket_status` | Fetch real-time status from Shiprocket | `top_n_orders`: Number of recent orders to enrich (default: 3) |

## Testing with Different Configurations

```python
# Test with specific client
state = {"client_id": "woocommerce_client"}
result = get_customer_orders_by_phone("9876543210", state=state)
# Uses WOOCOMMERCE_ONLY_CONFIG (no enrichers)

# Test with different client
state = {"client_id": "groovee"}
result = get_customer_orders_by_phone("9876543210", state=state)
# Uses SHOPIFY_SHIPROCKET_CONFIG (3 enrichers)
```

## Debugging Configuration

```python
from fashion_bot.core.vendor_config import VendorConfigManager

# Check what config is being used
config = VendorConfigManager.get_config(client_id="groovee")
print(f"Primary Vendor: {config.primary_vendor.value}")
print(f"Logistics: {config.logistics_provider.value}")
print(f"Enrichment Steps: {len(config.enrichment_pipeline)}")

for step in config.enrichment_pipeline:
    print(f"  - {step.enricher_type} (enabled={step.enabled})")
```

## Migration from Hardcoded to Config

### Before (Hardcoded)
```python
# Factory had hardcoded logic
if vendor == "shopify":
    enrichers = [GraphQLEnricher(), TrackingEnricher(), StatusEnricher()]
elif vendor == "woocommerce":
    enrichers = [StatusEnricher()]
```

### After (Config-Driven)
```python
# Factory reads from VendorConfigManager
pipeline_config = VendorConfigManager.get_enrichment_pipeline_config(state=state)
enrichers = [ServiceFactory._create_enricher(step.enricher_type, step.config) 
             for step in pipeline_config]
```

**Benefits:**
- ✅ Add new clients without code changes
- ✅ Modify pipelines without redeploying
- ✅ A/B test different enrichment strategies
- ✅ Client-specific customization
- ✅ Easy to move to database storage

## Future Enhancements

### 1. Database-Backed Configuration
Store configurations in PostgreSQL/MongoDB:

```sql
CREATE TABLE client_vendor_configs (
    client_id VARCHAR PRIMARY KEY,
    primary_vendor VARCHAR,
    logistics_provider VARCHAR,
    enrichment_pipeline JSONB,
    config_version INT
);
```

### 2. Dynamic Pipeline Modification
Allow clients to customize their pipeline via API:

```python
POST /api/config/pipeline
{
    "client_id": "groovee",
    "add_enricher": {
        "type": "custom_enricher",
        "position": 1,
        "config": {...}
    }
}
```

### 3. Conditional Enrichers
Run enrichers based on conditions:

```python
EnrichmentStep(
    enricher_type="expensive_enricher",
    enabled=True,
    config={
        "condition": "order_count < 10"  # Only run if few orders
    }
)
```

## Summary

### Configuration Flow
```
1. Request → state["client_id"] = "groovee"
2. VendorConfigManager.get_config("groovee")
3. Returns SHOPIFY_SHIPROCKET_CONFIG
4. Factory builds enricher pipeline from config
5. Orchestrator executes pipeline
```

### Key Files
- `fashion_bot/core/vendor_config.py` - Configuration definitions
- `fashion_bot/core/factory.py` - Uses config to build services/enrichers
- `fashion_bot/core/orchestrator.py` - Executes pipeline

### Adding New Client
1. Define `VendorConfig` in `vendor_config.py`
2. Add to `CLIENT_VENDOR_CONFIGS` dict
3. Done! No changes to Orchestrator/Tools needed.

