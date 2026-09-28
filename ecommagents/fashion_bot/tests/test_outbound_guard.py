"""Unit tests for the last-mile outbound leak guard.

Covers the Concept Groove production leak of 2026-08-01..04, where the
early/fast-delivery paths (which forbid tool calls and mandate an exact
sentence) handed the model an unfilled ``{{support_team_contact_details}}``
slot and it answered with an invented ``+91 99999 99999``.

Fully offline: the guard is pure, so no state, Redis, or LLM is involved.
"""

import pytest

from fashion_bot.utils.outbound_guard import (
    PLACEHOLDER_PHONE,
    PLACEHOLDER_TOKEN,
    TOOL_CALL_DIRECTIVE,
    sanitize_outbound_text,
)

# Verbatim replies pulled from the Loki logs for client c3ffcb1b (Concept
# Groove) during the incident window.
LEAKED_SPACED_PHONE = (
    "We have noted your early delivery request. You can contact the support "
    "team directly at +91 99999 99999 for tracking your order."
)
LEAKED_PHONE_WITH_EMAIL = (
    "We have noted your early delivery request. You can contact the support "
    "team directly at support@cncptgroove.com or +91 9999999999 for tracking "
    "your order."
)

# Real tenant contact details, from client_configs.vendor_contact_details.
# None of these may ever be stripped.
REAL_CONTACT_REPLIES = [
    "You can contact the support team directly at contact@serekoshop.com or "
    "+91 8588860547 for tracking your order.",
    "Reach us at care@iconicindia.com or +91 730 6660 660.",
    "Call 1800 120 000 500 (India) or +91 9674373838 (International).",
    # Posh Affair's real line — four consecutive 9s, one short of the bar.
    "Write to support@poshaffair.co or call +919999727891.",
    "Our numbers are +91 8607845846, +91 9518217803, +91 8076038573.",
]


# ---------------------------------------------------------------------------
# Placeholder phone numbers
# ---------------------------------------------------------------------------

def test_strips_spaced_dummy_phone_and_its_connector():
    result = sanitize_outbound_text(LEAKED_SPACED_PHONE)

    assert result.blocked
    assert result.kinds == [PLACEHOLDER_PHONE]
    assert result.violations[0].matched == "+91 99999 99999"
    # The clause connector goes with it, so the sentence still reads correctly.
    assert result.text == (
        "We have noted your early delivery request. You can contact the "
        "support team directly for tracking your order."
    )


def test_strips_dummy_phone_but_keeps_the_real_email_beside_it():
    result = sanitize_outbound_text(LEAKED_PHONE_WITH_EMAIL)

    assert result.kinds == [PLACEHOLDER_PHONE]
    assert "9999999999" not in result.text
    assert "support@cncptgroove.com" in result.text
    assert "directly at support@cncptgroove.com for tracking" in result.text


@pytest.mark.parametrize(
    "phone",
    ["+91 99999 99999", "9999999999", "+91 9999999999", "0000000000", "+911111111111"],
)
def test_repeated_digit_phones_are_blocked(phone):
    result = sanitize_outbound_text(f"Please call us at {phone} for help.")

    assert result.kinds == [PLACEHOLDER_PHONE]
    assert phone not in result.text


@pytest.mark.parametrize("reply", REAL_CONTACT_REPLIES)
def test_real_support_numbers_pass_through_untouched(reply):
    result = sanitize_outbound_text(reply)

    assert not result.blocked
    assert result.text == reply


@pytest.mark.parametrize(
    "reply",
    [
        "📦 Order #gv18022 | 📅 16 Aug 2026 | 💰 1700",
        "Your order 999999 is out for delivery.",  # 6 digits, under the length bar
        "That'll be ₹2,999 including shipping.",
    ],
)
def test_non_phone_digit_runs_are_not_touched(reply):
    assert sanitize_outbound_text(reply).text == reply


