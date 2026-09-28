"""
Conversation Resolution Manager - Modular system to mark conversations as resolved/unresolved.
Only processes conversations that have ended (>90 min inactive).
"""
import os
import logging
import json
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from fashion_bot.database_manager import get_postgres_connection, get_direct_postgres_cursor
from fashion_bot.rollbar_config import report_error

logger = logging.getLogger(__name__)

# Configuration - can be disabled via environment variable
RESOLUTION_TRACKING_ENABLED = os.getenv("CONVERSATION_RESOLUTION_ENABLED", "true").lower() == "true"
INACTIVITY_THRESHOLD_MINUTES = int(os.getenv("CONVERSATION_RESOLUTION_THRESHOLD_MINUTES", "90"))
LOOP_DETECTION_ENABLED = os.getenv("CONVERSATION_LOOP_DETECTION_ENABLED", "true").lower() == "true"


def is_resolution_tracking_enabled() -> bool:
    """Check if resolution tracking is enabled"""
    return RESOLUTION_TRACKING_ENABLED


def has_escalation_tag(tags: Optional[List[str]]) -> bool:
    """
    Check if conversation has escalation-related tags.
    
    Args:
        tags: List of conversation tags
        
    Returns:
        True if escalation tag found, False otherwise
    """
    if not tags:
        return False
    
    escalation_tags = {'escalated', 'agent_requested', 'escalation', 'human_agent', 'agent'}
    return bool(set(tags) & escalation_tags)


