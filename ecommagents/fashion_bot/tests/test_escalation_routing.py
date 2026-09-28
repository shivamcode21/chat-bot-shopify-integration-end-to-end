"""
Unit tests for multi-number, agent-routed escalation notifications.

Covers:
  * ``aget_escalation_contacts`` / ``aget_escalation_recipients`` resolution
    order: routes[category] -> routes[agent] -> routes[CATEGORY_TO_AGENT] ->
    routes[PARENT_INTENT_TO_AGENT] -> default -> legacy AGENT_PHONE_NUMBER.
  * Per-channel independent resolution (a phone-only category route still gets
    its email from the agent/default node).
  * Backward compatibility: legacy ``{"AGENT_PHONE_NUMBER": ...}`` and bare-list
    node shapes, plus the legacy email-as-list shape.
  * De-duplication of phone numbers (``+91`` / ``91`` variants collapse).
  * Graceful degradation on missing / malformed config.
  * Map consistency: every ESCALATION_TOOL_CATEGORIES value (except "General")
    is keyed in both CATEGORY_TO_AGENT and CATEGORY_TO_ESCALATION_GROUP.
  * ``build_escalation_metadata`` always emits the six canonical fields and the
    caller's extras never clobber them.
  * ``asend_escalation_notification`` fans out to every resolved number with
    per-recipient isolation (one failure does not suppress the others) and adds
    the immediate-attention banner.
"""

import sys
import types

import fashion_bot.agent_config as ac
from fashion_bot import config_manager


# ── helpers ────────────────────────────────────────────────────────────────


def _patch_config(monkeypatch, configs):
    """Patch config_manager.aget_config to serve from an in-memory dict.

    ``configs`` maps config_key -> value (dict or JSON string). The resolvers
    import aget_config lazily from config_manager, so patching the attribute on
    the module is sufficient.
    """

    async def _fake_aget_config(config_key, default=None, client_id=None):
        return configs.get(config_key, default)

    monkeypatch.setattr(config_manager, "aget_config", _fake_aget_config)


CONTACTS_CONFIG = {
    "escalation_contact": {
        "AGENT_PHONE_NUMBER": "9111111111",
        "ESCALATION_ROUTING": {
            "default": {
                "contacts": {
                    "phone": ["9111111111", "9999999999"],
                    "email": {"to": ["support@example.com"], "cc": ["esc@example.com"]},
                }
            },
            "routes": {
                "return_exchange": {
                    "contacts": {
                        "phone": ["9111111112"],
                        "email": {"to": ["returns@example.com"]},
                    }
                },
                "order_status": {
                    "contacts": {"phone": ["9111111113"]}
                },
                "Misrouted Order": {
                    "contacts": {
                        "phone": ["9111111114"],
                        "email": {"to": ["logistics@example.com"], "cc": ["ops@example.com"]},
                    }
                },
            },
        },
    }
}


# ── resolution order ───────────────────────────────────────────────────────