# ---------------------------------------------------------------------------
# Unresolved template placeholders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "token",
    ["{{support_team_contact_details}}", "{support_team_contact_details}"],
)
def test_unresolved_placeholder_is_stripped_in_both_brace_forms(token):
    result = sanitize_outbound_text(
        f"You can reach us on WhatsApp or email at {token} anytime."
    )

    assert result.kinds == [PLACEHOLDER_TOKEN]
    assert result.violations[0].matched == token
    assert result.text == "You can reach us on WhatsApp or email anytime."


def test_reply_that_is_only_a_placeholder_falls_back_to_a_safe_line():
    result = sanitize_outbound_text("{{support_team_contact_details}}")

    assert result.blocked
    assert "{" not in result.text
    assert result.text.strip()  # never delivers an empty message


@pytest.mark.parametrize(
    "reply",
    [
        'Here is the raw payload: {"order_id": 123, "status": "shipped"}',
        "Use the {} button on the site.",
        "Sizes available: {S, M, L}",  # not identifier-shaped
    ],
)
def test_braces_that_are_not_template_tokens_are_left_alone(reply):
    assert sanitize_outbound_text(reply).text == reply


# ---------------------------------------------------------------------------
# Guard contract
# ---------------------------------------------------------------------------

def test_both_violation_kinds_are_reported_together():
    result = sanitize_outbound_text(
        "Email {{support_email}} or call +91 99999 99999 for help."
    )

    assert set(result.kinds) == {PLACEHOLDER_TOKEN, PLACEHOLDER_PHONE}
    assert len(result.violations) == 2


def test_guard_is_idempotent_so_it_can_run_at_several_hops():
    once = sanitize_outbound_text(LEAKED_SPACED_PHONE)
    twice = sanitize_outbound_text(once.text)

    assert twice.text == once.text
    assert not twice.blocked


def test_clean_reply_is_returned_byte_for_byte():
    reply = "Your order #gv18022 shipped on 16 Aug and arrives in 2-3 days. 📦"
    result = sanitize_outbound_text(reply)

    assert result.text is reply
    assert result.violations == []


@pytest.mark.parametrize("value", ["", None])
def test_empty_input_is_handled_without_raising(value):
    assert sanitize_outbound_text(value).text == ""


# ---------------------------------------------------------------------------
# Wiring: the guard must actually run on the shared outbound path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_graph_response_guards_the_end_event_and_the_transcript(monkeypatch):
    """Both channels read the reply off this `end` event, so it must be clean.

    Gupshup sends `full_response`; webchat stores it. `result.customer_message`
    is what gets persisted, so it has to match what the customer received.
    """
    import sys
    import types

    ss = pytest.importorskip("fashion_bot.core.streaming_service")

    class _LeakyGraph:
        async def astream(self, _state, stream_mode=None, config=None):
            yield ("custom", {"type": "token", "content": LEAKED_SPACED_PHONE})
            yield ("values", {"customer_message": LEAKED_SPACED_PHONE, "messages": []})

    fake_graph_mod = types.ModuleType("fashion_bot.graph_context_meta")
    fake_graph_mod.graph = _LeakyGraph()
    monkeypatch.setitem(sys.modules, "fashion_bot.graph_context_meta", fake_graph_mod)

    state = {"trace_id": "t1", "phone_number": "9592383936"}
    events = [
        event
        async for event in ss.stream_graph_response(state, "I need this early", "c1")
    ]

    end_event = events[-1]
    assert end_event["type"] == "end"
    assert "99999" not in end_event["full_response"]
    assert end_event["full_response"].endswith("directly for tracking your order.")
    assert end_event["result"]["customer_message"] == end_event["full_response"]


