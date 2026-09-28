"""
LogisticsRouter — picks which integrated logistics partner(s) to call for a
given order, then races them with per-partner timeouts.

Why this exists:
  - Multi-partner clients: a tenant may have *both* Shiprocket and Delhivery
    connected. Today's orchestrator hard-codes Shiprocket. The router lets
    the orchestrator stay vendor-neutral.
  - Per-partner deadlines: a slow partner shouldn't gate the user-facing
    answer once we have a valid response from another partner. Each call
    is wrapped in `asyncio.wait_for(..., 10)` per AGENTS.md graceful-degradation.
  - Order's primary partner first: Shopify's `tracking_company` tells us
    which partner physically shipped the order. We call that one and
    short-circuit. Only fan out when the carrier signal is missing/unknown
    (e.g. NEW unfulfilled orders).

Read-path racing cancels losers (no point keeping them around). Write-path
runs all partners under ONE shared deadline via `asyncio.wait(timeout=)` —
which never cancels its tasks — so we don't half-cancel a POST mid-flight;
any partner still running when the deadline fires finishes in the background
and we log its outcome with the same trace_id.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)


# Hard deadline per partner attempt. Configurable via env for tests.
import os
PER_PARTNER_TIMEOUT_S: float = float(os.getenv("LOGISTICS_PER_PARTNER_TIMEOUT_S", "10.0"))


# Track shielded write tasks that timed out and were left in flight. Without a
# strong reference here, the create_task return value would be GC'd and the
# orphan could be cancelled non-deterministically by the event-loop reaper.
# WeakSet means tasks self-evict on completion; nothing leaks if they run.
_ORPHAN_WRITE_TASKS: "weakref.WeakSet[asyncio.Task]" = weakref.WeakSet()


def get_orphan_write_tasks() -> List[asyncio.Task]:
    """Snapshot of currently in-flight orphaned writes (for metrics / shutdown)."""
    return [t for t in _ORPHAN_WRITE_TASKS if not t.done()]


# ── validity rules (pure Python, no LLM) ──────────────────────────────


def _is_valid_status_result(r: Optional[Dict[str, Any]]) -> bool:
    """Treat a result as valid if it looks like a real lookup hit."""
    if not isinstance(r, dict):
        return False
    if r.get("success") is False:
        return False
    if not r.get("found", False) and "orders" not in r:
        return False
    od = r.get("order_data") or {}
    if not od and r.get("orders"):
        first = r["orders"][0] or {}
        od = first.get("order_data") or {}
    return bool(od.get("status") or od.get("awb") or od.get("tracking_url") or od.get("shipments"))


def _is_valid_tracking_result(r: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(r, dict):
        return False
    if r.get("status") == "error":
        return False
    return bool(r.get("awb") or r.get("current_location") or r.get("latest_activity"))


def _is_valid_write_result(r: Optional[Dict[str, Any]]) -> bool:
    return bool(r and r.get("success"))


def _is_clean_not_found(r: Optional[Dict[str, Any]]) -> bool:
    """A partner responded cleanly with 'order not found in my system'.

    Distinguishes a legitimate "I don't have this order" answer from a
    timeout / network error / 5xx. Callers can treat all-partners-clean-
    not-found as a meaningful outcome ('no partner has this order'),
    separate from 'partners failed to respond'.
    """
    if not isinstance(r, dict):
        return False
    if r.get("error") or r.get("timeout"):
        return False
    # success=True with found=False explicitly, or success unset with empty
    # orders list — both signal a clean lookup miss.
    if r.get("found") is False and r.get("success") is not False:
        return True
    if r.get("success") is True and not r.get("found", True) and not r.get("orders"):
        return True
    return False


def _summarize_unanswered(
    per_partner: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the empty-winner result, distinguishing 'no partner had it' from
    'partners failed'. When every responding partner reported a clean
    not-found, callers can render 'order not found in any logistics
    partner' to the user instead of a generic error."""
    if not per_partner:
        return {}
    clean_miss = [p for p, r in per_partner.items() if _is_clean_not_found(r)]
    if clean_miss and len(clean_miss) == len(per_partner):
        return {
            "found": False,
            "not_found": True,
            "checked_partners": clean_miss,
            "message": "Order not found in any connected logistics partner",
        }
    # At least one partner errored — keep the empty dict so callers fall back
    # to the legacy error path. per_partner is still returned for diagnostics.
    return {}


# ── adapter resolution ────────────────────────────────────────────────


async def _aget_adapter_for(partner: str, state: Optional[Dict]) -> Any:
    """Return a logistics adapter instance for a canonical partner name."""
    from fashion_bot.core.factory import ServiceFactory

    return await ServiceFactory.aget_logistics_service(state=state, vendor=partner)