async def test_agent_route_hit(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    phones = await ac.aget_escalation_recipients("c1", agent="return_exchange")
    assert phones == ["9111111112"]


async def test_category_route_overrides_agent(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    # category "Misrouted Order" is most specific; even with agent=order_status
    # the category route wins.
    phones = await ac.aget_escalation_recipients(
        "c1", agent="order_status", category="Misrouted Order"
    )
    assert phones == ["9111111114"]


async def test_category_to_agent_map_hit(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    # "Restocking Query" maps to product_details (no route) -> falls to default.
    # "Cancellation Requests" maps to cancel_or_update_order (no route) -> default.
    # Use a category whose mapped agent HAS a route: "Order Update" -> cancel_or_update_order
    # (also no route here) so assert the CATEGORY_TO_AGENT path via order_status:
    # category with only a category-map target present is order_status routes.
    # "Order Status Query" -> order_status route.
    phones = await ac.aget_escalation_recipients("c1", category="Order Status Query")
    assert phones == ["9111111113"]


async def test_parent_intent_fallback(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    # No agent / category match; parent_intent "Post-Purchase Support" -> order_status route.
    phones = await ac.aget_escalation_recipients(
        "c1", parent_intent="Post-Purchase Support"
    )
    assert phones == ["9111111113"]


async def test_default_fallback(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    phones = await ac.aget_escalation_recipients("c1", agent="unknown_agent")
    assert phones == ["9111111111", "9999999999"]


async def test_discount_agent_bulk_order_routing(monkeypatch):
    # design §5.4a: the discount agent can now raise an escalation for a bulk /
    # B2B request; it routes to routes["discount"] and groups as pre_sales.
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "AGENT_PHONE_NUMBER": "9111111111",
                "ESCALATION_ROUTING": {
                    "default": {"contacts": {"phone": ["9111111111"]}},
                    "routes": {"discount": {"contacts": {"phone": ["9111111116"], "email": {"to": ["wholesale@example.com"]}}}},
                },
            }
        },
    )
    contacts = await ac.aget_escalation_contacts(
        "c1", agent="discount", category="Bulk Order Discount"
    )
    assert contacts["phone"] == ["9111111116"]
    assert contacts["email"]["to"] == ["wholesale@example.com"]


def test_bulk_categories_map_to_discount_and_pre_sales():
    for cat in ("Bulk Order Discount", "Bulk Order", "Wholesale Inquiry", "B2B Order"):
        assert ac.CATEGORY_TO_AGENT[cat] == "discount"
        assert ac.CATEGORY_TO_ESCALATION_GROUP[cat] == "pre_sales"


async def test_bulk_order_group_resolves_pre_sales(monkeypatch):
    _patch_config(monkeypatch, {})
    assert await ac.aget_escalation_group("c1", "Bulk Order Discount") == "pre_sales"


# ── per-channel independent resolution ─────────────────────────────────────


async def test_email_falls_through_to_default_when_route_phone_only(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    # order_status route lists only phone; email must come from default node.
    contacts = await ac.aget_escalation_contacts("c1", agent="order_status")
    assert contacts["phone"] == ["9111111113"]
    assert contacts["email"]["to"] == ["support@example.com"]
    assert contacts["email"]["cc"] == ["esc@example.com"]


async def test_category_email_with_cc(monkeypatch):
    _patch_config(monkeypatch, CONTACTS_CONFIG)
    contacts = await ac.aget_escalation_contacts("c1", category="Misrouted Order")
    assert contacts["email"]["to"] == ["logistics@example.com"]
    assert contacts["email"]["cc"] == ["ops@example.com"]


# ── backward compatibility ─────────────────────────────────────────────────


async def test_legacy_agent_phone_only(monkeypatch):
    _patch_config(monkeypatch, {"escalation_contact": {"AGENT_PHONE_NUMBER": "9876543210"}})
    phones = await ac.aget_escalation_recipients("c1", agent="return_exchange", category="X")
    assert phones == ["9876543210"]
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["email"] == {"to": [], "cc": []}


async def test_legacy_bare_list_node(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": ["9111111111"],
                    "routes": {"return_exchange": ["9111111112", "9111111113"]},
                }
            }
        },
    )
    phones = await ac.aget_escalation_recipients("c1", agent="return_exchange")
    assert phones == ["9111111112", "9111111113"]
    # bare list is phone-only -> no email
    contacts = await ac.aget_escalation_contacts("c1", agent="return_exchange")
    assert contacts["email"] == {"to": [], "cc": []}


async def test_legacy_email_list_shape(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {"contacts": {"phone": ["9111111111"], "email": ["a@b.com"]}}
                }
            }
        },
    )
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["email"] == {"to": ["a@b.com"], "cc": []}


async def test_config_value_as_json_string(monkeypatch):
    import json

    _patch_config(
        monkeypatch,
        {"escalation_contact": json.dumps({"AGENT_PHONE_NUMBER": "9000000000"})},
    )
    phones = await ac.aget_escalation_recipients("c1")
    assert phones == ["9000000000"]


# ── de-duplication ─────────────────────────────────────────────────────────


async def test_phone_dedup_country_code_variants(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {"contacts": {"phone": ["9111111111", "+919111111111", "919111111111"]}}
                }
            }
        },
    )
    phones = await ac.aget_escalation_recipients("c1")
    # all three are the same number -> collapse to the first-seen form
    assert phones == ["9111111111"]


# ── graceful degradation ───────────────────────────────────────────────────


async def test_missing_config_returns_empty(monkeypatch):
    _patch_config(monkeypatch, {})
    phones = await ac.aget_escalation_recipients("c1", agent="return_exchange")
    assert phones == []
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["phone"] == []
    assert contacts["email"] == {"to": [], "cc": []}
    assert contacts["template"] is None


async def test_malformed_json_string(monkeypatch):
    _patch_config(monkeypatch, {"escalation_contact": "{not valid json"})
    phones = await ac.aget_escalation_recipients("c1")
    assert phones == []


# ── escalation_group resolution ────────────────────────────────────────────


async def test_escalation_group_code_default(monkeypatch):
    _patch_config(monkeypatch, {})
    assert await ac.aget_escalation_group("c1", "Undelivered Order") == "post_sales"
    assert await ac.aget_escalation_group("c1", "Restocking Query") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Offline Store Suggestion") == "offline_leads"
    # unknown category -> conservative pre_sales
    assert await ac.aget_escalation_group("c1", "Totally Unknown") == "pre_sales"


async def test_escalation_group_client_override(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_group_categories": {
                "escalation_group_categories": {
                    "post_sales": ["Custom Category"],
                }
            }
        },
    )
    assert await ac.aget_escalation_group("c1", "Custom Category") == "post_sales"


async def test_frustration_group_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # No order in play -> pre_sales lead (unchanged default).
    assert await ac.aget_escalation_group("c1", "Frustration") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Frustration", order_id=None) == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Frustration", order_id="") == "pre_sales"
    # An order_id present -> post-purchase concern -> post_sales.
    assert await ac.aget_escalation_group("c1", "Frustration", order_id="gv16384") == "post_sales"


