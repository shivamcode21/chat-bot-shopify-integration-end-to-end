"""
Centralized Rollbar configuration and initialization.
This module ensures Rollbar is initialized once and can be used across the entire codebase.
"""
import rollbar
import os
from dotenv import load_dotenv
from fashion_bot.utils.utils import get_current_environment

# Load environment variables from .env file
load_dotenv()

# Get Rollbar access token from environment variable
ROLLBAR_ACCESS_TOKEN = os.getenv("ROLLBAR_ACCESS_TOKEN") or os.environ.get("ROLLBAR_ACCESS_TOKEN")

# Track if Rollbar has been initialized
_rollbar_initialized = False

def init_rollbar():
    """
    Initialize Rollbar once. Safe to call multiple times - will only initialize once.
    """
    global _rollbar_initialized
    
    if _rollbar_initialized:
        return
    
    # Check if Rollbar token is available
    if not ROLLBAR_ACCESS_TOKEN:
        import logging
        logger = logging.getLogger(__name__)
        logger.warning("ROLLBAR_ACCESS_TOKEN not found in environment variables. Rollbar will not be initialized.")
        return
    
    try:
        rollbar.init(
            access_token=ROLLBAR_ACCESS_TOKEN,
            environment=get_current_environment(),
            code_version='1.0'
        )
        _rollbar_initialized = True
    except Exception as e:
        # If Rollbar initialization fails, log but don't crash
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to initialize Rollbar: {e}")


def report_error(message: str, level: str = 'error', exc_info=None, **kwargs):
    """
    Report an error to Rollbar.
    
    Args:
        message: Error message to report
        level: Error level ('error', 'warning', 'critical', 'info')
        exc_info: Exception info (from sys.exc_info() or exception object)
        **kwargs: Additional context data to include (will be passed as extra_data)
    """
    if not _rollbar_initialized:
        init_rollbar()
    
    # If Rollbar is not initialized (e.g., no token), skip reporting
    if not _rollbar_initialized:
        return
    
    try:
        # Prepare extra data from kwargs
        extra_data = kwargs if kwargs else {}
        
        if exc_info:
            # For exceptions, use report_exc_info with extra_data
            rollbar.report_exc_info(exc_info=exc_info, level=level, extra_data=extra_data)
        else:
            # For messages, use report_message with extra_data
            rollbar.report_message(message, level=level, extra_data=extra_data)
    except Exception as e:
        # If Rollbar reporting fails, log but don't crash
        import logging
        logger = logging.getLogger(__name__)
        logger.warning(f"Failed to report to Rollbar: {e}")


# Auto-initialize on import
init_rollbar()

