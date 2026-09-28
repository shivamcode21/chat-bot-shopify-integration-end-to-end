"""Force LLM attribute re-extraction for named products.

LLM extraction is gated on a product's *identity* — title, product_type,
description (see ``NormalizedProduct.compute_semantic_content_hash``) — so that
merchandising churn does not re-derive attributes that cannot have changed. The
accepted cost is that an attribute inferred only from tags or metafields (a
``color_family`` for a product whose colour appears nowhere in the title or
description, say) can go stale until an identity field changes.

This module is that escape hatch. It lives outside ``agent_controller`` so the
batching and guardrails stay unit-testable without importing the FastAPI app,
which opens database connections at import time.
"""

import asyncio
import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Bounded so one request cannot fan out into an unbounded LLM bill. Callers
# wanting a whole catalogue should use the full ingestion path instead.
MAX_PRODUCTS = 200

# Kept low on purpose: a burst of parallel single-product ingests hammers the
# Shopify GraphQL enrich, and enrich throttling is itself what destabilised the
# webhook hash and caused redundant extractions in the first place.
CONCURRENCY = 4


class ReExtractRequestError(ValueError):
    """Raised when a re-extract request is malformed or too large."""


def normalize_product_ids(client_id: Optional[str], product_ids: Any) -> List[str]:
    """Validate a re-extract request and return de-duplicated string IDs.

    Raises:
        ReExtractRequestError: with a caller-facing message.
    """
    if not client_id:
        raise ReExtractRequestError("client_id is required")
    if not isinstance(product_ids, Sequence) or isinstance(product_ids, (str, bytes)):
        raise ReExtractRequestError("product_ids must be a non-empty list")
    if not product_ids:
        raise ReExtractRequestError("product_ids must be a non-empty list")
    if len(product_ids) > MAX_PRODUCTS:
        raise ReExtractRequestError(
            f"product_ids exceeds the {MAX_PRODUCTS} per-call limit "
            f"(got {len(product_ids)}). Split the batch, or use "
            f"/api/v1/products/ingest to rebuild the whole catalog."
        )
    # dict.fromkeys de-duplicates while preserving order, so a repeated ID is
    # not paid for twice.
    return list(dict.fromkeys(str(pid) for pid in product_ids))


async def arun_product_re_extraction(
    client_id: str,
    product_ids: Any,
    *,
    trace_id: Optional[str] = None,
    orchestrator: Optional[Any] = None,
) -> Dict[str, Any]:
    """Re-run LLM attribute extraction for each product, bypassing the hash gate.

    ``ingest_single_product`` always extracts, so no cache invalidation is
    needed. One product failing does not abort the batch — a partial result is
    more useful than none when Shopify throttles midway.

    Args:
        client_id: Client UUID.
        product_ids: Shopify numeric product IDs (max ``MAX_PRODUCTS``).
        trace_id: Correlation id for the batch (AGENTS.md §5); generated when
            not supplied so every log line here is traceable.
        orchestrator: Injectable for tests; defaults to a fresh
            ``ProductIngestionOrchestrator``.

    Returns:
        Summary dict with ``success``, ``requested``, ``succeeded``, ``failed``
        and per-product ``results``.
    """
    unique_ids = normalize_product_ids(client_id, product_ids)

    if not trace_id:
        from fashion_bot.trace_context import generate_trace_id
        trace_id = generate_trace_id()

    if orchestrator is None:
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator
        orchestrator = ProductIngestionOrchestrator()

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def _re_extract_one(product_id: str) -> Dict[str, Any]:
        async with semaphore:
            try:
                return await orchestrator.ingest_single_product(
                    client_id=client_id, product_id=product_id,
                )
            except Exception as exc:
                logger.error(
                    f"[RE_EXTRACT] [{trace_id}] Failed for "
                    f"client={client_id} product={product_id}: {exc}"
                )
                return {"success": False, "product_id": product_id, "error": str(exc)}

    results = await asyncio.gather(*(_re_extract_one(pid) for pid in unique_ids))

    failed = [r for r in results if not (isinstance(r, dict) and r.get("success"))]
    succeeded_count = len(results) - len(failed)
    logger.info(
        f"[RE_EXTRACT] [{trace_id}] client={client_id} "
        f"requested={len(unique_ids)} succeeded={succeeded_count} "
        f"failed={len(failed)}"
    )
    return {
        "success": not failed,
        "client_id": client_id,
        "trace_id": trace_id,
        "requested": len(unique_ids),
        "succeeded": succeeded_count,
        "failed": len(failed),
        "results": list(results),
    }
