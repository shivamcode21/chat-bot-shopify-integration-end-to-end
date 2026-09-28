# Design Doc: Product Image & Carousel Data Flow

## Problem

When the search pipeline (Upstash Search) returns product results, each result includes an `image_url` in its metadata. This image URL needs to reach the frontend so product cards can render with thumbnails. The architecture has two delivery surfaces:

1. **WebSocket chat** (`websocket_chat.py`) — a LangGraph agent where the LLM response flows through tool messages, state transitions, and finally a WebSocket send.
2. **Demo chat** (`demo_chat_router.py`) — a FastAPI router in the main app serving `/demo/chat` (JSON) and `/demo/chat/stream` (SSE). Uses OpenAI function calling with the same search pipeline as the LangGraph agent.

## Solution: Structured Dict Data Flow

Product data flows as structured dicts through the system. Each layer reads only the fields it needs:

- **Tool / `ProductSearchService`** returns full Upstash documents (content + metadata merged) as a dict.
- **Tool loop / Demo chat** serializes a cleaned JSON for the LLM via the shared `format_products_for_llm()` utility (`product_utils.py`). Internal-only keys are stripped; all product attributes the LLM may need (variants, care instructions, size chart, etc.) are preserved.
- **Skill node** reads the raw products from `intermediate_steps` and passes them as `recent_products` in state.
- **`match_products_to_reply()`** filters the product list to only those the LLM mentioned in its reply, capped at 3 (`MAX_PRODUCT_CARDS`). This ensures the carousel matches the text.
- **WebSocket** reads `recent_products` from state, filters via `match_products_to_reply()`, formats for the frontend carousel, and sends inline.
- **Demo chat** filters via `match_products_to_reply()`, formats via `_format_product_for_chrome()` (mapping Upstash field names to Chrome extension schema), and returns in the JSON response or SSE `done` event.

No delimiters. No regex parsing. No SystemMessage hacking. No async tasks.

---

## Architecture: Responsibility Boundaries

| Component | Responsibility |
|---|---|
| **Recommendation Handler LLM** | Routes to tool (passes raw user query), curates top 3 results, composes customer response |
| **Tool factory** (`_build_conv_history`, `_extract_focal_context`) | Pre-computes context from state at creation time (WebSocket path only) |
| **Tool** (`search_and_recommend_products`) | Calls search pipeline, merges content+metadata, returns full Upstash docs. **Stateless** — no state reads or writes, no formatting |
| **QU** (`understand_query`) | All query refinement: semantic query, hard filters, follow-up questions |
| **`format_products_for_llm()`** (`product_utils.py`) | Shared formatter: serializes full Upstash docs as clean JSON (strips internal keys like `client_id`, `content_hash`; strips empty values). Schema-agnostic — any attribute present in Upstash (fashion, FMCG, etc.) passes through to the LLM |
| **`match_products_to_reply()`** (`product_utils.py`) | Filters product list to only those whose title appears in the LLM reply, capped at `max_results` (default 3). Falls back to first N by reranker order if no matches |
| **`_format_product_for_chrome()`** (`demo_chat_router.py`) | Maps Upstash field names to Chrome extension card schema (`price_min` → `₹1299`, `image_url` → `image`, `product_url` → `url`, `in_stock` → `available`, plus discount calculation) |
| **Tool loop** (`_invoke_native_tool_loop`) | Calls `format_products_for_llm()` to build the `ToolMessage` content |
| **Skill node** (`generic_skill_node.py`, post-loop) | Reads raw products from `intermediate_steps`, calls `add_selectable_entities`, passes `recent_products` in response dict |
| **`websocket_chat.py`** | Reads `state["recent_products"]`, filters via `match_products_to_reply()`, formats via `format_product_for_carousel()`, sends text + carousel inline |
| **`demo_chat_router.py`** | Own OpenAI function-calling loop via `EnhancedLLMService`, calls `ProductSearchService.search()` (same `search_products_pipeline` + `format_products_for_llm`). Filters via `match_products_to_reply()`, formats via `_format_product_for_chrome()`. Page context from Chrome extension scraping + Shopify public APIs. Returns formatted cards in JSON/SSE |
| **Gupshup webhook** | Sends clean text only. No product cards (WhatsApp handles product display separately) |

