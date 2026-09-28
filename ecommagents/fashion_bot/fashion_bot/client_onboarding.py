"""
Client Onboarding Service

Automates the generation of client_configs and agents_config for new clients
by copying from a reference client and adapting via domain scraping + LLM calls.

Reference client: 7878c45d-a39d-488d-b37e-7fa4fc04ed7a

Flow:
1. Fetch new client's name & domain from `clients` table
2. Scrape the domain + policy subpages
3. Detect business type & product categories via LLM
4. Copy all client_configs from reference, replacing scrapable keys with LLM-extracted values
5. Copy all agents_config from reference, adapting prompts for the new business type
"""

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import httpx

from psycopg.types.json import Json

from fashion_bot.core.llm_factory import LLMFactory
from fashion_bot.core.llm_config import (
    LLMConfig,
    get_background_llm_config,
)
from fashion_bot.database_manager import get_async_postgres_connection, get_postgres_connection
from fashion_bot.env_loader import get_env
from fashion_bot.monitoring.otel_metrics import request_client_id
from fashion_bot.services.product_ingestion.sync_logger import ProductSyncLogger

logger = logging.getLogger("client_onboarding")

_NO_TRACE_CONFIG: Dict[str, Any] = {"callbacks": []}

# ─────────────── Onboarding status stage definitions ─────────────────

ONBOARDING_STAGES = [
    "client_lookup",
    "domain_scrape",
    "business_detection",
    "client_configs",
    "agents_config",
    "prompt_generation",
    "product_ingestion",
]


def _derive_overall_status(steps: Dict[str, Any]) -> str:
    """Derive overall onboarding status from individual step statuses."""
    statuses = [s.get("status", "pending") for s in steps.values()]
    if any(s == "running" for s in statuses):
        return "in_progress"
    if all(s in ("success", "skipped", "success_with_warnings") for s in statuses):
        if any(s == "success_with_warnings" for s in statuses):
            return "completed_with_warnings"
        return "completed"
    if any(s in ("failure", "failed") for s in statuses):
        return "failed"
    if all(s == "pending" for s in statuses):
        return "pending"
    return "in_progress"


class OnboardingStatusTracker:
    """
    Persists per-step onboarding progress to the ``onboarding_runs`` table.

    Every mutating method is async and writes to PostgreSQL immediately so
    the poll endpoint always returns fresh data.
    """

    def __init__(self, run_id: str, client_id: str):
        self.run_id = run_id
        self.client_id = client_id

    async def acreate_run(
        self,
        *,
        client_name: str = "",
        domain: str = "",
    ) -> None:
        """Insert the initial onboarding_runs row with all stages pending."""
        initial_steps = {
            stage: {"status": "pending", "message": ""}
            for stage in ONBOARDING_STAGES
        }
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO onboarding_runs
                            (client_id, run_id, status, steps,
                             client_name, domain)
                        VALUES (%s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (run_id) DO NOTHING
                        """,
                        (
                            self.client_id,
                            self.run_id,
                            "in_progress",
                            json.dumps(initial_steps),
                            client_name,
                            domain,
                        ),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to create onboarding run row: {e}", exc_info=True)

    async def aupdate_step(
        self,
        step_name: str,
        status: str,
        message: str = "",
        **extra: Any,
    ) -> None:
        """Update a single step's status inside the JSONB ``steps`` column."""
        step_data: Dict[str, Any] = {"status": status, "message": message}
        now_iso = datetime.now(timezone.utc).isoformat()
        if status == "running":
            step_data["started_at"] = now_iso
        if status in ("success", "failure", "failed", "skipped",
                       "partial_failure", "success_with_warnings"):
            step_data["completed_at"] = now_iso
        step_data.update(extra)
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE onboarding_runs
                           SET steps = jsonb_set(steps, %s, %s::jsonb),
                               updated_at = NOW()
                         WHERE run_id = %s
                        """,
                        (
                            [step_name],
                            json.dumps(step_data),
                            self.run_id,
                        ),
                    )
        except Exception as e:
            logger.error(
                f"❌ Failed to update onboarding step {step_name}: {e}",
                exc_info=True,
            )

    async def aupdate_counters(
        self,
        *,
        configs_created: int = 0,
        configs_skipped: int = 0,
        agents_created: int = 0,
        agents_skipped: int = 0,
        business_type: str = "",
        categories: str = "",
        errors: Optional[List[str]] = None,
        warnings: Optional[List[str]] = None,
    ) -> None:
        """Flush aggregate counters and metadata to the run row."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE onboarding_runs
                           SET configs_created  = %s,
                               configs_skipped  = %s,
                               agents_created   = %s,
                               agents_skipped   = %s,
                               business_type    = %s,
                               categories       = %s,
                               errors           = %s::jsonb,
                               warnings         = %s::jsonb,
                               updated_at       = NOW()
                         WHERE run_id = %s
                        """,
                        (
                            configs_created,
                            configs_skipped,
                            agents_created,
                            agents_skipped,
                            business_type,
                            categories,
                            json.dumps(errors or []),
                            json.dumps(warnings or []),
                            self.run_id,
                        ),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to update onboarding counters: {e}", exc_info=True)

    async def acomplete(self, overall_status: str, duration_seconds: float) -> None:
        """Mark the onboarding run as terminal."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        UPDATE onboarding_runs
                           SET status           = %s,
                               completed_at     = NOW(),
                               duration_seconds = %s,
                               updated_at       = NOW()
                         WHERE run_id = %s
                        """,
                        (overall_status, round(duration_seconds, 2), self.run_id),
                    )
        except Exception as e:
            logger.error(f"❌ Failed to complete onboarding run: {e}", exc_info=True)

    async def aderive_and_complete(self, duration_seconds: float) -> None:
        """Read current steps, derive overall status, and mark complete."""
        try:
            async with get_async_postgres_connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT steps FROM onboarding_runs WHERE run_id = %s",
                        (self.run_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        return
                    steps = row["steps"] or {}
                    overall = _derive_overall_status(steps)
                    await cur.execute(
                        """
                        UPDATE onboarding_runs
                           SET status           = %s,
                               completed_at     = NOW(),
                               duration_seconds = %s,
                               updated_at       = NOW()
                         WHERE run_id = %s
                        """,
                        (overall, round(duration_seconds, 2), self.run_id),
                    )
        except Exception as e:
            logger.error(
                f"❌ Failed to derive+complete onboarding run: {e}",
                exc_info=True,
            )


async def get_onboarding_run(client_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    """Fetch a single onboarding_runs row by client_id + run_id."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT run_id, client_id, status, steps,
                           client_name, domain, business_type, categories,
                           configs_created, configs_skipped,
                           agents_created, agents_skipped,
                           errors, warnings,
                           started_at, completed_at, duration_seconds
                      FROM onboarding_runs
                     WHERE client_id = %s AND run_id = %s
                    """,
                    (client_id, run_id),
                )
                row = await cur.fetchone()
                if not row:
                    return None

                steps = row["steps"] or {}
                total_steps = len(ONBOARDING_STAGES)
                completed_steps = sum(
                    1 for s in steps.values()
                    if s.get("status") in (
                        "success", "skipped", "failure", "failed",
                        "partial_failure", "success_with_warnings",
                    )
                )
                current_step = None
                for stage in ONBOARDING_STAGES:
                    if steps.get(stage, {}).get("status") == "running":
                        current_step = stage
                        break

                duration = row.get("duration_seconds")
                started = row.get("started_at")
                completed = row.get("completed_at")

                return {
                    "success": True,
                    "client_id": str(row["client_id"]),
                    "run_id": str(row["run_id"]),
                    "status": row["status"],
                    "message": _status_message(row["status"]),
                    "progress": {
                        "total_steps": total_steps,
                        "completed_steps": completed_steps,
                        "current_step": current_step,
                    },
                    "steps": steps,
                    "client_name": row.get("client_name"),
                    "domain": row.get("domain"),
                    "business_type": row.get("business_type"),
                    "categories": row.get("categories"),
                    "configs_created": row.get("configs_created") or 0,
                    "configs_skipped": row.get("configs_skipped") or 0,
                    "agents_created": row.get("agents_created") or 0,
                    "agents_skipped": row.get("agents_skipped") or 0,
                    "errors": row.get("errors") or [],
                    "warnings": row.get("warnings") or [],
                    "started_at": started.isoformat() if started else None,
                    "completed_at": completed.isoformat() if completed else None,
                    "duration_seconds": float(duration) if duration is not None else None,
                }
    except Exception as e:
        logger.error(f"❌ Failed to fetch onboarding run: {e}", exc_info=True)
        return None


def _status_message(status: str) -> str:
    return {
        "pending": "Onboarding has not started yet.",
        "in_progress": "Onboarding is in progress.",
        "completed": "Client onboarding completed successfully.",
        "completed_with_warnings": "Client onboarding completed with warnings.",
        "failed": "Client onboarding failed. Check errors for details.",
    }.get(status, f"Onboarding status: {status}")


async def get_onboarding_history(client_id: str) -> List[Dict[str, Any]]:
    """Return all onboarding runs for a client, newest first."""
    try:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT run_id, status, started_at, completed_at,
                           duration_seconds, client_name, domain
                      FROM onboarding_runs
                     WHERE client_id = %s
                     ORDER BY started_at DESC
                    """,
                    (client_id,),
                )
                rows = await cur.fetchall()
                result = []
                for r in rows:
                    started = r.get("started_at")
                    completed = r.get("completed_at")
                    duration = r.get("duration_seconds")
                    result.append({
                        "run_id": str(r["run_id"]),
                        "status": r["status"],
                        "client_name": r.get("client_name"),
                        "domain": r.get("domain"),
                        "started_at": started.isoformat() if started else None,
                        "completed_at": completed.isoformat() if completed else None,
                        "duration_seconds": float(duration) if duration is not None else None,
                    })
                return result
    except Exception as e:
        logger.error(f"❌ Failed to fetch onboarding history: {e}", exc_info=True)
        return []

