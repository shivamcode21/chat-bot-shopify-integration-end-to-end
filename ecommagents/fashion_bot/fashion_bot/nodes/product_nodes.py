"""
Product-related utility functions.
"""


def get_synonym_map():
    """Get the centralized synonym mapping for product search"""
    return {
        'jeans': 'denim',
        'denim': 'jeans',
    }

def get_synonym_list_map():
    """Get the synonym mapping as lists for fuzzy search"""
    return {
        'jeans': ['jeans', 'denim'],
        'denim': ['denim', 'jeans'],
    }