---

## Tool Design: Stateless with Factory Pre-computation

The `search_and_recommend_products` tool is fully stateless. It does not read or write `state` variables. All context it needs is pre-computed when the tool factory creates the closure:

```
_get_recommendations_tools(state, messages_list, client_id)
    │
    ├── _build_conv_history(messages_list)
    │     Converts last 20 LangChain messages into [{role, content}] dicts
    │
    ├── _extract_focal_context(state)
    │     Navigates conversation_context → focal_entity → full_data
    │     Returns (exclude_handle, focal_product_context)
    │
    └── Creates tool closures capturing:
          _conv_history, _exclude_handle, _focal_product_context, client_id
```

### Tool Signature

```python
search_and_recommend_products(query: str) -> dict
```

The tool takes only the raw user query. The LLM is instructed to pass the customer's message as-is without rephrasing. Query Understanding (QU) handles all refinement — semantic query optimization, hard filter extraction (category, price, size, segment), and follow-up question generation.

### Tool Return Value

```python
{
    "products": [
        {
            # Full Upstash document (content + metadata merged)
            "title": "Sunny Cotton Shirt",
            "description": "Men Yellow Solid...",
            "category": "topwear",
            "subcategory": "shirt",
            "brand": "Raymond",
            "colors": ["Yellow"],
            "sizes": ["S", "M", "L", "XL"],
            "material": "cotton",
            "fit": "slim fit",
            "occasion": ["casual"],
            "pattern": "solid",
            "segment": "men",
            "price_min": 1299,
            "compare_at_price_min": 1799,
            "in_stock": True,
            "handle": "sunny-cotton-shirt",
            "image_url": "https://cdn.shopify.com/...",
            "all_images": ["https://cdn.shopify.com/...", ...],
            "product_url": "https://store.com/products/sunny-cotton-shirt",
            "variants": [...],
            ...
        },
        ...  # up to 5 products
    ],
    "follow_up": "Are you looking for formal or casual shirts?"
}
```

The tool is a pure data retrieval layer. It returns everything Upstash stores without cherry-picking fields or formatting text.

---

## End-to-End Walkthrough: "Show me yellow shirts"

### Step 1: WebSocket receives the message

`websocket_chat.py` receives `"Show me yellow shirts"` and calls `process_message_streaming(...)`, which invokes the LangGraph agent.

### Step 2: Intent detection

`detect_intent_node` (gpt-4o-mini) classifies the message as product discovery and routes to the **recommendation handler** skill node.

### Step 3: LLM decides to call the tool

The recommendation handler LLM (gpt-4o) sees the `search_and_recommend_products` tool and calls it with the raw user message:

```json
{"name": "search_and_recommend_products", "arguments": {"query": "Show me yellow shirts"}}
```

### Step 4: Tool executes (pure data retrieval)

Inside `search_and_recommend_products` (`tool_registry.py`):

**4a. Query Understanding (gpt-4o)**

QU receives the raw query + conversation history + focal product context:

```
Input:  "Show me yellow shirts"
Output: {"query": "yellow shirts casual", "filter": "subcategory = 'shirt' AND in_stock = true", "follow_up": "Are you looking for formal or casual shirts?"}
```

**4b. Upstash Search**

```
Input:  query="yellow shirts casual", filter="subcategory = 'shirt' AND in_stock = true", limit=10
Output: Up to 10 raw product documents (each with content + metadata)
```

**4c. Business Rules Reranker**

```
Input:  10 candidates + user context (occasions, size, budget)
Output: Top 5 products (after occasion boosts, diversity penalties, discount boosts)
```

**4d. Merge content + metadata**

```python
for p in pipeline.products:
    merged = {}
    merged.update(p.get("content", {}))
    merged.update(p.get("metadata", {}))
    products.append(merged)
```

**Tool returns:**
```python
{"products": [5 full Upstash docs], "follow_up": "Are you looking for formal or casual shirts?"}
```

### Step 5: Tool loop processes the dict