async def test_general_group_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # The catch-all "General" is a pre_sales lead with no order, but a
    # post-purchase concern once an order is referenced.
    assert await ac.aget_escalation_group("c1", "General") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "General", order_id="") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "General", order_id="gv16569") == "post_sales"


async def test_delivery_timeline_inquiry_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # Without an order it's a pre-purchase "how long does shipping take?" question.
    assert await ac.aget_escalation_group("c1", "Delivery Timeline Inquiry") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Delivery Timeline Inquiry", order_id="") == "pre_sales"
    # With an order it's a post-purchase "where is my package?" concern.
    assert await ac.aget_escalation_group("c1", "Delivery Timeline Inquiry", order_id="gv17248") == "post_sales"


async def test_callback_request_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # Without an order it's a pre-sales lead wanting to talk.
    assert await ac.aget_escalation_group("c1", "Callback Request") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Callback Request", order_id="") == "pre_sales"
    # With an order it's a customer needing help with their existing purchase.
    assert await ac.aget_escalation_group("c1", "Callback Request", order_id="gv17403") == "post_sales"


async def test_recommendation_handoff_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # Without an order it's a pre-sales product discovery handoff.
    assert await ac.aget_escalation_group("c1", "Recommendation Hand-off") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Recommendation Hand-off", order_id="") == "pre_sales"
    # With an order it's a post-purchase concern (e.g. customer wants alternative for an ordered item).
    assert await ac.aget_escalation_group("c1", "Recommendation Hand-off", order_id="gv17412") == "post_sales"


async def test_order_id_does_not_affect_mapped_category(monkeypatch):
    _patch_config(monkeypatch, {})
    # A mapped category keeps its static group regardless of order presence —
    # an explicit pre_sales category stays pre_sales even with an order.
    assert await ac.aget_escalation_group("c1", "Restocking Query", order_id="gv16384") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Bulk Order Discount", order_id="gv16384") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "Undelivered Order", order_id="gv16384") == "post_sales"


async def test_unmapped_category_group_depends_on_order_context(monkeypatch):
    _patch_config(monkeypatch, {})
    # Off-enum slugs the LLM may pass (e.g. "urgent_delivery") are not in the
    # static map. With no order they stay conservative pre_sales; with an order
    # in play they are post-purchase concerns -> post_sales.
    assert await ac.aget_escalation_group("c1", "urgent_delivery") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "urgent_delivery", order_id=None) == "pre_sales"
    assert await ac.aget_escalation_group("c1", "urgent_delivery", order_id="") == "pre_sales"
    assert await ac.aget_escalation_group("c1", "urgent_delivery", order_id="gv16569") == "post_sales"
    # Any other unknown slug behaves the same.
    assert await ac.aget_escalation_group("c1", "Totally Unknown", order_id="gv1") == "post_sales"


async def test_unmapped_category_override_still_wins(monkeypatch):
    # An explicit client override for an off-enum category wins over the
    # order-context default.
    _patch_config(
        monkeypatch,
        {
            "escalation_group_categories": {
                "escalation_group_categories": {
                    "pre_sales": ["urgent_delivery"],
                }
            }
        },
    )
    assert await ac.aget_escalation_group("c1", "urgent_delivery", order_id="gv16569") == "pre_sales"


# ── order-aware bucket contact routing ─────────────────────────────────────

# Distinct phone + email per bucket so a switch is unambiguous.
BUCKET_ROUTING_CONFIG = {
    "escalation_contact": {
        "ESCALATION_ROUTING": {
            "default": {"contacts": {"phone": ["9000000000"], "email": {"to": ["default@example.com"]}}},
            "buckets": {
                "pre_sales": {"contacts": {"phone": ["9111111111"], "email": {"to": ["pre@example.com"]}}},
                "post_sales": {"contacts": {"phone": ["9222222222"], "email": {"to": ["post@example.com"]}}},
            },
        }
    }
}


async def test_bucket_routing_general_is_order_aware(monkeypatch):
    _patch_config(monkeypatch, BUCKET_ROUTING_CONFIG)
    # General with no order -> pre_sales bucket.
    c = await ac.aget_escalation_contacts("c1", category="General")
    assert c["phone"] == ["9111111111"]
    assert c["email"]["to"] == ["pre@example.com"]
    # General WITH an order -> post_sales bucket (the whole point of the fix):
    # the notification now lands in the same bucket the card's "Type" advertises.
    c = await ac.aget_escalation_contacts("c1", category="General", order_id="gv16569")
    assert c["phone"] == ["9222222222"]
    assert c["email"]["to"] == ["post@example.com"]


async def test_bucket_routing_frustration_and_unmapped_order_aware(monkeypatch):
    _patch_config(monkeypatch, BUCKET_ROUTING_CONFIG)
    # Frustration + order -> post_sales bucket.
    c = await ac.aget_escalation_contacts("c1", category="Frustration", order_id="gv1")
    assert c["phone"] == ["9222222222"]
    # Unmapped slug + order -> post_sales bucket.
    c = await ac.aget_escalation_contacts("c1", category="urgent_delivery", order_id="gv1")
    assert c["phone"] == ["9222222222"]


