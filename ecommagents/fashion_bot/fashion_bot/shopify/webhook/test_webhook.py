#!/usr/bin/env python3
"""
Test script for Shopify webhook event handling system.
Covers all event types and prints WhatsApp message output.
"""
import asyncio
import logging
import json
from typing import Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Example Shopify webhook payloads for all scenarios
test_payloads = [
    # Paid, unfulfilled
    {
        "id": "1001",
        "financial_status": "paid",
        "fulfillment_status": "unfulfilled",
        "updated_at": "2024-07-01T12:00:00Z",
        "shipping_address": {"first_name": "Amit", "phone": "9876543210"},
        "customer": {"first_name": "Amit", "phone": "9876543210"},
        "name": "#1001",
        "total_price": "1999"
    },
    # Partially paid, unfulfilled
    {
        "id": "1002",
        "financial_status": "partially_paid",
        "fulfillment_status": "unfulfilled",
        "updated_at": "2024-07-01T12:05:00Z",
        "shipping_address": {"first_name": "Priya", "phone": "9123456789"},
        "customer": {"first_name": "Priya", "phone": "9123456789"},
        "name": "#1002",
        "total_price": "2999"
    },
    # Payment pending, unfulfilled (COD)
    {
        "id": "1003",
        "financial_status": "pending",
        "fulfillment_status": "unfulfilled",
        "updated_at": "2024-07-01T12:10:00Z",
        "shipping_address": {"first_name": "Rahul", "phone": "9988776655"},
        "customer": {"first_name": "Rahul", "phone": "9988776655"},
        "name": "#1003",
        "total_price": "1599"
    },
    # Fulfilled
    {
        "id": "1004",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "updated_at": "2024-07-01T12:15:00Z",
        "shipping_address": {"first_name": "Sneha", "phone": "9001122334"},
        "customer": {"first_name": "Sneha", "phone": "9001122334"},
        "name": "#1004",
        "total_price": "2499"
    },
    # Voided/cancelled
    {
        "id": "1005",
        "financial_status": "voided",
        "fulfillment_status": "unfulfilled",
        "updated_at": "2024-07-01T12:20:00Z",
        "cancelled_at": "2024-07-01T12:21:00Z",
        "shipping_address": {"first_name": "Riya", "phone": "9112233445"},
        "customer": {"first_name": "Riya", "phone": "9112233445"},
        "name": "#1005",
        "total_price": "1899"
    }
]

async def test_shopify_event_processing():
    from fashion_bot.shopify.webhook.event_processor import ShopifyEventProcessor
    processor = ShopifyEventProcessor()
    results = []
    for payload in test_payloads:
        logger.info(f"\nTesting payload: {json.dumps(payload)}")
        result = await processor.process_webhook_event(payload)
        logger.info(f"Result: {json.dumps(result, indent=2)}")
        results.append(result)
    return results

if __name__ == "__main__":
    asyncio.run(test_shopify_event_processing()) 