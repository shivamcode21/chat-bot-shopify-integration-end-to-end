"""
Shared-secret verification for service-to-service internal endpoints.

Same X-Internal-Token header scheme already enforced on the cron trigger
endpoints in agent_controller.py (/cron/trigger-conversation-analytics,
/cron/invalidate-template-cache) — but each internal route group gets its
own env-var secret rather than sharing one token everywhere, so a leak or
rotation on one integration doesn't affect the others.

Usage:
    require_internal_token_from("ATTRIBUTION_VERIFY_INTERNAL_TOKEN")
as a route/router `dependencies=[Depends(...)]` entry.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request

INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def require_internal_token_from(env_var: str):
    """Build a FastAPI dependency that checks X-Internal-Token against `env_var`.

    Raises HTTPException(503) if the server has no token configured,
    401 if the caller sent none, 403 if it doesn't match.
    """

    async def _dependency(request: Request) -> None:
        expected_token = (os.getenv(env_var) or "").strip()
        if not expected_token:
            raise HTTPException(
                status_code=503,
                detail=f"{env_var} not configured on server",
            )

        provided_token = (request.headers.get(INTERNAL_TOKEN_HEADER) or "").strip()
        if not provided_token:
            raise HTTPException(status_code=401, detail=f"Missing {INTERNAL_TOKEN_HEADER} header")

        if not hmac.compare_digest(provided_token, expected_token):
            raise HTTPException(status_code=403, detail=f"Invalid {INTERNAL_TOKEN_HEADER}")

    return _dependency
