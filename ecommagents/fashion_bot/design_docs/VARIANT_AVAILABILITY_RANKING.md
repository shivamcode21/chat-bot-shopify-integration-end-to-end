# Variant Availability Ranking (`variant_availability_pct`)

> Prioritise products whose **majority of variants/sizes are in stock** whenever
> more than one product is shown to the user, across the `product_details`,
> `recommendations`, and `place_order` agents.

**Status:** Implemented
**Owner:** Search / Recommendation pipeline
**Related docs:** `vector_search_design_doc.md`, `PRODUCT_INGESTION_VECTOR_DB.md`

> **Implementation note (minimal-footprint).** To keep the change small and avoid
> any risk to the cost-optimised re-ingestion path, two items from the original
> design were intentionally **not** changed:
> - **Content-hash functions left untouched.** `variant_availability_pct` is
>   recomputed on every ingestion (full / delta / webhook upsert) regardless of
>   the hash, and is derived from inventory that already influences the existing
>   hash inputs (`in_stock`, `available_sizes`). Adding it to the hash had no
>   functional benefit (delta compares the *no-inventory* hash) and risked extra
>   re-ingestion/LLM cost.
> - **`stock_status_changed` left untouched.** The common stock change (a size
>   selling out) already flips `available_sizes` → the webhook stock-patch runs →
>   the field is refreshed there (one added line). The rare colour-only sellout
>   (a colour fully sells out while every size remains available in another
>   colour) can leave the field briefly stale until the next stock change or a
>   re-run of the backfill — an accepted trade-off for the smaller footprint.
> - **Relaxation ladder left untouched.** The existing last-resort fallback
>   already collapses to `in_stock = true` when results hit zero
>   (`recommendation_service.py:371`), which drops the
>   `variant_availability_pct > 50` clause automatically — so no ladder change
>   was needed.

---

## 1. Problem & root cause

A client asked the bot to "show articles on discount" and the top results were
products that have **multiple sizes but only one size in stock**. They want
products whose **majority of variants are in stock** (> 50% — 2-of-3, 3-of-4, …)
to rank on top.

All three agents (`product_details`, `recommendations`, `place_order`) share the
same retrieval path via the `search_products` tool →
`recommendation_service.search_products_pipeline()`:

```
QU LLM (query_understanding.py)
   → Upstash Search (upstash_search_service.py)
   → business_rules_reranker.rerank()
   → format → LLM
```

Two gaps cause the bug:

1. **No availability signal anywhere in retrieval or ranking.** The reranker only
   applies a soft penalty when a *known user size* is missing
   (`business_rules_reranker.py:61-64`). It never considers how many variants are
   actually purchasable.
2. **The discount sort is purely `discount_pct` descending**
   (`business_rules_reranker.py:146-153`). A heavily-discounted item with one live
   size always beats a slightly-less-discounted, fully-stocked item.

### Key nuance

The bug is products with *many* sizes but only one in stock. A product genuinely
sold in a single size that is in stock is fine. A **percentage** distinguishes
these cleanly:

| Product | In-stock / total variants | `variant_availability_pct` | Treatment |
|---|---|---|---|
| 1 size, in stock | 1 / 1 | 100 | keep / not penalised |
| 5 sizes, 1 in stock | 1 / 5 | 20 | demote / exclude |
| 2 colours × 5 sizes, 1 live | 1 / 10 | 10 | demote / exclude |

---

## 2. The new field

**`variant_availability_pct`** — a float `0.0–100.0`:

```
available  = count of variants where variant_is_in_stock(qty, policy) is True
total      = total number of variants
pct        = round(available / total * 100, 1)   # 0.0 when no variants
```

The data already exists at ingestion. Each variant built in
`shopify_product_service._normalize_product()` (lines 1273-1285) already carries
an `"available"` boolean from the single source of truth
`variant_is_in_stock(inv_qty, inv_policy)` (which already treats oversell
`CONTINUE` variants as available). The metric is a one-line reduction over that
list — **no extra Shopify data required**.

**Storage:** in the Upstash Search document's **`content`** block (searchable +
filterable), *not* `metadata`, because:

- Upstash hard filters and the reranker operate on `content`.
- The reranker reads `r.get("content", {})`.
- It then reaches the LLM automatically via `_upstash_doc_to_tool_product()`
  (content + metadata are flattened), so the model can also *mention* stock
  richness ("most sizes available").

---

## 3. Mechanisms (belt-and-suspenders)

Two complementary mechanisms, both fed by the same ingested field.

### 3a. Primary — QU LLM hard filter `variant_availability_pct > {threshold}`

