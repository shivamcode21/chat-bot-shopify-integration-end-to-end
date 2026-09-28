import os
import logging
from typing import Dict, Any, Optional, List
from fashion_bot.interfaces.product import ProductInterface

logger = logging.getLogger("fashion_bot.mock.product")


def _log_mock_call(method: str, params: str = ""):
    """Log mock adapter method calls for debugging."""
    msg = f"[MOCK CALL] 🛍️ MockProductAdapter.{method}({params})"
    logger.info(msg)
    if os.getenv("DEBUG_FACTORY_ROUTES", "").lower() == "true":
        print(msg)


class MockProductAdapter(ProductInterface):
    def __init__(self, client_id: str = None):
        self.client_id = client_id
        _log_mock_call("__init__", f"client_id={client_id}")
        
    def get_product_details_by_url(self, url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        _log_mock_call("get_product_details_by_url", f"url={url[:50]}...")
        return {
            "name": "Mock Product - Cosmic Shacket",
            "handle": "mock-cosmic-shacket",
            "url": url,
            "price": {"min": "2999.00", "max": "2999.00"},
            "available_sizes": ["S", "M", "L"],
            "description": "This is a mock product description for testing.",
            "images": [{"src": "https://example.com/mock.jpg"}],
            "vendor": "Mock Vendor"
        }

    def get_product_details_by_id(self, product_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        _log_mock_call("get_product_details_by_id", f"product_id={product_id}")
        return {
            "name": f"Mock Product ({product_id})",
            "handle": product_id,
            "price": "1999.00",
            "available_sizes": ["M", "L"]
        }
        
    def search_products(self, query: str, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        _log_mock_call("search_products", f"query={query}, limit={limit}")
        return [
            {
                "name": f"Mock Result for {query} #{i}",
                "handle": f"mock-{query}-{i}",
                "price": "1500.00"
            } for i in range(limit)
        ]
    
    def get_top_selling_products(self, limit: int = 5, state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        _log_mock_call("get_top_selling_products", f"limit={limit}")
        return [
            {
                "title": f"Top Product #{i}",
                "handle": f"top-{i}",
                "price": "2500.00",
                "sales": 100 - i,
                "all_sizes": ["S", "M", "L", "XL"],
                "available_sizes": ["S", "M", "L"],
                "colors": ["Black", "White"],
                "variants": [
                    {"id": j, "title": size, "price": "2500.00", "sku": f"TP{i}-{size}", "inventory_quantity": 5, "available": True, "option1": size, "option2": None, "option3": None}
                    for j, size in enumerate(["S", "M", "L", "XL"])
                ],
                "total_inventory": 20,
                "in_stock": True,
            } for i in range(limit)
        ]

