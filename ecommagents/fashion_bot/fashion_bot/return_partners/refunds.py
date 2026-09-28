"""Refund visibility layer for return/exchange conversations.

This module deliberately does not execute refunds. It reports what is known
from partner/Shopify/manual sources and falls back to configured SLA guidance.
"""

from __future__ import annotations

import re
from datetime import timezone
from typing import Any

from fashion_bot.config_manager import aget_json_config
from fashion_bot.return_partners.models import RefundStatusResult
from fashion_bot.return_partners.sla import evaluate_sla, parse_datetime

REFUND_POLICY_QA_CONFIG_KEY = "return_exchange_policy"


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _refund_config(rules_config: dict, request_type: str | None) -> dict:
    root = rules_config.get("refund") if isinstance(rules_config.get("refund"), dict) else {}
    type_rules = rules_config.get(request_type or "return")
    type_refund = type_rules.get("refund") if isinstance(type_rules, dict) and isinstance(type_rules.get("refund"), dict) else {}
    return {**root, **type_refund}


def _anchor_from_request(request: dict) -> Any:
    for key in (
        "refund_initiated_at",
        "refunded_at",
        "approved_at",
        "received_at",
        "updated_at",
        "created_at",
    ):
        value = request.get(key)
        if value:
            return value
    checkpoints = request.get("status_checkpoints") if isinstance(request.get("status_checkpoints"), dict) else {}
    for key in ("approved", "received", "inspected"):
        value = checkpoints.get(key)
        if isinstance(value, dict):
            return _first_non_empty(value.get("at"), value.get("date"), value.get("updated_at"))
    raw = request.get("raw") if isinstance(request.get("raw"), dict) else {}
    return _first_non_empty(
        raw.get("refund_initiated_at"),
        raw.get("approved_at"),
        raw.get("updated_at"),
        raw.get("created_at"),
    )


def _classify_partner_refund(refund_value: Any) -> tuple[str | None, str | int | float | None, str | None]:
    if isinstance(refund_value, dict):
        status = str(_first_non_empty(refund_value.get("status"), refund_value.get("state")) or "").lower()
        amount = _first_non_empty(refund_value.get("amount"), refund_value.get("refund_amount"))
        currency = refund_value.get("currency")
        if any(token in status for token in ("complete", "processed", "success", "paid")):
            return "processed", amount, currency
        if any(token in status for token in ("fail", "reject")):
            return "failed", amount, currency
        if any(token in status for token in ("initiat", "process", "pending")):
            return "initiated", amount, currency
        return (status or None), amount, currency
    if refund_value not in (None, "", {}, []):
        return "initiated", refund_value, None
    return None, None, None


def _first_line_item_refund(request: dict) -> Any:
    for item in request.get("line_items") or []:
        if isinstance(item, dict) and item.get("refund") not in (None, "", {}, []):
            return item.get("refund")
    return None


def _partner_refund_value(request: dict) -> Any:
    return _first_non_empty(request.get("refund"), _first_line_item_refund(request))


