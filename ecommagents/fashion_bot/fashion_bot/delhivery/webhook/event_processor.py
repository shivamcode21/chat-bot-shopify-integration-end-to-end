"""
Lean event processor for Delhivery webhook events.

Why it's separate from the Shiprocket processor:
  - Different inbound payload shape (`ShipmentData[].Shipment.{...}`).
  - Different Gupshup template channel (`'delhivery'`).
  - Independent dedup namespace (`delhivery_event:{cid}:{order_id}:{event_key}`
    Redis keys; `partner='delhivery'` rows in `shipment_events` /
    `event_deduplication`).

Reuses the same Postgres tables as Shiprocket (with the new `partner` column
introduced in this PR). Reuses the same Gupshup-sender helper by passing
``channel='delhivery'`` into ``aget_client_template``.

State guidance (AGENTS.md): stateless, async only, trace-id-aware logging.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fashion_bot.config_manager import aresolve_client_id
from fashion_bot.database_manager import (
    awith_retry,
    get_async_postgres_connection,
)
from fashion_bot.shopify.webhook.templates_db import aget_client_template

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover
    redis_async = None
import certifi

logger = logging.getLogger(__name__)

PARTNER_NAME = "delhivery"

REDIS_URL = (
    os.getenv("REDIS_URL")
    or os.getenv("REDIS_CONNECTION_STRING")
    or "redis://localhost:6379/0"
)

_async_redis_client: Any = None
_REDIS_CACHE_TTL_SECONDS = 86400  # 1 day

# ── In-memory dedup cache (L1) ────────────────────────────────────────
_PROCESSED_EVENTS_CACHE: Dict[tuple, float] = {}
_MEM_CACHE_TTL_SECONDS = 600   # 10 minutes — same as Shiprocket
_MEM_CACHE_MAX_SIZE = 10_000


def _cleanup_event_cache() -> None:
    current = time.time()
    expired = [k for k, ts in _PROCESSED_EVENTS_CACHE.items() if current - ts > _MEM_CACHE_TTL_SECONDS]
    for k in expired:
        _PROCESSED_EVENTS_CACHE.pop(k, None)
    if len(_PROCESSED_EVENTS_CACHE) > _MEM_CACHE_MAX_SIZE:
        oldest = sorted(_PROCESSED_EVENTS_CACHE.items(), key=lambda x: x[1])
        for k, _ in oldest[: len(_PROCESSED_EVENTS_CACHE) - _MEM_CACHE_MAX_SIZE]:
            _PROCESSED_EVENTS_CACHE.pop(k, None)


def _is_recently_processed(client_id: str, order_id: str, event_key: str) -> bool:
    cache_key = (client_id, order_id, event_key)
    if len(_PROCESSED_EVENTS_CACHE) % 100 == 0:
        _cleanup_event_cache()
    cached_ts = _PROCESSED_EVENTS_CACHE.get(cache_key)
    return cached_ts is not None and (time.time() - cached_ts) < _MEM_CACHE_TTL_SECONDS


def _mark_as_processed_in_memory(client_id: str, order_id: str, event_key: str) -> None:
    _PROCESSED_EVENTS_CACHE[(client_id, order_id, event_key)] = time.time()


# ── Redis helpers ─────────────────────────────────────────────────────


async def _get_redis_client_for_events():
    global _async_redis_client
    if _async_redis_client is not None:
        return _async_redis_client
    if not redis_async:
        return None
    try:
        kwargs: Dict[str, Any] = {"decode_responses": True}
        if str(REDIS_URL).lower().startswith("rediss://"):
            kwargs["ssl_ca_certs"] = certifi.where()
            if os.getenv("REDIS_SSL_INSECURE", "").lower() in ("1", "true", "yes"):
                kwargs["ssl_cert_reqs"] = None
        client = redis_async.Redis.from_url(REDIS_URL, **kwargs)
        await client.ping()
        _async_redis_client = client
        return client
    except Exception:
        _async_redis_client = None
        return None


# ── Delhivery payload normalisation ───────────────────────────────────


def normalize_delhivery_webhook(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Convert Delhivery's nested webhook into a flat shape we can persist.

    Delhivery sends an envelope ``{"ShipmentData": [{"Shipment": {...}}, ...]}``
    or sometimes a single Shipment block at top level. We always return:
        {
            "order_id":         "...",   # ReferenceNo
            "awb":              "...",   # AWB
            "shipment_status":  "...",   # Status.Status
            "current_status":   "...",   # Status.StatusType
            "current_timestamp":"...",   # Status.StatusDateTime
            "scans": [ {date, status, location, activity, sr-status}, ... ],
        }
    or None if required fields are missing.
    """
    if not isinstance(payload, dict):
        return None

    shipment = None
    if isinstance(payload.get("ShipmentData"), list) and payload["ShipmentData"]:
        first = payload["ShipmentData"][0] or {}
        shipment = first.get("Shipment") or first
    elif isinstance(payload.get("Shipment"), dict):
        shipment = payload["Shipment"]
    elif "AWB" in payload or "ReferenceNo" in payload:
        shipment = payload

    if not shipment:
        return None

    status_block = shipment.get("Status") or {}
    order_id = (
        shipment.get("ReferenceNo")
        or shipment.get("Reference_No")
        or shipment.get("order_id")
        or ""
    )
    awb = shipment.get("AWB") or shipment.get("waybill") or ""
    shipment_status = status_block.get("Status") or shipment.get("shipment_status") or ""

    if not (order_id and awb and shipment_status):
        return None

    scans_raw = shipment.get("Scans") or []
    scans: List[Dict[str, Any]] = []
    for entry in scans_raw:
        scan = (entry or {}).get("ScanDetail") or entry
        if not isinstance(scan, dict):
            continue
        date = scan.get("ScanDateTime") or scan.get("StatusDateTime")
        status = scan.get("Scan") or scan.get("Status")
        if not (date and status):
            continue
        scans.append({
            "date": date,
            "status": status,
            "location": scan.get("ScannedLocation") or scan.get("StatusLocation"),
            "activity": scan.get("Instructions") or scan.get("ScanType"),
            "sr-status": scan.get("StatusType"),
        })

    return {
        "order_id": str(order_id),
        "awb": str(awb),
        "shipment_status": str(shipment_status),
        "current_status": status_block.get("StatusType", ""),
        "current_timestamp": status_block.get("StatusDateTime", ""),
        "scans": scans,
    }


