"""
Tests that a product webhook cannot demote a bestseller or delete its analytics.

Background
----------
Upstash Search ``upsert`` REPLACES a document rather than merging into it. Four
content fields are written only by the monthly ``bestseller_refresh`` cron and
are absent from every Shopify webhook payload:

    bestseller, unique_sessions, units_sold, conversion_rate

``NormalizedProduct.bestseller`` defaults to ``False`` and the analytics default
to ``None`` (stripped from the document by the ``if v is not None`` filter). So
every product webhook rewrote the flag as false and dropped the analytics keys
entirely — silently undoing the cron until its next monthly run.

Two halves are needed, and both are tested here:

  1. The webhook restores these from Redis on **every** upsert — not only when
     LLM extraction is skipped, since a genuine title change must not demote a
     bestseller either.
  2. The cron re-syncs Redis after it writes Upstash. Without this, caching the
     flag would be worse than the original bug: a stale ``bestseller: true``
     would resurrect every product the cron had just demoted.
"""

import asyncio
import json

import pytest

from fashion_bot.services.product_ingestion.product_llm_cache import (
    CRON_OWNED_CONTENT_FIELDS,
    collect_cron_owned_fields_from_doc,
)
from fashion_bot.shopify.webhook.product_webhook import _preserve_cron_owned_fields

CACHED_ENTRY = {
    "cron_owned_fields": {
        "bestseller": True,
        "unique_sessions": 412,
        "units_sold": 37,
        "conversion_rate": 0.09,
    }
}


def _webhook_doc():
    """A document as built from a Shopify webhook: cron fields absent/default."""
    return {
        "id": "client_998",
        "content": {
            "title": "Gant Men Green Solid Beanies",
            "price_min": 4299.0,
            # NormalizedProduct.bestseller defaults to False and is always written.
            "bestseller": False,
            # Analytics are None and get stripped, so the keys are simply absent.
        },
        "metadata": {"product_id": "998"},
    }


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------

def test_bestseller_is_not_demoted_by_a_webhook():
    doc = _webhook_doc()

    _preserve_cron_owned_fields(doc, "trace-1", cached_entry=CACHED_ENTRY)

    assert doc["content"]["bestseller"] is True, (
        "a webhook payload knows nothing about bestseller; upsert replaces the "
        "document, so a defaulted False would silently demote the product"
    )


@pytest.mark.parametrize("field", ["unique_sessions", "units_sold", "conversion_rate"])
def test_analytics_are_not_dropped_by_a_webhook(field):
    doc = _webhook_doc()
    assert field not in doc["content"]  # stripped as None before the upsert

    _preserve_cron_owned_fields(doc, "trace-1", cached_entry=CACHED_ENTRY)

    assert doc["content"][field] == CACHED_ENTRY["cron_owned_fields"][field]


def test_fixture_covers_every_cron_owned_field():
    assert set(CACHED_ENTRY["cron_owned_fields"]) == set(CRON_OWNED_CONTENT_FIELDS)


# ---------------------------------------------------------------------------
# Demotion must still work — this is what makes caching the flag safe
# ---------------------------------------------------------------------------

def test_demoted_product_stays_demoted():
    """Once the cron clears the flag, nothing is left to restore."""
    doc = _webhook_doc()

    _preserve_cron_owned_fields(doc, "trace-1", cached_entry={"cron_owned_fields": {}})

    assert doc["content"]["bestseller"] is False


def test_collect_drops_falsy_so_demotion_propagates():
    collected = collect_cron_owned_fields_from_doc({
        "content": {"bestseller": False, "units_sold": 0, "unique_sessions": 5},
    })

    assert collected == {"unique_sessions": 5}


def test_fresh_values_are_never_overwritten():
    doc = _webhook_doc()
    doc["content"]["unique_sessions"] = 999

    _preserve_cron_owned_fields(doc, "trace-1", cached_entry=CACHED_ENTRY)

    assert doc["content"]["unique_sessions"] == 999


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

