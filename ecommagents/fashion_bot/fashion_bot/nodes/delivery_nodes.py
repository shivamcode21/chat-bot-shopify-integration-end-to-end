"""
Delivery Timeline data collection node.
"""

from typing import Dict, Any
from fashion_bot.schema import SupportState


def data_collection_delivery_timeline(state: SupportState) -> Dict[str, Any]:
    """
    Simplified data collection - just route to intent handler.
    All logic is now handled by the generic skill node with tools.
    """
    return {
        "type": "intent_handle",
        "response_type": "intent_handle"
    }