REFERENCE_CLIENT_ID = "7878c45d-a39d-488d-b37e-7fa4fc04ed7a"

SCRAPABLE_CONFIG_KEYS = frozenset({
    "return_exchange_policy",
    "vendor_inquiry",
    "after_delivery_return_exchange",
    "vendor_contact_details",
    "delivery_policy",
    "discount_coupons",
})

# Reference configs whose actual values are copied verbatim to new clients
# (not emptied into placeholders). These are tenant-agnostic operational
# defaults that every client should inherit as-is.
VERBATIM_COPY_CONFIG_KEYS = frozenset({
    "order_update_rules",
    "order_display_limit",
})

# Never clone reference integration/auth configs across tenants.
EXCLUDED_REFERENCE_CONFIG_KEYS = frozenset({
    "website_urls",
    "gupshup_details",
    "gupshup_template_details",
    "shopify_details",
    "shiprocket_details",
    # Holds the display_rating toggle (config_manager.
    # aget_judgeme_rating_display_enabled) AND, for a tenant with the
    # Judge.me read integration configured, real API credentials
    # (config_manager.aget_judgeme_config) -- both per-tenant, neither should
    # ever be cloned onto another tenant (AGENTS.md §7: never clone
    # tenant-specific config across tenants).
    "judgeme_details",
})

EXCLUDED_REFERENCE_CONFIG_PREFIXES = (
    "shopify_",
    "shiprocket_",
)

CREDENTIAL_CONFIG_PLACEHOLDERS: Dict[str, Any] = {}

POLICY_SUBPAGES = [
    "/policies/refund-policy",
    "/policies/shipping-policy",
    "/policies/privacy-policy",
    "/policies/terms-of-service",
    "/pages/about-us",
    "/pages/contact-us",
    "/pages/faq",
    "/pages/return-policy",
    "/pages/exchange-policy",
    "/pages/shipping-policy",
    "/pages/delivery-policy",
    "/pages/terms-and-conditions",
]

MAX_CONCURRENT_LLM_CALLS = 5

def _onboard_llm_config(temperature: float = 0.1, max_tokens: int = 4000) -> LLMConfig:
    """OpenRouter config for onboarding.

    Delegates to the shared background helper so the model follows
    BACKGROUND_LLM_MODEL / LLM_MODEL like every other background workload,
    instead of being pinned to a literal here. Still lands on the background
    key (OPENROUTER_API_KEY_2) so onboarding never competes with the chat key.
    """
    return get_background_llm_config(temperature=temperature, max_tokens=max_tokens)


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _empty_placeholder(ref_value: Any) -> Any:
    """Return an empty value that preserves the type/structure of ref_value."""
    if isinstance(ref_value, dict):
        return {k: _empty_placeholder(v) for k, v in ref_value.items()}
    if isinstance(ref_value, list):
        return []
    if isinstance(ref_value, bool):
        return False
    if isinstance(ref_value, (int, float)):
        return 0
    return ""


