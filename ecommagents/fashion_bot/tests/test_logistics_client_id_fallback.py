"""
Regression tests for client_id resolution in the Shiprocket logistics adapter.

Production bug (RCA: trace 257952799c308155780f3773a9d56f1f, 2026-06-24,
client c3ffcb1b-afb9-4ca4-8746-a06698bec870 / "Concept Groove", groovee.in
"Ask Bloom" web widget):

The web/streaming delivery-timeline flow invoked
``get_delivery_estimate_tool_enhanced`` → ``LogisticsOrchestrator`` →
``ServiceFactory.aget_logistics_service(state=...)``. The graph ``state`` dict
reaching LangChain tools did NOT carry ``client_id`` (the value lives in the
request-scoped ``client_context`` ContextVar, set in websocket_chat /
streaming_service). The factory and adapter resolved client_id ONLY from
``state``, so the Shiprocket config lookup ran with ``client_id=None`` and
returned "Shiprocket is not configured for client None" — even though the
client is fully configured. The customer saw:
"I am currently unable to provide a specific delivery estimate for your area."

These tests lock in the ContextVar fallback so a state-only call path can no
longer collapse client_id to None.
"""

from unittest.mock import AsyncMock, patch

import pytest

from fashion_bot import client_context
from fashion_bot.shiprocket.tools.logistics_adapter import ShiprocketLogisticsAdapter


@pytest.fixture(autouse=True)
def _reset_client_context():
    """Ensure the ContextVar never leaks between tests."""
    token = client_context.client_id_context.set(None)
    try:
        yield
    finally:
        client_context.client_id_context.reset(token)


def test_get_client_id_prefers_explicit_adapter_value():
    adapter = ShiprocketLogisticsAdapter(client_id="explicit-client")
    client_context.set_client_id("ctx-client")
    # Explicit construction wins over both state and ContextVar.
    assert adapter._get_client_id({"client_id": "state-client"}) == "explicit-client"


def test_get_client_id_uses_state_when_adapter_unset():
    adapter = ShiprocketLogisticsAdapter(client_id=None)
    client_context.set_client_id("ctx-client")
    assert adapter._get_client_id({"client_id": "state-client"}) == "state-client"


def test_get_client_id_falls_back_to_context_var():
    """The core regression: no adapter client_id, no client_id in state."""
    adapter = ShiprocketLogisticsAdapter(client_id=None)
    client_context.set_client_id("ctx-client")
    assert adapter._get_client_id({}) == "ctx-client"
    assert adapter._get_client_id(None) == "ctx-client"


def test_get_client_id_returns_none_when_nothing_set():
    adapter = ShiprocketLogisticsAdapter(client_id=None)
    assert adapter._get_client_id({}) is None


@pytest.mark.asyncio
async def test_aget_config_resolves_client_id_via_context_var():
    """`_aget_config` must pass the ContextVar client_id to the config lookup."""
    adapter = ShiprocketLogisticsAdapter(client_id=None)
    client_context.set_client_id("ctx-client")

    fake_cfg = {"api_base": "https://x", "email": "a@b.c", "password": "pw"}
    with patch(
        "fashion_bot.shiprocket.tools.logistics_adapter.aget_shiprocket_config",
        new=AsyncMock(return_value=fake_cfg),
    ) as mock_cfg:
        cfg = await adapter._aget_config(state={})

    assert cfg == fake_cfg
    mock_cfg.assert_awaited_once_with(client_id="ctx-client")


@pytest.mark.asyncio
async def test_aget_config_retries_after_empty_config():
    """An adapter built with client_id=None caches {} but must re-resolve once
    a real client_id becomes available (truthy guard, not ``is not None``)."""
    adapter = ShiprocketLogisticsAdapter(client_id=None)
    adapter._config = {}  # simulate eager create() with no client_id

    fake_cfg = {"api_base": "https://x", "email": "a@b.c", "password": "pw"}
    client_context.set_client_id("ctx-client")
    with patch(
        "fashion_bot.shiprocket.tools.logistics_adapter.aget_shiprocket_config",
        new=AsyncMock(return_value=fake_cfg),
    ) as mock_cfg:
        cfg = await adapter._aget_config(state={})

    assert cfg == fake_cfg
    mock_cfg.assert_awaited_once_with(client_id="ctx-client")
