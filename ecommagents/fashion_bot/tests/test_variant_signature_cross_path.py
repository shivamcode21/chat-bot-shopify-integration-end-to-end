"""The signature must mean the same thing to every writer.

Four code paths compute a variant-availability signature for the same product,
from four different in-memory shapes. If any two disagree for one real-world
stock state, each will read the other's receipt as stale and patch Upstash on
every single webhook — turning a fix that was meant to *reduce* index writes
into a write amplifier. These tests pin the shapes against each other.
"""

from fashion_bot.services.product_ingestion.product_llm_cache import (
    UPSTASH_VARIANT_SIGNATURE_KEY,
    aset_cached_llm_data_bulk,
    variant_availability_signature,
)
from fashion_bot.services.product_ingestion.shopify_product_service import (
    ShopifyProductService,
)
from fashion_bot.shopify.webhook import product_webhook


CLIENT_ID = "f5a737a7-c274-48d3-a6d2-d067f14b755b"

# One product, one real stock state, expressed as Shopify's REST webhook payload.
# M sold out, everything else in stock — the GANT shirt's state.
REST_PAYLOAD = {
    "id": 9897715433772,
    "title": "Gant Men Green Striped Collar Shirt",
    "handle": "gant-gms25-003000140322-green-shirt",
    "body_html": "<p>Shirt</p>",
    "vendor": "Gant",
    "product_type": "Shirt",
    "status": "active",
    "tags": "Shirt, Men",
    "options": [{"name": "Size", "values": ["S", "M", "L", "XL", "XXL"]}],
    "variants": [
        {"id": 50815679004972, "title": "S", "option1": "S", "price": "4739.00",
         "inventory_quantity": 7, "inventory_policy": "deny",
         "inventory_item_id": 52860965323052, "sku": "a"},
        {"id": 50815679037740, "title": "M", "option1": "M", "price": "4739.00",
         "inventory_quantity": 0, "inventory_policy": "deny",
         "inventory_item_id": 52860965355820, "sku": "b"},
        {"id": 50815679070508, "title": "L", "option1": "L", "price": "4739.00",
         "inventory_quantity": 3, "inventory_policy": "deny",
         "inventory_item_id": 52860965388588, "sku": "c"},
        {"id": 50815679103276, "title": "XL", "option1": "XL", "price": "4739.00",
         "inventory_quantity": 2, "inventory_policy": "deny",
         "inventory_item_id": 52860965421356, "sku": "d"},
        {"id": 50815679136044, "title": "XXL", "option1": "XXL", "price": "4739.00",
         "inventory_quantity": 5, "inventory_policy": "deny",
         "inventory_item_id": 52860965454124, "sku": "e"},
    ],
    "images": [],
}


def _graphql_nodes():
    """What ``_INVENTORY_RESOLVE_QUERY`` returns — uppercase policy enum, GIDs."""
    return [
        {
            "id": f"gid://shopify/ProductVariant/{v['id']}",
            "inventoryQuantity": v["inventory_quantity"],
            "inventoryPolicy": v["inventory_policy"].upper(),
            "selectedOptions": [{"name": "Size", "value": v["option1"]}],
            "inventoryItem": {"id": f"gid://shopify/InventoryItem/{v['inventory_item_id']}"},
        }
        for v in REST_PAYLOAD["variants"]
    ]


async def _normalized_product(payload=None):
    """What the products/update path hands to the repair helper."""
    service = object.__new__(ShopifyProductService)
    # Only the two attrs _normalize_product reads; __init__ opens clients we
    # neither have nor need for a pure shape comparison.
    service.shop_domain = "gant-india.myshopify.com"
    service.website_base_url = "https://gant.in"
    graphql_shaped = product_webhook._convert_webhook_to_graphql_format(
        payload or REST_PAYLOAD
    )
    return await service._normalize_product(graphql_shaped)