# ── Gupshup helpers (mirror Shiprocket's template sender) ────────────


def _extract_customer_name_and_phone(order: dict):
    shipping = order.get("shipping_address", {}) if isinstance(order, dict) else {}
    customer = order.get("customer", {}) if isinstance(order, dict) else {}
    default_address = customer.get("default_address", {}) if isinstance(customer, dict) else {}
    name = (
        (shipping.get("first_name", "") + " " + shipping.get("last_name", "")).strip()
        or shipping.get("name")
        or (customer.get("first_name", "") + " " + customer.get("last_name", "")).strip()
        or default_address.get("name")
        or customer.get("name")
        or "Customer"
    )
    phone = (
        shipping.get("phone")
        or default_address.get("phone")
        or customer.get("phone")
        or order.get("phone")
        or ""
    )
    if phone:
        phone = phone.replace(" ", "")
        if phone.startswith("+91"):
            pass
        elif phone.startswith("91") and len(phone) == 12:
            phone = "+" + phone
        elif len(phone) == 10:
            phone = "+91" + phone
        elif len(phone) > 10 and not phone.startswith("+"):
            phone = "+" + phone
    return name, phone


# ── Event-key resolution ──────────────────────────────────────────────
# The Delhivery raw-status → canonical event_key mapping now lives in
# ``core.partner_response_mappings.WEBHOOK_STATUS_TO_EVENT_KEY['delhivery']``
# so it sits alongside the Shiprocket mapping and is easy to extend when a
# new partner is added.


