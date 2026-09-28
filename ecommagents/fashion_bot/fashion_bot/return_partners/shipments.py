"""Shipment leg enrichment for return pickup and exchange delivery."""

from __future__ import annotations

from typing import Any

from fashion_bot.return_partners.models import ShipmentLegStatus


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def extract_return_shipping(request: dict) -> dict:
    raw = request.get("raw") if isinstance(request.get("raw"), dict) else request
    line_items = raw.get("line_items") or raw.get("items") or []
    if not isinstance(line_items, list):
        line_items = []

    for item in line_items:
        if not isinstance(item, dict):
            continue
        shipping_entries = item.get("shipping") or []
        if isinstance(shipping_entries, dict):
            shipping_entries = [shipping_entries]
        if not isinstance(shipping_entries, list):
            continue
        for shipping in shipping_entries:
            if not isinstance(shipping, dict):
                continue
            labels = shipping.get("labels") or []
            label = labels[0] if labels and isinstance(labels[0], dict) else {}
            return {
                "awb": first_non_empty(
                    shipping.get("awb"),
                    shipping.get("tracking_number"),
                    shipping.get("waybill"),
                    shipping.get("tracking_id"),
                    label.get("awb"),
                    label.get("tracking_number"),
                    label.get("waybill"),
                    label.get("tracking_id"),
                ),
                "tracking_url": first_non_empty(
                    shipping.get("tracking_url"),
                    shipping.get("tracking_link"),
                    shipping.get("track_url"),
                    label.get("tracking_url"),
                    label.get("tracking_link"),
                    label.get("track_url"),
                    label.get("label_url"),
                ),
                "raw_status": first_non_empty(
                    shipping.get("status"),
                    shipping.get("delivery_status"),
                    shipping.get("shipment_status"),
                    label.get("status"),
                    label.get("delivery_status"),
                    label.get("shipment_status"),
                ),
                "carrier": first_non_empty(
                    shipping.get("carrier"),
                    shipping.get("courier"),
                    shipping.get("shipping_company"),
                    shipping.get("tracking_company"),
                    label.get("carrier"),
                    label.get("courier"),
                    label.get("shipping_company"),
                    label.get("tracking_company"),
                ),
                "raw": shipping,
            }
    delivery = request.get("delivery") if isinstance(request.get("delivery"), dict) else {}
    shipping = request.get("shipping") if isinstance(request.get("shipping"), dict) else {}
    return {
        "awb": first_non_empty(
            request.get("awb"),
            request.get("tracking_number"),
            request.get("waybill"),
            request.get("tracking_id"),
            shipping.get("awb"),
            shipping.get("tracking_number"),
            shipping.get("waybill"),
            shipping.get("tracking_id"),
        ),
        "tracking_url": first_non_empty(
            request.get("tracking_url"),
            request.get("tracking_link"),
            request.get("track_url"),
            shipping.get("tracking_url"),
            shipping.get("tracking_link"),
            shipping.get("track_url"),
        ),
        "raw_status": first_non_empty(
            delivery.get("status"),
            request.get("shipment_status"),
            request.get("delivery_status"),
            shipping.get("status"),
            shipping.get("delivery_status"),
            shipping.get("shipment_status"),
        ),
        "carrier": first_non_empty(
            request.get("carrier"),
            request.get("courier"),
            request.get("shipping_company"),
            request.get("tracking_company"),
            shipping.get("carrier"),
            shipping.get("courier"),
            shipping.get("shipping_company"),
            shipping.get("tracking_company"),
        ),
        "raw": shipping or delivery,
    }


def build_tracking_url(*, awb: Any, tracking_url: Any = None, carrier: Any = None, partner: Any = None) -> str | None:
    explicit_url = str(tracking_url or "").strip()
    if explicit_url:
        return explicit_url

    awb_text = str(awb or "").strip()
    if not awb_text:
        return None

    partner_text = str(partner or "").strip().lower()
    carrier_text = str(carrier or "").strip().lower()
    if "delhivery" in partner_text or "delhivery" in carrier_text:
        return f"https://www.delhivery.com/track-v2/package/{awb_text}"
    if "shiprocket" in partner_text or "shiprocket" in carrier_text:
        return f"https://shiprocket.co/tracking/{awb_text}"
    return None


