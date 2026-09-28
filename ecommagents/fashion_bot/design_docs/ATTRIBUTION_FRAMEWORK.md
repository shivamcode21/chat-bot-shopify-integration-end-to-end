# Chatbot Attribution Framework

## Overview

This document describes the attribution system implemented in the Fashion Bot chat widget for tracking chatbot-driven conversions on Shopify stores.

The framework separates **deterministic (direct) attribution** from **influence-based (assisted) attribution**, following industry standards used by Google Ads, Meta, and affiliate networks.

---

## Architecture

```
User → Chat Widget → Attribution Events → Backend API → Database
                                              ↓
                     Shopify Order Webhook → Order Attribution → Dashboard
```

---

## Core Identifiers

### 1. Attribution Token (`bot_ref`)
- **Purpose**: Unique identifier generated at chat session start
- **Format**: `sm_<random>_<timestamp>` (e.g., `sm_a7x9k2_m4np8q`)
- **TTL**: 72 hours (configurable)
- **Persistence**: localStorage + Shopify cart attributes

### 2. Anonymous User ID (`anon_id`)
- **Purpose**: Browser-scoped identifier for assisted conversions
- **Format**: UUID v4 (e.g., `550e8400-e29b-41d4-a716-446655440000`)
- **TTL**: 90 days (configurable)
- **Persistence**: localStorage (with creation timestamp)
- **Behavior**: 
  - Generated once per browser/device
  - Persists across sessions within TTL
  - Automatically regenerated after expiration
  - Enables assisted attribution matching

---

## Attribution Types

### Direct Attribution
An order is **directly attributed** when:
- User clicked a chatbot-generated product link
- `bot_ref` was persisted to cart attributes
- Order contains the `bot_ref` in note attributes

**Verification**: `order.note_attributes.bot_ref` exists

### Assisted Attribution
An order is **assisted** when:
- User had a chat interaction
- No chatbot link was clicked (no `bot_ref` in order)
- `anon_id` matches a chat session
- Order placed within 72-hour attribution window

**Verification**: Chat event exists for `anon_id` within window

---

## Tracked Events

| Event Type | Trigger | Data Captured |
|------------|---------|---------------|
| `session_started` | Widget initializes | Page URL, context |
| `chat_opened` | User opens chat | Page type, product info |
| `chat_closed` | User closes chat | Duration implied |
| `message_sent` | User sends message | Message length |
| `message_received` | Bot responds | Has products, response time |
| `product_clicked` | User clicks product card | Product details, URL |
| `link_clicked` | User clicks any link | Link URL, text |
| `add_to_cart` | User adds item via chat | Variant ID, quantity, price |
| `checkout_started` | User proceeds to checkout | Cart context |
| `session_ended` | Widget destroyed | Final flush |

---

## Database Schema

### `chat_attribution_events` Table
Stores all trackable user interactions.

