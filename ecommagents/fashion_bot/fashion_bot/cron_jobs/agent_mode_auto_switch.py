"""
Cron job to automatically switch inactive agent modes back to bot mode.

This handles the case where:
1. User escalates to agent (mode switches to 'agent')
2. No support responds
3. User drops off
4. User mode remains stuck in 'agent' mode

This cron runs every hour and switches users back to 'bot' mode if:
- Mode is 'agent'
- Last activity was more than 15 minutes ago
"""
import logging
from datetime import datetime, timedelta
from fashion_bot.database_manager import get_direct_postgres_cursor
from fashion_bot.monitoring.otel_metrics import track_cron
from fashion_bot.cron_jobs.cron_lock import (
    acquire_cron_lock,
    release_cron_lock,
    get_cron_lock_owner,
)

logger = logging.getLogger(__name__)

LOCK_KEY = "auto_switch_agent_modes_lock"
LOCK_TTL_SECONDS = 1800  # 30 minutes

@track_cron("auto_switch", items_key="switched_count")
async def auto_switch_inactive_agent_modes(inactivity_minutes: int = 15):
    """
    Switch users from 'agent' mode to 'bot' mode if they've been inactive.
    
    Args:
        inactivity_minutes: Number of minutes of inactivity before switching (default: 15)
        
    Returns:
        dict: {
            'success': bool,
            'switched_count': int,
            'message': str,
            'switched_users': list of {client_id, phone_number, last_activity}
        }
    """
    lease = None
    conn = None
    cur = None
    try:
        lease = await acquire_cron_lock(lock_key=LOCK_KEY, ttl_seconds=LOCK_TTL_SECONDS)
        if not lease:
            current_owner = await get_cron_lock_owner(LOCK_KEY)
            logger.info(
                "[AUTO_SWITCH_CRON] Auto-switch already running or lock backend unavailable. "
                "owner=%s",
                current_owner or "unknown",
            )
            return {
                'success': True,
                'skipped': True,
                'switched_count': 0,
                'message': 'Skipped: auto-switch already running',
                'switched_users': []
            }

        logger.info("[AUTO_SWITCH_CRON] Auto-switch lock acquired by %s", lease.owner_id)

        conn, cur = get_direct_postgres_cursor()
        
        # Calculate the cutoff time (15 minutes ago)
        cutoff_time = datetime.utcnow() - timedelta(minutes=inactivity_minutes)
        
        print(f"\n{'='*80}")
        print(f"[AUTO_SWITCH_CRON] Starting at {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"[AUTO_SWITCH_CRON] Cutoff time: {cutoff_time.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        print(f"[AUTO_SWITCH_CRON] Looking for users in 'agent' mode with last_activity < {cutoff_time}")
        print(f"{'='*80}\n")
        
        logger.info(f"[AUTO_SWITCH_CRON] Starting auto-switch cron job")
        logger.info(f"[AUTO_SWITCH_CRON] Cutoff time: {cutoff_time}")
        
        # First, find all users that need to be switched
        cur.execute("""
            SELECT client_id, phone_number, last_activity, 
                   EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC' - last_activity))/60 AS minutes_inactive
            FROM conversation_mode_human_agent_and_bot
            WHERE mode = 'agent'
            AND last_activity < %s
            ORDER BY last_activity ASC
        """, (cutoff_time,))
        
        users_to_switch = cur.fetchall()
        switched_count = len(users_to_switch)
        
        if switched_count == 0:
            print(f"[AUTO_SWITCH_CRON] ✅ No inactive agent modes found")
            logger.info(f"[AUTO_SWITCH_CRON] No users to switch")
            return {
                'success': True,
                'switched_count': 0,
                'message': 'No inactive agent modes found',
                'switched_users': []
            }
        
        # Log details of users being switched
        print(f"[AUTO_SWITCH_CRON] Found {switched_count} users to switch from 'agent' to 'bot' mode:\n")
        switched_users = []
        for user in users_to_switch:
            client_id = user['client_id']
            phone = user['phone_number']
            last_activity = user['last_activity']
            minutes_inactive = int(user['minutes_inactive'])
            
            masked_phone = f"{phone[:4]}***{phone[-3:]}" if len(phone) > 7 else phone
            
            print(f"  📱 {masked_phone} (Client: {client_id})")
            print(f"     Last activity: {last_activity.strftime('%Y-%m-%d %H:%M:%S')} UTC")
            print(f"     Inactive for: {minutes_inactive} minutes\n")
            
            logger.info(f"[AUTO_SWITCH_CRON] Switching user {masked_phone} (client: {client_id}) - inactive for {minutes_inactive} min")
            
            switched_users.append({
                'client_id': client_id,
                'phone_number': phone,
                'last_activity': last_activity.isoformat(),
                'minutes_inactive': minutes_inactive
            })
        
        # Update all users to 'bot' mode
        cur.execute("""
            UPDATE conversation_mode_human_agent_and_bot
            SET mode = 'bot',
                updated_at = (NOW() AT TIME ZONE 'UTC')
            WHERE mode = 'agent'
            AND last_activity < %s
        """, (cutoff_time,))
        
        print(f"[AUTO_SWITCH_CRON] ✅ Successfully switched {switched_count} users to 'bot' mode")
        print(f"{'='*80}\n")
        
        logger.info(f"[AUTO_SWITCH_CRON] Successfully switched {switched_count} users to 'bot' mode")

        return {
            'success': True,
            'switched_count': switched_count,
            'message': f'Successfully switched {switched_count} users from agent to bot mode',
            'switched_users': switched_users
        }
        
    except Exception as e:
        error_msg = f"Error in auto_switch_inactive_agent_modes: {e}"
        print(f"[AUTO_SWITCH_CRON] ❌ {error_msg}")
        logger.error(f"[AUTO_SWITCH_CRON] {error_msg}", exc_info=True)
        return {
            'success': False,
            'switched_count': 0,
            'message': error_msg,
            'switched_users': [],
            'error': str(e)
        }
    finally:
        # CRITICAL: Always close connection even if exception occurs
        if cur:
            try:
                cur.close()
            except:
                pass
        if conn:
            try:
                conn.close()
            except:
                pass
        if lease:
            try:
                await release_cron_lock(lease)
            except Exception as e:
                logger.warning("[AUTO_SWITCH_CRON] Failed to release auto-switch lock: %s", e)

if __name__ == "__main__":
    print("Manual script execution is disabled for async cron jobs. Use /cron/trigger-agent-switch.")
