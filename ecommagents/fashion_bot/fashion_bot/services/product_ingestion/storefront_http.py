"""
Shared HTTP helper for public Shopify storefront JSON endpoints.

Two separate problems live here.

**Bot mitigation.** Storefronts behind Cloudflare fingerprint the TLS
handshake (JA3), not just the User-Agent. Python's OpenSSL signature is
flagged and answered with 429/403 *deterministically* — retrying or changing
the UA does not help, because nothing about the request is transient. This is
what took Clarks' catalog offline: ``/products.json`` returned 429 to every
Python request while ``curl`` against the same host, from the same IP, in the
same second, returned 200. We therefore fetch through ``curl_cffi``, which
forges a real browser's TLS fingerprint, and fall back to plain ``httpx``
when it is unavailable.

**Genuine rate limiting.** A single ``/onboard`` run fetches
``/products.json`` more than once — prompt generation and ingestion run
concurrently — on top of the policy page scrape. Some storefronts legitimately
rate-limit that burst. Retries with backoff (honouring ``Retry-After``) and an
inter-page delay ride those out instead of aborting ingestion.

Callers see ``httpx.HTTPStatusError`` on failure regardless of which client
performed the request, so error handling does not have to care.
"""

import asyncio
import logging
import os
from typing import Any, Dict, Optional, Tuple

import httpx

from fashion_bot.utils.http_client import get_shared_async_http_client

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import guard
    from curl_cffi import requests as _curl_requests

    _CURL_CFFI_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only on installs without the dep
    _curl_requests = None
    _CURL_CFFI_AVAILABLE = False
    logger.warning(
        "[STOREFRONT] curl_cffi not installed — storefront fetches fall back to "
        "httpx and will be blocked by Cloudflare-protected storefronts"
    )

# Browser profiles to impersonate, tried in order. Which profiles a given
# storefront (and our own egress) accepts varies, so we probe rather than
# hardcode one: at time of writing clarks.in accepts safari and rejects edge.
# Override with STOREFRONT_IMPERSONATE_PROFILES="prof1,prof2".
DEFAULT_IMPERSONATE_PROFILES: Tuple[str, ...] = ("safari17_0", "chrome", "edge")


def _configured_profiles() -> Tuple[str, ...]:
    raw = os.getenv("STOREFRONT_IMPERSONATE_PROFILES", "").strip()
    if not raw:
        return DEFAULT_IMPERSONATE_PROFILES
    profiles = tuple(p.strip() for p in raw.split(",") if p.strip())
    return profiles or DEFAULT_IMPERSONATE_PROFILES


# Which profile last worked for a given host, so a paginated catalog probes
# once rather than on every page. Process-local and purely an optimisation —
# a stale entry costs one failed request before we re-probe.
_host_profile: Dict[str, str] = {}

# Sent alongside the forged TLS fingerprint. Kept consistent with what
# ``scrape_domain`` already sends.
STOREFRONT_USER_AGENT = "Mozilla/5.0 (compatible; EcommAgents/1.0)"

STOREFRONT_HEADERS = {
    "User-Agent": STOREFRONT_USER_AGENT,
    "Accept": "application/json",
}

# Rate-limit windows on these endpoints are short, so a few spaced retries
# clear them.
MAX_RETRIES = 4
BACKOFF_BASE = 2.0

# Politeness delay between paginated pages of the same catalog.
PER_PAGE_DELAY = 0.5

# Cap on how long we will honour a server-supplied Retry-After, so a
# misconfigured storefront cannot stall onboarding indefinitely.
_MAX_RETRY_AFTER = 30.0

_RETRYABLE_STATUS = (429, 503)

# Statuses that suggest bot mitigation rather than load, and are therefore
# worth re-probing with a different browser profile.
_FINGERPRINT_STATUS = (403, 429)


def _retry_after_seconds(headers: Any, fallback: float) -> float:
    """Honour ``Retry-After`` when present and sane, else use *fallback*."""
    try:
        raw = headers.get("retry-after")
    except AttributeError:
        return fallback
    if not raw:
        return fallback
    try:
        return min(float(raw), _MAX_RETRY_AFTER)
    except (TypeError, ValueError):
        # Retry-After may also be an HTTP-date; backoff is a fine substitute.
        return fallback