```sql
CREATE TABLE chat_attribution_events (
    id SERIAL PRIMARY KEY,
    bot_ref VARCHAR(100) NOT NULL,
    anon_id VARCHAR(100) NOT NULL,
    session_id VARCHAR(100),
    client_id UUID,
    event_type VARCHAR(50) NOT NULL,
    event_data JSONB,
    page_url TEXT,
    page_type VARCHAR(50),
    product_handle VARCHAR(255),
    product_title TEXT,
    product_price VARCHAR(50),
    user_agent TEXT,
    referrer TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

### `order_attribution` Table
Links Shopify orders to chat sessions.

```sql
CREATE TABLE order_attribution (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(100) NOT NULL,
    order_number VARCHAR(50),
    client_id UUID NOT NULL,
    bot_ref VARCHAR(100),
    anon_id VARCHAR(100),
    attribution_type VARCHAR(20) NOT NULL,
    attribution_window_hours INTEGER,
    order_total DECIMAL(10, 2),
    order_currency VARCHAR(10),
    order_items_count INTEGER,
    chat_session_id VARCHAR(100),
    last_chat_event_at TIMESTAMP WITH TIME ZONE,
    chat_messages_count INTEGER,
    order_created_at TIMESTAMP WITH TIME ZONE,
    attributed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
```

---

## API Endpoints

### Event Storage

**POST** `/api/attribution/event`
```json
{
  "bot_ref": "sm_abc123_xyz789",
  "anon_id": "550e8400-e29b-41d4-a716-446655440000",
  "event_type": "product_clicked",
  "event_data": {"product_title": "Classic Sunglasses"},
  "page_url": "https://store.com/products/classic-sunglasses"
}
```

**POST** `/api/attribution/events/batch`
```json
{
  "events": [
    {"bot_ref": "...", "anon_id": "...", "event_type": "chat_opened"},
    {"bot_ref": "...", "anon_id": "...", "event_type": "message_sent"}
  ]
}
```

### Order Attribution

**POST** `/api/attribution/order`
```json
{
  "order_id": "12345678901234",
  "order_number": "#1234",
  "client_id": "c3ffcb1b-afb9-4ca4-8746-a06698bec870",
  "bot_ref": "sm_abc123_xyz789",
  "anon_id": "550e8400-e29b-41d4-a716-446655440000",
  "order_total": 2999.00,
  "order_currency": "INR",
  "order_items_count": 2
}
```

### KPI Queries

**GET** `/api/attribution/kpi/summary/{client_id}?days=30`

Response:
```json
{
  "client_id": "c3ffcb1b-...",
  "period_days": 30,
  "summary": {
    "total_attributed_orders": 45,
    "total_attributed_revenue": 134500.00,
    "direct_orders": 28,
    "direct_revenue": 89000.00,
    "assisted_orders": 17,
    "assisted_revenue": 45500.00,
    "unique_chat_sessions": 320,
    "conversion_rate": 14.06
  },
  "event_breakdown": {
    "chat_opened": 320,
    "message_sent": 890,
    "product_clicked": 156,
    "add_to_cart": 67
  }
}
```

**GET** `/api/attribution/kpi/daily/{client_id}?days=30`

Returns daily breakdown for charting.

**GET** `/api/attribution/session/{bot_ref}`

Returns all events for a specific chat session.

---

## Widget Integration

### JavaScript API

```javascript
// Access attribution data
FashionBotWidget.attribution.getBotRef()    // Returns current bot_ref
FashionBotWidget.attribution.getAnonId()    // Returns anon_id
FashionBotWidget.attribution.decorateUrl(url) // Adds bot_ref to URL
FashionBotWidget.attribution.persistToCart() // Save tokens to Shopify cart
FashionBotWidget.attribution.trackEvent(type, data) // Manual event tracking
```

### URL Decoration
Product links from the chatbot are automatically decorated:
```
/products/classic-sunglasses → /products/classic-sunglasses?bot_ref=sm_abc123_xyz789
```

### Cart Persistence
When a user adds to cart via chat, attribution tokens are saved:
```javascript
fetch('/cart/update.js', {
  method: 'POST',
  body: JSON.stringify({
    attributes: {
      bot_ref: 'sm_abc123_xyz789',
      anon_id: '550e8400-e29b-...'
    }
  })
});
```

---

## Shopify Order Integration

### Automatic Propagation
Cart attributes automatically flow to order note_attributes:
```
cart.attributes.bot_ref → order.note_attributes.bot_ref
cart.attributes.anon_id → order.note_attributes.anon_id
```

### Webhook Integration
When Shopify sends an order webhook, extract and attribute:

```python
# In Shopify order webhook handler
bot_ref = order.get('note_attributes', {}).get('bot_ref')
anon_id = order.get('note_attributes', {}).get('anon_id')

# Call attribution API
requests.post('/api/attribution/order', json={
    'order_id': order['id'],
    'client_id': client_id,
    'bot_ref': bot_ref,
    'anon_id': anon_id,
    'order_total': order['total_price'],
    ...
})
```

### Client Verification
Clients can verify attribution in Shopify Admin:
```
Shopify Admin → Orders → Select Order → Notes/Attributes → bot_ref, anon_id
```

---

## Testing Guide

### 1. Initialize Database Tables

```bash
# Run table creation
python -c "from fashion_bot.initialize_db import initialize_database; initialize_database()"

# Or via API
curl -X POST http://localhost:8000/api/attribution/init-tables
```

### 2. Test Event Storage

```bash
# Store a single event
curl -X POST http://localhost:8000/api/attribution/event \
  -H "Content-Type: application/json" \
  -d '{
    "bot_ref": "sm_test123_abc456",
    "anon_id": "test-anon-id-123",
    "event_type": "chat_opened",
    "page_url": "https://example.com/",
    "page_type": "home"
  }'

