# Inbound WhatsApp Image Analysis (OCR + Product Vision)

Extract text and recognize products from customer-sent WhatsApp images so the
bot can understand and act on screenshots, order confirmations, payment
receipts, error pages, and product photos instead of always replying with
"I can't view images."

## Problem

When a customer sends an image on WhatsApp, the bot short-circuits with a
canned "I'm not able to view images yet" reply and skips the agent graph
entirely. But many customer images contain actionable content:

1. **Text-bearing images**: Order ID screenshots, payment confirmations, error
   messages, product labels — the bot could understand and respond if the text
   were extracted.
2. **Product photos**: Customers send photos of products they want to find or
   ask about — the bot could identify the product and search the catalog.

## Solution

Before the media short-circuit in `gupshup_webhook.py`, run a single vision
LLM call (Gemini 2.5 Flash Lite) that combines OCR and product recognition.

- **OCR text found** → rewrite `message_type` to `"text"`, compose extracted
  text (plus any caption) as the message, enter the agent graph normally.
- **Product detected (no OCR text)** → rewrite `message_type` to `"text"`,
  compose a natural-language product description as the message, enter the
  agent graph (routed to `product_details` or `recommendations` intent).
- **Neither** → fall through to the existing short-circuit unchanged.

Video, audio, and file attachments are not affected.

### Decision matrix

| Image has text? | Product photo? | User sent caption? | Behavior |
|---|---|---|---|
| Yes | (any) | Yes | OCR text appended to caption, sent to graph |
| Yes | (any) | No | OCR text becomes the message, sent to graph |
| No | Yes | Yes | Product summary appended to caption, sent to graph |
| No | Yes | No | `"I'm looking for {product_summary}"` sent to graph |
| No | No | Yes | Existing flow: canned reply + caption stored |
| No | No | No | Existing flow: canned reply |

OCR takes priority over product vision, subject to a length floor — if the image has readable text (e.g.,
a receipt with product names), the OCR path is used regardless of whether a
product is also detected.

## Architecture

```
WhatsApp image webhook
       │
       ▼
_extract_runtime_message_content() → (message_type="image", message_content)
       │
       ▼
message_type == "image"?
       │ YES
       ▼
_extract_media_url_from_payload() → Gupshup temporary URL
       │
       ▼
aanalyze_inbound_image(url, client_id)
 ├─ Feature flag check (inbound_image_ocr_enabled)
 ├─ Single vision LLM call (Gemini 2.5 Flash Lite, 10s timeout)
 │   └─ Combined prompt: OCR + product attribute extraction
 └─ Returns: InboundImageAnalysis or None
       │
       ├─ ocr_text found → rewrite message_type="text"
       │                    compose "[Text from image]: ..."
       │                    → enter agent graph normally
       │
       ├─ product detected (no OCR text) → rewrite message_type="text"
       │                                    compose "[Product from image]: ..."
       │                                    → enter agent graph → recommendations/product_details
       │
       └─ None (nothing actionable / timeout / error / flag off)
                → fall through to existing media short-circuit
```

### Components

**`fashion_bot/utils/inbound_image_ocr.py`**

Stateless async utility with two entry points:

```python
@dataclass
class InboundImageAnalysis:
    ocr_text: Optional[str] = None
    is_product_image: bool = False
    product_summary: Optional[str] = None
    product_attributes: Optional[Dict[str, str]] = None

async def aanalyze_inbound_image(
    image_url: str,
    client_id: str,
    trace_id: Optional[str] = None,
    timeout_seconds: float = 5.0,
) -> Optional[InboundImageAnalysis]:
    ...

# Backward-compatible wrapper (returns OCR text only)
async def aextract_text_from_inbound_image(...) -> Optional[str]:
    ...
```

- Uses Gemini 2.5 Flash Lite via OpenRouter (background key 2).
- Falls back to Gemini-direct when OpenRouter key is absent.
- Single LLM call handles both OCR and product vision (no double-call).
- 10-second `asyncio.wait_for` timeout (`INBOUND_OCR_TIMEOUT_SECONDS` to retune).
  A warm call measured ~3.5s in production; the original 5s left ~1.5s of
  headroom and cold starts blew through it. The LLM client is constructed
  outside the budget so that construction cannot eat the vision call's time.
- No LangSmith tracing (`config={"callbacks": []}`).
- No Postgres caching — inbound images are one-shot.
- Fail-open: returns `None` on any failure path.
- `aextract_text_from_inbound_image` is kept as a backward-compatible alias.

