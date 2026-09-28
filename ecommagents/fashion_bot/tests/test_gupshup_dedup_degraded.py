import pytest
from fashion_bot.gupshup_webhook import _amark_message_processed, _redis_guard


class FailingRedisClient:
    async def set(self, *args, **kwargs):
        raise RuntimeError("redis down")


@pytest.mark.asyncio
async def test_dedup_fail_open_when_redis_errors(monkeypatch):
    async def _fake_async_redis():
        return FailingRedisClient()

    monkeypatch.setattr("fashion_bot.gupshup_webhook._get_async_redis_client", _fake_async_redis)
    # Keep guard strict for deterministic fail-open behavior
    _redis_guard.timeout_ms = 20
    _redis_guard.fail_threshold = 1
    allowed = await _amark_message_processed("client-1", "msg-1")
    assert allowed is True
