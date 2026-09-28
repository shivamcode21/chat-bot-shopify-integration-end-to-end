"""
Second-opinion LLM verification for borderline (llm_inferred) order
conversion attributions.

Called via POST /api/v1/attribution/verify by the caller service's
attribution_verification_service.py, which reads verdict/confidence/reasoning
directly from the response of averify_attribution().
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fashion_bot.core.llm_factory import LLMInvoker
from fashion_bot.monitoring.otel_metrics import request_client_id
from fashion_bot.prompts.attribution_verification_prompt import (
    SYSTEM_PROMPT,
    USER_PROMPT_TEMPLATE,
)

logger = logging.getLogger("attribution_verification")

VALID_VERDICTS = {"confirmed", "rejected"}


def _format_transcript(transcript: List[Dict[str, Any]]) -> str:
    if not transcript:
        return "(empty transcript)"

    lines = []
    for msg in transcript:
        side = "Customer" if msg.get("side") == "user_to_system" else "Bot"
        tags = msg.get("tags") or []
        tag_suffix = f" [{', '.join(tags)}]" if tags else ""
        created_at = msg.get("created_at") or ""
        lines.append(f"[{created_at}] {side}{tag_suffix}: {msg.get('message', '')}")
    return "\n".join(lines)


async def averify_attribution(
    *,
    client_id: str,
    conversation_id: Optional[str],
    order_number: Optional[str],
    conversion_detected_via: Optional[str],
    existing_llm_reasoning: Optional[str],
    transcript: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Ask the LLM to confirm or reject a tentative order-conversion attribution.

    Returns {"verdict": "confirmed" | "rejected", "confidence": float, "reasoning": str}.
    Raises ValueError if the LLM response could not be parsed as JSON.
    """
    user_prompt = USER_PROMPT_TEMPLATE.format(
        order_number=order_number or "(not provided)",
        conversion_detected_via=conversion_detected_via or "(unknown)",
        existing_llm_reasoning=existing_llm_reasoning or "(none provided)",
        transcript_text=_format_transcript(transcript),
    )

    with request_client_id(client_id):
        raw = await LLMInvoker.ainvoke(
            prompt=user_prompt,
            tool_name="attribution_verification",
            client_id=client_id,
            system_prompt=SYSTEM_PROMPT,
        )

    logger.debug(
        "[ATTRIBUTION_VERIFY] Raw LLM response for conversation_id=%s: %s",
        conversation_id,
        raw,
    )

    raw = (raw or "").strip()
    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start == -1 or end == 0:
        logger.warning(
            "[ATTRIBUTION_VERIFY] No JSON found in LLM response for conversation_id=%s",
            conversation_id,
        )
        raise ValueError("LLM did not return a JSON object")

    parsed: Dict[str, Any] = json.loads(raw[start:end])

    verdict = str(parsed.get("verdict") or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        logger.warning(
            "[ATTRIBUTION_VERIFY] Unexpected verdict %r for conversation_id=%s; defaulting to 'rejected'",
            verdict,
            conversation_id,
        )
        verdict = "rejected"

    try:
        confidence = float(parsed.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(parsed.get("reasoning") or "").strip() or "No reasoning provided."

    return {"verdict": verdict, "confidence": confidence, "reasoning": reasoning}