async def test_bucket_routing_unaffected_cases(monkeypatch):
    _patch_config(monkeypatch, BUCKET_ROUTING_CONFIG)
    # An explicit pre_sales category with an order stays in the pre_sales bucket.
    c = await ac.aget_escalation_contacts("c1", category="Bulk Order Discount", order_id="gv1")
    assert c["phone"] == ["9111111111"]
    # A statically post_sales category routes to post_sales regardless of order.
    c = await ac.aget_escalation_contacts("c1", category="Undelivered Order")
    assert c["phone"] == ["9222222222"]
    # Frustration with NO order stays pre_sales.
    c = await ac.aget_escalation_contacts("c1", category="Frustration")
    assert c["phone"] == ["9111111111"]


async def test_recipients_wrapper_threads_order_id(monkeypatch):
    _patch_config(monkeypatch, BUCKET_ROUTING_CONFIG)
    assert await ac.aget_escalation_recipients("c1", category="General") == ["9111111111"]
    assert await ac.aget_escalation_recipients("c1", category="General", order_id="gv1") == ["9222222222"]


async def test_client_override_wins_over_order_context(monkeypatch):
    # An explicit client override pinning Frustration to pre_sales must win even
    # when an order_id is present.
    _patch_config(
        monkeypatch,
        {
            "escalation_group_categories": {
                "escalation_group_categories": {
                    "pre_sales": ["Frustration"],
                }
            }
        },
    )
    assert await ac.aget_escalation_group("c1", "Frustration", order_id="gv16384") == "pre_sales"


# ── category normalization ─────────────────────────────────────────────────


def test_normalize_category_exact_canonical_passthrough():
    for cat in ("Frustration", "Delivery Query", "Undelivered Order", "General"):
        assert ac.normalize_escalation_category(cat) == cat


def test_normalize_category_case_and_spacing_variants():
    assert ac.normalize_escalation_category("delivery query") == "Delivery Query"
    assert ac.normalize_escalation_category("  Delivery-Query ") == "Delivery Query"
    assert ac.normalize_escalation_category("UNDELIVERED_ORDER") == "Undelivered Order"


def test_normalize_category_off_enum_alias_slugs():
    # The reported bug: LLM passes the internal escalation_type slug.
    assert ac.normalize_escalation_category("urgent_delivery") == "Delivery Query"
    assert ac.normalize_escalation_category("cancellation_threat") == "Cancellation Requests"


def test_normalize_category_unknown_falls_back_to_general():
    for junk in ("", "   ", None, "something_totally_made_up"):
        assert ac.normalize_escalation_category(junk) == "General"


async def test_normalized_category_resolves_to_expected_group(monkeypatch):
    # End-to-end intent of the fix: urgent_delivery + order → Delivery Query → post_sales.
    _patch_config(monkeypatch, {})
    cat = ac.normalize_escalation_category("urgent_delivery")
    assert cat == "Delivery Query"
    assert await ac.aget_escalation_group("c1", cat, order_id="gv16569") == "post_sales"


def test_alias_targets_are_canonical():
    # Every alias must point at a real canonical category.
    for target in ac.ESCALATION_CATEGORY_ALIASES.values():
        assert target in ac.ESCALATION_TOOL_CATEGORIES


# ── map consistency (locks the enum/maps together) ─────────────────────────


def test_tool_categories_have_agent_and_group():
    for cat in ac.ESCALATION_TOOL_CATEGORIES:
        if cat == "General":
            continue
        assert cat in ac.CATEGORY_TO_AGENT, f"{cat} missing from CATEGORY_TO_AGENT"
        assert cat in ac.CATEGORY_TO_ESCALATION_GROUP, f"{cat} missing from CATEGORY_TO_ESCALATION_GROUP"


def test_all_groups_valid():
    for grp in ac.CATEGORY_TO_ESCALATION_GROUP.values():
        assert grp in ac.VALID_ESCALATION_GROUPS


def test_all_routed_agents_are_real_agents():
    # Every agent bucket referenced by the maps must be a real registry agent.
    from fashion_bot.core.tool_registry import TOOL_REGISTRY

    valid = set(TOOL_REGISTRY.keys())
    for agent in set(ac.CATEGORY_TO_AGENT.values()) | set(ac.PARENT_INTENT_TO_AGENT.values()):
        assert agent in valid, f"{agent} is not a registered agent"


# ── build_escalation_metadata ──────────────────────────────────────────────


