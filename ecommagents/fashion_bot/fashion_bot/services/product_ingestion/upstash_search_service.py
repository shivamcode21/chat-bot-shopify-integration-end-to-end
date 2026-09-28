"""
Upstash Search Service - Handles all Upstash Search operations.

Uses Upstash Search for text-based product discovery with:
- Semantic + full-text hybrid search
- Structured content filtering
- AI-powered input enrichment and reranking
"""

import asyncio
import os
import logging
import json
from typing import List, Optional, Dict, Any

from fashion_bot.services.product_ingestion.product_content_summarizer import (
    ProductContentSummarizer,
)

logger = logging.getLogger(__name__)

# Upstash Search caps a prefix `index.fetch(prefix=...)` at 100 documents and
# offers NO pagination on it. The paginated primitive is
# `index.range(cursor=..., limit=..., prefix=...)`, which returns a
# `next_cursor` ("" when exhausted). Any consumer that needs ALL of a client's
# docs (delta-sync hash comparison, doc count, full export) must page via range
# — otherwise it silently sees only the first 100 docs and mis-classifies the
# rest (this caused the weekly delta sync to re-ingest entire catalogs).
_RANGE_PAGE_LIMIT = 100  # server max per request
_RANGE_MAX_PAGES = 2000  # defensive bound: 200k docs/client, far beyond any real catalog


