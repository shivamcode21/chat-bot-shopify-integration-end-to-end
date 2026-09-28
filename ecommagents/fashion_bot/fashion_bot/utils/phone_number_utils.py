"""
Phone Number Utilities - Functions for phone number validation and formatting.
"""
from typing import Tuple
import re


def validate_phone_for_order_access(state_phone: str, order_phone: str, bypass_phone: str = "9012345678") -> Tuple[bool, str]:
    """
    Validate if the customer can access order information based on phone number.
    
    Args:
        state_phone: Phone number from state (customer's phone)
        order_phone: Phone number from order details
        bypass_phone: Phone number that can bypass validation (default: 9012345678)
    
    Returns:
        Tuple of (is_valid: bool, message: str)
    """
    # Allow bypass for specific phone number
    if state_phone == bypass_phone:
        return True, "✅ Phone validation PASSED - bypass phone used"
    
    # Remove all non-digit characters for comparison
    clean_state_phone = re.sub(r'\D', '', state_phone) if state_phone else ""
    clean_order_phone = re.sub(r'\D', '', order_phone) if order_phone else ""
    
    # If either phone is empty, deny access
    if not clean_state_phone or not clean_order_phone:
        return False, "❌ Phone validation FAILED - one or both phone numbers are empty"
    
    # Compare last 10 digits (Indian phone numbers)
    state_last_10 = clean_state_phone[-10:] if len(clean_state_phone) >= 10 else clean_state_phone
    order_last_10 = clean_order_phone[-10:] if len(clean_order_phone) >= 10 else clean_order_phone
    
    if state_last_10 == order_last_10:
        return True, "✅ Phone validation PASSED - customer is authorized"
    else:
        return False, f"❌ Phone validation FAILED - phone numbers don't match. Customer: {state_phone}, Order: {order_phone}"


def normalize_phone_number(phone: str) -> str:
    """
    Normalize a phone number by removing non-digit characters.
    
    Args:
        phone: Raw phone number string
        
    Returns:
        Cleaned phone number with only digits
    """
    if not phone:
        return ""
    return re.sub(r'\D', '', phone)


def strip_country_code(phone: str) -> str:
    """
    Strip the 91 (India) country code prefix from a phone number.
    Used for displaying phone numbers to agents without the country code.
    
    Args:
        phone: Phone number string (may include +91, 91 prefix)
        
    Returns:
        10-digit phone number without country code
    """
    if not phone:
        return ""
    # Remove non-digit characters first
    clean_phone = str(phone).lstrip("+")
    # Remove 91 prefix if present and number is longer than 10 digits
    if clean_phone.startswith("91") and len(clean_phone) > 10:
        clean_phone = clean_phone[2:]
    return clean_phone


def is_real_phone_number(phone: str) -> bool:
    """
    Check if phone is a real customer phone number, not a web session ID or placeholder.
    Web chat sessions use 'web_<UUID>' or 'fbw_<ID>' as a fallback phone when the user
    skips the phone prompt. These must not be treated as real phone numbers.
    """
    if not phone:
        return False
    if phone.startswith(("web_", "fbw_")):
        return False
    clean = re.sub(r'\D', '', phone)
    return len(clean) >= 10


def get_last_n_digits(phone: str, n: int = 10) -> str:
    """
    Get the last N digits of a phone number.
    
    Args:
        phone: Phone number string
        n: Number of digits to return (default 10 for Indian numbers)
        
    Returns:
        Last N digits of the phone number
    """
    clean_phone = normalize_phone_number(phone)
    return clean_phone[-n:] if len(clean_phone) >= n else clean_phone


# Prefixes used by the web chat widget as placeholder identifiers.
_WEB_SESSION_PREFIXES = ("web_", "fbw_")
# The escalations table stores customer_phone as VARCHAR(20).
_STORED_PHONE_MAXLEN = 20
# Default country code for phone variant generation.
_DEFAULT_COUNTRY_CODE = "91"


def phone_match_variants(
    phone_number: str,
    *,
    country_code: str = _DEFAULT_COUNTRY_CODE,
    stored_maxlen: int = _STORED_PHONE_MAXLEN,
    web_prefixes: Tuple[str, ...] = _WEB_SESSION_PREFIXES,
) -> list:
    """Generate spelling variants of a phone number for DB matching.

    Production stores whatever spelling the channel handed over — some rows
    carry the bare 10-digit local number, others carry the country-code-prefixed
    form. An exact match would miss records stored under the other spelling.

    For web-chat session identifiers (``web_``, ``fbw_``), returns the stored
    truncation only — no numeric variants apply.

    The ``country_code`` parameter defaults to "91" (India) which covers all
    current production clients. Callers serving other regions can pass a
    different code without changing the implementation.

    Returns a sorted, deduplicated list suitable for SQL ``= ANY(array)``.
    """
    raw = str(phone_number or "").strip()
    if not raw:
        return []
    if raw.startswith(web_prefixes):
        return [raw[:stored_maxlen]]
    last10 = get_last_n_digits(raw, 10)
    if not last10:
        return [raw[:stored_maxlen]]
    return sorted(
        {raw[:stored_maxlen], last10, f"{country_code}{last10}", f"+{country_code}{last10}"}
    )