def _deep_merge_with_reference(llm_value: Any, ref_value: Any) -> Any:
    """
    Merge LLM-extracted config_value with the reference config_value so that
    every key present in the reference is guaranteed to appear in the result.

    - If both are dicts: iterate all ref keys; use LLM value when present and
      non-empty, otherwise keep an empty placeholder (never Groovee's data).
    - If both are lists: use LLM list if non-empty, else empty list.
    - Otherwise: use LLM value if truthy, else empty string.
    """
    if isinstance(ref_value, dict):
        if not isinstance(llm_value, dict):
            return _empty_placeholder(ref_value)
        merged = {}
        for k, v in ref_value.items():
            if k in llm_value and llm_value[k] not in (None, "", [], {}):
                merged[k] = _deep_merge_with_reference(llm_value[k], v)
            else:
                merged[k] = _empty_placeholder(v)
        for k, v in llm_value.items():
            if k not in merged and v not in (None, "", [], {}):
                merged[k] = v
        return merged
    if isinstance(ref_value, list):
        if isinstance(llm_value, list) and llm_value:
            return llm_value
        return []
    if llm_value not in (None, "", [], {}):
        return llm_value
    return _empty_placeholder(ref_value)


# ─────────────────────────── Web scraping ───────────────────────────


async def _fetch_page_text(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url)
        if resp.status_code != 200:
            return ""
        html = resp.text
        html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]
    except Exception as e:
        logger.debug(f"Failed to fetch {url}: {e}")
        return ""


async def scrape_domain(domain: str) -> str:
    base = domain.rstrip("/")
    if not base.startswith("http"):
        base = f"https://{base}"

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; EcommAgents/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    }

    dynamic_paths = set(POLICY_SUBPAGES)

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=headers) as client:
        # Step 1: Fetch homepage raw html to find dynamic policy links
        homepage_html = ""
        try:
            resp = await client.get(base)
            if resp.status_code == 200:
                homepage_html = resp.text
        except Exception as e:
            logger.debug(f"Failed to fetch homepage for link discovery: {e}")

        # Step 2: Extract dynamic links matching keywords
        if homepage_html:
            policy_keywords = {"contact", "policy", "return", "shipping", "exchange", "terms", "faq", "about"}
            links = re.findall(r'href=["\']([^"\']+)["\']', homepage_html, re.IGNORECASE)
            for link in links:
                if any(link.startswith(p) for p in ("mailto:", "tel:", "#", "javascript:")):
                    continue
                path_lower = link.lower()
                if any(kw in path_lower for kw in policy_keywords):
                    if link.startswith("http"):
                        from urllib.parse import urlparse
                        try:
                            parsed = urlparse(link)
                            # Ensure it belongs to the same domain (allowing for subdomain differences)
                            domain_clean = domain.replace("www.", "").strip()
                            netloc_clean = parsed.netloc.replace("www.", "").strip()
                            if netloc_clean and domain_clean not in netloc_clean:
                                continue
                            link = parsed.path
                        except Exception:
                            continue
                    if not link.startswith("/"):
                        link = f"/{link}"
                    dynamic_paths.add(link)

        # Step 3: Fetch all standard and discovered subpages
        paths_to_scrape = sorted(list(dynamic_paths))
        logger.info(f"🔍 Discovered {len(paths_to_scrape)} paths to scrape for {domain}")

        tasks = [_fetch_page_text(client, base)]
        for path in paths_to_scrape:
            tasks.append(_fetch_page_text(client, f"{base}{path}"))

        results = await asyncio.gather(*tasks, return_exceptions=True)

    fragments: List[str] = []
    labels = ["HOMEPAGE"] + paths_to_scrape
    for label, result in zip(labels, results):
        if isinstance(result, str) and len(result) > 100:
            fragments.append(f"=== {label} ===\n{result}")

    combined = "\n\n".join(fragments)
    return combined[:50_000]


# ─────────────────────────── DB helpers ─────────────────────────────


def fetch_client_info(client_id: str) -> Dict[str, Any]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id, "name", domain FROM clients WHERE id = %s',
                (client_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"Client {client_id} not found in clients table")
            return {"id": str(row["id"]), "name": row["name"], "domain": row["domain"]}


def fetch_reference_client_configs() -> List[Dict[str, Any]]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_key, config_value FROM client_configs WHERE client_id = %s",
                (REFERENCE_CLIENT_ID,),
            )
            rows = cur.fetchall()
            return [
                row for row in rows
                if not _is_excluded_reference_config_key(row["config_key"])
            ]


def _is_excluded_reference_config_key(config_key: str) -> bool:
    if config_key in EXCLUDED_REFERENCE_CONFIG_KEYS:
        return True
    return any(config_key.startswith(prefix) for prefix in EXCLUDED_REFERENCE_CONFIG_PREFIXES)


def fetch_existing_config_keys(client_id: str) -> Set[str]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT config_key FROM client_configs WHERE client_id = %s",
                (client_id,),
            )
            return {row["config_key"] for row in cur.fetchall()}


def fetch_reference_agents() -> List[Dict[str, Any]]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT agent_name, agent_prompt, when_to_route, when_not_to_route,
                          created_by, display_name, test_q_a, route_test
                   FROM agents_config WHERE client_id = %s""",
                (REFERENCE_CLIENT_ID,),
            )
            return cur.fetchall()


def fetch_existing_agent_names(client_id: str) -> Set[str]:
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT agent_name FROM agents_config WHERE client_id = %s",
                (client_id,),
            )
            return {row["agent_name"] for row in cur.fetchall()}


def _upsert_client_configs(client_id: str, configs: Dict[str, Any]) -> int:
    """Upsert configs into client_configs. Returns count of rows written."""
    count = 0
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            for key, value in configs.items():
                json_str = json.dumps(value)
                cur.execute(
                    """
                    INSERT INTO client_configs (client_id, config_key, config_value)
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (client_id, config_key) DO UPDATE
                        SET config_value = EXCLUDED.config_value
                    """,
                    (client_id, key, json_str),
                )
                count += 1
    logger.info(f"✅ Upserted {count} client_configs for {client_id}")
    return count


def _insert_agent(client_id: str, agent_name: str, prompt: str,
                  when_to_route: str, when_not_to_route: str,
                  display_name: Optional[str] = None,
                  test_q_a: Any = None,
                  route_test: Any = None):
    now = datetime.now(timezone.utc)
    with get_postgres_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM agents_config WHERE client_id = %s AND agent_name = %s",
                (client_id, agent_name),
            )
            if cur.fetchone():
                cur.execute(
                    """UPDATE agents_config
                       SET agent_prompt = %s, display_name = %s,
                           test_q_a = %s, route_test = %s, updated_at = %s
                       WHERE client_id = %s AND agent_name = %s""",
                    (prompt, display_name,
                     Json(test_q_a) if test_q_a is not None else None,
                     Json(route_test) if route_test is not None else None,
                     now, client_id, agent_name),
                )
            else:
                cur.execute(
                    """INSERT INTO agents_config
                           (client_id, agent_name, agent_prompt,
                            when_to_route, when_not_to_route, created_by,
                            display_name, test_q_a, route_test,
                            created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (client_id, agent_name, prompt,
                     when_to_route or "", when_not_to_route or "",
                     "onboarding_system",
                     display_name,
                     Json(test_q_a) if test_q_a is not None else None,
                     Json(route_test) if route_test is not None else None,
                     now, now),
                )


