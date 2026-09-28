#!/usr/bin/env bash
set -euo pipefail

# Runs a local Dramatiq worker against local Redis, then invokes the real
# conversation inactivity scheduler. This writes to the DATABASE_URL you pass.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${ROOT_DIR}"

if [[ "${CONFIRM_WRITE:-}" != "yes" ]]; then
  cat >&2 <<'EOF'
Refusing to run because this test performs real writes:
- conversation_inactivity_cursors rows are claimed/advanced
- messages/conversations may get tags
- conversation_leads may be inserted

Set CONFIRM_WRITE=yes to run intentionally.
EOF
  exit 1
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is required." >&2
  exit 1
fi

if ! command -v redis-cli >/dev/null 2>&1; then
  echo "redis-cli is required. Start/install Redis locally first." >&2
  exit 1
fi

if ! redis-cli ping >/dev/null 2>&1; then
  echo "Local Redis is not responding. Start it first, e.g. redis-server." >&2
  exit 1
fi

export PYTHONPATH="${APP_DIR}:${PYTHONPATH:-}"
export USE_DB_POOL="${USE_DB_POOL:-false}"
export WEBHOOK_QUEUE_ENABLED="${WEBHOOK_QUEUE_ENABLED:-true}"
export WEBHOOK_QUEUE_LANES="${WEBHOOK_QUEUE_LANES:-conversation_event}"
export DRAMATIQ_BROKER_URL="${DRAMATIQ_BROKER_URL:-redis://localhost:6379/0}"
export CONVERSATION_INACTIVITY_BATCH_SIZE="${CONVERSATION_INACTIVITY_BATCH_SIZE:-5}"
export CONVERSATION_INACTIVITY_CANDIDATE_SCAN_LIMIT="${CONVERSATION_INACTIVITY_CANDIDATE_SCAN_LIMIT:-20}"
export CONVERSATION_INACTIVITY_THRESHOLD_MINUTES="${CONVERSATION_INACTIVITY_THRESHOLD_MINUTES:-10}"
export CONVERSATION_INACTIVITY_OPEN_WINDOW_MINUTES="${CONVERSATION_INACTIVITY_OPEN_WINDOW_MINUTES:-90}"
export CONVERSATION_INACTIVITY_REQUEUE_AFTER_MINUTES="${CONVERSATION_INACTIVITY_REQUEUE_AFTER_MINUTES:-1}"

WORKER_LOG="${WORKER_LOG:-/tmp/conversation-inactivity-dramatiq-worker.log}"

cleanup() {
  if [[ -n "${WORKER_PID:-}" ]] && kill -0 "${WORKER_PID}" >/dev/null 2>&1; then
    kill "${WORKER_PID}" >/dev/null 2>&1 || true
    wait "${WORKER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "Starting local Dramatiq worker..."
: > "${WORKER_LOG}"
python3 -m dramatiq fashion_bot.workers.run \
  --queues events.conversations \
  --processes 1 \
  --threads 1 \
  > "${WORKER_LOG}" 2>&1 &
WORKER_PID=$!

sleep 5
if ! kill -0 "${WORKER_PID}" >/dev/null 2>&1; then
  echo "Worker failed to start. Logs:" >&2
  cat "${WORKER_LOG}" >&2
  exit 1
fi

echo "Running scheduler..."
python3 - <<'PY'
import asyncio
import json

from fashion_bot.cron_jobs.conversation_inactivity_scheduler import (
    emit_inactive_conversation_events,
)


async def main() -> None:
    result = await emit_inactive_conversation_events()
    print(json.dumps(result, default=str, indent=2))


asyncio.run(main())
PY

echo "Waiting for worker to process queued events..."
sleep "${WORKER_WAIT_SECONDS:-30}"

echo "Recent worker logs:"
tail -n "${WORKER_LOG_LINES:-120}" "${WORKER_LOG}" || true

echo "Local Redis queue depths:"
redis-cli llen dramatiq:events.conversations || true
redis-cli zcard dramatiq:events.conversations.XQ 2>/dev/null || true
redis-cli hlen dramatiq:events.conversations.XQ.msgs 2>/dev/null || true

if command -v psql >/dev/null 2>&1; then
  echo "Database cursor/lead status:"
  psql "${DATABASE_URL}" <<'SQL'
\pset pager off
SELECT status, COUNT(*)::bigint AS count
FROM conversation_inactivity_cursors
GROUP BY status
ORDER BY status;

SELECT
  conversation_id::text,
  status,
  retry_count,
  last_error,
  last_processed_inbound_message_at,
  last_processed_inbound_message_id::text,
  processed_at,
  updated_at
FROM conversation_inactivity_cursors
ORDER BY updated_at DESC
LIMIT 10;

SELECT
  id::text,
  conversation_id::text,
  client_id::text,
  phone_number,
  lead_type,
  lead_status,
  generated_at
FROM conversation_leads
WHERE source = 'inactive_conversation'
ORDER BY created_at DESC
LIMIT 10;
SQL
else
  echo "psql not found, skipping DB summary."
fi
