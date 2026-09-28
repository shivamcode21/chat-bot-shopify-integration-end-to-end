"""Agent-only escalation follow-up context.

Escalation is write-only from the bot's point of view: ``alog_escalation`` inserts
a row with ``status='unresolved'`` and a human later flips it to ``resolved`` from
the ops dashboard. The bot never reads any of it back, so a customer chasing a
pending hand-off is answered as if nothing was ever escalated.

This module closes the loop with a single cached read. The ``escalations`` table is
already the cross-service event log — the bot writes the "opened" transition and
apiandui writes the "closed" one — so both are available by reading it, with no
producer changes on either side.

The result is rendered into ``generic_skill_node``'s ``system_blocks`` as an
internal block: visible to the agent, never to the customer, never stored as a
conversation event.

See ``design_docs/ESCALATION_FOLLOW_UP_CONTEXT.md``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fashion_bot.env_loader import get_bool, get_int
from fashion_bot.utils.phone_number_utils import get_last_n_digits, phone_match_variants
from fashion_bot.utils.tiered_cache import (
    aget_with_tiered_cache,
    ainvalidate_tiered_cache_key,
)

logger = logging.getLogger(__name__)

CACHE_PREFIX = "esc_ctx"
HUMAN_REPLY_CACHE_PREFIX = "esc_hr"
_MAX_HUMAN_REPLIES = 3

_TTL_SECONDS = get_int("ESCALATION_CONTEXT_TTL_SECONDS", 60)
# "This customer has no escalations" is the answer on ~95% of turns, and unlike a
# positive result nothing outside this process can invalidate it: it can only stop
# being true when an escalation is inserted, and every insert path here busts the
# key (``abust_escalation_snapshot``). So it is cached far longer, which keeps the
# common case off the database entirely. Bounded, not infinite, because apiandui
# exposes a POST /escalations that could in principle insert out-of-band.
_EMPTY_TTL_SECONDS = get_int("ESCALATION_CONTEXT_EMPTY_TTL_SECONDS", 900)
# The memory tier is PROCESS-LOCAL, so a bust on one pod cannot reach another —
# ``ainvalidate_tiered_cache_key`` clears Redis globally but only this process's
# memory. AGENTS.md §3 states the model plainly: "bust Redis, then local cache
# expires via TTL". That is fine for client configs on a 10-minute TTL; it is not
# fine here, where a stale local "[]" would hide an escalation the bot just raised
# and silently no-op the whole feature for any turn landing on another pod.
#
# So the long TTLs live in REDIS, where the bust is global, and the memory tier is
# kept to a few seconds — just enough to collapse the 2-3 reads within a single
# turn (prefetch, then follow-up lineage). Worst-case cross-pod staleness after a
# bust is therefore this value, not 900s.
_MEMORY_TTL_SECONDS = get_int("ESCALATION_CONTEXT_MEMORY_TTL_SECONDS", 10)
_LOOKBACK_DAYS = get_int("ESCALATION_CONTEXT_LOOKBACK_DAYS", 14)
_MAX_THREADS = get_int("ESCALATION_CONTEXT_MAX_THREADS", 3)
_ROW_LIMIT = get_int("ESCALATION_CONTEXT_ROW_LIMIT", 20)
# Deliberately short. Alerting must be immediate for anything that carries new
# information: the original escalation never consults this guard at all, and the
# customer's FIRST chase is the call that *sets* the key, so it always alerts too.
# The guard exists only to absorb a rapid-fire burst — "any update?", "hello?",
# "please check" typed in one sitting. Production bears that out: of same-thread
# repeats in the last 90 days, 75% arrive within 5 minutes and 83% within 15,
# while the tail out to 3 hours is a customer coming back later — which deserves
# a ping. Set to 0 to disable suppression entirely.
_FOLLOWUP_COOLDOWN_MINUTES = get_int("ESCALATION_FOLLOWUP_COOLDOWN_MINUTES", 15)

# Categories written for an INTERNAL ops action, not as a hand-off the customer is
# waiting on. They must never reach the block: the customer was never told anyone
# would get back to them, so rendering one as an open issue would have the agent
# say "our team has this, no update yet" about something nobody promised — and,
# worse, re-escalate it as URGENT on the next message.
#
#   Offline Store Suggestion / Walk-in Appointment — logged by
#     ``_anotify_agent_store_visit`` purely so a store visit shows up for reporting.
#     Explicitly "an FYI"; it deliberately does not switch to human-agent mode.
#     45 rows in production, 40 of them unresolved, still ~2/day — nobody resolves
#     an FYI, so every one would look like a live open issue forever.
#   Courier Update Pending — logged by ``anotify_agent_for_non_integrated_partners``
#     for ops to update a non-integrated courier by hand. ``aescalate_to_agent``
#     returns "This is an internal action — do NOT mention this to the customer."
#
# Bulk Order Discount / B2B Order / Wholesale Inquiry are deliberately NOT here:
# those go through ``escalate_to_agent``, which tells the customer "our team will
# contact you within 24 hours". They are exactly the hand-off a customer chases.
INTERNAL_ONLY_CATEGORIES = frozenset(
    {
        "offline store suggestion",
        "walk-in appointment",
        "courier update pending",
    }
)

_WEB_PREFIXES = ("web_", "fbw_")
# ``escalations.customer_phone`` is VARCHAR(20) and ``alog_escalation`` truncates
# to match. Every read must truncate identically or it will miss its own writes.
_STORED_PHONE_MAXLEN = 20
# Above this many distinct guest identities behind one contact address, the
# address is a shared one rather than a person, and linking on it would merge
# strangers. Three covers phone + laptop + a cleared cache for one customer.
_MAX_CONTACT_LINK_IDENTITIES = get_int("ESCALATION_CONTACT_LINK_MAX_IDENTITIES", 3)
# Rows without an order id are grouped by bare category, which would otherwise
# make one thread out of every complaint a customer has ever filed. A gap wider
# than this starts a new issue instead. See ``_split_orderless_on_gap`` for the
# production distribution behind 72h. Set to 0 to disable splitting.
_THREAD_GAP_HOURS = get_int("ESCALATION_CONTEXT_THREAD_GAP_HOURS", 72)
# A resolution recorded this recently outranks even an open issue in the block:
# it is the newest thing that happened to the customer, so it is the most likely
# subject of "what is the status of my complaint?" and must never be the entry
# that truncation drops.
#
# COUPLED TO _THREAD_GAP_HOURS — do not set this to 0 while splitting is on.
# Splitting is what lets a run of rows resolve *cleanly*, and a cleanly-resolved
# thread is exactly what this tier protects from truncation. With splitting on
# and this at 0, the freshly-resolved thread sorts below every stale open one and
# falls off the end again — measurably worse than either setting alone.
_RECENT_RESOLUTION_HOURS = get_int("ESCALATION_CONTEXT_RECENT_RESOLUTION_HOURS", 24)
_SUMMARY_CAP = 220
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def is_escalation_context_enabled() -> bool:
    """Global kill switch. Off ⇒ no read, no block, byte-for-byte prior behaviour.

    Env-backed, so it is fixed for the life of the process (``env_loader`` caches
    the bootstrap snapshot) — flipping it needs a restart. For a per-tenant switch
    that takes effect without one, use the ``escalation_context_enabled``
    client_config key, which overrides this (see ``ais_enabled_for_client``).
    """
    return get_bool("ESCALATION_CONTEXT_ENABLED", True)


def _coerce_flag(raw: Any) -> Optional[bool]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if not text:
        return None
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return None


async def ais_enabled_for_client(client_id: Optional[str]) -> bool:
    """Per-tenant opt-out, checked only when the env switch is on.

    ``ESCALATION_CONTEXT_ENABLED=false`` is a **hard** kill switch: it returns
    here before any I/O, so the feature performs no config read, no snapshot
    read, and no Redis call. That is what makes "off ⇒ byte-for-byte prior
    behaviour" literally true rather than approximately true.

    With the env switch on, an explicit ``escalation_context_enabled=false`` in
    ``client_configs`` disables one noisy tenant without a redeploy. Read through
    the tiered cache (AGENTS.md §3); any failure falls back to enabled rather
    than blocking the turn.
    """
    if not is_escalation_context_enabled():
        return False
    if not client_id:
        return True
    try:
        from fashion_bot.config_manager import aget_config

        override = _coerce_flag(await aget_config("escalation_context_enabled", client_id=client_id))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("[ESCALATION_CTX] client flag read failed, staying enabled: %s", e)
        return True
    return True if override is None else override


# ──────────────────────────────────────────────────────────────────────────
# DTOs
# ──────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EscalationRecord:
    """One row of ``escalations``, normalised for context rendering."""

    escalation_id: str
    category: str
    status: str  # 'unresolved' | 'resolved'
    raised_at: Optional[datetime]
    resolved_at: Optional[datetime]
    resolution_text: Optional[str]
    reason: Optional[str]
    order_id: Optional[str]
    conversation_id: Optional[str] = None
    # Reachable contact the customer gave when the channel had none of its own
    # (web chat). Never rendered to the customer — it is their own address, and
    # repeating it back adds nothing.
    customer_contact: Optional[str] = None
    # The ops dashboard has TWO ways to record that a human acted, and only one
    # of them touches `status`. "Final Resolution" sets status/resolved_date/
    # resolution_text; "Add Progress Update" writes a free-text entry into
    # `comments` and may set `resolution_status` (e.g. 'in_progress') while the
    # row stays `unresolved`. Reading only the first pair made the block say
    # "no human update recorded yet" about an escalation where a teammate had
    # noted, three minutes after it was raised, that the product ships at 7pm.
    progress_note: Optional[str] = None
    progress_note_at: Optional[datetime] = None
    resolution_status: Optional[str] = None

    @property
    def is_unresolved(self) -> bool:
        return self.status == "unresolved"


@dataclass(frozen=True)
class HumanReply:
    """A message a human teammate sent the customer after an issue was raised."""

    sent_at: Optional[datetime]
    text: str


@dataclass(frozen=True)
class EscalationContextBundle:
    """Everything one turn needs, fetched together in the prefetch."""

    records: List[EscalationRecord]
    human_replies: List[HumanReply]

    @property
    def has_unresolved(self) -> bool:
        return any(r.is_unresolved for r in self.records)


@dataclass(frozen=True)
class EscalationThread:
    """One *issue*, built from every row that refers to it.

    A single late order is routinely logged as ``Delivery Query``, then
    ``Order Delivery Delayed``, then ``Frustration`` as the LLM's category choice
    drifts turn to turn. Those are one problem to the customer, so they must be
    one entry to the agent — otherwise "re-escalate under the same category" has
    no well-defined meaning and the block shows three entries for one issue.
    """

    thread_key: str
    root_escalation_id: str  # earliest member — the ticket a chase is chasing
    category: str  # latest member's category
    status: str  # unresolved if ANY member is unresolved
    order_id: Optional[str]
    raised_at: Optional[datetime]  # earliest member: the customer's real wait
    resolved_at: Optional[datetime]
    resolution_text: Optional[str]
    reason: Optional[str]
    chase_count: int  # members beyond the first
    customer_contact: Optional[str] = None  # latest reachable contact on the thread
    # Set only when the thread is still open but SOME member was resolved. A
    # recorded human action is information the agent must have even when other
    # reports in the thread remain open — without it the bot contradicts a
    # teammate who has already closed the customer's issue.
    partial_resolved_at: Optional[datetime] = None
    partial_resolution_text: Optional[str] = None
    open_member_count: int = 0
    # Newest "Add Progress Update" note across the thread's rows. A teammate
    # saying "dispatching at 7pm" is the update a chasing customer is asking for,
    # even though the row is still formally unresolved.
    progress_note: Optional[str] = None
    progress_note_at: Optional[datetime] = None

    @property
    def is_unresolved(self) -> bool:
        return self.status == "unresolved"

    @property
    def has_progress_note(self) -> bool:
        """Open thread carrying a note a teammate posted from the dashboard."""
        return bool(self.is_unresolved and str(self.progress_note or "").strip())

    @property
    def has_partial_resolution(self) -> bool:
        """Open thread carrying an action a human already recorded on part of it."""
        return bool(
            self.is_unresolved
            and self.partial_resolved_at
            and str(self.partial_resolution_text or "").strip()
        )

    def hours_waiting(self, now: Optional[datetime] = None) -> Optional[float]:
        if not self.raised_at:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (now - self.raised_at).total_seconds() / 3600.0)


# ──────────────────────────────────────────────────────────────────────────
# Identity helpers
# ──────────────────────────────────────────────────────────────────────────


def _slug(value: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def escalation_identity(phone_number: Optional[str]) -> Optional[str]:
    """Stable per-customer identity used for the cache key and cooldown key."""
    raw = str(phone_number or "").strip()
    if not raw:
        return None
    if raw.startswith(_WEB_PREFIXES):
        return raw[:_STORED_PHONE_MAXLEN]
    return get_last_n_digits(raw, 10) or None


# phone_match_variants is now centralized in phone_number_utils.py and
# imported at the top of this module. The re-export keeps existing callers
# and tests working without import changes.


# ──────────────────────────────────────────────────────────────────────────
# Read (tiered cache: memory → redis → postgres)
# ──────────────────────────────────────────────────────────────────────────


def _cache_key(client_id: str, identity: str) -> str:
    return f"{CACHE_PREFIX}:{client_id}:{identity}"


def _scope_hash(conversation_ids: List[str], since: Optional[datetime]) -> str:
    """Short digest of the query scope, so a changed scope is a different entry."""
    raw = "|".join(sorted(conversation_ids)) + "@" + (since.isoformat() if since else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


async def _redis_client():
    from fashion_bot.utils.redis_client import get_shared_async_redis_client

    return await get_shared_async_redis_client()


async def _redis_get(key: str) -> Optional[str]:
    client = await _redis_client()
    if not client:
        return None
    raw = await client.get(key)
    return raw if raw else None


async def _redis_set(key: str, value: Optional[str], ttl: int) -> None:
    if not value:
        return
    client = await _redis_client()
    if client:
        await client.setex(key, ttl, value)


def _redis_ttl_for(value: Optional[str]) -> int:
    """Long TTL for "no escalations", short for a live one.

    An empty answer can only stop being true when an escalation is inserted, and
    every insert path busts the key — so nothing outside this process can make it
    stale. A positive answer can be flipped by a human clicking Resolve at any
    moment, so it stays fresh. Both live in Redis, where the bust is global.
    """
    return _EMPTY_TTL_SECONDS if str(value or "").strip() in ("[]", "") else _TTL_SECONDS


def _as_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def _load_rows_json(client_id: str, variants: List[str]) -> str:
    """Fetch recent escalations for this customer as a JSON string.

    Returns ``"[]"`` (never ``None``) so the tiered cache stores the common
    no-escalations answer instead of hitting the DB on every turn.
    """
    from fashion_bot.database_manager import get_async_postgres_connection

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT escalation_id, category, status, reason, escalation_date,
                       resolved_date, resolution_text, escalation_metadata,
                       conversation_id, resolution_status, comments
                FROM escalations
                WHERE client_id = %s::uuid
                  AND customer_phone = ANY(%s)
                  AND escalation_date > NOW() - make_interval(days => %s)
                  AND LOWER(COALESCE(category, '')) <> ALL(%s)
                ORDER BY (status = 'unresolved') DESC, escalation_date DESC
                LIMIT %s
                """,
                (
                    str(client_id),
                    variants,
                    _LOOKBACK_DAYS,
                    # Filtered in SQL, not after the fetch, so internal rows cannot
                    # eat the LIMIT and push a real open issue out of the window.
                    sorted(INTERNAL_ONLY_CATEGORIES),
                    _ROW_LIMIT,
                ),
            )
            rows = await cur.fetchall()

    payload: List[Dict[str, Any]] = []
    for row in rows or []:
        row = dict(row) if not isinstance(row, dict) else row
        meta = row.get("escalation_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except (TypeError, ValueError):
                meta = {}
        raised = _as_utc(row.get("escalation_date"))
        resolved = _as_utc(row.get("resolved_date"))
        note_text, note_at = _latest_comment(row.get("comments"))
        payload.append(
            {
                "escalation_id": str(row.get("escalation_id")),
                "category": row.get("category") or "General",
                "status": row.get("status") or "unresolved",
                "reason": row.get("reason"),
                "raised_at": raised.isoformat() if raised else None,
                "resolved_at": resolved.isoformat() if resolved else None,
                "resolution_text": row.get("resolution_text"),
                "order_id": (meta or {}).get("order_id"),
                "customer_contact": (meta or {}).get("customer_contact"),
                "conversation_id": (
                    str(row["conversation_id"]) if row.get("conversation_id") else None
                ),
                "resolution_status": row.get("resolution_status"),
                "progress_note": note_text,
                "progress_note_at": note_at.isoformat() if note_at else None,
            }
        )
    return json.dumps(payload)


def _latest_comment(raw: Any) -> tuple:
    """Newest ``comments[]`` entry as ``(text, created_at)``.

    The dashboard's "Add Progress Update" writes here. Only the text and its
    timestamp are taken: ``user_email`` identifies a staff member by name and the
    block forbids revealing those, so it is deliberately dropped at the source
    rather than relied on being filtered downstream.
    """
    entries = raw
    if isinstance(entries, str):
        try:
            entries = json.loads(entries)
        except (TypeError, ValueError):
            return None, None
    if not isinstance(entries, list) or not entries:
        return None, None

    best_text, best_at = None, None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        created = _as_utc(entry.get("created_at"))
        # No timestamp still counts — a note without one is better than silence,
        # and list order approximates recency.
        if best_at is None or (created and created >= best_at):
            best_text, best_at = text, created or best_at
    return best_text, best_at


def _records_from_json(raw: Optional[str]) -> List[EscalationRecord]:
    from fashion_bot.agent_config import normalize_escalation_category

    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    records: List[EscalationRecord] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        # Belt and braces for payloads cached before the SQL filter existed. Must
        # be checked on the RAW value: none of these are in the tool enum, so
        # ``normalize_escalation_category`` maps them to "General" and the name is
        # gone by the next line — which is also why they were previously rendering
        # as an anonymous "General" open issue rather than anything recognisable.
        if str(row.get("category") or "").strip().lower() in INTERNAL_ONLY_CATEGORIES:
            continue
        order_id = row.get("order_id")
        records.append(
            EscalationRecord(
                escalation_id=str(row.get("escalation_id") or ""),
                category=normalize_escalation_category(row.get("category")),
                status=row.get("status") or "unresolved",
                raised_at=_as_utc(row.get("raised_at")),
                resolved_at=_as_utc(row.get("resolved_at")),
                resolution_text=row.get("resolution_text"),
                reason=row.get("reason"),
                order_id=str(order_id) if order_id else None,
                # ``.get`` — a payload cached before these fields existed must not
                # blow up on read.
                conversation_id=row.get("conversation_id"),
                customer_contact=row.get("customer_contact"),
                resolution_status=row.get("resolution_status"),
                progress_note=row.get("progress_note"),
                progress_note_at=_as_utc(row.get("progress_note_at")),
            )
        )
    return records


async def aget_escalation_records(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
) -> List[EscalationRecord]:
    """Recent escalations for one customer. Never raises; ``[]`` on any failure."""
    if not is_escalation_context_enabled():
        return []
    identity = escalation_identity(phone_number)
    if not identity:
        return []
    if not client_id:
        # A missing client_id is an error, not a default (AGENTS.md §7) — an
        # unscoped read here would be a cross-tenant leak.
        from fashion_bot.rollbar_config import report_error

        report_error(
            "escalation_context: missing client_id, skipping snapshot",
            level="error",
            trace_id=trace_id,
        )
        return []

    if not await ais_enabled_for_client(str(client_id)):
        return []

    variants = phone_match_variants(phone_number)
    if not variants:
        return []

    key = _cache_key(str(client_id), identity)
    try:
        raw, tier = await aget_with_tiered_cache(
            cache_key=key,
            # NOTE: this argument governs the MEMORY tier only — the Redis TTL is
            # chosen per value by set_to_redis_fn below. Keeping memory short is
            # what makes a cross-pod bust effective (see _MEMORY_TTL_SECONDS).
            ttl_seconds=_MEMORY_TTL_SECONDS,
            load_from_source_fn=lambda: _load_rows_json(str(client_id), variants),
            get_from_redis_fn=lambda: _redis_get(key),
            set_to_redis_fn=lambda v: _redis_set(key, v, _redis_ttl_for(v)),
        )
    except Exception as e:
        logger.warning(
            "[ESCALATION_CTX] snapshot read failed: %s",
            e,
            extra={"trace_id": trace_id, "client_id": client_id},
        )
        return []

    records = _records_from_json(raw)
    if records:
        logger.info(
            "[ESCALATION_CTX] %d record(s) (%d unresolved) from %s",
            len(records),
            sum(1 for r in records if r.is_unresolved),
            tier,
            extra={"trace_id": trace_id, "client_id": client_id},
        )
    return records


async def _load_human_replies_json(
    client_id: str, conversation_ids: List[str], since: Optional[datetime]
) -> str:
    """Messages a human teammate sent this customer after the issue was raised.

    Indexed by ``ix_message_conversation_created`` — the escalation row carries the
    conversation it was raised in, so this is an index scan (~0.1ms), not a phone
    scan (``messages.phone`` has no index).

    ``created_by`` is ``'support'`` for dashboard replies and a user UUID for some
    surfaces; ``'bot'``/``'user'``/``'system'`` are excluded.
    """
    from fashion_bot.database_manager import get_async_postgres_connection

    async with get_async_postgres_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT message, created_at
                FROM messages
                WHERE client_id = %s::uuid
                  AND conversation_id = ANY(%s::uuid[])
                  AND created_at > %s
                  AND (created_by = 'support' OR created_by ~ '^[0-9a-f]{8}-')
                  AND message IS NOT NULL AND TRIM(message) <> ''
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (str(client_id), conversation_ids, since, _MAX_HUMAN_REPLIES),
            )
            rows = await cur.fetchall()

    payload = []
    for row in rows or []:
        row = dict(row) if not isinstance(row, dict) else row
        sent = _as_utc(row.get("created_at"))
        payload.append(
            {"sent_at": sent.isoformat() if sent else None, "text": row.get("message") or ""}
        )
    payload.reverse()  # oldest first reads naturally in the block
    return json.dumps(payload)


