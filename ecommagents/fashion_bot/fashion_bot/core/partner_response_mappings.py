"""
Single source of truth for partner API response → canonical field mapping.

This module declares, for every integrated delivery partner and every
endpoint we consume, *exactly* how the partner's raw API/webhook response
maps into our canonical internal shapes (``order_data`` dict, ``event_key``
string, fulfillment-status bucket).

Adding a new partner (e.g. BlueDart):
  1. Append one entry to each section below — ``GET_ORDER_DATA_FIELD_MAPS``,
     ``WEBHOOK_STATUS_TO_EVENT_KEY``, ``FULFILLMENT_STATUS_RULES``,
     ``KNOWN_FULFILLED_STATUSES``, and (if its tracking endpoint shape
     differs from ours) ``RAW_PAYLOAD_EXTRACTORS``.
  2. No code changes needed in adapters, processors, or the orchestrator —
     they all go through the helper functions defined at the bottom.

The mini-spec language for field maps is intentionally tiny: every value is
either a dotted path string, or a tuple whose first element names a
transform (``const``, ``first``, ``template``, ``call``). Anything more
exotic should be expressed as a ``("call", fn)`` entry rather than growing
the DSL.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# A field-map spec is one of:
#   "a.b.c"                          dotted accessor into the raw dict
#   ("const", value)                 literal value
#   ("first", [spec, spec, ...])     first non-empty result
#   ("template", "...{}...", [spec]) Python format string filled from specs
#   ("call", callable)               callable(raw) -> value
Spec = Union[str, Tuple[Any, ...]]


def _get_path(raw: Any, path: str) -> Any:
    """Read a dotted path from a (possibly nested) dict.

    Returns ``None`` if any intermediate node is missing or not a dict.
    Empty path returns ``raw`` itself, so ``("const", x)`` callers can use
    that to mean "the whole payload".
    """
    if raw is None or not path:
        return raw
    cur: Any = raw
    for segment in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(segment)
        else:
            return None
        if cur is None:
            return None
    return cur


def _resolve_spec(raw: Any, spec: Spec) -> Any:
    """Resolve a mapping spec against ``raw`` data."""
    if isinstance(spec, str):
        return _get_path(raw, spec)
    if not isinstance(spec, tuple) or not spec:
        return None
    op = spec[0]
    if op == "const":
        return spec[1] if len(spec) > 1 else None
    if op == "first":
        for sub in (spec[1] or []):
            val = _resolve_spec(raw, sub)
            if val not in (None, "", [], {}):
                return val
        return None
    if op == "template":
        fmt = spec[1]
        args = [_resolve_spec(raw, s) for s in (spec[2] or [])]
        # If any required arg is missing, return empty string so callers can
        # use "first" to fall back. This matches how the old hand-written
        # adapter behaved for the Delhivery tracking_url case.
        if any(a in (None, "") for a in args):
            return ""
        try:
            return fmt.format(*args)
        except Exception:
            return ""
    if op == "call":
        fn = spec[1]
        try:
            return fn(raw)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(f"[MAPPING] callable spec raised {exc!r}")
            return None
    return None


def _set_path(target: Dict[str, Any], path: str, value: Any) -> None:
    """Write a value into ``target`` at a dotted path, creating sub-dicts as needed."""
    if value is None:
        return
    if "." not in path:
        target[path] = value
        return
    segments = path.split(".")
    cur = target
    for segment in segments[:-1]:
        sub = cur.get(segment)
        if not isinstance(sub, dict):
            sub = {}
            cur[segment] = sub
        cur = sub
    cur[segments[-1]] = value


def apply_field_map(raw: Any, field_map: Dict[str, Spec]) -> Dict[str, Any]:
    """Translate ``raw`` into a canonical dict using ``field_map``."""
    out: Dict[str, Any] = {}
    if not isinstance(field_map, dict):
        return out
    for canonical_path, spec in field_map.items():
        value = _resolve_spec(raw, spec)
        if value is not None:
            _set_path(out, canonical_path, value)
    return out


# ═════════════════════════════════════════════════════════════════════
# RAW PAYLOAD EXTRACTORS
# ═════════════════════════════════════════════════════════════════════
# Each entry pulls "the meaningful shipment unit" out of a partner's
# response envelope. For Shiprocket the API already returns flat data,
# so it is identity. For Delhivery the response is nested under
# ``ShipmentData[].Shipment``.

def _extract_delhivery_shipment(payload: Any) -> Optional[Dict[str, Any]]:
    """Pull the first ``Shipment`` block out of a Delhivery packages-json response."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("ShipmentData") or []
    if not items:
        # Some Delhivery responses put the Shipment block at top level.
        if "Shipment" in payload:
            return payload.get("Shipment")
        if "AWB" in payload or "ReferenceNo" in payload:
            return payload
        return None
    first = items[0] or {}
    return first.get("Shipment") or first


