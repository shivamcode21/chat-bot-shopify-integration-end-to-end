import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

from fastapi import APIRouter, Request

from fashion_bot.config_manager import aget_client_id_by_app_name, aresolve_client_id
from fashion_bot.database_manager import get_async_postgres_connection
from fashion_bot.trace_context import generate_trace_id, set_trace_id
from fashion_bot.workers import config
from fashion_bot.workers.enqueue import submit_or_inline

logger = logging.getLogger("gupshup_events_webhook")

router = APIRouter()

_table_ensured = False


async def _ensure_gupshup_events_table() -> None:
    global _table_ensured
    if _table_ensured:
        return

    sql = """
        CREATE TABLE IF NOT EXISTS gupshup_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            client_id UUID,
            app_name VARCHAR(255),
            message_id VARCHAR(255),
            event_type VARCHAR(100),
            event_status VARCHAR(30) NOT NULL DEFAULT 'NA',
            phone_number VARCHAR(100),
            source VARCHAR(100),
            error_code VARCHAR(100),
            error_reason TEXT,
            raw_event JSONB NOT NULL DEFAULT '{}'::jsonb,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE INDEX IF NOT EXISTS idx_gupshup_events_client_received
            ON gupshup_events (client_id, received_at DESC);

        CREATE INDEX IF NOT EXISTS idx_gupshup_events_message_id
            ON gupshup_events (message_id);

        CREATE INDEX IF NOT EXISTS idx_gupshup_events_status_received
            ON gupshup_events (event_status, received_at DESC);
    """

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
    _table_ensured = True


def _walk_values(value: Any, keys: Iterable[str]) -> Optional[Any]:
    wanted = {key.lower() for key in keys}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                return item
        for item in value.values():
            found = _walk_values(item, wanted)
            if found not in (None, ""):
                return found
    elif isinstance(value, list):
        for item in value:
            found = _walk_values(item, wanted)
            if found not in (None, ""):
                return found
    return None


def _extract_payload(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else {}


def _normalize_status(*values: Any) -> str:
    text = " ".join(str(value).lower() for value in values if value not in (None, ""))
    if not text:
        return "NA"
    if any(token in text for token in ("fail", "failed", "failure", "error", "undeliver", "rejected")):
        return "failure"
    if "delivered" in text:
        return "delivered"
    return "NA"


def _parse_event(data: Dict[str, Any]) -> Dict[str, Any]:
    payload = _extract_payload(data)
    nested_payload = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}

    app_name = (
        data.get("app")
        or data.get("appName")
        or data.get("src.name")
        or payload.get("app")
        or payload.get("appName")
        or payload.get("src.name")
        or _walk_values(data, ("app", "appName", "src.name"))
    )
    event_type = (
        data.get("eventType")
        or data.get("event_type")
        or data.get("type")
        or payload.get("eventType")
        or payload.get("type")
        or payload.get("status")
        or _walk_values(data, ("eventType", "event_type", "status", "type"))
    )
    message_id = (
        payload.get("id")
        or payload.get("messageId")
        or payload.get("message_id")
        or data.get("messageId")
        or data.get("message_id")
        or data.get("id")
        or _walk_values(data, ("messageId", "message_id", "externalId", "external_id"))
    )
    phone_number = (
        payload.get("destination")
        or payload.get("phone")
        or payload.get("phoneNumber")
        or payload.get("sender")
        or nested_payload.get("destination")
        or _walk_values(data, ("destination", "phone", "phoneNumber", "recipient", "to"))
    )
    source = payload.get("source") or data.get("source") or _walk_values(data, ("source", "from"))
    error_code = (
        payload.get("code")
        or payload.get("errorCode")
        or nested_payload.get("code")
        or nested_payload.get("errorCode")
        or _walk_values(data, ("errorCode", "error_code", "code"))
    )
    error_reason = (
        payload.get("reason")
        or payload.get("error")
        or payload.get("errorMessage")
        or nested_payload.get("reason")
        or nested_payload.get("error")
        or nested_payload.get("errorMessage")
        or _walk_values(data, ("reason", "error", "errorMessage", "description"))
    )

    return {
        "app_name": str(app_name) if app_name else None,
        "event_type": str(event_type) if event_type else None,
        "event_status": _normalize_status(event_type, error_code, error_reason),
        "message_id": str(message_id) if message_id else None,
        "phone_number": str(phone_number) if phone_number else None,
        "source": str(source) if source else None,
        "error_code": str(error_code) if error_code else None,
        "error_reason": str(error_reason) if error_reason else None,
    }


async def _resolve_client_id_for_event(parsed: Dict[str, Any]) -> tuple[Optional[str], str]:
    app_name = parsed.get("app_name")
    if app_name:
        client_id = await aget_client_id_by_app_name(app_name)
        if client_id:
            return str(client_id), "app_name"

    source = parsed.get("source")
    if source:
        client_id = await aresolve_client_id(source)
        if client_id:
            return str(client_id), "source"
        normalized_source = re.sub(r"\D+", "", str(source))
        if normalized_source and normalized_source != source:
            client_id = await aresolve_client_id(normalized_source)
            if client_id:
                return str(client_id), "source_normalized"

    message_id = parsed.get("message_id")
    if message_id:
        client_id = await _resolve_client_id_from_template_message_id(message_id)
        if client_id:
            return client_id, "template_message_id"

    phone_number = parsed.get("phone_number")
    if phone_number:
        client_id = await _resolve_client_id_from_recent_conversation(phone_number)
        if client_id:
            return client_id, "phone_recent_conversation"

    return None, "unresolved"


