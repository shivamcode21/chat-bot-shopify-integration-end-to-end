"""
Slack slash-command router for on-demand PR review via Claude Code routines.

Setup:
1. Create a Slack App at https://api.slack.com/apps
2. Add a slash command: /review-pr
3. Set the Request URL to: https://<your-domain>/slack/commands
4. Copy the Signing Secret → set env var SLACK_SIGNING_SECRET
5. Set env vars ANTHROPIC_ROUTINE_TOKEN and ANTHROPIC_ROUTINE_TRIGGER_ID

Env vars:
    SLACK_SIGNING_SECRET          – Slack app signing secret (for request verification)
    ANTHROPIC_ROUTINE_TOKEN       – Per-routine Bearer token (sk-ant-oat01-...)
    ANTHROPIC_ROUTINE_TRIGGER_ID  – Routine trigger ID (trig_...)
    GITHUB_REPO_SLUG              – GitHub repo slug, default: groovee-ecomm-agents/ecommagents
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Request, HTTPException

logger = logging.getLogger("slack_commands")
router = APIRouter(tags=["Slack Commands"])

SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
ANTHROPIC_ROUTINE_TOKEN = os.environ.get("ANTHROPIC_ROUTINE_TOKEN", "")
ANTHROPIC_ROUTINE_TRIGGER_ID = os.environ.get(
    "ANTHROPIC_ROUTINE_TRIGGER_ID", "trig_01G38CdqwDiWFuBx6reyDYi7"
)
GITHUB_REPO_SLUG = os.environ.get("GITHUB_REPO_SLUG", "groovee-ecomm-agents/ecommagents")

ANTHROPIC_FIRE_URL = (
    f"https://api.anthropic.com/v1/claude_code/routines/{ANTHROPIC_ROUTINE_TRIGGER_ID}/fire"
)


def _verify_slack_signature(request_body: bytes, timestamp: str, signature: str) -> bool:
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not set — skipping verification")
        return True
    if abs(time.time() - int(timestamp)) > 300:
        return False
    base = f"v0:{timestamp}:{request_body.decode('utf-8')}"
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), base.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


def _parse_pr_number(text: str) -> Optional[int]:
    """Extract PR number from slash command text.

    Accepts:
        498
        #498
        https://github.com/org/repo/pull/498
    """
    text = text.strip().lstrip("#")
    if text.isdigit():
        return int(text)
    if "/pull/" in text:
        try:
            return int(text.split("/pull/")[-1].split("/")[0].split("?")[0])
        except (ValueError, IndexError):
            return None
    return None


@router.post("/slack/commands")
async def handle_slack_command(request: Request):
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
    signature = request.headers.get("X-Slack-Signature", "")

    if SLACK_SIGNING_SECRET and not _verify_slack_signature(body, timestamp, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")

    form = await request.form()
    command = form.get("command", "")
    text = form.get("text", "").strip()
    user_name = form.get("user_name", "unknown")
    channel_name = form.get("channel_name", "unknown")

    logger.info("Slack command=%s text=%s user=%s channel=%s", command, text, user_name, channel_name)

    if command not in ("/review-pr", "/review_pr"):
        return {"response_type": "ephemeral", "text": f"Unknown command: {command}"}

    pr_number = _parse_pr_number(text)
    if not pr_number:
        return {
            "response_type": "ephemeral",
            "text": "Usage: `/review-pr <PR number or URL>`\nExamples:\n• `/review-pr 498`\n• `/review-pr #498`\n• `/review-pr https://github.com/org/repo/pull/498`",
        }

    if not ANTHROPIC_ROUTINE_TOKEN:
        logger.error("ANTHROPIC_ROUTINE_TOKEN not configured")
        return {"response_type": "ephemeral", "text": "PR review is not configured (missing API token)."}

    pr_url = f"https://github.com/{GITHUB_REPO_SLUG}/pull/{pr_number}"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                ANTHROPIC_FIRE_URL,
                headers={
                    "Authorization": f"Bearer {ANTHROPIC_ROUTINE_TOKEN}",
                    "Content-Type": "application/json",
                    "anthropic-beta": "experimental-cc-routine-2026-04-01",
                    "anthropic-version": "2023-06-01",
                },
                json={"text": f"Review this PR: {pr_url}"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("Anthropic routine fire failed: %s %s", exc.response.status_code, exc.response.text)
        return {
            "response_type": "ephemeral",
            "text": f"Failed to trigger review (HTTP {exc.response.status_code}). Check logs.",
        }
    except Exception as exc:
        logger.error("Anthropic routine fire error: %s", exc, exc_info=True)
        return {"response_type": "ephemeral", "text": f"Failed to trigger review: {exc}"}

    session_url = data.get("claude_code_session_url", "")
    session_id = data.get("claude_code_session_id", "")

    return {
        "response_type": "in_channel",
        "text": (
            f"*PR Review triggered* by @{user_name}\n"
            f"• PR: <{pr_url}|#{pr_number}>\n"
            f"• Session: <{session_url}|{session_id}>"
        ),
    }
