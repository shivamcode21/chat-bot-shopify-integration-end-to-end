"""
Tests for the gated resolution-first escalation toolset
(fashion_bot/core/tool_registry.py::_get_escalation_tools).

Asserts that the escalation node gains the READ-ONLY resolution tools by
default, keeps only the base toolset when a client opts out via
``escalation_policy.resolution_first_tools = false`` — with ``escalate_to_agent``
staying last and no mutating tools ever added on this node — and that the
legacy sync tools path degrades loudly (never silently) for this async factory.
"""

import fashion_bot.config_manager as config_manager
import fashion_bot.core.tool_registry as tr

BASE = {"get_order_details", "get_recent_orders", "escalate_to_agent"}

# Tools that must NEVER appear on the escalation node (mutating / order-changing).
FORBIDDEN_MUTATORS = {
    "cancel_order_tool", "update_order_address", "update_order_size_tool",
    "update_order_phone_number_tool", "update_order_email_tool",
    "update_order_name_tool", "change_order_product_tool", "annotate_order",
    "add_to_cart", "remove_from_cart", "update_cart_quantity",
}


def _names(tools):
    return [getattr(t, "name", str(t)) for t in tools]


def _patch_config(monkeypatch, configs):
    async def _fake(config_key, default=None, client_id=None):
        return configs.get(config_key, default)
    monkeypatch.setattr(config_manager, "aget_config", _fake)


async def test_resolution_tools_included_by_default(monkeypatch):
    # On by default (no config): the node gets the read-only resolution toolset.
    _patch_config(monkeypatch, {})
    names = _names(await tr._get_escalation_tools({"client_id": "c1"}, [], "c1"))
    assert BASE <= set(names)
    assert {"get_policy_information", "search_products", "get_delivery_partner_information"} <= set(names)
    assert names[-1] == "escalate_to_agent"  # last-resort actuator stays last


async def test_opt_out_keeps_base(monkeypatch):
    # A client can explicitly opt out → only the base toolset.
    _patch_config(monkeypatch, {"escalation_policy": {"resolution_first_tools": False}})
    tools = await tr._get_escalation_tools({"client_id": "c1"}, [], "c1")
    assert set(_names(tools)) == BASE


async def test_explicit_on_adds_readonly_resolution_tools(monkeypatch):
    _patch_config(monkeypatch, {"escalation_policy": {"resolution_first_tools": True}})
    tools = await tr._get_escalation_tools({"client_id": "c1"}, [], "c1")
    names = _names(tools)
    assert BASE <= set(names)
    assert {"get_policy_information", "search_products", "get_delivery_partner_information"} <= set(names)
    assert names[-1] == "escalate_to_agent"
    assert len(names) > len(BASE)


async def test_resolution_tools_add_no_mutating_tools(monkeypatch):
    _patch_config(monkeypatch, {})  # default on
    tools = await tr._get_escalation_tools({"client_id": "c1"}, [], "c1")
    assert FORBIDDEN_MUTATORS.isdisjoint(set(_names(tools)))


async def test_malformed_config_defaults_to_resolution_tools(monkeypatch):
    # Malformed config degrades to the default (on) — never silently strips tools.
    _patch_config(monkeypatch, {"escalation_policy": "{not valid json"})
    names = _names(await tr._get_escalation_tools({"client_id": "c1"}, [], "c1"))
    assert {"get_policy_information", "search_products"} <= set(names)


def test_sync_path_degrades_loudly_for_async_factory(monkeypatch, caplog):
    # _get_escalation_tools is async; the legacy SYNC get_tools_for_agent path
    # must not silently swallow the coroutine (RuntimeWarning + contact-only
    # tools with no escalate_to_agent). It must detect the coroutine, log an
    # error pointing at aget_tools_for_agent, and fall back cleanly.
    import logging

    with caplog.at_level(logging.ERROR):
        tools = tr.get_tools_for_agent("escalation", {"client_id": "c1"}, [], "c1")
    assert isinstance(tools, list)  # clean fallback, no exception
    assert "aget_tools_for_agent" in caplog.text  # loud, actionable error