@pytest.mark.asyncio
async def test_whatsapp_send_path_guards_replies_that_bypass_the_stream(monkeypatch):
    """Canned/orchestrator sends never pass through stream_graph_response.

    The last-mile guard on the WhatsApp send is what covers them.
    """
    gw = pytest.importorskip("fashion_bot.gupshup_webhook")

    sent = {}

    async def _fake_send_message(to, message, trace_id, client_id=None):
        sent["to"] = to
        sent["message"] = message

    monkeypatch.setattr(gw, "send_message", _fake_send_message)

    await gw._send_whatsapp_message_to_customer(
        sender_phone="919592383936",
        text=LEAKED_SPACED_PHONE,
        trace_id="t1",
        client_id="c3ffcb1b-afb9-4ca4-8746-a06698bec870",
        langsmith_trace_id="ls1",
        operation_name="test.send",
    )

    assert "99999" not in sent["message"]
    assert sent["message"].endswith("directly for tracking your order.")


# ---------------------------------------------------------------------------
# Verbatim shapes pulled from the `messages` table (real delivered replies)
# ---------------------------------------------------------------------------

def test_blocks_the_contact_block_shape_that_reached_customers_on_aug_13():
    """The QA-reported shape: correct email, fabricated phone, on its own line.

    Dropping the number would strand a bare "Phone" label, so the whole label
    line goes with it.
    """
    delivered = (
        "If you need anything else, please reach out to our support team "
        "directly at:\n\nEmail: support@groovee.in\nPhone: +919999999999 "
        "(Available Monday to Saturday, 11:30 AM to 6:30 PM)\n\n"
        "Our support team will help you further."
    )

    result = sanitize_outbound_text(delivered)

    assert result.kinds == [PLACEHOLDER_PHONE]
    assert "9999999999" not in result.text
    assert "Phone" not in result.text          # no stranded label
    assert "Email: support@groovee.in" in result.text   # real detail survives
    assert "\n\n\n" not in result.text


def test_blocks_dummy_phone_but_keeps_the_hours_that_follow_it():
    delivered = (
        "You may also contact our support team directly at support@no-mercy.in "
        "or +91 9999999999, Monday to Saturday, 11:30 AM to 6:30 PM."
    )

    result = sanitize_outbound_text(delivered)

    assert "9999999999" not in result.text
    assert result.text == (
        "You may also contact our support team directly at support@no-mercy.in, "
        "Monday to Saturday, 11:30 AM to 6:30 PM."
    )


def test_delivered_raw_placeholder_is_blocked():
    """This exact string was delivered to customers on 2026-08-01 and 08-03."""
    delivered = (
        "Yes, it might be possible to deliver the order earlier. You can "
        "contact the support team directly at {{support_team_contact_details}} "
        "for exact delivery timelines."
    )

    result = sanitize_outbound_text(delivered)

    assert result.kinds == [PLACEHOLDER_TOKEN]
    assert "{" not in result.text


def test_a_label_line_that_still_has_a_real_value_is_never_dropped():
    reply = (
        "Reach us:\nEmail: care@clarks.in\nPhone: +91-9653306311\n"
        "Or call +91 9999999999 anytime."
    )

    result = sanitize_outbound_text(reply)

    assert "Email: care@clarks.in" in result.text
    assert "Phone: +91-9653306311" in result.text   # real number and its label stay
    assert "9999999999" not in result.text


# ---------------------------------------------------------------------------
# False positives found by sweeping the guard over 112,990 real bot replies.
# Both of these shipped to customers legitimately and must survive the guard.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "reply",
    [
        # Gant / Iconic India catalogue slugs carry long zero runs. Stripping a
        # digit run out of one silently produces a dead product link.
        "1. [Gant Men Black Solid Polo Tshirt]"
        "(https://gant.in/products/gant-gmw25-000000022105-black-polo-tshirt) — ₹4,024",
        "   https://www.iconicindia.com/products/gant-gms25-000000022205black-t-shirt",
        "https://www.iconicindia.com/products/gant-gms25-0000002220277-beige-polo-tshirt",
    ],
)
def test_product_url_slugs_are_never_treated_as_phone_numbers(reply):
    result = sanitize_outbound_text(reply)

    assert not result.blocked
    assert result.text == reply


