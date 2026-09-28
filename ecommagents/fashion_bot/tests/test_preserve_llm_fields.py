"""
Tests that skipping LLM extraction never blanks previously-extracted fields.

Background
----------
Upstash Search ``upsert`` REPLACES a document rather than merging into it —
which is why ``_preserve_llm_fields`` and ``_preserve_enrich_fields`` exist at
all, why ``/api/v1/products/bulk-update-fields`` fetches-patches-reupserts, and
why the bestseller cron re-upserts "the whole doc (content + metadata)". So any
field left blank in the freshly-built document is lost from the index.

When a product webhook decides the LLM does not need to run (price change, tag
sweep, throttled enrich, inventory-only update), the document is still
rewritten, and every LLM-derived field in it starts empty. ``_preserve_llm_fields``
merges the previous values back in first. This matters far more now that
identity-gated extraction makes the skip path the common case.

``fit`` is the field this file was written for. It mirrors
``NormalizedProduct.fit_type`` into the search document: metafield-sourced, but
overwritten by ``_apply_extracted_attributes``, so a skipped extraction on a
throttled enrich blanked it. It was absent from every preserve list, even
though the delta sync already reads a ``"fit"`` key back out of these cached
fields — a lookup that could never resolve because nothing wrote it.

Note ``extracted_color`` and ``fit_type`` are NOT covered here: those live in
``to_vector_data``'s metadata, and the product webhook writes only the Search
document via ``to_search_document``.
"""

import pytest

from fashion_bot.services.product_ingestion.product_llm_cache import (
    LLM_EXTRACTED_CONTENT_FIELDS,
    collect_llm_fields_from_doc,
)
from fashion_bot.shopify.webhook.product_webhook import _preserve_llm_fields

# What a previous successful extraction produced.
CACHED = {
    "base_product_name": "beanie",
    "product_line": "shield wool beanie",
    "product_line_normalized": "shield wool beanie",
    "category": "accessories",
    "subcategory": "beanies",
    "segment": "men",
    "color_family": "green",
    "pattern": "solid",
    "material": "wool",
    "occasion": ["casual"],
    "style": ["classic"],
    "vibe": ["cosy"],
    "pairing_tags": ["coat"],
    "fit": "regular",
}


def test_cached_fixture_covers_every_preserved_field():
    """Guard: if a field is added to the list, this file must cover it."""
    assert set(CACHED) == set(LLM_EXTRACTED_CONTENT_FIELDS)


def _fresh_doc_without_extraction():
    """A search doc built from a webhook payload with the LLM skipped."""
    return {
        "id": "client_9988512350508",
        "content": {
            "title": "Gant Men Green Solid Beanies",
            "description": "Introducing the Shield Wool Beanie…",
            "tags": ["Beanies", "AW25", "DEAL10"],
            "price_min": 4299.0,
            # Every LLM-derived content field comes back blank.
            **{f: "" for f in LLM_EXTRACTED_CONTENT_FIELDS},
        },
        "metadata": {"product_id": "9988512350508"},
    }


# ---------------------------------------------------------------------------
# The regression: `fit` was in no preserve list
# ---------------------------------------------------------------------------

def test_fit_is_restored():
    doc = _fresh_doc_without_extraction()

    _preserve_llm_fields(doc, "trace-1", cached_llm_fields=CACHED)

    assert doc["content"]["fit"] == "regular", (
        "fit is overwritten by extraction, so a skipped extraction on a "
        "throttled enrich must not leave it blank"
    )


@pytest.mark.parametrize("field", LLM_EXTRACTED_CONTENT_FIELDS)
def test_every_llm_content_field_is_restored(field):
    doc = _fresh_doc_without_extraction()

    _preserve_llm_fields(doc, "trace-1", cached_llm_fields=CACHED)

    assert doc["content"][field] == CACHED[field]


# ---------------------------------------------------------------------------
# It must never clobber a genuinely fresh value
# ---------------------------------------------------------------------------

def test_fresh_values_win_over_cached_ones():
    """Only blanks are filled — a real extraction must not be overwritten."""
    doc = _fresh_doc_without_extraction()
    doc["content"]["category"] = "headwear"
    doc["content"]["fit"] = "slim"

    _preserve_llm_fields(doc, "trace-1", cached_llm_fields=CACHED)

    assert doc["content"]["category"] == "headwear"
    assert doc["content"]["fit"] == "slim"


def test_missing_cache_leaves_the_doc_untouched():
    doc = _fresh_doc_without_extraction()
    before = dict(doc["content"])

    _preserve_llm_fields(doc, "trace-1", cached_llm_fields={})

    assert doc["content"] == before


def test_never_raises_on_a_malformed_doc():
    """A preserve failure must not take down the webhook."""
    _preserve_llm_fields({}, "trace-1", cached_llm_fields=CACHED)
    _preserve_llm_fields({"content": None}, "t", cached_llm_fields=CACHED)


# ---------------------------------------------------------------------------
# Round-trip: what gets cached is what can be restored
# ---------------------------------------------------------------------------

def test_collect_then_restore_round_trips():
    """The write path and the restore path must agree on the field set."""
    extracted_doc = {"content": dict(CACHED), "metadata": {"product_id": "1"}}

    collected = collect_llm_fields_from_doc(extracted_doc)
    assert set(collected) == set(LLM_EXTRACTED_CONTENT_FIELDS)

    doc = _fresh_doc_without_extraction()
    _preserve_llm_fields(doc, "trace-1", cached_llm_fields=collected)

    for field in LLM_EXTRACTED_CONTENT_FIELDS:
        assert doc["content"][field] == CACHED[field]


def test_collect_drops_blanks_so_they_cannot_overwrite_good_values():
    collected = collect_llm_fields_from_doc({
        "content": {"category": "accessories", "material": "", "fit": "regular"},
    })

    assert collected == {"category": "accessories", "fit": "regular"}


def test_upstash_fallback_used_when_redis_is_empty():
    """A cold Redis entry must still recover values from the existing doc."""
    doc = _fresh_doc_without_extraction()

    class _SearchService:
        def fetch_document_by_id(self, client_id, product_id):
            return {"content": dict(CACHED)}

    _preserve_llm_fields(
        doc, "trace-1",
        cached_llm_fields=None,
        search_service=_SearchService(),
        client_id="client",
        product_id="9988512350508",
    )

    assert doc["content"]["category"] == "accessories"
    assert doc["content"]["fit"] == "regular"
