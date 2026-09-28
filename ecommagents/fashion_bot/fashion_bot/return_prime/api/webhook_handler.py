"""HTTP webhook handler for Return Prime events."""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse

from fashion_bot.return_prime.workflow.service import return_prime_workflow


async def return_prime_webhook(request: Request):
    """Receive and persist Return Prime webhooks for a tenant."""
    client_id = (request.path_params.get("client_id") or "").strip()
    if not client_id:
        return JSONResponse({"status": "invalid_client_id"}, status_code=400)

    raw_body = await request.body()
    headers = dict(request.headers.items())
    auth_result = await return_prime_workflow.verify_webhook_request(
        client_id,
        raw_body,
        headers,
    )
    if not auth_result.get("ok"):
        return JSONResponse(
            {"status": auth_result.get("status", "unauthorized")},
            status_code=auth_result.get("status_code", 401),
        )
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"status": "invalid_json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"status": "invalid_json"}, status_code=400)

    stored = await return_prime_workflow.receive_webhook(client_id, payload, headers)
    if not stored.get("duplicate") and stored.get("id"):
        return_prime_workflow.enqueue_stored_webhook(
            client_id,
            stored["id"],
            payload,
            stored,
        )
    return JSONResponse({"status": "received"})