async def aget_human_replies(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    conversation_ids: List[str],
    since: Optional[datetime],
    trace_id: Optional[str] = None,
) -> List[HumanReply]:
    """Human teammate replies since an issue was raised. ``[]`` on any failure.

    Kept on the short TTL: a teammate can reply at any moment, and a stale "no
    replies" would put the agent back to claiming there is no update.
    """
    identity = escalation_identity(phone_number)
    if not client_id or not identity or not conversation_ids:
        return []
    # The query scope — which conversations, and from when — is part of the cache
    # identity. Without it, raising a NEW escalation (new conversation, later
    # ``since``) would reuse replies fetched under the PREVIOUS issue's scope for
    # the rest of the TTL, attributing an old teammate answer to a new issue.
    key = f"{HUMAN_REPLY_CACHE_PREFIX}:{client_id}:{identity}:{_scope_hash(conversation_ids, since)}"
    try:
        raw, _ = await aget_with_tiered_cache(
            cache_key=key,
            ttl_seconds=_MEMORY_TTL_SECONDS,  # memory tier only — see above
            load_from_source_fn=lambda: _load_human_replies_json(
                str(client_id), conversation_ids, since
            ),
            get_from_redis_fn=lambda: _redis_get(key),
            set_to_redis_fn=lambda v: _redis_set(key, v, _TTL_SECONDS),
        )
    except Exception as e:
        logger.warning(
            "[ESCALATION_CTX] human-reply read failed: %s",
            e,
            extra={"trace_id": trace_id, "client_id": client_id},
        )
        return []
    try:
        rows = json.loads(raw) if raw else []
    except (TypeError, ValueError):
        return []
    return [
        HumanReply(sent_at=_as_utc(r.get("sent_at")), text=str(r.get("text") or ""))
        for r in rows
        if isinstance(r, dict) and str(r.get("text") or "").strip()
    ]