# ── core racing helpers ───────────────────────────────────────────────


async def _race_reads(
    partners: List[str],
    fn: Callable[[Any], Awaitable[Dict[str, Any]]],
    is_valid: Callable[[Optional[Dict[str, Any]]], bool],
    state: Optional[Dict],
    op_name: str,
) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Race read calls across `partners`. First valid wins; losers cancelled.

    Returns: (winner_partner, winning_result, per_partner_results)."""
    if not partners:
        return None, {}, {}

    async def _one(name: str) -> Tuple[str, Dict[str, Any]]:
        try:
            adapter = await _aget_adapter_for(name, state)
            result = await asyncio.wait_for(fn(adapter), PER_PARTNER_TIMEOUT_S)
            result.setdefault("_partner", name)
            return name, result
        except asyncio.TimeoutError:
            log_with_trace_id(state, f"⏱️ {name}.{op_name} timed out after {PER_PARTNER_TIMEOUT_S}s", "warning")
            return name, {"success": False, "error": "timeout", "_partner": name}
        except Exception as exc:
            log_with_trace_id(state, f"❌ {name}.{op_name} error: {exc}", "warning")
            return name, {"success": False, "error": str(exc), "_partner": name}

    tasks = [asyncio.create_task(_one(p)) for p in partners]
    per_partner: Dict[str, Dict[str, Any]] = {}
    winner: Optional[str] = None
    winning: Dict[str, Any] = {}

    while tasks:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in done:
            tasks.remove(t)
            name, result = t.result()
            per_partner[name] = result
            if is_valid(result) and not winner:
                winner = name
                winning = result
                # Cancel anything still pending — we have an answer. Await the
                # cancellations so the underlying httpx response handles get a
                # chance to aclose() their connection-pool slot.
                losers = list(tasks)
                tasks = []
                for pending in losers:
                    pending.cancel()
                if losers:
                    await asyncio.gather(*losers, return_exceptions=True)
                break

    if not winner:
        # No partner returned a "valid" result. Distinguish two cases:
        #  (a) every partner cleanly answered "not found" → return a
        #      synthesized not-found result so the caller can render
        #      "no logistics partner has this order".
        #  (b) at least one partner errored / timed out → empty winning
        #      result (legacy shape) and per_partner carries the errors.
        return None, _summarize_unanswered(per_partner), per_partner

    return winner, winning, per_partner


async def _log_orphan(name: str, raw_task: asyncio.Task, op_name: str, state: Optional[Dict]) -> None:
    try:
        result = await raw_task
        log_with_trace_id(
            state,
            f"📌 orphan {name}.{op_name} eventually finished: success={(result or {}).get('success')}",
            "info",
        )
    except Exception as exc:
        log_with_trace_id(state, f"📌 orphan {name}.{op_name} eventually failed: {exc}", "warning")


async def _best_effort_writes(
    partners: List[str],
    fn: Callable[[Any], Awaitable[Dict[str, Any]]],
    state: Optional[Dict],
    op_name: str,
) -> Dict[str, Any]:
    """Run writes across all partners under ONE shared deadline. Don't cancel
    in-flight writes on timeout — let them finish and log their outcome.

    The whole fan-out shares a single ``PER_PARTNER_TIMEOUT_S`` window via
    ``asyncio.wait(..., timeout=)`` rather than awaiting each partner in turn:
    the previous per-partner ``wait_for`` loop opened a fresh window for every
    partner, so N hung partners could block N×timeout of wall-clock. ``asyncio.wait``
    never cancels its tasks, so any partner still running when the deadline
    fires is left in flight (orphan) and its outcome logged later — the same
    "don't half-cancel an inflight POST" guarantee the old ``shield`` gave.

    Returns a dict shaped:
        {
            "success": bool,                # True iff any partner succeeded
            "winning_partners": [name, ...],
            "per_partner": {name: result, ...},
        }
    """
    if not partners:
        return {"success": False, "winning_partners": [], "per_partner": {}}

    async def _one(name: str) -> Dict[str, Any]:
        try:
            adapter = await _aget_adapter_for(name, state)
            return await fn(adapter)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    raw_tasks: Dict[str, asyncio.Task] = {p: asyncio.create_task(_one(p)) for p in partners}

    # Single shared deadline for the entire fan-out (not per partner).
    done, _pending = await asyncio.wait(
        raw_tasks.values(),
        timeout=PER_PARTNER_TIMEOUT_S,
        return_when=asyncio.ALL_COMPLETED,
    )

    per_partner: Dict[str, Dict[str, Any]] = {}
    winners: List[str] = []
    # Iterate in the original partner order for deterministic results.
    for name in partners:
        raw = raw_tasks[name]
        if raw in done:
            try:
                result = raw.result()
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
        else:
            log_with_trace_id(
                state,
                f"⏱️ {name}.{op_name} write timed out after {PER_PARTNER_TIMEOUT_S}s "
                f"(shared fan-out deadline); leaving in flight (orphan)",
                "warning",
            )
            result = {"success": False, "error": "timeout", "_orphan_continues": True}
            # Hold a strong reference so the event loop's task reaper doesn't
            # eat the orphan while it's still running its HTTP write.
            orphan_task = asyncio.create_task(_log_orphan(name, raw, op_name, state))
            _ORPHAN_WRITE_TASKS.add(orphan_task)

        per_partner[name] = result
        if _is_valid_write_result(result):
            winners.append(name)

    return {
        "success": bool(winners),
        "winning_partners": winners,
        "per_partner": per_partner,
    }


# ── public API ────────────────────────────────────────────────────────


class LogisticsRouter:
    """Vendor-neutral entrypoint for cross-partner logistics calls."""

    @staticmethod
    async def aroute_for_order(
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> List[str]:
        """Decide which partner(s) to call for this order. See design §C."""
        from fashion_bot.utils.delivery_partner_utils import aget_partners_for_order

        return await aget_partners_for_order(order_dto, state=state)

    @staticmethod
    async def aget_order_data_first_valid(
        order_id: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """For each candidate partner, call adapter.aget_order_data(order_id).

        Returns (winner_name, winning_result, per_partner_results)."""
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)
        if not partners:
            return None, {}, {}

        async def _call(adapter):
            return await adapter.aget_order_data(order_id, state=state)

        return await _race_reads(
            partners, _call, _is_valid_status_result, state, op_name="aget_order_data",
        )

    @staticmethod
    async def aget_expected_delivery(
        order_id: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> str:
        """Fail-open, vendor-neutral lookup of an order's estimated delivery
        date (ETA).

        Shopify order records carry no EDD ("Shopify doesn't usually have
        EDD"), so the ETA only lives in the courier partner's data. Races the
        order's candidate partners (same path as ``aget_order_data_first_valid``)
        and pulls the canonical ETD via
        ``partner_response_mappings.extract_expected_delivery`` — so it works
        for any integrated partner that populates ``shipments.etd`` /
        ``etd_date`` (Shiprocket; Delhivery when it returns
        ExpectedDeliveryDate). Returns ``""`` on any miss/error/absent-ETA so
        callers can treat it as "not available" without extra guarding.
        """
        try:
            from fashion_bot.core.partner_response_mappings import (
                extract_expected_delivery,
            )

            _winner, logistics_data, _per_partner = (
                await LogisticsRouter.aget_order_data_first_valid(
                    order_id, order_dto or {}, state=state,
                )
            )
            # Preserve historical ETA for delivered orders, but suppress
            # stale (past) ETAs on in-flight orders — those otherwise show
            # up as "will arrive on <past-date>" in AI-generated replies.
            # ``status`` can be absent (callers that pass a routing-only dto)
            # or explicitly None, so coalesce before lowercasing — an
            # AttributeError here would be swallowed by the except below and
            # silently drop a perfectly good future ETA.
            _dto = order_dto or {}
            _status = _dto.get("status") or _dto.get("shipment_status") or ""
            is_delivered = str(_status).strip().lower() == "delivered"
            return extract_expected_delivery(logistics_data, is_delivered=is_delivered)
        except Exception as exc:
            log_with_trace_id(
                state,
                f"⚠️ Expected-delivery lookup failed for {order_id}: {exc}",
                "warning",
            )
            return ""

    @staticmethod
    async def aget_order_details_first_valid(
        order_id: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Dict[str, Any]]]:
        """For each candidate partner, fetch via the *order* adapter
        (returns the wrapper-shape ``{"orders": [...]}`` payload that the
        orchestrator's enrichment pipeline expects)."""
        from fashion_bot.core.factory import ServiceFactory

        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)
        if not partners:
            return None, {}, {}

        async def _one(name: str) -> Tuple[str, Dict[str, Any]]:
            try:
                order_service = await ServiceFactory.aget_order_service(state=state, vendor=name)
                result = await asyncio.wait_for(
                    order_service.aget_order_details(order_id, state=state),
                    PER_PARTNER_TIMEOUT_S,
                )
                return name, (result or {})
            except asyncio.TimeoutError:
                log_with_trace_id(
                    state,
                    f"⏱️ {name}.aget_order_details timed out after {PER_PARTNER_TIMEOUT_S}s",
                    "warning",
                )
                return name, {"orders": [], "_partner": name, "error": "timeout"}
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"❌ {name}.aget_order_details error: {exc}",
                    "warning",
                )
                return name, {"orders": [], "_partner": name, "error": str(exc)}

        tasks = [asyncio.create_task(_one(p)) for p in partners]
        per_partner: Dict[str, Dict[str, Any]] = {}
        winner: Optional[str] = None
        winning: Dict[str, Any] = {}

        while tasks:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                tasks.remove(t)
                name, result = t.result()
                per_partner[name] = result
                if result.get("orders") and not winner:
                    winner = name
                    winning = result
                    losers = list(tasks)
                    tasks = []
                    for pending in losers:
                        pending.cancel()
                    if losers:
                        await asyncio.gather(*losers, return_exceptions=True)
                    break

        if not winner:
            # Same "clean not-found vs error" disambiguation as _race_reads.
            # Treat a result with empty orders and no error/timeout as a
            # clean miss.
            def _clean_miss(r: Dict[str, Any]) -> bool:
                return (
                    isinstance(r, dict)
                    and not r.get("error")
                    and not r.get("orders")
                )

            clean = [p for p, r in per_partner.items() if _clean_miss(r)]
            if clean and len(clean) == len(per_partner):
                return (
                    None,
                    {
                        "orders": [],
                        "not_found": True,
                        "checked_partners": clean,
                        "message": "Order not found in any connected logistics partner",
                    },
                    per_partner,
                )
        return winner, winning, per_partner

    @staticmethod
    async def aget_tracking_first_valid(
        awb: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
        fallback_to_all: bool = False,
    ) -> Tuple[Optional[str], Dict[str, Any], Dict[str, Dict[str, Any]]]:
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)
        if fallback_to_all and partners:
            from fashion_bot.utils.delivery_partner_utils import aget_integrated_partners

            integrated = await aget_integrated_partners(state=state)
            partners = partners + [p for p in integrated if p not in partners]

        async def _call(adapter):
            return await adapter.aget_tracking_details(awb, state=state)

        if fallback_to_all and len(partners) > 1:
            combined_per_partner: Dict[str, Dict[str, Any]] = {}
            for partner in partners:
                winner, winning, per_partner = await _race_reads(
                    [partner], _call, _is_valid_tracking_result, state, op_name="aget_tracking",
                )
                combined_per_partner.update(per_partner)
                if winner:
                    return winner, winning, combined_per_partner
            return None, _summarize_unanswered(combined_per_partner), combined_per_partner

        return await _race_reads(
            partners, _call, _is_valid_tracking_result, state, op_name="aget_tracking",
        )

    @staticmethod
    async def aupdate_address_best_effort(
        order_id: str,
        address_data: Dict[str, Any],
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)

        async def _call(adapter):
            return await adapter.aupdate_shipment_address(order_id, address_data, state=state)

        return await _best_effort_writes(partners, _call, state, op_name="aupdate_shipment_address")

    @staticmethod
    async def aupdate_phone_best_effort(
        order_id: str,
        new_phone: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)

        async def _call(adapter):
            return await adapter.aupdate_shipment_phone(order_id, new_phone, state=state)

        return await _best_effort_writes(partners, _call, state, op_name="aupdate_shipment_phone")

    @staticmethod
    async def aupdate_email_best_effort(
        order_id: str,
        new_email: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)

        async def _call(adapter):
            return await adapter.aupdate_shipment_email(order_id, new_email, state=state)

        return await _best_effort_writes(partners, _call, state, op_name="aupdate_shipment_email")

    @staticmethod
    async def aupdate_name_best_effort(
        order_id: str,
        first_name: str,
        last_name: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Multi-partner best-effort update for the recipient name.

        Mirrors ``aupdate_phone_best_effort`` and ``aupdate_email_best_effort``
        so the generic write paths in ``OrderUpdateOrchestrator`` can stay
        vendor-neutral.
        """
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)

        async def _call(adapter):
            return await adapter.aupdate_shipment_name(
                order_id, first_name, last_name, state=state,
            )

        return await _best_effort_writes(partners, _call, state, op_name="aupdate_shipment_name")

    @staticmethod
    async def acancel_first_success(
        order_id: str,
        order_dto: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Try to cancel on each candidate partner until one succeeds.
        Uses best-effort writes (don't cancel slow tasks)."""
        partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)

        async def _call(adapter):
            return await adapter.acancel_shipment(order_id, state=state)

        return await _best_effort_writes(partners, _call, state, op_name="acancel_shipment")
