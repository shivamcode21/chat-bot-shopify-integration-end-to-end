"""JSON -> normalized dict helpers for ClickPost tracking responses.

Shapes here follow ClickPost's published track-order API. Its ``result`` is
keyed by waybill rather than being a list, and each entry carries a
``latest_status``, a ``scans`` history and a ``valid`` flag::

    {"meta": {...},
     "result": {"<waybill>": {"latest_status": {...},
                              "scans": [...],
                              "additional": {...},
                              "valid": true}}}

Status is taken from ``clickpost_status_bucket`` -- ClickPost's own
consolidation of every courier's vocabulary into nine buckets -- rather than
from the free-text ``status`` string, which varies per courier ("Assigned to
a Shadowfax Rider", "Delivered to consignee", ...). ``clickpost_status_code``
is the finer-grained signal underneath it and is preserved in the output for
callers that need it.

This parser is deliberately defensive: malformed input never raises, it
returns a ``parse_error`` dict so an unexpected response degrades gracefully
instead of crashing the order-status flow.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

ORDER_PLACED = "order_placed"
DISPATCHED = "dispatched"
DELIVERED = "delivered"
OUT_FOR_DELIVERY = "out_for_delivery"
IN_TRANSIT = "in_transit"
PICKED_UP = "picked_up"
CANCELLED = "cancelled"
RETURN_TO_ORIGIN = "return_to_origin"
EXCEPTION = "exception"
LOST = "lost"
DAMAGED = "damaged"
UNKNOWN = "unknown"

# ClickPost's clickpost_status_bucket -> our canonical bucket.
# Buckets are documented as: 1 Order Placed, 2 Dispatched, 3 In Transit,
# 4 Out For Delivery, 5 Failed Delivery, 6 Delivered, 7 Returned, 8 Lost,
# 9 Damaged.
_BUCKET_TO_STATUS: Dict[int, str] = {
    1: ORDER_PLACED,
    2: DISPATCHED,
    3: IN_TRANSIT,
    4: OUT_FOR_DELIVERY,
    5: EXCEPTION,
    6: DELIVERED,
    7: RETURN_TO_ORIGIN,
    8: LOST,
    9: DAMAGED,
}

# Fallback only: used when a payload carries no bucket. ClickPost's
# status_code set is wider than the bucket set, so this maps the codes we
# can name confidently and leaves the rest to the bucket/keyword path.
_CODE_TO_STATUS: Dict[int, str] = {
    1: ORDER_PLACED,
    4: DISPATCHED,
    5: IN_TRANSIT,
    6: OUT_FOR_DELIVERY,
    7: EXCEPTION,
    8: DELIVERED,
    9: EXCEPTION,
    16: LOST,
    17: DAMAGED,
}


def normalize_clickpost_status(
    raw_status: Optional[str],
    bucket: Optional[int] = None,
    status_code: Optional[int] = None,
) -> str:
    """Map a ClickPost status to a canonical bucket.

    Prefers ``bucket`` (``clickpost_status_bucket``), then ``status_code``
    (``clickpost_status_code``). ``raw_status`` is a per-courier free-text
    string and is only consulted when neither numeric signal is present.
    """
    if bucket is not None:
        try:
            mapped = _BUCKET_TO_STATUS.get(int(bucket))
            if mapped:
                return mapped
        except (TypeError, ValueError):
            pass

    if status_code is not None:
        try:
            mapped = _CODE_TO_STATUS.get(int(status_code))
            if mapped:
                return mapped
        except (TypeError, ValueError):
            pass

    s = (raw_status or "").strip().upper()
    if not s:
        return UNKNOWN
    if "OUT FOR DELIVERY" in s or s == "OFD":
        return OUT_FOR_DELIVERY
    if "RTO" in s or ("RETURN" in s and ("ORIGIN" in s or "SHIPPER" in s)):
        return RETURN_TO_ORIGIN
    if "DELIVERED" in s:
        return DELIVERED
    if "CANCEL" in s:
        return CANCELLED
    return UNKNOWN


def _scan(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": entry.get("status"),
        "remark": entry.get("remark"),
        "location": entry.get("location"),
        "timestamp": entry.get("timestamp"),
        "clickpost_status_code": entry.get("clickpost_status_code"),
        "clickpost_status_description": entry.get("clickpost_status_description"),
    }


def _error(message: str, payload: Any) -> Dict[str, Any]:
    return {
        "success": False,
        "status": "parse_error",
        "message": message,
        "raw_response": payload,
    }


def parse_clickpost_tracking_response(
    payload: Any,
    waybill: Optional[str] = None,
    allow_single_fallback: bool = True,
) -> Dict[str, Any]:
    """Parse a track-order response into a normalized dict.

    ``waybill`` selects an entry when the response carries several (the API
    accepts up to 15 comma-separated waybills); with one entry it is
    inferred. Never raises -- any failure degrades to a ``parse_error`` dict.

    ``allow_single_fallback`` controls that inference. It is safe when one
    waybill was requested and the response echoes it in a different form, and
    unsafe when several were batched into one request: there a waybill missing
    from the response is a parcel whose status is unknown, not a parcel that
    shares its neighbour's. Callers batching waybills must pass ``False``.

    On success returns::

        {
          "success": True,
          "waybill": <str>,
          "status": <canonical bucket>,
          "raw_status": <courier's own status text>,
          "clickpost_status_code": <int|None>,
          "clickpost_status_bucket": <int|None>,
          "location": <str>,
          "status_date": <str>,
          "courier_partner_edd": <str|None>,
          "ndr": <dict|None>,
          "scans": [...],
        }
    """
    if payload is None:
        return _error("Empty tracking response", payload)

    parsed: Any = payload
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return _error("Empty tracking response", payload)
        try:
            parsed = json.loads(stripped)
        except (json.JSONDecodeError, ValueError) as exc:
            return _error(f"Invalid JSON tracking response: {exc}", payload)

    if not isinstance(parsed, dict):
        return _error("Tracking response is not a JSON object", payload)

    try:
        meta = parsed.get("meta") or {}
        if isinstance(meta, dict) and meta.get("success") is False:
            return _error(meta.get("message") or "ClickPost reported failure", payload)

        result = parsed.get("result")
        if result is None or (isinstance(result, dict) and not result):
            # ClickPost answers an unknown waybill with meta.success=true and a
            # null result -- the call succeeded, the shipment simply is not
            # theirs. That is the same clean miss as valid=false below, and
            # reporting it as an error would make a mistyped AWB look like a
            # partner outage.
            return {
                "success": True,
                "found": False,
                "status": "not_found",
                "waybill": waybill,
                "message": "Waybill not registered with ClickPost",
            }
        if not isinstance(result, dict):
            return _error("No shipment data found in tracking response", payload)

        # result is keyed by waybill; pick the requested one. Falling back to
        # the only record present is a tolerance for a key echoed in a
        # different form, not a licence to answer about a different parcel --
        # see ``allow_single_fallback``.
        if waybill and waybill in result:
            key, record = waybill, result[waybill]
        elif len(result) == 1 and allow_single_fallback:
            key, record = next(iter(result.items()))
        elif waybill:
            # Asked for a specific parcel and the courier did not report it.
            # That is the same clean miss as an unregistered waybill: the
            # caller keeps the parcels that did resolve and says nothing about
            # this one, rather than repeating another parcel's status for it.
            return {
                "success": True,
                "found": False,
                "status": "not_found",
                "waybill": waybill,
                "message": "Waybill absent from tracking response",
            }
        else:
            return _error(
                "Tracking response holds several waybills; none requested", payload,
            )

        if not isinstance(record, dict):
            return _error("Malformed shipment record in tracking response", payload)

        # An unregistered or mistyped AWB comes back with valid=false rather
        # than an error, so it must not be read as a real status.
        if record.get("valid") is False:
            # A clean "not my shipment", not a failure: success stays True so
            # the router classifies it as a miss rather than a partner outage.
            return {
                "success": True,
                "found": False,
                "status": "not_found",
                "waybill": key,
                "message": "Waybill not registered with ClickPost",
            }

        latest = record.get("latest_status")
        latest = latest if isinstance(latest, dict) else {}
        additional = record.get("additional")
        additional = additional if isinstance(additional, dict) else {}

        raw_scans = record.get("scans")
        scans: List[Dict[str, Any]] = [
            _scan(s) for s in raw_scans if isinstance(s, dict)
        ] if isinstance(raw_scans, list) else []

        bucket = latest.get("clickpost_status_bucket")
        code = latest.get("clickpost_status_code")
        raw_status = latest.get("status") or latest.get("clickpost_status_description")

        if not latest and not scans:
            return _error("No shipment data found in tracking response", payload)

        ndr = additional.get("ndr")
        return {
            "success": True,
            "waybill": key,
            "status": normalize_clickpost_status(raw_status, bucket, code),
            "raw_status": raw_status,
            "clickpost_status_code": code,
            "clickpost_status_bucket": bucket,
            "location": latest.get("location"),
            "status_date": latest.get("timestamp"),
            "remark": latest.get("remark"),
            "courier_partner_edd": additional.get("courier_partner_edd"),
            "ndr": ndr if isinstance(ndr, dict) else None,
            "scans": scans,
        }
    except (AttributeError, TypeError, ValueError, KeyError) as exc:
        # Not logged here: this helper is pure and has no trace id to attach.
        # Callers surface the returned parse_error through log_with_trace_id.
        return _error(str(exc), payload)