RAW_PAYLOAD_EXTRACTORS: Dict[str, Dict[str, Callable[[Any], Optional[Dict[str, Any]]]]] = {
    "get_order_data": {
        "shiprocket": lambda payload: payload,
        "delhivery": _extract_delhivery_shipment,
    },
    "get_tracking_details": {
        "shiprocket": lambda payload: payload,
        "delhivery": _extract_delhivery_shipment,
    },
}


# ═════════════════════════════════════════════════════════════════════
# GET_ORDER_DATA → canonical order_data shape
# ═════════════════════════════════════════════════════════════════════
# The canonical shape (consumed by processors and the orchestrator) is:
#
#   {
#     "shipments": {
#         "awb", "courier", "tracking_url", "etd", "delivered_date",
#     },
#     "products": [{"name", "quantity", "sku"}, ...],
#     "billing_customer_name", "billing_phone", "billing_address",
#     "billing_address_2", "billing_city", "billing_state",
#     "billing_pincode", "billing_country", "billing_email",
#     "customer_phone", "customer_name",
#     "created_at", "updated_at",
#     "etd_date", "delivered_date", "out_for_delivery_date", "cancelled_at",
#     "scans": [{"date", "status", "location", "activity", "sr-status"}],
#   }


# ─── Delhivery callables for the few fields that need light parsing ──

def _delhivery_flat_scans(shipment: Dict[str, Any]) -> List[Dict[str, Any]]:
    scans_raw = shipment.get("Scans") or []
    flat: List[Dict[str, Any]] = []
    for entry in scans_raw:
        scan = (entry or {}).get("ScanDetail") or entry
        if not scan:
            continue
        flat.append({
            "date": scan.get("ScanDateTime") or scan.get("StatusDateTime"),
            "status": scan.get("Scan") or scan.get("Status"),
            "location": scan.get("ScannedLocation") or scan.get("StatusLocation"),
            "activity": scan.get("Instructions") or scan.get("ScanType"),
            "sr-status": scan.get("StatusType"),
        })
    return flat


def _delhivery_delivered_date_from_scans(shipment: Dict[str, Any]) -> Optional[str]:
    for scan in _delhivery_flat_scans(shipment):
        if (scan.get("status") or "").upper() == "DELIVERED":
            return scan.get("date")
    return None