def append_tracking_details(message: str, *, awb: Any, tracking_url: Any = None, carrier: Any = None) -> str:
    parts = [message]
    awb_text = str(awb or "").strip()
    if awb_text:
        parts.append(f"Tracking ID: {awb_text}.")
    url = build_tracking_url(awb=awb_text, tracking_url=tracking_url, carrier=carrier)
    if url:
        parts.append(f"Tracking link: {url}")
    return " ".join(part for part in parts if part)


def classify_pickup_status(raw_status: str | None, request_status: str | None) -> dict:
    status_text = str(raw_status or request_status or "").strip()
    normalized = status_text.lower().replace("_", " ")
    if not normalized:
        return {"status": "unknown", "message": "I could not find a pickup status for this return yet."}
    if any(token in normalized for token in ("exception", "failed", "cancel")):
        return {"status": "pickup_issue", "message": "There seems to be an issue with the return pickup."}
    if "return to origin" in normalized or "rto" in normalized or normalized == "delivered":
        return {"status": "return_to_origin", "message": "Your returned item has reached or is reaching the origin facility."}
    if "picked" in normalized or "in transit" in normalized:
        return {"status": "picked_up", "message": "Your return pickup is completed and the item is in transit."}
    if "out for pickup" in normalized:
        return {"status": "out_for_pickup", "message": "Your return pickup is out for pickup."}
    if "scheduled" in normalized:
        return {"status": "pickup_scheduled", "message": "Your return pickup has been scheduled."}
    if "requested" in normalized or "approved" in normalized:
        return {"status": "request_under_process", "message": "Your return request is under process. Pickup details will be updated once scheduled."}
    return {"status": normalized, "message": f"Your return pickup status is {status_text}."}


def _status_from_logistics(result: dict) -> str | None:
    if not isinstance(result, dict):
        return None
    order = None
    orders = result.get("orders")
    if isinstance(orders, list) and orders:
        order = orders[0] if isinstance(orders[0], dict) else None
    order = order or result
    return first_non_empty(
        order.get("shipment_status"),
        order.get("current_status"),
        order.get("status"),
        order.get("tracking_status"),
        result.get("status"),
    )


async def aenrich_return_pickup_leg(
    *,
    request: dict,
    state: dict | None = None,
) -> dict:
    shipping = extract_return_shipping(request)
    awb = shipping.get("awb")
    raw_status = shipping.get("raw_status")
    classification = classify_pickup_status(raw_status, request.get("status"))
    if not awb:
        tracking_url = build_tracking_url(
            awb=None,
            tracking_url=shipping.get("tracking_url"),
            carrier=shipping.get("carrier"),
        )
        return ShipmentLegStatus(
            leg="return_pickup",
            status=classification["status"],
            message=append_tracking_details(
                classification["message"],
                awb=None,
                tracking_url=tracking_url,
                carrier=shipping.get("carrier"),
            ),
            awb=None,
            tracking_url=tracking_url,
            carrier=shipping.get("carrier"),
            raw_status=raw_status,
        ).model_dump()

    from fashion_bot.core.logistics_router import LogisticsRouter

    order_dto = {
        "tracking_url": shipping.get("tracking_url") or "",
        "tracking_company": shipping.get("carrier") or "",
        "awb": awb,
    }
    partner, logistics_result, per_partner = await LogisticsRouter.aget_tracking_first_valid(
        str(awb),
        order_dto,
        state=state,
        fallback_to_all=True,
    )
    live_status = _status_from_logistics(logistics_result)
    final_classification = classify_pickup_status(live_status or raw_status, request.get("status"))
    tracking_url = build_tracking_url(
        awb=awb,
        tracking_url=shipping.get("tracking_url"),
        carrier=shipping.get("carrier"),
        partner=partner,
    )
    return ShipmentLegStatus(
        leg="return_pickup",
        status=final_classification["status"],
        message=append_tracking_details(
            final_classification["message"],
            awb=awb,
            tracking_url=tracking_url,
            carrier=shipping.get("carrier") or partner,
        ),
        awb=str(awb),
        tracking_url=tracking_url,
        carrier=shipping.get("carrier"),
        partner=partner,
        raw_status=live_status or raw_status,
        logistics_result=logistics_result or {},
        per_partner=per_partner or {},
    ).model_dump()