In `_invoke_native_tool_loop` (`generic_skill_node.py`):

**`intermediate_steps`** stores the raw dict (all 5 full Upstash documents with every field).

The tool loop detects `"products" in observation` and calls `_format_products_for_llm()` to serialize the products as JSON for the `ToolMessage`. Internal-only keys (`client_id`, `content_hash`, `product_id`, `seo_title`, `seo_description`, `product_line_normalized`, `base_product_name`, `all_images`) and empty values are stripped. Everything else passes through:

```json
{
  "products": [
    {
      "title": "Sunny Cotton Shirt",
      "description": "Men Yellow Solid...",
      "category": "topwear",
      "subcategory": "shirt",
      "brand": "Raymond",
      "colors": ["Yellow"],
      "sizes": ["S", "M", "L", "XL"],
      "material": "cotton",
      "fit": "slim fit",
      "occasion": ["casual"],
      "pattern": "solid",
      "segment": "men",
      "price_min": 1299,
      "compare_at_price_min": 1799,
      "in_stock": true,
      "total_inventory": 42,
      "handle": "sunny-cotton-shirt",
      "image_url": "https://cdn.shopify.com/...",
      "product_url": "https://store.com/products/sunny-cotton-shirt",
      "variants": [{"title": "S / Yellow", "price": "1299", "available": true}, ...],
      "care_instructions": "Machine wash cold...",
      "size_chart": {"S": {"chest": "38"}, ...}
    },
    ...
  ],
  "follow_up": "Are you looking for formal or casual shirts?"
}
```

The LLM sees the **full product JSON** — all attributes including variants, care instructions, size charts, stock levels, etc. The formatter is schema-agnostic: any attribute stored in Upstash (fashion, FMCG, beauty, etc.) passes through automatically.

### Step 6: LLM curates top 3

The LLM reads the ToolMessage with 5 products. Following the prompt ("Pick the top 3 most relevant products"), it writes:

```
Here are some great yellow shirts for you! 👕
1. Sunny Cotton Shirt — Rs. 1,299 (MRP Rs. 1,799)
https://store.com/products/sunny-cotton-shirt
2. Lemon Linen Shirt — Rs. 1,599
https://store.com/products/lemon-linen-shirt
3. Pale Yellow Oxford — Rs. 1,899
https://store.com/products/pale-yellow-oxford

Are you looking for formal or casual shirts?
```

### Step 7: Skill node extracts recent_products

After the LLM finishes, the skill node reads the raw dict directly from `intermediate_steps`:

```python
recent_products = []
for step in intermediate_steps:
    obs = step[1]
    if isinstance(obs, dict) and obs.get("products"):
        recent_products = obs["products"]          # all 5 full Upstash docs
        add_selectable_entities(context, recent_products, ...)
        break
```

No parsing. A direct dict key lookup.

### Step 8: Skill node returns response

```python
response = {
    "type": "customer_message",
    "customer_message": "Here are some great yellow shirts...",   # LLM's curated text (3 products)
    "conversation_context": context,
    "messages": updated_messages,
    "recent_products": [... all 5 full Upstash docs ...],         # full data for carousel filtering
}
```

LangGraph merges this into `SupportState`. `state["recent_products"]` is populated. On subsequent turns without product results, `deletable_field_reducer` clears it.

### Step 9: WebSocket streams text

```json
{"type": "stream_token", "token": "Here "}
{"type": "stream_token", "token": "are some "}
...
{"type": "stream_end"}
```

### Step 10: WebSocket sends product carousel inline

Immediately after `stream_end`, `match_products_to_reply()` filters the 5 candidates to only the products the LLM mentioned in its reply (capped at 3):

```python
recent_products = session["state"].get("recent_products") or []
if recent_products:
    matched = match_products_to_reply(recent_products, response or "")
    formatted = [format_product_for_carousel(p) for p in matched if p]
    formatted = [f for f in formatted if f]
    if formatted:
        await asyncio.sleep(0.3)
        await websocket.send_json({
            "type": "products",
            "products": formatted,
            "timestamp": "..."
        })
```

`format_product_for_carousel` extracts only what the frontend needs from each full Upstash doc:

