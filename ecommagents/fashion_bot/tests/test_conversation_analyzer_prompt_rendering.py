"""Regression tests for analytics prompt rendering.

Background: tenant prompts stored in ``agents_config.agent_prompt`` contain a
"BUSINESS POLICIES" section referencing the client's policy variables, plus a
JSON output schema with literal braces. ``str.format()`` parses the whole
template, so either one aborted the render and every conversation for the
affected tenant was silently analysed with the generic default prompt.
"""

import pytest


conversation_analyzer = pytest.importorskip("fashion_bot.analytics.conversation_analyzer")

_render_prompt = conversation_analyzer._render_prompt
_find_unresolved_placeholders = conversation_analyzer._find_unresolved_placeholders


# Mirrors the real production template: policy placeholders (three bare, one
# escaped) followed by the JSON output schema.
TENANT_PROMPT = """Conversation:
{conversation_text}

Client: {client_id} | Phone: {phone}
Messages: {message_count} | Duration: {duration_minutes}
Post-conversation orders: {post_conversation_orders}

4. BUSINESS POLICIES (Men's fashion and apparel business):
   - Return/Exchange Policy: {return_exchange_policy}
   - Delivery Policy: {delivery_policy}
   - After Delivery Return/Exchange: {after_delivery_return_exchange}
   - Contact: Always provide {{support_team_contact_details}} for support inquiries.

== Output Schema ==
Return ONLY a valid JSON object:
{
  "cancellation_analysis": {"cancellation_attempted": true},
  "satisfaction": {"sentiment": "positive"}
}"""


def _kwargs(**overrides):
    base = {
        "conversation_text": "Customer: hi",
        "client_id": "a78f071b-1b88-4f3b-82bb-7ee7ee3ad364",
        "phone": "+919999999999",
        "message_count": 4,
        "duration_minutes": 7,
        "post_conversation_orders": "(none)",
        "return_exchange_policy": "Q: What is your return policy?\nA: 30 days from delivery.",
        "delivery_policy": "Q: Do you deliver outside India?\nA: No.",
        "after_delivery_return_exchange": "Q: Order Return process\nA: Within 30 days.",
        "support_team_contact_details": "Q: support email id\nA: connect@vahro.in",
    }
    base.update(overrides)
    return base


def test_str_format_still_fails_on_the_tenant_prompt():
    """Guards the premise: this template is genuinely unformattable."""
    with pytest.raises((KeyError, IndexError, ValueError)):
        TENANT_PROMPT.format(**_kwargs())


def test_render_substitutes_policy_placeholders():
    rendered = _render_prompt(TENANT_PROMPT, _kwargs())

    assert "{return_exchange_policy}" not in rendered
    assert "{delivery_policy}" not in rendered
    assert "{after_delivery_return_exchange}" not in rendered
    assert "30 days from delivery." in rendered
    assert "Q: Do you deliver outside India?" in rendered


def test_escaped_field_stays_literal_like_str_format():
    """``{{name}}`` is escaped text, so it collapses to ``{name}`` — not a value.

    17 tenants share this construct and have always been served the literal
    token; the renderer must not silently start substituting it for them.
    """
    rendered = _render_prompt(TENANT_PROMPT, _kwargs())

    assert "Always provide {support_team_contact_details} for support" in rendered
    assert "connect@vahro.in" not in rendered
    assert "{{" not in rendered and "}}" not in rendered


def test_json_output_schema_survives_rendering():
    rendered = _render_prompt(TENANT_PROMPT, _kwargs())

    assert '"cancellation_analysis": {"cancellation_attempted": true}' in rendered
    assert '"satisfaction": {"sentiment": "positive"}' in rendered


def test_json_schema_is_not_reported_as_unresolved():
    kwargs = _kwargs()

    assert _find_unresolved_placeholders(TENANT_PROMPT, kwargs) == []


def test_unsupplied_placeholder_is_reported():
    kwargs = _kwargs()
    template = TENANT_PROMPT + "\nStore hours: {store_opening_hours}"

    assert _find_unresolved_placeholders(template, kwargs) == ["store_opening_hours"]


def test_customer_message_cannot_trigger_a_spurious_alert():
    """Unresolved detection reads the template, not customer-supplied content."""
    kwargs = _kwargs(conversation_text="Customer: is {store_opening_hours} a typo?")

    assert _find_unresolved_placeholders(TENANT_PROMPT, kwargs) == []


def test_customer_message_cannot_inject_a_placeholder():
    """Inserted text is never rescanned, so chat content stays inert."""
    kwargs = _kwargs(conversation_text="Customer: {return_exchange_policy}")
    rendered = _render_prompt(TENANT_PROMPT, kwargs)

    assert "Customer: {return_exchange_policy}" in rendered
    # The real policy still renders once, in the policies section.
    assert rendered.count("30 days from delivery.") == 1


