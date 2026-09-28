# Cron Jobs

This directory contains automated background jobs that run on a schedule.

## Overview

The cron jobs are managed by APScheduler and run as background tasks within the FastAPI application.

## Available Cron Jobs

### 1. Agent Mode Auto-Switch

**File:** `agent_mode_auto_switch.py`

**Purpose:** Automatically switches users from 'agent' mode back to 'bot' mode when they've been inactive for more than 15 minutes.

**Problem it solves:**
- User escalates to agent (mode → 'agent')
- No support agent responds
- User drops off
- User mode remains stuck in 'agent' mode forever
- Next time user messages, they don't get bot responses

**Schedule:** Runs every hour at :00 minutes (1:00, 2:00, 3:00, etc.)

**Logic:**
```sql
UPDATE conversation_mode_human_agent_and_bot
SET mode = 'bot'
WHERE mode = 'agent'
AND last_activity < (NOW() - INTERVAL '15 minutes')
```

**Console Output:**
```
================================================================================
[AUTO_SWITCH_CRON] Starting at 2025-01-24 14:00:00 UTC
[AUTO_SWITCH_CRON] Cutoff time: 2025-01-24 13:45:00 UTC
[AUTO_SWITCH_CRON] Looking for users in 'agent' mode with last_activity < cutoff
================================================================================

[AUTO_SWITCH_CRON] Found 3 users to switch from 'agent' to 'bot' mode:

  📱 9876***210 (Client: abc-123)
     Last activity: 2025-01-24 13:30:00 UTC
     Inactive for: 30 minutes

  📱 9876***345 (Client: xyz-456)
     Last activity: 2025-01-24 13:40:00 UTC
     Inactive for: 20 minutes

[AUTO_SWITCH_CRON] ✅ Successfully switched 3 users to 'bot' mode
================================================================================
```

## Setup

### 1. Installation

The cron jobs automatically start when the FastAPI application starts:

```python
# In agent_controller.py
@app.on_event("startup")
async def startup_event():
    start_cron_scheduler()
```

### 2. Dependencies

Add to `requirements.txt`:
```
APScheduler==3.10.4
```

Install:
```bash
pip install APScheduler==3.10.4
```

## API Endpoints

### Check Cron Job Status

**GET** `/cron/status`

Get status of all scheduled cron jobs.

**Response:**
```json
{
  "success": true,
  "scheduler_running": true,
  "jobs": [
    {
      "id": "auto_switch_agent_modes",
      "name": "Auto-switch inactive agent modes to bot mode",
      "next_run_time": "2025-01-24T15:00:00+00:00",
      "trigger": "cron[hour='*', minute='0']"
    }
  ]
}
```

### Trigger Agent Switch Manually

**POST** `/cron/trigger-agent-switch`

Manually trigger the agent mode auto-switch without waiting for scheduled run.

**Response:**
```json
{
  "success": true,
  "switched_count": 3,
  "message": "Successfully switched 3 users from agent to bot mode",
  "switched_users": [
    {
      "client_id": "abc-123",
      "phone_number": "919876543210",
      "last_activity": "2025-01-24T13:30:00",
      "minutes_inactive": 30
    }
  ]
}
```

## Testing

### Test Manually

Run the cron job directly:

```bash
cd fashion_bot/cron_jobs
python agent_mode_auto_switch.py
```

### Test with Scheduler

```bash
python scheduler.py
```

This will start the scheduler and run for 2 minutes for testing.

### Trigger via API

```bash
# Check status
curl http://localhost:8000/cron/status

# Trigger manually
curl -X POST http://localhost:8000/cron/trigger-agent-switch
```

## Configuration

### Modify Inactivity Timeout

To change the 15-minute inactivity threshold:

```python
# In scheduler.py
_scheduler.add_job(
    func=lambda: auto_switch_inactive_agent_modes(inactivity_minutes=30),  # Changed to 30 min
    ...
)
```

### Modify Schedule

To run every 30 minutes instead of hourly:

```python
# In scheduler.py
_scheduler.add_job(
    func=auto_switch_inactive_agent_modes,
    trigger=CronTrigger(minute='*/30', timezone='UTC'),  # Every 30 minutes
    ...
)
```

### Common Schedules

```python
# Every hour
CronTrigger(hour='*', minute=0)

# Every 30 minutes  
CronTrigger(minute='*/30')

# Every day at 2 AM
CronTrigger(hour=2, minute=0)

# Every Monday at 9 AM
CronTrigger(day_of_week='mon', hour=9, minute=0)
```

## Monitoring

### Logs

Check application logs for cron job execution:

```bash
# Look for [AUTO_SWITCH_CRON] tags
grep "AUTO_SWITCH_CRON" logs/fashion_bot.log

# Check scheduler logs
grep "CRON_SCHEDULER" logs/fashion_bot.log
```

### Render Dashboard

In Render's logs, you'll see clear output:

```
[STARTUP] ✅ Cron scheduler started successfully
[AUTO_SWITCH_CRON] Starting at...
[AUTO_SWITCH_CRON] ✅ Successfully switched X users to 'bot' mode
```

## Adding New Cron Jobs

1. Create new file in `cron_jobs/` directory:

```python
# my_new_cron.py
def my_cron_function():
    """My cron job logic"""
    print("[MY_CRON] Running...")
    # Your logic here
```

2. Register in `scheduler.py`:

```python
from .my_new_cron import my_cron_function

# In start_cron_scheduler()
_scheduler.add_job(
    func=my_cron_function,
    trigger=CronTrigger(hour='*/6', timezone='UTC'),  # Every 6 hours
    id='my_cron_job',
    name='My Custom Cron Job'
)
```

## Troubleshooting

### Cron not running

Check if scheduler started:
```bash
curl http://localhost:8000/cron/status
```

### Cron running but not switching users

1. Check database connection
2. Verify users are actually in 'agent' mode:
```sql
SELECT * FROM conversation_mode_human_agent_and_bot WHERE mode = 'agent';
```
3. Check last_activity timestamps
4. Review logs for errors

### Manual trigger not working

Check application logs for errors and ensure database is accessible.

## Production Deployment

### Render

The cron will automatically start with your FastAPI app. No additional configuration needed.

### Heroku

Use Heroku Scheduler for hourly jobs as backup:
```bash
heroku addons:create scheduler:standard
heroku addons:open scheduler
```

Add command:
```bash
python -m fashion_bot.cron_jobs.agent_mode_auto_switch
```

## Safety Notes

- ✅ Uses UTC timezone for consistency
- ✅ Transaction-based (commits all changes together)
- ✅ Logs all switched users for audit trail
- ✅ Non-blocking (runs in background thread)
- ✅ Graceful error handling (won't crash app)
- ✅ Idempotent (safe to run multiple times)

## Support

For issues or questions, check:
1. Application logs: `grep "AUTO_SWITCH_CRON"`
2. API status endpoint: `GET /cron/status`
3. Database state: Check `conversation_mode_human_agent_and_bot` table

