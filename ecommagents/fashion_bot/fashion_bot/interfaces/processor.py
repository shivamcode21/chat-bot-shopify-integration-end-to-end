from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List

class OrderProcessorInterface(ABC):
    """
    Interface for processing raw vendor order data into a standardized format.
    """
    
    @abstractmethod
    def process_order(self, raw_data: Any, state: Optional[Dict] = None, **kwargs) -> Dict[str, Any]:
        """
        Convert raw vendor order object/dict to standardized OrderInfoDTO.
        
        Args:
            raw_data: The raw data returned by the Adapter (dict or object)
            state: Conversation state for logging/context
            **kwargs: Additional context for processing (e.g., source_type)
            
        Returns:
            Dict matching the OrderInfoDTO structure
        """
        pass
        
    @abstractmethod
    def process_orders(self, raw_orders: List[Any], state: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        Process a list of orders.
        """
        pass

