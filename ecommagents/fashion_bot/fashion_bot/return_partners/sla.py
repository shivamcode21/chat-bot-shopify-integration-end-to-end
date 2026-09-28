"""SLA helpers for return/exchange customer responses."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


def parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text[:10], "%Y-%m-%d")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def add_business_days(start: datetime, days: int) -> datetime:
    current = start
    remaining = max(int(days or 0), 0)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5:
            remaining -= 1
    return current


def evaluate_sla(
    *,
    anchor_at: datetime | None,
    sla_days: int | None,
    business_days: bool = True,
    now: datetime | None = None,
    due_soon_hours: int = 24,
) -> dict:
    if not anchor_at or not sla_days:
        return {"sla_status": "unknown", "sla_due_at": None, "should_escalate": False}
    now = now or datetime.now(timezone.utc)
    due_at = add_business_days(anchor_at, sla_days) if business_days else anchor_at + timedelta(days=sla_days)
    if now > due_at:
        return {"sla_status": "breached", "sla_due_at": due_at, "should_escalate": True}
    if due_at - now <= timedelta(hours=due_soon_hours):
        return {"sla_status": "due_soon", "sla_due_at": due_at, "should_escalate": False}
    return {"sla_status": "within_sla", "sla_due_at": due_at, "should_escalate": False}

