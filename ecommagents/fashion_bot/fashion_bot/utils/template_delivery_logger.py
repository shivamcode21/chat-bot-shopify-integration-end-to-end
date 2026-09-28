"""
Template Delivery Logger - Modular system to track template message sends.
Can be enabled/disabled via environment variable.
"""
import os
import json
import logging
from typing import Optional, Dict, Any
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.utils.shared_utils import strip_nul_bytes

logger = logging.getLogger(__name__)

# Configuration - can be disabled via environment variable
TEMPLATE_LOGGING_ENABLED = os.getenv("TEMPLATE_DELIVERY_LOGGING_ENABLED", "true").lower() == "true"


def is_template_logging_enabled() -> bool:
    """Check if template delivery logging is enabled"""
    return TEMPLATE_LOGGING_ENABLED


async def alog_template_delivery(
    client_id: str,
    phone_number: str,
    template_id: str,
    success: bool,
    event_key: Optional[str] = None,
    channel: str = 'whatsapp',
    response_data: Optional[Dict[str, Any]] = None,
    error_message: Optional[str] = None,
    template_name: Optional[str] = None,
    template_message: Optional[str] = None,
    template_params: Optional[Dict[str, Any]] = None,
    message_id: Optional[str] = None
) -> bool:
    """
    Log template message delivery attempt (async, native).

    Args:
        client_id: Client/tenant ID
        phone_number: Recipient phone number
        template_id: Gupshup template ID
        success: Whether template was sent successfully
        event_key: Event that triggered the template (e.g., 'DELIVERED', 'VOIDED')
        channel: Communication channel (default: 'whatsapp')
        response_data: API response data (will be stored as JSONB)
        error_message: Error message if send failed
        template_name: Human-readable template name (optional)
        template_message: Complete rendered template message with placeholders replaced (optional)
        template_params: Parameters used to render the template (optional, stored as JSONB)
        message_id: Gupshup message ID (optional)

    Returns:
        True if logged successfully, False otherwise
    """
    if not is_template_logging_enabled():
        logger.debug("Template delivery logging is disabled, skipping")
        return False

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO template_delivery_logs (
                        client_id, phone_number, template_id, template_name, event_key, channel,
                        success, response_data, error_message, template_message, template_params,
                        message_id, sent_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW()
                    )
                """, strip_nul_bytes((
                    str(client_id),
                    phone_number,
                    template_id,
                    template_name,
                    event_key,
                    channel,
                    success,
                    json.dumps(strip_nul_bytes(response_data)) if response_data else None,
                    error_message,
                    template_message,
                    json.dumps(strip_nul_bytes(template_params)) if template_params else None,
                    message_id
                )))

                logger.debug(f"Template delivery logged: {template_id} ({template_name}) to {phone_number} - success={success}")
                return True

    except Exception as e:
        logger.error(f"Error logging template delivery: {e}")
        return False