# ─────────────────────────── LLM helpers ────────────────────────────


async def _detect_business_type(
    client_name: str, domain: str, scraped_text: str
) -> Tuple[str, str]:
    prompt = f"""Analyze this e-commerce website and determine:
1. The business type/industry (e.g., "fashion and apparel", "mobile accessories", "beauty products", "baby products", "electronics", "home decor")
2. The main product categories available on the website

Website: {client_name} ({domain})

Scraped content (truncated):
{scraped_text[:15000]}

Return a JSON object:
{{"business_type": "<short description>", "categories": ["cat1", "cat2", ...]}}

Return ONLY the JSON, no markdown."""

    llm = LLMFactory.get_llm(
        tool_name="client_onboarding",
        override_config=_onboard_llm_config(temperature=0.1, max_tokens=1000),
    )
    response = await llm.ainvoke([("human", prompt)], config=_NO_TRACE_CONFIG)
    content = _strip_markdown_fences(response.content or "")
    try:
        data = json.loads(content)
        btype = data.get("business_type", "e-commerce")
        cats = data.get("categories", [])
        return btype, ", ".join(cats) if cats else "general products"
    except json.JSONDecodeError:
        logger.warning("Business-type detection returned non-JSON; defaulting")
        return "e-commerce", "general products"


async def _fetch_shopify_collections(domain: str) -> Dict[str, str]:
    """Fetch real collection handles from Shopify storefront API.

    Returns a dict of ``{handle: title}`` for all public collections.
    """
    import ssl
    import aiohttp
    from urllib.parse import urlparse

    clean = domain.strip().rstrip("/")
    if clean.startswith(("http://", "https://")):
        clean = urlparse(clean).hostname or clean
    url = f"https://{clean}/collections.json?limit=250"
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15), ssl=ssl_ctx) as resp:
                if resp.status != 200:
                    logger.warning(f"⚠️ collections.json returned {resp.status} for {domain}")
                    return {}
                data = await resp.json()
                collections = data.get("collections", [])
                return {c["handle"]: c["title"] for c in collections if c.get("handle")}
    except Exception as e:
        logger.warning(f"⚠️ Failed to fetch collections from {domain}: {e}")
        return {}


async def _extract_category_urls(
    client_name: str,
    domain: str,
    scraped_text: str,
    shopify_collections: Dict[str, str],
) -> Dict[str, str]:
    """Extract category_urls by having the LLM select from real Shopify collections."""
    from urllib.parse import urlparse

    clean = domain.strip().rstrip("/")
    if clean.startswith(("http://", "https://")):
        clean = urlparse(clean).hostname or clean
    domain = clean

    collections_list = "\n".join(
        f"  {handle} -> {title}" for handle, title in shopify_collections.items()
    )

    prompt = f"""You are selecting the main navigation categories for an e-commerce website.

Client: {client_name}
Domain: {domain}

Here is the scraped website content showing the navigation structure:
{scraped_text[:15000]}

---

Below are ALL real Shopify collection handles for this store:
{collections_list}

---

Select the collections that correspond to the MAIN site navigation categories and subcategories.
Include:
- Every subcategory under Men's, Women's, Kids (e.g. jeans, hoodies, t-shirts, jackets, shorts, etc.)
- Top-level sections (new arrivals, sale, online exclusive, best sellers, etc.)
- Sale subcategories if they exist
- Do NOT include internal/system collections (test, tax, dummy, frontpage, etc.)

Return a JSON object where:
- Each key is the EXACT collection handle from the list above (do NOT invent handles)
- Each value is the full URL: https://{domain}/collections/{{handle}}

Aim for 25-50 entries covering the full site navigation. Return ONLY the JSON object, no markdown."""

    llm = LLMFactory.get_llm(
        tool_name="client_onboarding",
        override_config=_onboard_llm_config(temperature=0.1, max_tokens=4000),
    )
    response = await llm.ainvoke([("human", prompt)], config=_NO_TRACE_CONFIG)
    content = _strip_markdown_fences(response.content or "")
    try:
        result = json.loads(content)
        valid = {
            k: v for k, v in result.items()
            if k in shopify_collections
        }
        invalid_count = len(result) - len(valid)
        if invalid_count:
            logger.warning(f"⚠️ category_urls: dropped {invalid_count} handles not in Shopify collections")
        logger.info(f"✅ category_urls: {len(valid)} valid entries selected from {len(shopify_collections)} collections")
        return valid
    except json.JSONDecodeError:
        logger.warning("category_urls extraction returned non-JSON")
        return {}


