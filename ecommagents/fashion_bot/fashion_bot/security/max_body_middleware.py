"""
Reject HTTP request bodies larger than MAX_REQUEST_BYTES (default 2 MiB).

Uses Content-Length when present. Requests without Content-Length (chunked) are
passed through; clients should send Content-Length for strict enforcement.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from fashion_bot.env_loader import get_int

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES = 2 * 1024 * 1024


def _max_bytes() -> int:
    return max(1024, get_int("MAX_REQUEST_BYTES", DEFAULT_MAX_BYTES))


def _should_skip_path(path: str) -> bool:
    prefixes = (
        "/webhook",
        "/order/",
        "/product/",
        "/shipping/",
        "/gupshup",
    )
    return any(path.startswith(p) for p in prefixes)


class MaxBodySizeMiddleware:
    """Blocks requests whose Content-Length exceeds the limit (GET/HEAD skipped)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path") or ""
        if _should_skip_path(path):
            await self.app(scope, receive, send)
            return

        method = (scope.get("method") or "").upper()
        if method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        cl = headers.get("content-length")
        if cl:
            try:
                length = int(cl)
            except ValueError:
                await self.app(scope, receive, send)
                return
            limit = _max_bytes()
            if length > limit:
                logger.warning("Request body too large: %s > %s path=%s", length, limit, path)
                resp = JSONResponse(
                    {"detail": "Request body too large", "max_bytes": limit},
                    status_code=413,
                )
                await resp(scope, receive, send)
                return

        await self.app(scope, receive, send)
