"""Judge.me Reviews API adapter -- READ methods only.

Covers review counts, product-ID resolution, review listing, and shop
settings. All shapes below were verified live against a real Judge.me shop
(field names, pagination behaviour, the ``setting_keys[]`` array
requirement, and the absence of any rate-limit headers).

WRITE methods (create_review / POST) are intentionally not implemented here.
A review created via the API is permanent and cannot be deleted, so that
path is a separate, carefully-reviewed change requiring explicit sign-off.

Stateless: no state mutation beyond the one-time cached config load. All
I/O is async via the shared httpx client (``utils/http_client.py``).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt

from fashion_bot.config_manager import aget_judgeme_config
from fashion_bot.rollbar_config import report_error
from fashion_bot.utils.http_client import get_shared_async_http_client
from fashion_bot.utils.utils import log_with_trace_id

logger = logging.getLogger(__name__)

# Required keys for a usable Judge.me config. Either missing/dummy
# short-circuits every method before a network call can fire.
_REQUIRED_CONFIG_KEYS = ("shop_domain", "api_token")

# Judge.me exposes NO rate-limit headers on any response (verified live) --
# there is nothing like Shopify's Retry-After to honour, so this is a plain
# capped exponential backoff, blind to how close any real limit is.
#
# Kept deliberately tight: this adapter backs a live chat-turn tool call, not
# a background job. Worst case PER CALL is _MAX_ATTEMPTS *
# _REQUEST_TIMEOUT_SECONDS (request time) + backoff between attempts -- with
# these values that's 2*6 + 2 = ~14s (the actual capped-exponential backoff
# for a single gap between 2 attempts), or ~15s using the simpler, more
# pessimistic bound of the full backoff cap per gap regardless of attempt
# count. aget_curated_reviews makes up to TWO of these sequentially
# (resolve, then fetch), so ~28-30s is the real worst case for one tool
# call, not this figure alone -- the caller
# (tool_factory._create_product_reviews_tool) wraps the whole thing in a
# single asyncio.wait_for(_PRODUCT_REVIEWS_DEADLINE_S=40.0) with real margin
# above that, so the retry budget actually gets to run rather than always
# losing a race against an outer deadline that fires first on every attempt
# after the first. test_adapter_retry_budget_fits_under_outer_deadline_twice
# in tests/test_judgeme_reviews_adapter.py asserts this relationship holds.
_MAX_ATTEMPTS = 2
_BACKOFF_CAP_SECONDS = 3.0
_REQUEST_TIMEOUT_SECONDS = 6


def _is_dummy_value(value: Optional[str]) -> bool:
    """True if ``value`` is empty or looks like a placeholder/test value."""
    if not value:
        return True
    v = value.strip().lower()
    if not v:
        return True
    return v.startswith("dummy") or v in {"placeholder", "test", "your_api_token", "your_shop_domain"}


def is_judgeme_configured(config: Dict[str, Any]) -> bool:
    """True if ``config`` (an ``aget_judgeme_config()`` result) has real,
    non-placeholder ``shop_domain``/``api_token`` values.

    Public so a caller deciding whether to even offer the review-listing
    tool (e.g. product_details_tools_factory registering it) can use the
    SAME placeholder detection :meth:`_validate_config` uses at call time,
    rather than a separate, weaker truthiness check that would treat a
    leftover ``{"shop_domain": "dummy", "api_token": "placeholder"}`` row
    as configured.
    """
    config = config or {}
    return not any(_is_dummy_value(config.get(key)) for key in _REQUIRED_CONFIG_KEYS)


class JudgeMeRetryableError(Exception):
    """Transient Judge.me failure (429 / 5xx) worth retrying."""


class JudgeMeReviewsAdapter:
    """Read-only Judge.me Reviews API client, scoped to one tenant.

    Use :meth:`create` to construct -- it loads the tenant's config once so
    every method afterwards reuses it instead of re-fetching per call.
    """

    def __init__(self, client_id: Optional[str] = None):
        self.client_id = client_id
        self._config: Optional[Dict[str, Any]] = None

    @classmethod
    async def create(cls, client_id: Optional[str] = None) -> "JudgeMeReviewsAdapter":
        """Async factory -- eagerly loads tenant config once."""
        adapter = cls(client_id=client_id)
        adapter._config = await aget_judgeme_config(client_id=client_id)
        return adapter

    # ── helpers ────────────────────────────────────────────────────────

    async def _aget_config(self) -> Dict[str, Any]:
        if self._config is not None:
            return self._config
        self._config = await aget_judgeme_config(client_id=self.client_id) or {}
        return self._config

    @staticmethod
    def _validate_config(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Return a ``configuration_missing`` result if shop_domain/api_token
        are absent or still placeholder values; else ``None``."""
        if is_judgeme_configured(config):
            return None
        return {
            "success": False,
            "status": "configuration_missing",
            "message": "Judge.me configuration missing or placeholder",
        }

    def _endpoints(self, config: Dict[str, Any]) -> Dict[str, str]:
        """Single place Judge.me URLs live. Verified live against a real shop."""
        api_base = (config.get("api_base") or "https://judge.me/api/v1").rstrip("/")
        return {
            "reviews": f"{api_base}/reviews",
            "reviews_count": f"{api_base}/reviews/count",
            "products": f"{api_base}/products/-1",
            "settings": f"{api_base}/settings",
        }

    async def _aget_with_backoff(self, url: str, params: Dict[str, Any]) -> httpx.Response:
        """GET with blind exponential backoff on 429/5xx.

        Non-429/5xx statuses (404, 422, etc.) are returned as-is for the
        caller to interpret -- they are NOT retried.
        """
        client = await get_shared_async_http_client()

        async def _attempt() -> httpx.Response:
            response = await client.get(url, params=params, timeout=_REQUEST_TIMEOUT_SECONDS)
            if response.status_code == 429 or response.status_code >= 500:
                raise JudgeMeRetryableError(f"Judge.me HTTP {response.status_code} on {url}")
            return response

        async for attempt in AsyncRetrying(
            retry=retry_if_exception_type((JudgeMeRetryableError, httpx.TransportError)),
            stop=stop_after_attempt(_MAX_ATTEMPTS),
            wait=lambda rs: min(2.0 ** rs.attempt_number, _BACKOFF_CAP_SECONDS),
            reraise=True,
        ):
            with attempt:
                return await _attempt()
        raise JudgeMeRetryableError("Judge.me request failed after retries")  # unreachable, reraise=True always propagates

    async def _aget_json(self, url: str, params: Dict[str, Any], *, context: str) -> Dict[str, Any]:
        """Shared GET-and-parse used by every read method.

        Returns ``{"success": False, "status": "configuration_missing", ...}``
        before any network call when config is absent/placeholder. Escalates
        via ``report_error`` only on genuine failures (exhausted retries,
        network error, bad JSON) -- not on ordinary 4xx like a 404, which
        callers interpret themselves from the status/body.
        """
        config = await self._aget_config()
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        try:
            response = await self._aget_with_backoff(url, params)
        except Exception as exc:
            message = f"JudgeMeReviewsAdapter.{context} failed after retries: {exc}"
            log_with_trace_id(None, message, "error", client_id=self.client_id)
            try:
                report_error(message, level="error", client_id=self.client_id)
            except Exception as report_exc:
                # report_error itself failing must never mask the original
                # Judge.me failure being returned below -- swallowed, but
                # not silently: still worth knowing report_error is broken.
                log_with_trace_id(
                    None, f"report_error failed in {context}: {report_exc}", "debug", client_id=self.client_id
                )
            return {"success": False, "status": "error", "message": str(exc)}

        try:
            body = response.json()
        except Exception as exc:
            log_with_trace_id(
                None,
                f"JudgeMeReviewsAdapter.{context}: non-JSON response: {exc}",
                "error",
                client_id=self.client_id,
            )
            return {
                "success": False,
                "status": "error",
                "message": f"Non-JSON response (HTTP {response.status_code})",
            }

        if response.status_code == 200:
            return {"success": True, "status_code": response.status_code, "body": body}
        # A non-200 that still made it here is a status Judge.me returned
        # cleanly (not a network/JSON failure, those are handled above) but
        # that no caller maps to something more specific -- 404 is mapped by
        # aresolve_product_id, for example. Default to "error" so every
        # failure carries a `status` key, matching the tool docstring's
        # promise that failures always have one, rather than callers that
        # don't special-case a code leaking the raw status_code/body instead.
        return {
            "success": False,
            "status": "error",
            "status_code": response.status_code,
            "body": body,
        }

    # ── read methods (all verified live) ────────────────────────────────

    async def aget_review_count(
        self,
        product_id: Optional[str] = None,
        published_only: bool = False,
    ) -> Dict[str, Any]:
        """GET /reviews/count -- ``{"count": N}``, no wrapper. Private token only.

        ``product_id`` omitted -> store-wide total. ``published_only=True``
        narrows correctly (verified: count dropped from 3 to 0 for a product
        whose reviews were all unpublished).
        """
        config = await self._aget_config()
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        endpoints = self._endpoints(config)
        params: Dict[str, Any] = {
            "shop_domain": config["shop_domain"],
            "api_token": config["api_token"],
        }
        if product_id:
            params["product_id"] = product_id
        if published_only:
            params["published"] = "true"

        result = await self._aget_json(endpoints["reviews_count"], params, context="aget_review_count")
        if not result.get("success"):
            return result
        return {"success": True, "count": result["body"].get("count")}

    async def aresolve_product_id(self, external_id: str) -> Dict[str, Any]:
        """GET /products/-1?external_id=<shopify id>.

        The internal Judge.me ID is ``id``, nested inside a ``product``
        object; ``external_id`` echoes the Shopify ID back. Returns the
        whole product object plus the internal id exposed at the top level.
        """
        config = await self._aget_config()
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        endpoints = self._endpoints(config)
        params = {
            "shop_domain": config["shop_domain"],
            "api_token": config["api_token"],
            "external_id": external_id,
        }

        result = await self._aget_json(endpoints["products"], params, context="aresolve_product_id")
        if not result.get("success"):
            if result.get("status_code") == 404:
                return {
                    "success": False,
                    "status": "not_found",
                    "message": f"No Judge.me product for external_id={external_id}",
                }
            return result

        product = result["body"].get("product") or {}
        return {"success": True, "id": product.get("id"), "product": product}

    async def aget_reviews(
        self,
        product_id: str,
        published_only: bool = True,
        page: int = 1,
        per_page: int = 100,
    ) -> Dict[str, Any]:
        """GET /reviews -- verified per-review fields: ``published`` (bool),
        ``rating`` (int 1-5), ``curated``, ``verified``, ``source``.

        The response carries no ``total_count``/``total_pages`` -- only
        ``current_page`` and ``per_page``. Callers detect the LAST page by
        getting back fewer rows than ``per_page``. ``per_page`` is capped at
        100 (the server caps + echoes it back even when a higher value is
        requested).

        ``published_only`` filters client-side on the real ``published``
        boolean -- Judge.me's ``published`` query param was only verified on
        the count endpoint, not on this listing endpoint, so filtering here
        ourselves is the only confirmed-correct behaviour.
        """
        config = await self._aget_config()
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        capped_per_page = min(per_page, 100)
        endpoints = self._endpoints(config)
        params = {
            "shop_domain": config["shop_domain"],
            "api_token": config["api_token"],
            "product_id": product_id,
            "page": page,
            "per_page": capped_per_page,
        }

        result = await self._aget_json(endpoints["reviews"], params, context="aget_reviews")
        if not result.get("success"):
            return result

        body = result["body"]
        raw_reviews = body.get("reviews") or []
        reviews = [r for r in raw_reviews if r.get("published") is True] if published_only else raw_reviews

        return {
            "success": True,
            "current_page": body.get("current_page"),
            "per_page": body.get("per_page"),
            "is_last_page": len(raw_reviews) < capped_per_page,
            "reviews": reviews,
        }

    async def aget_all_published_reviews(self, product_id: str, max_pages: int = 20) -> Dict[str, Any]:
        """Loop pages until a short page, returning ALL published reviews
        for a product -- for rollup building, which needs the full set.

        Capped at ``max_pages`` (default 20, i.e. up to 2000 reviews / 20
        sequential API calls) -- uncapped, a product with an unusually large
        review count would turn one call into an unbounded number of
        sequential HTTP requests. ``more_pages_exist`` tells the caller when
        the cap was hit before reaching the actual last page, so a rollup
        built from this can be honest about not being exhaustive rather than
        silently under-counting.
        """
        all_reviews: List[Dict[str, Any]] = []
        page = 1
        hit_cap = False
        while True:
            result = await self.aget_reviews(product_id=product_id, published_only=True, page=page, per_page=100)
            if not result.get("success"):
                return result
            all_reviews.extend(result["reviews"])
            if result.get("is_last_page"):
                break
            if page >= max_pages:
                hit_cap = True
                break
            page += 1

        return {
            "success": True,
            "reviews": all_reviews,
            "count": len(all_reviews),
            "more_pages_exist": hit_cap,
        }

    async def aget_settings(self, setting_keys: List[str]) -> Dict[str, Any]:
        """GET /settings -- requires ``setting_keys[]`` as an ARRAY param,
        else a 422. Confirmed readable keys: ``autopublish`` (bool),
        ``enable_review_pictures`` (bool). No "web reviews enabled" key is
        confirmed to exist -- this returns whatever Judge.me sends back for
        the keys given, nothing invented."""
        config = await self._aget_config()
        invalid = self._validate_config(config)
        if invalid:
            return invalid

        endpoints = self._endpoints(config)
        params = {
            "shop_domain": config["shop_domain"],
            "api_token": config["api_token"],
            "setting_keys[]": list(setting_keys or []),
        }

        result = await self._aget_json(endpoints["settings"], params, context="aget_settings")
        if not result.get("success"):
            return result

        return {"success": True, "settings": result["body"].get("settings") or {}}
