"""
Tests for the resolution-first escalation actionability gate
(fashion_bot/agent_config.py).

Two escalation decisions are enforced in code:

* the actionability gate — an escalation the escalating agent itself flagged
  unfulfillable (``human_can_resolve=False``) is soft-blocked so the customer is
  offered the real alternatives instead of a hand-off no human could action; and
* ``frustration_should_escalate`` — a first-turn frustration that carries a
  resolvable intent routes to the resolving agent instead of escalating, with a
  one-turn streak loop-breaker.

Everything else — clarify first, de-escalate, attempt before escalating — is
owned by the escalation_handler prompt and covered by the full-agent eval layer.
"""

import pytest

import fashion_bot.agent_config as ac
import fashion_bot.config_manager as config_manager
from fashion_bot.agent_config import frustration_should_escalate


# ---------------------------------------------------------------------------
# Mandatory categories
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("category", sorted(ac.MANDATORY_ESCALATION_CATEGORIES))
def test_mandatory_categories_are_recognised(category):
    assert ac.is_mandatory_escalation_category(category) is True


def test_off_enum_system_categories_are_not_collapsed_to_general():
    # "Offline Store Suggestion" is a valid escalation category but NOT in the
    # LLM tool enum. It must resolve to itself, not be normalized to "General".
    assert ac.is_mandatory_escalation_category("Offline Store Suggestion") is True
    assert ac.is_mandatory_escalation_category("Walk-in Appointment") is True


@pytest.mark.parametrize(
    "category", ["General", "Frustration", "Product Complaint", "Order Update", "", None]
)
def test_non_mandatory_categories(category):
    assert ac.is_mandatory_escalation_category(category) is False


def test_mandatory_recognised_through_normalization():
    # case / spacing variants still resolve to the canonical mandatory category
    assert ac.is_mandatory_escalation_category("cancellation requests") is True


# ---------------------------------------------------------------------------
# classify_escalation_actionability
# ---------------------------------------------------------------------------
def test_normal_escalation_is_actionable():
    assert ac.classify_escalation_actionability("General") == "actionable"
    assert ac.classify_escalation_actionability("Frustration", human_can_resolve=True) == "actionable"


def test_llm_flag_marks_unfulfillable():
    assert ac.classify_escalation_actionability("Order Update", human_can_resolve=False) == "unfulfillable"


def test_mandatory_wins_over_the_llm_flag():
    # A mandatory hand-off is never reclassified, even if the agent wrongly sets
    # human_can_resolve=False — a cancellation must always reach a human.
    assert ac.classify_escalation_actionability(
        "Cancellation Requests", human_can_resolve=False
    ) == "mandatory"
    assert ac.classify_escalation_actionability(
        "Callback Request", human_can_resolve=False
    ) == "mandatory"


def test_missing_order_is_actionable_not_unfulfillable():
    # The false-positive guard: "order does not exist" needs a human, and the
    # agent signals that with human_can_resolve=True. No keyword matching.
    assert ac.classify_escalation_actionability("General", human_can_resolve=True) == "actionable"


# ---------------------------------------------------------------------------
# frustration_should_escalate (first-turn routing rule)
# ---------------------------------------------------------------------------
def _intents(*names):
    return [{"intent": n} for n in names]


@pytest.mark.parametrize(
    "intent",
    ["order_status", "return_exchange_policy", "cancel_or_update_order",
     "product_details", "after_delivery_return_exchange", "delivery_timeline"],
)
def test_frustration_with_a_resolvable_intent_does_not_escalate(intent):
    # "where's my order, this is terrible" → let the resolving agent help.
    assert frustration_should_escalate(_intents(intent)) is False


@pytest.mark.parametrize(
    "intents",
    [(), ("escalation",), ("continuity_agent",), ("",), ("escalation", "continuity_agent")],
)
def test_standalone_frustration_escalates(intents):
    # Pure anger / explicit hand-off / small talk — nothing to resolve.
    assert frustration_should_escalate(_intents(*intents)) is True