async def _resolve_client_id_from_template_message_id(message_id: str) -> Optional[str]:
    sql = """
        SELECT client_id::text
        FROM template_delivery_logs
        WHERE message_id = %s
        ORDER BY sent_at DESC
        LIMIT 1
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (message_id,))
                row = await cur.fetchone()
                if row and row.get("client_id"):
                    return str(row["client_id"])
    except Exception as exc:
        logger.debug("Template message_id client lookup failed: %s", exc)
    return None


async def _resolve_client_id_from_recent_conversation(phone_number: str) -> Optional[str]:
    digits = re.sub(r"\D+", "", str(phone_number or ""))
    if not digits:
        return None

    phone_candidates = {str(phone_number)}
    phone_candidates.add(digits)
    if len(digits) == 10:
        phone_candidates.add(f"91{digits}")
        phone_candidates.add(f"+91{digits}")
    elif digits.startswith("91") and len(digits) == 12:
        phone_candidates.add(digits[-10:])
        phone_candidates.add(f"+{digits}")

    sql = """
        SELECT client_id::text
        FROM conversations
        WHERE channel_type = 'whatsapp'
          AND phone = ANY(%s)
          AND updated_at >= NOW() - INTERVAL '30 days'
        ORDER BY updated_at DESC
        LIMIT 1
    """
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (list(phone_candidates),))
                row = await cur.fetchone()
                if row and row.get("client_id"):
                    return str(row["client_id"])
    except Exception as exc:
        logger.debug("Recent conversation client lookup failed: %s", exc)
    return None


async def _read_request_payload(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
        return payload if isinstance(payload, dict) else {"payload": payload}
    except Exception:
        form = await request.form()
        return dict(form)


async def _store_gupshup_event(
    *,
    client_id: Optional[str],
    parsed: Dict[str, Any],
    raw_event: Dict[str, Any],
) -> None:
    await _ensure_gupshup_events_table()
    sql = """
        INSERT INTO gupshup_events (
            client_id, app_name, message_id, event_type, event_status,
            phone_number, source, error_code, error_reason, raw_event, received_at
        ) VALUES (
            %s::uuid, %s, %s, %s, %s,
            %s, %s, %s, %s, %s::jsonb, %s
        )
    """
    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                sql,
                (
                    client_id,
                    parsed.get("app_name"),
                    parsed.get("message_id"),
                    parsed.get("event_type"),
                    parsed.get("event_status") or "NA",
                    parsed.get("phone_number"),
                    parsed.get("source"),
                    parsed.get("error_code"),
                    parsed.get("error_reason"),
                    json.dumps(raw_event, default=str),
                    datetime.now(timezone.utc),
                ),
            )


async def process_gupshup_event_payload(
    data: Dict[str, Any],
    *,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Parse and store a Gupshup delivery/failure event payload."""
    if trace_id:
        set_trace_id(trace_id)

    try:
        logger.info(
            "Gupshup event webhook raw payload: %s",
            json.dumps(data, default=str),
            extra={"trace_id": trace_id},
        )
        parsed = _parse_event(data)
        client_id, client_resolution_method = await _resolve_client_id_for_event(parsed)

        logger.info(
            "Gupshup event webhook received",
            extra={
                "trace_id": trace_id,
                "client_id": client_id,
                "event_status": parsed.get("event_status"),
                "event_type": parsed.get("event_type"),
                "message_id": parsed.get("message_id"),
                "app_name": parsed.get("app_name"),
                "client_resolution_method": client_resolution_method,
                "source": parsed.get("source"),
            },
        )

        await _store_gupshup_event(client_id=client_id, parsed=parsed, raw_event=data)

        return {
            "success": True,
            "status": "ok",
            "event_status": parsed.get("event_status") or "NA",
            "message_id": parsed.get("message_id"),
            "trace_id": trace_id,
        }
    except Exception as exc:
        logger.exception("Gupshup event webhook failed", extra={"trace_id": trace_id})
        return {
            "success": False,
            "status": "error",
            "message": str(exc),
            "trace_id": trace_id,
        }


@router.post("/gupshup/events/webhook")
async def gupshup_events_webhook(request: Request):
    """Passive Gupshup delivery/failure event logger."""
    trace_id = generate_trace_id()
    set_trace_id(trace_id)

    try:
        data = await _read_request_payload(request)
        result = await submit_or_inline(
            config.JOB_GUPSHUP_EVENT,
            {"event_data": data, "trace_id": trace_id},
            inline=lambda: process_gupshup_event_payload(data, trace_id=trace_id),
        )
        return {
            "status": "queued" if result.get("queued") else result.get("status", "ok"),
            "event_status": result.get("event_status"),
            "trace_id": trace_id,
        }
    except Exception as exc:
        logger.exception("Gupshup event webhook failed", extra={"trace_id": trace_id})
        return {
            "status": "error",
            "message": str(exc),
            "trace_id": trace_id,
        }
