"""
Gupshup API Client
Provides functionality to fetch template definitions from Gupshup API.
"""
import logging
import json
from typing import Optional, Dict, Any, List
from functools import lru_cache
import time
import httpx

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

# Cache template definitions for 1 hour (3600 seconds)
_TEMPLATE_CACHE = {}
_CACHE_TTL = 3600


async def aget_gupshup_config(client_id=None):
    """Async variant of get_gupshup_config."""
    try:
        from fashion_bot.config_manager import aget_config

        # Fetch gupshup_template_details scoped to the client
        gupshup_details_json = await aget_config('gupshup_template_details', client_id=client_id)

        if gupshup_details_json:
            # Parse JSON if it's a string, otherwise use as-is if already dict
            if isinstance(gupshup_details_json, str):
                gupshup_config = json.loads(gupshup_details_json)
            else:
                gupshup_config = gupshup_details_json

            if gupshup_config and isinstance(gupshup_config, dict):
                logger.debug(f"✅ Gupshup configuration loaded from database for client_id={client_id}")
                return gupshup_config

        logger.warning(f"⚠️ No gupshup_template_details found in client_configs for client_id={client_id}")
        return None

    except Exception as e:
        logger.error(f"❌ Error fetching async Gupshup configuration from database for client_id={client_id}: {str(e)}")
        return None


