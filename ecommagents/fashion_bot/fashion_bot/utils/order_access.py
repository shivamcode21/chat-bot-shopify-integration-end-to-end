"""Order-access verification (second factor for order tools on web chat).

The legacy check releases an order to whoever asserts a phone number attached
to it. On WhatsApp that number is asserted by the channel, so it is a real
possession proof; on web chat the customer types it, so a known mobile number
is enough to list someone's orders and then view or modify them.

This module adds a per-client, web-chat-only second factor:

* ``order_id``  — the customer names the order; the existing phone-vs-order
  match in ``tool_factory._avalidate_phone_for_order_access`` is the proof.
* ``pincode``   — fallback for customers who don't know their order ID. The
  N most recent orders for the phone are fetched and the pincode is compared
  against each order's shipping ``zip``. Only the orders that match are
  released.

A successful check writes a *grant* onto the turn state (``order_auth``) so a
follow-up message ("now change the address") is not re-challenged. The grant is
bound to client + conversation + phone, lists the order IDs it covers, and
expires.

Everything here is inert unless the client has opted in via the
``order_access_verification`` config key AND the turn is on a channel listed in
that policy (default: web chat only). With the policy off, every function
short-circuits and callers keep their current behaviour byte-for-byte.

State note (AGENTS.md §2 — tools are stateless): nothing in this module writes
to conversation state. The gates are pure: they take the current grant as input
and RETURN the updated grant under ``GRANT_RESULT_KEY`` in the tool's result.
``OrderAuthMiddleware`` (runtime layer) is what actually applies it to state —
mid-loop, so the next tool in the same turn sees it — and ``generic_skill_node``
hands it back to LangGraph for persistence.

See ``design_docs/ORDER_ACCESS_VERIFICATION.md``.
"""

from __future__ import annotations

import functools
import inspect
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

POLICY_CONFIG_KEY = "order_access_verification"

# Reserved key a gated tool puts its grant under in its own result. The runtime
# (OrderAuthMiddleware) reads it, applies it to state, and strips it from what
# the model sees. Tools never touch conversation state themselves.
GRANT_RESULT_KEY = "_order_auth"

# Off unless a client opts in — every existing client keeps today's behaviour.
_POLICY_DISABLED: Dict[str, Any] = {"enabled": False}

_WEB_CHANNEL = "web-chat"
_DEFAULT_CHANNELS = (_WEB_CHANNEL,)
# The policy read is bounded so a Redis/DB stall degrades instead of hanging a turn.
_POLICY_READ_TIMEOUT_S = 3.0
_DEFAULT_METHODS = ("order_id", "pincode")
_DEFAULT_RECENT_WINDOW = 3
_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_GRANT_TTL_MINUTES = 60
_DEFAULT_SCOPE = "read_write"

# How many of the customer's own messages to scan when confirming that a
# verification value actually came from them (see ``value_from_customer``).
_MESSAGE_LOOKBACK = 6

# The verification fetch pulls the same width the actionable-orders path uses
# (orchestrator: limit=20) so the ONE fetch serves both the pincode match and
# the listing tool's own view. Matching still only considers the policy's
# recent_window; the extra rows exist purely so the tool can skip a second
# phone->orders lookup (~2 sequential Shopify round-trips).
_VERIFY_FETCH_WIDTH = 20


# ==================== policy ====================


