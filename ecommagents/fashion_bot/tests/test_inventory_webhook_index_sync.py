"""End-to-end cover for the inventory webhook's Upstash write decision.

Reproduces the GANT incident at the handler level: an inventory webhook takes a
variant out of stock (index patched), a sibling ``products/update`` refreshes
Redis, and then the variant is restocked. The restock must still reach Upstash
Search, and routine quantity changes must still cost nothing.
"""

import pytest

from fashion_bot.services.product_ingestion import product_llm_cache
from fashion_bot.services.product_ingestion.product_llm_cache import (
    UPSTASH_VARIANT_SIGNATURE_KEY,
    variant_availability_signature,
)
from fashion_bot.shopify.webhook import product_webhook


CLIENT_ID = "f5a737a7-c274-48d3-a6d2-d067f14b755b"
PRODUCT_ID = "9897715433772"
SIZES = ["S", "M", "L", "XL", "XXL"]


def _snapshot(quantities):
    """Redis variant snapshot, one entry per size."""
    return [
        {
            "variant_id": f"variant-{size}",
            "inventory_item_id": 52860965355820 + i,
            "inventoryQuantity": quantities[size],
            "inventoryPolicy": "deny",
            "selectedOptions": [{"name": "Size", "value": size}],
        }
        for i, size in enumerate(SIZES)
    ]


def _sizes_in_stock(quantities):
    return sorted(s for s in SIZES if quantities[s] > 0)


class _Recorder:
    """Captures Upstash Search writes without touching the network."""

    def __init__(self):
        self.updates = []

    def install(self, monkeypatch):
        recorder = self

        class FakeSearchService:
            async def abulk_update_fields(self, client_id, updates):
                recorder.updates.extend(updates)
                return {"updated_count": len(updates)}

        import fashion_bot.services.product_ingestion.upstash_search_service as svc_mod
        monkeypatch.setattr(svc_mod, "UpstashSearchService", FakeSearchService)
        return self

    @property
    def call_count(self):
        return len(self.updates)

    def variants_written(self):
        """``{size: available}`` from the most recent write."""
        variants = self.updates[-1]["metadata_fields"]["variants"]
        return {v["option1"]: v["available"] for v in variants}

    def content_written(self):
        return self.updates[-1]["content_fields"]


@pytest.fixture
def recorder(monkeypatch):
    return _Recorder().install(monkeypatch)


@pytest.fixture(autouse=True)
def no_sync_logging(monkeypatch):
    """Keep the sync logger off Postgres — this suite is about write decisions."""
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(
        product_webhook.ProductSyncLogger, "alog_webhook_event", noop
    )


@pytest.fixture
def redis_writes(monkeypatch):
    """Capture Redis cache writes and expose the resulting cache entry."""
    written = {}

    async def fake_set(client_id, product_id, content_hash, llm_fields,
                       in_stock=True, available_sizes=None, available_colors=None,
                       variant_snapshot=None, semantic_content_hash=None,
                       cron_owned_fields=None, upstash_variant_signature=None):
        written.clear()
        written.update({
            "in_stock": in_stock,
            "available_sizes": sorted(available_sizes or []),
            "available_colors": sorted(available_colors or []),
            "variant_snapshot": variant_snapshot,
            UPSTASH_VARIANT_SIGNATURE_KEY: upstash_variant_signature,
        })

    monkeypatch.setattr(product_llm_cache, "aset_cached_product_data", fake_set)

    async def always_claim(client_id, product_id):
        return True

    monkeypatch.setattr(product_llm_cache, "atry_claim_inventory_upsert", always_claim)
    return written


async def _run_inventory_patch(quantities, redis_cached):
    """Drive the handler the way the warm path does."""
    snapshot = _snapshot(quantities)
    in_stock, total, sizes, colors = product_webhook._compute_stock_from_variants(snapshot)
    return await product_webhook._inventory_stock_diff_and_patch(
        client_id=CLIENT_ID,
        product_id=PRODUCT_ID,
        product_title="Gant Men Green Striped Collar Shirt",
        trace_id="test",
        redis_cached=redis_cached,
        new_in_stock=in_stock,
        new_total_inventory=total,
        new_avail_sizes=sizes,
        new_avail_colors=colors,
        updated_snapshot=snapshot,
        start=0.0,
        shopify_webhook_id="wh-test",
        shopify_shop_domain="gant-india.myshopify.com",
    )