def _raise_status(url: str, status_code: int) -> None:
    """Raise a uniform httpx error regardless of which client was used."""
    raise httpx.HTTPStatusError(
        f"Client error '{status_code}' for url '{url}'",
        request=httpx.Request("GET", url),
        response=httpx.Response(status_code),
    )


async def _get_via_curl_cffi(url, params, timeout, profile):
    """Single GET with a forged browser TLS fingerprint."""
    async with _curl_requests.AsyncSession() as session:
        return await session.get(
            url,
            params=params,
            headers=STOREFRONT_HEADERS,
            impersonate=profile,
            timeout=timeout,
        )


async def _get_via_httpx(url, params, timeout):
    """Fallback GET. Blocked by fingerprinting storefronts — see module docs."""
    client = await get_shared_async_http_client()
    return await client.get(
        url, params=params, headers=STOREFRONT_HEADERS, timeout=timeout
    )


async def _get_once(url: str, params, timeout: float, host: str):
    """
    Perform one logical GET, probing browser profiles as needed.

    Returns the first response that is not a fingerprint rejection, or the
    last response if every profile was rejected.
    """
    if not _CURL_CFFI_AVAILABLE:
        return await _get_via_httpx(url, params, timeout)

    # Prefer the profile that already worked for this host.
    profiles = list(_configured_profiles())
    known = _host_profile.get(host)
    if known:
        profiles = [known] + [p for p in profiles if p != known]

    last_response = None
    for profile in profiles:
        try:
            response = await _get_via_curl_cffi(url, params, timeout, profile)
        except Exception as exc:
            # Some profiles fail at the TLS layer depending on egress path.
            logger.debug(f"[STOREFRONT] profile {profile} failed for {host}: {exc}")
            continue

        if response.status_code not in _FINGERPRINT_STATUS:
            if _host_profile.get(host) != profile:
                logger.info(
                    f"[STOREFRONT] using impersonation profile '{profile}' for {host}"
                )
                _host_profile[host] = profile
            return response

        last_response = response
        logger.debug(
            f"[STOREFRONT] profile {profile} rejected by {host} "
            f"({response.status_code}), trying next"
        )

    # Every profile was rejected. Drop the memo so the next call re-probes
    # from the top rather than sticking to a profile that stopped working.
    _host_profile.pop(host, None)
    if last_response is not None:
        return last_response
    return await _get_via_httpx(url, params, timeout)


async def afetch_storefront_json(
    url: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    max_retries: int = MAX_RETRIES,
    timeout: float = 30.0,
    trace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    GET a storefront JSON endpoint, forging a browser TLS fingerprint and
    retrying transient rate limits.

    Retries on 429/503 with exponential backoff, preferring the server's
    ``Retry-After`` hint. Any other non-2xx raises immediately — a 404 means
    the endpoint is not a Shopify storefront and no amount of waiting helps.

    Raises:
        httpx.HTTPStatusError: non-retryable status, or retries exhausted.
    """
    tid = f"[{trace_id}] " if trace_id else ""
    host = httpx.URL(url).host

    last_status: Optional[int] = None

    for attempt in range(max_retries):
        response = await _get_once(url, params, timeout, host)
        status = response.status_code

        if status < 400:
            return response.json()

        if status in _RETRYABLE_STATUS and attempt < max_retries - 1:
            last_status = status
            wait = _retry_after_seconds(
                response.headers, BACKOFF_BASE ** (attempt + 1)
            )
            logger.warning(
                f"[STOREFRONT] {tid}{status} for {url}, "
                f"retry {attempt + 1}/{max_retries} after {wait:.0f}s"
            )
            await asyncio.sleep(wait)
            continue

        # Non-retryable, or we just burned the final attempt.
        _raise_status(url, status)

    logger.error(
        f"[STOREFRONT] {tid}giving up on {url} after {max_retries} attempts "
        f"(last status {last_status})"
    )
    _raise_status(url, last_status or 429)
    raise AssertionError("unreachable")  # pragma: no cover
