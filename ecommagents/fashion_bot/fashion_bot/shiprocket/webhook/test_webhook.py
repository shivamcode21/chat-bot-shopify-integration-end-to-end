#!/usr/bin/env python3
"""
Test script for Shiprocket webhook event handling system.
Covers the delivered event and prints WhatsApp message output.
"""
import asyncio
import logging
import json
from typing import Dict, Any

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Example Shiprocket webhook payload for delivered event
test_payloads = [
    {
        "order_id": "SR1001",
        "awb": "19041424751540",
        "shipment_status": "DELIVERED",
        "current_status": "DELIVERED",
        "current_timestamp": "2024-07-01T13:00:00Z",
        "sr_order_id": 348456385,
        "channel_id": 3422553,
        # Simulate what get_shiprocket_order_details would return
        "customer_name": "Amit",
        "customer_phone": "9876543210",
        "order_id": "SR1001",
        "order_number": "#1001",
        "total": "1999"
    }
]

async def test_shiprocket_event_processing():
    from fashion_bot.shiprocket.webhook.event_processor import ShipRocketEventProcessor
    processor = ShipRocketEventProcessor()
    results = []
    for payload in test_payloads:
        logger.info(f"\nTesting payload: {json.dumps(payload)}")
        result = await processor.process_webhook_event(payload)
        logger.info(f"Result: {json.dumps(result, indent=2)}")
        results.append(result)
    return results

if __name__ == "__main__":
    asyncio.run(test_shiprocket_event_processing()) 