def _cache_entry(quantities, *, receipt_for=None):
    """A Redis entry whose stock fields track *quantities*.

    ``receipt_for`` is the state Upstash actually holds — the whole point of the
    fix is that it can legitimately differ from the stock fields above it.
    """
    receipt_state = quantities if receipt_for is None else receipt_for
    return {
        "content_hash_no_inventory": "hash",
        "llm_fields": {},
        "in_stock": any(q > 0 for q in quantities.values()),
        "available_sizes": _sizes_in_stock(quantities),
        "available_colors": [],
        "variant_snapshot": _snapshot(quantities),
        UPSTASH_VARIANT_SIGNATURE_KEY: variant_availability_signature(
            _snapshot(receipt_state)
        ),
    }


class TestRestockReachesTheIndex:
    async def test_restock_patches_upstash_even_when_redis_already_caught_up(
        self, recorder, redis_writes
    ):
        """The GANT incident, start to finish.

        M sells out and the index is patched. A sibling products/update then
        refreshes Redis with M back in stock. The restock webhook must still
        write to Upstash — under the old Redis-based gate it logged "stock
        unchanged" and the widget struck M out for days.
        """
        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        restocked = {**sold_out, "M": 2}

        cached = _cache_entry(restocked, receipt_for=sold_out)
        result = await _run_inventory_patch(restocked, cached)

        assert result["action"] == "stock_patched"
        assert recorder.call_count == 1
        assert recorder.variants_written()["M"] is True

    async def test_patch_carries_variant_availability_pct(self, recorder, redis_writes):
        """pct rides along with the flags it is derived from."""
        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        restocked = {**sold_out, "M": 2}

        await _run_inventory_patch(restocked, _cache_entry(restocked, receipt_for=sold_out))

        content = recorder.content_written()
        assert content["variant_availability_pct"] == 100.0
        assert content["in_stock"] is True
        assert content["available_sizes"] == SIZES_SORTED

    async def test_sell_out_patches_upstash(self, recorder, redis_writes):
        all_stocked = {"S": 7, "M": 2, "L": 3, "XL": 2, "XXL": 5}
        sold_out_m = {**all_stocked, "M": 0}

        await _run_inventory_patch(sold_out_m, _cache_entry(all_stocked))

        assert recorder.call_count == 1
        assert recorder.variants_written()["M"] is False
        assert recorder.content_written()["variant_availability_pct"] == 80.0

    async def test_receipt_is_stamped_so_the_next_webhook_is_free(
        self, recorder, redis_writes
    ):
        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        restocked = {**sold_out, "M": 2}

        await _run_inventory_patch(restocked, _cache_entry(restocked, receipt_for=sold_out))

        assert redis_writes[UPSTASH_VARIANT_SIGNATURE_KEY] == (
            variant_availability_signature(_snapshot(restocked))
        )


class TestUpstashWritesStayMinimal:
    async def test_quantity_change_without_a_flip_writes_nothing(
        self, recorder, redis_writes
    ):
        """A sale that does not empty a variant must not touch Upstash."""
        before = {"S": 7, "M": 2, "L": 3, "XL": 2, "XXL": 5}
        after = {**before, "S": 6, "XXL": 4}

        result = await _run_inventory_patch(after, _cache_entry(before))

        assert result["action"] == "no_change"
        assert recorder.call_count == 0

    async def test_replayed_webhook_writes_nothing(self, recorder, redis_writes):
        state = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}

        result = await _run_inventory_patch(state, _cache_entry(state))

        assert result["action"] == "no_change"
        assert recorder.call_count == 0

    async def test_entry_without_a_receipt_repairs_once(self, recorder, redis_writes):
        """Pre-rollout cache entries pay exactly one write, then go quiet."""
        state = {"S": 7, "M": 2, "L": 3, "XL": 2, "XXL": 5}
        legacy = _cache_entry(state)
        legacy.pop(UPSTASH_VARIANT_SIGNATURE_KEY)

        await _run_inventory_patch(state, legacy)
        assert recorder.call_count == 1

        healed = {**legacy, UPSTASH_VARIANT_SIGNATURE_KEY: redis_writes[UPSTASH_VARIANT_SIGNATURE_KEY]}
        await _run_inventory_patch(state, healed)
        assert recorder.call_count == 1

    async def test_dedup_loss_does_not_claim_a_write_it_did_not_make(
        self, recorder, monkeypatch, redis_writes
    ):
        """Losing the claim must leave the old receipt in place.

        Stamping the new signature here would tell every later webhook the index
        is current — the exact false assurance that stranded the document.
        """
        async def never_claim(client_id, product_id):
            return False

        monkeypatch.setattr(product_llm_cache, "atry_claim_inventory_upsert", never_claim)

        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        restocked = {**sold_out, "M": 2}
        cached = _cache_entry(restocked, receipt_for=sold_out)

        await _run_inventory_patch(restocked, cached)

        assert recorder.call_count == 0
        assert redis_writes[UPSTASH_VARIANT_SIGNATURE_KEY] == (
            cached[UPSTASH_VARIANT_SIGNATURE_KEY]
        )


