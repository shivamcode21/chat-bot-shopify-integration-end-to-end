"""
Utility to add template messages to conversations.
This ensures template messages sent via webhooks are reflected in the conversation history.

Updated to support conversation classification system:
- Creates template_initiated conversations (non-billable)
- Marks conversations as awaiting customer response
"""

import logging
from typing import Optional
from fashion_bot.history.conversation_classification import acreate_template_conversation

logger = logging.getLogger(__name__)


async def aadd_template_to_conversation(
    client_id: str,
    phone_number: str,
    template_message: str,
    channel_type: str = "whatsapp",
    conversation_id: Optional[str] = None,
    metadata: Optional[dict] = None,
    template_id: Optional[str] = None,
    template_name: Optional[str] = None,
) -> Optional[str]:
    """
    Add a sent template message to the conversation history (async).

    Creates template_initiated conversations (non-billable).
    Customer's reply will convert it to billable.
    """
    try:
        if not template_message:
            logger.warning(f"[CONV_MESSAGE] No template message to add for {phone_number}")
            return None

        logger.info(f"[CONV_MESSAGE] 📝 Adding template message to conversation")
        logger.info(f"[CONV_MESSAGE] Phone: {phone_number}, Client ID: {client_id}")
        logger.info(f"[CONV_MESSAGE] Message preview: {template_message[:100]}...")

        logger.info(f"[CONV_MESSAGE] 🆕 Creating template-initiated conversation")

        conv_id = await acreate_template_conversation(
            client_id=client_id,
            phone=phone_number,
            template_message=template_message,
            channel_type=channel_type,
            template_id=template_id,
            template_name=template_name
        )

        if conv_id:
            logger.info(f"[CONV_MESSAGE] ✅ Template-initiated conversation created: {conv_id}")
            logger.info(f"[CONV_MESSAGE]    Type: template_initiated (non-billable)")
            logger.info(f"[CONV_MESSAGE]    Will convert to billable if customer responds")
        else:
            logger.warning(f"[CONV_MESSAGE] ⚠️  Failed to create template conversation")

        return conv_id

    except Exception as e:
        logger.error(f"[CONV_MESSAGE] ❌ Error adding template to conversation: {e}", exc_info=True)
        return None


async def aadd_template_to_conversation_safe(**kwargs) -> Optional[str]:
    """Safe async wrapper that never raises exceptions."""
    try:
        return await aadd_template_to_conversation(**kwargs)
    except Exception as e:
        logger.error(f"[CONV_MESSAGE] ❌ Unexpected error in safe wrapper: {e}", exc_info=True)
        return None
