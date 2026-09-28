"""Unit tests for cancellation-event order-number extraction.

Covers the production gap where the Cancellation Requests page showed "—" for
the order on ~33% of events:

  1. `_extract_order_id`'s message fallback only matched `gv1234`, one client's
     order format. Clients numbering orders differently (#71379, 70849, ...)
     fell through the branch entirely — 0% order coverage for two of them.
  2. Broadening it must not start capturing phone numbers, which is exactly what
     a bare run of digits usually is: the bot asks for the customer's number,
     and that reply is the very next message. Numeric orders therefore require
     an explicit `#` or "order" marker.

These run fully offline (pure function, no DB or LLM calls).
"""

import pytest

from fashion_bot.analytics.cancellation_aversion_tracker import _extract_order_id


def _msg_only(message):
    """Drive the message-regex branch: empty state, so earlier priorities miss."""
    return _extract_order_id({}, message)


# --- alphanumeric order codes (the previously-supported shape) ---------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "message, expected",
    [
        ("gv16956", "GV16956"),
        ("#gv16956", "GV16956"),
        ("GV-16956", "GV16956"),
        ("order gv16956", "GV16956"),
        ("please cancel my order gv17074 today", "GV17074"),
    ],
)
def test_extracts_alphanumeric_order_codes(message, expected):
    assert _msg_only(message) == expected


# --- purely numeric orders (the regression this closes) ---------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "message, expected",
    [
        ("#71379", "71379"),
        ("my order is #71379", "71379"),
        ("order 71379", "71379"),
        ("cancel order no 70849", "70849"),
        ("Order #70971 please cancel", "70971"),
        ("order number 69375", "69375"),
    ],
)
def test_extracts_numeric_orders(message, expected):
    assert _msg_only(message) == expected


# --- phone numbers must never be mistaken for orders ------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "9700306917",        # bare 10-digit phone — the reply to "give me your number"
        "97003 06917",       # spaced phone
        "916398242997",      # country-code prefixed
        "my number is 6398242997",
    ],
)
def test_does_not_capture_phone_numbers(message):
    assert _msg_only(message) is None


# --- ordinary cancellation phrasing carries no order ------------------------

@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "How to cancel my order",
        "I want to cancel my order",
        "Can u cancel the order",
        "order 2 items",   # too few digits to be an order number
        "Ok",
        "",
    ],
)
def test_returns_none_without_an_order(message):
    assert _msg_only(message) is None


# --- explicit state still wins over the message fallback --------------------

@pytest.mark.unit
def test_selected_order_id_takes_priority_over_message():
    state = {"selected_order_id": "71379"}
    assert _extract_order_id(state, "order gv16956") == "71379"
