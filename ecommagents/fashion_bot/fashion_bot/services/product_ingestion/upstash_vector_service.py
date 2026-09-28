"""
Upstash Vector Service - Handles all Upstash VectorDB operations.

Uses Upstash's built-in embedding with BAAI/bge-m3 model.
"""

import os
import time
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


class UpstashVectorService:
    """
    Handles all Upstash VectorDB operations.

    Uses Upstash's built-in embedding (data field) with BAAI/bge-m3 model.
    """

    def __init__(self):
        """Initialize Upstash Vector Index connection."""
        self._index = None
        self._async_index = None
        self._url = os.getenv("UPSTASH_VECTOR_REST_URL")
        self._token = os.getenv("UPSTASH_VECTOR_REST_TOKEN")

        if not self._url or not self._token:
            logger.warning("⚠️ UPSTASH_VECTOR_REST_URL or UPSTASH_VECTOR_REST_TOKEN not set")

    @property
    def index(self):
        """Lazy initialization of Upstash Vector Index (sync)."""
        if self._index is None:
            if not self._url or not self._token:
                raise ValueError(
                    "UPSTASH_VECTOR_REST_URL and UPSTASH_VECTOR_REST_TOKEN must be set"
                )

            try:
                from upstash_vector import Index
                self._index = Index(
                    url=self._url,
                    token=self._token
                )
                logger.info("✅ Upstash Vector Index initialized successfully")
            except ImportError:
                raise ImportError(
                    "upstash-vector package not installed. "
                    "Install with: pip install upstash-vector"
                )

        return self._index

    @property
    def async_index(self):
        """Lazy initialization of Upstash AsyncIndex (native async)."""
        if self._async_index is None:
            if not self._url or not self._token:
                raise ValueError(
                    "UPSTASH_VECTOR_REST_URL and UPSTASH_VECTOR_REST_TOKEN must be set"
                )
            from upstash_vector import AsyncIndex
            self._async_index = AsyncIndex(
                url=self._url,
                token=self._token
            )
            logger.info("✅ Upstash AsyncIndex initialized successfully")
        return self._async_index
    
    def upsert_batch(
        self,
        vectors: List["VectorData"],
        batch_size: int = 100,
        client_id: str = None
    ) -> Dict[str, Any]:
        """
        Upserts vectors in batches.
        
        Uses Upstash's built-in embedding (data field).
        
        Args:
            vectors: List of VectorData objects to upsert
            batch_size: Number of vectors per batch (default: 100)
            client_id: Client ID for namespace isolation (required)
            
        Returns:
            Dictionary with success_count and failed details
        """
        from fashion_bot.services.product_ingestion.models import VectorData
        
        success_count = 0
        failed = []
        batch_count = 0

        # Build namespace for client isolation
        namespace = f"client_{client_id}" if client_id else None

        # Process in batches
        _t0 = time.monotonic()
        for i in range(0, len(vectors), batch_size):
            batch = vectors[i:i + batch_size]
            batch_count += 1

            try:
                # Prepare vectors for Upstash
                # Using 'data' field for built-in embedding
                upsert_data = [
                    {
                        "id": v.id,
                        "data": v.searchable_text,  # Upstash embeds this
                        "metadata": v.metadata
                    }
                    for v in batch
                ]

                # Upsert with namespace if provided
                if namespace:
                    self.index.upsert(vectors=upsert_data, namespace=namespace)
                else:
                    self.index.upsert(vectors=upsert_data)
                success_count += len(batch)

            except Exception as e:
                error_msg = str(e)
                logger.error(f"❌ Failed to upsert batch {i//batch_size + 1}: {error_msg}")

                for v in batch:
                    failed.append({
                        "product_id": v.metadata.get("product_id", v.id),
                        "error": error_msg
                    })

        logger.info(f"[UPSTASH] upsert elapsed_ms={int((time.monotonic() - _t0) * 1000)} vectors={success_count} batches={batch_count} namespace={namespace}")

        return {
            "success_count": success_count,
            "failed": failed
        }
    
    def delete_by_client(self, client_id: str) -> int:
        """
        Deletes all vectors for a specific client.
        
        Uses paginated queries to find all vectors, then deletes by ID.
        Upstash Vector delete() only accepts a list of IDs.
        
        Args:
            client_id: The client ID whose vectors should be deleted
            
        Returns:
            Number of deleted vectors
        """
        try:
            total_deleted = 0
            batch_count = 0
            batch_size = 100  # Query in smaller batches to avoid limits

            # Build namespace for client isolation
            namespace = f"client_{client_id}"

            # Keep querying and deleting until no more vectors found
            _t0 = time.monotonic()
            while True:
                # Query for vectors belonging to this client
                try:
                    result = self.index.query(
                        data="products",  # Generic query for embedding
                        top_k=batch_size,
                        filter=f"client_id = '{client_id}'",
                        include_metadata=False,
                        include_vectors=False,
                        namespace=namespace
                    )
                except Exception as query_err:
                    logger.warning(f"⚠️ Query with filter failed: {query_err}, trying without filter")
                    # If filter not supported, we can't efficiently delete by client
                    break

                if not result or len(result) == 0:
                    break

                # Extract IDs from results
                ids_to_delete = [r.id for r in result]

                if not ids_to_delete:
                    break

                # Delete by IDs (with namespace)
                try:
                    self.index.delete(ids=ids_to_delete, namespace=namespace)
                    total_deleted += len(ids_to_delete)
                    batch_count += 1
                except Exception as del_err:
                    logger.error(f"❌ Error deleting batch: {del_err}")
                    break

                # If we got less than batch_size, we're done
                if len(ids_to_delete) < batch_size:
                    break

            logger.info(f"[UPSTASH] delete_by_client elapsed_ms={int((time.monotonic() - _t0) * 1000)} deleted={total_deleted} batches={batch_count} client_id={client_id}")
            return total_deleted
            
        except Exception as e:
            logger.error(f"❌ Error deleting vectors for client {client_id}: {e}")
            return 0
    
    def get_client_vector_count(self, client_id: str) -> int:
        """
        Get the count of vectors for a specific client.
        
        Note: This is an approximation using query. For exact count,
        you would need to iterate through all vectors.
        
        Args:
            client_id: The client ID to count vectors for
            
        Returns:
            Number of vectors for this client (up to 1000)
        """
        try:
            # Build namespace for client isolation
            namespace = f"client_{client_id}"

            # Query with a generic term and filter by client_id
            # Use max allowed limit of 1000
            _t0 = time.monotonic()
            result = self.index.query(
                data="products",
                top_k=1000,
                filter=f"client_id = '{client_id}'",
                include_metadata=False,
                include_vectors=False,
                namespace=namespace
            )
            logger.info(f"[UPSTASH] get_client_vector_count elapsed_ms={int((time.monotonic() - _t0) * 1000)} client_id={client_id}")

            if result and hasattr(result, '__len__'):
                return len(result)

            return 0

        except Exception as e:
            logger.error(f"❌ Error counting vectors for client {client_id}: {e}")
            return 0
    
    def get_index_info(self) -> Dict[str, Any]:
        """
        Get information about the Upstash Vector index.
        
        Returns:
            Dictionary with index statistics
        """
        try:
            _t0 = time.monotonic()
            info = self.index.info()
            logger.info(f"[UPSTASH] get_index_info elapsed_ms={int((time.monotonic() - _t0) * 1000)}")
            return {
                "dimension": getattr(info, 'dimension', None),
                "similarity_function": getattr(info, 'similarity_function', None),
                "vector_count": getattr(info, 'vector_count', 0),
                "pending_vector_count": getattr(info, 'pending_vector_count', 0),
            }
        except Exception as e:
            logger.error(f"❌ Error getting index info: {e}")
            return {"error": str(e)}
    
    def search(
        self,
        query: str,
        client_id: str,
        top_k: int = 10,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        product_type: Optional[str] = None,
        product_line: Optional[str] = None,
        segment: Optional[str] = None,
        in_stock_only: bool = False
    ) -> List[Dict[str, Any]]:
        """
        Search for products using semantic similarity with metadata filtering.
        
        Uses product_line for exact-match filtering to ensure precise results
        so that closely named variants are not confused.
        Color and material are handled by semantic similarity (not filtered).
        
        Args:
            query: Search query text
            client_id: Client ID to filter results
            top_k: Number of results to return
            min_price: Optional minimum price filter
            max_price: Optional maximum price filter
            product_type: Optional product type filter
            product_line: Optional product line for exact-match filter (normalized lowercase).
            segment: Optional segment filter (men, women, kids)
            in_stock_only: If True, only return in-stock products
            
        Returns:
            List of search results with metadata and scores
        """
        try:
            # Build filter string for client_id (required)
            filter_parts = [f"client_id = '{client_id}'"]
            
            # Add optional price filters
            if min_price is not None:
                filter_parts.append(f"price_min >= {min_price}")
            if max_price is not None:
                filter_parts.append(f"price_max <= {max_price}")
            
            # Add product type filter
            if product_type:
                filter_parts.append(f"product_type = '{product_type}'")
            
            # Add product_line exact-match filter (critical for device model precision)
            if product_line:
                filter_parts.append(f"product_line_normalized = '{product_line}'")
            
            # Add segment filter
            if segment:
                filter_parts.append(f"segment = '{segment}'")
            
            # Add in-stock filter
            if in_stock_only:
                filter_parts.append("in_stock = true")
            
            # Combine filters with AND
            filter_str = " AND ".join(filter_parts)
            
            # Build namespace for client isolation
            namespace = f"client_{client_id}"
            
            # Perform semantic search with namespace
            _t0 = time.monotonic()
            results = self.index.query(
                data=query,
                top_k=top_k,
                filter=filter_str,
                include_metadata=True,
                include_vectors=False,
                namespace=namespace
            )
            logger.info(f"[UPSTASH] search elapsed_ms={int((time.monotonic() - _t0) * 1000)} query='{query}' top_k={top_k} namespace={namespace}")
            
            # Convert results to list of dicts
            search_results = []
            for res in results:
                meta = res.metadata or {}
                search_results.append({
                    "id": res.id,
                    "score": res.score,
                    "product_id": meta.get("product_id", ""),
                    "title": meta.get("title", ""),
                    "product_type": meta.get("product_type", ""),
                    "vendor": meta.get("vendor", ""),
                    "price_min": meta.get("price_min", 0),
                    "price_max": meta.get("price_max", 0),
                    "compare_at_price_min": meta.get("compare_at_price_min"),
                    "compare_at_price_max": meta.get("compare_at_price_max"),
                    "discount_pct": meta.get("discount_pct", 0),
                    "image_url": meta.get("image_url", ""),
                    "all_images": meta.get("all_images", []),
                    "product_url": meta.get("product_url", ""),
                    "in_stock": meta.get("in_stock", False),
                    "total_inventory": meta.get("total_inventory", 0),
                    "colors": meta.get("colors", []),
                    "sizes": meta.get("sizes", []),
                    "tags": meta.get("tags", []),
                    "description": meta.get("description"),
                    # Fabric/care fields from metafields
                    "fabric": meta.get("fabric"),
                    "care_instructions": meta.get("care_instructions"),
                    "fit_type": meta.get("fit_type"),
                    "size_chart": meta.get("size_chart"),
                    # SEO fields
                    "seo_title": meta.get("seo_title"),
                    "seo_description": meta.get("seo_description"),
                    # Variants
                    "variants": meta.get("variants", []),
                    # LLM-extracted structured attributes
                    "base_product_name": meta.get("base_product_name", ""),
                    "product_line": meta.get("product_line", ""),
                    "product_line_normalized": meta.get("product_line_normalized", ""),
                    "extracted_color": meta.get("extracted_color", ""),
                    "material": meta.get("material", ""),
                    "bestseller": meta.get("bestseller", False),
                })
            
            return search_results

        except Exception as e:
            logger.error(f"❌ Search error: {e}")
            raise

    async def asearch(
        self,
        query: str,
        client_id: str,
        top_k: int = 10,
        min_price: Optional[float] = None,
        max_price: Optional[float] = None,
        product_type: Optional[str] = None,
        product_line: Optional[str] = None,
        segment: Optional[str] = None,
        in_stock_only: bool = False
    ) -> List[Dict[str, Any]]:
        """Native async search using AsyncIndex. Same API as search()."""
        try:
            filter_parts = [f"client_id = '{client_id}'"]
            if min_price is not None:
                filter_parts.append(f"price_min >= {min_price}")
            if max_price is not None:
                filter_parts.append(f"price_max <= {max_price}")
            if product_type:
                filter_parts.append(f"product_type = '{product_type}'")
            if product_line:
                filter_parts.append(f"product_line_normalized = '{product_line}'")
            if segment:
                filter_parts.append(f"segment = '{segment}'")
            if in_stock_only:
                filter_parts.append("in_stock = true")

            filter_str = " AND ".join(filter_parts)
            namespace = f"client_{client_id}"

            _t0 = time.monotonic()
            results = await self.async_index.query(
                data=query,
                top_k=top_k,
                filter=filter_str,
                include_metadata=True,
                include_vectors=False,
                namespace=namespace
            )
            logger.info(f"[UPSTASH] asearch elapsed_ms={int((time.monotonic() - _t0) * 1000)} query='{query}' top_k={top_k} namespace={namespace}")

            search_results = []
            for res in results:
                meta = res.metadata or {}
                search_results.append({
                    "id": res.id,
                    "score": res.score,
                    "product_id": meta.get("product_id", ""),
                    "title": meta.get("title", ""),
                    "product_type": meta.get("product_type", ""),
                    "vendor": meta.get("vendor", ""),
                    "price_min": meta.get("price_min", 0),
                    "price_max": meta.get("price_max", 0),
                    "compare_at_price_min": meta.get("compare_at_price_min"),
                    "compare_at_price_max": meta.get("compare_at_price_max"),
                    "image_url": meta.get("image_url", ""),
                    "all_images": meta.get("all_images", []),
                    "product_url": meta.get("product_url", ""),
                    "in_stock": meta.get("in_stock", False),
                    "total_inventory": meta.get("total_inventory", 0),
                    "colors": meta.get("colors", []),
                    "sizes": meta.get("sizes", []),
                    "tags": meta.get("tags", []),
                    "description": meta.get("description"),
                    "fabric": meta.get("fabric"),
                    "care_instructions": meta.get("care_instructions"),
                    "fit_type": meta.get("fit_type"),
                    "size_chart": meta.get("size_chart"),
                    "seo_title": meta.get("seo_title"),
                    "seo_description": meta.get("seo_description"),
                    "variants": meta.get("variants", []),
                    "base_product_name": meta.get("base_product_name", ""),
                    "product_line": meta.get("product_line", ""),
                    "product_line_normalized": meta.get("product_line_normalized", ""),
                    "extracted_color": meta.get("extracted_color", ""),
                    "material": meta.get("material", ""),
                })

            return search_results

        except Exception as e:
            logger.error(f"❌ Async search error: {e}")
            raise

    def get_existing_product_hashes(
        self,
        client_id: str,
        batch_size: int = 100
    ) -> Dict[str, str]:
        """
        Fetch all existing product_id -> content_hash_no_inventory mappings.

        Uses pagination to fetch all vectors for the client.

        Args:
            client_id: Client ID to fetch hashes for
            batch_size: Number of vectors to fetch per query (max 1000)

        Returns:
            Dictionary mapping product_id to content_hash_no_inventory
        """
        try:
            product_hashes = {}
            total_fetched = 0
            
            # Build namespace for client isolation
            namespace = f"client_{client_id}"
            
            # Upstash doesn't support cursor-based pagination for queries,
            # so we query with max limit and process
            # For large catalogs (>1000 products), this is an approximation
            _t0 = time.monotonic()
            result = self.index.query(
                data="products",  # Generic query
                top_k=min(batch_size, 1000),  # Upstash max is 1000
                filter=f"client_id = '{client_id}'",
                include_metadata=True,
                include_vectors=False,
                namespace=namespace
            )

            if result:
                for r in result:
                    meta = r.metadata or {}
                    product_id = meta.get("product_id", "")
                    content_hash = meta.get("content_hash_no_inventory", "") or meta.get("content_hash", "")

                    if product_id:
                        product_hashes[product_id] = content_hash

                total_fetched = len(result)

            logger.info(f"[UPSTASH] get_existing_product_hashes elapsed_ms={int((time.monotonic() - _t0) * 1000)} fetched={total_fetched} client_id={client_id}")
            return product_hashes
            
        except Exception as e:
            logger.error(f"❌ Error fetching existing product hashes: {e}")
            return {}

    def get_all_product_ids(self, client_id: str) -> List[str]:
        """
        Get all vector IDs (composite keys) for a client.
        
        Args:
            client_id: Client ID
            
        Returns:
            List of vector IDs in format {client_id}_{product_id}
        """
        try:
            # Build namespace for client isolation
            namespace = f"client_{client_id}"

            _t0 = time.monotonic()
            result = self.index.query(
                data="products",
                top_k=1000,
                filter=f"client_id = '{client_id}'",
                include_metadata=False,
                include_vectors=False,
                namespace=namespace
            )
            logger.info(f"[UPSTASH] get_all_product_ids elapsed_ms={int((time.monotonic() - _t0) * 1000)} client_id={client_id}")

            if result:
                return [r.id for r in result]

            return []

        except Exception as e:
            logger.error(f"❌ Error fetching product IDs: {e}")
            return []

    def delete_by_ids(self, ids: List[str], client_id: str = None) -> int:
        """
        Delete vectors by their IDs.
        
        Args:
            ids: List of vector IDs to delete
            client_id: Optional client ID for namespace (required if using namespaces)
            
        Returns:
            Number of deleted vectors
        """
        if not ids:
            return 0
        
        try:
            # Build namespace if client_id provided
            _t0 = time.monotonic()
            if client_id:
                namespace = f"client_{client_id}"
                self.index.delete(ids=ids, namespace=namespace)
            else:
                self.index.delete(ids=ids)
            logger.info(f"[UPSTASH] delete_by_ids elapsed_ms={int((time.monotonic() - _t0) * 1000)} count={len(ids)} client_id={client_id}")
            return len(ids)
        except Exception as e:
            logger.error(f"❌ Error deleting vectors by IDs: {e}")
            return 0