async def test_metadata_canonical_fields(monkeypatch):
    _patch_config(monkeypatch, {})
    from fashion_bot.utils.escalation_helper import build_escalation_metadata

    md = await build_escalation_metadata(
        client_id="c1",
        category="Cancellation Requests",
        trace_id="tr_1",
        phone_number="9123",
        escalation_classification="user_configured",
        immediate_attention=True,
        agent="cancel_or_update_order",
        extra={"order_id": "GV1", "escalation_type": "SHOULD_NOT_WIN"},
    )
    assert md["trace_id"] == "tr_1"
    assert md["phone_number"] == "9123"
    assert md["escalation_type"] == "cancellation_requests"  # canonical wins over extra
    assert md["escalation_classification"] == "user_configured"
    assert md["escalation_group"] == "post_sales"
    assert md["immediate_attention"] is True
    assert md["order_id"] == "GV1"  # caller extra preserved


async def test_metadata_invalid_classification_defaults_system(monkeypatch):
    _patch_config(monkeypatch, {})
    from fashion_bot.utils.escalation_helper import build_escalation_metadata

    md = await build_escalation_metadata(
        client_id="c1", category="General", escalation_classification="bogus"
    )
    assert md["escalation_classification"] == "system"
    assert md["escalation_group"] == "pre_sales"
    assert md["immediate_attention"] is False


# ── asend_escalation_notification fan-out ──────────────────────────────────


def _install_fake_gupshup(monkeypatch, sent, fail_numbers=()):
    """Inject a fake fashion_bot.gupshup_webhook module exposing send_message."""

    mod = types.ModuleType("fashion_bot.gupshup_webhook")

    async def _send_message(to, message, trace_id=None, gupshup_source=None, client_id=None):
        if to in fail_numbers:
            raise RuntimeError(f"boom {to}")
        sent.append((to, message))
        return {"ok": True}

    mod.send_message = _send_message
    monkeypatch.setitem(sys.modules, "fashion_bot.gupshup_webhook", mod)


async def test_fanout_all_numbers(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)

    async def _fake_contacts(client_id, *, agent=None, category=None, parent_intent=None):
        return {"phone": ["9111111111", "9999999999"], "email": {"to": [], "cc": []}}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _fake_contacts)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification("hello", client_id="c1", agent="return_exchange")
    assert set(result["sent"]) == {"9111111111", "9999999999"}
    assert result["failed"] == []
    assert len(sent) == 2


async def test_fanout_isolation_one_failure(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent, fail_numbers={"9999999999"})

    async def _fake_contacts(client_id, *, agent=None, category=None, parent_intent=None):
        return {"phone": ["9111111111", "9999999999"], "email": {"to": [], "cc": []}}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _fake_contacts)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification("hello", client_id="c1")
    assert result["sent"] == ["9111111111"]
    assert result["failed"] == ["9999999999"]


async def test_immediate_attention_banner(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)

    async def _fake_contacts(client_id, *, agent=None, category=None, parent_intent=None):
        return {"phone": ["9111111111"], "email": {"to": [], "cc": []}}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _fake_contacts)

    from fashion_bot.utils.escalation_helper import (
        IMMEDIATE_ATTENTION_BANNER,
        asend_escalation_notification,
    )

    await asend_escalation_notification(
        "body", client_id="c1", immediate_attention=True
    )
    assert sent[0][1].startswith(IMMEDIATE_ATTENTION_BANNER)


async def test_no_numbers_no_send(monkeypatch):
    sent = []
    _install_fake_gupshup(monkeypatch, sent)

    async def _fake_contacts(client_id, *, agent=None, category=None, parent_intent=None):
        return {"phone": [], "email": {"to": [], "cc": []}}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _fake_contacts)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification("body", client_id="c1")
    assert result["sent"] == [] and result["failed"] == []
    assert sent == []


# ── central metadata enrichment in alog_escalation_from_state ───────────────


def _install_fake_publishers(monkeypatch):
    """Stub the lazily-imported escalation event publisher module."""
    pub = types.ModuleType("fashion_bot.workers.event_publishers")

    def _fire_and_forget(coro, label=None):
        # Close the coroutine so it doesn't emit an un-awaited warning.
        try:
            coro.close()
        except Exception:
            pass

    async def _publish_escalation_event(payload):
        return None

    pub.fire_and_forget = _fire_and_forget
    pub.publish_escalation_event = _publish_escalation_event
    monkeypatch.setitem(sys.modules, "fashion_bot.workers.event_publishers", pub)


def _capture_alog(monkeypatch):
    """Patch escalation_logger.alog_escalation to capture what it receives."""
    import fashion_bot.utils.escalation_logger as el

    captured = {}

    async def _fake_alog_escalation(**kwargs):
        captured.update(kwargs)
        return "esc_1"

    monkeypatch.setattr(el, "alog_escalation", _fake_alog_escalation)
    return captured


