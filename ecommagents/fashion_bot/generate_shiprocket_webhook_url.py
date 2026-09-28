#!/usr/bin/env python3
"""
Helper script to generate Shiprocket webhook URLs with base64-encoded client_id.

Usage:
    python generate_shiprocket_webhook_url.py <client_id> [base_url]
    
Examples:
    python generate_shiprocket_webhook_url.py groovee
    python generate_shiprocket_webhook_url.py fashionstore https://api.example.com
"""

import base64
import sys


def encode_client_id(client_id: str) -> str:
    """
    Encode client_id to base64 for URL.
    
    Args:
        client_id: The client identifier
        
    Returns:
        Base64 encoded client_id
    """
    return base64.b64encode(client_id.encode('utf-8')).decode('utf-8')


def decode_client_id(encoded_client_id: str) -> str:
    """
    Decode base64 encoded client_id.
    
    Args:
        encoded_client_id: Base64 encoded client_id
        
    Returns:
        Decoded client_id
    """
    return base64.b64decode(encoded_client_id).decode('utf-8')


def generate_webhook_url(client_id: str, base_url: str = "https://your-domain.com") -> dict:
    """
    Generate Shiprocket webhook URLs for a client.
    
    Args:
        client_id: The client identifier
        base_url: Base URL of your API (default: https://your-domain.com)
        
    Returns:
        Dictionary with webhook URLs and encoded client_id
    """
    encoded = encode_client_id(client_id)
    
    return {
        "client_id": client_id,
        "encoded_client_id": encoded,
        "single_event_url": f"{base_url}/shipping/event/webhook/{encoded}",
        "bulk_event_url": f"{base_url}/shipping/event/webhook/bulk/{encoded}"
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python generate_shiprocket_webhook_url.py <client_id> [base_url]")
        print()
        print("Examples:")
        print("  python generate_shiprocket_webhook_url.py groovee")
        print("  python generate_shiprocket_webhook_url.py fashionstore https://api.example.com")
        sys.exit(1)
    
    client_id = sys.argv[1]
    base_url = sys.argv[2] if len(sys.argv) > 2 else "https://your-domain.com"
    
    result = generate_webhook_url(client_id, base_url)
    
    print("=" * 80)
    print("Shiprocket Webhook URLs for Multi-Client Setup")
    print("=" * 80)
    print()
    print(f"Client ID:        {result['client_id']}")
    print(f"Encoded Client:   {result['encoded_client_id']}")
    print()
    print("Webhook URLs:")
    print("-" * 80)
    print(f"Single Event:     {result['single_event_url']}")
    print(f"Bulk Events:      {result['bulk_event_url']}")
    print()
    print("=" * 80)
    print("Configuration Steps:")
    print("=" * 80)
    print("1. Log into Shiprocket dashboard for this client")
    print("2. Navigate to Settings > Webhooks")
    print("3. Configure the webhook URL (Single Event URL)")
    print("4. Select events to track (e.g., Shipped, Delivered, etc.)")
    print("5. Save the webhook configuration")
    print("6. Test by triggering a shipment event")
    print()
    print("=" * 80)
    print("Testing the Webhook:")
    print("=" * 80)
    print(f"curl -X POST '{result['single_event_url']}' \\")
    print("  -H 'Content-Type: application/json' \\")
    print("  -d '{")
    print('    "order_id": "TEST123",')
    print('    "awb": "AWB123456",')
    print('    "shipment_status": "delivered",')
    print('    "current_status": "Delivered"')
    print("  }'")
    print()
    print("=" * 80)
    print()


if __name__ == "__main__":
    main()