def _normalize_destination(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if text == "wallet":
        return "wallet"
    if text in {"store_credit", "store credit", "credit", "nector", "points"}:
        return "store_credit"
    if text in {"source", "original_source", "original payment source", "original_payment_source", "bank", "card"}:
        return "source"
    return text


def _destination_from_partner_refund(refund_value: Any) -> str | None:
    if not isinstance(refund_value, dict):
        return None
    return _normalize_destination(
        _first_non_empty(
            refund_value.get("requested_mode"),
            refund_value.get("mode"),
            refund_value.get("refund_mode"),
            refund_value.get("destination"),
            refund_value.get("type"),
        )
    )


def _destination_label(destination: str | None) -> str:
    if destination in {"wallet", "store_credit"}:
        return "store credit" if destination == "store_credit" else "wallet"
    return "original payment source"


def _max_days(text: str | None) -> int | None:
    numbers = [int(n) for n in re.findall(r"\d+", text or "")]
    return max(numbers) if numbers else None


async def _refund_policy_qa_fallback(client_id: str) -> tuple[str | None, str | None, int | None]:
    """When no structured ``refund`` config exists, fall back to the free-text
    answer already configured for this client in ``return_exchange_policy``
    (the same Q&A source the policy agent reads from) rather than guessing.

    Returns (message, destination, sla_days) — any of which may be ``None``.
    """
    qa_config = await aget_json_config(REFUND_POLICY_QA_CONFIG_KEY, client_id=client_id) or {}
    if not isinstance(qa_config, dict):
        return None, None, None
    for question, answer in qa_config.items():
        normalized_question = str(question or "").strip().lower()
        if "refund" in normalized_question and ("when" in normalized_question or "money" in normalized_question):
            text = str(answer or "").strip()
            if not text:
                continue
            destination = "wallet" if "wallet" in text.lower() else None
            return text, destination, _max_days(text)
    return None, None, None


async def aget_refund_visibility(
    *,
    client_id: str,
    request: dict | None,
    request_type: str | None = "return",
) -> dict:
    rules_config = await aget_json_config("return_exchange_rules", client_id=client_id) or {}
    refund_cfg = _refund_config(rules_config, request_type)
    visibility_mode = str(refund_cfg.get("visibility_mode") or refund_cfg.get("mode") or "none").lower()
    request = request or {}
    partner_refund = _partner_refund_value(request)
    partner_destination = _destination_from_partner_refund(partner_refund)

    # No structured `refund` policy configured — fall back to the free-text
    # answer already set up in return_exchange_policy instead of defaulting
    # to "original payment source" / 7 days, which may not reflect this
    # client's actual policy (e.g. wallet-only refunds).
    policy_qa_message = policy_qa_destination = None
    policy_qa_sla_days = None
    if not refund_cfg:
        policy_qa_message, policy_qa_destination, policy_qa_sla_days = await _refund_policy_qa_fallback(client_id)

    configured_destination = _normalize_destination(
        refund_cfg.get("default_destination")
        or refund_cfg.get("destination")
        or policy_qa_destination
        or "source"
    )
    destination = partner_destination or configured_destination or "source"

    partner_status, amount, currency = _classify_partner_refund(partner_refund)
    if visibility_mode == "return_partner" and partner_status:
        status = partner_status if partner_status in {"pending", "initiated", "processed", "failed"} else "pending"
        return RefundStatusResult(
            status=status,
            confidence="partner_reported",
            source="return_partner",
            destination=destination,
            amount=amount,
            currency=currency,
            message=(
                "The return partner reports that your refund is processed."
                if status == "processed"
                else "The return partner reports that your refund is under process."
            ),
            raw={"refund": partner_refund, "partner_destination": partner_destination},
        ).model_dump()

    anchor_at = parse_datetime(_anchor_from_request(request))
    if anchor_at:
        anchor_at = anchor_at.astimezone(timezone.utc)
    if destination in {"wallet", "store_credit"}:
        sla_days_raw = refund_cfg.get("wallet_sla_days") or refund_cfg.get("sla_business_days") or policy_qa_sla_days or 7
    else:
        sla_days_raw = refund_cfg.get("source_sla_business_days") or refund_cfg.get("sla_business_days") or policy_qa_sla_days or 7
    sla_days = int(sla_days_raw)
    sla = evaluate_sla(anchor_at=anchor_at, sla_days=sla_days, business_days=True)
    status = "sla_breached" if sla["sla_status"] == "breached" else "unknown_with_sla"
    destination_label = _destination_label(destination)
    if sla["sla_status"] == "breached":
        message = (
            f"I do not have live refund confirmation yet, and this appears past the configured "
            f"{sla_days}-business-day refund window. I will escalate this to support."
        )
    else:
        message = policy_qa_message or (
            f"I do not have live bank/gateway refund confirmation yet. Based on the configured policy, "
            f"refunds to {destination_label} usually take up to {sla_days} business days after approval/QC."
        )

    return RefundStatusResult(
        status=status,
        confidence="policy_based",
        source=visibility_mode,
        destination=destination,
        anchor_at=anchor_at,
        sla_due_at=sla["sla_due_at"],
        sla_status=sla["sla_status"],
        should_escalate=sla["should_escalate"],
        message=message,
        raw={
            "visibility_mode": visibility_mode,
            "refund": partner_refund,
            "partner_destination": partner_destination,
        },
    ).model_dump()
