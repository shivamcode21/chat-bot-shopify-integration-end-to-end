"""
Tests for forced LLM attribute re-extraction
(``services/product_ingestion/re_extract.py``, exposed as
``POST /api/v1/products/re-extract``).

This is the escape hatch for the narrowed LLM gate. Since extraction now only
re-runs when a product's identity changes (title / product_type / description),
an attribute inferred solely from tags or metafields can go stale. This forces
a re-derivation for named products.

The guardrails matter as much as the happy path: every call drives real LLM
requests, so an unbounded or accidentally catalog-wide request is a spend
incident rather than a slow response.

The logic lives outside ``agent_controller`` deliberately — that module opens
database connections at import time, which would make this untestable.
"""

import asyncio

import pytest

from fashion_bot.services.product_ingestion.re_extract import (
    CONCURRENCY,
    MAX_PRODUCTS,
    ReExtractRequestError,
    arun_product_re_extraction,
    normalize_product_ids,
)


class _StubOrchestrator:
    """Records the products it was asked to re-ingest."""

    def __init__(self, impl=None):
        self.seen = []
        self._impl = impl

    async def ingest_single_product(self, client_id, product_id, **kwargs):
        self.seen.append(product_id)
        if self._impl is not None:
            return await self._impl(client_id, product_id)
        return {"success": True, "product_id": product_id}


def _run(client_id, product_ids, orchestrator):
    return asyncio.run(
        arun_product_re_extraction(client_id, product_ids, orchestrator=orchestrator)
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_re_extracts_each_requested_product():
    stub = _StubOrchestrator()
    result = _run("c1", ["1", "2", "3"], stub)

    assert result["success"] is True
    assert (result["requested"], result["succeeded"], result["failed"]) == (3, 3, 0)
    assert sorted(stub.seen) == ["1", "2", "3"]


def test_duplicate_ids_are_paid_for_once():
    stub = _StubOrchestrator()
    result = _run("c1", ["7", "7", 7, "8"], stub)

    assert stub.seen.count("7") == 1
    assert result["requested"] == 2


def test_numeric_ids_are_coerced_to_strings():
    stub = _StubOrchestrator()
    _run("c1", [12345], stub)

    assert stub.seen == ["12345"]


# ---------------------------------------------------------------------------
# Guardrails — this path spends money
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("client_id,product_ids,expected", [
    (None, ["1"], "client_id"),
    ("", ["1"], "client_id"),
    ("c1", None, "product_ids"),
    ("c1", [], "product_ids"),
    ("c1", "1,2,3", "product_ids"),          # a string is a Sequence — must still reject
    ("c1", {"a": 1}, "product_ids"),
])
def test_invalid_requests_rejected_before_any_llm_call(client_id, product_ids, expected):
    stub = _StubOrchestrator()

    with pytest.raises(ReExtractRequestError) as exc:
        _run(client_id, product_ids, stub)

    assert expected in str(exc.value)
    assert stub.seen == [], "no LLM call may happen for a rejected request"


def test_batch_larger_than_cap_is_rejected():
    stub = _StubOrchestrator()
    over_cap = [str(i) for i in range(MAX_PRODUCTS + 1)]

    with pytest.raises(ReExtractRequestError) as exc:
        _run("c1", over_cap, stub)

    assert "per-call limit" in str(exc.value)
    assert stub.seen == []


def test_batch_at_the_cap_is_allowed():
    stub = _StubOrchestrator()
    at_cap = [str(i) for i in range(MAX_PRODUCTS)]

    result = _run("c1", at_cap, stub)

    assert result["requested"] == MAX_PRODUCTS
    assert result["succeeded"] == MAX_PRODUCTS


def test_concurrency_is_bounded():
    in_flight = 0
    peak = 0

    async def _impl(client_id, product_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return {"success": True, "product_id": product_id}

    stub = _StubOrchestrator(_impl)
    _run("c1", [str(i) for i in range(40)], stub)

    assert peak <= CONCURRENCY, (
        "a burst of parallel ingests throttles the Shopify enrich, which is the "
        "very failure this whole change exists to stop"
    )


# ---------------------------------------------------------------------------
# Partial failure must not lose the successes
# ---------------------------------------------------------------------------

def test_one_failure_does_not_abort_the_batch():
    async def _impl(client_id, product_id):
        if product_id == "2":
            raise RuntimeError("Shopify throttled")
        return {"success": True, "product_id": product_id}

    stub = _StubOrchestrator(_impl)
    result = _run("c1", ["1", "2", "3"], stub)

    assert result["success"] is False
    assert (result["succeeded"], result["failed"]) == (2, 1)
    failed = [r for r in result["results"] if not r.get("success")]
    assert "Shopify throttled" in failed[0]["error"]
    assert failed[0]["product_id"] == "2"


def test_orchestrator_reporting_failure_is_counted_as_failed():
    """A clean `{"success": False}` return is a failure, not just an exception."""
    async def _impl(client_id, product_id):
        return {"success": False, "product_id": product_id, "error": "not found"}

    stub = _StubOrchestrator(_impl)
    result = _run("c1", ["9"], stub)

    assert result["success"] is False
    assert result["failed"] == 1


def test_normalize_is_pure_and_reusable():
    assert normalize_product_ids("c1", ["3", "1", "3", "2"]) == ["3", "1", "2"]
