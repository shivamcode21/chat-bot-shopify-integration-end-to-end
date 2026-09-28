"""
Reusable Redis lock helpers for cron jobs.

Pattern:
- Acquire lock with SET key owner NX EX ttl
- Release lock with Lua compare-and-delete (owner-safe unlock)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Optional

from fashion_bot.utils.redis_client import get_shared_async_redis_client


_SAFE_UNLOCK_LUA = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""


@dataclass(frozen=True)
class CronLockLease:
    lock_key: str
    owner_id: str
    ttl_seconds: int


async def acquire_cron_lock(
    *,
    lock_key: str,
    ttl_seconds: int,
    owner_id: Optional[str] = None,
) -> Optional[CronLockLease]:
    """
    Acquire a Redis lock for a cron run.

    Returns:
        CronLockLease if acquired, else None.
    """
    redis_client = await get_shared_async_redis_client()
    if not redis_client:
        return None

    lease_owner = (owner_id or str(uuid.uuid4())).strip()
    if not lease_owner:
        lease_owner = str(uuid.uuid4())

    acquired = await redis_client.set(
        lock_key,
        lease_owner,
        nx=True,
        ex=max(1, int(ttl_seconds)),
    )

    if acquired == "OK" or acquired is True:
        return CronLockLease(lock_key=lock_key, owner_id=lease_owner, ttl_seconds=max(1, int(ttl_seconds)))
    return None


async def release_cron_lock(lease: CronLockLease) -> None:
    """Release lock only if current owner still matches."""
    redis_client = await get_shared_async_redis_client()
    if not redis_client:
        return
    await redis_client.eval(_SAFE_UNLOCK_LUA, 1, lease.lock_key, lease.owner_id)


async def get_cron_lock_owner(lock_key: str) -> Optional[str]:
    """Best-effort read of current lock owner for logging/diagnostics."""
    redis_client = await get_shared_async_redis_client()
    if not redis_client:
        return None
    owner = await redis_client.get(lock_key)
    return str(owner) if owner else None
