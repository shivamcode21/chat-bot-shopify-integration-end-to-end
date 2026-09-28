"""Per-tenant on/off switch for the product-card rating/count display
(senior review finding #7 on PR #1181: "no switch to enable/disable rating
display per tenant").

Two layers:
  - config_manager.aget_judgeme_rating_display_enabled() -- reads the switch
  - websocket_chat.format_product_for_carousel(rating_enabled=...) -- obeys it
"""
import pytest

pytest.importorskip("pydantic")
pytest.importorskip("fastapi")

from fashion_bot import config_manager
from fashion_bot.websocket_chat import format_product_for_carousel


def _patch_judgeme_details(monkeypatch, value):
    async def _fake(config_key, default=None, client_id=None):
        return value if config_key == "judgeme_details" else default
    monkeypatch.setattr(config_manager, "aget_config", _fake)


# ── aget_judgeme_rating_display_enabled ─────────────────────────────────

@pytest.mark.asyncio
async def test_defaults_true_when_no_judgeme_config_at_all(monkeypatch):
    """A tenant with no Judge.me config yet has no rating data to show
    anyway -- default True is harmless and keeps the function fail-open."""
    _patch_judgeme_details(monkeypatch, None)
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


@pytest.mark.asyncio
async def test_defaults_true_when_key_absent(monkeypatch):
    """Every tenant configured before this switch existed (e.g. Groovee)
    has judgeme_details with no display_rating key -- must keep working
    exactly as before, not silently go dark."""
    _patch_judgeme_details(monkeypatch, {"api_token": "x", "shop_domain": "y"})
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


@pytest.mark.asyncio
async def test_explicit_false_disables(monkeypatch):
    _patch_judgeme_details(monkeypatch, {"display_rating": False})
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is False


@pytest.mark.asyncio
async def test_explicit_true_enables(monkeypatch):
    _patch_judgeme_details(monkeypatch, {"display_rating": True})
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


@pytest.mark.asyncio
async def test_string_false_disables_not_python_truthy_bug(monkeypatch):
    """Regression test: bool("false") is True in Python. If this config gets
    hand-edited or copied in as a JSON string rather than a real JSON
    boolean, the naive bool(value) coercion would silently treat "false" as
    enabled -- the toggle would appear to do nothing."""
    for falsy_string in ("false", "False", "FALSE", "0", "no", ""):
        _patch_judgeme_details(monkeypatch, {"display_rating": falsy_string})
        assert await config_manager.aget_judgeme_rating_display_enabled("c1") is False, falsy_string


@pytest.mark.asyncio
async def test_string_true_enables(monkeypatch):
    _patch_judgeme_details(monkeypatch, {"display_rating": "true"})
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


@pytest.mark.asyncio
async def test_uppercase_key_tolerated(monkeypatch):
    _patch_judgeme_details(monkeypatch, {"DISPLAY_RATING": False})
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is False


@pytest.mark.asyncio
async def test_json_string_blob_parsed(monkeypatch):
    _patch_judgeme_details(monkeypatch, '{"display_rating": false}')
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is False


@pytest.mark.asyncio
async def test_malformed_json_fails_open_true(monkeypatch):
    _patch_judgeme_details(monkeypatch, "not-json")
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


@pytest.mark.asyncio
async def test_non_dict_blob_fails_open_true(monkeypatch):
    _patch_judgeme_details(monkeypatch, "[1, 2, 3]")
    assert await config_manager.aget_judgeme_rating_display_enabled("c1") is True


# ── format_product_for_carousel(rating_enabled=...) ─────────────────────

def _product(**overrides):
    base = {
        "title": "Rated Tee", "handle": "rated-tee",
        "url": "https://shop.example.com/products/rated-tee",
        "image_url": "https://shop.example.com/img.jpg", "price": "999",
        "metafield_attributes": {
            "rating": '{"scale_min":"1.0","scale_max":"5.0","value":"4.6"}',
            "rating_count": "128",
        },
    }
    base.update(overrides)
    return base


def test_rating_enabled_default_true_matches_pre_toggle_behavior():
    """No callers pass rating_enabled explicitly except the two call sites
    that fetch it from config -- every other/test caller keeps working."""
    card = format_product_for_carousel(_product())
    assert card["rating"] == 4.6
    assert card["rating_count"] == 128


def test_rating_disabled_omits_rating_even_when_data_present():
    card = format_product_for_carousel(_product(), rating_enabled=False)
    assert "rating" not in card
    assert "rating_count" not in card
    # Sanity: still a normal, otherwise-complete card -- the switch only
    # affects the rating keys, nothing else about the card.
    assert card["title"] == "Rated Tee"
    assert card["handle"] == "rated-tee"


def test_rating_enabled_true_explicit_same_as_default():
    card = format_product_for_carousel(_product(), rating_enabled=True)
    assert card["rating"] == 4.6