async def test_log_from_state_enriches_group_and_canonical(monkeypatch):
    _patch_config(monkeypatch, {})  # code-default group map
    _install_fake_publishers(monkeypatch)
    captured = _capture_alog(monkeypatch)

    from fashion_bot.utils.escalation_helper import alog_escalation_from_state

    state = {"client_id": "c1", "phone_number": "9123", "trace_id": "tr_x"}
    await alog_escalation_from_state(
        state,
        category="Undelivered Order",
        reason="r",
        action_required="a",
        metadata={
            "escalation_classification": "agentic",
            "immediate_attention": True,
            "agent": "order_status",
            "escalation_type": "undelivered",  # caller subtype, differs from category slug
            "order_id": "GV1",
        },
    )
    md = captured["metadata"]
    assert md["escalation_group"] == "post_sales"
    assert md["immediate_attention"] is True
    assert md["escalation_classification"] == "agentic"
    assert md["escalation_type"] == "undelivered_order"  # canonical = category slug
    assert md["escalation_subtype"] == "undelivered"  # caller type preserved (non-lossy)
    assert md["order_id"] == "GV1"
    assert md["trace_id"] == "tr_x"
    # control keys consumed, not left dangling as duplicates
    assert "agent" not in md


async def test_log_from_state_offline_store_group(monkeypatch):
    _patch_config(monkeypatch, {})
    _install_fake_publishers(monkeypatch)
    captured = _capture_alog(monkeypatch)

    from fashion_bot.utils.escalation_helper import alog_escalation_from_state

    state = {"client_id": "c1", "phone_number": "9123", "trace_id": "tr_s"}
    await alog_escalation_from_state(
        state,
        category="Offline Store Suggestion",
        reason="Store visit suggested: Flagship",
        action_required="Follow up",
        metadata={"escalation_classification": "system", "store_name": "Flagship"},
    )
    md = captured["metadata"]
    assert md["escalation_group"] == "offline_leads"
    assert md["escalation_type"] == "offline_store_suggestion"
    assert md["store_name"] == "Flagship"


# ── store-visit contacts (design §5.4b, store-manager-first) ────────────────


async def test_store_visit_manager_first_plus_default(monkeypatch):
    # Scenario A — store manager configured, no Offline-Store override; ops from default.
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {"contacts": {"phone": ["9222222222"], "email": {"to": ["support@brand.com"]}}}
                }
            }
        },
    )
    store = {"name": "Connaught Place", "phone": "9876543210", "manager_email": "cp-mgr@brand.com"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    # store manager first, then default ops
    assert contacts["phone"] == ["9876543210", "9222222222"]
    assert contacts["email"]["to"] == ["cp-mgr@brand.com", "support@brand.com"]


async def test_store_visit_dedicated_route_with_cc(monkeypatch):
    # Scenario B — store manager + dedicated ops route with CC.
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {"contacts": {"phone": ["9111111111"]}},
                    "routes": {
                        "Offline Store Suggestion": {
                            "contacts": {
                                "phone": ["9222222222"],
                                "email": {"to": ["retail-ops@brand.com"], "cc": ["store-leads@brand.com"]},
                            }
                        }
                    },
                }
            }
        },
    )
    store = {"phone": "9876543210", "manager_email": "cp-mgr@brand.com"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    assert contacts["phone"] == ["9876543210", "9222222222"]
    assert contacts["email"]["to"] == ["cp-mgr@brand.com", "retail-ops@brand.com"]
    assert contacts["email"]["cc"] == ["store-leads@brand.com"]


async def test_store_visit_no_manager_falls_through(monkeypatch):
    # Scenario C — legacy store with no contacts → identical to generic routing.
    _patch_config(
        monkeypatch,
        {"escalation_contact": {"AGENT_PHONE_NUMBER": "9111111111"}},
    )
    store = {"name": "Old Store", "city": "Delhi"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    assert contacts["phone"] == ["9111111111"]
    assert contacts["email"]["to"] == []


async def test_store_visit_dedup_same_person(monkeypatch):
    # Scenario D — store manager == ops contact → one recipient, not two.
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {"default": {"contacts": {"phone": ["9876543210"]}}}
            }
        },
    )
    store = {"phone": "9876543210", "manager_email": "x@brand.com"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    assert contacts["phone"] == ["9876543210"]  # collapsed


async def test_contacts_override_bypasses_routing(monkeypatch):
    # asend_escalation_notification honors contacts_override without hitting config.
    sent = []
    _install_fake_gupshup(monkeypatch, sent)

    called = {"routing": False}

    async def _should_not_run(*a, **k):
        called["routing"] = True
        return {"phone": [], "email": {"to": [], "cc": []}}

    monkeypatch.setattr(ac, "aget_escalation_contacts", _should_not_run)

    from fashion_bot.utils.escalation_helper import asend_escalation_notification

    result = await asend_escalation_notification(
        "hi",
        client_id="c1",
        contacts_override={"phone": ["9876543210"], "email": {"to": [], "cc": []}},
    )
    assert result["sent"] == ["9876543210"]
    assert called["routing"] is False  # routing resolver never consulted


async def test_log_from_state_defaults_when_no_metadata(monkeypatch):
    _patch_config(monkeypatch, {})
    _install_fake_publishers(monkeypatch)
    captured = _capture_alog(monkeypatch)

    from fashion_bot.utils.escalation_helper import alog_escalation_from_state

    state = {"client_id": "c1", "phone_number": "9123", "trace_id": "tr_y"}
    await alog_escalation_from_state(
        state, category="Restocking Query", reason="r", action_required="a"
    )
    md = captured["metadata"]
    assert md["escalation_group"] == "pre_sales"
    assert md["escalation_classification"] == "system"
    assert md["immediate_attention"] is False
    assert md["escalation_type"] == "restocking_query"


# ── per-route template resolution ──────────────────────────────────────────

TEMPLATE_ROUTING_CONFIG = {
    "escalation_contact": {
        "AGENT_PHONE_NUMBER": "9111111111",
        "ESCALATION_ROUTING": {
            "default": {
                "contacts": {
                    "phone": ["9111111111"],
                    "email": {"to": ["support@example.com"]},
                },
                "template": {
                    "enabled": True,
                    "template_id": "default_tmpl",
                    "image_url": "https://cdn.example.com/logo.png",
                },
            },
            "buckets": {
                "pre_sales": {
                    "contacts": {"phone": ["9111111115"]},
                    "template": {
                        "enabled": True,
                        "template_id": "presales_tmpl",
                    },
                },
                "post_sales": {
                    "template": {
                        "enabled": True,
                        "template_id": "postsales_tmpl",
                    },
                },
            },
            "routes": {
                "order_status": {
                    "contacts": {"phone": ["9111111113"]},
                    "template": {
                        "enabled": True,
                        "template_id": "orderstatus_agent_tmpl",
                    },
                },
                "Order Status Query": {
                    "contacts": {"phone": ["9111111114"]},
                    "template": {
                        "enabled": True,
                        "template_id": "orderstatus_cat_tmpl",
                    },
                },
                "Offline Store Suggestion": {
                    "template": {"enabled": False},
                },
                "return_exchange": {
                    "contacts": {"phone": ["9111111112"]},
                },
            },
        },
    }
}


async def test_template_category_route_wins(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    contacts = await ac.aget_escalation_contacts(
        "c1", category="Order Status Query", agent="order_status"
    )
    assert contacts["template"]["template_id"] == "orderstatus_cat_tmpl"
    assert "param_order" not in contacts["template"]


async def test_template_agent_route_wins_when_no_category(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    contacts = await ac.aget_escalation_contacts("c1", agent="order_status")
    assert contacts["template"]["template_id"] == "orderstatus_agent_tmpl"


async def test_template_bucket_fallback(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    # "Restocking Query" maps to product_details (no template route),
    # falls to buckets["pre_sales"].
    contacts = await ac.aget_escalation_contacts("c1", category="Restocking Query")
    assert contacts["template"]["template_id"] == "presales_tmpl"


async def test_template_default_fallback(monkeypatch):
    # Use a config with NO buckets so the cascade falls through to default.
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {
                        "contacts": {"phone": ["9111111111"]},
                        "template": {
                            "enabled": True,
                            "template_id": "default_tmpl",
                            "image_url": "https://cdn.example.com/logo.png",
                        },
                    },
                    "routes": {
                        "order_status": {
                            "contacts": {"phone": ["9111111113"]},
                            "template": {
                                "enabled": True,
                                "template_id": "orderstatus_agent_tmpl",
                            },
                        },
                    },
                }
            }
        },
    )
    # Unknown agent, no buckets → default.
    contacts = await ac.aget_escalation_contacts("c1", agent="unknown_agent")
    assert contacts["template"]["template_id"] == "default_tmpl"
    assert contacts["template"]["image_url"] == "https://cdn.example.com/logo.png"


