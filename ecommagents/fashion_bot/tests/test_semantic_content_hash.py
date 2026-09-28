"""
Tests for the semantic content hash — the LLM-extraction gate split out of
``compute_content_hash_excluding_inventory``.

Background
----------
One hash used to answer two unrelated questions:

  1. "Does the Upstash search document need rewriting?" — price, discount,
     status, tags and image all matter here.
  2. "Is the cached LLM extraction still valid?"        — only what the product
     *is* matters here: title, product_type, description.

Because (2) reused (1)'s hash, ordinary merchandising re-ran attribute
extraction across whole catalogues. Two causes were measured in production,
neither of them a real product change:

  * Promotional churn — tag sweeps (``BUY2FOR10``, ``DEAL10``, ``AW25DIS``, hex
    colour codes) moved the hash on ~5,722 of 18,370 product webhooks in one
    day, with title and description untouched.
  * Enrich instability — a throttled Shopify GraphQL enrich drops
    ``fabric``/``colors``, flipping the hash one way; the next healthy webhook
    flips it back. Two extractions per cycle, ~59% of re-extractions on quiet
    days.

Together these drove ingestion to ~2,273 calls/hour against the 500/hour alert
threshold, while the entire conversational stack peaked near 200.

The gate is therefore narrow by design, and the cost is explicit: an attribute
inferred only from tags or metafields can go stale until an identity field
changes or ``/api/v1/products/re-extract`` is called.

Migration safety is the other half of this: cache entries written before the
split carry no semantic hash, and ``llm_inputs_unchanged`` must fall back to the
old full-hash comparison for them rather than treating them as a miss — a miss
would re-extract every product on every client at once.
"""

import pytest

from fashion_bot.services.product_ingestion.models import NormalizedProduct
from fashion_bot.services.product_ingestion.product_llm_cache import (
    _build_payload,
    llm_inputs_unchanged,
)

# Merchandising / plumbing fields. These still drive the search-document upsert
# via the full hash, but must NOT re-run LLM extraction.
MERCHANDISING_FIELDS = {
    "price_min": 1499.0,
    "price_max": 2499.0,
    "compare_at_price_min": 3999.0,
    "compare_at_price_max": 4999.0,
    "discount_pct": 62.5,
    "status": "draft",
    "image_url": "https://cdn.example.com/other.jpg",
    "tags": ["Red", "BUY2FOR10", "DEAL10"],
    "vendor": "OtherVendor",
    "colors": ["Red"],
    "sizes": ["XS"],
    "fabric": "Linen",
    "care_instructions": "Dry clean only",
}

# Identity fields — what the product *is*. Only these re-extract.
IDENTITY_FIELDS = {
    "title": "Gant Men Red Striped Slim Fit Shirt",
    "product_type": "Sweater",
    "description": "A completely different description of the product.",
}


def _product(**overrides):
    base = dict(
        id="10207359598892",
        title="Gant Men Navy Blue Solid Slim Fit Oxford Shirt",
        handle="gant-men-navy-blue-oxford-shirt",
        product_url="https://example.com/products/gant-oxford",
        in_stock=True,
        product_type="Shirt",
        vendor="Gant",
        tags=["Blue", "Adult Topwear", "AW24"],
        colors=["Blue"],
        sizes=["S", "M", "L"],
        price_min=7899.0,
        price_max=7899.0,
        compare_at_price_min=None,
        compare_at_price_max=None,
        discount_pct=0.0,
        status="active",
        description="Discover the epitome of classic style with our mens blue striped shirt.",
        fabric="Cotton",
        care_instructions="Machine wash cold",
        image_url="https://cdn.example.com/a.jpg",
    )
    base.update(overrides)
    return NormalizedProduct(**base)


# ---------------------------------------------------------------------------
# The gate ignores pricing/campaign churn ...
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", sorted(MERCHANDISING_FIELDS.items()))
def test_merchandising_fields_do_not_move_the_llm_gate(field, value):
    before = _product()
    after = _product(**{field: value})

    assert after.compute_semantic_content_hash() == before.compute_semantic_content_hash(), (
        f"{field} is merchandising metadata, not product identity — changing it "
        f"must not force a re-extraction"
    )


def test_throttled_enrich_does_not_re_extract():
    """The measured ping-pong: a throttled GraphQL enrich drops fabric/colors.

    Observed in production as the same product alternating between two hashes
    (4e04b19b <-> a426e0db) with identical title, description, type and tags —
    two LLM calls per throttle cycle, caused purely by Shopify rate limiting.
    """
    enriched = _product(fabric="Cotton", colors=["Blue"], care_instructions="Machine wash cold")
    throttled = _product(fabric=None, colors=[], care_instructions=None)

    assert throttled.compute_semantic_content_hash() == enriched.compute_semantic_content_hash()


