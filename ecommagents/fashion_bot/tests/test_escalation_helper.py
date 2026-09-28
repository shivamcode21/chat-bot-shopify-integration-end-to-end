"""Unit tests for the customer-facing escalation footer helper.

Covers ``append_escalation_footer``: the append must be idempotent so the
customer never sees the same SLA sentence twice. Production regression it
fixes — conversation ``7c7ea023`` on 2026-08-25 14:18 IST, where the model had
learnt the footer from five prior turns, closed its own reply with it, and the
node appended a second copy (678-char message, footer x2).
"""
from __future__ import annotations

from fashion_bot.utils.escalation_helper import append_escalation_footer

FOOTER = (
    "Our support team will reach out to you within 48 working hours. "
    "Monday to Friday between 10 am to 7 pm."
)


def test_appends_footer_when_absent():
    """Unchanged behaviour for the ordinary case: one blank line, then footer."""
    assert append_escalation_footer("We have flagged it.", FOOTER) == (
        f"We have flagged it.\n\n{FOOTER}"
    )


def test_does_not_duplicate_when_llm_already_closed_with_footer():
    reply = f"We have flagged it.\n\n{FOOTER}"
    assert append_escalation_footer(reply, FOOTER) == reply


def test_collapses_footer_the_model_repeated():
    """The 3x production case: model wrote it twice, node appended a third."""
    reply = f"Sorry for the wait.\n\n{FOOTER}\n\n{FOOTER}"
    assert append_escalation_footer(reply, FOOTER) == f"Sorry for the wait.\n\n{FOOTER}"


def test_is_idempotent_under_repeated_application():
    once = append_escalation_footer("We have flagged it.", FOOTER)
    assert append_escalation_footer(once, FOOTER) == once
    assert append_escalation_footer(append_escalation_footer(once, FOOTER), FOOTER) == once


def test_matches_footer_despite_whitespace_and_case_drift():
    """The model reproduces the line with its own spacing — still one footer.

    Its rendition is recognised as the footer and replaced by the canonical
    wording from config, rather than being kept alongside a second copy.
    """
    reply = (
        "We have flagged it.\n\nour support team will reach out to you within 48\n"
        "working hours.  Monday to Friday between 10 am to 7 pm."
    )
    assert append_escalation_footer(reply, FOOTER) == f"We have flagged it.\n\n{FOOTER}"


def test_footer_woven_into_the_body_is_left_in_place():
    reply = f"{FOOTER} Meanwhile, your tracking link is https://example.com/t/1"
    assert append_escalation_footer(reply, FOOTER) == reply


def test_empty_reply_yields_the_bare_footer():
    assert append_escalation_footer("", FOOTER) == FOOTER
    assert append_escalation_footer(None, FOOTER) == FOOTER


def test_blank_or_missing_footer_leaves_the_reply_untouched():
    assert append_escalation_footer("We have flagged it.", None) == "We have flagged it."
    assert append_escalation_footer("We have flagged it.", "   ") == "We have flagged it."


def test_trailing_whitespace_between_body_and_footer_is_tidied():
    """A trailing blank block must not push the footer a paragraph further out."""
    assert append_escalation_footer("We have flagged it.\n\n", FOOTER) == (
        f"We have flagged it.\n\n{FOOTER}"
    )
