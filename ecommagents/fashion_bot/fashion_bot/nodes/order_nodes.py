"""
Order-related utility functions.
"""

from typing import Optional
import json
import logging

logger = logging.getLogger("meta_nodes")


async def get_contact_details_message(client_id: Optional[str] = None) -> str:
    """
    Fetch contact details from client_configs table and format them for customer messages.

    Args:
        client_id: Optional client ID for multi-client support

    Returns:
        Formatted contact details string
    """
    try:
        from fashion_bot.config_manager import aget_config

        contact_data = await aget_config("vendor_contact_details", client_id=client_id)

        if contact_data:
            try:
                # If it's already a dict (from JSONB), use it directly
                if isinstance(contact_data, dict):
                    data = contact_data
                else:
                    # If it's a string, parse it as JSON
                    data = json.loads(contact_data)

                # Format the contact details
                support_email = data.get("support email id", "")
                support_phones = data.get("support phone numbers", "")

                contact_message = "📞 For immediate assistance, please contact our support team:"
                if support_email:
                    contact_message += f"\n📧 Email: {support_email}"
                if support_phones:
                    contact_message += f"\n📱 Phone: {support_phones}"

                return contact_message

            except (json.JSONDecodeError, TypeError, KeyError) as e:
                logger.warning(f"Error parsing contact details: {e}")
                return ""

        return ""

    except Exception as e:
        logger.warning(f"Error fetching contact details: {e}")
        return ""