async def aprefetch_escalation_context(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
    user_message: Optional[str] = None,
) -> EscalationContextBundle:
    """Everything the block needs, fetched inside ONE prefetch task.

    The human-reply lookup only runs when there is an unresolved escalation — the
    only situation where it changes what the agent says — so the ~95% of turns
    with nothing open pay for it not at all. When it does run, it is a second
    round trip inside the same overlapped window, not a second serial wait.

    When ``user_message`` contains an email and the identity is a web guest,
    attempt to link escalations from another device before fetching records, so
    the context block is correct on the customer's very first message.
    """
    # Email-based identity linking for web guests (design doc §11).  Runs here
    # rather than in websocket_chat so the delivery-layer file is untouched and
    # the link completes before the records are read.
    _phone = str(phone_number or "").strip()
    if user_message and _phone.startswith(_WEB_PREFIXES):
        try:
            from fashion_bot.utils.utils import extract_email_candidate

            email = extract_email_candidate(user_message)
            if email:
                await alink_escalations_by_contact(
                    client_id=client_id,
                    contact=email,
                    identity=_phone,
                    trace_id=trace_id,
                )
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug("[ESCALATION_CTX] email link in prefetch skipped: %s", e)

    records = await aprefetch_escalation_records(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    replies: List[HumanReply] = []
    unresolved = [r for r in records if r.is_unresolved]
    if unresolved:
        conv_ids = sorted({r.conversation_id for r in unresolved if r.conversation_id})
        since = min(
            (r.raised_at for r in unresolved if r.raised_at), default=None
        )
        if conv_ids and since:
            try:
                replies = await aget_human_replies(
                    client_id=client_id,
                    phone_number=phone_number,
                    conversation_ids=conv_ids,
                    since=since,
                    trace_id=trace_id,
                )
            except Exception as e:  # pragma: no cover - belt and braces
                logger.debug("[ESCALATION_CTX] human-reply prefetch swallowed: %s", e)
    return EscalationContextBundle(records=records, human_replies=replies)


async def aprefetch_escalation_records(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
) -> List[EscalationRecord]:
    """``aget_escalation_records`` hardened for ``asyncio.create_task`` prefetch.

    The node starts this the moment it knows the client and phone, then awaits it
    only when it assembles the prompt — by which time tool loading, LLM resolution
    and the store-context lookup have all been awaited in between, so the read
    overlaps work already in flight and costs ~0 wall clock (AGENTS.md §1:
    ``asyncio.gather``-style concurrency for independent I/O).

    Because the task may be abandoned if the node returns early, this must never
    raise — an unretrieved task exception would surface as a noisy asyncio warning.
    """
    try:
        return await aget_escalation_records(
            client_id=client_id, phone_number=phone_number, trace_id=trace_id
        )
    except Exception as e:  # pragma: no cover - belt and braces
        logger.debug("[ESCALATION_CTX] prefetch swallowed: %s", e)
        return []


async def abust_escalation_snapshot(
    client_id: Optional[str], phone_number: Optional[str]
) -> None:
    """Drop the cached snapshot so a just-raised escalation is visible next turn."""
    identity = escalation_identity(phone_number)
    if not client_id or not identity:
        return
    try:
        await ainvalidate_tiered_cache_key(_cache_key(str(client_id), identity))
    except Exception as e:  # pragma: no cover - defensive, never blocks a turn
        logger.debug("[ESCALATION_CTX] cache bust skipped: %s", e)


# ──────────────────────────────────────────────────────────────────────────
# Issue threading
# ──────────────────────────────────────────────────────────────────────────


def thread_key_for(order_id: Optional[str], category: Optional[str]) -> str:
    """Group key for one issue: the order when known, else the category."""
    if order_id and str(order_id).strip():
        return f"order:{str(order_id).strip().lower()}"
    return f"cat:{_slug(category) or 'general'}"


def _newest_note(members: List[EscalationRecord]) -> tuple:
    """Latest dashboard progress note across a thread's rows."""
    noted = [m for m in members if str(m.progress_note or "").strip()]
    if not noted:
        return None, None
    latest = max(noted, key=lambda m: m.progress_note_at or _EPOCH)
    return latest.progress_note, latest.progress_note_at


def _base_thread_key(thread_key: str) -> str:
    """The group key a thread came from, without any gap-split suffix."""
    return str(thread_key or "").split("#", 1)[0]


def _split_orderless_on_gap(
    key: str, members: List[EscalationRecord]
) -> List[tuple]:
    """Break an order-less group wherever a multi-day gap separates two rows.

    An order id names one real issue, so rows sharing one belong together however
    far apart they are. A bare category does not: ``cat:frustration`` collects
    *every* complaint the customer has made in the window, and merging them makes
    a thread that can never read resolved — one still-open row from last week
    suppresses the resolution recorded on this week's, which is exactly how a
    customer whose issue was closed got told it had been re-escalated instead.

    A real issue is a burst. Of consecutive same-category order-less pairs in the
    last 90 days, **89% arrive within an hour** of each other and the median gap
    is 36 seconds — that is the category drifting turn to turn on one complaint.
    The default cut of 72 hours keeps 99% of those pairs together, and comfortably
    spans the two-day follow-up this feature exists to serve, while separating
    complaints raised a week apart into the distinct issues they are.

    Order-bearing groups are returned untouched.
    """
    if key.startswith("order:") or _THREAD_GAP_HOURS <= 0 or len(members) < 2:
        return [(key, members)]

    ordered = sorted(members, key=lambda r: (r.raised_at or _EPOCH))
    runs: List[List[EscalationRecord]] = [[ordered[0]]]
    for previous, current in zip(ordered, ordered[1:]):
        gap_hours = (
            (current.raised_at - previous.raised_at).total_seconds() / 3600.0
            if current.raised_at and previous.raised_at
            else 0.0
        )
        if gap_hours > _THREAD_GAP_HOURS:
            runs.append([current])
        else:
            runs[-1].append(current)

    if len(runs) == 1:
        return [(key, ordered)]
    # Suffix keeps each run's key stable and distinct, so thread-keyed state —
    # the alert cooldown especially — cannot collide across two separate issues.
    return [(f"{key}#{run[0].escalation_id}", run) for run in runs]


def build_threads(
    records: List[EscalationRecord], *, now: Optional[datetime] = None
) -> List[EscalationThread]:
    """Collapse rows into issue threads, ranked by what the customer likely means.

    ``now`` is injectable so the recency tiers below are testable without
    freezing the module clock; it defaults to the current time.
    """
    by_key: Dict[str, List[EscalationRecord]] = {}
    for rec in records:
        by_key.setdefault(thread_key_for(rec.order_id, rec.category), []).append(rec)

    grouped: Dict[str, List[EscalationRecord]] = {}
    for key, members in by_key.items():
        for split_key, split_members in _split_orderless_on_gap(key, members):
            grouped.setdefault(split_key, []).extend(split_members)

    threads: List[EscalationThread] = []
    for key, members in grouped.items():
        # Oldest first: index 0 is the root, the last is the most recent.
        ordered = sorted(members, key=lambda r: (r.raised_at or _EPOCH))
        root, latest = ordered[0], ordered[-1]
        unresolved = any(m.is_unresolved for m in ordered)
        resolved_members = [m for m in ordered if m.resolved_at]
        newest_resolved = (
            sorted(resolved_members, key=lambda r: r.resolved_at)[-1]
            if resolved_members
            else None
        )
        threads.append(
            EscalationThread(
                thread_key=key,
                root_escalation_id=root.escalation_id,
                category=latest.category,
                status="unresolved" if unresolved else "resolved",
                order_id=next((m.order_id for m in ordered if m.order_id), None),
                raised_at=root.raised_at,
                resolved_at=None if unresolved else (newest_resolved.resolved_at if newest_resolved else None),
                resolution_text=(
                    None
                    if unresolved
                    else next(
                        (
                            m.resolution_text
                            for m in reversed(ordered)
                            if m.resolution_text and m.resolution_text.strip()
                        ),
                        None,
                    )
                ),
                # An action a human RECORDED is never discarded, even while the
                # thread stays open. Suppressing it is how a customer whose issue
                # was closed at 16:41 got told at 17:06 that it had been
                # re-escalated: two rows carried "Talked to the customer. He is
                # satisfied now.", seven older rows in the same thread were still
                # open, and ``None if unresolved`` threw the resolution away.
                partial_resolved_at=(
                    newest_resolved.resolved_at if (unresolved and newest_resolved) else None
                ),
                partial_resolution_text=(
                    next(
                        (
                            m.resolution_text
                            for m in reversed(ordered)
                            if m.resolution_text and m.resolution_text.strip()
                        ),
                        None,
                    )
                    if unresolved
                    else None
                ),
                open_member_count=sum(1 for m in ordered if m.is_unresolved),
                progress_note=_newest_note(ordered)[0],
                progress_note_at=_newest_note(ordered)[1],
                reason=next((m.reason for m in ordered if m.reason and m.reason.strip()), None),
                chase_count=len(ordered) - 1,
                # Latest wins — a customer who corrects their address mid-thread
                # should not have the stale one carried forward.
                customer_contact=next(
                    (
                        m.customer_contact
                        for m in reversed(ordered)
                        if m.customer_contact and str(m.customer_contact).strip()
                    ),
                    None,
                ),
            )
        )

    def _sort_key(t: EscalationThread) -> tuple:
        # Three tiers, because only ranking unresolved-first hid the very thing a
        # "what's the status of my complaint?" question is about: a support agent
        # closed this customer's issue at 16:41, and at 17:06 the freshly-resolved
        # thread sorted behind three eight-day-old open ones and fell off the end
        # of _MAX_THREADS. The bot then apologised for a delay and re-escalated.
        #
        #   0  resolved in the last _RECENT_RESOLUTION_HOURS, newest first — the
        #      most recent thing that happened to this customer, and the most
        #      likely subject of a status question. Never truncated away.
        #   1  unresolved, oldest first — the longest-waiting issue is the one
        #      being chased, and it must outrank stale resolved history.
        #   2  older resolved, newest first.
        stamp = (t.raised_at or _EPOCH).timestamp()
        # "Most recent team action" covers a resolution AND a progress note: both
        # are a human doing something the customer is about to ask about, and an
        # open issue that a teammate updated ten minutes ago is at least as likely
        # to be the subject as one nobody has touched in a week.
        acted_at = t.resolved_at or t.progress_note_at
        if acted_at is not None:
            age_hours = (now_ref - acted_at).total_seconds() / 3600.0
            if age_hours <= _RECENT_RESOLUTION_HOURS:
                return (0, -acted_at.timestamp())
        if t.is_unresolved:
            return (1, stamp)
        return (2, -stamp)

    now_ref = now or datetime.now(timezone.utc)
    threads.sort(key=_sort_key)
    return threads


def find_matching_thread(
    threads: List[EscalationThread],
    *,
    order_id: Optional[str] = None,
    category: Optional[str] = None,
) -> Optional[EscalationThread]:
    """The open thread an escalation for ``(order_id, category)`` belongs to.

    Order match wins over category match — 62% of multi-escalation windows are one
    order wearing several category labels, so matching on category alone would
    treat a re-labelled chase as a brand-new issue.
    """
    open_threads = [t for t in threads if t.is_unresolved]
    if not open_threads:
        return None
    if order_id and str(order_id).strip():
        key = thread_key_for(order_id, None)
        for thread in open_threads:
            if thread.thread_key == key:
                return thread
        return None
    # Compare on the BASE key. An order-less category can be split into several
    # issues by ``_split_orderless_on_gap``, and those carry a "#<root id>"
    # suffix — an equality test against the unsuffixed key would match none of
    # them, silently dropping the lineage and the alert cooldown on every
    # order-less follow-up.
    key = thread_key_for(None, category)
    matches = [t for t in open_threads if _base_thread_key(t.thread_key) == key]
    if not matches:
        return None
    # The newest open run: a customer chasing "frustration" means the complaint
    # they raised this week, not the one from a fortnight ago.
    return max(matches, key=lambda t: t.raised_at or _EPOCH)


# ──────────────────────────────────────────────────────────────────────────
# Rendering (pure)
# ──────────────────────────────────────────────────────────────────────────


def _ist(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown time"
    try:
        import pytz

        return dt.astimezone(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
    except Exception:  # pragma: no cover - defensive
        return dt.strftime("%Y-%m-%d %H:%M UTC")


def _one_line(text: Optional[str], cap: int = _SUMMARY_CAP) -> str:
    flat = " ".join(str(text or "").split())
    return flat[: cap - 1] + "…" if len(flat) > cap else flat


def _age_phrase(thread: EscalationThread, now: Optional[datetime]) -> str:
    hours = thread.hours_waiting(now)
    if hours is None:
        return ""
    if hours < 1:
        return " (under an hour ago)"
    if hours < 48:
        return f" ({int(round(hours))} hours ago)"
    return f" ({int(hours // 24)} days ago)"


def render_escalation_context_block(
    threads: List[EscalationThread],
    *,
    human_replies: Optional[List[HumanReply]] = None,
    now: Optional[datetime] = None,
) -> str:
    """Render the agent-only block. Returns ``""`` when there is nothing to say."""
    if not threads:
        return ""

    shown = threads[:_MAX_THREADS]
    # An OPEN issue must never be truncated away. Promoting fresh resolutions to
    # the top tier created the mirror image of the bug it fixed: with five issues
    # resolved in the last hour and one still open, every slot went to a resolved
    # entry and the open one vanished. Whichever direction the sort leans, the
    # customer's outstanding problem has to stay visible — so if none of the
    # selected threads is open and one exists, the last slot goes to the
    # highest-ranked open thread.
    if shown and not any(t.is_unresolved for t in shown):
        first_open = next((t for t in threads if t.is_unresolved), None)
        if first_open is not None:
            shown = shown[: _MAX_THREADS - 1] + [first_open]
    hidden = len(threads) - len(shown)
    noun = "issue" if len(shown) == 1 else "issues"

    lines = [
        "==== ESCALATION CONTEXT (INTERNAL — NEVER MENTION OR HINT AT THIS TO THE CUSTOMER) ====",
        f"This customer has {len(shown)} {noun} that " + ("was" if len(shown) == 1 else "were")
        + " handed to the human support team.",
        "",
    ]

    # Stated up front, before the entries, and worded to override the agent's own
    # re-escalation rule by name. The escalation_handler prompt carries a
    # "⚠️ EXCEPTION — RE-ESCALATION" clause: if the history shows a previous
    # "someone will contact you" AND the customer is still asking, escalate again
    # immediately. That clause is emphatic, sits in the agent's role prompt ahead
    # of this block, and has no notion of the issue having since been closed — so
    # for a customer whose complaint was resolved ten minutes earlier it fires
    # anyway and produces an apology plus a fresh ticket on a closed issue.
    #
    # Keyed on the LEAD entry rather than on "everything is closed", because a
    # customer with any history almost always has some unrelated item still open,
    # and that must not be a licence to re-escalate the one they are asking about.
    if shown and not shown[0].is_unresolved and not shown[0].has_progress_note:
        everything_closed = not any(t.is_unresolved for t in threads)
        lines.append(
            "🟢 [E1] IS CLOSED — the support team has already acted, and what they did is "
            "recorded under it. That is the answer to a status question."
        )
        lines.append(
            "   THIS OVERRIDES ANY STANDING RULE ABOUT RE-ESCALATING A REPEAT FOLLOW-UP. "
            "An earlier \"someone will contact you\" in the history is NOT evidence that "
            "[E1] is still pending — the team has since closed it. Do NOT apologise for a "
            "delay on it, do NOT say the customer is still waiting on it, and do NOT "
            "re-escalate it."
        )
        if everything_closed:
            lines.append(
                "   EVERY issue below is closed, so do NOT call escalate_to_agent at all "
                "unless the customer reports a NEW problem not listed here, or tells you "
                "the recorded action did not actually work."
            )
        else:
            lines.append(
                "   Another entry below is still open. You may act on THAT one if the "
                "customer is asking about it — but describe it as itself, never as a "
                "delay on [E1]."
            )
        lines.append("")

    for idx, thread in enumerate(shown, start=1):
        tag = f"[E{idx}]"
        head = f"{tag} {thread.category}"
        if thread.order_id:
            head += f" · order {thread.order_id}"
        head += f" · first raised {_ist(thread.raised_at)}{_age_phrase(thread, now)}"
        lines.append(head)
        if thread.has_partial_resolution:
            # Open, but a human already closed part of it. Say what was done and
            # when, and say plainly that this is NOT "no update yet" — otherwise
            # the agent apologises for a delay and re-escalates an issue the
            # support team has already dealt with.
            still_open = thread.open_member_count
            lines.append(
                f"     STATUS: PARTLY RESOLVED — the team recorded an action on "
                f"{_ist(thread.partial_resolved_at)}"
            )
            lines.append(
                f'     Action taken: "{_one_line(thread.partial_resolution_text)}"'
            )
            lines.append(
                f"     {still_open} earlier report(s) in this issue are still open. "
                "Lead with the action above — it is the latest thing the team did. "
                "Do NOT say there is no update, and do NOT re-escalate unless the "
                "customer says that action did not work."
            )
        elif thread.has_progress_note:
            # Still open — a progress note is not a resolution and must not be
            # dressed up as one — but "no human update recorded yet" is simply
            # false here, and saying it to a customer whose teammate posted a
            # dispatch time three minutes after they complained is the whole
            # failure this branch exists to prevent.
            lines.append(
                "     STATUS: OPEN — the team is working on it and posted an update on "
                f"{_ist(thread.progress_note_at)}"
            )
            lines.append(f'     Team update: "{_one_line(thread.progress_note)}"')
            lines.append(
                "     THIS is the update the customer is asking for. Restate it in your "
                "own words. Do NOT say there is no update. Do NOT re-escalate unless "
                "the customer says what it promised did not happen."
            )
        elif thread.is_unresolved:
            lines.append("     STATUS: UNRESOLVED — no human update recorded yet")
        else:
            lines.append(f"     STATUS: RESOLVED by the support team on {_ist(thread.resolved_at)}")
            if thread.resolution_text and thread.resolution_text.strip():
                lines.append(f'     Action taken: "{_one_line(thread.resolution_text)}"')
            else:
                lines.append(
                    "     Action taken: not recorded — do not guess what was done"
                )
        if thread.reason:
            lines.append(f'     Handed over as: "{_one_line(thread.reason)}"')
        if thread.chase_count > 0:
            times = "time" if thread.chase_count == 1 else "times"
            lines.append(
                f"     Customer has already chased this {thread.chase_count} {times}."
            )
        lines.append("")

    if hidden > 0:
        # Says "older" rather than "older resolved": with more issues than slots
        # the overflow can be unresolved too, and claiming otherwise would tell
        # the agent something false about what it is not being shown.
        lines.append(f"(+{hidden} older issue(s) not shown)")
        lines.append("")

    # Pulled from Postgres, not from the chat history — the history lives in Redis
    # and is gone after 24h of silence, which is exactly when a customer chasing a
    # days-old issue comes back. Without this the agent would claim "no update yet"
    # over the top of an answer a teammate already gave.
    if human_replies:
        lines.append(
            "⚠️ A HUMAN TEAMMATE HAS ALREADY REPLIED to this customer since the issue "
            "above was raised:"
        )
        for reply in human_replies:
            lines.append(f"   › {_ist(reply.sent_at)}: \"{_one_line(reply.text)}\"")
        lines.append(
            "   Treat this as the update. It is why an issue can read UNRESOLVED and "
            "still have been handled — marking it resolved is a separate step people skip."
        )
        lines.append("")

    lines.append(_HOW_TO_USE)
    return "\n".join(lines)


_HOW_TO_USE = """HOW TO USE THIS — follow in order:

1. FIRST decide which ONE of these, if any, the customer's CURRENT message is about.
   THE ENTRIES ARE ALREADY ORDERED BY HOW LIKELY THEY ARE TO BE THE ONE MEANT.
   [E1] is the default answer; only move off it for a positive reason.
   - Names or implies an order listed above   -> that issue.
   - No order named, or ambiguous             -> [E1].
   - A vague status question — "any update?", "what's happening with my complaint?",
     "any news?" — is about [E1]. It is NOT a reason to skip a RESOLVED entry:
     [E1] is listed first BECAUSE it is the most recent thing that happened to this
     customer, and if the team has just acted, that action is the update they are
     asking for. Answering about an older open entry instead tells someone whose
     issue was just settled that nothing has happened.
   - NONE OF THEM -> a product, price, size, stock, offer, discount or policy question,
     an order NOT listed above, or any new issue.
   Pick at most one. Never respond about two issues in one reply, and never merge them.

2. IF NONE OF THEM -> answer the actual question normally and completely.
   Do NOT mention any escalation, the support team, a pending issue, or "we're on it".
   Do NOT apologise for an unrelated issue. Behave exactly as if this block did not exist.

3. IF THE MATCHED ISSUE IS PARTLY RESOLVED -> the team HAS acted on it. Answer from the
   "Action taken" line: restate what was done, in your own words. Do NOT apologise for a
   delay, do NOT say there is no update, and do NOT re-escalate — the older open reports
   are earlier duplicates of the same complaint, not a fresh grievance. Re-escalate ONLY
   if the customer tells you that action did not work, and then follow step 4c.

3b. IF THE MATCHED ISSUE SHOWS A "Team update" -> the team is working on it and has said
   what is happening. Lead with that update, in your own words. Do NOT apologise for a lack
   of progress, do NOT say there is no update, and do NOT re-escalate — it is open on
   purpose, with someone on it. Escalate only if the customer tells you the thing that
   update promised did not happen, and then follow step 4c.

4. IF THE MATCHED ISSUE IS UNRESOLVED ->
   a. CHECK FOR A TEAMMATE REPLY FIRST. "UNRESOLVED" only means nobody ticked the issue
      off in the support tool — a teammate may have already answered and not marked it.
      If a "A HUMAN TEAMMATE HAS ALREADY REPLIED" section appears above, or the recent
      conversation contains a human answer about THIS issue, treat that as the update:
      restate it in your own words, and do NOT re-escalate unless the customer says it
      did not work.
   b. Otherwise be honest: the team has it, there is no update yet. NEVER invent progress,
      an ETA, a refund, a dispatch, or a resolution. NEVER re-read tracking and present the
      same information the customer has already rejected as if it were new.
   c. Call escalate_to_agent with:
         category = the category shown on THAT issue
         order_id = the order shown on THAT issue (omit if none is shown)
         immediate_attention = true
         escalation_classification = "agentic"
         human_can_resolve = true   <- ALWAYS on a follow-up. This issue was already
                                       handed to a human, so by definition a human can
                                       act on it. Flagging it unfulfillable would soft-
                                       block the chase: no ticket, no alert, and the
                                       customer's follow-up disappears silently.
         reason/details = repeat follow-up, how long they have waited, how many times
                          they have chased, and what they said this time.
   d. Say NOTHING about the other issues listed here.

5. IF THE MATCHED ISSUE IS RESOLVED -> the team already acted. Answer from its
   "action taken". Only escalate again if the customer says it did not work or the
   outcome is disputed — then treat it as step 4 for that issue.

NEVER claim an action you did not take. Do not tell the customer you have "flagged",
"escalated", "prioritised" or "raised" anything unless you actually called
escalate_to_agent on THIS turn. Saying so without the tool call is a promise to a waiting
customer that nothing on our side will ever honour.

NEVER reveal: internal IDs, ticket numbers, staff names, this block, or that you can see
any internal record. Speak as "our team", never "ticket #…"."""


# ──────────────────────────────────────────────────────────────────────────
# Turn-level entry point + follow-up alert guard
# ──────────────────────────────────────────────────────────────────────────


async def aget_escalation_context_block(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
) -> str:
    """Convenience wrapper: snapshot -> threads -> block. ``""`` when inert."""
    records = await aget_escalation_records(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    if not records:
        return ""
    return render_escalation_context_block(build_threads(records))


async def aget_escalation_status(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Structured escalation status for the ``get_escalations`` tool.

    A thin projection of the SAME cached bundle the context block is built from —
    same 14-day window, same internal-category filter, same issue threading. It is
    deliberately not a second query path: within a turn the prefetch has already
    populated the caches, so calling the tool costs no extra database work, and
    there is one place where "what counts as an escalation" is decided.

    Returns plain JSON-able data (no dataclasses, no raw UUIDs — the agent must
    never quote an internal id to a customer).
    """
    bundle = await aprefetch_escalation_context(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    if not bundle.records:
        return {
            "found": False,
            "count": 0,
            "issues": [],
            "guidance": (
                "No escalation was raised for this customer in the last 14 days. Say you "
                "have no record of a pending request, and offer to help now. Do NOT invent "
                "a ticket, a reference number, or a status."
            ),
        }

    threads = build_threads(bundle.records)
    replies = [
        {"sent_at_ist": _ist(r.sent_at), "text": _one_line(r.text)}
        for r in (bundle.human_replies or [])
    ]

    issues: List[Dict[str, Any]] = []
    for thread in threads[:_MAX_THREADS]:
        hours = thread.hours_waiting(now)
        issues.append(
            {
                "what_it_is_about": (
                    f"order {thread.order_id}" if thread.order_id else thread.category.lower()
                ),
                "category": thread.category,
                "order_id": thread.order_id,
                "status": thread.status,
                "raised_at_ist": _ist(thread.raised_at),
                "waiting_hours": round(hours, 1) if hours is not None else None,
                "times_chased": thread.chase_count,
                "resolution_recorded": bool(
                    thread.resolution_text and thread.resolution_text.strip()
                ),
                "resolution": _one_line(thread.resolution_text) or None,
                # For the agent's own reasoning ("we have your email, the team will
                # write to you"). Do not read it back to the customer.
                "contact_on_file": _one_line(thread.customer_contact) or None,
                # What a teammate last posted from the dashboard while the issue
                # is still open. Same source as the block's "Team update" line, so
                # the tool and the block can never tell the agent different things.
                "team_update": _one_line(thread.progress_note) or None,
                "team_update_at": _ist(thread.progress_note_at) if thread.progress_note_at else None,
            }
        )

    return {
        "found": True,
        "count": len(issues),
        "issues": issues,
        "human_replies_since_raised": replies,
        "guidance": (
            "Answer about ONE issue — the one the customer asked about. Never read out an "
            "internal id or invent a ticket number; refer to the issue by its order or "
            "subject. If human_replies_since_raised is non-empty, that is the real update: "
            "restate it. If the issue is unresolved with no such reply, say honestly that "
            "the team has it and no update is recorded yet — never invent progress, an ETA "
            "or a dispatch — and call escalate_to_agent with the same category and order, "
            "immediate_attention=true. If it is resolved, answer from 'resolution'."
        ),
    }


async def ahas_unresolved_escalation(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
) -> bool:
    """True when the customer has at least one open escalation (cached read)."""
    records = await aget_escalation_records(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    return any(r.is_unresolved for r in records)


async def aget_contact_on_file(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str] = None,
) -> Optional[str]:
    """The most recent contact this customer gave on an earlier escalation.

    Web chat asks a guest for a phone or email before handing off, because the
    session id alone is not something a support agent can reach anyone by. That
    prompt is gated on a scratchpad flag living in state that expires after 24h
    of silence — so a customer chasing a two-day-old issue is asked for the same
    address they already gave, on a ticket that already carries it.

    Reads the cached snapshot the context block uses, so on a turn where the
    prefetch already ran this costs nothing. Returns ``None`` when the feature
    is off, nothing is on file, or anything fails — the caller then asks, which
    is exactly the behaviour that existed before this function.
    """
    records = await aget_escalation_records(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    for record in sorted(records, key=lambda r: r.raised_at or _EPOCH, reverse=True):
        contact = str(record.customer_contact or "").strip()
        if contact:
            return contact
    return None


async def alink_escalations_by_contact(
    *,
    client_id: Optional[str],
    contact: Optional[str],
    identity: Optional[str],
    trace_id: Optional[str] = None,
) -> int:
    """Re-key guest escalations carrying ``contact`` onto ``identity``.

    A web guest is only ever known by the id in their browser's localStorage, so
    the same person on a second device is a different customer as far as the
    escalation lookup is concerned. A phone number fixes that — the session
    migration re-keys their rows onto it. An email never did, which left an
    email-only customer permanently unable to be recognised anywhere but the
    browser they first complained from.

    This closes that by moving the earlier rows onto the identity in front of us
    now, mirroring ``amigrate_webchat_guest_to_phone``. Deliberately narrow:

    * only rows whose ``customer_phone`` is *itself* a guest id are moved, so a
      row already keyed to a real phone number is never downgraded to a session
      id — the phone is the stronger key and must win;
    * only within the same ``client_id`` (AGENTS.md §7) and the same 14-day
      window the context block reads, which keeps the scan on
      ``ix_escalation_client_created`` (~0.5 ms against production);
    * only onto a guest identity, since a real phone gets rows via the phone
      migration and does not need this path.

    Idempotent: the second call matches nothing, because the rows it would move
    already carry ``identity``. Returns the number of rows moved, ``0`` on any
    failure — this is a convenience, never a reason to fail a turn.
    """
    if not is_escalation_context_enabled():
        return 0
    address = str(contact or "").strip().lower()
    target = str(identity or "").strip()
    if "@" not in address or not target.startswith(_WEB_PREFIXES):
        return 0
    if not client_id:
        # A missing client_id is an error, not a default (AGENTS.md §7). An
        # unscoped UPDATE here would hand one tenant's escalations to another.
        from fashion_bot.rollbar_config import report_error

        report_error(
            "escalation_context: missing client_id, skipping contact link",
            level="error",
            trace_id=trace_id,
        )
        return 0
    if not await ais_enabled_for_client(str(client_id)):
        return 0

    stored_target = target[:_STORED_PHONE_MAXLEN]
    try:
        from fashion_bot.database_manager import get_async_postgres_connection

        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                # Find first: the common answer is "nothing to move", and this
                # keeps the write off the vast majority of calls. It also names
                # the identities whose cached snapshot has to be dropped, which
                # an UPDATE ... RETURNING cannot give us (it returns the new
                # value, and the stale cache is keyed on the old one).
                #
                # Windowed on created_at rather than escalation_date to use
                # ix_escalation_client_created; the two are written in the same
                # statement and differ by microseconds in production.
                await cur.execute(
                    """
                    SELECT DISTINCT customer_phone
                    FROM escalations
                    WHERE client_id = %s::uuid
                      AND created_at > NOW() - make_interval(days => %s)
                      AND LOWER(escalation_metadata->>'customer_contact') = %s
                      AND customer_phone IS NOT NULL
                      AND customer_phone <> %s
                      AND LEFT(customer_phone, 4) = ANY(%s)
                    """,
                    (
                        str(client_id),
                        _LOOKBACK_DAYS,
                        address,
                        stored_target,
                        list(_WEB_PREFIXES),
                    ),
                )
                previous = []
                for row in await cur.fetchall() or []:
                    value = (row if isinstance(row, dict) else dict(row)).get(
                        "customer_phone"
                    )
                    if value:
                        previous.append(str(value))
                if not previous:
                    return 0
                if len(previous) > _MAX_CONTACT_LINK_IDENTITIES:
                    # More guest identities than one person plausibly browses
                    # from means this address is not identifying — a store's own
                    # support address that several customers typed when asked
                    # for a contact, most likely. Merging on it would show one
                    # customer another's escalations. Bail rather than guess.
                    logger.info(
                        "[ESCALATION_CTX] contact matches %d identities, too many to be "
                        "one person — not linking",
                        len(previous),
                        extra={"trace_id": trace_id, "client_id": client_id},
                    )
                    return 0

                # Moves EVERY row from those guest sessions in the window, not
                # only the ones that recorded the contact: the identity we just
                # matched is a browser session belonging to this customer, so
                # its other escalations are theirs too, and leaving them behind
                # would split one person's history in half. Same reasoning, and
                # the same shared-browser caveat, as the phone migration.
                await cur.execute(
                    """
                    UPDATE escalations
                    SET customer_phone = %s
                    WHERE client_id = %s::uuid
                      AND created_at > NOW() - make_interval(days => %s)
                      AND customer_phone = ANY(%s)
                    """,
                    (stored_target, str(client_id), _LOOKBACK_DAYS, previous),
                )
                moved = cur.rowcount or 0
    except Exception as e:
        logger.warning(
            "[ESCALATION_CTX] contact link failed: %s",
            e,
            extra={"trace_id": trace_id, "client_id": client_id},
        )
        return 0

    # Both sides of the move are now cached wrong: the old keys still list rows
    # that have left, and the target key may hold a long-lived "no escalations"
    # answer that would hide the rows that just arrived.
    for stale in previous + [target]:
        await abust_escalation_snapshot(client_id, stale)

    logger.info(
        "[ESCALATION_CTX] linked %d escalation(s) to %s by contact from %d prior identity(ies)",
        moved,
        stored_target[:12],
        len(previous),
        extra={"trace_id": trace_id, "client_id": client_id},
    )
    return moved


async def aget_follow_up_lineage(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    category: Optional[str],
    order_id: Optional[str],
    trace_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Lineage for an escalation that repeats an already-open issue.

    ``None`` when this is a first-time escalation — the caller then behaves exactly
    as it does today.
    """
    records = await aget_escalation_records(
        client_id=client_id, phone_number=phone_number, trace_id=trace_id
    )
    if not records:
        return None
    thread = find_matching_thread(
        build_threads(records), order_id=order_id, category=category
    )
    if not thread:
        return None
    hours = thread.hours_waiting(now)
    return {
        "thread_key": thread.thread_key,
        "follow_up_of": thread.root_escalation_id,
        "follow_up_count": thread.chase_count + 1,
        "original_raised_at": thread.raised_at.isoformat() if thread.raised_at else None,
        "hours_waiting": round(hours, 1) if hours is not None else None,
    }


async def ashould_send_follow_up_alert(
    *,
    client_id: Optional[str],
    phone_number: Optional[str],
    thread_key: str,
) -> bool:
    """Absorb a rapid-fire burst of chases on ONE issue. Alerting stays immediate.

    Only reached when the escalation repeats an already-open issue, so the
    original always alerts. This call is also the one that *sets* the key, so the
    customer's first chase always alerts too (marked urgent). Only the third and
    later messages on the same issue inside the window go quiet — and the
    escalation row is written regardless, so dashboard counts and resolution-rate
    denominators are unaffected.

    Fail-open: Redis down ⇒ ``True`` (a duplicate alert beats a dropped one).
    """
    from fashion_bot.workers.idempotency import already_processed

    if _FOLLOWUP_COOLDOWN_MINUTES <= 0:
        return True  # suppression disabled by config
    identity = escalation_identity(phone_number)
    if not client_id or not identity or not thread_key:
        return True
    suppressed = await already_processed(
        f"{client_id}:{identity}:{thread_key}",
        namespace="escalation_followup",
        ttl_seconds=_FOLLOWUP_COOLDOWN_MINUTES * 60,
    )
    return not suppressed
