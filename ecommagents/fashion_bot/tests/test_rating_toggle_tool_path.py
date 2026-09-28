"""Tests for _apply_rating_toggle in tool_factory.py -- the per-tenant
display_rating switch (aget_judgeme_rating_display_enabled), now applied to
the LLM-facing tool result, not just the web-widget card.

Before this fix, a client with the toggle off still got the LLM stating a
rating in text on every channel (WhatsApp included), because
_normalize_product() added rating/rating_count unconditionally and nothing
downstream re-checked the toggle for the tool path.
"""

import pytest


def _apply():
    pytest.importorskip("pydantic")
    pytest.importorskip("langchain_core")
    from fashion_bot.tool_factory import _apply_rating_toggle
    return _apply_rating_toggle


def _rated_product(**overrides):
    base = {"title": "Rated Tee", "handle": "rated-tee", "rating": 4.5, "rating_count": 2}
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_strips_rating_when_toggle_off(monkeypatch):
    apply_toggle = _apply()
    import fashion_bot.config_manager as config_manager
    monkeypatch.setattr(config_manager, "aget_judgeme_rating_display_enabled", lambda client_id: _afalse())

    product = _rated_product()
    result = await apply_toggle(product, "client-x")
    assert "rating" not in result
    assert "rating_count" not in result


@pytest.mark.asyncio
async def test_keeps_rating_when_toggle_on(monkeypatch):
    apply_toggle = _apply()
    import fashion_bot.config_manager as config_manager
    monkeypatch.setattr(config_manager, "aget_judgeme_rating_display_enabled", lambda client_id: _atrue())

    product = _rated_product()
    result = await apply_toggle(product, "client-x")
    assert result["rating"] == 4.5
    assert result["rating_count"] == 2


@pytest.mark.asyncio
async def test_handles_list_of_products(monkeypatch):
    apply_toggle = _apply()
    import fashion_bot.config_manager as config_manager
    monkeypatch.setattr(config_manager, "aget_judgeme_rating_display_enabled", lambda client_id: _afalse())

    products = [_rated_product(handle="a"), _rated_product(handle="b")]
    result = await apply_toggle(products, "client-x")
    assert all("rating" not in p and "rating_count" not in p for p in result)


@pytest.mark.asyncio
async def test_unrated_product_unaffected_either_way(monkeypatch):
    apply_toggle = _apply()
    import fashion_bot.config_manager as config_manager
    monkeypatch.setattr(config_manager, "aget_judgeme_rating_display_enabled", lambda client_id: _afalse())

    product = {"title": "Unrated Tee", "handle": "unrated-tee"}
    result = await apply_toggle(product, "client-x")
    assert result == {"title": "Unrated Tee", "handle": "unrated-tee"}


async def _atrue():
    return True


async def _afalse():
    return False
