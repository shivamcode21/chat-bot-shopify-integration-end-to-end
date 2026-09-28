"""Durable storage for inbound WhatsApp media (Cloudinary).

Gupshup hands us a short-lived file-manager URL for every inbound media
message (``payload.payload.url`` with a ``urlExpiry``). Once that expiry
passes the asset is unreachable, so a customer's photo is gone forever
unless we copy it somewhere durable at receive time.

This module downloads the asset once and re-uploads it to Cloudinary,
returning a permanent link that callers persist onto the ``messages`` row
(both in the message text and in ``message_metadata``).

Design notes:
- Fully async (AGENTS.md §1). The official ``cloudinary`` SDK is sync, so we
  sign and call the REST upload endpoint directly over the shared
  ``httpx.AsyncClient`` (AGENTS.md §4) instead of adding a blocking dependency.
- Fail-open. Every failure path returns ``None`` and the caller falls back to
  the placeholder-text behaviour that predates this module. Media storage must
  never cost a customer their reply.
- Stateless (AGENTS.md §2). Nothing here writes to conversation state, Redis,
  or Postgres — it returns data and lets the caller persist it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from fashion_bot.env_loader import get_bool, get_env, get_int
from fashion_bot.rollbar_config import report_error
from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

# Gupshup inbound message types that carry a media URL.
MEDIA_MESSAGE_TYPES = ("image", "video", "audio", "file")

# Cloudinary splits its API by resource type. Audio is served by the video
# pipeline; anything non-playable (documents) goes to `raw`.
_RESOURCE_TYPE_BY_MEDIA_TYPE = {
    "image": "image",
    "video": "video",
    "audio": "video",
    "file": "raw",
}

_EXTENSION_BY_CONTENT_TYPE = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/heic": "heic",
    "video/mp4": "mp4",
    "video/3gpp": "3gp",
    "video/quicktime": "mov",
    "audio/ogg": "ogg",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/amr": "amr",
    "application/pdf": "pdf",
}

_DEFAULT_MAX_BYTES = 25 * 1024 * 1024
# Storage always runs behind the customer's reply, so these bound how long the
# detached task lives — not how long anyone waits. Sized for the largest thing
# WhatsApp will deliver rather than for a photo: a phone photo round-trips in
# ~3s and a short video in ~6s, so the budget only bites on a genuinely sick
# network path. The total is the binding ceiling; the per-phase values sit under
# it so one hung socket cannot eat the whole budget, but they are generous
# enough never to cut a healthy transfer short.
_DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 30
_DEFAULT_UPLOAD_TIMEOUT_SECONDS = 45
_DEFAULT_TOTAL_TIMEOUT_SECONDS = 60
_DEFAULT_BASE_FOLDER = "whatsapp-inbound"

# Per-client override, resolved through the tiered config cache (AGENTS.md §3).
MEDIA_STORAGE_CONFIG_KEY = "inbound_media_storage_enabled"


@dataclass(frozen=True)
class InboundMedia:
    """A media attachment as described by the Gupshup webhook payload."""

    media_type: str
    url: str
    content_type: Optional[str] = None
    caption: Optional[str] = None
    url_expiry: Optional[int] = None
    message_id: Optional[str] = None
    filename: Optional[str] = None


@dataclass(frozen=True)
class StoredMedia:
    """A media attachment after it has been copied to durable storage."""

    url: str
    public_id: str
    resource_type: str
    provider: str
    size_bytes: Optional[int] = None
    content_type: Optional[str] = None


def extract_inbound_media(data: dict) -> Optional[InboundMedia]:
    """Pull the media descriptor out of a Gupshup inbound webhook payload.

    Returns ``None`` for text/button/unknown payloads or when Gupshup did not
    include a URL.
    """
    try:
        payload = (data or {}).get("payload", {}) or {}
        media_type = payload.get("type", "") or ""
        if media_type not in MEDIA_MESSAGE_TYPES:
            return None

        inner = payload.get("payload", {}) or {}
        url = (inner.get("url") or "").strip()
        if not url:
            return None

        expiry = inner.get("urlExpiry")
        try:
            expiry = int(expiry) if expiry is not None else None
        except (TypeError, ValueError):
            expiry = None

        return InboundMedia(
            media_type=media_type,
            url=url,
            content_type=(inner.get("contentType") or "").strip() or None,
            caption=(inner.get("caption") or "").strip() or None,
            url_expiry=expiry,
            message_id=(payload.get("id") or "").strip() or None,
            filename=(inner.get("filename") or "").strip() or None,
        )
    except Exception as exc:  # pragma: no cover - defensive, payload is untrusted
        logger.warning("Failed to extract inbound media from payload: %s", exc)
        return None


_missing_credentials_warned = False


def _warn_missing_credentials_once() -> None:
    """Warn once per process, not once per photo.

    Storage is on by default, so an environment that has not provisioned
    Cloudinary yet hits this on every inbound image. Repeating the warning
    would bury real errors in the log.
    """
    global _missing_credentials_warned
    if _missing_credentials_warned:
        return
    _missing_credentials_warned = True
    logger.warning(
        "Inbound media storage is enabled but Cloudinary credentials are missing; "
        "media will not be stored until CLOUDINARY_CLOUD_NAME / _API_KEY / _API_SECRET are set"
    )


def _cloudinary_credentials() -> Optional[Dict[str, str]]:
    cloud_name = get_env("CLOUDINARY_CLOUD_NAME")
    api_key = get_env("CLOUDINARY_API_KEY")
    api_secret = get_env("CLOUDINARY_API_SECRET")
    if not (cloud_name and api_key and api_secret):
        return None
    return {"cloud_name": cloud_name, "api_key": api_key, "api_secret": api_secret}


async def ais_media_storage_enabled(client_id: Optional[str]) -> bool:
    """Whether inbound media should be copied to durable storage.

    On by default. ``INBOUND_MEDIA_STORAGE_ENABLED=false`` turns it off
    globally, and a per-client ``inbound_media_storage_enabled`` config row
    overrides either way, so a single tenant can be opted out (or back in)
    without a deploy. Without Cloudinary credentials the storage step no-ops
    regardless, so "enabled" alone never changes behaviour.
    """
    global_default = get_bool("INBOUND_MEDIA_STORAGE_ENABLED", True)
    if not client_id:
        return global_default

    try:
        # Imported lazily: config_manager pulls in the DB/Redis stack, and this
        # module is imported from the webhook hot path at startup.
        from fashion_bot.config_manager import aget_config

        raw = await aget_config(
            MEDIA_STORAGE_CONFIG_KEY,
            default=None,
            client_id=client_id,
        )
    except Exception as exc:
        logger.warning("Media storage config lookup failed, using env default: %s", exc)
        return global_default

    if raw is None:
        return global_default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _phone_suffix(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return digits[-4:] if digits else "unknown"


def _target_folder(client_id: Optional[str], phone: Optional[str]) -> str:
    base = (get_env("CLOUDINARY_UPLOAD_FOLDER") or _DEFAULT_BASE_FOLDER).strip("/")
    # Only the last 4 phone digits are used in the object path — the full number
    # already lives on the messages row, and object paths end up in CDN URLs.
    return f"{base}/{client_id or 'unknown-client'}/{_phone_suffix(phone)}"


def _filename_for(media: InboundMedia) -> str:
    if media.filename:
        return media.filename
    extension = _EXTENSION_BY_CONTENT_TYPE.get((media.content_type or "").lower())
    return f"{media.media_type}.{extension}" if extension else media.media_type


def _public_id_for(media: InboundMedia, client_id: Optional[str]) -> str:
    """Stable, unguessable object id derived from the Gupshup message id.

    Deterministic so a webhook redelivery overwrites the same asset instead of
    leaving a duplicate behind (dedup normally prevents reprocessing, but it
    fails open when Redis is down). Hashed with the client id so the id is not
    derivable from the message id alone, keeping the delivery URL unguessable
    under the default public delivery type.
    """
    if not media.message_id:
        return uuid.uuid4().hex
    digest = hashlib.sha256(f"{client_id or ''}:{media.message_id}".encode("utf-8"))
    return digest.hexdigest()[:32]


def _sign_params(params: Dict[str, str], api_secret: str) -> str:
    """Cloudinary signature: sha1 over sorted `k=v` pairs plus the API secret."""
    payload = "&".join(
        f"{key}={params[key]}"
        for key in sorted(params)
        if params[key] not in (None, "")
    )
    return hashlib.sha1(f"{payload}{api_secret}".encode("utf-8")).hexdigest()


async def _adownload(media: InboundMedia, max_bytes: int) -> Optional[bytes]:
    client = await get_shared_async_http_client()
    response = await client.get(
        media.url,
        timeout=get_int("INBOUND_MEDIA_DOWNLOAD_TIMEOUT_SECONDS", _DEFAULT_DOWNLOAD_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    content = response.content
    if len(content) > max_bytes:
        logger.warning(
            "Inbound %s media too large to store: %d bytes (limit %d)",
            media.media_type,
            len(content),
            max_bytes,
        )
        return None
    return content


async def _aupload_to_cloudinary(
    content: bytes,
    media: InboundMedia,
    credentials: Dict[str, str],
    folder: str,
    client_id: Optional[str],
) -> Optional[StoredMedia]:
    resource_type = _RESOURCE_TYPE_BY_MEDIA_TYPE.get(media.media_type, "auto")
    delivery_type = (get_env("CLOUDINARY_DELIVERY_TYPE") or "upload").strip() or "upload"

    signed_params = {
        "folder": folder,
        "overwrite": "true",
        "public_id": _public_id_for(media, client_id),
        "timestamp": str(int(time.time())),
        "type": delivery_type,
    }
    form = {
        **signed_params,
        "api_key": credentials["api_key"],
        "signature": _sign_params(signed_params, credentials["api_secret"]),
    }

    client = await get_shared_async_http_client()
    response = await client.post(
        f"https://api.cloudinary.com/v1_1/{credentials['cloud_name']}/{resource_type}/upload",
        data=form,
        files={"file": (_filename_for(media), content, media.content_type or "application/octet-stream")},
        timeout=get_int("INBOUND_MEDIA_UPLOAD_TIMEOUT_SECONDS", _DEFAULT_UPLOAD_TIMEOUT_SECONDS),
    )
    response.raise_for_status()
    body = response.json() or {}

    stored_url = body.get("secure_url") or body.get("url")
    if not stored_url:
        logger.warning("Cloudinary upload returned no URL: %s", body)
        return None

    return StoredMedia(
        url=stored_url,
        public_id=str(body.get("public_id") or f"{folder}/{signed_params['public_id']}"),
        resource_type=str(body.get("resource_type") or resource_type),
        provider="cloudinary",
        size_bytes=body.get("bytes") if isinstance(body.get("bytes"), int) else len(content),
        content_type=media.content_type,
    )


async def astore_inbound_media(
    media: InboundMedia,
    *,
    client_id: Optional[str],
    phone: Optional[str],
    trace_id: Optional[str] = None,
) -> Optional[StoredMedia]:
    """Copy an inbound attachment to Cloudinary and return the durable link.

    Returns ``None`` — never raises — when the feature is disabled, Cloudinary
    is unconfigured, the asset is oversized, or any network call fails. Callers
    keep their existing placeholder behaviour in that case.
    """

    async def _run() -> Optional[StoredMedia]:
        # The flag check reads config (memory → Redis → Postgres), so it lives
        # inside the timeout budget: a degraded database must not add latency
        # to the reply this call sits in front of.
        if not await ais_media_storage_enabled(client_id):
            return None

        credentials = _cloudinary_credentials()
        if not credentials:
            _warn_missing_credentials_once()
            return None

        content = await _adownload(media, get_int("INBOUND_MEDIA_MAX_BYTES", _DEFAULT_MAX_BYTES))
        if not content:
            return None
        return await _aupload_to_cloudinary(
            content, media, credentials, _target_folder(client_id, phone), client_id
        )

    started = time.monotonic()
    try:
        stored = await asyncio.wait_for(
            _run(),
            timeout=get_int("INBOUND_MEDIA_TOTAL_TIMEOUT_SECONDS", _DEFAULT_TOTAL_TIMEOUT_SECONDS),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "[%s] Inbound %s media storage timed out after %dms",
            trace_id,
            media.media_type,
            int((time.monotonic() - started) * 1000),
        )
        report_error(
            "Inbound media storage timed out",
            level="warning",
            media_type=media.media_type,
            client_id=client_id,
            trace_id=trace_id,
        )
        return None
    except Exception as exc:
        logger.warning("[%s] Inbound %s media storage failed: %s", trace_id, media.media_type, exc)
        report_error(
            "Inbound media storage failed",
            level="warning",
            exc_info=(type(exc), exc, exc.__traceback__),
            media_type=media.media_type,
            client_id=client_id,
            trace_id=trace_id,
        )
        return None

    if stored:
        logger.info(
            "[%s] Stored inbound %s media in %s (%dms, public_id=%s)",
            trace_id,
            media.media_type,
            stored.provider,
            int((time.monotonic() - started) * 1000),
            stored.public_id,
        )
    return stored


def build_media_metadata(media: InboundMedia, stored: StoredMedia) -> Dict[str, Any]:
    """Structured descriptor for the ``messages.message_metadata`` JSONB column."""
    return {
        "media": {
            "type": media.media_type,
            "caption": media.caption,
            "content_type": media.content_type,
            "filename": media.filename,
            "source_url_expiry": media.url_expiry,
            "url": stored.url,
            "provider": stored.provider,
            "public_id": stored.public_id,
            "resource_type": stored.resource_type,
            "size_bytes": stored.size_bytes,
            "stored": True,
        }
    }


def append_media_link(text: str, stored: Optional[StoredMedia]) -> str:
    """Append the durable link to the transcript line.

    Keeping the link in ``messages.message`` (as well as in the metadata JSONB)
    means existing dashboards surface it with no frontend change.
    """
    if not stored or not stored.url:
        return text
    if stored.url in (text or ""):
        return text
    return f"{text}\n{stored.url}".strip()


async def aprepare_inbound_media(
    data: dict,
    *,
    placeholder_text: str,
    client_id: Optional[str],
    phone: Optional[str],
    trace_id: Optional[str] = None,
) -> tuple[str, Optional[Dict[str, Any]]]:
    """Store an inbound attachment and return ``(transcript_text, metadata)``.

    The single entry point every inbound path uses, so all three behave
    identically. The invariant callers rely on: **the message row changes if
    and only if an asset was actually stored.** Feature disabled, no media,
    misconfigured, oversized, or any failure — all return the placeholder
    unchanged with ``None`` metadata, which is byte-for-byte the behaviour that
    predates this module.
    """
    # extract_inbound_media already restricts to MEDIA_MESSAGE_TYPES, so every
    # media kind Gupshup can deliver (image, video, audio, document) is stored.
    media = extract_inbound_media(data)
    if not media:
        return placeholder_text, None

    stored = await astore_inbound_media(
        media,
        client_id=client_id,
        phone=phone,
        trace_id=trace_id,
    )
    if not stored:
        return placeholder_text, None
    return append_media_link(placeholder_text, stored), build_media_metadata(media, stored)
