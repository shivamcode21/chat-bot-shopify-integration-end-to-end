from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

class ProductInterface(ABC):
    """Interface for Product related operations"""
    
    @abstractmethod
    def get_product_details_by_url(self, url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get product details from a URL"""
        pass

    @abstractmethod
    def get_product_details_by_id(self, product_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Get product details from an ID or handle"""
        pass
        
    @abstractmethod
    def search_products(self, query: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Search for products"""
        pass
    
    async def aget_product_details_by_url(self, url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async variant of get_product_details_by_url."""
        return self.get_product_details_by_url(url, state=state)

    async def aget_product_details_by_id(self, product_id: str, state: Optional[Dict] = None, id_type: str = "handle") -> Dict[str, Any]:
        """Async variant of get_product_details_by_id."""
        return self.get_product_details_by_id(product_id, state=state)

    async def asearch_products(self, query: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Async variant of search_products."""
        return self.search_products(query, limit=limit, state=state)

    async def asearch_products_by_name(
        self,
        product_name: str,
        limit: int = 5,
        state: Optional[Dict] = None,
        **kwargs,
    ) -> List[Dict[str, Any]]:
        """Async product-name search. Defaults to asearch_products."""
        return await self.asearch_products(product_name, limit=limit, state=state)

    async def aget_top_selling_products(self, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Async variant of get_top_selling_products."""
        return self.get_top_selling_products(limit=limit, state=state)
    
    @abstractmethod
    def get_top_selling_products(self, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Get top selling products"""
        pass