```json
{
    "type": "products",
    "products": [
        {"title": "Sunny Cotton Shirt", "handle": "sunny-cotton-shirt", "url": "https://store.com/products/sunny-cotton-shirt", "price": "1299", "image_url": "https://cdn.shopify.com/sunny.jpg"},
        {"title": "Lemon Linen Shirt", ...},
        {"title": "Pale Yellow Oxford", ...}
    ]
}
```

### Step 11: Frontend renders

- **Text bubble**: LLM's curated response mentioning **3 products** with URLs.
- **Image carousel**: Scrollable horizontal carousel showing the **same 3 products** with `image_url` as `<img>` tags, plus title, price, and link. The carousel matches the text exactly.

---

## Flow 2: Demo Chat Router (`demo_chat_router.py`)

Part of the main FastAPI app. Serves `/demo/chat` and `/demo/chat/stream` for the Chrome extension demo. Uses its own OpenAI function-calling loop but shares the same search pipeline and formatting logic as the LangGraph agent.

### What it shares with the LangGraph agent

| Shared Component | How |
|---|---|
| **Search pipeline** | `ProductSearchService.search()` calls `search_products_pipeline()` — same QU, Upstash Search, Business Rules Reranker |
| **Product formatting** | Uses `format_products_for_llm()` from `product_utils.py` — same JSON serialization |
| **Content+metadata merge** | Same merge logic as `tool_registry.py` — full Upstash docs |
| **max_results** | 5 (reranker picks 5, LLM curates top 3) |
| **Conversation history** | Last 20 messages for both LLM context and QU context |
| **Follow-up relay** | Prompt instructs LLM to ask follow-up from QU |
| **Top 3 curation** | Prompt instructs LLM to pick 3 most relevant products |
| **Function calling** | LLM autonomously decides when to call `search_products` tool |

### What it handles additionally

| Area | Details |
|---|---|
| **Page context** | Product details, pricing, discounts from Chrome extension scraping |
| **JSON-LD variants** | `_build_current_product_from_jsonld()` for per-variant stock/price data |
| **Shopify policies** | `ShopifyFetcher.fetch_policies()` for return/shipping/refund policies |
| **Order handling** | Mock order/cart flows via `_initiate_order_response`, `_complete_order`, `_order_status_response` |
| **SSE streaming** | `AsyncOpenAI` for non-blocking function calling + streaming second call |

### Walkthrough: "Show me yellow shirts"

```
User: "Show me yellow shirts"
  │
  ▼
_prepare_chat_context()
  ├─ Checks for order intents (short-circuit if order/status)
  ├─ Builds current_product from JSON-LD (variant stock data)
  ├─ Fetches Shopify policies (if policy intent detected)
  ├─ Returns shopify_data (current_product, policies) — NO search results
  │
  ▼
EnhancedLLMService.get_response() / get_response_streaming()
  ├─ Builds system prompt with page context + policies + recommendation rules
  ├─ Includes conversation history (last 20 messages)
  ├─ 1st OpenAI call (with search_products tool): LLM decides to call search_products(query="Show me yellow shirts")
  │
  ▼
ProductSearchService.search()
  ├─ Resolves client_id via get_pg_client_id_for_domain(domain)
  ├─ Builds conv_history from last 20 messages
  ├─ Extracts focal_product_context + exclude_handle from page context
  ├─ search_products_pipeline(): QU → Upstash (10) → Reranker (top 5)
  ├─ Merges content + metadata into flat dicts (same as tool_registry)
  ├─ format_products_for_llm(products, follow_up) → clean JSON string
  ├─ Returns (formatted_json, [5 full merged Upstash docs])
  │
  ▼
EnhancedLLMService (continued)
  ├─ Appends formatted_json as tool message
  ├─ 2nd OpenAI call (streaming for /stream, non-streaming for /chat)
  ├─ LLM curates top 3, composes response, relays follow-up
  ├─ match_products_to_reply() → filters to products LLM mentioned (max 3)
  ├─ _format_product_for_chrome() → maps fields to Chrome extension schema
  ├─ Returns (reply_text, [3 formatted Chrome cards])
  │
  ▼
/demo/chat endpoint → JSON: {"reply": "...", "products": [3 Chrome cards]}
/demo/chat/stream  → SSE: event:thinking → event:token (streamed) → event:done with products
  │
  ▼
Chrome Extension (content.js)
  ├─ Shows typing indicator ("...") until first SSE event arrives
  ├─ On event:thinking → shows "Searching products..." in message bubble
  ├─ On event:token → replaces with streamed LLM text
  ├─ On event:done → renders image carousel from products (image, title, price, url, discount)
```