def _delhivery_products(shipment: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = shipment.get("ProductDetails")
    if not name:
        return []
    return [{"name": name, "quantity": 1, "sku": ""}]


GET_ORDER_DATA_FIELD_MAPS: Dict[str, Dict[str, Spec]] = {
    # Shiprocket's `data` block from `/orders/show/{id}` is already our
    # canonical shape. We declare the mapping explicitly anyway so this
    # file documents the contract (which fields we read, with which
    # fallbacks) rather than relying on undocumented passthrough.
    "shiprocket": {
        "shipments.awb":              "shipments.awb",
        "shipments.courier":          ("first", ["shipments.courier", ("const", "N/A")]),
        "shipments.tracking_url":     "shipments.tracking_url",
        "shipments.etd":              "shipments.etd",
        "shipments.delivered_date":   ("first", ["shipments.delivered_date", "delivered_date"]),
        "products":                   "products",
        "billing_customer_name":      ("first", [
            "customer_name", "billing_customer_name", "billing_name", ("const", "Guest"),
        ]),
        "billing_phone":              ("first", [
            "billing_phone", "billing_customer_phone", "customer_phone", "shipping_phone",
        ]),
        "billing_address":            ("first", ["billing_address", "customer_address"]),
        "billing_address_2":          "billing_address_2",
        "billing_city":               ("first", ["billing_city", "customer_city"]),
        "billing_state":              ("first", ["billing_state", "customer_state"]),
        "billing_pincode":            ("first", ["billing_pincode", "customer_pincode"]),
        "billing_country":            ("first", [
            "billing_country", "customer_country", ("const", "India"),
        ]),
        "billing_email":              ("first", ["billing_email", "customer_email"]),
        "created_at":                 "created_at",
        "updated_at":                 "updated_at",
        "etd_date":                   "etd_date",
        "delivered_date":             ("first", ["delivered_date", "shipments.delivered_date"]),
        "out_for_delivery_date":      "out_for_delivery_date",
        "cancelled_at":               "cancelled_at",
    },
    "delhivery": {
        "shipments.awb":              "AWB",
        "shipments.courier":          ("const", "Delhivery"),
        "shipments.tracking_url":     ("template",
                                       "https://www.delhivery.com/track-v2/package/{}",
                                       ["AWB"]),
        "shipments.etd":              "ExpectedDeliveryDate",
        "shipments.delivered_date":   ("call", _delhivery_delivered_date_from_scans),
        "products":                   ("call", _delhivery_products),
        "billing_customer_name":      "Consignee.Name",
        "billing_phone":              ("first", ["Consignee.Telephone1", "Consignee.Telephone2"]),
        "billing_address":            "Consignee.Address1",
        "billing_city":               "Consignee.City",
        "billing_state":              "Consignee.State",
        "billing_pincode":            "Consignee.PinCode",
        "billing_country":            ("first", ["Consignee.Country", ("const", "India")]),
        "created_at":                 ("first", ["OrderDate", "PickedupDate"]),
        "updated_at":                 "Status.StatusDateTime",
        "etd_date":                   "ExpectedDeliveryDate",
        "delivered_date":             ("call", _delhivery_delivered_date_from_scans),
        "scans":                      ("call", _delhivery_flat_scans),
    },
    # ClickPost's adapter normalises the courier's own response first (its
    # status vocabulary is numeric buckets, not text), so this map runs over
    # that normalised dict rather than a raw payload. Its purpose is to keep
    # the canonical field names declared here with every other partner --
    # `shipments.etd` in particular is what extract_expected_delivery reads.
    "clickpost": {
        "shipments.awb":            "waybill",
        "shipments.status":         "status",
        "shipments.current_location": "location",
        "shipments.etd":            "courier_partner_edd",
        "shipments.delivered_date": "delivered_date",
        "shipments.scans":          "scans",
        "etd_date":                 "courier_partner_edd",
        "delivered_date":           "delivered_date",
        "scans":                    "scans",
        "updated_at":               "status_date",
    },
}


def apply_order_data_mapping(partner: str, raw_extracted: Any) -> Dict[str, Any]:
    """Translate a partner-specific extracted payload into canonical ``order_data``."""
    field_map = GET_ORDER_DATA_FIELD_MAPS.get((partner or "").lower())
    if not field_map:
        # Unknown partner — return the raw shape as-is so legacy callers
        # still see something. Adding a partner without mapping is a bug
        # but we'd rather degrade than crash production.
        logger.warning(
            f"[MAPPING] no GET_ORDER_DATA mapping for partner {partner!r}; "
            "returning raw payload unchanged"
        )
        return raw_extracted if isinstance(raw_extracted, dict) else {}
    return apply_field_map(raw_extracted, field_map)


def extract_raw_payload(partner: str, endpoint: str, raw: Any) -> Optional[Dict[str, Any]]:
    """Pull the meaningful shipment unit out of a partner's response envelope."""
    extractors = RAW_PAYLOAD_EXTRACTORS.get(endpoint, {})
    extractor = extractors.get((partner or "").lower())
    if not extractor:
        return raw if isinstance(raw, dict) else None
    return extractor(raw)


# ═════════════════════════════════════════════════════════════════════
# WEBHOOK STATUS → CANONICAL EVENT KEY
# ═════════════════════════════════════════════════════════════════════
# Each integrated partner emits webhook status strings; we map them to the
# canonical ``event_key`` used by ``gupshup_templates(channel=<partner>,
# event_key=<key>)``. Two partners can map different raw strings to the
# same canonical event_key (e.g. both "MANIFESTED" (Delhivery) and
# "MANIFEST GENERATED" (Shiprocket) → "manifested").
#
# Shiprocket historically used the raw status text as the event_key (the
# template DB uses upper-case raw status as the key). That is preserved
# below as a ``RAW_PASSTHROUGH`` flag so the resolver matches the legacy
# behaviour rather than hard-mapping every Shiprocket status here.

WEBHOOK_RAW_PASSTHROUGH: Dict[str, bool] = {
    # Shiprocket's webhook handler matches raw status text against rows in
    # gupshup_templates; the existing event_config.py is the source of
    # truth. Set passthrough so this module does not duplicate that map.
    "shiprocket": True,
    "delhivery": False,
}

WEBHOOK_STATUS_TO_EVENT_KEY: Dict[str, Dict[str, str]] = {
    "shiprocket": {
        # Empty — see WEBHOOK_RAW_PASSTHROUGH. Adding entries here lets us
        # remap raw text to a different DB key without touching event_config.
    },
    "delhivery": {
        "MANIFESTED":       "manifested",
        "PENDING":          "pending",
        "OPEN":             "manifested",
        "SCHEDULED":        "manifested",
        "IN TRANSIT":       "in_transit",
        "DISPATCHED":       "in_transit",
        "OUT FOR DELIVERY": "out_for_delivery",
        "DELIVERED":        "delivered",
        "RTO":              "rto",
        "RTO DELIVERED":    "rto_delivered",
        "CANCELLED":        "cancelled",
        "CANCELED":         "cancelled",
        "RETURNED":         "rto",
    },
}


def resolve_event_key(partner: str, raw_status: str) -> Optional[str]:
    """Map a partner's raw webhook status to its canonical ``event_key``.

    Returns ``None`` if the status is unknown for this partner. Callers
    should treat ``None`` as "skip notification" rather than as an error.
    """
    p = (partner or "").lower()
    s = (raw_status or "").upper().strip()
    if not s:
        return None
    if WEBHOOK_RAW_PASSTHROUGH.get(p):
        # Partner uses the raw status text as the DB event_key.
        return s
    mapping = WEBHOOK_STATUS_TO_EVENT_KEY.get(p, {})
    return mapping.get(s)


# ═════════════════════════════════════════════════════════════════════
# RAW STATUS → FULFILLMENT BUCKET
# ═════════════════════════════════════════════════════════════════════
# Used by processors to fill the ``fulfillment_status`` field of
# ``OrderInfoDTO``. Buckets are {"cancelled", "unfulfilled", "fulfilled",
# "unknown"}. The ``unknown`` bucket is new: it replaces the previous
# behaviour where any unrecognised status silently defaulted to
# "fulfilled" (which could mark a stuck order as delivered).

FULFILLMENT_STATUS_RULES: Dict[str, Dict[str, frozenset]] = {
    "shiprocket": {
        "cancelled":   frozenset({"CANCELED", "CANCELLED"}),
        "unfulfilled": frozenset({
            "PICKUP SCHEDULED", "PICKUP RESCHEDULED",
            "PICKUP EXCEPTION", "OUT FOR PICKUP", "NEW",
        }),
    },
    "delhivery": {
        "cancelled":   frozenset({"CANCELED", "CANCELLED"}),
        "unfulfilled": frozenset({"MANIFESTED", "PENDING", "OPEN", "SCHEDULED"}),
    },
    # ClickPost's parser works in this repo's own lower_snake buckets, but the
    # adapter translates them into the spaced vocabulary the other partners
    # emit before anything outside the module sees them
    # (clickpost/tools/logistics_adapter._to_shared_status), so these read the
    # same way Delhivery's and Shiprocket's do.
    "clickpost": {
        "cancelled":   frozenset({"CANCELLED"}),
        "unfulfilled": frozenset({"NEW"}),
    },
}

# Statuses we positively know mean "shipment is in motion or delivered".
# Used to keep the legacy "default to fulfilled" behaviour for *known* good
# statuses while routing genuinely unrecognised statuses to "unknown".
KNOWN_FULFILLED_STATUSES: Dict[str, frozenset] = {
    "shiprocket": frozenset({
        "DELIVERED", "OUT FOR DELIVERY", "IN TRANSIT", "IN TRANSIT-EN-ROUTE",
        "PICKED UP", "REACHED AT DESTINATION HUB", "SHIPPED",
        "RTO", "RTO DELIVERED", "RETURNED",
        "UNDELIVERED-1ST ATTEMPT", "UNDELIVERED-2ND ATTEMPT",
        "UNDELIVERED-3RD ATTEMPT", "UNDELIVERED", "MISROUTED",
        "DELIVERY ATTEMPTED",
        # Merchant marked the order fulfilled outside Shiprocket's logistics
        # pipeline (e.g. manual fulfillment). Surfaces in real prod traces;
        # treat as fulfilled so the agent doesn't bucket it as "unknown".
        "SELF FULFILLED",
    }),
    "delhivery": frozenset({
        "DELIVERED", "OUT FOR DELIVERY", "IN TRANSIT", "DISPATCHED",
        "RTO", "RTO DELIVERED", "RETURNED", "LOST", "DAMAGED",
    }),
    # Every status the ClickPost adapter can emit except the two claimed
    # above, so none of theirs falls through to "unknown". Spelled as the
    # adapter emits them -- translated into the shared vocabulary where a
    # counterpart exists, left in the parser's own form where none does
    # (return_to_origin, lost, damaged, exception have no shared equivalent).
    "clickpost": frozenset({
        "IN TRANSIT", "PICKED UP", "OUT FOR DELIVERY", "DELIVERED",
        "RETURN_TO_ORIGIN", "LOST", "DAMAGED", "EXCEPTION",
    }),
}


def map_fulfillment_status(partner: str, raw_status: str) -> str:
    """Map a partner's raw status to ``cancelled``/``unfulfilled``/``fulfilled``/``unknown``.

    Returns ``"unknown"`` for genuinely unrecognised statuses so callers can
    surface them (Rollbar, alert) instead of silently treating them as
    fulfilled. This is the deliberate behaviour change from the old
    per-processor ``_map_status`` helpers.
    """
    p = (partner or "").lower()
    s = (raw_status or "").upper().strip()
    rules = FULFILLMENT_STATUS_RULES.get(p, {})
    if s in rules.get("cancelled", frozenset()):
        return "cancelled"
    if s in rules.get("unfulfilled", frozenset()):
        return "unfulfilled"
    if s in KNOWN_FULFILLED_STATUSES.get(p, frozenset()):
        return "fulfilled"
    if not s:
        return "unknown"
    # New partner-status that nobody catalogued yet — log once so we notice.
    logger.info(
        f"[MAPPING] unknown {p} status {raw_status!r}; bucketing as 'unknown'. "
        "Add it to FULFILLMENT_STATUS_RULES or KNOWN_FULFILLED_STATUSES."
    )
    return "unknown"


# ═════════════════════════════════════════════════════════════════════
# PRE-SHIP (EDIT-ALLOWED) VOCABULARY
# ═════════════════════════════════════════════════════════════════════
# Shopify marks an order "fulfilled" the moment a label/AWB is generated —
# often before the parcel is physically handed to the courier. While the
# shipment is still in this pre-pickup window an order remains safely editable
# (e.g. product/size change). ``is_pre_ship_status`` answers "is this partner's
# status still pre-ship?" in a partner-aware way, sourced from the same
# ``FULFILLMENT_STATUS_RULES`` table plus the label/queue states that sit
# between AWB generation and pickup. Kept separate from
# ``map_fulfillment_status`` so tweaking the edit-window vocabulary never
# perturbs OrderInfoDTO status classification.

_PRE_SHIP_EXTRA: Dict[str, frozenset] = {
    # Shiprocket label/queue states that precede pickup but are not in the
    # "unfulfilled" bucket above.
    "shiprocket": frozenset({"PICKUP QUEUED", "LABEL GENERATED", "READY TO SHIP"}),
}


def is_pre_ship_status(partner: str, raw_status: str) -> bool:
    """True if ``raw_status`` means the parcel is not yet handed to the courier.

    Partner-aware: derived from that partner's ``FULFILLMENT_STATUS_RULES``
    ``unfulfilled`` bucket plus any ``_PRE_SHIP_EXTRA`` label/queue states.
    Empty status or an unknown partner → ``False`` so callers default to
    trusting Shopify's "fulfilled" (i.e. treat as already shipped).
    """
    p = (partner or "").lower()
    s = (raw_status or "").upper().strip()
    if not s:
        return False
    if s in FULFILLMENT_STATUS_RULES.get(p, {}).get("unfulfilled", frozenset()):
        return True
    return s in _PRE_SHIP_EXTRA.get(p, frozenset())


# ═════════════════════════════════════════════════════════════════════
# SHARED PROCESSOR HELPER
# ═════════════════════════════════════════════════════════════════════
# Centralises the logic that both ShiprocketOrderProcessor and
# DelhiveryOrderProcessor were duplicating — they all turn an already-
# canonical ``order_data`` dict + a raw status into the same OrderInfoDTO.

def build_order_info_dto_from_canonical(
    *,
    partner: str,
    order_id: Any,
    channel_order_id: Any,
    raw_status: str,
    shipment_status: str,
    order_data: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the canonical ``OrderInfoDTO`` from a partner-canonical ``order_data`` dict.

    All partners produce the same DTO by routing through this single helper.
    The only per-partner behaviour is encoded in ``map_fulfillment_status``
    via the rules table above.
    """
    # Imported here to avoid a circular import at module load (the order_utils
    # module imports schema, which imports nothing from core).
    from fashion_bot.tools import classify_status, format_etd_date
    from fashion_bot.utils.order_utils import create_order_info_dto

    order_data = order_data or {}
    shipments = order_data.get("shipments") or {}
    awb = shipments.get("awb")
    courier = shipments.get("courier") or "N/A"
    tracking_url = shipments.get("tracking_url", "")
    etd = shipments.get("etd", "")

    delivered_on = (
        shipments.get("delivered_date")
        or order_data.get("delivered_date")
    )
    etd_date = order_data.get("etd_date")
    upper_status = (raw_status or "").upper()

    if not delivered_on and upper_status == "DELIVERED":
        delivered_on = etd_date or (format_etd_date(etd) if etd else None)

    delivery_date = None
    if upper_status == "DELIVERED" and delivered_on:
        delivery_date = delivered_on
    elif etd:
        delivery_date = format_etd_date(etd)

    customer_name = (
        order_data.get("billing_customer_name")
        or order_data.get("customer_name")
        or "Guest"
    )
    customer_phone = (
        order_data.get("billing_phone")
        or order_data.get("customer_phone")
        or ""
    )

    products = order_data.get("products") or []
    items = [p.get("name", "Unknown Item") for p in products if isinstance(p, dict)]

    cancel = order_data.get("cancelled_at")
    fulfillment_status = map_fulfillment_status(partner, raw_status)
    status = classify_status(raw_status, cancel, fulfillment_status)

    dto = create_order_info_dto(
        order_id=order_id,
        channel_order_id=channel_order_id,
        status=status,
        partner_status=raw_status,
        shipment_status=shipment_status or raw_status,
        customer=customer_name,
        delivery_date=delivery_date,
        delivered_date=delivered_on,
        out_for_delivery_date=order_data.get("out_for_delivery_date"),
        items=items,
        courier=courier,
        tracking_url=tracking_url,
        awb=awb,
        products=items,
        created_at=order_data.get("created_at", "N/A"),
        updated_at=order_data.get("updated_at", "N/A"),
        source=(partner or "").lower(),
        customer_phone=customer_phone,
        billing_phone=customer_phone,
    )
    dto["partner_name"] = (partner or "").lower()
    return dto


# ═════════════════════════════════════════════════════════════════════
# EXPECTED DELIVERY DATE (ETA) EXTRACTION
# ═════════════════════════════════════════════════════════════════════
# The canonical estimated-delivery field is ``shipments.etd`` (nested) with
# an ``etd_date`` flat fallback — both populated by GET_ORDER_DATA_FIELD_MAPS
# (Shiprocket: shipments.etd / etd_date; Delhivery: ExpectedDeliveryDate).
# These live here, next to that contract, so any caller holding a partner
# ``aget_order_data`` result can pull a clean ETA without re-deriving the
# field precedence.

def normalize_etd(raw: Any) -> str:
    """Best-effort format a courier ETD into a clean ``DD Mon YYYY`` string.

    Partner ETDs arrive in mixed shapes (``2026-07-30``, ``2026-07-30
    18:00:00``, ISO8601, or an already-formatted ``30 Jul 2026``). Normalise
    what we can and fall back to the trimmed raw string when it isn't
    parseable so we never drop a value the partner did supply.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    from datetime import datetime

    date_part = text.replace("T", " ").split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_part, fmt).strftime("%d %b %Y")
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d %b %Y")
    except ValueError:
        return text


# Courier ETDs are business dates in IST, and the LLM is grounded on IST too
# (``generic_skill_node`` injects "Today is <date> (IST)" every turn).
# Comparing against a UTC date would leave a 00:00–05:30 IST window each day
# in which yesterday's ETA still reads as "not yet past" and gets echoed.
_IST = timezone(timedelta(hours=5, minutes=30))

# Shapes an ETD can arrive in *after* ``normalize_etd`` has had a go at it.
# ``normalize_etd`` already handles the ISO-ish shapes and returns anything
# else verbatim — these cover the leftovers (Shiprocket's
# ``7 Aug 2025 08:08 AM``, ``Jul 20, 2026``) so a stale date can't slip
# through just because it was already human-formatted.
_ETD_DATE_HEAD_FORMATS = ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y")


def coerce_etd_date(value: Any) -> Optional[date]:
    """Best-effort parse of an ETD into a ``date``, or ``None`` if unparseable.

    Accepts both the canonical ``normalize_etd`` output (``DD Mon YYYY``) and
    the raw partner shapes ``normalize_etd`` passes through untouched. Any
    trailing time component is ignored — only the calendar date matters here.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    date_part = text.replace("T", " ").split(" ")[0]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(date_part, fmt).date()
        except ValueError:
            continue
    # Month-name shapes span three whitespace-separated tokens, so match the
    # head of the string and let any trailing time fall away.
    head = " ".join(text.split()[:3])
    for fmt in _ETD_DATE_HEAD_FORMATS:
        try:
            return datetime.strptime(head, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _is_past_etd(formatted_etd: str, raw_etd: Any = None) -> bool:
    """True if an ETD sits before today's IST date.

    Used to suppress stale courier ETAs the LLM would otherwise echo as
    a future promise (e.g. "will arrive on 20 Jul 2026" surfaced on Aug 5).
    Falls back to ``raw_etd`` when the normalised form isn't parseable, and
    returns False on any parse failure so callers never accidentally hide a
    valid future date.
    """
    etd_date = coerce_etd_date(formatted_etd)
    if etd_date is None:
        etd_date = coerce_etd_date(raw_etd)
    if etd_date is None:
        return False
    return etd_date < datetime.now(_IST).date()


def normalize_etd_if_current(raw: Any, *, is_delivered: bool = False) -> str:
    """``normalize_etd`` plus stale-date suppression — the single place the
    "is this ETA still a promise we can make?" decision lives.

    Returns ``""`` instead of a past date for in-flight orders, so no caller
    can hand the LLM a courier ETA that has already elapsed. Delivered orders
    (``is_delivered=True``) keep the historical date, which is factually
    correct rather than a promise.
    """
    formatted = normalize_etd(raw)
    if formatted and not is_delivered and _is_past_etd(formatted, raw):
        logger.debug(
            "Suppressed stale courier ETA %r (in-flight order, ETA already past)",
            formatted,
        )
        return ""
    return formatted


def extract_expected_delivery(
    logistics_data: Optional[Dict[str, Any]],
    *,
    is_delivered: bool = False,
) -> str:
    """Pull the estimated delivery date (ETA) out of a partner
    ``aget_order_data`` result and normalise it.

    Reads the canonical ``shipments.etd`` with an ``etd_date`` /
    ``delivery_date`` fallback. Returns ``""`` when the result is
    missing/invalid or carries no ETA (e.g. Delhivery frequently returns a
    null ExpectedDeliveryDate), so callers can treat it as "not available".

    ``is_delivered=False`` (the default) additionally suppresses ETAs that
    are already in the past — those are courier-side stale promises that,
    when handed to an LLM, produce misleading "will arrive on <past-date>"
    replies. Callers rendering a *delivered* order should pass
    ``is_delivered=True`` to keep the historical ETA.
    """
    if not logistics_data or not logistics_data.get("found"):
        return ""
    od = logistics_data.get("order_data", {}) or {}
    shipments = logistics_data.get("shipments") or od.get("shipments") or {}
    etd = shipments.get("etd") if isinstance(shipments, dict) else None
    etd = etd or od.get("etd_date") or od.get("delivery_date")
    return normalize_etd_if_current(etd, is_delivered=is_delivered)
