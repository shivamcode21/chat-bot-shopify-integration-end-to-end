# Tags Config Update — Impact Analysis & Required Changes

> Impact analysis for updating the `client_configs` `tags` config key to the new
> message/conversation tag dictionary. Covers every code path that reads, writes,
> or matches against these tag names.

---

## 1. New Tag Dictionary (proposed)

```json
{
  "Escalations": "User expressing frustration or requesting to speak with a human agent.",
  "Order Update": "User wants to modify order details such as address, size, name, phone number, variant change, product change of an existing order.",
  "Size Inquiry": "User seeking help with sizing, fit, or size chart details.",
  "General Query": "User asking for general help or unclear requests not tied to a specific issue.",
  "Cart Addition": "Whenever a customer adds one or more products to their cart.",
  "Order Placed": "User has successfully placed an order.",
  "Pricing Query": "User asking about product prices, total cost, or price breakdown.",
  "Order Details Query": "Whenever a user has an already placed order and user is asking about price, size, for current order status, delivery status, refund status, return and exchange status or their order.",
  "Product Query": "User asking for product details such as material, features, restocking or availability.",
  "Delivery Query": "User asking about delivery timelines, tracking, or shipping before placing an order.",
  "Discount Query": "User inquiring about discounts, offers, or available coupons before placing an order.",
  "Product Recommendation": "User asking for or being offered product recommendations.",
  "Return Request": "User wants to return a received product or inquire about return pickup.",
  "Exchange Request": "User wants to exchange a received item for a different size, color, or variant.",
  "R&E Policy Query": "User asking about return or exchange policies, terms, or timelines.",
  "Wholesale Inquiry": "User asking about bulk or corporate order options and wholesale pricing.",
  "Cancellation Requests": "User wants to cancel an order or request a refund against a cancellation.",
  "Delivery Policy Query": "User asking about shipping policies, delivery coverage, or courier information.",
  "Payment Policy Query": "User asking about payment methods, terms, or related policies.",
  "My Company's Inquiry": "User asking about brand authenticity, company info, or business details.",
  "Offline Leads": "If the user asks for offline stores, or we have guided a user to offline store recommendations."
}
```

### Tag Name Diff (old → new)

| Old Tag Name          | New Tag Name            | Change Type |
|-----------------------|-------------------------|-------------|
| `Recommendation`      | `Product Recommendation` | Renamed     |
| `Order Status Query`  | `Order Details Query`    | Renamed     |
| `Order Created`       | `Order Placed`           | Renamed     |
| `Payment Options Query` | `Payment Policy Query` | Renamed     |
| `Preorder Inquiry`    | *(removed)*              | Removed     |
| `Back in Stock Inquiry` | *(removed)*            | Removed     |
| `Availability Inquiry` | *(removed)*             | Removed     |
| —                     | `Cart Addition`          | **New**     |
| —                     | `General Query`          | **New**     |
| —                     | `Delivery Policy Query`  | **New**     |
| —                     | `My Company's Inquiry`   | **New**     |
| —                     | `Offline Leads`          | **New**     |

### Unchanged Tags

`Escalations`, `Order Update`, `Size Inquiry`, `Pricing Query`, `Product Query`,
`Delivery Query`, `Discount Query`, `Return Request`, `Exchange Request`,
`R&E Policy Query`, `Wholesale Inquiry`, `Cancellation Requests`.

---

## 2. Where the `client_configs` `tags` Config Is Used

### Data flow

```
client_configs (Postgres, config_key='tags')
  └─→ aget_tags_with_caching()              [utils/utils.py]
       └─→ aensure_tags_loaded()            [tag_manager.py]
            ├─→ classify_interaction_tag()   [async_tag_generator.py]   ← real-time, per-message
            │    └─→ generate_tags_async()   ← called from:
            │         ├── gupshup_webhook.py       (WhatsApp)
            │         └── websocket_chat.py        (Web)
            │
            └─→ _batch_tag_and_lead_prompt() [conversation_inactivity_processor.py]  ← batch worker

Tags consumed downstream by:
  ├── conversation_analyzer.py    (lead classification)
  ├── intent_detection_node.py    (detected_tags in state)
  ├── langsmith_tracing.py        (observability)
  ├── schema.py                   (SupportState definition)
  ├── postgres_conversations.py   (conversation & message tag storage)
  ├── websocket_chat.py           (passes conversation_tags on message store)
  └── gupshup_webhook.py          (passes conversation_tags on message store)
```

