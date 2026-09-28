"""
Unit tests for the helpers added to intent_detection_node alongside the
diagnostics + 400-retry change.

These cover:

  * ``_extract_provider_error_code`` — must recognise both the OpenAI SDK
    ``APIStatusError`` shape (``.status_code`` attribute) and the
    OpenRouter-style gateway-wrapped envelope
    ``{'message': 'Provider returned error', 'code': N}`` exception form.
    The 400-retry path keys on this function returning ``400`` exactly,
    so case-by-case coverage matters.

  * ``_build_conversation_history_string`` — must produce the same
    formatting on the primary path and the truncated-retry path so the
    LLM never sees a divergent prompt shape. Tests also lock down the
    third-to-last intent-annotation rule that the original inline code
    used (the offset-by-5 logic is easy to break).
"""

from langchain_core.messages import AIMessage, HumanMessage

from fashion_bot.nodes.intent_detection_node import (
    _build_conversation_history_string,
    _extract_provider_error_code,
)


# ── _extract_provider_error_code ──────────────────────────────────────────


class _FakeAPIStatusError(Exception):
    """Stand-in for openai.APIStatusError subclasses (BadRequestError, etc.)."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_extracts_status_code_from_sdk_exception():
    """OpenAI SDK shape — must read .status_code attribute directly."""
    exc = _FakeAPIStatusError("Bad Request", 400)
    assert _extract_provider_error_code(exc) == 400


def test_extracts_404_from_sdk_exception():
    exc = _FakeAPIStatusError("Not Found", 404)
    assert _extract_provider_error_code(exc) == 404


def test_extracts_429_from_sdk_exception():
    """Rate limit — different code but same extraction path."""
    exc = _FakeAPIStatusError("Rate limited", 429)
    assert _extract_provider_error_code(exc) == 429


def test_extracts_code_from_openrouter_envelope_400():
    """
    The exact production error shape that motivated this work — gateway
    forwards upstream errors as a dict whose str() form gets stuffed into
    the exception message.
    """
    exc = Exception("{'message': 'Provider returned error', 'code': 400}")
    assert _extract_provider_error_code(exc) == 400


def test_extracts_code_from_openrouter_envelope_404():
    exc = Exception("{'message': 'Provider returned error', 'code': 404}")
    assert _extract_provider_error_code(exc) == 404


def test_extracts_code_from_double_quoted_envelope():
    """Some serialisers use double quotes."""
    exc = Exception('{"message": "Provider returned error", "code": 400}')
    assert _extract_provider_error_code(exc) == 400


def test_returns_none_for_exception_without_code():
    """Network / TimeoutError / etc. — no code, never retry."""
    exc = Exception("Connection reset by peer")
    assert _extract_provider_error_code(exc) is None


def test_returns_none_for_empty_exception():
    assert _extract_provider_error_code(Exception("")) is None


def test_status_code_takes_precedence_over_envelope_text():
    """If both signals are present, trust the structured attribute."""
    exc = _FakeAPIStatusError("{'code': 999}", 400)
    assert _extract_provider_error_code(exc) == 400


def test_non_int_status_code_falls_back_to_envelope_parse():
    """Defensive: if .status_code is None/str, the envelope text still saves us."""
    class _Weird(Exception):
        status_code = None
    exc = _Weird("{'code': 404}")
    assert _extract_provider_error_code(exc) == 404


def test_ignores_two_digit_numbers_in_message():
    """The regex requires 3 digits to avoid matching arbitrary numbers."""
    exc = Exception("retry attempt 42 of 99")
    assert _extract_provider_error_code(exc) is None


# ── _build_conversation_history_string ────────────────────────────────────


def _human(text):
    return HumanMessage(content=text)


def _ai(text):
    return AIMessage(content=text)


def test_history_is_empty_string_when_no_messages():
    assert _build_conversation_history_string([], None, None) == ""


def test_history_renders_user_and_bot_lines():
    msgs = [_human("hi"), _ai("hello! how can I help"), _human("track my order")]
    out = _build_conversation_history_string(msgs, None, None)
    assert out == "User: hi\nBot: hello! how can I help\nUser: track my order"


def test_template_messages_render_first_in_chronological_order():
    """
    template_messages_context is passed newest-first by the caller; the
    helper must reverse it so the LLM sees oldest template first.
    """
    msgs = [_human("yes")]
    templates = ["Order shipped", "Order confirmed"]  # newest-first
    out = _build_conversation_history_string(msgs, templates, None)
    lines = out.splitlines()
    assert lines[0] == "--- Recent Template Messages (sent outside chat) ---"
    # Oldest template first after reversal
    assert lines[1] == "Bot (Template): Order confirmed"
    assert lines[2] == "Bot (Template): Order shipped"
    assert lines[3] == "--- End of Template Messages ---"
    assert lines[4] == "User: yes"


def test_template_messages_omitted_when_empty():
    out = _build_conversation_history_string([_human("hi")], [], None)
    assert "Template Messages" not in out
    assert out == "User: hi"


def test_intent_annotation_attaches_to_third_to_last_user_message():
    """
    Locks in the offset-by-5 rule from the original inline code. With 7
    messages the third-to-last position (idx == len-5 == 2) must be a
    HumanMessage to receive the [Detected Intent: X] annotation.
    """
    msgs = [
        _human("u1"),
        _ai("b1"),
        _human("u2"),   # idx=2, len-5=2 → annotated
        _ai("b2"),
        _human("u3"),
        _ai("b3"),
        _human("u4_current"),
    ]
    out = _build_conversation_history_string(msgs, None, "order_status")
    assert "User: u2 [Detected Intent: order_status]" in out
    # The current message must NOT be annotated
    assert "User: u4_current [Detected Intent" not in out
    # Earlier user messages must not be annotated either
    assert "User: u1 [Detected Intent" not in out


def test_intent_annotation_skipped_when_no_previous_intent():
    """No annotation when previous_intent is falsy, even at the magic offset."""
    msgs = [_human("u1"), _ai("b1"), _human("u2"), _ai("b2"), _human("u3"), _ai("b3"), _human("u4")]
    out = _build_conversation_history_string(msgs, None, None)
    assert "[Detected Intent" not in out


def test_short_history_does_not_emit_intent_annotation():
    """
    With fewer than 5 messages the magic offset never resolves to a
    HumanMessage, so no annotation is added. This matters for the
    400-retry path which truncates to last 5 — we want the truncated
    prompt to be valid even when annotation drops off.
    """
    msgs = [_human("a"), _ai("b"), _human("c")]  # len=3, len-5=-2 → never hits
    out = _build_conversation_history_string(msgs, None, "order_status")
    assert "[Detected Intent" not in out


def test_messages_without_type_treated_as_bot_lines():
    """
    Defensive: the original loop tested hasattr(m, 'type') before reading
    it, so non-message objects (rare, but possible from broken state)
    fall through to the Bot branch instead of crashing.
    """
    class _BareMessage:
        content = "raw text"
    out = _build_conversation_history_string([_BareMessage()], None, None)
    assert out == "Bot: raw text"


def test_helper_produces_identical_output_for_full_vs_truncated_callers():
    """
    This is the core invariant the 400-retry path depends on: rebuilding
    the prompt with a truncated message list must produce a string that
    is character-for-character a tail of (or otherwise consistent with)
    what the primary path would have produced. We exercise both paths
    against the same input slice and compare.
    """
    full_msgs = [_human(f"u{i}") if i % 2 == 0 else _ai(f"b{i}") for i in range(20)]
    full_out = _build_conversation_history_string(full_msgs, None, None)
    short_out = _build_conversation_history_string(full_msgs[-5:], None, None)
    # The truncated output must be the last 5 lines of the full output —
    # no leftover template markers, no offset drift, no annotation leakage.
    assert short_out == "\n".join(full_out.splitlines()[-5:])