async def aresolve_delhivery_event_key(
    shipment_status: str,
    current_status: str,
    client_id: str,
) -> Optional[str]:
    """Resolve a Delhivery shipment status to one of the ``event_key`` rows the
    client has configured under ``gupshup_templates(channel='delhivery')``.

    Strategy:
      1. Look up the canonical key in the shared partner mapping module.
      2. If the client has a row for that key under channel='delhivery', use it.
      3. Else return None (caller skips notifications, no error).
    """
    from fashion_bot.core.partner_response_mappings import resolve_event_key

    candidate = resolve_event_key(PARTNER_NAME, shipment_status)
    if not candidate:
        return None
    try:
        cfg = await aget_client_template(client_id, PARTNER_NAME, candidate)
        return candidate if cfg else None
    except Exception as exc:
        logger.warning(f"[DELHIVERY-EVENT-KEY] resolve failed for {client_id}/{candidate}: {exc}")
        return None


# ── Database I/O (uses same tables, partner='delhivery') ─────────────


def _create_event_hash(event_data: Dict[str, Any]) -> str:
    import hashlib

    stable = {
        "awb": event_data.get("awb", ""),
        "shipment_status": event_data.get("shipment_status", ""),
        "current_status": event_data.get("current_status", ""),
        "current_timestamp": event_data.get("current_timestamp", ""),
        "order_id": event_data.get("order_id", ""),
    }
    return hashlib.md5(
        json.dumps(stable, sort_keys=True).encode()
    ).hexdigest()


@awith_retry
async def _atry_claim_event(
    client_id: str,
    order_id: str,
    event_name: str,
    event_data: Dict[str, Any],
) -> bool:
    """
    Atomically claim (client_id, partner='delhivery', order_id, event_name,
    event_hash) for processing.

    The INSERT and the uniqueness check happen as a single atomic operation
    (INSERT ... ON CONFLICT DO NOTHING RETURNING id), so two near-simultaneous
    webhook deliveries for the same event can never both "win" the way a
    separate SELECT-then-INSERT could. Only the caller whose INSERT actually
    creates the row gets True; every other caller gets False and must skip
    sending.
    """
    event_hash = _create_event_hash(event_data)
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO event_deduplication (client_id, partner, order_id, event_name, event_hash)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (client_id, partner, order_id, event_name, event_hash) DO NOTHING
                RETURNING id
                """,
                (client_id, PARTNER_NAME, order_id, event_name, event_hash),
            )
            return await cur.fetchone() is not None


@awith_retry
async def _arelease_event_claim(
    client_id: str,
    order_id: str,
    event_name: str,
    event_data: Dict[str, Any],
) -> bool:
    """Release a claim previously won via _atry_claim_event(), so a genuine
    webhook redelivery for the same event isn't dropped forever."""
    event_hash = _create_event_hash(event_data)
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                DELETE FROM event_deduplication
                WHERE client_id = %s AND partner = %s AND order_id = %s
                  AND event_name = %s AND event_hash = %s
                """,
                (client_id, PARTNER_NAME, order_id, event_name, event_hash),
            )
    return True


@awith_retry
async def _aupsert_event_audit_log(
    client_id: str,
    order_id: str,
    event_name: str,
    event_data: Dict[str, Any],
) -> bool:
    """Upsert the latest event snapshot. Deduplication itself is handled by _atry_claim_event()."""
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO shipment_events (client_id, partner, order_id, awb, event_name, event_data)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (client_id, partner, order_id, event_name) DO UPDATE SET
                    event_data = EXCLUDED.event_data,
                    processed_at = NOW()
                """,
                (
                    client_id, PARTNER_NAME, order_id,
                    event_data.get("awb", ""), event_name, json.dumps(event_data),
                ),
            )
    return True


