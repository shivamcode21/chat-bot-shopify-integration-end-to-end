import asyncio
from typing import Dict, Any, Optional, List, Tuple
import json
import re
from datetime import datetime
from fashion_bot.core.factory import ServiceFactory
from fashion_bot.utils.utils import log_with_trace_id, get_trace_id
from fashion_bot.rollbar_config import report_error

from fashion_bot.tools import create_order_status_summary_dto, create_delivery_timeline_dto
from fashion_bot.utils.delivery_utils import determine_delivery_status_and_message, categorize_items_by_fulfillment
from fashion_bot.utils.order_utils import get_pricing_formatter

from fashion_bot.core.vendor_config import VendorConfigManager, AsyncVendorConfigManager
from fashion_bot.shopify.order_tags import OrderTag


def _unwrap_primary_order_record(order_payload: Any, vendor: Optional[str]) -> Any:
    """Unwrap a partner-specific envelope into the bare order record.

    Each integrated partner declares its envelope key via
    ``PartnerRegistration.order_search_result_key`` (Shiprocket = ``"orders"``,
    Delhivery = ``None``). The orchestrator stays vendor-neutral by reading
    that hint from the registry rather than hard-coding partner names.
    """
    if not isinstance(order_payload, dict) or not vendor:
        return order_payload
    try:
        from fashion_bot.core.logistics_registry import get_partner
        partner_reg = get_partner(vendor)
    except Exception:
        partner_reg = None
    envelope_key = partner_reg.order_search_result_key if partner_reg else None
    if envelope_key and envelope_key in order_payload:
        records = order_payload.get(envelope_key) or []
        return records[0] if records else None
    return order_payload


def _order_record_to_mapping(order_record: Any) -> Dict[str, Any]:
    if isinstance(order_record, dict):
        return order_record

    order_data = getattr(order_record, "order_data", None)
    if isinstance(order_data, dict):
        merged = dict(order_data)
        merged.setdefault("order_id", getattr(order_record, "order_id", ""))
        merged.setdefault("channel_order_id", getattr(order_record, "channel_order_id", ""))
        merged.setdefault("partner_status", getattr(order_record, "status", ""))
        merged.setdefault("shipment_status", getattr(order_record, "shipment_status", ""))
        return merged

    return {}


