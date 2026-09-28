# ShipRocket Webhook Event Handling System

This system provides a comprehensive solution for handling ShipRocket webhook events with automatic event processing, deduplication, and template-based notifications.

## Features

- **Event Processing**: Automatically processes ShipRocket webhook events
- **Deduplication**: Prevents duplicate notifications for the same event
- **Template System**: Configurable notification templates for different events
- **Database Persistence**: Stores event history and processing status
- **Priority-based Notifications**: Different priority levels for different events
- **Bulk Processing**: Handle multiple events in a single request
- **Statistics & Monitoring**: Track event processing metrics
- **Easy Configuration**: Simple configuration for adding new events and templates

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   ShipRocket   │───▶│   Webhook API    │───▶│ Event Processor │
│    Webhook     │    │   Endpoint       │    │                 │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                       │
                                                       ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Notification  │◀───│  Event Database  │◀───│ Event Config    │
│    Service     │    │     Service      │    │                 │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Components

### 1. Event Configuration (`event_config.py`)
- Defines event templates and mappings
- Configurable notification settings
- Priority levels for events

### 2. Event Database Service (`event_database.py`)
- PostgreSQL database operations
- Event deduplication logic
- Shipment status history tracking

### 3. Notification Service (`notification_service.py`)
- Template-based notifications
- Priority-based delivery
- Extensible for multiple channels (WhatsApp, Email, SMS)

### 4. Event Processor (`event_processor.py`)
- Main orchestration logic
- Event validation and routing
- Bulk processing capabilities

### 5. Webhook API (`shiprocket_webhook.py`)
- FastAPI endpoints
- Request validation
- Response handling

## Setup

### 1. Database Configuration
Ensure your PostgreSQL database is configured and the `DATABASE_URL` environment variable is set:

```bash
export DATABASE_URL="postgresql://username:password@localhost:5432/database_name"
```

### 2. Dependencies
The system requires the following packages (already in requirements.txt):
- `psycopg[binary]==3.2.9`
- `fastapi`
- `asyncio`

### 3. Environment Variables
```bash
# Required
DATABASE_URL=postgresql://username:password@localhost:5432/database_name

# Optional
LOG_LEVEL=INFO
```

## Usage

### 1. Basic Webhook Endpoint
```bash
POST /shiprocket/event/webhook
```

**Request Body Example:**
```json
{
   "awb":"19041424751540",
   "courier_name":"Delhivery Surface",
   "current_status":"IN TRANSIT",
   "current_status_id":20,
   "shipment_status":"IN TRANSIT",
   "shipment_status_id":18,
   "current_timestamp":"23 05 2023 11:43:52",
   "order_id":"1373900_150876814",
   "sr_order_id":348456385,
   "awb_assigned_date":"2023-05-19 11:59:16",
   "pickup_scheduled_date":"2023-05-19 11:59:17",
   "etd":"2023-05-23 15:40:19",
   "scans":[...],
   "is_return":0,
   "channel_id":3422553,
   "pod_status":"OTP Based Delivery",
   "pod":"Not Available"
}
```

**Response:**
```json
{
   "status": "ok",
   "message": "Event processed successfully",
   "order_id": "1373900_150876814",
   "event_name": "IN_TRANSIT",
   "notification_sent": false,
   "trace_id": "abc123"
}
```

### 2. Bulk Webhook Processing
```bash
POST /shiprocket/event/webhook/bulk
```

**Request Body:**
```json
{
   "events": [
      { /* event 1 */ },
      { /* event 2 */ },
      { /* event 3 */ }
   ]
}
```

### 3. Get Event Statistics
```bash
GET /shiprocket/events/statistics?order_id=123&days=30
```

### 4. Cleanup Old Events
```bash
POST /shiprocket/events/cleanup?days_to_keep=90
```

## Event Configuration

### Adding New Events
To add a new event, update `event_config.py`:

```python
# Add to SHIPMENT_EVENTS
"NEW_STATUS": EventTemplate(
    event_name="NEW_EVENT",
    template_id="new_event_template",
    message="Your new event message! 🎉",
    should_notify=True,
    priority=1
)
```

### Adding New Templates
To add a new notification template, update `notification_service.py`:

```python
# Add to self.templates in _load_templates method
"new_event_template": {
    "title": "🎉 New Event",
    "message": "Your new event message!",
    "subtitle": "Event description",
    "action_text": "Take Action",
    "priority": "high"
}
```