def test_a_real_customer_phone_with_six_repeated_digits_survives():
    """8056666668 is a genuine number we echo back in order details.

    Six in a row is reachable for a real mobile; the dummies all carry ten.
    """
    reply = (
        "*   **Customer Name:** V. Saravanakumar\n"
        "*   **Phone:** 8056666668\n"
        "*   **Delivery Address:** 1657/3"
    )

    result = sanitize_outbound_text(reply)

    assert not result.blocked
    assert "8056666668" in result.text


# ---------------------------------------------------------------------------
# Calibration, from a backtest over 3 months of bot replies (57,516 messages,
# 638 distinct 9-15 digit numbers). Longest repeated run in a genuine number
# was 6; shortest in a dummy was 8. The bar sits in that empty band.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "number",
    [
        "8056666668",      # 6 in a row — the longest run in any real number seen
        "919999933221",    # 5
        "9904444427",      # 5
        "919999961892",    # 5
        "+916238888307",   # 4
        "9300070000",      # 4
        "9620976666",      # 4
        "+91 9999883408",  # 4
    ],
)
def test_real_customer_numbers_from_the_backtest_are_kept(number):
    reply = f"I've checked the orders for {number} and everything is on track."
    assert not sanitize_outbound_text(reply).blocked


@pytest.mark.parametrize("number", ["+91 99999 99999", "9999999999", "5900000000"])
def test_every_dummy_from_the_backtest_is_blocked(number):
    reply = f"You can reach our support team at {number} for help."
    assert sanitize_outbound_text(reply).kinds == [PLACEHOLDER_PHONE]


@pytest.mark.parametrize(
    "reply",
    [
        "Your order id is 11111111 and it ships tomorrow.",   # 8 digits
        "Reference 0000000 has been logged.",                 # 7 digits
        "That comes to 2222222 loyalty points.",              # 7 digits
    ],
)
def test_short_digit_runs_are_not_phone_numbers(reply):
    """Under 9 digits nothing is dialable, so length rules it out first."""
    assert not sanitize_outbound_text(reply).blocked


# ---------------------------------------------------------------------------
# Bracketed tool-call directives that leaked into customer replies.
#
# Concept Groove delivered ``[Call get_contact_information]`` and
# ``[Email provided by get_contact_information]`` on 2026-08-26/27 — the model
# described the tool call in prose instead of invoking it, and the raw directive
# reached the customer. Every real fashion_bot tool name is snake_case with two
# or more underscore-joined tokens, which is what anchors the pattern.
# ---------------------------------------------------------------------------

LEAKED_TOOL_DIRECTIVE_INLINE = (
    "I am sorry to hear you are having trouble with the EMI option. Since I "
    "don't have access to your screen, please contact us at "
    "[Call get_contact_information] for immediate help."
)
LEAKED_TOOL_DIRECTIVE_LABELS = (
    "Please reach out to our team:\n"
    "Phone: [Call the support number provided by get_contact_information]\n"
    "Email: [Email provided by get_contact_information]"
)


def test_strips_a_bracketed_call_directive_from_the_reply():
    result = sanitize_outbound_text(LEAKED_TOOL_DIRECTIVE_INLINE)

    assert result.blocked
    assert result.kinds == [TOOL_CALL_DIRECTIVE]
    assert result.violations[0].matched == "[Call get_contact_information]"
    assert "get_contact_information" not in result.text
    assert result.text.endswith("please contact us for immediate help.")


def test_strips_every_bracketed_directive_and_leaves_no_orphan_labels():
    result = sanitize_outbound_text(LEAKED_TOOL_DIRECTIVE_LABELS)

    assert result.kinds == [TOOL_CALL_DIRECTIVE]
    assert len(result.violations) == 2
    assert "get_contact_information" not in result.text
    # The "Phone" / "Email" labels lose their values, so they go too.
    assert "Phone" not in result.text
    assert "Email" not in result.text
    assert result.text.startswith("Please reach out to our team")


