"""
Client ID encoding/decoding utilities.

Provides utilities to encode client_id for use in URLs (widget integration)
and decode it on the server side to avoid database lookups.

Usage:
    # In admin panel / widget generator (Python):
    encoded = encode_client_id("groovee")
    # -> "Z3Jvb3ZlZQ=="
    
    # In widget (JavaScript):
    const encodedId = btoa("groovee");  // -> "Z3Jvb3ZlZQ=="
    
    # In server (Python):
    decoded = decode_client_id("Z3Jvb3ZlZQ==")
    # -> "groovee"
"""
import base64
import logging
import os
from functools import lru_cache
from typing import Optional, Set

logger = logging.getLogger(__name__)

_BLOCKLIST_ENV_KEY = "BLOCKLIST_WEBHOOK_CLIENTS"
_BLOCKLIST_SHOPIFY_DOMAINS_ENV_KEY = "BLOCKLIST_WEBHOOK_SHOPIFY_DOMAINS"
# Scoped to the abandoned-checkout webhook only -- unlike BLOCKLIST_WEBHOOK_CLIENTS
# (which silences every webhook type for a client), this lets a client opt out of
# abandoned-checkout recovery specifically while Shopify order sync, Shiprocket,
# Delhivery, etc. keep working normally.
_BLOCKLIST_ABANDONED_CHECKOUT_ENV_KEY = "BLOCKLIST_ABANDONED_CHECKOUT_CLIENTS"


@lru_cache(maxsize=1)
def _get_blocklisted_client_ids() -> Set[str]:
    raw = os.environ.get(_BLOCKLIST_ENV_KEY, "")
    if not raw or not raw.strip():
        return set()
    return {cid.strip().lower() for cid in raw.split(",") if cid.strip()}


def is_client_blocklisted(client_id: Optional[str]) -> bool:
    """Return True if client_id is on the webhook blocklist (env: BLOCKLIST_WEBHOOK_CLIENTS)."""
    if not client_id:
        return False
    return str(client_id).strip().lower() in _get_blocklisted_client_ids()


@lru_cache(maxsize=1)
def _get_blocklisted_shopify_domains() -> Set[str]:
    raw = os.environ.get(_BLOCKLIST_SHOPIFY_DOMAINS_ENV_KEY, "")
    if not raw or not raw.strip():
        return set()
    return {
        domain.strip().lower()
        for domain in raw.split(",")
        if domain.strip()
    }


def is_shop_domain_blocklisted(shop_domain: Optional[str]) -> bool:
    """Return True if a Shopify shop domain is blocked before any DB lookup."""
    if not shop_domain:
        return False
    return str(shop_domain).strip().lower() in _get_blocklisted_shopify_domains()


@lru_cache(maxsize=1)
def _get_abandoned_checkout_blocklisted_client_ids() -> Set[str]:
    raw = os.environ.get(_BLOCKLIST_ABANDONED_CHECKOUT_ENV_KEY, "")
    if not raw or not raw.strip():
        return set()
    return {cid.strip().lower() for cid in raw.split(",") if cid.strip()}


def is_abandoned_checkout_blocklisted(client_id: Optional[str]) -> bool:
    """
    Return True if client_id is opted out of abandoned-checkout webhook
    processing (env: BLOCKLIST_ABANDONED_CHECKOUT_CLIENTS).

    Scoped deliberately: use this instead of is_client_blocklisted when a
    client just doesn't want abandoned-checkout recovery (e.g. no template
    configured, using a different recovery channel) but should keep
    receiving every other webhook type normally.
    """
    if not client_id:
        return False
    return str(client_id).strip().lower() in _get_abandoned_checkout_blocklisted_client_ids()


def encode_client_id(client_id: str) -> str:
    """
    Encode a client_id to base64 for use in URLs.
    
    Args:
        client_id: The client's UUID or identifier
        
    Returns:
        Base64 encoded string safe for URL usage
    """
    if not client_id:
        raise ValueError("client_id cannot be empty")
    
    # Use URL-safe base64 encoding
    encoded_bytes = base64.urlsafe_b64encode(client_id.encode('utf-8'))
    return encoded_bytes.decode('utf-8')


def decode_client_id(encoded_client_id: str) -> str:
    """
    Decode a base64 encoded client_id from URL.
    
    Args:
        encoded_client_id: Base64 encoded client_id
        
    Returns:
        Decoded client_id
        
    Raises:
        ValueError: If the encoded string is invalid
    """
    if not encoded_client_id:
        raise ValueError("encoded_client_id cannot be empty")
    
    try:
        # Handle URL-safe base64 (replace - with + and _ with /)
        # Standard base64 uses + and /, URL-safe uses - and _
        padded = encoded_client_id + '=='  # Add padding if needed
        padded = padded.replace('-', '+').replace('_', '/')
        
        decoded_bytes = base64.urlsafe_b64decode(padded)
        return decoded_bytes.decode('utf-8')
    except Exception as e:
        logger.error(f"Error decoding client_id '{encoded_client_id[:20]}...': {e}")
        raise ValueError(f"Invalid base64 encoded client_id: {e}")


def is_encoded_client_id(identifier: str) -> bool:
    """
    Check if the identifier is likely an encoded client_id vs a client_name.
    
    Heuristics:
    - Encoded IDs are typically base64 (alphanumeric with +/-_ and = padding)
    - Client names are usually plain text (store names)
    
    Args:
        identifier: The identifier to check
        
    Returns:
        True if it looks like an encoded client_id, False otherwise
    """
    if not identifier:
        return False
    
    # Check for base64-like patterns
    # Encoded IDs usually have: uppercase/lowercase letters, numbers, +/-, and often = at end
    import re
    
    # Pattern for likely base64 encoded ID (with optional = padding)
    base64_pattern = r'^[A-Za-z0-9+\/=]{20,}$'
    if re.match(base64_pattern, identifier):
        return True
    
    # Also check shorter patterns that end with = (common for short UUIDs)
    short_base64_pattern = r'^[A-Za-z0-9+\/]+={1,2}$'
    if re.match(short_base64_pattern, identifier):
        return True
        
    return False
