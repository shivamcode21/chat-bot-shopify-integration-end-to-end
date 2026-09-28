"""
Cron job to process ended conversations and mark them as resolved/unresolved.
"""
import logging
from datetime import datetime, timezone
from typing import Dict, Any

logger = logging.getLogger(__name__)


def process_conversation_resolutions() -> Dict[str, Any]:
    """
    Cron job to process ended conversations (>90 min inactive).
    Checks for escalation tags and loops, marks conversations accordingly.
    
    Returns:
        Dictionary with processing results
    """
    try:
        from fashion_bot.history.conversation_resolution import (
            process_ended_conversations,
            is_resolution_tracking_enabled
        )
        
        if not is_resolution_tracking_enabled():
            logger.info("[CRON_RESOLUTION] Resolution tracking is disabled")
            return {
                "success": False,
                "message": "Resolution tracking is disabled",
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        
        print("\n" + "="*80, flush=True)
        print(f"[CRON_RESOLUTION] Starting conversation resolution processing at {datetime.now(timezone.utc).isoformat()}", flush=True)
        print(f"[CRON_RESOLUTION] Processing TODAY'S conversations only (>90 min inactive)", flush=True)
        print("="*80, flush=True)
        
        # Process ended conversations
        stats = process_ended_conversations(dry_run=False)
        
        print(f"\n[CRON_RESOLUTION] ✅ Processing complete:", flush=True)
        print(f"  - Total processed: {stats.get('processed', 0)}", flush=True)
        print(f"  - Resolved: {stats.get('resolved', 0)}", flush=True)
        print(f"  - Unresolved (escalation): {stats.get('unresolved_escalation', 0)}", flush=True)
        print(f"  - Unresolved (loop): {stats.get('unresolved_loop', 0)}", flush=True)
        print(f"  - Errors: {stats.get('errors', 0)}", flush=True)
        print("="*80 + "\n", flush=True)
        
        logger.info(f"[CRON_RESOLUTION] Processed {stats.get('processed', 0)} conversations")
        
        return {
            "success": True,
            "stats": stats,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
    except Exception as e:
        error_msg = f"Error in conversation resolution cron job: {e}"
        logger.error(f"[CRON_RESOLUTION] {error_msg}")
        print(f"\n[CRON_RESOLUTION] ❌ {error_msg}\n", flush=True)
        return {
            "success": False,
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }


if __name__ == "__main__":
    # For testing
    logging.basicConfig(level=logging.INFO)
    result = process_conversation_resolutions()
    print(f"\nResult: {result}")

