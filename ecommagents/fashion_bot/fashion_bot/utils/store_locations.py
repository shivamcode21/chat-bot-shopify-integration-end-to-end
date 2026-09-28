"""Offline store location registry and nearest-store lookup.

Store data lives in the ``client_configs`` Postgres table under
``config_key = 'store_locations'``.  The ``config_value`` column holds either
a plain JSON array of store objects (legacy) or a dict with top-level flags
and a ``locations`` key::

    # Dict shape (preferred)
    {
      "template_enabled_for_notification": true,
      "template_id": "1665491144557994",
      "locations": [
        { "name": "Flagship Store", ... }
      ]
    }

    # Legacy list shape (still supported)
    [ { "name": "Flagship Store", ... } ]

``template_enabled_for_notification`` controls whether store-visit escalation
WhatsApp messages are sent as template messages (when ``true``) or free-text
(when ``false``/absent/null).

Reads go through the three-tier cache (memory → Redis → DB) via
``config_manager.aget_config``.

Pincode → lat/lng geocoding uses the OpenStreetMap Nominatim API (free, no key)
with an in-memory cache so each pincode is resolved at most once per process.
"""

from __future__ import annotations

import json
import logging
from math import atan2, cos, radians, sin, sqrt
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_EARTH_RADIUS_KM = 6371.0
_MAX_DISTANCE_KM = 30.0

# In-memory cache: pincode → (lat, lng) | None
_pincode_cache: Dict[str, Optional[Tuple[float, float]]] = {}


# ── Pure helpers (no I/O) ──────────────────────────────────────────


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points on Earth in kilometres."""
    rlat1, rlon1, rlat2, rlon2 = map(radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    return _EARTH_RADIUS_KM * 2 * atan2(sqrt(a), sqrt(1 - a))


def _ensure_maps_url(store: Dict[str, Any]) -> str:
    if store.get("google_maps_url"):
        return store["google_maps_url"]
    lat, lon = store.get("latitude"), store.get("longitude")
    if lat is not None and lon is not None:
        return f"https://maps.google.com/?q={lat},{lon}"
    return ""


async def _geocode_pincode(pincode: str) -> Optional[Tuple[float, float]]:
    """Convert an Indian pincode to (latitude, longitude).

    Uses the free OpenStreetMap Nominatim API (no key required).
    Results are cached in-memory so each pincode is resolved at most once
    per process lifetime.  Returns ``None`` on any failure (fail-open).
    """
    p = pincode.strip()
    if p in _pincode_cache:
        return _pincode_cache[p]

    try:
        from fashion_bot.utils.http_client import get_shared_async_http_client
        client = await get_shared_async_http_client()
        resp = await client.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "postalcode": p,
                "country": "IN",
                "format": "json",
                "limit": "1",
            },
            headers={"User-Agent": "FashionBot/1.0"},
            timeout=4.0,
        )
        data = resp.json()
        if isinstance(data, list) and data:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            if lat and lon:
                logger.debug("Geocoded pincode %s → (%s, %s)", p, lat, lon)
                _pincode_cache[p] = (lat, lon)
                return (lat, lon)
        _pincode_cache[p] = None
        return None
    except Exception:
        logger.debug("Pincode geocode failed for %s", p, exc_info=True)
        _pincode_cache[p] = None
        return None


# ── Async accessors (tiered-cached via aget_config) ───────────────


async def _aget_store_locations_raw(client_id: str) -> Any:
    """Load the raw ``store_locations`` config value (cached, fail-open)."""
    try:
        from fashion_bot.config_manager import aget_config

        raw = await aget_config("store_locations", client_id=client_id)
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        logger.debug("store_locations config missing or invalid for %s", client_id, exc_info=True)
        return None


async def aget_all_stores(client_id: str) -> List[Dict[str, Any]]:
    """Return all stores for *client_id* from ``client_configs`` (cached).

    Accepts both the legacy list shape and the dict shape
    (``{"locations": [...], ...}``).  Returns an empty list when no
    ``store_locations`` config exists or on any error (fail-open).
    """
    data = await _aget_store_locations_raw(client_id)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        locations = data.get("locations")
        if isinstance(locations, list):
            return locations
    return []


_TRUTHY_STRINGS = {"1", "true", "yes", "on", "enabled"}


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return False


async def aget_store_notification_template(client_id: str) -> Optional[Dict[str, Any]]:
    """Return the store-visit notification template config, or ``None``.

    Reads ``template_enabled_for_notification`` and ``template_id`` from the
    ``store_locations`` config.  Returns a ``{"template_id": ...}`` dict only
    when both the flag is truthy and a ``template_id`` is present.  Returns
    ``None`` otherwise (flag absent/false, no template_id, or legacy list
    shape).
    """
    data = await _aget_store_locations_raw(client_id)
    if not isinstance(data, dict):
        return None
    if not _is_truthy(data.get("template_enabled_for_notification")):
        return None
    tid = data.get("template_id")
    if not tid:
        return None
    return {
        "template_id": str(tid),
        "image_url": data.get("image_url"),
    }


async def afind_nearest_store(
    client_id: str,
    *,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    city: Optional[str] = None,
    pincode: Optional[str] = None,
    limit: int = 1,
) -> List[Dict[str, Any]]:
    """Return the nearest store(s) for *client_id*, sorted by distance.

    Resolution priority:
      1. Haversine on ``latitude``/``longitude`` (most precise).
      2. Pincode geocoded to lat/lng → haversine.
      3. Case-insensitive ``city`` match.

    Each returned dict is a **copy** of the store record enriched with
    ``distance_km`` (float | None) and ``google_maps_url``.
    """
    stores = await aget_all_stores(client_id)
    if not stores:
        return []

    # If only pincode provided (no lat/lng), geocode it to coordinates
    if pincode and latitude is None and longitude is None:
        coords = await _geocode_pincode(pincode.strip())
        if coords:
            latitude, longitude = coords
            logger.debug("Geocoded pincode %s → (%s, %s)", pincode, latitude, longitude)

    has_coords = (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )

    # Exact pincode match takes absolute priority over haversine distance.
    # A store IN the queried pincode is always "nearest" regardless of centroid math.
    pincode_normalized = pincode.strip() if pincode else None

    scored: List[tuple[float, Dict[str, Any]]] = []

    for s in stores:
        entry = {**s, "google_maps_url": _ensure_maps_url(s)}

        # Check for exact pincode match (address or dedicated field).
        store_pincode = str(s.get("pincode") or "").strip()
        if pincode_normalized and store_pincode == pincode_normalized:
            s_lat, s_lon = s.get("latitude"), s.get("longitude")
            if has_coords and s_lat is not None and s_lon is not None:
                entry["distance_km"] = round(haversine_km(latitude, longitude, s_lat, s_lon), 1)  # type: ignore[arg-type]
            else:
                entry["distance_km"] = 0.0
            scored.append((0.0, entry))
            continue

        s_lat, s_lon = s.get("latitude"), s.get("longitude")

        if has_coords and s_lat is not None and s_lon is not None:
            dist = round(haversine_km(latitude, longitude, s_lat, s_lon), 1)  # type: ignore[arg-type]
            entry["distance_km"] = dist
            scored.append((dist, entry))
            continue

        if city and s.get("city", "").lower() == city.strip().lower():
            entry["distance_km"] = None
            scored.append((0.0, entry))
            continue

    if not scored:
        return []

    scored.sort(key=lambda t: t[0])
    return [
        entry for dist, entry in scored[:limit]
        if dist == 0.0 or entry.get("distance_km") is None or dist <= _MAX_DISTANCE_KM
    ]