async def test_template_explicit_disable_stops_cascade(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    # "Offline Store Suggestion" route has template.enabled=false.
    # Even though default has a template, cascade stops → None.
    contacts = await ac.aget_escalation_contacts(
        "c1", category="Offline Store Suggestion"
    )
    assert contacts["template"] is None


async def test_template_none_when_no_block(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    # return_exchange route has contacts but no template block.
    # Falls through to buckets["pre_sales"] which has a template.
    # Wait — return_exchange categories are post_sales so bucket won't match pre_sales.
    # "Return Request" maps to return_exchange; CATEGORY_TO_ESCALATION_GROUP says post_sales.
    # post_sales bucket has a template.
    contacts = await ac.aget_escalation_contacts(
        "c1", agent="return_exchange", category="Return Request"
    )
    assert contacts["template"]["template_id"] == "postsales_tmpl"


async def test_template_resolves_independently_from_contacts(monkeypatch):
    _patch_config(monkeypatch, TEMPLATE_ROUTING_CONFIG)
    # post_sales bucket has a template but no contacts.phone.
    # Phones should come from a different node; template from the bucket.
    contacts = await ac.aget_escalation_contacts(
        "c1", category="Cancellation Requests"
    )
    # Cancellation Requests → CATEGORY_TO_AGENT → cancel_or_update_order (no route)
    # Phones: falls to default → ["9111111111"]
    assert contacts["phone"] == ["9111111111"]
    # Template: cancel_or_update_order not in routes, CATEGORY_TO_ESCALATION_GROUP
    # says post_sales → buckets["post_sales"].template wins.
    assert contacts["template"]["template_id"] == "postsales_tmpl"


async def test_template_no_config_returns_none(monkeypatch):
    _patch_config(monkeypatch, {"escalation_contact": {"AGENT_PHONE_NUMBER": "9876543210"}})
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["template"] is None


async def test_template_missing_config_returns_none(monkeypatch):
    _patch_config(monkeypatch, {})
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["template"] is None


async def test_template_bare_list_node_has_no_template(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": ["9111111111"],
                }
            }
        },
    )
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["template"] is None


