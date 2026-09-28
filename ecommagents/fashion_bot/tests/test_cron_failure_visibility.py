"""
Tests that a failed per-client product sync is actually visible to alerting.

Background
----------
Two Grafana alerts key off the same series:

    Product Vector Sync Failed:
        sum(increase(cron_runs_total{job_name="product_sync", status="failure"}[30m])) > 0
    Cron Job Failure Detected:
        sum(increase(cron_runs_total{status="failure"}[15m])) > 0

Nothing emitted that series for product sync. The weekly cron is a thin
producer that fans out one Dramatiq message per client and returns
``{"success": True}`` unconditionally — "dispatch always succeeds" — so the
only tracked function could essentially never report failure. The real work
runs on the worker in ``delta_sync_single_client``, which was untracked: the
actor emitted ``cron_items_processed`` and nothing else.

Net effect: every client's sync could fail every week and both alerts stayed
green. Observed in production as ``cron_runs_total{job_name="product_sync"}``
flat at zero while the job demonstrably ran, with the alert sitting in
``Normal (NoData)`` since June.

These tests lock down that a per-client failure now increments the failure
counter, and that the decorator's existing skip/success semantics are intact.
"""

import asyncio

import pytest

from fashion_bot.monitoring import otel_metrics


@pytest.fixture
def recorded_runs(monkeypatch):
    """Capture every cron_runs_total emission."""
    calls = []
    monkeypatch.setattr(
        otel_metrics, "cron_run_counter",
        type("_C", (), {"add": staticmethod(lambda value, labels: calls.append(labels))})(),
    )
    # Silence the sibling instruments the decorator also touches.
    for name in ("cron_duration", "cron_items_processed"):
        monkeypatch.setattr(
            otel_metrics, name,
            type("_N", (), {
                "add": staticmethod(lambda *a, **k: None),
                "record": staticmethod(lambda *a, **k: None),
            })(),
        )
    return calls


# ---------------------------------------------------------------------------
# The regression this exists to prevent
# ---------------------------------------------------------------------------

def test_per_client_sync_is_tracked_as_product_sync():
    """delta_sync_single_client must carry @track_cron, or failures stay silent."""
    from fashion_bot.cron_jobs import product_vector_sync_job as job

    assert hasattr(job.delta_sync_single_client, "__wrapped__"), (
        "delta_sync_single_client is not wrapped by @track_cron — per-client "
        "sync failures would be invisible to both cron alerts"
    )


def test_failed_client_sync_emits_a_failure_run(recorded_runs, monkeypatch):
    from fashion_bot.cron_jobs import product_vector_sync_job as job

    async def _failing_sync(client_id, hours=168):
        return {"client_id": client_id, "success": False, "error": "Shopify 500"}

    monkeypatch.setattr(job, "sync_client_products", _failing_sync)
    result = asyncio.run(job.delta_sync_single_client("client-1"))

    assert result["success"] is False
    assert {"job_name": "product_sync", "status": "failure"} in recorded_runs, (
        "a failed client sync must increment "
        "cron_runs_total{job_name=\"product_sync\", status=\"failure\"}"
    )


def test_raising_client_sync_emits_a_failure_run(recorded_runs, monkeypatch):
    """An exception must be recorded as a failure and still propagate.

    The Dramatiq actor relies on the raise for its retry semantics.
    """
    from fashion_bot.cron_jobs import product_vector_sync_job as job

    async def _exploding_sync(client_id, hours=168):
        raise RuntimeError("upstash unreachable")

    monkeypatch.setattr(job, "sync_client_products", _exploding_sync)

    with pytest.raises(RuntimeError):
        asyncio.run(job.delta_sync_single_client("client-1"))

    assert {"job_name": "product_sync", "status": "failure"} in recorded_runs


def test_successful_client_sync_emits_a_success_run(recorded_runs, monkeypatch):
    from fashion_bot.cron_jobs import product_vector_sync_job as job

    async def _ok_sync(client_id, hours=168):
        return {"client_id": client_id, "success": True, "products_updated": 3}

    monkeypatch.setattr(job, "sync_client_products", _ok_sync)
    asyncio.run(job.delta_sync_single_client("client-1"))

    assert {"job_name": "product_sync", "status": "success"} in recorded_runs


# ---------------------------------------------------------------------------
# Pre-existing decorator semantics must not regress
# ---------------------------------------------------------------------------

def test_lock_skip_is_not_counted_as_a_run(recorded_runs):
    """Leader-locked jobs fire on every pod; the losers must not inflate counts."""
    @otel_metrics.track_cron("some_job")
    async def _skipped():
        return {"success": True, "skipped": True}

    asyncio.run(_skipped())

    assert recorded_runs == []


def test_producer_still_records_its_own_dispatch(recorded_runs, monkeypatch):
    """The fan-out producer keeps its tracking; this change only adds to it."""
    from fashion_bot.cron_jobs import product_vector_sync_job as job

    async def _dispatch():
        return {"success": True, "clients_queued": 2}

    monkeypatch.setattr(job, "_async_product_vector_delta_sync", _dispatch)

    async def _lock(*_args, **_kwargs):
        return type("_Lease", (), {"owner_id": "test-pod"})()

    async def _release(*_args, **_kwargs):
        return None

    monkeypatch.setattr(job, "acquire_cron_lock", _lock)
    monkeypatch.setattr(job, "release_cron_lock", _release)

    asyncio.run(job.product_vector_delta_sync())

    assert {"job_name": "product_sync", "status": "success"} in recorded_runs
