"""Unit tests for Return Prime single-request lookup by number or id.

Covers the production gap where `get_return_request_by_id` never once returned a
real request:

  1. Customers quote the request NUMBER ("RET777"), but the lookup pushed that
     value into Return Prime's by-id path (`/return-exchange/v2/{id}`), which
     accepts only a 24-char hex ObjectId and rejects anything else with
     HTTP 412 "Invalid id".
  2. That 412 was clamped up to 502 "please try again later", so a permanent
     bad-identifier error was reported to the customer as a partner outage.
  3. The webhook-table fallback matched only `request_id` / `return_request_id`,
     never `request_number`, so rows we already held could not rescue the call.

These tests exercise the routing, the DB fallback key, and the cache-state
markers. They run fully offline: the adapter and Postgres are both stubbed, so
no Return Prime or database call is made.
"""

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SERVICE_PATH = (
    Path(__file__).resolve().parents[1]
    / "fashion_bot"
    / "return_prime"
    / "workflow"
    / "service.py"
)

CLIENT_ID = "00000000-0000-0000-0000-000000000000"

# A real Return Prime request, as returned by the live API.
LIVE_REQUEST = {
    "id": "6a7a004a55258d08a43604f2",
    "request_number": "RET777",
    "request_type": "return",
    "status": "requested",
    "order": {"name": "#gv17083"},
    "line_items": [],
}

# A real return_prime_webhook_events row, payload-less so the column fallback runs.
WEBHOOK_ROW = {
    "request_id": "6a772098a53f7b84e6816be1",
    "return_request_id": "6a772098a53f7b84e6816be1",
    "request_number": "RET766",
    "request_type": "return",
    "request_status": "requested",
    "order_number": "17238-EXC",
    "payload": None,
    "customer_phone": None,
    "customer_email": None,
    "created_at": None,
}


class _FakeAdapter:
    """Stands in for the Return Prime transport adapter."""

    def __init__(self):
        self.calls = []
        self.by_id_ok = False
        self.list_ok = True
        self.record = None

    async def get_request_by_id(self, client_id, request_id):
        self.calls.append(("get_request_by_id", request_id))
        if self.by_id_ok:
            return _ok({"request": self.record}, "REQUEST_EXCHANGE_S3")
        # Shape of a real 412 from /return-exchange/v2/{bad-id}
        return {
            "success": False,
            "status_code": 412,
            "message_code": "GLOBAL_E2",
            "message": "Invalid id: id contains an invalid value",
            "data": None,
            "raw": {},
            "error": {},
        }

    async def list_requests(self, client_id, **kwargs):
        self.calls.append(("list_requests", kwargs))
        if not self.list_ok:
            return {
                "success": False,
                "status_code": 503,
                "message": "upstream unavailable",
                "message_code": None,
                "data": None,
                "raw": {},
                "error": {},
            }
        rows = [self.record] if self.record else []
        return _ok({"list": rows, "hasNextPage": False}, "REQUEST_EXCHANGE_S2")


def _ok(inner, message_code):
    return {
        "success": True,
        "status_code": 200,
        "message_code": None,
        "message": None,
        "data": {"status": True, "messageCode": message_code, "data": inner},
        "raw": {},
        "error": None,
    }


class _FakePostgres:
    def __init__(self):
        self.rows = []
        self.queries = []

    async def fetch_all(self, sql, params):
        self.queries.append((sql, params))
        return list(self.rows)


@pytest.fixture
def rp(monkeypatch):
    """Load the workflow service with its I/O dependencies stubbed.

    The module is loaded from its file under a private name and every stub added
    to ``sys.modules`` is removed afterwards, so nothing leaks into other tests.
    """
    adapter = _FakeAdapter()
    postgres = _FakePostgres()

    async def _noop(*args, **kwargs):
        return None

    stubs = {
        "fashion_bot": {},
        "fashion_bot.return_prime": {},
        "fashion_bot.return_prime.adapter": {},
        "fashion_bot.return_prime.workflow": {},
        "fashion_bot.return_prime.webhook": {},
        "fashion_bot.utils": {},
        "fashion_bot.return_prime.adapter.client": {"return_prime_service": adapter},
        "fashion_bot.config_manager": {"aget_json_config": _noop, "aget_shopify_config": _noop},
        "fashion_bot.env_loader": {
            "get_env": lambda *a, **k: None,
            "get_int": lambda key, default=0: default,
        },
        "fashion_bot.return_prime.db": {"db": types.SimpleNamespace(postgres=postgres)},
        "fashion_bot.return_prime.shopify_lookup": {"fetch_order_by_name": _noop},
        "fashion_bot.return_prime.webhook.service": {"return_prime_webhook_service": object()},
        "fashion_bot.return_prime.workflow.constants": {"NO_REQUEST_MESSAGE": "none"},
        "fashion_bot.utils.redis_client": {"get_shared_async_redis_client": _noop},
        "fashion_bot.utils.tiered_cache": {"aget_with_tiered_cache": _noop},
    }

    added = []
    for name, attrs in stubs.items():
        if name in sys.modules:
            continue
        module = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(module, key, value)
        sys.modules[name] = module
        added.append(name)

    spec = importlib.util.spec_from_file_location("_rp_service_under_test", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    try:
        yield types.SimpleNamespace(
            service=module.ReturnPrimeWorkflowService(),
            module=module,
            adapter=adapter,
            postgres=postgres,
        )
    finally:
        for name in added:
            sys.modules.pop(name, None)
        sys.modules.pop("_rp_service_under_test", None)


def _run(coro):
    return asyncio.run(coro)


# --- identifier shape -------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("6a7a004a55258d08a43604f2", True),
        ("6A7A004A55258D08A43604F2", True),
        ("RET777", False),
        ("ret777", False),
        ("6a7a004a55258d08a43604f", False),   # 23 chars
        ("zzzzzzzzzzzzzzzzzzzzzzzz", False),  # 24 non-hex
        ("", False),
        (None, False),
    ],
)
def test_object_id_detection(rp, value, expected):
    assert rp.module._is_object_id(value) is expected


