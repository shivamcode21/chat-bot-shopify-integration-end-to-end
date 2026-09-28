import logging
from datetime import datetime, timezone
from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)
from fashion_bot.config_manager import aresolve_client_id

logger = logging.getLogger(__name__)

def normalize_phone_for_db(phone_number: str) -> str:
    """
    Normalize phone number for database storage.
    Removes '+' and ensures it starts with '91'.
    
    Args:
        phone_number: Phone number in any format
        
    Returns:
        Normalized phone number (e.g., '919876543210')
    
    Examples:
        '+919876543210' -> '919876543210'
        '919876543210'  -> '919876543210'
        '9876543210'    -> '919876543210'
        '+91 9876543210' -> '919876543210'
    """
    if not phone_number:
        return phone_number
    
    # Remove spaces, dashes, and other non-digit characters except '+'
    cleaned = phone_number.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    
    # Remove '+' prefix
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    
    # Ensure it starts with '91'
    if len(cleaned) == 10:
        # Indian number without country code
        cleaned = "91" + cleaned
    elif not cleaned.startswith("91"):
        # Has some other format, try to fix
        if len(cleaned) == 12 and cleaned.startswith("91"):
            pass  # Already correct
        else:
            # Can't reliably fix, return as is
            pass
    
    return cleaned

@awith_retry
async def aget_conversation_state(phone_number: str, client_id: str = None) -> dict:
    """Async variant of get_conversation_state."""
    if client_id is None:
        client_id = await aresolve_client_id()

    normalized_phone = normalize_phone_for_db(phone_number)

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT mode, last_activity FROM conversation_mode_human_agent_and_bot WHERE client_id = %s AND phone_number = %s",
                    (client_id, normalized_phone),
                )
                row = await cur.fetchone()
                if row:
                    logger.debug(f"fetched async data for client: {client_id}, phone: {normalized_phone}")
                    return row
                return {"mode": "bot", "last_activity": datetime.now(timezone.utc)}
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"Async DB error fetching state for client: {client_id}, phone: {normalized_phone}: {e}")
        return {"mode": "bot", "last_activity": datetime.now(timezone.utc)}


async def aget_conversation_mode(phone_number: str, client_id: str = None) -> tuple:
    """Async variant of get_conversation_mode."""
    state_data = await aget_conversation_state(phone_number, client_id)
    mode = state_data.get("mode", "bot") if isinstance(state_data, dict) else "bot"
    last_activity = state_data.get("last_activity") if isinstance(state_data, dict) else None
    return (mode, last_activity)

@awith_retry
async def aset_conversation_mode(phone_number: str, mode: str, client_id: str = None):
    """Async variant of set_conversation_mode."""
    if client_id is None:
        client_id = await aresolve_client_id()

    normalized_phone = normalize_phone_for_db(phone_number)

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_mode_human_agent_and_bot (client_id, phone_number, mode, last_activity, updated_at)
                    VALUES (%s, %s, %s, (NOW() AT TIME ZONE 'UTC'), (NOW() AT TIME ZONE 'UTC'))
                    ON CONFLICT (client_id, phone_number) DO UPDATE
                    SET mode = EXCLUDED.mode,
                    last_activity = (NOW() AT TIME ZONE 'UTC'),
                    updated_at = (NOW() AT TIME ZONE 'UTC');
                    """,
                    (client_id, normalized_phone, mode),
                )
                logger.info(f"Conversation mode for client: {client_id}, phone: {normalized_phone} set to {mode} (async)")
    except Exception as e:
        if is_connection_error(e):
            raise
        logger.error(f"Async DB error setting mode for client: {client_id}, phone: {normalized_phone}: {e}")
