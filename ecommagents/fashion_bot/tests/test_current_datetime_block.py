"""
Regression tests for ``_build_current_datetime_block`` — the authoritative
current-date grounding injected into every skill agent's system prompt.

Incident (Groovee, web chat, 25 Jul): a customer placing a COD pre-order
asked for the exact arrival date. The delivery-timeline prompt only carries
day-ranges ("dispatch in 7-10 days", "delivery in 15-20 days") and nothing in
the system prompt told the LLM what "today" was, so it invented calendar
dates — replying that the order would arrive "15-20 July", a window already in
the PAST on the day it spoke. Nothing anchored the arithmetic to the real
current date.

These tests lock down that:
  1. the block is emitted with a concrete, machine-checkable current date, and
  2. it carries the anti-hallucination rules (compute-from-today, never emit a
     past date) so a prompt can rely on them instead of guessing.
"""

import re
from datetime import datetime

import pytz

from fashion_bot.nodes.generic_skill_node import _build_current_datetime_block


def test_block_contains_todays_iso_date_in_ist():
    """The block must state today's real IST date, not a training-data guess."""
    block = _build_current_datetime_block()
    today_iso = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    assert today_iso in block


def test_block_marks_itself_authoritative_and_system_provided():
    block = _build_current_datetime_block()
    assert "AUTHORITATIVE" in block
    assert "SYSTEM-PROVIDED" in block


def test_block_forbids_past_and_invented_dates():
    """The two rules that would have prevented the '15-20 July on 25 Jul' bug."""
    block = _build_current_datetime_block().lower()
    # Never emit a delivery/arrival date on or before today.
    assert "on or before today" in block
    # Never guess/invent/recall a specific date; compute from today or use a tool.
    assert "never guess" in block or "never guess, invent" in block
    assert "computed from today" in block


def test_block_instructs_computing_from_today():
    """Day-range → calendar conversion must add days to today, not anchor elsewhere."""
    block = _build_current_datetime_block()
    assert re.search(r"add", block, re.IGNORECASE)
    assert "today" in block.lower()
