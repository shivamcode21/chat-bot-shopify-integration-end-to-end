"""
Attribution Verification API Router
Second-opinion LLM check for borderline (llm_inferred) order conversion
attributions, called by the caller service's attribution_verification_service.py.

Internal-only: requires the X-Internal-Token header (see
fashion_bot.security.internal_auth), matched against its own dedicated
ATTRIBUTION_VERIFY_INTERNAL_TOKEN secret — the same header scheme as the
/cron/* trigger endpoints in agent_controller.py, but not the same token.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from fashion_bot.analytics.attribution_verification import averify_attribution
from fashion_bot.security.internal_auth import require_internal_token_from

logger = logging.getLogger("attribution_verification")

ATTRIBUTION_VERIFY_TOKEN_ENV_VAR = "ATTRIBUTION_VERIFY_INTERNAL_TOKEN"

router = APIRouter(
    prefix="/api/v1/attribution",
    tags=["Attribution Verification"],
    dependencies=[Depends(require_internal_token_from(ATTRIBUTION_VERIFY_TOKEN_ENV_VAR))],
)


class TranscriptMessage(BaseModel):
    side: str
    message: str
    tags: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None


class AttributionVerifyRequest(BaseModel):
    client_id: str
    conversation_id: Optional[str] = None
    order_number: Optional[str] = None
    conversion_detected_via: Optional[str] = None
    existing_llm_reasoning: Optional[str] = None
    transcript: List[TranscriptMessage] = Field(default_factory=list)


class AttributionVerifyResponse(BaseModel):
    verdict: str
    confidence: float
    reasoning: str


@router.post("/verify", response_model=AttributionVerifyResponse)
async def verify_attribution(payload: AttributionVerifyRequest) -> Dict[str, Any]:
    """
    Re-check a tentative, LLM-inferred order conversion attribution against
    the full transcript and return a confirm/reject verdict with confidence
    and reasoning.
    """
    logger.info(
        "[ATTRIBUTION_VERIFY] Request received: client_id=%s conversation_id=%s "
        "order_number=%s conversion_detected_via=%s transcript_messages=%d",
        payload.client_id,
        payload.conversation_id,
        payload.order_number,
        payload.conversion_detected_via,
        len(payload.transcript),
    )
    try:
        result = await averify_attribution(
            client_id=payload.client_id,
            conversation_id=payload.conversation_id,
            order_number=payload.order_number,
            conversion_detected_via=payload.conversion_detected_via,
            existing_llm_reasoning=payload.existing_llm_reasoning,
            transcript=[msg.model_dump() for msg in payload.transcript],
        )
        logger.info(
            "[ATTRIBUTION_VERIFY] Verdict for conversation_id=%s order_number=%s: "
            "verdict=%s confidence=%.2f reasoning=%s",
            payload.conversation_id,
            payload.order_number,
            result.get("verdict"),
            result.get("confidence", 0.0),
            result.get("reasoning"),
        )
        return result
    except Exception as exc:
        logger.error(
            "[ATTRIBUTION_VERIFY] Failed to verify attribution for conversation_id=%s "
            "order_number=%s; defaulting to rejected: %s",
            payload.conversation_id,
            payload.order_number,
            exc,
            exc_info=True,
        )
        # Always return the {verdict, confidence, reasoning} shape the
        # caller reads directly — never a bare error response — so a
        # failed verification degrades to a conservative "rejected"
        # instead of breaking the caller's parsing.
        return {
            "verdict": "rejected",
            "confidence": 0.0,
            "reasoning": f"Verification failed, defaulting to rejected: {exc}",
        }
