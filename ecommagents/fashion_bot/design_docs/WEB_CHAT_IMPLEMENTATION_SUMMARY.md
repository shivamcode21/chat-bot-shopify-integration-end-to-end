# Web Chat Widget Implementation Summary

## ✅ What's Been Created

I've built a complete web chat widget system that works **exactly like your existing Gupshup WhatsApp and Streamlit interfaces**. All three use the same `graph.invoke(state)` pattern!

### Files Created/Modified:

1. **`fashion_bot/websocket_chat.py`** (NEW)
   - WebSocket server handling real-time chat
   - Uses `fashion_bot.graph_meta.graph` (same as streamlit_app)
   - Session management with localStorage persistence
   - Phone number linking capability
   - BigQuery logging integration

2. **`fashion_bot/main.py`** (MODIFIED)
   - Integrated websocket_router
   - Added CORS middleware for cross-origin requests
   - Added static file serving for widget
   - Added `/test` endpoint for widget demo page
   - Added `/health` endpoint

3. **`run_chat_server.py`** (MODIFIED)
   - Enhanced launcher script with helpful info
   - Shows all available endpoints

4. **`WEB_CHAT_WIDGET_README.md`** (NEW)
   - Complete integration guide
   - API reference
   - Deployment instructions
   - Troubleshooting guide

5. **`static/chat-widget.js`** (ALREADY EXISTS)
   - Embeddable JavaScript widget
   - Beautiful UI with gradient design
   - WebSocket connection handling
   - Message queue for reliability

6. **`static/test-chat-widget.html`** (ALREADY EXISTS)
   - Beautiful demo website
   - Shows integration example
   - Ready to test

## 🚀 How to Use

### 1. Start the Server

```bash
python run_chat_server.py
```

You'll see:
```
🚀 Starting Fashion Bot Server with Chat Widget
📍 Server: http://localhost:8000
🧪 Test widget: http://localhost:8000/test
💚 Health: http://localhost:8000/health
🔌 WebSocket: ws://localhost:8000/ws/chat/{client_id}/{session_id}
```

### 2. Test the Widget

Open: `http://localhost:8000/test`

Try chatting:
- "Where is my order GV10455?"
- "What are your return policies?"
- "Show me hoodies collection"

### 3. Embed on ANY Website

Add these 2 lines to any HTML page:

```html
<script src="http://localhost:8000/static/chat-widget.js"></script>
<script>
  FashionBotWidget.init({
    clientId: 'groovee',  // Your client from vendor_config.py
    apiUrl: 'ws://localhost:8000/ws/chat',
    position: 'bottom-right',
    theme: 'light'
  });
</script>
```

That's it! The chat button appears and connects to your bot.

## 🔄 Same Flow as Gupshup & Streamlit

```
┌─────────────────────────────────────────────────────────┐
│                  ALL USE THE SAME GRAPH                  │
├─────────────────────────────────────────────────────────┤
│                                                          │
│  Gupshup Webhook    →  graph.invoke(state)              │
│  Streamlit App      →  graph.invoke(state)              │
│  Web Chat Widget    →  graph.invoke(state)              │
│                                                          │
│  ALL HAVE ACCESS TO:                                     │
│  ✅ All refactored tools (order status, pricing, etc.)  │
│  ✅ All orchestrators (vendor-agnostic)                 │
│  ✅ All processors (Shopify, etc.)                      │
│  ✅ All enrichers (GraphQL, tracking, Shiprocket)       │
│  ✅ Configuration-driven behavior                       │
│  ✅ Client context (via set_client_id)                  │
│  ✅ BigQuery logging                                    │
│                                                          │
└─────────────────────────────────────────────────────────┘
```

### Implementation Comparison

| Feature | Gupshup | Streamlit | Web Chat |
|---------|---------|-----------|----------|
| **Graph Import** | `from fashion_bot.graph_meta import graph` | ✅ Same | ✅ Same |
| **Invoke Pattern** | `graph.invoke(state)` | ✅ Same | ✅ Same |
| **State Structure** | SupportState with messages, phone_number, etc. | ✅ Same | ✅ Same |
| **Client Context** | `set_client_id(state.get('client_id'))` | N/A | ✅ Same as Gupshup |
| **BigQuery Logging** | ✅ Enabled | ❌ Disabled | ✅ Enabled |
| **Session Persistence** | Phone number as thread_id | Session state | localStorage + session_id |

## ✨ Key Features

### 1. Anonymous Sessions
- Users can chat without providing phone number
- Session ID stored in browser localStorage
- Format: `web_xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx`
- Persists across page reloads
- Auto-cleanup after 24 hours of inactivity

### 2. Phone Number Linking
When user provides phone during chat:
```javascript
// Auto-detected from conversation or explicitly set
{
  "type": "phone_update",
  "phone": "9876543210"
}
```
Session gets linked to phone for conversation history retrieval.

### 3. Real-time Communication
- WebSocket for instant messaging
- Auto-reconnection on disconnect
- Message queue for reliability
- Typing indicators (ready for implementation)

### 4. Beautiful UI
- Modern gradient design (purple/blue)
- Smooth animations (slide up, fade in)
- Responsive (works on mobile, tablet, desktop)
- Accessible (proper ARIA labels)

### 5. Easy Integration
- Just 2 lines of JavaScript
- No dependencies
- Works on any website
- Customizable position and theme

## 📊 Session Management