@pytest.mark.parametrize(
    "reply",
    [
        # Escalation metadata never carries an underscore identifier, so the
        # existing internal tag shape is untouched.
        "[Escalation] Payment/Refund Status: refund not received",
        "[Escalation] Cancellation: customer changed mind",
        # A markdown link with a snake_case slug inside must not be treated as
        # a tool-call directive — the trailing ``(url)`` rules it out.
        "1. [gant-gmw25-000000022105-black-polo-tshirt](https://gant.in/products/gant-gmw25-000000022105-black-polo-tshirt) — ₹4,024",
        "Check the [size chart](https://groovee.in/pages/size-chart) here.",
        # Real prose that contains an underscore identifier as a bare word (no
        # bracketing) is fine — only bracketed directives are stripped.
        "Your tracking id is AWB_12345678.",
    ],
)
def test_directive_pattern_does_not_swallow_legitimate_content(reply):
    result = sanitize_outbound_text(reply)

    assert not result.blocked
    assert result.text == reply


def test_directive_leak_is_idempotent_across_multiple_hops():
    once = sanitize_outbound_text(LEAKED_TOOL_DIRECTIVE_INLINE)
    twice = sanitize_outbound_text(once.text)

    assert twice.text == once.text
    assert not twice.blocked


# ---------------------------------------------------------------------------
# Post-guard enrichment: inject real contact info after stripping directives.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_enrich_appends_contact_details_to_stripped_reply(monkeypatch):
    from fashion_bot.utils.outbound_guard import enrich_after_tool_call_directive, OutboundViolation

    async def _mock_aget_config(key, client_id=None):
        return {
            "support email id": "care@cncptgroove.com",
            "support phone numbers": "+91 8588860547",
        }

    monkeypatch.setattr(
        "fashion_bot.config_manager.aget_config", _mock_aget_config
    )

    stripped = (
        "I am sorry to hear you are having trouble with the EMI option. "
        "Since I don't have access to your screen, please contact us for "
        "immediate help."
    )
    violations = [
        OutboundViolation(kind=TOOL_CALL_DIRECTIVE, matched="[Call get_contact_information]")
    ]
    result = await enrich_after_tool_call_directive(stripped, "test-client-id", violations=violations)

    assert "+91 8588860547" in result
    assert "care@cncptgroove.com" in result


@pytest.mark.asyncio
async def test_enrich_returns_text_unchanged_when_no_contact_configured(monkeypatch):
    from fashion_bot.utils.outbound_guard import enrich_after_tool_call_directive, OutboundViolation

    async def _mock_aget_config(key, client_id=None):
        return None

    monkeypatch.setattr(
        "fashion_bot.config_manager.aget_config", _mock_aget_config
    )

    text = "Please reach out to our team."
    violations = [
        OutboundViolation(kind=TOOL_CALL_DIRECTIVE, matched="[Call get_contact_information]")
    ]
    result = await enrich_after_tool_call_directive(text, "test-client-id", violations=violations)

    assert result == text


@pytest.mark.asyncio
async def test_enrich_is_safe_with_no_client_id():
    from fashion_bot.utils.outbound_guard import enrich_after_tool_call_directive

    text = "Please contact us for help."
    result = await enrich_after_tool_call_directive(text, None)

    assert result == text


@pytest.mark.asyncio
async def test_enrich_skips_non_contact_related_tool_directives():
    from fashion_bot.utils.outbound_guard import enrich_after_tool_call_directive, OutboundViolation

    text = "Here are the products I found for you."
    violations = [
        OutboundViolation(kind=TOOL_CALL_DIRECTIVE, matched="[Call search_products]")
    ]
    result = await enrich_after_tool_call_directive(text, "test-client-id", violations=violations)

    assert result == text


def test_emphasis_markers_do_not_survive_a_stripped_number():
    """Real shape: "call us at **+91-9999999999** for immediate assistance"."""
    reply = (
        "Please reach our support team directly at **support@groovee.in** "
        "or call us at **+91-9999999999** for immediate assistance."
    )

    result = sanitize_outbound_text(reply)

    assert result.kinds == [PLACEHOLDER_PHONE]
    assert "****" not in result.text
    assert "**support@groovee.in**" in result.text
    assert result.text.endswith("for immediate assistance.")