def test_none_intents_escalates():
    assert frustration_should_escalate(None) is True


def test_mixed_intents_take_the_resolvable_one():
    assert frustration_should_escalate(_intents("escalation", "order_status")) is False


def test_streak_breaks_the_resolve_loop():
    # Resolution-first gets exactly ONE chance: still frustrated on the next
    # consecutive turn → escalate even though a resolvable intent is present.
    assert frustration_should_escalate(_intents("order_status"), 0) is False
    assert frustration_should_escalate(_intents("order_status"), 1) is True
    assert frustration_should_escalate(_intents("order_status"), 3) is True


def test_malformed_intent_entries_are_ignored():
    # Non-dict entries must not be mistaken for a resolvable intent.
    assert frustration_should_escalate(["order_status", None, 42]) is True


# ---------------------------------------------------------------------------
# escalation_resolution_tools_enabled (config parsing, default ON)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cfg",
    [None, {}, "", "{not valid json", [], {"other_key": 1}, {"resolution_first_tools": True},
     '{"resolution_first_tools": true}'],
)
def test_resolution_tools_default_on(cfg):
    assert ac.escalation_resolution_tools_enabled(cfg) is True


@pytest.mark.parametrize(
    "cfg",
    [{"resolution_first_tools": False}, '{"resolution_first_tools": false}',
     {"resolution_first_tools": "no"}, {"resolution_first_tools": 0}],
)
def test_resolution_tools_explicit_opt_out(cfg):
    assert ac.escalation_resolution_tools_enabled(cfg) is False


# ---------------------------------------------------------------------------
# aevaluate_escalation_gate
# ---------------------------------------------------------------------------
def _patch_config(monkeypatch, value):
    async def _fake(config_key, default=None, client_id=None):
        return value if config_key == "escalation_policy" else default
    monkeypatch.setattr(config_manager, "aget_config", _fake)


async def test_gate_passes_a_normal_escalation(monkeypatch):
    _patch_config(monkeypatch, None)
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Frustration", human_can_resolve=True
    )
    assert gate["soft_block"] is False
    assert gate["actionability"] == "actionable"
    assert gate["message"] is None


async def test_gate_soft_blocks_unfulfillable_by_default(monkeypatch):
    _patch_config(monkeypatch, None)  # no config at all → gate on
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Order Update", human_can_resolve=False
    )
    assert gate["soft_block"] is True
    assert gate["actionability"] == "unfulfillable"
    assert "alternative" in gate["message"] or "available" in gate["message"]


async def test_gate_never_blocks_a_mandatory_handoff(monkeypatch):
    _patch_config(monkeypatch, None)
    for category in ("Cancellation Requests", "Callback Request", "Offline Store Suggestion"):
        gate = await ac.aevaluate_escalation_gate(
            client_id="c1", category=category, human_can_resolve=False
        )
        assert gate["soft_block"] is False, category
        assert gate["actionability"] == "mandatory", category


async def test_client_can_opt_out_of_the_gate(monkeypatch):
    _patch_config(monkeypatch, {"gate_enabled": False})
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Order Update", human_can_resolve=False
    )
    assert gate["gate_enabled"] is False
    assert gate["soft_block"] is False


async def test_gate_reads_json_string_config(monkeypatch):
    _patch_config(monkeypatch, '{"gate_enabled": false}')
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Order Update", human_can_resolve=False
    )
    assert gate["soft_block"] is False


async def test_malformed_config_keeps_the_gate_on(monkeypatch):
    # A bad config must never silently disable the guard.
    _patch_config(monkeypatch, "{not valid json")
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Order Update", human_can_resolve=False
    )
    assert gate["gate_enabled"] is True
    assert gate["soft_block"] is True


async def test_config_read_failure_keeps_the_gate_on(monkeypatch):
    async def _boom(config_key, default=None, client_id=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(config_manager, "aget_config", _boom)
    gate = await ac.aevaluate_escalation_gate(
        client_id="c1", category="Order Update", human_can_resolve=False
    )
    assert gate["soft_block"] is True