class UpstashSearchService:
    """
    Handles all Upstash Search operations for product documents.

    Each client gets its own index (multi-tenancy via index name).
    Documents have 'content' (searchable + filterable) and 'metadata' (display-only).
    """

    def __init__(self):
        self._client = None
        self._url = os.getenv("UPSTASH_SEARCH_REST_URL")
        self._token = os.getenv("UPSTASH_SEARCH_REST_TOKEN")
        self._summarizer = ProductContentSummarizer()

        if not self._url or not self._token:
            logger.warning("UPSTASH_SEARCH_REST_URL or UPSTASH_SEARCH_REST_TOKEN not set")

    @property
    def client(self):
        if self._client is None:
            if not self._url or not self._token:
                raise ValueError(
                    "UPSTASH_SEARCH_REST_URL and UPSTASH_SEARCH_REST_TOKEN must be set"
                )
            try:
                from upstash_search import Search
                self._client = Search(url=self._url, token=self._token)
                logger.info("Upstash Search client initialized")
            except ImportError:
                raise ImportError(
                    "upstash-search not installed. Install with: pip install upstash-search"
                )
        return self._client

    def _get_index(self, client_id: str):
        return self.client.index(f"client_{client_id}")

    def _iter_doc_pages(self, client_id: str, page_limit: int = _RANGE_PAGE_LIMIT):
        """Yield successive pages (lists of documents) for a client's WHOLE index.

        Pages via ``index.range`` + ``next_cursor`` because ``index.fetch(prefix=)``
        is hard-capped at 100 docs with no pagination. Yielding per page keeps
        peak memory at one page (≤ ``page_limit`` docs), not the whole index — so
        callers can stream-extract just the fields they need.
        """
        index = self._get_index(client_id)
        prefix = f"{client_id}_"
        cursor = ""
        pages = 0
        while True:
            res = index.range(cursor=cursor, limit=page_limit, prefix=prefix)
            docs = getattr(res, "documents", None) or []
            if docs:
                yield docs
            cursor = getattr(res, "next_cursor", "") or ""
            pages += 1
            if not cursor:
                break
            if pages >= _RANGE_MAX_PAGES:
                logger.warning(
                    "[UPSTASH_SEARCH] _iter_doc_pages hit safety cap of %d pages "
                    "(client_id=%s); stopping enumeration. Raise _RANGE_MAX_PAGES "
                    "if this is a legitimately huge catalog.",
                    _RANGE_MAX_PAGES, client_id,
                )
                break

    async def aupsert_documents(
        self,
        documents: List[Dict[str, Any]],
        client_id: str,
        batch_size: int = 50,
    ) -> Dict[str, Any]:
        """
        Upsert documents to Upstash Search index for a client.

        Args:
            documents: List of dicts with 'id', 'content', 'metadata' keys
            client_id: Client ID (determines index name)
            batch_size: Documents per batch

        Returns:
            Dict with success_count and failed details

        Note: upstash-search has no native async client, so the sync
        ``index.upsert`` call is wrapped in ``asyncio.to_thread`` to keep
        the event loop unblocked (AGENTS.md §1).
        """
        documents = await self._summarizer.acompress_documents(documents)

        index = self._get_index(client_id)
        success_count = 0
        failed = []

        for i in range(0, len(documents), batch_size):
            batch = documents[i:i + batch_size]
            batch_num = i // batch_size + 1
            try:
                await asyncio.to_thread(index.upsert, documents=batch)
                success_count += len(batch)
                logger.info(
                    f"Upserted batch {batch_num}: "
                    f"{len(batch)} docs to index client_{client_id}"
                )
            except Exception as batch_error:
                logger.warning(
                    f"Batch {batch_num} upsert failed ({batch_error}); "
                    f"retrying {len(batch)} documents individually"
                )
                batch_success = 0
                for doc in batch:
                    try:
                        await asyncio.to_thread(index.upsert, documents=[doc])
                        batch_success += 1
                    except Exception as doc_error:
                        product_id = doc.get("id", "unknown")
                        logger.error(
                            f"Failed to upsert doc {product_id}: {doc_error}"
                        )
                        failed.append({
                            "product_id": product_id,
                            "error": str(doc_error),
                        })
                success_count += batch_success
                logger.info(
                    f"Batch {batch_num} per-doc retry: "
                    f"{batch_success}/{len(batch)} succeeded"
                )

        return {"success_count": success_count, "failed": failed}

    def search(
        self,
        query: str,
        client_id: str,
        filter_str: Optional[str] = None,
        limit: int = 50,
        semantic_weight: float = 0.75,
        reranking: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Search products in a client's Upstash Search index.

        Args:
            query: Natural language search query
            client_id: Client ID
            filter_str: Upstash Search filter string (SQL-like)
            limit: Max results
            semantic_weight: Balance between semantic (1.0) and keyword (0.0)
            reranking: Enable AI reranking

        Returns:
            List of search result dicts
        """
        index = self._get_index(client_id)

        kwargs: Dict[str, Any] = {
            "query": query,
            "limit": limit,
            "reranking": reranking,
            "semantic_weight": semantic_weight,
        }
        if filter_str:
            kwargs["filter"] = filter_str

        try:
            import time
            _t0 = time.monotonic()
            results = index.search(**kwargs)
            _elapsed = int((time.monotonic() - _t0) * 1000)
            out = [
                {
                    "id": r.id,
                    "content": r.content if hasattr(r, "content") else {},
                    "metadata": r.metadata if hasattr(r, "metadata") else {},
                    "score": r.score if hasattr(r, "score") else 0.0,
                }
                for r in results
            ]
            _handles = [
                (o.get("content") or {}).get("handle")
                or (o.get("metadata") or {}).get("handle")
                or "?"
                for o in out
            ]
            logger.info(
                f"[UPSTASH_SEARCH] search elapsed_ms={_elapsed} results={len(out)} "
                f"client={client_id} query={query!r} filter={filter_str!r} handles={_handles}"
            )
            return out
        except Exception as e:
            logger.error(
                f"[UPSTASH_SEARCH] search failed client={client_id} "
                f"filter={filter_str!r}: {e}"
            )
            return []

    async def asearch(
        self,
        query: str,
        client_id: str,
        filter_str: Optional[str] = None,
        limit: int = 50,
        semantic_weight: float = 0.75,
        reranking: bool = True,
    ) -> List[Dict[str, Any]]:
        """Async search — wraps sync client in to_thread (upstash-search has no native async yet)."""
        return await asyncio.to_thread(
            self.search,
            query=query,
            client_id=client_id,
            filter_str=filter_str,
            limit=limit,
            semantic_weight=semantic_weight,
            reranking=reranking,
        )

    def delete_documents(
        self,
        client_id: str,
        ids: Optional[List[str]] = None,
        prefix: Optional[str] = None,
        filter_str: Optional[str] = None,
    ) -> int:
        """
        Delete documents from a client's index.

        Args:
            client_id: Client ID
            ids: Specific document IDs to delete
            prefix: ID prefix for bulk deletion
            filter_str: Filter-based deletion

        Returns:
            Number of deleted documents
        """
        index = self._get_index(client_id)
        try:
            kwargs = {}
            if ids:
                kwargs["ids"] = ids
            elif prefix:
                kwargs["prefix"] = prefix
            elif filter_str:
                kwargs["filter"] = filter_str
            else:
                return 0

            result = index.delete(**kwargs)
            deleted = result if isinstance(result, int) else 0
            logger.info(f"Deleted {deleted} documents from client_{client_id}")
            return deleted
        except Exception as e:
            logger.error(f"Delete failed for client {client_id}: {e}")
            return 0

    def delete_all_for_client(self, client_id: str) -> int:
        """Delete all documents for a client using prefix."""
        return self.delete_documents(client_id, prefix=f"{client_id}_")

    def get_existing_product_hashes(self, client_id: str) -> Dict[str, str]:
        """
        Fetch all existing product_id -> content_hash_no_inventory mappings.

        Paginates the ENTIRE client index via cursor (``range``) — a plain
        ``index.fetch(prefix=)`` is capped at 100 docs, which made delta sync
        treat every product beyond the first 100 as a new ADD and re-ingest the
        whole catalog each run. Extracts the inventory-excluding hash from each
        document's metadata, matching the webhook handler's hash strategy so
        delta sync only flags true content changes, not inventory drift.

        Returns:
            Dictionary mapping product_id to content_hash_no_inventory
        """
        import time
        _t0 = time.monotonic()
        try:
            product_hashes: Dict[str, str] = {}
            pages = 0
            for page in self._iter_doc_pages(client_id):
                pages += 1
                for doc in page:
                    meta = doc.metadata if hasattr(doc, "metadata") and doc.metadata else {}
                    product_id = meta.get("product_id", "")
                    content_hash = meta.get("content_hash_no_inventory", "") or meta.get("content_hash", "")
                    if product_id:
                        product_hashes[product_id] = content_hash

            _elapsed = int((time.monotonic() - _t0) * 1000)
            logger.info(
                f"[UPSTASH_SEARCH] get_existing_product_hashes "
                f"elapsed_ms={_elapsed} fetched={len(product_hashes)} pages={pages} client_id={client_id}"
            )
            return product_hashes
        except Exception as e:
            logger.error(f"[UPSTASH_SEARCH] Error fetching product hashes: {e}")
            return {}

    def get_document_count(self, client_id: str) -> int:
        """Return the document count for a client (paginated → exact).

        A plain prefix fetch caps at 100; this pages the whole index so the
        count is correct for catalogs larger than 100.
        """
        try:
            count = 0
            for page in self._iter_doc_pages(client_id):
                count += len(page)
            return count
        except Exception as e:
            logger.error(f"[UPSTASH_SEARCH] Error counting documents: {e}")
            return 0

    def fetch_documents(
        self,
        client_id: str,
        product_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch full documents from the index.

        Args:
            client_id: Client ID
            product_ids: Optional list of Shopify product IDs.  If provided,
                only those docs are returned.  Otherwise all docs for the
                client are fetched.

        Returns:
            List of dicts with ``id``, ``content``, ``metadata`` keys.
        """
        try:
            results = []

            # Targeted ids → direct O(1) fetch by composite id. Bounded by the
            # request, and not subject to the 100-doc prefix cap.
            if product_ids:
                index = self._get_index(client_id)
                doc_ids = [f"{client_id}_{pid}" for pid in product_ids]
                for doc in index.fetch(ids=doc_ids):
                    if doc is None:  # id not present in the index
                        continue
                    meta = doc.metadata if hasattr(doc, "metadata") and doc.metadata else {}
                    results.append({
                        "id": doc.id if hasattr(doc, "id") else "",
                        "content": doc.content if hasattr(doc, "content") else {},
                        "metadata": meta,
                    })
                return results

            # No ids → enumerate the WHOLE index (paginated, not capped at 100).
            for page in self._iter_doc_pages(client_id):
                for doc in page:
                    meta = doc.metadata if hasattr(doc, "metadata") and doc.metadata else {}
                    results.append({
                        "id": doc.id if hasattr(doc, "id") else "",
                        "content": doc.content if hasattr(doc, "content") else {},
                        "metadata": meta,
                    })
            return results
        except Exception as e:
            logger.error(f"[UPSTASH_SEARCH] fetch_documents failed for {client_id}: {e}")
            return []

    def fetch_document_by_id(
        self,
        client_id: str,
        product_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Fetch a single document by its composite ID (``{client_id}_{product_id}``).

        Uses ``index.fetch(ids=[...])`` which is an O(1) lookup —
        unlike ``fetch_documents`` which downloads every document in the
        index via a prefix scan.
        """
        doc_id = f"{client_id}_{product_id}"
        try:
            index = self._get_index(client_id)
            docs = index.fetch(ids=[doc_id])
            if not docs:
                return None
            doc = docs[0]
            if doc is None:
                return None
            meta = doc.metadata if hasattr(doc, "metadata") and doc.metadata else {}
            return {
                "id": doc.id if hasattr(doc, "id") else "",
                "content": doc.content if hasattr(doc, "content") else {},
                "metadata": meta,
            }
        except Exception as e:
            logger.error(f"[UPSTASH_SEARCH] fetch_document_by_id failed for {doc_id}: {e}")
            return None

    async def afetch_documents_by_ids(
        self,
        client_id: str,
        product_ids: List[str],
        batch_size: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetch specific documents by ``product_id`` via direct id lookups.

        Unlike ``fetch_documents`` (which prefix-scans the entire client index),
        this fetches ONLY the requested composite ids
        (``{client_id}_{product_id}``) in batches — keeping Upstash reads
        minimal when only a handful of docs are needed (e.g. the monthly
        bestseller refresh). ``upstash-search`` has no native async client, so
        the sync ``index.fetch`` is wrapped in ``asyncio.to_thread`` (AGENTS.md §1).

        Returns a list of dicts with ``id``, ``content``, ``metadata`` keys.
        Ids absent from the index are omitted from the result — an id missing
        from the return value therefore means "not indexed", never "the lookup
        failed". A transport/API error RAISES rather than yielding a short list,
        so callers can never mistake an unreadable index for an empty one.
        """
        if not product_ids:
            return []

        index = self._get_index(client_id)
        doc_ids = [f"{client_id}_{pid}" for pid in product_ids]
        results: List[Dict[str, Any]] = []

        for i in range(0, len(doc_ids), batch_size):
            chunk = doc_ids[i:i + batch_size]
            try:
                docs = await asyncio.to_thread(index.fetch, ids=chunk)
            except Exception as e:
                logger.error(
                    f"[UPSTASH_SEARCH] afetch_documents_by_ids batch failed "
                    f"client={client_id}: {e}"
                )
                raise
            for doc in docs or []:
                if doc is None:
                    continue
                meta = doc.metadata if hasattr(doc, "metadata") and doc.metadata else {}
                results.append({
                    "id": doc.id if hasattr(doc, "id") else "",
                    "content": doc.content if hasattr(doc, "content") else {},
                    "metadata": meta,
                })
        return results

    def fetch_documents_by_content_flag(
        self,
        client_id: str,
        field: str,
    ) -> List[Dict[str, Any]]:
        """Return EVERY document for *client_id* whose ``content[field]`` is True.

        Upstash Search offers no filtered scan, so neither obvious primitive is
        usable on its own:

        * ``index.search(filter=...)`` is a relevance-ranked **top-K** query with
          no offset or cursor (``limit`` maps straight to ``topK``). It can never
          return more than one page, and it orders by similarity to the query
          text — meaningless when the goal is "every doc where the flag is set".
        * ``index.range(...)`` paginates correctly by id but takes no filter.

        The only complete enumeration is therefore a cursor scan over the
        client's id prefix with the flag applied locally. Pages are streamed via
        ``_iter_doc_pages`` and only matching docs are retained, so peak memory
        is one page plus the (small) flagged set rather than the whole catalog.

        Raises on transport/API failure — an empty list always means "no
        documents carry this flag", never "the index could not be read".
        """
        matched: List[Dict[str, Any]] = []
        for page in self._iter_doc_pages(client_id):
            for doc in page:
                content = getattr(doc, "content", None) or {}
                if content.get(field) is not True:
                    continue
                meta = doc.metadata if getattr(doc, "metadata", None) else {}
                matched.append({
                    "id": getattr(doc, "id", "") or "",
                    "content": content,
                    "metadata": meta,
                })
        logger.info(
            f"[UPSTASH_SEARCH] content-flag scan client={client_id} "
            f"field={field!r} matched={len(matched)}"
        )
        return matched

    async def afetch_documents_by_content_flag(
        self,
        client_id: str,
        field: str,
    ) -> List[Dict[str, Any]]:
        """Async wrapper for :meth:`fetch_documents_by_content_flag`.

        ``upstash-search`` has no native async client, so the blocking cursor
        scan runs in a worker thread (AGENTS.md §1).
        """
        return await asyncio.to_thread(
            self.fetch_documents_by_content_flag, client_id, field
        )

    async def abulk_update_fields(
        self,
        client_id: str,
        updates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Update specific fields across multiple documents.

        Each entry in *updates* is::

            {
                "product_id": "gid://shopify/Product/123",
                "content_fields": {"bestseller": True},   # optional
                "metadata_fields": {"some_key": "val"},    # optional
            }

        Internally this fetches the existing doc, patches the requested
        fields, and re-upserts.

        Returns:
            Dict with ``updated_count`` and ``failed`` list.
        """
        pid_to_update = {u["product_id"]: u for u in updates if "product_id" in u}
        if not pid_to_update:
            return {"updated_count": 0, "failed": []}

        existing_docs = await asyncio.to_thread(
            self.fetch_documents, client_id, product_ids=list(pid_to_update.keys())
        )

        doc_map: Dict[str, Dict[str, Any]] = {}
        for doc in existing_docs:
            pid = (doc.get("metadata") or {}).get("product_id", "")
            if pid:
                doc_map[pid] = doc

        docs_to_upsert = []
        failed = []

        for pid, upd in pid_to_update.items():
            doc = doc_map.get(pid)
            if not doc:
                failed.append({"product_id": pid, "error": "Document not found in index"})
                continue

            content = dict(doc.get("content") or {})
            metadata = dict(doc.get("metadata") or {})

            for k, v in (upd.get("content_fields") or {}).items():
                content[k] = v
            for k, v in (upd.get("metadata_fields") or {}).items():
                metadata[k] = v

            docs_to_upsert.append({
                "id": doc["id"],
                "content": content,
                "metadata": metadata,
            })

        result = {"updated_count": 0, "failed": failed}

        if docs_to_upsert:
            upsert_result = await self.aupsert_documents(docs_to_upsert, client_id=client_id)
            result["updated_count"] = upsert_result.get("success_count", 0)
            result["failed"].extend(upsert_result.get("failed", []))

        logger.info(
            f"[UPSTASH_SEARCH] abulk_update_fields client={client_id} "
            f"updated={result['updated_count']} failed={len(result['failed'])}"
        )
        return result

    def health_check(self) -> bool:
        try:
            _ = self.client
            return True
        except Exception:
            return False


# Module-level singleton
_search_service_instance: Optional[UpstashSearchService] = None


def get_upstash_search_service() -> UpstashSearchService:
    global _search_service_instance
    if _search_service_instance is None:
        _search_service_instance = UpstashSearchService()
    return _search_service_instance
