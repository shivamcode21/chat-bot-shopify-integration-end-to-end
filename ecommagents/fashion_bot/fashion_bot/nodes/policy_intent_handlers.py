"""
Policy data collection node.
"""

import re
from typing import Dict, Any
from fashion_bot.schema import SupportState
from fashion_bot.utils.utils import log_with_trace_id, check_missing_vars, merge_customer_messages


def data_collection_return_policy(state: SupportState) -> Dict[str, Any]:
    """
    Data collection for return/exchange policy queries.
    Extracts product link from message if present.
    """
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
                log_with_trace_id(state, f"🔗 Extracted Product Link: {match.group(0)}")
                break
    required_vars = [
        {
            "var_name": "product_link",
            "state_map": "product_link",
            "type": "shopify_product_url",
            "customer_message": "the product link"
        }
    ]
    missing_vars = check_missing_vars(state, required_vars)
    if missing_vars:
        return {
            "type": "customer_message",
            "customer_message": merge_customer_messages(missing_vars)
        }
    else:
        return {"type": "intent_handle", "response_type": "intent_handle"}