async def afetch_template_from_gupshup(
    template_id: str,
    force_refresh: bool = False,
    client_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Async variant of ``fetch_template_from_gupshup`` for request-path webhook use.
    """
    log_client_id = client_id or "unknown"

    if not force_refresh:
        cached = _get_cached_template(template_id)
        if cached:
            logger.info(
                f"[GUPSHUP_API] ✅ Template {template_id} found in cache "
                f"client_id={log_client_id}"
            )
            return cached

    try:
        config = await aget_gupshup_config(client_id=client_id)
        if not config:
            logger.warning(f"[GUPSHUP_API] No Gupshup configuration available for client_id={client_id}")
            return None

        api_key = config.get("GUPSHUP_TEMPLATE_API_KEY", "")
        app_id = config.get("APP_ID", "")
        if not api_key or not app_id:
            logger.warning(
                f"[GUPSHUP_API] Missing configuration for client_id={client_id} - "
                f"API Key: {'✓' if api_key else '✗'}, APP_ID: {'✓' if app_id else '✗'}"
            )
            return None

        url = f"https://api.gupshup.io/wa/app/{app_id}/template/{template_id}"
        headers = {"apikey": api_key}
        logger.info(
            f"[GUPSHUP_API] 📡 Fetching template from: {url} "
            f"client_id={log_client_id}"
        )

        client = await get_shared_async_http_client()
        response = await client.get(url, headers=headers, timeout=10)
        logger.info(
            f"[GUPSHUP_API] Response status: {response.status_code} "
            f"client_id={log_client_id}"
        )

        if response.status_code == 200:
            data = response.json()
            if data.get("status") == "success" and "template" in data:
                template = data["template"]
                template_text = template.get("data", "")
                template_name = template.get("elementName", "")
                logger.info(
                    f"[GUPSHUP_API] ✅ Template fetched successfully "
                    f"client_id={log_client_id}"
                )
                logger.info(f"[GUPSHUP_API] Template Name: {template_name}")
                logger.info(f"[GUPSHUP_API] Template Text: {template_text[:100]}...")

                template_obj = {
                    "id": template_id,
                    "elementName": template_name,
                    "data": template_text,
                    "templateType": template.get("templateType", ""),
                    "meta": template.get("meta", ""),
                }
                _cache_template(template_id, template_obj)
                return template_obj

            logger.warning(
                f"[GUPSHUP_API] Unexpected response format for client_id={log_client_id}: {data}"
            )
            return None

        if response.status_code in (401, 403):
            logger.error(
                f"[GUPSHUP_API] ❌ Authentication failed client_id={log_client_id} "
                f"status={response.status_code} - check API key/app permissions"
            )
            return None

        if response.status_code == 404:
            logger.error(
                f"[GUPSHUP_API] ❌ Template {template_id} not found or APP_ID is incorrect "
                f"client_id={log_client_id}"
            )
            return None

        logger.error(
            f"[GUPSHUP_API] ❌ API request failed client_id={log_client_id}: "
            f"{response.status_code} - {response.text}"
        )
        return None
    except httpx.HTTPError as e:
        logger.error(
            f"[GUPSHUP_API] ❌ Error fetching template for client_id={log_client_id}: {e}",
            exc_info=True,
        )
        return None
    except Exception as e:
        logger.error(
            f"[GUPSHUP_API] ❌ Error fetching template for client_id={log_client_id}: {e}",
            exc_info=True,
        )
        return None


def _get_cached_template(template_id: str) -> Optional[Dict[str, Any]]:
    """Get template from cache if not expired"""
    if template_id in _TEMPLATE_CACHE:
        cached_data, timestamp = _TEMPLATE_CACHE[template_id]
        if time.time() - timestamp < _CACHE_TTL:
            return cached_data
        else:
            # Expired, remove from cache
            del _TEMPLATE_CACHE[template_id]
    return None


def _cache_template(template_id: str, template_data: Dict[str, Any]):
    """Store template in cache with timestamp"""
    _TEMPLATE_CACHE[template_id] = (template_data, time.time())


def render_template_message(template_definition: Dict[str, Any], params: List[str]) -> Optional[str]:
    """
    Render a complete template message by replacing {{1}}, {{2}}, etc. with parameter values.
    
    Args:
        template_definition: Template definition with 'data' field containing template text
        params: List of parameter values in order (e.g., ['John', 'ORD123'])
        
    Returns:
        Complete rendered message text, or None if rendering fails
        
    Example:
        template_definition = {'data': 'Hello {{1}}, your order {{2}} is ready!'}
        params = ['John', 'ORD123']
        Returns: "Hello John, your order ORD123 is ready!"
    """
    try:
        if not template_definition or not isinstance(template_definition, dict):
            logger.error("[TEMPLATE_RENDER] ❌ Invalid template definition")
            return None
        
        # Get template text from 'data' field (new Gupshup format)
        template_text = template_definition.get('data', '')
        
        if not template_text:
            logger.warning("[TEMPLATE_RENDER] ⚠️  No template text found in 'data' field")
            return None
        
        # Replace placeholders with actual parameter values
        rendered_message = _replace_placeholders(template_text, params)
        
        logger.info(f"[TEMPLATE_RENDER] ✅ Template rendered successfully")
        logger.info(f"[TEMPLATE_RENDER] 📝 Rendered message: {rendered_message}")
        
        return rendered_message
        
    except Exception as e:
        logger.error(f"[TEMPLATE_RENDER] ❌ Error rendering template: {e}", exc_info=True)
        return None


def _replace_placeholders(text: str, params: List[str]) -> str:
    """
    Replace WhatsApp template placeholders with actual parameter values.
    
    WhatsApp uses {{1}}, {{2}}, {{3}}, etc. for placeholders (1-indexed)
    
    Args:
        text: Template text with placeholders (e.g., "Hello {{1}}, order {{2}}")
        params: List of parameter values (e.g., ['John', 'ORD123'])
        
    Returns:
        Text with placeholders replaced
    """
    if not text:
        return ""
    
    result = text
    
    logger.info(f"[TEMPLATE_RENDER] 🔄 Replacing placeholders...")
    logger.info(f"[TEMPLATE_RENDER] Template: {text}")
    logger.info(f"[TEMPLATE_RENDER] Parameters: {params}")
    
    # Replace placeholders {{1}}, {{2}}, etc.
    for i, param in enumerate(params, start=1):
        placeholder = f"{{{{{i}}}}}"
        if placeholder in result:
            result = result.replace(placeholder, str(param))
            logger.info(f"[TEMPLATE_RENDER] Replaced {{{{{i}}}}} → {param}")
    
    return result


def get_template_body_text(template_definition: Dict[str, Any]) -> Optional[str]:
    """
    Extract just the body text from a template definition (without rendering).
    
    Args:
        template_definition: Template definition from Gupshup API
        
    Returns:
        Body text with placeholders, or None if not found
    """
    try:
        components = template_definition.get('components', [])
        for component in components:
            if component.get('type', '').upper() == 'BODY':
                return component.get('text', '')
        return None
    except Exception as e:
        logger.error(f"[TEMPLATE_RENDER] Error extracting body text: {e}")
        return None


def clear_template_cache():
    """Clear the template cache (useful for testing or manual refresh)"""
    global _TEMPLATE_CACHE
    _TEMPLATE_CACHE = {}
    logger.info("[GUPSHUP_API] Template cache cleared")
