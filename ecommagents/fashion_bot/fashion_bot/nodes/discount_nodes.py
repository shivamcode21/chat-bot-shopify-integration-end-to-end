"""
Discount data collection node.
"""

from typing import Dict, Any
from fashion_bot.schema import SupportState
import re
import logging

logger = logging.getLogger("discount_nodes")


def data_collection_discount(state: SupportState) -> Dict[str, Any]:
    """Data collection for discount queries."""
    current_message = state.get("messages", [])[-1].content if state.get("messages") else ""
    if not state.get("product_link") and current_message:
        url_patterns = [
            r'https?://[^\s]+',
            r'www\.[^\s]+',
            r'product/[^\s]+',
            r'item/[^\s]+'
        ]
        for pattern in url_patterns:
            match = re.search(pattern, current_message, re.IGNORECASE)
            if match:
                state["product_link"] = match.group(0)
                logger.info(f"🔗 Extracted Product Link: {match.group(0)}")
                break
    # For discount queries, product URL is optional
    # General discount queries don't need a specific product
    # Proceed directly to intent handling
    return {"type": "intent_handle", "response_type": "intent_handle"}
