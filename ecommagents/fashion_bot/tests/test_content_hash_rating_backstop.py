"""compute_content_hash_excluding_inventory() must move when rating/count
metafields change -- this is what lets the existing products/update webhook
and the weekly delta sync notice a Judge.me review at all. There's no
dedicated Judge.me webhook in this codebase; a metafield write (rating
included) already fires Shopify's products/update webhook on its own, so
this hash change is the whole mechanism, not a backstop for something else.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from fashion_bot.services.product_ingestion.models import NormalizedProduct


def _product(**overrides) -> NormalizedProduct:
    base = dict(
        id="1", title="Test Product", handle="test-product", product_type="Tee",
        vendor="Groovee", tags=[], colors=[], sizes=[], price_min=999.0, price_max=999.0,
        image_url="https://example.com/img.jpg", product_url="https://example.com/p",
        in_stock=True, description="A product.",
    )
    base.update(overrides)
    return NormalizedProduct(**base)


def test_hash_changes_when_rating_metafield_changes():
    p1 = _product(metafield_attributes={"rating": '{"value":"4.5"}', "rating_count": "10"})
    p2 = _product(metafield_attributes={"rating": '{"value":"4.6"}', "rating_count": "11"})
    assert p1.compute_content_hash_excluding_inventory() != p2.compute_content_hash_excluding_inventory()


def test_hash_stable_when_nothing_changes():
    p1 = _product(metafield_attributes={"rating": '{"value":"4.5"}', "rating_count": "10"})
    p2 = _product(metafield_attributes={"rating": '{"value":"4.5"}', "rating_count": "10"})
    assert p1.compute_content_hash_excluding_inventory() == p2.compute_content_hash_excluding_inventory()


def test_hash_unaffected_by_unrelated_metafields():
    """Only rating/rating_count are pulled in -- other metafields (e.g. the
    judgeme namespace's badge/widget HTML, deliberately skipped elsewhere)
    must not affect this hash."""
    p1 = _product(metafield_attributes={"rating": '{"value":"4.5"}', "rating_count": "10", "size_guide": "A"})
    p2 = _product(metafield_attributes={"rating": '{"value":"4.5"}', "rating_count": "10", "size_guide": "B"})
    assert p1.compute_content_hash_excluding_inventory() == p2.compute_content_hash_excluding_inventory()


def test_hash_works_with_no_rating_yet():
    """A product with 0 reviews (no rating metafield at all) must not crash --
    the common/default case."""
    p = _product(metafield_attributes={})
    assert p.compute_content_hash_excluding_inventory()  # just must not raise, non-empty string


def _pre_feature_hash(p: NormalizedProduct) -> str:
    """Reconstruct the exact hash this function computed before rating/
    rating_count existed as keys at all, independent of the current
    implementation, to prove a no-rating product's hash is byte-identical
    to what it always was -- not just "doesn't crash"."""
    hash_content = {
        "title": p.title, "product_type": p.product_type, "vendor": p.vendor,
        "tags": sorted(p.tags) if p.tags else [],
        "colors": sorted(p.colors) if p.colors else [],
        "sizes": sorted(p.sizes) if p.sizes else [],
        "price_min": p.price_min, "price_max": p.price_max,
        "compare_at_price_min": p.compare_at_price_min,
        "compare_at_price_max": p.compare_at_price_max,
        "discount_pct": p.discount_pct, "status": p.status,
        "description": p.description[:500] if p.description else "",
        "fabric": p.fabric, "care_instructions": p.care_instructions,
        "image_url": p.image_url,
    }
    content_str = json.dumps(hash_content, sort_keys=True, default=str)
    return hashlib.md5(content_str.encode()).hexdigest()


def test_hash_unchanged_for_unrated_product_vs_pre_feature_baseline():
    """The actual regression this backstop must not cause: a product with no
    rating metafield must NOT get a new hash just because these two keys
    were added to the function. Adding them unconditionally (even as None)
    would move every unrated product's hash on the first sync after deploy,
    forcing a full-catalog re-extraction storm to fix staleness on the
    (usually small) minority of products that actually have reviews."""
    p_no_metafields = _product(metafield_attributes={})
    assert p_no_metafields.compute_content_hash_excluding_inventory() == _pre_feature_hash(p_no_metafields)

    p_other_metafields_only = _product(metafield_attributes={"size_guide": "A", "delivery": "3-5 days"})
    assert (
        p_other_metafields_only.compute_content_hash_excluding_inventory()
        == _pre_feature_hash(p_other_metafields_only)
    )


def test_hash_changes_once_when_rating_first_appears():
    """A product transitioning from 0 reviews to its first review DOES
    legitimately need to re-sync once -- that's the whole point of the
    backstop -- so its hash must differ from the unrated baseline."""
    p_unrated = _product(metafield_attributes={})
    p_first_review = _product(metafield_attributes={"rating": '{"value":"5.0"}', "rating_count": "1"})
    assert (
        p_unrated.compute_content_hash_excluding_inventory()
        != p_first_review.compute_content_hash_excluding_inventory()
    )


def test_hash_still_ignores_inventory_fields():
    """Sanity: the existing exclude-inventory behavior isn't broken by this change."""
    p1 = _product(total_inventory=5, metafield_attributes={"rating_count": "10"})
    p2 = _product(total_inventory=50, metafield_attributes={"rating_count": "10"})
    assert p1.compute_content_hash_excluding_inventory() == p2.compute_content_hash_excluding_inventory()