The Query-Understanding LLM emits a hard filter on multi-product queries so the
majority-in-stock guarantee is enforced **at retrieval time**. The threshold is
**per-client configurable** (see §6).

- **Register the field** in `DEFAULT_FILTERABLE_FIELDS`
  (`query_understanding.py:22-33`):

  ```
  {"name": "variant_availability_pct", "type": "number", "operator": ">",
   "description": "percentage of variants/sizes in stock"}
  ```

  The QU prompt only allows hard filters on listed fields (`_validate_filter`
  strips anything else), so registration is mandatory for the LLM to use it.

- **Instruct the QU prompt** to append
  `variant_availability_pct > {variant_availability_min_pct}` to the `filter` on
  multi-product / browse / discount queries, combined like the existing
  `in_stock = true`:

  ```
  (price_max <= 2400) AND in_stock = true AND variant_availability_pct > 50
  ```

  The `{variant_availability_min_pct}` placeholder is formatted at runtime with
  the per-client resolved value (default 50).

**Semantics of `> 50` (default):** it *excludes*, not merely demotes.
`1/2 = 50` → dropped; `2/3 = 66.7` → kept; a genuine single-size in-stock
product = `100` → kept. A client with threshold `10` would only exclude products
where fewer than 10% of variants are in stock.

**Must be added to the relaxation ladder.** The pipeline already relaxes filters
when fewer than ~3 results return (`recommendation_service.py:340-387`), with a
last resort of keeping only `in_stock = true`. The new
`variant_availability_pct > 50` clause **must be dropped in that ladder before
returning a thin/empty set**, otherwise a sparse-inventory catalog could return
"no products" for a valid query.

### 3b. Backup — deterministic reranker rule

Orders whatever survives retrieval so the best-stocked appear first, and is the
safety net when relaxation has to drop the hard filter on thin catalogs. This is
a pure, always-on business rule in `business_rules_reranker.py` (consistent with
the existing discount/diversity/budget rules — no LLM, no per-query cost).

Introduce a configurable majority threshold (default `50`) and:

1. **Graduated multiplier** in the scoring loop (after the discount boost,
   ~line 109). Missing field → treated as `100` (neutral) for backward compat:

   ```
   pct    = content.get("variant_availability_pct", 100)
   factor = 0.6 + 0.4 * (pct / 100)   # 1-of-5 → ~0.68, full → 1.0
   score *= factor
   ```

   This feeds the default relevance sort directly, and tie-breaks the
   discount/price sorts (which use `score` as their secondary key) toward
   better-stocked items.