class TestEveryWriterAgrees:
    async def test_redis_snapshot_matches_normalized_product(self):
        """products/update (REST -> NormalizedProduct) vs the warm Redis snapshot."""
        snapshot, _ = product_webhook._build_variant_snapshot_from_product_data(REST_PAYLOAD)
        from_snapshot = variant_availability_signature(
            product_webhook._build_variant_dicts(snapshot)
        )
        normalized = await _normalized_product()
        from_normalized = variant_availability_signature(normalized.variants)
        assert from_snapshot == from_normalized

    def test_graphql_cold_path_matches_the_warm_path(self):
        """Cold path speaks uppercase enums and GIDs; warm path does not."""
        gql_snapshot, _ = product_webhook._build_variant_snapshot_from_graphql(
            _graphql_nodes(), str(REST_PAYLOAD["id"])
        )
        rest_snapshot, _ = product_webhook._build_variant_snapshot_from_product_data(REST_PAYLOAD)

        assert variant_availability_signature(
            product_webhook._build_variant_dicts(gql_snapshot)
        ) == variant_availability_signature(
            product_webhook._build_variant_dicts(rest_snapshot)
        )

    async def test_indexed_document_matches_its_source(self):
        """The receipt stamped after a full upsert must match the next webhook's read."""
        normalized = await _normalized_product()
        doc = normalized.to_search_document(CLIENT_ID)

        assert variant_availability_signature(doc["metadata"]["variants"]) == (
            variant_availability_signature(normalized.variants)
        )

    async def test_a_flip_moves_every_writer_together(self):
        """Restock M: all shapes must agree it changed, and agree on the new value."""
        restocked = {
            **REST_PAYLOAD,
            "variants": [
                {**v, "inventory_quantity": 2} if v["option1"] == "M" else v
                for v in REST_PAYLOAD["variants"]
            ],
        }
        before, _ = product_webhook._build_variant_snapshot_from_product_data(REST_PAYLOAD)
        after, _ = product_webhook._build_variant_snapshot_from_product_data(restocked)

        sig_before = variant_availability_signature(product_webhook._build_variant_dicts(before))
        sig_after = variant_availability_signature(product_webhook._build_variant_dicts(after))
        assert sig_before != sig_after

        normalized_after = await _normalized_product(restocked)
        assert variant_availability_signature(normalized_after.variants) == sig_after

    def test_oversell_variant_agrees_across_rest_and_graphql_casing(self):
        """A zero-quantity 'continue' variant is purchasable in both dialects."""
        oversell = {
            **REST_PAYLOAD,
            "variants": [
                {**v, "inventory_quantity": 0, "inventory_policy": "continue"}
                for v in REST_PAYLOAD["variants"]
            ],
        }
        rest_snapshot, _ = product_webhook._build_variant_snapshot_from_product_data(oversell)
        gql_nodes = [
            {**n, "inventoryQuantity": 0, "inventoryPolicy": "CONTINUE"}
            for n in _graphql_nodes()
        ]
        gql_snapshot, _ = product_webhook._build_variant_snapshot_from_graphql(
            gql_nodes, str(oversell["id"])
        )

        rest_dicts = product_webhook._build_variant_dicts(rest_snapshot)
        assert all(v["available"] for v in rest_dicts)
        assert variant_availability_signature(rest_dicts) == (
            variant_availability_signature(product_webhook._build_variant_dicts(gql_snapshot))
        )


class TestFailedUpsertsGetNoReceipt:
    async def test_failed_doc_is_cached_but_carries_no_receipt(self, monkeypatch):
        """A doc the index rejected must stay repairable.

        Bulk ingestion hands every doc to the cache, successes and failures
        alike. Stamping a receipt for a rejected doc would assert the index is
        current and strand it exactly as the original bug did.
        """
        writes = {}

        class FakePipe:
            def setex(self, key, ttl, payload):
                writes[key] = payload

            async def execute(self):
                return None

        class FakeRedis:
            def pipeline(self, transaction=False):
                return FakePipe()

        async def fake_client():
            return FakeRedis()

        import fashion_bot.services.product_ingestion.product_llm_cache as cache_mod
        monkeypatch.setattr(cache_mod, "_aget_redis_client", fake_client)

        ok_doc = (await _normalized_product()).to_search_document(CLIENT_ID)
        bad_doc = {
            **ok_doc,
            "id": f"{CLIENT_ID}_rejected",
            "metadata": {**ok_doc["metadata"], "product_id": "rejected"},
        }

        written = await aset_cached_llm_data_bulk(
            CLIENT_ID, [ok_doc, bad_doc], failed_doc_ids={bad_doc["id"]}
        )
        assert written == 2

        import json
        by_pid = {k: json.loads(v) for k, v in writes.items()}
        succeeded = next(v for k, v in by_pid.items() if k.endswith(str(REST_PAYLOAD["id"])))
        rejected = next(v for k, v in by_pid.items() if k.endswith("rejected"))

        assert succeeded[UPSTASH_VARIANT_SIGNATURE_KEY] == (
            variant_availability_signature(ok_doc["metadata"]["variants"])
        )
        assert UPSTASH_VARIANT_SIGNATURE_KEY not in rejected
        # Still cached — only the index claim is withheld.
        assert rejected["available_sizes"] == sorted(["S", "L", "XL", "XXL"])
