"""
Prompts module for centralized prompt management.

This module contains all system prompts used by various nodes,
organized by domain (product, order, delivery, etc.)
"""

try:
    from fashion_bot.prompts.product_prompts import (
        get_product_details_prompt,
        get_synonym_map,
        get_synonym_list_map
    )
    __all__ = [
        "get_product_details_prompt",
        "get_synonym_map",
        "get_synonym_list_map"
    ]
except ModuleNotFoundError:
    __all__ = []

