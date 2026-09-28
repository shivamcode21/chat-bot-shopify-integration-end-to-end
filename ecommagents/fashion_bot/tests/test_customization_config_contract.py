"""
Contract tests for ``UtilityOrchestrator.get_customization_config``.

These complement the live-DB suite in ``test_product_details_tools.py``
(``TestGetCustomizationConfig``), which exercises the tool against Concept
Groove's real, populated policy. That suite passed throughout the GANT
false-promise incident because it only ever asserted on the tool return — and
the tool was never called. See
``design_docs/RCA_GANT_CUSTOMIZATION_FALSE_PROMISE.md``.

What is pinned here is the part the incident turned on: a policy that is
missing, blank, or unreadable must never be reported as a successful fetch,
because "success with nothing in it" is what let an agent fall back on its own
assumptions and invent an alteration service.

``aget_config`` is mocked, so these run without a database.
"""
import copy
from unittest.mock import AsyncMock, patch

import pytest

from fashion_bot.core.orchestrator import UtilityOrchestrator

CLIENT_ID = "f5a737a7-c274-48d3-a6d2-d067f14b755b"

POPULATED = {
    "Do you support customization?": "No we do not support personal customization",
    "Do you support size alteration?": "No",
}
BLANK = {"Do you support customization?": "", "Do you support size alteration?": ""}


def _patch_config(value=None, exc=None):
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=value)
    return patch("fashion_bot.config_manager.aget_config", mock), mock


class TestBlankPolicyDetection:
    """A row that exists but says nothing is not a policy."""

    @pytest.mark.parametrize(
        "value",
        [None, "", "   ", {}, BLANK, {"a": None, "b": "  "}, [], ["", "  "]],
        ids=["none", "empty-str", "whitespace", "empty-dict", "blank-values",
             "none-and-whitespace", "empty-list", "blank-list"],
    )
    def test_blank_values_are_blank(self, value):
        assert UtilityOrchestrator._is_blank_policy(value) is True

    @pytest.mark.parametrize(
        "value",
        [POPULATED, "No alterations", {"a": "", "b": "No"}, ["", "No"]],
        ids=["full-dict", "string", "partially-filled-dict", "partially-filled-list"],
    )
    def test_populated_values_are_not_blank(self, value):
        assert UtilityOrchestrator._is_blank_policy(value) is False


class TestReturnContract:

    @pytest.mark.asyncio
    async def test_populated_policy_is_a_successful_fetch(self):
        ctx, _ = _patch_config(POPULATED)
        with ctx:
            result = await UtilityOrchestrator.get_customization_config(
                state={"client_id": CLIENT_ID}
            )
        assert result["success"] is True
        assert result["policy_found"] is True
        assert result["policy"] == POPULATED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [None, "", BLANK], ids=["absent", "empty", "blank"])
    async def test_missing_or_blank_policy_is_not_success(self, value):
        """The regression that caused the incident."""
        ctx, _ = _patch_config(value)
        with ctx:
            result = await UtilityOrchestrator.get_customization_config(
                state={"client_id": CLIENT_ID}
            )
        assert result["success"] is False
        assert result["policy_found"] is False
        assert result["policy"] is None
        assert result["message"], "failure path must carry guidance for the agent"

    @pytest.mark.asyncio
    async def test_never_reports_success_with_an_empty_policy(self):
        """Guards the exact shape the old implementation returned."""
        for value in (None, "", BLANK, {}, POPULATED, "No alterations"):
            ctx, _ = _patch_config(value)
            with ctx:
                result = await UtilityOrchestrator.get_customization_config(
                    state={"client_id": CLIENT_ID}
                )
            assert not (result["success"] and not result["policy"]), (
                f"success=True with falsy policy for input {value!r}"
            )

    @pytest.mark.asyncio
    async def test_db_failure_does_not_look_like_a_policy(self):
        ctx, _ = _patch_config(exc=RuntimeError("db down"))
        with ctx:
            result = await UtilityOrchestrator.get_customization_config(
                state={"client_id": CLIENT_ID}
            )
        assert result["success"] is False
        assert result["policy_found"] is False
        assert result["policy"] is None
        assert "db down" in result["error"]

    @pytest.mark.asyncio
    async def test_always_carries_the_keys_callers_read(self):
        for value in (POPULATED, BLANK, None):
            ctx, _ = _patch_config(value)
            with ctx:
                result = await UtilityOrchestrator.get_customization_config(
                    state={"client_id": CLIENT_ID}
                )
            for key in ("success", "policy_found", "policy"):
                assert key in result, f"{key!r} missing for input {value!r}"


class TestTenantIsolation:
    """AGENTS.md §7 — a missing client_id is an error, not a default."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [None, {}, {"client_id": None}, {"client_id": ""}])
    async def test_missing_client_id_escalates(self, state):
        ctx, _ = _patch_config(POPULATED)
        with ctx, patch("fashion_bot.core.orchestrator.report_error") as reporter:
            result = await UtilityOrchestrator.get_customization_config(state=state)
        assert result["success"] is False
        assert result["policy_found"] is False
        reporter.assert_called_once()
        assert reporter.call_args.kwargs.get("level") == "error"
        assert "trace_id" in reporter.call_args.kwargs, "AGENTS.md §5: trace_id on Rollbar errors"

    @pytest.mark.asyncio
    async def test_missing_client_id_never_reads_config(self):
        """No client_id must mean no read at all — never a cross-tenant fallback."""
        ctx, mock = _patch_config(POPULATED)
        with ctx, patch("fashion_bot.core.orchestrator.report_error"):
            await UtilityOrchestrator.get_customization_config(state={})
        assert mock.await_count == 0


class TestStatelessness:
    """AGENTS.md Core Principle 2 — tools must not mutate shared state."""

    @pytest.mark.asyncio
    async def test_does_not_mutate_state(self):
        state = {"client_id": CLIENT_ID, "conversation_context": {"entity_count": 3}}
        before = copy.deepcopy(state)
        ctx, _ = _patch_config(POPULATED)
        with ctx:
            await UtilityOrchestrator.get_customization_config(state=state)
        assert state == before
