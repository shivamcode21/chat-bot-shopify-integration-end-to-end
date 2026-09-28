"""Tests for _preserve_enrich_fields in shopify/webhook/product_webhook.py.

Background: when the Admin GraphQL enrich call fails, the freshly-normalized
product has empty collections/metafield_attributes, so
_preserve_enrich_fields restores those specific fields from the existing
Upstash document rather than let a transient fetch failure wipe them.

But to_search_document() already computed content_hash /
content_hash_no_inventory from the normalized product BEFORE this function
runs -- with metafield_attributes empty. Restoring the content without also
restoring the matching hash leaves the doc with correct content next to a
hash that says it's empty, which the PR that added rating to the hash made
much more likely to matter (any product with reviews now hits this on every
enrich failure, not just the handful with fabric/care-instruction
metafields).
"""

import pytest

from fashion_bot.shopify.webhook.product_webhook import _preserve_enrich_fields


class _FakeSearchService:
    def __init__(self, existing_doc):
        self._existing_doc = existing_doc

    def fetch_document_by_id(self, client_id, product_id):
        return self._existing_doc


def _existing_doc(**overrides):
    doc = {
        "content": {
            "collections": [{"title": "New In", "handle": "new-in"}],
            "metafield_attributes": {"rating": '{"value":"4.5"}', "rating_count": "2"},
        },
        "metadata": {
            "size_chart": {"has_size_guide": True},
            "care_instructions": "Hand wash",
            "content_hash": "old-content-hash",
            "content_hash_no_inventory": "old-hash-no-inv",
        },
    }
    doc.update(overrides)
    return doc


def _fresh_search_doc():
    # What to_search_document() produced when enrich failed: metafield-derived
    # fields empty, hashes already computed from that (rating-less) state.
    return {
        "content": {"collections": [], "metafield_attributes": {}},
        "metadata": {
            "size_chart": {},
            "care_instructions": "",
            "content_hash": "fresh-hash-no-rating",
            "content_hash_no_inventory": "fresh-hash-no-rating-no-inv",
        },
    }


def test_restores_content_and_matching_hash():
    search_doc = _fresh_search_doc()
    _preserve_enrich_fields(
        search_doc, "trace-1", _FakeSearchService(_existing_doc()), "client-x", "prod-1",
    )
    assert search_doc["content"]["metafield_attributes"]["rating_count"] == "2"
    assert search_doc["metadata"]["content_hash"] == "old-content-hash"
    assert search_doc["metadata"]["content_hash_no_inventory"] == "old-hash-no-inv"


def test_hash_untouched_when_nothing_needed_restoring():
    # Fresh doc already has real values for every enrich-derived field --
    # nothing gets restored, so the fresh hash (computed for exactly this
    # content) must be left alone.
    search_doc = {
        "content": {
            "collections": [{"title": "New In"}],
            "metafield_attributes": {"rating": '{"value":"4.5"}'},
        },
        "metadata": {
            "size_chart": {"has_size_guide": True},
            "care_instructions": "Hand wash",
            "content_hash": "fresh-hash",
            "content_hash_no_inventory": "fresh-hash-no-inv",
        },
    }
    _preserve_enrich_fields(
        search_doc, "trace-2", _FakeSearchService(_existing_doc()), "client-x", "prod-1",
    )
    assert search_doc["metadata"]["content_hash"] == "fresh-hash"
    assert search_doc["metadata"]["content_hash_no_inventory"] == "fresh-hash-no-inv"


def test_no_existing_doc_is_a_noop():
    search_doc = _fresh_search_doc()
    before = dict(search_doc["metadata"])
    _preserve_enrich_fields(
        search_doc, "trace-3", _FakeSearchService(None), "client-x", "prod-1",
    )
    assert search_doc["metadata"] == before


def test_existing_doc_missing_hash_leaves_fresh_hash_alone():
    # Old doc predates this fix (or is itself malformed) and has no hash to
    # carry over -- must not overwrite with an empty/missing value.
    stale_existing = _existing_doc(metadata={
        "size_chart": {"has_size_guide": True},
        "care_instructions": "Hand wash",
        # no content_hash / content_hash_no_inventory at all
    })
    search_doc = _fresh_search_doc()
    fresh_hash = search_doc["metadata"]["content_hash"]
    _preserve_enrich_fields(
        search_doc, "trace-4", _FakeSearchService(stale_existing), "client-x", "prod-1",
    )
    assert search_doc["metadata"]["content_hash"] == fresh_hash


def test_never_raises_on_malformed_service(caplog):
    class _BrokenService:
        def fetch_document_by_id(self, client_id, product_id):
            raise RuntimeError("boom")

    search_doc = _fresh_search_doc()
    # Must not raise -- fail-open, same as every other preserve helper here.
    _preserve_enrich_fields(search_doc, "trace-5", _BrokenService(), "client-x", "prod-1")
