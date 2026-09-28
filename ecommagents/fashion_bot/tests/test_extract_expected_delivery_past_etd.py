"""Targeted regression tests for the ETA-suppression guard added to
``partner_response_mappings.extract_expected_delivery``.

Motivation: production replies were surfacing courier-side stale ETAs
(e.g. "will arrive on 20 Jul 2026" on Aug 5) because the helper handed the
raw partner ETD to the LLM unchanged even when the date sat in the past.
For in-flight orders we now hide those ETAs so the LLM cannot echo them as
a future promise; delivered orders keep the historical ETA.

"Today" is IST throughout — couriers quote IST business dates and the LLM is
grounded on IST ("Today is <date> (IST)"), so a UTC comparison would leave a
00:00-05:30 IST window each day where yesterday's ETA still reads as current.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fashion_bot.core.partner_response_mappings import (
    _IST,
    coerce_etd_date,
    _is_past_etd,
    extract_expected_delivery,
    normalize_etd,
    normalize_etd_if_current,
)


def _today():
    return datetime.now(_IST).date()


def _fmt(d):
    return d.strftime("%d %b %Y")


def test_is_past_etd_yesterday_true():
    assert _is_past_etd(_fmt(_today() - timedelta(days=1))) is True


def test_is_past_etd_today_false():
    assert _is_past_etd(_fmt(_today())) is False


def test_is_past_etd_tomorrow_false():
    assert _is_past_etd(_fmt(_today() + timedelta(days=1))) is False


def test_is_past_etd_empty_or_garbled_false():
    assert _is_past_etd("") is False
    assert _is_past_etd("soon") is False


def test_is_past_etd_uses_ist_not_utc():
    """The cutoff is the IST calendar date, which can be a day ahead of UTC."""
    from datetime import timezone

    ist_today = _today()
    utc_today = datetime.now(timezone.utc).date()
    assert ist_today in (utc_today, utc_today + timedelta(days=1))
    # Today-in-IST is never "past", even in the 18:30-24:00 UTC window where
    # the two calendars disagree.
    assert _is_past_etd(_fmt(ist_today)) is False


def _logistics_payload(etd_iso: str):
    return {
        "found": True,
        "order_data": {},
        "shipments": {"etd": etd_iso},
    }


def test_extract_hides_past_etd_by_default_for_in_flight_orders():
    past_iso = (_today() - timedelta(days=7)).strftime("%Y-%m-%d")
    payload = _logistics_payload(past_iso)
    # In-flight (default): the past ETA is suppressed so the LLM cannot echo it.
    assert extract_expected_delivery(payload) == ""


def test_extract_keeps_past_etd_when_order_is_delivered():
    past_iso = (_today() - timedelta(days=7)).strftime("%Y-%m-%d")
    payload = _logistics_payload(past_iso)
    assert extract_expected_delivery(payload, is_delivered=True) == normalize_etd(past_iso)


def test_extract_keeps_future_etd_for_in_flight_orders():
    future_iso = (_today() + timedelta(days=3)).strftime("%Y-%m-%d")
    payload = _logistics_payload(future_iso)
    assert extract_expected_delivery(payload) == normalize_etd(future_iso)


def test_extract_returns_empty_when_no_logistics_data():
    assert extract_expected_delivery(None) == ""
    assert extract_expected_delivery({}) == ""
    assert extract_expected_delivery({"found": False}) == ""


# ---------------------------------------------------------------------------
# Partner ETD shapes ``normalize_etd`` cannot normalise
#
# ``normalize_etd`` returns anything it can't parse verbatim. If the guard
# only ever looked at that output, an already-human-formatted stale ETD (a
# real Shiprocket shape — see ``tool_helpers.format_etd_date``) would sail
# straight through to the LLM.
# ---------------------------------------------------------------------------

def test_coerce_handles_shapes_normalize_etd_passes_through():
    past = _today() - timedelta(days=30)
    for raw in (
        past.strftime("%d %b %Y %I:%M %p"),   # "7 Aug 2025 08:08 AM"
        past.strftime("%b %d, %Y"),           # "Jul 20, 2026"
        past.strftime("%d-%m-%Y %H:%M:%S"),
        past.strftime("%Y-%m-%dT%H:%M:%SZ"),
    ):
        assert coerce_etd_date(raw) == past, raw


def test_stale_etd_suppressed_even_when_unnormalisable():
    past_human = (_today() - timedelta(days=30)).strftime("%d %b %Y %I:%M %p")
    # normalize_etd can't parse this shape and hands it back unchanged...
    assert normalize_etd(past_human) == past_human
    # ...but it must still not reach the LLM.
    assert extract_expected_delivery(_logistics_payload(past_human)) == ""


def test_future_unnormalisable_etd_is_preserved():
    future_human = (_today() + timedelta(days=5)).strftime("%d %b %Y %I:%M %p")
    assert extract_expected_delivery(_logistics_payload(future_human)) == future_human


def test_coerce_returns_none_for_garbage():
    assert coerce_etd_date(None) is None
    assert coerce_etd_date("") is None
    assert coerce_etd_date("soon") is None
    assert coerce_etd_date("N/A") is None


# ---------------------------------------------------------------------------
# The shared helper — used by both the partner-race path and the
# order-summary path in ``tool_factory.get_order_details``.
# ---------------------------------------------------------------------------

def test_normalize_etd_if_current_suppresses_and_preserves():
    past_iso = (_today() - timedelta(days=2)).strftime("%Y-%m-%d")
    future_iso = (_today() + timedelta(days=2)).strftime("%Y-%m-%d")

    assert normalize_etd_if_current(past_iso) == ""
    assert normalize_etd_if_current(past_iso, is_delivered=True) == normalize_etd(past_iso)
    assert normalize_etd_if_current(future_iso) == normalize_etd(future_iso)


def test_normalize_etd_if_current_passes_through_empty():
    assert normalize_etd_if_current(None) == ""
    assert normalize_etd_if_current("") == ""
    # Unparseable non-date text is preserved rather than dropped (fail-open).
    assert normalize_etd_if_current("soon") == "soon"