def detect_conversation_loop_llm(conversation_id: str) -> Dict[str, Any]:
    """
    Detect if conversation has a loop using LLM analysis.
    Analyzes last 10 messages to understand if conversation is stuck.
    
    Args:
        conversation_id: The conversation ID
        
    Returns:
        Dict with keys: is_loop (bool), confidence (str), reason (str)
    """
    conn = None
    cur = None
    try:
        # Use direct connection for background analysis
        conn, cur = get_direct_postgres_cursor()
        
        # Get last 10 messages (both user and bot)
        cur.execute("""
            SELECT message, message_side, created_at FROM messages
            WHERE conversation_id = %s 
            ORDER BY created_at DESC
            LIMIT 10
        """, (conversation_id,))
        
        rows = cur.fetchall()
        
        if len(rows) < 4:
            return {"is_loop": False, "confidence": "low", "reason": "Too few messages to analyze"}
        
        # Build conversation history for LLM (reverse to chronological order)
        conversation_history = []
        for row in reversed(rows):
            # 🛡️ Access by keys since get_direct_postgres_cursor uses dict_row
            message = row.get("message", "")
            side = row.get("message_side", "")
            created_at = row.get("created_at")
            
            role = "Bot" if side == "system_to_user" else "User"
            conversation_history.append(f"{role}: {message}")
        
        conversation_text = "\n".join(conversation_history)
        
        # Use LLM to detect loop. Resolve per-call (not via the
        # fashion_bot.llm_config shim) so llm.* metrics get tagged with
        # caller="loop_detection" instead of the singleton's permanent
        # "unknown".
        from fashion_bot.core.llm_factory import LLMFactory
        llm = LLMFactory.get_llm(tool_name="loop_detection")

        loop_detection_prompt = f"""Analyze the following conversation to determine if the bot is stuck in an EGREGIOUS loop.

Conversation History (last 10 messages):
{conversation_text}

**IMPORTANT**: Only mark as loop if it's VERY CLEAR and OBVIOUS.

An EGREGIOUS loop occurs when:
1. The bot gives THE EXACT SAME or NEARLY IDENTICAL response 3+ times in a row
2. The bot ignores user's questions/responses and keeps repeating the same thing
3. The conversation is completely stuck with no progress after multiple attempts
4. The bot is clearly malfunctioning and repeating itself

NOT a loop (mark as FALSE):
1. Bot gives similar but contextually appropriate responses to similar questions
2. Conversation naturally progresses even if some messages are similar
3. Bot is correctly asking for required information (like order ID)
4. User repeats questions and bot gives appropriate similar answers
5. Conversation has fewer than 6 messages total
6. Conversation shows ANY progress or variety in bot responses
7. Bot ends with a resolution or clear next step

**BE LENIENT**: If there's any doubt, mark it as NOT a loop. Only mark clear, obvious, egregious loops.

Respond in JSON format (ONLY JSON, no other text):
{{
  "is_loop": true/false,
  "confidence": "high/medium/low",
  "reason": "Brief explanation of why this is or isn't a loop"
}}"""

        try:
            response = llm.invoke(loop_detection_prompt)
            
            # Extract JSON from response
            response_text = response.content if hasattr(response, 'content') else str(response)
            
            # Try to find JSON in response
            json_match = re.search(r'\{[^}]+\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
                is_loop = result.get('is_loop', False)
                confidence = result.get('confidence', 'low')
                reason = result.get('reason', 'Unknown')
                
                logger.info(f"Loop detection for {conversation_id}: is_loop={is_loop}, confidence={confidence}, reason={reason}")
                
                return {
                    "is_loop": is_loop,
                    "confidence": confidence,
                    "reason": reason
                }
            else:
                logger.warning(f"Could not parse LLM response for loop detection: {response_text[:100]}")
                return {"is_loop": False, "confidence": "low", "reason": "Failed to parse LLM response"}
                
        except Exception as llm_error:
            logger.error(f"Error using LLM for loop detection: {llm_error}")
            report_error(
                "Error using LLM for loop detection",
                level='error',
                exc_info=(type(llm_error), llm_error, llm_error.__traceback__),
                conversation_id=conversation_id,
            )
            return {"is_loop": False, "confidence": "low", "reason": f"LLM error: {str(llm_error)}"}
                
    except Exception as e:
        logger.error(f"Error detecting conversation loop: {e}")
        report_error(
            "Error detecting conversation loop",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            conversation_id=conversation_id,
        )
        return {"is_loop": False, "confidence": "low", "reason": f"Error: {str(e)}"}
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def mark_conversation_resolved(
    conversation_id: str,
    resolved: bool = True,
    unresolved_reason: Optional[str] = None,
    is_loop: bool = False
) -> bool:
    """
    Mark a conversation as resolved or unresolved.
    
    Args:
        conversation_id: The conversation ID
        resolved: True for resolved, False for unresolved
        unresolved_reason: Reason if unresolved ('escalation' or 'loop')
        is_loop: Whether conversation had a loop
        
    Returns:
        True if successful, False otherwise
    """
    if not is_resolution_tracking_enabled():
        logger.debug("Resolution tracking is disabled, skipping")
        return False
    
    conn = None
    cur = None
    try:
        # Use direct connection for cron job processing
        conn, cur = get_direct_postgres_cursor()
        
        if resolved:
            # Mark as resolved (don't update updated_at to preserve UI ordering)
            cur.execute("""
                UPDATE conversations
                SET 
                    is_resolved = TRUE,
                    resolved_at = NOW(),
                    is_loop = %s,
                    unresolved_reason = NULL
                WHERE conversation_id = %s
                AND is_resolved IS NULL  -- Only update if not already marked
            """, (is_loop, conversation_id))
        else:
            # Mark as unresolved (don't update updated_at to preserve UI ordering)
            cur.execute("""
                UPDATE conversations
                SET 
                    is_resolved = FALSE,
                    resolved_at = NOW(),
                    is_loop = %s,
                    unresolved_reason = %s
                WHERE conversation_id = %s
                AND is_resolved IS NULL  -- Only update if not already marked
            """, (is_loop, unresolved_reason, conversation_id))
        
        rows_affected = cur.rowcount
        conn.commit()
        
        if rows_affected > 0:
            status = "resolved" if resolved else f"unresolved ({unresolved_reason})"
            logger.info(f"Marked conversation {conversation_id} as {status}")
            return True
        else:
            logger.debug(f"Conversation {conversation_id} already marked or not found")
            return False
            
    except Exception as e:
        logger.error(f"Error marking conversation resolution: {e}")
        report_error(
            "Error marking conversation resolution",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            conversation_id=conversation_id,
        )
        return False
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def process_ended_conversations(dry_run: bool = False) -> Dict[str, Any]:
    """
    Process all ended conversations (>90 min inactive) and mark them as resolved/unresolved.
    This is the main function called by the cron job.
    
    Args:
        dry_run: If True, only analyze without updating database
        
    Returns:
        Dict with processing statistics
    """
    if not is_resolution_tracking_enabled():
        logger.info("Resolution tracking is disabled")
        return {"enabled": False, "message": "Resolution tracking is disabled"}
    
    stats = {
        "processed": 0,
        "resolved": 0,
        "unresolved_escalation": 0,
        "unresolved_loop": 0,
        "skipped": 0,
        "errors": 0
    }
    
    conn = None
    cur = None
    try:
        # Use direct connection for cron job to bypass pool
        conn, cur = get_direct_postgres_cursor()
        
        # Find conversations that have ended (>90 min inactive) and not yet marked
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=INACTIVITY_THRESHOLD_MINUTES)
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        
        cur.execute("""
            SELECT 
                c.conversation_id,
                c.tags,
                c.phone,
                c.first_message,
                c.created_at,
                c.updated_at,
                COALESCE(MAX(m.created_at), c.updated_at) AS last_activity
            FROM conversations c
            LEFT JOIN messages m ON m.conversation_id = c.conversation_id
            WHERE 
                c.is_resolved IS NULL  -- Only process conversations not yet marked
                AND c.status = 'active'  -- Active conversations only
                AND c.created_at >= %s  -- Only today's conversations
            GROUP BY c.conversation_id, c.tags, c.phone, c.first_message, c.created_at, c.updated_at
            HAVING COALESCE(MAX(m.created_at), c.updated_at) < %s
            ORDER BY last_activity ASC
            LIMIT 500  -- Process more per batch
        """, (today_start, cutoff_time))
        
        conversations = cur.fetchall()
        
        print(f"\n{'='*80}", flush=True)
        print(f"[RESOLUTION] Found {len(conversations)} TODAY'S conversations to process (>90 min inactive)", flush=True)
        print(f"[RESOLUTION] Processing only conversations created today (since {today_start.strftime('%Y-%m-%d %H:%M UTC')})", flush=True)
        print(f"{'='*80}\n", flush=True)
        logger.info(f"Found {len(conversations)} ended conversations to process")
        
        for idx, row in enumerate(conversations, 1):
            # 🛡️ Access by keys since get_direct_postgres_cursor uses dict_row
            conversation_id = row.get("conversation_id")
            tags = row.get("tags")
            phone = row.get("phone")
            first_message = row.get("first_message")
            created_at = row.get("created_at")
            updated_at = row.get("updated_at")
            last_activity = row.get("last_activity")
            
            stats["processed"] += 1
            
            # Convert UUID to string if needed
            conv_id_str = str(conversation_id)
            
            # 🛡️ DEFENSIVE: Ensure last_activity is a datetime object
            if isinstance(last_activity, str):
                try:
                    # Try common ISO formats
                    if 'T' in last_activity:
                        last_activity = datetime.fromisoformat(last_activity.replace('Z', '+00:00'))
                    else:
                        last_activity = datetime.strptime(last_activity, '%Y-%m-%d %H:%M:%S.%f')
                except Exception as e:
                    logger.warning(f"Could not parse last_activity string '{last_activity}': {e}")
                    # Fallback to created_at if possible
                    last_activity = created_at if isinstance(created_at, datetime) else datetime.now(timezone.utc)
            
            # Ensure last_activity is timezone-aware
            if last_activity and last_activity.tzinfo is None:
                last_activity = last_activity.replace(tzinfo=timezone.utc)
            
            minutes_inactive = (datetime.now(timezone.utc) - last_activity).total_seconds() / 60
            
            print(f"[{idx}/{len(conversations)}] Processing conversation {conv_id_str[:8]}...", flush=True)
            print(f"  Phone: {phone}", flush=True)
            print(f"  Inactive for: {minutes_inactive:.0f} minutes", flush=True)
            print(f"  Tags: {tags or 'None'}", flush=True)
            
            try:
                # Check for escalation
                if has_escalation_tag(tags):
                    print(f"  ❌ Has ESCALATION tag → Marking as UNRESOLVED", flush=True)
                    logger.info(f"Conversation {conversation_id} has escalation tag → UNRESOLVED")
                    if not dry_run:
                        mark_conversation_resolved(
                            conversation_id=conversation_id,
                            resolved=False,
                            unresolved_reason='escalation',
                            is_loop=False
                        )
                    stats["unresolved_escalation"] += 1
                    print(flush=True)  # Empty line
                    continue
                
                # Check for loop using LLM (only mark as unresolved if HIGH confidence)
                # If LLM check fails or is disabled, default to marking as RESOLVED
                loop_detected = False
                
                if LOOP_DETECTION_ENABLED:
                    try:
                        print(f"  🔍 Checking for conversation loops...", flush=True)
                        loop_result = detect_conversation_loop_llm(conversation_id)
                        
                        if loop_result["is_loop"] and loop_result["confidence"] == 'high':
                            loop_detected = True
                            print(f"  ❌ LOOP detected (high confidence) → Marking as UNRESOLVED", flush=True)
                            print(f"     Reason: {loop_result['reason']}", flush=True)
                            logger.info(f"Loop detected in conversation {conversation_id}: {loop_result['reason']} → UNRESOLVED")
                        else:
                            print(f"  ✅ No loop detected", flush=True)
                    except Exception as loop_error:
                        print(f"  ⚠️ Loop detection failed (will mark as RESOLVED): {loop_error}", flush=True)
                        print(f"  ⚠️ Loop error type: {type(loop_error).__name__}", flush=True)
                        
                        # Print traceback for loop errors
                        import traceback
                        print(f"  ⚠️ Loop detection traceback:", flush=True)
                        traceback.print_exc()
                        
                        logger.warning(f"Loop detection failed for {conversation_id}: {loop_error}", exc_info=True)
                else:
                    print(f"  ⏭️  Loop detection disabled (skipping)", flush=True)
                
                # Mark conversation based on loop detection result
                if loop_detected:
                    if not dry_run:
                        mark_conversation_resolved(
                            conversation_id=conversation_id,
                            resolved=False,
                            unresolved_reason='loop',
                            is_loop=True
                        )
                    stats["unresolved_loop"] += 1
                else:
                    # No escalation, no loop → Resolved
                    print(f"  ✅ No issues found → Marking as RESOLVED", flush=True)
                    logger.info(f"Conversation {conversation_id} resolved successfully → RESOLVED")
                    if not dry_run:
                        success = mark_conversation_resolved(
                            conversation_id=conversation_id,
                            resolved=True,
                            is_loop=False
                        )
                        if not success:
                            print(f"  ⚠️ Failed to mark in database", flush=True)
                            logger.warning(f"Failed to mark conversation {conversation_id} as resolved")
                    stats["resolved"] += 1
                
                print(flush=True)  # Empty line for readability
                    
            except Exception as conv_error:
                print(f"  ⚠️ ERROR: {conv_error}", flush=True)
                print(f"  ⚠️ ERROR TYPE: {type(conv_error).__name__}", flush=True)
                
                # Print full traceback
                import traceback
                print(f"  ⚠️ FULL TRACEBACK:", flush=True)
                traceback.print_exc()
                
                logger.error(f"Error processing conversation {conversation_id}: {conv_error}", exc_info=True)
                report_error(
                    "Error processing conversation",
                    level='error',
                    exc_info=(type(conv_error), conv_error, conv_error.__traceback__),
                    conversation_id=str(conversation_id) if conversation_id else None,
                )
                stats["errors"] += 1
                print(flush=True)
        
        logger.info(f"Processing complete: {stats}")
        return stats
                
    except Exception as e:
        print(f"\n❌ FATAL ERROR in process_ended_conversations: {e}")
        print(f"❌ ERROR TYPE: {type(e).__name__}")
        
        import traceback
        print(f"❌ FULL TRACEBACK:")
        traceback.print_exc()
        
        logger.error(f"Error in process_ended_conversations: {e}", exc_info=True)
        stats["errors"] += 1
        return stats
    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()