def test_promotional_tag_sweep_does_not_re_extract():
    """Bulk campaign tagging moved ~5.7k of 18.4k webhooks in one day."""
    before = _product(tags=["Blue", "Adult Topwear", "AW24"])
    after = _product(tags=[
        "Blue", "Adult Topwear", "AW24",
        "BUY1GET40BUY2GET50", "BUY2EXTRA10", "DEAL10", "#233334",
    ])

    # Search doc must be rewritten so the tags are indexed ...
    assert after.compute_content_hash_excluding_inventory() != \
        before.compute_content_hash_excluding_inventory()
    # ... but the product is the same product.
    assert after.compute_semantic_content_hash() == before.compute_semantic_content_hash()


def test_discount_campaign_does_not_re_extract():
    """The exact production scenario: a sale sweeps price + compare-at + discount."""
    before = _product()
    after = _product(
        price_min=3949.0, price_max=3949.0,
        compare_at_price_min=7899.0, compare_at_price_max=7899.0,
        discount_pct=50.0,
    )

    # Search doc must be rewritten (price changed) ...
    assert after.compute_content_hash_excluding_inventory() != \
        before.compute_content_hash_excluding_inventory()
    # ... but the cached extraction is still valid.
    assert after.compute_semantic_content_hash() == before.compute_semantic_content_hash()


# ---------------------------------------------------------------------------
# ... but still moves for anything the model actually sees
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field,value", sorted(IDENTITY_FIELDS.items()))
def test_identity_fields_still_move_the_llm_gate(field, value):
    before = _product()
    after = _product(**{field: value})

    assert after.compute_semantic_content_hash() != before.compute_semantic_content_hash(), (
        f"{field} defines what the product is, so changing it must re-extract"
    )


def test_description_is_not_truncated():
    """The full hash truncates at 500 chars; the identity gate must not, or a
    genuine rewrite past the cut-off would silently keep stale attributes."""
    filler = "x" * 600
    before = _product(description=filler + " original ending")
    after = _product(description=filler + " rewritten ending")

    assert after.compute_semantic_content_hash() != before.compute_semantic_content_hash()


def test_semantic_hash_is_deterministic():
    product = _product()
    assert product.compute_semantic_content_hash() == product.compute_semantic_content_hash()


def test_semantic_hash_differs_from_full_hash():
    """Distinct namespaces — a semantic hash must never be mistaken for a full one."""
    product = _product()
    assert product.compute_semantic_content_hash() != \
        product.compute_content_hash_excluding_inventory()


def test_search_doc_metadata_carries_semantic_hash():
    """So the weekly sync's bulk cache write can populate it too."""
    product = _product()
    doc = product.to_search_document("client-123")
    assert doc["metadata"]["semantic_content_hash"] == product.compute_semantic_content_hash()
    # Existing metadata keys are untouched.
    assert doc["metadata"]["content_hash_no_inventory"] == \
        product.compute_content_hash_excluding_inventory()


# ---------------------------------------------------------------------------
# Migration: pre-split cache entries must keep working, with no re-extract storm
# ---------------------------------------------------------------------------

def test_legacy_entry_without_semantic_hash_falls_back_to_full_hash():
    """A cache entry written before this change must behave exactly as before."""
    legacy = {"content_hash_no_inventory": "full-abc", "llm_fields": {}}

    # Full hash matches -> still a hit (no re-extraction storm on deploy).
    assert llm_inputs_unchanged(legacy, "semantic-xyz", "full-abc") is True
    # Full hash differs -> miss, exactly like the old behaviour.
    assert llm_inputs_unchanged(legacy, "semantic-xyz", "full-different") is False


def test_migrated_entry_prefers_semantic_hash():
    entry = {"content_hash_no_inventory": "full-abc", "semantic_content_hash": "semantic-xyz"}

    # Full hash moved (price change) but prompt inputs did not -> still a hit.
    assert llm_inputs_unchanged(entry, "semantic-xyz", "full-changed") is True
    # Prompt inputs moved -> miss even though the full hash matches.
    assert llm_inputs_unchanged(entry, "semantic-changed", "full-abc") is False


def test_empty_cache_entry_is_a_miss():
    assert llm_inputs_unchanged({}, "semantic-xyz", "full-abc") is False
    assert llm_inputs_unchanged({"content_hash_no_inventory": ""}, "s", "f") is False


def test_payload_omits_semantic_hash_when_not_supplied():
    """Callers that cannot vouch for the hash must not write a bogus one."""
    import json

    payload = json.loads(_build_payload("full-abc", {}, True, [], [], semantic_content_hash=None))
    assert "semantic_content_hash" not in payload

    payload = json.loads(_build_payload("full-abc", {}, True, [], [], semantic_content_hash="s1"))
    assert payload["semantic_content_hash"] == "s1"
    # Pre-existing payload shape is unchanged.
    assert payload["content_hash_no_inventory"] == "full-abc"