# --- routing ----------------------------------------------------------------

def test_request_number_uses_the_list_endpoint(rp):
    """A customer-quoted RET number must never hit the by-id path (412)."""
    rp.adapter.list_ok = True
    rp.adapter.record = LIVE_REQUEST

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "RET777"))

    assert rp.adapter.calls == [("list_requests", {"request_number": "RET777"})]
    assert result["success"] is True
    assert result["is_cached"] is False
    assert result["request"]["request_number"] == "RET777"
    assert result["request"]["request_id"] == "6a7a004a55258d08a43604f2"


def test_object_id_still_uses_the_by_id_endpoint(rp):
    rp.adapter.by_id_ok = True
    rp.adapter.record = LIVE_REQUEST

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "6a7a004a55258d08a43604f2"))

    assert rp.adapter.calls == [("get_request_by_id", "6a7a004a55258d08a43604f2")]
    assert result["success"] is True
    assert result["is_cached"] is False


def test_hash_prefix_is_stripped(rp):
    """'#RET777' used to become a URL fragment and silently list everything."""
    rp.adapter.list_ok = True
    rp.adapter.record = LIVE_REQUEST

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "#RET777"))

    assert rp.adapter.calls == [("list_requests", {"request_number": "RET777"})]
    assert result["request"]["request_number"] == "RET777"


# --- cached fallback --------------------------------------------------------

def test_db_fallback_matches_request_number_and_flags_cache_state(rp):
    rp.adapter.list_ok = False
    row = dict(WEBHOOK_ROW)
    row["received_at"] = datetime.now(timezone.utc) - timedelta(hours=53)
    rp.postgres.rows = [row]

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "ret766"))

    sql, params = rp.postgres.queries[-1]
    assert "request_number" in sql, "fallback must look at the request_number column"
    assert params == (CLIENT_ID, "ret766", "ret766", "ret766")

    assert result["success"] is True
    assert result["source"] == "return_prime_webhook_events"
    assert result["is_cached"] is True
    assert result["last_updated_at"] is not None
    assert result["cached_age_hours"] == pytest.approx(53.0, abs=0.2)
    assert result["request"]["request_number"] == "RET766"
    assert "may have changed" in result["message"]


def test_naive_timestamp_is_treated_as_utc(rp):
    rp.adapter.list_ok = False
    row = dict(WEBHOOK_ROW)
    row["received_at"] = datetime.utcnow() - timedelta(hours=2)
    rp.postgres.rows = [row]

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "RET766"))

    assert result["is_cached"] is True
    assert result["cached_age_hours"] == pytest.approx(2.0, abs=0.2)


# --- failure modes ----------------------------------------------------------

def test_unknown_number_is_a_clean_not_found_not_an_outage(rp):
    """Partner answered fine and holds nothing: must not look like a 502."""
    rp.adapter.list_ok = True
    rp.adapter.record = None

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "RET999"))

    assert result["success"] is True
    assert result["status_code"] == 200
    assert result["request"] is None
    assert "no return/exchange request" in result["message"]


def test_outage_without_cached_row_still_reports_failure(rp):
    rp.adapter.list_ok = False
    rp.postgres.rows = []

    result = _run(rp.service.get_request_by_id(CLIENT_ID, "RET999"))

    assert result["success"] is False
    assert result["status_code"] >= 500


def test_blank_identifier_is_rejected_without_any_call(rp):
    result = _run(rp.service.get_request_by_id(CLIENT_ID, "   "))

    assert result["success"] is False
    assert result["status_code"] == 400, "a bad argument must not be reported as a 502 outage"
    assert rp.adapter.calls == []
    assert rp.postgres.queries == []
