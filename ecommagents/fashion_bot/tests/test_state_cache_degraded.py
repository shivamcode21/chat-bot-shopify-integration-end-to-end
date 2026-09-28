import time

from langchain_core.messages import HumanMessage

from fashion_bot.schema import SupportState
from fashion_bot.state_cache import UnifiedStateCache
from fashion_bot.utils.redis_guard import RedisGuard


class SlowRedisClient:
    def __init__(self):
        self._store = {}

    def get(self, key):
        time.sleep(0.2)
        return self._store.get(key)

    def setex(self, key, ttl, value):
        time.sleep(0.2)
        self._store[key] = value
        return True

    def expire(self, key, ttl):
        return True

    def sadd(self, key, value):
        return 1

    def srem(self, key, value):
        return 1

    def delete(self, key):
        self._store.pop(key, None)
        return 1

    def smembers(self, key):
        return set()

    def scard(self, key):
        return 0

    def info(self, section):
        return {"used_memory_human": "1M"}


def test_set_state_timeout_falls_back_to_process_local_store():
    cache = UnifiedStateCache(ttl_hours=1)
    cache._redis_client = SlowRedisClient()
    cache._redis_initialized = True
    cache._redis_guard = RedisGuard(timeout_ms=10, fail_threshold=2, reset_seconds=1)

    thread_id = "whatsapp:client:919999999999"
    state = SupportState(messages=[HumanMessage(content="hello")], trace_id="t1")
    ok = cache.set_state(thread_id, state)

    assert ok is True  # degraded fail-open
    fallback_state = cache._fallback_store.get(thread_id)
    assert fallback_state is not None
    assert fallback_state.get("degraded_mode") is True
    assert "state_cache" in (fallback_state.get("degraded_components") or [])


def test_get_state_returns_fallback_with_degraded_flag_on_timeout():
    cache = UnifiedStateCache(ttl_hours=1)
    cache._redis_client = SlowRedisClient()
    cache._redis_initialized = True
    cache._redis_guard = RedisGuard(timeout_ms=10, fail_threshold=2, reset_seconds=1)

    thread_id = "whatsapp:client:918888888888"
    cached = SupportState(messages=[HumanMessage(content="from-fallback")], trace_id="t2")
    cache._fallback_store.set(thread_id, cached)

    state = cache.get_state(thread_id)
    assert state is not None
    assert state.get("degraded_mode") is True
    assert "state_cache" in (state.get("degraded_components") or [])
    assert state.get("messages")[0].content == "from-fallback"