def get_resolution_stats(client_id: Optional[str] = None, days: int = 30) -> Dict[str, Any]:
    """
    Get resolution statistics.
    
    Args:
        client_id: Optional client ID filter
        days: Number of days to look back
        
    Returns:
        Dictionary with statistics
    """
    try:
        with get_postgres_connection() as conn:
            with conn.cursor() as cur:
                where_clause = "WHERE created_at > NOW() - INTERVAL '%s days'"
                params = [days]
                
                if client_id:
                    where_clause += " AND client_id = %s"
                    params.append(client_id)
                
                cur.execute(f"""
                    SELECT 
                        COUNT(*) FILTER (WHERE is_resolved = TRUE) as resolved,
                        COUNT(*) FILTER (WHERE is_resolved = FALSE AND unresolved_reason = 'escalation') as escalated,
                        COUNT(*) FILTER (WHERE is_resolved = FALSE AND unresolved_reason = 'loop') as looped,
                        COUNT(*) FILTER (WHERE is_resolved = FALSE) as unresolved_total,
                        COUNT(*) FILTER (WHERE is_resolved IS NULL) as not_yet_marked,
                        COUNT(*) as total
                    FROM conversations
                    {where_clause}
                """, tuple(params))
                
                row = cur.fetchone()
                if row:
                    # 🛡️ Access by keys since get_postgres_connection uses dict_row
                    resolved_count = row.get("resolved", 0)
                    total_count = row.get("total", 0)
                    
                    return {
                        "resolved": resolved_count,
                        "escalated": row.get("escalated", 0),
                        "looped": row.get("looped", 0),
                        "unresolved_total": row.get("unresolved_total", 0),
                        "not_yet_marked": row.get("not_yet_marked", 0),
                        "total": total_count,
                        "resolution_rate_pct": round(100.0 * resolved_count / total_count, 2) if total_count > 0 else 0
                    }
                
                return {}
                
    except Exception as e:
        logger.error(f"Error getting resolution stats: {e}")
        report_error(
            "Error getting resolution stats",
            level='error',
            exc_info=(type(e), e, e.__traceback__),
            client_id=client_id,
        )
        return {}

