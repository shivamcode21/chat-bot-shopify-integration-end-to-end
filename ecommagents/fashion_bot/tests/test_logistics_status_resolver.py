"""
Unit tests for fashion_bot.logistics.status_resolver.resolve_logistics_status.

Each test below is annotated with the production scenario it locks down so
that future edits cannot silently regress either of the two bugs that
motivated the resolver:

  Bug A — stale partner status overrides reality
    Order gv15094 (client c3ffcb1b-afb9-4ca4-8746-a06698bec870) was shipped
    via Delhivery Surface (AWB 19041915983746). Shopify
    fulfillment.shipment_status = "in_transit". Shiprocket API returned
    "PICKUP EXCEPTION" for the same order, multiple times. The agent
    annotated the Shopify note: "Customer asked about order status.
    Logistics status: PICKUP EXCEPTION. Informed about dispatch tomorrow."

  Bug B — recent-orders path has no logistics_status
    Web-chat session web_2e6b4bf6-bf73-460f-8729-611fcf91faaf, message_id
    f9b7a7ae-a7ea-4f44-84f2-a5adb486ffca, 2026-05-26 02:56:33 UTC. Agent
    invoked get_recent_orders (not get_order_details), so the tool output
    had shipment_status="in_transit" but no logistics_status field. Rule
    4 ("already dispatched") never fired and the agent improvised with
    "Your order is packed and ready to be shipped".

PR #654 attempted to fix Bug A only, with a helper that (a) was never
wired in, (b) compared Pascal-case strings against uppercase partner
output, and (c) used tracking-URL presence — not carrier-reported
shipment_status — as the override signal. See status_resolver.py module
docstring for the full critique.
"""

from fashion_bot.logistics.status_resolver import (
    DELIVERED,
    IN_TRANSIT,
    OUT_FOR_DELIVERY,
    PICKUP_EXCEPTION,
    PICKUP_RESCHEDULED,
    READY_TO_SHIP,
    REACHED_DESTINATION_HUB,
    resolve_logistics_status,
)


# ── Bug A: stale partner status, Shopify confirms movement ────────────────

def test_partner_says_pickup_exception_but_shopify_says_in_transit():
    """The exact gv15094 scenario. Partner is stale; carrier is truth."""
    assert resolve_logistics_status(
        raw_partner_status="PICKUP EXCEPTION",
        shipment_status="in_transit",
    ) == IN_TRANSIT


def test_partner_says_pickup_rescheduled_but_shopify_says_in_transit():
    assert resolve_logistics_status(
        raw_partner_status="PICKUP RESCHEDULED",
        shipment_status="in_transit",
    ) == IN_TRANSIT


def test_partner_says_delayed_but_shopify_says_delivered():
    """Carrier truth always wins, even past in-transit."""
    assert resolve_logistics_status(
        raw_partner_status="DELAYED",
        shipment_status="delivered",
    ) == DELIVERED


# ── Bug B: recent-orders path with no partner enrichment ──────────────────

def test_recent_orders_in_transit_with_no_partner_status():
    """
    The f9b7a7ae case. get_recent_orders has shipment_status but no
    partner status; resolver must still produce "In Transit" so the
    prompt's Rule 4 fires.
    """
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status="in_transit",
    ) == IN_TRANSIT


def test_recent_orders_delivered_with_no_partner_status():
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status="delivered",
    ) == DELIVERED


# ── Genuine pre-dispatch states should NOT be overridden ──────────────────

def test_genuine_pickup_exception_with_no_carrier_movement():
    """
    When the partner says PICKUP EXCEPTION and Shopify has no
    shipment_status, the parcel really is stuck at the warehouse and
    Rule 3 ("packed and ready, dispatch tomorrow") is correct.
    """
    assert resolve_logistics_status(
        raw_partner_status="PICKUP EXCEPTION",
        shipment_status=None,
    ) == PICKUP_EXCEPTION