def check_phone_collection_gate(
    state: dict | None,
    flag_name: str = "escalation_phone_requested",
) -> tuple[bool, str | None]:
    """Check whether a web-chat user needs to provide contact info.

    Implements the reusable phone-collection gate pattern used before
    escalations and store-visit notifications.  The caller provides a
    ``flag_name`` so different flows (escalation vs store visit) track
    their own flag independently.

    Returns ``(phone_required, customer_contact)``:
    - ``(True, None)``  — first attempt: flag was not set, now set.
      Caller should ask for phone/email and skip the action.
    - ``(False, <contact>)`` — second attempt: flag was set, contact
      extracted from recent messages.
    - ``(False, None)`` — second attempt: flag was set but no contact
      found in messages.  Or phone is real / not a session ID.
    """
    import json as _json

    phone = (state or {}).get("phone_number")
    if not phone or is_real_phone_number(phone):
        return False, None

    scratchpad_raw = state.get("scratchpad", "") if state else ""
    scratchpad_obj: dict = {}
    if scratchpad_raw:
        try:
            scratchpad_obj = (
                _json.loads(scratchpad_raw)
                if isinstance(scratchpad_raw, str)
                else (scratchpad_raw if isinstance(scratchpad_raw, dict) else {})
            )
        except (ValueError, TypeError):
            scratchpad_obj = {}

    if not scratchpad_obj.get(flag_name):
        scratchpad_obj[flag_name] = True
        if state:
            state["scratchpad"] = (
                _json.dumps(scratchpad_obj)
                if isinstance(scratchpad_raw, str)
                else scratchpad_obj
            )
        return True, None

    human_msgs = [
        m
        for m in (state.get("messages", []) if state else [])
        if getattr(m, "type", "") == "human"
    ]
    return False, extract_contact_from_messages(human_msgs)


def _contact_from_state(state: dict | None) -> str | None:
    """Phone or email the customer typed in their recent messages."""
    human_msgs = [
        m
        for m in (state.get("messages", []) if state else [])
        if getattr(m, "type", "") == "human"
    ]
    return extract_contact_from_messages(human_msgs)


async def aresolve_contact_collection_gate(
    state: dict | None,
    *,
    phone_number: str | None = None,
    flag_name: str = "escalation_phone_requested",
    client_id: str | None = None,
    trace_id: str | None = None,
) -> tuple[bool, str | None]:
    """Decide whether to ask a web-chat guest for a contact, reusing a known one.

    Async sibling of :func:`check_phone_collection_gate`, and the single place
    the escalation flows make this decision — the same block was previously
    inlined at both escalation call sites.

    Adds one thing over the sync version: before asking, it checks whether an
    earlier escalation already recorded a reachable contact. The "already asked"
    flag lives in the scratchpad, which is part of conversation state and
    expires after 24h of silence, so a customer chasing a two-day-old issue used
    to be asked for the same address they had already given — on a ticket that
    was already carrying it.

    Returns ``(ask_now, customer_contact)``:

    * ``(True, None)`` — nothing on file and we have not asked yet. The caller
      should ask and skip the action. The flag is set as a side effect.
    * ``(False, <contact>)`` — we have something usable: what they typed this
      turn if present, else the address on file.
    * ``(False, None)`` — already asked and still nothing, or the customer has a
      real phone number and was never a guest.

    When the escalation-context feature is disabled the lookup returns ``None``
    without any I/O, so this collapses to exactly the previous behaviour.

    ``phone_number`` is the identifier the *caller* resolved, which is not always
    ``state["phone_number"]``: ``escalate_to_agent`` takes one as a tool argument
    and only falls back to state, and the restock flow defaults a missing one to
    ``"Not provided"``. Both gates were evaluating that effective value, so it
    has to be passed in rather than re-derived here — reading state directly
    would silently change who gets asked.
    """
    import json as _json

    phone = phone_number if phone_number is not None else (state or {}).get("phone_number")
    if not phone or is_real_phone_number(phone):
        return False, None

    scratchpad_raw = state.get("scratchpad", "") if state else ""
    scratchpad_obj: dict = {}
    if scratchpad_raw:
        try:
            scratchpad_obj = (
                _json.loads(scratchpad_raw)
                if isinstance(scratchpad_raw, str)
                else (scratchpad_raw if isinstance(scratchpad_raw, dict) else {})
            )
        except (ValueError, TypeError):
            scratchpad_obj = {}

    typed = _contact_from_state(state)
    if scratchpad_obj.get(flag_name):
        return False, typed

    from fashion_bot.utils.escalation_context import aget_contact_on_file

    on_file = await aget_contact_on_file(
        client_id=client_id, phone_number=phone, trace_id=trace_id
    )
    if on_file:
        # Anything typed this turn wins: a customer correcting their address
        # must not be overridden by the stale one on the earlier ticket.
        return False, typed or on_file

    scratchpad_obj[flag_name] = True
    if state:
        state["scratchpad"] = (
            _json.dumps(scratchpad_obj)
            if isinstance(scratchpad_raw, str)
            else scratchpad_obj
        )
    return True, None


def extract_contact_from_messages(messages: list) -> str | None:
    """Extract a phone number or email from recent user messages.

    Scans the last 3 human messages (most recent first) for an Indian phone
    number (10+ digits) or an email address.  Returns the first match found,
    or ``None`` if neither is present.
    """
    _EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
    _PHONE_RE = re.compile(r'(?:\+?91[\s\-]?)?[6-9]\d{9}')

    recent = messages[-3:] if messages else []
    for msg in reversed(recent):
        text = msg if isinstance(msg, str) else (getattr(msg, "content", str(msg)) if msg else "")
        phone_match = _PHONE_RE.search(text)
        if phone_match:
            digits = re.sub(r'\D', '', phone_match.group())
            if digits.startswith("91") and len(digits) > 10:
                digits = digits[2:]
            return digits
        email_match = _EMAIL_RE.search(text)
        if email_match:
            return email_match.group()
    return None
