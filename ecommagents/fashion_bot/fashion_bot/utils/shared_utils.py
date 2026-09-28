"""
Utility functions shared across the fashion bot modules.
"""
import re
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("shared_utils")


def strip_nul_bytes(value: Any) -> Any:
    """Recursively remove NUL (``0x00``) bytes from text bound for PostgreSQL.

    PostgreSQL text/varchar columns (and JSONB values) cannot store a NUL byte.
    psycopg enforces this client-side and raises ``DataError: PostgreSQL text
    fields cannot contain NUL (0x00) bytes`` *before* the row is written, so a
    single stray ``\\x00`` in a rendered template, customer name, or upstream
    API payload aborts the whole insert and silently drops the data. WhatsApp /
    Gupshup payloads occasionally carry these bytes.

    Strings are cleaned; dicts, lists, and tuples are cleaned recursively so
    values destined for JSONB columns (and full parameter tuples passed to
    ``cur.execute``) are safe too. Any other type is returned unchanged.

    Note: sanitize the *dict* before ``json.dumps`` rather than the dumped
    string — ``json.dumps`` escapes a NUL to the literal ``\\u0000`` sequence,
    which is not a ``0x00`` byte but is still rejected by JSONB columns.
    """
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {k: strip_nul_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul_bytes(v) for v in value]
    if isinstance(value, tuple):
        return tuple(strip_nul_bytes(v) for v in value)
    return value


def iso_to_epoch(value: Optional[str]) -> Optional[int]:
    """Convert an ISO-8601 timestamp string to integer epoch seconds.

    Powers the numeric ``created_at_ts`` field that the new-arrivals search
    filters on — Upstash Search range operators (``>=``) apply to numbers, not
    strings, so the ISO ``created_at`` cannot be range-filtered directly.

    Returns ``None`` when the value is missing or unparseable. Shopify
    ``createdAt`` is tz-aware (e.g. ``2026-04-16T11:24:32+05:30``); a naive value
    is assumed UTC.
    """
    if not value or not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())

def extract_order_id_from_message(message: str, use_llm: bool = True, previous_bot_message: str = "") -> str:
    """
    Extract order ID from user message using LLM-based approach to avoid false positives.
    
    Args:
        message: User message to extract order ID from
        use_llm: Whether to use LLM-based extraction (default: True)
        previous_bot_message: The bot's previous message for context (helps avoid false positives)
    
    Returns:
        Extracted order ID or empty string if no valid order ID found
    """
    if not message or not message.strip():
        return ""
    
    if use_llm:
        return _extract_order_id_with_llm(message, previous_bot_message)
    else:
        # Fallback to regex-based extraction (legacy)
        return _extract_order_id_with_regex(message)