def test_genuine_pickup_rescheduled_with_empty_shipment_status():
    assert resolve_logistics_status(
        raw_partner_status="PICKUP RESCHEDULED",
        shipment_status="",
    ) == PICKUP_RESCHEDULED


def test_ready_to_ship_passes_through():
    assert resolve_logistics_status(
        raw_partner_status="READY TO SHIP",
        shipment_status=None,
    ) == READY_TO_SHIP


# ── Case-insensitivity of the partner status ──────────────────────────────
# This is the bug that would have prevented PR #654's helper from ever
# firing in production, because the Shiprocket adapter uppercases.

def test_partner_status_uppercase_resolves():
    """Production Shiprocket adapter returns uppercase."""
    assert resolve_logistics_status(
        raw_partner_status="PICKUP EXCEPTION",
        shipment_status=None,
    ) == PICKUP_EXCEPTION


def test_partner_status_pascal_case_resolves():
    """Defensive: handle Pascal case too in case a partner emits it."""
    assert resolve_logistics_status(
        raw_partner_status="Pickup Exception",
        shipment_status=None,
    ) == PICKUP_EXCEPTION


def test_partner_status_lowercase_resolves():
    assert resolve_logistics_status(
        raw_partner_status="pickup exception",
        shipment_status=None,
    ) == PICKUP_EXCEPTION


def test_partner_status_with_surrounding_whitespace():
    assert resolve_logistics_status(
        raw_partner_status="  PICKUP EXCEPTION  ",
        shipment_status=None,
    ) == PICKUP_EXCEPTION


# ── In-transit synonyms ────────────────────────────────────────────────────

def test_in_transit_one_word_form():
    assert resolve_logistics_status(
        raw_partner_status="INTRANSIT",
        shipment_status=None,
    ) == IN_TRANSIT


def test_in_transit_hyphen_form():
    assert resolve_logistics_status(
        raw_partner_status="IN-TRANSIT",
        shipment_status=None,
    ) == IN_TRANSIT


# ── Out-for-delivery family ────────────────────────────────────────────────

def test_partner_ofd_resolves_to_out_for_delivery():
    assert resolve_logistics_status(
        raw_partner_status="OFD",
        shipment_status=None,
    ) == OUT_FOR_DELIVERY


def test_shopify_out_for_delivery_resolves():
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status="out_for_delivery",
    ) == OUT_FOR_DELIVERY


def test_shopify_attempted_delivery_treated_as_out_for_delivery():
    """Customer-facing wording is the same: courier was at the address."""
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status="attempted_delivery",
    ) == OUT_FOR_DELIVERY


# ── Reached destination hub ────────────────────────────────────────────────

def test_shopify_ready_for_pickup_maps_to_reached_destination_hub():
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status="ready_for_pickup",
    ) == REACHED_DESTINATION_HUB


# ── Cancellation short-circuit ─────────────────────────────────────────────

def test_cancelled_order_returns_none_even_with_in_transit_shipment():
    """Cancellation wording is handled separately; do not fire Rules 1-6."""
    assert resolve_logistics_status(
        raw_partner_status="IN TRANSIT",
        shipment_status="in_transit",
        cancelled_at="2026-05-20T10:00:00+05:30",
    ) is None


def test_cancelled_order_returns_none_for_partner_exception():
    assert resolve_logistics_status(
        raw_partner_status="PICKUP EXCEPTION",
        shipment_status=None,
        cancelled_at="2026-05-20T10:00:00+05:30",
    ) is None


# ── No-signal cases ────────────────────────────────────────────────────────

def test_all_none_returns_none():
    assert resolve_logistics_status(
        raw_partner_status=None,
        shipment_status=None,
    ) is None


def test_empty_strings_return_none():
    assert resolve_logistics_status(
        raw_partner_status="",
        shipment_status="",
    ) is None


def test_unknown_partner_status_returns_none():
    """Unknown partner strings should not surface; caller must fall back."""
    assert resolve_logistics_status(
        raw_partner_status="WEIRD CARRIER STATE THE PROMPT DOES NOT KNOW",
        shipment_status=None,
    ) is None