async def _extract_scrapable_configs(
    client_name: str,
    domain: str,
    scraped_text: str,
    reference_configs_full: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Extract scrapable config values via LLM.

    reference_configs_full contains the COMPLETE reference JSON for each
    scrapable key (not truncated) so the LLM sees every sub-key.
    """
    ref_without_categories = {k: v for k, v in reference_configs_full.items() if k != "category_urls"}

    prompt = f"""You are analyzing an e-commerce website to extract business configuration data.

Client: {client_name}
Domain: {domain}

Scraped website content:
{scraped_text[:30000]}

---

Below are the COMPLETE reference config values from a fashion e-commerce store.
Each top-level key is a config_key. The value is the FULL JSON stored in config_value.

CRITICAL RULES:
- You MUST output every single sub-key that exists in the reference value for each config_key.
- Do NOT drop or skip any key. The output JSON for each config_key must have AT LEAST all the same keys as the reference.
- For sub-keys where you found relevant information from the scraped content, replace the value with the new client's information.
- For sub-keys where you did NOT find information from the scraped content, keep the reference value EXACTLY as-is (it is better to keep the reference text than to omit the key).
- You may add NEW keys if you discovered extra information, but never remove existing ones.

Reference values (COMPLETE — preserve every key):
{json.dumps(ref_without_categories, indent=2, ensure_ascii=False)}

Return a JSON object with the same {len(ref_without_categories)} top-level keys. Each value must be the adapted config_value for "{client_name}" with ALL sub-keys preserved.
Return ONLY the JSON object, no markdown."""

    llm = LLMFactory.get_llm(
        tool_name="client_onboarding",
        override_config=_onboard_llm_config(temperature=0.2, max_tokens=8000),
    )
    response = await llm.ainvoke([("human", prompt)], config=_NO_TRACE_CONFIG)
    content = _strip_markdown_fences(response.content or "")
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        logger.warning("Config extraction returned non-JSON")
        return {}


async def _adapt_agent_prompt(
    semaphore: asyncio.Semaphore,
    client_name: str,
    domain: str,
    business_type: str,
    categories_text: str,
    agent_name: str,
    reference_prompt: str,
    contact_details: Dict[str, Any],
    policies_and_rules: Dict[str, Any],
) -> str:
    async with semaphore:
        contact_str = json.dumps(contact_details, indent=2, ensure_ascii=False)
        policies_str = json.dumps(policies_and_rules, indent=2, ensure_ascii=False)

        prompt = f"""Adapt this AI agent prompt from a reference fashion e-commerce business to a new client.

New client: {client_name}
Domain: {domain}
Business type: {business_type}
Product categories: {categories_text}

---
NEW CLIENT SPECIFIC DATA:
1. Contact Details:
{contact_str}

2. Scraped Policies & Guidelines:
{policies_str}
---

Agent: {agent_name}

Make MINIMAL changes to the reference prompt:
1. If the intro says "fashion business" or similar, change it to "{business_type} business"
2. Replace fashion-specific category/product examples (t-shirts, kurtas, jeans, etc.) with categories relevant to {business_type}: {categories_text}
3. IDENTIFY any contact details (support phone numbers, email ids, physical addresses) in the reference prompt and REPLACE them with the new client's contact details listed above. Instruct the agent to output the dynamic variable placeholder `{{{{support_team_contact_details}}}}` when responding to customer contact inquiries, ensuring future contact updates do not require regenerating prompt configurations.
4. IDENTIFY any specific business rules (e.g. "7-day return policy", "shipping takes 3-5 days") in the reference prompt and REPLACE them with the new client's scraped policies listed above.
5. Keep ALL rules, tool chains, workflows, formatting instructions, headings (e.g. [HEADING]), and logic EXACTLY the same.
6. When providing support team/contact details (phone/email), do not forget to add the timings (Mon - Sat, 9 AM - 7 PM IST) 

Reference prompt:
---
{reference_prompt}
---

Return ONLY the adapted prompt. No explanation, no markdown fences."""

        llm = LLMFactory.get_llm(
            tool_name="client_onboarding",
            override_config=_onboard_llm_config(temperature=0.1, max_tokens=16000),
        )
        response = await llm.ainvoke([("human", prompt)], config=_NO_TRACE_CONFIG)
        return (response.content or "").strip()


# ─────────────────────────── Main orchestrator ──────────────────────


async def onboard_client(
    client_id: str,
    *,
    skip_client_configs: bool = False,
    skip_agents_config: bool = False,
    skip_product_ingestion: bool = False,
) -> Dict[str, Any]:
    """
    Onboard a new client by copying and adapting configs from the reference client.
    Skips any configs or agents that already exist for the target client.

    Persists per-step progress to the ``onboarding_runs`` table so the poll
    endpoint can return a unified view of all stages (sync + async).
    """
    # Scope the OTel baggage to this onboarding so every downstream LLM
    # call (prompt generation, attribute extraction, etc.) gets tagged
    # with this client's id in llm.* metrics. Without this, onboarding
    # spend lands in the client_id=unknown bucket on the dashboard.
    # See request_client_id docstring for why this is scoped (not
    # fire-and-forget like set_request_client_id).
    with request_client_id(client_id):
        return await _onboard_client_impl(
            client_id,
            skip_client_configs=skip_client_configs,
            skip_agents_config=skip_agents_config,
            skip_product_ingestion=skip_product_ingestion,
        )


async def _onboard_client_impl(
    client_id: str,
    *,
    skip_client_configs: bool = False,
    skip_agents_config: bool = False,
    skip_product_ingestion: bool = False,
) -> Dict[str, Any]:
    """Implementation body of onboard_client. See onboard_client for the
    public wrapper that handles OTel baggage scoping."""
    t0 = time.time()
    ingestion_run_id = str(uuid.uuid4())
    tracker = OnboardingStatusTracker(run_id=ingestion_run_id, client_id=client_id)

    result: Dict[str, Any] = {
        "client_id": client_id,
        "run_id": ingestion_run_id,
        "configs_created": 0,
        "configs_skipped": 0,
        "agents_created": 0,
        "agents_skipped": 0,
        "errors": [],
        "warnings": [],
    }
    result["steps"] = {}

    # ── 1. Client info ──────────────────────────────────────────────
    await tracker.aupdate_step("client_lookup", "running", "Loading client record...")
    try:
        info = fetch_client_info(client_id)
        result["steps"]["client_lookup"] = {
            "status": "success",
            "message": "Client record loaded successfully.",
        }
        await tracker.aupdate_step(
            "client_lookup", "success", "Client record loaded successfully.",
        )
    except ValueError as e:
        await tracker.aupdate_step(
            "client_lookup", "failure", str(e),
        )
        await tracker.acomplete("failed", time.time() - t0)
        return {"success": False, "error": str(e)}

    client_name = info["name"]
    domain = info["domain"]
    result["client_name"] = client_name
    result["domain"] = domain

    if not domain:
        await tracker.aupdate_step(
            "client_lookup", "failure",
            f"Client {client_id} has no domain set in the clients table",
        )
        await tracker.acomplete("failed", time.time() - t0)
        return {"success": False, "error": f"Client {client_id} has no domain set in the clients table"}

    # Now that we know the client, create the persistent run row
    await tracker.acreate_run(client_name=client_name, domain=domain)
    await tracker.aupdate_step(
        "client_lookup", "success", "Client record loaded successfully.",
    )

    logger.info(f"🚀 Onboarding {client_name} ({domain})")

    # ── 2. Scrape domain ────────────────────────────────────────────
    logger.info(f"🔍 Scraping {domain}...")
    await tracker.aupdate_step("domain_scrape", "running", f"Scraping {domain}...")
    scraped_text = await scrape_domain(domain)
    if not scraped_text:
        logger.warning(f"⚠️ No content scraped from {domain}")
        result["steps"]["domain_scrape"] = {
            "status": "warning",
            "message": "No content could be scraped from the configured domain.",
            "content_found": False,
        }
        await tracker.aupdate_step(
            "domain_scrape", "success_with_warnings",
            "No content could be scraped from the configured domain.",
            content_found=False,
        )
    else:
        result["steps"]["domain_scrape"] = {
            "status": "success",
            "message": "Domain scraped successfully.",
            "content_found": True,
        }
        await tracker.aupdate_step(
            "domain_scrape", "success", "Domain scraped successfully.",
            content_found=True,
        )

    # ── 3. Detect business type ─────────────────────────────────────
    logger.info("🧠 Detecting business type...")
    await tracker.aupdate_step(
        "business_detection", "running", "Detecting business type...",
    )
    business_type, categories_text = await _detect_business_type(
        client_name, domain, scraped_text
    )
    result["business_type"] = business_type
    result["categories"] = categories_text
    result["steps"]["business_detection"] = {
        "status": "success",
        "message": "Business type and categories detected successfully.",
        "business_type": business_type,
        "categories": categories_text,
    }
    await tracker.aupdate_step(
        "business_detection", "success",
        "Business type and categories detected successfully.",
        business_type=business_type, categories=categories_text,
    )
    logger.info(f"📋 Type: {business_type} | Categories: {categories_text}")

    # ── 4. Client configs ───────────────────────────────────────────
    if skip_client_configs:
        logger.info("⏭️ Skipping client_configs (skip_client_configs=True)")
        result["steps"]["client_configs"] = {
            "status": "skipped",
            "message": "Skipped by request (skip_client_configs=True).",
            "created": 0,
            "skipped": 0,
        }
        await tracker.aupdate_step(
            "client_configs", "skipped",
            "Skipped by request (skip_client_configs=True).",
        )
    else:
        logger.info("📦 Processing client_configs...")
        await tracker.aupdate_step(
            "client_configs", "running", "Processing client configs...",
        )
        ref_configs = fetch_reference_client_configs()
        existing_keys = fetch_existing_config_keys(client_id)

        ref_dict: Dict[str, Any] = {r["config_key"]: r["config_value"] for r in ref_configs}

        ref_scrapable_full: Dict[str, Any] = {
            k: ref_dict[k] for k in SCRAPABLE_CONFIG_KEYS if k in ref_dict
        }

        scraped_configs: Dict[str, Any] = {}
        if scraped_text:
            try:
                scraped_configs = await _extract_scrapable_configs(
                    client_name, domain, scraped_text, ref_scrapable_full
                )
                logger.info(f"🧠 LLM extracted {len(scraped_configs)} scrapable configs: {list(scraped_configs.keys())}")
            except Exception as e:
                logger.error(f"❌ Config extraction failed: {e}", exc_info=True)
                result["warnings"].append(f"Config extraction LLM error: {e}")

        category_urls: Dict[str, str] = {}
        if "category_urls" not in existing_keys:
            try:
                shopify_collections = await _fetch_shopify_collections(domain)
                logger.info(f"🛍️ Fetched {len(shopify_collections)} real Shopify collections from {domain}")
                if shopify_collections and scraped_text:
                    category_urls = await _extract_category_urls(
                        client_name, domain, scraped_text, shopify_collections
                    )
                elif shopify_collections:
                    category_urls = {
                        h: f"https://{domain}/collections/{h}"
                        for h in shopify_collections
                    }
                    logger.info(f"📂 Using all {len(category_urls)} Shopify collections as category_urls (no scraped text)")
            except Exception as e:
                logger.error(f"❌ category_urls extraction failed: {e}", exc_info=True)
                result["warnings"].append(f"category_urls extraction error: {e}")

        configs_to_insert: Dict[str, Any] = {}
        for key, ref_value in ref_dict.items():
            if key in existing_keys:
                result["configs_skipped"] += 1
                continue
            if key == "category_urls":
                configs_to_insert[key] = category_urls
                logger.info(f"📂 category_urls: {len(category_urls)} entries from verified Shopify handles")
            elif key in SCRAPABLE_CONFIG_KEYS and key in scraped_configs:
                configs_to_insert[key] = _deep_merge_with_reference(
                    scraped_configs[key], ref_value
                )
            elif key in CREDENTIAL_CONFIG_PLACEHOLDERS:
                configs_to_insert[key] = CREDENTIAL_CONFIG_PLACEHOLDERS[key]
                logger.info(f"🔑 Using empty placeholder for credential key '{key}'")
            elif key in VERBATIM_COPY_CONFIG_KEYS:
                configs_to_insert[key] = copy.deepcopy(ref_value)
                logger.info(f"📋 Key '{key}' copied verbatim from reference client (value preserved)")
            else:
                configs_to_insert[key] = _empty_placeholder(ref_value)
                logger.info(f"📄 Key '{key}' created with empty placeholder (structure preserved)")

        for key, placeholder in CREDENTIAL_CONFIG_PLACEHOLDERS.items():
            if key not in existing_keys and key not in configs_to_insert:
                configs_to_insert[key] = placeholder
                logger.info(f"🔑 Using empty placeholder for credential key '{key}'")

        if configs_to_insert:
            try:
                result["configs_created"] = _upsert_client_configs(client_id, configs_to_insert)
            except Exception as e:
                logger.error(f"❌ Config insert failed: {e}", exc_info=True)
                result["errors"].append(f"Config insert error: {e}")

        configs_step_status = (
            "failed" if any(err.startswith("Config insert error:") for err in result["errors"])
            else "success_with_warnings" if any(w.startswith("Config extraction LLM error:") for w in result["warnings"])
            else "success"
        )
        result["steps"]["client_configs"] = {
            "status": configs_step_status,
            "message": "Client configs prepared from the reference template.",
            "created": result["configs_created"],
            "skipped": result["configs_skipped"],
            "excluded_reference_keys": sorted(EXCLUDED_REFERENCE_CONFIG_KEYS),
            "excluded_reference_prefixes": list(EXCLUDED_REFERENCE_CONFIG_PREFIXES),
        }
        await tracker.aupdate_step(
            "client_configs", configs_step_status,
            "Client configs prepared from the reference template.",
            created=result["configs_created"], skipped=result["configs_skipped"],
        )

    # ── 5. Agents config ────────────────────────────────────────────
    if skip_agents_config:
        logger.info("⏭️ Skipping agents_config (skip_agents_config=True)")
        result["steps"]["agents_config"] = {
            "status": "skipped",
            "message": "Skipped by request (skip_agents_config=True).",
            "created": 0,
            "skipped": 0,
        }
        await tracker.aupdate_step(
            "agents_config", "skipped",
            "Skipped by request (skip_agents_config=True).",
        )
    else:
        logger.info("🤖 Processing agents_config...")
        await tracker.aupdate_step(
            "agents_config", "running", "Adapting agent prompts...",
        )
        ref_agents = fetch_reference_agents()
        existing_agents = fetch_existing_agent_names(client_id)

        # Resolve client contact details and policies for prompt adaptation
        contact_details = {}
        scraped_policies = {}

        if not skip_client_configs and 'configs_to_insert' in locals():
            contact_details = configs_to_insert.get("vendor_contact_details", {})
            scraped_policies = {
                "return_exchange_policy": configs_to_insert.get("return_exchange_policy", {}),
                "delivery_policy": configs_to_insert.get("delivery_policy", {}),
                "after_delivery_return_exchange": configs_to_insert.get("after_delivery_return_exchange", {}),
            }
        else:
            # Fallback: Query the database for the client's configurations
            try:
                with get_postgres_connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT config_key, config_value FROM client_configs WHERE client_id = %s",
                            (client_id,),
                        )
                        db_configs = {row["config_key"]: row["config_value"] for row in cur.fetchall()}
                contact_details = db_configs.get("vendor_contact_details", {})
                scraped_policies = {
                    "return_exchange_policy": db_configs.get("return_exchange_policy", {}),
                    "delivery_policy": db_configs.get("delivery_policy", {}),
                    "after_delivery_return_exchange": db_configs.get("after_delivery_return_exchange", {}),
                }
            except Exception as e:
                logger.warning(f"⚠️ Failed to load client configs from DB for prompt personalization: {e}")

        agents_to_create = [a for a in ref_agents if a["agent_name"] not in existing_agents]
        result["agents_skipped"] = len(ref_agents) - len(agents_to_create)

        if agents_to_create:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)

            async def _adapt_and_insert(agent: Dict[str, Any]) -> Optional[str]:
                name = agent["agent_name"]
                try:
                    adapted = await _adapt_agent_prompt(
                        semaphore, client_name, domain,
                        business_type, categories_text,
                        name, agent["agent_prompt"] or "",
                        contact_details=contact_details,
                        policies_and_rules=scraped_policies,
                    )
                    _insert_agent(
                        client_id, name, adapted,
                        agent.get("when_to_route", ""),
                        agent.get("when_not_to_route", ""),
                        display_name=agent.get("display_name"),
                        test_q_a=agent.get("test_q_a"),
                        route_test=agent.get("route_test"),
                    )
                    logger.info(f"  ✅ {name}")
                    return None
                except Exception as e:
                    logger.error(f"  ❌ {name}: {e}", exc_info=True)
                    return f"Agent {name}: {e}"

            errors = await asyncio.gather(
                *[_adapt_and_insert(a) for a in agents_to_create]
            )
            for err in errors:
                if err:
                    result["warnings"].append(err)
                else:
                    result["agents_created"] += 1

        agent_warning_count = len([w for w in result["warnings"] if w.startswith("Agent ")])
        agents_step_status = "success_with_warnings" if agent_warning_count else "success"
        result["steps"]["agents_config"] = {
            "status": agents_step_status,
            "message": "Agent prompts were adapted from the reference client.",
            "created": result["agents_created"],
            "skipped": result["agents_skipped"],
            "warnings_count": agent_warning_count,
        }
        await tracker.aupdate_step(
            "agents_config", agents_step_status,
            "Agent prompts were adapted from the reference client.",
            created=result["agents_created"], skipped=result["agents_skipped"],
        )

    # ── 6 + 7. Prompt generation & product ingestion (background) ──
    website_url = f"https://{domain}" if not domain.startswith("http") else domain
    _upsert_client_configs(client_id, {
        "website_urls": {"website_url": website_url},
    })

    if skip_product_ingestion:
        logger.info("⏭️ Skipping prompt generation & product ingestion (skip_product_ingestion=True)")
        result["steps"]["prompt_generation"] = {
            "status": "skipped",
            "message": "Skipped by request (skip_product_ingestion=True).",
        }
        result["steps"]["product_ingestion"] = {
            "status": "skipped",
            "message": "Skipped by request (skip_product_ingestion=True).",
        }
        await tracker.aupdate_step(
            "prompt_generation", "skipped",
            "Skipped by request (skip_product_ingestion=True).",
        )
        await tracker.aupdate_step(
            "product_ingestion", "skipped",
            "Skipped by request (skip_product_ingestion=True).",
        )
        await tracker.aupdate_counters(
            configs_created=result["configs_created"],
            configs_skipped=result["configs_skipped"],
            agents_created=result["agents_created"],
            agents_skipped=result["agents_skipped"],
            business_type=business_type,
            categories=categories_text,
            errors=result["errors"],
            warnings=result["warnings"],
        )
        await tracker.aderive_and_complete(time.time() - t0)
        result["success"] = True
        result["duration_seconds"] = round(time.time() - t0, 2)
        return result

    scraped_categories = [
        c.strip() for c in categories_text.split(",") if c.strip()
    ]

    # Mark background steps as running before launching the task
    await tracker.aupdate_step(
        "prompt_generation", "running",
        "Generating personalized prompts in background...",
    )
    await tracker.aupdate_step(
        "product_ingestion", "running",
        "Product ingestion started in background...",
    )

    # Flush aggregate counters now (sync steps are done)
    await tracker.aupdate_counters(
        configs_created=result["configs_created"],
        configs_skipped=result["configs_skipped"],
        agents_created=result["agents_created"],
        agents_skipped=result["agents_skipped"],
        business_type=business_type,
        categories=categories_text,
        errors=result["errors"],
        warnings=result["warnings"],
    )

    async def _run_prompts_and_ingestion():
        """Fetch products once, then run prompt generation followed by ingestion."""
        bg_t0 = time.time()
        from fashion_bot.prompt_generator import (
            ensure_personalized_prompts,
            _fetch_product_sample,
        )
        from fashion_bot.services.product_ingestion import ProductIngestionOrchestrator

        try:
            raw_products = await _fetch_product_sample(domain, client_id, limit=5000)
            logger.info(
                f"📦 Fetched {len(raw_products)} products from {domain} "
                f"(shared for prompts + ingestion) run_id={ingestion_run_id}"
            )
        except Exception as e:
            logger.error(
                f"❌ Product fetch failed for {client_name} run_id={ingestion_run_id}: {e}",
                exc_info=True,
            )
            await tracker.aupdate_step(
                "prompt_generation", "failure",
                f"Product fetch failed: {e}",
            )
            await tracker.aupdate_step(
                "product_ingestion", "failure",
                f"Product fetch failed: {e}",
            )
            await tracker.aderive_and_complete(time.time() - t0)
            return

        async def _generate_prompts():
            try:
                prompt_result = await ensure_personalized_prompts(
                    client_id, domain,
                    business_type=business_type, force=True,
                    business_categories=scraped_categories,
                    raw_products=raw_products,
                )
                created = prompt_result.get("created", [])
                prompt_errors = prompt_result.get("errors", [])
                logger.info(
                    f"✅ Background prompt generation complete for {client_name}: "
                    f"{len(created)} created, {len(prompt_errors)} errors"
                )
                if prompt_errors:
                    await tracker.aupdate_step(
                        "prompt_generation", "success_with_warnings",
                        f"{len(created)} prompts created, {len(prompt_errors)} errors.",
                        created=created, errors=prompt_errors,
                    )
                else:
                    await tracker.aupdate_step(
                        "prompt_generation", "success",
                        f"{len(created)} personalized prompts generated.",
                        created=created,
                    )
            except Exception as e:
                logger.error(
                    f"❌ Background prompt generation failed for {client_name}: {e}",
                    exc_info=True,
                )
                await tracker.aupdate_step(
                    "prompt_generation", "failure",
                    f"Prompt generation failed: {e}",
                )

        async def _ingest():
            try:
                orchestrator = ProductIngestionOrchestrator()
                # Use source="auto" so the orchestrator picks the best
                # available source (Shopify GraphQL > products.json).
                # Do NOT pass raw_products here — the GraphQL path fetches
                # products with full metafields, collections, and resolved
                # taxonomy references that the REST/JSON format lacks.
                ing_result = await orchestrator.ingest_products(
                    client_id=client_id,
                    source="auto",
                    force_refresh=True,
                    max_products=5000,
                    trace_id=ingestion_run_id,
                )
                logger.info(
                    f"✅ Background ingestion complete for {client_name}: "
                    f"{ing_result.success_count} products run_id={ingestion_run_id}"
                )
                ing_status = (
                    "success" if ing_result.failed_count == 0
                    else "partial_failure" if ing_result.success_count > 0
                    else "failure"
                )
                await tracker.aupdate_step(
                    "product_ingestion", ing_status,
                    f"{ing_result.success_count} products ingested, "
                    f"{ing_result.failed_count} failed.",
                    products_added=ing_result.success_count,
                    products_failed=ing_result.failed_count,
                    duration_seconds=round(time.time() - bg_t0, 2),
                )
            except Exception as e:
                logger.error(
                    f"❌ Background ingestion failed for {client_name} "
                    f"run_id={ingestion_run_id}: {e}",
                    exc_info=True,
                )
                await tracker.aupdate_step(
                    "product_ingestion", "failure",
                    f"Product ingestion failed: {e}",
                )

        # Sequentialize: prompt generation MUST finish before ingestion starts.
        # The extractor prompt and taxonomy config are written to Postgres by
        # _generate_prompts(); ingestion reads them. Running them concurrently
        # caused ingestion to use stale/default prompts, producing mismatched
        # category/subcategory values in the search index.
        await _generate_prompts()
        await _ingest()
        await tracker.aderive_and_complete(time.time() - t0)

    logger.info(
        "🚀 Launching prompt generation → product ingestion (sequential) in background..."
    )
    await ProductSyncLogger.alog_manual_api_event(
        client_id=client_id,
        sync_type="full_ingestion",
        status=ProductSyncLogger.STATUS_STARTED,
        trace_id=ingestion_run_id,
    )
    asyncio.create_task(_run_prompts_and_ingestion())

    result["prompts_generation"] = "started_in_background"
    result["products_ingestion"] = "started_in_background"
    result["ingestion_run_id"] = ingestion_run_id
    result["ingestion_poll_path"] = (
        f"/api/v1/client/onboard/status/{client_id}/{ingestion_run_id}"
    )
    result["steps"]["prompt_generation"] = {
        "status": "running",
        "message": "Generating personalized prompts in background...",
    }
    result["steps"]["product_ingestion"] = {
        "status": "running",
        "message": "Product ingestion started in background.",
        "run_id": ingestion_run_id,
        "mode": "background",
        "source": "auto",
        "force_refresh": True,
        "max_products": 5000,
        "poll_path": result["ingestion_poll_path"],
    }

    # ── Done (synchronous portion) ───────────────────────────────────
    result["success"] = len(result["errors"]) == 0
    if result["errors"]:
        result["status"] = "failed"
        result["message"] = "Client onboarding failed. Check errors for details."
    elif result["warnings"]:
        result["status"] = "in_progress"
        result["message"] = (
            "Synchronous steps completed with warnings. "
            "Background tasks are running."
        )
    else:
        result["status"] = "in_progress"
        result["message"] = (
            "Synchronous steps completed successfully. "
            "Background tasks are running."
        )
    result["duration_seconds"] = round(time.time() - t0, 2)

    logger.info(
        f"🎉 Onboarding sync phase {result['status']} for {client_name}: "
        f"{result['configs_created']} configs, {result['agents_created']} agents, "
        f"prompts + ingestion running in background, "
        f"{len(result['errors'])} errors, {len(result['warnings'])} warnings "
        f"in {result['duration_seconds']}s"
    )
    return result


async def get_onboarding_ingestion_status(client_id: str, run_id: str) -> Dict[str, Any]:
    """
    Return the unified onboarding status for a run.

    Tries the ``onboarding_runs`` table first (unified view). Falls back to
    ``product_sync_logs`` for backward compatibility with runs that predate
    the ``onboarding_runs`` table.
    """
    unified = await get_onboarding_run(client_id, run_id)
    if unified:
        return unified

    # ── Fallback: legacy product_sync_logs-only lookup ──────────────
    latest_event = await ProductSyncLogger.aget_latest_sync_event(
        client_id=client_id,
        sync_type="full_ingestion",
        trace_id=run_id,
        sync_source=ProductSyncLogger.SOURCE_MANUAL_API,
    )

    if not latest_event:
        return {
            "success": False,
            "client_id": client_id,
            "run_id": run_id,
            "status": "unknown",
            "message": "No onboarding status found for this run.",
        }

    raw_status = latest_event.get("status")
    if raw_status == ProductSyncLogger.STATUS_STARTED:
        status = "in_progress"
        success = True
        message = "Product ingestion is still running in the background."
    elif raw_status == ProductSyncLogger.STATUS_SUCCESS:
        status = "completed"
        success = True
        message = "Product ingestion completed successfully."
    elif raw_status == ProductSyncLogger.STATUS_PARTIAL_FAILURE:
        status = "completed_with_warnings"
        success = True
        message = "Product ingestion completed with partial failures."
    else:
        status = "failed"
        success = False
        message = latest_event.get("error_message") or "Product ingestion failed."

    return {
        "success": success,
        "client_id": client_id,
        "run_id": run_id,
        "status": status,
        "message": message,
        "sync_event": latest_event,
    }
