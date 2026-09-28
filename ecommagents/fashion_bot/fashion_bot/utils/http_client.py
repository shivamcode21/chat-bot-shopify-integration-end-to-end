from typing import Optional

import httpx


# Single process-wide async HTTP client. Safe because the FastAPI app and
# APScheduler crons share one event loop (AsyncIOScheduler binds to the
# running loop), so the client's transports/pools have a stable owner.
_shared_async_client: Optional[httpx.AsyncClient] = None


async def get_shared_async_http_client() -> httpx.AsyncClient:
    global _shared_async_client
    if _shared_async_client is None or _shared_async_client.is_closed:
        _shared_async_client = httpx.AsyncClient(follow_redirects=True)
    return _shared_async_client


async def close_shared_async_http_client() -> None:
    global _shared_async_client
    if _shared_async_client is None:
        return
    client = _shared_async_client
    _shared_async_client = None
    if not client.is_closed:
        await client.aclose()
