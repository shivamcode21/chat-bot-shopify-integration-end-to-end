"""Return Prime webhook receive/store helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any

from fashion_bot.return_prime.db import db
from fashion_bot.env_loader import get_env
from fashion_bot.return_prime.notifications.service import process_return_prime_notification

logger = logging.getLogger(__name__)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coerce_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


async def _aget_json_config_safe(config_key: str, client_id: str | None) -> dict:
    try:
        from fashion_bot.config_manager import aget_json_config

        return await aget_json_config(config_key, client_id=client_id) or {}
    except Exception:
        return {}


class ReturnPrimeWebhookService:
    """Store, dedupe, and process Return Prime webhook events."""

    SIGNATURE_HEADERS = (
        "x-return-prime-signature",
        "x-rp-signature",
        "x-webhook-signature",
    )
    STALE_PROCESSING_MINUTES = 15

    async def _load_webhook_secret(self, client_id: str | None) -> str | None:
        details = await _aget_json_config_safe("return_prime_details", client_id)
        secret = _first_non_empty(
            details.get("webhook_secret"),
            details.get("RETURN_PRIME_WEBHOOK_SECRET"),
            get_env("RETURN_PRIME_WEBHOOK_SECRET"),
        )
        cleaned = str(secret or "").strip()
        return cleaned or None

    def _extract_signature(self, headers: dict[str, Any]) -> str | None:
        normalized_headers = {
            str(key).lower(): str(value).strip()
            for key, value in (headers or {}).items()
            if value not in (None, "")
        }
        for header_name in self.SIGNATURE_HEADERS:
            signature = normalized_headers.get(header_name)
            if signature:
                return signature
        return None

    def _signature_candidates(self, signature: str | None) -> list[str]:
        if not signature:
            return []
        normalized = signature.strip()
        if "=" in normalized:
            _, _, normalized = normalized.partition("=")
            normalized = normalized.strip()
        candidates = [normalized.lower()]
        try:
            decoded = base64.b64decode(normalized, validate=True).hex()
        except Exception:
            decoded = None
        if decoded:
            candidates.append(decoded.lower())
        return candidates

    def _compute_hmac_sha256(self, secret: str, raw_body: bytes) -> str:
        return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    async def verify_webhook_request(
        self,
        client_id: str | None,
        raw_body: bytes,
        headers: dict[str, Any],
    ) -> dict:
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            return {"ok": False, "status": "invalid_client_id", "status_code": 400}

        secret = await self._load_webhook_secret(normalized_client_id)
        if not secret:
            return {"ok": True, "status": "received", "status_code": 200}

        signature = self._extract_signature(headers)
        if not signature:
            return {"ok": False, "status": "missing_signature", "status_code": 401}

        expected_signature = self._compute_hmac_sha256(secret, raw_body)
        for candidate in self._signature_candidates(signature):
            if hmac.compare_digest(expected_signature, candidate):
                return {"ok": True, "status": "received", "status_code": 200}

        return {"ok": False, "status": "invalid_signature", "status_code": 403}

    def extract_webhook_fields(self, payload: dict) -> dict:
        body = _coerce_dict(payload)
        request = _coerce_dict(body.get("request")) or _coerce_dict(body.get("data")) or body
        order = _coerce_dict(request.get("order"))
        customer = _coerce_dict(request.get("customer"))
        return {
            "event_type": _first_non_empty(body.get("event_type"), body.get("event"), body.get("topic")),
            "request_id": _first_non_empty(request.get("id"), body.get("request_id")),
            "request_number": _first_non_empty(request.get("request_number"), body.get("request_number")),
            "request_type": _first_non_empty(request.get("request_type"), body.get("request_type")),
            "request_status": _first_non_empty(request.get("status"), body.get("request_status")),
            "order_name": _first_non_empty(order.get("name"), body.get("order_name")),
            "customer_email": _first_non_empty(customer.get("email"), body.get("customer_email")),
            "customer_phone": _first_non_empty(customer.get("phone"), body.get("customer_phone")),
            "status_timestamp": _first_non_empty(
                request.get("status_timestamp"),
                request.get("updated_at"),
                request.get("processed_at"),
                body.get("status_timestamp"),
                body.get("updated_at"),
            ),
        }

    def generate_dedupe_key(self, client_id: str, payload: dict, extracted: dict) -> str:
        request_id = extracted.get("request_id")
        request_number = extracted.get("request_number")
        if request_id:
            material = {
                "client_id": client_id,
                "request_id": request_id,
                "event_type": extracted.get("event_type"),
                "request_status": extracted.get("request_status"),
            }
        elif request_number:
            material = {
                "client_id": client_id,
                "request_number": request_number,
                "event_type": extracted.get("event_type"),
                "request_status": extracted.get("request_status"),
            }
        else:
            payload_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()
            material = {
                "client_id": client_id,
                "event_type": extracted.get("event_type"),
                "request_status": extracted.get("request_status"),
                "payload_hash": payload_hash,
            }
        if not any(material.values()):
            material = {
                "client_id": client_id,
                "payload_hash": hashlib.sha256(repr(payload).encode("utf-8")).hexdigest(),
            }
        encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    async def store_webhook_event(self, client_id: str, payload: dict, headers: dict) -> dict:
        normalized_client_id = str(client_id or "").strip()
        if not normalized_client_id:
            raise ValueError("client_id is required for Return Prime webhook storage")
        extracted = self.extract_webhook_fields(payload)
        dedupe_key = self.generate_dedupe_key(normalized_client_id, payload, extracted)
        timestamp = _iso_now()
        row = await db.postgres.fetch_one(
            """
            INSERT INTO return_prime_webhook_events (
                client_id, event_type, request_id, request_number, request_type,
                request_status, order_name, customer_email, customer_phone,
                payload_json, headers_json, dedupe_key, processing_status,
                received_at, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
            """,
            (
                normalized_client_id,
                extracted.get("event_type"),
                extracted.get("request_id"),
                extracted.get("request_number"),
                extracted.get("request_type"),
                extracted.get("request_status"),
                extracted.get("order_name"),
                extracted.get("customer_email"),
                extracted.get("customer_phone"),
                json.dumps(payload, default=str),
                json.dumps(headers, default=str),
                dedupe_key,
                "received",
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        if row:
            return {"duplicate": False, "id": row["id"], "dedupe_key": dedupe_key, **extracted}

        existing = await db.postgres.fetch_one(
            "SELECT id, processing_status FROM return_prime_webhook_events WHERE dedupe_key = %s",
            (dedupe_key,),
        )
        return {
            "duplicate": True,
            "id": existing.get("id") if existing else None,
            "dedupe_key": dedupe_key,
            "processing_status": existing.get("processing_status") if existing else None,
            **extracted,
        }

    async def mark_event_processed(
        self,
        event_id: int,
        *,
        processing_status: str,
        processing_error: str | None = None,
    ) -> None:
        await db.execute(
            """
            UPDATE return_prime_webhook_events
            SET processing_status = %s,
                processing_error = %s,
                processed_at = %s,
                updated_at = %s
            WHERE id = %s
            """,
            (processing_status, processing_error, _iso_now(), _iso_now(), event_id),
        )

    async def process_webhook(self, client_id: str, payload: dict, headers: dict) -> dict:
        stored = await self.store_webhook_event(client_id, payload, headers)
        if stored["duplicate"]:
            if stored.get("processing_status") in ("processed", "ignored", "processing"):
                return {"stored": True, "duplicate": True, "event_id": stored.get("id")}

        return await self.process_stored_event(client_id, stored["id"], payload, stored)

    async def process_stored_event(
        self,
        client_id: str,
        event_id: int,
        payload: dict,
        extracted: dict,
    ) -> dict:
        await self.mark_event_processed(event_id, processing_status="processing")
        try:
            notification_result = await process_return_prime_notification(
                client_id=client_id,
                webhook_event_id=event_id,
                payload=payload,
                extracted=extracted,
            )
        except Exception as exc:
            notification_result = {"success": False, "status": "failed", "error": str(exc)}

        processing_status = "processed"
        if notification_result.get("status") == "ignored":
            processing_status = "ignored"
        elif notification_result.get("status") == "failed":
            processing_status = "failed"

        if notification_result.get("status_reason") and not notification_result.get("error"):
            notification_result["error"] = notification_result["status_reason"]
        await self.mark_event_processed(
            event_id,
            processing_status=processing_status,
            processing_error=notification_result.get("error"),
        )
        return {
            "stored": True,
            "duplicate": False,
            "event_id": event_id,
            "notification": notification_result,
        }

    async def requeue_stale_processing_events(
        self,
        *,
        stale_after_minutes: int | None = None,
    ) -> dict:
        stale_minutes = stale_after_minutes or self.STALE_PROCESSING_MINUTES
        stale_events = await db.postgres.fetch_all(
            """
            SELECT id
            FROM return_prime_webhook_events
            WHERE processing_status = 'processing'
              AND updated_at < NOW() - (%s * INTERVAL '1 minute')
            """,
            (stale_minutes,),
        )
        requeued = 0
        for row in stale_events:
            await db.execute(
                """
                UPDATE return_prime_webhook_events
                SET processing_status = %s,
                    processing_error = %s,
                    processed_at = NULL,
                    updated_at = %s
                WHERE id = %s
                """,
                (
                    "received",
                    "requeued after stale processing timeout",
                    _iso_now(),
                    row["id"],
                ),
            )
            requeued += 1
        if requeued:
            logger.warning("Requeued %s stale Return Prime webhook event(s).", requeued)
        return {"requeued_count": requeued, "stale_after_minutes": stale_minutes}


return_prime_webhook_service = ReturnPrimeWebhookService()