2. **Availability-aware discount sort** (the client's exact case). Replace
   `sort by (discount_pct, score)` with a bucketed key:

   ```
   key = (round(discount_pct to nearest 10%), variant_availability_pct, score)  # desc
   ```

   Preserves "biggest discounts first" coarsely while guaranteeing that, among
   comparably-discounted items, the majority-in-stock product leads.

`price_low` / `price_high` / `newest` keep their primary key (the user explicitly
asked for that order); the availability multiplier only nudges secondary
ordering.

---

## 4. Ingestion, hashing & freshness

| Change | File | Location |
|---|---|---|
| `variant_availability_pct: float = 0.0` on dataclass | `services/product_ingestion/models.py` | `NormalizedProduct`, ~line 252 |
| compute value, pass into `NormalizedProduct(...)` | `services/product_ingestion/shopify_product_service.py` | after line 1285 / return at 1287 |
| add to searchable `content` | `models.py` | `to_search_document()` |
| add to legacy vector metadata (parity) | `models.py` | `to_vector_data()` |
| patch field into Upstash on inventory webhooks | `shopify/webhook/product_webhook.py` | stock-patch `content_fields` |
| _(not changed — see implementation note)_ content-hash functions | `models.py` | — |
| _(not changed — see implementation note)_ `stock_status_changed` | `services/product_ingestion/product_llm_cache.py` | — |

**Why the hash split:** the field is inventory-derived. Including it in the
inventory-inclusive hash makes the weekly delta re-ingest on change; excluding it
from the no-inventory hash prevents needless LLM re-extraction on inventory-only
webhooks.

**Why extend `stock_status_changed`:** if a *colour* sells out but the *size* is
still available in another colour, `available_sizes` is unchanged yet true
availability dropped. Comparing the rounded pct catches that and triggers the
hot-patch.

---

## 5. Backfill & rollout

The field is null on existing documents until re-ingested. Rollout order (safe
because missing → neutral in the reranker, and the QU clause is additive):

1. Ship ingestion + hash + webhook changes (§4). New/updated products carry it.
2. **Active backfill (recommended):** a one-time script paging each client index
   via the existing `_iter_doc_pages` / `abulk_update_fields`, recomputing pct
   from the `variants` array already stored in metadata — **no Shopify calls
   needed** (per-variant `available` flags are already persisted).
   (Passive alternative: the weekly delta repopulates it, up to a week stale.)
3. Enable the reranker rule (§3b) behind a default-on constant.
4. Register the QU filter field + prompt instruction (§3a), and add the clause to
   the relaxation ladder.

**Backward compatibility:** until backfill completes, missing field → `100`
(neutral) in the reranker, so no catalog-wide regression; the rule activates
per-product as data lands.

---

## 6. Configuration / tunables

Per config-driven principles (AGENTS.md):

- **`variant_availability_min_pct`** — per-client `client_configs` key (integer
  0–100, default 50). Controls the QU hard filter threshold. Resolved per request
  via `aresolve_client_int` (same pattern as `new_arrival_window_days`,
  `product_results_count`, etc.): `client_configs` override → env default
  (`VARIANT_AVAILABILITY_MIN_PCT_DEFAULT`) → clamped to `[0, 100]` → fail-open.
- Multiplier floor (default 0.6) and discount bucket size (default 10%) in the
  reranker (module constants).
- **Per-client example:**
  ```sql
  -- Lenient (thin-inventory client): keep products with ≥10% sizes in stock
  INSERT INTO client_configs (client_id, config_key, config_value)
  VALUES ('client-uuid', 'variant_availability_min_pct', '10');

  -- Strict: require ≥70% sizes in stock
  INSERT INTO client_configs (client_id, config_key, config_value)
  VALUES ('client-uuid', 'variant_availability_min_pct', '70');
  ```
  Clients without a row get the default (50). No restart needed — the tiered
  cache (memory → Redis → Postgres) picks it up within TTL.

---

## 7. Edge cases

| Case | Behaviour |
|---|---|
| 1 size, in stock | `1/1 = 100` → kept / not penalised |
| 5 sizes, 1 in stock | `1/5 = 20` → excluded by QU filter, demoted by reranker |
| 2 colours × 5 sizes, 1 live | `10` → excluded / demoted (size set alone misses this) |
| Oversell `CONTINUE` policy | counted available via `variant_is_in_stock` (consistent) |
| Zero variants | guard → `0.0`, treated out of stock |
| Legacy doc, field missing | reranker treats as `100` (neutral); QU filter relaxed if it thins results |
| Sparse catalog, filter empties results | relaxation ladder drops the clause before returning empty |

---

## 8. Testing

- Unit-test pct computation in `_normalize_product` (full, partial, single,
  zero, oversell).
- Reranker tests: high-discount/low-availability ranks **below** comparable
  discount/high-availability; relevance ties break toward availability; missing
  field neutral.
- Hash tests: inventory-only change flips `compute_content_hash` but **not**
  `compute_content_hash_excluding_inventory`.
- Webhook test: a colour selling out (size set unchanged) still patches the
  field.
- QU/pipeline test: filter is emitted with the per-client threshold, and the
  relaxation ladder drops the `variant_availability_pct` clause before returning
  an empty set.

---

## 9. AGENTS.md compliance

- **Stateless/pure:** reranker stays a pure function; computation lives in the
  model/ingestion layer.
- **Tenant isolation:** field is per-document inside the per-client index — no
  cross-tenant surface.
- **Async + caching:** no new I/O on the hot path; per-client tunables go through
  the tiered cache; backfill reuses `_iter_doc_pages` / `abulk_update_fields`
  (already `to_thread`-wrapped).
- **Minimal footprint:** additive — one dataclass field, one computed value, a
  few content keys, a localized reranker edit, one QU field + prompt line.

---

## 10. Files touched (summary)

| File | Change |
|---|---|
| `services/product_ingestion/models.py` | dataclass field; `to_search_document` + `to_vector_data`; `compute_content_hash` (not the no-inventory variant) |
| `services/product_ingestion/shopify_product_service.py` | compute `variant_availability_pct` in `_normalize_product` |
| `shopify/webhook/product_webhook.py` | patch field into Upstash on inventory updates |
| `services/product_ingestion/product_llm_cache.py` | `stock_status_changed` compares the pct |
| `services/recommendation/query_understanding.py` | **primary** — register filterable field + templated prompt instruction (`> {variant_availability_min_pct}`) |
| `services/recommendation/recommendation_service.py` | resolve per-client threshold via `aresolve_client_int`; add the clause to the filter-relaxation ladder |
| `services/recommendation/business_rules_reranker.py` | **backup** — availability multiplier + availability-aware discount sort |
| `scripts/` | one-time backfill |
| `design_docs/VARIANT_AVAILABILITY_RANKING.md` | this document |
