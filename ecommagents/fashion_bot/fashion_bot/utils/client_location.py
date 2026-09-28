"""Client location helpers for widget and demo chat integrations."""

from __future__ import annotations

import logging
import os
from typing import Any, Mapping, Optional

import httpx

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

_LOOKUP_TIMEOUT_SECONDS = float(os.getenv("DEMO_CLIENT_LOCATION_LOOKUP_TIMEOUT_SECONDS", "2.5"))
_WEB_WIDGET_LOOKUP_TIMEOUT_SECONDS = float(os.getenv("WEB_WIDGET_LOCATION_LOOKUP_TIMEOUT_SECONDS", "2.5"))
_AWS_PLACES_REVERSE_GEOCODE_URL = os.getenv(
    "AWS_PLACES_REVERSE_GEOCODE_URL",
    "https://places.geo.ap-south-1.amazonaws.com/v2/reverse-geocode",
)
_AWS_PLACES_API_KEY = os.getenv("AWS_PLACES_API_KEY", "").strip()
_WEB_WIDGET_LOCATION_ENABLED = os.getenv(
    "WEB_WIDGET_LOCATION_ENABLED",
    "false",
).lower() in {"1", "true", "yes", "on"}
_NOMINATIM_USER_AGENT = os.getenv(
    "DEMO_NOMINATIM_USER_AGENT",
    "my-app/1.0 (contact@yourdomain.com)",
)
_IP_LOCATION_FALLBACK_ENABLED = os.getenv(
    "DEMO_IP_LOCATION_FALLBACK_ENABLED",
    "false",
).lower() in {"1", "true", "yes", "on"}


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _valid_lat_lng(latitude: Optional[float], longitude: Optional[float]) -> bool:
    return (
        latitude is not None
        and longitude is not None
        and -90 <= latitude <= 90
        and -180 <= longitude <= 180
    )


def _browser_location_base(browser_location: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "permission_status": browser_location.get("permissionStatus") or browser_location.get("permission_status"),
        "timezone": browser_location.get("timezone"),
        "locale": browser_location.get("locale"),
        "captured_at": browser_location.get("capturedAt") or browser_location.get("captured_at"),
    }


def extract_client_ip(request: Any) -> Optional[str]:
    """Extract the best available client IP from proxy headers or socket info."""
    headers = getattr(request, "headers", {}) or {}
    forwarded_for = headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or None

    for header in ("cf-connecting-ip", "x-real-ip"):
        value = headers.get(header)
        if value:
            return value.strip()

    client = getattr(request, "client", None)
    return getattr(client, "host", None)


def _extract_aws_places_address(item: Mapping[str, Any]) -> dict[str, Any]:
    address = item.get("Address") if isinstance(item.get("Address"), Mapping) else {}
    country = address.get("Country") if isinstance(address.get("Country"), Mapping) else {}
    region = address.get("Region") if isinstance(address.get("Region"), Mapping) else {}
    sub_region = address.get("SubRegion") if isinstance(address.get("SubRegion"), Mapping) else {}
    position = item.get("Position") if isinstance(item.get("Position"), list) else []
    longitude = _to_float(position[0]) if len(position) > 0 else None
    latitude = _to_float(position[1]) if len(position) > 1 else None

    return {
        "city": address.get("Locality") or sub_region.get("Name") or address.get("District"),
        "pincode": address.get("PostalCode"),
        "state": region.get("Name"),
        "state_code": region.get("Code"),
        "country": country.get("Name"),
        "country_code": country.get("Code2") or country.get("Code3"),
        "district": address.get("District"),
        "building": address.get("Building"),
        "name": address.get("Building") or item.get("Title"),
        "display_name": address.get("Label") or item.get("Title"),
        "place_id": item.get("PlaceId"),
        "place_type": item.get("PlaceType"),
        "reverse_geocode_title": item.get("Title"),
        "reverse_geocode_distance_meters": item.get("Distance"),
        "reverse_geocode_position": item.get("Position"),
        "reverse_geocode_map_view": item.get("MapView"),
        "reverse_geocode_latitude": latitude,
        "reverse_geocode_longitude": longitude,
    }


