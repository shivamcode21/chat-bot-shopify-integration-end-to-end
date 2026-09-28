"""
Escalation Helper - Utility functions for logging escalations with consistent formatting.
"""
import asyncio
import os
import re
import uuid
from typing import Dict, Any, Optional, List
from fashion_bot.schema import SupportState
from fashion_bot.utils.utils import log_with_trace_id, get_trace_id
from fashion_bot.config_manager import aresolve_client_id
import logging

logger = logging.getLogger(__name__)

# Channel banner prepended to the notification when an escalation is flagged for
# immediate attention (customer frustration / auto-detected failure).
IMMEDIATE_ATTENTION_BANNER = "⚠️ IMMEDIATE ATTENTION REQUIRED ⚠️"

_VALID_CLASSIFICATIONS = {"user_configured", "agentic", "system"}
_VALID_GROUPS = {"pre_sales", "post_sales", "offline_leads"}


async def build_escalation_metadata(
    *,
    client_id: Optional[str],
    category: str,
    trace_id: Optional[str] = None,
    phone_number: Optional[str] = None,
    escalation_classification: str = "system",
    immediate_attention: bool = False,
    agent: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build escalation metadata guaranteeing the six canonical fields.

    Canonical keys (``trace_id``, ``phone_number``, ``escalation_type``,
    ``escalation_classification``, ``escalation_group``, ``immediate_attention``)
    are merged last so caller-supplied ``extra`` (e.g. ``order_id``) can add
    context but never clobber them.
    """
    from fashion_bot.agent_config import aget_escalation_group

    classification = (
        escalation_classification
        if escalation_classification in _VALID_CLASSIFICATIONS
        else "system"
    )
    # ``order_id`` (when the caller threaded it through ``extra``) lets the group
    # resolver route order-context-sensitive categories (e.g. Frustration) to
    # post_sales instead of the pre_sales default. Falls back to ``order_number``
    # which return-partner escalations use as the order reference key.
    order_id = (extra or {}).get("order_id") or (extra or {}).get("order_number")
    try:
        group = await aget_escalation_group(
            client_id, category, agent=agent, order_id=order_id
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("escalation_group resolution failed: %s", e)
        group = "pre_sales"
    if group not in _VALID_GROUPS:
        group = "pre_sales"

    canonical = {
        "trace_id": trace_id,
        "phone_number": phone_number,
        "escalation_type": (category or "general").lower().replace(" ", "_"),
        "escalation_classification": classification,
        "escalation_group": group,
        "immediate_attention": bool(immediate_attention),
    }
    merged: Dict[str, Any] = dict(extra or {})
    merged.update(canonical)
    return merged


async def _asend_escalation_email(
    notification: str,
    email: Dict[str, List[str]],
    *,
    subject: str,
    trace_id: Optional[str],
    html_body: Optional[str] = None,
) -> None:
    """Send the escalation email off the event loop (SMTP is sync under the hood)."""
    from fashion_bot.utils.utils import asend_email

    from_addr = os.environ.get("ESCALATION_FROM_EMAIL", "escalations@bloomerce.ai")
    to = email.get("to") or []
    cc = email.get("cc") or []
    if not to:
        return
    await asend_email(from_addr, to, cc, f"[Escalation] {subject}", notification, html_body=html_body)


async def asend_escalation_notification(
    notification: str,
    *,
    client_id: Optional[str],
    state: Optional[Dict[str, Any]] = None,
    agent: Optional[str] = None,
    category: Optional[str] = None,
    immediate_attention: bool = False,
    contacts_override: Optional[Dict[str, Any]] = None,
    email_html: Optional[str] = None,
    customer_contact_provided: bool = False,
    details: Optional[str] = None,
    order_id: Optional[str] = None,
    escalation_group: Optional[str] = None,
    customer_contact: Optional[str] = None,
    timestamp_ist: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve escalation recipients and notify ALL of them.

    Replaces the duplicated single-number ``send_message(aget_agent_phone_number(...))``
    pattern. Sends the notification to every resolved WhatsApp number (with
    per-recipient isolation — one failure never suppresses the others) and, when
    the resolved contacts include email recipients, emails them too. The email
    channel is opt-in: legacy / phone-only configs send WhatsApp exactly as
    before.

    ``contacts_override`` short-circuits routing resolution with a caller-supplied
    ``{"phone": [...], "email": {"to": [...], "cc": [...]}}`` — used by the
    store-visit path to inject its store-manager-first contact list into the
    standard pipeline (design §5.4b).

    Passing ``details`` opts this escalation into template-based WhatsApp delivery
    *when the client has configured* ``escalation_template``: the scalar fields
    (``details``/``order_id``/``escalation_group``/``customer_contact``, plus
    ``category``/``immediate_attention`` and the customer phone from ``state``) are
    assembled into template params via :func:`build_escalation_template_fields`,
    and each staff number is tried through the approved Gupshup template
    (deliverable outside the 24h session window). It falls back to free-text
    ``send_message`` only when the template gateway was never hit — so a recipient
    is never double-alerted. Clients without the config, and callers that pass no
    ``details``, keep the exact prior free-text behavior. See
    ``design_docs/ESCALATION_GUPSHUP_TEMPLATES.md``.

    Returns ``{"sent": [...], "failed": [...], "degraded": [...],
    "email_sent": [...], "email_failed": [...], "email_cc": [...],
    "whatsapp_channel": {num: "template"|"text"}}``. ``sent`` = a WhatsApp send
    the gateway accepted (template or in-window free text); ``degraded`` = the
    template was accepted-configured but the gateway rejected it (email is the
    real channel); ``failed`` = a free-text send raised.
    """
    from fashion_bot.agent_config import aget_escalation_contacts
    from fashion_bot.utils.phone_number_utils import is_real_phone_number

    trace_id = get_trace_id(state) if state else None
    parent_intent = state.get("parent_intent") if isinstance(state, dict) else None

    # Skip notification delivery when the customer has no contactable phone
    # (web chat with session ID only) AND no user-provided contact was collected.
    # The escalation is still logged to the DB by the caller (alog_escalation_from_state),
    # but sending a WhatsApp/email to staff with a non-reachable session ID is not actionable.
    customer_phone = state.get("phone_number") if isinstance(state, dict) else None
    if customer_phone and not is_real_phone_number(customer_phone) and not customer_contact_provided:
        logger.info(
            "[ESCALATION_NOTIFY] skipped: customer phone is a session ID (%s), not a real number",
            customer_phone[:20],
        )
        return {
            "sent": [], "failed": [], "degraded": [], "email_sent": [],
            "email_failed": [], "email_cc": [], "whatsapp_channel": {},
            "skipped_reason": "no_real_phone",
        }

    if contacts_override is not None:
        contacts = contacts_override
    else:
        try:
            contacts = await aget_escalation_contacts(
                client_id, agent=agent, category=category,
                parent_intent=parent_intent, order_id=order_id,
            )
        except Exception as e:
            logger.error(
                "[ESCALATION_NOTIFY] contact resolution failed: %s",
                e,
                extra={"trace_id": trace_id, "client_id": client_id},
            )
            contacts = {"phone": [], "email": {"to": [], "cc": []}}

    phones: List[str] = contacts.get("phone") or []
    email: Dict[str, List[str]] = contacts.get("email") or {"to": [], "cc": []}
    route_template: Optional[Dict[str, Any]] = contacts.get("template")

    if immediate_attention:
        notification = f"{IMMEDIATE_ATTENTION_BANNER}\n\n{notification}"

    # Assemble template params once (staff-alert template delivery is opt-in per
    # client via ``escalation_template``). ``details`` is the opt-in signal: only
    # escalations that carry a summary are eligible; the actual template vs
    # free-text decision happens per-recipient in ``_send_one`` and depends on
    # the client's config. ``None`` here ⇒ free-text only (prior behavior).
    template_fields: Optional[Dict[str, Any]] = None
    if details is not None:
        ts = timestamp_ist
        if not ts:
            try:
                import pytz
                from datetime import datetime

                ts = datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:  # pragma: no cover - defensive
                ts = ""
        customer_phone_for_template = (
            state.get("phone_number") if isinstance(state, dict) else None
        )
        # Last few customer messages for the template's conversation field —
        # derived from state so no call site needs re-threading (mirrors the
        # source the free-text notification's conversation block uses).
        recent_msgs: Optional[List[str]] = None
        if isinstance(state, dict):
            _msgs = state.get("messages") or []
            _human = [
                getattr(m, "content", m)
                for m in _msgs
                if getattr(m, "type", "human") == "human"
            ]
            recent_msgs = [str(m) for m in _human[-3:]] if _human else None
        template_fields = build_escalation_template_fields(
            category or "Escalation",
            order_id,
            customer_phone_for_template,
            details,
            escalation_group=escalation_group,
            immediate_attention=immediate_attention,
            customer_contact=customer_contact,
            trace_id=trace_id,
            timestamp_ist=ts,
            recent_customer_messages=recent_msgs,
        )

    # Escalation delivery is a side effect, not part of the customer reply, so it
    # runs through the queue/consumer architecture (AGENTS.md §6). WhatsApp and
    # email each get their own lane so a slow/failing channel never blocks the
    # conversation turn or the other channel (design §5.2a). The payloads are
    # fully JSON-serializable (no ``state``) so they can cross the broker. With a
    # lane disabled (default) that channel is delivered inline — byte-for-byte the
    # prior behaviour; with it enabled the actor delivers on the worker.
    subject = category or "Escalation"
    notify_id = uuid.uuid4().hex
    whatsapp_payload: Dict[str, Any] = {
        "notify_id": notify_id,
        "client_id": client_id,
        "trace_id": trace_id,
        "phones": phones,
        "notification": notification,
        "template_fields": template_fields,
        "immediate_attention": bool(immediate_attention),
        "route_template": route_template,
    }
    email_payload: Dict[str, Any] = {
        "notify_id": notify_id,
        "client_id": client_id,
        "trace_id": trace_id,
        "email": email,
        "notification": notification,
        "subject": subject,
        "email_html": email_html,
    }

    from fashion_bot.workers.event_publishers import publish_escalation_notification

    result: Dict[str, Any] = await publish_escalation_notification(
        whatsapp_payload=whatsapp_payload,
        email_payload=email_payload,
    )

    result.setdefault("sent", [])
    result.setdefault("failed", [])
    result.setdefault("degraded", [])
    result.setdefault("email_sent", [])
    result.setdefault("email_failed", [])
    result.setdefault("email_cc", list(email.get("cc") or []))
    result.setdefault("whatsapp_channel", {})

    was_queued = result.get("queued", False)
    if was_queued:
        log_with_trace_id(
            state,
            f"[ESCALATION_NOTIFY] queued for async delivery — "
            f"whatsapp_phones={len(phones)} email_to={len(email.get('to') or [])} "
            f"notify_id={notify_id}",
        )
    else:
        _via_template = sum(1 for c in result["whatsapp_channel"].values() if c == "template")
        _via_text = sum(1 for c in result["whatsapp_channel"].values() if c == "text")
        log_with_trace_id(
            state,
            f"[ESCALATION_NOTIFY] whatsapp_sent={len(result['sent'])} "
            f"whatsapp_failed={len(result['failed'])} whatsapp_degraded={len(result['degraded'])} "
            f"via_template={_via_template} via_text={_via_text} "
            f"email_to={len(result['email_sent'])}",
        )
    return result


async def asend_escalation_whatsapp(
    *,
    phones: List[str],
    notification: str,
    client_id: Optional[str] = None,
    template_fields: Optional[Dict[str, Any]] = None,
    immediate_attention: bool = False,
    trace_id: Optional[str] = None,
    notify_id: Optional[str] = None,
    route_template: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """State-free WhatsApp fan-out for one escalation (design §5.2a).

    Template-first with a free-text fallback per the opt-in ``escalation_template``
    config: a gateway accept/reject never also sends free text, so a staff number
    is never double-alerted. Per-recipient isolation — one number's failure never
    suppresses the others. Called both inline (lane disabled) and from the
    ``escalation_whatsapp`` actor (lane enabled); it reads no ``state`` so its
    arguments are JSON-serializable. ``notify_id`` is accepted for correlation
    (dedup is the actor's job) and otherwise unused.

    ``route_template``, when set, carries the per-route template config resolved
    from the ``escalation_contact`` routing nodes (template_id, param_order,
    image_url).  It takes precedence over the global ``escalation_template``
    config key inside ``amaybe_send_escalation_template``.

    Returns ``{"sent": [...], "failed": [...], "degraded": [...],
    "whatsapp_channel": {num: "template"|"text"}}``.
    """
    from fashion_bot.gupshup_webhook import send_message
    from fashion_bot.utils.escalation_template_sender import (
        TemplateSendOutcome,
        amaybe_send_escalation_template,
    )

    result: Dict[str, Any] = {"sent": [], "failed": [], "degraded": [], "whatsapp_channel": {}}
    if not phones:
        return result

    async def _send_one(num: str):
        # Template-first (only when the client opted in AND this escalation
        # supplied structured fields). We fall back to free text ONLY when the
        # template gateway was never hit — a gateway accept/reject never also
        # sends free text, so a staff number is never double-alerted.
        if template_fields:
            try:
                outcome = await amaybe_send_escalation_template(
                    to=num,
                    client_id=client_id,
                    template_fields=template_fields,
                    immediate_attention=immediate_attention,
                    trace_id=trace_id,
                    route_template=route_template,
                )
            except Exception as e:  # defensive: treat as pre-send failure
                logger.warning(
                    "[ESCALATION_NOTIFY] template send errored for %s: %s", num, e
                )
                outcome = TemplateSendOutcome.FAILED_PRESEND
            if outcome == TemplateSendOutcome.SENT:
                return ("sent", num, "template")
            if outcome == TemplateSendOutcome.GATEWAY_FAILED:
                # Accepted by config but rejected by the gateway — do NOT send
                # free text (it would be dropped outside the window anyway).
                # Email remains the guaranteed channel.
                return ("degraded", num, "template")
            # NOT_CONFIGURED / FAILED_PRESEND → free-text default & fallback.

        try:
            await send_message(num, notification, trace_id=trace_id, client_id=client_id)
            return ("sent", num, "text")
        except Exception as e:
            logger.error(
                "[ESCALATION_NOTIFY] WhatsApp send failed to %s: %s",
                num,
                e,
                extra={"trace_id": trace_id, "client_id": client_id},
            )
            return ("failed", num, "text")

    for status, num, channel in await asyncio.gather(*[_send_one(n) for n in phones]):
        result["whatsapp_channel"][num] = channel
        if status == "sent":
            result["sent"].append(num)
        elif status == "degraded":
            result["degraded"].append(num)
        else:
            result["failed"].append(num)
    return result


async def asend_escalation_email(
    *,
    email: Dict[str, List[str]],
    notification: str,
    subject: str,
    client_id: Optional[str] = None,
    email_html: Optional[str] = None,
    trace_id: Optional[str] = None,
    notify_id: Optional[str] = None,
) -> Dict[str, Any]:
    """State-free email fan-out for one escalation (design §5.2a).

    Emails the ``to`` recipients (with ``cc`` in the Cc header). Called both
    inline (lane disabled) and from the ``escalation_email`` actor (lane enabled).

    Returns ``{"email_sent": [...], "email_failed": [...], "email_cc": [...]}``.
    """
    email = email or {"to": [], "cc": []}
    result: Dict[str, Any] = {
        "email_sent": [],
        "email_failed": [],
        "email_cc": list(email.get("cc") or []),
    }
    to = email.get("to") or []
    if not to:
        return result
    try:
        await _asend_escalation_email(
            notification,
            email,
            subject=subject,
            trace_id=trace_id,
            html_body=email_html,
        )
        result["email_sent"] = list(to)
    except Exception as e:
        logger.error(
            "[ESCALATION_NOTIFY] email send failed: %s",
            e,
            extra={"trace_id": trace_id, "client_id": client_id},
        )
        result["email_failed"] = list(to)
    return result


async def alog_escalation_from_state(
    state: SupportState,
    category: str,
    reason: str,
    action_required: str,
    user_messages: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    configuration_gap: Optional[str] = None,
) -> Optional[str]:
    """
    Async variant of log_escalation_from_state for request-path usage.
    """
    try:
        from fashion_bot.utils.escalation_logger import alog_escalation

        gupshup_source_phone_number = state.get("gupshup_source_phone_number")
        client_id = state.get("client_id") or state.get("context", {}).get("client_id")
        if not client_id:
            client_id = await aresolve_client_id(gupshup_source_phone_number)
        phone_number = state.get("phone_number")
        conversation_id = state.get("conversation_id") or state.get("context", {}).get("conversation_id")
        channel = resolve_channel_from_state(state)
        trace_id = get_trace_id(state)

        if user_messages:
            cleaned_messages = []
            for msg in user_messages:
                if isinstance(msg, str):
                    clean_msg = msg
                elif hasattr(msg, "content"):
                    clean_msg = msg.content
                else:
                    clean_msg = str(msg)
                clean_msg = clean_msg.replace(" additional_kwargs={}", "")
                clean_msg = clean_msg.replace(" response_metadata={}", "")
                clean_msg = clean_msg.strip()
                if clean_msg:
                    cleaned_messages.append(clean_msg)
            formatted_messages = [f"[Message {idx}]: {msg}" for idx, msg in enumerate(cleaned_messages, 1)]
            formatted_whatsapp_message = "\n\n".join(formatted_messages)
        else:
            formatted_whatsapp_message = None

        # Central metadata enrichment — guarantees EVERY escalation path (LLM
        # hand-off, auto delivery escalations, cancellation threat, delivery
        # partner sync, …) logs the six canonical fields, including
        # ``escalation_group`` (pre_sales / post_sales / offline_leads) and
        # ``immediate_attention``. Control keys are consumed from the incoming
        # metadata; a caller-supplied ``escalation_type`` that differs from the
        # category slug is preserved as ``escalation_subtype`` (non-lossy).
        incoming_meta = dict(metadata or {})
        _classification = incoming_meta.pop("escalation_classification", None) or "system"
        _immediate = bool(incoming_meta.pop("immediate_attention", False))
        _agent = incoming_meta.pop("agent", None)
        _caller_type = incoming_meta.pop("escalation_type", None)
        _category_slug = (category or "general").lower().replace(" ", "_")
        if _caller_type and _caller_type != _category_slug:
            incoming_meta.setdefault("escalation_subtype", _caller_type)
        enriched_metadata = await build_escalation_metadata(
            client_id=client_id,
            category=category,
            trace_id=trace_id,
            phone_number=str(phone_number) if phone_number else None,
            escalation_classification=_classification,
            immediate_attention=_immediate,
            agent=_agent,
            extra=incoming_meta,
        )

        escalation_id = await alog_escalation(
            client_id=client_id,
            customer_phone=str(phone_number) if phone_number else "",
            category=category,
            reason=reason,
            whatsapp_message=formatted_whatsapp_message,
            conversation_id=conversation_id,
            channel=channel,
            action_required=action_required,
            configuration_gap=configuration_gap,
            metadata=enriched_metadata,
        )

        if escalation_id:
            # Drop the cached escalation snapshot so this escalation is visible in
            # the agent's context on the very next turn instead of after the TTL.
            # Best-effort: a failure here must never affect the turn.
            from fashion_bot.utils.escalation_context import abust_escalation_snapshot

            await abust_escalation_snapshot(client_id, phone_number)

            from fashion_bot.workers.event_publishers import (
                fire_and_forget,
                publish_escalation_event,
            )

            fire_and_forget(
                publish_escalation_event(
                    {
                        "escalation_id": escalation_id,
                        "client_id": client_id,
                        "customer_phone": str(phone_number) if phone_number else "",
                        "conversation_id": conversation_id,
                        "channel": channel,
                        "category": category,
                        "reason": reason,
                        "action_required": action_required,
                        "metadata": enriched_metadata,
                        "trace_id": trace_id,
                    }
                ),
                label="escalation_event",
            )
            log_with_trace_id(state, f"[ESCALATION_LOG] ✅ {category} escalation logged: {escalation_id}")
        else:
            log_with_trace_id(state, f"[ESCALATION_LOG] ⚠️ Failed to log {category} escalation")

        return escalation_id
    except Exception as e:
        log_with_trace_id(state, f"[ESCALATION_LOG] ❌ Error logging {category} escalation: {e}", "error")
        return None


def resolve_channel_from_state(state: SupportState) -> Optional[str]:
    """
    Resolve the delivery channel the user's message came from, normalized to the
    canonical escalation channel values: ``"whatsapp"`` and ``"web-chat"``.

    State does not carry an explicit ``channel`` key everywhere, so fall back to
    the channel-specific markers set by the channel adapters / state cache:
      - WhatsApp state carries ``gupshup_source_phone_number`` (the business number).
      - Web state carries ``session_id``.
    """
    channel = state.get("channel") or state.get("context", {}).get("channel")
    if not channel:
        if state.get("gupshup_source_phone_number"):
            channel = "whatsapp"
        elif state.get("session_id"):
            channel = "web"
    if not channel:
        return None

    normalized = str(channel).strip().lower()
    if normalized in ("whatsapp", "wa", "gupshup"):
        return "whatsapp"
    if normalized in ("web", "web-chat", "webchat", "websocket"):
        return "web-chat"
    return normalized


# Backwards-compatible private alias (kept so existing imports keep working).
_resolve_channel_from_state = resolve_channel_from_state


# ──────────────────────────────────────────────────────────────────────────
# Customer-facing escalation footer
# ──────────────────────────────────────────────────────────────────────────

_FOOTER_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_footer_text(text: Optional[str]) -> str:
    """Casefolded, whitespace-collapsed form used to compare footer text.

    The model reproduces the footer from history with its own spacing, so an
    exact ``==`` would miss a duplicate that reads identically to the customer.
    """
    return _FOOTER_WHITESPACE_RE.sub(" ", text or "").strip().casefold()


def append_escalation_footer(message: Optional[str], footer: Optional[str]) -> str:
    """Return ``message`` with ``footer`` present exactly once, at the end.

    The footer (``client_configs.escalation_success_footer``) is stitched on
    after the LLM has generated, so it lands in the stored transcript and is
    replayed as history on the customer's next turn. The model learns it as the
    house closing line and writes it itself — a blind ``+=`` then sends the
    customer the same sentence twice (three times when the model repeats it).

    Idempotent by construction: converge on the end state rather than appending
    conditionally. Footer-only paragraphs are stripped off the tail and one
    footer is re-appended; a footer the model wove into the body is left where
    it is and never duplicated.
    """
    body = message or ""
    normalized_footer = _normalize_footer_text(footer)
    if not normalized_footer:
        return body

    # Drop trailing footer-only (and empty) paragraphs — whether the model wrote
    # them or an earlier append did — so repeats collapse instead of stacking.
    blocks = body.split("\n\n")
    while blocks and _normalize_footer_text(blocks[-1]) in ("", normalized_footer):
        blocks.pop()
    body = "\n\n".join(blocks).rstrip()

    if normalized_footer in _normalize_footer_text(body):
        # Already said inside the reply itself — saying it again adds nothing.
        return body
    return f"{body}\n\n{footer}" if body else str(footer)


def format_user_messages(
    user_messages: List[str]
) -> Optional[str]:
    """
    Format a list of user message strings with standardized numbering.
    
    Args:
        user_messages: List of user message strings (typically last 3 messages)
        
    Returns:
        Formatted string with [Message 1]:, [Message 2]:, etc., or None if no messages
        
    Example:
        user_msgs = [m.content for m in messages_list if getattr(m, "type", "human") == "human"][-3:]
        formatted = format_user_messages(user_msgs)
        # Returns: "[Message 1]: text\n\n[Message 2]: text\n\n[Message 3]: text"
    """
    if not user_messages:
        return None
    
    # Clean messages to ensure only text content (no metadata)
    cleaned_messages = []
    for msg in user_messages:
        # If msg is a string, use it; if it's an object with content, extract it
        if isinstance(msg, str):
            clean_msg = msg
        elif hasattr(msg, 'content'):
            clean_msg = msg.content
        else:
            clean_msg = str(msg)
        
        # Remove any LangChain metadata artifacts
        clean_msg = clean_msg.replace(" additional_kwargs={}", "")
        clean_msg = clean_msg.replace(" response_metadata={}", "")
        clean_msg = clean_msg.strip()
        
        if clean_msg:  # Only add non-empty messages
            cleaned_messages.append(clean_msg)
    
    formatted_messages = [f"[Message {idx}]: {msg}" for idx, msg in enumerate(cleaned_messages, 1)]
    
    return "\n\n".join(formatted_messages)


def build_escalation_notification(
    category: str,
    order_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str],
    details: str,
    timestamp_ist: str,
    recent_customer_messages: Optional[List[str]] = None,
    escalation_group: Optional[str] = None,
    customer_name: Optional[str] = None,
    customer_contact: Optional[str] = None,
) -> str:
    """
    Build a formatted escalation notification message for WhatsApp.
    
    Args:
        category: Type of escalation (e.g., "Return Request", "Exchange Request")
        order_id: Order ID being escalated
        phone_number: Customer's phone number
        trace_id: Trace ID for debugging
        details: Full details for the agent
        timestamp_ist: Current timestamp in IST
        recent_customer_messages: Last N customer messages for context
        escalation_group: Operational group (pre_sales / post_sales / offline_leads)
        customer_name: Customer's name if available
        customer_contact: User-provided contact override (phone or email from web chat)
        
    Returns:
        Formatted notification string
    """
    from fashion_bot.utils.phone_number_utils import strip_country_code, is_real_phone_number
    
    if customer_contact:
        clean_phone = customer_contact
    elif phone_number and is_real_phone_number(phone_number):
        clean_phone = strip_country_code(phone_number)
    else:
        clean_phone = "Not provided (web chat)"
    
    group_label = (escalation_group or "pre_sales").replace("_", " ").title()
    
    # Split timestamp into date and time parts for cleaner display
    date_part, time_part = timestamp_ist.rsplit(" ", 1) if " " in timestamp_ist else (timestamp_ist, "")

    notification = f"""🔔 *{category}*
━━━━━━━━━━━━━━━━━━━━

⏰  {date_part} {time_part} IST
👤  Name: {customer_name or 'Unknown'}
📱  Contact: {clean_phone}
🔢  Order ID: {order_id or 'Unknown'}
🏷️  Type: {group_label}

━━━━━━━━━━━━━━━━━━━━
📋 *SUMMARY*

{details}"""

    if recent_customer_messages:
        msgs = "\n".join(f"   › {m}" for m in recent_customer_messages[-3:])
        notification += f"""

━━━━━━━━━━━━━━━━━━━━
💬 *CUSTOMER CONVERSATION*

{msgs}"""

    return notification


def build_escalation_template_fields(
    category: str,
    order_id: Optional[str],
    phone_number: Optional[str],
    details: str,
    *,
    escalation_group: Optional[str] = None,
    immediate_attention: bool = False,
    customer_contact: Optional[str] = None,
    customer_name: Optional[str] = None,
    trace_id: Optional[str] = None,
    timestamp_ist: str = "",
    recent_customer_messages: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Canonical, sanitized, **never-empty** fields for escalation template params.

    Mirrors the arguments of :func:`build_escalation_notification` so callers can
    build it from the same locals. Every value is a single-line, length-capped,
    **non-empty** string:

    * never ``None`` — a ``None`` for a non-dynamic param key would make
      ``resolve_template_params`` raise and abort the template send; and
    * never empty (``""``) — WhatsApp/Meta rejects a template send whose
      parameter value is blank, so optional fields fall back to a safe token
      (``"N/A"`` / ``"Normal"``). This lets a template reference *any* of these
      fields (e.g. a dedicated Order ID or Urgency line) without risking a
      blank-parameter rejection on web-chat / non-urgent / no-order escalations.

    The returned dict is passed to
    :func:`asend_escalation_notification`'s ``template_fields`` and mapped onto
    ``var1..varN`` via the client-configured ``param_order``. ``priority`` carries
    urgency (``"URGENT"`` / ``"Normal"``) so a single approved template serves both
    normal and immediate-attention escalations.
    """
    from fashion_bot.utils.escalation_template_sender import (
        sanitize_template_param,
        sanitize_template_summary,
    )
    from fashion_bot.utils.phone_number_utils import strip_country_code, is_real_phone_number

    if customer_contact:
        contact = customer_contact
    elif phone_number and is_real_phone_number(phone_number):
        contact = strip_country_code(phone_number)
    else:
        contact = "web chat"

    group = (escalation_group or "pre_sales")

    convo = " / ".join(
        str(m).strip() for m in (recent_customer_messages or []) if str(m).strip()
    )

    return {
        "priority": "URGENT" if immediate_attention else "Normal",
        "category": sanitize_template_param(category or "Escalation") or "Escalation",
        "customer_name": sanitize_template_param(customer_name or "") or "N/A",
        "customer_contact": sanitize_template_param(contact) or "web chat",
        "order_id": sanitize_template_param(order_id or "") or "N/A",
        "escalation_group": sanitize_template_param(group.replace("_", " ").title()) or "Pre Sales",
        "summary": sanitize_template_summary(details or "") or "No additional details",
        "conversation": sanitize_template_summary(convo, cap=400) or "N/A",
        "trace_id": sanitize_template_param(trace_id or "") or "N/A",
        "timestamp_ist": sanitize_template_param(timestamp_ist or "") or "N/A",
    }


def build_escalation_email_html(
    category: str,
    order_id: Optional[str],
    phone_number: Optional[str],
    details: str,
    timestamp_ist: str,
    recent_customer_messages: Optional[List[str]] = None,
    escalation_group: Optional[str] = None,
    immediate_attention: bool = False,
    customer_name: Optional[str] = None,
    customer_contact: Optional[str] = None,
) -> str:
    """Build a styled HTML email for the escalation notification."""
    from fashion_bot.utils.phone_number_utils import strip_country_code, is_real_phone_number
    from datetime import datetime

    if customer_contact:
        clean_phone = customer_contact
    elif phone_number and is_real_phone_number(phone_number):
        clean_phone = strip_country_code(phone_number)
    else:
        clean_phone = "Not provided (web chat)"

    group_label = (escalation_group or "pre_sales").replace("_", " ").title()
    date_part, time_part = timestamp_ist.rsplit(" ", 1) if " " in timestamp_ist else (timestamp_ist, "")
    current_year = datetime.now().year
    import html as html_mod
    safe_details = html_mod.escape(details)

    attention_banner = ""
    if immediate_attention:
        attention_banner = """
        <div style="background-color:#FEF2F2;border:1px solid #FCA5A5;border-radius:8px;padding:12px 16px;margin-bottom:24px;text-align:center;">
            <span style="font-size:18px;font-weight:700;color:#DC2626;">⚠️ IMMEDIATE ATTENTION REQUIRED ⚠️</span>
        </div>"""

    conversation_html = ""
    if recent_customer_messages:
        msgs_html = ""
        for msg in recent_customer_messages[-3:]:
            safe_msg = html_mod.escape(msg)
            msgs_html += f'<div style="padding:8px 12px;margin-bottom:6px;background-color:#F0F4FF;border-radius:6px;font-size:14px;color:#1E293B;">💬 {safe_msg}</div>'
        conversation_html = f"""
        <div style="margin-top:24px;">
            <div style="font-size:14px;font-weight:700;color:#1E293B;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px;">Customer Conversation</div>
            <div style="border:1px solid #E2E8F0;border-radius:8px;padding:12px;background-color:#FAFBFC;">
                {msgs_html}
            </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1.0"></head>
<body style="margin:0;padding:0;background-color:#F1F5F9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background-color:#F1F5F9;padding:32px 16px;">
<tr><td align="center">
<table width="600" cellpadding="0" cellspacing="0" style="background-color:#FFFFFF;border-radius:12px;overflow:hidden;box-shadow:0 4px 6px rgba(0,0,0,0.05);">

    <!-- Header -->
    <tr>
        <td style="background:linear-gradient(135deg,#1E293B 0%,#334155 100%);padding:24px 32px;">
            <span style="font-size:20px;font-weight:700;color:#FFFFFF;">🔔 Escalation Alert</span>
        </td>
    </tr>

    <!-- Body -->
    <tr>
        <td style="padding:32px;">
            {attention_banner}

            <!-- Meta Info -->
            <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;width:40%;">🔔 Category</td><td style="padding:6px 0;font-size:14px;font-weight:600;color:#1E293B;">{html_mod.escape(category)}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;">⏰ Date &amp; Time</td><td style="padding:6px 0;font-size:14px;color:#1E293B;">{date_part} {time_part} IST</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;">👤 Name</td><td style="padding:6px 0;font-size:14px;color:#1E293B;">{html_mod.escape(customer_name or 'Unknown')}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;">📱 Contact</td><td style="padding:6px 0;font-size:14px;color:#1E293B;">{html_mod.escape(clean_phone)}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;">🔢 Order ID</td><td style="padding:6px 0;font-size:14px;color:#1E293B;">{html_mod.escape(order_id or 'Unknown')}</td></tr>
                <tr><td style="padding:6px 0;font-size:14px;color:#64748B;">🏷️ Type</td><td style="padding:6px 0;font-size:14px;color:#1E293B;">{html_mod.escape(group_label)}</td></tr>
            </table>

            <!-- Summary -->
            <div style="margin-top:24px;">
                <div style="font-size:14px;font-weight:700;color:#1E293B;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px;">Summary</div>
                <div style="font-size:14px;color:#334155;line-height:1.6;white-space:pre-wrap;">{safe_details}</div>
            </div>

            <!-- Conversation -->
            {conversation_html}
        </td>
    </tr>

    <!-- Footer -->
    <tr>
        <td style="background-color:#F8FAFC;padding:20px 32px;border-top:1px solid #E2E8F0;">
            <p style="margin:0 0 4px;font-size:12px;color:#94A3B8;">This is an automated escalation generated by the Bloomerce AI CX Agent. Please do not reply directly to this email.</p>
            <p style="margin:0;font-size:12px;color:#94A3B8;">© {current_year} Bloomerce · Automated Customer Experience System</p>
        </td>
    </tr>

</table>
</td></tr>
</table>
</body>
</html>"""


def prepare_escalation_metadata(
    order_id: Optional[str],
    phone_number: Optional[str],
    trace_id: Optional[str],
    escalation_type: str = "general",
    escalation_classification: Optional[str] = None,
    additional_data: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """
    Prepare metadata dictionary for escalation logging.
    
    Args:
        order_id: Order ID being escalated
        phone_number: Customer's phone number
        trace_id: Trace ID for debugging
        escalation_type: Type of escalation
        escalation_classification: Classification label (defaults to escalation_type)
        additional_data: Any additional metadata to include
        
    Returns:
        Metadata dictionary for escalation logging
    """
    metadata = {
        "order_id": order_id,
        "phone_number": phone_number,
        "trace_id": trace_id,
        "escalation_type": escalation_type,
        "escalation_classification": escalation_classification or escalation_type,
    }
    
    if additional_data:
        metadata.update(additional_data)
    
    return metadata
