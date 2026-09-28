"""Dramatiq broker configuration for Return Prime workers."""

from __future__ import annotations

from fashion_bot.workers.broker import get_broker

RETURN_PRIME_WEBHOOK_QUEUE = "return_prime_webhooks"

broker = get_broker()
