"""Typed contracts for return/exchange orchestration.

These models intentionally describe customer-visible conclusions rather than
partner-specific payloads. Partner adapters can carry raw data in ``raw`` while
the chat/orchestration layer works from stable fields.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ReturnIdentityResult(BaseModel):
    success: bool = True
    verified: bool = False
    needs_identity: bool = False
    should_block: bool = True
    matched_on: Literal["phone", "email", "bypass", "not_matched", "not_provided"] = "not_provided"
    message: str = ""
    order_number: str | None = None
    order: dict[str, Any] = Field(default_factory=dict)
    provided_phone: str | None = None
    provided_email: str | None = None
    masked_order_phone: str | None = None
    order_email: str | None = None
    failed_reason: str | None = None


class ReturnLineItemSelection(BaseModel):
    line_item_id: str | None = None
    product_id: str | None = None
    variant_id: str | None = None
    title: str | None = None
    quantity: int | None = None


class ShipmentLegStatus(BaseModel):
    success: bool = True
    leg: Literal["return_pickup", "exchange_forward"] = "return_pickup"
    status: str = "unknown"
    message: str = ""
    awb: str | None = None
    tracking_url: str | None = None
    carrier: str | None = None
    partner: str | None = None
    raw_status: str | None = None
    logistics_result: dict[str, Any] = Field(default_factory=dict)
    per_partner: dict[str, Any] = Field(default_factory=dict)


class RefundStatusResult(BaseModel):
    success: bool = True
    status: Literal[
        "not_applicable",
        "unknown_with_sla",
        "pending",
        "initiated",
        "processed",
        "failed",
        "sla_breached",
    ] = "unknown_with_sla"
    confidence: Literal["none", "policy_based", "partner_reported", "shopify_reported", "manual"] = "none"
    source: str = "none"
    destination: str | None = None
    amount: str | float | int | None = None
    currency: str | None = None
    anchor_at: datetime | None = None
    sla_due_at: datetime | None = None
    sla_status: Literal["not_started", "within_sla", "due_soon", "breached", "unknown"] = "unknown"
    should_escalate: bool = False
    message: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

