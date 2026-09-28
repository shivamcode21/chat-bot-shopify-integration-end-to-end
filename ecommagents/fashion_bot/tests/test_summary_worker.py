import json

import pytest
from langchain_core.messages import HumanMessage, AIMessage

from fashion_bot import gupshup_webhook as gw
from fashion_bot.utils.redis_guard import RedisGuard


class FakeSummaryRedis:
    def __init__(self):
        self.queue = []
        self.locks = {}

    def set(self, key, value, nx=False, ex=None):
        if nx:
            if key in self.locks:
                return False
            self.locks[key] = value
            return True
        self.locks[key] = value
        return True

    def delete(self, key):
        self.locks.pop(key, None)
        return 1

    def lpop(self, key):
        if not self.queue:
            return None
        return self.queue.pop(0)


@pytest.mark.asyncio
async def test_process_summary_job_updates_watermark_and_context(monkeypatch):
    fake_redis = FakeSummaryRedis()
    state_ref = {
        "messages": [
            HumanMessage(content="Where is my order?"),
            AIMessage(content="Please share your order ID"),
            HumanMessage(content="#GV1234"),
            AIMessage(content="Your order is shipped."),
        ],
        "conversation_context": {},
    }

    monkeypatch.setattr(gw, "_get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(gw, "_redis_guard", RedisGuard(timeout_ms=200, fail_threshold=5, reset_seconds=1))
    monkeypatch.setattr(gw, "aget_state_by_numbers", lambda phone, cid: state_ref)
    monkeypatch.setattr(gw, "aupdate_state", lambda phone, cid, st: state_ref.update(st))

    job = {
        "client_id": "c1",
        "phone": "9999999999",
        "trace_id": "t1",
        "target_msg_idx": 4,
    }

    support = gw._get_runtime_support()
    await support.process_summary_job(job)

    assert state_ref.get("summary_applied_upto_msg_idx") == 4
    rolling = (state_ref.get("conversation_context") or {}).get("rolling_summary", "")
    assert "User:" in rolling
    assert "Bot:" in rolling


@pytest.mark.asyncio
async def test_run_summary_worker_drains_queue(monkeypatch):
    fake_redis = FakeSummaryRedis()
    fake_redis.queue = [
        json.dumps(
            {
                "client_id": "c1",
                "phone": "9999999999",
                "trace_id": "t2",
                "target_msg_idx": 2,
            }
        )
    ]

    state_ref = {
        "messages": [HumanMessage(content="hello"), AIMessage(content="hi")],
        "conversation_context": {},
    }

    monkeypatch.setattr(gw, "_get_redis_client", lambda: fake_redis)
    monkeypatch.setattr(gw, "_redis_guard", RedisGuard(timeout_ms=200, fail_threshold=5, reset_seconds=1))
    monkeypatch.setattr(gw, "aget_state_by_numbers", lambda phone, cid: state_ref)
    monkeypatch.setattr(gw, "aupdate_state", lambda phone, cid, st: state_ref.update(st))

    support = gw._get_runtime_support()
    await support.run_summary_worker("c1")

    assert state_ref.get("summary_applied_upto_msg_idx") == 2
    assert fake_redis.queue == []