class OrderStatusOrchestrator:
    """
    Generic orchestrator for fetching order status.
    Shopify-first strategy:
      1. Always fetch from Shopify (primary order source) first.
      2. If order is NEW (unfulfilled, not cancelled, not voided) → return Shopify data only.
      3. If order is dispatched/fulfilled → check tracking_company from fulfillments:
         a. If delivery partner is integrated (e.g., Shiprocket) → fetch enriched data from that provider.
         b. If delivery partner is NOT integrated → return Shopify data only.
    """
    
    @staticmethod
    def _is_order_new(order_dto: Dict[str, Any]) -> bool:
        """
        Determine if a Shopify order is still "NEW" (not yet dispatched).
        NEW = fulfillment_status is null/unfulfilled AND not cancelled AND financial_status is not voided.
        """
        fulfillment_status = (order_dto.get("fulfillment_status") or "unfulfilled").lower()
        cancelled_at = order_dto.get("cancelled_at")
        financial_status = (order_dto.get("financial_status") or "").lower()
        
        is_unfulfilled = fulfillment_status in ("unfulfilled", "null", "", "pending")
        is_not_cancelled = not cancelled_at
        is_not_voided = financial_status != "voided"
        
        return is_unfulfilled and is_not_cancelled and is_not_voided
    
    @staticmethod
    def _is_order_cancelled_or_voided(order_dto: Dict[str, Any]) -> bool:
        """Check if order is cancelled or voided — these are terminal states."""
        cancelled_at = order_dto.get("cancelled_at")
        financial_status = (order_dto.get("financial_status") or "").lower()
        return bool(cancelled_at) or financial_status == "voided"
    
    @staticmethod
    async def aget_order_status(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"Orchestrator: Async getting order status for {order_id} (Shopify-first)")

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)

            shopify_order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            )
            shopify_order_dto = None

            if shopify_order_data:
                log_with_trace_id(state, f"Found order {order_id} in {primary_vendor}")
                shopify_order_dto = processor.process_order(shopify_order_data, state=state, source=primary_vendor)
            else:
                exchange_suffix = VendorConfigManager.get_exchange_suffix(state=state)
                if exchange_suffix:
                    exchange_order_id = f"{order_id}{exchange_suffix}"
                    log_with_trace_id(state, f"Attempting exchange lookup: {exchange_order_id}")
                    exchange_data = _unwrap_primary_order_record(
                        await order_service.aget_order_details(exchange_order_id, state=state),
                        primary_vendor,
                    )
                    if exchange_data:
                        log_with_trace_id(state, f"Found exchange order in {primary_vendor}")
                        shopify_order_dto = processor.process_order(
                            exchange_data,
                            state=state,
                            source=f"{primary_vendor}_exchange",
                        )

            if not shopify_order_dto:
                log_with_trace_id(state, f"Order {order_id} not found in {primary_vendor}", "error")
                return {"total_orders_found": 0, "orders": []}

            if OrderStatusOrchestrator._is_order_new(shopify_order_dto):
                log_with_trace_id(state, f"Order {order_id} is NEW/unfulfilled → returning Shopify data only")
                shopify_order_dto["_routing"] = "shopify_only_new"
                return {"total_orders_found": 1, "orders": [shopify_order_dto]}

            if OrderStatusOrchestrator._is_order_cancelled_or_voided(shopify_order_dto):
                log_with_trace_id(state, f"Order {order_id} is cancelled/voided → returning Shopify data only")
                shopify_order_dto["_routing"] = "shopify_only_terminal"
                return {"total_orders_found": 1, "orders": [shopify_order_dto]}

            # Route through LogisticsRouter so:
            #   - if tracking_company resolves to a connected integrated
            #     partner → single call to that partner (fast path);
            #   - if tracking_company is empty/unknown → race ALL connected
            #     integrated partners with per-partner deadlines, first
            #     valid wins;
            #   - if no integrated partner is connected at all → router
            #     returns no winner and we fall back to Shopify data.
            #
            # The router already encapsulates the partner-selection logic
            # (see ``utils.delivery_partner_utils.aget_partners_for_order``)
            # so this orchestrator stays vendor-neutral.
            tracking_company = shopify_order_dto.get("tracking_company", "")

            try:
                from fashion_bot.core.logistics_router import LogisticsRouter

                candidate_partners = await LogisticsRouter.aroute_for_order(
                    shopify_order_dto, state=state,
                )
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"LogisticsRouter.aroute_for_order failed for {order_id}: {exc}",
                    "warning",
                )
                candidate_partners = []

            if not candidate_partners:
                log_with_trace_id(
                    state,
                    f"Order {order_id} has no integrated partner candidates "
                    f"(tracking_company={tracking_company!r}) → returning Shopify data only",
                )
                shopify_order_dto["_routing"] = "shopify_only_non_integrated"
                return {"total_orders_found": 1, "orders": [shopify_order_dto]}

            log_with_trace_id(
                state,
                f"Order {order_id} candidate partners={candidate_partners} "
                f"(tracking_company={tracking_company!r}) → racing via LogisticsRouter",
            )
            try:
                winner, logistics_result, per_partner = (
                    await LogisticsRouter.aget_order_details_first_valid(
                        order_id, shopify_order_dto, state=state,
                    )
                )
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"LogisticsRouter.aget_order_details_first_valid failed for "
                    f"{order_id}: {exc}, returning Shopify data",
                    "warning",
                )
                shopify_order_dto["_routing"] = "shopify_fallback_logistics_error"
                return {"total_orders_found": 1, "orders": [shopify_order_dto]}

            if winner:
                raw_orders = (logistics_result or {}).get("orders", []) or []
                if raw_orders:
                    log_with_trace_id(
                        state,
                        f"Found {len(raw_orders)} orders in {winner} "
                        f"(checked={list(per_partner.keys())})",
                    )
                    logistics_processor = ServiceFactory.get_order_processor(winner, state=state)
                    processed_orders = logistics_processor.process_orders(raw_orders, state=state)
                    enrichers = ServiceFactory.get_enrichment_pipeline(state=state)
                    if enrichers:
                        log_with_trace_id(state, f"Running {len(enrichers)} enrichers...")
                        for enricher in enrichers:
                            processed_orders = await enricher.aenrich(processed_orders, state)
                    for order in processed_orders:
                        order["_routing"] = "logistics_integrated"
                        order["_partner_winner"] = winner
                        order["_checked_partners"] = list(per_partner.keys())
                        # Preserve the Shopify-reported carrier when present;
                        # otherwise stamp the winning partner so downstream
                        # render code has something to display.
                        order["tracking_company"] = tracking_company or winner.title()
                    return {"total_orders_found": len(processed_orders), "orders": processed_orders}

            # No winner: figure out whether every partner cleanly said
            # "not in my system" (logistics_miss), they all errored
            # (logistics_error), or there was nothing to race.
            checked = list(per_partner.keys()) if per_partner else []
            if logistics_result and logistics_result.get("not_found"):
                log_with_trace_id(
                    state,
                    f"Order {order_id} not found in any connected partner "
                    f"({checked}), returning Shopify data",
                )
                shopify_order_dto["_routing"] = "shopify_fallback_logistics_miss"
            elif checked:
                log_with_trace_id(
                    state,
                    f"All partners failed for {order_id} ({checked}), "
                    f"returning Shopify data",
                    "warning",
                )
                shopify_order_dto["_routing"] = "shopify_fallback_logistics_error"
            else:
                shopify_order_dto["_routing"] = "shopify_only_non_integrated"
            shopify_order_dto["_checked_partners"] = checked
            return {"total_orders_found": 1, "orders": [shopify_order_dto]}
        except Exception as e:
            log_with_trace_id(state, f"Orchestrator failed: {e}", "error")
            return {"total_orders_found": 0, "orders": []}
    
    @staticmethod
    async def aget_order_status_summary(
        order_id: str,
        state: Optional[Dict] = None,
        cached_base_result: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        if cached_base_result is not None:
            base_result = cached_base_result
        else:
            base_result = await OrderStatusOrchestrator.aget_order_status(order_id, state)

        if base_result.get("total_orders_found", 0) == 0:
            return base_result

        summary_orders = []
        for order in base_result.get("orders", []):
            fulfilled_items, pending_items = categorize_items_by_fulfillment(order)
            summary_dto = create_order_status_summary_dto(
                order_id=order.get("order_id"),
                channel_order_id=order.get("channel_order_id"),
                status=order.get("status"),
                partner_status=order.get("partner_status"),
                shipment_status=order.get("shipment_status"),
                customer_name=order.get("customer"),
                fulfillment_status=order.get("fulfillment_status"),
                fulfilled_items=fulfilled_items,
                pending_items=pending_items,
                courier=order.get("courier"),
                tracking_url=order.get("tracking_url"),
                awb=order.get("awb"),
                etd_date=None,
                delivery_date=order.get("delivery_date"),
                out_for_delivery_date=order.get("out_for_delivery_date"),
                cancelled_at=order.get("cancelled_at"),
                products=order.get("products", []),
                created_at=order.get("created_at"),
                updated_at=order.get("updated_at"),
                source=order.get("source"),
            )
            summary_orders.append(summary_dto)

        return {
            "total_orders_found": len(summary_orders),
            "orders": summary_orders,
        }
    
    @staticmethod
    async def aget_delivery_timeline(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get delivery timeline with ETD information.
        Uses same pipeline as aget_order_status but with different output format.
        """
        base_result = await OrderStatusOrchestrator.aget_order_status(order_id, state)

        if base_result.get("total_orders_found", 0) == 0:
            return base_result

        timeline_orders = []
        for order in base_result.get("orders", []):
            timeline_info = determine_delivery_status_and_message(order)

            timeline_dto = create_delivery_timeline_dto(
                order_id=order.get("order_id"),
                channel_order_id=order.get("channel_order_id"),
                status=timeline_info["timeline_status"],
                partner_status=order.get("partner_status"),
                shipment_status=order.get("shipment_status"),
                customer_name=order.get("customer"),
                awb=order.get("awb"),
                etd_date=order.get("delivery_date"),
                delivery_message=timeline_info["message"],
                formatted_etd=timeline_info["formatted_etd"],
            )
            timeline_orders.append(timeline_dto)

        return {
            "total_orders_found": len(timeline_orders),
            "orders": timeline_orders,
        }

    @staticmethod
    async def aget_order_details_with_pricing(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"Orchestrator: Async getting order pricing for {order_id}")

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)

            order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            )
            if order_data:
                order_info = processor.process_order(order_data, state=state, source=f"{primary_vendor}_pricing")
                formatter = get_pricing_formatter(primary_vendor)
                return formatter(order_info)

            exchange_suffix = VendorConfigManager.get_exchange_suffix(state=state)
            if exchange_suffix:
                exchange_order_id = f"{order_id}{exchange_suffix}"
                exchange_data = _unwrap_primary_order_record(
                    await order_service.aget_order_details(exchange_order_id, state=state),
                    primary_vendor,
                )
                if exchange_data:
                    order_info = processor.process_order(
                        exchange_data,
                        state=state,
                        source=f"{primary_vendor}_exchange_pricing",
                    )
                    formatter = get_pricing_formatter(primary_vendor)
                    return formatter(order_info)

            log_with_trace_id(state, f"Order {order_id} not found for pricing", "error")
            return {}
        except Exception as e:
            log_with_trace_id(state, f"Async pricing orchestrator failed: {e}", "error")
            return {}

class CustomerOrderOrchestrator:
    """
    Generic orchestrator for fetching customer orders.
    Uses a topology-driven approach where the execution pipeline is defined by configuration.
    """
    @staticmethod
    async def aget_customer_orders(phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"Orchestrator: Async getting customer orders for phone: {phone}")

        from fashion_bot.utils.phone_number_utils import is_real_phone_number
        if not is_real_phone_number(phone):
            log_with_trace_id(
                state,
                f"Rejecting order lookup: '{phone}' is not a valid 10-digit phone number",
                "warning",
            )
            return {"customer_phone": phone, "total_orders": 0, "orders": [], "invalid_phone": True}

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            log_with_trace_id(state, f"Primary vendor: {primary_vendor}")

            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            raw_orders = await order_service.aget_orders_by_customer_phone(phone, limit=50, state=state)

            if not raw_orders:
                log_with_trace_id(state, f"No orders found via {primary_vendor}", "warning")
                return {"customer_phone": phone, "total_orders": 0, "orders": []}

            processor = ServiceFactory.get_order_processor(primary_vendor)
            processed_orders = processor.process_orders(raw_orders, state=state)
            processed_orders.sort(key=lambda x: x.get("created_at", ""), reverse=True)

            enrichers = ServiceFactory.get_enrichment_pipeline(state=state)
            if enrichers:
                log_with_trace_id(state, f"Running {len(enrichers)} enrichers for {primary_vendor}...")
                for enricher in enrichers:
                    processed_orders = await enricher.aenrich(processed_orders, state)
            else:
                log_with_trace_id(state, f"✅ No enrichers enabled - using {primary_vendor} data directly (optimized)")

            return {
                "customer_phone": phone,
                "total_orders": len(processed_orders),
                "orders": processed_orders,
            }
        except Exception as e:
            log_with_trace_id(state, f"Async customer order orchestrator failed: {e}", "error")
            raise e

class LogisticsOrchestrator:
    """
    Orchestrator for logistics operations (tracking, shipping).
    """
    @staticmethod
    async def aget_shipment_location(awb: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"Orchestrator: Async getting shipment location for AWB: {awb}")
        try:
            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
            return await logistics_service.aget_tracking_details(awb, state=state)
        except Exception as e:
            log_with_trace_id(state, f"Async logistics orchestrator failed: {e}", "error")
            raise e

    @staticmethod
    async def get_delivery_estimate(
        pickup_pincode: str, 
        destination_pincode: str, 
        weight: float = 0.5, 
        cod: bool = False, 
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Get estimated delivery time between two postal codes.
        
        Args:
            pickup_pincode: Warehouse or origin postal code
            destination_pincode: Customer / delivery postal code
            weight: Parcel weight in kilograms (default 0.5 kg)
            cod: Whether Cash-on-Delivery is required (default False)
            state: Optional state dictionary
            
        Returns:
            Dictionary with best courier, estimated delivery, and all options
        """
        log_with_trace_id(state, f"Orchestrator: Getting delivery estimate from {pickup_pincode} to {destination_pincode}")
        try:
            # Get logistics service from factory (config-driven)
            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
            
            # Delegate to async logistics adapter
            result = await logistics_service.aget_delivery_estimate(
                pickup_pincode=pickup_pincode,
                destination_pincode=destination_pincode,
                weight=weight,
                cod=cod,
                state=state
            )
            
            return result
        except Exception as e:
            log_with_trace_id(state, f"Logistics orchestrator failed: {e}", "error")
            raise e

class CancellationOrchestrator:
    """
    Orchestrates cancellation across multiple systems using Shopify-first strategy.
    
    Routing logic:
    - NEW order → cancel in Shopify + integrated delivery partners + add note + escalation for non-integrated.
    - Dispatched + integrated partner → cancel in Shopify + cancel shipment in logistics (existing flow).
    - Dispatched + non-integrated partner → add note only (NO Shopify cancel) + escalation.
    - Cancelled/Voided → already terminal, return error.
    """
    
    @staticmethod
    def _get_client_id(state: Optional[Dict] = None) -> Optional[str]:
        """Extract client_id from state."""
        if not state:
            return None
        client_id = state.get("client_id")
        if not client_id:
            context = state.get("context", {})
            client_id = context.get("client_id")
        return client_id

    @staticmethod
    async def acancel_order(order_id: str, reason: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"CancellationOrchestrator: Async cancellation for order {order_id} (Shopify-first)")

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)
            if not order_service:
                return {"success": False, "error": "Order service not configured", "order_id": order_id}

            shopify_order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            )
            if not shopify_order_data:
                return {"success": False, "error": f"Order {order_id} not found", "order_id": order_id}

            order_dto = processor.process_order(shopify_order_data, state=state, source=primary_vendor)
            if OrderStatusOrchestrator._is_order_cancelled_or_voided(order_dto):
                return {
                    "success": False,
                    "error": f"Order {order_id} is already cancelled or voided",
                    "order_id": order_id,
                    "status": order_dto.get("status"),
                }

            results = {
                "order_id": order_id,
                "shopify_cancel": {"success": False},
                "logistics_cancel": {"success": False},
                "note_added": False,
                "escalation": None,
                "routing": "",
            }

            async def _refund_if_prepaid(cancel_success: bool) -> None:
                """Issue a refund via the order service if the order was paid/partially_paid."""
                if not cancel_success:
                    return
                fin = (order_dto.get("financial_status") or "").lower()
                if fin not in ("paid", "partially_paid"):
                    return
                try:
                    refund_result = await order_service.arefund_order(order_id, state=state)
                    results["refund"] = refund_result
                    if refund_result.get("success"):
                        log_with_trace_id(state, f"✅ Refund processed for prepaid order {order_id}")
                    else:
                        log_with_trace_id(state, f"⚠️ Refund failed for order {order_id}: {refund_result.get('error')}", "warning")
                except Exception as exc:
                    log_with_trace_id(state, f"⚠️ Refund attempt failed for order {order_id}: {exc}", "warning")
                    results["refund"] = {"success": False, "error": str(exc)}

            if OrderStatusOrchestrator._is_order_new(order_dto):
                results["routing"] = "new_order"
                cancel_result = await order_service.acancel_order(order_id, reason, state=state)
                results["shopify_cancel"] = cancel_result

                integrated_providers = await AsyncVendorConfigManager.aget_integrated_providers_list(state=state)
                if integrated_providers:
                    # NEW orders may live on either connected partner's system.
                    # Route via LogisticsRouter so we fan out across every
                    # integrated partner (with per-partner timeouts) instead
                    # of picking connected[0] blindly.
                    try:
                        from fashion_bot.core.logistics_router import LogisticsRouter
                        routed = await LogisticsRouter.acancel_first_success(
                            order_id, order_dto or {}, state=state,
                        )
                        if routed.get("success"):
                            winners = routed.get("winning_partners") or []
                            results["logistics_cancel"] = {
                                "success": True,
                                "winning_partners": winners,
                                "per_partner": routed.get("per_partner", {}),
                            }
                        else:
                            results["logistics_cancel"] = {
                                "success": False,
                                "error": "All partners failed to cancel",
                                "per_partner": routed.get("per_partner", {}),
                            }
                    except Exception as exc:
                        results["logistics_cancel"] = {"success": False, "error": str(exc)}

                try:
                    note = f"[Bloomerce] Cancellation requested by customer. Reason: {reason}"
                    await order_service.aadd_order_note(order_id, note, state=state)
                    results["note_added"] = True
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add cancellation note: {exc}", "warning")

                results["success"] = cancel_result.get("success", False)
                if results["success"]:
                    await _refund_if_prepaid(True)
                    results["message"] = f"Order {order_id} cancelled successfully in Shopify"
                    if results["logistics_cancel"].get("success"):
                        results["message"] += " and logistics system"
                else:
                    results["error"] = cancel_result.get("error", "Cancellation failed")
                return results

            tracking_company = order_dto.get("tracking_company", "")
            tracking_url = order_dto.get("tracking_url", "")
            # Resolve the order's actual carrier the SAME way the LogisticsRouter
            # will (URL-first: tracking_url is the authoritative platform signal —
            # Shiprocket can dispatch via Delhivery, writing
            # tracking_company='Delhivery' while tracking_url contains
            # 'shiprocket'). Resolving here with the router's own helper means the
            # integrated/non-integrated decision below and the partner we actually
            # cancel on (LogisticsRouter.acancel_first_success) cannot diverge —
            # so we never issue a Shopify cancel + refund on the "integrated" path
            # only for the router to then find no matching partner.
            from fashion_bot.core.vendor_config import resolve_partner_from_url
            from fashion_bot.utils.delivery_partner_utils import (
                aresolve_effective_partner_for_order,
            )
            effective_partner, is_integrated = await aresolve_effective_partner_for_order(
                order_dto, state=state,
            )
            if not is_integrated:
                # Legacy fallback: honour the VendorConfig single-partner default
                # for tenants that pre-date the delivery_partner_integrations
                # table (no DB rows), so Shiprocket-only tenants keep their
                # existing auto-cancel behaviour.
                effective_partner = resolve_partner_from_url(tracking_url) or tracking_company
                is_integrated = await AsyncVendorConfigManager.ais_integrated_delivery_partner(
                    effective_partner, state=state,
                )
            if is_integrated:
                results["routing"] = "dispatched_integrated"
                cancel_result = await order_service.acancel_order(order_id, reason, state=state)
                results["shopify_cancel"] = cancel_result
                # Dispatched-integrated: tracking_company is known, so the
                # router resolves to a single partner (the one that actually
                # shipped this order). Going via the router guarantees we hit
                # that partner — calling aget_logistics_service() with no
                # vendor= can silently route to connected[0] on multi-partner
                # tenants where Shiprocket is first connected but the order
                # shipped on Delhivery.
                try:
                    from fashion_bot.core.logistics_router import LogisticsRouter
                    routed = await LogisticsRouter.acancel_first_success(
                        order_id, order_dto or {}, state=state,
                    )
                    if routed.get("success"):
                        winners = routed.get("winning_partners") or []
                        results["logistics_cancel"] = {
                            "success": True,
                            "winning_partners": winners,
                            "per_partner": routed.get("per_partner", {}),
                        }
                    else:
                        results["logistics_cancel"] = {
                            "success": False,
                            "error": "Logistics cancel failed",
                            "per_partner": routed.get("per_partner", {}),
                        }
                except Exception as exc:
                    results["logistics_cancel"] = {"success": False, "error": str(exc)}
                try:
                    note = f"[Bloomerce] Cancellation requested for dispatched order. Partner: {tracking_company}. Reason: {reason}"
                    await order_service.aadd_order_note(order_id, note, state=state)
                    results["note_added"] = True
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add cancellation note: {exc}", "warning")

                cancel_success = cancel_result.get("success", False) or results["logistics_cancel"].get("success", False)
                results["success"] = cancel_success

                # A Shopify-only cancel leaves the parcel moving at the courier.
                # The non-integrated branch below already treats an un-cancellable
                # shipment as manual work; an integrated partner that refused or
                # errored is the same situation and must not pass silently -- the
                # customer has been told the order is cancelled and refunded while
                # the shipment is still on its way.
                if cancel_result.get("success") and not results["logistics_cancel"].get("success"):
                    try:
                        results["escalation"] = await EscalationOrchestrator.aescalate_to_agent(
                            category="Order Cancellation - Partner Cancel Not Confirmed",
                            reason=(
                                f"Order {order_id} cancelled in Shopify but "
                                f"{tracking_company} did not confirm the shipment cancel"
                            ),
                            details=(
                                f"logistics_cancel={results['logistics_cancel']}. "
                                "Recall the shipment with the courier."
                            ),
                            state=state,
                            order_id=order_id,
                        )
                    except Exception as exc:
                        # The customer still gets their Shopify cancel if the
                        # escalation itself fails.
                        log_with_trace_id(
                            state,
                            f"Cancel escalation failed for {order_id}: {exc}",
                            "error",
                        )
                        results["escalation"] = {"success": False, "error": str(exc)}

                # Escalation first, refund second: raising before the money
                # leaves gives ops a window to recall the shipment.
                if cancel_success:
                    await _refund_if_prepaid(True)
                results["message"] = f"Cancellation processed for order {order_id} (Shopify + {tracking_company})"
                return results

            results["routing"] = "dispatched_non_integrated"
            from fashion_bot.utils.order_utils import aadd_escalation_order_note

            results["note_added"] = await aadd_escalation_order_note(
                order_service,
                order_id,
                (
                    f"[Bloomerce] Customer requested cancellation. Delivery partner: {tracking_company} "
                    f"(non-integrated). Reason: {reason}. Requires manual intervention."
                ),
                state=state,
            )

            try:
                escalation_result = await EscalationOrchestrator.aescalate_to_agent(
                    category="Order Cancellation - Non-Integrated Partner",
                    reason=f"Customer requested cancellation for order {order_id} dispatched via {tracking_company}",
                    details=(
                        f"Order {order_id} is dispatched via non-integrated delivery partner '{tracking_company}'. "
                        f"Cannot cancel automatically. Reason: {reason}. "
                        f"{'Note added to Shopify order. ' if results['note_added'] else ''}"
                        "Manual cancellation with delivery partner required."
                    ),
                    state=state,
                    order_id=order_id,
                )
                results["escalation"] = escalation_result
            except Exception as exc:
                results["escalation"] = {"success": False, "error": str(exc)}

            results["success"] = True
            results["message"] = (
                f"Order {order_id} is dispatched via {tracking_company} (non-integrated partner). "
                + (
                    "A note has been added to the order and the team has been notified for manual cancellation."
                    if results["note_added"]
                    else "The team has been notified for manual cancellation."
                )
            )
            results["requires_manual_action"] = True
            return results
        except Exception as exc:
            log_with_trace_id(state, f"❌ Cancellation failed: {str(exc)}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def acancel_shipment(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"CancellationOrchestrator: Async cancelling shipment for order {order_id}")
        try:
            # Vendor-neutral routing: use LogisticsRouter when there's an order
            # context so we can race / pick the right partner. If the caller
            # didn't pass an order_dto via state, fall back to the auto-resolving
            # factory call (single-partner case).
            from fashion_bot.core.logistics_router import LogisticsRouter

            order_dto = (state or {}).get("order_dto") or {}
            partners = await LogisticsRouter.aroute_for_order(order_dto, state=state)
            if partners:
                # Use the router for any partner count (>=1) so single-partner
                # Delhivery orders also get tracking_company-aware routing
                # instead of falling through to aget_logistics_service() with
                # no vendor= hint (which picks connected[0]).
                routed = await LogisticsRouter.acancel_first_success(order_id, order_dto, state=state)
                if routed.get("success"):
                    winners = routed.get("winning_partners") or []
                    return {
                        "success": True,
                        "order_id": order_id,
                        "partner_name": winners[0] if winners else "unknown",
                        "winning_partners": winners,
                        "per_partner": routed.get("per_partner", {}),
                        "message": "Shipment cancelled in logistics partner(s)",
                    }
                # All integrated partners failed: fall through to the
                # single-partner path so the legacy error response shape is
                # preserved. Pass the first routed partner so we hit the
                # right adapter rather than whatever connected[0] happens to be.
                logistics_service = await ServiceFactory.aget_logistics_service(
                    state=state, vendor=partners[0],
                )
                return await logistics_service.acancel_shipment(order_id, state=state)
            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
            return await logistics_service.acancel_shipment(order_id, state=state)
        except Exception as exc:
            log_with_trace_id(state, f"❌ Shipment cancellation failed: {str(exc)}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}


class ReturnExchangeOrchestrator:
    """
    Orchestrator for return/exchange operations.
    Uses CustomerOrderOrchestrator for fetching orders and applies business logic.
    """

    @staticmethod
    async def aget_return_status(
        *,
        client_id: Optional[str],
        order_number: str,
        request_type: Optional[str] = None,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_return_status(
            client_id=client_id,
            order_number=order_number,
            request_type=request_type,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def alist_return_requests(
        *,
        client_id: Optional[str],
        order_number: str,
        request_type: Optional[str] = None,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.alist_return_requests(
            client_id=client_id,
            order_number=order_number,
            request_type=request_type,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def aget_return_request_by_id(
        *,
        client_id: Optional[str],
        request_id: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_return_request_by_id(
            client_id=client_id,
            request_id=request_id,
            state=state,
            partner=partner,
        )

    @staticmethod
    async def aget_return_or_exchange_portal(
        *,
        client_id: Optional[str],
        order_number: str,
        customer_email: Optional[str] = None,
        request_type: Optional[str] = None,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        selected_line_items: Optional[List[Dict[str, Any]]] = None,
        return_reason: Optional[str] = None,
        proof_provided: Optional[bool] = None,
        tag_intact_confirmed: Optional[bool] = None,
        desired_resolution: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_return_or_exchange_portal(
            client_id=client_id,
            order_number=order_number,
            customer_email=customer_email,
            request_type=request_type,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            selected_line_items=selected_line_items,
            return_reason=return_reason,
            proof_provided=proof_provided,
            tag_intact_confirmed=tag_intact_confirmed,
            desired_resolution=desired_resolution,
        )

    @staticmethod
    async def aget_return_pickup_status(
        *,
        client_id: Optional[str],
        order_number: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_return_pickup_status(
            client_id=client_id,
            order_number=order_number,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def aensure_exchange_order(
        *,
        client_id: Optional[str],
        order_number: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        force: bool = False,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aensure_exchange_order(
            client_id=client_id,
            order_number=order_number,
            state=state,
            partner=partner,
            force=force,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def arequest_exchange_size_change(
        *,
        client_id: Optional[str],
        order_number: str,
        desired_size: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.arequest_exchange_size_change(
            client_id=client_id,
            order_number=order_number,
            desired_size=desired_size,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def aget_exchange_delivery_status(
        *,
        client_id: Optional[str],
        order_number: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_exchange_delivery_status(
            client_id=client_id,
            order_number=order_number,
            state=state,
            partner=partner,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def aget_return_exchange_instructions(
        *,
        client_id: Optional[str],
        request_type: Optional[str] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_return_exchange_instructions(
            client_id=client_id,
            request_type=request_type,
            state=state,
        )

    @staticmethod
    async def aget_refund_status(
        *,
        client_id: Optional[str],
        order_number: str,
        state: Optional[Dict] = None,
        partner: Optional[str] = None,
        request_type: Optional[str] = None,
        customer_phone: Optional[str] = None,
        customer_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.return_partners.orchestrator import ReturnPartnerOrchestrator

        return await ReturnPartnerOrchestrator.aget_refund_status(
            client_id=client_id,
            order_number=order_number,
            state=state,
            partner=partner,
            request_type=request_type,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

    @staticmethod
    async def _aget_return_grace_days(client_id: Optional[str], state: Optional[Dict] = None) -> int:
        """
        Fetch the return/exchange grace period (in days) from client config.

        The 'Grace days for product return validity' config value is the single
        source of truth. There is intentionally NO hard-coded fallback: a missing
        or invalid value surfaces as an error so a misconfigured client is caught,
        rather than silently quoting a wrong window (e.g. defaulting to 7 days when
        the client's policy is 10).

        Raises:
            ValueError: if the config is missing or not a valid integer.
        """
        from fashion_bot.config_manager import aget_config

        raw = await aget_config('Grace days for product return validity', client_id=client_id)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            raise ValueError(
                "Missing 'Grace days for product return validity' config for client "
                f"{client_id or '<unknown>'}"
            )
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Invalid 'Grace days for product return validity' config value "
                f"{raw!r} for client {client_id or '<unknown>'}"
            ) from exc

    @staticmethod
    async def aget_customers_delivered_orders(
        phone: str,
        state: Optional[Dict] = None,
        limit: int = 3,
        cached_orders: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"ReturnExchangeOrchestrator: Async getting delivered orders for phone: {phone}")

        from fashion_bot.utils.phone_number_utils import is_real_phone_number
        if not is_real_phone_number(phone):
            log_with_trace_id(
                state,
                f"Rejecting return/exchange lookup: '{phone}' is not a valid 10-digit phone number",
                "warning",
            )
            return {
                "success": False,
                "count": 0,
                "delivered_orders": [],
                "formatted_orders": [],
                "invalid_phone": True,
                "response": (
                    "INVALID_PHONE: That is not a complete 10-digit mobile number. "
                    "Ask the customer to re-enter their 10-digit phone number, or provide their Order ID."
                ),
            }

        try:
            # See UtilityOrchestrator.aget_recent_orders_all_statuses: a caller
            # that already fetched this customer's orders (the order-access
            # verification gate) hands them over so the turn issues one
            # phone->orders lookup instead of two.
            if cached_orders is None:
                vendor = ServiceFactory.get_primary_vendor(state)
                order_service = await ServiceFactory.aget_order_service(state=state, vendor=vendor)
                orders = await order_service.aget_orders_by_customer_phone(phone, limit=10, state=state)
            else:
                orders = cached_orders

            if not orders:
                return {
                    "success": False,
                    "count": 0,
                    "delivered_orders": [],
                    "formatted_orders": [],
                    "response": "NO_ORDERS: No orders found for this phone number. Please ask customer for order ID.",
                }

            delivered_orders = order_service.filter_delivered_orders(orders)
            log_with_trace_id(state, f"Found {len(delivered_orders)} delivered orders out of {len(orders)} total orders")

            if not delivered_orders:
                return {
                    "success": True,
                    "count": 0,
                    "delivered_orders": [],
                    "formatted_orders": [],
                    "response": "NO_DELIVERED_ORDERS: No delivered orders found. Only delivered orders are eligible for return/exchange. The customer may still have orders in progress — call get_recent_orders with include_all_statuses=True to see them before asking anything. Never guess an order ID from a number in the customer's message.",
                }

            formatted_orders = order_service.format_delivered_orders_for_display(delivered_orders[:limit])
            response_data = order_service.build_delivered_orders_response(delivered_orders[:limit], phone)

            return {
                "success": True,
                "count": len(delivered_orders[:limit]),
                "delivered_orders": delivered_orders[:limit],
                "formatted_orders": formatted_orders,
                "response": response_data.get("message", ""),
            }
        except Exception as e:
            log_with_trace_id(state, f"Async ReturnExchangeOrchestrator failed: {e}", "error")
            return {
                "success": False,
                "count": 0,
                "delivered_orders": [],
                "formatted_orders": [],
                "response": f"ERROR: Failed to fetch orders. Error: {str(e)}. Ask customer for order ID manually.",
                "error": str(e),
            }

    @staticmethod
    async def check_delivery_eligibility(order_id: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Check if an order is eligible for delivery-related actions (return, exchange).
        Checks order status and delivery date against grace period.
        Uses ServiceFactory to get the appropriate logistics service.
        
        Args:
            order_id: Order ID to check
            state: Current state dictionary
            
        Returns:
            Dictionary with eligibility status and details
        """
        from datetime import datetime
        import pytz

        log_with_trace_id(state, f"ReturnExchangeOrchestrator: Checking delivery eligibility for order: {order_id}")
        
        try:
            # Get client_id from state
            client_id = None
            if state:
                client_id = state.get("client_id") or state.get("context", {}).get("client_id")
            
            # Get logistics service from factory
            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
            
            if not logistics_service:
                return {
                    "success": False,
                    "eligible": False,
                    "message": "Logistics service not configured"
                }
            
            # Get order data from logistics system
            order_result = logistics_service.get_order_data(order_id, state=state)
            
            if not order_result.get("success") or not order_result.get("found"):
                return {
                    "success": False,
                    "eligible": False,
                    "message": f"Order {order_id} not found"
                }
            
            order_status = order_result.get("status", "")
            order_data = order_result.get("order_data", {})
            
            # Check if order is delivered
            if order_status != "DELIVERED":
                return {
                    "success": True,
                    "eligible": False,
                    "message": f"Order is not delivered yet. Current status: {order_status}",
                    "order_status": order_status
                }
            
            # Get delivery date
            delivered_on = order_result.get("delivered_on")
            
            if not delivered_on:
                return {
                    "success": True,
                    "eligible": True,  # Allow if we can't determine
                    "message": "Delivery date not available, but order is marked delivered",
                    "order_status": order_status
                }
            
            # Calculate days since delivery
            ist_tz = pytz.timezone('Asia/Kolkata')
            now = datetime.now(ist_tz)
            
            # Parse delivery date
            if isinstance(delivered_on, str):
                try:
                    delivery_date = datetime.fromisoformat(delivered_on.replace('Z', '+00:00'))
                    delivery_date = delivery_date.astimezone(ist_tz)
                except:
                    delivery_date = datetime.strptime(delivered_on[:10], '%Y-%m-%d')
                    delivery_date = ist_tz.localize(delivery_date)
            else:
                delivery_date = delivered_on
            
            days_since_delivery = (now - delivery_date).days
            
            # Get grace period from config (single source of truth — no hard-coded fallback)
            grace_days = await ReturnExchangeOrchestrator._aget_return_grace_days(client_id, state)
            is_eligible = days_since_delivery <= grace_days
            
            log_with_trace_id(state, f"✅ Eligibility check: {days_since_delivery} days since delivery, grace period: {grace_days} days, eligible: {is_eligible}")
            
            return {
                "success": True,
                "eligible": is_eligible,
                "days_since_delivery": days_since_delivery,
                "grace_days": grace_days,
                "delivery_date": delivery_date.strftime('%Y-%m-%d'),
                "message": f"Order delivered on {delivery_date.strftime('%Y-%m-%d')} ({days_since_delivery} days ago). Grace period: {grace_days} days.",
                "order_status": order_status
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Error checking delivery eligibility: {str(e)}", "error")
            return {
                "success": False,
                "eligible": False,
                "error": str(e)
            }

    @staticmethod
    async def check_grace_period_eligibility(delivery_date: str, request_type: str = "return", state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Check if an order is within the grace period for returns or exchanges based on delivery date.
        
        This is a stateless check that takes delivery_date as input (unlike check_delivery_eligibility
        which fetches order data). Use this when you already have the delivery date from order details.
        
        Args:
            delivery_date: The delivery date string (ISO format or YYYY-MM-DD)
            request_type: Type of request - "return" or "exchange"
            state: Current state dictionary for client_id and logging
            
        Returns:
            Dictionary with:
                - success: bool
                - eligible: bool
                - days_since_delivery: int
                - grace_days: int
                - message: str (human readable summary)
        """
        from datetime import datetime
        import pytz

        log_with_trace_id(state, f"ReturnExchangeOrchestrator: Checking grace period for delivery_date: {delivery_date}, request_type: {request_type}")

        try:
            # Get client_id from state
            client_id = None
            if state:
                client_id = state.get("client_id") or state.get("context", {}).get("client_id")

            # Get grace period from config (single source of truth — no hard-coded fallback)
            grace_days = await ReturnExchangeOrchestrator._aget_return_grace_days(client_id, state)

            # Handle "Not available" or empty delivery dates
            if not delivery_date or delivery_date.lower() in ["not available", "n/a", "none", ""]:
                log_with_trace_id(state, "⚠️ Delivery date not available, assuming eligible")
                return {
                    "success": True,
                    "eligible": True,  # Allow if we can't determine
                    "days_since_delivery": 0,
                    "grace_days": grace_days,
                    "message": "Delivery date not available. Please verify the order status. If the order is delivered, customer may be eligible for return/exchange."
                }

            # Parse delivery date
            ist_tz = pytz.timezone('Asia/Kolkata')
            now = datetime.now(ist_tz)
            
            parsed_date = None
            try:
                # Try different date formats
                if 'T' in delivery_date or '+' in delivery_date:
                    # ISO format: 2025-12-10T12:46:00+05:30
                    parsed_date = datetime.fromisoformat(delivery_date.replace('Z', '+00:00'))
                    parsed_date = parsed_date.astimezone(ist_tz)
                else:
                    # Try multiple date formats
                    date_formats = [
                        '%Y-%m-%d',           # 2025-12-10
                        '%d-%m-%Y %H:%M:%S',  # 10-12-2025 12:46:00 (Shiprocket format)
                        '%d-%m-%Y',           # 10-12-2025
                        '%d %b %Y',           # 10 Dec 2025 (Shiprocket format)
                        '%d %B %Y',           # 10 December 2025
                        '%B %d, %Y',          # December 10, 2025 (US format)
                        '%b %d, %Y',          # Dec 10, 2025 (US short format)
                        '%Y-%m-%d %H:%M:%S',  # 2025-12-10 12:46:00
                        '%d/%m/%Y',           # 10/12/2025
                        '%m/%d/%Y',           # 12/10/2025
                    ]
                    
                    for fmt in date_formats:
                        try:
                            parsed_date = datetime.strptime(delivery_date.strip(), fmt)
                            parsed_date = ist_tz.localize(parsed_date)
                            log_with_trace_id(state, f"✅ Parsed delivery date '{delivery_date}' using format '{fmt}'")
                            break
                        except ValueError:
                            continue
                    
                    if parsed_date is None:
                        raise ValueError(f"No matching date format found for '{delivery_date}'")
                        
            except Exception as parse_error:
                log_with_trace_id(state, f"⚠️ Could not parse delivery date '{delivery_date}': {parse_error}")
                return {
                    "success": False,
                    "eligible": False,
                    "days_since_delivery": 0,
                    "grace_days": grace_days,
                    "message": f"Could not parse delivery date '{delivery_date}'. Please verify the order status."
                }
            
            days_since_delivery = (now - parsed_date).days
            is_eligible = days_since_delivery <= grace_days
            
            log_with_trace_id(state, f"✅ Grace period check: {days_since_delivery} days since delivery, grace period: {grace_days} days, eligible: {is_eligible}")
            
            if is_eligible:
                message = f"✅ Order is ELIGIBLE for {request_type}. Delivered on {parsed_date.strftime('%Y-%m-%d')} ({days_since_delivery} days ago). Grace period: {grace_days} days."
            else:
                message = f"❌ Order is NOT ELIGIBLE for {request_type}. Delivered on {parsed_date.strftime('%Y-%m-%d')} ({days_since_delivery} days ago). Grace period of {grace_days} days has expired."
            
            return {
                "success": True,
                "eligible": is_eligible,
                "days_since_delivery": days_since_delivery,
                "grace_days": grace_days,
                "delivery_date": parsed_date.strftime('%Y-%m-%d'),
                "message": message
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Error checking grace period eligibility: {str(e)}", "error")
            return {
                "success": False,
                "eligible": False,
                "days_since_delivery": 0,
                # grace_days may be unresolved if the config lookup itself failed
                "grace_days": locals().get("grace_days"),
                "error": str(e),
                "message": f"Error checking grace period: {str(e)}"
            }


class EscalationOrchestrator:
    """
    Orchestrator for escalation operations.
    Coordinates WhatsApp notifications, database logging, and response generation.
    """
    
    @staticmethod
    def _clean_phone_number(phone: str) -> str:
        """
        Remove +91 or 91 prefix from phone number for display.
        Does not modify state, only cleans the value for notification messages.
        """
        if not phone or phone == 'Not provided':
            return phone
        # Remove + first, then check for 91 prefix
        cleaned = phone.lstrip('+')
        if cleaned.startswith('91') and len(cleaned) > 10:
            cleaned = cleaned[2:]
        return cleaned
    
    @staticmethod
    async def escalate_to_agent(
        category: str,
        reason: str,
        details: str,
        state: Optional[Dict] = None,
        order_id: Optional[str] = None,
        phone_number: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Escalate case to human agent with WhatsApp notification.
        
        Args:
            category: Type of escalation (e.g., "Return Request", "Exchange Request")
            reason: Brief reason for escalation
            details: Full details for the agent
            state: Current state dictionary
            order_id: Order ID being escalated (optional, can be extracted from state)
            phone_number: Customer's phone number (optional, can be extracted from state)
            
        Returns:
            Dictionary with escalation result
        """
        from fashion_bot.utils.escalation_helper import (
            build_escalation_notification,
            build_escalation_email_html,
            prepare_escalation_metadata
        )
        from fashion_bot.utils.utils import get_trace_id
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating - Category: {category}")
        
        try:
            # Extract data from state if not provided
            if not phone_number and state:
                phone_number = state.get("phone_number")
            if not order_id and state:
                order_id = state.get("order_id")
            
            trace_id = get_trace_id(state) if state else None
            
            # Get current IST timestamp
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            
            # 1. Build notification message (business logic)
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            client_id = state.get("client_id") if state else None

            from fashion_bot.agent_config import aget_escalation_group
            escalation_group = await aget_escalation_group(
                client_id, category, order_id=order_id
            )

            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            
            # 2. Send WhatsApp notification
            from fashion_bot.gupshup_webhook import send_message
            from fashion_bot.agent_config import aget_agent_phone_number
            
            client_id = state.get("client_id") if state else None
            agent_phone = await aget_agent_phone_number(client_id=client_id)
            await send_message(agent_phone, notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Sent WhatsApp notification to agent: {agent_phone}")
            
            # 3. Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            
            # Prepare metadata
            metadata = prepare_escalation_metadata(
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                escalation_type=category.lower().replace(" ", "_")
            )
            
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=reason,
                action_required=details,
                user_messages=user_msgs[-5:] if len(user_msgs) >= 5 else user_msgs,
                metadata=metadata
            )
            
            # 4. Get contact details message
            from fashion_bot.nodes.order_nodes import get_contact_details_message
            contact_msg = await get_contact_details_message(client_id=client_id)
            
            return {
                "success": True,
                "message": f"ESCALATION_REQUIRED: {category} logged. Our team will contact you soon.",
                "support_team_contact_details": contact_msg,
                "escalation_id": trace_id,
                "category": category
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "message": f"Error during escalation: {str(e)}",
                "error": str(e)
            }
    
    @staticmethod
    async def escalate_cancellation_threat(order_id: str, reason: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Escalate cancellation threat immediately with WhatsApp notification.
        
        Args:
            order_id: Order ID
            reason: Reason for cancellation threat
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result and customer message
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating cancellation threat for order {order_id}")
        
        try:
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"][-3:]
            
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            customer_context = f"Customer threatening cancellation: {order_id}\n\nReason: {reason}\n\nRecent conversation:\n" + "\n".join(user_msgs)
            notification_type = "[Cancellation Threat]"
            
            agent_notification = f"{notification_type}:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n{customer_context}\nPhone: {EscalationOrchestrator._clean_phone_number(state.get('phone_number', 'Not provided') if state else 'Not provided')}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), agent_notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Cancellation threat escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Cancellation Requests",
                reason=reason,
                action_required=f"Contact customer urgently regarding order {order_id}",
                user_messages=user_msgs,
                metadata={
                    "escalation_type": "cancellation_threat",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "trace_id": trace_id,
                    "context": customer_context
                }
            )

            # Write to cancellation_aversion_events so the Cancellation Requests
            # page shows data even in escalate_only mode (PRD v2.4).
            try:
                from fashion_bot.analytics.cancellation_aversion_tracker import (
                    _db_open_event,
                    _db_close_event,
                )
                client_id = state.get("client_id") if state else None
                phone_number = state.get("phone_number", "") if state else ""
                conversation_id = state.get("conversation_id") if state else None
                _snapshot = [{"role": "human", "content": m} for m in (user_msgs or [])]
                _meta = {
                    "trace_id": trace_id,
                    "cancellation_reason": reason,
                    "escalation_source": "escalate_only_mode",
                }
                if conversation_id:
                    _meta["conversation_id"] = str(conversation_id)
                _evt_id = await _db_open_event(
                    client_id=client_id,
                    phone=phone_number,
                    order_id=order_id,
                    intent_trigger_msg=reason or "Cancellation escalation",
                    intent_trigger_type="escalation",
                    intent_confidence="high",
                    tools_called=[],
                    conversation_snapshot=_snapshot,
                    metadata=_meta,
                    event_type="cancellation_aversion",
                    conversation_id=str(conversation_id) if conversation_id else None,
                )
                if _evt_id:
                    await _db_close_event(
                        event_id=_evt_id,
                        status="escalated",
                        resolution="escalation",
                        aversion_method="escalate_only",
                        outcome_trigger_tool=None,
                        turn_count=1,
                        tools_called=[],
                        intermediate_signals=[],
                        conversation_snapshot=_snapshot,
                        cancellation_reason=reason,
                    )
                    log_with_trace_id(state, f"[TRACKER] Cancellation aversion event {_evt_id} written (escalate_only mode)")
            except Exception as _tracker_err:
                log_with_trace_id(state, f"[TRACKER] Failed to write cancellation_aversion_event (non-fatal): {_tracker_err}", "warning")

            # Build customer message
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            customer_message = "We truly understand that, let me mark this order as priority and update the order. Meanwhile, I will ask customer support to connect with you."
            
            return {
                "success": True,
                "escalated": True,
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "cancellation_threat",
                "priority_order": True
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Cancellation threat escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "escalated": False,
                "error": str(e),
                "customer_message": "I'll connect you with our support team to resolve this urgently."
            }
    
    @staticmethod
    async def escalate_urgent_delivery(order_id: str, urgency_count: int, reason: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Escalate repeated urgent delivery requests with WhatsApp notification.
        
        Args:
            order_id: Order ID
            urgency_count: Number of urgent requests detected
            reason: Reason for escalation
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result and customer message
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating urgent delivery for order {order_id} (count: {urgency_count})")
        
        try:
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"][-5:]
            
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            customer_context = f"Customer urgently requesting faster delivery: {order_id}\n\nReason: {reason}\n\nRecent conversation:\n" + "\n".join(user_msgs)
            notification_type = "[Urgent Delivery Request]"
            
            agent_notification = f"{notification_type}:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n📊 Urgency Count: {urgency_count}\n{customer_context}\nPhone: {EscalationOrchestrator._clean_phone_number(state.get('phone_number', 'Not provided') if state else 'Not provided')}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), agent_notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Urgent delivery escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason=reason,
                action_required=f"Contact customer urgently regarding order {order_id}",
                user_messages=user_msgs,
                metadata={
                    "escalation_type": "urgent_delivery",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "urgent_count": urgency_count,
                    "trace_id": trace_id,
                    "context": customer_context
                }
            )
            
            # Build customer message
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            customer_message = "This seems important. I'll transfer you to a team member who will reach out to you shortly. Thank you for your patience."
            
            return {
                "success": True,
                "escalated": True,
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "urgent_delivery",
                "urgency_count": urgency_count
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Urgent delivery escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "escalated": False,
                "error": str(e),
                "customer_message": "I'll connect you with our support team to prioritize your delivery."
            }

    @staticmethod
    async def acheck_order_for_escalation(order_id: str, state: Optional[Dict] = None, cached_order_data: Optional[Dict] = None) -> Dict[str, Any]:
        """Async version of check_order_for_escalation."""
        log_with_trace_id(state, f"EscalationOrchestrator: Async checking escalation for order {order_id}")

        try:
            if cached_order_data and not cached_order_data.get("error"):
                order_status = cached_order_data
            else:
                order_status = await OrderStatusOrchestrator.aget_order_status_summary(order_id, state=state)

            if not order_status or order_status.get("error"):
                return {
                    "needs_escalation": False,
                    "error": order_status.get("error", "Unable to fetch order status") if order_status else "Unable to fetch order status",
                    "order_status": "unknown"
                }

            status = order_status.get("status", "").lower()
            partner_status = order_status.get("partner_status", "").lower()
            days_since_shipped = order_status.get("days_since_shipped", 0)

            needs_escalation = False
            escalation_type = None
            reason = None

            if partner_status in ["in transit", "out for delivery"] and days_since_shipped > 7:
                needs_escalation = True
                escalation_type = "undelivered"
                reason = f"Order in transit for {days_since_shipped} days"
            elif partner_status in ["rto initiated", "rto in transit", "exception", "lost", "misrouted"]:
                needs_escalation = True
                escalation_type = "misrouted"
                reason = f"Order status: {partner_status}"
            elif partner_status == "in transit" and days_since_shipped > 5:
                needs_escalation = True
                escalation_type = "delayed"
                reason = f"Order delayed - {days_since_shipped} days in transit"

            log_with_trace_id(state, f"Escalation check: needs={needs_escalation}, type={escalation_type}")

            return {
                "needs_escalation": needs_escalation,
                "escalation_type": escalation_type,
                "reason": reason,
                "order_status": status,
                "partner_status": partner_status,
                "days_since_shipped": days_since_shipped
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Escalation check failed: {str(e)}", "error")
            return {
                "needs_escalation": False,
                "error": str(e),
                "order_status": "unknown"
            }

    @staticmethod
    async def escalate_undelivered_order(order_id: str, last_message: str = "N/A", state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Escalate an undelivered order - sends WhatsApp and email notifications.
        
        Args:
            order_id: Order ID to escalate
            last_message: Last user message for context
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result including customer_message for response
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating undelivered order {order_id}")
        
        try:
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            notification = f"[Undelivered Order Escalation]:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n📦 Order: {order_id}\n💬 Last Message: {last_message}\n📱 Phone: {EscalationOrchestrator._clean_phone_number(state.get('phone_number', 'Not provided') if state else 'Not provided')}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Undelivered order escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason="Undelivered order",
                action_required=f"Check delivery status for order {order_id}",
                user_messages=[last_message] if last_message != "N/A" else [],
                metadata={
                    "escalation_type": "undelivered",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "trace_id": trace_id
                }
            )
            
            # Get real contact details if available
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            
            # Build customer message - DO NOT include fake phone numbers
            customer_message = f"We truly apologize that you haven't received order {order_id}. I've escalated this as an undelivered order to our support team. They will investigate and get back to you ASAP."
            
            return {
                "success": True,
                "escalated": True,
                "message": f"Undelivered order {order_id} has been escalated to our support team.",
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "undelivered"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Undelivered order escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e)
            }

    @staticmethod
    async def escalate_misrouted_order(order_id: str, last_message: str = "N/A", state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Escalate a misrouted order - sends WhatsApp and email notifications.
        
        Args:
            order_id: Order ID to escalate
            last_message: Last user message for context
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating misrouted order {order_id}")
        
        try:
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            notification = f"[Misrouted Order Escalation]:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n📦 Order: {order_id}\n⚠️ Status: Order appears misrouted or in exception status\n💬 Last Message: {last_message}\n📱 Phone: {EscalationOrchestrator._clean_phone_number(state.get('phone_number', 'Not provided') if state else 'Not provided')}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Misrouted order escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason="Misrouted order",
                action_required=f"Urgent: Check routing for order {order_id}",
                user_messages=[last_message] if last_message != "N/A" else [],
                metadata={
                    "escalation_type": "misrouted",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "trace_id": trace_id
                }
            )
            
            return {
                "success": True,
                "escalated": True,
                "message": f"Misrouted order {order_id} has been escalated to our logistics team.",
                "escalation_type": "misrouted"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Misrouted order escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e)
            }

    @staticmethod
    async def escalate_delayed_order(order_id: str, days_delayed: int = 0, recent_messages: list = None, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Escalate a delayed order - sends WhatsApp notification to agent.
        
        Args:
            order_id: Order ID to escalate
            days_delayed: Number of days the order is delayed
            recent_messages: Recent conversation messages for context
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating delayed order {order_id} ({days_delayed} days)")
        
        try:
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            recent_context = "\n".join(recent_messages[:3] if recent_messages else [])
            
            notification = f"[Delayed Order Escalation]:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n📦 Order: {order_id}\n📅 Days Delayed: {days_delayed}\n💬 Recent Messages:\n{recent_context}\n📱 Phone: {EscalationOrchestrator._clean_phone_number(state.get('phone_number', 'Not provided') if state else 'Not provided')}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Delayed order escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason=f"Order delayed by {days_delayed} days",
                action_required=f"Check delivery timeline for order {order_id}",
                user_messages=recent_messages or [],
                metadata={
                    "escalation_type": "delayed",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "days_delayed": days_delayed,
                    "trace_id": trace_id
                }
            )
            
            return {
                "success": True,
                "escalated": True,
                "message": f"Delayed order {order_id} ({days_delayed} days) has been escalated.",
                "escalation_type": "delayed"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Delayed order escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e)
            }

    @staticmethod
    async def escalate_delayed_pickup(
        order_id: str,
        phone_number: str = "",
        days_since_delivery: int = 0,
        recent_messages: list = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Escalate a significantly delayed pickup (>13 days) to support team.
        
        Args:
            order_id: Order ID to escalate
            phone_number: Customer's phone number
            days_since_delivery: Number of days since delivery
            recent_messages: Recent conversation messages for context
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation result and customer message
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Escalating delayed pickup for order {order_id} ({days_since_delivery} days since delivery)")
        
        try:
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            recent_context = "\n".join(recent_messages[:3] if recent_messages else [])
            clean_phone = EscalationOrchestrator._clean_phone_number(phone_number or (state.get('phone_number', 'Not provided') if state else 'Not provided'))
            
            notification = (
                f"[Delayed Pickup Escalation]:\n"
                f"⏰ Time: {current_time_ist} IST\n"
                f"🔍 Trace ID: {trace_id}\n"
                f"📦 Order: {order_id}\n"
                f"📅 Days Since Delivery: {days_since_delivery}\n"
                f"💬 Recent Messages:\n{recent_context}\n"
                f"📱 Phone: {clean_phone}"
            )
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Delayed pickup escalation sent to agent")
            
            # 2. Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Pickup Query",
                reason=f"Pickup delayed - {days_since_delivery} days since delivery for order {order_id}",
                action_required=f"Arrange pickup for order {order_id} - {days_since_delivery} days since delivery",
                user_messages=recent_messages or [],
                metadata={
                    "escalation_type": "delayed_pickup",
                    "escalation_classification": "agentic",
                    "order_id": order_id,
                    "phone_number": clean_phone,
                    "days_since_delivery": days_since_delivery,
                    "trace_id": trace_id
                }
            )
            
            # 3. Build customer message
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            customer_message = (
                f"Your pickup request for order {order_id} has been delayed ({days_since_delivery} days since delivery). "
                f"I've escalated this to our support team for immediate attention. "
                f"Someone will contact you shortly to arrange the pickup."
            )
            
            return {
                "success": True,
                "escalated": True,
                "message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "delayed_pickup"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Delayed pickup escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "escalated": False,
                "error": str(e),
                "message": f"ESCALATION_REQUIRED: Pickup delayed for {days_since_delivery} days. Our team will contact you shortly."
            }

    @staticmethod
    async def send_escalation_whatsapp(
        message: str,
        escalation_type: str = "general",
        order_id: str = "",
        phone: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Send a custom escalation message to agent via WhatsApp.
        
        Args:
            message: Escalation message to send
            escalation_type: Type of escalation
            order_id: Order ID related to escalation
            phone: Customer's phone number
            state: Current state dictionary
            
        Returns:
            Dictionary with result
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Sending {escalation_type} escalation via WhatsApp")
        
        try:
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            
            notification = f"[{escalation_type.title()} Escalation]:\n⏰ Time: {current_time_ist} IST\n🔍 Trace ID: {trace_id}\n📦 Order: {order_id or 'N/A'}\n📱 Phone: {EscalationOrchestrator._clean_phone_number(phone or state.get('phone_number', 'Not provided') if state else phone or 'Not provided')}\n💬 Message: {message}"
            
            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ {escalation_type} escalation sent to agent")
            
            return {
                "success": True,
                "message": f"Escalation message sent to support team.",
                "escalation_type": escalation_type
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ WhatsApp escalation failed: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e)
            }

    @staticmethod
    async def check_restocking_query(
        query: str,
        recent_messages: List[str] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Handle restocking query escalation - called when LLM detects customer wants restock notification.
        
        Args:
            query: The customer's message about restocking
            recent_messages: Recent user messages for context
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation status and customer message
        """
        from fashion_bot.gupshup_webhook import send_message
        from fashion_bot.agent_config import aget_agent_phone_number
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime
        
        log_with_trace_id(state, f"EscalationOrchestrator: Handling restocking query escalation")
        
        try:
            # Prepare escalation
            ist_tz = pytz.timezone('Asia/Kolkata')
            current_time_ist = datetime.now(ist_tz).strftime('%Y-%m-%d %H:%M:%S')
            trace_id = get_trace_id(state) if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            
            # Build context from recent messages
            context = "\n".join(recent_messages) if recent_messages else query
            
            # Build notification
            notification = f"""[Restocking Query - Customer Notification Request]:
⏰ Time: {current_time_ist} IST
🔍 Trace ID: {trace_id}
📱 Phone: {phone_number}

💬 Customer Query: {query}

📝 Recent Context:
{context}

⚡ Action Required: Add customer to restock notification list or update on availability."""

            client_id = state.get("client_id") if state else None
            await send_message(await aget_agent_phone_number(client_id=client_id), notification, client_id=client_id)
            log_with_trace_id(state, f"✅ Restocking query escalation sent to agent")
            
            # Log to database
            from fashion_bot.utils.escalation_helper import alog_escalation_from_state
            await alog_escalation_from_state(
                state=state,
                category="Restocking Query",
                reason="Customer asking about product restocking/availability",
                action_required="Add customer to restock notification list",
                user_messages=recent_messages if recent_messages else [query],
                metadata={
                    "escalation_type": "restocking_query",
                    "escalation_classification": "agentic",
                    "trace_id": trace_id,
                    "query": query
                }
            )
            
            # Get contact details for customer message
            client_id = state.get("client_id") if state else None
            contact_details = await get_contact_details_message(client_id=client_id)
            
            customer_message = "I've noted your interest in this product. Our team will notify you as soon as it's back in stock."
            
            return {
                "escalated": True,
                "message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "restocking_query"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Restocking query escalation failed: {str(e)}", "error")
            return {
                "escalated": False,
                "error": str(e),
                "message": "I'll note your interest and have our team reach out to you about availability."
            }

    @staticmethod
    async def aescalate_to_agent(
        category: str,
        reason: str,
        details: str,
        state: Optional[Dict] = None,
        order_id: Optional[str] = None,
        phone_number: Optional[str] = None,
        escalation_classification: str = "system",
        agent: str = "",
        immediate_attention: bool = False,
        human_can_resolve: bool = True,
    ) -> Dict[str, Any]:
        from fashion_bot.utils.utils import get_trace_id
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        import pytz
        from datetime import datetime

        try:
            if not phone_number and state:
                phone_number = state.get("phone_number")
            if not order_id and state:
                order_id = state.get("order_id")

            trace_id = get_trace_id(state) if state else None

            # Constrain the free-form tool ``category`` to a canonical
            # ESCALATION_TOOL_CATEGORIES value so the staff-card title, group
            # routing, and analytics never surface an off-enum slug (e.g.
            # "urgent_delivery"). Done once here so every downstream use —
            # notification, group resolution, logging — sees the canonical form.
            from fashion_bot.agent_config import normalize_escalation_category
            _raw_category = category
            category = normalize_escalation_category(category)
            if category != _raw_category:
                log_with_trace_id(
                    state,
                    f"[ESCALATION] normalized category '{_raw_category}' → '{category}'",
                )

            valid_classifications = {"user_configured", "agentic", "system"}
            classification = escalation_classification if escalation_classification in valid_classifications else "system"

            def _emit_escalation_metric(actionability, soft_blocked, metric_client_id):
                """Count one escalation OUTCOME on escalations.total.

                Emitted exactly once per outcome — a delivered escalation, a
                soft-blocked one (soft_blocked=true), or a courier internal
                sync — and NOT for intermediate returns like the web-chat
                "please share your phone number" turn, so the counter matches
                escalations that actually happened. Telemetry must never break
                the request.
                """
                try:
                    from fashion_bot.monitoring.otel_metrics import escalation_counter

                    escalation_counter.add(
                        1,
                        {
                            "category": str(category),
                            "classification": str(classification),
                            "actionability": str(actionability or "unknown"),
                            "soft_blocked": str(bool(soft_blocked)).lower(),
                            "client_id": str(metric_client_id or "unknown"),
                        },
                    )
                except Exception:
                    pass

            # Courier Update Pending uses a specialized notification format
            # and sends to the client's configured agent phone (per client_id).
            if category == "Courier Update Pending":
                from fashion_bot.utils.delivery_utils import (
                    anotify_agent_for_non_integrated_partners,
                )
                action_type = "updated"
                if reason and "cancel" in reason.lower():
                    action_type = "cancelled"
                client_id = state.get("client_id") if state else None
                sync_result = await anotify_agent_for_non_integrated_partners(
                    client_id=client_id,
                    order_id=order_id or "",
                    action_type=action_type,
                    action_details=details or reason,
                    customer_phone=phone_number or "",
                    state=state,
                )
                # Courier syncs are mandatory hand-offs — count them so every
                # escalation that flows through THIS function is counted.
                # Note: escalations.total does NOT cover escalation paths that
                # call alog_escalation_from_state directly and bypass
                # aescalate_to_agent (offline-store leads, return-partner
                # hand-offs, the delayed-order/pickup notifiers).
                _emit_escalation_metric("mandatory", False, client_id)
                return {
                    "success": True,
                    "internal_sync": True,
                    "notified": sync_result.get("notified", False),
                    "partners": sync_result.get("partners", []),
                    "message": (
                        "Courier Update Pending notification sent to our operations team. "
                        "This is an internal action — do NOT mention this to the customer. "
                        "Respond to the customer based on the order update/cancellation result only."
                    ),
                    "escalation_id": trace_id,
                    "category": category,
                }

            # ── Actionability gate + escalation observability ──
            # Soft-blocks ONLY an escalation the escalating agent itself flagged
            # unfulfillable (human_can_resolve=False — e.g. a variant/SKU that
            # doesn't exist), turning it back into an "offer the real options"
            # reply. Mandatory hand-offs (cancellation, callback, courier, …) are
            # never blocked. On by default; opt out per client with
            # escalation_policy.gate_enabled = false. escalations.total is
            # emitted once per OUTCOME (here for a soft-block, or after the
            # phone-collection gate below for a delivered escalation).
            _gate_client_id = state.get("client_id") if state else None
            try:
                from fashion_bot.agent_config import aevaluate_escalation_gate

                gate = await aevaluate_escalation_gate(
                    client_id=_gate_client_id,
                    category=category,
                    human_can_resolve=human_can_resolve,
                )
            except Exception as gate_err:  # never break the escalation path
                log_with_trace_id(
                    state,
                    f"[ESCALATION_GATE] eval failed (non-critical): {gate_err}",
                    "warning",
                )
                gate = {"actionability": "actionable", "soft_block": False, "message": None}

            log_with_trace_id(
                state,
                f"[ESCALATION_GATE] category={category} "
                f"actionability={gate.get('actionability')} soft_block={gate.get('soft_block')}",
            )

            if gate.get("soft_block"):
                log_with_trace_id(
                    state,
                    f"[ESCALATION_GATE] soft-blocked unfulfillable escalation "
                    f"(category={category}) — instructing agent to offer alternatives",
                )
                _emit_escalation_metric(gate.get("actionability"), True, _gate_client_id)
                # Durable audit trail (conversation history, same pattern as the
                # "[Escalation]" event below) so a suppressed escalation can be
                # reviewed later without relying on log retention. Deliberately
                # NOT an escalations-table row: that would surface in the
                # support dashboard and trigger lead-gen emission.
                try:
                    from fashion_bot.history.conversation_handler import astore_conversation_event

                    _sb_state_phone = str(state.get("phone_number", "")) if state else ""
                    _sb_is_web = _sb_state_phone.startswith("web_") or bool(state.get("session_id")) if state else False
                    await astore_conversation_event(
                        client_id=_gate_client_id,
                        phone=str(phone_number) if phone_number else "",
                        sender="bot",
                        text=f"[Escalation soft-blocked] {category}: {reason}",
                        channel_type="web-chat" if _sb_is_web else "whatsapp",
                    )
                except Exception as sb_store_err:
                    log_with_trace_id(
                        state,
                        f"⚠️ Error storing soft-block audit event (non-critical): {sb_store_err}",
                        "warning",
                    )
                # success=True: the tool call itself worked, it simply did not
                # escalate. Returning success=False here reads as a TECHNICAL
                # FAILURE to the agent, whose prompt guardrail would then answer
                # "I won't be able to answer your query due to a technical
                # error" instead of offering alternatives. ``escalated`` is the
                # field that says what happened.
                return {
                    "success": True,
                    "escalated": False,
                    "resolution_required": True,
                    "message": gate.get("message"),
                }

            # Web chat contact-collection gate: if the customer's identifier is a
            # session ID (not a real phone), ask once for phone or email before
            # proceeding. On the second attempt (flag already set), extract any
            # contact from the latest message and proceed with escalation.
            from fashion_bot.utils.phone_number_utils import aresolve_contact_collection_gate
            ask_for_contact, customer_contact = await aresolve_contact_collection_gate(
                state, phone_number=phone_number, client_id=_gate_client_id, trace_id=trace_id
            )
            if ask_for_contact:
                return {
                    "success": False,
                    "phone_number_required": True,
                    "message": (
                        "STOP. Do NOT call any other tool. Do NOT provide contact details. "
                        "Simply ask the customer: 'Could you please share your phone number "
                        "or email address so our team can reach out to you?'"
                    ),
                }

            # Count the escalation only now — past the soft-block and the
            # phone-collection gate — so each delivered escalation increments
            # escalations.total exactly once (the "please share your phone"
            # turn above is not an escalation and is not counted).
            _emit_escalation_metric(gate.get("actionability"), False, _gate_client_id)

            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            client_id = state.get("client_id") if state else None

            from fashion_bot.agent_config import aget_escalation_group
            escalation_group = await aget_escalation_group(
                client_id, category, agent=agent, order_id=order_id
            )

            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
                customer_contact=customer_contact,
            )

            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
                immediate_attention=immediate_attention,
                customer_contact=customer_contact,
            )

            # Repeat-chase handling (design_docs/ESCALATION_FOLLOW_UP_CONTEXT.md §3.4).
            # When this escalation repeats an issue the customer already has open,
            # thread the lineage onto the row and rate-limit the STAFF ALERT so a
            # customer sending five messages in two minutes doesn't produce five
            # URGENT pings. The row itself is ALWAYS written — gating the insert
            # would silently drop ~18% of escalations and move the dashboard's
            # counts and resolution-rate denominators.
            follow_up: Optional[Dict[str, Any]] = None
            send_alert = True
            try:
                from fashion_bot.utils.escalation_context import (
                    aget_follow_up_lineage,
                    ashould_send_follow_up_alert,
                )

                follow_up = await aget_follow_up_lineage(
                    client_id=client_id,
                    phone_number=phone_number,
                    category=category,
                    order_id=order_id,
                    trace_id=trace_id,
                )
                if follow_up:
                    send_alert = await ashould_send_follow_up_alert(
                        client_id=client_id,
                        phone_number=phone_number,
                        thread_key=follow_up["thread_key"],
                    )
            except Exception as _fu_err:  # never block an escalation
                log_with_trace_id(
                    state, f"⚠️ [escalation_context] follow-up check failed: {_fu_err}", "warning"
                )

            if send_alert:
                await asend_escalation_notification(
                    notification,
                    client_id=client_id,
                    state=state,
                    agent=agent or None,
                    category=category,
                    immediate_attention=immediate_attention,
                    email_html=email_html,
                    customer_contact_provided=bool(customer_contact),
                    details=details,
                    order_id=order_id,
                    escalation_group=escalation_group,
                    customer_contact=customer_contact,
                    timestamp_ist=current_time_ist,
                )
            else:
                log_with_trace_id(
                    state,
                    f"🔕 [ESCALATION] repeat chase on {follow_up['thread_key']} within cooldown — "
                    f"row logged, staff alert suppressed (chase #{follow_up['follow_up_count']})",
                )

            metadata = {
                "escalation_classification": classification,
                "immediate_attention": immediate_attention,
                "agent": agent or None,
            }
            if order_id:
                metadata["order_id"] = order_id
            if customer_contact:
                # Web chat has no reachable identifier of its own: the row's
                # ``customer_phone`` is the session id (``web_02095d1c-2e07-4d``).
                # When the customer gives a phone the session links and the column
                # becomes that number, but an EMAIL cannot be an identity key — so
                # without this the only contact on the record is buried in the
                # ``whatsapp_message`` free text, and an ops person opening the
                # escalation later has nothing to reach them by. Persist it as a
                # field so the dashboard, the context block and get_escalations can
                # all surface it.
                metadata["customer_contact"] = customer_contact
            if follow_up:
                metadata.update(
                    {
                        "follow_up_of": follow_up["follow_up_of"],
                        "follow_up_count": follow_up["follow_up_count"],
                        "original_raised_at": follow_up["original_raised_at"],
                        "hours_waiting": follow_up["hours_waiting"],
                        "alert_suppressed": not send_alert,
                    }
                )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=reason,
                action_required=details,
                user_messages=user_msgs[-5:] if len(user_msgs) >= 5 else user_msgs,
                metadata=metadata,
            )

            # Store escalation summary in conversation history
            try:
                from fashion_bot.history.conversation_handler import astore_conversation_event
                client_id = state.get("client_id") if state else None
                _phone = str(state.get("phone_number", "")) if state else ""
                _is_web = _phone.startswith("web_") or bool(state.get("session_id")) if state else False
                _channel = "web-chat" if _is_web else "whatsapp"
                await astore_conversation_event(
                    client_id=client_id,
                    phone=str(phone_number) if phone_number else "",
                    sender="bot",
                    text=f"[Escalation] {category}: {reason}",
                    channel_type=_channel
                )
            except Exception as store_err:
                log_with_trace_id(state, f"⚠️ Error storing escalation to conversation history (non-critical): {store_err}", "warning")

            # Agent-mode switch after escalation is disabled — the bot continues
            # replying on subsequent turns. Manual dashboard toggle still works.
            log_with_trace_id(state, f"ℹ️ Escalation logged; agent-mode auto-switch is disabled")

            contact_msg = await get_contact_details_message(client_id=client_id)
            from fashion_bot.agent_config import aget_escalation_customer_message

            # Per-client override of the confirmation the customer is told
            # (escalation_messaging.customer_message). A client who does not
            # want a "someone will reach out" promise configures the wording
            # once and every escalation path picks it up.
            customer_message = await aget_escalation_customer_message(
                client_id, default="Our team will contact you soon."
            )
            return {
                "success": True,
                "message": f"ESCALATION_REQUIRED: {category} logged. {customer_message}",
                "customer_message": customer_message,
                "support_team_contact_details": contact_msg,
                "escalation_id": trace_id,
                "category": category,
            }
        except Exception as e:
            log_with_trace_id(state, f"❌ Async escalation failed: {str(e)}", "error")
            return {"success": False, "message": f"Error during escalation: {str(e)}", "error": str(e)}

    @staticmethod
    async def aescalate_cancellation_threat(order_id: str, reason: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"][-3:]
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            details = f"Customer threatening cancellation: {order_id}\n\nReason: {reason}"
            category = "Cancellation Requests"

            escalation_group = await aget_escalation_group(client_id, category, agent="order_status")
            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs if user_msgs else None,
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs if user_msgs else None,
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=reason,
                action_required=f"Contact customer urgently regarding order {order_id}",
                user_messages=user_msgs,
                metadata={"escalation_type": "cancellation_threat", "escalation_classification": "agentic", "order_id": order_id, "trace_id": trace_id},
            )

            # Write to cancellation_aversion_events so the Cancellation Requests
            # page shows data even in escalate_only mode (PRD v2.4).
            try:
                from fashion_bot.analytics.cancellation_aversion_tracker import (
                    _db_open_event,
                    _db_close_event,
                )
                conversation_id = state.get("conversation_id") if state else None
                _snapshot = [{"role": "human", "content": m} for m in (user_msgs or [])]
                _meta = {
                    "trace_id": trace_id,
                    "cancellation_reason": reason,
                    "escalation_source": "escalate_only_mode",
                }
                if conversation_id:
                    _meta["conversation_id"] = str(conversation_id)
                _evt_id = await _db_open_event(
                    client_id=client_id,
                    phone=phone_number,
                    order_id=order_id,
                    intent_trigger_msg=reason or "Cancellation escalation",
                    intent_trigger_type="escalation",
                    intent_confidence="high",
                    tools_called=[],
                    conversation_snapshot=_snapshot,
                    metadata=_meta,
                    event_type="cancellation_aversion",
                    conversation_id=str(conversation_id) if conversation_id else None,
                )
                if _evt_id:
                    await _db_close_event(
                        event_id=_evt_id,
                        status="escalated",
                        resolution="escalation",
                        aversion_method="escalate_only",
                        outcome_trigger_tool=None,
                        turn_count=1,
                        tools_called=[],
                        intermediate_signals=[],
                        conversation_snapshot=_snapshot,
                        cancellation_reason=reason,
                    )
                    log_with_trace_id(state, f"[TRACKER] Cancellation aversion event {_evt_id} written (escalate_only mode)")
            except Exception as _tracker_err:
                log_with_trace_id(state, f"[TRACKER] Failed to write cancellation_aversion_event (non-fatal): {_tracker_err}", "warning")

            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            from fashion_bot.agent_config import aget_escalation_customer_message

            customer_message = await aget_escalation_customer_message(
                client_id,
                default=(
                    "We truly understand that, let me mark this order as priority and update "
                    "the order. Meanwhile, I will ask customer support to connect with you."
                ),
            )
            return {
                "success": True,
                "escalated": True,
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "cancellation_threat",
                "priority_order": True,
            }
        except Exception as e:
            return {"success": False, "escalated": False, "error": str(e), "customer_message": "I'll connect you with our support team to resolve this urgently."}

    @staticmethod
    async def aescalate_urgent_delivery(
        order_id: str,
        urgency_count: int,
        reason: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"][-5:]
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            details = f"Customer urgently requesting faster delivery: {order_id}\nUrgency Count: {urgency_count}\nReason: {reason}"
            category = "Delivery Query"

            escalation_group = await aget_escalation_group(client_id, category, agent="order_status")
            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=reason,
                action_required=f"Contact customer urgently regarding order {order_id}",
                user_messages=user_msgs,
                metadata={"escalation_type": "urgent_delivery", "escalation_classification": "agentic", "order_id": order_id, "urgent_count": urgency_count, "trace_id": trace_id},
            )
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            from fashion_bot.agent_config import aget_escalation_customer_message

            customer_message = await aget_escalation_customer_message(
                client_id,
                default=(
                    "This seems important. I'll transfer you to a team member who will reach "
                    "out to you shortly. Thank you for your patience."
                ),
            )
            return {
                "success": True,
                "escalated": True,
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "urgent_delivery",
                "urgency_count": urgency_count,
            }
        except Exception as e:
            return {"success": False, "escalated": False, "error": str(e), "customer_message": "I'll connect you with our support team to prioritize your delivery."}

    @staticmethod
    async def aescalate_undelivered_order(order_id: str, last_message: str = "N/A", state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]

            category = "Delivery Query"
            escalation_group = "post_sales"
            details = f"Undelivered order reported by customer.\nLast message: {last_message}"

            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason="Undelivered order",
                action_required=f"Check delivery status for order {order_id}",
                user_messages=[last_message] if last_message != "N/A" else [],
                metadata={
                    "escalation_classification": "agentic",
                    "immediate_attention": True,
                    "agent": "order_status",
                    "escalation_subtype": "undelivered",
                    "order_id": order_id,
                },
            )
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            from fashion_bot.agent_config import aget_escalation_customer_message

            customer_message = await aget_escalation_customer_message(
                client_id,
                default=(
                    f"We truly apologize that you haven't received order {order_id}. I've escalated "
                    "this as an undelivered order to our support team. They will investigate and "
                    "get back to you ASAP."
                ),
            )
            return {"success": True, "escalated": True, "message": f"Undelivered order {order_id} has been escalated to our support team.", "customer_message": customer_message, "support_team_contact_details": contact_details, "escalation_type": "undelivered"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aescalate_misrouted_order(order_id: str, last_message: str = "N/A", state: Optional[Dict] = None) -> Dict[str, Any]:
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]

            category = "Delivery Query"
            escalation_group = "post_sales"
            details = f"Order appears misrouted or in exception status.\nLast message: {last_message}"

            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category="Delivery Query",
                reason="Misrouted order",
                action_required=f"Urgent: Check routing for order {order_id}",
                user_messages=[last_message] if last_message != "N/A" else [],
                metadata={
                    "escalation_classification": "agentic",
                    "immediate_attention": True,
                    "agent": "order_status",
                    "escalation_subtype": "misrouted",
                    "order_id": order_id,
                },
            )
            return {"success": True, "escalated": True, "message": f"Misrouted order {order_id} has been escalated to our logistics team.", "escalation_type": "misrouted"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aescalate_delayed_order(
        order_id: str,
        days_delayed: int = 0,
        recent_messages: list = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            details = f"Order delayed by {days_delayed} days\nOrder: {order_id}"
            category = "Order Delivery Delayed"

            escalation_group = await aget_escalation_group(client_id, category, agent="order_status")
            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=f"Order delayed by {days_delayed} days",
                action_required=f"Check delivery timeline for order {order_id}",
                user_messages=recent_messages or [],
                metadata={"escalation_type": "delayed", "escalation_classification": "agentic", "order_id": order_id, "days_delayed": days_delayed, "trace_id": trace_id},
            )
            return {"success": True, "escalated": True, "message": f"Delayed order {order_id} ({days_delayed} days) has been escalated.", "escalation_type": "delayed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aescalate_delayed_pickup(
        order_id: str,
        phone_number: str = "",
        days_since_delivery: int = 0,
        recent_messages: list = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            cust_phone = phone_number or (state.get("phone_number", "Not provided") if state else "Not provided")
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            details = f"Pickup delayed - {days_since_delivery} days since delivery\nOrder: {order_id}"
            category = "Pickup Query"

            escalation_group = await aget_escalation_group(client_id, category, agent="order_status")
            notification = build_escalation_notification(
                category=category,
                order_id=order_id,
                phone_number=cust_phone,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id,
                phone_number=cust_phone,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
                immediate_attention=True,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="order_status",
                category=category,
                immediate_attention=True,
                email_html=email_html,
                details=details,
                order_id=order_id,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason=f"Pickup delayed - {days_since_delivery} days since delivery for order {order_id}",
                action_required=f"Arrange pickup for order {order_id} - {days_since_delivery} days since delivery",
                user_messages=recent_messages or [],
                metadata={"escalation_type": "delayed_pickup", "escalation_classification": "agentic", "order_id": order_id, "phone_number": cust_phone, "days_since_delivery": days_since_delivery, "trace_id": trace_id},
            )
            contact_details = await get_contact_details_message(client_id=state.get("client_id") if state else None)
            from fashion_bot.agent_config import aget_escalation_customer_message

            customer_message = await aget_escalation_customer_message(
                client_id,
                default=(
                    f"Your pickup request for order {order_id} has been delayed "
                    f"({days_since_delivery} days since delivery). I've escalated this to our "
                    "support team for immediate attention. Someone will contact you shortly to "
                    "arrange the pickup."
                ),
            )
            return {
                "success": True,
                "escalated": True,
                "message": customer_message,
                "customer_message": customer_message,
                "support_team_contact_details": contact_details,
                "escalation_type": "delayed_pickup",
            }
        except Exception as e:
            return {"success": False, "escalated": False, "error": str(e), "message": f"ESCALATION_REQUIRED: Pickup delayed for {days_since_delivery} days. Our team will contact you shortly."}

    @staticmethod
    async def asend_escalation_whatsapp(
        message: str,
        escalation_type: str = "general",
        order_id: str = "",
        phone: str = "",
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.utils.escalation_helper import (
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            cust_phone = phone or (state.get("phone_number", "Not provided") if state else "Not provided")
            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            category = escalation_type.replace("_", " ").title() if escalation_type != "general" else "General"

            escalation_group = await aget_escalation_group(
                client_id, category, order_id=order_id or None
            )
            notification = build_escalation_notification(
                category=category,
                order_id=order_id or None,
                phone_number=cust_phone,
                trace_id=trace_id,
                details=message,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=order_id or None,
                phone_number=cust_phone,
                details=message,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else None,
                escalation_group=escalation_group,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                category=category,
                email_html=email_html,
                details=message,
                order_id=order_id or None,
                escalation_group=escalation_group,
                timestamp_ist=current_time_ist,
            )
            return {"success": True, "message": "Escalation message sent to support team.", "escalation_type": escalation_type}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def acheck_restocking_query(
        query: str,
        recent_messages: List[str] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.nodes.order_nodes import get_contact_details_message
        from fashion_bot.utils.escalation_helper import (
            alog_escalation_from_state,
            asend_escalation_notification,
            build_escalation_notification,
            build_escalation_email_html,
        )
        from fashion_bot.agent_config import aget_escalation_group
        import pytz
        from datetime import datetime

        try:
            current_time_ist = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            trace_id = get_trace_id(state) if state else None
            client_id = state.get("client_id") if state else None
            phone_number = state.get("phone_number", "Not provided") if state else "Not provided"

            # Web chat contact-collection gate: if the customer only has a
            # session ID (not a real phone), ask once for phone or email.
            from fashion_bot.utils.phone_number_utils import aresolve_contact_collection_gate
            ask_for_contact, customer_contact = await aresolve_contact_collection_gate(
                state, phone_number=phone_number, client_id=client_id, trace_id=trace_id
            )
            if ask_for_contact:
                return {
                    "escalated": False,
                    "phone_number_required": True,
                    "message": (
                        "STOP. Do NOT call any other tool. Do NOT provide contact details. "
                        "Simply ask the customer: 'Could you please share your phone number "
                        "or email address so our team can notify you when this product is back in stock?'"
                    ),
                }

            messages = state.get("messages", []) if state else []
            user_msgs = [m.content for m in messages if getattr(m, "type", "human") == "human"]
            details = f"Customer Query: {query}\n\nAction Required: Add customer to restock notification list or update on availability."
            category = "Restocking Query"

            escalation_group = await aget_escalation_group(client_id, category, agent="product_details")
            notification = build_escalation_notification(
                category=category,
                order_id=None,
                phone_number=phone_number,
                trace_id=trace_id,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
                customer_contact=customer_contact,
            )
            email_html = build_escalation_email_html(
                category=category,
                order_id=None,
                phone_number=phone_number,
                details=details,
                timestamp_ist=current_time_ist,
                recent_customer_messages=user_msgs[-3:] if user_msgs else (recent_messages[:3] if recent_messages else None),
                escalation_group=escalation_group,
                customer_contact=customer_contact,
            )
            await asend_escalation_notification(
                notification,
                client_id=client_id,
                state=state,
                agent="product_details",
                category=category,
                email_html=email_html,
                customer_contact_provided=bool(customer_contact),
                details=details,
                order_id=None,
                escalation_group=escalation_group,
                customer_contact=customer_contact,
                timestamp_ist=current_time_ist,
            )
            await alog_escalation_from_state(
                state=state,
                category=category,
                reason="Customer asking about product restocking/availability",
                action_required="Add customer to restock notification list",
                user_messages=recent_messages if recent_messages else [query],
                metadata={"escalation_type": "restocking_query", "escalation_classification": "agentic", "trace_id": trace_id, "query": query},
            )
            contact_details = await get_contact_details_message(client_id=client_id)
            return {
                "escalated": True,
                "message": "I've noted your interest in this product. Our team will notify you as soon as it's back in stock.",
                "support_team_contact_details": contact_details,
                "escalation_type": "restocking_query",
            }
        except Exception as e:
            return {
                "escalated": False,
                "error": str(e),
                "message": "I'll note your interest and have our team reach out to you about availability.",
            }


class ProductOrchestrator:
    """
    Orchestrator for product operations.
    Uses configuration-driven approach for vendor selection.
    """
    
    @staticmethod
    async def asearch_products_by_name(
        product_name: str,
        state: Optional[Dict] = None,
        limit: int = 3,
        product_line: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Native-async product search for the live request path."""
        log_with_trace_id(state, f"ProductOrchestrator: Async search for product: {product_name} (product_line={product_line})")

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            log_with_trace_id(state, f"Primary vendor for async product search: {primary_vendor}")

            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)
            if not product_service:
                log_with_trace_id(state, "❌ Product service not configured", "error")
                return {"found": False, "error": "Product service not configured"}

            products = await product_service.asearch_products_by_name(
                product_name,
                limit=limit,
                state=state,
                product_line=product_line,
            )

            if not products:
                log_with_trace_id(state, f"❌ No products found for '{product_name}'")
                return {
                    "found": False,
                    "message": f"No products found matching '{product_name}'",
                    "products": [],
                    "count": 0,
                }

            log_with_trace_id(state, f"📋 Found {len(products)} product(s) for '{product_name}'")
            return {
                "found": True,
                "products": products,
                "count": len(products),
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Async product search failed: {str(e)}", "error")
            return {"found": False, "error": str(e)}

    @staticmethod
    async def get_product_details_from_url(product_url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get comprehensive product details from a product URL.
        
        Args:
            product_url: Product URL to fetch details from
            state: Current state dictionary
            
        Returns:
            Dictionary with product details or error
        """
        from fashion_bot.tools import is_url_allowed, get_allowed_urls
        
        log_with_trace_id(state, f"ProductOrchestrator: Getting product details from URL: {product_url}")
        
        try:
            # 1. Validate domain
            if not await is_url_allowed(product_url, state=state):
                allowed_urls = await get_allowed_urls(state=state)
                url_list = ", ".join(allowed_urls) if allowed_urls else "configured websites"
                return {
                    "success": False,
                    "error": "invalid_domain",
                    "message": f"Only links related to {url_list} are supported."
                }
            
            # 2. Get product service from factory
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)
            
            if not product_service:
                return {"success": False, "error": "Product service not configured"}
            
            # 3. Fetch product details using vendor-specific adapter
            product_data = product_service.get_product_details_by_url(product_url, state=state)
            
            if not product_data:
                return {"success": False, "error": "Product not found"}
            
            return {"success": True, "product": product_data}
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Product details fetch failed: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aget_product_details_from_url(product_url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Native-async product detail fetch for the live request path."""
        from fashion_bot.tools import is_url_allowed, get_allowed_urls

        log_with_trace_id(state, f"ProductOrchestrator: Async get product details from URL: {product_url}")

        try:
            if not await is_url_allowed(product_url, state=state):
                allowed_urls = await get_allowed_urls(state=state)
                url_list = ", ".join(allowed_urls) if allowed_urls else "configured websites"
                return {
                    "success": False,
                    "error": "invalid_domain",
                    "message": f"Only links related to {url_list} are supported.",
                }

            primary_vendor = ServiceFactory.get_primary_vendor(state)
            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)

            if not product_service:
                return {"success": False, "error": "Product service not configured"}

            product_data = await product_service.aget_product_details_by_url(product_url, state=state)
            if not product_data:
                return {"success": False, "error": "Product not found"}

            return {"success": True, "product": product_data}

        except Exception as e:
            log_with_trace_id(state, f"❌ Async product details fetch failed: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def check_product_availability(product_ref: str, size: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Check if a particular size is available for a product.
        
        Args:
            product_ref: Full product URL or product handle
            size: Requested size label (e.g., 'M', '32')
            state: Current state dictionary
            
        Returns:
            Dictionary with availability info and product details
        """
        from fashion_bot.tools import is_url_allowed, get_allowed_urls
        from fashion_bot.utils.product_utils import normalize_size
        import re
        
        log_with_trace_id(state, f"ProductOrchestrator: Checking availability for {product_ref} in size {size}")
        
        try:
            # 1. Validate domain if URL
            if product_ref.startswith("http"):
                if not await is_url_allowed(product_ref, state=state):
                    allowed_urls = await get_allowed_urls(state=state)
                    url_list = ", ".join(allowed_urls) if allowed_urls else "configured websites"
                    return {
                        "success": False,
                        "error": "invalid_domain",
                        "message": f"Only links related to {url_list} are supported."
                    }
            
            # 2. Normalize size
            size_norm = normalize_size(size)
            
            # 3. Extract handle from URL or use as handle
            if product_ref.startswith("http"):
                match = re.search(r"/products/([^/?#]+)", product_ref)
                if not match:
                    return {"success": False, "error": "Could not extract product handle from URL"}
                handle = match.group(1)
            else:
                handle = product_ref.lstrip('/')
                if handle.startswith('products/'):
                    handle = handle[9:]
            
            # 4. Get product service and fetch details
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)
            
            if not product_service:
                return {"success": False, "error": "Product service not configured"}
            
            product_data = product_service.get_product_details_by_id(handle, state=state)
            
            if not product_data:
                return {"success": False, "error": "Product not found"}
            
            # 5. Check availability
            available_sizes = [s.upper() for s in product_data.get("available_sizes", [])]
            all_sizes = [s.upper() for s in product_data.get("total_sizes", [])]
            is_available = size_norm in available_sizes
            
            # Extract price information
            price_range = product_data.get("price", {})
            if isinstance(price_range, dict) and "min" in price_range:
                price_info = f"${price_range['min']}"
                if price_range.get("max") and price_range["max"] != price_range["min"]:
                    price_info += f" - ${price_range['max']}"
            else:
                price_info = str(price_range) if price_range else "N/A"
            
            return {
                "success": True,
                "product_handle": product_data.get("handle"),
                "product_name": product_data.get("name"),
                "size": size_norm,
                "available": is_available,
                "available_sizes": available_sizes,
                "all_sizes": all_sizes,
                "price": price_info,
                "total_variants": product_data.get("total_variants", 0)
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Product availability check failed: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aget_product_info_from_context(
        product_handle: str = "",
        page_url: str = "",
        cached_product: Optional[Dict] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Fetch product details by handle. Pure function — no state reads/writes.

        Args:
            product_handle: Shopify product handle to look up.
            page_url: Current page URL. Used as fallback to extract the handle
                and returned in the response as ``product_url``.
            cached_product: Previously fetched product dict. If its handle
                matches *product_handle* the API call is skipped and the
                cached data is returned immediately.
            state: Forwarded to ServiceFactory / logger for trace_id and
                client_id resolution only. This method does **not** read or
                write any product-related keys on *state*.

        Returns:
            Dict with product details (``found=True``) or error info
            (``found=False``).
        """
        import re

        log_with_trace_id(state, "ProductOrchestrator: Async get product info from context")

        effective_handle = (product_handle or "").lower().strip()

        if not effective_handle and page_url:
            match = re.search(r"/products/([^/?#]+)", page_url)
            if match:
                effective_handle = match.group(1).lower().strip()
                log_with_trace_id(state, f"📍 Extracted product handle from URL: {effective_handle}")

        if not effective_handle:
            log_with_trace_id(state, "❌ No product handle provided or extractable")
            return {"found": False, "message": "Could not identify product. No handle provided."}

        def _normalize(h: str) -> str:
            return re.sub(r"[^a-z0-9]", "", str(h).lower()) if h else ""

        if cached_product and isinstance(cached_product, dict):
            existing_handle = (
                cached_product.get("handle")
                or cached_product.get("product_handle")
                or cached_product.get("slug")
                or ""
            ).lower().strip()

            if _normalize(existing_handle) == _normalize(effective_handle):
                log_with_trace_id(state, f"✅ Using cached product info: {cached_product.get('name', 'Unknown')} (handle: {existing_handle})")
                return {
                    "found": True,
                    "source": "cached",
                    "product": cached_product,
                    "name": cached_product.get("name") or cached_product.get("title", "Unknown"),
                    "available_sizes": cached_product.get("available_sizes") or cached_product.get("sizes_in_stock", []),
                    "size_guide": cached_product.get("size_guide", {}),
                    "fit_type": cached_product.get("fit_type", "Regular fit"),
                    "fabric": cached_product.get("fabric", "Not specified"),
                    "product_url": page_url,
                }

            log_with_trace_id(state, f"⚠️ Cache stale: cached='{existing_handle}' vs requested='{effective_handle}'")

        try:
            log_with_trace_id(state, f"📦 Async fetching product info for handle: {effective_handle}")
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)

            if not product_service:
                return {"found": False, "error": "Product service not configured"}

            product_data = await product_service.aget_product_details_by_id(effective_handle, state=state)
            if not product_data or (isinstance(product_data, dict) and not product_data.get("name") and not product_data.get("title")):
                log_with_trace_id(state, f"❌ Product not found or empty for handle: {effective_handle}")
                return {"found": False, "message": f"Product '{effective_handle}' details are not available."}

            log_with_trace_id(state, f"✅ Fetched product: {product_data.get('name', 'Unknown')}")
            return {
                "found": True,
                "source": "fetched",
                "product": product_data,
                "name": product_data.get("name") or product_data.get("title", "Unknown"),
                "handle": product_data.get("handle"),
                "available_sizes": product_data.get("available_sizes", []),
                "total_sizes": product_data.get("total_sizes", []),
                "size_guide": product_data.get("size_guide", {}),
                "fit_type": product_data.get("fit_type", "Regular fit"),
                "fabric": product_data.get("fabric", product_data.get("material", "Not specified")),
                "price": product_data.get("price", {}),
                "product_url": page_url,
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching product from async context: {str(e)}", "error")
            report_error(
                "Error fetching product from async context",
                level="error",
                exc_info=(type(e), e, e.__traceback__),
                product_handle=effective_handle,
            )
            return {"found": False, "error": str(e)}

    @staticmethod
    def _bestseller_doc_to_product(result: Dict[str, Any]) -> Dict[str, Any]:
        """Map an Upstash Search document to the product shape returned by the
        live top-selling path, so downstream normalisation/carousel code is
        unaffected. Field origins: ``ProductDocument.to_search_document``."""
        content = result.get("content", {}) or {}
        metadata = result.get("metadata", {}) or {}
        price_min = content.get("price_min")
        price_max = content.get("price_max")
        return {
            "id": metadata.get("product_id") or result.get("id", ""),
            "product_id": metadata.get("product_id", ""),
            "title": content.get("title", ""),
            "handle": content.get("handle") or metadata.get("handle", ""),
            "vendor": content.get("brand", ""),
            "category": content.get("category", ""),
            "tags": content.get("tags", []),
            "price": {"min": price_min, "max": price_max} if price_min is not None else {},
            "compare_at_price_min": content.get("compare_at_price_min"),
            "discount_pct": content.get("discount_pct"),
            "image": metadata.get("image_url", ""),
            "url": metadata.get("product_url", ""),
            "description": content.get("description", ""),
            "all_sizes": content.get("sizes", []),
            "available_sizes": content.get("available_sizes", []),
            "colors": content.get("colors", []),
            "color_family": content.get("color_family", ""),
            "variants": metadata.get("variants", []),
            "total_inventory": content.get("total_inventory", 0),
            "in_stock": content.get("in_stock", True),
            "created_at": content.get("created_at") or metadata.get("created_at"),
            "updated_at": metadata.get("updated_at"),
            "status": "active",
            "bestseller": True,
        }

    @staticmethod
    async def _aget_top_selling_via_bestseller_index(
        top_n: int, client_id: str, state: Optional[Dict] = None,
        collection_hint: Optional[str] = None,
        category_hint: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fast path: read pre-computed bestsellers from the Upstash Search index.

        The ``bestseller`` flag is tagged offline during product ingestion
        (``ShopifyProductService._tag_bestsellers``), so this avoids the live,
        multi-second Shopify Orders pagination scan. Returns ``[]`` when no
        bestsellers are tagged yet (e.g. a brand-new store), letting the caller
        fall back to the live orders scan.

        ``category_hint`` (e.g. "denims") scopes the bestseller list to a product
        category the customer is browsing/asking about (e.g. "best selling
        denims") via semantic ranking over the tagged bestsellers — the same
        mechanism as ``collection_hint`` (e.g. "denim jeans"), which scopes to
        the collection page being viewed. ``category_hint`` takes precedence.
        When both are absent, the lookup is catalog-wide.
        """
        from fashion_bot.services.product_ingestion.upstash_search_service import (
            get_upstash_search_service,
        )

        search_service = get_upstash_search_service()
        # Over-fetch so the zero-price/freebie filter can't starve the result set.
        fetch_limit = max(top_n * 3, 10)
        query = category_hint or collection_hint or "popular bestselling products"
        results = await search_service.asearch(
            query=query,
            client_id=client_id,
            filter_str="bestseller = true AND in_stock = true",
            limit=fetch_limit,
        )

        # Drop zero-price freebies (e.g. gift items) — not purchasable, and must
        # never surface as "top selling" products.
        def _is_purchasable(r: Dict[str, Any]) -> bool:
            try:
                return float((r.get("content", {}) or {}).get("price_min") or 0) > 0
            except (TypeError, ValueError):
                return False

        results = [r for r in results if _is_purchasable(r)][:top_n]
        return [ProductOrchestrator._bestseller_doc_to_product(r) for r in results]

    @staticmethod
    async def aget_top_selling(
        top_n: int = 5, state: Optional[Dict] = None, category: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Native-async top-selling lookup for the live request path.

        Prefers the pre-computed ``bestseller`` flag in the Upstash Search index
        (sub-second) and only falls back to the live Shopify Orders scan when no
        bestsellers are tagged yet.

        ``category`` (e.g. "denims") scopes the result to a single product
        category — used for category-scoped requests like "show me bestselling
        denims" so the carousel stays on-topic instead of returning catalog-wide
        top sellers.
        """
        log_with_trace_id(state, f"ProductOrchestrator: Async get top {top_n} selling products")
        category_hint = (category or "").strip() or None

        # --- Fast path: pre-computed bestsellers from the Upstash Search index ---
        try:
            client_id = state.get("client_id") if state else None
            if not client_id:
                from fashion_bot.client_context import get_client_id
                client_id = get_client_id()
            if client_id:
                # Scope to the collection the customer is browsing (web chat only),
                # mirroring the search_products tool. WhatsApp never sets
                # current_page_type, so this stays catalog-wide there. An explicit
                # category (from the request) takes precedence over the page's
                # collection.
                collection_hint = None
                if state and state.get("current_page_type") == "collection":
                    from fashion_bot.services.recommendation.query_understanding import (
                        collection_handle_to_search_hint,
                    )
                    collection_hint = collection_handle_to_search_hint(
                        state.get("current_collection_handle")
                    ) or None
                bestsellers = await ProductOrchestrator._aget_top_selling_via_bestseller_index(
                    top_n=top_n, client_id=client_id, state=state,
                    collection_hint=collection_hint,
                    category_hint=category_hint,
                )
                if bestsellers:
                    scope_hint = category_hint or collection_hint
                    log_with_trace_id(
                        state,
                        f"✅ Top selling via bestseller index: {len(bestsellers)} products"
                        + (f" (scope='{scope_hint}')" if scope_hint else ""),
                    )
                    return {"success": True, "products": bestsellers, "count": len(bestsellers)}
                # A category-scoped request (e.g. "bestselling denims") must NOT
                # fall back to the catalog-wide live orders scan — that would
                # surface off-category products. Return empty so the agent can
                # search/answer instead.
                if category_hint:
                    log_with_trace_id(
                        state,
                        f"ℹ️ No bestsellers in index for category='{category_hint}' — skipping catalog-wide fallback",
                    )
                    return {"success": True, "products": [], "count": 0}
                log_with_trace_id(
                    state,
                    "ℹ️ No bestsellers tagged in search index — falling back to live orders scan",
                )
        except Exception as e:
            log_with_trace_id(
                state,
                f"⚠️ Bestseller-index top selling failed ({e}) — falling back to live orders scan",
                "warning",
            )

        # --- Fallback: live Shopify Orders analysis ---
        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            log_with_trace_id(state, f"Primary vendor for async top selling: {primary_vendor}")

            product_service = await ServiceFactory.aget_product_service(state=state, vendor=primary_vendor)
            if not product_service:
                log_with_trace_id(state, "❌ Product service not configured", "error")
                return {"success": False, "error": "Product service not configured", "products": []}

            products = await product_service.aget_top_selling_products(limit=top_n, state=state)
            if not products:
                return {"success": True, "products": [], "message": "No top selling products found"}

            return {"success": True, "products": products, "count": len(products)}

        except Exception as e:
            log_with_trace_id(state, f"❌ Async top selling products fetch failed: {str(e)}", "error")
            return {"success": False, "error": str(e), "products": []}

    @staticmethod
    async def get_recommendations(gender: str = None, category: str = None, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get product recommendations from database configuration.
        
        Args:
            gender: Optional gender filter (men, women, unisex)
            category: Optional category filter (hoodies, jeans, t-shirts, etc.)
            state: Current state dictionary
            
        Returns:
            Dictionary with recommendations data
        """
        log_with_trace_id(state, f"ProductOrchestrator: Getting recommendations (gender={gender}, category={category})")
        
        try:
            from fashion_bot.config_manager import aget_json_config
            from fashion_bot.client_context import get_client_id
            
            # Get client_id from context or state
            client_id = state.get("client_id") if state else None
            if not client_id:
                client_id = get_client_id()
            
            # Fetch recommendations data from database
            recommendations_data = await aget_json_config("recommendations", client_id)
            
            if not recommendations_data:
                log_with_trace_id(state, "⚠️ No recommendations data found in database", "warning")
                return {
                    "success": False,
                    "error": "No recommendations configuration found",
                    "message": "Recommendations data not available"
                }
            
            # Filter by gender and category if provided
            filtered_recommendations = recommendations_data
            
            # Apply gender filter
            if gender and isinstance(recommendations_data, dict):
                if "genders" in recommendations_data:
                    gender_lower = gender.lower()
                    filtered_recommendations = {
                        k: v for k, v in recommendations_data.items()
                        if k == "genders" or gender_lower in str(v).lower()
                    }
            
            # Apply category filter
            if category and isinstance(recommendations_data, dict):
                if "categories" in recommendations_data:
                    category_lower = category.lower()
                    filtered_recommendations = {
                        k: v for k, v in filtered_recommendations.items()
                        if k == "categories" or category_lower in str(v).lower()
                    }
            
            log_with_trace_id(state, f"✅ Retrieved recommendations data")
            return {
                "success": True,
                "data": filtered_recommendations,
                "message": "Retrieved product recommendations"
            }
        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching recommendations: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e),
                "message": "Error retrieving recommendations configuration"
            }


class UpdateStrategyResolution:
    """Result of ``OrderUpdateOrchestrator._aresolve_strategy_for_order``.

    Attributes:
        strategy: One of the ``STRATEGY_*`` constants on
            ``OrderUpdateOrchestrator`` describing the update path:
            ``STRATEGY_UPDATE_INPLACE``, ``STRATEGY_ESCALATE_WITH_PARTNERS``,
            or ``STRATEGY_CANCEL_AND_RECREATE``.
        partner: The single canonical delivery partner name this resolution
            applies to when the order's carrier could be deduced (URL-first
            from ``tracking_url``, alias fallback from ``tracking_company``).
            ``None`` for NEW/unfulfilled orders or when the carrier is unknown.
        partners_to_escalate: Partner names that need a manual dashboard
            verification after the API update.  Populated when
            ``strategy == STRATEGY_ESCALATE_WITH_PARTNERS``.  For per-order
            resolutions this is a single-element list ``[partner]``; for the
            legacy aggregate path used by NEW orders it lists every connected
            integrated partner configured for escalation.
    """

    __slots__ = ("strategy", "partner", "partners_to_escalate")

    def __init__(
        self,
        strategy: str,
        partner: Optional[str] = None,
        partners_to_escalate: Optional[List[str]] = None,
    ) -> None:
        self.strategy = strategy
        self.partner: Optional[str] = partner
        self.partners_to_escalate: List[str] = partners_to_escalate or []

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"UpdateStrategyResolution(strategy={self.strategy!r}, "
            f"partner={self.partner!r}, "
            f"partners_to_escalate={self.partners_to_escalate!r})"
        )


class OrderUpdateOrchestrator:
    """
    Orchestrator for order update operations.
    
    Uses vendor-agnostic ServiceFactory and VendorConfigManager abstractions.
    Does NOT contain status comparison, vendor-specific variable names, or
    business rule logic. Status resolution is handled by
    OrderStatusOrchestrator.aget_order_status() and the LLM prompt's
    decision matrix.
    
    Routing logic (executed by ServiceFactory):
    - NEW order → update primary vendor + integrated logistics partners + note.
    - Dispatched + integrated partner → update primary vendor + logistics.
    - Dispatched + non-integrated partner → note only + escalation.
    - Cancelled/Voided → terminal state, return error.
    """
    
    @staticmethod
    def _get_client_id(state: Optional[Dict] = None) -> Optional[str]:
        """Extract client_id from state."""
        if not state:
            return None
        client_id = state.get("client_id")
        if not client_id:
            context = state.get("context", {})
            client_id = context.get("client_id")
        return client_id

    @staticmethod
    async def enrich_with_delivery_partner_info(
        result: dict,
        state: Optional[Dict] = None,
        order_dto: Optional[Dict] = None,
    ) -> dict:
        """Check for non-integrated delivery partners and auto-trigger sync
        notification if any are found.  The escalation runs silently — it
        sends a WhatsApp message to the client's agent but does NOT surface
        anything to the customer.

        Also fires a per-partner manual-sync escalation for any integrated
        partner whose ``order_update_strategy`` is ``escalate_for_manual_update``
        (the default for limited-API partners such as Delhivery).  This covers
        the gap where a partner's edit API cannot reach pre-AWB orders — the
        agent verifies the change via the partner dashboard.
        ``cancel_and_recreate`` strategy short-circuits this branch because
        cloned orders bypass partner APIs entirely (Shopify auto-sync handles it).

        When ``order_dto`` is provided, escalation uses **per-order resolution**
        — the actual carrier of THIS order determines whether to escalate.
        Without it, falls back to aggregate strategy (escalates if ANY connected
        partner is configured for ``escalate_for_manual_update``).
        """
        if not isinstance(result, dict) or not result.get("success"):
            return result

        # Robust order_id extraction — size updates and cancel-and-
        # recreate flows return ``old_order_id`` / ``new_order_id`` and
        # don't set the canonical ``order_id`` key. Both escalations
        # below need a non-empty order_id; falling back through
        # new_order_id → old_order_id covers those flows. Without this,
        # the size-change escalation in particular emits "Order: " and
        # "Order ID: Unknown" because result["order_id"] is missing.
        order_id_for_escalation = _resolve_result_order_id(result)

        try:
            from fashion_bot.utils.delivery_utils import aget_non_integrated_partners
            client_id = OrderUpdateOrchestrator._get_client_id(state)
            partners = await aget_non_integrated_partners(client_id)
            if partners:
                result["delivery_partner_sync_required"] = partners
                action_details = result.get("message", "Order updated")
                try:
                    sync_result = await EscalationOrchestrator.aescalate_to_agent(
                        category="Courier Update Pending",
                        reason=(
                            f"Non-integrated partners {partners} need sync "
                            f"for order {order_id_for_escalation}"
                        ),
                        details=action_details,
                        state=state,
                        order_id=order_id_for_escalation,
                        escalation_classification="system",
                    )
                    result["delivery_partner_sync_notified"] = sync_result.get("notified", False)
                    log_with_trace_id(state, f"📤 Auto-triggered Courier Update Pending for {partners} on order {order_id_for_escalation}")
                except Exception as sync_exc:
                    log_with_trace_id(state, f"⚠️ Failed to auto-trigger Courier Update Pending: {sync_exc}", "warning")
        except Exception as exc:
            log_with_trace_id(state, f"⚠️ Failed to enrich Courier Update Pending info: {exc}", "warning")

        # Per-partner manual-sync escalation for partners configured with
        # ``escalate_for_manual_update`` (the default for limited-API partners
        # like Delhivery). Skipped for ``cancel_and_recreate`` because those
        # flows produce a fresh Shopify order that auto-syncs to all partners —
        # there is nothing to manually verify on any partner dashboard.
        try:
            # Per-order resolution when ``order_dto`` is supplied; aggregate
            # fallback otherwise.  Per-order keeps escalations targeted to the
            # actual carrier of THIS order rather than every connected partner.
            resolution = await OrderUpdateOrchestrator._aresolve_strategy_for_order(
                state=state, order_dto=order_dto,
            )
            if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_ESCALATE_WITH_PARTNERS:
                # Don't double-escalate if the result already came from
                # cancel-and-recreate (defensive against future config drift).
                if result.get("method") != "cancel_and_recreate":
                    update_type = (
                        result.get("update_type")
                        or _infer_update_type_from_result(result)
                    )
                    change_summary = result.get("message", "")
                    # Per-order resolution carries a single partner; aggregate
                    # carries the full list.  Both flow into the same escalation.
                    partners_to_escalate = (
                        resolution.partners_to_escalate
                        or ([resolution.partner] if resolution.partner else [])
                    )
                    await OrderUpdateOrchestrator._aescalate_partners_manual_sync(
                        order_id=order_id_for_escalation,
                        update_type=update_type,
                        change_summary=change_summary,
                        partners=partners_to_escalate,
                        state=state,
                    )
                    result["partners_manual_sync_escalated"] = partners_to_escalate
        except Exception as exc:
            log_with_trace_id(
                state,
                f"⚠️ Failed to fire partner manual-sync escalation: {exc}",
                "warning",
            )
        return result

    # Resolved strategy modes returned by ``_aresolve_strategy_for_order``.
    # See ``config_manager.ORDER_UPDATE_STRATEGY_*`` for the raw config
    # values; this enum-like alias is what callers actually branch on.
    STRATEGY_UPDATE_INPLACE = "update_inplace"
    STRATEGY_CANCEL_AND_RECREATE = "cancel_and_recreate"
    # Generic name: any partner(s) with limited edit APIs need manual sync.
    STRATEGY_ESCALATE_WITH_PARTNERS = "escalate_with_partners"

    # Backward-compat aliases — external code that imported these constants
    # still works.  ``STRATEGY_LEGACY`` was the historical name for "no special
    # handling" which is now ``STRATEGY_UPDATE_INPLACE``.
    STRATEGY_LEGACY = STRATEGY_UPDATE_INPLACE
    STRATEGY_ESCALATE_WITH_DELHIVERY = STRATEGY_ESCALATE_WITH_PARTNERS

    @staticmethod
    async def _aresolve_strategy_for_order(
        state: Optional[Dict] = None,
        order_dto: Optional[Dict[str, Any]] = None,
    ) -> "UpdateStrategyResolution":
        """Resolve the effective update strategy for an order.

        **Per-order resolution (preferred path)** — when ``order_dto`` is
        provided AND the order is fulfilled (post-Shopify-fulfillment), the
        order's actual carrier is deduced via
        :func:`utils.delivery_partner_utils.aresolve_effective_partner_for_order`
        (URL-first, then ``tracking_company`` alias).  The strategy returned is
        whatever ``client_configs.order_update_strategy`` maps that single
        carrier to.  Adding a new partner = add a row to the JSON map; no code
        changes needed.

        **Aggregate fallback** — for NEW (unfulfilled) orders or when no
        ``order_dto`` is available, the function aggregates strategies across
        ALL connected integrated partners (the historical behaviour).  This is
        intentional: a NEW order has no carrier yet, so we can't pick one.
        Most-aggressive wins (C&R > escalate > inplace) so configurations like
        ``{"delhivery": "cancel_and_recreate"}`` still apply pre-fulfillment.

        Reads ``client_configs.order_update_strategy`` via
        :func:`config_manager.aget_partner_update_strategy`.  The config value
        may be a JSONB dict (recommended, per-partner) or a legacy plain string
        (applies uniformly):

            JSON:   ``{"delhivery": "cancel_and_recreate", "shiprocket": "update_inplace"}``
            String: ``"cancel_and_recreate"``  |  ``"escalate_for_manual_update"``

        Returns:
            ``UpdateStrategyResolution`` with:
              - ``strategy`` — one of ``STRATEGY_*`` constants (see class).
              - ``partner`` — the single carrier this resolution applies to,
                or ``None`` for the aggregate path.
              - ``partners_to_escalate`` — non-empty only when
                ``strategy == STRATEGY_ESCALATE_WITH_PARTNERS``.
        """
        from fashion_bot.config_manager import (
            aget_partner_update_strategy,
            ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE,
            ORDER_UPDATE_STRATEGY_ESCALATE,
            ORDER_UPDATE_STRATEGY_UPDATE_INPLACE,
        )

        _INPLACE = UpdateStrategyResolution(
            strategy=OrderUpdateOrchestrator.STRATEGY_UPDATE_INPLACE
        )

        client_id = OrderUpdateOrchestrator._get_client_id(state)
        if not client_id:
            return _INPLACE

        # ── Per-order resolution path ────────────────────────────────────
        # Only kick in when we have an order_dto AND the order is past the
        # NEW state (i.e. fulfilled / dispatched / has a carrier signal).
        # NEW orders have no carrier so they fall through to the aggregate
        # path below by design (see docstring).
        is_new_order = (
            order_dto is not None
            and OrderStatusOrchestrator._is_order_new(order_dto)
        )
        if order_dto and not is_new_order:
            try:
                from fashion_bot.utils.delivery_partner_utils import (
                    aresolve_effective_partner_for_order,
                )
                canonical, is_integrated = (
                    await aresolve_effective_partner_for_order(
                        order_dto, state=state,
                    )
                )
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"⚠️ Could not resolve effective partner for order "
                    f"({exc}); falling back to aggregate strategy",
                    "warning",
                )
                canonical, is_integrated = None, False

            if canonical and is_integrated:
                try:
                    ps = await aget_partner_update_strategy(
                        canonical, client_id=client_id,
                    )
                except Exception as exc:
                    log_with_trace_id(
                        state,
                        f"⚠️ Could not read strategy for partner={canonical} "
                        f"({exc}); treating as inplace",
                        "warning",
                    )
                    return UpdateStrategyResolution(
                        strategy=OrderUpdateOrchestrator.STRATEGY_UPDATE_INPLACE,
                        partner=canonical,
                    )

                if ps == ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE:
                    return UpdateStrategyResolution(
                        strategy=OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE,
                        partner=canonical,
                    )
                if ps == ORDER_UPDATE_STRATEGY_ESCALATE:
                    return UpdateStrategyResolution(
                        strategy=OrderUpdateOrchestrator.STRATEGY_ESCALATE_WITH_PARTNERS,
                        partner=canonical,
                        partners_to_escalate=[canonical],
                    )
                # ORDER_UPDATE_STRATEGY_UPDATE_INPLACE or unknown → inplace.
                return UpdateStrategyResolution(
                    strategy=OrderUpdateOrchestrator.STRATEGY_UPDATE_INPLACE,
                    partner=canonical,
                )

        # ── Aggregate fallback (NEW orders or no order_dto) ──────────────
        try:
            from fashion_bot.utils.delivery_partner_utils import aget_integrated_partners
            integrated = await aget_integrated_partners(
                client_id=client_id, state=state,
            )
        except Exception as exc:
            log_with_trace_id(
                state,
                f"⚠️ Could not list integrated partners while resolving "
                f"update strategy ({exc}); falling back to INPLACE",
                "warning",
            )
            return _INPLACE

        if not integrated:
            return _INPLACE

        cancel_recreate_partners: List[str] = []
        escalate_partners: List[str] = []

        for partner in integrated:
            try:
                ps = await aget_partner_update_strategy(partner, client_id=client_id)
            except Exception as exc:
                log_with_trace_id(
                    state,
                    f"⚠️ Could not read strategy for partner={partner} ({exc}); "
                    f"treating as inplace",
                    "warning",
                )
                continue  # skip — don't assume escalation on read failure
            if ps == ORDER_UPDATE_STRATEGY_CANCEL_AND_RECREATE:
                cancel_recreate_partners.append(partner)
            elif ps == ORDER_UPDATE_STRATEGY_ESCALATE:
                escalate_partners.append(partner)
            # else: ORDER_UPDATE_STRATEGY_UPDATE_INPLACE → no special handling.

        if cancel_recreate_partners:
            # C&R is Shopify-level: the cloned order auto-syncs to all connected
            # partners, so no per-partner escalation is needed.
            return UpdateStrategyResolution(
                strategy=OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE,
                partner=None,
                partners_to_escalate=[],
            )

        if escalate_partners:
            return UpdateStrategyResolution(
                strategy=OrderUpdateOrchestrator.STRATEGY_ESCALATE_WITH_PARTNERS,
                partner=None,
                partners_to_escalate=escalate_partners,
            )

        return _INPLACE

    # Backward-compat: callers that still use ``_aresolve_update_strategy(state)``
    # get the aggregate behaviour (no order_dto = aggregate path).  New code
    # should call ``_aresolve_strategy_for_order(state, order_dto=...)`` so the
    # order's actual carrier drives the strategy.
    @staticmethod
    async def _aresolve_update_strategy(
        state: Optional[Dict] = None,
    ) -> "UpdateStrategyResolution":
        """Deprecated alias — calls :meth:`_aresolve_strategy_for_order` with
        no order_dto, producing the aggregate (tenant-wide) resolution.

        Kept for callers that haven't migrated to per-order resolution yet.
        """
        return await OrderUpdateOrchestrator._aresolve_strategy_for_order(
            state=state, order_dto=None,
        )

    @staticmethod
    async def _aescalate_partners_manual_sync(
        order_id: str,
        update_type: str,
        change_summary: str,
        partners: List[str],
        state: Optional[Dict] = None,
    ) -> None:
        """Fire a manual-sync escalation for partners whose edit APIs cannot
        confirm the update (e.g. Delhivery pre-AWB, or any other limited-API
        partner configured with ``escalate_for_manual_update``).

        A single combined escalation is sent listing all affected partners so
        the operations team can verify each one via their respective dashboards.

        Best-effort — failure must not break the customer-facing update result.
        Defensive against an empty/None order_id: the message still reads
        cleanly with a placeholder.
        """
        display_order_id = str(order_id) if order_id else "<missing>"
        order_phrase = (
            f"order {display_order_id}"
            if display_order_id != "<missing>"
            else "this order (order_id missing — check trace)"
        )
        partner_list = ", ".join(p.title() for p in partners) if partners else "unknown partner"
        try:
            await EscalationOrchestrator.aescalate_to_agent(
                category="Order Update - Manual Partner Sync",
                reason=(
                    f"Order {display_order_id}: {update_type} updated — "
                    f"verify {partner_list} picked up the change"
                ),
                details=(
                    f"Customer updated {update_type} on {order_phrase}: "
                    f"{change_summary}. Shopify was updated; Shiprocket (if "
                    f"connected) was updated via API. {partner_list} may not "
                    f"support pre-AWB edits via API — please confirm via the "
                    f"{partner_list} dashboard(s) that the new {update_type} "
                    f"reaches the AWB once it is generated."
                ),
                state=state,
                order_id=display_order_id,
                escalation_classification="system",
            )
        except Exception as exc:
            log_with_trace_id(
                state,
                f"⚠️ Failed to escalate partner manual sync for {order_id} "
                f"({partner_list}): {exc}",
                "warning",
            )

    @staticmethod
    async def _aescalate_delhivery_manual_sync(
        order_id: str,
        update_type: str,
        change_summary: str,
        state: Optional[Dict] = None,
    ) -> None:
        """Deprecated: calls ``_aescalate_partners_manual_sync`` with partners=[\"delhivery\"].

        Retained for backward-compatibility. New code should call
        ``_aescalate_partners_manual_sync`` directly with the full partner list
        from ``UpdateStrategyResolution.partners_to_escalate``.
        """
        await OrderUpdateOrchestrator._aescalate_partners_manual_sync(
            order_id=order_id,
            update_type=update_type,
            change_summary=change_summary,
            partners=["delhivery"],
            state=state,
        )

    @staticmethod
    async def aupdate_order(
        order_id: str,
        update_type: str,
        new_address: Optional[str] = None,
        delivery_instructions: Optional[str] = None,
        new_phone: Optional[str] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async updating order {order_id} type={update_type}")
        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)
            if not order_service:
                return {"success": False, "error": "Order service not configured", "order_id": order_id}

            raw_order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            )
            if not raw_order_data:
                return {"success": False, "error": f"Order {order_id} not found", "order_id": order_id}

            order_dto = processor.process_order(raw_order_data, state=state, source=primary_vendor)
            if OrderStatusOrchestrator._is_order_cancelled_or_voided(order_dto):
                return {
                    "success": False,
                    "error": f"Order {order_id} is cancelled or voided and cannot be updated",
                    "order_id": order_id,
                    "status": order_dto.get("status"),
                }

            # ── Strategy branch: per-order resolution.
            # ``_aresolve_strategy_for_order`` deduces the order's actual carrier
            # (URL-first from ``tracking_url``, alias fallback from
            # ``tracking_company``) and looks up that single partner's strategy
            # from ``client_configs.order_update_strategy``. NEW orders fall
            # back to aggregate strategy across all connected partners.
            # ``delivery_instructions`` is metadata-only on Shopify (no
            # logistics partner sees it), so cancel-and-recreate is not
            # applicable to that update_type — fall through to inplace path.
            resolution = await OrderUpdateOrchestrator._aresolve_strategy_for_order(
                state=state, order_dto=order_dto,
            )
            if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE and (
                new_address or new_phone
            ):
                if new_address:
                    cnr_overrides = {
                        "shipping_address": _aupdate_order_parse_address(new_address)
                    }
                    cnr_update_type = CancelAndRecreateOrchestrator.UPDATE_TYPE_ADDRESS
                else:
                    cnr_overrides = {"new_phone": new_phone}
                    cnr_update_type = CancelAndRecreateOrchestrator.UPDATE_TYPE_PHONE
                return await CancelAndRecreateOrchestrator.aupdate_via_clone(
                    order_id=order_id,
                    update_type=cnr_update_type,
                    overrides=cnr_overrides,
                    state=state,
                )

            update_data = {
                "update_type": update_type,
                "new_address": new_address,
                "delivery_instructions": delivery_instructions,
                "new_phone": new_phone,
            }

            update_description = f"{update_type} update"
            if new_address:
                update_description = f"Address update to: {new_address[:80]}..."
            elif new_phone:
                update_description = f"Phone update to: {new_phone}"
            elif delivery_instructions:
                update_description = f"Delivery instructions: {delivery_instructions[:80]}..."

            results = {
                "order_id": order_id,
                "primary_vendor_update": {"success": False},
                "logistics_update": {"success": False},
                "note_added": False,
                "escalation": None,
                "routing": "",
            }

            if OrderStatusOrchestrator._is_order_new(order_dto):
                results["routing"] = "new_order"
                primary_result = await order_service.aupdate_order(order_id, update_data, state=state)
                results["primary_vendor_update"] = primary_result

                integrated_providers = await AsyncVendorConfigManager.aget_integrated_providers_list(state=state)
                if integrated_providers:
                    try:
                        # Multi-partner-aware: route through LogisticsRouter
                        # so EVERY connected integrated partner gets the
                        # update, not just the priority-0 partner returned
                        # by the single-partner factory. Falls back to the
                        # legacy single-partner path if the router can't
                        # find any candidate (e.g. tracking_company resolves
                        # to a non-integrated alias that excludes both).
                        from fashion_bot.core.logistics_router import LogisticsRouter

                        if update_type == "address" and new_address:
                            routed = await LogisticsRouter.aupdate_address_best_effort(
                                order_id,
                                {"address": new_address},
                                order_dto or {},
                                state=state,
                            )
                            if routed.get("success"):
                                winners = routed.get("winning_partners") or []
                                results["logistics_update"] = {
                                    "success": True,
                                    "winning_partners": winners,
                                    "per_partner": routed.get("per_partner", {}),
                                    "message": (
                                        f"Address updated in logistics service "
                                        f"({', '.join(winners)})"
                                    ),
                                }
                            else:
                                logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                                results["logistics_update"] = await logistics_service.aupdate_shipment_address(
                                    order_id,
                                    {"address": new_address},
                                    state=state,
                                )
                        elif update_type == "phone" and new_phone:
                            routed = await LogisticsRouter.aupdate_phone_best_effort(
                                order_id, new_phone, order_dto or {}, state=state,
                            )
                            if routed.get("success"):
                                winners = routed.get("winning_partners") or []
                                results["logistics_update"] = {
                                    "success": True,
                                    "winning_partners": winners,
                                    "per_partner": routed.get("per_partner", {}),
                                    "message": (
                                        f"Phone updated in logistics service "
                                        f"({', '.join(winners)})"
                                    ),
                                }
                            else:
                                logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                                results["logistics_update"] = await logistics_service.aupdate_shipment_phone(
                                    order_id,
                                    new_phone,
                                    state=state,
                                )
                        else:
                            results["logistics_update"] = {"success": True, "message": "No logistics update needed for this update type"}
                    except Exception as exc:
                        results["logistics_update"] = {"success": False, "error": str(exc)}

                try:
                    note = f"[Bloomerce] Order updated by customer request. Type: {update_type}. Details: {update_description}"
                    await order_service.aadd_order_note(order_id, note, state=state)
                    results["note_added"] = True
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add update note: {exc}", "warning")

                results["success"] = primary_result.get("success", False)
                if results["success"]:
                    results["message"] = f"Order {order_id} updated successfully"
                    from fashion_bot.utils.order_utils import astamp_bloomerce_edited
                    await astamp_bloomerce_edited(
                        order_service, order_id, update_type, state=state, order_data=raw_order_data,
                    )
                else:
                    results["error"] = primary_result.get("error", "Update failed")
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    results, state, order_dto=order_dto,
                )

            tracking_company = order_dto.get("tracking_company", "")
            tracking_url = order_dto.get("tracking_url", "")
            # URL-first: tracking_url is the authoritative platform signal.
            # Shiprocket can dispatch via Delhivery, writing tracking_company=
            # 'Delhivery', but the tracking_url will contain 'shiprocket'.
            from fashion_bot.core.vendor_config import resolve_partner_from_url
            effective_partner = resolve_partner_from_url(tracking_url) or tracking_company
            is_integrated = await AsyncVendorConfigManager.ais_integrated_delivery_partner(effective_partner, state=state)
            if is_integrated:
                results["routing"] = "dispatched_integrated"
                primary_result = await order_service.aupdate_order(order_id, update_data, state=state)
                results["primary_vendor_update"] = primary_result
                try:
                    # Multi-partner-aware via LogisticsRouter. When the order
                    # has a definite tracking_company, the router will only
                    # call that partner; when it doesn't, every connected
                    # integrated partner is updated. Either way, we no
                    # longer silently skip Delhivery (or any other
                    # connected integrated partner) just because Shiprocket
                    # has priority 0.
                    from fashion_bot.core.logistics_router import LogisticsRouter

                    if update_type == "address" and new_address:
                        routed = await LogisticsRouter.aupdate_address_best_effort(
                            order_id,
                            {"address": new_address},
                            order_dto or {},
                            state=state,
                        )
                        if routed.get("success"):
                            winners = routed.get("winning_partners") or []
                            results["logistics_update"] = {
                                "success": True,
                                "winning_partners": winners,
                                "per_partner": routed.get("per_partner", {}),
                                "message": (
                                    f"Address updated in logistics service "
                                    f"({', '.join(winners)})"
                                ),
                            }
                        else:
                            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                            results["logistics_update"] = await logistics_service.aupdate_shipment_address(
                                order_id,
                                {"address": new_address},
                                state=state,
                            )
                    elif update_type == "phone" and new_phone:
                        routed = await LogisticsRouter.aupdate_phone_best_effort(
                            order_id, new_phone, order_dto or {}, state=state,
                        )
                        if routed.get("success"):
                            winners = routed.get("winning_partners") or []
                            results["logistics_update"] = {
                                "success": True,
                                "winning_partners": winners,
                                "per_partner": routed.get("per_partner", {}),
                                "message": (
                                    f"Phone updated in logistics service "
                                    f"({', '.join(winners)})"
                                ),
                            }
                        else:
                            logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                            results["logistics_update"] = await logistics_service.aupdate_shipment_phone(
                                order_id,
                                new_phone,
                                state=state,
                            )
                    else:
                        results["logistics_update"] = {"success": True, "message": "No logistics update needed"}
                except Exception as exc:
                    results["logistics_update"] = {"success": False, "error": str(exc)}

                try:
                    note = f"[Bloomerce] Dispatched order updated. Partner: {tracking_company}. Type: {update_type}. Details: {update_description}"
                    await order_service.aadd_order_note(order_id, note, state=state)
                    results["note_added"] = True
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add update note: {exc}", "warning")

                results["success"] = primary_result.get("success", False) or results["logistics_update"].get("success", False)
                if results["success"]:
                    from fashion_bot.utils.order_utils import astamp_bloomerce_edited
                    await astamp_bloomerce_edited(
                        order_service, order_id, update_type, state=state, order_data=raw_order_data,
                    )
                results["message"] = f"Order {order_id} updated in {primary_vendor} + {tracking_company}"
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    results, state, order_dto=order_dto,
                )

            results["routing"] = "dispatched_non_integrated"
            from fashion_bot.utils.order_utils import aadd_escalation_order_note

            results["note_added"] = await aadd_escalation_order_note(
                order_service,
                order_id,
                (
                    f"[Bloomerce] Customer requested {update_type} update. Delivery partner: {tracking_company} "
                    f"(non-integrated). Details: {update_description}. Requires manual intervention."
                ),
                state=state,
            )

            try:
                results["escalation"] = await EscalationOrchestrator.aescalate_to_agent(
                    category="Order Update - Non-Integrated Partner",
                    reason=f"Customer requested {update_type} update for order {order_id} dispatched via {tracking_company}",
                    details=(
                        f"Order {order_id} is dispatched via non-integrated delivery partner '{tracking_company}'. "
                        f"Cannot update automatically. {update_description}. "
                        f"{'Note added to order. ' if results['note_added'] else ''}"
                        "Manual update with delivery partner required."
                    ),
                    state=state,
                    order_id=order_id,
                )
            except Exception as exc:
                results["escalation"] = {"success": False, "error": str(exc)}

            results["success"] = True
            results["message"] = (
                f"Order {order_id} is dispatched via {tracking_company} (non-integrated partner). "
                + (
                    "A note has been added to the order and the team has been notified for manual update."
                    if results["note_added"]
                    else "The team has been notified for manual update."
                )
            )
            results["requires_manual_action"] = True
            return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                results, state, order_dto=order_dto,
            )
        except Exception as exc:
            log_with_trace_id(state, f"❌ Order update failed: {str(exc)}", "error")
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def aupdate_name(
        order_id: str,
        first_name: str,
        last_name: str,
        state: Optional[Dict] = None,
        validated_rule: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async updating name for order {order_id}")
        try:
            # ── Strategy branch (per-order resolution) ──
            # Reads ``validated_rule.order_dto`` if the caller (tool_factory)
            # provided it; falls back to the aggregate strategy otherwise.
            order_dto_hint = (validated_rule or {}).get("order_dto")
            resolution = await OrderUpdateOrchestrator._aresolve_strategy_for_order(
                state=state, order_dto=order_dto_hint,
            )
            if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE:
                return await CancelAndRecreateOrchestrator.aupdate_via_clone(
                    order_id=order_id,
                    update_type=CancelAndRecreateOrchestrator.UPDATE_TYPE_NAME,
                    overrides={"first_name": first_name, "last_name": last_name},
                    state=state,
                )

            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            resolved_status = (validated_rule or {}).get("resolved_status", "UNKNOWN")
            is_integrated = (validated_rule or {}).get("is_integrated", True)
            is_new = resolved_status.upper() == "NEW"

            results = {"shopify": {"success": False}, "logistics": {"success": False, "skipped": False}}
            primary_result = await order_service.aupdate_order(
                order_id,
                {"update_type": "name", "first_name": first_name, "last_name": last_name},
                state=state,
            )
            if primary_result.get("success"):
                results["shopify"] = {"success": True, "message": f"Name updated to {first_name} {last_name} in Shopify"}
            else:
                error_msg = primary_result.get("error", "Unknown error")
                log_with_trace_id(state, f"⚠️ Shopify name update failed for {order_id}: {error_msg}", "warning")
                results["shopify"] = {"success": False, "error": error_msg}

            if is_new or is_integrated:
                try:
                    # Multi-partner-aware. ``aupdate_name`` doesn't fetch an
                    # order_dto (the caller passes ``validated_rule`` with
                    # only is_integrated/resolved_status), so we pass {} —
                    # which causes the router to race every connected
                    # integrated partner. That's the right behaviour for
                    # name updates: every partner that has the shipment
                    # should learn the new name.
                    from fashion_bot.core.logistics_router import LogisticsRouter

                    # Pass the order_dto (or empty dict) to the router so it
                    # can do tracking_url / tracking_company-based partner
                    # resolution.  ``order_dto_hint`` was already extracted
                    # from validated_rule at the top of this function.
                    routed = await LogisticsRouter.aupdate_name_best_effort(
                        order_id, first_name, last_name, order_dto_hint or {}, state=state,
                    )
                    if routed.get("success"):
                        winners = routed.get("winning_partners") or []
                        results["logistics"] = {
                            "success": True,
                            "message": (
                                f"Name updated to {first_name} {last_name} in logistics "
                                f"({', '.join(winners)})"
                            ),
                            "winning_partners": winners,
                            "per_partner": routed.get("per_partner", {}),
                        }
                    else:
                        # Fall back to the legacy single-partner path so
                        # response shape is preserved when no integrated
                        # partner is connected at all.
                        logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                        if not logistics_service:
                            results["logistics"] = {"success": False, "warning": "Logistics service not configured"}
                        else:
                            logistics_result = await logistics_service.aupdate_shipment_name(
                                order_id,
                                first_name,
                                last_name,
                                state=state,
                            )
                            if logistics_result.get("success"):
                                results["logistics"] = {"success": True, "message": f"Name updated to {first_name} {last_name} in logistics"}
                            else:
                                results["logistics"] = {"success": False, "error": logistics_result.get("error", "Failed to update logistics")}
                except Exception as exc:
                    results["logistics"] = {"success": False, "error": str(exc)}
            else:
                results["logistics"] = {"success": True, "skipped": True, "reason": "Non-integrated delivery partner"}

            try:
                await order_service.aadd_order_note(order_id, f"[Bloomerce] Order customer name updated to: {first_name} {last_name}", state=state)
            except Exception as exc:
                log_with_trace_id(state, f"Failed to add note: {exc}", "warning")

            overall_success = results["shopify"]["success"]
            if overall_success:
                from fashion_bot.utils.order_utils import astamp_bloomerce_edited
                await astamp_bloomerce_edited(
                    order_service, order_id, "name", state=state,
                )
            updated_in = []
            failed_in = []
            if results["shopify"]["success"]:
                updated_in.append("Shopify")
            else:
                failed_in.append("Shopify")
            if results["logistics"].get("skipped"):
                updated_in.append("logistics (skipped)")
            elif results["logistics"]["success"]:
                updated_in.append("logistics")
            else:
                failed_in.append("logistics")

            if updated_in and not failed_in:
                message = f"Order {order_id} name updated to {first_name} {last_name} in: {', '.join(updated_in)}"
            elif updated_in and failed_in:
                message = f"Order {order_id} name partially updated. Success: {', '.join(updated_in)}. Failed: {', '.join(failed_in)}"
            else:
                message = f"Order {order_id} name update failed in all systems."

            return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                {"success": overall_success, "order_id": order_id, "message": message, "details": results},
                state,
                order_dto=order_dto_hint,
            )
        except Exception as exc:
            return {"success": False, "error": str(exc), "order_id": order_id}

    @staticmethod
    async def aupdate_notes(
        order_id: str, notes: str, state: Optional[Dict] = None, order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async adding notes to order {order_id}")
        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            if not order_service:
                return {"success": False, "error": "Order service not configured", "order_id": order_id}
            return await order_service.aadd_order_note(order_id, notes, state=state, order_record=order_record)
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def aupdate_tags(
        order_id: str, tags: List[str], state: Optional[Dict] = None, order_record: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async adding tags to order {order_id}")
        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            if not order_service:
                return {"success": False, "error": "Order service not configured", "order_id": order_id}
            return await order_service.aadd_order_tags(order_id, tags, state=state, order_record=order_record)
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def aupdate_phone(order_id: str, new_phone: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async updating phone for order {order_id}")
        try:
            if not new_phone or len(new_phone) < 10:
                return {"success": False, "error": "Invalid phone number. Please provide a valid 10-digit phone number."}

            cleaned_phone = str(new_phone)
            if cleaned_phone.startswith("+91"):
                cleaned_phone = cleaned_phone[3:]
            elif len(cleaned_phone) > 10 and cleaned_phone.startswith("91"):
                cleaned_phone = cleaned_phone[2:]
            cleaned_phone = "".join(filter(str.isdigit, cleaned_phone))
            if cleaned_phone.startswith("91") and len(cleaned_phone) > 10:
                cleaned_phone = cleaned_phone[2:]

            # Fetch order first so per-order strategy resolution can read
            # the carrier from tracking_url / tracking_company.
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)
            raw_order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            ) if order_service else None
            order_dto = processor.process_order(raw_order_data, state=state, source=primary_vendor) if raw_order_data else None
            is_new = order_dto and OrderStatusOrchestrator._is_order_new(order_dto)
            is_terminal = order_dto and OrderStatusOrchestrator._is_order_cancelled_or_voided(order_dto)

            # ── Strategy branch (per-order resolution) ──
            # Resolves the actual carrier for THIS order and looks up its
            # strategy from client_configs.order_update_strategy. NEW orders
            # fall back to aggregate strategy (no carrier yet to resolve).
            resolution = await OrderUpdateOrchestrator._aresolve_strategy_for_order(
                state=state, order_dto=order_dto,
            )
            if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE:
                return await CancelAndRecreateOrchestrator.aupdate_via_clone(
                    order_id=order_id,
                    update_type=CancelAndRecreateOrchestrator.UPDATE_TYPE_PHONE,
                    overrides={"new_phone": "+91" + cleaned_phone if len(cleaned_phone) == 10 else cleaned_phone},
                    state=state,
                )
            tracking_company = (order_dto or {}).get("tracking_company", "")
            tracking_url = (order_dto or {}).get("tracking_url", "")
            # URL-first: tracking_url is the authoritative platform signal.
            # Shiprocket can dispatch via Delhivery, writing tracking_company=
            # 'Delhivery', but the tracking_url will contain 'shiprocket'.
            from fashion_bot.core.vendor_config import resolve_partner_from_url
            effective_partner = resolve_partner_from_url(tracking_url) or tracking_company
            is_integrated = await AsyncVendorConfigManager.ais_integrated_delivery_partner(effective_partner, state=state) if effective_partner else True

            if is_terminal:
                return {"success": False, "error": f"Order {order_id} is cancelled/voided and cannot be updated", "order_id": order_id}

            if order_dto and not is_new and not is_integrated:
                from fashion_bot.utils.order_utils import aadd_escalation_order_note

                note_added = await aadd_escalation_order_note(
                    order_service,
                    order_id,
                    f"[Bloomerce] Customer requested phone update to {cleaned_phone[-4:]}****. Delivery partner: {tracking_company} (non-integrated). Requires manual intervention.",
                    state=state,
                )
                try:
                    await EscalationOrchestrator.aescalate_to_agent(
                        category="Order Update - Non-Integrated Partner",
                        reason=f"Phone update for order {order_id} dispatched via {tracking_company}",
                        details=f"Customer requested phone update to {cleaned_phone} for order {order_id}. Partner: {tracking_company} (non-integrated). Manual update required.",
                        state=state,
                        order_id=order_id,
                    )
                except Exception as exc:
                    log_with_trace_id(state, f"Escalation failed: {exc}", "error")
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    {
                        "success": True,
                        "order_id": order_id,
                        "message": (
                            f"Order {order_id} is dispatched via {tracking_company}. "
                            + ("Note added and team notified." if note_added else "Team notified.")
                        ),
                        "requires_manual_action": True,
                    },
                    state,
                    order_dto=order_dto,
                )

            results = {"order_service": {"success": False}, "logistics_service": {"success": False}}
            try:
                order_result = await order_service.aupdate_order_phone(order_id, cleaned_phone, state=state)
                if order_result.get("success"):
                    results["order_service"] = {"success": True, "message": "Phone updated in order service"}
                else:
                    results["order_service"] = {"success": False, "error": order_result.get("error", "Failed to update order")}
            except Exception as exc:
                results["order_service"] = {"success": False, "error": str(exc)}

            if is_new or is_integrated:
                try:
                    # Multi-partner aware: race connected integrated partners.
                    from fashion_bot.core.logistics_router import LogisticsRouter

                    routed = await LogisticsRouter.aupdate_phone_best_effort(
                        order_id, cleaned_phone, order_dto or {}, state=state,
                    )
                    if routed.get("success"):
                        winners = routed.get("winning_partners") or []
                        results["logistics_service"] = {
                            "success": True,
                            "message": f"Phone updated in logistics service ({', '.join(winners)})",
                            "winning_partners": winners,
                        }
                    else:
                        # Fall back to legacy single-partner path so the rest of the
                        # method's response shape stays unchanged.
                        logistics_service = await ServiceFactory.aget_logistics_service(state=state)
                        logistics_result = await logistics_service.aupdate_shipment_phone(order_id, cleaned_phone, state=state)
                        if logistics_result.get("success"):
                            results["logistics_service"] = {"success": True, "message": "Phone updated in logistics service"}
                        else:
                            results["logistics_service"] = {"success": False, "error": logistics_result.get("error", "Failed to update logistics")}
                except Exception as exc:
                    results["logistics_service"] = {"success": False, "error": str(exc)}

            overall_success = results["order_service"]["success"] or results["logistics_service"]["success"]
            if overall_success:
                from fashion_bot.utils.order_utils import astamp_bloomerce_edited
                await astamp_bloomerce_edited(
                    order_service, order_id, "phone", state=state,
                )
                updated_services = []
                if results["order_service"]["success"]:
                    updated_services.append("order service")
                if results["logistics_service"]["success"]:
                    updated_services.append("logistics service")
                try:
                    await order_service.aadd_order_note(order_id, f"[Bloomerce] Order phone number updated to {cleaned_phone[-4:]}****", state=state)
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add phone update note: {exc}", "warning")
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    {
                        "success": True,
                        "order_id": order_id,
                        "new_phone": new_phone,
                        "message": f"Phone updated in: {', '.join(updated_services)}",
                        "details": results,
                    },
                    state,
                    order_dto=order_dto,
                )
            return {"success": False, "order_id": order_id, "error": "Failed to update phone", "details": results}
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def aupdate_email(order_id: str, new_email: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async updating email for order {order_id}")
        try:
            email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
            if not new_email or not re.match(email_pattern, new_email):
                return {"success": False, "error": "Invalid email address format"}

            # Fetch order first so per-order strategy resolution can read
            # the carrier from tracking_url / tracking_company.
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            processor = ServiceFactory.get_order_processor(primary_vendor)
            raw_order_data = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            ) if order_service else None
            order_dto = processor.process_order(raw_order_data, state=state, source=primary_vendor) if raw_order_data else None
            is_new = order_dto and OrderStatusOrchestrator._is_order_new(order_dto)
            is_terminal = order_dto and OrderStatusOrchestrator._is_order_cancelled_or_voided(order_dto)
            tracking_company = (order_dto or {}).get("tracking_company", "")
            is_integrated = await AsyncVendorConfigManager.ais_integrated_delivery_partner(tracking_company, state=state) if tracking_company else True

            # ── Strategy branch (per-order resolution) ──
            resolution = await OrderUpdateOrchestrator._aresolve_strategy_for_order(
                state=state, order_dto=order_dto,
            )
            if resolution.strategy == OrderUpdateOrchestrator.STRATEGY_CANCEL_AND_RECREATE:
                return await CancelAndRecreateOrchestrator.aupdate_via_clone(
                    order_id=order_id,
                    update_type=CancelAndRecreateOrchestrator.UPDATE_TYPE_EMAIL,
                    overrides={"new_email": new_email},
                    state=state,
                )

            if is_terminal:
                return {"success": False, "error": f"Order {order_id} is cancelled/voided and cannot be updated", "order_id": order_id}

            if order_dto and not is_new and not is_integrated:
                from fashion_bot.utils.order_utils import aadd_escalation_order_note

                note_added = await aadd_escalation_order_note(
                    order_service,
                    order_id,
                    f"[Bloomerce] Customer requested email update to {new_email}. Delivery partner: {tracking_company} (non-integrated). Requires manual intervention.",
                    state=state,
                )
                try:
                    await EscalationOrchestrator.aescalate_to_agent(
                        category="Order Update - Non-Integrated Partner",
                        reason=f"Email update for order {order_id} dispatched via {tracking_company}",
                        details=f"Customer requested email update to {new_email} for order {order_id}. Partner: {tracking_company} (non-integrated). Manual update required.",
                        state=state,
                        order_id=order_id,
                    )
                except Exception as exc:
                    log_with_trace_id(state, f"Escalation failed: {exc}", "error")
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    {
                        "success": True,
                        "order_id": order_id,
                        "message": (
                            f"Order {order_id} is dispatched via {tracking_company}. "
                            + ("Note added and team notified." if note_added else "Team notified.")
                        ),
                        "requires_manual_action": True,
                    },
                    state,
                    order_dto=order_dto,
                )

            results = {"order_service": {"success": False}, "logistics_service": {"success": False}}
            try:
                order_result = await order_service.aupdate_order_email(order_id, new_email, state=state)
                if order_result.get("success"):
                    results["order_service"] = {"success": True, "message": "Email updated in order service"}
                else:
                    results["order_service"] = {"success": False, "error": order_result.get("error", "Failed to update order")}
            except Exception as exc:
                results["order_service"] = {"success": False, "error": str(exc)}

            if is_new or is_integrated:
                try:
                    from fashion_bot.core.logistics_router import LogisticsRouter

                    # Use the email-specific best-effort helper so each adapter
                    # can hit its proper email endpoint. Shoehorning email into
                    # aupdate_address_best_effort risked partners rejecting an
                    # address payload that's missing required address fields.
                    routed = await LogisticsRouter.aupdate_email_best_effort(
                        order_id, new_email, order_dto or {}, state=state,
                    )
                    if routed.get("success"):
                        logistics_result = {
                            "success": True,
                            "winning_partners": routed.get("winning_partners"),
                            "per_partner": routed.get("per_partner", {}),
                        }
                    else:
                        # Fall back to the routed partner's email endpoint
                        # directly (preserves the legacy single-partner shape).
                        partners = await LogisticsRouter.aroute_for_order(order_dto or {}, state=state)
                        logistics_service = await ServiceFactory.aget_logistics_service(
                            state=state, vendor=(partners[0] if partners else None),
                        )
                        logistics_result = await logistics_service.aupdate_shipment_email(
                            order_id, new_email, state=state,
                        )
                    if logistics_result.get("success"):
                        results["logistics_service"] = {"success": True, "message": "Email updated in logistics service"}
                    else:
                        results["logistics_service"] = {"success": False, "error": logistics_result.get("error", "Failed to update logistics")}
                except Exception as exc:
                    results["logistics_service"] = {"success": False, "error": str(exc)}

            overall_success = results["order_service"]["success"] or results["logistics_service"]["success"]
            if overall_success:
                from fashion_bot.utils.order_utils import astamp_bloomerce_edited
                await astamp_bloomerce_edited(
                    order_service, order_id, "email", state=state,
                )
                updated_services = []
                if results["order_service"]["success"]:
                    updated_services.append("order service")
                if results["logistics_service"]["success"]:
                    updated_services.append("logistics service")
                try:
                    await order_service.aadd_order_note(order_id, f"[Bloomerce] Order email updated to {new_email}", state=state)
                except Exception as exc:
                    log_with_trace_id(state, f"Failed to add email update note: {exc}", "warning")
                return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(
                    {
                        "success": True,
                        "order_id": order_id,
                        "new_email": new_email,
                        "message": f"Email updated in: {', '.join(updated_services)}",
                        "details": results,
                    },
                    state,
                    order_dto=order_dto,
                )
            return {"success": False, "order_id": order_id, "error": "Failed to update email", "details": results}
        except Exception as exc:
            return {"success": False, "order_id": order_id, "error": str(exc)}

    @staticmethod
    async def aupdate_order_size(
        order_id: str,
        old_size: str,
        new_size: str,
        line_item_variant_id: str = "",
        quantity: int = 0,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Async variant update orchestration using native async Shopify/Shiprocket flows."""
        from fashion_bot.config_manager import aget_shopify_config
        from fashion_bot.shopify.modules.order_editing_graphql import aupdate_order_size_graphql

        qty_info = f", qty={quantity}" if quantity > 0 else ""
        log_with_trace_id(state, f"OrderUpdateOrchestrator: Async updating variant for order {order_id} ({old_size} → {new_size}{qty_info})")

        try:
            client_id = OrderUpdateOrchestrator._get_client_id(state)
            shopify_config = await aget_shopify_config(client_id=client_id)
            if not shopify_config or not shopify_config.get("access_token"):
                return {"success": False, "error": "Shopify configuration not found", "order_id": order_id}

            result = await aupdate_order_size_graphql(
                order_id=order_id,
                old_size=old_size,
                new_size=new_size,
                line_item_variant_id=line_item_variant_id,
                quantity=quantity,
                access_token=shopify_config["access_token"],
                shop_url=shopify_config["shop_url"],
                state=state,
                api_version=shopify_config.get("api_version", "2024-10"),
            )
            if result.get("success"):
                log_with_trace_id(state, f"✅ Order variant updated successfully: {order_id} ({old_size} → {new_size})")
                try:
                    primary_vendor = ServiceFactory.get_primary_vendor(state)
                    order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
                    if order_service:
                        await order_service.aadd_order_note(order_id, f"[Bloomerce] Order variant updated from {old_size} to {new_size}", state=state)
                except Exception as note_exc:
                    log_with_trace_id(state, f"Failed to add size update note: {note_exc}", "warning")
            else:
                log_with_trace_id(state, f"❌ Order variant update failed: {result.get('error')}", "error")
            # The in-place orderEdit path in ``aupdate_order_size_graphql``
            # at order_editing_graphql.py:1249 does not include order_id
            # (nor old/new_order_id) in its result dict — only old_size,
            # new_size, message, etc. Stamp the order_id here so
            # ``enrich_with_delivery_partner_info`` and any downstream
            # escalation can find it. Safe: never overrides an already-
            # set value (cancel-and-recreate paths set old/new explicitly).
            if isinstance(result, dict) and not result.get("order_id"):
                result["order_id"] = order_id
            return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(result, state)
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in aupdate_order_size: {str(e)}", "error")
            return {
                "success": False,
                "order_id": order_id,
                "error": str(e),
                "details": "This may happen if the order cannot be edited (e.g., already fulfilled, shipped, or COD pending payment)",
            }

    @staticmethod
    async def achange_order_product(
        order_id: str,
        target_variant_id: str,
        new_variant_gid: str,
        new_variant_price: float,
        quantity: int = 1,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Orchestrate an in-place product change via the order-editing GraphQL API.

        Resolves vendor config internally so the tool layer stays vendor-agnostic.
        """
        from fashion_bot.config_manager import aget_shopify_config
        from fashion_bot.shopify.modules.order_editing_graphql import achange_order_product_graphql

        log_with_trace_id(
            state,
            f"OrderUpdateOrchestrator: Changing product in order {order_id} "
            f"(old variant {target_variant_id} → new {new_variant_gid}, qty={quantity})",
        )

        try:
            client_id = OrderUpdateOrchestrator._get_client_id(state)
            shopify_config = await aget_shopify_config(client_id=client_id)
            if not shopify_config or not shopify_config.get("access_token"):
                return {"success": False, "error": "Shopify configuration not found", "order_id": order_id}

            result = await achange_order_product_graphql(
                order_id=order_id,
                target_variant_id=target_variant_id,
                new_variant_gid=new_variant_gid,
                new_variant_price=new_variant_price,
                quantity=quantity,
                access_token=shopify_config["access_token"],
                shop_url=shopify_config["shop_url"],
                state=state,
                api_version=shopify_config.get("api_version", "2024-10"),
            )
            if result.get("success"):
                log_with_trace_id(state, f"✅ Product change succeeded for order {order_id}")
                try:
                    primary_vendor = ServiceFactory.get_primary_vendor(state)
                    order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
                    if order_service:
                        await order_service.aadd_order_note(
                            order_id,
                            f"[Bloomerce] Product changed in order via GraphQL edit. Old variant: {target_variant_id}, New variant: {new_variant_gid}",
                            state=state,
                        )
                except Exception as note_exc:
                    log_with_trace_id(state, f"Failed to add product change note: {note_exc}", "warning")
            else:
                log_with_trace_id(state, f"❌ Product change failed: {result.get('error')}", "error")
            return await OrderUpdateOrchestrator.enrich_with_delivery_partner_info(result, state)
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in achange_order_product: {str(e)}", "error")
            return {
                "success": False,
                "order_id": order_id,
                "error": str(e),
                "details": "This may happen if the order cannot be edited (e.g., already fulfilled or shipped)",
            }


class CancelAndRecreateOrchestrator:
    """Cancel-and-recreate update flow.

    When a merchant opts in via ``client_configs.order_update_strategy =
    "cancel_and_recreate"`` and has Delhivery in their integrated partners,
    address/phone/email/name/size updates take this path instead of the
    legacy per-partner-API path:

      1. Fetch the original Shopify order.
      2. Refuse if already cancelled / voided / fulfilled (we don't want to
         disturb a shipment that's already gone out).
      3. Cancel the original Shopify order with ``skip_refund=True`` so
         the customer's payment is preserved as a credit we re-apply to
         the new order via the ``transactions`` field of
         ``aclone_order``.
      4. Clone the original with the single changed field overridden
         (address / phone / email / name). Line items, customer, totals,
         discounts, payment status (prepaid/partial-paid/COD) are all
         carried over verbatim.
      5. Shopify's own webhook auto-sync propagates the new order to
         every connected delivery partner. We do NOT call Delhivery or
         Shiprocket APIs directly in this strategy — the merchant
         explicitly chose to delegate sync to Shopify.

    Why this exists: Delhivery's public APIs cannot edit a pre-AWB
    (``Pending AWB``) order — the documented edit endpoint requires a
    waybill. For tenants that don't want the agent to escalate to a
    human in that case, cancel-and-recreate is the only Shopify-API-
    only path that guarantees the new address reaches Delhivery before
    AWB generation.

    Trade-off (intentional, gated behind merchant opt-in): the customer
    sees a new order number after the update. Tag ``CANCEL_AND_RECREATE``
    is added so reporting can pair the old and new order_ids.
    """

    # Update-type constants — matches strings used by the agent's tools
    # (update_order_address → "address", etc.) so the dispatcher in
    # ``OrderUpdateOrchestrator.aupdate_*`` can pass them through.
    UPDATE_TYPE_ADDRESS = "address"
    UPDATE_TYPE_PHONE = "phone"
    UPDATE_TYPE_EMAIL = "email"
    UPDATE_TYPE_NAME = "name"
    UPDATE_TYPE_SIZE = "size"

    @staticmethod
    async def aupdate_via_clone(
        order_id: str,
        update_type: str,
        overrides: Dict[str, Any],
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Cancel the order and clone with the changed field.

        Args:
            order_id: Shopify order identifier (name like ``gv15308`` or
                numeric id).
            update_type: one of ``UPDATE_TYPE_*`` constants. Determines
                which ``*_override`` arguments are passed into
                ``aclone_order``.
            overrides: per-update-type payload —
              ``address``: ``{"shipping_address": {address1?, address2?,
                              city?, province?, zip?, country?, phone?,
                              first_name?, last_name?}}``
              ``phone``:   ``{"new_phone": "+91…"}``
              ``email``:   ``{"new_email": "…@…"}``
              ``name``:    ``{"first_name": "…", "last_name": "…"}``
              ``size``:    ``{"new_line_items": [{variant_id, quantity}, …]}``
            state: LangGraph state (carries ``client_id``, ``trace_id``).

        Returns:
            Standard update result dict with keys:
              ``success``: bool
              ``old_order_id``, ``new_order_id``: Shopify names
              ``method``: ``"cancel_and_recreate"``
              ``message``: customer-facing summary
              ``payment_type``: ``prepaid`` / ``partial_prepaid`` / ``cod``
              ``original_amount_paid``: float (rupees)
              On failure: ``error``, plus ``old_order_cancelled`` /
              ``customer_credit`` / ``requires_manual_intervention``
              flags so the agent can compose a recovery message.
        """
        from decimal import Decimal

        from fashion_bot.core.factory import ServiceFactory
        from fashion_bot.utils.order_utils import (
            astamp_bloomerce_edited,
            build_payment_reference_tags,
            classify_payment_type,
        )

        log_with_trace_id(
            state,
            f"CancelAndRecreateOrchestrator: cancel-and-recreate {update_type} for {order_id}",
        )

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(
                state=state, vendor=primary_vendor,
            )
            if not order_service:
                return {
                    "success": False, "order_id": order_id,
                    "error": "Order service not configured",
                }

            raw_order = _unwrap_primary_order_record(
                await order_service.aget_order_details(order_id, state=state),
                primary_vendor,
            )
            if not raw_order:
                return {
                    "success": False, "order_id": order_id,
                    "error": f"Order {order_id} not found in Shopify",
                }

            # Refuse to cancel-and-recreate on terminal/shipped orders.
            # The clone wouldn't reverse a delivered shipment.
            fulfillment_status = (raw_order.get("fulfillment_status") or "").lower()
            cancelled_at = raw_order.get("cancelled_at")
            if cancelled_at:
                return {
                    "success": False, "order_id": order_id,
                    "error": f"Order {order_id} is already cancelled",
                }
            if fulfillment_status in {"fulfilled", "partial"}:
                return {
                    "success": False, "order_id": order_id,
                    "error": (
                        f"Order {order_id} has been fulfilled "
                        f"(status={fulfillment_status}); cancel-and-recreate "
                        f"is unsafe for shipped orders. Falling back to "
                        f"escalation."
                    ),
                    "requires_escalation": True,
                }

            # Pull payment info so we can rebuild the transactions list
            # that preserves the customer's payment when we cancel + clone.
            payment_info = classify_payment_type(raw_order)
            payment_type = payment_info["payment_type"]
            amount_paid = payment_info["amount_paid"]
            original_financial_status = raw_order.get("financial_status", "pending")
            original_gateways = raw_order.get("payment_gateway_names") or []

            order_name = raw_order.get("name") or order_id

            # Translate update_type + overrides into aclone_order kwargs.
            clone_overrides = (
                CancelAndRecreateOrchestrator._abuild_clone_overrides(
                    update_type, overrides, raw_order,
                )
            )
            change_summary = clone_overrides.pop("_change_summary", update_type)

            # Cancel original. skip_refund=True preserves the payment;
            # we re-attach it to the clone via `transactions`. For COD
            # there's no payment to preserve, but skip_refund=True is
            # still safe (Shopify just records the cancellation).
            await astamp_bloomerce_edited(
                order_service, order_id, update_type, state=state,
                order_data=raw_order, extra_tags=[OrderTag.BLOOMERCE_UPDATED],
            )
            cancel_note = (
                f"Order cancelled for {update_type} update — {change_summary}. "
                f"Recreating via cancel-and-recreate strategy. "
                f"Tag: {OrderTag.CANCEL_AND_RECREATE}."
            )
            cancel_result = await order_service.acancel_order(
                order_id, "customer", state=state,
                custom_note=cancel_note,
                skip_refund=True,
            )
            if not cancel_result.get("success"):
                return {
                    "success": False, "order_id": order_id,
                    "error": (
                        f"Failed to cancel original order: "
                        f"{cancel_result.get('error')}"
                    ),
                    "cancel_result": cancel_result,
                }

            # Build payment-preserving transactions list (only when there
            # was real money on the original order).
            clone_transactions: Optional[List[Dict[str, Any]]] = None
            if float(amount_paid) > 0:
                gateway = original_gateways[0] if original_gateways else "manual"
                clone_transactions = [{
                    "kind": "sale",
                    "status": "success",
                    "amount": str(amount_paid),
                    "gateway": gateway,
                }]

            new_line_items = clone_overrides.pop(
                "new_line_items", None,
            ) or _build_existing_line_items_clone_input(raw_order)

            clone_note = (
                f"Cancel-and-recreate from order {order_name}: {update_type} "
                f"updated — {change_summary}. Original payment of ₹{amount_paid} "
                f"({payment_type}) carried over."
            )

            from fashion_bot.shopify.tools.order_adapter import ShopifyOrderAdapter
            if not isinstance(order_service, ShopifyOrderAdapter):
                # We need the Shopify-specific clone; primary_vendor should
                # always be shopify for our supported tenants, but be
                # defensive.
                order_service = await ShopifyOrderAdapter.create(
                    client_id=(state or {}).get("client_id"),
                )

            # Pin the clone back to the original order's captured payment by
            # tagging it with the original Shopify transaction id (from the
            # transactions API) + the gateway payment reference (from the
            # original order's note_attributes, e.g. PayU_txn_id), so
            # finance/ops can reconcile the new order against the real payment.
            # Fail-open: COD orders and transaction-fetch failures simply yield
            # no extra tags.
            payment_ref_tags: List[str] = []
            if float(amount_paid) > 0:
                txns = await order_service.aget_order_transactions(
                    raw_order.get("id"), state=state,
                )
                payment_ref_tags = build_payment_reference_tags(
                    txns, note_attributes=raw_order.get("note_attributes"),
                )

            create_result = await order_service.aclone_order(
                original_order_data=raw_order,
                new_line_items=new_line_items,
                state=state,
                note=clone_note,
                additional_tags=[
                    OrderTag.CANCEL_AND_RECREATE, OrderTag.BLOOMERCE_UPDATED,
                ] + payment_ref_tags,
                financial_status_override=original_financial_status,
                payment_gateway_names_override=(
                    original_gateways if original_gateways else None
                ),
                transactions=clone_transactions,
                **clone_overrides,
            )

            if not create_result.get("success"):
                # We've cancelled but couldn't clone — customer is owed
                # the credit. Surface this so the agent can escalate.
                return {
                    "success": False, "order_id": order_id,
                    "error": (
                        f"Original order cancelled but failed to recreate: "
                        f"{create_result.get('error')}"
                    ),
                    "old_order_cancelled": True,
                    "customer_credit": float(amount_paid),
                    "payment_type": payment_type,
                    "requires_manual_intervention": True,
                    "create_error": create_result.get("error"),
                    "method": "cancel_and_recreate",
                }

            new_order_id = (
                create_result.get("order_name")
                or create_result.get("order_id")
            )
            return {
                "success": True,
                "method": "cancel_and_recreate",
                "old_order_id": order_id,
                "new_order_id": new_order_id,
                "payment_type": payment_type,
                "original_amount_paid": float(amount_paid),
                "financial_status": original_financial_status,
                "update_type": update_type,
                "change_summary": change_summary,
                "message": (
                    f"Order {order_name} cancelled and recreated as "
                    f"{new_order_id} with the updated {update_type}. "
                    f"Previous payment of ₹{amount_paid} ({payment_type}) "
                    f"was carried over."
                ),
                "create_result": create_result,
            }

        except Exception as exc:
            log_with_trace_id(
                state,
                f"❌ CancelAndRecreateOrchestrator.aupdate_via_clone failed: {exc}",
                "error",
            )
            return {
                "success": False, "order_id": order_id,
                "error": str(exc),
                "method": "cancel_and_recreate",
            }

    @staticmethod
    def _abuild_clone_overrides(
        update_type: str,
        overrides: Dict[str, Any],
        raw_order: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Translate an update-type + raw overrides dict into the kwargs
        ``aclone_order`` accepts (``shipping_address_override``,
        ``email_override``, etc.).

        Always returns a dict; the special key ``_change_summary`` is a
        short human-readable description used in notes and replies.
        """
        out: Dict[str, Any] = {}
        ut = (update_type or "").strip().lower()

        if ut == CancelAndRecreateOrchestrator.UPDATE_TYPE_ADDRESS:
            new_addr = overrides.get("shipping_address") or {}
            # Accept either dict (preferred) or plain string from older
            # callers that pass new_address as text.
            if isinstance(new_addr, str):
                new_addr = {"address1": new_addr}
            out["shipping_address_override"] = new_addr
            # Summary for notes / reply
            parts = [
                new_addr.get("address1"),
                new_addr.get("address2"),
                new_addr.get("city"),
                new_addr.get("province") or new_addr.get("state"),
                new_addr.get("zip") or new_addr.get("pincode"),
            ]
            out["_change_summary"] = ", ".join(p for p in parts if p) or "new address"
        elif ut == CancelAndRecreateOrchestrator.UPDATE_TYPE_PHONE:
            new_phone = overrides.get("new_phone") or overrides.get("phone")
            out["phone_override"] = new_phone
            masked = (
                f"***{str(new_phone)[-4:]}" if new_phone else "(empty)"
            )
            out["_change_summary"] = f"phone {masked}"
        elif ut == CancelAndRecreateOrchestrator.UPDATE_TYPE_EMAIL:
            new_email = overrides.get("new_email") or overrides.get("email")
            out["email_override"] = new_email
            out["_change_summary"] = f"email {new_email}"
        elif ut == CancelAndRecreateOrchestrator.UPDATE_TYPE_NAME:
            first = overrides.get("first_name") or ""
            last = overrides.get("last_name") or ""
            out["customer_override"] = {
                "first_name": first or None,
                "last_name": last or None,
            }
            out["_change_summary"] = f"name {first} {last}".strip()
        elif ut == CancelAndRecreateOrchestrator.UPDATE_TYPE_SIZE:
            line_items = overrides.get("new_line_items") or []
            out["new_line_items"] = line_items
            out["_change_summary"] = "size change"
        else:
            out["_change_summary"] = update_type
        return out


def _build_existing_line_items_clone_input(
    raw_order: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Convert the original Shopify order's line_items into the minimal
    shape ``aclone_order`` accepts (just variant_id + quantity).

    Used by ``CancelAndRecreateOrchestrator`` for non-size updates where
    we want every line item preserved verbatim.
    """
    out: List[Dict[str, Any]] = []
    for li in raw_order.get("line_items") or []:
        variant_id = li.get("variant_id")
        if not variant_id:
            continue
        out.append({
            "variant_id": int(str(variant_id).replace("gid://shopify/ProductVariant/", "")),
            "quantity": int(li.get("quantity") or 1),
        })
    return out


def _resolve_result_order_id(result: Dict[str, Any]) -> str:
    """Extract a non-empty order identifier from an update-result dict.

    Different update flows populate different keys:
      - Plain address/phone/email/name updates return ``order_id``.
      - Cancel-and-recreate flow returns ``old_order_id`` +
        ``new_order_id`` and sets ``method='cancel_and_recreate'``.
      - Size updates use the orchestrator wrapper at
        ``aupdate_order_size`` which stamps ``order_id`` onto the
        result before this resolver runs — guaranteeing the first
        key in the chain below resolves for that flow too.

    Try, in priority order:
      1. ``order_id`` — explicit, set by most flows + wrapper stamp.
      2. ``new_order_id`` — cancel-and-recreate's currently-active id.
      3. ``old_order_id`` — degraded cancel-and-recreate where new failed.

    Without this resolver, escalation messages render as "Order: " or
    "Order ID: <missing>" — breaking the agent's ability to act on
    them. We prefer ``new_order_id`` over ``old_order_id`` because
    the new id is the currently-active one to look up in dashboards.
    """
    if not isinstance(result, dict):
        return ""
    return str(
        result.get("order_id")
        or result.get("new_order_id")
        or result.get("old_order_id")
        or ""
    )


def _infer_update_type_from_result(result: Dict[str, Any]) -> str:
    """Best-effort recover the update-type string from a result dict that
    didn't include one explicitly. Used by ``enrich_with_delivery_partner_info``
    when firing the Delhivery manual-sync escalation."""
    if not isinstance(result, dict):
        return "update"
    if result.get("update_type"):
        return result["update_type"]
    msg = (result.get("message") or "").lower()
    if "address" in msg:
        return "address"
    if "phone" in msg:
        return "phone"
    if "email" in msg:
        return "email"
    if "name" in msg:
        return "name"
    if "size" in msg or "variant" in msg:
        return "size"
    return "update"


def _aupdate_order_parse_address(addr_str: str) -> Dict[str, Any]:
    """Parse the pipe-delimited address string the ``update_order_address``
    tool builds at ``tool_factory.py:3513-3521`` back into the structured
    ``shipping_address`` dict that ``aclone_order``'s override params
    expect.

    Format (7 pipe-separated fields):
      ``"<full_name>|<address1>|<address2>|<city>|<state>|<zip>|<phone>"``

    Missing trailing fields are tolerated. Empty fields are dropped from
    the output so they don't blank out values inherited from the
    original order.
    """
    if not addr_str:
        return {}
    if isinstance(addr_str, dict):
        return addr_str
    parts = (str(addr_str).split("|") + [""] * 7)[:7]
    name, address1, address2, city, state_val, zip_code, phone = parts

    out: Dict[str, Any] = {}
    if name and name.strip():
        names = name.strip().split(" ", 1)
        out["first_name"] = names[0]
        if len(names) > 1:
            out["last_name"] = names[1]
    if address1:
        out["address1"] = address1
    if address2:
        out["address2"] = address2
    if city:
        out["city"] = city
    if state_val:
        # Shopify Admin REST orders.json uses 'province' (full state
        # name); 'province_code' optional. Pass both — the API ignores
        # whichever it doesn't need.
        out["province"] = state_val
    if zip_code:
        out["zip"] = zip_code
    if phone:
        out["phone"] = phone
    return out


class CustomerOrchestrator:
    """
    Orchestrator for customer-related operations.
    Handles customer lookup, data retrieval in a vendor-agnostic manner.
    """
    
    @staticmethod
    async def afetch_customer_by_phone(phone_number: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Async variant of fetch_customer_by_phone.

        Preserves the original orchestrator routing, but uses the vendor adapter's
        native async customer lookup instead of calling Shopify modules directly.
        """
        log_with_trace_id(state, f"CustomerOrchestrator: Async fetching customer by phone: {phone_number[-4:] if phone_number else 'N/A'}...")

        from fashion_bot.utils.phone_number_utils import is_real_phone_number
        if not is_real_phone_number(phone_number):
            log_with_trace_id(
                state,
                f"Rejecting customer lookup: '{phone_number}' is not a valid 10-digit phone number",
                "warning",
            )
            return {"found": False, "invalid_phone": True}

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            log_with_trace_id(state, f"Primary vendor for async customer lookup: {primary_vendor}")

            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)
            if not order_service:
                log_with_trace_id(state, "❌ Order service not configured", "error")
                return {"found": False, "error": "Order service not configured"}
            
            return await order_service.aget_customer_by_phone(phone_number, state=state)
                
        except Exception as e:
            log_with_trace_id(state, f"❌ Async customer fetch failed: {str(e)}", "error")
            return {"found": False, "error": str(e)}


class UtilityOrchestrator:
    """
    Orchestrator for utility operations like validation, extraction, and config retrieval.
    Provides a unified interface for utility functions.
    """
    
    @staticmethod
    def _get_client_id(state: Optional[Dict]) -> Optional[str]:
        """Extract client_id from state."""
        if not state:
            return None
        client_id = state.get("client_id")
        if not client_id:
            context = state.get("context", {})
            client_id = context.get("client_id")
        return client_id

    @staticmethod
    async def select_product_by_number(
        selected_product: Dict[str, Any],
        client_id: Optional[str] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Process a selected product from search results.
        
        Constructs the proper product link and updates state.
        """
        log_with_trace_id(state, f"UtilityOrchestrator: Processing selected product", "debug")

        try:
            product_url = selected_product.get("url", "")
            product_name = selected_product.get("title") or selected_product.get("name", "Unknown")
            
            if not product_url and selected_product.get("handle"):
                try:
                    from fashion_bot.config_manager import aget_shopify_config
                    shopify_cfg = await aget_shopify_config(client_id=client_id)
                    base_url = shopify_cfg.get("shop_url", "") if shopify_cfg else ""
                except Exception:
                    base_url = ""

                if base_url:
                    product_url = f"{base_url.rstrip('/')}/products/{selected_product['handle']}"
            
            log_with_trace_id(state, f"✅ Selected product: {product_name}, URL: {product_url}")
            
            return {
                "success": True,
                "product_link": product_url,
                "product_name": product_name,
                "product": selected_product
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Error processing selected product: {str(e)}", "error")
            return {
                "success": False,
                "error": str(e),
                "product_link": ""
            }

    @staticmethod
    async def get_policy_config(policy_type: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get policy configuration from database.
        
        Args:
            policy_type: Type of policy (e.g., 'delivery_policy', 'payment_policy', 
                        'return_exchange_policy', 'vendor_inquiry')
            state: Current state dictionary
            
        Returns:
            Dictionary with policy data
        """
        from fashion_bot.config_manager import aget_json_config
        
        try:
            client_id = UtilityOrchestrator._get_client_id(state)
            
            # Get policy data from config manager
            policy_data = await aget_json_config(policy_type, client_id)
            
            if policy_data:
                log_with_trace_id(state, f"policy_config: {policy_type} found", "debug")
                
                # Format policy data for agent consumption
                formatted_data = ""
                if isinstance(policy_data, dict):
                    # Convert Q&A format to readable string
                    for key, value in policy_data.items():
                        if isinstance(value, str):
                            formatted_data += f"**{key}**: {value}\n\n"
                        elif isinstance(value, list):
                            formatted_data += f"**{key}**:\n"
                            for item in value:
                                formatted_data += f"  - {item}\n"
                            formatted_data += "\n"
                        elif isinstance(value, dict):
                            formatted_data += f"**{key}**:\n"
                            for k, v in value.items():
                                formatted_data += f"  - {k}: {v}\n"
                            formatted_data += "\n"
                else:
                    formatted_data = str(policy_data)
                
                return {
                    "success": True,
                    "policy_type": policy_type,
                    "policy_data": formatted_data if formatted_data else str(policy_data),
                    "raw_data": policy_data
                }
            else:
                log_with_trace_id(state, f"⚠️ No policy data found for {policy_type}")
                return {
                    "success": False,
                    "policy_type": policy_type,
                    "error": f"No {policy_type} configuration found",
                    "message": f"Policy information for {policy_type} is not configured"
                }
                
        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching policy config: {str(e)}", "error")
            return {
                "success": False,
                "policy_type": policy_type,
                "error": str(e),
                "message": f"Error retrieving {policy_type} information"
            }

    @staticmethod
    def validate_address_has_postal_code(address: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate that an address contains a postal/ZIP code.
        
        Args:
            address: The address string to validate
            state: Current state dictionary
            
        Returns:
            Dictionary with validation result
        """
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Validating address for postal code", "debug")
        
        try:
            if not address:
                return {"is_valid": False, "error": "Address is empty"}
            
            # Indian PIN code patterns (6 digits)
            indian_pin_patterns = [
                r'\b\d{6}\b',  # 6 digits standalone
                r'\b\d{3}\s?\d{3}\b',  # 3+3 with optional space
            ]
            
            # International postal code patterns
            intl_patterns = [
                r'\b\d{5}(?:-\d{4})?\b',  # US ZIP (5 or 9 digits)
                r'\b[A-Z]{1,2}\d{1,2}\s?\d[A-Z]{2}\b',  # UK postcode
            ]
            
            all_patterns = indian_pin_patterns + intl_patterns
            
            for pattern in all_patterns:
                if re.search(pattern, address, re.IGNORECASE):
                    return {"is_valid": True, "message": "Address contains valid postal code"}
            
            return {
                "is_valid": False, 
                "error": "No postal code found in address",
                "message": "Please provide your complete address including PIN code/postal code"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Address validation failed: {str(e)}", "error")
            return {"is_valid": False, "error": str(e)}

    @staticmethod
    def validate_pincode(pincode: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate if a pincode/postal code is valid (accepts any country format).
        
        Args:
            pincode: The pincode/postal code to validate
            state: Current state dictionary
            
        Returns:
            Dictionary with validation result
        """
        from fashion_bot.tool_helpers import validate_indian_pincode
        
        log_with_trace_id(state, f"UtilityOrchestrator: Validating pincode: {pincode}", "debug")
        
        try:
            if not pincode:
                return {"valid": False, "error": "Pincode is empty", "pincode": pincode}
            
            is_valid = validate_indian_pincode(pincode)
            
            if is_valid:
                return {
                    "valid": True,
                    "pincode": pincode.strip(),
                    "message": "Valid postal code format"
                }
            else:
                return {
                    "valid": False,
                    "pincode": pincode,
                    "error": "Invalid postal code format",
                    "message": "Please provide a valid postal code (3-10 characters, alphanumeric)"
                }
                
        except Exception as e:
            log_with_trace_id(state, f"❌ Pincode validation failed: {str(e)}", "error")
            return {"valid": False, "pincode": pincode, "error": str(e)}

    @staticmethod
    async def validate_url_domain(url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate if a product URL is from an allowed domain.
        
        Args:
            url: The product URL to validate
            state: Current state dictionary
            
        Returns:
            Dictionary with validation result (is_valid, domain, error)
        """
        from urllib.parse import urlparse
        from fashion_bot.config_manager import aget_shopify_config
        
        log_with_trace_id(state, f"UtilityOrchestrator: Validating URL domain: {url}", "debug")
        
        try:
            if not url:
                return {"is_valid": False, "error": "URL is empty"}
            
            # Parse the URL
            parsed = urlparse(url)
            domain = parsed.netloc.lower()
            
            if not domain:
                return {"is_valid": False, "error": "Invalid URL format"}
            
            # Get client_id from state
            client_id = UtilityOrchestrator._get_client_id(state)
            log_with_trace_id(state, f"UtilityOrchestrator: Resolved client_id for domain validation: {client_id}", "debug")
            
            # Get allowed domains from config
            allowed_domains = []
            
            # First, try to get website_urls config which contains both shopify_url and website_url
            try:
                from fashion_bot.config_manager import aget_config
                import json
                
                website_urls_config = await aget_config("website_urls", client_id=client_id)
                if website_urls_config:
                    # Parse JSON if it's a string
                    if isinstance(website_urls_config, str):
                        website_urls_config = json.loads(website_urls_config)
                    
                    # Extract shopify_url
                    shopify_url = website_urls_config.get("shopify_url", "")
                    if shopify_url:
                        shopify_parsed = urlparse(shopify_url)
                        if shopify_parsed.netloc:
                            allowed_domains.append(shopify_parsed.netloc.lower())
                    
                    # Extract website_url
                    website_url = website_urls_config.get("website_url", "")
                    if website_url:
                        website_parsed = urlparse(website_url)
                        if website_parsed.netloc:
                            allowed_domains.append(website_parsed.netloc.lower())
                    
                    log_with_trace_id(state, f"📋 Loaded allowed domains from website_urls config: {allowed_domains}")
            except Exception as e:
                log_with_trace_id(state, f"⚠️ Error loading website_urls config: {e}")
            
            # Fallback: Get from shopify_config if website_urls not found
            if not allowed_domains:
                shopify_config = await aget_shopify_config(client_id=client_id)
                shop_url = shopify_config.get("shop_url", "") if shopify_config else ""
                
                if shop_url:
                    shop_parsed = urlparse(shop_url)
                    if shop_parsed.netloc:
                        allowed_domains.append(shop_parsed.netloc.lower())
            
            # Add common variations (with/without www)
            expanded_domains = []
            for d in allowed_domains:
                expanded_domains.append(d)
                if d.startswith("www."):
                    expanded_domains.append(d[4:])
                else:
                    expanded_domains.append("www." + d)
            
            # Check if domain is allowed
            domain_clean = domain.replace("www.", "")
            for allowed in expanded_domains:
                allowed_clean = allowed.replace("www.", "")
                if domain_clean == allowed_clean or domain_clean.endswith("." + allowed_clean):
                    return {
                        "is_valid": True,
                        "domain": domain,
                        "message": "URL is from an allowed domain"
                    }
            
            return {
                "is_valid": False,
                "domain": domain,
                "allowed_domains": list(set(allowed_domains)),
                "error": f"Domain '{domain}' is not in the allowed list",
                "message": "This product URL is not from our store. Please share a link from our website."
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ URL domain validation failed: {str(e)}", "error")
            return {"is_valid": False, "error": str(e)}

    @staticmethod
    async def get_available_categories(client_id: Optional[str] = None, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get available product categories with links.
        
        Args:
            client_id: Optional client ID for multi-tenant support
            state: Current state dictionary
            
        Returns:
            Dictionary with formatted category links
        """
        import json
        from fashion_bot.config_manager import aget_config
        
        log_with_trace_id(state, "UtilityOrchestrator: Fetching available categories")
        
        try:
            # Get client_id from state if not provided
            if not client_id and state:
                client_id = UtilityOrchestrator._get_client_id(state)
            
            category_urls_json = await aget_config('category_urls', client_id=client_id)
            
            if category_urls_json:
                if isinstance(category_urls_json, str):
                    category_urls = json.loads(category_urls_json)
                else:
                    category_urls = category_urls_json
                
                if category_urls and isinstance(category_urls, dict):
                    category_links = []
                    for category, url in category_urls.items():
                        category_name = category.replace('-', ' ').title()
                        category_links.append({
                            "name": category_name,
                            "url": url,
                            "formatted": f"• {category_name}: {url}"
                        })
                    
                    log_with_trace_id(state, f"✅ Found {len(category_links)} categories")
                    return {
                        "found": True,
                        "success": True,
                        "categories": category_links,
                        "formatted_text": "\n".join([c["formatted"] for c in category_links])
                    }
            
            log_with_trace_id(state, "⚠️ No category URLs configured")
            return {"found": False, "success": False, "categories": [], "formatted_text": ""}
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching categories: {str(e)}", "error")
            return {"found": False, "success": False, "categories": [], "error": str(e)}

    @staticmethod
    def validate_size_for_exchange(size: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate size format for exchange requests.
        
        Args:
            size: The size string to validate (S/M/L/XL or 28/30/32 etc)
            state: Current state dictionary
            
        Returns:
            Dictionary with validation result
        """
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Validating size for exchange: {size}", "debug")
        
        try:
            if not size:
                return {"valid": False, "error": "Size is empty"}
            
            size_upper = size.strip().upper()
            
            # Letter sizes
            letter_sizes = ['XXXS', 'XXS', 'XS', 'S', 'M', 'L', 'XL', 'XXL', 'XXXL', '2XL', '3XL', '4XL', '5XL']
            if size_upper in letter_sizes:
                return {
                    "valid": True,
                    "size": size_upper,
                    "size_type": "letter",
                    "message": f"Valid size: {size_upper}"
                }
            
            # Numeric sizes (common ranges for pants/jeans: 26-44, shirts: 36-48)
            if re.match(r'^(\d{2})$', size.strip()):
                num = int(size.strip())
                if 24 <= num <= 52:
                    return {
                        "valid": True,
                        "size": str(num),
                        "size_type": "numeric",
                        "message": f"Valid size: {num}"
                    }
            
            # Combination sizes like "32/M" or "L/40"
            if re.match(r'^(\d{2})/([A-Z]+)$', size_upper) or re.match(r'^([A-Z]+)/(\d{2})$', size_upper):
                return {
                    "valid": True,
                    "size": size_upper,
                    "size_type": "combination",
                    "message": f"Valid size: {size_upper}"
                }
            
            return {
                "valid": False,
                "size": size,
                "error": "Invalid size format",
                "message": "Please provide a valid size (e.g., S, M, L, XL, 28, 30, 32)"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Size validation failed: {str(e)}", "error")
            return {"valid": False, "size": size, "error": str(e)}

    @staticmethod
    def validate_exchange_size(size_input: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Alias for validate_size_for_exchange for backward compatibility.
        """
        result = UtilityOrchestrator.validate_size_for_exchange(size_input, state)
        # Map response format for compatibility
        return {
            "is_valid": result.get("valid", False),
            "size": result.get("size", size_input),
            "size_type": result.get("size_type", "unknown"),
            "error": result.get("error"),
            "message": result.get("message")
        }

    @staticmethod
    def validate_indian_state(address: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate if address contains a valid Indian state.
        
        Args:
            address: The address string to check
            state: Current state dictionary
            
        Returns:
            Dictionary with validation result and detected state
        """
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Validating Indian state in address", "debug")
        
        try:
            if not address:
                return {"is_valid": False, "detected_state": None, "reasoning": "Address is empty"}
            
            # List of Indian states and union territories
            indian_states = [
                "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
                "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
                "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
                "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
                "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
                "Delhi", "New Delhi", "NCR", "Chandigarh", "Puducherry", "Ladakh",
                "Jammu and Kashmir", "Jammu & Kashmir", "J&K", "Andaman and Nicobar",
                "Dadra and Nagar Haveli", "Daman and Diu", "Lakshadweep"
            ]
            
            address_lower = address.lower()
            
            for indian_state in indian_states:
                if indian_state.lower() in address_lower:
                    return {
                        "is_valid": True,
                        "detected_state": indian_state,
                        "reasoning": f"Found state: {indian_state}"
                    }
            
            # Check abbreviations
            state_abbrevs = {
                "AP": "Andhra Pradesh", "AR": "Arunachal Pradesh", "AS": "Assam",
                "BR": "Bihar", "CG": "Chhattisgarh", "GA": "Goa", "GJ": "Gujarat",
                "HR": "Haryana", "HP": "Himachal Pradesh", "JH": "Jharkhand",
                "KA": "Karnataka", "KL": "Kerala", "MP": "Madhya Pradesh",
                "MH": "Maharashtra", "MN": "Manipur", "ML": "Meghalaya",
                "MZ": "Mizoram", "NL": "Nagaland", "OD": "Odisha", "PB": "Punjab",
                "RJ": "Rajasthan", "SK": "Sikkim", "TN": "Tamil Nadu",
                "TS": "Telangana", "TR": "Tripura", "UP": "Uttar Pradesh",
                "UK": "Uttarakhand", "WB": "West Bengal", "DL": "Delhi"
            }
            
            for abbrev, full_name in state_abbrevs.items():
                if re.search(rf'\b{abbrev}\b', address.upper()):
                    return {
                        "is_valid": True,
                        "detected_state": full_name,
                        "reasoning": f"Found state abbreviation: {abbrev} ({full_name})"
                    }
            
            return {
                "is_valid": False,
                "detected_state": None,
                "reasoning": "No Indian state found in address"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ State validation failed: {str(e)}", "error")
            return {"is_valid": False, "detected_state": None, "reasoning": str(e)}

    @staticmethod
    def validate_order_confirmation(message: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate if customer confirmed or rejected an order.
        
        Args:
            message: Customer's response message
            state: Current state dictionary
            
        Returns:
            Dictionary with confirmation type and status
        """
        log_with_trace_id(state, f"UtilityOrchestrator: Validating order confirmation", "debug")
        
        try:
            if not message:
                return {"confirmation_type": "UNKNOWN", "is_confirmed": False, "reasoning": "Empty message"}
            
            message_lower = message.lower().strip()
            
            # Positive confirmation patterns
            positive_patterns = [
                r'\b(yes|yeah|yep|yup|sure|ok|okay|confirm|confirmed|proceed|go ahead|place|order|done)\b',
                r'\b(right|correct|perfect|great|good|fine)\b'
            ]
            
            # Negative/rejection patterns
            negative_patterns = [
                r'\b(no|nope|nah|cancel|stop|wait|hold|change|wrong|incorrect)\b',
                r'\b(don\'t|dont|do not|not sure)\b'
            ]
            
            import re
            
            is_positive = any(re.search(p, message_lower) for p in positive_patterns)
            is_negative = any(re.search(p, message_lower) for p in negative_patterns)
            
            if is_positive and not is_negative:
                return {
                    "confirmation_type": "CONFIRMED",
                    "is_confirmed": True,
                    "reasoning": "Customer confirmed the order"
                }
            elif is_negative:
                return {
                    "confirmation_type": "REJECTED",
                    "is_confirmed": False,
                    "reasoning": "Customer rejected or wants changes"
                }
            else:
                return {
                    "confirmation_type": "UNKNOWN",
                    "is_confirmed": False,
                    "reasoning": "Unable to determine confirmation intent"
                }
                
        except Exception as e:
            log_with_trace_id(state, f"❌ Confirmation validation failed: {str(e)}", "error")
            return {"confirmation_type": "UNKNOWN", "is_confirmed": False, "reasoning": str(e)}

    @staticmethod
    def validate_product_domain(url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Alias for validate_url_domain for backward compatibility.
        """
        return UtilityOrchestrator.validate_url_domain(url, state)

    @staticmethod
    def validate_product_url(url: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Alias for validate_url_domain for backward compatibility.
        """
        return UtilityOrchestrator.validate_url_domain(url, state)
    
    @staticmethod
    def extract_order_details_from_message(message: str, field_type: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Extract order details from customer message.
        
        Args:
            message: Customer message to extract from
            field_type: Type of field to extract ('size', 'quantity', 'phone', 'name', 'address', 'postal_code', 'product_selection')
            state: Current state dictionary
            
        Returns:
            Dictionary with extracted field value
        """
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Extracting {field_type} from message {message}", "debug")
        
        try:
            message_lower = message.lower().strip()
            
            if field_type == "size":
                # Size patterns: S, M, L, XL, XXL, or numeric like 28, 30, 32
                letter_sizes = ['xxxl', '3xl', 'xxl', '2xl', 'xl', 'l', 'm', 's', 'xs']
                for size in letter_sizes:
                    if re.search(rf'\b{size}\b', message_lower):
                        return {"found": True, "field": "size", "value": size.upper()}
                
                # Numeric sizes
                numeric_match = re.search(r'\b(2[6-9]|3[0-9]|4[0-8])\b', message)
                if numeric_match:
                    return {"found": True, "field": "size", "value": numeric_match.group(1)}
                    
            elif field_type == "quantity":
                # Extract quantity
                qty_patterns = [
                    r'\b(\d+)\s*(?:pcs?|pieces?|items?|qty|quantity)\b',
                    r'\b(?:qty|quantity)\s*[:=]?\s*(\d+)\b',
                    r'\bneed\s+(\d+)\b',
                    r'\bwant\s+(\d+)\b'
                ]
                for pattern in qty_patterns:
                    match = re.search(pattern, message_lower)
                    if match:
                        return {"found": True, "field": "quantity", "value": match.group(1)}
                
                # Single digit at start/end might be quantity
                if re.match(r'^\d$', message.strip()):
                    return {"found": True, "field": "quantity", "value": message.strip()}
                    
            elif field_type == "phone":
                # Indian phone patterns
                phone_patterns = [
                    r'\b(?:\+91|91)?[6-9]\d{9}\b',
                    r'\b[6-9]\d{9}\b'
                ]
                for pattern in phone_patterns:
                    match = re.search(pattern, message.replace(" ", "").replace("-", ""))
                    if match:
                        phone = match.group(0)
                        # Normalize to 10 digits
                        if phone.startswith("+91"):
                            phone = phone[3:]
                        elif phone.startswith("91") and len(phone) > 10:
                            phone = phone[2:]
                        return {"found": True, "field": "phone", "value": phone}
                        
            elif field_type == "name":
                # Name is typically 2-4 words without numbers
                if len(message.split()) <= 5 and not re.search(r'\d', message):
                    # Check if it looks like a name (capitalized words)
                    words = message.strip().split()
                    if all(word[0].isupper() or word.lower() in ['and', 'of'] for word in words if word):
                        return {"found": True, "field": "name", "value": message.strip().title()}
                        
            elif field_type == "address":
                # Address typically contains numbers and multiple words
                if len(message) > 15 and re.search(r'\d', message):
                    return {"found": True, "field": "address", "value": message.strip()}
                    
            elif field_type == "postal_code":
                # Extract just the postal code
                pin_match = re.search(r'\b\d{6}\b', message)
                if pin_match:
                    return {"found": True, "field": "postal_code", "value": pin_match.group(0)}
                    
            elif field_type == "product_selection":
                # Numeric selection from list
                if re.match(r'^\s*\d+\s*$', message):
                    return {"found": True, "field": "product_selection", "value": int(message.strip())}
            
            return {"found": False, "field": field_type}
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Extraction failed: {str(e)}", "error")
            return {"found": False, "field": field_type, "error": str(e)}
    
    @staticmethod
    def validate_order_confirmation_response(response: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Validate customer's response to order confirmation.
        
        Args:
            response: Customer's response message
            state: Current state dictionary
            
        Returns:
            Dictionary with response type: confirm, reject, or modify
        """
        log_with_trace_id(state, f"UtilityOrchestrator: Validating confirmation response", "debug")
        
        try:
            response_lower = response.lower().strip()
            
            # Confirmation keywords
            confirm_keywords = ['yes', 'confirm', 'ok', 'okay', 'proceed', 'place order', 
                               'go ahead', 'done', 'correct', 'right', 'haan', 'ha', 'ji']
            
            # Rejection keywords
            reject_keywords = ['no', 'cancel', 'stop', 'wrong', 'don\'t', 'nahi', 'nhi', 
                              'galat', 'band karo', 'mat']
            
            # Modification keywords
            modify_keywords = ['change', 'update', 'modify', 'different', 'wrong size', 
                              'wrong address', 'badlo', 'correct karo']
            
            if any(kw in response_lower for kw in reject_keywords):
                return {
                    "response_type": "reject", 
                    "message": "Customer rejected the order. Clear order state and acknowledge cancellation.",
                    "next_action": "cancel_order"
                }
            
            if any(kw in response_lower for kw in modify_keywords):
                return {
                    "response_type": "modify", 
                    "message": "Customer wants to modify order details. Ask which detail they want to change.",
                    "next_action": "ask_modification"
                }
            
            if any(kw in response_lower for kw in confirm_keywords):
                return {
                    "response_type": "confirm", 
                    "message": "Customer confirmed the order. IMMEDIATELY call create_order() tool NOW to place the order. Do NOT call check_confirmation_response again.",
                    "next_action": "create_order",
                    "instruction": "Call create_order() tool immediately"
                }
            
            return {
                "response_type": "unclear", 
                "message": "Customer response is unclear. Ask for explicit confirmation (Yes/No).",
                "next_action": "ask_confirmation"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Confirmation validation failed: {str(e)}", "error")
            return {"response_type": "unclear", "error": str(e)}
    
    _NO_CUSTOMIZATION_POLICY_MESSAGE = (
        "No customization/alteration policy is configured for this client. "
        "Do NOT state, imply or promise any customization, alteration, shortening "
        "or tailoring capability. Escalate to a human agent instead."
    )

    @staticmethod
    def _is_blank_policy(config: Any) -> bool:
        """
        True when the policy row is absent or carries no usable content.

        A row like ``{"Do you support customization?": "", "Do you support size
        alteration?": ""}`` is truthy as a dict but says nothing, so callers must
        not treat it as an answer. Reporting it as a successful fetch is what let
        an agent fall back on its own assumptions (see
        ``design_docs/RCA_GANT_CUSTOMIZATION_FALSE_PROMISE.md``).
        """
        if config is None:
            return True
        if isinstance(config, str):
            return not config.strip()
        if isinstance(config, dict):
            return not any(str(v).strip() for v in config.values() if v is not None)
        if isinstance(config, (list, tuple, set)):
            return not any(str(v).strip() for v in config if v is not None)
        return not str(config).strip()

    @staticmethod
    async def get_customization_config(state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Get customization/alteration policy configuration.

        The returned policy is the only authoritative source for what the client
        does and does not alter. When no policy is configured this returns
        ``success=False`` / ``policy_found=False`` so the caller can escalate
        rather than guess — a missing policy must never read as a successful fetch.

        Args:
            state: Current state dictionary

        Returns:
            Dictionary with ``success``, ``policy_found`` and ``policy``
        """
        from fashion_bot.config_manager import aget_config

        log_with_trace_id(state, f"UtilityOrchestrator: Fetching customization config", "debug")

        client_id = state.get("client_id") if state else None

        # A missing client_id is tenant misconfiguration, not a config gap — it must
        # escalate rather than return quietly (AGENTS.md §7, Tenant Isolation).
        if not client_id:
            log_with_trace_id(state, "❌ get_customization_config called without client_id", "error")
            report_error(
                "get_customization_config called without a resolvable client_id",
                level="error",
                trace_id=get_trace_id(state),
                agent="product_details",
                config_key="customization_policy",
            )
            return {
                "success": False,
                "policy_found": False,
                "policy": None,
                "message": UtilityOrchestrator._NO_CUSTOMIZATION_POLICY_MESSAGE,
            }

        try:
            config = await aget_config('customization_policy', client_id=client_id)

            if not UtilityOrchestrator._is_blank_policy(config):
                policy = config if isinstance(config, dict) else str(config)
                return {"success": True, "policy_found": True, "policy": policy}

            # client_id resolved fine; the tenant simply has no policy recorded.
            log_with_trace_id(
                state,
                f"⚠️ No customization policy configured for client_id={client_id}",
                "warning",
            )
            return {
                "success": False,
                "policy_found": False,
                "policy": None,
                "message": UtilityOrchestrator._NO_CUSTOMIZATION_POLICY_MESSAGE,
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Config fetch failed: {str(e)}", "error")
            return {
                "success": False,
                "policy_found": False,
                "policy": None,
                "message": UtilityOrchestrator._NO_CUSTOMIZATION_POLICY_MESSAGE,
                "error": str(e),
            }

    @staticmethod
    async def get_product_reviews(
        product_id: str,
        sort: Optional[str] = None,
        sentiment: Optional[str] = None,
        client_id: Optional[str] = None,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Fetch a product's published Judge.me reviews, sorted/filtered.

        Thin pass-through to the Judge.me vendor package
        (judgeme/tools/review_listing.py) -- the orchestration boundary
        product_details_tools_factory's tool wrapper calls through, rather
        than importing the vendor module directly (AGENTS.md: tools reach
        vendor integrations via a factory/orchestrator, not a direct
        import -- get_customization_config and get_available_categories,
        this method's neighbours in the same tool list, both go through this
        same class).

        One call returns the entire batch for a given sort/sentiment; there
        is no position or page argument, and deliberately so (see
        review_listing.py's module docstring). A follow-up "show me more" is
        answered from the batch the caller already holds, not by calling
        again -- only a NEW product, ordering, or sentiment is a new call.

        Args:
            product_id: Shopify product id (external_id on Judge.me's side).
            sort: "top_rated" or "recent", or None when the customer
                expressed no preference -- None resolves to the tenant's
                configured default_review_sort.
            sentiment: "positive", "negative", or None.
            client_id: tenant id -- required, resolves Judge.me credentials.
            state: current conversation state, for trace-id logging.

        Stateless, per AGENTS.md ("Tools Are Stateless - No State
        Mutation"): nothing is written to ``state`` here. The fetch-once
        reuse that makes a re-sort free lives inside review_listing.py,
        keyed by client_id + product id in the shared tiered cache.

        Returns:
            See judgeme/tools/review_listing.py:aget_curated_reviews for the
            full result shape (success/status/reviews/matched_count/etc).
        """
        if not client_id:
            log_with_trace_id(state, "❌ get_product_reviews called without client_id", "error")
            report_error(
                "get_product_reviews called without a resolvable client_id",
                level="error",
                trace_id=get_trace_id(state),
                agent="product_details",
                config_key="judgeme_details",
            )
            return {"success": False, "status": "error", "message": "No client_id resolved"}

        from fashion_bot.judgeme.tools.review_listing import aget_curated_reviews

        # None (customer expressed no preference) resolves to the tenant's
        # configured ordering rather than a global constant -- see
        # aget_judgeme_default_review_sort for why the preference cannot live
        # in the prompt. An unrecognised value resolves the same way: a bad
        # value should land on the tenant's choice, not on a hardcoded one.
        if sort in ("top_rated", "recent"):
            clean_sort = sort
        else:
            from fashion_bot.config_manager import aget_judgeme_default_review_sort
            clean_sort = await aget_judgeme_default_review_sort(client_id=client_id)
        clean_sentiment = sentiment if sentiment in ("positive", "negative") else None
        return await aget_curated_reviews(
            client_id=client_id,
            external_id=str(product_id).strip(),
            sort=clean_sort,
            sentiment=clean_sentiment,
        )

    @staticmethod
    async def get_final_return_exchange_message(
        request_type: str = "return",
        days_since_delivery: int = 1,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Get the final return/exchange message from database configuration.
        Use this AFTER customer confirms they want to proceed with return OR exchange.
        
        Config key: 'after_delivery_return_exchange' in client_configs table
        Config structure:
        {
            "return": "Return message with website and contact details",
            "exchange": "Exchange message with website and contact details", 
            "status": "Status inquiry message",
            "Grace days for product return validity": 7,
            "Grace days for product exchange validity": 7,
            "Response if user tries for same day return as delivery date": "Message for same-day return attempt",
            "Response if user tries for same day exchange as delivery date": "Message for same-day exchange attempt"
        }
        
        For same-day deliveries (0 days ago), returns special message that customer needs to wait 24 hours.
        
        Args:
            request_type: Either 'return' or 'exchange' to get the appropriate message
            state: Current state dictionary
            
        Returns:
            Dictionary with message and status
        """
        from fashion_bot.config_manager import aget_config
        import json as json_module
        
        log_with_trace_id(state, f"UtilityOrchestrator: Getting final {request_type} message", "debug")
        
        try:
            client_id = state.get("client_id") if state else None
            
            # Get after_delivery_return_exchange config from database
            config_raw = await aget_config('after_delivery_return_exchange', client_id=client_id)
            
            # Parse config if it's a string
            config = None
            if config_raw:
                if isinstance(config_raw, str):
                    try:
                        config = json_module.loads(config_raw)
                    except json_module.JSONDecodeError:
                        log_with_trace_id(state, f"⚠️ Config is not valid JSON: {config_raw[:100]}", "warning")
                        config = None
                elif isinstance(config_raw, dict):
                    config = config_raw
            
            if config:
                # Check if same-day delivery (less than 24 hours)
                if days_since_delivery == 0:
                    log_with_trace_id(state, f"⚠️ Same-day delivery detected, returning wait message")
                    # Use configured same-day message or fallback
                    if request_type == "exchange":
                        same_day_msg = config.get("Response if user tries for same day exchange as delivery date", "")
                    else:
                        same_day_msg = config.get("Response if user tries for same day return as delivery date", "")
                    
                    if not same_day_msg:
                        same_day_msg = "Your order was just delivered today. To process a return or exchange, please wait at least 24 hours after delivery. This helps ensure you have enough time to inspect the product. Please reach out to us tomorrow, and we'll be happy to assist you!"
                    
                    return {
                        "success": True,
                        "message": same_day_msg,
                        "is_same_day": True
                    }
                
                # Get the appropriate message based on request type
                # Config keys are 'return' and 'exchange' (not 'return_message' and 'exchange_message')
                if request_type == "exchange":
                    message = config.get("Order Exchange process", "") or config.get("Order exchange process", "")
                else:
                    message = config.get("Order Return process", "") or config.get("Order return process", "")
                
                if message:
                    log_with_trace_id(state, f"✅ Retrieved {request_type} message from config")
                    return {
                        "success": True,
                        "message": message,
                        "request_type": request_type
                    }
                else:
                    log_with_trace_id(state, f"⚠️ No {request_type} message in config, checking status message")
                    # Fallback to status message if specific message not found
                    status_msg = config.get("How to check the status of order return/exchange", "")
                    if status_msg:
                        return {
                            "success": True,
                            "message": status_msg,
                            "request_type": request_type,
                            "is_status_fallback": True
                        }
            
            # Default fallback message if no config found
            default_message = (
                f"To proceed with your {request_type}, please visit our website or contact our customer support team. "
                f"We'll guide you through the process and arrange pickup if needed."
            )
            
            log_with_trace_id(state, f"⚠️ No config found, using default {request_type} message")
            return {
                "success": True,
                "message": default_message,
                "is_default": True
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Error getting {request_type} message: {str(e)}", "error")
            return {
                "success": False,
                "message": f"To proceed with your {request_type}, please contact our customer support team.",
                "error": str(e)
            }
    
    @staticmethod
    def extract_order_id_from_message(message: str, conversation_history: Optional[List] = None, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Extract order ID from message using intelligent parsing.
        Uses shared utility function.
        
        Args:
            message: Customer message to extract from
            conversation_history: Optional list of previous messages
            state: Current state dictionary
            
        Returns:
            Dictionary with extracted order ID
        """
        from fashion_bot.utils.shared_utils import extract_order_id_from_message as extract_id_util
        
        log_with_trace_id(state, f"UtilityOrchestrator: Extracting order ID from message", "debug")
        
        try:
            # Get previous bot message for context if available
            previous_bot_message = ""
            if conversation_history and len(conversation_history) > 0:
                for msg in reversed(conversation_history):
                    if hasattr(msg, 'type') and msg.type == 'ai' and hasattr(msg, 'content'):
                        previous_bot_message = msg.content
                        break
            
            # Use shared utility with LLM-powered extraction
            order_id = extract_id_util(message, use_llm=True, previous_bot_message=previous_bot_message)
            
            if order_id:
                log_with_trace_id(state, f"✅ Extracted order ID: {order_id}")
                return {
                    "found": True,
                    "order_id": order_id,
                    "source": "current_message"
                }
            else:
                log_with_trace_id(state, f"⚠️ No order ID found in message")
                return {
                    "found": False,
                    "order_id": None,
                    "message": "No order ID found in message"
                }
                
        except Exception as e:
            log_with_trace_id(state, f"❌ Order ID extraction failed: {str(e)}", "error")
            return {
                "found": False,
                "order_id": None,
                "error": str(e)
            }
    
    @staticmethod
    def search_order_id_in_conversation(conversation_history: List, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Search conversation history for previously mentioned order IDs.
        
        Args:
            conversation_history: List of conversation messages
            state: Current state dictionary
            
        Returns:
            Dictionary with found order ID
        """
        from fashion_bot.utils.shared_utils import extract_order_id_from_message as extract_id_util
        
        log_with_trace_id(state, f"UtilityOrchestrator: Searching conversation history for order ID", "debug")
        
        try:
            # Search through messages from most recent to oldest
            for i in range(len(conversation_history) - 1, -1, -1):
                msg = conversation_history[i]
                
                if not hasattr(msg, 'content'):
                    continue
                    
                # Skip bot messages for extraction
                if hasattr(msg, 'type') and msg.type == 'ai':
                    continue
                
                # Get previous bot message for context
                previous_bot_message = ""
                if i > 0:
                    prev_msg = conversation_history[i - 1]
                    if hasattr(prev_msg, 'type') and prev_msg.type == 'ai' and hasattr(prev_msg, 'content'):
                        previous_bot_message = prev_msg.content
                
                # Try to extract order ID
                order_id = extract_id_util(msg.content, use_llm=True, previous_bot_message=previous_bot_message)
                
                if order_id:
                    log_with_trace_id(state, f"✅ Found order ID in conversation history: {order_id}")
                    return {
                        "found": True,
                        "order_id": order_id,
                        "source": "conversation_history",
                        "message_index": i
                    }
            
            log_with_trace_id(state, f"⚠️ No order ID found in conversation history")
            return {
                "found": False,
                "order_id": None,
                "message": "No order ID found in conversation history"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Conversation search failed: {str(e)}", "error")
            return {
                "found": False,
                "order_id": None,
                "error": str(e)
            }
    
    @staticmethod
    def extract_phone_from_message(message: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Extract phone number from customer message.
        
        Args:
            message: Customer message
            state: Current state dictionary
            
        Returns:
            Dictionary with extracted phone number
        """
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Extracting phone from message", "debug")
        
        try:
            # Reuse the shared extractor: it strips a well-defined +91/91/0
            # prefix and rejects over-long/typo'd input (returns None) instead
            # of truncating it into a plausible-but-wrong number. The old logic
            # returned an 11-digit typo as-is (or sliced a 91-prefixed number's
            # last 10), feeding a corrupt phone into order lookup.
            from fashion_bot.utils.utils import extract_webchat_phone_candidate

            extracted_phone = extract_webchat_phone_candidate(message)
            if extracted_phone:
                log_with_trace_id(state, f"✅ Extracted phone number: {extracted_phone}")
                return {
                    "found": True,
                    "phone": extracted_phone,
                    "source": "current_message"
                }

            log_with_trace_id(state, f"⚠️ No valid phone number found in message")
            return {
                "found": False,
                "phone": None,
                "message": "No valid phone number (10+ digits) found in message"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Phone extraction failed: {str(e)}", "error")
            return {
                "found": False,
                "phone": None,
                "error": str(e)
            }
    
    @staticmethod
    def get_phone_from_state(state: Dict) -> Dict[str, Any]:
        """
        Get phone number from conversation state or sender info.
        
        Args:
            state: Current state dictionary
            
        Returns:
            Dictionary with phone number from state
        """
        log_with_trace_id(state, f"UtilityOrchestrator: Getting phone from state", "debug")
        
        try:
            # Check if user already has a phone number in state
            if state.get("phone_number"):
                phone = state.get("phone_number")
                log_with_trace_id(state, f"✅ Found phone in state: {phone}")
                return {
                    "found": True,
                    "phone": phone,
                    "source": "state_phone_number"
                }
            
            # Try to get from sender info (from Gupshup API)
            if state.get("sender_phone"):
                phone = state.get("sender_phone")
                log_with_trace_id(state, f"✅ Found sender phone from API: {phone}")
                return {
                    "found": True,
                    "phone": phone,
                    "source": "sender_phone_api"
                }
            
            # Check messages for user's phone
            messages_list = state.get("messages", [])
            if messages_list:
                for msg in reversed(messages_list):
                    if hasattr(msg, 'content') and hasattr(msg, 'type') and msg.type == 'human':
                        result = UtilityOrchestrator.extract_phone_from_message(msg.content, state)
                        if result.get("found"):
                            log_with_trace_id(state, f"✅ Found phone in message history")
                            return {
                                "found": True,
                                "phone": result.get("phone"),
                                "source": "message_history"
                            }
            
            log_with_trace_id(state, f"⚠️ No phone number found in state or messages")
            return {
                "found": False,
                "phone": None,
                "message": "No phone number found in state or conversation"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Failed to get phone from state: {str(e)}", "error")
            return {
                "found": False,
                "phone": None,
                "error": str(e)
            }
    
    @staticmethod
    def analyze_escalation_patterns(message: str, conversation_history: List, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        AI-powered analysis of cancellation threats and urgency patterns.
        NO keyword matching - uses LLM for intelligent detection.
        
        Args:
            message: Current customer message
            conversation_history: List of recent messages
            state: Current state dictionary
            
        Returns:
            Dictionary with escalation analysis results
        """
        from fashion_bot.core.llm_factory import LLMFactory
        import json
        import re
        
        log_with_trace_id(state, f"UtilityOrchestrator: Analyzing escalation patterns with LLM", "debug")
        
        try:
            # Extract recent user messages for context
            recent_user_messages = []
            for msg in conversation_history:
                if hasattr(msg, 'type') and msg.type == 'human' and hasattr(msg, 'content'):
                    recent_user_messages.append(msg.content)
            
            # Take last 6 messages
            recent_user_messages = recent_user_messages[-6:] if len(recent_user_messages) > 6 else recent_user_messages
            
            analysis_prompt = f"""
Analyze the customer's current message and recent conversation history to identify urgent situations requiring escalation.

Current message: "{message}"

Recent customer messages (for context):
{chr(10).join([f"- {msg}" for msg in recent_user_messages])}

Analyze for TWO specific patterns:

1. CANCELLATION THREAT: A conditional threat where customer says they will cancel IF certain conditions aren't met.
   Examples: "I will cancel if not delivered today", "deliver today or I'll cancel", "cancel if delayed"

2. URGENT DELIVERY PATTERN: Repeated requests for faster/urgent delivery across multiple messages.
   Examples of URGENT requests: "can I get it sooner", "I want it tomorrow", "deliver faster", "urgent", "expedite", "asap", "rush delivery"
   NOT urgent: "when will I get my order", "order status", "where is my order" (these are normal status inquiries)

Instructions:
- Count ONLY messages that explicitly request faster/urgent delivery, NOT basic status inquiries
- A cancellation threat is immediate escalation regardless of count
- Urgent delivery requests need 2+ occurrences to escalate
- Be precise in counting - don't include normal status questions as urgent requests

Respond in this EXACT JSON format:
{{
  "is_cancellation_threat": true/false,
  "urgent_delivery_count": number,
  "should_escalate": true/false,
  "escalation_type": "cancellation_threat" | "urgent_delivery" | "none",
  "reason": "brief explanation"
}}
"""
            
            # Get LLM for analysis
            import time as _t
            llm = LLMFactory.get_llm(tool_name="get_order_status", state=state)
            _t0 = _t.monotonic()
            llm_response = llm.invoke(analysis_prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_content = llm_response.content.strip()
            
            log_with_trace_id(state, f"🤖 [LLM] escalation_analysis elapsed_ms={_elapsed} result={response_content[:120]}")
            
            # Extract JSON from response
            json_content = response_content
            if response_content.startswith("```json"):
                json_match = re.search(r'```json\s*\n(.*?)\n```', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
            elif response_content.startswith("```"):
                json_match = re.search(r'```\s*\n(.*?)\n```', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
            else:
                json_match = re.search(r'\{.*\}', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(0)
            
            # Parse JSON result
            analysis_result = json.loads(json_content.strip())
            
            is_cancellation_threat = analysis_result.get("is_cancellation_threat", False)
            urgent_delivery_count = analysis_result.get("urgent_delivery_count", 0)
            should_escalate = analysis_result.get("should_escalate", False)
            escalation_type = analysis_result.get("escalation_type", "none")
            reason = analysis_result.get("reason", "Unknown reason")
            
            log_with_trace_id(state, f"✅ Escalation analysis complete: {escalation_type} - {reason}")
            
            return {
                "success": True,
                "is_cancellation_threat": is_cancellation_threat,
                "urgent_delivery_count": urgent_delivery_count,
                "should_escalate": should_escalate,
                "escalation_type": escalation_type,
                "reason": reason,
                "analysis_method": "llm_powered"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Escalation analysis failed: {str(e)}", "error")
            return {
                "success": False,
                "should_escalate": False,
                "escalation_type": "none",
                "error": str(e),
                "analysis_method": "error_fallback"
            }

    @staticmethod
    async def aanalyze_escalation_patterns(message: str, conversation_history: List, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async version of analyze_escalation_patterns — uses ainvoke for LLM."""
        from fashion_bot.core.llm_factory import LLMFactory
        import json
        import re

        log_with_trace_id(state, f"UtilityOrchestrator: Async analyzing escalation patterns with LLM", "debug")

        try:
            recent_user_messages = []
            for msg in conversation_history:
                if hasattr(msg, 'type') and msg.type == 'human' and hasattr(msg, 'content'):
                    recent_user_messages.append(msg.content)
            recent_user_messages = recent_user_messages[-6:]

            analysis_prompt = f"""
Analyze the customer's current message and recent conversation history to identify urgent situations requiring escalation.

Current message: "{message}"

Recent customer messages (for context):
{chr(10).join([f"- {msg}" for msg in recent_user_messages])}

Analyze for TWO specific patterns:

1. CANCELLATION THREAT: A conditional threat where customer says they will cancel IF certain conditions aren't met.
   Examples: "I will cancel if not delivered today", "deliver today or I'll cancel", "cancel if delayed"

2. URGENT DELIVERY PATTERN: Repeated requests for faster/urgent delivery across multiple messages.
   Examples of URGENT requests: "can I get it sooner", "I want it tomorrow", "deliver faster", "urgent", "expedite", "asap", "rush delivery"
   NOT urgent: "when will I get my order", "order status", "where is my order" (these are normal status inquiries)

Instructions:
- Count ONLY messages that explicitly request faster/urgent delivery, NOT basic status inquiries
- A cancellation threat is immediate escalation regardless of count
- Urgent delivery requests need 2+ occurrences to escalate
- Be precise in counting - don't include normal status questions as urgent requests

Respond in this EXACT JSON format:
{{
  "is_cancellation_threat": true/false,
  "urgent_delivery_count": number,
  "should_escalate": true/false,
  "escalation_type": "cancellation_threat" | "urgent_delivery" | "none",
  "reason": "brief explanation"
}}
"""
            import time as _t
            llm = await LLMFactory.aget_llm(tool_name="get_order_status", state=state)
            _t0 = _t.monotonic()
            llm_response = await llm.ainvoke(analysis_prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_content = llm_response.content.strip()

            log_with_trace_id(state, f"🤖 [LLM] async_escalation_analysis elapsed_ms={_elapsed} result={response_content[:120]}")

            json_content = response_content
            if response_content.startswith("```json"):
                json_match = re.search(r'```json\s*\n(.*?)\n```', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
            elif response_content.startswith("```"):
                json_match = re.search(r'```\s*\n(.*?)\n```', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(1).strip()
            else:
                json_match = re.search(r'\{.*\}', response_content, re.DOTALL)
                if json_match:
                    json_content = json_match.group(0)

            analysis_result = json.loads(json_content.strip())

            return {
                "success": True,
                "is_cancellation_threat": analysis_result.get("is_cancellation_threat", False),
                "urgent_delivery_count": analysis_result.get("urgent_delivery_count", 0),
                "should_escalate": analysis_result.get("should_escalate", False),
                "escalation_type": analysis_result.get("escalation_type", "none"),
                "reason": analysis_result.get("reason", "Unknown reason"),
                "analysis_method": "llm_powered"
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Escalation analysis failed: {str(e)}", "error")
            return {
                "success": False,
                "should_escalate": False,
                "escalation_type": "none",
                "error": str(e),
                "analysis_method": "error_fallback"
            }
    
    @staticmethod
    def check_if_exchange_query(message: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Detect if customer is asking about exchange orders.
        Uses LLM for intelligent detection, not keyword matching.
        
        Args:
            message: Customer message
            state: Current state dictionary
            
        Returns:
            Dictionary with exchange query detection result
        """
        from fashion_bot.core.llm_factory import LLMFactory
        
        log_with_trace_id(state, f"UtilityOrchestrator: Checking if exchange query", "debug")
        
        try:
            detection_prompt = f"""
Analyze if the customer is specifically asking about an exchange order status.

Customer message: "{message}"

Look for terms like:
- "exchange order"
- "exchange kab aayega" 
- "where is my exchange"
- "when will my exchange arrive"
- "status of my exchange"
- "exchange delivery status"

Respond with ONLY "YES" if this is specifically about exchange order status, or "NO" if it's a general order query.

Answer: """
            
            import time as _t
            llm = LLMFactory.get_llm(tool_name="get_order_status", state=state)
            _t0 = _t.monotonic()
            llm_response = llm.invoke(detection_prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_content = llm_response.content.strip().upper()
            
            is_exchange_query = "YES" in response_content
            
            log_with_trace_id(state, f"🤖 [LLM] exchange_detection elapsed_ms={_elapsed} result={is_exchange_query}")
            
            return {
                "is_exchange_query": is_exchange_query,
                "confidence": "high" if "YES" == response_content else "medium",
                "raw_response": response_content
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Exchange query detection failed: {str(e)}", "error")
            return {
                "is_exchange_query": False,
                "error": str(e)
            }
    
    @staticmethod
    async def acheck_if_exchange_query(message: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """Async version of check_if_exchange_query — uses ainvoke to avoid blocking."""
        from fashion_bot.core.llm_factory import LLMFactory

        log_with_trace_id(state, f"UtilityOrchestrator: Async checking if exchange query", "debug")

        try:
            detection_prompt = f"""
Analyze if the customer is specifically asking about an exchange order status.

Customer message: "{message}"

Look for terms like:
- "exchange order"
- "exchange kab aayega"
- "where is my exchange"
- "when will my exchange arrive"
- "status of my exchange"
- "exchange delivery status"

Respond with ONLY "YES" if this is specifically about exchange order status, or "NO" if it's a general order query.

Answer: """

            import time as _t
            llm = await LLMFactory.aget_llm(tool_name="get_order_status", state=state)
            _t0 = _t.monotonic()
            ainvoke = getattr(llm, "ainvoke", None)
            if callable(ainvoke):
                llm_response = await ainvoke(detection_prompt)
            else:
                import asyncio
                llm_response = await asyncio.to_thread(llm.invoke, detection_prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_content = llm_response.content.strip().upper()

            is_exchange_query = "YES" in response_content

            log_with_trace_id(state, f"🤖 [LLM] async_exchange_detection elapsed_ms={_elapsed} result={is_exchange_query}")

            return {
                "is_exchange_query": is_exchange_query,
                "confidence": "high" if "YES" == response_content else "medium",
                "raw_response": response_content
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Async exchange query detection failed: {str(e)}", "error")
            return {
                "is_exchange_query": False,
                "error": str(e)
            }

    @staticmethod
    def check_if_delivery_estimate_query(message: str, order_status: str, state: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Detect delivery estimate requests for unshipped orders.
        
        Args:
            message: Customer message
            order_status: Current order status
            state: Current state dictionary
            
        Returns:
            Dictionary with delivery estimate query detection result
        """
        log_with_trace_id(state, f"UtilityOrchestrator: Checking if delivery estimate query", "debug")
        
        try:
            # Check if order is not yet shipped
            not_shipped_statuses = ['not yet shipped', 'not yet dispatched', 'processing', 'new', 'pending shipment']
            is_unshipped = any(status in order_status.lower() for status in not_shipped_statuses)
            
            # Check if message contains delivery date inquiry
            delivery_keywords = [
                "when will it deliver", "delivery date", "estimated delivery", 
                "when will i get", "expected delivery", "when will it arrive",
                "delivery time", "kab milega", "kab aayega"
            ]
            is_delivery_query = any(keyword in message.lower() for keyword in delivery_keywords)
            
            is_estimate_query = is_unshipped and is_delivery_query
            
            log_with_trace_id(state, f"✅ Delivery estimate query: {is_estimate_query} (unshipped={is_unshipped}, delivery_query={is_delivery_query})")
            
            return {
                "is_delivery_estimate_query": is_estimate_query,
                "is_unshipped": is_unshipped,
                "is_delivery_query": is_delivery_query,
                "order_status": order_status
            }
            
        except Exception as e:
            log_with_trace_id(state, f"❌ Delivery estimate detection failed: {str(e)}", "error")
            return {
                "is_delivery_estimate_query": False,
                "error": str(e)
            }
    
    @staticmethod
    async def aget_recent_actionable_orders(
        phone_number: str,
        state: Optional[Dict] = None,
        limit: int = 3,
        cached_orders: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        from fashion_bot.utils.utils import log_with_trace_id
        from fashion_bot.core.factory import ServiceFactory
        from datetime import datetime

        log_with_trace_id(state, f"UtilityOrchestrator: Async fetching recent actionable orders for {phone_number}", "debug")

        try:
            if not phone_number:
                return {
                    "success": False,
                    "message": "Phone number not provided",
                    "orders": []
                }

            # See aget_recent_orders_all_statuses: reuse a caller's fetch when it
            # already has this customer's orders.
            if cached_orders is None:
                order_service = await ServiceFactory.aget_order_service(state=state)
                orders = await order_service.aget_orders_by_customer_phone(phone_number, limit=20, state=state)
            else:
                orders = cached_orders

            if not orders or not isinstance(orders, list):
                return {
                    "success": False,
                    "message": "No orders found for this customer.",
                    "orders": []
                }

            actionable_orders = []
            checked_count = 0
            max_orders_to_check = 3

            for order in orders:
                if len(actionable_orders) >= limit:
                    break

                checked_count += 1
                if checked_count > max_orders_to_check:
                    break

                if order.get("cancelled_at"):
                    continue

                status = order.get("status", "").lower()
                if status in ["delivered", "cancelled", "rto"]:
                    continue

                financial_status = order.get("financial_status", "").lower()
                if financial_status in ["refunded", "voided"]:
                    continue

                partner_status = order.get("partner_status", "")
                if partner_status:
                    shiprocket_upper = partner_status.upper()
                    if shiprocket_upper in ["CANCELED", "CANCELLED", "DELIVERED", "RTO DELIVERED", "RTO_DELIVERED"]:
                        continue

                actionable_orders.append(order)

            if not actionable_orders:
                return {
                    "success": False,
                    "message": "No actionable orders found. All your orders have either been delivered, cancelled, or are in return process.",
                    "orders": [],
                    "suggestion": "For delivered orders, you can request a return or exchange as per our return/exchange policy."
                }

            recent_orders = sorted(actionable_orders, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
            for order in recent_orders:
                created_at = order.get("created_at", "N/A")
                try:
                    if created_at and created_at != "N/A":
                        date_obj = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                        order["formatted_date"] = date_obj.strftime("%d %b %Y")
                    else:
                        order["formatted_date"] = "N/A"
                except Exception:
                    order["formatted_date"] = created_at if created_at else "N/A"

            return {
                "success": True,
                "message": "Found actionable orders",
                "total_actionable_orders": len(actionable_orders),
                "orders": recent_orders,
                "showing_top": len(recent_orders)
            }
        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching recent orders: {str(e)}", "error")
            return {
                "success": False,
                "message": f"Unable to fetch recent orders. Error: {str(e)}",
                "orders": []
            }

    @staticmethod
    async def aget_recent_orders_all_statuses(
        phone_number: str,
        state: Optional[Dict] = None,
        limit: int = 3,
        cached_orders: Optional[List[Dict]] = None,
    ) -> Dict[str, Any]:
        """Return raw Shopify orders (all statuses) with formatted_date added.

        Unlike the previous implementation that returned a simplified dict,
        this now returns raw order objects so downstream consumers (tool
        factory sanitization) can access ``cancelled_at``, ``tags``,
        ``fulfillments``, ``line_items``, etc.  This mirrors the format
        used by ``aget_recent_actionable_orders``.
        """
        from fashion_bot.utils.utils import log_with_trace_id
        from fashion_bot.core.factory import ServiceFactory
        from datetime import datetime

        log_with_trace_id(state, f"UtilityOrchestrator: Async fetching recent orders (all statuses) for {phone_number}", "debug")

        try:
            if not phone_number:
                return {"success": False, "message": "Phone number not provided", "orders": []}

            # ``cached_orders`` lets a caller that already fetched this customer's
            # orders (e.g. the order-access verification gate) hand them over so
            # the same phone->orders lookup isn't issued twice in one turn. That
            # lookup costs ~2 sequential Shopify round-trips and draws on the
            # shared per-shop rate-limit budget.
            if cached_orders is None:
                order_service = await ServiceFactory.aget_order_service(state=state)
                orders = await order_service.aget_orders_by_customer_phone(phone_number, limit=limit + 5, state=state)
            else:
                orders = cached_orders

            if not orders or not isinstance(orders, list):
                return {"success": False, "message": "No orders found for this customer.", "orders": []}

            recent_orders = sorted(orders, key=lambda o: o.get("created_at", ""), reverse=True)[:limit]

            for order in recent_orders:
                created_at = order.get("created_at", "")
                try:
                    if created_at:
                        date_obj = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        order["formatted_date"] = date_obj.strftime("%d %b %Y")
                    else:
                        order["formatted_date"] = "N/A"
                except Exception:
                    order["formatted_date"] = created_at if created_at else "N/A"

            log_with_trace_id(state, f"✅ Found {len(orders)} total orders, returning top {len(recent_orders)}")
            return {
                "success": True,
                "message": "Found orders",
                "total_orders": len(orders),
                "orders": recent_orders,
                "showing_top": len(recent_orders),
            }
        except Exception as e:
            log_with_trace_id(state, f"❌ Error fetching recent orders (all statuses): {str(e)}", "error")
            return {
                "success": False,
                "message": f"Unable to fetch recent orders. Error: {str(e)}",
                "orders": [],
            }


class OrderDataOrchestrator:
    """
    Orchestrator for fetching raw order data from order systems.
    Used when you need order-system specific data like financial status, payment gateway.
    """

    @staticmethod
    async def avalidate_phone_for_order(
        provided_phone: str,
        order_id: str,
        state: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """
        Validate if the provided phone number matches the order's phone number.
        Uses ServiceFactory to get the appropriate async order service.

        Args:
            provided_phone: Phone number provided by the user
            order_id: Order ID to validate against
            state: Current state dictionary

        Returns:
            Dictionary with validation result
        """
        from fashion_bot.utils.phone_number_utils import validate_phone_for_order_access

        log_with_trace_id(state, f"OrderDataOrchestrator: Async validating phone for order {order_id}")

        try:
            primary_vendor = ServiceFactory.get_primary_vendor(state)
            order_service = await ServiceFactory.aget_order_service(state=state, vendor=primary_vendor)

            if not order_service:
                return {
                    "valid": False,
                    "error": "Order service not configured",
                    "order_phone": "",
                }

            order_details = _order_record_to_mapping(
                _unwrap_primary_order_record(
                    await order_service.aget_order_details(order_id, state=state),
                    primary_vendor,
                )
            )

            if not order_details or order_details.get("error"):
                return {
                    "valid": False,
                    "error": order_details.get("error", "Order not found") if order_details else "Order not found",
                    "order_phone": "",
                }

            order_phone = (
                order_details.get("shipping_address", {}).get("phone", "")
                or order_details.get("phone", "")
                or order_details.get("customer_phone", "")
                or order_details.get("billing_address", {}).get("phone", "")
            )

            if not order_phone:
                return {
                    "valid": False,
                    "error": "Order phone number not found",
                    "order_phone": "",
                }

            is_valid, message = validate_phone_for_order_access(provided_phone, order_phone)

            log_with_trace_id(state, f"Phone validation result: {message}")

            return {
                "valid": is_valid,
                "message": message,
                "order_phone": order_phone[-4:] if order_phone else "",
                "order_id": order_id,
            }

        except Exception as e:
            log_with_trace_id(state, f"❌ Phone validation failed: {str(e)}", "error")
            return {
                "valid": False,
                "error": str(e),
                "order_phone": "",
            }


class AnalysisOrchestrator:
    """
    Orchestrator for message analysis and pattern detection.
    Uses a SINGLE unified LLM call to analyze all aspects of the message.
    Results are cached in state to avoid repeated LLM calls.
    """
    
    # Cache key for storing analysis results in state
    _CACHE_KEY = "_analysis_cache"
    
    @staticmethod
    def _get_llm():
        """Get LLM instance for analysis.

        Routes through LLMFactory so llm.* metrics get tagged with
        caller="analysis_orchestrator". Previously imported the
        module-level `llm` singleton from fashion_bot.llm_config which
        was created with NO tool_name → caller="unknown" forever.
        """
        from fashion_bot.core.llm_factory import LLMFactory
        return LLMFactory.get_llm(tool_name="analysis_orchestrator")
    
    @staticmethod
    def _get_cached_analysis(message: str, state: Optional[Dict] = None) -> Optional[Dict]:
        """Get cached analysis results if available for the same message."""
        if not state:
            return None
        cache = state.get(AnalysisOrchestrator._CACHE_KEY, {})
        if cache.get("message") == message:
            return cache.get("result")
        return None
    
    @staticmethod
    def _cache_analysis(message: str, result: Dict, state: Optional[Dict] = None):
        """Cache analysis results in state."""
        if state is not None:
            state[AnalysisOrchestrator._CACHE_KEY] = {
                "message": message,
                "result": result
            }
    
    @staticmethod
    def analyze_message(
        message: str,
        conversation_context: str = "",
        recent_messages: Optional[List[str]] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """
        Perform comprehensive analysis of customer message with a SINGLE LLM call.
        All other methods in this class should call this method and extract their specific fields.
        
        Returns a dictionary with ALL analysis results:
        - preference: return|exchange|unclear
        - is_persistent: bool
        - wants_bank_transfer: bool
        - is_pickup_query: bool
        - pickup_intent_type: status|schedule|general|none
        - reason_category: size_issue|defective|color_issue|quality_issue|wrong_product|not_as_expected|change_of_mind|other
        - reason_details: str
        - can_exchange_fix: bool
        - is_confirmed: bool
        - is_exchange_order_query: bool
        - is_cancellation_threat: bool
        - wants_discount: bool
        """
        # Check cache first
        cached = AnalysisOrchestrator._get_cached_analysis(message, state)
        if cached:
            log_with_trace_id(state, f"AnalysisOrchestrator: Using cached analysis result")
            return cached
        
        log_with_trace_id(state, f"AnalysisOrchestrator: Performing unified message analysis via LLM")
        
        # Build context from recent messages
        recent_context = ""
        if recent_messages:
            recent_context = "\n".join([f"- {m}" for m in recent_messages[:5] if isinstance(m, str)])
        
        prompt = f"""Analyze this customer message comprehensively and return ALL analysis results in a single JSON response.

Customer Message: "{message}"
Conversation Context: {conversation_context[:300] if conversation_context else "None"}
Recent Messages:
{recent_context if recent_context else "None"}

Analyze the message for ALL of the following aspects and respond with EXACTLY this JSON format (no other text):

{{
  "preference": "return|exchange|unclear",
  "is_persistent": true|false,
  "wants_bank_transfer": true|false,
  "is_pickup_query": true|false,
  "pickup_intent_type": "status|schedule|general|none",
  "reason_category": "size_issue|defective|color_issue|quality_issue|wrong_product|not_as_expected|change_of_mind|none|other",
  "reason_details": "brief description of reason if provided, else empty string",
  "can_exchange_fix": true|false,
  "is_confirmed": true|false,
  "is_exchange_order_query": true|false,
  "is_cancellation_threat": true|false,
  "wants_discount": true|false
}}

Analysis Rules:
1. **preference**: 
   - "return" = customer wants refund/money back
   - "exchange" = customer wants different size/color/product replacement
   - "unclear" = intent not clear

2. **is_persistent**: Customer sounds frustrated, making repeated requests, urgent/demanding tone

3. **wants_bank_transfer**: Customer mentions bank account, UPI, NEFT, IMPS, Paytm, PhonePe, GPay, direct transfer

4. **is_pickup_query**: Customer asking about return/exchange pickup (courier, collection, pickup status)
   - pickup_intent_type: "status" (asking when), "schedule" (wants to book), "general" (general question), "none"

5. **reason_category** (why they want return/exchange):
   - "size_issue": too small, too big, doesn't fit, tight, loose
   - "defective": damaged, torn, broken, stained, faulty
   - "color_issue": wrong color, color doesn't match
   - "quality_issue": poor quality, material issue, cheap looking
   - "wrong_product": received different item than ordered
   - "not_as_expected": doesn't match description/photos
   - "change_of_mind": simply doesn't want it anymore
   - "none": no reason provided yet
   - "other": unclear or different reason

6. **can_exchange_fix**: true if reason is size_issue, color_issue, wrong_product, or defective

7. **is_confirmed**: Customer is confirming/agreeing to proceed (yes, ok, confirm, proceed, go ahead)

8. **is_exchange_order_query**: Asking about status of a PREVIOUSLY placed exchange/replacement order (not requesting new exchange)

9. **is_cancellation_threat**: Customer threatening to cancel order or refuse delivery

10. **wants_discount**: Customer asking about discounts, offers, coupons, promo codes"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            response = llm.invoke(prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
            
            log_with_trace_id(state, f"🤖 [LLM] analyze_conversation elapsed_ms={_elapsed}")
            
            # Extract JSON from response
            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)
                
                # Ensure all fields have defaults
                analysis_result = {
                    "preference": result.get("preference", "unclear"),
                    "is_persistent": result.get("is_persistent", False),
                    "wants_bank_transfer": result.get("wants_bank_transfer", False),
                    "is_pickup_query": result.get("is_pickup_query", False),
                    "pickup_intent_type": result.get("pickup_intent_type", "none"),
                    "reason_category": result.get("reason_category", "none"),
                    "reason_details": result.get("reason_details", ""),
                    "can_exchange_fix": result.get("can_exchange_fix", False),
                    "is_confirmed": result.get("is_confirmed", False),
                    "is_exchange_order_query": result.get("is_exchange_order_query", False),
                    "is_cancellation_threat": result.get("is_cancellation_threat", False),
                    "wants_discount": result.get("wants_discount", False)
                }
                
                # Cache the result
                AnalysisOrchestrator._cache_analysis(message, analysis_result, state)
                
                log_with_trace_id(state, f"✅ Analysis complete: preference={analysis_result['preference']}, reason={analysis_result['reason_category']}")
                
                return analysis_result
                
        except Exception as e:
            log_with_trace_id(state, f"❌ LLM analysis error: {str(e)}", "error")
        
        # Return defaults on error
        default_result = {
            "preference": "unclear",
            "is_persistent": False,
            "wants_bank_transfer": False,
            "is_pickup_query": False,
            "pickup_intent_type": "none",
            "reason_category": "none",
            "reason_details": "",
            "can_exchange_fix": False,
            "is_confirmed": False,
            "is_exchange_order_query": False,
            "is_cancellation_threat": False,
            "wants_discount": False
        }
        return default_result
    
    @staticmethod
    async def aanalyze_message(
        message: str,
        conversation_context: str = "",
        recent_messages: Optional[List[str]] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_message — uses ainvoke to avoid blocking the event loop."""
        # Check cache first
        cached = AnalysisOrchestrator._get_cached_analysis(message, state)
        if cached:
            log_with_trace_id(state, f"AnalysisOrchestrator: Using cached analysis result")
            return cached

        log_with_trace_id(state, f"AnalysisOrchestrator: Performing unified message analysis via async LLM")

        recent_context = ""
        if recent_messages:
            recent_context = "\n".join([f"- {m}" for m in recent_messages[:5] if isinstance(m, str)])

        prompt = f"""Analyze this customer message comprehensively and return ALL analysis results in a single JSON response.

Customer Message: "{message}"
Conversation Context: {conversation_context[:300] if conversation_context else "None"}
Recent Messages:
{recent_context if recent_context else "None"}

Analyze the message for ALL of the following aspects and respond with EXACTLY this JSON format (no other text):

{{
  "preference": "return|exchange|unclear",
  "is_persistent": true|false,
  "wants_bank_transfer": true|false,
  "is_pickup_query": true|false,
  "pickup_intent_type": "status|schedule|general|none",
  "reason_category": "size_issue|defective|color_issue|quality_issue|wrong_product|not_as_expected|change_of_mind|none|other",
  "reason_details": "brief description of reason if provided, else empty string",
  "can_exchange_fix": true|false,
  "is_confirmed": true|false,
  "is_exchange_order_query": true|false,
  "is_cancellation_threat": true|false,
  "wants_discount": true|false
}}

Analysis Rules:
1. **preference**:
   - "return" = customer wants refund/money back
   - "exchange" = customer wants different size/color/product replacement
   - "unclear" = intent not clear

2. **is_persistent**: Customer sounds frustrated, making repeated requests, urgent/demanding tone

3. **wants_bank_transfer**: Customer mentions bank account, UPI, NEFT, IMPS, Paytm, PhonePe, GPay, direct transfer

4. **is_pickup_query**: Customer asking about return/exchange pickup (courier, collection, pickup status)
   - pickup_intent_type: "status" (asking when), "schedule" (wants to book), "general" (general question), "none"

5. **reason_category** (why they want return/exchange):
   - "size_issue": too small, too big, doesn't fit, tight, loose
   - "defective": damaged, torn, broken, stained, faulty
   - "color_issue": wrong color, color doesn't match
   - "quality_issue": poor quality, material issue, cheap looking
   - "wrong_product": received different item than ordered
   - "not_as_expected": doesn't match description/photos
   - "change_of_mind": simply doesn't want it anymore
   - "none": no reason provided yet
   - "other": unclear or different reason

6. **can_exchange_fix**: true if reason is size_issue, color_issue, wrong_product, or defective

7. **is_confirmed**: Customer is confirming/agreeing to proceed (yes, ok, confirm, proceed, go ahead)

8. **is_exchange_order_query**: Asking about status of a PREVIOUSLY placed exchange/replacement order (not requesting new exchange)

9. **is_cancellation_threat**: Customer threatening to cancel order or refuse delivery

10. **wants_discount**: Customer asking about discounts, offers, coupons, promo codes"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            ainvoke = getattr(llm, "ainvoke", None)
            if callable(ainvoke):
                response = await ainvoke(prompt)
            else:
                import asyncio
                response = await asyncio.to_thread(llm.invoke, prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()

            log_with_trace_id(state, f"🤖 [LLM] async_analyze_conversation elapsed_ms={_elapsed}")

            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)

                analysis_result = {
                    "preference": result.get("preference", "unclear"),
                    "is_persistent": result.get("is_persistent", False),
                    "wants_bank_transfer": result.get("wants_bank_transfer", False),
                    "is_pickup_query": result.get("is_pickup_query", False),
                    "pickup_intent_type": result.get("pickup_intent_type", "none"),
                    "reason_category": result.get("reason_category", "none"),
                    "reason_details": result.get("reason_details", ""),
                    "can_exchange_fix": result.get("can_exchange_fix", False),
                    "is_confirmed": result.get("is_confirmed", False),
                    "is_exchange_order_query": result.get("is_exchange_order_query", False),
                    "is_cancellation_threat": result.get("is_cancellation_threat", False),
                    "wants_discount": result.get("wants_discount", False)
                }

                AnalysisOrchestrator._cache_analysis(message, analysis_result, state)

                log_with_trace_id(state, f"✅ Async analysis complete: preference={analysis_result['preference']}, reason={analysis_result['reason_category']}")

                return analysis_result

        except Exception as e:
            log_with_trace_id(state, f"❌ Async LLM analysis error: {str(e)}", "error")

        default_result = {
            "preference": "unclear",
            "is_persistent": False,
            "wants_bank_transfer": False,
            "is_pickup_query": False,
            "pickup_intent_type": "none",
            "reason_category": "none",
            "reason_details": "",
            "can_exchange_fix": False,
            "is_confirmed": False,
            "is_exchange_order_query": False,
            "is_cancellation_threat": False,
            "wants_discount": False
        }
        return default_result

    # ==================== WRAPPER METHODS ====================
    # These methods call analyze_message() and extract specific fields
    # They maintain backward compatibility with existing code
    
    @staticmethod
    def detect_return_exchange_preference(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Detect if customer wants return (refund) or exchange."""
        result = AnalysisOrchestrator.analyze_message(message, conversation_context, state=state)
        return {
            "preference": result["preference"],
            "is_persistent": result["is_persistent"],
            "wants_bank_transfer": result["wants_bank_transfer"]
        }
    
    @staticmethod
    def detect_pickup_query(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Detect if customer is asking about return pickup."""
        result = AnalysisOrchestrator.analyze_message(message, conversation_context, state=state)
        return {
            "is_pickup_query": result["is_pickup_query"],
            "pickup_intent_type": result["pickup_intent_type"]
        }
    
    @staticmethod
    def analyze_bank_transfer_preference(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Check if customer wants bank/UPI transfer refund."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        return {
            "wants_bank_transfer": result["wants_bank_transfer"],
            "message": "Customer prefers bank/UPI transfer for refund." if result["wants_bank_transfer"] else "No specific payment preference detected."
        }
    
    @staticmethod
    def get_return_reason(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Extract and categorize the reason for return."""
        result = AnalysisOrchestrator.analyze_message(message, conversation_context, state=state)
        category = result["reason_category"]
        can_fix = result["can_exchange_fix"]
        return {
            "reason_category": category,
            "reason_details": result["reason_details"] or message,
            "can_exchange_fix": can_fix,
            "message": f"Return reason: {category}. Exchange can {'resolve' if can_fix else 'NOT resolve'} this issue."
        }
    
    @staticmethod
    def suggest_exchange_instead_of_return(
        reason_category: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Suggest exchange as alternative to return. Simple lookup - no LLM needed."""
        log_with_trace_id(state, f"AnalysisOrchestrator: Suggesting exchange for {reason_category}")
        
        suggestions = {
            "size_issue": "I understand the size didn't work out. Would you like to exchange it for a different size? We can arrange for the same product in your preferred size.",
            "color_issue": "I see you're not happy with the color. Would you prefer to exchange it for a different color variant? We have other options available.",
            "wrong_product": "I apologize for the wrong product. Would you like us to send the correct item as an exchange? We'll ensure you get exactly what you ordered.",
            "defective": "I'm sorry about the defect. We can arrange an exchange for a new, quality-checked piece. Would that work for you?",
            "quality_issue": "I understand your concern about quality. Would you like to try an exchange? We can send a fresh piece for you to evaluate.",
            "not_as_expected": "I'm sorry the product didn't meet your expectations. Would you consider an exchange for a different style or variant?",
            "change_of_mind": "Would you consider an exchange instead? Perhaps a different size or color might work better for you.",
            "other": "Would you consider an exchange instead of a return? We'd love to help you find something that works better for you."
        }
        
        return {
            "message": suggestions.get(reason_category, suggestions["other"]),
            "reason_category": reason_category
        }
    
    @staticmethod
    def check_customer_exchange_response(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Check if customer accepted or declined exchange suggestion."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        
        # Determine preference based on analysis
        if result["preference"] == "exchange" or result["is_confirmed"]:
            pref = "exchange"
            msg = "Customer accepted the exchange suggestion."
        elif result["preference"] == "return":
            pref = "return"
            msg = "Customer declined exchange, wants to proceed with return."
        else:
            pref = "unclear"
            msg = "Customer's preference is unclear. Need to clarify."
        
        return {"preference": pref, "message": msg}
    
    @staticmethod
    def check_confirmation(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Check if customer has confirmed their request."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        return {
            "is_confirmed": result["is_confirmed"],
            "message": "Customer confirmed the request." if result["is_confirmed"] else "No clear confirmation detected. Please ask customer to confirm."
        }
    
    # ==================== ASYNC WRAPPER METHODS ====================

    @staticmethod
    async def adetect_return_exchange_preference(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of detect_return_exchange_preference."""
        result = await AnalysisOrchestrator.aanalyze_message(message, conversation_context, state=state)
        return {
            "preference": result["preference"],
            "is_persistent": result["is_persistent"],
            "wants_bank_transfer": result["wants_bank_transfer"]
        }

    @staticmethod
    async def adetect_pickup_query(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of detect_pickup_query."""
        result = await AnalysisOrchestrator.aanalyze_message(message, conversation_context, state=state)
        return {
            "is_pickup_query": result["is_pickup_query"],
            "pickup_intent_type": result["pickup_intent_type"]
        }

    @staticmethod
    async def aanalyze_bank_transfer_preference(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_bank_transfer_preference."""
        result = await AnalysisOrchestrator.aanalyze_message(message, state=state)
        return {
            "wants_bank_transfer": result["wants_bank_transfer"],
            "message": "Customer prefers bank/UPI transfer for refund." if result["wants_bank_transfer"] else "No specific payment preference detected."
        }

    @staticmethod
    async def aget_return_reason(
        message: str,
        conversation_context: str = "",
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of get_return_reason."""
        result = await AnalysisOrchestrator.aanalyze_message(message, conversation_context, state=state)
        category = result["reason_category"]
        can_fix = result["can_exchange_fix"]
        return {
            "reason_category": category,
            "reason_details": result["reason_details"] or message,
            "can_exchange_fix": can_fix,
            "message": f"Return reason: {category}. Exchange can {'resolve' if can_fix else 'NOT resolve'} this issue."
        }

    @staticmethod
    async def acheck_customer_exchange_response(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of check_customer_exchange_response."""
        result = await AnalysisOrchestrator.aanalyze_message(message, state=state)
        if result["preference"] == "exchange" or result["is_confirmed"]:
            pref = "exchange"
            msg = "Customer accepted the exchange suggestion."
        elif result["preference"] == "return":
            pref = "return"
            msg = "Customer declined exchange, wants to proceed with return."
        else:
            pref = "unclear"
            msg = "Customer's preference is unclear. Need to clarify."
        return {"preference": pref, "message": msg}

    @staticmethod
    async def acheck_confirmation(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of check_confirmation."""
        result = await AnalysisOrchestrator.aanalyze_message(message, state=state)
        return {
            "is_confirmed": result["is_confirmed"],
            "message": "Customer confirmed the request." if result["is_confirmed"] else "No clear confirmation detected. Please ask customer to confirm."
        }

    @staticmethod
    async def aanalyze_bank_transfer_request(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_bank_transfer_request."""
        result = await AnalysisOrchestrator.aanalyze_message(message, state=state)
        return {
            "wants_bank_transfer": result["wants_bank_transfer"],
            "reasoning": "Customer requested bank/UPI transfer" if result["wants_bank_transfer"] else "No bank transfer preference detected"
        }

    @staticmethod
    async def aanalyze_cancellation_threat(
        message: str,
        recent_messages: List[str] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_cancellation_threat."""
        result = await AnalysisOrchestrator.aanalyze_message(message, recent_messages=recent_messages, state=state)
        return {
            "is_threat": result["is_cancellation_threat"],
            "reasoning": "Customer indicated cancellation intent" if result["is_cancellation_threat"] else "No cancellation threat detected"
        }

    @staticmethod
    async def aanalyze_discount_intent(
        messages: List[str],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_discount_intent."""
        last_message = messages[-1] if messages else ""
        result = await AnalysisOrchestrator.aanalyze_message(last_message, state=state)
        return {
            "wants_discount": result["wants_discount"],
            "matching_keywords": [],
            "reasoning": "Customer looking for discounts/offers" if result["wants_discount"] else "No discount intent detected"
        }

    # ==================== ASYNC STANDALONE METHODS ====================

    @staticmethod
    async def arecommend_size(
        measurements: Dict[str, Any],
        available_sizes: List[str],
        size_guide: Dict[str, Any],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of recommend_size — uses ainvoke to avoid blocking."""
        log_with_trace_id(state, f"AnalysisOrchestrator: Recommending size via async LLM")

        if not measurements or not available_sizes:
            return {
                "recommended_size": None,
                "reasoning": "Insufficient data. Need measurements and available sizes."
            }

        prompt = f"""Recommend the best size based on customer measurements.

Customer Measurements: {json.dumps(measurements) if measurements else 'Not provided'}
Available Sizes: {available_sizes}
Size Guide: {json.dumps(size_guide) if size_guide else 'Not provided'}

Respond with EXACTLY this JSON format (no other text):
{{"recommended_size": "size or null", "reasoning": "explanation"}}"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            ainvoke = getattr(llm, "ainvoke", None)
            if callable(ainvoke):
                response = await ainvoke(prompt)
            else:
                import asyncio
                response = await asyncio.to_thread(llm.invoke, prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
            log_with_trace_id(state, f"🤖 [LLM] async_recommend_size elapsed_ms={_elapsed}")

            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)
                return {
                    "recommended_size": result.get("recommended_size"),
                    "reasoning": result.get("reasoning", "Based on provided measurements")
                }
        except Exception as e:
            log_with_trace_id(state, f"Failed to get async size recommendation: {e}", "error")

        if available_sizes:
            mid_idx = len(available_sizes) // 2
            return {
                "recommended_size": available_sizes[mid_idx],
                "reasoning": "Based on typical sizing. Please check the size chart for exact measurements."
            }

        return {"recommended_size": None, "reasoning": "Could not determine size. Please refer to the size chart."}

    @staticmethod
    async def aanalyze_urgent_delivery_pattern(
        recent_messages: List[str],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Async version of analyze_urgent_delivery_pattern — uses ainvoke."""
        log_with_trace_id(state, f"AnalysisOrchestrator: Analyzing urgent delivery pattern via async LLM")

        if not recent_messages:
            return {
                "urgent_delivery_count": 0,
                "is_pattern": False,
                "should_escalate": False,
                "reasoning": "No messages to analyze"
            }

        messages_text = "\n".join([f"- {m}" for m in recent_messages[:10] if isinstance(m, str)])

        prompt = f"""Count how many times this customer asked about urgent/faster delivery.

Recent Messages:
{messages_text}

ONLY count messages explicitly requesting faster/urgent delivery (urgent, asap, faster, expedite, need it today/tomorrow).
Do NOT count basic status inquiries (where is my order, order status, when will it arrive).

Respond with EXACTLY this JSON format:
{{"urgent_delivery_count": number, "is_pattern": true|false, "should_escalate": true|false, "reasoning": "brief explanation"}}

Rules: is_pattern=true if count>=2, should_escalate=true if count>=3"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            ainvoke = getattr(llm, "ainvoke", None)
            if callable(ainvoke):
                response = await ainvoke(prompt)
            else:
                import asyncio
                response = await asyncio.to_thread(llm.invoke, prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
            log_with_trace_id(state, f"🤖 [LLM] async_urgent_delivery_count elapsed_ms={_elapsed}")

            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)
                count = result.get("urgent_delivery_count", 0)
                return {
                    "urgent_delivery_count": count,
                    "is_pattern": result.get("is_pattern", count >= 2),
                    "should_escalate": result.get("should_escalate", count >= 3),
                    "reasoning": result.get("reasoning", f"Found {count} urgent delivery requests")
                }
        except Exception as e:
            log_with_trace_id(state, f"Failed to async analyze urgent pattern: {e}", "error")

        return {
            "urgent_delivery_count": 0,
            "is_pattern": False,
            "should_escalate": False,
            "reasoning": "Analysis incomplete"
        }

    @staticmethod
    def recommend_size(
        measurements: Dict[str, Any],
        available_sizes: List[str],
        size_guide: Dict[str, Any],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Recommend size based on measurements. Requires separate LLM call due to different inputs."""
        log_with_trace_id(state, f"AnalysisOrchestrator: Recommending size via LLM")
        
        if not measurements or not available_sizes:
            return {
                "recommended_size": None,
                "reasoning": "Insufficient data. Need measurements and available sizes."
            }
        
        prompt = f"""Recommend the best size based on customer measurements.

Customer Measurements: {json.dumps(measurements) if measurements else 'Not provided'}
Available Sizes: {available_sizes}
Size Guide: {json.dumps(size_guide) if size_guide else 'Not provided'}

Respond with EXACTLY this JSON format (no other text):
{{"recommended_size": "size or null", "reasoning": "explanation"}}"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            response = llm.invoke(prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
            log_with_trace_id(state, f"🤖 [LLM] recommend_size elapsed_ms={_elapsed}")
            
            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)
                return {
                    "recommended_size": result.get("recommended_size"),
                    "reasoning": result.get("reasoning", "Based on provided measurements")
                }
        except Exception as e:
            log_with_trace_id(state, f"Failed to get size recommendation: {e}", "error")
        
        # Fallback to middle size
        if available_sizes:
            mid_idx = len(available_sizes) // 2
            return {
                "recommended_size": available_sizes[mid_idx],
                "reasoning": "Based on typical sizing. Please check the size chart for exact measurements."
            }
        
        return {"recommended_size": None, "reasoning": "Could not determine size. Please refer to the size chart."}
    
    @staticmethod
    def analyze_bank_transfer_request(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Analyze if customer wants bank transfer/UPI refund."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        return {
            "wants_bank_transfer": result["wants_bank_transfer"],
            "reasoning": "Customer requested bank/UPI transfer" if result["wants_bank_transfer"] else "No bank transfer preference detected"
        }
    
    @staticmethod
    def analyze_return_confirmation(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Analyze if customer confirmed return/exchange."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        return {
            "is_confirmed": result["is_confirmed"],
            "reasoning": "Customer confirmed the request" if result["is_confirmed"] else "No confirmation detected"
        }
    
    @staticmethod
    def check_exchange_order_query(
        message: str,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Check if asking about exchange order status."""
        result = AnalysisOrchestrator.analyze_message(message, state=state)
        return {
            "is_exchange_query": result["is_exchange_order_query"],
            "reasoning": "Customer asking about exchange order" if result["is_exchange_order_query"] else "Not an exchange order query"
        }
    
    @staticmethod
    def analyze_cancellation_threat(
        message: str,
        recent_messages: List[str] = None,
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Analyze if customer is making a cancellation threat."""
        result = AnalysisOrchestrator.analyze_message(message, recent_messages=recent_messages, state=state)
        return {
            "is_threat": result["is_cancellation_threat"],
            "reasoning": "Customer indicated cancellation intent" if result["is_cancellation_threat"] else "No cancellation threat detected"
        }
    
    @staticmethod
    def analyze_urgent_delivery_pattern(
        recent_messages: List[str],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Analyze urgent delivery pattern. Requires separate call to analyze multiple messages."""
        log_with_trace_id(state, f"AnalysisOrchestrator: Analyzing urgent delivery pattern")
        
        if not recent_messages:
            return {
                "urgent_delivery_count": 0,
                "is_pattern": False,
                "should_escalate": False,
                "reasoning": "No messages to analyze"
            }
        
        messages_text = "\n".join([f"- {m}" for m in recent_messages[:10] if isinstance(m, str)])
        
        prompt = f"""Count how many times this customer asked about urgent/faster delivery.

Recent Messages:
{messages_text}

ONLY count messages explicitly requesting faster/urgent delivery (urgent, asap, faster, expedite, need it today/tomorrow).
Do NOT count basic status inquiries (where is my order, order status, when will it arrive).

Respond with EXACTLY this JSON format:
{{"urgent_delivery_count": number, "is_pattern": true|false, "should_escalate": true|false, "reasoning": "brief explanation"}}

Rules: is_pattern=true if count>=2, should_escalate=true if count>=3"""

        try:
            import time as _t
            llm = AnalysisOrchestrator._get_llm()
            _t0 = _t.monotonic()
            response = llm.invoke(prompt)
            _elapsed = int((_t.monotonic() - _t0) * 1000)
            response_text = response.content.strip() if hasattr(response, 'content') else str(response).strip()
            log_with_trace_id(state, f"🤖 [LLM] urgent_delivery_count elapsed_ms={_elapsed}")
            
            if '{' in response_text:
                json_str = response_text[response_text.find('{'):response_text.rfind('}')+1]
                result = json.loads(json_str)
                count = result.get("urgent_delivery_count", 0)
                return {
                    "urgent_delivery_count": count,
                    "is_pattern": result.get("is_pattern", count >= 2),
                    "should_escalate": result.get("should_escalate", count >= 3),
                    "reasoning": result.get("reasoning", f"Found {count} urgent delivery requests")
                }
        except Exception as e:
            log_with_trace_id(state, f"Failed to analyze urgent pattern: {e}", "error")
        
        return {
            "urgent_delivery_count": 0,
            "is_pattern": False,
            "should_escalate": False,
            "reasoning": "Analysis incomplete"
        }
    
    @staticmethod
    def analyze_discount_intent(
        messages: List[str],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Analyze discount intent from messages."""
        # Use last message for analysis
        last_message = messages[-1] if messages else ""
        result = AnalysisOrchestrator.analyze_message(last_message, state=state)
        return {
            "wants_discount": result["wants_discount"],
            "matching_keywords": [],
            "reasoning": "Customer looking for discounts/offers" if result["wants_discount"] else "No discount intent detected"
        }
    
    @staticmethod
    def detect_discount_intent(
        messages: List[str],
        state: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Detect if customer wants a discount."""
        result = AnalysisOrchestrator.analyze_discount_intent(messages, state)
        return {
            "wants_discount": result["wants_discount"],
            "message": result["reasoning"]
        }


class OrderCreationOrchestrator:
    """
    Orchestrator for order creation operations.
    Routes order creation through the configured order service wrapper.
    """
    
    @staticmethod
    async def acreate_order_in_shopify(
        product_link: str,
        quantity: int,
        requested_size: str,
        phone_number: str,
        customer_name: str,
        customer_address: str,
        state: Optional[Dict] = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        log_with_trace_id(state, f"🛒 OrderCreationOrchestrator: Async creating order for {customer_name}")

        try:
            order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            if not order_service:
                log_with_trace_id(state, "❌ Order service not configured", "error")
                return {"success": False, "error": "Order service not configured"}

            return await order_service.acreate_order(
                {
                    "product_link": product_link,
                    "quantity": quantity,
                    "requested_size": requested_size,
                    "phone_number": phone_number,
                    "customer_name": customer_name,
                    "customer_address": customer_address,
                    "financial_status": financial_status,
                    "payment_gateway_names": payment_gateway_names,
                    "note": note,
                    "transactions": transactions,
                },
                state=state,
            )
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in acreate_order_in_shopify: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def acreate_multi_item_order_in_shopify(
        items: list,
        phone_number: str,
        customer_name: str,
        customer_address: str,
        state: Optional[Dict] = None,
        financial_status: str = None,
        payment_gateway_names: list = None,
        note: str = None,
        transactions: list = None
    ) -> Dict[str, Any]:
        """Create a single Shopify order containing multiple line items.

        ``items`` is a list of ``{product_link, requested_size, quantity,
        variant_id}`` dicts (``variant_id`` optional — when present it pins the
        exact variant instead of re-deriving it from the size label).
        """
        log_with_trace_id(
            state,
            f"🛒 OrderCreationOrchestrator: Async creating multi-item order ({len(items)} items) for {customer_name}",
        )

        try:
            order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            if not order_service:
                log_with_trace_id(state, "❌ Order service not configured", "error")
                return {"success": False, "error": "Order service not configured"}

            return await order_service.acreate_order_multi(
                {
                    "items": items,
                    "phone_number": phone_number,
                    "customer_name": customer_name,
                    "customer_address": customer_address,
                    "financial_status": financial_status,
                    "payment_gateway_names": payment_gateway_names,
                    "note": note,
                    "transactions": transactions,
                },
                state=state,
            )
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in acreate_multi_item_order_in_shopify: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def acreate_draft_order_in_shopify(
        variant_id: str,
        quantity: int,
        phone_number: str,
        state: Optional[Dict] = None,
        note: Optional[str] = None,
        tags: Optional[str] = None,
        use_customer_default_address: bool = True,
    ) -> Dict[str, Any]:
        """Async Shopify draft-order creation for prepaid checkout."""
        import httpx
        from fashion_bot.utils.utils import log_with_trace_id
        from fashion_bot.config_manager import aget_shopify_config
        from fashion_bot.utils.http_client import get_shared_async_http_client
        
        log_with_trace_id(state, f"🛒 OrderCreationOrchestrator: Async creating draft order for phone {phone_number}")
        
        try:
            client_id = state.get("client_id") if state else None
            shopify_config = await aget_shopify_config(client_id=client_id)
            if not shopify_config or not shopify_config.get("access_token"):
                return {"success": False, "error": "Shopify configuration not found"}
            
            access_token = shopify_config["access_token"]
            shop_url = shopify_config["shop_url"]
            api_version = shopify_config.get("api_version", "2024-04")
            shop_domain = shop_url.replace("https://", "").replace("http://", "").rstrip("/")
            clean_variant_id = str(variant_id).replace("gid://shopify/ProductVariant/", "")
            
            formatted_phone = phone_number.strip()
            if not formatted_phone.startswith("+"):
                if formatted_phone.startswith("91"):
                    formatted_phone = f"+{formatted_phone}"
                else:
                    formatted_phone = f"+91{formatted_phone}"
            
            draft_order_payload = {
                "draft_order": {
                    "line_items": [
                        {
                            "variant_id": int(clean_variant_id),
                            "quantity": int(quantity),
                        }
                    ],
                    "customer": {"phone": formatted_phone},
                    "use_customer_default_address": use_customer_default_address,
                    "note": note or "source=whatsapp_bot;intent=checkout",
                    "tags": tags or "bot, prepaid",
                }
            }

            api_url = f"https://{shop_domain}/admin/api/{api_version}/draft_orders.json"
            log_with_trace_id(state, f"📤 Async creating draft order at: {api_url}")
            log_with_trace_id(state, f"📦 Variant ID: {clean_variant_id}, Quantity: {quantity}")
            
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            }
            client = await get_shared_async_http_client()
            response = await client.post(api_url, headers=headers, json=draft_order_payload, timeout=30)

            if response.status_code not in [200, 201]:
                error_msg = f"Shopify API error: {response.status_code}"
                try:
                    error_data = response.json()
                    if "errors" in error_data:
                        error_msg = f"Shopify error: {error_data['errors']}"
                except Exception:
                    error_msg = f"Shopify API error: {response.status_code} - {response.text[:200]}"

                log_with_trace_id(state, f"❌ Draft order creation failed: {error_msg}", "error")
                return {"success": False, "error": error_msg}

            response_data = response.json()
            draft_order = response_data.get("draft_order", {})
            draft_order_id = draft_order.get("id")
            draft_order_name = draft_order.get("name", "")
            invoice_url = draft_order.get("invoice_url")
            total_price = draft_order.get("total_price", "0")
            subtotal_price = draft_order.get("subtotal_price", "0")
            currency = draft_order.get("currency", "INR")
            status = draft_order.get("status", "")
            
            line_items = draft_order.get("line_items", [])
            first_item = line_items[0] if line_items else {}
            product_title = first_item.get("title", "")
            variant_title = first_item.get("variant_title", "")
            item_price = first_item.get("price", "0")
            
            if not invoice_url:
                log_with_trace_id(state, "⚠️ Draft order created but no invoice_url returned", "warning")
                return {
                    "success": False,
                    "error": "Draft order created but checkout URL not available",
                    "draft_order_id": draft_order_id,
                    "draft_order_name": draft_order_name,
                }
            
            log_with_trace_id(state, f"✅ Draft order created: {draft_order_name}")
            log_with_trace_id(state, f"💳 Checkout URL: {invoice_url}")
            
            return {
                "success": True,
                "checkout_url": invoice_url,
                "draft_order_id": draft_order_id,
                "draft_order_name": draft_order_name,
                "total_price": total_price,
                "subtotal_price": subtotal_price,
                "currency": currency,
                "status": status,
                "product_title": product_title,
                "variant_title": variant_title,
                "item_price": item_price,
                "quantity": quantity,
                "message": f"Draft order {draft_order_name} created! Complete payment here: {invoice_url}",
            }

        except httpx.TimeoutException:
            error_msg = "Request timeout while creating draft order"
            log_with_trace_id(state, f"❌ {error_msg}", "error")
            return {"success": False, "error": error_msg}
        except httpx.RequestError as e:
            error_msg = f"Network error while creating draft order: {str(e)}"
            log_with_trace_id(state, f"❌ {error_msg}", "error")
            return {"success": False, "error": error_msg}
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in acreate_draft_order_in_shopify: {str(e)}", "error")
            return {"success": False, "error": str(e)}

    @staticmethod
    async def aclone_order(
        original_order_data: Dict[str, Any],
        new_line_items: List[Dict[str, Any]],
        state: Optional[Dict] = None,
        note: str = "",
        additional_tags: Optional[List[str]] = None,
        financial_status_override: Optional[str] = None,
        payment_gateway_names_override: Optional[List[str]] = None,
        transactions: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Clone an existing order with modified line items.

        Delegates to the configured order service adapter (Shopify / mock).
        Preserves shipping/billing address, customer, discounts, note attrs,
        and tags from the original order.
        """
        log_with_trace_id(state, f"🛒 OrderCreationOrchestrator.aclone_order: cloning {original_order_data.get('name', 'N/A')}")
        try:
            order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            if not order_service:
                log_with_trace_id(state, "❌ Order service not configured", "error")
                return {"success": False, "error": "Order service not configured"}

            return await order_service.aclone_order(
                original_order_data,
                new_line_items,
                state=state,
                note=note,
                additional_tags=additional_tags,
                financial_status_override=financial_status_override,
                payment_gateway_names_override=payment_gateway_names_override,
                transactions=transactions,
            )
        except Exception as e:
            log_with_trace_id(state, f"❌ Exception in aclone_order: {str(e)}", "error")
            return {"success": False, "error": str(e)}