def _extract_order_id_with_llm(message: str, previous_bot_message: str = "") -> str:
    """
    Use LLM to extract order ID from message with context awareness.
    This avoids false positives like pincodes, phone numbers, prices, etc.
    """
    try:
        # Resolve LLM per-call (not via the module-level fashion_bot.llm_config
        # shim) so llm.* metrics get tagged with caller="order_id_extraction"
        # instead of the singleton's permanent "unknown".
        from fashion_bot.core.llm_factory import LLMFactory
        llm = LLMFactory.get_llm(tool_name="order_id_extraction")

        # Build context section
        context_section = ""
        if previous_bot_message:
            context_section = f"""
**Previous Bot Message (for context):**
{previous_bot_message}

This helps you understand what the user is responding to. For example:
- If the bot asked for a pincode and the user provides a 6-digit number, it's a pincode, NOT an order ID
- If the bot asked for a product link and the user provides a URL, don't extract numbers from the URL
- If the bot is asking about delivery/shipping and user provides numbers, they're likely pincodes
"""
        
        extraction_prompt = f"""You are an order ID extraction assistant. Your task is to identify and extract valid order IDs from the user's message.

**Important Guidelines:**
- An order ID is typically a 4-8 character identifier (e.g., "GV5656", "AB1234", "9721", "123456")
- Order IDs can be alphanumeric (like "GV5656") or purely numeric (like "9721")
- In the context of order cancellation, returns, or tracking, numbers are likely order IDs
- DO NOT extract pincodes (6-digit numbers explicitly in address context like "send to 110058" or "my pincode is 560001")
- DO NOT extract phone numbers (10-digit numbers like "9876543210")
- DO NOT extract prices or amounts (numbers with currency symbols or in payment context like "₹500" or "paid 1000")
- DO NOT extract quantities in product context (like "2 items" or "quantity 3")
- **CRITICAL**: If the previous bot message asked for a pincode/delivery location, then the user's number is a PINCODE, not an order ID

**Context Clues for Order IDs:**
- Words like "cancel", "return", "track", "status" followed by a number
- Just a 4-8 digit number provided by itself (likely in response to "what's your order ID?")
- Numbers after "#", "order", "ID", "number"
- Alphanumeric codes (like GV5656, AB1234)
{context_section}
**User Message:**
{message}

**Instructions:**
1. **FIRST**: Check the previous bot message - if it asked for pincode/delivery location, then numbers in the user's response are pincodes, NOT order IDs
2. Check if the message contains words like "cancel", "return", "track" followed by a number - that number is likely an order ID
3. Check if the message is just a standalone 4-8 character alphanumeric code - that's likely an order ID (UNLESS previous message asked for pincode)
4. Ignore numbers that are clearly pincodes (6 digits in address context), phone numbers (10 digits), or prices
5. When in doubt and the number is 4-8 digits, prefer treating it as an order ID unless it's clearly something else OR the previous message asked for pincode

**Output Format (JSON only):**
{{
    "order_id": "extracted_order_id_or_empty_string",
    "confidence": "high/medium/low",
    "reasoning": "brief explanation of why this is or isn't an order ID"
}}

Respond ONLY with valid JSON, no additional text."""

        response = llm.invoke(extraction_prompt)
        response_content = response.content.strip()
        
        # Extract JSON from response
        json_start = response_content.find('{')
        json_end = response_content.rfind('}') + 1
        
        if json_start == -1 or json_end == 0:
            logger.warning(f"No JSON found in LLM response for order ID extraction")
            return ""
        
        json_content = response_content[json_start:json_end]
        result = json.loads(json_content)
        
        order_id = result.get("order_id", "").strip()
        confidence = result.get("confidence", "low")
        reasoning = result.get("reasoning", "")
        
        logger.info(f"LLM Order ID Extraction - ID: '{order_id}', Confidence: {confidence}, Reasoning: {reasoning}")
        
        # Return order ID if confidence is high or medium
        if order_id and confidence in ["high", "medium"]:
            return order_id.upper()
        
        return ""
        
    except Exception as e:
        logger.error(f"LLM-based order ID extraction failed: {e}")
        # Fallback to regex if LLM fails
        return _extract_order_id_with_regex(message)

def _extract_order_id_with_regex(message: str) -> str:
    """
    Legacy regex-based order ID extraction (fallback only).
    Note: This may produce false positives with pincodes and other numbers.
    """
    message_lower = message.lower()
    message_upper = message.upper()
    
    # Skip if message contains pincode/address indicators
    pincode_indicators = ['pincode', 'pin code', 'zip', 'postal', 'send to', 'ship to', 'address']
    if any(indicator in message_lower for indicator in pincode_indicators):
        # Only extract if there's also order-related context
        order_indicators = ['cancel', 'return', 'track', 'order', '#']
        if not any(indicator in message_lower for indicator in order_indicators):
            return ""
    
    # Skip if message contains phone indicators
    phone_indicators = ['phone', 'mobile', 'contact', 'number', 'call']
    if any(indicator in message_lower for indicator in phone_indicators):
        # Don't extract 10-digit numbers from phone context
        if re.search(r'\b\d{10}\b', message):
            return ""
    
    # Common order ID patterns (prioritize alphanumeric patterns)
    alphanumeric_patterns = [
        r'\b[A-Z]{2}\d{4}\b',  # gv5656, ab1234, etc.
        r'\b[A-Z]{3}\d{3}\b',  # abc123, etc.
        r'\b[A-Z]{2}\d{5}\b',  # ab12345, etc.
        r'\b[A-Z]\d{5}\b',     # a12345, etc.
    ]
    
    # Try alphanumeric patterns first (least likely to be false positives)
    for pattern in alphanumeric_patterns:
        match = re.search(pattern, message_upper)
        if match:
            return match.group(0)
    
    # Try numeric patterns only if there's order-related context
    order_context = ['cancel', 'return', 'track', 'order', '#', 'status']
    has_order_context = any(word in message_lower for word in order_context)
    
    if has_order_context:
        # Extract 4-8 digit numbers in order context
        numeric_patterns = [
            r'\b\d{4,8}\b',  # 4-8 digit numbers
        ]
        
        for pattern in numeric_patterns:
            matches = re.findall(pattern, message)
            # Skip 10-digit numbers (likely phone numbers)
            for match in matches:
                if len(match) != 10:
                    return match.upper()
    
    # If message is JUST a number (4-8 digits), assume it's an order ID
    stripped = message.strip()
    if re.match(r'^\d{4,8}$', stripped):
        return stripped.upper()
    
    return ""
