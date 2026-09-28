# Vector Search Architecture Design Document

## Overview

This document describes the architecture and flow of the product vector search system used in the Fashion Bot. The system enables semantic product search capabilities by storing product embeddings in Upstash Vector DB and provides real-time synchronization with Shopify product catalogs.

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Multi-Tenant Design](#multi-tenant-design)
3. [Product Ingestion Flow](#product-ingestion-flow)
4. [Data Synchronization Strategy](#data-synchronization-strategy)
5. [Search Flow](#search-flow)
6. [Data Model](#data-model)
7. [API Endpoints](#api-endpoints)
8. [Error Handling & Resilience](#error-handling--resilience)
9. [LLM Attribute Extraction](#llm-attribute-extraction-new) ✅ NEW
10. [Observability & Tracking](#observability--tracking)

---

## System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DATA SOURCES                                    │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌─────────────────┐              ┌─────────────────┐                     │
│   │  Shopify Admin  │              │  /products.json │                     │
│   │   GraphQL API   │              │   (Fallback)    │                     │
│   └────────┬────────┘              └────────┬────────┘                     │
│            │                                │                               │
│            └───────────────┬────────────────┘                               │
│                            ▼                                                │
│                 ┌─────────────────────┐                                     │
│                 │  Product Source     │                                     │
│                 │     Factory         │                                     │
│                 └──────────┬──────────┘                                     │
│                            │                                                │
└────────────────────────────┼────────────────────────────────────────────────┘
                             │
                             ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                                      │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   ┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐        │
│   │   Normalizer    │───▶│  LLM Attribute  │───▶│  Content Hash   │        │
│   │  (Standardize)  │    │   Extractor     │    │   Generator     │        │
│   └─────────────────┘    │ (Gemini 2.5     │    └────────┬────────┘        │
│                          │  Flash Lite)    │             │                  │
│                          └─────────────────┘             ▼                  │
│                                                  ┌─────────────────┐        │
│                                                  │ Vector Data     │        │
│                                                  │   Builder       │        │
│                                                  └────────┬────────┘        │
│                                                           │                 │
│                                                           ▼                 │
│                                              ┌─────────────────────┐        │
│                                              │  Embedding Model    │        │
│                                              │   (BAAI/bge-m3)    │        │
│                                              └──────────┬──────────┘        │
│                                                         │                   │
└─────────────────────────────────────────────────────────┼───────────────────┘
                                                          │
                                                          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         STORAGE LAYER                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│                        ┌─────────────────────────────┐                      │
│                        │    Upstash Vector DB        │                      │
│                        │                             │                      │
│                        │  ┌───────────────────────┐  │                      │
│                        │  │ Namespace: client_A   │  │                      │
│                        │  │  - product vectors    │  │                      │
│                        │  │  - metadata           │  │                      │
│                        │  └───────────────────────┘  │                      │
│                        │                             │                      │
│                        │  ┌───────────────────────┐  │                      │
│                        │  │ Namespace: client_B   │  │                      │
│                        │  │  - product vectors    │  │                      │
│                        │  │  - metadata           │  │                      │
│                        │  └───────────────────────┘  │                      │
│                        │                             │                      │
│                        └─────────────────────────────┘                      │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Components

| Component | Responsibility |
|-----------|----------------|
| **Product Source Factory** | Determines data source (Shopify API vs /products.json) based on client config |
| **Shopify Product Service** | Fetches products via Shopify Admin GraphQL API |
| **Products JSON Service** | Fallback - fetches from public /products.json endpoint |
| **Normalizer** | Converts source-specific formats to standardized NormalizedProduct |
| **LLM Attribute Extractor** | Uses Gemini 2.5 Flash Lite to extract structured attributes (base_product_name, product_line, color, material) from product titles/metadata at ingestion time. Enables precise metadata filtering during search. (`product_attribute_extractor.py`) |
| **Content Hash Generator** | Creates MD5 hash of product content for change detection |
| **Vector Data Builder** | Constructs searchable text and metadata for embedding |
| **Upstash Vector Service** | Handles all vector DB operations (upsert, search, delete) |

---

## Multi-Tenant Design

### Client Isolation via Namespaces

The system supports multiple clients (merchants) with complete data isolation using Upstash Vector's namespace feature.

```
┌─────────────────────────────────────────────────────────────────┐
│                    UPSTASH VECTOR INDEX                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────────┐  ┌─────────────────────┐              │
│  │  Namespace:         │  │  Namespace:         │              │
│  │  client_uuid_1      │  │  client_uuid_2      │              │
│  │                     │  │                     │              │
│  │  • Product A        │  │  • Product X        │              │
│  │  • Product B        │  │  • Product Y        │              │
│  │  • Product C        │  │  • Product Z        │              │
│  │                     │  │                     │              │
│  │  (Isolated)         │  │  (Isolated)         │              │
│  └─────────────────────┘  └─────────────────────┘              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Vector ID Convention

Each vector has a unique ID following the pattern:

```
{client_id}_{shopify_product_id}
```

**Example:** `550e8400-e29b-41d4-a716-446655440000_8234567890123`

This ensures:
- Uniqueness across the entire index
- Easy identification of client ownership
- Simple deletion by client or product

### Client ID Resolution

When processing requests, the system resolves client_id through multiple fallback mechanisms:

1. **Explicit parameter** - Query param or header (for API calls)
2. **Shopify Shop Domain lookup** - Maps shop domain to client_id via database
3. **Default client** - Fallback for single-tenant deployments

---

## Product Ingestion Flow

### Initial Full Ingestion

Used when setting up a new client or when complete re-sync is needed.

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Trigger    │────▶│    Fetch     │────▶│  Normalize   │────▶│  LLM Extract │────▶│   Upsert     │
│  (API Call)  │     │  All Active  │     │   Products   │     │  Attributes  │     │  to Vector   │
│              │     │   Products   │     │              │     │ (Gemini 2.5  │     │     DB       │
│              │     │              │     │              │     │  Flash Lite) │     │              │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │   Shopify    │
                     │  GraphQL API │
                     │              │
                     │  Pagination  │
                     │  (50/page)   │
                     └──────────────┘
```

**Steps:**

1. API endpoint receives ingestion request with `client_id`
2. Product Source Factory selects appropriate data source
3. Fetch all active products using cursor-based pagination
4. Normalize products to standard `NormalizedProduct` format
5. **LLM Attribute Extraction** (Gemini 2.5 Flash Lite):
   - Build lightweight product dicts (title, collections, tags, category, description, options)
   - Batch process via `ProductAttributeExtractor.extract_batch_async()`
   - Extract: `base_product_name`, `product_line`, `product_line_normalized`, `extracted_color`, `material`
   - Populate extracted attributes back onto `NormalizedProduct` instances
   - Graceful fallback: if LLM extraction fails, ingestion continues without structured fields
6. For each product:
   - Build searchable text from structured + raw fields
   - Compute content hash for future delta detection
   - Generate embedding vectors
7. Batch upsert vectors to Upstash in client's namespace
8. Return ingestion statistics

### Searchable Text Construction

The embedding is generated from a structured concatenation of product attributes. When LLM-extracted fields are available, they take priority for better semantic accuracy:

**With LLM-extracted fields (preferred):**
```
Product: {base_product_name} | Model: {product_line} | Color: {extracted_color} | 
Material: {material} | Category: {product_type} | Brand: {vendor} | 
Tags: {tags} | Colors: {colors} | Sizes: {sizes} | Description | Fabric | Fit Type
```

**Example (phone case with LLM extraction):**
```
Product: Casence Leather Case | Model: iphone 17 pro max | Color: royal black | 
Material: leather | Category: Phone Cases | Brand: Casence | 
Tags: premium, leather, iphone | Colors: Black | Sizes: | 
Premium genuine leather case with card slots
```

**Fallback (without LLM extraction):**
```
Title | Product Type | Vendor | Tags | Colors | Sizes | Description | Fabric | Fit Type
```

**Example (fashion product fallback):**
```
Summer Cotton T-Shirt | T-Shirts | FashionBrand | summer, casual, cotton | 
Blue, Red, Black | S, M, L, XL | Comfortable breathable cotton tee perfect for summer | 
100% Organic Cotton | Regular Fit
```

---

## Data Synchronization Strategy

The system uses a **hybrid synchronization approach** combining real-time webhooks with periodic fallback sync.

### Synchronization Methods

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     SYNC STRATEGY OVERVIEW                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   PRIMARY: Real-time Webhooks                                               │
│   ─────────────────────────────                                             │
│   • products/create  ──▶  Upsert new product to vector DB                  │
│   • products/update  ──▶  Upsert updated product to vector DB              │
│   • products/delete  ──▶  Remove product from vector DB                    │
│                                                                             │
│   FALLBACK: Weekly Cron Job                                                 │
│   ────────────────────────                                                  │
│   • Runs every Sunday at 3:00 AM UTC                                        │
│   • Fetches products updated in last 7 days                                 │
│   • Catches any missed webhooks                                             │
│   • Ensures data consistency                                                │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Real-Time Webhook Flow

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Shopify    │────▶│   Webhook    │────▶│   Resolve    │────▶│   Process    │
│   Event      │     │   Endpoint   │     │  Client ID   │     │    Event     │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                      │
                          ┌───────────────────────────────────────────┤
                          │                                           │
                          ▼                                           ▼
                   ┌──────────────┐                           ┌──────────────┐
                   │   CREATE /   │                           │    DELETE    │
                   │   UPDATE     │                           │              │
                   │              │                           │   Remove     │
                   │  Normalize   │                           │   Vector     │
                   │  + LLM       │                           │   by ID      │
                   │  Extract     │                           └──────────────┘
                   │  & Upsert    │
                   └──────────────┘
```

**Webhook Topics:**
- `products/create` - New product added in Shopify
- `products/update` - Product details modified
- `products/delete` - Product removed from Shopify

**LLM Extraction on Webhooks:**
For `products/create` and `products/update` events, the system runs synchronous LLM attribute extraction (`extract_attributes()`) via `product_webhook.py` → `handle_product_upsert()` before upserting to vector DB. This ensures all products — whether ingested in bulk or via real-time webhooks — have consistent structured metadata.

**Non-Active Product Handling:**
Products with status `draft` or `archived` are skipped during create/update webhooks.

### Weekly Delta Sync (Cron Job)

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Cron       │────▶│  Get All     │────▶│  For Each    │────▶│   Delta      │
│   Trigger    │     │  Clients     │     │   Client     │     │    Sync      │
│ (Sun 3AM UTC)│     │              │     │              │     │              │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
                                                                      │
                                                                      ▼
                                                               ┌──────────────┐
                                                               │   Fetch      │
                                                               │  Products    │
                                                               │  Updated in  │
                                                               │  Last 7 Days │
                                                               └──────────────┘
```

**Delta Sync Algorithm:**

1. Fetch products updated in the lookback period (7 days for weekly sync)
2. Compute content hash for each fetched product
3. Retrieve existing hashes from vector DB for those product IDs
4. Compare hashes to determine action:
   - **ADD**: Product ID exists in source but not in vector DB
   - **UPDATE**: Product exists in both but hashes differ
   - **UNCHANGED**: Hashes match - skip (no action needed)
5. **LLM Attribute Extraction** (✅ NEW): For products needing ADD/UPDATE, batch extract structured attributes via `ProductAttributeExtractor.extract_batch_async()`
6. Batch upsert products (with extracted attributes) that need ADD or UPDATE

**Note:** Time-based delta sync cannot detect deletions. Product deletions are handled exclusively via webhooks.

### Content Hash for Change Detection

A content hash is computed from key product fields to detect changes:

```
┌─────────────────────────────────────────────────────────────────┐
│                    CONTENT HASH FIELDS                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  CORE FIELDS                                                    │
│  • title                    • price_min / price_max            │
│  • product_type             • compare_at_price_min/max         │
│  • vendor                   • in_stock                         │
│  • tags (sorted)            • total_inventory                  │
│  • colors (sorted)          • status                           │
│  • sizes (sorted)           • description (first 500 chars)    │
│  • fabric                   • image_url                        │
│  • care_instructions                                            │
│  • fit_type                                                     │
│                                                                 │
│  LLM-EXTRACTED FIELDS (✅ NEW)                                  │
│  • base_product_name        • extracted_color                  │
│  • product_line             • material                         │
│  • product_line_normalized                                      │
│                                                                 │
│  Hash Algorithm: MD5                                            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

If any of these fields change, the hash changes, triggering a vector update.

---

## Search Flow

### Semantic Search Process

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│   Search     │────▶│  LLM Agent   │────▶│   Generate   │────▶│   Query      │────▶│   Return     │
│   Query      │     │  Extracts    │     │  Embedding   │     │  Vector DB   │     │   Results    │
│              │     │ product_line │     │              │     │              │     │              │
│ "show cases  │     │              │     │              │     │  Namespace:  │     │  Top K       │
│  for iphone  │     │ → "iphone    │     │  Embedding   │     │  {client_id} │     │  Products    │
│  17 pro max" │     │  17 pro max" │     │              │     │  + Filter:   │     │              │
│              │     │              │     │              │     │  product_line│     │              │
└──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘     └──────────────┘
```

**Search with Metadata Filtering:**

The search combines semantic vector similarity with **exact-match metadata filters** for precision:

1. The LLM agent (via `tool_factory.py` → `search_products_by_name` tool) extracts `product_line` from the user query or conversation context
2. The query embedding is generated for semantic matching
3. Upstash Vector DB applies the `product_line_normalized` exact-match filter **before** similarity scoring
4. Only vectors matching the metadata filter are considered for semantic ranking

**Filter Construction:**
```python
# In upstash_vector_service.py search()
filter_parts = []
if product_line:
    filter_parts.append(f"product_line_normalized = '{product_line}'")
if segment:
    filter_parts.append(f"segment = '{segment}'")
# Combined with AND logic
filter_str = " AND ".join(filter_parts)
```

**Context Carry-Forward:**
The LLM agent infers `product_line` from conversation history when the user asks follow-up questions (e.g., "show me a different color" → infers product_line from previous messages). This is enforced via instructions in `tool_factory.py` and `generic_skill_node.py`.

### Search Parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `query` | Natural language search query | Required |
| `client_id` | Client namespace to search | Required |
| `top_k` | Number of results to return | 10 (max: 50) |
| `product_line` | **Exact-match filter** on `product_line_normalized` metadata. Extracted by LLM agent from query or conversation context. (e.g., "iphone 17 pro max") | None |
| `segment` | **Exact-match filter** on `segment` metadata. (e.g., "men", "women", "kids") | None |
| `min_price` | Minimum price filter | None |
| `max_price` | Maximum price filter | None |
| `product_type` | Filter by product type | None |
| `in_stock_only` | Only return in-stock products | false |

### Search Response

Results are returned with similarity scores and full product metadata:

```
┌─────────────────────────────────────────────────────────────────┐
│                      SEARCH RESULT                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  CORE                                                           │
│  • product_id          • colors, sizes                         │
│  • title               • tags                                  │
│  • product_type        • description                           │
│  • vendor              • fabric, fit_type                      │
│  • price_min/max       • product_url                           │
│  • image_url           • variants (with inventory)             │
│  • in_stock            • similarity_score (0-1)                │
│                                                                 │
│  LLM-EXTRACTED (✅ NEW)                                         │
│  • base_product_name   • extracted_color                       │
│  • product_line        • material                              │
│  • product_line_normalized                                      │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Data Model

### NormalizedProduct

The standardized product representation used throughout the system:

```
┌─────────────────────────────────────────────────────────────────┐
│                    NormalizedProduct                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  IDENTITY                                                       │
│  ─────────                                                      │
│  • id (Shopify product ID)                                      │
│  • handle (URL slug)                                            │
│  • status (active/draft/archived)                               │
│                                                                 │
│  BASIC INFO                                                     │
│  ──────────                                                     │
│  • title                                                        │
│  • description                                                  │
│  • product_type                                                 │
│  • vendor                                                       │
│  • tags []                                                      │
│                                                                 │
│  PRICING                                                        │
│  ───────                                                        │
│  • price_min / price_max                                        │
│  • compare_at_price_min / compare_at_price_max                  │
│                                                                 │
│  INVENTORY                                                      │
│  ─────────                                                      │
│  • in_stock (boolean)                                           │
│  • total_inventory (count)                                      │
│                                                                 │
│  VARIANTS                                                       │
│  ────────                                                       │
│  • colors []                                                    │
│  • sizes []                                                     │
│  • variants [] (full variant details)                           │
│                                                                 │
│  MEDIA                                                          │
│  ─────                                                          │
│  • image_url (primary)                                          │
│  • all_images []                                                │
│  • product_url                                                  │
│                                                                 │
│  ATTRIBUTES                                                     │
│  ──────────                                                     │
│  • fabric                                                       │
│  • care_instructions                                            │
│  • fit_type                                                     │
│  • size_chart                                                   │
│                                                                 │
│  LLM-EXTRACTED ATTRIBUTES (✅ NEW)                               │
│  ─────────────────────────────────                               │
│  • base_product_name   (Core name without model/color/size)     │
│  • product_line        (e.g., "iphone 17 pro max")             │
│  • product_line_normalized  (Lowercased for exact-match filter) │
│  • extracted_color     (Color from title, via LLM)             │
│  • material            (e.g., "leather", "silicone")           │
│                                                                 │
│  SEO                                                            │
│  ───                                                            │
│  • seo_title                                                    │
│  • seo_description                                              │
│                                                                 │
│  TIMESTAMPS                                                     │
│  ──────────                                                     │
│  • created_at                                                   │
│  • updated_at                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Vector Metadata

Metadata stored alongside each vector in Upstash:

```
┌─────────────────────────────────────────────────────────────────┐
│                    Vector Metadata                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  CORE METADATA                                                  │
│  • product_id           • tags (comma-separated)               │
│  • client_id            • in_stock (boolean)                   │
│  • title                • total_inventory                      │
│  • product_type         • image_url                            │
│  • vendor               • all_images (JSON)                    │
│  • price_min            • product_url                          │
│  • price_max            • description                          │
│  • compare_at_prices    • fabric, fit_type, etc.               │
│  • colors (JSON)        • variants (JSON)                      │
│  • sizes (JSON)         • content_hash (for delta sync)        │
│                         • updated_at                            │
│                                                                 │
│  LLM-EXTRACTED METADATA (✅ NEW — used for search filtering)    │
│  • base_product_name    • extracted_color                      │
│  • product_line         • material                             │
│  • product_line_normalized  (exact-match filter key)           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## API Endpoints

### Ingestion Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/products/ingest` | POST | Full ingestion of all products for a client |
| `/api/v1/products/delta-sync` | POST | Delta sync for a single client |
| `/api/v1/products/delta-sync/all` | POST | Delta sync for all configured clients |
| `/api/v1/products/ingest/status/{client_id}` | GET | Get ingestion status for a client |

### Search Endpoint

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/products/search` | POST | Semantic search across products |

### Webhook Endpoint

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/product/webhook/products` | POST | Receives Shopify product webhooks |

### Manual Triggers

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/cron/trigger-product-vector-sync` | POST | Manually trigger weekly sync job |

---

## Error Handling & Resilience

### Webhook Resilience

```
┌─────────────────────────────────────────────────────────────────┐
│                    RESILIENCE STRATEGY                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. WEBHOOK FAILURES                                            │
│     • Weekly cron job catches missed webhooks                   │
│     • 7-day lookback ensures coverage                           │
│                                                                 │
│  2. PARTIAL FAILURES                                            │
│     • Batch operations track individual failures                │
│     • Failed products logged with error details                 │
│     • Successful products not rolled back                       │
│                                                                 │
│  3. IDEMPOTENCY                                                 │
│     • Upsert operations are idempotent                          │
│     • Same webhook can be safely processed multiple times       │
│                                                                 │
│  4. RATE LIMITING                                               │
│     • Clients processed sequentially in cron jobs               │
│     • Pagination limits API calls (50 products/page)            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Failure Scenarios

| Scenario | Handling |
|----------|----------|
| Webhook delivery failure | Weekly sync catches up |
| Vector DB unavailable | Operation fails, logged for retry |
| Invalid product data | Skipped with warning, other products continue |
| Client not found | Falls back to default client or returns error |
| Shopify API rate limit | Pagination reduces risk; errors logged |

---

## Summary

The vector search system provides:

1. **Multi-tenant isolation** via namespaces keyed by client_id
2. **Real-time sync** via Shopify webhooks for immediate updates
3. **Weekly fallback sync** to catch missed webhooks
4. **Efficient delta detection** using content hashes
5. **Semantic search** using BAAI/bge-m3 embeddings
6. **Rich metadata** enabling filtered searches
7. **Resilient architecture** with multiple sync mechanisms
8. **Observability** via PostgreSQL sync logging
9. **LLM-powered attribute extraction** (✅ NEW) via Gemini 2.5 Flash Lite at ingestion time for structured metadata (product_line, material, color, base_product_name)
10. **Exact-match metadata filtering** (✅ NEW) using `product_line_normalized` for precise search results (e.g., "iphone 17" vs "iphone 17 pro max")
11. **Contextual search inference** (✅ NEW) where the LLM agent carries forward product_line from conversation history for follow-up queries

This hybrid approach ensures data consistency while minimizing unnecessary API calls and vector updates.

---

## LLM Attribute Extraction (✅ NEW)

### Overview

At ingestion time, every product is processed through the **ProductAttributeExtractor** (`product_attribute_extractor.py`) which uses **Gemini 2.5 Flash Lite** to extract structured attributes from product titles and metadata. These attributes are stored as vector metadata and enable precise **exact-match filtering** during search, complementing the semantic similarity ranking.

### Why LLM Extraction?

| Problem | Solution |
|---------|----------|
| Semantic search returns "iPhone 17 Pro Max" cases for "iPhone 17" queries (high similarity score) | `product_line_normalized` exact-match filter ensures only exact device model matches |
| Hardcoded regex parsers are not scalable across different product categories | LLM extracts attributes generically for phone cases, fashion, jewelry, skincare, etc. |
| Product titles contain mixed info (brand + model + color + material) | LLM separates into structured fields for independent filtering |

### Extracted Fields

| Field | Description | Example |
|-------|-------------|---------|
| `base_product_name` | Core product name without model/color/variant qualifiers | "casence leather case" |
| `product_line` | Device model, series, or sub-category | "iphone 17 pro max" |
| `product_line_normalized` | Lowercased, trimmed version for exact-match filtering | "iphone 17 pro max" |
| `extracted_color` | Color variant extracted from title | "royal black" |
| `material` | Primary material type | "leather" |

### Extraction Flow

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  NormalizedProduct │──▶│  Build Prompt   │──▶│  Gemini 2.5     │──▶│  Parse JSON     │
│  (title, tags,  │    │  (title, tags,  │    │  Flash Lite     │    │  Response &     │
│   collections,  │    │   collections,  │    │  (temp=0.0)     │    │  Populate       │
│   options, etc) │    │   options, desc) │    │                 │    │  NormalizedProduct│
└─────────────────┘     └─────────────────┘     └─────────────────┘     └─────────────────┘
```

### Integration Points

LLM extraction runs in **all 5 ingestion paths**:

| Ingestion Path | Method | File |
|----------------|--------|------|
| Full ingestion API | `extract_batch_async()` | `services/product_ingestion/orchestrator.py` |
| Delta sync (weekly cron) | `extract_batch_async()` | `services/product_ingestion/orchestrator.py` |
| Webhook: product create | `extract_attributes()` (sync) | `shopify/webhook/product_webhook.py` |
| Webhook: product update | `extract_attributes()` (sync) | `shopify/webhook/product_webhook.py` |
| Demo chat ingestion | `extract_attributes()` (sync) | `demo_chat_router.py` |

### Graceful Degradation

LLM extraction is **non-blocking** — if it fails (API error, timeout, invalid JSON response), ingestion continues without structured fields. Semantic search still works, just without exact-match filtering precision.

---

## Observability & Tracking

### Product Sync Logs

The system tracks all product sync operations in a PostgreSQL table for monitoring, debugging, and analytics.

#### Table Schema

```sql
CREATE TABLE product_sync_logs (
    id SERIAL PRIMARY KEY,
    client_id UUID NOT NULL,
    
    -- Sync metadata
    sync_source VARCHAR(50) NOT NULL,  -- 'webhook', 'cron', 'manual_api'
    sync_type VARCHAR(100) NOT NULL,   -- e.g., 'products/create', 'weekly_delta_sync'
    status VARCHAR(20) NOT NULL,       -- 'success', 'partial_failure', 'failure'
    
    -- Product counts
    products_added INTEGER DEFAULT 0,
    products_updated INTEGER DEFAULT 0,
    products_deleted INTEGER DEFAULT 0,
    products_unchanged INTEGER DEFAULT 0,
    products_failed INTEGER DEFAULT 0,
    
    -- Single product details (for webhooks)
    product_id VARCHAR(100),
    product_title VARCHAR(500),
    
    -- Error tracking
    error_message TEXT,
    
    -- Performance
    duration_seconds DECIMAL(10, 3),
    
    -- Tracing
    trace_id VARCHAR(100),
    shopify_webhook_id VARCHAR(100),
    shopify_shop_domain VARCHAR(255),
    
    -- Timestamps
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX idx_product_sync_logs_client_id ON product_sync_logs(client_id);
CREATE INDEX idx_product_sync_logs_sync_source ON product_sync_logs(sync_source);
CREATE INDEX idx_product_sync_logs_created_at ON product_sync_logs(created_at);
```

#### Tracking Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        SYNC LOGGING ARCHITECTURE                            │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐           │
│  │     Webhook     │   │    Cron Job     │   │   Manual API    │           │
│  │    Handler      │   │    (Weekly)     │   │   (/ingest)     │           │
│  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘           │
│           │                     │                     │                     │
│           │ log_webhook_event() │ log_cron_event()   │ log_manual_api()    │
│           │                     │                     │                     │
│           └──────────────┬──────┴──────────┬──────────┘                     │
│                          ▼                 ▼                                │
│                 ┌─────────────────────────────────┐                         │
│                 │     ProductSyncLogger           │                         │
│                 │  (sync_logger.py)               │                         │
│                 └───────────────┬─────────────────┘                         │
│                                 │                                           │
│                                 ▼                                           │
│                 ┌─────────────────────────────────┐                         │
│                 │   PostgreSQL                    │                         │
│                 │   product_sync_logs table       │                         │
│                 └─────────────────────────────────┘                         │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

#### Example Log Entries

**Webhook Event (Product Created):**
```json
{
    "id": 1,
    "client_id": "123e4567-e89b-12d3-a456-426614174000",
    "sync_source": "webhook",
    "sync_type": "products/create",
    "status": "success",
    "products_added": 1,
    "product_id": "7654321098765",
    "product_title": "Classic White T-Shirt",
    "duration_seconds": 0.342,
    "trace_id": "TRC_20240115_143022_abc123",
    "shopify_webhook_id": "web_123456789",
    "shopify_shop_domain": "mystore.myshopify.com"
}
```

**Cron Job Event (Weekly Delta Sync):**
```json
{
    "id": 2,
    "client_id": "123e4567-e89b-12d3-a456-426614174000",
    "sync_source": "cron",
    "sync_type": "weekly_delta_sync",
    "status": "success",
    "products_added": 5,
    "products_updated": 12,
    "products_deleted": 0,
    "products_unchanged": 83,
    "products_failed": 0,
    "duration_seconds": 15.672
}
```

#### Statistics Query

Get sync statistics for a client over the last 7 days:

```sql
SELECT 
    sync_source,
    COUNT(*) as total_operations,
    SUM(products_added) as total_added,
    SUM(products_updated) as total_updated,
    SUM(products_deleted) as total_deleted,
    SUM(products_failed) as total_failed,
    COUNT(CASE WHEN status = 'success' THEN 1 END) as successful_ops,
    COUNT(CASE WHEN status = 'failure' THEN 1 END) as failed_ops
FROM product_sync_logs
WHERE client_id = 'your-client-id'
AND created_at >= NOW() - INTERVAL '7 days'
GROUP BY sync_source;
```

#### Use Cases

| Use Case | Query Approach |
|----------|----------------|
| Debug failed syncs | Filter by `status = 'failure'`, check `error_message` |
| Monitor webhook health | Count webhooks by day, check for gaps |
| Performance analysis | Average `duration_seconds` by sync_type |
| Capacity planning | Track growth in products_added over time |
| Audit trail | Filter by `client_id` and date range |

---

## Document Info

- **Version**: 2.0
- **Last Updated**: February 8, 2026
- **Status**: ✅ Implemented

### Changelog

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | — | Initial design document |
| 2.0 | Feb 8, 2026 | Added: LLM Attribute Extraction section (Gemini 2.5 Flash Lite), `product_line`/`segment` exact-match search filters, updated NormalizedProduct model with 5 new LLM-extracted fields, updated architecture diagram with LLM Attribute Extractor component, updated searchable text construction with structured format, updated ingestion/delta sync/webhook flows with LLM extraction step, updated content hash fields, corrected embedding model to BAAI/bge-m3. |

