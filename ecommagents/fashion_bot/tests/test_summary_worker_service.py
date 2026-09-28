import pytest

from fashion_bot import summary_worker as sw
from fashion_bot.utils.redis_guard import RedisGuard


class FakeScanRedis:
    def __init__(self, keys):
        self._keys = keys

    def scan_iter(self, match=None, count=None):
        for k in self._keys:
            yield k


def test_extract_client_id_from_summary_key():
    assert sw._extract_client_id_from_summary_key("summary:jobs:abc") == "abc"
    assert sw._extract_client_id_from_summary_key("summary:jobs:") is None
    assert sw._extract_client_id_from_summary_key("other:key") is None


def test_list_clients_with_pending_summary_jobs(monkeypatch):
    fake = FakeScanRedis(["summary:jobs:c1", "summary:jobs:c2", "summary:jobs:c1"])
    monkeypatch.setattr(sw, "_get_redis_client", lambda: fake)
    monkeypatch.setattr(sw, "_redis_guard", RedisGuard(timeout_ms=200, fail_threshold=5, reset_seconds=1))

    clients = sw.list_clients_with_pending_summary_jobs()
    assert clients == ["c1", "c2"]


@pytest.mark.asyncio
async def test_process_summary_jobs_once_specific_client(monkeypatch):
    calls = []

    async def _fake_run(client_id):
        calls.append(client_id)

    monkeypatch.setattr(sw, "_run_summary_worker", _fake_run)

    processed = await sw.process_summary_jobs_once(client_id="client-x")
    assert processed == 1
    assert calls == ["client-x"]