### Storage
- **Development**: In-memory dictionary `active_sessions`
- **Production**: Use Redis or database (replace the dict)

### Session Structure
```python
{
    "session_id": "web_uuid...",
    "client_id": "groovee",
    "state": {
        "messages": [HumanMessage(...), AIMessage(...)],
        "phone_number": "session_id or actual_phone",
        "selected_order_id": None,
        "known_orders": None,
        # ... all SupportState fields
    },
    "created_at": "2025-12-04T10:00:00",
    "last_activity": "2025-12-04T10:05:00"
}
```

### Cleanup
- Auto-cleanup runs when new connections arrive
- Sessions older than 24 hours are removed
- Prevents memory leaks

## 🔧 Configuration

### Client ID
The `clientId` must match a configured client in `fashion_bot/core/vendor_config.py`:

```python
# fashion_bot/core/vendor_config.py
VENDOR_CLIENT_MAPPING = {
    "groovee": SHOPIFY_SHIPROCKET_CONFIG,  # ← Your client
    # ... other clients
}
```

### Customization Options

```javascript
FashionBotWidget.init({
    clientId: 'groovee',           // Required: Client from vendor_config.py
    apiUrl: 'ws://localhost:8000/ws/chat',  // Required: WebSocket URL
    position: 'bottom-right',      // Optional: 'bottom-right' | 'bottom-left'
    theme: 'light'                 // Optional: 'light' | 'dark' (future)
});
```

## 🌐 Production Deployment

### 1. Update URLs
```html
<script src="https://your-domain.com/static/chat-widget.js"></script>
<script>
  FashionBotWidget.init({
    clientId: 'groovee',
    apiUrl: 'wss://your-domain.com/ws/chat',  // Note: wss:// not ws://
    position: 'bottom-right'
  });
</script>
```

### 2. Update CORS in `fashion_bot/main.py`
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://your-website.com",
        "https://www.your-website.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
```

### 3. Use Redis for Sessions
Replace the in-memory dict with Redis:
```python
# In websocket_chat.py
import redis
redis_client = redis.Redis(host='localhost', port=6379, db=0)

def get_or_create_session(session_id, client_id):
    session = redis_client.get(f"session:{session_id}")
    if session:
        return json.loads(session)
    # ... create new session
    redis_client.setex(f"session:{session_id}", 86400, json.dumps(session))
```

### 4. Deploy with HTTPS
- Use nginx as reverse proxy
- Or deploy to platforms with SSL:
  - Heroku
  - AWS ECS/Fargate
  - Google Cloud Run
  - DigitalOcean App Platform

## 🧪 Testing

### Test Queries
```
Order Status:
- "Where is my order GV10455?"
- "Track my order"

Product Info:
- "Show me hoodies collection"
- "What sizes do you have?"

Policies:
- "What is your return policy?"
- "How long does shipping take?"

Order Creation:
- "I want to order a shacket"
```

### Monitoring Active Sessions
```bash
curl http://localhost:8000/ws/sessions/active
```

Returns:
```json
{
  "active_sessions": 3,
  "sessions": [
    {
      "session_id": "web_1234...",
      "client_id": "groovee",
      "created_at": "2025-12-04T10:00:00",
      "last_activity": "2025-12-04T10:05:00",
      "message_count": 8
    }
  ]
}
```

## 🔍 Troubleshooting

### Widget not appearing?
1. Check browser console for errors
2. Verify script URL is accessible: `http://localhost:8000/static/chat-widget.js`
3. Check for Content Security Policy blocking

### WebSocket connection failed?
1. Check server is running: `http://localhost:8000/health`
2. Verify WebSocket URL format: `ws://` for local, `wss://` for production
3. Check CORS settings in `main.py`

### Messages not sending?
1. Open DevTools → Network → WS tab
2. Check WebSocket frames
3. Check server logs for errors

### Bot not responding correctly?
The web chat uses the EXACT same bot as WhatsApp. If it works in WhatsApp (Gupshup), it will work here.

## 📚 API Reference

### WebSocket Endpoint
**URL**: `ws://{host}/ws/chat/{client_id}/{session_id}`

**Messages**:

Client → Server:
```json
{
  "type": "message",
  "message": "Where is my order?",
  "timestamp": "2025-12-04T10:00:00Z"
}
```

Server → Client:
```json
{
  "type": "message",
  "message": "Your order #GV10455 is shipped...",
  "timestamp": "2025-12-04T10:00:01Z",
  "trace_id": "trace_123..."
}
```

Phone Update:
```json
{
  "type": "phone_update",
  "phone": "9876543210"
}
```

Heartbeat:
```json
{
  "type": "ping"
}
```

## 🎯 Next Steps

1. **Start the server**: `python run_chat_server.py`
2. **Test the widget**: Open `http://localhost:8000/test`
3. **Embed on your site**: Add the 2-line script
4. **Monitor sessions**: Use `/ws/sessions/active` endpoint
5. **Deploy to production**: Update URLs and CORS settings

## 🔐 Security Considerations

- ✅ Input sanitization handled by LangGraph
- ✅ Session expiry (24 hours)
- ⚠️ Add rate limiting to WebSocket endpoint (recommended)
- ⚠️ Validate client_id against allowed list (recommended)
- ⚠️ Use WSS (secure WebSocket) in production (required)

---

**That's it!** Your web chat widget is ready to use and works identically to your WhatsApp bot. All the refactored architecture (orchestrators, processors, enrichers, formatters) is fully available! 🚀

