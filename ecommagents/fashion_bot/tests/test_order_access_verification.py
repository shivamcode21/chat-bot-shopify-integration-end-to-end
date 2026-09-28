"""Unit tests for order-access verification (web-chat second factor).

Runs fully offline: the client-config read, the Shopify recent-orders fetch and
the phone-vs-order check are all patched, so no Redis / Postgres / Shopify is
touched.

The most important cases here are the ones that prove NOTHING changed for
existing traffic: policy off, and any channel that is not web chat.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from fashion_bot.utils import order_access as oa

CLIENT = "client-abc"
CONV = "conv-1"
PHONE = "9876543210"
PINCODE = "560103"

POLICY_ON = json.dumps({"enabled": True})


def _web_state(**overrides):
    """A web-chat turn: session_id is what marks the channel as web."""
    state = {
        "client_id": CLIENT,
        "conversation_id": CONV,
        "phone_number": PHONE,
        "session_id": "web_abc123",
        "messages": [],
    }
    state.update(overrides)
    return state


def _whatsapp_state(**overrides):
    state = _web_state(**overrides)
    state.pop("session_id", None)
    state["gupshup_source_phone_number"] = "918000000000"
    return state


class _Msg:
    """Stand-in for a LangChain HumanMessage."""

    def __init__(self, content, type="human"):
        self.content = content
        self.type = type


def _said(*texts):
    return [_Msg(t) for t in texts]


def _grant(state, **kwargs):
    """Build a grant and apply it the way the runtime middleware would.

    Tests must not write state directly either — this mirrors exactly what
    OrderAuthMiddleware does with a tool's returned grant.
    """
    update = oa.build_grant(state, **kwargs)
    oa.apply_grant_update(state, update)
    return update


def _order(name, zipcode, **extra):
    order = {"name": name, "shipping_address": {"zip": zipcode}}
    order.update(extra)
    return order


@pytest.fixture
def config_on(monkeypatch):
    async def _fake_config(key, default=None, client_id=None):
        return POLICY_ON if key == oa.POLICY_CONFIG_KEY else default

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _fake_config)


@pytest.fixture
def config_off(monkeypatch):
    """No verification config for this client — every key falls back to its default.

    Still patched: the tools read other config keys (order_display_limit), and a
    test must never reach for real Redis/Postgres.
    """

    async def _fake_config(key, default=None, client_id=None):
        return default

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _fake_config)


@pytest.fixture
def config_value(monkeypatch):
    """Set an arbitrary policy JSON for one test."""

    def _apply(payload):
        async def _fake_config(key, default=None, client_id=None):
            return json.dumps(payload) if key == oa.POLICY_CONFIG_KEY else default

        monkeypatch.setattr("fashion_bot.config_manager.aget_config", _fake_config)

    return _apply


@pytest.fixture
def recent_orders(monkeypatch):
    """Patch the Shopify recent-orders fetch used by the pincode path."""

    def _apply(orders):
        async def _fake(state, phone):
            return list(orders)

        monkeypatch.setattr(oa, "_afetch_recent_orders", _fake)

    return _apply


# ==================== pure helpers ====================


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,expected",
    [("560103", "560103"), ("560 103", "560103"), ("560103-", "560103"), ("56010", ""), ("", ""), (None, "")],
)
def test_normalize_pincode(raw, expected):
    assert oa.normalize_pincode(raw) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("#GV63084", "gv63084", True),
        ("gv63084", "63084", True),
        ("63084", "63102", False),
        ("", "63084", False),
        ("gv63084", "ab63084", True),  # same digits, different prefix
    ],
)
def test_order_keys_match(left, right, expected):
    assert oa.order_keys_match(left, right) is expected


@pytest.mark.unit
def test_value_from_customer_requires_the_customer_to_have_said_it():
    state = _web_state(messages=_said("my pincode is 560103"))
    assert oa.value_from_customer("560103", state) is True
    # The model lifted this from an order it read, not from the customer.
    assert oa.value_from_customer("110001", state) is False


@pytest.mark.unit
def test_value_from_customer_ignores_bot_messages():
    state = _web_state(messages=[_Msg("is it 560103?", type="ai")])
    assert oa.value_from_customer("560103", state) is False


# ==================== grant ====================


@pytest.mark.unit
def test_grant_round_trip_and_scoping():
    state = _web_state()
    _grant(state, method="order_id", order_ids=["#63084"], phone=PHONE)
    grant = state["order_auth"]

    assert oa.grant_is_valid(grant, state, 60, phone=PHONE) is True
    assert oa.grant_covers(grant, "gv63084") is True
    assert oa.grant_covers(grant, "63102") is False

    # A different phone in the same conversation is a different customer.
    assert oa.grant_is_valid(grant, state, 60, phone="9000011111") is False

    # A different conversation never inherits the grant.
    assert oa.grant_is_valid(grant, {**state, "conversation_id": "conv-2"}, 60, phone=PHONE) is False
    # Nor does a different tenant.
    assert oa.grant_is_valid(grant, {**state, "client_id": "other"}, 60, phone=PHONE) is False


@pytest.mark.unit
def test_grant_expires():
    state = _web_state()
    _grant(state, method="order_id", order_ids=["63084"], phone=PHONE)
    state["order_auth"]["verified_at"] = (
        datetime.now(timezone.utc) - timedelta(minutes=90)
    ).isoformat()
    assert oa.grant_is_valid(state["order_auth"], state, 60, phone=PHONE) is False


@pytest.mark.unit
def test_grant_accumulates_orders():
    state = _web_state()
    _grant(state, method="pincode", order_ids=["63084", "63102"], phone=PHONE)
    _grant(state, method="order_id", order_ids=["62990"], phone=PHONE)
    assert oa.granted_order_ids(state["order_auth"]) == {"63084", "63102", "62990"}


@pytest.mark.unit
def test_read_only_grant_does_not_cover_mutations():
    state = _web_state()
    _grant(state, method="pincode", order_ids=["63084"], phone=PHONE, scope="read_only")
    grant = state["order_auth"]
    assert oa.grant_covers(grant, "63084", mutating=False) is True
    assert oa.grant_covers(grant, "63084", mutating=True) is False


# ==================== policy resolution ====================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_off_without_config():
    """No config row -> disabled, which is every existing client."""
    policy = await oa.aget_verification_policy(_web_state())
    assert policy["enabled"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_off_on_whatsapp_without_reading_config(monkeypatch):
    """WhatsApp short-circuits before any I/O — the channel authenticates itself."""

    async def _explode(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("config must not be read on a WhatsApp turn")

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _explode)
    policy = await oa.aget_verification_policy(_whatsapp_state())
    assert policy["enabled"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_policy_on_for_web(config_on):
    policy = await oa.aget_verification_policy(_web_state())
    assert policy["enabled"] is True
    assert policy["recent_window"] == 3
    assert policy["max_attempts"] == 3
    assert policy["pincode_grants"] == "read_write"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_malformed_policy_degrades_to_off(monkeypatch):
    async def _bad_config(key, default=None, client_id=None):
        return "{not json"

    monkeypatch.setattr("fashion_bot.config_manager.aget_config", _bad_config)
    assert (await oa.aget_verification_policy(_web_state()))["enabled"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_client_id_is_never_a_default_tenant():
    state = _web_state()
    state.pop("client_id")
    assert (await oa.aget_verification_policy(state))["enabled"] is False


# ==================== listing gate ====================


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_unfiltered_when_policy_off():
    gate = await oa.averify_listing_access(_web_state(), phone=PHONE)
    assert gate["allowed"] is True
    assert gate["filter_order_ids"] is None  # None == no filtering at all


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_refuses_without_an_identifier(config_on):
    gate = await oa.averify_listing_access(_web_state(), phone=PHONE)
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_required"
    assert gate["response"]["orders"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_pincode_matches_two_of_three(config_on, recent_orders):
    recent_orders([
        _order("#63084", PINCODE),
        _order("#63102", "560 103"),
        _order("#62901", "110001"),
    ])
    state = _web_state(messages=_said("my pincode is 560103"))

    gate = await oa.averify_listing_access(
        state, phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert gate["allowed"] is True
    assert gate["filter_order_ids"] == {"63084", "63102"}
    # the gate RETURNS the grant; it must not have written state itself
    assert state.get("order_auth") is None
    assert gate["grant_update"]["method"] == "pincode"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_pincode_no_match_counts_a_failure(config_on, recent_orders):
    recent_orders([_order("#63084", PINCODE)])
    state = _web_state(messages=_said("110001"))

    gate = await oa.averify_listing_access(
        state, phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value="110001",
    )
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_failed"
    assert gate["response"]["attempts_remaining"] == 2
    # the failure rides back in the result; the tool wrote nothing to state
    assert state.get("order_auth") is None
    assert gate["grant_update"]["failed_attempts"] == 1
    assert gate["response"][oa.GRANT_RESULT_KEY] == gate["grant_update"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_locks_after_three_failures(config_on, recent_orders):
    recent_orders([_order("#63084", PINCODE)])
    state = _web_state(messages=_said("110001 400001 700016"))

    for _ in range(3):
        gate = await oa.averify_listing_access(
            state, phone=PHONE,
            verification_identifier_type="pincode", verification_identifier_value="110001",
        )
        oa.apply_grant_update(state, gate["grant_update"])  # what the runtime does
    gate = await oa.averify_listing_access(
        state, phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value="400001",
    )
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_locked"
    assert gate["response"]["needs_escalation"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_refuses_a_pincode_the_customer_never_typed(config_on, monkeypatch):
    """The model lifted the pincode from an order it had already read."""
    called = {"fetched": False}

    async def _fake(state, phone):
        called["fetched"] = True
        return [_order("#63084", PINCODE)]

    monkeypatch.setattr(oa, "_afetch_recent_orders", _fake)

    state = _web_state(messages=_said("where is my order?"))
    gate = await oa.averify_listing_access(
        state, phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_required"
    assert called["fetched"] is False, "must refuse before hitting Shopify"
    assert gate["grant_update"] is None, "a model bug is not a customer attempt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_listing_order_id_path_grants_that_order(config_on, recent_orders):
    recent_orders([_order("#63084", PINCODE), _order("#63102", "110001")])
    state = _web_state(messages=_said("my order is gv63084"))

    gate = await oa.averify_listing_access(
        state, phone=PHONE,
        verification_identifier_type="order_id", verification_identifier_value="gv63084",
    )
    assert gate["allowed"] is True
    assert gate["filter_order_ids"] == {"63084"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_existing_grant_skips_re_verification(config_on, monkeypatch):
    async def _must_not_fetch(*a, **k):  # pragma: no cover
        raise AssertionError("a valid grant must not re-fetch")

    state = _web_state()
    _grant(state, method="pincode", order_ids=["63084"], phone=PHONE)
    monkeypatch.setattr(oa, "_afetch_recent_orders", _must_not_fetch)

    gate = await oa.averify_listing_access(state, phone=PHONE)
    assert gate["allowed"] is True
    assert gate["filter_order_ids"] == {"63084"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_grant_does_not_carry_to_another_phone(config_on, recent_orders):
    recent_orders([])  # no orders for the friend's number
    state = _web_state()
    _grant(state, method="pincode", order_ids=["63084"], phone=PHONE)

    gate = await oa.averify_listing_access(state, phone="9000011111")
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_required"


# ==================== result filtering ====================


@pytest.mark.unit
def test_filter_orders_to_grant():
    orders = [_order("#63084", PINCODE), _order("#63102", PINCODE), _order("#62901", "110001")]
    kept = oa.filter_orders_to_grant(orders, {"63084", "62901"})
    assert [o["name"] for o in kept] == ["#63084", "#62901"]


@pytest.mark.unit
def test_filter_is_a_no_op_without_a_grant():
    orders = [_order("#63084", PINCODE)]
    assert oa.filter_orders_to_grant(orders, None) == orders


# ==================== instruction block ====================


@pytest.mark.unit
def test_first_ask_never_offers_the_pincode_fallback():
    """The customer must be asked for the Order ID alone.

    Regression: the refusal message named the pincode fallback, and the model
    paraphrased it straight to the customer ("...or you can also provide the
    6-digit pincode"), so the weaker proof was offered before the customer had
    even tried to find their Order ID.
    """
    # Neither the first ask nor a failed attempt may name the fallback...
    for payload in (oa.verification_required_response(), oa.verification_failed_response(2)):
        assert "pincode" not in payload["message"].lower(), payload["message"]
    # ...and the first ask must name the proof we actually want.
    assert "order id" in oa.verification_required_response()["message"].lower()


@pytest.mark.unit
def test_instruction_block_keeps_the_fallback_conditional():
    block = oa.build_verification_instruction_block(
        {"enabled": True, "methods": ["order_id", "pincode"]}
    ).lower()
    assert "fallback" in block
    assert "only after the customer has told you" in block
    assert "never offer it in the same message" in block


@pytest.mark.unit
def test_instruction_block_only_when_enabled():
    assert oa.build_verification_instruction_block({"enabled": False}) == ""
    block = oa.build_verification_instruction_block(
        {"enabled": True, "methods": ["order_id", "pincode"]}
    )
    assert "ORDER ID first" in block
    assert "PINCODE" in block
    assert "SUPERSEDES" in block


@pytest.mark.unit
def test_instruction_block_omits_pincode_when_not_a_method():
    block = oa.build_verification_instruction_block({"enabled": True, "methods": ["order_id"]})
    assert "PINCODE" not in block


# ==================== functional: the real get_recent_orders tool ====================
# These exercise the tool body (gate call + result filter), not just the module,
# because that is where a regression would actually reach a customer.

_ORDERS = [
    {"name": "#63084", "shipping_address": {"zip": "560103"}, "created_at": "2026-08-08T10:00:00Z", "line_items": []},
    {"name": "#63102", "shipping_address": {"zip": "560 103"}, "created_at": "2026-08-05T10:00:00Z", "line_items": []},
    {"name": "#62901", "shipping_address": {"zip": "110001"}, "created_at": "2026-08-01T10:00:00Z", "line_items": []},
]


@pytest.fixture
def recent_orders_tool(monkeypatch):
    """The real tool, with Shopify replaced by a fixed 3-order response.

    Patches the ADAPTER (the phone->orders lookup) rather than the orchestrator,
    so the orchestrator's real ``cached_orders`` short-circuit is exercised and
    ``fetch_calls`` counts actual Shopify round-trips.
    """
    from fashion_bot.core.factory import ServiceFactory
    from fashion_bot.tool_factory import _create_get_recent_orders_tool

    fetch_calls = []

    class _FakeOrderService:
        async def aget_orders_by_customer_phone(self, phone, limit=50, state=None):
            fetch_calls.append(limit)
            return [dict(o) for o in _ORDERS]

    async def _fake_service(*args, **kwargs):
        return _FakeOrderService()

    monkeypatch.setattr(ServiceFactory, "aget_order_service", staticmethod(_fake_service))

    def _factory(state):
        return _create_get_recent_orders_tool(state)

    _factory.fetch_calls = fetch_calls
    return _factory


async def _call(tool_factory, state, **kwargs):
    """Invoke the tool, then apply its grant exactly as the runtime would.

    The tool itself must never touch state — OrderAuthMiddleware is what applies
    what the tool returns. `test_middleware_*` below covers that hand-off with
    the real middleware; this helper keeps the tool tests focused on the tool.
    """
    result = await tool_factory(state).ainvoke(kwargs)
    if isinstance(result, dict):
        oa.apply_grant_update(state, result.get(oa.GRANT_RESULT_KEY))
    return result


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_returns_everything_when_policy_off(config_off, recent_orders_tool):
    state = _web_state()
    result = await _call(recent_orders_tool, state, phone_number=PHONE)
    assert len(result["orders"]) == 3
    assert state.get("order_auth") is None, "no grant is written for an opted-out client"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_is_untouched_on_whatsapp(config_on, recent_orders_tool):
    state = _whatsapp_state()
    result = await _call(recent_orders_tool, state, phone_number=PHONE)
    assert len(result["orders"]) == 3
    assert state.get("order_auth") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_refuses_to_list_from_a_phone_alone(config_on, recent_orders_tool):
    state = _web_state(messages=_said("where is my order?"))
    result = await _call(recent_orders_tool, state, phone_number=PHONE)
    assert result["error"] == "verification_required"
    assert result["orders"] == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_lists_only_the_pincode_matched_orders(config_on, recent_orders_tool):
    state = _web_state(messages=_said("my pincode is 560103"))
    result = await _call(
        recent_orders_tool, state, phone_number=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert [o["order_id"] for o in result["orders"]] == ["63084", "63102"]
    # every count the orchestrator emitted must match the filtered list, or the
    # reply claims more orders than it shows
    counts = {k: v for k, v in result.items()
              if k in {"total_orders", "total_actionable_orders", "total_orders_found",
                       "count", "showing_top"}}
    assert counts, "expected the orchestrator to report a count"
    assert set(counts.values()) == {2}, counts


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_reuses_the_grant_on_a_later_message(config_on, recent_orders_tool):
    verified = _web_state(messages=_said("my pincode is 560103"))
    await _call(
        recent_orders_tool, verified, phone_number=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    # Next turn: no verification arguments at all, grant carried in state.
    later = _web_state(messages=_said("and the second one?"), order_auth=verified["order_auth"])
    result = await _call(recent_orders_tool, later, phone_number=PHONE)
    assert [o["order_id"] for o in result["orders"]] == ["63084", "63102"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_locks_out_after_three_wrong_pincodes(config_on, recent_orders_tool):
    state = _web_state(messages=_said("999999", "888888", "777777", "560103"))

    for expected_remaining in (2, 1, 0):
        result = await _call(
            recent_orders_tool, state, phone_number=PHONE,
            verification_identifier_type="pincode", verification_identifier_value="999999",
        )
        assert result["error"] == "verification_failed"
        assert result["attempts_remaining"] == expected_remaining

    # Even the CORRECT pincode is refused once the conversation is locked.
    result = await _call(
        recent_orders_tool, state, phone_number=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert result["error"] == "verification_locked"
    assert result["needs_escalation"] is True


# ==================== the runtime hand-off (OrderAuthMiddleware) ====================
# Tools stay pure (AGENTS.md §2): they RETURN the grant, the middleware applies it.


def _tool_message(payload, artifact=None):
    from langchain_core.messages import ToolMessage

    return ToolMessage(
        content=json.dumps(payload, default=str),
        tool_call_id="tc_1",
        name="get_recent_orders",
        artifact=artifact,
    )


async def _run_middleware(state, tool_message):
    from fashion_bot.utils.agent_middleware import OrderAuthMiddleware

    async def _handler(_request):
        return tool_message

    return await OrderAuthMiddleware(state).awrap_tool_call(object(), _handler)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_applies_the_grant_a_tool_returned():
    state = _web_state()
    grant = oa.build_grant(state, method="pincode", order_ids=["63084"], phone=PHONE)
    result = await _run_middleware(state, _tool_message({"orders": [], oa.GRANT_RESULT_KEY: grant}))

    assert state["order_auth"]["order_ids"] == ["63084"]
    # ...and the marker is stripped from what the model reads.
    assert oa.GRANT_RESULT_KEY not in json.loads(result.content)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_is_a_no_op_without_a_grant():
    state = _web_state()
    payload = {"orders": [{"order_id": "63084"}]}
    result = await _run_middleware(state, _tool_message(payload))

    assert state.get("order_auth") is None
    assert json.loads(result.content) == payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_applies_the_grant_even_if_content_is_unparseable():
    """Stripping is best-effort; recording the grant is not."""
    from langchain_core.messages import ToolMessage

    state = _web_state()
    grant = oa.build_grant(state, method="pincode", order_ids=["63084"], phone=PHONE)
    message = ToolMessage(
        content="not json at all",
        tool_call_id="tc_2",
        name="get_recent_orders",
        artifact={"orders": [], oa.GRANT_RESULT_KEY: grant},
    )
    await _run_middleware(state, message)
    assert state["order_auth"]["order_ids"] == ["63084"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_middleware_makes_the_grant_visible_to_the_next_tool_in_the_same_turn(
    config_on, recent_orders_tool
):
    """The reason this is a per-tool-call middleware and not an end-of-turn harvest."""
    state = _web_state(messages=_said("my pincode is 560103"))

    first = await recent_orders_tool(state).ainvoke({
        "phone_number": PHONE,
        "verification_identifier_type": "pincode",
        "verification_identifier_value": PINCODE,
    })
    assert state.get("order_auth") is None, "the tool must not have written state"

    await _run_middleware(state, _tool_message(first))

    # Second tool call in the SAME turn, no verification arguments: it sees the
    # grant because the tools all close over this one state object.
    second = await recent_orders_tool(state).ainvoke({"phone_number": PHONE})
    assert [o["order_id"] for o in second["orders"]] == ["63084", "63102"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_verification_turn_issues_one_phone_lookup(config_on, recent_orders_tool):
    """The pincode check must not double-fetch the customer's orders.

    The phone->orders lookup is ~2 sequential Shopify calls drawing on a shared
    per-shop rate-limit budget, so the gate hands its fetch to the tool rather
    than each paying for one.
    """
    state = _web_state(messages=_said("my pincode is 560103"))
    result = await _call(
        recent_orders_tool, state, phone_number=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert [o["order_id"] for o in result["orders"]] == ["63084", "63102"]
    assert len(recent_orders_tool.fetch_calls) == 1, (
        f"expected one phone->orders lookup, got {recent_orders_tool.fetch_calls}"
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_unverified_turn_still_issues_one_lookup(config_off, recent_orders_tool):
    """The opted-out path must not have gained a fetch either."""
    await _call(recent_orders_tool, _web_state(), phone_number=PHONE)
    assert len(recent_orders_tool.fetch_calls) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_delivered_orders_verification_turn_issues_one_lookup(config_on, monkeypatch):
    """The delivered-orders listing tool reuses the gate's fetch too."""
    from fashion_bot.core.factory import ServiceFactory
    from fashion_bot.tool_factory import return_exchange_tools_factory

    fetch_calls = []
    delivered = dict(_ORDERS[0])
    delivered.update({"fulfillment_status": "fulfilled", "financial_status": "paid"})

    class _FakeOrderService:
        async def aget_orders_by_customer_phone(self, phone, limit=50, state=None):
            fetch_calls.append(limit)
            return [dict(delivered)]

    async def _fake_service(*args, **kwargs):
        return _FakeOrderService()

    monkeypatch.setattr(ServiceFactory, "aget_order_service", staticmethod(_fake_service))

    state = _web_state(messages=_said("my pincode is 560103"))
    tools = {t.name: t for t in return_exchange_tools_factory(state, [])}
    tool = tools["get_customers_delivered_orders_by_phone"]

    await tool.ainvoke({
        "phone_number": PHONE,
        "verification_identifier_type": "pincode",
        "verification_identifier_value": PINCODE,
    })
    assert len(fetch_calls) == 1, f"expected one phone->orders lookup, got {fetch_calls}"


# ==================== the order-scoped gate ====================
# The return/exchange partner tools reach order data through this gate, not the
# listing gate above. Everything here was previously untested.


@pytest.fixture
def phone_check(monkeypatch):
    """Patch the shared phone-vs-order check the order-scoped gate borrows."""

    def _apply(**verdict):
        async def _fake(order_id, current_state, **kwargs):
            return dict(verdict)

        monkeypatch.setattr("fashion_bot.tool_factory._avalidate_phone_for_order_access", _fake)

    return _apply


async def _scoped(state, **kwargs):
    """Run the order-scoped gate and apply any grant the way the runtime would."""
    gate = await oa.averify_order_scoped_access(state, **kwargs)
    oa.apply_grant_update(state, gate.get("grant_update"))
    return gate


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_is_untouched_when_policy_off(config_off):
    """The regression guard for every existing client."""
    gate = await oa.averify_order_scoped_access(_web_state(), order_id="", phone="")
    assert gate["allowed"] is True
    assert gate["grant_update"] is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_refuses_without_an_order_id(config_on):
    gate = await oa.averify_order_scoped_access(_web_state(), order_id="", phone=PHONE)
    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_required"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_records_a_grant_on_a_phone_match(config_on, phone_check):
    """Rule 6 of the instruction block ("don't re-ask") needs a grant to be real."""
    phone_check(should_block=False, valid=True)
    state = _web_state()

    gate = await _scoped(state, order_id="#GV63084", phone=PHONE)

    assert gate["allowed"] is True
    assert state["order_auth"]["method"] == "order_id"
    assert state["order_auth"]["order_ids"] == ["gv63084"]

    # ...and the follow-up message is not challenged again.
    phone_check(should_block=True, failed_verification=True, message="nope")
    assert (await oa.averify_order_scoped_access(state, order_id="gv63084", phone=PHONE))["allowed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_counts_a_wrong_order_as_an_attempt(config_on, phone_check):
    """Order numbers are largely sequential; guessing them must cost something."""
    phone_check(should_block=True, failed_verification=True, message="Order #1 not found.")
    state = _web_state()

    gate = await _scoped(state, order_id="#1", phone=PHONE)

    assert gate["allowed"] is False
    assert state["order_auth"]["failed_attempts"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_does_not_count_a_missing_phone(config_on, phone_check):
    """"Tell me your phone number" is a prompt for input, not a wrong answer."""
    phone_check(should_block=True, needs_phone=True, message="Customer phone number not available.")
    state = _web_state()

    gate = await _scoped(state, order_id="#GV63084", phone="")

    assert gate["allowed"] is False
    assert gate["response"]["needs_phone"] is True
    assert state.get("order_auth") is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_locks_out_after_the_attempt_cap(config_on, phone_check):
    phone_check(should_block=True, failed_verification=True, message="not found")
    state = _web_state()

    for _ in range(3):
        gate = await _scoped(state, order_id="#1", phone=PHONE)
    assert gate["response"]["error"] == "verification_locked"
    assert gate["response"]["needs_escalation"] is True

    # Locked for the rest of the conversation, even with a correct order.
    phone_check(should_block=False, valid=True)
    blocked = await oa.averify_order_scoped_access(state, order_id="#GV63084", phone=PHONE)
    assert blocked["response"]["error"] == "verification_locked"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_pincode_must_match_that_order(config_on, recent_orders, phone_check):
    """A pincode used to pass unread here — only the phone match was consulted."""
    phone_check(should_block=False, valid=True)
    recent_orders([_order("#63084", "560103"), _order("#62901", "110001")])
    state = _web_state(messages=_said("my pincode is 560103"))

    gate = await _scoped(
        state, order_id="#62901", phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )

    assert gate["allowed"] is False, "560103 is not the pincode on order #62901"
    # Not an attempt: the customer answered correctly, the agent named the wrong
    # order, and a genuine pincode still releases only the orders it matched.
    assert oa.failure_count(state) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_wrong_pincode_is_counted(config_on, recent_orders):
    recent_orders([_order("#63084", "560103")])
    state = _web_state(messages=_said("my pincode is 110099"))

    gate = await _scoped(
        state, order_id="#63084", phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value="110099",
    )

    assert gate["allowed"] is False
    assert oa.failure_count(state) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_read_only_pincode_cannot_drive_a_mutating_return_tool(config_value, recent_orders):
    """pincode_grants=read_only must hold on the return/exchange path too."""
    config_value({"enabled": True, "pincode_grants": "read_only"})
    recent_orders([_order("#63084", "560103")])
    state = _web_state(messages=_said("my pincode is 560103"))

    read = await oa.averify_order_scoped_access(
        state, order_id="#63084", phone=PHONE, mutating=False,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert read["allowed"] is True

    write = await oa.averify_order_scoped_access(
        state, order_id="#63084", phone=PHONE, mutating=True,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )
    assert write["allowed"] is False
    assert "Order ID" in write["response"]["message"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_pincode_matching_that_order_is_allowed(config_on, recent_orders):
    recent_orders([_order("#63084", "560103"), _order("#62901", "110001")])
    state = _web_state(messages=_said("my pincode is 560103"))

    gate = await _scoped(
        state, order_id="#63084", phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )

    assert gate["allowed"] is True
    assert state["order_auth"]["method"] == "pincode"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_pincode_the_customer_never_typed_is_refused(config_on, recent_orders):
    """The model may have lifted it from an order it just read."""
    recent_orders([_order("#63084", "560103")])
    state = _web_state(messages=_said("where is my order"))

    gate = await _scoped(
        state, order_id="#63084", phone=PHONE,
        verification_identifier_type="pincode", verification_identifier_value=PINCODE,
    )

    assert gate["allowed"] is False
    assert gate["response"]["error"] == "verification_required"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_accepts_an_email_that_matches_the_order(config_on, phone_check, monkeypatch):
    """Email-led web chat must not be locked out the day a client opts in."""
    phone_check(should_block=True, needs_phone=True, message="Customer phone number not available.")

    async def _identity(**kwargs):
        return {"verified": True, "matched_on": "email"}

    monkeypatch.setattr("fashion_bot.return_partners.identity.averify_order_identity", _identity)

    gate = await oa.averify_order_scoped_access(
        _web_state(), order_id="#GV63084", phone="", customer_email="a@b.com",
    )
    assert gate["allowed"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_order_scoped_rejects_an_email_that_does_not_match(config_on, phone_check, monkeypatch):
    phone_check(should_block=True, needs_phone=True, message="Customer phone number not available.")

    async def _identity(**kwargs):
        return {"verified": False, "matched_on": "not_matched"}

    monkeypatch.setattr("fashion_bot.return_partners.identity.averify_order_identity", _identity)

    gate = await oa.averify_order_scoped_access(
        _web_state(), order_id="#GV63084", phone="", customer_email="stranger@b.com",
    )
    assert gate["allowed"] is False


# ==================== grant scope + attempt bookkeeping ====================


@pytest.mark.unit
def test_an_order_id_proof_does_not_upgrade_a_read_only_pincode_grant():
    state = _web_state()
    _grant(state, method="pincode", order_ids=["63102"], phone=PHONE, scope="read_only")
    _grant(state, method="order_id", order_ids=["63084"], phone=PHONE, scope="read_write")

    grant = state["order_auth"]
    assert grant["scope"] == "read_write"
    assert grant["order_ids"] == ["63084"], "the pincode-granted order must not become mutable"


@pytest.mark.unit
def test_failed_attempts_survive_a_change_of_method():
    """Otherwise switching order_id -> pincode would reset the rate limit."""
    state = _web_state()
    oa.apply_grant_update(state, oa.bump_failure(state, phone=PHONE))
    oa.apply_grant_update(state, oa.bump_failure(state, phone=PHONE))

    _grant(state, method="order_id", order_ids=["63084"], phone=PHONE, scope="read_write")
    assert state["order_auth"]["failed_attempts"] == 2


@pytest.mark.unit
def test_value_from_customer_ignores_a_window_inside_their_phone_number():
    """"9876543210" contains "987654" — that is not a pincode they gave us."""
    state = _web_state(messages=_said("my number is 9876543210"))
    assert oa.value_from_customer("987654", state) is False
    # A pincode typed with a space is still theirs.
    assert oa.value_from_customer("560103", _web_state(messages=_said("560 103"))) is True


# ==================== the real return/exchange tools ====================


def _return_tools(state):
    from fashion_bot.return_partners.tools import create_return_partner_chat_tools

    return {t.name: t for t in create_return_partner_chat_tools(state)}


@pytest.fixture
def return_status(monkeypatch):
    """Patch the partner call the return-status tool makes."""
    calls = []

    async def _fake(**kwargs):
        calls.append(kwargs)
        return {"success": True, "request": {"status": "approved"}}

    monkeypatch.setattr(
        "fashion_bot.core.orchestrator.ReturnExchangeOrchestrator.aget_return_status",
        staticmethod(_fake),
    )
    return calls


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_tool_is_untouched_when_policy_off(config_off, return_status):
    state = _web_state()
    result = await _return_tools(state)["get_return_status_by_order_number"].ainvoke(
        {"order_number": "#GV63084", "customer_phone": PHONE}
    )
    assert result["success"] is True
    assert len(return_status) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_tool_refuses_a_phone_that_does_not_match(config_on, phone_check, return_status):
    phone_check(should_block=True, failed_verification=True, message="does not match")
    state = _web_state()

    result = await _return_tools(state)["get_return_status_by_order_number"].ainvoke(
        {"order_number": "#GV63084", "customer_phone": PHONE}
    )

    assert result["success"] is False
    assert return_status == [], "the partner must not be called for an unverified order"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_return_tool_hands_its_grant_back_to_the_runtime(config_on, phone_check, return_status):
    phone_check(should_block=False, valid=True)
    state = _web_state()

    result = await _return_tools(state)["get_return_status_by_order_number"].ainvoke(
        {"order_number": "#GV63084", "customer_phone": PHONE}
    )

    assert result["success"] is True
    assert state.get("order_auth") is None, "the tool must not write state itself"
    oa.apply_grant_update(state, result[oa.GRANT_RESULT_KEY])
    assert state["order_auth"]["order_ids"] == ["gv63084"]


@pytest.mark.unit
def test_gated_tools_are_discoverable_from_their_schemas():
    """What generic_skill_node keys the instruction block off."""
    tools = _return_tools(_web_state())
    assert oa.agent_has_gated_order_tools(tools.values()) is True
    assert oa.agent_has_gated_order_tools([tools["get_return_exchange_request_instructions"]]) is False
    assert oa.agent_has_gated_order_tools([]) is False


@pytest.mark.unit
def test_gating_preserves_every_tool_schema_and_docstring():
    for name, tool in _return_tools(_web_state()).items():
        assert tool.description.strip(), f"{name} lost its docstring"
        if name in ("get_return_request_by_id", "get_return_exchange_request_instructions"):
            continue
        assert "order_number" in tool.args, f"{name} lost its order_number parameter"
        assert "verification_identifier_value" in tool.args, f"{name} is not gated"