**`gupshup_webhook.py`** (modified)

Payload helpers:
- `_extract_media_url_from_payload(data, trace_id, client_id)` — pulls Gupshup
  image URL; logs a warning on a malformed payload instead of failing silently.
- `_extract_caption_from_payload(data, trace_id, client_id)` — pulls user's
  caption text, with the same warning log on a malformed payload.

The analysis step itself lives in one shared coroutine:

```python
async def _aanalyze_inbound_image_message(
    data, message_type, message_content, trace_id, client_id, sender_phone,
) -> tuple[str, str]:
    ...
```

It returns the (possibly rewritten) `(message_type, message_content)` and is a
no-op for non-image types. Both `_execute_turn_run_graph_and_update_state_for_gupshup`
(live streaming path) and `_execute_runtime_turn_core` (legacy path) call it
immediately before the existing `if message_type in MEDIA_MESSAGE_TYPES`
short-circuit, so the two paths cannot drift apart.

### How product images reach the right agent

When a product image is detected, the composed message looks like:

- `"I'm looking for black oversized cotton t-shirt for men"` (no caption)
- `"Show me this\n[Product from image]: black oversized cotton t-shirt for men"` (with caption)

This natural-language text enters intent detection, which routes to
`recommendations` or `product_details` intent. The skill node then calls
`search_products` → Query Understanding (QU) → Upstash Search. The product
summary is designed to be search-friendly so QU extracts effective filters
(category, color, etc.) from it naturally.

If the product is not in the store's catalog (e.g., footwear on an innerwear
store), the existing "SEARCH RESULT RELEVANCE CHECK" logic in the agent
prompts handles it gracefully — the agent responds that those products are
not available.

### Image-sourced queries must not assert absence

