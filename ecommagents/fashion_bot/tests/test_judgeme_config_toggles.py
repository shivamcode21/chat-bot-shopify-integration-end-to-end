"""Offline tests for the Judge.me per-tenant settings in config_manager.py --
aget_judgeme_rating_display_enabled (defaults True) and
aget_judgeme_default_review_sort (defaults "recent"). Both read
client_configs.judgeme_details but are otherwise independent.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from fashion_bot import config_manager as cm

_MODULE = "fashion_bot.config_manager"


async def _with_judgeme_details(value, fn):
    raw = json.dumps(value) if value is not None else None
    with patch(f"{_MODULE}.aget_config", new_callable=AsyncMock, return_value=raw):
        return await fn(client_id="c1")


@pytest.mark.asyncio
async def test_rating_display_defaults_true_when_key_absent():
    result = await _with_judgeme_details(None, cm.aget_judgeme_rating_display_enabled)
    assert result is True



# ── flag-string polarity ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["off", "disabled", "n", "no", "0", "false"])
@pytest.mark.asyncio
async def test_rating_display_off_for_every_off_spelling(value):
    """Same bug on the opt-OUT flag: "off" used to leave the rating display
    ON, since it isn't one of ('false','0','no','')."""
    result = await _with_judgeme_details(
        {"display_rating": value}, cm.aget_judgeme_rating_display_enabled
    )
    assert result is False, f"{value!r} must switch the display off"


@pytest.mark.parametrize("value", ["maybe", "nope", "tbd"])
@pytest.mark.asyncio
async def test_rating_display_unrecognised_value_falls_back_to_on(value):
    """Mirror image of the opt-in case: an opt-OUT flag defaults True, so an
    unparseable value must not silently DISABLE something a tenant relies on."""
    result = await _with_judgeme_details(
        {"display_rating": value}, cm.aget_judgeme_rating_display_enabled
    )
    assert result is True


# ── the shared parse itself ────────────────────────────────────────────────

def test_is_config_flag_enabled_default_only_applies_to_unrecognised_values():
    """The `default` is the fallback for values we cannot read -- it must
    never override a value we CAN read."""
    assert cm.is_config_flag_enabled("on", default=False) is True
    assert cm.is_config_flag_enabled("off", default=True) is False
    assert cm.is_config_flag_enabled("???", default=False) is False
    assert cm.is_config_flag_enabled("???", default=True) is True
    assert cm.is_config_flag_enabled(None, default=True) is True
    assert cm.is_config_flag_enabled(None, default=False) is False


def test_is_config_flag_enabled_handles_real_booleans_and_numbers():
    assert cm.is_config_flag_enabled(True, default=False) is True
    assert cm.is_config_flag_enabled(False, default=True) is False
    assert cm.is_config_flag_enabled(1, default=False) is True
    assert cm.is_config_flag_enabled(0, default=True) is False


def test_is_config_flag_enabled_rejects_unusable_types_to_the_default():
    """A list/dict where a flag was expected is malformed config, not a
    signal -- fall back rather than guessing from truthiness."""
    assert cm.is_config_flag_enabled([], default=True) is True
    assert cm.is_config_flag_enabled(["on"], default=False) is False
    assert cm.is_config_flag_enabled({"a": 1}, default=False) is False


# ── aget_judgeme_default_review_sort: defaults "recent" ────────────────────

@pytest.mark.asyncio
async def test_default_review_sort_is_recent_when_config_absent():
    """Not "top_rated": a rating-ordered default is not neutral. On a product
    with more reviews than the page size it puts the highest-rated first and
    pushes every complaint past the end of the slice, so a tenant who never
    configures anything would get a systematically flattering listing."""
    result = await _with_judgeme_details(None, cm.aget_judgeme_default_review_sort)
    assert result == "recent"


@pytest.mark.asyncio
async def test_default_review_sort_is_recent_when_field_missing():
    result = await _with_judgeme_details(
        {"shop_domain": "s.myshopify.com", "api_token": "t"},
        cm.aget_judgeme_default_review_sort,
    )
    assert result == "recent"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["top_rated", "TOP_RATED", " Top_Rated "])
async def test_default_review_sort_honours_top_rated_case_and_space_insensitively(value):
    """A hand-edited config should not be defeated by capitalisation or a
    stray space."""
    result = await _with_judgeme_details(
        {"default_review_sort": value}, cm.aget_judgeme_default_review_sort
    )
    assert result == "top_rated"


@pytest.mark.asyncio
async def test_default_review_sort_honours_recent_explicitly():
    result = await _with_judgeme_details(
        {"default_review_sort": "recent"}, cm.aget_judgeme_default_review_sort
    )
    assert result == "recent"


@pytest.mark.asyncio
async def test_default_review_sort_accepts_uppercase_key():
    result = await _with_judgeme_details(
        {"DEFAULT_REVIEW_SORT": "top_rated"}, cm.aget_judgeme_default_review_sort
    )
    assert result == "top_rated"


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["newest", "best", "", "1", "asc", None])
async def test_default_review_sort_unrecognised_value_falls_back(value):
    """An unrecognised ordering must land on the neutral default rather than
    reaching the tool, which understands only these two values."""
    result = await _with_judgeme_details(
        {"default_review_sort": value}, cm.aget_judgeme_default_review_sort
    )
    assert result == "recent"


@pytest.mark.asyncio
async def test_default_review_sort_survives_malformed_config():
    with patch(f"{_MODULE}.aget_config", new_callable=AsyncMock, return_value="{not json"):
        assert await cm.aget_judgeme_default_review_sort(client_id="c1") == "recent"
