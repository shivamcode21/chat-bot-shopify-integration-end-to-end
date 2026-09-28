"""
Base Product Service - Abstract base class for product fetching services.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional
from fashion_bot.services.product_ingestion.models import NormalizedProduct


class BaseProductService(ABC):
    """Abstract base class for product fetching services."""
    
    @abstractmethod
    async def fetch_active_products(self, max_products: int = 0) -> List[NormalizedProduct]:
        """
        Fetch all active products from the source.
        
        Args:
            max_products: Upper limit on returned products (0 = unlimited).
                          Implementations should prioritise in-stock items.

        Returns:
            List of NormalizedProduct objects
        """
        pass
    
    async def fetch_recently_updated_products(
        self, 
        updated_since: Optional[datetime] = None,
        hours: int = 24
    ) -> List[NormalizedProduct]:
        """
        Fetch products updated since a given time.
        
        Default implementation falls back to fetch_active_products.
        Subclasses can override for optimized filtering at source.
        
        Args:
            updated_since: Fetch products updated after this datetime (UTC).
                          If None, uses current time minus `hours`.
            hours: Number of hours to look back (default: 24).
                   Only used if updated_since is None.
        
        Returns:
            List of NormalizedProduct objects updated in the time window
        """
        # Default: fall back to fetching all products
        # Subclasses should override for optimized queries
        return await self.fetch_active_products()
    
    @property
    @abstractmethod
    def source_name(self) -> str:
        """Return the name of this product source."""
        pass