@awith_retry
async def _amark_notification_sent(
    client_id: str,
    order_id: str,
    event_name: str,
) -> bool:
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE shipment_events
                SET notification_sent = TRUE, notification_sent_at = NOW()
                WHERE client_id = %s AND partner = %s AND order_id = %s AND event_name = %s
                """,
                (client_id, PARTNER_NAME, order_id, event_name),
            )
    return True


@awith_retry
async def _asave_status_history(
    client_id: str,
    statuses: List[Dict[str, Any]],
) -> bool:
    if not statuses:
        return True
    rows = [
        (
            client_id,
            s.get("order_id"),
            s.get("awb"),
            s.get("status"),
            s.get("status_code"),
            s.get("location"),
            s.get("activity"),
            _parse_status_timestamp(s.get("timestamp")),
            PARTNER_NAME,
        )
        for s in statuses
    ]
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.executemany(
                """
                INSERT INTO shipment_status_history
                (client_id, order_id, awb, status, status_code, location, activity, timestamp, partner)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                rows,
            )
    return True


def _parse_status_timestamp(ts: Optional[str]):
    from datetime import timezone

    if ts:
        try:
            if " " in str(ts):
                return datetime.strptime(ts, "%d %m %Y %H:%M:%S")
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            pass
    return datetime.now(timezone.utc)


# ── Public processor ──────────────────────────────────────────────────


