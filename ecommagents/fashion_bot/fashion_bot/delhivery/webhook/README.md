# Delhivery Webhook Pipeline

This package handles inbound Delhivery Push API webhooks (per-shipment scan
events) and converts them into:

1. Persisted rows in the shared `shipment_events`, `event_deduplication`, and
   `shipment_status_history` Postgres tables (with `partner = 'delhivery'`).
2. Optional WhatsApp template messages to the customer via Gupshup, looked
   up under `gupshup_templates(channel='delhivery', event_key=...)`.

Mounted at `/shipping/delhivery` in `agent_controller.py`. URLs:

- `POST /shipping/delhivery/event/webhook/{base64_client_id}` — primary.
- `POST /shipping/delhivery/event/webhook` — legacy (gated by
  `DELHIVERY_ALLOW_LEGACY_WEBHOOK=true`).
- Bulk variants under `/event/webhook/bulk(/...)`.

## Payload shape (inbound from Delhivery)

```
{
  "ShipmentData": [
    {
      "Shipment": {
        "AWB": "1234567890",
        "ReferenceNo": "GV12345",
        "Status": {
          "Status": "Delivered",          // -> shipment_status
          "StatusType": "DL",             // -> current_status
          "StatusDateTime": "2026-05-18 16:42:00",
          "StatusLocation": "Mumbai DC"
        },
        "Scans": [
          {
            "ScanDetail": {
              "Scan": "Out for delivery",
              "ScanDateTime": "2026-05-18 09:10:00",
              "ScannedLocation": "Mumbai DC",
              "Instructions": "OFD"
            }
          }
        ]
      }
    }
  ]
}
```

The handler also accepts a top-level `Shipment` block (without the envelope)
to be tolerant of minor format changes.

## Gupshup templates

Add per-tenant rows to `gupshup_templates`:

```
INSERT INTO gupshup_templates (client_id, channel, event_key, template_id, image_url, param_order)
VALUES (
  '<tenant_uuid>',
  'delhivery',
  'out_for_delivery',
  '<gupshup_template_id>',
  NULL,
  ARRAY['first_name', 'order_number']
);
```

Supported `event_key` values (mapped from Delhivery `Status.Status`):

| Delhivery status | event_key |
|------------------|-----------|
| Manifested / Open / Scheduled | `manifested` |
| Pending          | `pending` |
| In Transit / Dispatched | `in_transit` |
| Out For Delivery | `out_for_delivery` |
| Delivered        | `delivered` |
| RTO / Returned   | `rto` |
| RTO Delivered    | `rto_delivered` |
| Cancelled / Canceled | `cancelled` |

If the tenant has no template row for the event, the webhook still persists
the event and history but skips the WhatsApp message.

## Configuration

Per-tenant API token + warehouse client_name in `client_configs`:

```
INSERT INTO client_configs (client_id, config_key, config_value)
VALUES (
  '<tenant_uuid>',
  'delhivery_details',
  '{"api_token":"<DELHIVERY_TOKEN>","client_name":"<warehouse>","pickup_location":"Primary"}'
);
```

Environment flags:

- `DELHIVERY_ALLOW_LEGACY_WEBHOOK` — enable `/event/webhook` without
  `client_id` in URL (default: off).
- `LOGISTICS_PER_PARTNER_TIMEOUT_S` — per-partner deadline used by
  `core/logistics_router.py` (default: 10s).

## Generating per-tenant webhook URL

```
python fashion_bot/generate_shiprocket_webhook_url.py <client_id>
# Then replace "/shipping/event/webhook/" with "/shipping/delhivery/event/webhook/"
```
