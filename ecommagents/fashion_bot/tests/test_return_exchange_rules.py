from datetime import datetime, timedelta, timezone

import pytest

from fashion_bot.return_partners import rules


CLIENT_ID = "c3ffcb1b-afb9-4ca4-8746-a06698bec870"


def _order(*, delivered_at=None, tags=None):
    return {
        "name": "#1001",
        "fulfillment_status": "delivered",
        "updated_at": (delivered_at or datetime.now(timezone.utc)).isoformat(),
        "line_items": [
            {
                "product_id": 123,
                "title": "Test Product",
                "quantity": 1,
                "tags": tags or "",
            }
        ],
    }


@pytest.mark.asyncio
async def test_validate_allows_when_no_rules_configured(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return None

    monkeypatch.setattr(rules, "aget_json_config", fake_config)

    result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=_order(),
    )

    assert result["valid"] is True
    assert result["validation_applied"] is False


@pytest.mark.asyncio
async def test_validate_blocks_return_outside_window(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return {
            "return": {
                "window_days": 3,
                "require_delivered": True,
                "blocked_product_tags": [],
            }
        }

    monkeypatch.setattr(rules, "aget_json_config", fake_config)

    result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=_order(delivered_at=datetime.now(timezone.utc) - timedelta(days=5)),
    )

    assert result["valid"] is False
    assert "outside_window" in result["failed_rules"]


@pytest.mark.asyncio
async def test_validate_blocks_exchange_for_restricted_product_tag(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return {
            "exchange": {
                "window_days": 7,
                "require_delivered": True,
                "blocked_product_tags": ["Winter Drop", "winter"],
            }
        }

    monkeypatch.setattr(rules, "aget_json_config", fake_config)

    result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="exchange",
        order=_order(tags="summer, winter"),
    )

    assert result["valid"] is False
    assert "blocked_product_tag" in result["failed_rules"]
    assert result["details"]["matched_blocked_product_tags"] == ["winter"]


@pytest.mark.asyncio
async def test_validate_blocks_not_delivered_order(monkeypatch):
    async def fake_config(config_key, client_id=None):
        return {
            "return": {
                "window_days": 3,
                "require_delivered": True,
                "blocked_product_tags": [],
            }
        }

    monkeypatch.setattr(rules, "aget_json_config", fake_config)
    order = _order()
    order["fulfillment_status"] = "partial"

    result = await rules.avalidate_return_exchange_request(
        client_id=CLIENT_ID,
        order_number="#1001",
        request_type="return",
        order=order,
    )

    assert result["valid"] is False
    assert "order_not_delivered" in result["failed_rules"]
