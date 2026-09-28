"""Regression guard: judgeme_details must never be cloned from the reference
client during onboarding. Currently only holds the per-tenant display_rating
toggle (config_manager.aget_judgeme_rating_display_enabled), but excluded on
general principle since it's tenant-specific config, not something a newly
onboarded client should silently inherit (AGENTS.md §7).
"""
from __future__ import annotations

from fashion_bot.client_onboarding import (
    EXCLUDED_REFERENCE_CONFIG_KEYS,
    _is_excluded_reference_config_key,
)


def test_judgeme_details_is_excluded_from_reference_cloning():
    assert "judgeme_details" in EXCLUDED_REFERENCE_CONFIG_KEYS
    assert _is_excluded_reference_config_key("judgeme_details") is True


def test_other_known_credential_configs_still_excluded():
    """Sanity: this fix didn't accidentally replace the existing set."""
    for key in ("shopify_details", "shiprocket_details", "gupshup_details"):
        assert _is_excluded_reference_config_key(key) is True


def test_non_credential_config_still_cloned():
    """The fix must not be so broad it blocks legitimate reference cloning
    of ordinary, non-credential config keys."""
    assert _is_excluded_reference_config_key("delivery_policy") is False
