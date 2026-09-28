"""
Standalone summary worker for queued conversation summarization.

This worker reuses the same queue/lock/watermark contract implemented in
gupshup_webhook and can be run as a separate process.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fashion_bot.env_loader import bootstrap_environment, get_env
from fashion_bot.gupshup_webhook import _get_redis_client, _redis_guard, _get_runtime_support

bootstrap_environment()
logger = logging.getLogger("summary_worker")


async def _run_summary_worker(client_id: str):
    """
    Compatibility indirection for tests/monkeypatch while keeping implementation
    owned by runtime support.
    """
    await _get_runtime_support().run_summary_worker(client_id)


def _extract_client_id_from_summary_key(redis_key: str) -> Optional[str]:
    prefix = "summary:jobs:"
    if not redis_key or not redis_key.startswith(prefix):
        return None
    client_id = redis_key[len(prefix):].strip()
    return client_id or None


def list_clients_with_pending_summary_jobs(limit: int = 200) -> List[str]:
    """
    Discover client IDs that currently have summary queue keys.
    """
    rc = _get_redis_client()
    if not rc:
        return []

    result = _redis_guard.execute(
        op_name="summary_worker_scan_keys",
        fn=lambda: list(rc.scan_iter(match="summary:jobs:*", count=limit)),
        fallback=[],
    )
    if not result.ok:
        logger.warning("summary worker key scan degraded: %s", result.error)
        return []

    client_ids: List[str] = []
    for key in result.value or []:
        cid = _extract_client_id_from_summary_key(str(key))
        if cid and cid not in client_ids:
            client_ids.append(cid)
    return client_ids


async def process_summary_jobs_once(client_id: Optional[str] = None) -> int:
    """
    Process pending summary queues once.
    Returns number of client queues processed.
    """
    target_client_ids = [client_id] if client_id else list_clients_with_pending_summary_jobs()
    processed = 0
    for cid in target_client_ids:
        try:
            await _run_summary_worker(cid)
            processed += 1
        except Exception as err:
            logger.warning("summary worker failed for client=%s err=%s", cid, err)
    return processed


async def run_summary_worker_forever(
    poll_seconds: Optional[float] = None,
    client_id: Optional[str] = None,
) -> None:
    """
    Poll and process queued summary jobs forever.
    """
    sleep_seconds = poll_seconds
    if sleep_seconds is None:
        sleep_seconds = float(get_env("SUMMARY_WORKER_POLL_SECONDS") or "2")

    logger.info("summary worker started: poll=%.2fs client_id=%s", sleep_seconds, client_id or "ALL")
    while True:
        processed = await process_summary_jobs_once(client_id=client_id)
        if processed == 0:
            await asyncio.sleep(sleep_seconds)
