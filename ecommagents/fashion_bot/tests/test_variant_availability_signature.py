"""Gate that decides when Upstash Search's stock projection is rewritten.

Regression cover for the GANT "size M shown sold out while Shopify had 2 in
stock" incident: the old gate compared against Redis, which several webhook
paths refresh while deliberately skipping the index write. Once the two
diverged, every later check answered "nothing changed" and the index served a
sold-out flag for a restocked variant indefinitely.
"""

import pytest

from fashion_bot.services.product_ingestion.product_llm_cache import (
    UPSTASH_VARIANT_SIGNATURE_KEY,
    upstash_variants_stale,
    variant_availability_pct,
    variant_availability_signature,
)


def _snapshot(**sizes):
    """Redis-snapshot shape: variant_id + inventoryQuantity/inventoryPolicy."""
    return [
        {
            "variant_id": f"v{name}",
            "inventoryQuantity": qty,
            "inventoryPolicy": "deny",
            "selectedOptions": [{"name": "Size", "value": name}],
        }
        for name, qty in sizes.items()
    ]


def _index_dicts(**sizes):
    """Upstash metadata shape: id + available."""
    return [
        {"id": f"v{name}", "available": qty > 0, "option1": name}
        for name, qty in sizes.items()
    ]


class TestSignatureShapeAgnostic:
    def test_snapshot_and_index_shapes_agree(self):
        """All three variant shapes must hash alike or the gate flaps forever."""
        state = dict(S=7, M=0, L=3)
        assert variant_availability_signature(_snapshot(**state)) == (
            variant_availability_signature(_index_dicts(**state))
        )

    def test_gid_and_bare_variant_ids_agree(self):
        bare = [{"id": "50815671861548", "available": True}]
        gid = [{"id": "gid://shopify/ProductVariant/50815671861548", "available": True}]
        assert variant_availability_signature(bare) == variant_availability_signature(gid)

    def test_variant_order_does_not_matter(self):
        a = _index_dicts(S=1, M=0, L=2)
        assert variant_availability_signature(a) == variant_availability_signature(a[::-1])

    def test_continue_policy_counts_as_available_at_zero(self):
        oversell = [{
            "variant_id": "vM",
            "inventoryQuantity": 0,
            "inventoryPolicy": "continue",
        }]
        in_stock = [{"variant_id": "vM", "inventoryQuantity": 4, "inventoryPolicy": "deny"}]
        assert variant_availability_signature(oversell) == (
            variant_availability_signature(in_stock)
        )


class TestSignatureTracksFlipsOnly:
    def test_quantity_change_without_a_flip_is_the_same_signature(self):
        """Routine decrements must cost zero Upstash writes."""
        assert variant_availability_signature(_snapshot(S=7, M=2, L=3)) == (
            variant_availability_signature(_snapshot(S=1, M=1, L=9))
        )

    def test_sell_out_moves_the_signature(self):
        assert variant_availability_signature(_snapshot(S=7, M=2)) != (
            variant_availability_signature(_snapshot(S=7, M=0))
        )

    def test_restock_moves_the_signature(self):
        assert variant_availability_signature(_snapshot(S=7, M=0)) != (
            variant_availability_signature(_snapshot(S=7, M=2))
        )

    def test_flip_is_detected_even_when_sizes_list_is_unchanged(self):
        """Two colours share a size: available_sizes cannot see this flip.

        The old size-list gate was blind here; the per-variant signature is not.
        """
        before = [
            {"id": "blue-m", "available": True},
            {"id": "red-m", "available": False},
        ]
        after = [
            {"id": "blue-m", "available": True},
            {"id": "red-m", "available": True},
        ]
        assert variant_availability_signature(before) != variant_availability_signature(after)


class TestStalenessGate:
    def test_matching_receipt_is_not_stale(self):
        variants = _index_dicts(S=1, M=0)
        sig = variant_availability_signature(variants)
        cached = {UPSTASH_VARIANT_SIGNATURE_KEY: sig}
        assert upstash_variants_stale(cached, sig) is False

    def test_entry_without_a_receipt_is_stale(self):
        """Pre-rollout entries repair themselves on their next webhook."""
        assert upstash_variants_stale({"in_stock": True}, "abc") is True

    def test_missing_cache_is_stale(self):
        assert upstash_variants_stale(None, "abc") is True

    def test_the_gant_incident_restock_is_caught(self):
        """M sells out, index patched; M restocks — must be seen as stale.

        Under the old Redis-based gate this returned "unchanged" because a
        sibling products/update webhook had already refreshed Redis with M
        available, so the index kept M struck out in the add-to-cart panel.
        """
        sold_out = _snapshot(S=7, M=0, L=3, XL=2, XXL=5)
        cached = {
            UPSTASH_VARIANT_SIGNATURE_KEY: variant_availability_signature(sold_out),
            # Redis has already caught up with Shopify — this is exactly the
            # divergence the old gate could not see past.
            "in_stock": True,
            "available_sizes": ["L", "M", "S", "XL", "XXL"],
        }
        restocked = variant_availability_signature(_snapshot(S=7, M=2, L=3, XL=2, XXL=5))
        assert upstash_variants_stale(cached, restocked) is True


class TestVariantAvailabilityPct:
    @pytest.mark.parametrize(
        "variants,expected",
        [
            (_index_dicts(S=1, M=1, L=1, XL=1, XXL=1), 100.0),
            (_index_dicts(S=1, M=0, L=1, XL=1, XXL=1), 80.0),
            (_index_dicts(S=0, M=0, L=0, XL=0, XXL=1), 20.0),
            (_index_dicts(S=0, M=0), 0.0),
        ],
    )
    def test_pct_matches_share_of_purchasable_variants(self, variants, expected):
        assert variant_availability_pct(variants) == expected

    def test_empty_variant_list_does_not_divide_by_zero(self):
        assert variant_availability_pct([]) == 100.0

    def test_pct_matches_normalizer_formula(self):
        """Search filters on this field; the two writers must not drift."""
        variants = _index_dicts(S=1, M=0, L=1)
        expected = round(2 / 3 * 100, 1)
        assert variant_availability_pct(variants) == expected
