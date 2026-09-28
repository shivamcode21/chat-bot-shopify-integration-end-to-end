# Product Image OCR → Upstash Search

**Status:** Draft v3 — single-LLM-call-per-product + per-product summary cache + LLM on webhook hot path
**Author:** Prabhjot (with Claude)
**Date:** 2026-05-19 (v1-v2) → 2026-05-20 (v3)

## v3 architecture (current)

**Three-tier lookup per product** (replaces v1's per-image-call fan-out):

```
get_product_ocr_summary(client_id, product_id, current_urls):
  url_set_hash = sha256(sorted(canonical_urls))

  ┌─ Tier 1: per-product summary cache ─────────────────────────────────┐
  │  product_ocr_summary_cache row by (client_id, product_id)           │
  │  if row.url_set_sha256 == url_set_hash → reuse row.summary          │  ← 1 DB read, no LLM
  └─────────────────────────────────────────────────────────────────────┘
                              ↓ miss / URL set changed
  ┌─ Tier 2: per-image cache (existing product_image_ocr_cache) ────────┐
  │  Look up each URL → split into urls_new vs urls_cached              │
  └─────────────────────────────────────────────────────────────────────┘
                              ↓
  ┌─ Tier 3: ONE combined vision LLM call ──────────────────────────────┐
  │  Prompt: system rules + cached_text_for_known_urls + N image parts  │
  │  Output JSON: {per_image: [{url, text}], summary: "" (≤500 chars)}  │
  │  Summary is deduped, no English stopwords, summarised (not transcribed)
  └─────────────────────────────────────────────────────────────────────┘
                              ↓
  Persist: new per-image rows → product_image_ocr_cache (cross-product reuse)
           summary + url_set_hash → product_ocr_summary_cache
  Return: summary + per_image_texts
```

**Final `content.image_text` composition** is deterministic — keeps shop content out of the per-product cache key so a shop-content update doesn't invalidate every product's summary:

```
content.image_text = compose_final_image_text(
    shop_promo_terms,        ← from client_shop_content_cache (gated)
    shop_image_terms,        ← from client_shop_content_cache (gated)
    product_summary,         ← from product_ocr_summary_cache / LLM
)
# Concatenated with "; ", deduped, capped at 2000 chars,
# then to_search_document's 4092-byte budget gate trims further if needed.
```

**Webhook hot path**:
- Tier 1/2 lookups always run (cheap).
- Tier 3 LLM call runs inline on cache miss with a 15s `asyncio.wait_for` timeout.
- On timeout/failure, fall back to `UpstashSearchService.fetch_document_by_id` and copy the existing doc's `content.image_text` + `metadata.image_texts` onto the new upsert so the OCR data is never wiped.
- Next delta sync's UNCHANGED top-up scan back-fills properly if the cache row stayed stale.

## v3 cost/latency vs. v1 (Sereko Calming Gel Pen, 22 images)

| Scenario | v1 (per-image calls) | v3 (combined call) |
|---|---|---|
| Cold ingestion | 22 LLM calls, ~5.5s sequential, ~$0.00088 | 1 LLM call, ~3-4s, ~$0.00045 |
| Delta UNCHANGED (URL set same) | 22 per-image cache lookups, no LLM | 1 per-product cache lookup, no LLM |
| Webhook, URL set unchanged | 22 per-image cache lookups, deterministic merge | 1 per-product cache lookup, no LLM |
| Webhook, new image added | Same as cold | 1 LLM call with 1 new image + 21 cached texts as context — ~2-3s |
| Webhook, LLM times out | (didn't run LLM, just used empty) | Falls back to existing Upstash OCR fields |

~50% per-product LLM cost reduction on cold ingestion. Steady-state stays on Tier 1 (no LLM).

## What v3 changes vs v2

- **Per-product LLM call.** `aextract_for_product_combined(client_id, product_id, image_urls)` replaces v2's per-image batch (`extract_batch_async` still exists but is now only used by the shop-content extractor for HTML-embedded images).
- **New `product_ocr_summary_cache` table.** Per-product summary keyed by `(client_id, product_id)` with `url_set_sha256` for hit detection. The existing `product_image_ocr_cache` table is still used as a Tier-2 per-URL text cache for cross-product reuse.
- **LLM-produced summary** (≤ 500 chars, deduped, English stopwords removed, summarised) replaces v2's deterministic `build_image_text_summary` for the product portion. Shop content is still composed deterministically on top via `compose_final_image_text`.
- **Webhook runs the LLM** when the per-product cache misses, bounded by 15 s `asyncio.wait_for`. On timeout / failure, fall back to `UpstashSearchService.fetch_document_by_id` and copy the existing doc's OCR fields onto the new upsert so the data is never wiped. 15 s exceeds Shopify's 5 s webhook-response budget so cold-cache webhooks will trigger Shopify retries; the existing dedup (`atry_claim_inventory_upsert` + `product_llm_cache`) absorbs those retries idempotently.
- **UNCHANGED top-up detection** is now a single bulk Postgres query (`aget_product_ocr_summaries_bulk`) checking the per-product summary's `url_set_sha256` instead of N per-URL lookups.
- **Stopwords**: enforced both via prompt instruction (primary) and a belt-and-braces post-process whole-word strip (secondary) inside `_belt_and_braces_stopword_strip`.

## v3 locked decisions

| # | Decision | Resolution |
|---|---|---|
| 13 | Stopword removal | LLM prompt instruction + post-process whole-word strip as a safety net |
| 14 | Webhook timeout | 15 s `asyncio.wait_for`; on timeout, fetch existing Upstash doc and preserve its OCR fields. Exceeds Shopify's 5 s response budget — cold-cache webhooks trigger retries, absorbed idempotently by existing dedup |
| 15 | Shop content composition | Deterministic (shop terms first, then product summary; deduped, "; " separated). Caches stay product-scoped |
| 16 | Image caps with cached subset | Caps still apply to the full URL set; LLM input is the `urls_new` subset (cached URLs feed the prompt as text, not as image parts) |

---

## v2 architecture (deprecated by v3)

**Status (v2):** Locked in, pending second review

## Decisions locked

| # | Decision | Resolution |
|---|---|---|
| 1 | Where does image text land | **Dual-write**: per-image raw text → `metadata.image_texts`; deduped summary → `content.image_text` |
| 2 | Per-client enable | Two **independent** boolean flags in `client_configs`, both **default `false`**: <br>• `image_ocr_enabled` — master flag for the OCR pipeline (gallery + metafield image OCR). When false, nothing runs. <br>• `shop_content_extraction_enabled` — sub-flag for the storefront HTML scrape (Offer banner, marquee). Only has effect when `image_ocr_enabled` is also true. Enable per-client when you want to capture shop-wide promo text in `content.image_text`. <br>For Sereko (`a8dc1711-376b-411d-b990-80eb04cfd3ca`) we'd enable both. Webhook + delta-sync + full-ingest each gate independently |
| 3 | Images per product (gallery) | **10** (was 7) |
| 4 | Top-up trigger for webhook-created products | **(a)** Inside delta sync, scan UNCHANGED bucket for products with missing OCR cache rows and run OCR on them |
| 5 | Vision model | **Gemini 2.5 Flash Lite only.** No fallback ladder in v1 |
| 6 | `content` size cap | Compute remaining char budget vs 4092 **before** calling the summariser LLM; pass budget into the prompt; defensive truncate after |
| 7 | Metafield image extraction | **In scope for v1** (see new section below) |
| 8 | Shop-level promo/announce content (Offer banner, marquee) | **In scope for v1 via HTML scrape, not OCR** (see new "Shop-level content extractor" section). The Offer banner is plain HTML text, not an image — confirmed against live `serekoshop.com` PDP |
| 9 | Tag-driven badges (Vegan, Paraben Free, …) | **No action** — already indexed via `tags` array in `content` |

## Problem

Brands like Sereko (https://serekoshop.com) embed substantial sales-driving copy *inside* PDP images: offer banners ("FREE body wash on orders above ₹599"), frequently-bought-together panels, ingredient callouts ("Backed by Psychodermatology", "Powered by NeuroCalm®"), claim badges ("Vegan", "Paraben Free"), free-gift thresholds, etc.

Today none of this text is indexed in Upstash Search — only the Shopify `title`, `description`, tags, collections, metafields, and a few LLM-extracted attributes (`base_product_name`, `material`, `occasion`, …) reach the search content blob. A shopper or downstream LLM asking "which product gets me a free eye mask" or "show me paraben-free products" cannot retrieve these products by image-text signals.

## Goal

Extract textual content from each product's images using a cheap vision LLM and feed it into Upstash Search so the text becomes part of the recall surface — **without** doing the OCR on every webhook hit.

## Non-goals

- Re-OCRing on every webhook (latency + cost killer).
- Replacing the existing `ProductAttributeExtractor` taxonomy extraction — this is additive.
- Image vector search / CLIP embeddings — this design is text-only.
- Downloading and re-hosting images — we keep using Shopify CDN URLs.

---

## Today's pipeline (recap, so we know exactly where to splice)

Three entry points feed Upstash Search. All three converge on `ProductIngestionOrchestrator` or its slimmed-down webhook twin:

| Trigger | Code path | LLM extraction? | Frequency |
|---|---|---|---|
| **Full ingestion** (`POST /api/v1/products/ingest`) | `orchestrator.ingest_products` → `extract_batch_async(max_concurrent=10)` → `to_search_document` → `UpstashSearchService.aupsert_documents` | Yes, batch | On-demand / first onboarding |
| **Delta sync cron** (Sundays 03:00 UTC) | `cron_jobs/product_vector_sync_job.product_vector_delta_sync` → `orchestrator.delta_sync_products` (hash diff → batch extract only changed) | Yes, batch on changed | Weekly |
| **Shopify webhook** (`products/create`, `products/update`) | `shopify/webhook/product_webhook.handle_product_upsert` → `extractor.extract_attributes` (single product, sync-in-async) → `to_search_document` → `aupsert_documents` | Yes, single | Real-time per product update |

The search document split is:

- `content` (full-text + semantic searchable, hard cap ~4096 chars per Upstash Search): `title`, `description[:500]`, tags, collections, colors, sizes, material, attributes, … (`models.py:269`)
- `metadata` (display-only, **not** searchable): `product_id`, `handle`, `image_url`, `all_images[:5]`, `product_url`, `variants`, `all_metafields`, `content_hash`, … (`models.py:306`)

LLM stack already wired: `LLMFactory.get_llm(tool_name="product_ingestion", override_config=get_smaller_llm_config(temperature=0, max_tokens=500))` → Gemini 2.5 Flash Lite. Multi-provider support exists (Anthropic / OpenAI / Azure / Google).

---

## What gets OCR'd, what doesn't (verified against Sereko Admin API)

Confirmed via `GET /admin/api/2024-04/products/8123671249209.json` and `…/metafields.json` on `sereko.myshopify.com` (Calming Gel Pen).

### IN scope — gets OCR'd

**Source A: product gallery images**
`product.images[*].src` from Shopify Admin API. For Calming Gel Pen this is **10 images** (gel_pen_2.webp, 3_1, 4_1, 5, 6, 7, Calming_Gel_Pen.png, 9, pack_of_2, pack_of_3). Currently only the **first 5** make it into `metadata.all_images` because of the `[:5]` slice in `to_search_document` (`models.py:311`). The OCR pipeline uses the full un-capped list from `NormalizedProduct.all_images` (10 images), and we'll bump the metadata cap to `[:10]` too so the index reflects what we OCR'd.

**Source B: product-owned metafield images** (NEW — not in `all_images` today)
Metafield values often contain HTML / JSON with embedded `<img src>` references. For Calming Gel Pen, scanning all 65 metafields and excluding review-widget namespaces (`vstar`, `judgeme`) yields **16 unique product-owned CDN images**:

| Metafield | Count | What they show |
|---|---|---|
| `product.product_info_column_1` | 1 | Brain icon (NeuroCalm) |
| `product.small_image_with_text` | 1 | SEREKO SIGNATURE formula icon |
| `product.image_with_text` | 1 | Signature Calming Duo bundle |
| `custom.hero_ingredient_html` | 4 | Ingredient cards (Basil Oil, Cica, Tea Tree, Wild Indigo) |
| `custom.pdp_why_use_section` | 4 | Application areas (Under-eye, Dark Spots, Wrists/Temples, Active Acne) |
| `custom.pdp_body_routine` | 5 | Routine step images |

These contain high-value searchable text (ingredient names, claim copy, application zones). They are **not** in `all_images` today.

**Filtering rules for metafield images:**
- Skip `vstar.*` and `judgeme.*` namespaces (user-uploaded review photos — high volume, low retrieval value).
- Skip pure-icon SVGs under a size threshold (the Brain.svg / Sign.png are 1–2 KB icons; the LLM will return empty text but we still burn a cache row — acceptable, cached forever).
- Only accept `cdn.shopify.com` URLs (and the client's configured domain) — defends against indirect leakage of unrelated CDN URLs in metafields.

**Caps (separate budgets so one source can't starve the other):**
```python
PRODUCT_IMAGE_OCR_MAX_GALLERY    = 10   # locked per decision
PRODUCT_IMAGE_OCR_MAX_METAFIELD  = 12
PRODUCT_IMAGE_OCR_HARD_CAP       = 25   # total circuit breaker per product
```

### Source C: Shop-level promo / announce content (NEW — added after live-HTML verification)

I fetched `https://serekoshop.com/products/calming-gel-pen` and inspected the rendered HTML. The "Offer" banner in your screenshot is **not an image** — it's three plain HTML `<p>` tags:

```html
<div class="main-announce" data-announce>
  <div class="text-announce"><p>FREE Minis on orders above ₹599</p></div>
  <div class="text-announce"><p>FREE Eye Mask on orders above ₹799</p></div>
  <div class="text-announce"><p>FREE Super Glow Kit on orders above ₹999</p></div>
</div>
```

(The offers have already drifted vs your screenshot — used to be "body wash + body lotion sample", now "Minis". The pipeline has to re-extract regularly.)

Same applies to the top scrolling announcement bar ("India's 1st Psychodermatology brand", "Patented NeuroCalm®") — it's plain HTML inside `<marquee-text class="announcement-bar__scrolling-list">`.

So we capture these via **HTML scraping**, not OCR. New component:

```python
# fashion_bot/services/product_ingestion/shop_content_extractor.py

class ShopLevelContentExtractor:
    """
    Fetches one PDP from the storefront, runs configured CSS selectors,
    returns shop-wide promo / announce text. Run ONCE per ingestion run.
    """

    async def extract(self, client_id: str, sample_handle: str) -> ShopContent:
        # 1. GET https://{domain}/products/{sample_handle}
        # 2. Parse HTML once with selectolax (already in stack) or BeautifulSoup
        # 3. For each configured selector, collect non-empty .text strings
        # 4. Dedupe, normalise whitespace, drop strings < 4 chars
        # 5. Return ShopContent(promo_terms: List[str], hash: str)

@dataclass
class ShopContent:
    promo_terms: List[str]   # e.g. ["FREE Minis on orders above ₹599", ...]
    extracted_at: datetime
    content_hash: str        # SHA256 of joined terms — invalidates cache when promo drifts
```

**Default selector bundle** (ships in code, covers ~80% of Shopify themes):

```python
DEFAULT_PROMO_SELECTORS = [
    "[data-announce] p",
    "[data-announce] .text-announce p",
    ".announcement-bar__item",
    ".announcement-bar__item .announce_in",
    ".promo-banner",
    ".free-gift-bar",
    ".header__announcement",
    ".announcement",
]
```

Per-client **override** column on `clients_config` (nullable JSON): `shop_content_extractor_overrides`. When `NULL`, defaults apply. For Sereko the defaults already match (`[data-announce] p` + `.announcement-bar__item` both fire) — no override needed.

```json
// Example override (only set when a client's theme is unusual)
{
  "storefront_domain": "serekoshop.com",          // defaults to client's primary domain
  "promo_selectors": ["...", "..."],              // replaces default bundle entirely
  "additional_promo_selectors": ["..."],          // adds to default bundle
  "exclude_text_patterns": ["^\\s*$", "^[\\W_]+$"],
  "max_promo_chars": 600
}
```

**Storefront fetch handle:** picked **dynamically** at extraction time — first active product handle returned by the source service. No per-client config needed. If that handle ever fails (404 / unpublished), fall back to fetching `/products.json` and using the first handle there.

**Cache table** `client_shop_content_cache`:

```sql
CREATE TABLE client_shop_content_cache (
    client_id      text        NOT NULL PRIMARY KEY,
    promo_terms    jsonb       NOT NULL,
    content_hash   text        NOT NULL,
    extracted_at   timestamptz NOT NULL DEFAULT now()
);
```

**TTL = 7 days**, refreshed as part of the weekly delta sync cron job (Sundays 03:00 UTC). The flow is:

- **Full ingestion:** always re-extracts shop content (cheap, one HTTP fetch).
- **Delta sync (weekly cron):** re-extracts shop content at the start of the run, then proceeds. This is the canonical "refresh point" — the 7-day TTL is just a safety net for ad-hoc ingestion paths that didn't trigger a delta sync.
- **Webhook:** **never** re-extracts. Reads whatever is in the cache (may be up to 7 days old). For Sereko, given delta sync runs weekly, the cache will always be ≤ 7 days old.

Effectively shop-promo freshness == delta-sync cadence. If you ever need faster reflection (e.g. a flash sale), trigger `delta_sync_single_client` manually and the promo terms update on the next ingestion pass too.

**How shop content reaches `content.image_text`:**

The per-product summariser receives `shop_content.promo_terms` as an extra input alongside the per-image OCR results. It dedupes against the product's title/tags/attrs and folds whatever's left into the budgeted blob. So for every Sereko product, the final `content.image_text` includes both the in-image claims OCR'd from the gallery/metafield images **and** the shop-wide promo terms.

**Cost:** one HTTP fetch every 6 hours per client (`<10ms` parse with selectolax). **Zero LLM cost** for the shop-level extraction itself.

### What's now confirmed OUT of scope

- **"Backed by Psychodermatology / Powered by NeuroCalm®" panel** — the panel **text** is theme-rendered HTML, not an image. The relevant content already lives in `product_info_column_1` metafield which is already indexed into `content.metafield_attributes`. The `Brain.svg` icon will get a one-time near-empty OCR row in the cache and never be called again — acceptable.
- **"Contains natural oils / Vegan / Paraben Free / Sulphate Free" badges** — driven by product tags (`feature-vegan`, `our_promise-paraben_free`, …) already indexed in `content.tags`. No action needed.
- **Frequently Bought Together images** — other products' hero images, OCR'd when those products are ingested.

## Design decision the user needs to weigh in on first

> The user's instruction was: "These information should ideally go to the metadata section of the product on Upstash."

There's a tension here:

- Upstash Search `metadata` is **display-only**, not searched. If image text lands only there, retrieval ("free eye mask above ₹799") will still miss.
- Upstash Search `content` **is** searched (both semantic and keyword) but has a ~4096-char cap. Adding a verbose OCR blob risks pushing useful fields off the edge.

**Recommendation:** dual-write.

- `metadata.image_texts`: structured per-image map (full fidelity, for audit, display, debugging, and possible future re-use). Not size-capped meaningfully.
- `content.image_text`: a single **summarised + deduped** blob (≤ 800 chars) so it participates in search but doesn't crowd out title/description/tags.

The summarisation is done by the same LLM call that does the OCR — one pass returns both the raw per-image text and a "search-optimised summary" string for `content`.

→ See **Open question 1** at the bottom.

---

## Proposed architecture

### High level

```
                 ┌──────────────────────────────────────┐
                 │  Full ingestion  /  Delta sync cron  │
                 └────────────────┬─────────────────────┘
                                  │ (skipped on webhook)
                                  ▼
              ┌───────────────────────────────────────────────┐
              │  ProductImageOCRExtractor (new)               │
              │                                               │
              │  1. For each product, collect image URLs      │
              │     (image_url + all_images[:N], dedup)       │
              │  2. For each URL, compute SHA256(url)         │
              │  3. Look up product_image_ocr_cache (Postgres)│
              │     - cache hit  → reuse extracted text       │
              │     - cache miss → schedule vision-LLM call   │
              │  4. Run misses through Gemini 2.5 Flash Lite  │
              │     vision (asyncio.Semaphore concurrency)    │
              │  5. Persist results to cache table            │
              │  6. Return ImageOCRResult per product         │
              └───────────────┬───────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────────────┐
              │  Orchestrator applies OCR onto NormalizedProduct
              │  - product.image_ocr_texts: Dict[url, str]   │
              │  - product.image_ocr_summary: str (≤ 800 ch) │
              └───────────────┬───────────────────────────────┘
                              │
                              ▼
              ┌───────────────────────────────────────────────┐
              │  to_search_document(client_id)               │
              │  - content["image_text"] = summary           │
              │  - metadata["image_texts"] = per-url dict    │
              │  - metadata["image_ocr_hash"] = stable hash  │
              └───────────────┬───────────────────────────────┘
                              ▼
                  UpstashSearch + UpstashVector upsert
```

Webhook path stays untouched **except** for one read: when building the search doc inside `handle_product_upsert`, look up the cached OCR text from the Postgres cache table by URL hash. If a hit exists (because a prior full/delta sync already OCR'd this image), reuse it. If miss, leave the field empty — the next delta sync will fill it in. **Webhooks never invoke the vision LLM.**

### New component: `ProductImageOCRExtractor`

Location: `fashion_bot/services/product_ingestion/product_image_ocr_extractor.py`

Mirrors the shape of `ProductAttributeExtractor`:

```python
@dataclass
class ImageOCRResult:
    url: str
    text: str = ""           # raw extracted text, line-preserved
    summary_terms: List[str] = field(default_factory=list)
                             # noun-phrase / claim tokens for search
    has_text: bool = False   # False when image is product photo only
    extraction_success: bool = False
    error: Optional[str] = None
    cached: bool = False     # True if served from cache, no LLM call

@dataclass
class ProductOCRResult:
    product_id: str
    per_image: List[ImageOCRResult]
    content_summary: str     # ≤ 800 chars, fed to content.image_text


class ProductImageOCRExtractor:
    def __init__(self): ...

    async def extract_for_product(
        self, product_id: str, image_urls: List[str], client_id: str,
    ) -> ProductOCRResult: ...

    async def extract_batch_async(
        self,
        products: List[Tuple[str, List[str]]],  # (product_id, image_urls)
        client_id: str,
        max_concurrent_images: int = 8,
    ) -> Dict[str, ProductOCRResult]: ...
```

### Vision LLM choice

We already wire Gemini 2.5 Flash Lite via `LLMFactory`. It accepts image URLs as message parts and is the cheapest first-party option. Stick with it.

Add a sibling helper next to `get_smaller_llm_config`:

```python
# fashion_bot/core/llm_config.py
def get_vision_ocr_llm_config():
    return LLMConfig(
        provider="google",
        model="gemini-2.5-flash-lite",
        temperature=0,
        max_tokens=600,   # raw text + short summary array
        supports_vision=True,
    )
```

Fallback ladder (configurable, in case Gemini quota / region issues):

1. `gemini-2.5-flash-lite` (primary)
2. `claude-haiku-4-5` (cheapest Anthropic vision)
3. `gpt-4.1-mini` (OpenAI fallback)

The factory already does provider routing — we just expose the new config.

### Prompt sketch

```
You are an OCR + summariser for e-commerce product images.

Given ONE product image, return JSON with two fields:
- "text": ALL legible text visible in the image, top-to-bottom, line-preserved.
          Include offer text, ingredient names, claims, badges, prices, and
          callouts. If the image is a plain product photo with no text, return "".
- "summary_terms": array of 3-12 short noun phrases (≤ 4 words each) that
          capture searchable claims, offers, ingredients, and feature words
          a shopper might query. Lowercase. Empty array if no text.

Rules:
- Do NOT invent text that isn't visible.
- Strip decorative ASCII / emoji.
- Preserve currency symbols and numeric thresholds (e.g. "₹599", "20% off").
- Return ONLY JSON, no markdown.

Image: {{image_url}}
```

The model gets the image as a URL part (Gemini reads remote URLs directly; Anthropic + OpenAI need fetch-and-base64 — handled in the factory).

### Per-product summary assembly — with 4092-char budget gate

After per-image extraction, `extract_for_product` builds `content_summary` under a budget that's computed **before** the summariser LLM is called:

```python
# Step 1: build the rest of the content dict without image_text
content_no_image = build_content_dict(product, exclude=["image_text"])
# json size as Upstash will see it (it does the JSON encoding itself)
current_size = len(json.dumps(content_no_image, ensure_ascii=False))

# Step 2: reserve some headroom for the "image_text": "..." key encoding
JSON_OVERHEAD = 20   # "image_text":"" baseline
SAFETY        = 30
HARD_LIMIT    = 4092
budget        = HARD_LIMIT - current_size - JSON_OVERHEAD - SAFETY

# Step 3: gate — if no room, don't call the summariser at all
if budget < 120:
    image_text = ""
    logger.info(f"image_text skipped: budget={budget} for product {product.id}")
else:
    image_text = await ocr_summariser.summarise(
        per_image_results=ocr_results.per_image,
        existing_terms=existing_term_set(product),  # title+tags+attrs
        max_chars=budget,
    )
    # Defensive truncate — never trust the LLM to honour budget exactly
    if len(image_text) > budget:
        image_text = image_text[:budget].rsplit(";", 1)[0]

content_no_image["image_text"] = image_text
```

Notes:
- The **summariser prompt** receives `max_chars` and is instructed to stay strictly within it ("Return at most {budget} characters. Stop at the last complete phrase.").
- The summariser is the **same** vision call we made for per-image OCR — Gemini 2.5 Flash Lite. We make ONE extra non-vision call per product after all per-image calls return, which takes the dedup'd `summary_terms` arrays as input and produces the budgeted blob. ≈$0.00002 per product.
- `existing_terms` is the set of lowercased tokens already in `title + tags + extracted_attrs + collections + metafield_attributes` — the summariser is told to **omit** terms that appear there, so the budget is spent only on novel signal.

This guarantees that `len(json.dumps(content)) ≤ 4092` even if all other fields grow. The hard limit can be tuned in config later if Upstash raises the cap.

### Caching: `product_image_ocr_cache` table

```sql
CREATE TABLE product_image_ocr_cache (
    client_id           text        NOT NULL,
    image_url_sha256    text        NOT NULL,    -- SHA256(url)
    image_url           text        NOT NULL,    -- for debug / re-OCR
    extracted_text      text        NOT NULL,
    summary_terms       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    has_text            boolean     NOT NULL,
    model               text        NOT NULL,    -- "gemini-2.5-flash-lite"
    prompt_version      smallint    NOT NULL,    -- bump to force re-OCR
    extracted_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, image_url_sha256)
);

CREATE INDEX product_image_ocr_cache_client_idx
    ON product_image_ocr_cache (client_id, extracted_at);
```

Why URL-hash and not product-hash:
- Shopify CDN URLs are content-addressed (`cdn.shopify.com/.../filename_v=1234567.jpg`). If a designer re-uploads an image, the URL changes → automatic cache miss → re-OCR. We don't need to detect that ourselves.
- Multiple products sometimes share the same banner image (e.g. brand-wide promo). One OCR call serves all.

Force-re-OCR knobs:
- Bump `prompt_version` in code → the lookup includes `prompt_version` in its match → all rows are invalidated logically (we just stop reading older rows).
- Add a `force_reocr=True` flag to the ingestion endpoint that skips cache reads.

### Where image URLs come from

Two sources, each with its own dedup + cap:

```python
gallery_urls = dedupe([product.image_url] + product.all_images)[:10]

metafield_urls = dedupe(
    extract_image_urls_from_metafields(
        product.all_metafields,
        skip_namespaces={"vstar", "judgeme"},      # review widgets
        allowed_domains={"cdn.shopify.com"},        # safety
    )
)[:12]

ocr_urls = (gallery_urls + metafield_urls)[:25]    # hard circuit breaker
```

`extract_image_urls_from_metafields` walks every metafield value (string or stringified JSON), runs a regex for `https?://cdn\.shopify\.com/...\.(png|jpg|jpeg|webp|svg|gif)`, returns unique URLs in document order.

→ **Locked: 10 gallery + 12 metafield, 25 hard cap.**

### Filtering: don't OCR hero shots when we know they're text-free

Shopify doesn't tell us which images contain text. Two options:

- **A. OCR everything (recommended).** The model returns `"text": ""` for photo-only images and we cache that "no text" result so we don't re-call. After one full ingestion, every image's text-yes/text-no status is cached.
- **B. Pre-filter by image dimensions / position.** Too brittle. Skip.

We pick A. The "empty result" rows in the cache are tiny and prevent rerunning the LLM on photo-only frames forever.

### Cost guardrails

Per Gemini Flash Lite pricing (as of 2026), a single image input costs roughly $0.0001 per OCR call. For Sereko-scale catalogues (~150 SKUs × ~22 images = ~3,300 calls per cold full ingestion), that's ~$0.33 per cold full ingestion. After the first run the cache absorbs most of it; delta sync only touches changed products, so steady-state weekly cost is well under $0.05.

Caps in config:

```python
PRODUCT_IMAGE_OCR_MAX_GALLERY      = 10     # locked
PRODUCT_IMAGE_OCR_MAX_METAFIELD    = 12
PRODUCT_IMAGE_OCR_HARD_CAP         = 25     # per product
PRODUCT_IMAGE_OCR_MAX_CALLS_PER_RUN = 10000 # circuit breaker
```

If the per-run circuit breaker trips, we log a warning, finish ingestion with whatever was OCR'd, and rely on the next delta sync to pick up the rest (LRU by `extracted_at`).

### Concurrency

Same pattern as `ProductAttributeExtractor.extract_batch_async`:

```python
sem = asyncio.Semaphore(max_concurrent_images)   # default 8
```

Bottleneck is upstream model rate limit, not orchestration. 8 concurrent vision calls is comfortably under Gemini's default quota.

### Failure semantics

Mirror the attribute extractor: **non-fatal**.

- Per-image failure → that image gets `extraction_success=False` and a logged warning. We still ingest the product with whatever did succeed.
- Whole-product failure → empty `image_ocr_texts` in metadata, empty `image_text` in content. Product still indexed using existing fields.
- Cache write failure → log and continue.

### Search document changes

In `NormalizedProduct.to_search_document` (`models.py:248`):

```python
content["image_text"] = self.image_ocr_summary or ""        # ≤ 800 chars
...
metadata["image_texts"] = self.image_ocr_per_image_map      # {url: text}
metadata["image_ocr_hash"] = self.image_ocr_hash            # for delta
```

Also extend the **content hash** computation to **exclude** `image_ocr_*` fields so a re-OCR alone doesn't churn delta-sync diffs across the whole catalog. The OCR cache table is already the source of truth for "did the OCR change."

### Webhook path (no LLM)

In `handle_product_upsert`:

```python
# Look up cached OCR for this product's images (no LLM call).
ocr_lookup = await aget_cached_ocr_for_urls(
    client_id=client_id,
    image_urls=[normalized.image_url] + normalized.all_images[:6],
)
normalized.image_ocr_texts = ocr_lookup.per_image
normalized.image_ocr_summary = ocr_lookup.content_summary  # may be ""
```

If the cache is cold (brand-new product just pushed by Shopify webhook before any delta sync has run), the search doc goes in with empty image text. The next delta sync will see the product as already-indexed (hash match) **but** notice the OCR is missing and trigger a top-up. → See **Open question 3** for the exact trigger criterion.

### Delta sync hook

Inside `orchestrator.delta_sync_products` (`orchestrator.py:534`):

1. Existing path computes which products are ADD / UPDATE / UNCHANGED via `content_hash`.
2. **New:** for products in ADD ∪ UPDATE, run `ProductImageOCRExtractor.extract_batch_async` over their image URLs (cache-aware, so unchanged images are free).
3. **New:** for UNCHANGED products, run a lightweight scan: does the Postgres cache have an entry for every URL in `image_url + all_images[:6]`? If any URL is missing, mark the product for OCR top-up (still no other content change → search doc upsert needed only if OCR changed). This handles webhook-created products whose images haven't been OCR'd yet.

The top-up is the only way OCR ever happens "outside" an ADD/UPDATE bucket. It runs at most once per product, since after the top-up the cache rows exist.

### Full ingestion hook

Inside `orchestrator.ingest_products`, splice between Step 4 (LLM attribute extraction) and Step 6 (search upsert) — or run in parallel with Step 4 since they're independent:

```python
ocr_task = asyncio.create_task(
    ocr_extractor.extract_batch_async(
        [(p.id, [p.image_url] + p.all_images[:6]) for p in products],
        client_id=client_id,
        max_concurrent_images=8,
    )
)
# Step 4 (attribute extraction) runs concurrently …
ocr_results = await ocr_task
self._apply_ocr_results(products, ocr_results)
```

`_apply_ocr_results` populates `image_ocr_per_image_map`, `image_ocr_summary`, and the OCR hash on each `NormalizedProduct`.

---

### Per-client enable flag

Add `image_ocr_enabled: bool` to the existing `clients_config` table (or wherever per-client booleans live today). **Default `false`.**

Where the flag is checked:
- `orchestrator.ingest_products` — skip Source A+B URL collection and the entire OCR step.
- `orchestrator.delta_sync_products` — same skip.
- `handle_product_upsert` (webhook) — skip the cache read.

When the flag flips from `false → true` for a client:
- Next full ingestion or delta sync will populate the OCR cache on the fly. No manual backfill needed.
- The product `content_hash` does NOT include image_text (intentionally — see "Search document changes"), so flipping the flag without a content change still triggers an upsert because we add a side-channel "ocr_state_hash" comparison in delta sync: if the OCR'd `image_ocr_hash` in the index doesn't match what the OCR pipeline just produced, force an upsert even when `content_hash` matches.

For Sereko: set `image_ocr_enabled=true` for `client_id = a8dc1711-376b-411d-b990-80eb04cfd3ca` after PR 2 lands.

## Rollout plan

1. **Schema migration** for `product_image_ocr_cache` (alembic / whatever migration tool is in use — to be confirmed) **and** the `image_ocr_enabled` column on `clients_config`.
2. **Code in stages**, all gated by `image_ocr_enabled`:
   - PR 1: image OCR extractor + metafield URL scraper + `product_image_ocr_cache` table + tests, no orchestrator hook. Run a one-off script on Sereko's catalog and inspect the cache rows manually.
   - PR 2: shop-level content extractor (`ShopLevelContentExtractor`) + `client_shop_content_cache` table + per-client selector config. Run a one-off script for Sereko, verify it captures `FREE Minis…`, `FREE Eye Mask…`, `FREE Super Glow Kit…` and the marquee.
   - PR 3: hook both extractors into full ingestion. Flip flag on for Sereko. Run a force-refresh and compare search recall before/after on 10 canary queries (e.g. "free eye mask", "paraben free", "neurocalm", "wild indigo", "spot treats acne", "free gift above 799").
   - PR 4: hook into delta sync (ADD/UPDATE bucket OCR + UNCHANGED bucket top-up scan + shop-content re-extract if TTL expired).
   - PR 5: webhook-side cache read (image OCR cache + shop content cache — both read-only on the webhook path).
3. **Eval gate before enabling for additional clients:** measure search recall@5 on a curated query set (image-text-only queries vs. baseline queries) and confirm no regression on baseline.
4. **Cost telemetry:** emit `ocr_calls_total`, `ocr_cache_hits_total`, `ocr_failures_total`, `ocr_content_budget_skipped_total` to Grafana (Bloomerce MCP instance already configured).

---

## Round 3 decisions locked

| # | Decision | Resolution |
|---|---|---|
| 10 | Selector strategy | **Default bundle in code, per-client `shop_content_extractor_overrides` JSON for overrides.** Sereko uses defaults — no override needed |
| 11 | Shop-content TTL | **7 days**, refreshed inline with the weekly delta sync cron job. Webhooks read-only against the cache |
| 12 | Sample handle for storefront fetch | **Dynamic** — first active product handle returned by the source service, fall back to `/products.json` on 404 |

## Remaining minor open questions

1. **Metafield image cap (currently 12).** Sereko-comfortable. Global default OK, or per-client override? My pick: global for v1, revisit if any client has >25 metafield images per product.
2. **Tiny-icon pre-skip.** Brain.svg / Sign.png get OCR'd once, return empty, cached forever. Pre-skip by size, or let cache absorb? My pick: let cache absorb.
3. **Prompt-version cache invalidation.** All-or-nothing reset on `prompt_version` bump, or per-field versioning for incremental prompt iteration? My pick: all-or-nothing.
4. **Cache cleanup on product delete.** TTL sweep at 180 days. Sound right?

None of these block PR 1. If you OK my picks (or skip them silently), the design is ready to build.
