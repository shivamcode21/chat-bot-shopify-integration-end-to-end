"""
Sliding-window rate limit using Redis sorted sets (async).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional, Tuple

from fashion_bot.config_manager import _get_async_redis_client
from fashion_bot.env_loader import get_bool

logger = logging.getLogger(__name__)


async def sliding_window_allow(
    redis_key: str,
    *,
    window_seconds: float,
    max_requests: int,
    fail_open: bool = True,
) -> Tuple[bool, Optional[int]]:
    """
    Returns (allowed, retry_after_seconds_or_none).

    Uses ZSET: score = unix time in ms, member = unique id.
    """
    if not get_bool("ENABLE_REDIS_RATE_LIMIT", True):
        return True, None
    if max_requests <= 0:
        return True, None
    client = await _get_async_redis_client()
    if not client:
        if fail_open:
            return True, None
        return False, int(window_seconds) or 1

    now_ms = time.time() * 1000.0
    window_ms = float(window_seconds) * 1000.0
    cutoff = now_ms - window_ms

    try:
        pipe = client.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, 0, cutoff)
        pipe.zcard(redis_key)
        results = await pipe.execute()
        count_after_trim = int(results[1]) if len(results) > 1 else 0

        if count_after_trim >= max_requests:
            oldest = await client.zrange(redis_key, 0, 0, withscores=True)
            if oldest:
                oldest_ts_ms = float(oldest[0][1])
                retry_after = max(1, int((oldest_ts_ms + window_ms - now_ms) / 1000.0))
            else:
                retry_after = int(window_seconds) or 1
            return False, retry_after

        member = f"{now_ms}:{uuid.uuid4().hex}"
        await client.zadd(redis_key, {member: now_ms})
        await client.expire(redis_key, int(window_seconds) + 2)
        return True, None
    except Exception as e:
        logger.warning("sliding_window_allow redis error key=%s: %s", redis_key, e, exc_info=True)
        if fail_open:
            return True, None
        return False, int(window_seconds) or 1
