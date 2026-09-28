"""
Tests for the two per-client escalation overrides
(``fashion_bot/agent_config.py`` + ``fashion_bot/utils/order_utils.py``).

Both exist because the behaviour they control is NOT reachable from an agent
prompt:

* ``escalation_policy.order_notes_enabled`` — the ``[Bloomerce] … Requires
  manual intervention`` / ``[Bot - FAILED] …`` notes are written by
  orchestration code on the escalation path, not by the LLM; and
* ``escalation_messaging.customer_message`` — the web-chat escalation reply is
  force-set in ``generic_skill_node``, discarding whatever the prompt produced.

Both default to today's behaviour and degrade to it on malformed config, so an
unconfigured client is unaffected.
"""

import pytest

import fashion_bot.agent_config as ac
import fashion_bot.config_manager as config_manager
from fashion_bot.utils.order_utils import aadd_escalation_order_note


def _patch_config(monkeypatch, values: dict):
    """Serve ``values`` keyed by config_key; anything else returns the default."""

    async def _fake(config_key, default=None, client_id=None):
        return values.get(config_key, default)

    monkeypatch.setattr(config_manager, "aget_config", _fake)


class _FakeOrderService:
    def __init__(self, raises: bool = False):
        self.raises = raises
        self.notes = []

    async def aadd_order_note(self, order_id, note, state=None):
        if self.raises:
            raise RuntimeError("shopify 500")
        self.notes.append((order_id, note))
        return {"success": True}


# ---------------------------------------------------------------------------
# escalation_policy.order_notes_enabled
# ---------------------------------------------------------------------------
async def test_order_notes_enabled_by_default(monkeypatch):
    _patch_config(monkeypatch, {})
    assert await ac.aescalation_order_notes_enabled("c1") is True


async def test_order_notes_enabled_when_key_absent(monkeypatch):
    _patch_config(monkeypatch, {"escalation_policy": {"gate_enabled": False}})
    assert await ac.aescalation_order_notes_enabled("c1") is True


async def test_client_can_opt_out_of_order_notes(monkeypatch):
    _patch_config(monkeypatch, {"escalation_policy": {"order_notes_enabled": False}})
    assert await ac.aescalation_order_notes_enabled("c1") is False


async def test_order_notes_flag_reads_json_string_config(monkeypatch):
    _patch_config(monkeypatch, {"escalation_policy": '{"order_notes_enabled": false}'})
    assert await ac.aescalation_order_notes_enabled("c1") is False


async def test_malformed_config_keeps_order_notes_on(monkeypatch):
    # A bad config must never silently drop the ops audit trail.
    _patch_config(monkeypatch, {"escalation_policy": "{not valid json"})
    assert await ac.aescalation_order_notes_enabled("c1") is True


# ---------------------------------------------------------------------------
# aadd_escalation_order_note
# ---------------------------------------------------------------------------
async def test_escalation_note_is_written_by_default(monkeypatch):
    _patch_config(monkeypatch, {})
    svc = _FakeOrderService()
    written = await aadd_escalation_order_note(
        svc, "ENAMOR-1", "[Bloomerce] manual intervention", state={"client_id": "c1"}
    )
    assert written is True
    assert svc.notes == [("ENAMOR-1", "[Bloomerce] manual intervention")]


async def test_escalation_note_suppressed_when_opted_out(monkeypatch):
    _patch_config(monkeypatch, {"escalation_policy": {"order_notes_enabled": False}})
    svc = _FakeOrderService()
    written = await aadd_escalation_order_note(
        svc, "ENAMOR-1", "[Bloomerce] manual intervention", state={"client_id": "c1"}
    )
    assert written is False
    assert svc.notes == []


async def test_escalation_note_write_failure_is_swallowed(monkeypatch):
    # A note failure must never break the escalation that follows it.
    _patch_config(monkeypatch, {})
    svc = _FakeOrderService(raises=True)
    assert (
        await aadd_escalation_order_note(
            svc, "ENAMOR-1", "note", state={"client_id": "c1"}
        )
        is False
    )


# ---------------------------------------------------------------------------
# escalation_messaging.customer_message
# ---------------------------------------------------------------------------
DEFAULT = "Our team will contact you soon."
OVERRIDE = "Your issue has been escalated. Please contact our support team."


async def test_customer_message_falls_back_to_default(monkeypatch):
    _patch_config(monkeypatch, {})
    assert await ac.aget_escalation_customer_message("c1", default=DEFAULT) == DEFAULT


async def test_customer_message_override_is_used(monkeypatch):
    _patch_config(
        monkeypatch, {"escalation_messaging": {"customer_message": OVERRIDE}}
    )
    assert await ac.aget_escalation_customer_message("c1", default=DEFAULT) == OVERRIDE


async def test_customer_message_reads_json_string_config(monkeypatch):
    _patch_config(
        monkeypatch,
        {"escalation_messaging": '{"customer_message": "' + OVERRIDE + '"}'},
    )
    assert await ac.aget_escalation_customer_message("c1", default=DEFAULT) == OVERRIDE


@pytest.mark.parametrize(
    "raw",
    [
        {"customer_message": "   "},          # blank
        {"customer_message": None},           # wrong type
        {"customer_message": 42},             # wrong type
        {},                                   # key absent
        "{not valid json",                    # malformed
        [],                                   # wrong shape
    ],
)
async def test_customer_message_degrades_to_default(monkeypatch, raw):
    # A bad config must never blank out a customer-facing reply.
    _patch_config(monkeypatch, {"escalation_messaging": raw})
    assert await ac.aget_escalation_customer_message("c1", default=DEFAULT) == DEFAULT