## Event Types and Priorities

### High Priority (1)
- `OUT FOR DELIVERY` - Order is out for delivery
- `DELIVERED` - Order delivered successfully
- `COD ORDER DONE` - COD order completed

### Medium Priority (2)
- `DELIVERY ATTEMPTED` - Delivery attempted but failed
- `PICKED UP` - Order picked up
- `SHIPPED` - Order shipped
- `RETURNED` - Order returned

### Low Priority (3-5)
- `CANCELLED` - Order cancelled
- `IN TRANSIT` - Order in transit
- `MANIFEST GENERATED` - Manifest generated

## Notification Templates

The system includes pre-configured templates for common events:

- **Out for Delivery**: 🚚 Out for Delivery
- **Delivery Completed**: ✅ Delivery Completed
- **Delivery Attempted**: 🔄 Delivery Attempted
- **Order Returned**: 📦 Order Returned
- **Order Cancelled**: 💔 Order Cancelled
- **In Transit**: 🚛 In Transit
- **Picked Up**: 📦 Picked Up
- **Manifest Generated**: 📋 Manifest Generated
- **Shipped**: 🚢 Order Shipped
- **COD Order Done**: 💰 COD Order Completed

## Database Schema

The system creates three main tables:

### 1. `shipment_events`
- Tracks processed events
- Stores event data and notification status

### 2. `event_deduplication`
- Prevents duplicate event processing
- Uses event hashing for uniqueness

### 3. `shipment_status_history`
- Tracks all shipment status changes
- Stores scan history and locations

## Customization

### 1. Notification Channels
To add new notification channels, extend the `NotificationService`:

```python
async def _send_whatsapp_notification(self, notification_data, user_id):
    # Implement WhatsApp API integration
    pass

async def _send_email_notification(self, notification_data, user_id):
    # Implement email service integration
    pass
```

### 2. User Identification
To implement user identification, update the `_extract_user_id` method in `EventProcessor`:

```python
async def _extract_user_id(self, webhook_data):
    # Extract user ID from order data
    # This could be phone number, email, customer ID, etc.
    return webhook_data.get('customer_phone') or webhook_data.get('customer_email')
```

### 3. Event Logic
To add custom event logic, extend the `_process_event` method in `EventProcessor`.

## Monitoring and Logging

The system provides comprehensive logging:

- **Event Processing**: Logs all webhook events
- **Notifications**: Logs notification delivery status
- **Errors**: Logs processing errors with trace IDs
- **Statistics**: Tracks processing metrics

## Error Handling

The system handles various error scenarios:

- **Invalid Data**: Validates webhook payload structure
- **Database Errors**: Graceful fallback for database issues
- **Notification Failures**: Logs failed notifications
- **Duplicate Events**: Prevents duplicate processing

## Performance Considerations

- **Async Processing**: Uses asyncio for non-blocking operations
- **Database Indexing**: Optimized database queries with indexes
- **Bulk Operations**: Supports bulk event processing
- **Connection Pooling**: Efficient database connection management

## Security

- **Input Validation**: Validates all webhook data
- **SQL Injection Protection**: Uses parameterized queries
- **Error Sanitization**: Prevents sensitive data leakage
- **Rate Limiting**: Consider implementing rate limiting for production

## Testing

Test the system with sample webhook data:

```bash
# Test single event
curl -X POST http://localhost:8000/shiprocket/event/webhook \
  -H "Content-Type: application/json" \
  -d @sample_webhook.json

# Test bulk events
curl -X POST http://localhost:8000/shiprocket/event/webhook/bulk \
  -H "Content-Type: application/json" \
  -d @bulk_webhook.json
```

## Troubleshooting

### Common Issues

1. **Database Connection Errors**
   - Check `DATABASE_URL` environment variable
   - Verify PostgreSQL service is running
   - Check network connectivity

2. **Event Not Processing**
   - Verify webhook payload structure
   - Check event configuration
   - Review application logs

3. **Notifications Not Sending**
   - Check notification service configuration
   - Verify user identification logic
   - Review notification channel setup

### Debug Mode

Enable debug logging by setting:
```bash
export LOG_LEVEL=DEBUG
```

## Contributing

To contribute to this system:

1. Follow the existing code structure
2. Add comprehensive error handling
3. Include logging for new features
4. Update documentation
5. Add tests for new functionality

## License

This system is part of the Fashion Bot project and follows the same licensing terms. 