async def test_template_enabled_false_string(monkeypatch):
    _patch_config(
        monkeypatch,
        {
            "escalation_contact": {
                "ESCALATION_ROUTING": {
                    "default": {
                        "contacts": {"phone": ["9111111111"]},
                        "template": {
                            "enabled": "false",
                            "template_id": "should_not_resolve",
                        },
                    }
                }
            }
        },
    )
    contacts = await ac.aget_escalation_contacts("c1")
    assert contacts["template"] is None


# ── store-visit template threading ──────────────────────────────────────────

def _patch_store_notification_template(monkeypatch, result):
    from fashion_bot.utils import store_locations as sl

    async def _fake(client_id):
        return result

    monkeypatch.setattr(sl, "aget_store_notification_template", _fake)


async def test_store_visit_template_from_store_config(monkeypatch):
    _patch_config(monkeypatch, {
        "escalation_contact": {
            "ESCALATION_ROUTING": {
                "default": {"contacts": {"phone": ["9222222222"]}},
            },
        },
    })
    _patch_store_notification_template(monkeypatch, {"template_id": "store_tmpl_123", "image_url": None})

    store = {"phone": "9876543210", "manager_email": "mgr@brand.com"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    assert contacts["template"]["template_id"] == "store_tmpl_123"


async def test_store_visit_no_template_when_flag_disabled(monkeypatch):
    _patch_config(monkeypatch, {
        "escalation_contact": {
            "ESCALATION_ROUTING": {
                "default": {"contacts": {"phone": ["9222222222"]}},
            },
        },
    })
    _patch_store_notification_template(monkeypatch, None)

    store = {"phone": "9876543210"}
    contacts = await ac.aget_store_visit_contacts("c1", store)
    assert contacts["template"] is None


async def test_store_visit_no_template_when_no_client_id(monkeypatch):
    _patch_config(monkeypatch, {
        "escalation_contact": {
            "ESCALATION_ROUTING": {
                "default": {"contacts": {"phone": ["9222222222"]}},
            },
        },
    })
    _patch_store_notification_template(monkeypatch, {"template_id": "store_tmpl_123"})

    store = {"phone": "9876543210"}
    contacts = await ac.aget_store_visit_contacts(None, store)
    assert contacts["template"] is None


# ── store_locations config shape ────────────────────────────────────────────


async def test_store_locations_dict_shape(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_enabled_for_notification": True,
            "template_id": "store_tmpl_abc",
            "locations": [
                {"name": "Store A", "city": "Delhi"},
                {"name": "Store B", "city": "Mumbai"},
            ],
        },
    })
    stores = await sl.aget_all_stores("c1")
    assert len(stores) == 2
    assert stores[0]["name"] == "Store A"

    tmpl = await sl.aget_store_notification_template("c1")
    assert tmpl["template_id"] == "store_tmpl_abc"


async def test_store_locations_legacy_list_shape(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": [
            {"name": "Store A", "city": "Delhi"},
        ],
    })
    stores = await sl.aget_all_stores("c1")
    assert len(stores) == 1

    assert await sl.aget_store_notification_template("c1") is None


async def test_store_template_flag_false_string(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_enabled_for_notification": "false",
            "template_id": "store_tmpl_abc",
            "locations": [{"name": "Store A"}],
        },
    })
    assert await sl.aget_store_notification_template("c1") is None


async def test_store_template_flag_absent(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_id": "store_tmpl_abc",
            "locations": [{"name": "Store A"}],
        },
    })
    assert await sl.aget_store_notification_template("c1") is None


async def test_store_template_enabled_but_no_template_id(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_enabled_for_notification": True,
            "locations": [],
        },
    })
    assert await sl.aget_store_notification_template("c1") is None


async def test_store_template_enabled_with_template_id(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_enabled_for_notification": "true",
            "template_id": "1665491144557994",
            "locations": [],
        },
    })
    tmpl = await sl.aget_store_notification_template("c1")
    assert tmpl["template_id"] == "1665491144557994"
    assert tmpl["image_url"] is None


async def test_store_template_with_image_url(monkeypatch):
    from fashion_bot.utils import store_locations as sl

    _patch_config(monkeypatch, {
        "store_locations": {
            "template_enabled_for_notification": True,
            "template_id": "tmpl_media",
            "image_url": "https://cdn.example.com/logo.png",
            "locations": [],
        },
    })
    tmpl = await sl.aget_store_notification_template("c1")
    assert tmpl["template_id"] == "tmpl_media"
    assert tmpl["image_url"] == "https://cdn.example.com/logo.png"
