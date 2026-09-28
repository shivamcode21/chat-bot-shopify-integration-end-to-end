"""
Sliding-window HTTP rate limiting (Redis). Skips webhooks and OPTIONS.
"""

from __future__ import annotations

import logging
import os
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from fashion_bot.env_loader import get_bool, get_int
from fashion_bot.security.sliding_window_redis import sliding_window_allow

logger = logging.getLogger(__name__)

DEFAULT_PREFIX = os.getenv("RATE_LIMIT_REDIS_PREFIX", "rl:http:")


def _client_ip(scope: Scope) -> str:
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
    xff = headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip() or "unknown"
    client = scope.get("client")
    if client and len(client) >= 1:
        return client[0] or "unknown"
    return "unknown"


def _skip_rate_limit(path: str, method: str) -> bool:
    if method == "OPTIONS":
        return True
    prefixes = (
        "/webhook",
        "/order/",
        "/product/",
        "/shipping/",
        "/gupshup",
        "/cron/",
        "/static/",
        "/widget/health",
    )
    if path in ("/health", "/docs", "/openapi.json", "/redoc"):
        return True
    return any(path.startswith(p) for p in prefixes)


class SlidingWindowRateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not get_bool("ENABLE_HTTP_RATE_LIMIT", True):
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        method = (scope.get("method") or "").upper()
        if _skip_rate_limit(path, method):
            await self.app(scope, receive, send)
            return

        window = float(get_int("RATE_LIMIT_WINDOW_SEC", 60))
        max_req = get_int("RATE_LIMIT_MAX_REQUESTS", 200)
        ip = _client_ip(scope)
        key = f"{DEFAULT_PREFIX}{ip}:{method}:{path[:80]}"

        allowed, retry_after = await sliding_window_allow(
            key,
            window_seconds=window,
            max_requests=max_req,
            fail_open=get_bool("RATE_LIMIT_FAIL_OPEN", True),
        )
        if not allowed:
            logger.warning("Rate limited ip=%s path=%s", ip, path)
            headers = {}
            if retry_after is not None:
                headers["Retry-After"] = str(retry_after)
            resp = JSONResponse(
                {"detail": "Too many requests"},
                status_code=429,
                headers=headers,
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)