class DelhiveryEventProcessor:
    """Orchestrates Delhivery webhook event handling: dedup → persist → notify."""

    async def process_webhook_event(
        self,
        webhook_data: Dict[str, Any],
        client_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        if client_id is None:
            client_id = await aresolve_client_id()

        try:
            order_id = webhook_data.get("order_id")
            awb = webhook_data.get("awb")
            shipment_status = webhook_data.get("shipment_status")
            current_status = webhook_data.get("current_status")

            if not all([order_id, awb, shipment_status]):
                return {
                    "success": False,
                    "error": "Missing required fields",
                    "order_id": order_id,
                    "awb": awb,
                    "client_id": client_id,
                }

            logger.info(
                f"[DELHIVERY-EVENT] [CLIENT: {client_id}] order={order_id} awb={awb} "
                f"status={shipment_status}"
            )

            # Always persist scan history first (pre-dedup) so the timeline survives.
            await self._save_status_history(webhook_data, client_id)

            event_key = await aresolve_delhivery_event_key(
                shipment_status, current_status, client_id,
            )
            if not event_key:
                return {
                    "success": True,
                    "message": "No event template found for status",
                    "order_id": order_id,
                    "status": shipment_status,
                    "client_id": client_id,
                }

            # L1: in-memory cache (fastest, same-process duplicates).
            if _is_recently_processed(client_id, order_id, event_key):
                logger.info(
                    f"[DELHIVERY-EVENT] [CLIENT: {client_id}] 🚫 DUPLICATE (memory) "
                    f"order={order_id} event={event_key}"
                )
                return {
                    "success": True,
                    "message": "Duplicate event blocked (memory cache)",
                    "order_id": order_id,
                    "event_name": event_key,
                    "notification_sent": False,
                    "dedup_layer": "memory_cache",
                    "client_id": client_id,
                }

            # L2: Redis dedup (cross-instance, best-effort, fail-open).
            redis_dup = await self._check_redis_dup(client_id, order_id, event_key)
            if redis_dup:
                return redis_dup

            # L3: Atomically claim the event in the database. This is a single
            # INSERT ... ON CONFLICT DO NOTHING RETURNING id rather than a
            # SELECT-then-mark-later check: the row insert itself is the gate,
            # performed before any notification is sent. Whichever concurrent
            # caller's INSERT actually creates the row is the only one that
            # proceeds to send; every other caller — including a
            # near-simultaneous duplicate webhook delivery — is blocked here.
            claimed = await _atry_claim_event(client_id, order_id, event_key, webhook_data)
            if not claimed:
                logger.info(
                    f"[DELHIVERY-EVENT] [CLIENT: {client_id}] 🚫 DUPLICATE (DB claim) "
                    f"order={order_id} event={event_key}"
                )
                return {
                    "success": True,
                    "message": "Duplicate event blocked (DB)",
                    "order_id": order_id,
                    "event_name": event_key,
                    "notification_sent": False,
                    "dedup_layer": "database",
                    "client_id": client_id,
                }

            # This caller won the claim - mark memory/Redis and process.
            _mark_as_processed_in_memory(client_id, order_id, event_key)
            await self._mark_in_redis(client_id, order_id, event_key)

            # Send WhatsApp template via Gupshup.
            try:
                notif_sent = await self._send_template(client_id, order_id, event_key)
            except Exception:
                # Release the DB claim so a genuine webhook redelivery (same
                # event_hash) can retry instead of being dropped forever.
                await _arelease_event_claim(client_id, order_id, event_key, webhook_data)
                raise

            await _aupsert_event_audit_log(client_id, order_id, event_key, webhook_data)
            if notif_sent:
                await _amark_notification_sent(client_id, order_id, event_key)

            return {
                "success": True,
                "message": "Event processed",
                "order_id": order_id,
                "event_name": event_key,
                "notification_sent": notif_sent,
                "client_id": client_id,
            }

        except Exception as exc:
            logger.error(f"[DELHIVERY-EVENT] error: {exc}", exc_info=True)
            return {
                "success": False,
                "error": str(exc),
                "order_id": webhook_data.get("order_id", "Unknown"),
                "awb": webhook_data.get("awb", "Unknown"),
            }

    async def _save_status_history(
        self,
        webhook_data: Dict[str, Any],
        client_id: str,
    ) -> None:
        try:
            order_id = webhook_data.get("order_id")
            awb = webhook_data.get("awb")
            current_status = webhook_data.get("current_status")
            current_timestamp = webhook_data.get("current_timestamp")

            statuses: List[Dict[str, Any]] = []
            if order_id and awb and current_status:
                statuses.append({
                    "order_id": order_id, "awb": awb, "status": current_status,
                    "status_code": None, "location": None, "activity": None,
                    "timestamp": current_timestamp,
                })
            for scan in webhook_data.get("scans") or []:
                if not isinstance(scan, dict) or not (scan.get("date") and scan.get("status")):
                    continue
                statuses.append({
                    "order_id": order_id, "awb": awb, "status": scan["status"],
                    "status_code": scan.get("sr-status"),
                    "location": scan.get("location"),
                    "activity": scan.get("activity"),
                    "timestamp": scan["date"],
                })
            if statuses:
                await _asave_status_history(client_id, statuses)
        except Exception as exc:
            logger.error(f"[DELHIVERY-EVENT] save history error: {exc}")

    async def _check_redis_dup(
        self,
        client_id: str,
        order_id: str,
        event_key: str,
    ) -> Optional[Dict[str, Any]]:
        client = await _get_redis_client_for_events()
        if not client:
            return None
        cache_key = f"delhivery_event:{client_id}:{order_id}:{event_key}"
        try:
            if await client.exists(cache_key):
                return {
                    "success": True,
                    "message": "Duplicate event blocked (Redis)",
                    "order_id": order_id,
                    "event_name": event_key,
                    "notification_sent": False,
                    "dedup_layer": "redis_cache",
                    "client_id": client_id,
                }
        except Exception as exc:
            logger.warning(f"[DELHIVERY-EVENT] Redis check failed: {exc}")
        return None

    async def _mark_in_redis(
        self,
        client_id: str,
        order_id: str,
        event_key: str,
    ) -> None:
        client = await _get_redis_client_for_events()
        if not client:
            return
        cache_key = f"delhivery_event:{client_id}:{order_id}:{event_key}"
        try:
            await client.setex(cache_key, _REDIS_CACHE_TTL_SECONDS, "1")
        except Exception as exc:
            logger.warning(f"[DELHIVERY-EVENT] Redis set failed: {exc}")

    async def _send_template(
        self,
        client_id: str,
        order_id: str,
        event_key: str,
    ) -> bool:
        """Send a Gupshup WhatsApp template using the vendor-agnostic
        ``shipping.webhook.gupshup_template_sender`` helper. The helper takes
        the channel name as a parameter via the template lookup; for
        Delhivery, we look up under channel='delhivery'."""
        try:
            from fashion_bot.core.factory import ServiceFactory
            from fashion_bot.shipping.webhook.gupshup_template_sender import (
                asend_gupshup_template_generic,
            )

            state = {"client_id": client_id}
            order_service = await ServiceFactory.aget_order_service(state=state, vendor="shopify")
            shopify_order = await order_service.aget_order_details(order_id, state=state)
            if not shopify_order:
                logger.info(f"[DELHIVERY-EVENT] Shopify order {order_id} not found; skipping template")
                return False

            customer_full_name, phone = _extract_customer_name_and_phone(shopify_order)
            if not phone:
                return False

            customer_firstname = customer_full_name.split()[0] if customer_full_name else "Customer"
            order_number = shopify_order.get("order_number") or shopify_order.get("order_id") or order_id
            order_value = (
                shopify_order.get("total")
                or shopify_order.get("total_price")
                or shopify_order.get("current_total_price")
                or "N/A"
            )

            def _build_tracking_link() -> str:
                fulfillments = shopify_order.get("fulfillments") or []
                if fulfillments:
                    latest = fulfillments[-1]
                    url = latest.get("tracking_url") or ""
                    if url:
                        return url
                    urls = latest.get("tracking_urls") or []
                    if urls:
                        return urls[0]
                return (
                    shopify_order.get("tracking_url")
                    or shopify_order.get("shipments", {}).get("tracking_url")
                    or "N/A"
                )

            cfg_db = await aget_client_template(client_id, "delhivery", event_key)
            if not cfg_db:
                return False

            template_id = cfg_db.get("template_id")
            template_name = cfg_db.get("template_name")
            param_order = cfg_db.get("param_order", []) or []
            db_image_url = (cfg_db.get("image_url") or "").strip()

            from fashion_bot.utils.template_param_resolver import (
                build_template_params_from_context,
            )
            from fashion_bot.utils.whatsapp_api_version import (
                WHATSAPP_API_VERSION_ENTERPRISE,
                aget_whatsapp_api_version,
            )

            if (
                await aget_whatsapp_api_version(client_id)
                == WHATSAPP_API_VERSION_ENTERPRISE
            ):
                params = build_template_params_from_context(
                    param_order,
                    {
                        "customer_name": customer_firstname or "Customer",
                        "order_id": str(order_number) or "Order",
                        "order_value": order_value,
                        "tracking_link": _build_tracking_link,
                    },
                )
            else:
                params: List[str] = []
                for key in param_order:
                    if key in {"first_name", "name"}:
                        params.append(customer_firstname or "Customer")
                    elif key == "order_number":
                        params.append(str(order_number) or "Order")
                    else:
                        params.append(str(key))

            # Resolve final image URL: prefer dynamic product image (same as
            # Shiprocket), fall back to the static db_image_url from template
            # config, or send text-only if neither is available.
            final_image_url: Optional[str] = None
            if db_image_url:
                from fashion_bot.shopify.webhook.event_processor import _fetch_shopify_image_url
                dynamic_image_url = ""
                try:
                    line_items = shopify_order.get("line_items", [])
                    if line_items:
                        first_item = line_items[0]
                        product_id = first_item.get("product_id")
                        variant_id = first_item.get("variant_id")
                        if product_id or variant_id:
                            dynamic_image_url = await _fetch_shopify_image_url(
                                product_id, variant_id, client_id
                            )
                except Exception as img_exc:
                    logger.warning(f"[DELHIVERY-EVENT] dynamic image fetch failed: {img_exc}")

                final_image_url = dynamic_image_url or db_image_url or None
            else:
                logger.info(
                    f"[DELHIVERY-EVENT] [CLIENT: {client_id}] "
                    "No image_url in template config, sending text-only"
                )

            await asend_gupshup_template_generic(
                phone,
                template_id,
                params,
                image_url=final_image_url,
                client_id=client_id,
                event_key=event_key,
                template_name=template_name,
                log_tag="DELHIVERY",
            )
            return True
        except Exception as exc:
            logger.warning(f"[DELHIVERY-EVENT] template send failed: {exc}")
            return False
