from fastapi import APIRouter, Request
from typing import Dict, Any
import logging
from .templates_db import (
    ensure_tables,
    aupsert_client_template,
    aget_client_template,
    aupsert_client_template_catalog,
)
from fashion_bot.utils.http_client import get_shared_async_http_client

router = APIRouter()
logger = logging.getLogger(__name__)

@router.on_event("startup")
def _startup():
    ensure_tables()

@router.post("/templates/map")
async def map_event_to_template(request: Request):
    data = await request.json()
    client_id = data.get('client_id') or data.get('tenant_id')  # support both, prefer client_id
    channel = data.get('channel')  # e.g., 'shopify' or 'shiprocket'
    event_key = data.get('event_key')
    template_id = data.get('template_id')
    template_name = data.get('template_name')
    image_url = data.get('image_url')
    param_order = data.get('param_order') or []
    if not all([client_id, channel, event_key, template_id]):
        return {"success": False, "error": "client_id, channel, event_key, template_id are required"}
    ok = await aupsert_client_template(
        client_id,
        channel,
        event_key,
        template_id,
        image_url,
        param_order,
        template_name=template_name,
    )
    return {"success": ok}

@router.get("/templates/map")
async def get_mapping(client_id: str = None, tenant_id: str = None, channel: str = '', event_key: str = ''):
    resolved_client_id = client_id or tenant_id
    if not resolved_client_id or not channel or not event_key:
        return {"success": False, "error": "client_id, channel, event_key are required"}
    row = await aget_client_template(resolved_client_id, channel, event_key)
    if not row:
        return {"success": False, "error": "mapping not found"}
    return {"success": True, "data": row}

@router.post("/templates/gupshup/create")
async def create_gupshup_template(request: Request):
    data = await request.json()
    client_id = data.get('client_id') or data.get('tenant_id')
    apikey = data.get('apikey')
    name = data.get('name')
    languageCode = data.get('languageCode')
    category = data.get('category')
    components = data.get('components')
    if not all([client_id, apikey, name, languageCode, category, components]):
        return {"success": False, "error": "client_id, apikey, name, languageCode, category, components are required"}
    url = 'https://api.gupshup.io/wa/template/v1/create'
    headers = {
        'Content-Type': 'application/json',
        'apikey': apikey
    }
    payload = {
        'name': name,
        'languageCode': languageCode,
        'category': category,
        'components': components
    }
    try:
        client = await get_shared_async_http_client()
        resp = await client.post(url, json=payload, headers=headers, timeout=15)
        ok = resp.status_code in [200, 202]
        body = {}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text}
        # Persist catalog record (template_id may be absent until approval)
        await aupsert_client_template_catalog(
            client_id,
            name,
            languageCode,
            category,
            body.get('templateId') or body.get('id'),
            body,
        )
        return {"success": ok, "response": body}
    except Exception as e:
        logger.error(f"Gupshup template create error: {e}")
        return {"success": False, "error": str(e)} 