### All files that reference tag values

| File | How it uses `tags` |
|------|--------------------|
| `utils/utils.py` | `aget_tags_with_caching()` — loads from `client_configs WHERE config_key = 'tags'` with 3-tier cache |
| `tag_manager.py` | `aensure_tags_loaded()` — in-memory cache wrapper around `aget_tags_with_caching()` |
| `async_tag_generator.py` | Formats tag dict into LLM prompt, classifies each message with one tag, writes to Postgres + Redis |
| `workers/conversation_inactivity_processor.py` | Batch tagging — loads tags via `aensure_tags_loaded()`, embeds in combined tag+lead LLM prompt |
| `analytics/conversation_analyzer.py` | Lead classification — reads per-message tags from Postgres, matches against `FALLBACK_LEAD_GENERATION_TAGS`, `FALLBACK_PREORDER_LEAD_TAGS`, `NON_LEAD_SOURCE_TAGS` |
| `nodes/intent_detection_node.py` | Hardcodes `detected_tags` values for specific paths (`"Recommendation"`, `"Escalations"`) |
| `utils/langsmith_tracing.py` | Captures `detected_tags` in trace snapshots |
| `schema.py` | Defines `detected_tags` and `conversation_tags` fields in `SupportState` |
| `history/postgres_conversations.py` | `aupdate_conversation_tags()`, `aupdate_message_tags()` — merges tags into DB rows |
| `websocket_chat.py` | Passes `conversation_tags` when storing inbound messages |
| `gupshup_webhook.py` | Passes `conversation_tags` when storing inbound messages |
| `cron_jobs/conversion_tag_job.py` | Separate config key (`conversion_tags`), not affected by this change |

---

## 3. Required Code Changes

### 3.1 `conversation_analyzer.py` — `FALLBACK_LEAD_GENERATION_TAGS`

**File:** `fashion_bot/analytics/conversation_analyzer.py`, lines 38–48

Two renames required:

| Old Value              | New Value               |
|------------------------|-------------------------|
| `"Recommendation"`     | `"Product Recommendation"` |
| `"Payment Options Query"` | `"Payment Policy Query"` |

**Decision needed — new tags to add as lead signals:**

| New Tag               | Rationale                                           | Recommendation |
|-----------------------|-----------------------------------------------------|----------------|
| `"Cart Addition"`     | Strong buying intent (customer added items to cart)  | **Add**        |
| `"Delivery Policy Query"` | Presales intent, analogous to existing `"Delivery Query"` | **Add** |
| `"Offline Leads"`     | By definition a lead (guided to offline store)       | **Add**        |

**Before:**
```python
FALLBACK_LEAD_GENERATION_TAGS: List[str] = [
    "Product Query",
    "Recommendation",
    "Size Inquiry",
    "Pricing Query",
    "Discount Query",
    "Delivery Query",
    "Payment Options Query",
    "Wholesale Inquiry",
    "R&E Policy Query",
]
```

**After:**
```python
FALLBACK_LEAD_GENERATION_TAGS: List[str] = [
    "Product Query",
    "Product Recommendation",
    "Size Inquiry",
    "Pricing Query",
    "Discount Query",
    "Delivery Query",
    "Delivery Policy Query",
    "Payment Policy Query",
    "Wholesale Inquiry",
    "R&E Policy Query",
    "Cart Addition",
    "Offline Leads",
]
```

### 3.2 `conversation_analyzer.py` — `NON_LEAD_SOURCE_TAGS`

**File:** `fashion_bot/analytics/conversation_analyzer.py`, lines 54–62

Two renames required (values are lowercase):

| Old Value (lowercase)  | New Value (lowercase)   |
|------------------------|-------------------------|
| `"order created"`      | `"order placed"`        |
| `"order status query"` | `"order details query"` |

**Before:**
```python
NON_LEAD_SOURCE_TAGS = {
    "cancellation requests",
    "escalations",
    "exchange request",
    "order created",
    "order status query",
    "order update",
    "return request",
}
```

**After:**
```python
NON_LEAD_SOURCE_TAGS = {
    "cancellation requests",
    "escalations",
    "exchange request",
    "order placed",
    "order details query",
    "order update",
    "return request",
}
```

### 3.3 `conversation_analyzer.py` — `FALLBACK_PREORDER_LEAD_TAGS`

**File:** `fashion_bot/analytics/conversation_analyzer.py`, lines 49–53