async def aget_verification_policy(state: Optional[Dict]) -> Dict[str, Any]:
    """Resolve the effective verification policy for this turn.

    Returns ``{"enabled": False}`` when the client has not opted in, the config
    is unreadable, or the turn is not on web chat.

    The channel is resolved FIRST, before any I/O: verification exists because a
    web-chat phone number is self-declared, while WhatsApp asserts the number
    itself. So a WhatsApp (or unknown-channel) turn returns here without a
    config read at all — the majority channel pays nothing for this feature. A
    consequence worth knowing: the config's ``channels`` list can switch
    verification off for web chat, but cannot switch it on for WhatsApp.
    """
    state = state or {}
    client_id = state.get("client_id")
    if not client_id:
        # Tenant resolution failures are handled upstream; here we simply must
        # not invent a policy for an unknown tenant (AGENTS.md §7).
        return _POLICY_DISABLED

    if _resolve_channel(state) != _WEB_CHANNEL:
        return _POLICY_DISABLED

    try:
        import asyncio

        from fashion_bot.config_manager import aget_config

        # Bounded: this sits in the order-tool hot path, and a Redis/DB stall
        # must degrade (verification off, logged) rather than hang the turn.
        raw = await asyncio.wait_for(
            aget_config(POLICY_CONFIG_KEY, default=None, client_id=client_id),
            timeout=_POLICY_READ_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        _log(state, f"⚠️ {POLICY_CONFIG_KEY} read timed out; verification stays off this turn", "warning")
        return _POLICY_DISABLED
    except Exception:
        return _POLICY_DISABLED

    if not raw:
        return _POLICY_DISABLED

    try:
        cfg = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        _log(state, f"⚠️ Unparseable {POLICY_CONFIG_KEY} config; verification stays off", "warning")
        return _POLICY_DISABLED

    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return _POLICY_DISABLED

    if _WEB_CHANNEL not in (cfg.get("channels") or list(_DEFAULT_CHANNELS)):
        return _POLICY_DISABLED

    return {
        "enabled": True,
        "methods": cfg.get("methods") or list(_DEFAULT_METHODS),
        "recent_window": _int_or(cfg.get("recent_window"), _DEFAULT_RECENT_WINDOW),
        "max_attempts": _int_or(cfg.get("max_attempts"), _DEFAULT_MAX_ATTEMPTS),
        "grant_ttl_minutes": _int_or(cfg.get("grant_ttl_minutes"), _DEFAULT_GRANT_TTL_MINUTES),
        "pincode_grants": cfg.get("pincode_grants") or _DEFAULT_SCOPE,
    }


def _resolve_channel(state: Dict) -> str:
    try:
        from fashion_bot.utils.escalation_helper import resolve_channel_from_state

        return resolve_channel_from_state(state) or ""
    except Exception:
        return ""


def _int_or(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _log(state: Optional[Dict], message: str, level: str = "info") -> None:
    try:
        from fashion_bot.utils.utils import log_with_trace_id

        log_with_trace_id(state or {}, message, level)
    except Exception:
        pass


# ==================== pure helpers ====================


def normalize_pincode(value: Any) -> str:
    """Digits-only pincode, or "" when the value is not a 6-digit pincode."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 6 else ""


def normalize_order_key(value: Any) -> str:
    """Canonical key for an order identifier ("#GV63084" -> "gv63084")."""
    return str(value or "").strip().lstrip("#").strip().lower()


def order_keys_match(left: Any, right: Any) -> bool:
    """Two order identifiers refer to the same order.

    Compares canonical keys first, then the digit runs, so a prefixed name
    ("gv63084") matches the bare number ("63084") the way the order adapter's
    own lookup does.
    """
    lkey, rkey = normalize_order_key(left), normalize_order_key(right)
    if not lkey or not rkey:
        return False
    if lkey == rkey:
        return True
    ldigits = re.sub(r"\D", "", lkey)
    rdigits = re.sub(r"\D", "", rkey)
    return bool(ldigits) and ldigits == rdigits


def normalize_phone(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-10:] if len(digits) >= 10 else digits


def order_identity_keys(order: Dict) -> List[str]:
    """Every identifier an order may be referred to by."""
    keys = []
    for candidate in (order.get("name"), order.get("order_number"), order.get("order_id"), order.get("id")):
        key = normalize_order_key(candidate)
        if key and key not in keys:
            keys.append(key)
    return keys


def order_pincode(order: Dict) -> str:
    """The shipping pincode on an order, normalized. "" when absent."""
    shipping = order.get("shipping_address") or {}
    if not isinstance(shipping, dict):
        return ""
    return normalize_pincode(shipping.get("zip") or shipping.get("postal_code"))


def value_from_customer(value: str, state: Optional[Dict], lookback: int = _MESSAGE_LOOKBACK) -> bool:
    """The verification value appears in the customer's own recent messages.

    The pincode on an order is in the model's context the moment an order is
    read (``get_order_details`` returns ``shipping_address`` verbatim, and tool
    observations are replayed into the message list), so a value the model
    supplies is only trustworthy if the customer actually typed it.
    """
    digits = re.sub(r"\D", "", str(value or ""))
    if not digits:
        return False
    for message in _recent_user_messages(state, lookback):
        runs = re.findall(r"\d+", message)
        # A whole digit run the customer typed ("my order is gv63084" -> "63084").
        if digits in runs:
            return True
        # ...or the message's digits ARE the value, just spaced out ("560 103").
        # Deliberately not a substring test over the joined digits: the customer's
        # own 10-digit phone number contains six-digit windows ("9876543210"
        # contains "987654"), which would accept a pincode they never gave.
        if "".join(runs) == digits:
            return True
    return False


def _recent_user_messages(state: Optional[Dict], lookback: int) -> List[str]:
    messages = (state or {}).get("messages") or []
    texts: List[str] = []
    for message in reversed(messages):
        if getattr(message, "type", "") != "human":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, str) and content:
            texts.append(content)
        if len(texts) >= lookback:
            break
    return texts


# ==================== grant ====================


def grant_is_valid(grant: Any, state: Optional[Dict], ttl_minutes: int, phone: str = "") -> bool:
    """The grant was issued to this client + conversation + phone and is fresh."""
    if not isinstance(grant, dict) or not grant.get("order_ids"):
        return False
    state = state or {}
    if grant.get("client_id") != state.get("client_id"):
        return False
    if grant.get("conversation_id") != state.get("conversation_id"):
        return False

    requested_phone = normalize_phone(phone or state.get("phone_number", ""))
    if requested_phone and grant.get("phone") != requested_phone:
        return False

    verified_at = grant.get("verified_at")
    if not verified_at:
        return False
    try:
        issued = datetime.fromisoformat(str(verified_at))
        if issued.tzinfo is None:
            issued = issued.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    age_minutes = (datetime.now(timezone.utc) - issued).total_seconds() / 60
    return age_minutes <= ttl_minutes


def grant_covers(grant: Any, order_id: Any, mutating: bool = False) -> bool:
    """The grant authorizes this order for this kind of access."""
    if not isinstance(grant, dict):
        return False
    if mutating and grant.get("scope") == "read_only":
        return False
    return any(order_keys_match(order_id, granted) for granted in grant.get("order_ids") or [])


def granted_order_ids(grant: Any) -> Set[str]:
    if not isinstance(grant, dict):
        return set()
    return {normalize_order_key(o) for o in (grant.get("order_ids") or []) if o}


def _same_conversation(existing: Any, state: Dict) -> bool:
    return (
        isinstance(existing, dict)
        and existing.get("client_id") == state.get("client_id")
        and existing.get("conversation_id") == state.get("conversation_id")
    )


def build_grant(
    state: Optional[Dict],
    *,
    method: str,
    order_ids: List[str],
    phone: str = "",
    scope: str = _DEFAULT_SCOPE,
) -> Optional[Dict[str, Any]]:
    """Build the grant a successful check produces. PURE — writes nothing.

    The caller returns this to the runtime under ``GRANT_RESULT_KEY``;
    ``OrderAuthMiddleware`` is what puts it on state.
    """
    state = state or {}
    keys = [normalize_order_key(o) for o in order_ids if normalize_order_key(o)]
    if not keys:
        return None

    phone_key = normalize_phone(phone or state.get("phone_number", ""))
    existing = state.get("order_auth")
    merged = list(keys)
    failed_attempts = 0
    same_customer = _same_conversation(existing, state) and existing.get("phone") == phone_key
    if same_customer:
        # Carried regardless of scope: the attempt counter is a rate limit, so
        # switching verification method must never reset it.
        failed_attempts = int(existing.get("failed_attempts") or 0)
    if same_customer and existing.get("scope") == scope:
        # Same customer, same conversation, same scope: accumulate rather than
        # replace, so verifying a second order does not revoke access to the
        # first. Scopes are NOT merged — folding a read_write order_id proof into
        # a read_only pincode grant would silently upgrade the pincode-granted
        # orders to mutable. The dropped orders keep their normal access via the
        # phone-vs-order check, they simply no longer ride on this grant.
        merged = list(dict.fromkeys(list(existing.get("order_ids") or []) + keys))

    return {
        "client_id": state.get("client_id"),
        "conversation_id": state.get("conversation_id"),
        "phone": phone_key,
        "method": method,
        "order_ids": merged,
        "scope": scope,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "failed_attempts": failed_attempts,
    }


def bump_failure(state: Optional[Dict], phone: str = "") -> Dict[str, Any]:
    """Build the grant record with one more failed attempt. PURE — writes nothing."""
    state = state or {}
    existing = state.get("order_auth")
    carried = existing if _same_conversation(existing, state) else {}
    return {
        "client_id": state.get("client_id"),
        "conversation_id": state.get("conversation_id"),
        "phone": normalize_phone(phone or state.get("phone_number", "")),
        "method": carried.get("method"),
        "order_ids": list(carried.get("order_ids") or []),
        "scope": carried.get("scope") or _DEFAULT_SCOPE,
        "verified_at": carried.get("verified_at"),
        "failed_attempts": int(carried.get("failed_attempts") or 0) + 1,
    }


def apply_grant_update(state: Optional[Dict], update: Any) -> None:
    """Put a grant emitted by a tool onto state. RUNTIME ONLY.

    Called by ``OrderAuthMiddleware`` (and nothing else) — this is the single
    place a verification result becomes conversation state, which keeps the
    tools themselves pure per AGENTS.md §2.
    """
    if state is None or not isinstance(update, dict) or not update.get("conversation_id"):
        return
    state["order_auth"] = update
    if update.get("order_ids"):
        _log(state, f"🔓 Order access granted via {update.get('method')} for {len(update['order_ids'])} order(s)")


def failure_count(state: Optional[Dict]) -> int:
    grant = (state or {}).get("order_auth")
    if not isinstance(grant, dict):
        return 0
    if grant.get("conversation_id") != (state or {}).get("conversation_id"):
        return 0
    return int(grant.get("failed_attempts") or 0)


# ==================== refusal payloads ====================

# Deliberately says nothing about the pincode fallback. This text arrives in the
# model's context immediately before it writes its reply, and models paraphrase
# it almost verbatim — naming the fallback here makes the agent offer the
# customer a choice ("Order ID? Or the pincode…") on the very first ask, which
# defeats the point of asking for the stronger proof first. The conditional
# fallback rule lives in the system block instead (build_verification_instruction_block),
# which the model reads as policy rather than as a message to relay.
_ASK_TEXT = (
    "Before sharing or changing any order information, ask the customer for their "
    "Order ID and nothing else. Do NOT mention or offer any alternative way to "
    "verify in this message. Once they give it to you, call this tool again with "
    "verification_identifier_type='order_id' and verification_identifier_value "
    "set to exactly what they typed."
)


def verification_required_response() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "verification_required",
        "phone_validated": False,
        "orders": [],
        "message": _ASK_TEXT,
    }


def verification_failed_response(attempts_remaining: int) -> Dict[str, Any]:
    return {
        "success": False,
        "error": "verification_failed",
        "phone_validated": False,
        "orders": [],
        "attempts_remaining": max(attempts_remaining, 0),
        "message": (
            "That does not match the orders on this phone number. Tell the customer "
            "it did not match and ask them to re-check the detail they just gave "
            "you. Do not reveal any order details, and do not list other ways to "
            "verify unless they say they cannot find it."
        ),
    }


def verification_locked_response() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "verification_locked",
        "phone_validated": False,
        "needs_escalation": True,
        "orders": [],
        "message": (
            "Verification has failed too many times in this conversation. Do not "
            "share or change any order information. Tell the customer you are "
            "connecting them with the support team."
        ),
    }


# ==================== the gate ====================


def _gate_ok(
    *,
    filter_order_ids: Optional[Set[str]] = None,
    grant_update: Optional[Dict] = None,
    prefetched_orders: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Allowed. ``prefetched_orders`` is the fetch the gate already paid for —
    the caller passes it as ``cached_orders`` instead of fetching again."""
    return {
        "allowed": True,
        "response": None,
        "filter_order_ids": filter_order_ids,
        "grant_update": grant_update,
        "prefetched_orders": prefetched_orders,
    }


def _gate_refused(response: Dict[str, Any], *, grant_update: Optional[Dict] = None) -> Dict[str, Any]:
    """Refusal payload. The grant update (a failed attempt) rides along in the
    tool's own result so the runtime can record it — the tool writes nothing."""
    if grant_update:
        response = {**response, GRANT_RESULT_KEY: grant_update}
    return {
        "allowed": False,
        "response": response,
        "filter_order_ids": None,
        "grant_update": grant_update,
        "prefetched_orders": None,
    }


async def averify_listing_access(
    state: Optional[Dict],
    *,
    phone: str,
    verification_identifier_type: str = "",
    verification_identifier_value: str = "",
    policy: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Gate the phone-only listing tools (``get_recent_orders`` and friends).

    A phone number alone must never produce a list of orders — that is what
    lets an unverified caller harvest order IDs and walk in through the
    order-ID door.

    Returns ``{"allowed": bool, "response": dict|None, "filter_order_ids": set|None}``.
    ``filter_order_ids`` is ``None`` when no filtering applies (policy off);
    otherwise the caller must return only the orders it names.
    """
    state = state or {}
    policy = policy if policy is not None else await aget_verification_policy(state)
    if not policy.get("enabled"):
        return _gate_ok()

    ttl = policy["grant_ttl_minutes"]
    grant = state.get("order_auth")

    # Reuse the existing grant ONLY when the model is NOT actively supplying
    # new verification credentials.  When both identifier fields are provided
    # it means the customer typed a new value (e.g. a second pincode for a
    # different delivery address) and we must re-verify rather than replay
    # the old grant's filter — otherwise every subsequent pincode silently
    # returns the orders matched by the *first* pincode.
    vtype = str(verification_identifier_type or "").strip().lower()
    vvalue = str(verification_identifier_value or "").strip()
    _has_new_verification = bool(vtype and vvalue and vtype in (policy.get("methods") or list(_DEFAULT_METHODS)))

    if grant_is_valid(grant, state, ttl, phone=phone) and not _has_new_verification:
        return _gate_ok(filter_order_ids=granted_order_ids(grant))

    if failure_count(state) >= policy["max_attempts"]:
        _log(state, "🔒 Order access locked for this conversation (attempt cap reached)", "warning")
        return _gate_refused(verification_locked_response())

    if not vtype or not vvalue or vtype not in policy["methods"]:
        return _gate_refused(verification_required_response())

    if not value_from_customer(vvalue, state):
        # Either the model invented the value or it lifted it from an earlier
        # tool result. Refuse with the same text as "not supplied" so the
        # distinction is logged but never taught to a caller probing the rule.
        _log(
            state,
            f"🚨 Verification value ({vtype}) not found in the customer's own messages; refusing",
            "warning",
        )
        return _gate_refused(verification_required_response())

    fetched = await _afetch_recent_orders(state, phone)
    recent = fetched[: policy["recent_window"]]
    if not recent:
        failed = bump_failure(state, phone=phone)
        return _gate_refused(
            verification_failed_response(policy["max_attempts"] - failed["failed_attempts"]),
            grant_update=failed,
        )

    if vtype == "pincode":
        target = normalize_pincode(vvalue)
        matched = [o for o in recent if target and order_pincode(o) == target] if target else []
        scope = policy["pincode_grants"]
    else:
        matched = [o for o in recent if any(order_keys_match(vvalue, k) for k in order_identity_keys(o))]
        scope = _DEFAULT_SCOPE

    if not matched:
        failed = bump_failure(state, phone=phone)
        _log(state, f"🚫 Order verification failed via {vtype} (attempt {failed['failed_attempts']})", "warning")
        return _gate_refused(
            verification_failed_response(policy["max_attempts"] - failed["failed_attempts"]),
            grant_update=failed,
        )

    keys: List[str] = []
    for order in matched:
        keys.extend(order_identity_keys(order))
    issued = build_grant(state, method=vtype, order_ids=keys, phone=phone, scope=scope)
    return _gate_ok(
        filter_order_ids=granted_order_ids(issued), grant_update=issued, prefetched_orders=fetched,
    )


async def _afetch_recent_orders(state: Dict, phone: str) -> List[Dict]:
    """The customer's recent orders, all statuses, newest first.

    All statuses on purpose: a customer whose only order is already delivered
    must still be able to verify.

    Returns the full fetched list — the caller slices it to the policy window
    for matching and hands the whole thing back to the tool as ``cached_orders``
    so the turn issues one phone->orders lookup instead of two.
    """
    try:
        from fashion_bot.core.orchestrator import UtilityOrchestrator

        result = await UtilityOrchestrator.aget_recent_orders_all_statuses(
            phone, state=state, limit=_VERIFY_FETCH_WIDTH,
        )
        orders = (result or {}).get("orders") or []
        return [o for o in orders if isinstance(o, dict)]
    except Exception as exc:
        _log(state, f"⚠️ Could not load recent orders for verification: {exc}", "warning")
        return []


def filter_orders_to_grant(orders: List[Dict], allowed_keys: Optional[Set[str]]) -> List[Dict]:
    """Keep only the orders the grant covers. ``None`` means no filtering."""
    if allowed_keys is None:
        return orders
    kept = []
    for order in orders:
        identifiers = order_identity_keys(order) or [normalize_order_key(order.get("order_id"))]
        if any(any(order_keys_match(i, allowed) for allowed in allowed_keys) for i in identifiers):
            kept.append(order)
    return kept


async def averification_conflict(
    state: Optional[Dict],
    order_id: str,
    *,
    verification_identifier_type: str = "",
    verification_identifier_value: str = "",
    policy: Optional[Dict] = None,
) -> Optional[Dict[str, Any]]:
    """Fail closed when the caller names one order and verifies another.

    Returns a refusal payload on conflict, otherwise ``None``.
    """
    vtype = str(verification_identifier_type or "").strip().lower()
    vvalue = str(verification_identifier_value or "").strip()
    if vtype != "order_id" or not vvalue or not order_id:
        return None
    policy = policy if policy is not None else await aget_verification_policy(state)
    if not policy.get("enabled"):
        return None
    if order_keys_match(order_id, vvalue):
        return None
    # Only treat it as a conflict when both sides carry digits; a bare prefix or
    # a free-text value must not block an otherwise valid call.
    if not re.sub(r"\D", "", normalize_order_key(order_id)) or not re.sub(r"\D", "", normalize_order_key(vvalue)):
        return None
    _log(
        state,
        f"🚫 Verification conflict: tool called for order {order_id} but verified with {vvalue}",
        "warning",
    )
    return verification_required_response()


async def averify_order_scoped_access(
    state: Optional[Dict],
    *,
    order_id: str,
    phone: str = "",
    customer_email: str = "",
    verification_identifier_type: str = "",
    verification_identifier_value: str = "",
    mutating: bool = False,
    policy: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Gate a tool that already names one order (return/exchange partner tools).

    The order tools in ``tool_factory`` run this check inline through
    ``_avalidate_phone_for_order_access``; the partner tools have no phone check
    of their own, so they borrow it here rather than shipping a second copy.

    Two factors have to hold, and which pair depends on what the customer gave:

    * **order_id** — naming the order IS the second factor (design doc §6.1.5),
      and the phone-vs-order match is the first. Nothing extra to check.
    * **pincode** — naming the order proves nothing here, so the pincode is run
      through the SAME check the listing gate runs and the resulting grant must
      cover this order. Without this the pincode was accepted unread and a wrong
      one still passed on the phone match alone.

    A refused attempt is counted (``bump_failure``) so the policy's ``max_attempts``
    lockout applies here too — otherwise order numbers, which are largely
    sequential, could be guessed against a known phone number indefinitely.
    """
    state = state or {}
    policy = policy if policy is not None else await aget_verification_policy(state)
    if not policy.get("enabled"):
        return _gate_ok()

    if not order_id:
        return _gate_refused(verification_required_response())

    grant = state.get("order_auth")
    if grant_is_valid(grant, state, policy["grant_ttl_minutes"], phone=phone) and grant_covers(
        grant, order_id, mutating=mutating
    ):
        return _gate_ok()

    if failure_count(state) >= policy["max_attempts"]:
        return _gate_refused(verification_locked_response())

    vtype = str(verification_identifier_type or "").strip().lower()
    vvalue = str(verification_identifier_value or "").strip()
    if vtype == "pincode" and vvalue and "pincode" in (policy.get("methods") or list(_DEFAULT_METHODS)):
        return await _apincode_scoped_access(
            state, order_id=order_id, phone=phone, vvalue=vvalue, mutating=mutating, policy=policy,
        )

    from fashion_bot.tool_factory import _avalidate_phone_for_order_access

    check = await _avalidate_phone_for_order_access(
        order_id, state, phone_number=phone, mutating=mutating,
        verification_identifier_type=verification_identifier_type,
        verification_identifier_value=verification_identifier_value,
    )
    if not check.get("should_block"):
        # Record the proof so a follow-up message about the SAME order is not
        # re-challenged (instruction block rule 6). Scope is read_write: naming
        # the Order ID is the strong proof, whatever an earlier pincode granted.
        return _gate_ok(
            grant_update=build_grant(
                state, method="order_id", order_ids=[order_id], phone=phone,
            ),
        )

    if check.get("needs_phone") and customer_email:
        # Gate B accepts an email that matches the order as identity, and web-chat
        # customers who lead with their email would otherwise be locked out of
        # every gated tool the moment a client opts in. Email + named order is the
        # same two-factor shape as phone + named order.
        if await _aemail_matches_order(state, order_id=order_id, customer_email=customer_email):
            return _gate_ok()

    response = verification_required_response()
    if check.get("needs_phone"):
        response["needs_phone"] = True
    response["message"] = check.get("message") or response["message"]

    # Only a real mismatch is an attempt. "Tell me your phone number" is a prompt
    # for input, not a wrong answer, and must not burn the customer's budget.
    if not check.get("failed_verification"):
        return _gate_refused(response)

    failed = bump_failure(state, phone=phone)
    _log(state, f"🚫 Order access failed for {order_id} (attempt {failed['failed_attempts']})", "warning")
    if failed["failed_attempts"] >= policy["max_attempts"]:
        return _gate_refused(verification_locked_response(), grant_update=failed)
    return _gate_refused(response, grant_update=failed)


async def _apincode_scoped_access(
    state: Dict, *, order_id: str, phone: str, vvalue: str, mutating: bool, policy: Dict,
) -> Dict[str, Any]:
    """Pincode proof for a tool that names one order.

    Delegates to ``averify_listing_access`` rather than re-implementing the match:
    that is where the customer-typed check, the recent-order fetch, the failure
    count and the lockout already live. The order named by the tool then has to be
    one the pincode actually released.
    """
    from fashion_bot.utils.phone_number_utils import is_real_phone_number

    if not phone or not is_real_phone_number(str(phone)):
        # No phone to fetch the customer's orders with — fall back to asking for
        # it rather than counting a failure the customer could not have avoided.
        response = verification_required_response()
        response["needs_phone"] = True
        return _gate_refused(response)

    listing = await averify_listing_access(
        state, phone=phone,
        verification_identifier_type="pincode",
        verification_identifier_value=vvalue,
        policy=policy,
    )
    if not listing["allowed"]:
        # A wrong pincode was already counted and phrased by the listing gate.
        return listing

    allowed = listing.get("filter_order_ids")
    if allowed is not None and not any(order_keys_match(order_id, key) for key in allowed):
        # The pincode is genuine but belongs to a different order. Not counted as
        # an attempt: the customer answered correctly and the wrong order came
        # from the agent, and a valid pincode still releases only what it matched.
        _log(state, f"🚫 Pincode did not cover order {order_id}", "warning")
        return _gate_refused(
            verification_failed_response(policy["max_attempts"] - failure_count(state)),
        )

    if mutating and (listing.get("grant_update") or {}).get("scope") == "read_only":
        # Mirrors the read_only rule in _avalidate_phone_for_order_access: a
        # pincode may view the order, but changing it needs the Order ID.
        return _gate_refused({
            "success": False,
            "error": "verification_required",
            "phone_validated": False,
            "message": (
                "Changes to an order need the Order ID. Ask the customer for the "
                "Order ID of the order they want changed, then retry."
            ),
        }, grant_update=listing.get("grant_update"))

    return _gate_ok(grant_update=listing.get("grant_update"))


async def _aemail_matches_order(state: Dict, *, order_id: str, customer_email: str) -> bool:
    """The email the customer gave is on the named order (return_partners Gate B)."""
    try:
        from fashion_bot.return_partners.identity import averify_order_identity

        identity = await averify_order_identity(
            client_id=str(state.get("client_id") or ""),
            order_number=order_id,
            state=state,
            customer_email=customer_email,
        )
        return bool(identity.get("verified")) and identity.get("matched_on") == "email"
    except Exception as exc:
        _log(state, f"⚠️ Email identity check failed for {order_id}: {exc}", "warning")
        return False


def order_access_guarded(
    state: Optional[Dict],
    *,
    order_arg: str = "order_number",
    phone_arg: str = "customer_phone",
    email_arg: str = "customer_email",
    mutating: bool = False,
) -> Callable:
    """Apply the order-scoped gate to a tool without repeating it per tool.

    Sits *under* LangChain's ``@tool`` so the tool schema and docstring still
    come from the wrapped coroutine (``functools.wraps`` sets ``__wrapped__``,
    which ``inspect.signature`` follows).
    """

    def decorator(fn: Callable) -> Callable:
        signature = inspect.signature(fn)

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            policy = await aget_verification_policy(state)
            if not policy.get("enabled"):
                return await fn(*args, **kwargs)

            try:
                bound = signature.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                supplied = bound.arguments
            except TypeError:
                supplied = dict(kwargs)

            gate = await averify_order_scoped_access(
                state,
                order_id=str(supplied.get(order_arg) or ""),
                phone=str(supplied.get(phone_arg) or "") or str((state or {}).get("phone_number") or ""),
                customer_email=str(supplied.get(email_arg) or ""),
                verification_identifier_type=str(supplied.get("verification_identifier_type") or ""),
                verification_identifier_value=str(supplied.get("verification_identifier_value") or ""),
                mutating=mutating,
                policy=policy,
            )
            if not gate["allowed"]:
                return gate["response"]

            result = await fn(*args, **kwargs)
            # The gate stays pure: a grant it issued rides back on the tool's own
            # result and OrderAuthMiddleware is what puts it on state (AGENTS.md §2).
            if gate.get("grant_update") and isinstance(result, dict):
                return {**result, GRANT_RESULT_KEY: gate["grant_update"]}
            return result

        return wrapper

    return decorator


def agent_has_gated_order_tools(tools: Any) -> bool:
    """This agent holds at least one tool that the verification gate covers.

    Derived from the tool schemas rather than a hardcoded name list: every gated
    tool carries the ``verification_identifier_type`` parameter, so a tool gated
    later cannot silently lose its instruction block (which would leave the agent
    asking for nothing while the tool keeps refusing).
    """
    for candidate in tools or []:
        try:
            if "verification_identifier_type" in (getattr(candidate, "args", None) or {}):
                return True
        except Exception:
            continue
    return False


# ==================== agent instructions (injected from code) ====================


def build_verification_instruction_block(policy: Dict[str, Any]) -> str:
    """System block appended by ``generic_skill_node`` when the policy is on.

    Injected from code rather than stored in each client's ``agents_config``
    prompt so the instruction can never drift from the flag that enforces it.
    """
    if not policy.get("enabled"):
        return ""
    methods = policy.get("methods") or list(_DEFAULT_METHODS)
    lines = [
        "ORDER ACCESS VERIFICATION (authoritative for this conversation):",
        "Before you share or change ANY order information, the customer must prove the "
        "order is theirs. Their phone number alone is NOT enough on this channel.",
        "1. Ask for their ORDER ID first, together with the phone number on the order. "
        "Ask for the Order ID ON ITS OWN — do not present a choice, do not mention any "
        "other way to verify, and do not hint that one exists.",
    ]
    if "pincode" in methods:
        lines += [
            "2. The 6-digit PINCODE of the delivery address is a FALLBACK you hold in "
            "reserve. Offer it ONLY after the customer has told you they do not know, "
            "cannot find, or do not have their Order ID. Never offer it in the same "
            "message as the Order ID request, never as 'or you can also give me...', "
            "and never before they have had a chance to give you the Order ID.",
            "   Correct: \"Could you share your Order ID?\" → customer: \"I don't have "
            "it\" → \"No problem — what's the 6-digit pincode on the delivery address?\"",
            "   Wrong:   \"Could you share your Order ID? If you don't have it handy, "
            "you can also give me the 6-digit pincode.\"",
        ]
    lines += [
        "3. Pass what they gave you to the order tools as verification_identifier_type "
        "('order_id' or 'pincode') and verification_identifier_value (the exact value "
        "the customer typed).",
        "4. NEVER supply a verification value the customer did not type in this "
        "conversation — do not take a pincode or Order ID from earlier tool results, "
        "order details, or your own guesses. Doing so will be refused.",
        "5. Never reveal or confirm any part of the pincode, address or order stored on "
        "our side while verifying — the customer must state it first.",
        "6. Once a tool has verified the customer, later requests about the SAME order "
        "do not need to be verified again — do not re-ask.",
        "7. If the customer asks about OTHER orders beyond the one(s) already verified, "
        "ask them for the Order ID of the order they want to check. Do NOT claim that "
        "no other orders exist — the tool results only show verified orders, not all "
        "orders on the account.",
        "This SUPERSEDES any earlier instruction in this prompt that tells you to look "
        "orders up from the phone number alone, or never to ask the customer for their "
        "Order ID.",
    ]
    return "\n".join(lines)