class _CountingSearchService:
    def __init__(self, doc=None):
        self.calls = 0
        self._doc = doc

    def fetch_document_by_id(self, client_id, product_id):
        self.calls += 1
        return self._doc


def test_cold_cache_falls_back_to_the_existing_upstash_doc():
    doc = _webhook_doc()
    service = _CountingSearchService({"content": {"bestseller": True, "units_sold": 12}})

    _preserve_cron_owned_fields(
        doc, "trace-1",
        cached_entry=None,
        search_service=service,
        client_id="client",
        product_id="998",
    )

    assert doc["content"]["bestseller"] is True
    assert doc["content"]["units_sold"] == 12
    assert service.calls == 1


def test_warm_cache_without_cron_fields_does_not_read_upstash():
    """The common case: not a bestseller, no analytics.

    fetch_document_by_id is synchronous, so falling back here would block the
    event loop on essentially every product webhook — in the exact path the
    Redis cache exists to keep read-free.
    """
    doc = _webhook_doc()
    service = _CountingSearchService({"content": {"bestseller": True}})

    _preserve_cron_owned_fields(
        doc, "trace-1",
        cached_entry={"content_hash_no_inventory": "h"},  # warm, no cron block
        search_service=service,
        client_id="client",
        product_id="998",
    )

    assert service.calls == 0, "a warm cache entry must never trigger an Upstash read"
    assert doc["content"]["bestseller"] is False


def test_never_raises_when_everything_is_missing():
    _preserve_cron_owned_fields({}, "trace-1", cached_entry=None)
    _preserve_cron_owned_fields({"content": None}, "trace-1", cached_entry=CACHED_ENTRY)


# ---------------------------------------------------------------------------
# The cron must re-sync Redis, or caching the flag resurrects demotions
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, entries):
        self.store = dict(entries)
        self._queued = []

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    def pipeline(self, transaction=False):
        return self

    def setex(self, key, ttl, value):
        self._queued.append((key, value))

    async def execute(self):
        for key, value in self._queued:
            self.store[key] = value
        self._queued = []


def _run_cron_sync(monkeypatch, entries, documents):
    from fashion_bot.services.product_ingestion import product_llm_cache as cache

    fake = _FakeRedis(entries)

    async def _client():
        return fake

    monkeypatch.setattr(cache, "_aget_redis_client", _client)
    written = asyncio.run(cache.aupdate_cached_cron_fields("c1", documents))
    return fake, written


def test_cron_resync_clears_a_demoted_flag(monkeypatch):
    key = "product_llm:c1:998"
    entries = {key: json.dumps({
        "content_hash_no_inventory": "h",
        "cron_owned_fields": {"bestseller": True},
    })}
    demoted = {"metadata": {"product_id": "998"}, "content": {"bestseller": False}}

    fake, written = _run_cron_sync(monkeypatch, entries, [demoted])

    assert written == 1
    assert "cron_owned_fields" not in json.loads(fake.store[key]), (
        "a stale bestseller=true would resurrect the product on the next webhook"
    )


def test_cron_resync_records_a_promotion(monkeypatch):
    key = "product_llm:c1:998"
    entries = {key: json.dumps({"content_hash_no_inventory": "h"})}
    promoted = {
        "metadata": {"product_id": "998"},
        "content": {"bestseller": True, "units_sold": 40},
    }

    fake, written = _run_cron_sync(monkeypatch, entries, [promoted])

    assert written == 1
    stored = json.loads(fake.store[key])["cron_owned_fields"]
    assert stored == {"bestseller": True, "units_sold": 40}
    # Unrelated cache content survives the patch.
    assert json.loads(fake.store[key])["content_hash_no_inventory"] == "h"


def test_cron_resync_skips_products_with_no_cache_entry(monkeypatch):
    """A cache miss cannot restore anything later, so there is nothing to fix."""
    fake, written = _run_cron_sync(
        monkeypatch, {}, [{"metadata": {"product_id": "998"}, "content": {"bestseller": True}}]
    )

    assert written == 0
    assert fake.store == {}
