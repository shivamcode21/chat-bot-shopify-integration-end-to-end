"""
Cancellation Aversion — LLM Classifier (Background Job)
=========================================================
Runs periodically (every 5 minutes by default) to classify pending
cancellation aversion events whose sessions have expired (> 90 min old).

These are cases where no hard tool outcome signal was detected in real-time —
e.g. the bot persuaded the customer with pure text, the customer shifted topic,
or the session just dropped off.  The LLM reads the full conversation snapshot
and assigns a final verdict.

Start the background job from your app startup:
    from fashion_bot.analytics.cancellation_classifier import start_background_classifier
    asyncio.create_task(start_background_classifier())

Or trigger a one-shot classification run:
    from fashion_bot.analytics.cancellation_classifier import run_pending_classifications
    await run_pending_classifications()
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
    is_connection_error,
)

logger = logging.getLogger("cancellation_classifier")

# How long to wait between classifier sweeps (seconds)
CLASSIFIER_INTERVAL_SECONDS: int = 300  # 5 minutes

# Session expiry threshold: events older than this are eligible for LLM classification
SESSION_EXPIRY_MINUTES: int = 90

# Max events to classify in one batch (avoids runaway LLM spend)
BATCH_SIZE: int = 20

# ─── Prompts ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You are an expert analyst for an e-commerce customer support chatbot. "
    "Your task is to determine whether a customer's cancellation intent was "
    "ultimately averted, completed, or abandoned, based on the conversation provided."
)

_USER_PROMPT_TEMPLATE = """Analyse the following customer support conversation.

Conversation:
{conversation_text}

Additional signals observed during the conversation:
- Intermediate signals: {intermediate_signals}
- Tools the bot called: {tools_called}

Your task:
1. Confirm whether the customer expressed an intent to cancel an order.
2. Determine the final outcome.

Respond with ONLY a valid JSON object matching this exact schema (no markdown, no explanation outside JSON):
{{
  "cancellation_intent_confirmed": true or false,
  "verdict": "averted" or "cancelled" or "abandoned" or "unclear",
  "resolution": "llm_persuasion" or "context_shift" or "implicit_drop" or "exchange" or "return" or "address_fix" or "product_change" or null,
  "aversion_method": "tool_driven" or "llm_driven" or null,
  "confidence": 0.0 to 1.0,
  "reasoning": "one concise sentence"
}}

Definitions:
  verdict=averted     → customer had cancel intent but order was NOT cancelled
                         (they changed mind, accepted an alternative, or dropped the topic)
  verdict=cancelled   → cancellation was confirmed by customer and likely processed
  verdict=abandoned   → customer raised cancellation, bot responded, customer went silent
  verdict=unclear     → cannot determine with reasonable confidence

  resolution (only when verdict=averted):
    llm_persuasion  → bot used pure text to retain (no action tool was called)
    context_shift   → customer shifted to a completely different topic
    implicit_drop   → customer's replies implied acceptance without explicit confirmation
    exchange / return / address_fix / product_change → alternative was offered in text
                                                        but no action tool was logged

  aversion_method:
    tool_driven → a concrete action tool confirmed the alternative
    llm_driven  → pure conversation (no action tool confirmed it)
    null        → not applicable (cancelled / abandoned / unclear)