async def _reverse_geocode_aws_places(latitude: float, longitude: float) -> dict[str, Any]:
    if not _WEB_WIDGET_LOCATION_ENABLED or not _AWS_PLACES_API_KEY:
        return {}
    try:
        client = await get_shared_async_http_client()
        response = await client.post(
            _AWS_PLACES_REVERSE_GEOCODE_URL,
            params={"key": _AWS_PLACES_API_KEY},
            json={"QueryPosition": [longitude, latitude]},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=_WEB_WIDGET_LOOKUP_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        logger.debug("Web widget reverse geocode failed: %s", exc)
        return {}

    items = data.get("ResultItems") if isinstance(data, Mapping) else None
    first = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else None
    if not first:
        return {}
    resolved = _extract_aws_places_address(first)
    logger.info(
        "Web widget location reverse geocoded: lat=%s lon=%s city=%s pincode=%s",
        latitude,
        longitude,
        resolved.get("city"),
        resolved.get("pincode"),
    )
    return resolved


async def _reverse_geocode(latitude: float, longitude: float) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_LOOKUP_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "https://nominatim.openstreetmap.org/reverse",
                params={
                    "lat": latitude,
                    "lon": longitude,
                    "format": "jsonv2",
                },
                headers={
                    "User-Agent": _NOMINATIM_USER_AGENT,
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        logger.debug("Demo reverse geocode failed: %s", exc)
        return {}

    address = data.get("address") or {}
    city = (
        address.get("city")
        or address.get("town")
        or address.get("municipality")
        or address.get("county")
        or address.get("state_district")
    )
    logger.info(
        "Demo client location reverse geocoded: lat=%s lon=%s city=%s",
        latitude,
        longitude,
        city,
    )
    return {
        "city": city,
        "pincode": address.get("postcode"),
        "state": address.get("state"),
        "country": address.get("country"),
        "country_code": address.get("country_code"),
        "name": data.get("name"),
        "display_name": data.get("display_name"),
    }


async def resolve_client_location_for_demo(
    *,
    browser_location: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve demo client location from browser coordinates via Nominatim."""
    browser_location = dict(browser_location or {})
    latitude = _to_float(browser_location.get("latitude"))
    longitude = _to_float(browser_location.get("longitude"))
    base = _browser_location_base(browser_location)
    if _IP_LOCATION_FALLBACK_ENABLED:
        base.update(
            {
                "public_ip": browser_location.get("publicIp") or browser_location.get("public_ip"),
                "public_city": browser_location.get("publicCity") or browser_location.get("public_city"),
                "public_pincode": browser_location.get("publicPincode") or browser_location.get("public_pincode"),
                "public_region": browser_location.get("publicRegion") or browser_location.get("public_region"),
                "public_country": browser_location.get("publicCountry") or browser_location.get("public_country"),
                "public_country_code": browser_location.get("publicCountryCode")
                or browser_location.get("public_country_code"),
                "public_latitude": browser_location.get("publicLatitude") or browser_location.get("public_latitude"),
                "public_longitude": browser_location.get("publicLongitude") or browser_location.get("public_longitude"),
                "public_timezone": browser_location.get("publicTimezone") or browser_location.get("public_timezone"),
                "public_isp": browser_location.get("publicIsp") or browser_location.get("public_isp"),
            }
        )

    if _valid_lat_lng(latitude, longitude):
        logger.info("Demo client location received from frontend: lat=%s lon=%s", latitude, longitude)
        resolved = await _reverse_geocode(latitude, longitude)
        city_source = "nominatim" if resolved.get("city") else None
        if _IP_LOCATION_FALLBACK_ENABLED:
            city_source = city_source or "ip_geolocation"
            resolved["city"] = resolved.get("city") or base.get("public_city")
            resolved["pincode"] = resolved.get("pincode") or base.get("public_pincode")
            resolved["state"] = resolved.get("state") or base.get("public_region")
            resolved["country"] = resolved.get("country") or base.get("public_country")
            resolved["country_code"] = resolved.get("country_code") or base.get("public_country_code")
        return {
            **base,
            **resolved,
            "source": "browser_geolocation",
            "city_source": city_source,
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_meters": browser_location.get("accuracy"),
        }

    return {
        **base,
        "source": "unavailable",
        "city_source": None,
    }


async def resolve_client_location_for_web_widget(
    *,
    browser_location: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve web widget browser coordinates via AWS Places reverse geocoding."""
    browser_location = dict(browser_location or {})
    latitude = _to_float(browser_location.get("latitude"))
    longitude = _to_float(browser_location.get("longitude"))
    base = _browser_location_base(browser_location)

    if _valid_lat_lng(latitude, longitude):
        logger.info("Web widget location received from frontend: lat=%s lon=%s", latitude, longitude)
        resolved = await _reverse_geocode_aws_places(latitude, longitude)
        city_source = None
        if resolved.get("city"):
            city_source = "aws_places"
        elif not _WEB_WIDGET_LOCATION_ENABLED:
            city_source = "disabled"
        elif not _AWS_PLACES_API_KEY:
            city_source = "missing_api_key"
        return {
            **base,
            **resolved,
            "source": "browser_geolocation",
            "city_source": city_source,
            "latitude": latitude,
            "longitude": longitude,
            "accuracy_meters": browser_location.get("accuracy"),
        }

    return {
        **base,
        "source": "unavailable",
        "city_source": None,
    }
