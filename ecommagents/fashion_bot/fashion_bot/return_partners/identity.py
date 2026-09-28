"""Customer identity checks for return/exchange workflows.

All customer-visible return/exchange lookups should pass through this module
before partner data is returned. The helpers are stateless and async-only.
"""

from __future__ import annotations

from typing import Any

from fashion_bot.return_partners.models import ReturnIdentityResult
from fashion_bot.tool_helpers import validate_phone_number_access
from fashion_bot.utils.phone_number_utils import get_last_n_digits, is_real_phone_number
from fashion_bot.utils.utils import log_with_trace_id


def _normalize_phone(value: Any) -> str:
    """Last 10 digits of a phone number.

    Thin wrapper over ``phone_number_utils.get_last_n_digits`` so the
    digit-stripping rule lives in exactly one place and the order tools (Gate A)
    and the return-partner tools (Gate B) cannot drift apart. The only thing kept
    local is the ``str()`` coercion: Shopify order payloads occasionally carry
    numeric phone fields, which the shared helper does not accept.
    """
    return get_last_n_digits(str(value or ""))


def _normalize_email(value: Any) -> str:
    return str(value or "").strip().lower()


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _state_phone(state: dict | None) -> str | None:
    state = state or {}
    return _first_non_empty(
        state.get("phone_number"),
        state.get("customer_phone"),
        state.get("phone"),
        state.get("user_phone"),
        state.get("whatsapp_phone"),
    )


def _state_email(state: dict | None) -> str | None:
    state = state or {}
    return _first_non_empty(
        state.get("customer_email"),
        state.get("email"),
        state.get("user_email"),
    )


def _collect_order_phones(order: dict) -> dict[str, str]:
    candidates: list[Any] = [
        order.get("phone"),
        order.get("customer_phone"),
        order.get("billing_phone"),
    ]
    for key in ("shipping_address", "billing_address", "customer", "default_address"):
        value = order.get(key)
        if isinstance(value, dict):
            candidates.append(value.get("phone"))
            default_address = value.get("default_address")
            if isinstance(default_address, dict):
                candidates.append(default_address.get("phone"))

    collected: dict[str, str] = {}
    for candidate in candidates:
        normalized = _normalize_phone(candidate)
        if len(normalized) == 10:
            collected.setdefault(normalized, str(candidate))
    return collected


def _collect_order_emails(order: dict) -> set[str]:
    customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
    return {
        email
        for email in {
            _normalize_email(order.get("email")),
            _normalize_email(order.get("customer_email")),
            _normalize_email(customer.get("email")),
        }
        if email
    }


async def _aget_order(client_id: str, order_number: str, state: dict | None) -> dict:
    from fashion_bot.core.factory import ServiceFactory

    order_service = await ServiceFactory.aget_order_service(
        client_id=client_id,
        state=state,
        vendor="shopify",
    )
    return await order_service.aget_order_details(order_number, state=state) or {}


async def averify_order_identity(
    *,
    client_id: str,
    order_number: str,
    state: dict | None = None,
    order: dict | None = None,
    customer_phone: str | None = None,
    customer_email: str | None = None,
    require_identity: bool = True,
) -> dict:
    """Verify that the customer owns the order before exposing return data."""

    provided_phone = customer_phone or _state_phone(state)
    provided_email = customer_email or _state_email(state)

    # A web-chat session id is not an identity.
    #
    # When a visitor never shares a phone number, ``state["phone_number"]`` holds
    # the chat widget's session id (``fbw_...`` / ``web_...``) rather than a real
    # number. ``_normalize_phone`` below strips that to its digits, which yields a
    # short but *truthy* string ("fbw_msoa5322pp8k876dt" -> "5322876"). That value
    # then skipped the "nothing was provided" branch further down and fell through
    # to the mismatch branch instead — so an anonymous visitor who had shared
    # nothing was told "The details shared do not match this order", with
    # ``needs_identity`` left False, and was never asked for a phone number at all.
    #
    # Discarding a non-phone here routes it to the needs_identity branch, which is
    # the behaviour the order tools already have via the ``is_real_phone_number``
    # check in ``_avalidate_phone_for_order_access``.
    if provided_phone and not is_real_phone_number(str(provided_phone)):
        provided_phone = None

    try:
        order_data = order or await _aget_order(client_id, order_number, state)
    except Exception as exc:
        log_with_trace_id(
            state,
            f"[RETURN_IDENTITY] order lookup failed order={order_number}: {exc}",
            "error",
        )
        return ReturnIdentityResult(
            success=False,
            verified=False,
            should_block=True,
            order_number=order_number,
            message="I couldn't verify this order right now. Please contact support.",
            failed_reason="order_lookup_failed",
        ).model_dump()

    if not order_data:
        return ReturnIdentityResult(
            verified=False,
            should_block=True,
            order_number=order_number,
            message=f"I couldn't find order {order_number}.",
            failed_reason="order_not_found",
        ).model_dump()

    if not require_identity:
        return ReturnIdentityResult(
            verified=True,
            should_block=False,
            matched_on="bypass",
            order_number=order_number,
            order=order_data,
            message="Identity verification bypassed by configuration.",
        ).model_dump()

    order_emails = _collect_order_emails(order_data)
    normalized_email = _normalize_email(provided_email)
    if normalized_email and normalized_email in order_emails:
        return ReturnIdentityResult(
            verified=True,
            should_block=False,
            matched_on="email",
            order_number=order_number,
            order=order_data,
            provided_email=normalized_email,
            order_email=normalized_email,
            message="Email verified for this order.",
        ).model_dump()

    order_phones = _collect_order_phones(order_data)
    normalized_phone = _normalize_phone(provided_phone)
    if normalized_phone and any(
        validate_phone_number_access(normalized_phone, order_phone)
        for order_phone in order_phones
    ):
        return ReturnIdentityResult(
            verified=True,
            should_block=False,
            matched_on="phone",
            order_number=order_number,
            order=order_data,
            provided_phone=normalized_phone,
            message="Phone number verified for this order.",
        ).model_dump()

    if not normalized_phone and not normalized_email:
        return ReturnIdentityResult(
            verified=False,
            needs_identity=True,
            should_block=True,
            matched_on="not_provided",
            order_number=order_number,
            # No `order` here: this result is returned straight to the LLM as a
            # tool result on every should_block path (see orchestrator.py), so
            # embedding the full order (address, line items, contact info)
            # in a "you're not verified yet" response would leak exactly the
            # data identity verification exists to protect.
            message="Please share the phone number or email linked to this order so I can verify it.",
            failed_reason="identity_required",
        ).model_dump()

    sample_phone = next(iter(order_phones.values()), "")
    masked = sample_phone[-4:] if len(str(sample_phone)) >= 4 else None
    message = "The details shared do not match this order."
    if masked:
        message = f"The details shared do not match this order. The order phone ends in {masked}."
    return ReturnIdentityResult(
        verified=False,
        should_block=True,
        matched_on="not_matched",
        order_number=order_number,
        # See note above — no `order`, and the email stays unset (only the
        # already-masked last-4-digits phone is safe to echo back).
        provided_phone=normalized_phone or None,
        provided_email=normalized_email or None,
        masked_order_phone=masked,
        message=message,
        failed_reason="identity_mismatch",
    ).model_dump()

