"""
Cancellation Aversion Classifier Job
=====================================
Runs every 1 hour to classify pending cancellation aversion events.

What it does:
- Finds pending cancellation aversion events older than 90 minutes
- Uses LLM to analyze conversations and determine final verdict
- Updates events with: averted/cancelled/abandoned status, resolution, aversion_method

These are cases where no hard tool outcome signal was detected in real-time —
e.g. the bot persuaded the customer with pure text, the customer shifted topic,
or the session just dropped off.
"""

import asyncio
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

def classify_pending_cancellation_events() -> Dict[str, Any]:
    """
    Classify pending cancellation aversion events using LLM.
    
    This is a synchronous wrapper that runs the async classification function.
    APScheduler requires synchronous functions, so we use asyncio.run() here.
    
    Returns:
        Dictionary with classification results
    """
    try:
        from fashion_bot.analytics.cancellation_classifier import run_pending_classifications
        
        logger.info("[CANCELLATION_CLASSIFIER_CRON] Starting classification sweep...")
        
        # Run async function in sync context
        count = asyncio.run(run_pending_classifications())
        
        result = {
            "success": True,
            "events_classified": count,
            "message": f"Classified {count} pending event(s)"
        }
        
        if count > 0:
            logger.info(f"[CANCELLATION_CLASSIFIER_CRON] ✅ Classified {count} pending event(s)")
        else:
            logger.debug("[CANCELLATION_CLASSIFIER_CRON] ℹ️  No pending events to classify")
        
        return result
        
    except Exception as exc:
        logger.error(f"[CANCELLATION_CLASSIFIER_CRON] ❌ Classification failed: {exc}", exc_info=True)
        return {
            "success": False,
            "events_classified": 0,
            "error": str(exc)
        }