class _Normalized:
    def __init__(self, quantities):
        self.variants = [
            {"id": f"variant-{s}", "available": q > 0, "option1": s}
            for s, q in quantities.items()
        ]
        self.in_stock = any(q > 0 for q in quantities.values())
        self.total_inventory = sum(quantities.values())
        self.available_sizes = _sizes_in_stock(quantities)
        self.available_colors = []


class TestRepairRespectsAnExistingClaim:
    async def test_caller_holding_the_claim_still_patches(
        self, recorder, monkeypatch
    ):
        """The dedup claim is SET NX on a per-product key.

        A caller past ``handle_product_upsert``'s dedup gate already owns that
        key; re-taking it would fail against itself and silently skip the patch
        that this whole change exists to make.
        """
        async def never_claim(client_id, product_id):
            return False

        monkeypatch.setattr(product_llm_cache, "atry_claim_inventory_upsert", never_claim)

        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        restocked = {**sold_out, "M": 2}
        cached = {
            UPSTASH_VARIANT_SIGNATURE_KEY: variant_availability_signature(_snapshot(sold_out))
        }

        receipt = await product_webhook._arepair_variants_if_stale(
            client_id=CLIENT_ID,
            product_id=PRODUCT_ID,
            trace_id="test",
            normalized_product=_Normalized(restocked),
            redis_cached=cached,
            already_claimed=True,
        )

        assert recorder.call_count == 1
        assert recorder.variants_written()["M"] is True
        assert receipt == variant_availability_signature(_snapshot(restocked))

    async def test_without_the_claim_it_defers_to_the_winner(
        self, recorder, monkeypatch
    ):
        async def never_claim(client_id, product_id):
            return False

        monkeypatch.setattr(product_llm_cache, "atry_claim_inventory_upsert", never_claim)

        sold_out = {"S": 7, "M": 0, "L": 3, "XL": 2, "XXL": 5}
        prior = variant_availability_signature(_snapshot(sold_out))

        receipt = await product_webhook._arepair_variants_if_stale(
            client_id=CLIENT_ID,
            product_id=PRODUCT_ID,
            trace_id="test",
            normalized_product=_Normalized({**sold_out, "M": 2}),
            redis_cached={UPSTASH_VARIANT_SIGNATURE_KEY: prior},
        )

        assert recorder.call_count == 0
        assert receipt == prior

    async def test_no_flip_costs_nothing_even_holding_the_claim(self, recorder):
        state = {"S": 7, "M": 2, "L": 3, "XL": 2, "XXL": 5}
        cached = {
            UPSTASH_VARIANT_SIGNATURE_KEY: variant_availability_signature(_snapshot(state))
        }

        await product_webhook._arepair_variants_if_stale(
            client_id=CLIENT_ID,
            product_id=PRODUCT_ID,
            trace_id="test",
            normalized_product=_Normalized({**state, "S": 1}),
            redis_cached=cached,
            already_claimed=True,
        )

        assert recorder.call_count == 0


class TestDegradedInput:
    async def test_empty_variant_list_never_blanks_the_document(
        self, recorder, redis_writes
    ):
        """A degraded fetch must not write ``variants: []`` and pct 100."""
        patched = await product_webhook._apatch_upstash_variant_stock(
            client_id=CLIENT_ID,
            product_id=PRODUCT_ID,
            trace_id="test",
            variant_dicts=[],
            in_stock=False,
            total_inventory=0,
            available_sizes=[],
            available_colors=[],
        )

        assert patched is False
        assert recorder.call_count == 0


SIZES_SORTED = sorted(SIZES)
