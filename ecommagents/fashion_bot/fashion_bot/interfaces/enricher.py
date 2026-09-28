from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

class OrderEnricherInterface(ABC):
    """
    Interface for enriching a list of processed orders with additional data
    (e.g., tracking status, product images, etc.) from external sources.
    """
    
    @abstractmethod
    def enrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Enrich the list of orders in-place or return a new list.
        
        Args:
            orders: List of standardized OrderInfoDTOs (or dicts)
            state: Conversation state
            
        Returns:
            The enriched list of orders
        """
        pass

    async def aenrich(self, orders: List[Dict[str, Any]], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """Async variant of enrich."""
        return self.enrich(orders, state=state)
