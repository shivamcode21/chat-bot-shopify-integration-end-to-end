"""Unit tests for the note-trim-and-retry behaviour on Shopify 422.

Shopify caps the order ``note`` field at ~5000 characters. Because
``aadd_order_note`` appends to the existing note on every call, a heavily
updated order eventually crosses that cap and Shopify rejects the PUT with a
422. The adapter must then drop the oldest ~50% of the existing note (snapped
to a line boundary so no sentence is cut mid-way) and retry once.

These tests are fully self-contained — no network, no live Shopify.
"""

import httpx
import pytest

from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter


def _make_adapter():
    adapter = ShopifyOrderAdapter(client_id="test-client")
    adapter._config = {
        "access_token": "fake-token",
        "shop_url": "test-shop.myshopify.com",
        "api_version": "2024-04",
    }
    return adapter


# ---------------------------------------------------------------------------
# _trim_note_front
# ---------------------------------------------------------------------------

class TestTrimNoteFront:
    def test_drops_roughly_first_half(self):
        blocks = [f"Note block number {i} with some content." for i in range(10)]
        note = "\n\n".join(blocks)
        trimmed = ShopifyOrderAdapter._trim_note_front(note)
        # Roughly half the characters are gone.
        assert len(trimmed) < len(note)
        assert len(trimmed) <= len(note) * 0.6
        # The tail is preserved intact.
        assert trimmed.endswith(blocks[-1])

    def test_never_splits_a_line(self):
        blocks = [f"Block {i}: the quick brown fox jumps over the lazy dog." for i in range(8)]
        note = "\n\n".join(blocks)
        trimmed = ShopifyOrderAdapter._trim_note_front(note)
        # Every surviving line must be one of the original whole lines — no
        # partial/truncated sentence at the top.
        original_lines = set(note.split("\n"))
        for line in trimmed.split("\n"):
            assert line in original_lines

    def test_no_leading_blank_lines(self):
        note = "first\n\nsecond\n\nthird\n\nfourth"
        trimmed = ShopifyOrderAdapter._trim_note_front(note)
        assert not trimmed.startswith("\n")
        assert trimmed  # non-empty

    def test_empty_note(self):
        assert ShopifyOrderAdapter._trim_note_front("") == ""

    def test_single_line_without_newline_falls_back_to_hard_cut(self):
        note = "x" * 100
        trimmed = ShopifyOrderAdapter._trim_note_front(note)
        assert trimmed == "x" * 50


# ---------------------------------------------------------------------------
# aadd_order_note 422 -> trim -> retry
# ---------------------------------------------------------------------------

class _RecordingClient:
    """Minimal async httpx client stub that returns queued responses in order
    and records the payloads it was called with."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def put(self, url, json=None, headers=None, timeout=None):
        self.calls.append(json)
        return self._responses.pop(0)


def _resp(status, url="https://test-shop.myshopify.com/admin/api/2024-04/orders/999.json", body=None):
    return httpx.Response(status, request=httpx.Request("PUT", url), json=body or {})


@pytest.mark.asyncio
async def test_422_triggers_trim_and_retry(monkeypatch):
    adapter = _make_adapter()

    existing_note = "\n\n".join(f"Old note block {i}. Customer requested change." for i in range(40))
    order = {"id": 999, "note": existing_note}

    async def _fake_get_order_details(order_id, state=None):
        return order

    monkeypatch.setattr(adapter, "aget_order_details", _fake_get_order_details)

    # First PUT 422s (note too long), second PUT (after trim) succeeds.
    client = _RecordingClient([_resp(422), _resp(200, body={"order": {"id": 999}})])

    async def _fake_client():
        return client

    monkeypatch.setattr(
        "fashion_bot.shopify.tools.order_adapter.get_shared_async_http_client",
        _fake_client,
    )

    new_note = "[Bloomerce] Size update: XL -> L."
    result = await adapter.aadd_order_note("gv16389", new_note, state={})

    assert result["success"] is True
    assert result["note_added"] == new_note
    # Exactly two PUTs: the failed one and the retry.
    assert len(client.calls) == 2

    first_note = client.calls[0]["order"]["note"]
    retry_note = client.calls[1]["order"]["note"]
    # Retry note is the trimmed existing note + the new note appended.
    assert len(retry_note) < len(first_note)
    assert retry_note.endswith(new_note)
    # New note was never dropped.
    assert new_note in first_note and new_note in retry_note


@pytest.mark.asyncio
async def test_non_422_error_is_not_retried(monkeypatch):
    adapter = _make_adapter()
    order = {"id": 999, "note": "some existing note"}

    async def _fake_get_order_details(order_id, state=None):
        return order

    monkeypatch.setattr(adapter, "aget_order_details", _fake_get_order_details)

    client = _RecordingClient([_resp(500)])

    async def _fake_client():
        return client

    monkeypatch.setattr(
        "fashion_bot.shopify.tools.order_adapter.get_shared_async_http_client",
        _fake_client,
    )

    result = await adapter.aadd_order_note("gv16389", "new note", state={})

    # 500 is surfaced as a failure, and there is no retry.
    assert result["success"] is False
    assert len(client.calls) == 1
