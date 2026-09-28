# Nearest Offline Store Location

> Surfaces the closest physical store to the customer across webchat and WhatsApp, for out-of-stock suggestions, brand legitimacy inquiries, and return/exchange confirmations.

## Problem

When a product is out of stock, a user questions brand authenticity/location, or a return/exchange is confirmed, the bot has no way to suggest the nearest physical store. This feature bridges online-to-offline by surfacing the closest store with a clickable Google Maps link.

## Architecture

Two complementary mechanisms serve different channels:

### Path A — Context Injection (Webchat with browser geolocation)

1. Widget requests `navigator.geolocation.getCurrentPosition()` on WebSocket open.
2. Browser lat/lng is attached to every outbound WS payload as `userLocation`.
3. `websocket_chat.py` writes it to `state["user_location"]`.
4. `generic_skill_node.py` pre-computes the nearest store via `find_nearest_store()` and injects it into the system prompt before the LLM runs.
5. Zero tool calls, zero latency.

### Path B — LLM Tool (WhatsApp / geo denied)

1. The system prompt hints that offline stores are available.
2. The LLM asks the user for their city or pincode.
3. The LLM calls `get_nearest_store(city_or_pincode="...")`.
4. The tool returns the nearest store with distance and Google Maps link.
5. Works on any channel as a fallback.

## Target Agents

| Agent | Use Case |
|---|---|
| `product_details` | Out-of-stock product/variant — suggest nearest store |
| `recommendations` | No online alternatives — suggest store visit |
| `vendor_inquiry` | Brand authenticity, office/warehouse/location questions |
| `return_and_exchange` | Confirm return/exchange — mention in-store option |

## Components

### 1. Store Registry — `utils/store_locations.py`

- Store data lives in `client_configs` Postgres table under `config_key = 'store_locations'`.
- `config_value` is a JSON array of store objects (each with `name`, `address`, `city`, `pincode`, `latitude`, `longitude`, `phone`, `manager_name`, `manager_email`, `hours`).
- `aget_all_stores(client_id)` — async, fetches via `aget_config` (three-tier cache: memory → Redis → DB).
- `afind_nearest_store(client_id, ...)` — async, resolution priority: haversine > pincode match > city match.
- `haversine_km()` — pure great-circle distance, no I/O.
- Google Maps URL auto-generated as `https://maps.google.com/?q={lat},{lon}`.

### 2. State Field — `user_location` in `SupportState`

```python
user_location: Optional[Dict[str, Any]]  # {latitude, longitude, accuracy, source}
```

Ephemeral (session-scoped in Redis, not persisted to Postgres).

### 3. Widget Changes — `chat-widget-frame.html`

- Requests geolocation once on WebSocket `onopen`.
- Stores result in module-level `userLocation`.
- Does **not** block on permission — if denied, `userLocation` stays `null`.
- Attaches `userLocation` to every `sendMessage` payload alongside `pageContext`.

### 4. WebSocket Handler — `websocket_chat.py`

- Parses `data.get("userLocation")` in both event-batch and message paths.
- Writes to `session["state"]["user_location"]` with `source: "browser_geolocation"`.

### 5. LLM Tool — `get_nearest_store` in `tool_factory.py`

- Stateless: reads `client_id` from closure, `user_location` from state (read-only).
- No I/O: pure in-memory haversine + dictionary lookup.
- Returns `{success, nearest_store, other_stores}` or `{success, message, all_stores}`.
- Registered for all 4 target agents via `tool_factory.py` and `tool_registry.py`.
- On a successful suggestion, optionally notifies the client's escalation agent via
  `_anotify_agent_store_visit` (see below). Customer-facing behavior is unaffected.

### Agent Store-Visit Notification (opt-in)

- `_anotify_agent_store_visit` sends a WhatsApp `🏬 Store Visit Suggested` message to the
  client's escalation agent whenever a physical store is suggested.
- **Disabled by default.** Gated on the `send_physical_store_message` client_config flag
  (read via `aget_config`, three-tier cached). The message is only sent when the value is
  truthy (`true` / `"true"` / `"1"` / `"yes"` / `"y"` / `"on"`); otherwise it is skipped and
  logged as `🔕 [store_visit_notify] send_physical_store_message disabled, skipping`.

  ```sql
  -- enable the agent notification for a client
  INSERT INTO client_configs (client_id, config_key, config_value)
  VALUES ('<tenant_uuid>', 'send_physical_store_message', 'true')
  ON CONFLICT (client_id, config_key) DO UPDATE SET config_value = EXCLUDED.config_value;
  ```

### 6. Context Injection — `generic_skill_node.py`

- If `state.user_location` has lat/lng → pre-compute nearest store → inject `==== NEAREST OFFLINE STORE ====` block into `context_message`.
- Else if stores exist for the client → inject `==== OFFLINE STORES AVAILABLE ====` hint so the LLM knows to ask and call the tool.

## Google Maps Link Rendering

- **Webchat**: Widget formatter auto-links bare URLs → clickable.
- **WhatsApp**: WhatsApp auto-links bare URLs → clickable.

No special rendering logic needed.

## Files Changed

| File | Change |
|---|---|
| `fashion_bot/utils/store_locations.py` | **NEW** — store registry, haversine, `find_nearest_store()` |
| `fashion_bot/tool_factory.py` | Add `_create_nearest_store_tool()` factory + register in `product_details_tools_factory` and `return_exchange_tools_factory` |
| `fashion_bot/core/tool_registry.py` | Register `get_nearest_store` for `recommendations` and `vendor_inquiry` agents |
| `fashion_bot/schema.py` | Add `user_location: Optional[Dict]` to `SupportState` |
| `fashion_bot/static/chat-widget-frame.html` | Request geolocation on open; attach `userLocation` to WS payloads |
| `fashion_bot/websocket_chat.py` | Parse `userLocation` from WS data → `state["user_location"]` |
| `fashion_bot/nodes/generic_skill_node.py` | Inject nearest-store context block |

## Future Work

- Pincode-to-lat/lng static lookup table for better WhatsApp georesolution.
- Multiple-store carousel for clients with many locations.

## AGENTS.md Compliance

| Principle | Status |
|---|---|
| Full async (#1) | Tool is `async def`, no sync I/O |
| Stateless tools (#2) | No state mutation; reads `client_id` + `user_location` only |
| Three-tier cache (#3) | `aget_all_stores` reads via `aget_config` (memory → Redis → DB) |
| Trace ID (#5) | Tool returns structured dicts; logging in node uses existing `log_with_trace_id` |
| Tenant isolation (#7) | All lookups scoped by `client_id` |
| Shared utils | Logic in `utils/store_locations.py`, imported by tool_factory and skill node |
| Minimal footprint | New module for new logic, thin imports into hot files |
| Idempotent | Pure function, same input → same output |
| Error handling | Specific exceptions, graceful fallback dict on failure |
| Widget checklist | Geolocation is read-only data attachment to existing WS payload; no socket/queue/response changes |