"""


# ─── Core classifier ─────────────────────────────────────────────────────────

def _format_conversation(snapshot: List[Dict]) -> str:
    """Format snapshot list into a readable string for the LLM prompt."""
    lines: List[str] = []
    for turn in snapshot:
        role = str(turn.get("role", "?")).capitalize()
        content = str(turn.get("content", "")).strip()
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no conversation recorded)"


async def classify_event(
    event_id: str,
    snapshot: List[Dict],
    intermediate_signals: List[str],
    tools_called: List[str],
    client_id: str,
) -> Optional[Dict]:
    """
    Run LLM classification on a single pending event.

    Returns the parsed verdict dict or None if classification fails.
    """
    conversation_text = _format_conversation(snapshot)
    prompt = _USER_PROMPT_TEMPLATE.format(
        conversation_text   = conversation_text,
        intermediate_signals = json.dumps(intermediate_signals),
        tools_called         = json.dumps(tools_called),
    )

    try:
        from fashion_bot.core.llm_factory import LLMInvoker
        raw = await LLMInvoker.ainvoke(
            prompt      = prompt,
            tool_name   = "cancellation_aversion_classifier",
            client_id   = client_id,
            system_prompt = _SYSTEM_PROMPT,
        )

        # Parse JSON — tolerate surrounding text / markdown fences
        raw = raw.strip()
        start = raw.find("{")
        end   = raw.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning(f"[CLASSIFIER] No JSON found in LLM response for event {event_id}")
            return None

        parsed: Dict = json.loads(raw[start:end])

        # Validate required fields
        required = {"verdict", "confidence"}
        if not required.issubset(parsed.keys()):
            logger.warning(f"[CLASSIFIER] Incomplete LLM response for event {event_id}: {parsed}")
            return None

        return parsed

    except json.JSONDecodeError as exc:
        logger.warning(f"[CLASSIFIER] JSON parse error for event {event_id}: {exc}")
    except Exception as exc:
        logger.error(f"[CLASSIFIER] LLM call failed for event {event_id}: {exc}", exc_info=True)

    return None


# ─── DB helpers ──────────────────────────────────────────────────────────────

@awith_retry
async def _fetch_pending_events(batch_size: int = BATCH_SIZE) -> List[Dict]:
    """
    Return up to `batch_size` events that are:
      - status = 'pending'
      - llm_classified = FALSE
      - intent_detected_at older than SESSION_EXPIRY_MINUTES
    """
    sql = """
        SELECT id::text, client_id::text,
               conversation_snapshot, intermediate_signals, tools_called
        FROM cancellation_aversion_events
        WHERE status          = 'pending'
          AND llm_classified  = FALSE
          AND intent_detected_at < NOW() - INTERVAL '{minutes} minutes'
        ORDER BY intent_detected_at ASC
        LIMIT {batch}
    """.format(minutes=SESSION_EXPIRY_MINUTES, batch=batch_size)

    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql)
                rows = await cur.fetchall()
                return [
                    {
                        "event_id":             r["id"],
                        "client_id":            r["client_id"],
                        "conversation_snapshot": r["conversation_snapshot"] or [],
                        "intermediate_signals":  r["intermediate_signals"] or [],
                        "tools_called":          r["tools_called"] or [],
                    }
                    for r in rows
                ]
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[CLASSIFIER] Failed to fetch pending events: {exc}")
        return []


@awith_retry
async def _apply_verdict(event_id: str, verdict: Dict) -> None:
    """Write the LLM verdict back to the DB row."""
    llm_verdict   = str(verdict.get("verdict", "unclear"))
    llm_confidence = float(verdict.get("confidence", 0.0))
    llm_reasoning  = str(verdict.get("reasoning", ""))[:1000]
    resolution     = verdict.get("resolution")
    aversion_method = verdict.get("aversion_method")

    # Map LLM verdict to DB status
    status_map = {
        "averted":   "averted",
        "cancelled": "cancelled",
        "abandoned": "abandoned",
        "unclear":   "abandoned",  # conservative fallback
    }
    db_status = status_map.get(llm_verdict, "abandoned")

    sql = """
        UPDATE cancellation_aversion_events
        SET status          = %s,
            resolution      = %s,
            aversion_method = %s,
            resolved_at     = CASE WHEN %s != 'pending' THEN NOW() ELSE resolved_at END,
            llm_classified  = TRUE,
            llm_verdict     = %s,
            llm_confidence  = %s,
            llm_reasoning   = %s,
            updated_at      = NOW()
        WHERE id = %s::uuid
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    db_status, resolution, aversion_method,
                    db_status,
                    llm_verdict, llm_confidence, llm_reasoning,
                    event_id,
                ))
    except Exception as exc:
        if is_connection_error(exc):
            raise
        logger.error(f"[CLASSIFIER] Failed to write verdict for event {event_id}: {exc}")


# ─── Batch runner ─────────────────────────────────────────────────────────────

async def run_pending_classifications(batch_size: int = BATCH_SIZE) -> int:
    """
    Fetch expired pending events and run LLM classification on each.

    Returns the number of events processed.
    """
    events = await _fetch_pending_events(batch_size)
    if not events:
        logger.debug("[CLASSIFIER] No pending events to classify.")
        return 0

    logger.info(f"[CLASSIFIER] Classifying {len(events)} pending event(s).")
    processed = 0

    for event in events:
        event_id   = event["event_id"]
        client_id  = event["client_id"]
        snapshot   = event["conversation_snapshot"]
        signals    = event["intermediate_signals"]
        tools      = event["tools_called"]

        verdict = await classify_event(
            event_id             = event_id,
            snapshot             = snapshot,
            intermediate_signals = signals,
            tools_called         = tools,
            client_id            = client_id,
        )

        if verdict:
            await _apply_verdict(event_id, verdict)
            logger.info(
                f"[CLASSIFIER] Event {event_id} → "
                f"{verdict.get('verdict')} / {verdict.get('resolution')} "
                f"(confidence={verdict.get('confidence', 0):.2f})"
            )
        else:
            # LLM failed — mark as abandoned so it doesn't loop forever
            await _apply_verdict(event_id, {
                "verdict":        "unclear",
                "resolution":     None,
                "aversion_method": None,
                "confidence":     0.0,
                "reasoning":      "LLM classification failed or returned invalid response.",
            })
            logger.warning(f"[CLASSIFIER] Event {event_id} → marked unclear (LLM failed).")

        processed += 1
        # Small yield between events to keep event loop responsive
        await asyncio.sleep(0.05)

    logger.info(f"[CLASSIFIER] Batch complete — {processed} event(s) classified.")
    return processed


# ─── Periodic background task ─────────────────────────────────────────────────

async def start_background_classifier(
    interval_seconds: int = CLASSIFIER_INTERVAL_SECONDS,
) -> None:
    """
    Infinite loop that runs run_pending_classifications() every `interval_seconds`.

    Start via asyncio.create_task() from your app startup event:

        @app.on_event("startup")
        async def startup():
            asyncio.create_task(start_background_classifier())
    """
    logger.info(
        f"[CLASSIFIER] Background job started — interval={interval_seconds}s, "
        f"session_expiry={SESSION_EXPIRY_MINUTES}min, batch={BATCH_SIZE}."
    )
    while True:
        try:
            await run_pending_classifications()
        except Exception as exc:
            logger.error(f"[CLASSIFIER] Sweep error (non-fatal): {exc}", exc_info=True)
        await asyncio.sleep(interval_seconds)