### Query types

```
A) PRODUCT DETAILS — "What material is this shirt?"
   ├─ Answered from PAGE CONTEXT in system prompt (Chrome extension scraping + JSON-LD)
   ├─ LLM does NOT call search_products
   ├─ No product cards returned

B) DISCOVERY / RECOMMENDATIONS — "Show me blue jeans"
   ├─ LLM calls search_products(query="Show me blue jeans")
   ├─ Same pipeline as LangGraph agent (QU → Search → Rerank)
   ├─ Same JSON formatting (format_products_for_llm)
   ├─ LLM curates top 3, carousel shows same 3 (matched + capped)

C) ORDERS — "Buy this" / "Add to cart" / "Track my order"
   ├─ Intercepted in _prepare_chat_context (short-circuit, no LLM call)
   ├─ Returns mock order/cart/status confirmation

D) POLICIES — "What's the return policy?"
   ├─ Policies fetched from Shopify public API, injected into system prompt
   ├─ LLM answers from policy text in prompt
```

### Streaming flow detail (`/demo/chat/stream`)

```
1. _prepare_chat_context()          → shopify_data (current_product, policies)
2. AsyncOpenAI: 1st call (non-streaming, with tools)
   ├─ If tool call:
   │   ├─ event: thinking (Chrome extension shows "Searching products...")
   │   ├─ await ProductSearchService.search()
   │   ├─ AsyncOpenAI: 2nd call (streaming)
   │   ├─ event: token → token → token
   │   ├─ match_products_to_reply() → filter to mentioned products (max 3)
   │   ├─ _format_product_for_chrome() → map to Chrome extension schema
   │   └─ event: done {products: [3 Chrome cards], metadata}
   └─ If no tool call: yield content from 1st response
       └─ event: token → done {products: [], metadata}
```

The Chrome extension keeps the typing indicator ("...") until the first SSE event arrives (`thinking`, `token`, or `error`), ensuring visual feedback throughout the 1st LLM call + search pipeline delay.

---

## Gupshup (WhatsApp) — No Impact

The Gupshup webhook sends plain text over WhatsApp. It extracts `result["customer_message"]` from the LangGraph output, which is the clean LLM response. Product data lives in `state["recent_products"]`, which Gupshup never inspects. No image rendering logic exists in the Gupshup flow.

---

## Data Flow Summary

### WebSocket / LangGraph path

```
Tool → dict{products: [full Upstash docs], follow_up} → intermediate_steps
  │
  ├─ Tool loop: format_products_for_llm() → JSON ToolMessage for LLM
  │
  ├─ LLM: curates top 3, composes customer response
  │
  ├─ Skill node: reads obs["products"] → recent_products → state
  │
  ├─ match_products_to_reply() → filters to products LLM mentioned (max 3)
  │
  └─ WebSocket: format_product_for_carousel → inline send (3 cards)
```

### Demo chat router path

```
OpenAI function call → ProductSearchService.search()
  │
  ├─ search_products_pipeline(): QU → Upstash (10) → Reranker (top 5)
  │
  ├─ Merge content + metadata → [5 full Upstash docs]
  │
  ├─ format_products_for_llm() → clean JSON tool message (same shared utility)
  │
  ├─ 2nd OpenAI call: LLM curates top 3, composes response
  │
  ├─ match_products_to_reply() → filters to products LLM mentioned (max 3)
  │
  ├─ _format_product_for_chrome() → maps to Chrome extension card schema
  │
  ├─ /demo/chat: returns (reply, [3 Chrome cards]) in JSON
  │
  └─ /demo/chat/stream: event:thinking → tokens via SSE → event:done with 3 Chrome cards
```

