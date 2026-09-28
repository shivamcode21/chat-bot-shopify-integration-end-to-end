"""
Shared client context using ContextVar for multi-client support.
This allows passing client_id implicitly across async operations and LangChain tools.
"""
from contextvars import ContextVar
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Shared ContextVar for client_id
client_id_context: ContextVar[Optional[str]] = ContextVar('client_id_context', default=None)


def set_client_id(client_id: Optional[str]) -> None:
    """Set the client_id in shared context."""
    client_id_context.set(client_id)


def get_client_id() -> Optional[str]:
    """Get the client_id from shared context.
    
    Returns:
        Client ID from context, or None if not set
    """
    try:
        client_id = client_id_context.get()
        logger.debug(f"📥 Retrieved client_id from shared context: {client_id}")
        return client_id
    except LookupError:
        logger.warning("⚠️ LookupError: client_id not set in context")
        return None