# Store batch events
curl -X POST http://localhost:8000/api/attribution/events/batch \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"bot_ref": "sm_test123_abc456", "anon_id": "test-anon-id-123", "event_type": "message_sent"},
      {"bot_ref": "sm_test123_abc456", "anon_id": "test-anon-id-123", "event_type": "product_clicked", "product_title": "Test Product"}
    ]
  }'
```

### 3. Test Order Attribution

```bash
# Direct attribution (with bot_ref)
curl -X POST http://localhost:8000/api/attribution/order \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "test-order-001",
    "client_id": "c3ffcb1b-afb9-4ca4-8746-a06698bec870",
    "bot_ref": "sm_test123_abc456",
    "order_total": 1999.00
  }'

# Assisted attribution (no bot_ref, only anon_id)
curl -X POST http://localhost:8000/api/attribution/order \
  -H "Content-Type: application/json" \
  -d '{
    "order_id": "test-order-002",
    "client_id": "c3ffcb1b-afb9-4ca4-8746-a06698bec870",
    "anon_id": "test-anon-id-123",
    "order_total": 2499.00
  }'
```

### 4. Query KPIs

```bash
# Get summary KPIs
curl "http://localhost:8000/api/attribution/kpi/summary/c3ffcb1b-afb9-4ca4-8746-a06698bec870?days=30"

# Get daily breakdown
curl "http://localhost:8000/api/attribution/kpi/daily/c3ffcb1b-afb9-4ca4-8746-a06698bec870?days=7"

# Get session events
curl "http://localhost:8000/api/attribution/session/sm_test123_abc456"
```

### 5. Test Widget Integration

1. **Open browser console on a test store page**
2. **Check attribution is initialized:**
   ```javascript
   console.log(FashionBotWidget.attribution.getBotRef());
   console.log(FashionBotWidget.attribution.getAnonId());
   ```

3. **Open chat and verify events in Network tab:**
   - Look for POST requests to `/api/attribution/events/batch`
   - Events should include `chat_opened`, `message_sent`, etc.

4. **Click a product and verify:**
   - `product_clicked` event sent
   - URL decorated with `bot_ref` param

5. **Add to cart and verify:**
   - `add_to_cart` event sent
   - Cart attributes updated (check `/cart.json`)

### 6. End-to-End Test Flow

```
1. Open store page → session_started event
2. Open chat widget → chat_opened event  
3. Send message → message_sent event
4. Click product card → product_clicked event, URL decorated
5. Add to cart → add_to_cart event, cart attributes set
6. Complete checkout → bot_ref/anon_id in order
7. Order webhook → order attributed (direct or assisted)
8. Query KPIs → see attributed revenue
```

---

## KPI Definitions

### Deterministic Metrics
| Metric | Definition |
|--------|------------|
| Directly Attributed Orders | Orders with `bot_ref` in note_attributes |
| Directly Attributed Revenue | Sum of order totals for direct orders |
| Cart Creation Rate | Add-to-cart events / Chat sessions |

### Assisted Metrics
| Metric | Definition |
|--------|------------|
| Assisted Orders | Orders matched by `anon_id` within 72h window |
| Assisted Revenue | Sum of order totals for assisted orders |
| Chat-to-Purchase Rate | Orders (any type) / Chat sessions |

### Engagement Metrics
| Metric | Definition |
|--------|------------|
| Chats Started | Count of `chat_opened` events |
| Messages Exchanged | Sum of `message_sent` + `message_received` |
| Products Viewed | Count of `product_clicked` events |

---

## Privacy & Compliance

- ✅ No PII stored (only anonymous identifiers)
- ✅ No cross-device tracking
- ✅ Browser-scoped identifiers only
- ✅ Fully compliant with Shopify policies
- ✅ User can clear data by clearing browser storage

---

## Files Modified

- `static/chat-widget.v1.js` - Attribution module added
- `fashion_bot/Tables/attribution_table.py` - Database tables
- `fashion_bot/attribution_router.py` - API endpoints
- `fashion_bot/main.py` - Router registration
- `fashion_bot/initialize_db.py` - Table initialization

---

## Future Enhancements

1. **Dashboard UI** - Visual KPI dashboard
2. **Webhook Integration** - Auto-attribute orders from Shopify webhooks
3. **A/B Testing** - Compare conversion with/without chatbot
4. **Multi-touch Attribution** - Track multiple chat sessions per order
5. **Real-time Analytics** - WebSocket-based live dashboard

