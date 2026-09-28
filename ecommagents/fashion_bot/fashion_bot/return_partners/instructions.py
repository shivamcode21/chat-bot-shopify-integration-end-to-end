"""Config-backed customer instructions for return/exchange request creation."""

from __future__ import annotations

from typing import Any

from fashion_bot.config_manager import aget_json_config
from fashion_bot.return_partners.rules import _normalize_request_type
from fashion_bot.return_prime.workflow.rules import RETURN_PRIME_RULES_CONFIG_KEY


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def _template_format(template: str, values: dict[str, Any]) -> str:
    try:
        return template.format(**values)
    except Exception:
        return template


async def aget_return_prime_instruction_config(
    *,
    client_id: str,
    request_type: str | None = None,
) -> dict:
    rules_config = await aget_json_config(RETURN_PRIME_RULES_CONFIG_KEY, client_id=client_id) or {}
    details_config = await aget_json_config("return_prime_details", client_id=client_id) or {}
    instructions = rules_config.get("customer_instructions")
    if not isinstance(instructions, dict):
        instructions = (
            rules_config.get("instructions")
            if isinstance(rules_config.get("instructions"), dict)
            else {}
        )

    normalized_type = _normalize_request_type(request_type)
    type_rules = rules_config.get(normalized_type) if isinstance(rules_config.get(normalized_type), dict) else {}
    portal_url = str(
        _first_non_empty(
            instructions.get("portal_url"),
            details_config.get("portal_url"),
            details_config.get("return_portal_url"),
            details_config.get("return_exchange_portal_url"),
        )
        or ""
    ).strip()
    support_email = str(
        _first_non_empty(
            instructions.get("support_email"),
            details_config.get("support_email"),
            "support@groovee.in",
        )
    ).strip()
    return {
        "rules_config": rules_config,
        "details_config": details_config,
        "instructions": instructions,
        "request_type": normalized_type,
        "portal_url": portal_url,
        "support_email": support_email,
        "approval_sla": str(instructions.get("approval_sla") or "24-48 hrs").strip(),
        "window_days": _first_non_empty(
            instructions.get(f"{normalized_type}_window_days"),
            type_rules.get("window_days"),
            instructions.get("window_days"),
        ),
    }


async def aget_return_exchange_request_instructions(
    *,
    client_id: str,
    request_type: str | None = None,
) -> dict:
    config = await aget_return_prime_instruction_config(
        client_id=client_id,
        request_type=request_type,
    )
    instructions = config["instructions"]
    normalized_type = config["request_type"]
    portal_url = config["portal_url"]
    support_email = config["support_email"]
    approval_sla = config["approval_sla"]
    window_days = config["window_days"]
    request_label = "exchange" if normalized_type == "exchange" else "return/exchange"

    values = {
        "portal_url": portal_url,
        "support_email": support_email,
        "approval_sla": approval_sla,
        "window_days": window_days or "",
        "request_type": normalized_type,
        "request_label": request_label,
    }
    template = _first_non_empty(
        instructions.get(f"{normalized_type}_how_to_message"),
        instructions.get("how_to_message"),
    )
    if template:
        message = _template_format(str(template), values)
    else:
        window_sentence = (
            f" NOTE: {request_label.title()} can be done within {window_days} days of delivery."
            if window_days
            else ""
        )
        message = (
            f"You may click the link to raise your request: {portal_url}. "
            "Enter the order ID and your mobile number. Select the item along with the reason. "
            f"Update the image of the item with tags intact. Your {request_label} request will be "
            f"approved/rejected in {approval_sla}.{window_sentence}\n\n"
            f"If you face any issue, please email to {support_email}. Please note that you can only "
            "return or exchange of the product delivered."
        )

    return {
        "success": True,
        "partner": "return_prime",
        "request_type": normalized_type,
        "portal_url": portal_url,
        "support_email": support_email,
        "approval_sla": approval_sla,
        "window_days": window_days,
        "message": message,
        "source": RETURN_PRIME_RULES_CONFIG_KEY,
    }
