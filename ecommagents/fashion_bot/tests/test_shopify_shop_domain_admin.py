"""Tests for the Shopify shop-domain admin API.

The endpoint exists because the shop domain is stored in two tables and writing
only one silently breaks webhook delivery: for ``vahro.myshopify.com`` the
installed store was written on 5 Aug but the client record not until 8 Aug, and
2,345 webhooks were discarded in between with no error anywhere.

Self-contained — no database, no network.

Run:  python -m pytest fashion_bot/tests/test_shopify_shop_domain_admin.py -v
"""
from __future__ import annotations

import pytest

from fashion_bot.admin_router import (
    _SHOP_DOMAIN_RE,
    _classify_mapping,
    _normalize_shop_domain,
)


# ── Normalisation ──────────────────────────────────────────────────────────
# Resolution compares the stored value against the raw X-Shopify-Shop-Domain
# header with '=', so anything a human pastes has to collapse to the same bytes.

@pytest.mark.parametrize("raw,expected", [
    ("vahro.myshopify.com", "vahro.myshopify.com"),
    ("  vahro.myshopify.com  ", "vahro.myshopify.com"),
    ("Vahro.MyShopify.com", "vahro.myshopify.com"),
    ("https://vahro.myshopify.com", "vahro.myshopify.com"),
    ("http://vahro.myshopify.com/", "vahro.myshopify.com"),
    ("https://vahro.myshopify.com/admin/products", "vahro.myshopify.com"),
    ("vahro.myshopify.com:443", "vahro.myshopify.com"),
    ("", ""),
    (None, ""),
])
def test_normalize(raw, expected):
    assert _normalize_shop_domain(raw) == expected


# ── Validation ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("good", [
    "vahro.myshopify.com",
    "enamor-in.myshopify.com",
    "2dce50-98.myshopify.com",
])
def test_valid_domains_accepted(good):
    assert _SHOP_DOMAIN_RE.match(good)


@pytest.mark.parametrize("bad", [
    "lbplanet.myshipify.com",    # 'myshipify' typo — real value in production
    "prathaa.myshpify.com",      # 'myshpify' typo — real value in production
    "tattva@myshopify.com",      # '@' instead of '.' — real value in production
    "acme.shopify.com",          # missing the 'my' prefix
    "myshopify.com",             # no store handle
    "-bad.myshopify.com",        # cannot start with a hyphen
    "vahro.myshopify.com.evil",  # suffix must be exact
    "",
])
def test_invalid_domains_rejected(bad):
    """Every one of these can never equal an inbound Shopify header.

    The first three are values that are live in the clients table today, which
    is what this validation is meant to stop happening again.
    """
    assert not _SHOP_DOMAIN_RE.match(bad)


# ── Status classification ──────────────────────────────────────────────────

def test_synced():
    status, detail = _classify_mapping("vahro.myshopify.com", ["vahro.myshopify.com"])
    assert status == "synced"
    assert "vahro.myshopify.com" in detail


def test_installed_but_client_record_empty_is_the_vahro_case():
    status, detail = _classify_mapping(None, ["vahro.myshopify.com"])
    assert status == "clients_column_empty"
    assert "discarded" in detail


def test_mismatch_between_the_two_records():
    """Concept Groove: client record says one domain, installs say others."""
    status, detail = _classify_mapping(
        "2dce50-98.myshopify.com",
        ["anirudh-testing-dev.myshopify.com", "dev-final-2.myshopify.com"],
    )
    assert status == "mismatch"
    assert "2dce50-98.myshopify.com" in detail
    assert "dev-final-2.myshopify.com" in detail


def test_malformed_beats_mismatch():
    """An invalid domain is reported as invalid, not as a disagreement."""
    status, _ = _classify_mapping("tattva@myshopify.com", ["tattva.myshopify.com"])
    assert status == "malformed"


def test_valid_domain_without_install():
    status, _ = _classify_mapping("levis.myshopify.com", [])
    assert status == "no_oauth_install"


def test_nothing_configured():
    status, _ = _classify_mapping(None, [])
    assert status == "unconfigured"


def test_every_status_has_a_ui_label():
    """The page renders LABELS[status]; an unmapped status would leak a raw key."""
    import pathlib
    page = (
        pathlib.Path(__file__).resolve().parents[1] / "static" / "admin-shopify-domains.html"
    ).read_text()
    for status in (
        "synced", "clients_column_empty", "mismatch",
        "malformed", "no_oauth_install", "unconfigured",
    ):
        assert f"{status}:" in page, f"UI has no label for status '{status}'"
