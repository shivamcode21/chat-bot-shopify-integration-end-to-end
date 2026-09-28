# EcommAgents API

The primary FastAPI application is `fashion_bot/fashion_bot/main.py`.

## Start the API

~~~bash
cd ecommagents/fashion_bot
python -m fashion_bot.main
~~~

Default development URL: http://localhost:8000.

## Health

### GET /health

~~~bash
curl http://localhost:8000/health
~~~

Response:
~~~json
{"status":"healthy","service":"fashion_bot"}
~~~

## Support response

### POST /support-response

~~~json
{"product":"Example product","question":"Where is my order?","thread_id":"demo-thread"}
~~~

The handler invokes the configured LangGraph runtime and returns the final assistant message.

## WebSocket chat

### WS /ws/chat/{client_id}/{session_id}

The application exposes a WebSocket chat runtime for the embedded widget. See the websocket module and static widget test pages for the current message contract.

## Messaging webhooks

The application includes WhatsApp and Gupshup router families under `/whatsapp` and `/gupshup`. Exact endpoints and authentication requirements are integration-specific.

## Demo plugin endpoints

- POST /demo/chat
- GET /demo/fetch-products
- GET /demo/fetch-collections
- DELETE /demo/cache

See the demo plugin README for request/response examples.

## Production note

Do not expose debug or integration endpoints publicly without authentication and network controls. Configure CORS to trusted origins rather than the permissive development setting currently present in main.py.