`[Product from image]:` / `[Text from image]:` messages are a machine's reading of
a photo. They carry no product name or link, so the agent cannot establish from
one that a specific item is or is not in the catalog. The `SEARCH RESULT
RELEVANCE CHECK` block was written for category misses ("customer wants
sneakers, we sell shirts") and the model was applying it at item level — telling
a customer their photographed item was unavailable, in one case while listing
what was very likely that exact item among the alternatives.

An `IMAGE-SOURCED QUERIES` block in the `product_details` and `recommendations`
defaults (`utils/context_helpers.py`) scopes that wording: present the closest
matches as possibilities, never assert the specific item is unavailable, and ask
for the name or link.

`DEFAULT_PROMPTS` is only the fallback — `generic_skill_node` resolves
`"<agent>_handler"` from `agents_config` first. Clients with rows there need
`scripts/image_sourced_query_prompt_20260824.sql`, which is idempotent and
backs up the table first.

### Ordering requirement (do not move the analysis call)

`_aanalyze_inbound_image_message` must run **before** the turn is built around
`message_content` — before `astore_message_event_with_conversation_resolution`,
before `aget_or_create_state`, and before the trace I/O is set. Both webhook
paths call it immediately after `_extract_runtime_message_content`.

This is not cosmetic. `aget_or_create_state` appends its `initial_message` to
`state["messages"]`, and the graph runs on `state`: `stream_graph_response`
deliberately does *not* re-add its `message` argument (`websocket_chat.py` owns
that). So an analysis that runs after state creation rewrites a local variable
the graph never reads — the OCR result reaches a LangSmith label and nothing
else, and the agent keeps answering from the `[Image message]` placeholder.
The same ordering keeps the transcript row and the media-backfill
`placeholder_text` in agreement, since `aattach_media_to_message` matches the
row on exact text and silently no-ops on a mismatch.

`tests/test_inbound_image_ocr.py::TestAnalysisRunsBeforeStateIsBuilt` pins this.

### OCR-vs-product priority

A recognisable product with a usable description is a **product query**, however
much text is printed on it. Everything else — screenshots, receipts, order
confirmations, and product photos the model could not describe — is an OCR
message. The rule lives on `InboundImageAnalysis.prefer_ocr()` so the utility
and the webhook cannot disagree about it.

This replaced a length threshold, which got both ends wrong in production. An
11-character brand wordmark displaced
`black oversized denim sleeveless hoodie with bull graphic`; then a tee covered
in slogan text sent 161 characters of `ONE WITH ALL EXISTENCE / 戰 / 愛` to
`search_products` as the query. Neither finds anything: the catalog is indexed
by attributes, so it can be searched for "red oversized t-shirt with abstract
print" and not for the slogan printed on that shirt.

The text is not discarded when the product leads. `ocr_context()` returns it
whitespace-collapsed and capped at `MAX_OCR_CONTEXT_CHARS` (200), and the
webhook appends it as `(text on the item: "…")` so the agent can still use it
without it becoming the query.

### Vertical neutrality of the default prompt

The built-in default prompt is deliberately not apparel-only. `is_product_image`
is true for any physical product a retailer could stock, and `material`,
`pattern`, `style` and `fit` are declared optional so a non-garment product
returns them empty rather than having a garment term stretched over it. Only
`summary` reaches the customer flow, and it is free text — no taxonomy
constrains it — so a client in a new vertical still gets a usable search phrase
before anyone seeds a per-client prompt.

Two things remain per-client and are worth seeding anyway:

- An `agents_config` row (`agent_name = 'inbound_image_analysis'`) when a client
  wants wording tuned to its own catalog.
- A `client_taxonomy_config` row. Without one, `_abuild_taxonomy_addendum` falls
  back to `get_defaults()`, whose category/fit/colour lists are apparel-shaped,
  and the addendum instructs the model to use *only* those values. That
  constrains `product_description` (which nothing downstream reads today), not
  `summary` — but it does mean the attributes are snapped into another client's
  vocabulary.

### Relationship to product image OCR

This module is **not** related to `product_image_ocr_extractor.py`. That module
handles batch OCR for product catalog images during Shopify ingestion with
tiered caching, per-product summaries, and Shopify CDN filtering. This module
is a one-shot analysis for customer-sent WhatsApp images with a completely
different prompt and purpose.

## Configuration

| Variable / Config Key | Default | Purpose |
|---|---|---|
| `inbound_image_ocr_enabled` (client_configs) | `false` | Per-client feature flag. Must be explicitly enabled. |
| `OPENROUTER_API_KEY_2` (env) | — | Background key for OpenRouter routing. Falls back to `GOOGLE_API_KEY` direct. |
| `INBOUND_OCR_LLM_MODEL` (env) | `google/gemini-2.5-flash-lite` | Override the OpenRouter model slug. |
| `INBOUND_OCR_TIMEOUT_SECONDS` (env) | `10.0` | Override the analysis budget. Ignored if unparseable or <= 0. |

The feature flag is read via the standard tiered cache (`aget_config` →
memory → Redis → Postgres). No deploy needed to enable — insert the config
row and wait for cache TTL (10 min memory, 1 hour Redis).

## Failure behaviour

All failure paths return `None`, which leaves `message_type = "image"` and
the existing short-circuit fires. The feature is invisible when it fails:

| Failure | Result |
|---|---|
| Feature flag off | `None` → existing flow |
| Gupshup URL expired/unreachable | `None` → existing flow |
| LLM timeout (>10s) | `None` → existing flow |
| LLM returns empty/no text/no product | `None` → existing flow |
| LLM safety block | `None` → existing flow |
| JSON parse failure | `None` → existing flow |
| Any exception | Caught, logged, `None` → existing flow |

Cloudinary upload still runs as a detached background task regardless of
analysis outcome — durable storage for human agents is unaffected.

## Message composition

When OCR finds text:
- With caption: `{caption}\n[Text from image]: {ocr_text}`
- Without caption: `[Text from image]: {ocr_text}`

When a product is detected (no OCR text):
- With caption: `{caption}\n[Product from image]: {product_summary}`
- Without caption: `[Product from image]: I'm looking for {product_summary}`

The prefix tags (`[Text from image]:`, `[Product from image]:`) give the
intent detection LLM context about the source. No intent detection prompt
changes are needed — the LLM classifies based on the content naturally.

## Pending queue merge (edge case)

When an image arrives while another turn holds the single-flight lock, OCR has
NOT run yet (it runs inside `_execute_turn_run_graph_and_update_state_for_gupshup`,
which hasn't been reached). The merge path continues as-is — the placeholder
`[Image message]` text hits intent detection's existing "media → escalation"
rule. This is acceptable: the edge case is rare, and escalation is a safe
fallback.

## Cost

- Gemini 2.5 Flash Lite vision: ~$0.0001 per image
- One LLM call per inbound image (no caching)
- At 100 images/day per client: ~$0.01/day

## Latency

- Analysis adds ~3.5s on a warm process (measured in production, 2026-08-24);
  the first call on a fresh process is slower because of connection setup
- Hard-capped at 5 seconds via `asyncio.wait_for`
- When analysis succeeds: total reply time = analysis time + graph time
- When analysis fails/times out: reply time unchanged (canned reply)