All three tags have been **removed** from the new dictionary. The LLM will never
classify a message with these tags, making these fallbacks dead code.

| Removed Tag            | Status       |
|------------------------|-------------|
| `"Preorder Inquiry"`   | Not in new dict |
| `"Back in Stock Inquiry"` | Not in new dict |
| `"Availability Inquiry"` | Not in new dict |

**Options:**

- **Option A (remove preorder lead type):** Empty the list. The `"preorder_interest"`
  lead type will stop being assigned. All leads become `"product_interest"` or
  `"wholesale_interest"`.

  ```python
  FALLBACK_PREORDER_LEAD_TAGS: List[str] = []
  ```

- **Option B (keep preorder tracking):** Add equivalent tags to the new tag
  dictionary and update the fallback list to match:

  ```json
  "Preorder Inquiry": "User asking about preorder or back-in-stock availability."
  ```

  ```python
  FALLBACK_PREORDER_LEAD_TAGS: List[str] = [
      "Preorder Inquiry",
  ]
  ```

### 3.4 `conversation_analyzer.py` — Hardcoded `"wholesale inquiry"` check

**File:** `fashion_bot/analytics/conversation_analyzer.py`, line 733

```python
elif any(tag.lower() == "wholesale inquiry" for tag in matched_lead_tags):
    lead_type = "wholesale_interest"
```

**No change needed.** `"Wholesale Inquiry"` exists in the new dictionary with the
same name.

### 3.5 `nodes/intent_detection_node.py` — Hardcoded `"Recommendation"` tag

**File:** `fashion_bot/nodes/intent_detection_node.py`, line 664

```python
"detected_tags": ["Recommendation"],
```

**Change to:**
```python
"detected_tags": ["Product Recommendation"],
```

> Note: `detected_tags` is currently set to `[]` at runtime (tagging was moved
> to async post-reply), so this value is only hit in the recommendations
> follow-up edge case. The `"Escalations"` references (lines 696, 721, 736) are
> unchanged — that tag name was not renamed.

### 3.6 `global_configs` DB table — `lead_generation_tags` row

If the `global_configs` table has a `lead_generation_tags` entry, it must be
updated to reflect the renamed tags. The DB config takes precedence over the
fallback constants above, so stale DB values referencing old names would cause
tag matching to silently fail.

**Check with:**
```sql
SELECT config_value
FROM global_configs
WHERE config_key = 'lead_generation_tags';
```

If a row exists, update the tag names to match the new dictionary.

---

## 4. No Changes Needed

These files/systems are **not affected** because they don't hardcode tag names:

| Component | Why no change |
|-----------|---------------|
| `utils/utils.py` | Reads tag dict from DB generically |
| `tag_manager.py` | Caches tag dict generically |
| `async_tag_generator.py` | Passes tag dict to LLM prompt dynamically |
| `conversation_inactivity_processor.py` | Passes tag dict to LLM prompt dynamically |
| `schema.py` | Defines tag fields as `List[str]` (no name checks) |
| `history/postgres_conversations.py` | Stores/merges tags generically |
| `websocket_chat.py` | Passes `conversation_tags` through without inspecting values |
| `gupshup_webhook.py` | Passes `conversation_tags` through without inspecting values |
| `utils/langsmith_tracing.py` | Captures tags for observability without matching |
| `cron_jobs/conversion_tag_job.py` | Uses separate `conversion_tags` config key |

---

## 5. Rollout Considerations

1. **Historical data mismatch:** Existing messages in Postgres have tags from the
   old dictionary (e.g., `"Recommendation"`, `"Order Status Query"`). The
   analytics job will try to match these old tags against the updated
   `FALLBACK_LEAD_GENERATION_TAGS` / `NON_LEAD_SOURCE_TAGS` and **fail to match**
   the renamed ones. This affects lead classification accuracy for conversations
   that span the migration boundary.

2. **Cache TTL:** After updating the `client_configs` row, the old tag dictionary
   will persist in:
   - Redis cache (`tags:{client_id}`) — TTL 600s (10 min)
   - In-memory cache (`_TAGS_CACHE` in `tag_manager.py`) — until process restart

   Either wait ~10 minutes or restart the service to pick up the new config
   immediately.

3. **Testing:** After deployment, verify with a test message that the LLM
   correctly classifies against the new tag names by checking:
   - The `tags` column in the `messages` table
   - The `conversation_tags` field in Redis state
   - The analytics cron output for lead classification