Both paths share `search_products_pipeline`, `format_products_for_llm`, `match_products_to_reply`, merge logic, max_results=5, and history depth=20.

---

## Key Design Decisions

1. **Tools are stateless**: The `search_and_recommend_products` tool does not read or write state variables. Context (conversation history, focal product, exclude handle) is pre-computed at factory time and captured in closures. State mutation (selectable entities) is performed by the skill node after the tool loop.

2. **Tool returns raw data**: The tool merges Upstash `content` + `metadata` into flat dicts and returns them without cherry-picking fields or formatting text. Each consumer layer (tool loop, skill node, carousel formatter) takes only the fields it needs.

3. **Shared LLM-facing formatter (JSON)**: `format_products_for_llm()` lives in `product_utils.py` and is used by both the LangGraph tool loop and the demo chat router's `ProductSearchService`. It serializes full product dicts as clean JSON, stripping only internal keys (`client_id`, `content_hash`, etc.) and empty values. This gives the LLM access to all product attributes — variants, care instructions, size charts, stock levels — enabling richer answers. The formatter is schema-agnostic: any attribute stored in Upstash passes through automatically.

4. **QU owns all query refinement**: The recommendation handler LLM passes the raw user message to the tool. Query Understanding (a separate gpt-4o call inside the search pipeline) handles semantic query optimization, hard filter extraction, and follow-up generation.

5. **LLM curates, does not retrieve**: The tool returns 5 products. The LLM prompt instructs it to pick the top 3 most relevant based on context (climate, occasion, user preferences). The carousel shows only the products the LLM mentioned (via `match_products_to_reply()`), capped at 3, so text and carousel always match.

6. **Inline carousel send**: After streaming completes, `state["recent_products"]` is filtered by `match_products_to_reply()`, formatted, and sent with a 300ms delay (for UX — lets text render first). No async tasks, no message scanning.

7. **`deletable_field_reducer` for cleanup**: `recent_products` in `SupportState` uses `deletable_field_reducer`, so it is automatically cleared on turns where no products are returned.

8. **Image URL source**: The `image_url` originates from Upstash Search metadata, populated during product ingestion. It is never fetched live from Shopify or any external API at query time.

9. **Conversation history depth**: The search pipeline receives the last 20 messages so QU has sufficient context for multi-turn reference resolution.

10. **Follow-up questions**: QU generates follow-up questions (e.g., "Are you looking for formal or casual shirts?"). They are embedded in the formatted text as "Suggested follow-up: ..." and the recommendation handler LLM incorporates them naturally into its response.

11. **Demo chat router alignment**: `demo_chat_router.py` uses its own OpenAI function-calling loop (not the LangGraph graph), but reuses the same search pipeline (`search_products_pipeline`), product formatter (`format_products_for_llm`), product-to-reply matching (`match_products_to_reply`), content+metadata merge pattern, `max_results=5`, and `history[-20:]`. The LLM autonomously decides when to call `search_products` via function calling, eliminating the need for a separate intent detection step for search decisions. Product detail queries are answered from page context (Chrome extension scraping + JSON-LD), and policies come from Shopify public APIs.

12. **AsyncOpenAI for streaming**: The demo chat router's streaming endpoint uses `AsyncOpenAI` so the non-streaming first call (tool decision), the async search pipeline, and the streaming second call (response generation) all run without blocking the event loop. An `event: thinking` SSE event is sent immediately when a tool call is detected, so the Chrome extension can show a "Searching products..." indicator during the search pipeline delay.

13. **Chrome extension card formatting**: `_format_product_for_chrome()` maps Upstash field names to the Chrome extension's expected schema: `price_min` → formatted `₹1299`, `image_url` → `image`, `product_url` → `url`, `in_stock` → `available`, plus discount percentage calculation from `compare_at_price_min`.

14. **Typing indicator deferred removal**: The Chrome extension keeps the "..." typing indicator visible until the first SSE event arrives (not at `response.ok`), ensuring visual feedback throughout the 1st LLM call + search pipeline delay.