# ---------------------------------------------------------------------------
# Backward compatibility: templates that already format cleanly must render
# byte-identically. A production scan confirmed these prompts use only escaped
# braces and named fields — no format specs, conversions, positional fields,
# attribute access or indexing — so this corpus covers the real constructs.
# ---------------------------------------------------------------------------

HEALTHY_PROMPT = """Conversation:
{conversation_text}

Client: {client_id} | Phone: {phone}
Messages: {message_count} | Duration: {duration_minutes}
Orders: {post_conversation_orders}

   - Contact: Always provide {{support_team_contact_details}} for support.

== Output Schema ==
{{
  "cancellation_analysis": {{"cancellation_attempted": true}},
  "satisfaction": {{"sentiment": "positive"}}
}}"""

EQUIVALENCE_CASES = [
    HEALTHY_PROMPT,
    "no placeholders at all",
    "{conversation_text}",
    "{{escaped}}",
    "{{}}",
    "}}{{",
    "{{{conversation_text}}}",
    "trailing brace }} and {{ leading",
    "adjacent {phone}{client_id} fields",
]


@pytest.mark.parametrize("template", EQUIVALENCE_CASES)
def test_render_matches_str_format_when_format_succeeds(template):
    kwargs = _kwargs()

    assert _render_prompt(template, kwargs) == template.format(**kwargs)


def test_healthy_prompt_reports_nothing_unresolved():
    """A working tenant must not start emitting error logs after this change."""
    assert _find_unresolved_placeholders(HEALTHY_PROMPT, _kwargs()) == []


def test_render_leaves_unknown_tokens_untouched_instead_of_raising():
    """An unknown token degrades to literal text — it must never abort the render."""
    rendered = _render_prompt("Keep {conversation_text} and {mystery}", _kwargs())

    assert "Customer: hi" in rendered
    assert "{mystery}" in rendered


def test_default_template_renders_with_the_six_supported_keys():
    """The canonical prompt must not depend on the policy keys."""
    _, user_template, _ = conversation_analyzer._get_default_prompt_parts()

    supported = {
        "conversation_text": "Customer: hi",
        "client_id": "c1",
        "phone": "+91",
        "message_count": 2,
        "duration_minutes": 5,
        "post_conversation_orders": "(none)",
    }
    rendered = _render_prompt(user_template, supported)

    assert _find_unresolved_placeholders(user_template, supported) == []
    assert "Customer: hi" in rendered


async def test_policy_context_fetches_only_requested_keys(monkeypatch):
    """A prompt with no policies section must trigger no config reads."""
    import fashion_bot.config_manager as config_manager

    fetched = []

    async def _fake(config_key, client_id=None):
        fetched.append(config_key)
        return {"Q": "A"}

    monkeypatch.setattr(config_manager, "aget_json_config", _fake)

    assert await conversation_analyzer._aget_policy_context("c1", []) == {}
    assert fetched == []

    result = await conversation_analyzer._aget_policy_context("c1", ["delivery_policy"])
    assert fetched == ["delivery_policy"]
    assert "delivery_policy" in result


async def test_policy_context_ignores_unknown_placeholders(monkeypatch):
    import fashion_bot.config_manager as config_manager

    async def _fail(config_key, client_id=None):  # pragma: no cover
        raise AssertionError(f"should not fetch {config_key}")

    monkeypatch.setattr(config_manager, "aget_json_config", _fail)

    assert await conversation_analyzer._aget_policy_context("c1", ["store_hours"]) == {}


async def test_policy_context_degrades_when_config_read_fails(monkeypatch):
    """A config outage must not put a raw placeholder in front of the LLM."""
    import fashion_bot.config_manager as config_manager

    async def _boom(config_key, client_id=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(config_manager, "aget_json_config", _boom)

    result = await conversation_analyzer._aget_policy_context("c1", ["delivery_policy"])

    assert result == {"delivery_policy": conversation_analyzer._POLICY_NOT_CONFIGURED}


async def test_policy_context_marks_unconfigured_policy(monkeypatch):
    import fashion_bot.config_manager as config_manager

    async def _empty(config_key, client_id=None):
        return None

    monkeypatch.setattr(config_manager, "aget_json_config", _empty)

    result = await conversation_analyzer._aget_policy_context("c1", ["delivery_policy"])

    assert result == {"delivery_policy": conversation_analyzer._POLICY_NOT_CONFIGURED}


def test_policy_placeholder_map_covers_the_production_prompt_tokens():
    """The four tokens found in production must all be resolvable."""
    expected = {
        "return_exchange_policy",
        "delivery_policy",
        "after_delivery_return_exchange",
        "support_team_contact_details",
    }

    assert expected == set(conversation_analyzer._POLICY_PLACEHOLDER_CONFIG_KEYS)
